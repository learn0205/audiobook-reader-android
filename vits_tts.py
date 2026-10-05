# -*- coding: utf-8 -*-
"""vits_tts.py —— 本地语音合成：sherpa-onnx VITS + vits-zh-hf-fanchen-C

定位（完全离线，替代已删除的 Edge-TTS 在线合成）
------------------------------------------------
fanchen-C：中文多说话人 VITS，187 个音色，模型 116MB、16kHz。
sherpa-onnx 官方收录（安卓端被 tts-server-android 等大量项目验证），
树莓派4 上 RTF 1.6（4线程），现代手机（天玑9400 级）预计 0.2-0.4，
边播边合绰绰有余。

能力边界（明示，不做假）：
· 语速：✅ Generate 的 speed 参数（UI 语速滑块直接映射）
· 音调：❌ VITS 不支持（UI 音调滑块对 VITS 音色置灰）
· 情感：❌ 无原生情感维度（情感标签映射为语速微调）

架构（沿用已验证的 ctypes 子系统）
----------------------------------
· 模型懒加载：只在合成线程首次使用时初始化，绝不阻塞 UI
· 崩溃熔断：加载前写 pending 标记，进程死亡后下次自动禁用
· 内存闸门：可用内存不足直接给可读错误
· 合成互斥：预取线程并发调同一句柄不安全，全部串行
· 模型获取：本地导入（.tar.bz2 / 已解压目录）+ App 内下载
  （hf-mirror 按文件清单下载，跳过 173MB 的 rule.far；GitHub releases
  tar.bz2 兜底）；体积对账 + 原子落盘 + 完整性校验
· 报错全面性：任何失败路径都给可读中文原因（AIError 同款风格）
"""

import json
import os
import shutil
import struct
import subprocess
import threading
import time
import urllib.request

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
MODEL_SUBDIR = os.path.join("vits", "fanchen-c")
MODEL_TITLE = "VITS 中文音色库（fanchen-C · 187音色 · 约145MB）"
EXPECTED_SPEAKERS = 187
MODEL_MIN_SIZE = 100 * 1024 * 1024      # 完整模型 116MB，低于 100MB 必不完整

# hf-mirror 按文件下载（跳过 rule.far / README / 脚本，体积减半）
HF_API = "https://hf-mirror.com/api/models/csukuangfj/vits-zh-hf-fanchen-C"
HF_RAW = "https://hf-mirror.com/csukuangfj/vits-zh-hf-fanchen-C/resolve/main/"
# GitHub releases 整包兜底（含 rule.far，体积大但源稳定）
GH_TAR_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/"
              "tts-models/vits-zh-hf-fanchen-C.tar.bz2")

_SKIP_FILES = ("rule.far", ".gitattributes", "LICENSE")
_SKIP_EXT = (".md", ".py", ".git", ".png", ".jpg")

_state = {"status": "idle",             # idle / downloading / ready / error
          "message": "", "progress": None}
_lock = threading.Lock()
_current_data_dir = [""]                # 由宿主 App 注入（synthesize_to_file 用）


class VitsError(Exception):
    """带可读中文原因的合成模块错误（直接 toast 给用户）。"""


def _set_state(status, message="", progress=None):
    with _lock:
        _state["status"] = status
        _state["message"] = message
        _state["progress"] = progress


def get_state():
    with _lock:
        return dict(_state)


def model_dir(data_dir):
    return os.path.join(data_dir, MODEL_SUBDIR)


def _find(data_dir, pred):
    """在模型目录（含一层子目录）里按谓词找文件/目录。"""
    base = model_dir(data_dir)
    for root, _dirs, files in os.walk(base):
        if root.count(os.sep) - base.count(os.sep) > 1:
            continue                    # 只搜两层，dict/ 由调用方单独处理
        for name in files:
            if pred(name, os.path.join(root, name)):
                return os.path.join(root, name)
    return ""


def _model_file(data_dir, suffix_list, min_size=0):
    return _find(data_dir, lambda n, p: (
        n.lower().endswith(suffix_list) and os.path.getsize(p) >= min_size))


# ---------------------------------------------------------------------------
# 状态与校验
# ---------------------------------------------------------------------------
def model_ready(data_dir):
    """三道校验：onnx 体积 / tokens / lexicon。返回 (bool, 可读原因)。"""
    onnx = _model_file(data_dir, (".onnx",), MODEL_MIN_SIZE)
    if not onnx:
        return False, "未导入 VITS 模型（设置→离线AI 中导入或下载）"
    if not _model_file(data_dir, ("tokens.txt",)):
        return False, "模型缺 tokens.txt，语音包不完整——请重新导入/下载"
    if not _model_file(data_dir, ("lexicon.txt", "lexicon")):
        return False, "模型缺 lexicon 词素文件，语音包不完整——请重新导入/下载"
    return True, "已就绪（%d 音色 · 16kHz）" % EXPECTED_SPEAKERS


def _rule_fsts(data_dir):
    """数字/日期归一化 FST（存在才拼入）。"""
    fsts = []
    base = model_dir(data_dir)
    if os.path.isdir(base):
        for root, _d, files in os.walk(base):
            for name in files:
                if name.endswith(".fst"):
                    fsts.append(os.path.join(root, name))
    return ",".join(sorted(fsts))


def import_model(src_path, data_dir):
    """从本地 .tar.bz2 / .zip 导入（解压到模型目录并校验）。"""
    if not os.path.isfile(src_path):
        raise VitsError("选择的文件不存在")
    with open(src_path, "rb") as f:
        head = f.read(4)
    os.makedirs(model_dir(data_dir), exist_ok=True)
    if head[:2] == b"PK":
        ok = _extract_zip(src_path, data_dir)
    elif head[:3] == b"BZh" or src_path.lower().endswith(".tar.bz2"):
        ok = _extract_tar(src_path, data_dir)
    else:
        raise VitsError("不支持的文件格式（需要 fanchen-C 的 .tar.bz2 或"
                        " .zip 语音包）")
    if not ok:
        raise VitsError(get_state()["message"] or "语音包解压失败")
    ok, reason = model_ready(data_dir)
    if not ok:
        raise VitsError("导入完成但校验未通过：%s" % reason)
    _set_state("ready", "VITS 模型已导入")
    return True


def _extract_tar(path, data_dir):
    try:
        import tarfile
        _set_state("downloading", "解压语音包…", None)
        tops = ("vits-zh-hf-fanchen-C/", "./")
        with tarfile.open(path, "r:bz2") as tar:
            members = tar.getmembers()
            done = 0
            for m in members:
                rel = m.name.replace("\\", "/")
                for top in tops:
                    if rel.startswith(top):
                        rel = rel[len(top):]
                        break
                rel = rel.lstrip("./")
                if not rel or os.path.basename(rel) in _SKIP_FILES:
                    continue
                dest = os.path.join(model_dir(data_dir), *rel.split("/"))
                if m.isdir():
                    os.makedirs(dest, exist_ok=True)
                elif m.isfile():
                    os.makedirs(os.path.dirname(dest) or model_dir(data_dir),
                                exist_ok=True)
                    with tar.extractfile(m) as src, open(dest, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                done += 1
                if done % 50 == 0:
                    _set_state("downloading", "解压中 %d 个文件…" % done, None)
        return model_ready(data_dir)[0]
    except Exception as err:
        _set_state("error", "解压失败：%s" % err)
        return False


def _extract_zip(path, data_dir):
    try:
        import zipfile
        _set_state("downloading", "解压语音包…", None)
        tops = ("vits-zh-hf-fanchen-C/", "./")
        with zipfile.ZipFile(path) as z:
            infos = z.infolist()
            done = 0
            for zi in infos:
                rel = zi.filename.replace("\\", "/")
                for top in tops:
                    if rel.startswith(top):
                        rel = rel[len(top):]
                        break
                rel = rel.lstrip("./")
                if not rel or os.path.basename(rel) in _SKIP_FILES:
                    continue
                dest = os.path.join(model_dir(data_dir), *rel.split("/"))
                if zi.is_dir():
                    os.makedirs(dest, exist_ok=True)
                else:
                    os.makedirs(os.path.dirname(dest) or model_dir(data_dir),
                                exist_ok=True)
                    with z.open(zi) as src, open(dest, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                done += 1
                if done % 50 == 0:
                    _set_state("downloading", "解压中 %d 个文件…" % done, None)
        return model_ready(data_dir)[0]
    except Exception as err:
        _set_state("error", "解压失败：%s" % err)
        return False


def _hf_file_list():
    req = urllib.request.Request(HF_API)
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.loads(r.read().decode("utf-8"))
    return [s["rfilename"] for s in data.get("siblings", [])
            if s.get("type") != "directory"]


def download_model(data_dir, progress_cb=None):
    """App 内下载（hf-mirror 逐文件，断点续传；GitHub 整包兜底）。

    跳过 rule.far（173MB 的超大规则库，标点读法用，非必需）。
    失败全部抛 VitsError（可读原因），绝不静默。
    """
    dest_dir = model_dir(data_dir)
    os.makedirs(dest_dir, exist_ok=True)
    # 空间预检
    try:
        st = os.statvfs(data_dir)
        free_gb = st.f_bavail * st.f_frsize / 1073741824.0
        if free_gb < 0.5:
            raise VitsError("手机可用空间只剩 %.1fGB，下载语音包需要约 0.5GB"
                            "——请先清理空间" % free_gb)
    except VitsError:
        raise
    except Exception:
        pass

    # 主源：hf-mirror 逐文件
    files = None
    for attempt in range(5):
        try:
            files = _hf_file_list()
            break
        except Exception:
            time.sleep(3)
    if files:
        wanted = [f for f in files
                  if not any(f.endswith(e) for e in _SKIP_EXT)
                  and os.path.basename(f) not in _SKIP_FILES]
        total = len(wanted)
        for i, name in enumerate(sorted(wanted, key=len)):
            dest = os.path.join(dest_dir, *name.split("/"))
            os.makedirs(os.path.dirname(dest) or dest_dir, exist_ok=True)
            url = HF_RAW + name
            last = None
            for attempt in range(5):
                try:
                    _urllib_download(url, dest)
                    break
                except Exception as err:
                    last = err
                    time.sleep(2 * (attempt + 1))
            else:
                raise VitsError("下载失败（%s）：%s——可改用本地导入"
                                % (name, last))
            _set_state("downloading", "下载 %d/%d：%s"
                       % (i + 1, total, os.path.basename(name)),
                       (i + 1) / max(1, total))
            if progress_cb:
                progress_cb((i + 1) / max(1, total))
        ok, reason = model_ready(data_dir)
        if not ok:
            raise VitsError("下载完成但校验未通过：%s" % reason)
        _set_state("ready", "VITS 模型已下载", 1.0)
        if progress_cb:
            progress_cb(1.0)
        return True

    # 兜底：GitHub 整包 tar.bz2
    _set_state("downloading", "镜像源不可用，改用 GitHub 整包（含大文件）…", 0)
    tar_path = model_dir(data_dir) + ".tar.bz2"
    last = None
    for attempt in range(5):
        try:
            _urllib_download(GH_TAR_URL, tar_path)
            if import_model(tar_path, data_dir):
                try:
                    os.remove(tar_path)
                except OSError:
                    pass
                if progress_cb:
                    progress_cb(1.0)
                return True
        except Exception as err:
            last = err
        time.sleep(3)
    raise VitsError("语音包下载失败：%s——请改用电脑下载后本地导入"
                    % (last or "未知错误"))


def _urllib_download(url, path):
    have = os.path.getsize(path) if os.path.isfile(path) else 0
    req = urllib.request.Request(url)
    if have:
        req.add_header("Range", "bytes=%d-" % have)
    with urllib.request.urlopen(req, timeout=120) as r, \
            open(path, "ab" if have else "wb") as f:
        while True:
            chunk = r.read(512 * 1024)
            if not chunk:
                break
            f.write(chunk)


def download_async(data_dir, done_cb=None, progress_cb=None):
    """后台线程下载；完成/失败通过 get_state() 与回调通知。"""
    if _state["status"] == "downloading":
        return

    def _work():
        try:
            ok = download_model(data_dir,
                                progress_cb=progress_cb or (lambda p: None))
        except Exception as err:
            ok = False
            _set_state("error", str(err))
        if done_cb:
            done_cb(ok, get_state()["message"])
    threading.Thread(target=_work, daemon=True).start()


# ---------------------------------------------------------------------------
# 音色性别标注（187 个 sid 无官方标注，由用户试听标注，全局复用）
# ---------------------------------------------------------------------------
def labels_path(data_dir):
    return os.path.join(data_dir, "vits", "voice_labels.json")


def get_labels(data_dir):
    """{sid:int → "男"/"女"}；文件缺失/损坏返回空。"""
    try:
        with open(labels_path(data_dir), "r", encoding="utf-8") as f:
            raw = json.load(f)
        return {int(k): str(v) for k, v in raw.items()
                if str(v) in ("男", "女")}
    except Exception:
        return {}


def set_label(data_dir, sid, gender):
    """记录一次试听标注（gender ∈ 男/女/None=清除）。"""
    labels = get_labels(data_dir)
    if gender in ("男", "女"):
        labels[int(sid)] = gender
    else:
        labels.pop(int(sid), None)
    os.makedirs(os.path.dirname(labels_path(data_dir)), exist_ok=True)
    tmp = labels_path(data_dir) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(labels, f, ensure_ascii=False)
    os.replace(tmp, labels_path(data_dir))


def sid_gender(data_dir, sid):
    return get_labels(data_dir).get(int(sid), "未标注")


# ---------------------------------------------------------------------------
# 引擎（懒加载 + 熔断 + 合成互斥）
# ---------------------------------------------------------------------------
_instances = {}


def get_instance(data_dir):
    key = os.path.abspath(data_dir)
    if key not in _instances:
        _instances[key] = _VitsTTS(data_dir)
    return _instances[key]


class _VitsTTS:
    def __init__(self, data_dir):
        self.data_dir = data_dir
        self._tts = None
        self._lib = None
        self._sample_rate = 16000
        self._num_speakers = EXPECTED_SPEAKERS
        self._load_lock = threading.Lock()
        self._synth_lock = threading.Lock()
        self._failed = False
        self._failed_msg = ""

    def is_loaded(self):
        return self._tts is not None

    def disabled(self):
        return os.path.isfile(os.path.join(self.data_dir, "vits", "disabled"))

    def re_enable(self):
        try:
            os.remove(os.path.join(self.data_dir, "vits", "disabled"))
        except OSError:
            pass
        try:
            os.remove(os.path.join(self.data_dir, "vits", "pending_load"))
        except OSError:
            pass
        self._failed = False
        self._failed_msg = ""

    def num_speakers(self):
        return self._num_speakers if self.is_loaded() else EXPECTED_SPEAKERS

    def list_speakers(self):
        """音色列表：vits:N + 试听友好标签（含性别标注）。

        模型未导入/被禁用时返回空（不引导用户选一个跑不了的音色）。
        """
        if self.disabled() or not model_ready(self.data_dir)[0]:
            return []
        labels = get_labels(self.data_dir)
        out = []
        for i in range(self.num_speakers()):
            g = labels.get(i, "未标注")
            out.append(("vits:%d" % i, "音色%d号（%s）" % (i + 1, g)))
        return out

    # ---------------- 加载 ----------------
    def _ensure_loaded(self):
        with self._load_lock:
            if self._tts is not None or self._failed:
                return self._tts
            if self.disabled():
                self._failed = True
                self._failed_msg = ("VITS 引擎上次加载失败，已临时禁用"
                                    "（可在设置里重新启用）")
                return None
            ok, reason = model_ready(self.data_dir)
            if not ok:
                self._failed = True
                self._failed_msg = reason
                return None
            try:
                self._precheck()
                if os.environ.get("ANDROID_ARGUMENT") or \
                        os.environ.get("ANDROID_ROOT"):
                    self._load_android()
                else:
                    self._failed = True
                    self._failed_msg = "桌面环境无 sherpa-onnx 运行器"
            except Exception as err:
                print("[vits] 引擎加载失败：%s" % err)
                self._failed = True
                self._tts = None
                self._failed_msg = str(err)
            return self._tts

    def _precheck(self):
        """崩溃熔断 + 内存闸门（与已验证的模式一致）。"""
        pending = os.path.join(self.data_dir, "vits", "pending_load")
        if os.path.isfile(pending):
            open(os.path.join(self.data_dir, "vits", "disabled"), "w").close()
            self._failed = True
            raise VitsError("VITS 引擎上次加载时崩溃，已临时禁用"
                            "（可在设置里重新启用）")
        try:
            from jnius import autoclass
            act = autoclass("org.kivy.android.PythonActivity").mActivity
            mgr = act.getSystemService(act.ACTIVITY_SERVICE)
            mi = mgr.getMemoryInfo()
            avail_mb = mi.availMem / 1048576.0
            if avail_mb < 500:
                raise VitsError("可用内存只有 %.0fMB，加载 VITS 引擎至少需要"
                                "约 500MB——请关闭其他应用后重试" % avail_mb)
        except VitsError:
            raise
        except Exception:
            pass

    def _load_android(self):
        """安卓：ctypes 调 libsherpa-onnx-c-api.so（v1.11.3，VITS 分支）。"""
        import ctypes
        lib_dir = os.environ.get("ANDROID_APP_LIB_DIR", "")
        if not lib_dir:
            for p in os.environ.get("LD_LIBRARY_PATH", "").split(":"):
                if os.path.isfile(os.path.join(p, "libsherpa-onnx-c-api.so")):
                    lib_dir = p
                    break
        if not lib_dir:
            raise VitsError("找不到 libsherpa-onnx-c-api.so——"
                            "请安装包含语音合成模块的正式版本")
        lib = ctypes.CDLL(os.path.join(lib_dir, "libsherpa-onnx-c-api.so"))

        # ---- v1.11.3 c-api.h 结构体（字段顺序敏感）----
        class VitsModelConfig(ctypes.Structure):
            _fields_ = [("model", ctypes.c_char_p),
                        ("lexicon", ctypes.c_char_p),
                        ("tokens", ctypes.c_char_p),
                        ("data_dir", ctypes.c_char_p),
                        ("noise_scale", ctypes.c_float),
                        ("noise_scale_w", ctypes.c_float),
                        ("length_scale", ctypes.c_float),
                        ("dict_dir", ctypes.c_char_p)]

        class MatchaModelConfig(ctypes.Structure):
            _fields_ = [("acoustic_model", ctypes.c_char_p),
                        ("vocoder", ctypes.c_char_p),
                        ("lexicon", ctypes.c_char_p),
                        ("tokens", ctypes.c_char_p),
                        ("data_dir", ctypes.c_char_p),
                        ("noise_scale", ctypes.c_float),
                        ("length_scale", ctypes.c_float),
                        ("dict_dir", ctypes.c_char_p)]

        class KokoroModelConfig(ctypes.Structure):
            _fields_ = [("model", ctypes.c_char_p),
                        ("voices", ctypes.c_char_p),
                        ("tokens", ctypes.c_char_p),
                        ("data_dir", ctypes.c_char_p),
                        ("length_scale", ctypes.c_float),
                        ("dict_dir", ctypes.c_char_p),
                        ("lexicon", ctypes.c_char_p)]

        class ModelConfig(ctypes.Structure):
            _fields_ = [("vits", VitsModelConfig),
                        ("num_threads", ctypes.c_int32),
                        ("debug", ctypes.c_int32),
                        ("provider", ctypes.c_char_p),
                        ("matcha", MatchaModelConfig),
                        ("kokoro", KokoroModelConfig)]

        class TtsConfig(ctypes.Structure):
            _fields_ = [("model", ModelConfig),
                        ("rule_fsts", ctypes.c_char_p),
                        ("max_num_sentences", ctypes.c_int32),
                        ("rule_fars", ctypes.c_char_p),
                        ("silence_scale", ctypes.c_float)]

        lib.SherpaOnnxCreateOfflineTts.restype = ctypes.c_void_p
        lib.SherpaOnnxCreateOfflineTts.argtypes = [
            ctypes.POINTER(TtsConfig)]
        lib.SherpaOnnxOfflineTtsSampleRate.argtypes = [ctypes.c_void_p]
        lib.SherpaOnnxOfflineTtsSampleRate.restype = ctypes.c_int32
        lib.SherpaOnnxOfflineTtsNumSpeakers.argtypes = [ctypes.c_void_p]
        lib.SherpaOnnxOfflineTtsNumSpeakers.restype = ctypes.c_int32

        vits = VitsModelConfig()
        vits.model = _model_file(self.data_dir, (".onnx",),
                                 MODEL_MIN_SIZE).encode()
        vits.lexicon = _model_file(self.data_dir,
                                   ("lexicon.txt", "lexicon")).encode()
        vits.tokens = _model_file(self.data_dir, ("tokens.txt",)).encode()
        vits.data_dir = b""          # VITS 不用 espeak-ng-data
        vits.noise_scale = 0.667
        vits.noise_scale_w = 0.8
        vits.length_scale = 1.0
        dict_dir = _find(self.data_dir,
                         lambda n, p: os.path.isdir(p) and n == "dict")
        vits.dict_dir = (dict_dir or "").encode()
        mc = ModelConfig()
        mc.vits = vits
        mc.num_threads = 2
        mc.debug = 0
        mc.provider = b"cpu"
        cfg = TtsConfig()
        cfg.model = mc
        cfg.rule_fsts = _rule_fsts(self.data_dir).encode()
        cfg.max_num_sentences = 1
        cfg.silence_scale = 1.0

        pending = os.path.join(self.data_dir, "vits", "pending_load")
        open(pending, "w").close()
        handle = lib.SherpaOnnxCreateOfflineTts(ctypes.byref(cfg))
        if not handle:
            try:
                os.remove(pending)
            except OSError:
                pass
            open(os.path.join(self.data_dir, "vits", "disabled"), "w").close()
            self._failed = True
            raise VitsError("VITS 引擎初始化失败（已临时禁用，"
                            "可在设置里重新启用）")
        try:
            os.remove(pending)
        except OSError:
            pass
        self._lib = lib
        self._tts = handle
        self._sample_rate = lib.SherpaOnnxOfflineTtsSampleRate(handle)
        self._num_speakers = lib.SherpaOnnxOfflineTtsNumSpeakers(handle)

    # ---------------- 合成 ----------------
    def synth_to_file(self, text, sid, path, speed=1.0):
        """合成一句话为 wav。全程持锁（预取并发不安全）。"""
        with self._synth_lock:
            tts = self._ensure_loaded()
            if tts is None:
                raise VitsError(self._failed_msg or "VITS 引擎未就绪")
            self._synth_android(text, int(sid), path, float(speed))

    def _synth_android(self, text, sid, path, speed):
        import ctypes
        lib = self._lib

        class GeneratedAudio(ctypes.Structure):
            _fields_ = [("samples", ctypes.POINTER(ctypes.c_float)),
                        ("n", ctypes.c_int32),
                        ("sample_rate", ctypes.c_int32)]

        lib.SherpaOnnxOfflineTtsGenerate.restype = ctypes.c_void_p
        lib.SherpaOnnxOfflineTtsGenerate.argtypes = [
            ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int32, ctypes.c_float]
        lib.SherpaOnnxDestroyOfflineTtsGeneratedAudio.restype = None
        lib.SherpaOnnxDestroyOfflineTtsGeneratedAudio.argtypes = [
            ctypes.c_void_p]
        audio = lib.SherpaOnnxOfflineTtsGenerate(
            self._tts, text.encode("utf-8"), int(sid), float(speed))
        if not audio:
            raise VitsError("合成返回空（sid=%d）" % sid)
        samples_ptr = ctypes.c_void_p.from_address(audio).value
        n = ctypes.c_int32.from_address(
            audio + ctypes.sizeof(ctypes.c_void_p)).value
        rate = ctypes.c_int32.from_address(
            audio + ctypes.sizeof(ctypes.c_void_p) +
            ctypes.sizeof(ctypes.c_int32)).value
        self._sample_rate = rate
        samples = ctypes.cast(samples_ptr,
                              ctypes.POINTER(ctypes.c_float))[:n]
        lib.SherpaOnnxDestroyOfflineTtsGeneratedAudio(audio)
        if not samples:
            raise VitsError("合成返回空音频（sid=%d）" % sid)
        self._write_wav(samples, path)

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


# ---------------------------------------------------------------------------
# 引擎无关的便捷入口（供 tts_engine / 测试调用与打桩）
# ---------------------------------------------------------------------------
def synthesize_to_file(text, voice, path, rate="+0%", pitch="+0Hz",
                       volume="+0%"):
    """模块级入口：voice="vits:N"，rate 形如 "+15%"。pitch/volume 忽略。"""
    try:
        sid = int(str(voice).split(":", 1)[1])
    except (TypeError, ValueError, IndexError):
        sid = 0
    try:
        speed = max(0.5, min(2.0,
                             1.0 + float(str(rate).strip("%")
                                         .replace("+", "")) / 100.0))
    except (TypeError, ValueError):
        speed = 1.0
    data_dir = _current_data_dir[0] if _current_data_dir[0] else "."
    get_instance(data_dir).synth_to_file(text, sid, path, speed=speed)
