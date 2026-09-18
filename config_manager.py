# -*- coding: utf-8 -*-
"""
config_manager.py —— 本地配置管理（JSON 持久化）

职责
----
1. 保存/读取用户参数：倍速、音量、音调、语音包、主题、字体大小、定时设置；
2. 保存/读取阅读断点：每本书记录"读到第几段、第几个字符"，重开自动续读；
3. 保存/读取书签：每本书可存多条书签，支持增删与跳转。

存储位置：用户主目录 ~/.audiobook_reader/config.json
         （放在用户目录而不是程序目录，避免 exe 位于只读路径时写入失败）
异常策略：任何读写失败都只打印警告并回退默认值，绝不导致程序崩溃。
"""

import json
import os
import threading
import time

# 默认配置模板（新用户或配置损坏时使用）
DEFAULTS = {
    "speed": 1.0,          # 倍速 0.5 ~ 3.0
    "volume": 80,          # 音量 0 ~ 100
    "pitch": 0,            # 音调 -10 ~ 10（SAPI 音调档位）
    "pitch_variation": False,  # 音调随机微变：每段 ±2 档抖动，朗读更自然
    "voice_name": "",      # 选择的语音包名称
    "theme": "light",      # 界面主题：light / dark
    "font_size": 15,       # 阅读区字体大小（磅）
    "page_interval": 0,    # 定时翻页：每段读完后的停顿秒数，0 为连续朗读
    "sleep_minutes": 30,   # 定时休眠输入框的默认值（分钟）
    "last_book": "",       # 上次打开的书籍完整路径
    "library_dir": "",     # 网络下载书籍的保存目录（空 = D:\AudioBookReader\书库）
    "show_chapters": True, # 是否显示章节目录侧边栏
    "positions": {},       # 阅读断点：{书籍key: {"para": 段落号, "char": 字符数, "time": 时间}}
    "bookmarks": {},       # 书签：{书籍key: [{"para": 段落号, "label": 摘要, "time": 时间}]}
}


class ConfigManager:
    """线程安全的 JSON 配置管理器（所有 UI 操作都在主线程，锁仅作保险）。"""

    def __init__(self, path: str = None):
        # 配置文件路径：~/.audiobook_reader/config.json
        self.path = path or os.path.join(
            os.path.expanduser("~"), ".audiobook_reader", "config.json"
        )
        self._lock = threading.Lock()
        self._data = self._deep_copy(DEFAULTS)
        self._load()

    # ---------------- 基础读写 ----------------
    @staticmethod
    def _deep_copy(dict_obj):
        """通过 JSON 序列化做一次简单深拷贝，避免默认模板被引用污染。"""
        return json.loads(json.dumps(dict_obj))

    def _load(self):
        """从磁盘加载配置；文件缺失视为首次运行，损坏则回退默认值。"""
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                # 补齐旧版本配置中缺失的键，保证结构完整
                for key, value in DEFAULTS.items():
                    data.setdefault(key, self._deep_copy(value))
                self._data = data
        except FileNotFoundError:
            pass  # 首次运行，使用默认配置
        except Exception as err:
            print(f"[配置] 读取失败，使用默认配置：{err}")

    def export_data(self) -> str:
        """把整份配置序列化成 JSON 文本（书签/断点备份导出用）。"""
        with self._lock:
            return json.dumps(self._data, ensure_ascii=False, indent=2)

    def import_data(self, text: str):
        """从备份 JSON 恢复配置（整体替换后落盘）。

        只接受 dict；结构不合法的条目由各 get() 的默认值兜底。
        """
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("备份文件格式不对（顶层不是 JSON 对象）")
        with self._lock:
            self._data = data
        self.save()

    def save(self):
        """原子化写盘：先写临时文件再替换，防止写一半断电损坏配置。"""
        with self._lock:
            try:
                os.makedirs(os.path.dirname(self.path), exist_ok=True)
                tmp_path = self.path + ".tmp"
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(self._data, f, ensure_ascii=False, indent=2)
                os.replace(tmp_path, self.path)
            except Exception as err:
                print(f"[配置] 保存失败：{err}")

    def get(self, key, default=None):
        """读取一个配置项。"""
        return self._data.get(key, default)

    def set(self, key, value):
        """写入一个配置项（内存中生效，需调用 save() 才落盘）。"""
        self._data[key] = value

    # ---------------- 阅读断点 ----------------
    @staticmethod
    def book_key(path: str) -> str:
        """书籍唯一标识：规范化绝对路径（大小写不敏感，兼容 Windows）。"""
        return os.path.normcase(os.path.normpath(os.path.abspath(path)))

    def get_position(self, book_key: str):
        """取某本书的阅读断点，返回 (段落号, 字符数)；无记录返回 None。"""
        pos = self._data.get("positions", {}).get(book_key)
        if isinstance(pos, dict) and "para" in pos:
            try:
                return int(pos["para"]), int(pos.get("char", 0))
            except (TypeError, ValueError):
                return None
        return None

    def set_position(self, book_key: str, para: int, char: int):
        """记录某本书的阅读断点（只更新内存，由定时器/退出时统一落盘）。"""
        self._data.setdefault("positions", {})[book_key] = {
            "para": int(para),
            "char": int(char),
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

    def clear_position(self, book_key: str):
        """清除某本书的断点（菜单"重新开始阅读"可用）。"""
        self._data.get("positions", {}).pop(book_key, None)

    # ---------------- 书签 ----------------
    def get_bookmarks(self, book_key: str) -> list:
        """取某本书的全部书签（按段落号升序）。"""
        return list(self._data.get("bookmarks", {}).get(book_key, []))

    def add_bookmark(self, book_key: str, para: int, label: str) -> bool:
        """添加书签；同一段落重复添加返回 False。"""
        bookmarks = self._data.setdefault("bookmarks", {}).setdefault(book_key, [])
        if any(b.get("para") == int(para) for b in bookmarks):
            return False
        bookmarks.append({
            "para": int(para),
            "label": str(label)[:24],  # 截断摘要，避免菜单过长
            "time": time.strftime("%Y-%m-%d %H:%M"),
        })
        bookmarks.sort(key=lambda b: b["para"])
        return True

    def remove_bookmark(self, book_key: str, para: int) -> bool:
        """删除指定段落上的书签，返回是否删除成功。"""
        bookmarks = self._data.get("bookmarks", {}).get(book_key, [])
        for i, b in enumerate(bookmarks):
            if b.get("para") == int(para):
                del bookmarks[i]
                return True
        return False

    def clear_bookmarks(self, book_key: str):
        """清空某本书的全部书签。"""
        self._data.get("bookmarks", {}).pop(book_key, None)
