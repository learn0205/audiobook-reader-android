# -*- coding: utf-8 -*-
"""voice_template.py —— 全局固定音色清单模板

设计要点
--------
· 模板是**全局唯一**的：所有小说共用同一套「编号角色清单」，
  每个编号绑定一个本地 VITS 音色（vits:auto 令牌 = 第 N 个被用户
  标注为男/女的音色；未标注时用内置分散预设，标完即全员生效）。
· 单本小说只保存「人名 ↔ 编号」的映射（见 role_config.py），
  **不**重复保存音色参数 —— 人名的声音参数永远按编号从本模板现查。
  所以模板一改，所有绑定该编号的人物立即同步换声音；
  只有单本书里「单独覆盖了自定义参数」的角色不受影响（那部分参数
  存在 role_config 的人物条目里，优先级高于模板）。
· 模板持久化到应用私有目录 voice_template.json；文件缺失/损坏时
  回退到内置默认清单（内置清单即出厂状态，「恢复默认」也回到它）。

音调/语速的单位与 Edge 对齐：
  · pitch 保留字段但 VITS 忽略；
  · rate 用倍率（1.0 = 正常），由合成引擎按 speed 换算。
"""

import json
import os
import threading

# ---------------------------------------------------------------------------
# 内置默认清单（出厂模板）。可分配的编号按「类别 + 序号」命名：
#   男角色1..8、中年叔叔1..4、女角色1..8、奶奶1..4
# 每类内的多个编号用不同的 Edge 音色/音调区分开，避免一本书里
# 两个角色完全同声。
# ---------------------------------------------------------------------------
DEFAULT_SLOTS = {
    # ---- 年轻/常规男声 ----
    "男角色1": {"voice": "vits:auto:M1", "pitch": 0, "rate": 1.0},
    "男角色2": {"voice": "vits:auto:M2", "pitch": 0, "rate": 1.0},
    "男角色3": {"voice": "vits:auto:M3", "pitch": 0, "rate": 1.0},
    "男角色4": {"voice": "vits:auto:M4", "pitch": 0, "rate": 1.0},
    "男角色5": {"voice": "vits:auto:M5", "pitch": 0, "rate": 1.0},
    "男角色6": {"voice": "vits:auto:M6", "pitch": 0, "rate": 1.0},
    "男角色7": {"voice": "vits:auto:M7", "pitch": 0, "rate": 1.0},
    "男角色8": {"voice": "vits:auto:M8", "pitch": 0, "rate": 1.05},
    # ---- 中年男声（偏低偏慢） ----
    "中年叔叔1": {"voice": "vits:auto:M9", "pitch": 0, "rate": 0.95},
    "中年叔叔2": {"voice": "vits:auto:M10", "pitch": 0, "rate": 0.95},
    "中年叔叔3": {"voice": "vits:auto:M11", "pitch": 0, "rate": 0.92},
    "中年叔叔4": {"voice": "vits:auto:M12", "pitch": 0, "rate": 0.95},
    # ---- 女声 ----
    "女角色1": {"voice": "vits:auto:F1", "pitch": 0, "rate": 1.0},
    "女角色2": {"voice": "vits:auto:F2", "pitch": 0, "rate": 1.0},
    "女角色3": {"voice": "vits:auto:F3", "pitch": 0, "rate": 1.0},
    "女角色4": {"voice": "vits:auto:F4", "pitch": 0, "rate": 1.0},
    "女角色5": {"voice": "vits:auto:F5", "pitch": 0, "rate": 1.0},
    "女角色6": {"voice": "vits:auto:F6", "pitch": 0, "rate": 1.0},
    "女角色7": {"voice": "vits:auto:F7", "pitch": 0, "rate": 1.05},
    "女角色8": {"voice": "vits:auto:F8", "pitch": 0, "rate": 1.0},
    # ---- 老年女声（更低更慢，像长辈） ----
    "奶奶1": {"voice": "vits:auto:F9", "pitch": 0, "rate": 0.9},
    "奶奶2": {"voice": "vits:auto:F10", "pitch": 0, "rate": 0.9},
    "奶奶3": {"voice": "vits:auto:F11", "pitch": 0, "rate": 0.9},
    "奶奶4": {"voice": "vits:auto:F12", "pitch": 0, "rate": 0.88},
    # ---- 童声 ----
    "童声1": {"voice": "vits:auto:F13", "pitch": 0, "rate": 1.1},
    "童声2": {"voice": "vits:auto:F14", "pitch": 0, "rate": 1.0},
    # ---- 少女 ----
    "少女1": {"voice": "vits:auto:F15", "pitch": 0, "rate": 1.05},
    "少女2": {"voice": "vits:auto:F16", "pitch": 0, "rate": 1.0},
    "少女3": {"voice": "vits:auto:F17", "pitch": 0, "rate": 1.08},
    "少女4": {"voice": "vits:auto:F18", "pitch": 0, "rate": 0.95},
}

# 类别名 → 该类的编号清单（保持声明顺序，自动映射时按序取用）
CATEGORY_ORDER = ("男角色", "中年叔叔", "女角色", "奶奶", "童声", "少女")

# 常见 Edge 中文音色的短名（FriendlyName 太长，界面上显示短名更好认）
VOICE_FRIENDLY = {
    "zh-CN-XiaoxiaoNeural": "晓晓",
    "zh-CN-XiaoyiNeural": "晓伊",
    "zh-CN-XiaoyanNeural": "晓颜",
    "zh-CN-XiaoyouNeural": "晓悠",
    "zh-CN-XiaoyuNeural": "晓宇",
    "zh-CN-XiaozhenNeural": "晓甄",
    "zh-CN-XiaohanNeural": "晓涵",
    "zh-CN-XiaomengNeural": "晓梦",
    "zh-CN-XiaomoNeural": "晓墨",
    "zh-CN-XiaoqiuNeural": "晓秋",
    "zh-CN-XiaoruiNeural": "晓睿",
    "zh-CN-XiaoshuangNeural": "晓双",
    "zh-CN-XiaoxuanNeural": "晓萱",
    "zh-CN-XiaochenNeural": "晓辰",
    "zh-CN-XiaoniNeural": "晓妮",
    "zh-CN-XiaobeiNeural": "晓北",
    "zh-CN-YunxiNeural": "云希",
    "zh-CN-YunyangNeural": "云扬",
    "zh-CN-YunjianNeural": "云健",
    "zh-CN-YunyeNeural": "云野",
    "zh-CN-YunhaoNeural": "云皓",
    "zh-CN-YunzeNeural": "云泽",
    "zh-CN-YunfeiNeural": "云飞",
    "zh-CN-YunxiaNeural": "云夏",
    "zh-CN-YunyiNeural": "云逸",
    "zh-CN-YunfengNeural": "云枫",
    "zh-CN-YunzhengNeural": "云正",
    "zh-HK-HiuMaanNeural": "曉曼(粤)",
    "zh-HK-WanLungNeural": "雲龍(粤)",
    "zh-TW-HsiaoChenNeural": "曉臻(台)",
    "zh-TW-YunJheNeural": "雲哲(台)",
    "zh-TW-HsiaoYuNeural": "曉雨(台)",
    "zh-HK-HiuGaaiNeural": "曉佳(粤)",
    "zh-CN-liaoning-XiaobeiNeural": "晓北(东北)",
    "zh-CN-shaanxi-XiaoniNeural": "晓妮(陕西)",
    "en-US-JennyNeural": "Jenny(英)",
    "en-US-AriaNeural": "Aria(英)",
    "en-US-AnaNeural": "Ana(英)",
    "en-US-AvaNeural": "Ava(英)",
    "en-US-EmmaNeural": "Emma(英)",
    "en-US-MichelleNeural": "Michelle(英)",
    "en-US-GuyNeural": "Guy(英)",
    "en-US-DavisNeural": "Davis(英)",
    "en-US-AndrewNeural": "Andrew(英)",
    "en-US-BrianNeural": "Brian(英)",
    "en-US-EricNeural": "Eric(英)",
    "en-US-RogerNeural": "Roger(英)",
    "en-GB-SoniaNeural": "Sonia(英)",
    "en-GB-LibbyNeural": "Libby(英)",
    "en-GB-RyanNeural": "Ryan(英)",
}


def slot_ids():
    """按内置顺序返回全部编号（模板里新增/删减编号也以默认顺序为准）。"""
    return list(DEFAULT_SLOTS.keys())


def slot_category(slot_id: str) -> str:
    """「男角色3」→「男角色」；不认识的编号返回 None。"""
    if not slot_id:
        return None
    for cat in CATEGORY_ORDER:
        if slot_id.startswith(cat):
            return cat
    return None


def voice_friendly(name: str) -> str:
    """zh-CN-YunxiNeural → 云希；vits:N → 本地音色短名；未知原样返回。"""
    if isinstance(name, str) and name.startswith("vits:auto:"):
        tag = name.split(":", 2)[2]          # M1 / F12
        g = "男" if tag[:1] == "M" else "女"
        try:
            n = int(tag[1:])
        except ValueError:
            return name
        return "自动%s声%d号" % (g, n)
    if isinstance(name, str) and name.startswith("vits:"):
        try:
            sid = int(name.split(":", 1)[1])
        except (TypeError, ValueError):
            return name
        try:
            import vits_tts
            g = vits_tts.get_labels(
                vits_tts._current_data_dir[0] or ".").get(sid)
        except Exception:
            g = None
        return "音色%d号（%s）" % (sid + 1, g or "未标注")
    return VOICE_FRIENDLY.get(name, name or "默认")


class VoiceTemplate:
    """全局音色模板：加载/保存/修改编号参数。线程安全（JSON 落盘 + 锁）。"""

    def __init__(self, data_dir: str):
        self.dir = data_dir
        self.path = os.path.join(data_dir, "voice_template.json")
        self._lock = threading.Lock()
        self._slots = self._deep_copy(DEFAULT_SLOTS)
        self._load()

    @staticmethod
    def _deep_copy(d):
        return json.loads(json.dumps(d))

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return
            # 只接受内置编号；旧版本多出的键忽略，缺的键补默认
            slots = {}
            for sid, params in DEFAULT_SLOTS.items():
                v = data.get(sid)
                if isinstance(v, dict) and v.get("voice"):
                    voice = str(v["voice"])
                    # Edge 音色已删除：旧绑定自动迁移到本槽位的 auto 令牌
                    if (voice.startswith("zh-CN") or voice.startswith("en-")
                            or "Neural" in voice or voice.startswith("kokoro:")):
                        voice = params["voice"]
                    slots[sid] = {
                        "voice": voice,
                        "pitch": _clamp_pitch(v.get("pitch", 0)),
                        "rate": _clamp_rate(v.get("rate", 1.0)),
                    }
                else:
                    slots[sid] = self._deep_copy(params)
            self._slots = slots
        except FileNotFoundError:
            pass                       # 首次运行 → 内置默认
        except Exception as err:
            print("[音色模板] 读取失败，使用默认模板：%s" % err)

    def save(self):
        with self._lock:
            try:
                os.makedirs(self.dir, exist_ok=True)
                tmp = self.path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self._slots, f, ensure_ascii=False, indent=2)
                os.replace(tmp, self.path)
            except Exception as err:
                print("[音色模板] 保存失败：%s" % err)

    # ---------------- 查询 ----------------
    def get(self, slot_id: str):
        """取一个编号的参数副本 {"voice","pitch","rate"}；未知编号返回 None。"""
        with self._lock:
            p = self._slots.get(slot_id)
            return self._deep_copy(p) if p else None

    def all_slots(self):
        with self._lock:
            return {k: self._deep_copy(v) for k, v in self._slots.items()}

    def next_free_slot(self, category: str, used):
        """给自动映射用：返回该类别下第一个未被 used 占用的编号；没有则 None。"""
        if category not in CATEGORY_ORDER:
            return None
        with self._lock:
            for sid in self._slots:
                if sid.startswith(category) and sid not in used:
                    return sid
        return None

    # ---------------- 修改 ----------------
    def set_slot(self, slot_id: str, voice: str, pitch: int, rate: float):
        """修改一个编号的音色/音调/语速并落盘（全局即时生效）。"""
        if slot_id not in DEFAULT_SLOTS:
            return False
        with self._lock:
            self._slots[slot_id] = {
                "voice": str(voice or DEFAULT_SLOTS[slot_id]["voice"]),
                "pitch": _clamp_pitch(pitch),
                "rate": _clamp_rate(rate),
            }
        self.save()
        return True

    def reset_slot(self, slot_id: str):
        """把一个编号恢复为出厂默认参数并落盘。"""
        if slot_id not in DEFAULT_SLOTS:
            return False
        with self._lock:
            self._slots[slot_id] = self._deep_copy(DEFAULT_SLOTS[slot_id])
        self.save()
        return True

    def reset_all(self):
        with self._lock:
            self._slots = self._deep_copy(DEFAULT_SLOTS)
        self.save()


def _clamp_pitch(v):
    try:
        return max(-50, min(50, int(round(float(v)))))
    except (TypeError, ValueError):
        return 0


def _clamp_rate(v):
    try:
        return max(0.5, min(2.0, round(float(v), 2)))
    except (TypeError, ValueError):
        return 1.0
