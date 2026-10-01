# -*- coding: utf-8 -*-
"""role_config.py —— 单本小说的「人名 ↔ 音色编号」映射存储

存储规则（与需求一致）
----------------------
· 只保存【本书人名 ↔ 模板编号】的映射，**不**重复保存整套音色参数；
  人名的实际声音参数运行时按编号从全局模板（voice_template.py）现查。
· 用户对某个人物单独覆盖了音色/语速/音调时，把这份自定义参数额外存在
  该人物条目的 "custom" 字段里；有 custom 的人物不再跟随全局模板同步。
· 每本小说一个独立 JSON 文件（role_maps/<md5(书籍key)>.json），
  不同小说之间的人名映射互不干扰。
· 删除小说（历史记录）时直接删掉对应映射文件，不弹确认框 —— 见 delete_map()。
"""

import hashlib
import json
import os
import threading

from voice_template import voice_friendly


class RoleMap:
    """一本书的角色映射表。

    roles 内部结构（有序，按绑定顺序）：
        [{"name": 人名, "slot": 编号或 "", "custom": {"voice","pitch","rate"} 或 None}]
    """

    VERSION = 1

    def __init__(self, book_key: str, roles=None):
        self.book_key = book_key
        self.roles = list(roles or [])
        self._lock = threading.Lock()

    # ---------------- 条目操作 ----------------
    def get(self, name):
        """返回人物的条目 dict；不存在返回 None。"""
        with self._lock:
            for r in self.roles:
                if r.get("name") == name:
                    return r
        return None

    def ensure(self, name):
        """取条目，没有就补一个未绑定的条目（追加到末尾）。"""
        entry = self.get(name)
        if entry is None:
            entry = {"name": name, "slot": "", "custom": None}
            with self._lock:
                self.roles.append(entry)
        return entry

    def set_slot(self, name, slot_id):
        """换绑编号：只改人名绑定的编号，编号底层的音色参数不动。"""
        entry = self.ensure(name)
        entry["slot"] = str(slot_id or "")

    def set_custom(self, name, voice, pitch, rate):
        """给人物设置自定义音色参数（之后不再跟随全局模板）。"""
        entry = self.ensure(name)
        entry["custom"] = {"voice": str(voice), "pitch": int(pitch),
                           "rate": round(float(rate), 2)}

    def reset_custom(self, name):
        """一键重置：清掉自定义参数，恢复跟随模板编号自带参数。"""
        entry = self.get(name)
        if entry is not None:
            entry["custom"] = None

    def names(self):
        with self._lock:
            return [r.get("name") for r in self.roles]

    # ---------------- 声音参数 ----------------
    @staticmethod
    def entry_params(entry, template):
        """算出某个人物实际使用的声音参数 (voice, rate倍率, pitch Hz)。

        优先级：人物自定义 custom > 模板编号参数；都没有 → None
        （调用方回退全局单音色）。
        """
        if not entry:
            return None
        custom = entry.get("custom")
        if isinstance(custom, dict) and custom.get("voice"):
            try:
                return (str(custom["voice"]), float(custom.get("rate", 1.0)),
                        int(custom.get("pitch", 0)))
            except (TypeError, ValueError):
                pass
        slot = entry.get("slot")
        if slot and template is not None:
            params = template.get(slot)
            if params:
                return (params["voice"], float(params["rate"]),
                        int(params["pitch"]))
        return None

    # ---------------- 序列化 ----------------
    def to_dict(self):
        with self._lock:
            return {"version": self.VERSION, "book_key": self.book_key,
                    "roles": [dict(r) for r in self.roles]}

    @classmethod
    def from_dict(cls, data, book_key=None):
        if not isinstance(data, dict):
            return None
        roles = data.get("roles")
        if not isinstance(roles, list):
            return None
        clean = []
        for r in roles:
            if not isinstance(r, dict) or not r.get("name"):
                continue
            clean.append({"name": str(r["name"]),
                          "slot": str(r.get("slot") or ""),
                          "custom": r.get("custom")
                          if isinstance(r.get("custom"), dict) else None})
        return cls(book_key or str(data.get("book_key", "")), clean)


def _map_path(data_dir: str, book_key: str) -> str:
    """映射文件路径：role_maps/<md5(book_key)>.json。

    book_key 是规范化绝对路径，含反斜杠/冒号等非法文件名字符，
    取 md5 做文件名既安全又能从任意路径稳定定位同一本书。
    """
    digest = hashlib.md5(book_key.encode("utf-8", "replace")).hexdigest()
    return os.path.join(data_dir, "role_maps", digest + ".json")


def load_map(data_dir: str, book_key: str):
    """读取某本书的映射；文件不存在/损坏返回 None（调用方自动生成新映射）。"""
    path = _map_path(data_dir, book_key)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        rm = RoleMap.from_dict(data, book_key)
        return rm if (rm and rm.roles) else None
    except FileNotFoundError:
        return None
    except Exception as err:
        print("[角色映射] 读取失败，将重新生成：%s" % err)
        return None


def save_map(data_dir: str, role_map: RoleMap):
    """原子化落盘某本书的映射。"""
    if role_map is None:
        return
    path = _map_path(data_dir, role_map.book_key)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(role_map.to_dict(), f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception as err:
        print("[角色映射] 保存失败：%s" % err)


def delete_map(data_dir: str, book_key: str):
    """删除某本书的映射文件（删书时同步调用；不弹确认框）。

    只删单书映射 —— 全局音色模板 / 全局语速音调设置完全不受影响。
    """
    path = _map_path(data_dir, book_key)
    try:
        if os.path.isfile(path):
            os.remove(path)
    except Exception as err:
        print("[角色映射] 删除失败：%s" % err)


def delete_all_maps(data_dir: str, keep_book_key: str = None):
    """清空全部单书映射（清空历史时调用）；keep_book_key 保留当前书。"""
    folder = os.path.join(data_dir, "role_maps")
    try:
        if not os.path.isdir(folder):
            return
        keep = (_map_path(data_dir, keep_book_key)
                if keep_book_key else None)
        for name in os.listdir(folder):
            fp = os.path.join(folder, name)
            if keep is not None and os.path.abspath(fp) == os.path.abspath(keep):
                continue
            try:
                os.remove(fp)
            except OSError:
                pass
    except Exception as err:
        print("[角色映射] 清空失败：%s" % err)


def auto_map(book_key: str, detected, template):
    """按出场顺序把识别出的人物自动绑定到模板编号。

    detected 是 role_parser.analyze() 的第一项返回值：
    [(人名, 类别), ...]，类别 ∈ 男角色/女角色/中年叔叔/奶奶。
    规则：本书第一个出场男角色 → 男角色1，第二个 → 男角色2 ……以此类推；
    同类编号用尽后该人物保持未绑定（朗读时回退全局单音色）。
    """
    rm = RoleMap(book_key)
    used = set()
    for name, category in (detected or []):
        slot = template.next_free_slot(category, used) if template else None
        if slot:
            used.add(slot)
        entry = rm.ensure(name)
        entry["slot"] = slot or ""
    return rm


def describe_entry(entry, template=None):
    """人物条目的一行中文描述（角色管理面板用）。"""
    if entry is None:
        return "未绑定"
    slot = entry.get("slot")
    if not slot:
        return "未绑定（朗读用全局音色）"
    custom = entry.get("custom")
    if isinstance(custom, dict) and custom.get("voice"):
        return "%s · 自定义（%s·调%+d·%.2fx）" % (
            slot, voice_friendly(custom["voice"]),
            int(custom.get("pitch", 0)), float(custom.get("rate", 1.0)))
    params = template.get(slot) if template is not None else None
    if params:
        return "%s · 模板（%s·调%+d·%.2fx）" % (
            slot, voice_friendly(params["voice"]),
            int(params["pitch"]), float(params["rate"]))
    return slot
