# -*- coding: utf-8 -*-
"""main.py —— 有声书朗读 · 安卓版

与桌面版的差异
--------------
· 界面用 Kivy 重写（PyQt6 不支持安卓）；
· 语音改用安卓原生 TTS（见 tts_android.py）；
· 书籍解析与配置管理沿用桌面版模块（纯标准库，可直接复用）。

安卓端交互设计
--------------
· 点正文任意一段 → 从该段开始朗读（触屏上比右键菜单更自然）
· 顶部「目录」→ 章节目录，点章节即跳转并开始朗读
· 顶部「设置」→ 音色 / 音调 / 语速 / 语调起伏 / 字号 / 定时休眠
· 底部 → 进度条可拖动跳转，播放/暂停/停止，快捷倍速

文件导入
--------
用系统的「打开文件」选择器（SAF），**不需要任何存储权限**：
选中后把文件复制进应用私有目录再解析，这样 txt/epub 都能正常读取。
"""

import bisect
import os
import shutil
import sys
import time
import traceback

from kivy.app import App
from kivy.clock import Clock
from kivy.lang import Builder
from kivy.metrics import dp
from kivy.properties import (BooleanProperty, ListProperty, NumericProperty,
                             StringProperty)
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.floatlayout import FloatLayout
from kivy.uix.label import Label
from kivy.uix.popup import Popup
from kivy.uix.recycleboxlayout import RecycleBoxLayout
from kivy.uix.recycleview import RecycleView
from kivy.uix.recycleview.views import RecycleDataViewBehavior
from kivy.uix.slider import Slider

from book_parser import BookParseError
from book_parser import load_book as parse_book_file
from config_manager import ConfigManager
from tts_android import STATE_PAUSED, STATE_PLAYING, AndroidTTS

# ============================================================================
#  中文字体注册 —— 不做这一步，界面上所有中文都是空白！
#
#  Kivy 默认字体是 Roboto，**不含任何中文字形**，中文会渲染成空白（豆腐块）。
#  这里把 Noto Sans SC（Google 出品，OFL 开源许可，可自由分发）注册成默认字体名
#  "Roboto"，等于全局替换默认字体，所有控件自动生效，无需逐个设 font_name。
#
#  字体随源码一起打包进 APK（见 buildozer.spec 的 source.include_exts 里的 otf）。
#  用 __file__ 定位而不是相对路径，避免受工作目录影响。
# ============================================================================
from kivy.core.text import LabelBase

_FONT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "fonts", "NotoSansSC-Regular.otf")
if os.path.isfile(_FONT_PATH):
    LabelBase.register(name="Roboto", fn_regular=_FONT_PATH)
else:                                        # 字体缺失时给个明确提示，别静默变空白
    print("[警告] 找不到中文字体，界面中文将无法显示：%s" % _FONT_PATH)

# 请求码：文件选择器
REQUEST_PICK_BOOK = 1001

KV = """
<ParaView>:
    # 一段正文：点一下就从这段开始读；朗读中的那一段有底色。
    # 注意用 reader_font_size 而不是直接覆盖 font_size —— Label.font_size 自带
    # 'sp' 单位语义，重定义会破坏它。
    active: False
    font_size: str(int(root.reader_font_size)) + 'sp'
    text_size: self.width - dp(20), None
    size_hint_y: None
    height: max(dp(30), self.texture_size[1] + dp(14))
    halign: 'left'
    valign: 'top'
    padding_x: dp(10)
    canvas.before:
        Color:
            rgba: root.bg_color
        RoundedRectangle:
            pos: self.x + dp(2), self.y + dp(1)
            size: self.width - dp(4), self.height - dp(2)
            radius: [dp(6)]
"""


class ParaView(RecycleDataViewBehavior, Label):
    """正文里的一段。点一下 → 从这段开始朗读。"""

    active = BooleanProperty(False)
    index = NumericProperty(0)
    reader_font_size = NumericProperty(15)
    bg_color = ListProperty([0, 0, 0, 0])

    def on_active(self, *_):
        self.bg_color = [1.0, 0.90, 0.35, 1.0] if self.active else [0, 0, 0, 0]

    def on_touch_down(self, touch):
        # 关键：这里**不能** consume 触摸（不能 return True），
        # 否则 RecycleView 收不到手势，阅读区就滚不动了。
        # 只记下按下的位置，等 on_touch_up 时判断这是"点击"还是"滑动"。
        if self.collide_point(*touch.pos):
            self._press_pos = touch.pos
            self._pressed = True
            # 按住不动 0.5 秒 → 长按菜单（添加书签等）
            self._long_press = Clock.schedule_once(self._fire_long_press, 0.5)
        return super().on_touch_down(touch)

    def on_touch_move(self, touch):
        # 手指一移动就说明用户在滚动，取消长按判定
        if getattr(self, "_pressed", False) and hasattr(self, "_press_pos"):
            dx = abs(touch.pos[0] - self._press_pos[0])
            dy = abs(touch.pos[1] - self._press_pos[1])
            if dx > dp(12) or dy > dp(12):
                self._cancel_long_press()
        return super().on_touch_move(touch)

    def _cancel_long_press(self):
        event = getattr(self, "_long_press", None)
        if event is not None:
            event.cancel()
            self._long_press = None

    def _fire_long_press(self, *_):
        self._long_press = None
        self._pressed = False        # 长按已处理，抬手时不要再触发单击
        app = App.get_running_app()
        if app is not None:
            app.long_press_paragraph(self.index)

    def on_touch_up(self, touch):
        self._cancel_long_press()
        if getattr(self, "_pressed", False):
            self._pressed = False
            if self.collide_point(*touch.pos):
                dx = abs(touch.pos[0] - self._press_pos[0])
                dy = abs(touch.pos[1] - self._press_pos[1])
                if dx < dp(10) and dy < dp(10):        # 位移够小才算点击
                    app = App.get_running_app()
                    if app is not None:
                        app.tap_paragraph(self.index)
        return super().on_touch_up(touch)


class AudioBookApp(App):
    """主应用：书籍加载、播放调度、界面刷新。"""

    # ---- 供 KV 绑定的属性 ----
    book_title = StringProperty("未打开书籍")
    chapter_text = StringProperty("")
    progress_text = StringProperty("00:00 / 00:00  0.0%")
    sleep_text = StringProperty("")
    play_label = StringProperty("▶ 播放")
    reader_font = NumericProperty(15)
    highlight_index = NumericProperty(-1)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._config = None
        self._engine = None
        self._paragraphs = []
        self._offsets = [0]
        self._total_chars = 0
        self._book_path = ""
        self._book_key = ""
        self._chapters = []
        self._dragging = False
        self._sleep_until = 0.0     # 定时休眠的截止时间戳（0 = 未启用）
        self._rv = None
        self._status_popup = None
        self._voice_list = []       # 系统音色列表（引擎初始化后填充）
        self._shown_errors = set()  # 已提示过的错误，避免连续失败时刷屏
        self._font_event = None     # 字号刷新防抖用

    # ============================================================
    #                        启动
    # ============================================================
    def build(self):
        self.title = "有声书朗读"
        # 启动阶段的任何异常都渲染到屏幕上。
        # 安卓上普通用户拿不到 logcat，这是唯一能让用户把真实报错反馈回来的办法。
        try:
            return self._build_real()
        except Exception:
            return self._build_crash_screen(traceback.format_exc())

    def _build_crash_screen(self, tb_text):
        """把启动失败的 traceback 直接显示在屏幕上（可滚动、可截图）。"""
        from kivy.uix.scrollview import ScrollView
        root = BoxLayout(orientation="vertical", padding=dp(8), spacing=dp(6))
        root.add_widget(Label(
            text="启动失败 — 请把本页截图发给开发者",
            size_hint_y=None, height=dp(34), font_size="14sp",
            color=(1, .45, .45, 1)))
        scroll = ScrollView()
        label = Label(text=tb_text, size_hint_y=None, halign="left",
                      valign="top", font_size="11sp")
        label.bind(width=lambda w, *_: setattr(w, "text_size", (w.width, None)))
        label.bind(texture_size=lambda w, *_: setattr(w, "height", w.texture_size[1]))
        scroll.add_widget(label)
        root.add_widget(scroll)
        return root

    def _build_real(self):
        Builder.load_string(KV)

        # 配置与语音引擎都放在应用私有目录（安卓上必然可写）
        cfg_path = os.path.join(self.user_data_dir, "config.json")
        self._config = ConfigManager(cfg_path)
        self.reader_font = float(self._config.get("font_size", 15))

        self._engine = AndroidTTS(
            on_progress=self._on_progress,
            on_paragraph=self._on_paragraph,
            on_state=self._on_state,
            on_finished=self._on_finished,
            on_error=self._on_error,
            on_voices=self._on_voices,
        )
        self._engine.start()
        self._engine.set_speed(float(self._config.get("speed", 1.0)))
        self._engine.set_pitch(int(self._config.get("pitch", 0)))
        self._engine.set_intonation(bool(self._config.get("intonation", True)))

        root = self._build_ui()

        # 自动恢复上次的书；并轮询刷新进度
        Clock.schedule_once(self._restore_last_book, 0.6)
        Clock.schedule_interval(self._tick, 0.4)
        return root

    def _build_ui(self):
        """用 Python 拼布局（比 KV 更容易精确控制移动端尺寸）。"""
        root = BoxLayout(orientation="vertical")

        # ---- 顶栏 ----
        top = BoxLayout(size_hint_y=None, height=dp(46), spacing=dp(5),
                        padding=[dp(6), dp(4)])

        def _top_btn(text, color=(.18, .44, .93, 1), width=52):
            btn = Button(text=text, size_hint_x=None, width=dp(width),
                         background_normal="", background_color=color,
                         color=(1, 1, 1, 1), font_size="12sp")
            return btn

        btn_toc = _top_btn("目录")
        btn_toc.bind(on_release=lambda *_: self.show_chapters())
        self.lbl_title = Label(text=self.book_title, font_size="13sp",
                               shorten=True, shorten_from="right")
        self.bind(book_title=lambda _i, v: setattr(self.lbl_title, "text", v))
        btn_bm = _top_btn("书签")
        btn_bm.bind(on_release=lambda *_: self.show_bookmarks())
        btn_open = _top_btn("打开", color=(.30, .34, .40, 1))
        btn_open.bind(on_release=lambda *_: self.pick_file())
        btn_set = _top_btn("设置")
        btn_set.bind(on_release=lambda *_: self.show_settings())

        top.add_widget(btn_toc)
        top.add_widget(self.lbl_title)
        top.add_widget(btn_bm)
        top.add_widget(btn_open)
        top.add_widget(btn_set)
        root.add_widget(top)

        # ---- 正文区（RecycleView 会做视图回收，几千段的长篇也不卡） ----
        # 外面套一层 FloatLayout：没打开书的时候盖一张"操作说明"，
        # 否则界面上完全看不出"点一段就能从那儿开始念"。
        body = FloatLayout()
        self._rv = RecycleView(viewclass=ParaView)
        # ⚠️ 不要传 default_size=None —— default_size 是 ReferenceListProperty，
        # 只接受 list/tuple，传 None 会抛
        #   ValueError: RecycleBoxLayout.default_size must be a list or a tuple type
        # 这一行曾经导致安卓上一启动就闪退。
        layout = RecycleBoxLayout(orientation="vertical", spacing=dp(1),
                                  default_size_hint=(1, None),
                                  size_hint_y=None)
        layout.bind(minimum_height=layout.setter("height"))
        self._rv.add_widget(layout)
        self._rv.data = []
        body.add_widget(self._rv)

        self.lbl_hint = Label(
            text="尚未打开书籍\n\n"
                 "点右上角「打开」选择 txt / epub\n\n"
                 "打开之后：\n"
                 "· 点正文任意一段  →  从该处开始朗读\n"
                 "· 长按任意一段    →  加书签 / 选段朗读\n"
                 "· 顶部「目录」    →  按章节跳转朗读\n\n"
                 "（第一次打开书籍时会自动弹出提示）",
            halign="center", valign="middle", font_size="14sp",
            color=(.55, .60, .68, 1))
        self.lbl_hint.bind(
            size=lambda w, *_: setattr(w, "text_size", (w.width - dp(40), None)))
        body.add_widget(self.lbl_hint)
        root.add_widget(body)

        # ---- 进度区（按章进度，参照番茄小说） ----
        prog_box = BoxLayout(orientation="vertical", size_hint_y=None,
                             height=dp(78), padding=[dp(10), 0])

        # 第一行：当前章节名 + 定时休眠倒计时
        head_row = BoxLayout(size_hint_y=None, height=dp(20))
        self.lbl_chapter = Label(text=self.chapter_text, font_size="12sp",
                                 halign="left", valign="middle",
                                 shorten=True, shorten_from="right")
        self.lbl_chapter.bind(
            size=lambda w, *_: setattr(w, "text_size", (w.width, w.height)))
        self.bind(chapter_text=lambda _i, v: setattr(self.lbl_chapter, "text", v))

        self.lbl_sleep = Label(text=self.sleep_text, font_size="12sp",
                               size_hint_x=None, width=dp(104),
                               halign="right", valign="middle",
                               color=(.90, .49, .13, 1))
        self.lbl_sleep.bind(
            size=lambda w, *_: setattr(w, "text_size", (w.width, w.height)))
        self.bind(sleep_text=lambda _i, v: setattr(self.lbl_sleep, "text", v))

        head_row.add_widget(self.lbl_chapter)
        head_row.add_widget(self.lbl_sleep)

        # 第二行：本章进度条（点击或拖动都能跳转）
        self.slider = Slider(min=0, max=1, value=0)
        self.slider.bind(on_touch_down=self._slider_down,
                         on_touch_up=self._slider_up)

        # 第三行：本章已播 / 本章总时长 / 百分比
        self.lbl_progress = Label(text=self.progress_text, font_size="12sp",
                                  size_hint_y=None, height=dp(18),
                                  halign="left", valign="middle")
        self.lbl_progress.bind(
            size=lambda w, *_: setattr(w, "text_size", (w.width, w.height)))
        self.bind(progress_text=lambda _i, v: setattr(self.lbl_progress, "text", v))

        prog_box.add_widget(head_row)
        prog_box.add_widget(self.slider)
        prog_box.add_widget(self.lbl_progress)
        root.add_widget(prog_box)

        # ---- 控制行 ----
        ctrl = BoxLayout(size_hint_y=None, height=dp(56), spacing=dp(6),
                         padding=[dp(8), dp(4)])
        self.btn_prev = Button(text="上一段", background_normal="",
                               background_color=(.30, .34, .40, 1), color=(1, 1, 1, 1),
                               font_size="12sp")
        self.btn_prev.bind(on_release=lambda *_: self.jump_paragraph(-1))
        self.btn_play = Button(text="▶ 播放", background_normal="",
                               background_color=(.18, .44, .93, 1), color=(1, 1, 1, 1),
                               font_size="14sp")
        self.btn_play.bind(on_release=lambda *_: self.toggle_play())
        self.btn_stop = Button(text="■", background_normal="",
                               background_color=(.30, .34, .40, 1), color=(1, 1, 1, 1))
        self.btn_stop.bind(on_release=lambda *_: self._engine.stop())
        ctrl.add_widget(self.btn_prev)
        ctrl.add_widget(self.btn_play)
        ctrl.add_widget(self.btn_stop)
        root.add_widget(ctrl)
        return root

    # ============================================================
    #                      打开书籍
    # ============================================================
    def pick_file(self):
        """调系统文件选择器（SAF）。不需要存储权限。"""
        if not self._android():
            self._toast("桌面环境请把书放到应用数据目录后重启")
            return
        try:
            from android import activity
            from jnius import autoclass
            Intent = autoclass("android.content.Intent")
            PythonActivity = autoclass("org.kivy.android.PythonActivity")

            intent = Intent(Intent.ACTION_OPEN_DOCUMENT)
            intent.addCategory(Intent.CATEGORY_OPENABLE)
            intent.setType("*/*")
            intent.putExtra(Intent.EXTRA_MIME_TYPES,
                            ["text/plain", "application/epub+zip", "application/octet-stream"])
            activity.bind(on_activity_result=self._on_activity_result)
            PythonActivity.mActivity.startActivityForResult(intent, REQUEST_PICK_BOOK)
        except Exception as err:
            self._toast(f"打开文件选择器失败：{err}")

    @staticmethod
    def _android():
        return "ANDROID_ARGUMENT" in os.environ or "ANDROID_ROOT" in os.environ

    def _on_activity_result(self, request_code, result_code, intent):
        """文件选择器返回：把选中的文件复制进应用私有目录再打开。"""
        if request_code != REQUEST_PICK_BOOK:
            return
        try:
            from android import activity
            activity.unbind(on_activity_result=self._on_activity_result)
        except Exception:
            pass
        if intent is None:
            return
        try:
            uri = intent.getData()
            if uri is None:
                return
            path = self._copy_uri_to_private(uri)
            if path:
                self.open_book(path)
        except Exception as err:
            self._toast(f"读取所选文件失败：{err}")

    def _copy_uri_to_private(self, uri):
        """把 SAF 的 content:// 复制成本地文件，返回新路径。

        用 openFileDescriptor + os.dup(fd) 的方式读取：Java 与 Python 在同一进程，
        文件描述符可以直接复用，比逐字节读取快得多。
        """
        from jnius import autoclass
        PythonActivity = autoclass("org.kivy.android.PythonActivity")
        resolver = PythonActivity.mActivity.getContentResolver()

        name = self._query_display_name(resolver, uri) or "book.txt"
        safe = "".join(ch for ch in name if ch not in '\\/:*?"<>|').strip() or "book.txt"
        books_dir = os.path.join(self.user_data_dir, "books")
        os.makedirs(books_dir, exist_ok=True)
        dest = os.path.join(books_dir, safe)

        pfd = resolver.openFileDescriptor(uri, "r")
        try:
            fd = pfd.getFd()
            dup = os.dup(fd)
            with os.fdopen(dup, "rb") as src, open(dest, "wb") as dst:
                shutil.copyfileobj(src, dst, 1024 * 256)
        finally:
            try:
                pfd.close()
            except Exception:
                pass
        return dest

    @staticmethod
    def _query_display_name(resolver, uri):
        """从 ContentResolver 查出文件名。"""
        try:
            from jnius import autoclass
            OpenableColumns = autoclass("android.provider.OpenableColumns")
            cursor = resolver.query(uri, None, None, None, None)
            try:
                if cursor is not None and cursor.moveToFirst():
                    idx = cursor.getColumnIndex(OpenableColumns.DISPLAY_NAME)
                    if idx >= 0:
                        return str(cursor.getString(idx))
            finally:
                if cursor is not None:
                    cursor.close()
        except Exception:
            pass
        return None

    def open_book(self, path):
        """解析书籍 → 填充阅读区 → 载入引擎 → 恢复断点。"""
        try:
            doc = parse_book_file(path)
        except BookParseError as err:
            self._toast(f"打开失败：{err}")
            return
        except Exception as err:
            self._toast(f"解析书籍时出错：{err}")
            return

        self._engine.stop()
        self._paragraphs = doc.paragraphs
        offsets, total = [0], 0
        for para in doc.paragraphs:
            total += len(para)
            offsets.append(total)
        self._offsets, self._total_chars = offsets, total
        self._book_path = doc.path
        self._book_key = ConfigManager.book_key(doc.path)
        self._chapters = [(t, s) for t, s in doc.chapters
                          if 0 <= s < len(doc.paragraphs)]

        self.book_title = doc.title
        self._engine.load(doc.paragraphs)
        self._refresh_view()

        saved = self._config.get_position(self._book_key)
        start = saved[0] if saved else 0
        start = max(0, min(start, len(self._paragraphs) - 1))
        self._engine.seek_paragraph(start)
        self._set_highlight(start)

        # 初始化底部"当前章节"显示（进度条是按章的，得先知道在哪一章）
        _ci, ctitle, _cs, _ce = self._chapter_at(start)
        self.chapter_text = ctitle or ""

        self._config.set("last_book", doc.path)
        if not saved:
            self._config.set_position(self._book_key, start, 0)
        self._config.save()
        self._toast(f"已打开《{doc.title}》 共 {len(self._paragraphs)} 段")

        # 第一次打开书时弹一次操作说明——点段落/长按这些手势在界面上没有痕迹，
        # 不提示的话用户根本发现不了。
        if not self._config.get("hint_shown", False):
            self._config.set("hint_shown", True)
            self._config.save()
            Clock.schedule_once(lambda _dt: self.show_usage_hint(), 1.0)

    def show_usage_hint(self):
        """操作说明弹窗（首次打开书籍时自动出现，之后可从设置里再调出）。"""
        popup = Popup(title="怎么用", size_hint=(0.88, None), height=dp(320))
        box = BoxLayout(orientation="vertical", spacing=dp(8), padding=dp(14))
        tips = Label(
            text="· 点正文任意一段  →  从该处开始朗读\n\n"
                 "· 长按任意一段    →  加书签 / 选段朗读\n\n"
                 "· 顶部「目录」    →  按章节跳转朗读\n\n"
                 "· 底部进度条      →  拖动跳转任意位置\n\n"
                 "· 底部 ▶          →  播放 / 暂停（熄屏也会继续念）",
            halign="left", valign="top", font_size="13sp")
        tips.bind(size=lambda w, *_: setattr(w, "text_size", (w.width, None)))
        box.add_widget(tips)
        btn = Button(text="知道了", size_hint_y=None, height=dp(46),
                     background_normal="", background_color=(.18, .44, .93, 1),
                     color=(1, 1, 1, 1))
        btn.bind(on_release=lambda *_: popup.dismiss())
        box.add_widget(btn)
        popup.content = box
        popup.open()

    def _restore_last_book(self, *_):
        last = str(self._config.get("last_book", ""))
        if last and os.path.isfile(last):
            self.open_book(last)
        else:
            self._toast("点右上角「打开」选择 txt / epub")

    def _refresh_view(self):
        """重建正文列表数据。"""
        if self._rv is None:
            return
        self._rv.data = [{"text": p, "index": i, "reader_font_size": self.reader_font,
                          "active": i == self.highlight_index}
                         for i, p in enumerate(self._paragraphs)]
        self._update_hint()

    def _update_hint(self):
        """没有书籍时显示操作说明；有书时收起，并禁止它拦截触摸。"""
        if not hasattr(self, "lbl_hint"):
            return
        empty = not self._paragraphs
        self.lbl_hint.opacity = 1 if empty else 0
        self.lbl_hint.disabled = not empty

    def _set_highlight(self, index):
        """只更新高亮那一段，避免整本书重建视图。"""
        self.highlight_index = index
        data = self._rv.data
        for i, item in enumerate(data):
            item["active"] = (i == index)
        self._rv.refresh_from_data()
        self._scroll_to(index)

    def _scroll_to(self, index):
        """把朗读中的段落滚进视野（按比例估算，够用且稳定）。"""
        total = len(self._paragraphs)
        if total <= 1 or self._rv is None:
            return
        self._rv.scroll_y = max(0.0, min(1.0, 1.0 - index / float(total - 1)))

    # ============================================================
    #                      播放控制
    # ============================================================
    def tap_paragraph(self, index):
        """点正文某一段 → 从这一段开始朗读。"""
        if not self._paragraphs:
            return
        index = max(0, min(int(index), len(self._paragraphs) - 1))
        self._engine.play(index)
        self._set_highlight(index)
        self._toast(f"从第 {index + 1} 段开始朗读")

    def long_press_paragraph(self, index):
        """长按正文段落 → 弹出操作菜单（朗读 / 书签）。

        书签功能之前完全没接到界面上（config_manager 里的接口都白写了），
        长按是最自然的移动端入口。
        """
        if not self._paragraphs:
            return
        index = max(0, min(int(index), len(self._paragraphs) - 1))
        has_bookmark = any(b.get("para") == index
                           for b in self._config.get_bookmarks(self._book_key))

        popup = Popup(title="第 %d 段" % (index + 1), size_hint=(0.88, None),
                      height=dp(292))
        box = BoxLayout(orientation="vertical", spacing=dp(6), padding=dp(10))

        preview = Label(text=self._paragraphs[index][:70], font_size="12sp",
                        size_hint_y=None, height=dp(44), halign="left",
                        valign="top", color=(.62, .67, .74, 1))
        preview.bind(size=lambda w, *_: setattr(w, "text_size", (w.width, w.height)))
        box.add_widget(preview)

        def _action(text, callback, color=(.24, .27, .32, 1)):
            btn = Button(text=text, size_hint_y=None, height=dp(46),
                         background_normal="", background_color=color,
                         color=(1, 1, 1, 1), font_size="13sp")
            def _run(*_):
                popup.dismiss()
                callback()
            btn.bind(on_release=_run)
            return btn

        box.add_widget(_action("▶ 从这里开始朗读",
                               lambda: self.tap_paragraph(index)))
        if has_bookmark:
            box.add_widget(_action("× 删除此处书签",
                                   lambda: self.remove_bookmark(index),
                                   color=(.55, .25, .25, 1)))
        else:
            box.add_widget(_action("＋ 在此处添加书签",
                                   lambda: self.add_bookmark(index)))
        box.add_widget(_action("书签列表", self.show_bookmarks))
        popup.content = box
        popup.open()

    # ============================================================
    #                      书签
    # ============================================================
    def add_bookmark(self, index):
        """在指定段落添加书签（同一段落不重复添加）。"""
        if not self._book_key:
            self._toast("请先打开一本书")
            return
        index = max(0, min(int(index), len(self._paragraphs) - 1))
        label = self._paragraphs[index][:16]
        if self._config.add_bookmark(self._book_key, index, label):
            self._config.save()
            self._toast("已添加书签：第 %d 段" % (index + 1))
        else:
            self._toast("该位置已有书签")

    def remove_bookmark(self, index):
        if self._config.remove_bookmark(self._book_key, index):
            self._config.save()
            self._toast("已删除书签")
        else:
            self._toast("没有找到该书签")

    def clear_bookmarks(self):
        if not self._book_key:
            return
        self._config.clear_bookmarks(self._book_key)
        self._config.save()
        self._toast("已清空本书书签")

    def show_bookmarks(self):
        """书签列表：点条目跳转并朗读。"""
        bookmarks = (self._config.get_bookmarks(self._book_key)
                     if self._book_key else [])
        if not bookmarks:
            self._toast("本书还没有书签（长按正文段落可添加）")
            return
        from kivy.uix.scrollview import ScrollView
        popup = Popup(title="书签（%d 条）" % len(bookmarks),
                      size_hint=(0.92, 0.8))
        box = BoxLayout(orientation="vertical")
        scroll = ScrollView()
        inner = BoxLayout(orientation="vertical", size_hint_y=None, spacing=dp(2))
        inner.bind(minimum_height=inner.setter("height"))
        for bookmark in bookmarks:
            para = int(bookmark.get("para", 0))
            btn = Button(text="第 %d 段 · %s" % (para + 1, bookmark.get("label", "")),
                         size_hint_y=None, height=dp(46), halign="left",
                         valign="middle", background_normal="",
                         background_color=(.24, .27, .32, 1), color=(1, 1, 1, 1),
                         font_size="13sp")
            btn.bind(size=lambda w, *_: setattr(w, "text_size",
                                                (w.width - dp(20), None)))
            btn.bind(on_release=lambda _b, p=para: self._goto_bookmark(popup, p))
            inner.add_widget(btn)
        scroll.add_widget(inner)
        box.add_widget(scroll)

        btn_clear = Button(text="清空本书书签", size_hint_y=None, height=dp(46),
                           background_normal="",
                           background_color=(.55, .25, .25, 1), color=(1, 1, 1, 1))
        def _clear(*_):
            popup.dismiss()
            self.clear_bookmarks()
        btn_clear.bind(on_release=_clear)
        box.add_widget(btn_clear)
        popup.content = box
        popup.open()

    def _goto_bookmark(self, popup, para):
        popup.dismiss()
        para = max(0, min(int(para), len(self._paragraphs) - 1))
        self._engine.play(para)
        self._set_highlight(para)
        self._toast("已跳转到书签：第 %d 段" % (para + 1))

    def toggle_play(self):
        if not self._paragraphs:
            self._toast("请先打开一本书")
            return
        state = self._engine.get_state()
        if state == STATE_PLAYING:
            self._engine.pause()
        elif state == STATE_PAUSED:
            self._engine.resume()
        else:
            para, _, _ = self._engine.get_position()
            self._engine.play(para)

    def jump_paragraph(self, delta):
        if not self._paragraphs:
            return
        para, _, _ = self._engine.get_position()
        target = max(0, min(para + delta, len(self._paragraphs) - 1))
        self._engine.seek_paragraph(target)
        self._set_highlight(target)
        self._save_position()

    def _slider_down(self, _slider, touch):
        if _slider.collide_point(*touch.pos):
            self._dragging = True
        return False

    def _slider_up(self, _slider, touch):
        if self._dragging:
            self._dragging = False
            self.seek_fraction(self.slider.value)
        return False

    def seek_fraction(self, fraction):
        """拖动进度条 → 在**本章范围内**跳转（与进度条的章内语义保持一致）。"""
        if not self._paragraphs or self._total_chars <= 0:
            return
        para_now = self._engine.get_position()[0]
        _ci, _ct, cstart, cend = self._chapter_at(para_now)
        lo, hi = self._chapter_char_span(cstart, cend)
        target = lo + int(max(0.0, min(1.0, fraction)) * (hi - lo))
        # 关键：夹在本章内。hi 是「下一章第一段」的起点，不夹的话拖到最右
        # 会直接跳进下一章，进度条随即又变回 0%，体验很怪。
        target = min(target, hi - 1)
        index = bisect.bisect_right(self._offsets,
                                    min(target, self._total_chars - 1)) - 1
        index = max(0, min(index, len(self._paragraphs) - 1))
        self._engine.seek_paragraph(index)
        self._set_highlight(index)
        self._save_position()

    # ============================================================
    #                      引擎回调
    # ============================================================
    def _chapter_at(self, para_index):
        """返回当前段所属章节的 (序号, 标题, 起始段, 结束段)。

        没有识别到章节时，把整本书当作一个章节。
        """
        total_para = len(self._paragraphs)
        if total_para == 0:
            return 0, "", 0, 0
        if not self._chapters:
            return 0, self.book_title, 0, total_para - 1
        idx = 0
        for i, (_title, start) in enumerate(self._chapters):
            if start <= para_index:
                idx = i
            else:
                break
        title = self._chapters[idx][0]
        start = self._chapters[idx][1]
        end = (self._chapters[idx + 1][1] - 1
               if idx + 1 < len(self._chapters) else total_para - 1)
        return idx, title, max(0, start), max(start, end)

    def _chapter_char_span(self, cstart, cend):
        """章节对应的字符区间 [起, 止)，用于把进度换算到章内比例。"""
        offsets = self._offsets
        lo = offsets[min(cstart, len(offsets) - 1)]
        hi = offsets[min(cend + 1, len(offsets) - 1)]
        return lo, max(lo + 1, hi)

    def _on_progress(self, char_pos, total):
        """刷新进度。

        进度条是**按章**的（参照番茄小说）：每一章有自己独立的进度，
        而不是整本书一条到底 —— 整本书动辄几百小时，那条进度条没有参考价值。
        """
        if total <= 0 or self._dragging or not hasattr(self, "slider"):
            return
        para_now = self._engine.get_position()[0]
        _ci, ctitle, cstart, cend = self._chapter_at(para_now)
        lo, hi = self._chapter_char_span(cstart, cend)
        span = hi - lo
        fraction = max(0.0, min(1.0, (char_pos - lo) / float(span)))
        self.slider.value = fraction

        if ctitle and ctitle != self.chapter_text:
            self.chapter_text = ctitle

        speed = max(0.1, float(self._config.get("speed", 1.0)))
        # 用引擎实测的语速（倍速 1.0 基准）乘以当前倍速估算，比硬编码准得多
        cps = max(0.5, self._engine.get_cps()) * speed
        total_sec = span / cps
        self.progress_text = "本章 %s / %s  %.1f%%" % (
            self._fmt(fraction * total_sec), self._fmt(total_sec), fraction * 100)

    @staticmethod
    def _fmt(seconds):
        seconds = max(0, int(seconds))
        m, s = divmod(seconds, 60)
        return "%02d:%02d" % (m, s)

    def _on_paragraph(self, index):
        Clock.schedule_once(lambda _dt: self._set_highlight(index), 0)

    def _on_state(self, state):
        def _apply(_dt):
            self.play_label = "‖ 暂停" if state == STATE_PLAYING else "▶ 播放"
            # 界面可能还没建好（引擎初始化回调早于 build 完成）
            if hasattr(self, "btn_play"):
                self.btn_play.text = self.play_label
        Clock.schedule_once(_apply, 0)

    def _on_finished(self):
        Clock.schedule_once(lambda _dt: self._toast("本书朗读完毕"), 0)

    def _on_error(self, message):
        """错误提示：同样的信息只弹一次，避免连续失败时刷屏。"""
        key = str(message)
        if key in self._shown_errors:
            return
        # 上限保护：跑一整本书可能积累很多不同错误，别让集合无限长大
        if len(self._shown_errors) > 50:
            self._shown_errors.clear()
        self._shown_errors.add(key)
        Clock.schedule_once(lambda _dt: self._toast(key), 0)

    def _on_voices(self, voices):
        """音色列表就绪：记进配置备查，设置面板会用到。"""
        self._voice_list = voices
        current = str(self._config.get("voice_name", ""))
        if current:
            self._engine.set_voice(current)

    def _save_position(self):
        if not self._book_key:
            return
        para, char, _ = self._engine.get_position()
        self._config.set_position(self._book_key, para, char)

    def _tick(self, *_):
        """定时任务（每 0.4 秒）：保存断点 + 刷新定时休眠倒计时。

        注意休眠用**时间戳**而不是"每 tick 减 1"——本函数每 0.4 秒就跑一次，
        按 tick 计数会让倒计时快 2.5 倍（30 分钟变成 12 分钟就停）。
        """
        if self._engine.get_state() == STATE_PLAYING:
            self._save_position()
            # 用引擎的插值位置刷新进度条：否则它只在每读完一个朗读块时跳一格，
            # 长句会明显一顿一顿。0.4 秒刷一次，看起来是连续推进的。
            _para, char_now, total_chars = self._engine.get_position()
            self._on_progress(char_now, total_chars)
        if self._sleep_until > 0:
            left = self._sleep_until - time.time()
            if left <= 0:
                self._sleep_until = 0
                self.sleep_text = ""
                self._engine.stop()
                self._toast("定时时间到，已自动停止朗读")
            else:
                self.sleep_text = "剩余 %02d:%02d" % divmod(int(left), 60)
        return True

    # ============================================================
    #                      弹窗
    # ============================================================
    def _toast(self, message):
        """轻量提示：底部弹一个短消息。"""
        try:
            if self._status_popup is not None:
                self._status_popup.dismiss()
            content = Label(text=str(message), font_size="13sp", halign="center")
            content.bind(size=lambda w, *_: setattr(w, "text_size", (w.width - dp(20), None)))
            self._status_popup = Popup(title="", content=content, size_hint=(0.86, None),
                                       height=dp(86), separator_height=0)
            self._status_popup.open()
            Clock.schedule_once(lambda _dt: self._close_toast(), 2.2)
        except Exception:
            print(message)

    def _close_toast(self):
        if self._status_popup is not None:
            try:
                self._status_popup.dismiss()
            except Exception:
                pass
            self._status_popup = None

    def show_chapters(self):
        """章节目录：点章节 → 跳转并开始朗读。"""
        if not self._chapters:
            self._toast("本书没有识别到章节")
            return
        from kivy.uix.scrollview import ScrollView
        box = BoxLayout(orientation="vertical")
        scroll = ScrollView()
        inner = BoxLayout(orientation="vertical", size_hint_y=None, spacing=dp(2))
        inner.bind(minimum_height=inner.setter("height"))
        popup = Popup(title="目录", size_hint=(0.92, 0.85))
        for title, start in self._chapters:
            btn = Button(text="%s    (第 %d 段)" % (title, start + 1),
                         size_hint_y=None, height=dp(44), halign="left",
                         valign="middle", background_normal="",
                         background_color=(.24, .27, .32, 1), color=(1, 1, 1, 1),
                         font_size="13sp")
            btn.bind(size=lambda w, *_: setattr(w, "text_size", (w.width - dp(20), None)))
            btn.bind(on_release=lambda _b, s=start: self._goto_chapter(popup, s))
            inner.add_widget(btn)
        scroll.add_widget(inner)
        box.add_widget(scroll)
        popup.content = box
        popup.open()

    def _goto_chapter(self, popup, start):
        popup.dismiss()
        self._engine.play(start)
        self._set_highlight(start)
        self._toast("从「第 %d 段」开始朗读" % (start + 1))

    def show_settings(self):
        """设置面板：音色 / 音调 / 语速 / 语调起伏 / 字号 / 定时休眠。"""
        from kivy.uix.checkbox import CheckBox
        from kivy.uix.spinner import Spinner

        popup = Popup(title="设置", size_hint=(0.92, 0.88))
        box = BoxLayout(orientation="vertical", spacing=dp(8), padding=dp(12))

        # ---- 音色 ----
        box.add_widget(Label(text="音色（来自系统 TTS 引擎）", size_hint_y=None,
                             height=dp(24), font_size="13sp"))
        voice_names = [v["name"] for v in getattr(self, "_voice_list", [])]
        voice_labels = {v["label"]: v["name"] for v in getattr(self, "_voice_list", [])}
        current = str(self._config.get("voice_name", ""))
        current_label = next((lbl for lbl, n in voice_labels.items() if n == current), None)
        spinner = Spinner(text=current_label or (list(voice_labels)[0] if voice_labels else "无可用音色"),
                          values=list(voice_labels), size_hint_y=None, height=dp(44),
                          background_normal="", background_color=(.24, .27, .32, 1),
                          color=(1, 1, 1, 1))

        def _pick_voice(_s, text):
            name = voice_labels.get(text)
            if name:
                self._engine.set_voice(name)
                self._config.set("voice_name", name)
                self._config.save()
        spinner.bind(text=_pick_voice)
        box.add_widget(spinner)

        # ---- 音调 ----
        box.add_widget(self._slider_row("音调", -10, 10,
                                        int(self._config.get("pitch", 0)),
                                        self._on_pitch))
        # ---- 语速 ----
        box.add_widget(self._slider_row("语速", 5, 30,
                                        int(float(self._config.get("speed", 1.0)) * 10),
                                        self._on_speed, fmt=lambda v: "%.1fx" % (v / 10.0)))

        # ---- 语调起伏 ----
        row = BoxLayout(size_hint_y=None, height=dp(40))
        row.add_widget(Label(text="语调起伏（自然抑扬顿挫）", font_size="13sp"))
        chk = CheckBox(active=bool(self._config.get("intonation", True)),
                       size_hint_x=None, width=dp(44))

        def _toggle(_c, value):
            self._engine.set_intonation(value)
            self._config.set("intonation", bool(value))
            self._config.save()
        chk.bind(active=_toggle)
        row.add_widget(chk)
        box.add_widget(row)

        # ---- 字号 ----
        box.add_widget(self._slider_row("字号", 10, 30,
                                        int(self._config.get("font_size", 15)),
                                        self._on_font))

        # ---- 定时休眠 ----
        sleep_row = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(6))
        sleep_row.add_widget(Label(text="定时休眠（分钟）", font_size="13sp"))
        spin_sleep = Spinner(text="30", values=["15", "30", "45", "60", "90", "120"],
                             size_hint_x=None, width=dp(90), background_normal="",
                             background_color=(.24, .27, .32, 1), color=(1, 1, 1, 1))
        btn_sleep = Button(text="启动", size_hint_x=None, width=dp(70),
                           background_normal="", background_color=(.18, .44, .93, 1),
                           color=(1, 1, 1, 1))

        def _start_sleep(*_):
            self._sleep_until = time.time() + int(spin_sleep.text) * 60
            popup.dismiss()
            self._toast("定时休眠已启动：%s 分钟后自动停止" % spin_sleep.text)

        def _cancel_sleep(*_):
            self._sleep_until = 0
            self.sleep_text = ""
            popup.dismiss()
            self._toast("定时休眠已取消")

        btn_sleep.bind(on_release=_start_sleep)
        btn_cancel_sleep = Button(text="取消", size_hint_x=None, width=dp(64),
                                  background_normal="",
                                  background_color=(.30, .34, .40, 1),
                                  color=(1, 1, 1, 1))
        btn_cancel_sleep.bind(on_release=_cancel_sleep)
        sleep_row.add_widget(spin_sleep)
        sleep_row.add_widget(btn_sleep)
        sleep_row.add_widget(btn_cancel_sleep)
        box.add_widget(sleep_row)

        btn_help = Button(text="使用说明", size_hint_y=None, height=dp(44),
                          background_normal="",
                          background_color=(.30, .34, .40, 1), color=(1, 1, 1, 1))
        def _show_help(*_):
            popup.dismiss()
            self.show_usage_hint()
        btn_help.bind(on_release=_show_help)
        box.add_widget(btn_help)

        btn_close = Button(text="关闭", size_hint_y=None, height=dp(46),
                           background_normal="", background_color=(.18, .44, .93, 1),
                           color=(1, 1, 1, 1))
        btn_close.bind(on_release=lambda *_: popup.dismiss())
        box.add_widget(btn_close)
        popup.content = box
        popup.open()

    def _slider_row(self, title, lo, hi, value, callback, fmt=None):
        """一行「标题 + 滑块 + 当前值」。"""
        row = BoxLayout(size_hint_y=None, height=dp(40), spacing=dp(6))
        row.add_widget(Label(text=title, size_hint_x=None, width=dp(64), font_size="13sp"))
        slider = Slider(min=lo, max=hi, value=value)
        shown = fmt(value) if fmt else str(value)
        value_lbl = Label(text=shown, size_hint_x=None, width=dp(56), font_size="12sp")
        slider.bind(value=lambda _s, v: (
            setattr(value_lbl, "text", fmt(v) if fmt else str(int(v))),
            callback(v)))
        row.add_widget(slider)
        row.add_widget(value_lbl)
        return row

    def _on_pitch(self, value):
        self._engine.set_pitch(int(value))
        self._config.set("pitch", int(value))

    def _on_speed(self, value):
        speed = value / 10.0
        self._engine.set_speed(speed)
        self._config.set("speed", speed)

    def _on_font(self, value):
        """字号滑块：立即更新数据，但把重量级的视图重建**防抖**到停手之后。

        原来每动一下都 refresh_from_data()，在几万段的书上拖动会反复重建视图，
        明显卡顿。现在改成 0.35 秒内的连续变化只刷新一次。
        """
        self.reader_font = float(value)
        self._config.set("font_size", int(value))
        for item in self._rv.data:
            item["reader_font_size"] = self.reader_font
        if self._font_event is not None:
            self._font_event.cancel()
        self._font_event = Clock.schedule_once(self._apply_font_refresh, 0.35)

    def _apply_font_refresh(self, *_):
        self._font_event = None
        if self._rv is not None:
            self._rv.refresh_from_data()

    # ============================================================
    #                      退出
    # ============================================================
    def on_pause(self):
        """切到后台 / 熄屏：只保存断点，**故意不暂停朗读**。

        听书基本都是熄屏听的，之前这里主动 pause 会导致一熄屏就停，
        等于把核心场景废掉。安卓 TTS 是系统级服务，Activity 暂停后
        仍会继续发声，所以这里保持播放即可。
        """
        self._save_position()
        self._config.save()
        return True

    def on_resume(self):
        return True

    def on_stop(self):
        """退出前保存断点与配置。

        注意：build() 有可能中途失败（走崩溃屏分支），此时 _config / _engine
        还是 None，这里必须容错，否则退出时会再抛一次异常。
        """
        try:
            if self._config is not None:
                self._save_position()
                self._config.save()
        except Exception:
            pass
        try:
            if self._engine is not None:
                self._engine.shutdown()
        except Exception:
            pass


if __name__ == "__main__":
    try:
        AudioBookApp().run()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
