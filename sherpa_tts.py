# -*- coding: utf-8 -*-
"""sherpa_tts.py —— 离线语音引擎：sherpa-onnx + Kokoro-82M（开源音源）

为什么需要它
------------
Edge 在线 TTS 免费好用但音色有限（zh-CN 二十几个）且必须联网。
Kokoro-82M（Apache-2.0，hexgrad/kokoro）是开源 TTS 模型，
kokoro-multi-lang-v1_1 支持中英双语、103 个说话人——一次下载后
**完全离线**朗读，且可以给每个角色绑定不同的说话人。

架构（复用现有播放链路）
------------------------
本模块只负责「文本 → wav 文件」这一步合成；播放/推进/暂停/预取
全部复用 tts_engine.EdgeTTS 现有逻辑 —— 引擎侧通过音色名前缀路由：
  voice == "kokoro:<说话人编号>"  →  本模块合成
  其他                            →  Edge 在线合成
缓存键包含音色名，因此离线/在线、不同说话人的同一句话互不冲突。

运行形态
--------
· 桌面（验证用）：pip install sherpa-onnx，直接用 Python API；
· 安卓：sherpa-onnx 官方预编译 libsherpa-onnx-c-api.so 随 APK 打包，
  用 ctypes 调 C API（不依赖 jnius/Kotlin），模型目录由应用内下载。

模型文件（kokoro-multi-lang-v1_1）
----------------------------------
  model.onnx / voices.bin / tokens.txt / lexicon-zh-jss.fst? /
  espeak-ng-data/ / dict/（jieba 分词词典）
  全部位于 self.model_dir（应用私有目录 sherpa/kokoro/）。
"""

import os
import shutil
import struct
import subprocess
import threading
import time
import ctypes
import urllib.request


class SherpaOnnxGeneratedAudio(ctypes.Structure):
    """v1.11.3: { const float *samples; int32_t n; int32_t sample_rate; }"""
    _fields_ = [("samples", ctypes.POINTER(ctypes.c_float)),
                ("n", ctypes.c_int32),
                ("sample_rate", ctypes.c_int32)]

# 模型下载源（按顺序尝试；国内网络 GitHub 时好时坏，HF 镜像兜底）
GITHUB_URL = ("https://github.com/learn0205/audiobook-reader-android/"
              "releases/download/voicepack-v1/kokoro-multi-lang-v1_1.zip")
HF_MIRROR_URL = ("https://hf-mirror.com/csukuangfj/kokoro-multi-lang-v1_1/"
                 "resolve/main/")
HF_FILE_LIST = ("https://hf-mirror.com/api/models/"
                "csukuangfj/kokoro-multi-lang-v1_1")

MODEL_DIR_NAME = os.path.join("sherpa", "kokoro")
REQUIRED_FILES = ("model.onnx", "voices.bin", "tokens.txt")

_lock = threading.Lock()
_state = {
    "status": "idle",          # idle / downloading / ready / error
    "progress": 0.0,           # 0~1（仅 GitHub 单文件下载时可算）
    "message": "",
}


def set_state(status, message="", progress=None):
    _state["status"] = status
    _state["message"] = message
    if progress is not None:
        _state["progress"] = progress


def get_state():
    return dict(_state)


def _crumb(data_dir, msg):
    """加载面包屑：追加写入 sherpa/load.log，native 崩溃后仍能定位死在哪步。"""
    try:
        p = os.path.join(data_dir, "sherpa", "load.log")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(time.strftime("[%H:%M:%S] ") + msg + "\n")
    except Exception:
        pass


class SherpaTTS:
    """Kokoro 离线合成器（进程内单例，懒加载）。"""

    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.model_dir = os.path.join(data_dir, MODEL_DIR_NAME)
        self._tts = None            # 桌面 Python 对象 / 安卓 ctypes 句柄
        self._sample_rate = 24000
        self._num_speakers = 0
        self._load_lock = threading.Lock()
        # 原生合成互斥锁：预取会并发开多个线程调同一个引擎句柄的
        # Generate，sherpa-onnx 不承诺该调用线程安全（内部共享分词/
        # jieba 状态），并发调用是随机原生崩溃的来源 → 全部串行化
        self._synth_lock = threading.Lock()
        self._failed = False        # 加载失败后不再反复尝试
        self._failed_msg = ""       # 可读的失败原因（透传给合成错误提示）

    # ---------------- 状态 ----------------
    def model_ready(self):
        """模型文件是否齐全（不代表引擎已加载）。"""
        return all(os.path.isfile(os.path.join(self.model_dir, f))
                   for f in REQUIRED_FILES)

    def is_loaded(self):
        return self._tts is not None

    # ---------------- 模型下载 ----------------
    def download(self, progress_cb=None):
        """下载并解压模型（阻塞，建议放线程里调）。

        策略：① GitHub 单文件 tar.bz2（带断点续传重试）；
              ② 失败则从 HF 镜像逐文件下载（国内网络稳定）。
        """
        set_state("downloading", "开始下载语音包…", 0.0)
        os.makedirs(self.model_dir, exist_ok=True)
        tar_path = self.model_dir + ".tar.bz2"
        ok = False
        try:
            ok = self._download_github(tar_path, progress_cb)
            if ok and self._extract(tar_path):
                self._on_pack_complete()
                return True
            # GitHub 失败 → HF 镜像逐文件
            set_state("downloading", "GitHub 下载失败，改用镜像源…", None)
            ok = self._download_hf(progress_cb)
            if ok:
                self._on_pack_complete()
            return ok
        except Exception as err:
            set_state("error", "下载失败：%s" % err)
            return False

    def _on_pack_complete(self):
        """语音包就绪（新下载/重新导入）→ 清掉旧的熔断与崩溃标记，
        允许引擎重新加载（之前因包不完整被禁用的场景）。"""
        self.re_enable()
        self._failed = False
        self._failed_msg = ""
        try:
            os.remove(os.path.join(self.data_dir, "sherpa", "pending_load"))
        except OSError:
            pass

    def _download_github(self, tar_path, progress_cb):
        """curl 断点续传下载 tar.bz2（安卓上没有 curl 时退回 urllib）。"""
        total = self._remote_size_github()
        for attempt in range(8):
            try:
                if shutil.which("curl"):
                    rc = subprocess.call(
                        ["curl", "-L", "--retry", "3", "--retry-all-errors",
                         "-C", "-", "-o", tar_path, GITHUB_URL],
                        timeout=3600)
                    if rc != 0:
                        continue
                else:
                    self._urllib_download(GITHUB_URL, tar_path, total,
                                          progress_cb)
                if os.path.isfile(tar_path) and \
                        os.path.getsize(tar_path) > 100 * 1024 * 1024:
                    return True      # zip 完整性由 _extract_zip 校验
                    return True
            except Exception:
                time.sleep(3)
        return False

    def _remote_size_github(self):
        try:
            req = urllib.request.Request(GITHUB_URL, method="HEAD")
            with urllib.request.urlopen(req, timeout=20) as r:
                return int(r.headers.get("Content-Length", 0))
        except Exception:
            return 0

    def _urllib_download(self, url, path, total, progress_cb):
        have = os.path.getsize(path) if os.path.isfile(path) else 0
        req = urllib.request.Request(url)
        if have:
            req.add_header("Range", "bytes=%d-" % have)
        with urllib.request.urlopen(req, timeout=60) as r, \
                open(path, "ab" if have else "wb") as f:
            got = have
            while True:
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
                if total and progress_cb:
                    progress_cb(min(1.0, got / total))

    def _hf_file_list(self):
        import json
        req = urllib.request.Request(HF_FILE_LIST)
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8"))
        return [s["rfilename"] for s in data.get("siblings", [])
                if s.get("type") != "directory"]

    def _download_hf(self, progress_cb):
        """HF 镜像逐文件下载（model.onnx 最大，放最后显示进度）。"""
        try:
            files = self._hf_file_list()
        except Exception as err:
            set_state("error", "获取文件列表失败：%s" % err)
            return False
        files = [f for f in files if f != ".gitattributes"]
        total = len(files)
        for i, name in enumerate(sorted(files, key=len)):
            dest = os.path.join(self.model_dir, name)
            os.makedirs(os.path.dirname(dest) or self.model_dir, exist_ok=True)
            url = HF_MIRROR_URL + name
            for attempt in range(4):
                try:
                    self._urllib_download(url, dest, 0, None)
                    break
                except Exception:
                    time.sleep(2)
            else:
                set_state("error", "镜像下载失败：%s" % name)
                return False
            set_state("downloading", "镜像下载 %d/%d" % (i + 1, total),
                      (i + 1) / total)
            if progress_cb:
                progress_cb((i + 1) / total)
        return self.model_ready()

    # ---------------- 本地导入（推荐：QQ/网盘传到手机后从本地导入） ----------------
    def import_from_file(self, src_path, progress_cb=None):
        """从本地 .tar.bz2 导入语音包（手机上从 QQ 接收/下载目录选）。"""
        if not os.path.isfile(src_path):
            set_state("error", "文件不存在")
            return False
        set_state("downloading", "复制语音包…", 0.0)
        os.makedirs(self.model_dir, exist_ok=True)
        ext = ".zip" if src_path.lower().endswith(".zip") else ".tar.bz2"
        tar_path = self.model_dir + ext
        try:
            total = os.path.getsize(src_path)
            copied = 0
            with open(src_path, "rb") as src, open(tar_path, "wb") as dst:
                while True:
                    chunk = src.read(1024 * 512)
                    if not chunk:
                        break
                    dst.write(chunk)
                    copied += len(chunk)
                    if progress_cb and total:
                        progress_cb(min(1.0, copied / total) * 0.5)
            ok = self._extract(tar_path, progress_cb)
            if ok:
                self._on_pack_complete()
            if ok and progress_cb:
                progress_cb(1.0)
            return ok
        except Exception as err:
            set_state("error", "导入失败：%s" % err)
            return False

    def _detect_archive_format(self, path):
        """按文件头嗅探压缩格式（扩展名不可信——导入/下载的临时文件名
        可能与真实格式不符，这正是手机上 bz2 报错的根源）。"""
        try:
            with open(path, "rb") as f:
                head = f.read(4)
        except OSError:
            head = b""
        if head[:2] == b"PK":
            return "zip"
        if head[:3] == b"BZh":
            return "tar.bz2"
        return "zip" if path.lower().endswith(".zip") else "tar.bz2"

    def _extract(self, tar_path, progress_cb=None):
        """解压 tar.bz2 到模型目录，保留相对子目录结构。

        压缩包内顶层是 kokoro-multi-lang-v1_1/，其下的 espeak-ng-data/、
        dict/ 等**多级子目录必须原样保留**（早期实现用 basename 平铺，
        把 phontab/词典文件全写丢在根目录，导致合成报错）。
        """
        # 按文件头嗅探格式（扩展名不可信：导入/下载的临时文件名可能不符）
        fmt = self._detect_archive_format(tar_path)
        if fmt == "zip":
            return self._extract_zip(tar_path, progress_cb)
        try:
            import tarfile
            set_state("downloading", "解压语音包…", None)
            tops = ("kokoro-multi-lang-v1_1/", "./")
            with tarfile.open(tar_path, "r:bz2") as tar:
                members = tar.getmembers()
                done = 0
                for member in members:
                    rel = member.name.replace("\\", "/")
                    for top in tops:
                        if rel.startswith(top):
                            rel = rel[len(top):]
                            break
                    rel = rel.lstrip("./")
                    if not rel:
                        continue
                    dest = os.path.join(self.model_dir, *rel.split("/"))
                    if member.isdir() or rel.endswith("/"):
                        os.makedirs(dest, exist_ok=True)
                    elif member.isfile():
                        os.makedirs(os.path.dirname(dest) or self.model_dir,
                                    exist_ok=True)
                        with tar.extractfile(member) as src,                                 open(dest, "wb") as dst:
                            shutil.copyfileobj(src, dst)
                    done += 1
                    if progress_cb and done % 60 == 0:
                        progress_cb(0.5 + 0.5 * done / len(members))
            try:
                os.remove(tar_path)
            except OSError:
                pass
            ready = self.model_ready()
            if ready:
                set_state("ready", "离线语音包就绪")
            else:
                set_state("error", "解压后缺少模型文件")
            return ready
        except Exception as err:
            set_state("error", "解压失败：%s" % err)
            return False

    def _extract_zip(self, zip_path, progress_cb=None):
        """解压 zip 到模型目录（安卓没有 bz2 模块，zip 格式是唯一选择）。"""
        try:
            import zipfile
            set_state("downloading", "解压语音包…", None)
            tops = ("kokoro-multi-lang-v1_1/", "./")
            with zipfile.ZipFile(zip_path) as z:
                infos = z.infolist()
                done = 0
                for zi in infos:
                    rel = zi.filename.replace("\\", "/")
                    for top in tops:
                        if rel.startswith(top):
                            rel = rel[len(top):]
                            break
                    rel = rel.lstrip("./")
                    if not rel:
                        continue
                    dest = os.path.join(self.model_dir, *rel.split("/"))
                    if zi.is_dir():
                        os.makedirs(dest, exist_ok=True)
                    else:
                        os.makedirs(os.path.dirname(dest) or self.model_dir,
                                    exist_ok=True)
                        with z.open(zi) as src, open(dest, "wb") as dst:
                            shutil.copyfileobj(src, dst)
                    done += 1
                    if progress_cb and done % 60 == 0:
                        progress_cb(0.5 + 0.5 * done / len(infos))
            try:
                os.remove(zip_path)
            except OSError:
                pass
            ready = self.model_ready()
            if ready:
                set_state("ready", "离线语音包就绪")
            else:
                set_state("error", "解压后缺少模型文件")
            return ready
        except Exception as err:
            set_state("error", "解压失败：%s" % err)
            return False

    def _ensure_loaded(self):
        """懒加载模型（失败置 _failed，之后直接走降级）。"""
        with self._load_lock:
            if self._tts is not None or self._failed:
                return self._tts
            # 熔断已触发（上次加载失败/崩溃被禁用）→ 直接给可读错误，
            # 绝不再碰原生库（设置里「重新启用」会清掉 disabled 再试）
            if self.disabled():
                self._failed = True
                self._failed_msg = ("离线引擎上次加载失败，已临时禁用"
                                    "（可在设置里重新启用）")
                return None
            if not self.model_ready():
                self._failed = True
                self._failed_msg = "离线语音包未下载或未导入"
                return None
            try:
                self._precheck_model()   # 不完整 → 可读错误（双平台共用）
                if os.environ.get("ANDROID_ARGUMENT") or \
                        os.environ.get("ANDROID_ROOT"):
                    self._load_android()
                else:
                    self._load_desktop()
            except Exception as err:
                print("[sherpa] 离线引擎加载失败：%s" % err)
                self._failed = True
                self._tts = None
                self._failed_msg = str(err)
            return self._tts

    def _precheck_model(self):
        """完整性预检：导入时存储中断可能留下截断的文件，损坏的
        model/voices 会让原生初始化直接崩溃（且无法捕获）——先拦下，
        给出可读错误而不是闪退。桌面基准：model≈310MB、voices≈51MB、
        espeak≈355 个文件。"""
        mb = os.path.getsize(self._data_sub("model.onnx")) / 1048576.0
        vk = os.path.getsize(self._data_sub("voices.bin")) / 1024.0
        e_dir = self._data_sub("espeak-ng-data")
        e_cnt = sum(len(fs) for _, _, fs in os.walk(e_dir)) if e_dir else 0
        _crumb(self.data_dir,
               "文件: model=%.1fMB voices=%.0fKB espeak=%d"
               % (mb, vk, e_cnt))
        if mb < 250:
            _crumb(self.data_dir, "model.onnx 不完整")
            raise RuntimeError(
                "model.onnx 只有 %.0fMB（应约310MB），语音包不完整——"
                "请重新导入语音包" % mb)
        if vk < 30:
            _crumb(self.data_dir, "voices.bin 不完整")
            raise RuntimeError("voices.bin 不完整，请重新导入语音包")
        if e_cnt < 300:
            _crumb(self.data_dir, "espeak-ng-data 不完整(%d)" % e_cnt)
            raise RuntimeError(
                "espeak-ng-data 只解出 %d 个文件（应355个），语音包不完整"
                "——请重新导入语音包" % e_cnt)

    def _data_sub(self, *names):
        """在模型目录及其一级子目录里找文件/目录（兼容压缩包内层级差异）。"""
        for name in names:
            p = os.path.join(self.model_dir, name)
            if os.path.exists(p):
                return p
            p2 = os.path.join(self.model_dir, "kokoro-multi-lang-v1_1", name)
            if os.path.exists(p2):
                return p2
        return ""

    def _rule_fsts(self):
        """数字/日期归一化 FST（听书文本里的阿拉伯数字、日期必需）。"""
        fsts = [self._data_sub(f) for f in
                ("date-zh.fst", "number-zh.fst", "phone-zh.fst")]
        return ",".join(f for f in fsts if f)

    def _lexicon_files(self):
        """lexicon 词素文件列表（逗号分隔，sherpa-onnx 支持多文件）。

        ⚠️ 关键：kokoro 多语模型（v1.0/v1.1）在 v1.11.3 的实现里
        **强制要求 lexicon 非空**——offline-tts-kokoro-impl.h 会直接抛
        C++ 异常「please pass --kokoro-lexicon and --kokoro-dict-dir」，
        而 v1.11.3 的 SherpaOnnxCreateOfflineTts **没有 try/catch**，
        未捕获异常 → std::terminate → abort → 整个 App 进程闪退。
        安卓端此前把 lexicon 传成空字符串，正是「选离线音色必崩、
        崩后熔断禁用、重新启用又崩」的死循环根源。
        中文小说朗读主用 lexicon-zh.txt；英文词再补 lexicon-us-en.txt
        （混排文本里的人名/单词发音更准），按实际存在与否拼接。
        """
        files = [self._data_sub(f) for f in
                 ("lexicon-zh.txt", "lexicon-us-en.txt", "lexicon-gb-en.txt")]
        return ",".join(f for f in files if f)

    def _load_desktop(self):
        import sherpa_onnx
        kokoro = sherpa_onnx.OfflineTtsKokoroModelConfig(
            model=self._data_sub("model.onnx"),
            voices=self._data_sub("voices.bin"),
            tokens=self._data_sub("tokens.txt"),
            data_dir=self._data_sub("espeak-ng-data"),
            dict_dir=self._data_sub("dict"),
            lexicon=self._lexicon_files(),
        )
        config = sherpa_onnx.OfflineTtsConfig(
            model=sherpa_onnx.OfflineTtsModelConfig(
                kokoro=kokoro, num_threads=2, debug=0, provider="cpu"),
            rule_fsts=self._rule_fsts(),
        )
        tts = sherpa_onnx.OfflineTts(config)
        self._tts = tts
        self._sample_rate = tts.sample_rate
        self._num_speakers = tts.num_speakers
        print("[sherpa] Kokoro 离线引擎就绪：说话人 %d 个，采样率 %d"
              % (self._num_speakers, self._sample_rate))

    def _load_android(self):
        """安卓：ctypes 调官方预编译 libsherpa-onnx-c-api.so（v1.11.3）。

        结构体字段顺序必须与 v1.11.3 的 c-api.h 完全一致（ABI 按版本锁定，
        CI 下载对应版本的 .so 随 APK 打包，python 侧用 ctypes 直调 C API，
        不依赖 jnius/Kotlin）。
        """
        import ctypes
        lib_dir = os.environ.get("ANDROID_APP_LIB_DIR", "")
        if not lib_dir:
            for p in os.environ.get("LD_LIBRARY_PATH", "").split(":"):
                if os.path.isfile(os.path.join(p, "libsherpa-onnx-c-api.so")):
                    lib_dir = p
                    break
        if not lib_dir:
            _crumb(self.data_dir, "找不到 libsherpa-onnx-c-api.so")
            raise RuntimeError("找不到 libsherpa-onnx-c-api.so（v1.11.3）")
        _crumb(self.data_dir, "加载 .so: " + lib_dir)
        lib = ctypes.CDLL(os.path.join(lib_dir, "libsherpa-onnx-c-api.so"))
        _crumb(self.data_dir, "CDLL ok")
        try:
            from jnius import autoclass
            Build = autoclass("android.os.Build")
            ver = autoclass("android.os.Build$VERSION")
            _crumb(self.data_dir, "设备: %s | API%d | %s | RAM见上"
                   % (str(Build.MODEL)[:24], int(ver.SDK_INT),
                      str(Build.SUPPORTED_ABIS[0])[:16]))
        except Exception:
            pass
        try:
            st = os.statvfs(self.data_dir)
            free_gb = st.f_bavail * st.f_frsize / 1073741824.0
            _crumb(self.data_dir, "磁盘剩余 %.1fGB" % free_gb)
        except Exception:
            pass
        # espeak 关键文件（初始化时会读取，截断→abort）
        try:
            e_dir = self._data_sub("espeak-ng-data")
            for key in ("phontab", "phondata", "phonindex", "intonations"):
                p = os.path.join(e_dir, key)
                sz = os.path.getsize(p) if os.path.isfile(p) else -1
                _crumb(self.data_dir, "espeak/%s=%d" % (key, sz))
        except Exception:
            pass
        # rule_fsts 存在性
        try:
            _crumb(self.data_dir, "rule_fsts=%s" % ("Y" if self._rule_fsts() else "N"))
        except Exception:
            pass

        # ---- v1.11.3 c-api.h 结构体（字段顺序敏感）----
        class SherpaOnnxOfflineTtsVitsModelConfig(ctypes.Structure):
            _fields_ = [("model", ctypes.c_char_p),
                        ("lexicon", ctypes.c_char_p),
                        ("tokens", ctypes.c_char_p),
                        ("data_dir", ctypes.c_char_p),
                        ("noise_scale", ctypes.c_float),
                        ("noise_scale_w", ctypes.c_float),
                        ("length_scale", ctypes.c_float),
                        ("dict_dir", ctypes.c_char_p)]

        class SherpaOnnxOfflineTtsMatchaModelConfig(ctypes.Structure):
            _fields_ = [("acoustic_model", ctypes.c_char_p),
                        ("vocoder", ctypes.c_char_p),
                        ("lexicon", ctypes.c_char_p),
                        ("tokens", ctypes.c_char_p),
                        ("data_dir", ctypes.c_char_p),
                        ("noise_scale", ctypes.c_float),
                        ("length_scale", ctypes.c_float),
                        ("dict_dir", ctypes.c_char_p)]

        class SherpaOnnxOfflineTtsKokoroModelConfig(ctypes.Structure):
            _fields_ = [("model", ctypes.c_char_p),
                        ("voices", ctypes.c_char_p),
                        ("tokens", ctypes.c_char_p),
                        ("data_dir", ctypes.c_char_p),
                        ("length_scale", ctypes.c_float),
                        ("dict_dir", ctypes.c_char_p),
                        ("lexicon", ctypes.c_char_p)]

        class SherpaOnnxOfflineTtsModelConfig(ctypes.Structure):
            _fields_ = [("vits", SherpaOnnxOfflineTtsVitsModelConfig),
                        ("num_threads", ctypes.c_int32),
                        ("debug", ctypes.c_int32),
                        ("provider", ctypes.c_char_p),
                        ("matcha", SherpaOnnxOfflineTtsMatchaModelConfig),
                        ("kokoro", SherpaOnnxOfflineTtsKokoroModelConfig)]

        class SherpaOnnxOfflineTtsConfig(ctypes.Structure):
            _fields_ = [("model", SherpaOnnxOfflineTtsModelConfig),
                        ("rule_fsts", ctypes.c_char_p),
                        ("max_num_sentences", ctypes.c_int32),
                        ("rule_fars", ctypes.c_char_p),
                        ("silence_scale", ctypes.c_float)]

        lib.SherpaOnnxCreateOfflineTts.restype = ctypes.c_void_p
        lib.SherpaOnnxCreateOfflineTts.argtypes = [ctypes.POINTER(
            SherpaOnnxOfflineTtsConfig)]
        lib.SherpaOnnxOfflineTtsSampleRate.argtypes = [ctypes.c_void_p]
        lib.SherpaOnnxOfflineTtsSampleRate.restype = ctypes.c_int32
        lib.SherpaOnnxOfflineTtsNumSpeakers.argtypes = [ctypes.c_void_p]
        lib.SherpaOnnxOfflineTtsNumSpeakers.restype = ctypes.c_int32

        vits_cfg = SherpaOnnxOfflineTtsVitsModelConfig()
        matcha_cfg = SherpaOnnxOfflineTtsMatchaModelConfig()
        kokoro_cfg = SherpaOnnxOfflineTtsKokoroModelConfig()
        kokoro_cfg.model = self._data_sub("model.onnx").encode()
        kokoro_cfg.voices = self._data_sub("voices.bin").encode()
        kokoro_cfg.tokens = self._data_sub("tokens.txt").encode()
        kokoro_cfg.data_dir = self._data_sub("espeak-ng-data").encode()
        kokoro_cfg.length_scale = 1.0
        kokoro_cfg.dict_dir = self._data_sub("dict").encode()
        # ⚠️ lexicon 绝不能为空：多语模型实现里 lexicon 为空会抛未捕获的
        # C++ 异常 → 整个进程 abort（见 _lexicon_files 的说明）
        kokoro_cfg.lexicon = self._lexicon_files().encode()
        model_cfg = SherpaOnnxOfflineTtsModelConfig()
        model_cfg.vits = vits_cfg
        model_cfg.matcha = matcha_cfg
        model_cfg.kokoro = kokoro_cfg
        model_cfg.num_threads = 1
        model_cfg.debug = 0
        model_cfg.provider = b"cpu"
        cfg = SherpaOnnxOfflineTtsConfig()
        cfg.model = model_cfg
        cfg.rule_fsts = self._rule_fsts().encode()
        cfg.max_num_sentences = 1
        cfg.silence_scale = 1.0
        # 完整性预检已在 _ensure_loaded 里做过（双平台共用）
        # 崩溃熔断：上次 create 原生崩溃（pending 标记还在）→ 本次禁用离线，
        # 避免「选离线音色→崩溃→再选→再崩溃」的死循环
        pending = os.path.join(self.data_dir, "sherpa", "pending_load")
        if os.path.isfile(pending):
            _crumb(self.data_dir, "检测到上次加载原生崩溃 → 本次跳过并禁用")
            open(os.path.join(self.data_dir, "sherpa", "disabled"), "w").close()
            self._failed = True
            raise RuntimeError(
                "离线引擎上次加载时崩溃，已临时禁用（可在设置里重新启用）")
        # 内存诊断 + 内存闸门：加载 310MB 模型至少要 ~600MB 可用内存，
        # 不够时给出可读错误而不是赌 OOM（进程被系统杀 = 整个 App 闪退）
        try:
            from jnius import autoclass
            act = autoclass("org.kivy.android.PythonActivity").mActivity
            mgr = act.getSystemService(act.ACTIVITY_SERVICE)
            mi = mgr.getMemoryInfo()
            avail_mb = mi.availMem / 1048576.0
            _crumb(self.data_dir,
                   "内存: avail=%.0fMB total=%.0fMB"
                   % (avail_mb, mi.totalMem / 1048576.0))
            if avail_mb < 600:
                _crumb(self.data_dir, "可用内存不足，放弃加载")
                raise RuntimeError(
                    "可用内存只有 %.0fMB，加载离线引擎至少需要约 600MB"
                    "——请关闭其他应用后重试" % avail_mb)
        except RuntimeError:
            raise
        except Exception:
            pass
        open(pending, "w").close()
        _crumb(self.data_dir, "SherpaOnnxCreateOfflineTts 开始（若此后无日志即为原生崩溃）")
        handle = lib.SherpaOnnxCreateOfflineTts(ctypes.byref(cfg))
        if not handle:
            # 返回空 ≠ 原生崩溃：是配置/文件问题被引擎拒绝（有异常被
            # 引擎日志记录）。清掉 pending 免得下次误判成「原生崩溃」，
            # 但仍写入 disabled 熔断，避免反复空跑几十秒的加载流程。
            _crumb(self.data_dir, "create 返回空（配置被拒绝，非崩溃）")
            try:
                os.remove(os.path.join(self.data_dir, "sherpa", "pending_load"))
            except OSError:
                pass
            open(os.path.join(self.data_dir, "sherpa", "disabled"), "w").close()
            self._failed = True
            raise RuntimeError(
                "sherpa-onnx 初始化失败（引擎拒绝配置，已临时禁用离线，"
                "可在设置里重新启用）")
        _crumb(self.data_dir, "create ok")
        try:
            os.remove(os.path.join(self.data_dir, "sherpa", "pending_load"))
        except OSError:
            pass
        self._lib = lib
        self._tts = handle
        self._sample_rate = lib.SherpaOnnxOfflineTtsSampleRate(handle)
        self._num_speakers = lib.SherpaOnnxOfflineTtsNumSpeakers(handle)
        _crumb(self.data_dir, "加载完成 speakers=%d rate=%d"
               % (self._num_speakers, self._sample_rate))
        print("[sherpa] Kokoro 离线引擎就绪（安卓 v1.11.3）：说话人 %d"
              % self._num_speakers)

    # ---------------- 合成 ----------------
    # kokoro-multi-lang-v1_1 文档值：103 个说话人。
    # 音色列表只用这个常量，**绝不**为它触发模型加载 ——
    # 此前 list_speakers() → _ensure_loaded() 会在主线程同步初始化
    # onnxruntime（几十秒 ANR + 数百MB 内存），导致导入后/启动时
    # App 必卡死闪退。模型只在合成线程里真正懒加载。
    EXPECTED_SPEAKERS = 103

    def disabled(self):
        return os.path.isfile(os.path.join(self.data_dir, "sherpa", "disabled"))

    def re_enable(self):
        try:
            os.remove(os.path.join(self.data_dir, "sherpa", "disabled"))
        except OSError:
            pass
        self._failed = False

    def num_speakers(self):
        if self.is_loaded():
            return self._num_speakers
        return self.EXPECTED_SPEAKERS

    def synth_to_file(self, text, sid, path, speed=1.0):
        """合成一句话并写成 wav（MediaPlayer 可直接播放）。

        sid 为 Kokoro 说话人编号（int）。抛异常由调用方降级处理。
        全程持 _synth_lock：预取线程与当前句合成线程共用一个原生
        引擎句柄，必须串行（见 __init__ 的说明）。
        """
        with self._synth_lock:
            tts = self._ensure_loaded()
            if tts is None:
                raise RuntimeError(self._failed_msg or "离线引擎未就绪")
            if os.environ.get("ANDROID_ARGUMENT") or \
                    os.environ.get("ANDROID_ROOT"):
                self._synth_android(text, sid, path, speed)
            else:
                self._synth_desktop(text, sid, path, speed)

    def _write_wav(self, samples, path):
        import array
        import wave
        pcm = array.array("h")
        for s in samples:
            v = int(s * 32767)
            pcm.append(max(-32768, min(32767, v)))
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(self._sample_rate)
            w.writeframes(pcm.tobytes())

    def _synth_desktop(self, text, sid, path, speed):
        audio = self._tts.generate(text, sid=sid, speed=speed)
        if audio is None or len(audio.samples) == 0:
            raise RuntimeError("离线合成返回空音频")
        self._sample_rate = audio.sample_rate
        self._write_wav(audio.samples, path)

    def _synth_android(self, text, sid, path, speed):
        lib = self._lib
        _crumb(self.data_dir, "合成: sid=%d len=%d speed=%.2f"
               % (sid, len(text), speed))
        lib.SherpaOnnxOfflineTtsGenerate.restype = ctypes.c_void_p
        lib.SherpaOnnxOfflineTtsGenerate.argtypes = [
            ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int32, ctypes.c_float]
        # ⚠️ 销毁函数收的是指针句柄（Generate 的 c_void_p 返回值是 int）。
        # 之前 argtypes 写成了 POINTER(SherpaOnnxGeneratedAudio)，ctypes 在
        # 调用前直接抛 TypeError —— 而这行发生在「样本已读出、wav 未写」
        # 的节点，等于**每次合成都在最后一步失败**，全部回退在线音色。
        lib.SherpaOnnxDestroyOfflineTtsGeneratedAudio.restype = None
        lib.SherpaOnnxDestroyOfflineTtsGeneratedAudio.argtypes = [
            ctypes.c_void_p]
        audio = lib.SherpaOnnxOfflineTtsGenerate(
            self._tts, text.encode("utf-8"), int(sid), float(speed))
        if not audio:
            raise RuntimeError("离线合成返回空")
        # SherpaOnnxGeneratedAudio { const float *samples; int32 n; int32 sample_rate; }
        audio_t = SherpaOnnxGeneratedAudio
        samples_ptr = ctypes.c_void_p.from_address(audio).value
        n = ctypes.c_int32.from_address(audio + ctypes.sizeof(
            ctypes.c_void_p)).value
        rate = ctypes.c_int32.from_address(audio + ctypes.sizeof(
            ctypes.c_void_p) + ctypes.sizeof(ctypes.c_int32)).value
        self._sample_rate = rate
        samples = ctypes.cast(samples_ptr, ctypes.POINTER(ctypes.c_float))[:n]
        lib.SherpaOnnxDestroyOfflineTtsGeneratedAudio(audio)
        _crumb(self.data_dir, "合成完成: %d样本 %dHz" % (n, rate))
        if not samples:
            raise RuntimeError("离线合成返回空音频")
        self._write_wav(samples, path)

    def list_speakers(self):
        """供音色列表展示：离线1..N（含说话人名与性别/语言门类）。

        名单来自 voice_template.KOKORO_SPEAKERS（voices.bin 的固定 sid
        顺序），只做展示格式化，不触发模型加载。
        """
        if self.disabled():
            return []
        try:
            from voice_template import kokoro_label
        except Exception:
            kokoro_label = None
        n = self.num_speakers()
        out = []
        for i in range(n):
            label = ("离线%d号（Kokoro）" % (i + 1) if kokoro_label is None
                     else kokoro_label(i))
            out.append(("kokoro:%d" % i, label))
        return out


# ---------------- 进程级单例管理 ----------------
_instances = {}


def get_instance(data_dir):
    key = os.path.abspath(data_dir)
    if key not in _instances:
        _instances[key] = SherpaTTS(data_dir)
    return _instances[key]


def download_async(data_dir, done_cb=None, progress_cb=None):
    """后台线程下载语音包；完成/失败通过 get_state() 查询。"""
    inst = get_instance(data_dir)
    if _state["status"] == "downloading":
        return

    def _work():
        def _prog(p):
            if progress_cb:
                progress_cb(p)
        ok = inst.download(_prog)
        if done_cb:
            done_cb(ok, get_state()["message"])

    threading.Thread(target=_work, daemon=True).start()
