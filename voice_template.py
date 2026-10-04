# -*- coding: utf-8 -*-
"""voice_template.py —— 全局固定音色清单模板

设计要点
--------
· 模板是**全局唯一**的：所有小说共用同一套「编号角色清单」，
  每个编号预先绑定一个 Edge 音色 + 音调(Hz) + 语速(倍率)。
· 单本小说只保存「人名 ↔ 编号」的映射（见 role_config.py），
  **不**重复保存音色参数 —— 人名的声音参数永远按编号从本模板现查。
  所以模板一改，所有绑定该编号的人物立即同步换声音；
  只有单本书里「单独覆盖了自定义参数」的角色不受影响（那部分参数
  存在 role_config 的人物条目里，优先级高于模板）。
· 模板持久化到应用私有目录 voice_template.json；文件缺失/损坏时
  回退到内置默认清单（内置清单即出厂状态，「恢复默认」也回到它）。

音调/语速的单位与 Edge 对齐：
  · pitch 用 Hz（Edge 的 pitch 参数，如 "+10Hz"），范围 ±50Hz；
  · rate 用倍率（1.0 = 正常），换算成 Edge 的百分比由引擎负责。
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
    "男角色1":   {"voice": "zh-CN-YunxiNeural",  "pitch": 0,   "rate": 1.0},
    "男角色2":   {"voice": "zh-CN-YunyangNeural", "pitch": -5,  "rate": 1.0},
    "男角色3":   {"voice": "zh-CN-YunjianNeural", "pitch": 0,   "rate": 1.0},
    "男角色4":   {"voice": "zh-CN-YunhaoNeural",  "pitch": 0,   "rate": 1.0},
    "男角色5":   {"voice": "zh-CN-YunzeNeural",   "pitch": 0,   "rate": 1.0},
    "男角色6":   {"voice": "zh-CN-YunfeiNeural",  "pitch": 0,   "rate": 1.0},
    "男角色7":   {"voice": "zh-CN-YunyeNeural",   "pitch": 0,   "rate": 1.0},
    "男角色8":   {"voice": "zh-CN-YunxiaNeural",  "pitch": 5,   "rate": 1.05},
    # ---- 中年男声（偏低偏慢） ----
    "中年叔叔1": {"voice": "zh-CN-YunyeNeural",   "pitch": -10, "rate": 0.95},
    "中年叔叔2": {"voice": "zh-CN-YunjianNeural",  "pitch": -8,  "rate": 0.95},
    "中年叔叔3": {"voice": "zh-CN-YunzeNeural",    "pitch": -12, "rate": 0.92},
    "中年叔叔4": {"voice": "zh-CN-YunyangNeural",  "pitch": -10, "rate": 0.95},
    # ---- 女声 ----
    "女角色1":   {"voice": "zh-CN-XiaoxiaoNeural", "pitch": 0,   "rate": 1.0},
    "女角色2":   {"voice": "zh-CN-XiaoyiNeural",   "pitch": 5,   "rate": 1.0},
    "女角色3":   {"voice": "zh-CN-XiaoyanNeural",  "pitch": 0,   "rate": 1.0},
    "女角色4":   {"voice": "zh-CN-XiaoyuNeural",   "pitch": 0,   "rate": 1.0},
    "女角色5":   {"voice": "zh-CN-XiaozhenNeural", "pitch": 0,   "rate": 1.0},
    "女角色6":   {"voice": "zh-CN-XiaohanNeural",  "pitch": 0,   "rate": 1.0},
    "女角色7":   {"voice": "zh-CN-XiaomengNeural", "pitch": 5,   "rate": 1.05},
    "女角色8":   {"voice": "zh-CN-XiaoxuanNeural", "pitch": 0,   "rate": 1.0},
    # ---- 老年女声（更低更慢，像长辈） ----
    "奶奶1":     {"voice": "zh-CN-XiaoxiaoNeural", "pitch": -15, "rate": 0.9},
    "奶奶2":     {"voice": "zh-CN-XiaoyanNeural",  "pitch": -15, "rate": 0.9},
    "奶奶3":     {"voice": "zh-CN-XiaozhenNeural", "pitch": -15, "rate": 0.9},
    "奶奶4":     {"voice": "zh-CN-XiaomengNeural", "pitch": -15, "rate": 0.88},
    # ---- 童声 ----
    "童声1":     {"voice": "zh-CN-XiaoshuangNeural", "pitch": 15, "rate": 1.05},
    "童声2":     {"voice": "zh-CN-XiaoyouNeural",   "pitch": 15, "rate": 1.0},
    # ---- 少女 ----
    "少女1":     {"voice": "zh-CN-XiaoyiNeural",   "pitch": 12, "rate": 1.05},
    "少女2":     {"voice": "zh-CN-XiaomengNeural", "pitch": 10, "rate": 1.0},
    "少女3":     {"voice": "zh-CN-XiaoyanNeural",  "pitch": 8,  "rate": 1.08},
    "少女4":     {"voice": "zh-CN-XiaoyuNeural",   "pitch": 12, "rate": 0.95},
    # ---- 离线（sherpa-onnx + Kokoro-82M，中英双语；下载语音包后可用） ----
    # ⚠️ kokoro:0..2 是英文音色（af_maple/af_sol/bf_vale），读中文小说
    # 效果差，所以出厂「离线1..8」全部指向中文说话人。zf_001 与 zm_010
    # 是官方发布里仅有的两个带试听样例（HEARME_*.wav）的中文音色——
    # 质量最稳，各类门槽位让它们打头；旧映射里绑了离线1..8 的角色
    # 自动跟着换（设置里「全部恢复默认」可取到新出厂值）。
    "离线1":     {"voice": "kokoro:3",  "pitch": 0,  "rate": 1.0},
    "离线2":     {"voice": "kokoro:4",  "pitch": 0,  "rate": 1.0},
    "离线3":     {"voice": "kokoro:5",  "pitch": 0,  "rate": 1.0},
    "离线4":     {"voice": "kokoro:6",  "pitch": 0,  "rate": 1.0},
    "离线5":     {"voice": "kokoro:7",  "pitch": 0,  "rate": 1.0},
    "离线6":     {"voice": "kokoro:8",  "pitch": 0,  "rate": 1.0},
    "离线7":     {"voice": "kokoro:59", "pitch": 0,  "rate": 1.0},
    "离线8":     {"voice": "kokoro:58", "pitch": 0,  "rate": 1.0},
    # ---- 离线分类槽位（对应 Kokoro 音色库的门类，供角色换绑选用） ----
    "离线女1":   {"voice": "kokoro:3",  "pitch": 0,  "rate": 1.0},
    "离线女2":   {"voice": "kokoro:4",  "pitch": 0,  "rate": 1.0},
    "离线女3":   {"voice": "kokoro:5",  "pitch": 0,  "rate": 1.0},
    "离线女4":   {"voice": "kokoro:6",  "pitch": 0,  "rate": 1.0},
    "离线女5":   {"voice": "kokoro:7",  "pitch": 0,  "rate": 1.0},
    "离线女6":   {"voice": "kokoro:8",  "pitch": 0,  "rate": 1.0},
    # zm_010 是官方试听样例，男声第一位；zm_009 次之
    "离线男1":   {"voice": "kokoro:59", "pitch": 0,  "rate": 1.0},
    "离线男2":   {"voice": "kokoro:58", "pitch": 0,  "rate": 1.0},
    "离线男3":   {"voice": "kokoro:60", "pitch": 0,  "rate": 1.0},
    "离线男4":   {"voice": "kokoro:61", "pitch": 0,  "rate": 1.0},
    "离线男5":   {"voice": "kokoro:62", "pitch": 0,  "rate": 1.0},
    "离线男6":   {"voice": "kokoro:63", "pitch": 0,  "rate": 1.0},
    "离线少女1": {"voice": "kokoro:9",  "pitch": 0,  "rate": 1.05},
    "离线少女2": {"voice": "kokoro:10", "pitch": 0,  "rate": 1.05},
    "离线奶奶1": {"voice": "kokoro:11", "pitch": 0,  "rate": 0.9},
    "离线奶奶2": {"voice": "kokoro:12", "pitch": 0,  "rate": 0.9},
    "离线叔叔1": {"voice": "kokoro:66", "pitch": 0,  "rate": 0.95},
    "离线叔叔2": {"voice": "kokoro:67", "pitch": 0,  "rate": 0.95},
    "离线英文1": {"voice": "kokoro:0",  "pitch": 0,  "rate": 1.0},
    "离线英文2": {"voice": "kokoro:1",  "pitch": 0,  "rate": 1.0},
    "离线英文3": {"voice": "kokoro:2",  "pitch": 0,  "rate": 1.0},
}

# 类别名 → 该类的编号清单（保持声明顺序，自动映射时按序取用）。
# 「离线女/离线男/离线少女/离线奶奶/离线叔叔/离线英文」只供手动换绑
# （角色自动映射只发 Edge 槽位），Kokoro 引擎不支持音调，语速可调。
CATEGORY_ORDER = ("男角色", "中年叔叔", "女角色", "奶奶", "童声", "少女",
                  "离线女", "离线男", "离线少女", "离线奶奶", "离线叔叔",
                  "离线英文")

# ---------------------------------------------------------------------------
# Kokoro-82M v1.1-zh（kokoro-multi-lang-v1_1）音色库全表：说话人编号 →
# (音色名, 性别, 语言)。voices.bin 的 sid 顺序来自 sherpa-onnx
# scripts/kokoro/v1.1-zh/generate_voices_bin.py：
#   0..2   = af_maple / af_sol / bf_vale（英文女声）
#   3..57  = 存在的 zf_001..zf_099（中文女声，共 55 个）
#   58..102 = 存在的 zm_009..zm_100（中文男声，共 45 个）
# 共 103 个音色，与模型 EXPECTED_SPEAKERS 一致。
# ---------------------------------------------------------------------------
KOKORO_SPEAKERS = (
    (0, "af_maple", "女", "英"),
    (1, "af_sol", "女", "英"),
    (2, "bf_vale", "女", "英"),
    (3, "zf_001", "女", "中"),
    (4, "zf_002", "女", "中"),
    (5, "zf_003", "女", "中"),
    (6, "zf_004", "女", "中"),
    (7, "zf_005", "女", "中"),
    (8, "zf_006", "女", "中"),
    (9, "zf_007", "女", "中"),
    (10, "zf_008", "女", "中"),
    (11, "zf_017", "女", "中"),
    (12, "zf_018", "女", "中"),
    (13, "zf_019", "女", "中"),
    (14, "zf_021", "女", "中"),
    (15, "zf_022", "女", "中"),
    (16, "zf_023", "女", "中"),
    (17, "zf_024", "女", "中"),
    (18, "zf_026", "女", "中"),
    (19, "zf_027", "女", "中"),
    (20, "zf_028", "女", "中"),
    (21, "zf_032", "女", "中"),
    (22, "zf_036", "女", "中"),
    (23, "zf_038", "女", "中"),
    (24, "zf_039", "女", "中"),
    (25, "zf_040", "女", "中"),
    (26, "zf_042", "女", "中"),
    (27, "zf_043", "女", "中"),
    (28, "zf_044", "女", "中"),
    (29, "zf_046", "女", "中"),
    (30, "zf_047", "女", "中"),
    (31, "zf_048", "女", "中"),
    (32, "zf_049", "女", "中"),
    (33, "zf_051", "女", "中"),
    (34, "zf_059", "女", "中"),
    (35, "zf_060", "女", "中"),
    (36, "zf_067", "女", "中"),
    (37, "zf_070", "女", "中"),
    (38, "zf_071", "女", "中"),
    (39, "zf_072", "女", "中"),
    (40, "zf_073", "女", "中"),
    (41, "zf_074", "女", "中"),
    (42, "zf_075", "女", "中"),
    (43, "zf_076", "女", "中"),
    (44, "zf_077", "女", "中"),
    (45, "zf_078", "女", "中"),
    (46, "zf_079", "女", "中"),
    (47, "zf_083", "女", "中"),
    (48, "zf_084", "女", "中"),
    (49, "zf_085", "女", "中"),
    (50, "zf_086", "女", "中"),
    (51, "zf_087", "女", "中"),
    (52, "zf_088", "女", "中"),
    (53, "zf_090", "女", "中"),
    (54, "zf_092", "女", "中"),
    (55, "zf_093", "女", "中"),
    (56, "zf_094", "女", "中"),
    (57, "zf_099", "女", "中"),
    (58, "zm_009", "男", "中"),
    (59, "zm_010", "男", "中"),
    (60, "zm_011", "男", "中"),
    (61, "zm_012", "男", "中"),
    (62, "zm_013", "男", "中"),
    (63, "zm_014", "男", "中"),
    (64, "zm_015", "男", "中"),
    (65, "zm_016", "男", "中"),
    (66, "zm_020", "男", "中"),
    (67, "zm_025", "男", "中"),
    (68, "zm_029", "男", "中"),
    (69, "zm_030", "男", "中"),
    (70, "zm_031", "男", "中"),
    (71, "zm_033", "男", "中"),
    (72, "zm_034", "男", "中"),
    (73, "zm_035", "男", "中"),
    (74, "zm_037", "男", "中"),
    (75, "zm_041", "男", "中"),
    (76, "zm_045", "男", "中"),
    (77, "zm_050", "男", "中"),
    (78, "zm_052", "男", "中"),
    (79, "zm_053", "男", "中"),
    (80, "zm_054", "男", "中"),
    (81, "zm_055", "男", "中"),
    (82, "zm_056", "男", "中"),
    (83, "zm_057", "男", "中"),
    (84, "zm_058", "男", "中"),
    (85, "zm_061", "男", "中"),
    (86, "zm_062", "男", "中"),
    (87, "zm_063", "男", "中"),
    (88, "zm_064", "男", "中"),
    (89, "zm_065", "男", "中"),
    (90, "zm_066", "男", "中"),
    (91, "zm_068", "男", "中"),
    (92, "zm_069", "男", "中"),
    (93, "zm_080", "男", "中"),
    (94, "zm_081", "男", "中"),
    (95, "zm_082", "男", "中"),
    (96, "zm_089", "男", "中"),
    (97, "zm_091", "男", "中"),
    (98, "zm_095", "男", "中"),
    (99, "zm_096", "男", "中"),
    (100, "zm_097", "男", "中"),
    (101, "zm_098", "男", "中"),
    (102, "zm_100", "男", "中"),
)


def kokoro_speaker(sid: int):
    """sid → (音色名, 性别, 语言)；越界返回 None。"""
    if 0 <= sid < len(KOKORO_SPEAKERS):
        row = KOKORO_SPEAKERS[sid]
        return row[1], row[2], row[3]
    return None


def kokoro_label(sid: int, seq: int = None):
    """sid → 人读的音色短名，如「离线4号·zf_002（女·中）」。"""
    sp = kokoro_speaker(sid)
    if sp is None:
        return "离线%d号（Kokoro）" % (sid + 1)
    seq_num = (sid + 1) if seq is None else seq
    return "离线%d号·%s（%s·%s）" % (seq_num, sp[0], sp[1], sp[2])

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
    """zh-CN-YunxiNeural → 云希；kokoro:N → 离线音色短名；未知原样返回。"""
    if isinstance(name, str) and name.startswith("kokoro:"):
        try:
            return kokoro_label(int(name.split(":", 1)[1]))
        except (TypeError, ValueError):
            return name
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
                    slots[sid] = {
                        "voice": str(v["voice"]),
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
