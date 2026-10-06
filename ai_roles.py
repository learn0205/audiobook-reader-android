# -*- coding: utf-8 -*-
"""ai_roles.py —— 离线 AI 角色分析：llama.cpp 子进程 + Qwen2.5-1.5B (GGUF)

定位
----
role_parser 的正则规则判断人物性别/年龄段有天花板（人名本身无线索时
只能靠称谓词）。本模块把识别到的角色 + 每人几句对话摘录喂给本地小模型，
让它读上下文推断性别/年龄段，再按现有「门类 → 模板编号 → 音色」链路
自动重排配音。**每本书只在用户点「AI 精细识别」时跑一次**，不在播放
路径上，规则法始终是兜底。

架构与报错设计（要点：不允许静默失败）
----------------------------------------
· 运行器：llama.cpp 交叉编译出的 llama-cli，以 libllama-runner.so 名义
  随 APK 打包（Android 只解压 lib*.so），运行时从 nativeLibraryDir 以
  **独立子进程**执行 —— 模型吃内存/崩溃只死子进程，朗读 App 无恙。
  二进制缺失 / 模型缺失 / 文件损坏 / 内存不足 / 超时 / 崩溃 / 解析失败
  全部抛 AIError（带可读中文原因），由调用方 toast + 记入诊断。
· 模型：Qwen2.5-1.5B-Instruct Q4_K_M（约1.1GB），放应用私有目录 ai/。
  支持本地导入（电脑下载后传手机）与 App 内 hf-mirror 断点续传下载。
  校验三道闸：GGUF 魔数、最小体积、（下载时）远端 Content-Length 对账。
"""

import json
import os
import shutil
import subprocess
import threading
import time
import urllib.request

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
MODEL_DIR_NAME = os.path.join("ai")
MODEL_FILE = "qwen2.5-1.5b-instruct-q4_k_m.gguf"
MODEL_URL = ("https://hf-mirror.com/Qwen/Qwen2.5-1.5B-Instruct-GGUF/"
             "resolve/main/" + MODEL_FILE)
MODEL_LABEL = "Qwen2.5-1.5B（Q4 量化，约1.1GB）"

GGUF_MAGIC = b"GGUF"
MODEL_MIN_SIZE = 500 * 1024 * 1024      # 完整模型约1.1GB，低于500MB必不完整
RUNNER_LIB = "libllama-runner.so"
ANALYZE_TIMEOUT_S = 900                 # 子进程硬超时（15分钟）
RAM_MIN_MB = 2500                       # 跑 1.5B 至少要 ~1.5GB，留余量
# ⚠️ Errno 7 (E2BIG) 防线：Linux 单个 argv 参数上限 128KB（MAX_ARG_STRLEN），
# 提示词曾以 -p 整段塞进命令行，角色多时超限 → exec 直接被系统拒绝。
# 现在提示词一律写临时文件用 -f 传入；这里再做一层截断保险（60K 字符
# 远低于 128KB 上限，超出说明角色/样本异常多，截断不影响主体）。
MAX_PROMPT_CHARS = 60000

GEN_TIMEOUT = 800                       # 生成的最大新 token 数
CTX_SIZE = 8192                         # 上下文窗口（提示 ~3K + 输出 800 足够）

_state = {"status": "idle",             # idle / downloading / ready / error
          "message": "", "progress": None}
_lock = threading.Lock()


class AIError(Exception):
    """带可读中文原因的 AI 模块错误（直接 toast 给用户）。"""


def _set_state(status, message="", progress=None):
    with _lock:
        _state["status"] = status
        _state["message"] = message
        _state["progress"] = progress


def get_state():
    with _lock:
        return dict(_state)


# ---------------------------------------------------------------------------
# 模型文件管理（导入 / 下载 / 校验）
# ---------------------------------------------------------------------------
def model_path(data_dir):
    return os.path.join(data_dir, MODEL_DIR_NAME, MODEL_FILE)


def model_ready(data_dir):
    """模型是否可用。返回 (bool, 可读原因)。三道校验：存在/体积/魔数。"""
    path = model_path(data_dir)
    if not os.path.isfile(path):
        return False, "未导入 AI 模型（设置→AI 模型 中导入或下载）"
    try:
        size = os.path.getsize(path)
    except OSError as err:
        return False, "模型文件不可读：%s" % err
    if size < MODEL_MIN_SIZE:
        return False, ("模型文件只有 %.0fMB（完整约1.1GB），不完整——"
                       "请重新导入或下载" % (size / 1048576.0))
    try:
        with open(path, "rb") as f:
            magic = f.read(4)
    except OSError as err:
        return False, "模型文件不可读：%s" % err
    if magic != GGUF_MAGIC:
        return False, ("选中的文件不是有效的 GGUF 模型"
                       "（文件头不对）——请确认下载的是 "
                       "qwen2.5-1.5b-instruct-q4_k_m.gguf")
    return True, "已就绪"


def import_model(src_path, data_dir):
    """从本地 .gguf 导入：复制 → 校验（魔数+体积）→ 原子落盘。

    校验失败会删掉临时文件并抛 AIError，绝不把坏文件留在正式路径上
    （否则之后每次分析都莫名失败，用户无从排查）。
    """
    if not os.path.isfile(src_path):
        raise AIError("选择的文件不存在")
    src_size = os.path.getsize(src_path)
    if src_size < MODEL_MIN_SIZE:
        raise AIError("文件只有 %.0fMB，比最小的完整模型还小——"
                      "请确认选择的是完整模型文件（约1.1GB）"
                      % (src_size / 1048576.0))
    try:
        with open(src_path, "rb") as f:
            magic = f.read(4)
    except OSError as err:
        raise AIError("文件不可读：%s" % err)
    if magic != GGUF_MAGIC:
        raise AIError("文件不是 GGUF 模型（文件头不对）——请下载 "
                      "qwen2.5-1.5b-instruct-q4_k_m.gguf")
    dest_dir = os.path.join(data_dir, MODEL_DIR_NAME)
    os.makedirs(dest_dir, exist_ok=True)
    dest = model_path(data_dir)
    tmp = dest + ".importing"
    try:
        with open(src_path, "rb") as src, open(tmp, "wb") as dst:
            shutil.copyfileobj(src, dst, 1024 * 512)
        os.replace(tmp, dest)
    except Exception as err:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise AIError("复制模型失败（存储空间不足？需要约1.2GB可用）："
                      "%s" % err)
    _set_state("ready", "AI 模型已导入")
    return True


def _remote_model_size():
    req = urllib.request.Request(MODEL_URL, method="HEAD")
    with urllib.request.urlopen(req, timeout=60) as r:
        return int(r.headers.get("Content-Length", 0))


def download_model(data_dir, progress_cb=None):
    """App 内下载模型（hf-mirror，断点续传，最多重试 8 次）。

    全部失败路径都抛 AIError：空间不足 / 列表拉取失败 / 多次重试后仍
    失败 / 下载完校验不过。进度 0~1 经 progress_cb 上报。
    """
    dest_dir = os.path.join(data_dir, MODEL_DIR_NAME)
    part = model_path(data_dir) + ".part"
    os.makedirs(dest_dir, exist_ok=True)

    # 存储空间预检：模型 1.1GB，留 1.5GB 余量
    try:
        st = os.statvfs(data_dir)
        free_gb = st.f_bavail * st.f_frsize / 1073741824.0
        if free_gb < 1.5:
            raise AIError("手机可用空间只剩 %.1fGB，下载模型需要约 1.5GB"
                          "——请先清理空间" % free_gb)
    except AIError:
        raise
    except Exception:
        pass                                    # 查不到空间就跳过预检

    _set_state("downloading", "连接下载源…", 0.0)
    total = 0
    for attempt in range(8):
        try:
            total = _remote_model_size()
            break
        except Exception as err:
            if attempt == 7:
                raise AIError("无法连接下载源（hf-mirror）：%s" % err)
            time.sleep(3)

    last_err = None
    for attempt in range(8):
        try:
            have = os.path.getsize(part) if os.path.isfile(part) else 0
            if total and have >= total and os.path.isfile(part):
                break                           # 上次已下完，只剩校验
            req = urllib.request.Request(MODEL_URL)
            if have:
                req.add_header("Range", "bytes=%d-" % have)
            with urllib.request.urlopen(req, timeout=120) as r, \
                    open(part, "ab" if have else "wb") as f:
                got = have
                last_tick = 0.0
                while True:
                    chunk = r.read(512 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
                    now = time.time()
                    if total and now - last_tick > 1.0:
                        last_tick = now
                        _set_state("downloading",
                                   "下载中 %.0f/%.0fMB"
                                   % (got / 1048576.0, total / 1048576.0),
                                   got / total)
                        if progress_cb:
                            progress_cb(min(1.0, got / total))
            if total and os.path.getsize(part) < total:
                raise IOError("连接中断（%d/%d）"
                              % (os.path.getsize(part), total))
            break
        except AIError:
            raise
        except Exception as err:
            last_err = err
            _set_state("downloading", "重试中（第%d次）…" % (attempt + 2), None)
            time.sleep(3 * (attempt + 1))
    else:
        raise AIError("模型下载多次失败：%s（可改用电脑下载后 App 内导入）"
                      % last_err)

    # 校验：体积对账 + GGUF 魔数
    size = os.path.getsize(part)
    if total and abs(size - total) > 1024 * 1024:
        try:
            os.remove(part)
        except OSError:
            pass
        raise AIError("下载的文件大小与远端不符（%d/%d），已清除——请重试"
                      % (size, total))
    with open(part, "rb") as f:
        if f.read(4) != GGUF_MAGIC:
            try:
                os.remove(part)
            except OSError:
                pass
            raise AIError("下载内容不是有效的 GGUF 模型，已清除——请重试")
    os.replace(part, model_path(data_dir))
    _set_state("ready", "AI 模型已下载", 1.0)
    if progress_cb:
        progress_cb(1.0)
    return True


# ---------------------------------------------------------------------------
# 运行器（llama.cpp 子进程）
# ---------------------------------------------------------------------------
def find_runner():
    """定位 libllama-runner.so（随 APK 打包在 nativeLibraryDir）。

    桌面无此目录 → 明确报错（桌面仅支持提示词/解析的单元测试）。
    """
    try:
        from jnius import autoclass
        ctx = autoclass("org.kivy.android.PythonActivity").mActivity
        native_dir = str(ctx.getApplicationInfo().nativeLibraryDir)
    except Exception:
        raise AIError("桌面环境不支持运行 AI 分析（仅安卓设备上可用）")
    path = os.path.join(native_dir, RUNNER_LIB)
    if not os.path.isfile(path):
        raise AIError("找不到 AI 运行器（%s）——请安装包含 AI 模块的正式版本"
                      % RUNNER_LIB)
    return path


def check_ram():
    """内存闸门：返回 (ok, 可读原因)。安卓上查 availMem；桌面放行。"""
    try:
        from jnius import autoclass
        act = autoclass("org.kivy.android.PythonActivity").mActivity
        mgr = act.getSystemService(act.ACTIVITY_SERVICE)
        mi = mgr.getMemoryInfo()
        avail_mb = mi.availMem / 1048576.0
        if avail_mb < RAM_MIN_MB:
            return False, ("可用内存只有 %.0fMB，AI 分析至少需要约 %dMB"
                           "——请关闭其他应用后重试"
                           % (avail_mb, RAM_MIN_MB))
        return True, ""
    except Exception:
        return True, ""


# ---------------------------------------------------------------------------
# 提示词与解析（纯函数，可单元测试）
# ---------------------------------------------------------------------------
def build_prompt(characters):
    """characters: [(name, guess_category, [对话片段...])] → 提示词文本。"""
    lines = ["你是小说人物分析师。下面是一部小说中出现的人物，以及每个人的"
             "对话片段。括号里的「原猜测」来自简单规则，可能有错，仅供参考。",
             "请根据对话的内容与语言风格，判断每个人物的性别和年龄段。",
             "gender 只能是\"男\"或\"女\"；age 只能是\"少年\"、\"青年\"、"
             "\"中年\"、\"老年\"之一，不确定也要选最可能的。",
             "只输出一个 JSON 数组，不要任何解释、注释或代码块标记。格式：",
             '[{"name":"人物名","gender":"男","age":"青年"}]',
             "",
             "人物清单："]
    for i, (name, guess, samples) in enumerate(characters, 1):
        lines.append("%d. %s（原猜测：%s）" % (i, name, guess or "未知"))
        for s in samples:
            lines.append("   对话：%s" % s[:60])
    return "\n".join(lines)


def prepare_prompt(characters):
    """构建提示词并做超长截断保险（见 MAX_PROMPT_CHARS 的说明）。"""
    prompt = build_prompt(characters)
    if len(prompt) > MAX_PROMPT_CHARS:
        prompt = prompt[:MAX_PROMPT_CHARS] + "\n（人物过多，以下略）"
    return prompt


def _norm_gender(v):
    v = str(v).strip().lower()
    if v in ("男", "male", "m", "man", "他"):
        return "男"
    if v in ("女", "female", "f", "woman", "她"):
        return "女"
    return ""


def _norm_age(v):
    v = str(v).strip()
    for a in ("少年", "青年", "中年", "老年"):
        if a in v:
            return a
    return ""


def voice_category(gender, age):
    """(性别, 年龄段) → 模板门类（与 voice_template.CATEGORY_ORDER 对齐）。"""
    if gender == "男":
        return "中年叔叔" if age in ("中年", "老年") else "男角色"
    return ("少女" if age == "少年"
            else "奶奶" if age == "老年" else "女角色")


def parse_output(text, characters):
    """从模型输出解析 {name: 门类}。

    两级解析：JSON 数组 → 正则行匹配。两级都失败抛 AIError（带输出
    尾部片段，便于定位是提示词问题还是模型输出跑飞）。解析出来但
    名字对不上的条目忽略；没解析到的人物保持原猜测门类。
    characters: [(name, guess_category, samples)]，用于兜底与对齐。
    """
    names = [c[0] for c in characters]
    result = {}
    body = str(text or "")
    start, end = body.find("["), body.rfind("]")
    if start != -1 and end > start:
        raw = body[start:end + 1]
        try:
            arr = json.loads(raw)
            if isinstance(arr, list):
                for item in arr:
                    if not isinstance(item, dict):
                        continue
                    name = str(item.get("name", "")).strip()
                    if name not in names:
                        continue
                    g = _norm_gender(item.get("gender", ""))
                    a = _norm_age(item.get("age", ""))
                    if g:
                        result[name] = voice_category(g, a)
        except Exception:
            result = {}
    if not result:
        # 兜底：按已知人名逐个就近扫描（比盲正则稳——人名是已知的）
        import re
        for name in names:
            i = body.find(name)
            if i == -1:
                continue
            seg = body[i + len(name): i + len(name) + 40]
            gm = re.search(r"(男|女)", seg)
            if not gm:
                continue
            am = re.search(r"(少年|青年|中年|老年)", seg)
            result[name] = voice_category(
                gm.group(1), am.group(1) if am else "")
    if not result:
        tail = body.strip()[-160:] or "(空)"
        raise AIError("AI 输出无法解析（模型可能没按格式回答），输出尾部：%s"
                      % tail.replace("\n", " "))
    return result


def merge_detected(characters, ai_result):
    """AI 结果 + 原猜测 → [(name, 门类)]（AI 没覆盖到的保持原猜测）。"""
    out = []
    for name, guess, _samples in characters:
        out.append((name, ai_result.get(name, guess)))
    return out


# ---------------------------------------------------------------------------
# 逐句标注（播放路径的 Qwen 协同：正则判不了的代词/连续对话交给它）
# ---------------------------------------------------------------------------
EMOTION_RATE = {"平静": 1.00, "温和": 1.00, "低沉": 0.95,
                "激动": 1.06, "悲伤": 0.92}


def build_annot_prompt(lines, known_roles):
    """lines: [(句id, 句文本)]；known_roles: [人名]。→ 提示词。"""
    role_hint = "、".join(known_roles[:40]) if known_roles else "（暂无）"
    out = [
        "你是小说朗读的台词标注器。下面按行给出小说片段，每行格式：编号|文本。",
        "本书已知角色：%s。" % role_hint,
        "请为每一行判断说话人并给出情感标签：",
        "· speaker：已知角色名之一；叙述/描写行填\"旁白\"；实在无法判断填\"未知\"；",
        "  对话里的\"他说道/她喊道\"要结合上下文指代判断具体是谁；",
        "· emotion 只能是：平静、温和、低沉、激动、悲伤。",
        "只输出 JSON 数组，不要解释：",
        '[{"id":1,"speaker":"张三","emotion":"平静"}]',
        "",
    ]
    for idx, text in lines:
        out.append("%d|%s" % (idx, text[:80]))
    return "\n".join(out)


def parse_annot_output(text, valid_ids):
    """解析逐句标注。返回 {句id: (说话人, 情感)}；完全失败抛 AIError。"""
    body = str(text or "")
    out = {}
    start, end = body.find("["), body.rfind("]")
    if start != -1 and end > start:
        try:
            arr = json.loads(body[start:end + 1])
            if isinstance(arr, list):
                for item in arr:
                    if not isinstance(item, dict):
                        continue
                    try:
                        sid = int(item.get("id"))
                    except (TypeError, ValueError):
                        continue
                    if sid not in valid_ids:
                        continue
                    sp = str(item.get("speaker", "")).strip() or "未知"
                    em = str(item.get("emotion", "")).strip()
                    if em not in EMOTION_RATE:
                        em = "平静"
                    out[sid] = (sp, em)
        except Exception:
            out = {}
    if not out:
        tail = body.strip()[-160:] or "(空)"
        raise AIError("AI 逐句标注无法解析，输出尾部：%s"
                      % tail.replace("\n", " "))
    return out


def annotate_sentences(data_dir, lines, known_roles, timeout_s=None):
    """后台标注一组句子。lines: [(句id, 文本)]。

    返回 {句id: (说话人, 情感)}。失败抛 AIError（可读原因）。
    提示词同样走临时文件（Errno 7 防线），绝不进命令行。
    """
    ok, reason = model_ready(data_dir)
    if not ok:
        raise AIError(reason)
    runner = find_runner()
    prompt = build_annot_prompt(lines, known_roles)
    if len(prompt) > MAX_PROMPT_CHARS:
        # 只保留前一部分句子（块过大的兜底，正常分块不会触发）
        prompt_lines = build_annot_prompt(
            lines[:max(1, len(lines) * MAX_PROMPT_CHARS // max(1, len(prompt)))],
            known_roles)
        prompt = prompt_lines[:MAX_PROMPT_CHARS]
    prompt_file = os.path.join(data_dir, MODEL_DIR_NAME, "annot_prompt.tmp")
    try:
        with open(prompt_file, "w", encoding="utf-8") as f:
            f.write(prompt)
    except OSError as err:
        raise AIError("标注提示词写入失败：%s" % err)
    cmd = [runner, "-m", model_path(data_dir),
           "--single-turn", "--no-display-prompt", "--simple-io",
           "-f", prompt_file, "-n", str(GEN_TIMEOUT), "-c", str(CTX_SIZE),
           "-t", "0.2", "--top-p", "0.8"]
    out_file = os.path.join(data_dir, MODEL_DIR_NAME, "annot_out.tmp")
    err_file = os.path.join(data_dir, MODEL_DIR_NAME, "annot_err.tmp")
    started = time.time()
    timeout_s = timeout_s or ANALYZE_TIMEOUT_S
    stderr_tail = "(无日志)"
    try:
        with open(out_file, "wb") as fo, open(err_file, "wb") as fe:
            proc = subprocess.Popen(cmd, stdout=fo, stderr=fe,
                                    stdin=subprocess.DEVNULL)
            while proc.poll() is None:
                if time.time() - started > timeout_s:
                    proc.kill()
                    raise AIError("AI 标注超时（%.0f秒）" % timeout_s)
                time.sleep(2)
        rc = proc.returncode
    except AIError:
        raise
    except Exception as err:
        raise AIError("无法启动 AI 标注进程：%s" % err)
    finally:
        try:
            with open(err_file, "r", encoding="utf-8",
                      errors="replace") as f:
                stderr_tail = f.read()[-300:]
        except OSError:
            pass
        # stderr 全文留存（自检/导出诊断可查），不随临时文件一起删
        try:
            keep = os.path.join(data_dir, MODEL_DIR_NAME, "last_stderr.log")
            shutil.copyfile(err_file, keep)
        except Exception:
            pass
        try:
            keep = os.path.join(data_dir, MODEL_DIR_NAME, "last_stderr.log")
            shutil.copyfile(err_file, keep)
        except Exception:
            pass
        for stale in (err_file, prompt_file):
            try:
                os.remove(stale)
            except OSError:
                pass
    if rc != 0:
        raise AIError("AI 标注进程异常退出（码%d）：%s"
                      % (rc, stderr_tail.strip()[-400:] or "(无日志)"))
    with open(out_file, "r", encoding="utf-8", errors="replace") as f:
        output = f.read()
    try:
        os.remove(out_file)
    except OSError:
        pass
    return parse_annot_output(output, {i for i, _t in lines})


# ---------------------------------------------------------------------------
# 主入口：跑一次分析
# ---------------------------------------------------------------------------
def analyze(data_dir, characters, progress_cb=None, timeout_s=ANALYZE_TIMEOUT_S):
    """跑一次角色分析，返回 {name: 门类}。

    characters: [(name, guess_category, [对话片段...])]。
    任何失败都抛 AIError（可读原因），绝不静默返回空。
    """
    ok, reason = model_ready(data_dir)
    if not ok:
        raise AIError(reason)
    runner = find_runner()
    ram_ok, ram_msg = check_ram()
    if not ram_ok:
        raise AIError(ram_msg)
    if not characters:
        raise AIError("本书还没有识别到角色，无法分析")

    prompt = prepare_prompt(characters)
    # ⚠️ Errno 7 (E2BIG) 修复：提示词绝不走命令行（单个 argv 上限 128KB，
    # 角色多时必炸），写临时文件用 -f 传入
    prompt_file = os.path.join(data_dir, MODEL_DIR_NAME, "prompt.tmp")
    try:
        with open(prompt_file, "w", encoding="utf-8") as f:
            f.write(prompt)
    except OSError as err:
        raise AIError("提示词临时文件写入失败（存储空间不足？）：%s" % err)
    cmd = [runner, "-m", model_path(data_dir),
           "--single-turn", "--no-display-prompt", "--simple-io",
           "-f", prompt_file, "-n", str(GEN_TIMEOUT), "-c", str(CTX_SIZE),
           "-t", "4", "--temp", "0.2", "--top-p", "0.8"]
    out_file = os.path.join(data_dir, MODEL_DIR_NAME, "ai_out.tmp")
    err_file = os.path.join(data_dir, MODEL_DIR_NAME, "ai_err.tmp")
    started = time.time()
    try:
        with open(out_file, "wb") as fo, open(err_file, "wb") as fe:
            proc = subprocess.Popen(cmd, stdout=fo, stderr=fe,
                                    stdin=subprocess.DEVNULL)
            # 轮询等待：既能报进度，又不会像 communicate 那样卡死在管道
            while proc.poll() is None:
                if time.time() - started > timeout_s:
                    proc.kill()
                    raise AIError("AI 分析超时（超过%.0f秒）——手机负载过高"
                                  "或模型异常，请重试" % timeout_s)
                if progress_cb:
                    progress_cb(time.time() - started)
                time.sleep(3)
        rc = proc.returncode
    except AIError:
        raise
    except Exception as err:
        raise AIError("无法启动 AI 进程：%s" % err)
    finally:
        try:
            with open(err_file, "r", encoding="utf-8",
                      errors="replace") as f:
                stderr_tail = f.read()[-500:]
        except OSError:
            stderr_tail = "(错误日志缺失)"
        for stale in (err_file, prompt_file):
            try:
                os.remove(stale)
            except OSError:
                pass

    if rc != 0:
        with open(out_file, "r", encoding="utf-8", errors="replace") as f:
            out_tail = f.read()[-200:]
        raise AIError("AI 进程异常退出（码%d）：%s %s"
                      % (rc, stderr_tail.strip()[-400:] or "(无错误输出)",
                         out_tail.strip()[-80:]))
    with open(out_file, "r", encoding="utf-8", errors="replace") as f:
        output = f.read()
    try:
        os.remove(out_file)
    except OSError:
        pass
    if not output.strip():
        raise AIError("AI 没有产生输出（%s）：%s"
                      % ("耗时%.0f秒" % (time.time() - started),
                         stderr_tail.strip()[-160:] or "(无日志)"))
    return parse_output(output, characters)
