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
from kivy.metrics import dp, sp
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
from kivy.uix.scrollview import ScrollView
from kivy.uix.slider import Slider

from book_parser import BookParseError
from book_parser import load_book as parse_book_file
from config_manager import ConfigManager
from app_diag import diag, _DIAG, _CRASH, _record_crash, _install_crash_handlers
from app_wakelock_fg import WakelockFgMixin
# ⚠️ STATE_STOPPED 也必须导入：_on_state 里用它决定「是否关闭前台服务」。
#    漏了它会抛 NameError，而异常从按钮回调冒出 → Kivy 重新抛出 → **一点暂停就闪退**。
from tts_android import STATE_PAUSED, STATE_PLAYING, STATE_STOPPED
from tts_engine import ReaderTTS

# ---- 构建标识（CI 打包时由 .github/workflows/build-apk.yml 写入）----
# 目的是让「手机上装的是哪一版」一目了然，排查问题时不用靠猜。
try:
    from build_info import SHORT as _BI_SHORT, DATE as _BI_DATE
    BUILD_TAG = "%s · %s" % (_BI_SHORT, _BI_DATE)
except Exception:
    BUILD_TAG = "dev"

# ---- jnius（仅安卓打包后存在）：用主线程 Handler 驱动朗读推进兜底，
#      以及播放时持有 PARTIAL_WAKE_LOCK（熄屏也能继续念） ----
import threading
try:
    from jnius import autoclass, PythonJavaClass, java_method
    _JNIUS_OK = True
except Exception:
    autoclass = None
    PythonJavaClass = object
    java_method = None
    _JNIUS_OK = False

if _JNIUS_OK:
    class _TickRunnable(PythonJavaClass):
        """把 _tick 投递到安卓主线程执行。

        关屏后 Kivy 的逐帧时钟会停摆，但安卓主线程的 Looper 照常运转，
        所以走 Handler.post 才能保证「读完一句自动续下一句」在熄屏时也生效。
        """
        __javainterfaces__ = ["java/lang/Runnable"]
        __javacontext__ = "app"

        def __init__(self, cb):
            super().__init__()
            self.cb = cb

        @java_method("()V")
        def run(self):
            try:
                self.cb()
            except Exception:
                pass
else:
    class _TickRunnable(object):
        pass

# ---- 诊断 / 崩溃留痕已抽到 app_diag.py ----
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
REQUEST_EXPORT_CFG = 1002   # 导出书签/进度备份（ACTION_CREATE_DOCUMENT）

KV = """
# ============================================================
#  全局控件样式：统一圆角 + 扁平配色
#  按钮底色统一由 background_color 决定（下面用 canvas 自己画圆角矩形），
#  这样各处 new Button(background_color=...) 的写法不用改。
# ============================================================
<Button>:
    background_normal: ''
    background_down: ''
    color: 0.90, 0.92, 0.95, 1
    font_size: '14sp'
    canvas.before:
        Color:
            rgba: self.background_color
        RoundedRectangle:
            pos: self.pos
            size: self.size
            radius: [dp(10)]

<Slider>:
    # 滑块加大，手机上更好按
    cursor_size: dp(24), dp(24)
    cursor_image: ''

<ChapterRow>:
    # 章节目录里的一行（固定行高，配合 RecycleView 虚拟化）
    halign: 'left'
    valign: 'middle'
    text_size: self.width - dp(20), None
    padding_x: dp(10)
    font_size: '13sp'
    color: 0.90, 0.92, 0.95, 1
    background_color: 0.18, 0.21, 0.26, 1

<ParaView>:
    # 一段正文：点一下就从这段开始读；朗读中的那一段有底色。
    # 注意用 reader_font_size 而不是直接覆盖 font_size —— Label.font_size 自带
    # 'sp' 单位语义，重定义会破坏它。
    active: False
    line_height: root.line_height_factor   # 行距系数（设置里可调，1.0=系统默认）
    font_size: str(int(root.reader_font_size)) + 'sp'
    text_size: self.width - dp(24), None
    size_hint_y: None
    height: max(dp(30), self.texture_size[1] + dp(16))
    halign: 'left'
    valign: 'top'
    padding_x: dp(12)
    canvas.before:
        Color:
            rgba: root.bg_color
        RoundedRectangle:
            pos: self.x + dp(2), self.y + dp(1)
            size: self.width - dp(4), self.height - dp(2)
            radius: [dp(8)]
"""


# ---- 配色（深色主题）----
C_BG = (0.09, 0.10, 0.12, 1)        # 页面背景
C_SURFACE = (0.13, 0.15, 0.18, 1)   # 顶栏 / 弹窗
C_BTN = (0.18, 0.21, 0.26, 1)       # 普通按钮
C_PRIMARY = (0.23, 0.51, 0.96, 1)   # 主按钮（播放）
C_DANGER = (0.50, 0.22, 0.24, 1)    # 危险按钮（清空书签）
C_TEXT = (0.90, 0.92, 0.95, 1)      # 主文字
C_DIM = (0.55, 0.60, 0.68, 1)       # 次要文字

# 用户自己翻过之后，暂停「朗读自动跟随」的秒数。
# 没有这个缓冲，朗读每换一段就把视图拽回高亮处，
# 表现就是右侧滑块拖不动、刚拖走又跳回原处。
AUTO_FOLLOW_PAUSE = 6.0


class ParaView(Label):
    """正文里的一段。

    注意：只用于**当前章节**（不是整本书）。整本 15 万段一次性渲染，
    手机上必然错位卡死 —— 番茄小说也是按章加载的。
    index 是**本章内**的下标；映射到全书下标由主窗口负责。
    """

    active = BooleanProperty(False)
    index = NumericProperty(0)
    reader_font_size = NumericProperty(15)
    line_height_factor = NumericProperty(1.0)
    bg_color = ListProperty([0, 0, 0, 0])

    def on_active(self, *_):
        self.bg_color = [1.0, 0.90, 0.35, 1.0] if self.active else [0, 0, 0, 0]

    def on_touch_down(self, touch):
        diag(f"[PV] down {touch.pos} collide={self.collide_point(*touch.pos)}")
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
        diag(f"[PV] move {touch.pos}")
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


class ChapterRow(RecycleDataViewBehavior, Button):
    """章节目录里的一行。

    固定行高 → 适合用 RecycleView：只创建可见的十几行。
    之前一次性 new 出全部 1142 个按钮，手机上要卡 1~2 秒。
    """

    index = NumericProperty(0)      # 第几章

    def on_release(self):
        app = App.get_running_app()
        if app is not None:
            app.goto_chapter_index(self.index)


class AudioBookApp(App, WakelockFgMixin):
    """主应用：书籍加载、播放调度、界面刷新。"""

    # ---- 供 KV 绑定的属性 ----
    book_title = StringProperty("未打开书籍")
    chapter_text = StringProperty("")
    progress_text = StringProperty("00:00 / 00:00  0.0%")
    sleep_text = StringProperty("")
    play_label = StringProperty("▶ 播放")
    reader_font = NumericProperty(15)
    line_height = NumericProperty(1.0)   # 正文行距系数（1.0=系统默认，最大 2.0）
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
        self._scroll = None         # 正文滚动区
        self._last_user_scroll = 0.0    # 用户最后一次自己翻页的时刻
        self._setting_scroll = False    # True=当前是程序在设置 scroll_y
        self._follow = True             # 朗读时是否把高亮段滚到正中；
                                        # 用户一旦手动翻页就置 False，交还自由浏览，
                                        # 直到再次点段落/播放/跳章才恢复跟随
        self._text_box = None       # 正文容器（只装当前章节）
        self._view_chapter = -1     # 当前渲染的是第几章（-1 = 未渲染）
        self._view_start = 0        # 当前渲染的首段在全书中的下标
        self._chapter_popup = None  # 章节目录弹窗（点行后要关掉它）
        self._status_popup = None
        self._voice_list = []       # 系统音色列表（引擎初始化后填充）
        self._shown_errors = set()  # 已提示过的错误，避免连续失败时刷屏
        self._font_event = None     # 字号刷新防抖用

        # ---- 朗读推进兜底时钟（独立于 Kivy 逐帧时钟，熄屏也能跑） ----
        self._tick_running = False
        self._tick_thread = None
        self._handler = None
        self._tick_runnable = None
        # ---- 播放时的 PARTIAL_WAKE_LOCK（熄屏后 CPU 不睡，朗读不断） ----
        self._wake_lock = None
        self._wake_lock_tag = "AudioBookReader::play"
        # ---- 自检信息（显示在设置面板里，方便无 adb 时截图定位）----
        self._tick_count = 0        # _tick 累计执行次数
        self._tick_mode = "未启动"   # Handler / Clock / 未启动
        self._wake_error = ""       # wakelock 申请失败原因
        self._last_error = ""       # 最近一次错误（显示在自检信息里，便于截图定位）
        self._last_persist = 0.0    # 断点上次落盘的时刻（限流用，避免每 0.2s 写盘）
        self._layout_fixes = 0      # 布局保镖纠正错位的次数（自检 fix= 字段）
        # ---- 前台服务（熄屏/后台朗读保活）----
        self._fg_started = False
        self._fg_error = ""
        # ---- 通知栏媒体控制（MediaSession）----
        self._media_ok = False
        self._media_cb = None
        self._media_error = ""
        self._is_playing = False

    # ============================================================
    #                        启动
    # ============================================================
    def build(self):
        self.title = "有声书朗读 " + BUILD_TAG
        # ⚠️ 页面背景：C_BG 之前**只定义了却从未使用**，界面露出的是 Window 默认的
        # **纯黑**，看起来就像「顶部压了一层黑色覆盖层」。这里真正把它用上。
        try:
            from kivy.core.window import Window
            Window.clearcolor = C_BG
        except Exception:
            pass
        # 启动阶段的任何异常都渲染到屏幕上。
        # 安卓上普通用户拿不到 logcat，这是唯一能让用户把真实报错反馈回来的办法。
        try:
            return self._build_real()
        except Exception:
            return self._build_crash_screen(traceback.format_exc())

    def _build_crash_screen(self, tb_text):
        """把启动失败的 traceback 直接显示在屏幕上（可滚动、可截图）。"""
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

        # 调试：把关键事件写进文件，便于在真机上定位「为什么滑不动」
        os.makedirs(self.user_data_dir, exist_ok=True)
        _DIAG["path"] = os.path.join(self.user_data_dir, "diag.log")
        try:
            open(_DIAG["path"], "w", encoding="utf-8").close()
        except Exception:
            pass
        # 接住异常并留痕（闪退排查用）
        _install_crash_handlers()

        # 配置与语音引擎都放在应用私有目录（安卓上必然可写）
        cfg_path = os.path.join(self.user_data_dir, "config.json")
        self._config = ConfigManager(cfg_path)
        self.reader_font = float(self._config.get("font_size", 15))
        self.line_height = max(1.0, min(2.0, float(self._config.get("line_height", 1.0))))

        self._engine = ReaderTTS(
            on_progress=self._on_progress,
            on_paragraph=self._on_paragraph,
            on_state=self._on_state,
            on_finished=self._on_finished,
            on_error=self._on_error,
            on_voices=self._on_voices,
            user_data_dir=self.user_data_dir,
        )
        self._engine.start()
        self._engine.set_speed(float(self._config.get("speed", 1.0)))
        self._engine.set_pitch(int(self._config.get("pitch", 0)))
        self._engine.set_intonation(bool(self._config.get("intonation", True)))

        root = self._build_ui()

        # 自动恢复上次的书；并轮询刷新进度
        Clock.schedule_once(self._restore_last_book, 0.6)
        # 0.2 秒一次：_tick 里除了刷进度，还负责「朗读推进兜底」
        # （轮询 isSpeaking，不依赖安卓的 onDone 回调）。
        # 关键：用独立后台线程 + 安卓主线程 Handler 驱动，**不**依赖 Kivy 的逐帧时钟——
        # 因为熄屏后 Kivy 的帧时钟会停摆，导致「读完一句就卡住、不再续读」。
        self._setup_tick_loop()
        self._start_tick_loop()
        return root

    def _enforce_layout(self, *_dt):
        """布局保镖：每帧把 4 个区块钉在正确位置，错位就当场纠正并计数。

        设备上出现过诡异状态：两种完全不同的布局方案（手动绝对定位 /
        标准 BoxLayout）都把 body 尺寸算对、y 却停在 0——自检数据为
        bTop==body 高度、gap==底部两行总高，表现为顶部约 1/4 黑区 +
        正文穿透进度条/按钮行；桌面（同为 Kivy 2.3.1）无法复现。
        与其继续猜设备上是谁在布局后改位置，不如直接以 root 尺寸为准
        每帧强制钉死；自检 fix= 字段非 0 即说明设备上确有东西在改布局。
        """
        try:
            root = self._body.parent
            W, H = root.width, root.height
            top_h = self._top_w.height
            prog_h = self._prog_box.height
            ctrl_h = self._ctrl.height
            body_h = max(0, H - top_h - prog_h - ctrl_h)
            want = (
                (self._top_w, 0, H - top_h, W, top_h),
                (self._body, 0, prog_h + ctrl_h, W, body_h),
                (self._prog_box, 0, ctrl_h, W, prog_h),
                (self._ctrl, 0, 0, W, ctrl_h),
            )
            for w, x, y, ww, hh in want:
                if (abs(w.x - x) > 1 or abs(w.y - y) > 1
                        or abs(w.width - ww) > 1 or abs(w.height - hh) > 1):
                    w.pos = (x, y)
                    w.size = (ww, hh)
                    self._layout_fixes += 1
        except Exception:
            pass

    def _build_ui(self):
        """用 Python 拼布局（比 KV 更容易精确控制移动端尺寸）。"""
        # 标准 Kivy BoxLayout(vertical)：children[0] 排最**底**、后添加的排上面
        # （kivy/uix/boxlayout.py 的 _iterate_layout 从 padding_bottom 起步向上排）。
        # 按「顶栏→正文→进度→控制条」的顺序添加，顶栏自然在最上、正文紧贴其下。
        # ⚠️ 别再换 FloatLayout+绝对定位手动排位：那套在设备上 body.y 会被布局
        #    复位成 0（自检 bTop=body 高度、gap=底部两行总高），表现为顶部约
        #    1/4 黑区 + 正文穿透进度条/按钮行；BoxLayout 由 Kivy 排，不会复发。
        #    （当年「device 上方向反了」是把 g_translate 黑区误诊成了排列反转。）
        root = BoxLayout(orientation="vertical")
        # 系统导航栏高度（手势条/三键）：折算进最底排控制条，避免按钮被系统条压住
        self._nav_dp = self._nav_bar_dp()

        # ---- 顶栏 ----
        top = BoxLayout(size_hint_y=None, height=dp(46), spacing=dp(5),
                        padding=[dp(6), dp(4)])

        def _mk_btn(text, width=None, bg=C_BTN, fg=C_TEXT,
                    font_size="13sp", **kw):
            """统一构造按钮（顶部行 / 底部行共用，风格保持一致）。

            width=None   -> 按 weight 撑满父容器（底部行用法）
            width=dp 值  -> 固定宽度（顶部行用法）
            """
            btn = Button(text=text, background_color=bg, color=fg,
                         font_size=font_size, **kw)
            if width is not None:
                btn.size_hint_x = None
                btn.width = dp(width)
            return btn

        btn_toc = _mk_btn("目录", width=46)
        btn_toc.bind(on_release=lambda *_: self.show_chapters())
        self.lbl_title = Label(text=self.book_title, font_size="13sp",
                               shorten=True, shorten_from="right")
        self.bind(book_title=lambda _i, v: setattr(self.lbl_title, "text", v))
        btn_bm = _mk_btn("书签", width=46)
        btn_bm.bind(on_release=lambda *_: self.show_bookmarks())
        btn_open = _mk_btn("打开", width=46)
        btn_open.bind(on_release=lambda *_: self.pick_file())

        top.add_widget(btn_toc)
        top.add_widget(self.lbl_title)
        top.add_widget(btn_bm)
        top.add_widget(btn_open)
        root.add_widget(top)

        # ---- 正文区：只渲染「当前章节」 ----
        # 整本书塞进一个列表是行不通的（本书 153242 段），会同时引发两个症状：
        #   ① 布局尺寸算错 → 只看到章节标题、后面一片空白
        #   ② refresh_from_data() 卡死主线程 → onDone 回调回不来
        #      → 读完一段就停住，不再继续下一段
        # 番茄小说也是按章加载的。单章中位 130 段、最多 400 段，
        # 用普通控件即可，而且高度由 Kivy 自动算准
        # （RecycleView 对「高度不定的文本条目」反而算不准）。
        body = FloatLayout(size_hint_y=1)  # BoxLayout 会把它撑到占满中间剩余空间
        # 双重保险：body 自己画上 C_BG。即便 Window.clearcolor 没生效或被覆盖，
        # 正文区里的空白也显示为深灰（页面背景），不会再露 Window 默认纯黑。
        from kivy.graphics import Color, Rectangle
        with body.canvas.before:
            Color(*C_BG)
            rect = Rectangle(pos=body.pos, size=body.size)
        body.bind(pos=lambda w, v: setattr(rect, "pos", v),
                  size=lambda w, v: setattr(rect, "size", v))
        self._body = body          # 记住容器：_update_hint 要摘挂提示层
        self._top_w = top
        self._scroll = self._make_scroll()
        # ⚠️⚠️ 必须给 pos_hint！body 是 FloatLayout：**没有 pos_hint 的子控件
        # 根本不会被布局**（floatlayout.py 的 do_layout 只遍历 pos_hint 键），
        # scroll.pos 会永远停在默认 (0,0) —— 而普通控件的 pos 是窗口绝对坐标，
        # 于是正文区一直画在窗口底部：上方空出 顶栏+进度+控制条 的总高黑区、
        # 正文穿透底部两行（这正是折腾多轮的「顶部黑空区+内容下坠」真凶）。
        # 给了 pos_hint，do_layout 才会把 scroll 摆到 body 的位置上。
        self._scroll.pos_hint = {"x": 0, "y": 0}
        # 顶部留白 = 两倍 15 号字（2 × sp(15) = sp(30)）：
        # 强制正文内容从「界面顶端往下 30 字号」处开始展示，其余布局随之对齐。
        self._text_box = BoxLayout(orientation="vertical", size_hint_y=None,
                                   spacing=dp(1), padding=[0, sp(30), 0, dp(12)])

        # 内容高度 = max(内容实际高度, 视口高度)。
        # 只绑 minimum_height 的话：当某章内容比视口短时，Kivy 会把内容**贴到视口
        # 底部**，上方留出一大片黑（用户看到的「黑色遮蔽层挡住文字」）。
        # 撑满视口后，正文永远从顶部开始显示。
        def _sync_content_height(*_):
            try:
                self._text_box.height = max(self._text_box.minimum_height,
                                            self._scroll.height)
                # ⚠️ ScrollView 改 scroll_y 时不会自动重算 g_translate.y（g_translate
                # 是按 Kivy 内部规则累加的）—— 必须显式调 update_from_scroll() 才能让
                # 「顶部黑空区」消失（症状：gty 不等于 sv.y-(content-vp)）。
                self._scroll.update_from_scroll()
            except Exception:
                pass

        self._text_box.bind(minimum_height=_sync_content_height)
        self._scroll.bind(height=_sync_content_height)
        self._scroll.add_widget(self._text_box)
        body.add_widget(self._scroll)
        self._scroll.bind(scroll_y=self._on_scroll_y)
        self._scroll.bind(
            on_touch_down=lambda inst, t: diag(
                f"[SV] down {t.pos} collide={self._scroll.collide_point(*t.pos)}"))

        self.lbl_hint = Label(
            text="尚未打开书籍\n\n"
                 "点右上角「打开」选择 txt / epub\n\n"
                 "打开之后：\n"
                 "· 点正文任意一段  →  从该处开始朗读\n"
                 "· 长按任意一段    →  加书签 / 选段朗读\n"
                 "· 顶部「目录」    →  按章节跳转朗读\n\n"
                 "（第一次打开书籍时会自动弹出提示）",
            halign="center", valign="middle", font_size="14sp",
            color=C_DIM,
            pos_hint={"center_x": 0.5, "center_y": 0.5})  # 同 scroll：无 pos_hint 不会被 FloatLayout 摆位
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
        # 章节标题变化时同步刷新通知栏标题
        self.bind(chapter_text=lambda _i, _v: self._media_update(self._is_playing))

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
        self._prog_box = prog_box
        root.add_widget(prog_box)

        # ---- 控制行：上一章 / 播放-暂停 / 下一章 ----
        # 播放键按一下暂停、再按一下继续（不需要单独的停止键，
        # 停止功能挪到「设置」里，主界面保持干净）。
        # 高度额外加上系统导航栏高度，把这一条一直铺到屏幕最底（不透明底色
        # 才能盖住任何溢出），同时让按钮留在导航条上方、点得到。
        _nav = getattr(self, "_nav_dp", 0)
        ctrl = BoxLayout(size_hint_y=None, height=dp(58) + dp(_nav), spacing=dp(8),
                         padding=[dp(12), dp(5), dp(12), dp(5) + dp(_nav)])
        self.btn_prev_ch = _mk_btn("◀◀ 上一章")
        self.btn_prev_ch.bind(on_release=lambda *_: self.jump_chapter(-1))

        self.btn_play = _mk_btn("▶ 播放", bg=C_PRIMARY, fg=(1, 1, 1, 1),
                                font_size="15sp", bold=True)
        self.btn_play.bind(on_release=lambda *_: self.toggle_play())

        self.btn_next_ch = _mk_btn("下一章 ▶▶")
        self.btn_next_ch.bind(on_release=lambda *_: self.jump_chapter(+1))

        # 「设置」放在「下一章 ▶▶」右侧，与底部其余按钮同一套构造 / 风格
        self.btn_set = _mk_btn("设置")
        self.btn_set.bind(on_release=lambda *_: self.show_settings())

        ctrl.add_widget(self.btn_prev_ch)
        ctrl.add_widget(self.btn_play)
        ctrl.add_widget(self.btn_next_ch)
        ctrl.add_widget(self.btn_set)
        self._ctrl = ctrl
        root.add_widget(ctrl)
        # 布局保镖（见 _enforce_layout）：每帧核对、错位即纠，设备上若仍有
        # 东西在布局后复位 body.y，画面也会被立刻拉回正确位置，且自检 fix=
        # 计数会如实记录 —— 下次再有诡异布局，看这个数字就知道保镖在工作。
        Clock.schedule_interval(self._enforce_layout, 0)
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
        """SAF 选择器返回：书本 → 复制后打开；.json → 备份导入；导出码 → 落盘。"""
        if request_code not in (REQUEST_PICK_BOOK, REQUEST_EXPORT_CFG):
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
            if request_code == REQUEST_EXPORT_CFG:
                content = self._config.export_data()
                ok = self._write_uri_text(uri, content)
                self._post_to_main(lambda: self._toast("备份已导出" if ok else "导出失败"))
                return
            # 打开书本 / 导入备份：按扩展名分流
            resolver = None
            try:
                from jnius import autoclass as _ac
                PythonActivity = _ac("org.kivy.android.PythonActivity")
                resolver = PythonActivity.mActivity.getContentResolver()
            except Exception:
                pass
            name = (self._query_display_name(resolver, uri) if resolver else "") or ""
            if name.lower().endswith(".json"):
                data = self._read_uri_text(uri)
                self._post_to_main(lambda: self._import_backup(data))
                return
            path = self._copy_uri_to_private(uri)
            if path:
                # ⚠️ 这个回调不在 Kivy 主线程上；open_book 会创建控件（图形指令），
                #    必须投递回主线程，否则抛
                #    "Cannot create graphics instruction outside the main Kivy thread"
                #    → 正文渲染做一半 → 顶部出现黑色空区。
                self._post_to_main(lambda: self.open_book(path))
        except Exception as err:
            # 走 _on_error：既弹 toast，也记进「自检信息 → 最近错误」（便于截图定位）
            self._on_error(f"读取所选文件失败：{err}")

    def _import_backup(self, data):
        """恢复书签/进度备份（必须在 Kivy 主线程：会刷新界面）。"""
        try:
            self._config.import_data(data)
            self._toast("备份已恢复，重启应用后完全生效")
        except Exception as err:
            self._toast(f"备份导入失败：{err}")

    def _read_uri_text(self, uri):
        """读 SAF content:// 的文本内容（openFileDescriptor + dup）。"""
        from jnius import autoclass
        PythonActivity = autoclass("org.kivy.android.PythonActivity")
        resolver = PythonActivity.mActivity.getContentResolver()
        pfd = resolver.openFileDescriptor(uri, "r")
        try:
            with os.fdopen(os.dup(pfd.getFd()), encoding="utf-8") as f:
                return f.read()
        finally:
            try:
                pfd.close()
            except Exception:
                pass

    def _write_uri_text(self, uri, text):
        """把文本写进 SAF content://（导出备份用）。成功返回 True。"""
        try:
            from jnius import autoclass
            PythonActivity = autoclass("org.kivy.android.PythonActivity")
            resolver = PythonActivity.mActivity.getContentResolver()
            pfd = resolver.openFileDescriptor(uri, "w")
            try:
                with os.fdopen(os.dup(pfd.getFd()), "w", encoding="utf-8") as f:
                    f.write(text)
            finally:
                try:
                    pfd.close()
                except Exception:
                    pass
            return True
        except Exception as err:
            self._on_error(f"导出失败：{err}")
            return False

    def _export_backup(self):
        """导出书签/进度备份（系统「另存为」对话框）。"""
        if not self._android():
            self._toast("桌面环境直接复制 config.json 即可")
            return
        try:
            from android import activity
            from jnius import autoclass
            Intent = autoclass("android.content.Intent")
            PythonActivity = autoclass("org.kivy.android.PythonActivity")
            it = Intent(Intent.ACTION_CREATE_DOCUMENT)
            it.addCategory(Intent.CATEGORY_OPENABLE)
            it.setType("application/json")
            it.putExtra(Intent.EXTRA_TITLE, "audiobook_backup.json")
            activity.bind(on_activity_result=self._on_activity_result)
            PythonActivity.mActivity.startActivityForResult(it, REQUEST_EXPORT_CFG)
        except Exception as err:
            self._toast(f"导出失败：{err}")

    def _share_diag_log(self):
        """把 diag.log（含最近崩溃）以纯文本分享出去，方便反馈问题。"""
        content = ""
        try:
            with open(_DIAG.get("path", ""), encoding="utf-8") as f:
                content = f.read()
        except Exception:
            pass
        try:
            with open(_CRASH.get("path", ""), encoding="utf-8") as f:
                crash = f.read()
            if crash:
                content += chr(10) + "---- 崩溃记录 ----" + chr(10) + crash
        except Exception:
            pass
        if not content.strip():
            content = "(日志为空)"
        content = content[-8000:]      # Intent 太大系统会拒，截尾保留最近的
        if not self._android():
            self._toast("桌面日志文件：" + (_DIAG.get("path") or "-"))
            return
        try:
            from jnius import autoclass
            Intent = autoclass("android.content.Intent")
            PythonActivity = autoclass("org.kivy.android.PythonActivity")
            send = Intent(Intent.ACTION_SEND)
            send.setType("text/plain")
            send.putExtra(Intent.EXTRA_SUBJECT, "AudioBookReader 诊断日志")
            send.putExtra(Intent.EXTRA_TEXT, content)
            PythonActivity.mActivity.startActivity(
                Intent.createChooser(send, "分享诊断日志"))
        except Exception as err:
            self._toast(f"分享失败：{err}")

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

        # ⚠️ 「上次打开的书」必须**尽早落盘**：以前这句放在方法最后，只要后面
        #    任何一步（_refresh_view / _set_highlight …）抛异常，就永远存不上，
        #    表现为「每次打开 App 都要重新选书」。解析成功就立刻存。
        self._config.set("last_book", doc.path)
        self._config.save()

        saved = self._config.get_position(self._book_key)
        start = saved[0] if saved else 0
        start = max(0, min(start, len(self._paragraphs) - 1))

        try:
            self._engine.load(doc.paragraphs)
            self._refresh_view()
            self._engine.seek_paragraph(start)
            self._set_highlight(start)
            # 初始化底部"当前章节"显示（进度条是按章的，得先知道在哪一章）
            _ci, ctitle, _cs, _ce = self._chapter_at(start)
            self.chapter_text = ctitle or ""
        except Exception as err:
            # 以前这里的异常会被上层 try 吞成一句 toast，看不到细节。
            # 现在上报到「设置 → 自检信息 → 最近错误」，便于定位。
            self._on_error("打开书籍后初始化失败：%s" % err)

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
                     background_normal="", background_color=C_PRIMARY,
                     color=(1, 1, 1, 1))
        btn.bind(on_release=lambda *_: popup.dismiss())
        box.add_widget(btn)
        popup.content = box
        popup.open()

    def _restore_last_book(self, *_):
        """启动时自动恢复上次的书（并靠 config 里的断点续读）。

        断点恢复在 open_book 里（读 config 的 positions）完成，
        所以「上次的书 + 上次读到哪一章」都会自动回来。
        """
        last = str(self._config.get("last_book", ""))
        if last and not os.path.isfile(last):
            # 路径失效时退回 books/ 目录里的同名文件
            # （万一 user_data_dir 变了，也不至于让用户重新选书）
            alt = os.path.join(self.user_data_dir, "books",
                               os.path.basename(last))
            if os.path.isfile(alt):
                last = alt
        if last and os.path.isfile(last):
            self._follow = True
            self.open_book(last)
        elif last:
            self._toast("上次的书已找不到，请重新选择")
        else:
            self._toast("点右上角「打开」选择 txt / epub")

    def _refresh_view(self, global_index=None):
        """渲染**当前章节**的正文（不是整本书）。

        整本一次渲染会同时引发两个 bug（详见 _build_ui 里的注释）。
        这里按章加载：单章中位 130 段，普通控件完全够用，
        而且高度由 Kivy 自动算准。
        """
        if self._text_box is None or not self._paragraphs:
            self._update_hint()
            return
        if global_index is None:
            global_index = self._engine.get_position()[0]
        ci, ctitle, cstart, cend = self._chapter_at(global_index)

        # 同一章且已渲染过 → 不重建，只挪高亮。
        # 朗读时每段都会走到这里，绝不能每段都重建控件。
        if ci == self._view_chapter and self._text_box.children:
            self._set_highlight(global_index)
            return

        self._view_chapter = ci
        self._view_start = cstart
        self.chapter_text = ctitle or ""
        self._text_box.clear_widgets()
        for g in range(cstart, cend + 1):
            self._text_box.add_widget(ParaView(
                text=self._paragraphs[g],
                index=g - cstart,              # 章内下标，不是全书下标
                reader_font_size=self.reader_font,
                line_height_factor=self.line_height,
            ))
        # 换章后先把滚动位置顶到最上：否则会残留上一章居中时设的 scroll_y，
        # 若新章内容比视口短，Kivy 会把内容贴到底部，上方留出一大片黑
        # （用户看到的「黑色遮蔽层挡住文字」）。后面 _set_highlight 再按需居中。
        self._scroll_to_top()
        # ★ 双保险（强制）：直接设 g_translate.y，绕开 Kivy 内部 effect_y 状态
        # 与 scroll_y 不同步的问题，强制正文内容顶部 = ScrollView 顶部
        try:
            gt = self._scroll.g_translate
            gt.y = (self._scroll.y
                     - (self._text_box.height - self._scroll.height))
        except Exception:
            pass
        self._set_highlight(global_index)
        self._update_hint()
        diag(f"[load] scroll_h={self._scroll.height} content_h="
             f"{self._text_box.height} scrollable="
             f"{self._text_box.height > self._scroll.height}")

    def _update_hint(self):
        """没有书籍时显示操作说明；有书时**直接从控件树上摘掉**。

        ⚠️ 关键：不能再只靠 opacity=0 + disabled。
        提示层是加在滚动区**之后**的，FloatLayout 的触摸分发是「后添加的
        先收到」，于是它排在正文前面。只把 opacity 设成 0 它依然留在控件树
        里照样能拦截手势；disabled 在各 Kivy 版本下行为也不一致（有的版本
        反而会吞掉 touch）。实测表现就是：点顶栏按钮有反应，但正文怎么滑
        都不动。最保险的做法是直接 remove_widget —— 不在树里就不可能拦截。
        """
        if not hasattr(self, "lbl_hint") or not hasattr(self, "_body"):
            return
        empty = not self._paragraphs
        if empty:
            if self.lbl_hint.parent is None:
                self._body.add_widget(self.lbl_hint)
            self.lbl_hint.opacity = 1
            self.lbl_hint.disabled = False
        else:
            if self.lbl_hint.parent is not None:
                self.lbl_hint.parent.remove_widget(self.lbl_hint)
            self.lbl_hint.opacity = 0
            self.lbl_hint.disabled = True

    def _set_highlight(self, global_index):
        """把朗读中的那一段标黄。

        只遍历当前章节的控件（~130 个）。之前对 15 万条调
        refresh_from_data()，直接卡死主线程，导致「读完一段就不动了」。
        """
        # 目标不在当前渲染的章节里（例如拖进度条/跳书签跨了章）→ 先换章。
        # _refresh_view 内部会再调回本方法，此时章节已匹配，不会递归。
        if self._chapter_at(global_index)[0] != self._view_chapter:
            self._refresh_view(global_index)
            return

        self.highlight_index = global_index
        local = global_index - self._view_start
        # children 是倒序的，反转回正序
        for i, widget in enumerate(reversed(self._text_box.children)):
            widget.active = (i == local)
        self._scroll_to_local(local)

    def _on_scroll_y(self, *_):
        """滚动位置变了：只要不是程序自己设的，就认定是用户在手动翻。

        这样拖正文和拖右侧滑块都能被识别——两者的共同结果都是 scroll_y
        发生了变化，比去拦截触摸事件可靠得多。
        """
        diag(f"[scroll_y] {self._scroll.scroll_y:.3f} programmatic={self._setting_scroll}")
        if self._setting_scroll:
            return
        # 用户自己翻页了 → 暂停自动跟随，把控制权交还用户
        self._last_user_scroll = time.time()
        self._follow = False

    def _scroll_to_local(self, local_index, force=False):
        """把本章内第 local_index 段滚到**视口正中**。

        force=True 时无视「用户刚手动翻过」的限制（用户主动点了某一段时用）。
        """
        children = list(reversed(self._text_box.children))
        if not (0 <= local_index < len(children)):
            return
        # 用户手动翻过（或已关闭跟随）→ 别把他的位置抢回去，自由浏览
        if not force and (not self._follow
                          or time.time() - self._last_user_scroll
                          < AUTO_FOLLOW_PAUSE):
            return
        try:
            self._center_widget(children[local_index])
        except Exception:
            pass

    def _scroll_to_top(self):
        """把正文滚到最顶部（scroll_y=1）。

        `_setting_scroll` 用来告诉 `_on_scroll_y`「这是程序设的，不是用户手动翻页」，
        否则会被误判成用户操作、把自动跟随关掉。
        """
        self._setting_scroll = True
        try:
            self._scroll.scroll_y = 1.0
            # 强制立刻重算 g_translate，保证内容顶部严格贴住视口顶部（消除黑区）
            self._scroll.update_from_scroll()
        except Exception:
            pass
        finally:
            self._setting_scroll = False

    def _center_widget(self, widget):
        """把 widget 滚到正文视口的垂直正中（朗读跟随用）。

        Kivy 自带的 scroll_to() 只保证「滚进视野」，不保证居中，
        所以这里直接按内容坐标换算 scroll_y。
        """
        scroll = self._scroll
        content = self._text_box
        viewport_h = scroll.height
        content_h = content.height
        if viewport_h <= 0:
            return
        if content_h <= viewport_h:
            # 内容比视口短，本来不需要滚动；但如果 scroll_y 残留在 0
            # （上一章居中时设过），内容会被贴在底部、上方留一大片黑。
            # 这里强制顶到最上，保证正文从顶部开始显示。
            self._scroll_to_top()
            return
        # widget.y 是相对内容(_text_box)的坐标。
        # 内容底边在视口中的位置 = scroll_y*(viewport_h-content_h)，
        # 令「widget 中心」落在视口中心即可解出 scroll_y。
        target = ((viewport_h / 2.0 - widget.y - widget.height / 2.0)
                  / (viewport_h - content_h))
        self._setting_scroll = True
        try:
            scroll.scroll_y = max(0.0, min(1.0, target))
        finally:
            self._setting_scroll = False

    # ============================================================
    #                      播放控制
    # ============================================================
    def tap_paragraph(self, local_index):
        """点正文某一段 → 从这一段开始朗读。

        注意：local_index 是**本章内**的下标（ParaView 只渲染当前章节），
        这里换算成全书下标再交给引擎。
        """
        if not self._paragraphs:
            return
        index = max(0, min(self._view_start + int(local_index),
                           len(self._paragraphs) - 1))
        self._follow = True          # 点段落开始朗读 → 恢复「高亮跟随」
        self._engine.play(index)
        self._set_highlight(index)
        self._toast("从第 %d 段开始朗读" % (index + 1))

    def long_press_paragraph(self, local_index):
        """长按正文段落 → 弹出操作菜单（朗读 / 书签）。

        书签功能之前完全没接到界面上（config_manager 里的接口都白写了），
        长按是最自然的移动端入口。
        local_index 是章内下标；书签按全书下标存，这里统一换算。
        """
        if not self._paragraphs:
            return
        index = max(0, min(self._view_start + int(local_index),
                           len(self._paragraphs) - 1))
        has_bookmark = any(b.get("para") == index
                           for b in self._config.get_bookmarks(self._book_key))

        popup = Popup(title="第 %d 段" % (index + 1), size_hint=(0.88, None),
                      height=dp(292))
        box = BoxLayout(orientation="vertical", spacing=dp(6), padding=dp(10))

        preview = Label(text=self._paragraphs[index][:70], font_size="12sp",
                        size_hint_y=None, height=dp(44), halign="left",
                        valign="top", color=C_DIM)
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
        popup = Popup(title="书签（%d 条）" % len(bookmarks),
                      size_hint=(0.92, 0.8))
        box = BoxLayout(orientation="vertical")
        scroll = self._make_scroll()
        inner = BoxLayout(orientation="vertical", size_hint_y=None, spacing=dp(2))
        inner.bind(minimum_height=inner.setter("height"))
        for bookmark in bookmarks:
            para = int(bookmark.get("para", 0))
            btn = Button(text="第 %d 段 · %s" % (para + 1, bookmark.get("label", "")),
                         size_hint_y=None, height=dp(46), halign="left",
                         valign="middle", background_normal="",
                         background_color=C_BTN, color=(1, 1, 1, 1),
                         font_size="13sp")
            btn.bind(size=lambda w, *_: setattr(w, "text_size",
                                                (w.width - dp(20), None)))
            btn.bind(on_release=lambda _b, p=para: self._goto_bookmark(popup, p))
            inner.add_widget(btn)
        scroll.add_widget(inner)
        box.add_widget(scroll)

        btn_clear = Button(text="清空本书书签", size_hint_y=None, height=dp(46),
                           background_normal="",
                           background_color=C_DANGER, color=(1, 1, 1, 1))
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
        try:
            state = self._engine.get_state()
            if state == STATE_PLAYING:
                self._engine.pause()
            elif state == STATE_PAUSED:
                self._follow = True       # 继续播放 → 恢复「高亮跟随」
                self._engine.resume()
            else:
                self._follow = True
                para, _, _ = self._engine.get_position()
                self._engine.play(para)
        except Exception:
            # Kivy 会吞掉按钮回调里的异常 —— 这里显式留痕，便于定位「暂停闪退」
            tb = traceback.format_exc()
            _record_crash(tb)
            self._on_error("播放/暂停出错：%s" % tb.strip().splitlines()[-1])

    def jump_paragraph(self, delta):
        if not self._paragraphs:
            return
        para, _, _ = self._engine.get_position()
        target = max(0, min(para + delta, len(self._paragraphs) - 1))
        self._engine.seek_paragraph(target)
        self._set_highlight(target)
        self._save_position()

    def jump_chapter(self, delta):
        """跳到上一章 / 下一章的开头，并继续朗读。

        听书场景里按章跳比按段跳实用得多（一章一百多段，
        按段跳要按上百次才换一章）。
        """
        if not self._paragraphs:
            return
        if not self._chapters:
            self._toast("本书没有识别到章节，无法按章跳转")
            return
        para_now = self._engine.get_position()[0]
        ci, _ct, _cs, _ce = self._chapter_at(para_now)
        target = max(0, min(ci + int(delta), len(self._chapters) - 1))
        if target == ci:
            self._toast("已经是%s了" % ("第一章" if delta < 0 else "最后一章"))
            return
        title, start = self._chapters[target]
        self._follow = True          # 跳章并开始朗读 → 恢复「高亮跟随」
        self._engine.play(start)
        self._set_highlight(start)
        self._save_position(persist=True)   # 跳章立刻落盘，杀掉也不丢
        self._toast("已跳到「%s」" % title[:18])

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
        # 全书进度 + 全书剩余时间预估（按实测语速；没打开书/边界时省略）
        book_pct = (char_pos / float(total)) * 100 if total else 0.0
        remain = max(0, total - char_pos) / cps
        remain_s = ("剩约 %s" % self._fmt_hm(remain)) if remain > 60 else ""
        self.progress_text = "本章 %s/%s %.0f%%  ·全书 %.0f%%%s" % (
            self._fmt(fraction * total_sec), self._fmt(total_sec),
            fraction * 100, book_pct,
            ("  ·" + remain_s) if remain_s else "")

    @staticmethod
    def _fmt_hm(seconds):
        """把秒数格式化成「X小时Y分 / Y分Z秒」的紧凑可读形式。"""
        seconds = max(0, int(seconds))
        h, rem = divmod(seconds, 3600)
        m, sec = divmod(rem, 60)
        if h:
            return "%d小时%02d分" % (h, m)
        if m:
            return "%d分%02d秒" % (m, sec)
        return "%d秒" % sec

    @staticmethod
    def _fmt(seconds):
        seconds = max(0, int(seconds))
        m, s = divmod(seconds, 60)
        return "%02d:%02d" % (m, s)

    def _on_paragraph(self, index):
        """开始朗读某一段：跨章了就换章渲染，否则只挪高亮。

        跨章必须重建控件（正文区只装当前章节）；同章内只挪高亮 ——
        绝不能每段都重建或全量刷新，那会卡死主线程、让朗读直接停住。
        """
        def _apply(_dt):
            if not self._paragraphs:
                return
            if self._chapter_at(index)[0] != self._view_chapter:
                self._refresh_view(index)
            else:
                self._set_highlight(index)
        Clock.schedule_once(_apply, 0)

    def _on_state(self, state):
        # 播放时：持有 PARTIAL_WAKE_LOCK + 启动前台服务（熄屏/后台不冻结）；
        # 非播放态释放 wakelock，彻底停止时再关掉前台服务（暂停时保留，续播更快）。
        self._is_playing = (state == STATE_PLAYING)
        if state == STATE_PLAYING:
            self._acquire_wake()
            self._start_fg_service()
            self._media_setup()
            self._media_update(True)
        else:
            self._media_update(False)
            self._release_wake()
            if state == STATE_STOPPED:
                self._stop_fg_service()
                self._media_teardown()

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
        # 记下「最近一次错误」，显示在设置面板的自检信息里——
        # toast 只弹 2 秒、用户来不及截图，自检信息是持久可见的诊断入口。
        self._last_error = key
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

    def _save_position(self, persist=False):
        """记录当前断点。

        ⚠️ 以前只写内存、不落盘（只有 on_pause/on_stop 才 save）。于是 App 被系统
        **杀掉**（从最近任务划掉、后台被回收）时断点就丢了 —— 用户每次重开都得
        重新选章节。现在默认会按 5 秒限流落盘；关键操作（跳章/目录选择）用
        persist=True 立刻落盘。
        """
        if not self._book_key or self._engine is None:
            return
        para, char, _ = self._engine.get_position()
        self._config.set_position(self._book_key, para, char)
        now = time.time()
        if persist or (now - self._last_persist) > 5.0:
            self._config.save()
            self._last_persist = now

    def _tick(self, *_):
        """定时任务（每 0.2 秒）：推进兜底 + 保存断点 + 刷新休眠倒计时。

        注意休眠用**时间戳**而不是"每 tick 减 1"——本函数每秒跑 5 次，
        按 tick 计数会让倒计时快好几倍（30 分钟变成几分钟就停）。
        """
        # ★ 朗读推进的兜底：不依赖安卓的 onDone 回调。
        #   有设备上 onDone 根本不触发，只靠它就会「读完一句卡住不动」。
        #   这里用 isSpeaking() 轮询，0.2 秒一次，最多多等 0.2 秒。
        self._tick_count += 1
        self._engine.poll_advance()

        if self._engine.get_state() == STATE_PLAYING:
            self._save_position()
            # 用引擎的插值位置刷新进度条：否则它只在每读完一句时跳一格
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
    #  朗读推进兜底时钟：独立于 Kivy 逐帧时钟（熄屏也能跑）
    # ============================================================
    def _setup_tick_loop(self):
        """准备安卓主线程 Handler（用于把 _tick 投递回主线程执行 Java 调用）。"""
        if not _JNIUS_OK:
            self._tick_mode = "Clock(桌面)"
            return
        try:
            _Handler = autoclass("android.os.Handler")
            # ⚠️ Looper 在 android.os 下，不是 java.util！
            # 之前误写成 java.util.Looper → ClassNotFoundException → Handler 建不出来
            # → 退回 Kivy 的 Clock → 熄屏后 Clock 停摆 → 只念一段。
            _Looper = autoclass("android.os.Looper")
            self._handler = _Handler(_Looper.getMainLooper())
            self._tick_runnable = _TickRunnable(self._tick)
            self._tick_mode = "Handler"
        except Exception as e:
            self._handler = None
            self._tick_runnable = None
            # 截断：异常信息（含长长的 DexPathList）会把自检面板撑爆、盖住其它控件
            self._tick_mode = "Clock(Handler失败:%s)" % str(e)[:48]

    def _start_tick_loop(self):
        """启动独立后台线程：每 0.2 秒把 _tick 投递到主线程。

        之所以要独立线程而不是 Kivy 的 Clock.schedule_interval：
        熄屏后 Kivy 的帧时钟会停摆，poll_advance 不再被调用 → 读完一句就卡住。
        后台 Python 线程不受屏幕状态影响，每次唤醒后通过 Handler.post 到主线程
        执行 _tick（主线程才能安全调用安卓 TTS / MediaPlayer 等 Java 方法）。
        """
        if self._tick_running:
            return
        self._tick_running = True
        app = self

        def _loop():
            while app._tick_running:
                time.sleep(0.2)
                if not app._tick_running:
                    break
                app._post_tick()

        self._tick_thread = threading.Thread(target=_loop, daemon=True)
        self._tick_thread.start()

    def _post_to_main(self, fn):
        """把 fn 投递到 **Kivy 主线程** 执行。

        ⚠️⚠️ 一定要区分两个「主线程」，别搞混：
          · **Kivy 主线程** —— 唯一能创建控件 / 图形指令的线程
            → 必须用 `Clock.schedule_once`
          · **安卓 UI 线程（Looper）** —— `_tick` 用它，是为了保证**熄屏后仍能跑**
            （Kivy 的 Clock 在熄屏时会停摆）；它**不是** Kivy 线程

        之前 `open_book` 被投到了安卓 UI 线程，结果照样抛
            Cannot create graphics instruction outside the main Kivy thread
        —— 所以凡是**操作 UI / 建控件**的，一律用 Clock。
        """
        Clock.schedule_once(lambda _dt: fn(), 0)

    def _post_tick(self):
        """把 _tick 投到主线程；桌面无 Handler 时退回 Kivy Clock。"""
        if self._handler is not None and self._tick_runnable is not None:
            try:
                self._handler.post(self._tick_runnable)
                return
            except Exception:
                pass
        # 桌面环境兜底：仍用 Kivy 时钟验证逻辑
        Clock.schedule_once(lambda dt: self._tick())

    def _stop_tick_loop(self):
        self._tick_running = False


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

    @staticmethod
    def _scroll_kwargs():
        """滚动条设置 —— ScrollView 和 RecycleView 都用这套。

        Kivy 默认参数在手机上等于没法用：
          · scroll_type 默认 ['content'] —— 滚动条根本不是拖动目标
          · bar_width   默认只有 3px    —— 手指压根按不住
        合起来就是用户说的"右侧滚动条拖不动"。
        这里改成 bars+content 并加宽到 16dp（手指可点的尺寸），
        同时把颜色调亮一点，让用户知道这条能拖。
        """
        return dict(
            scroll_type=["bars", "content"],
            bar_width=dp(16),
            bar_margin=0,
            bar_pos_y="right",
            bar_color=(0.35, 0.62, 1.0, 0.95),
            bar_inactive_color=(0.35, 0.62, 1.0, 0.55),
            # 正文只纵向滚，不让横向手势来抢
            do_scroll_x=False,
            do_scroll_y=True,
            # 默认 20 太迟钝：手指要滑出挺长一段距离才开始滚，
            # 在手机上容易让人以为"滑不动"
            scroll_distance=dp(8),
        )

    @classmethod
    def _make_scroll(cls):
        """构造一个「滚动条可以用手指拖」的 ScrollView。"""
        return ScrollView(**cls._scroll_kwargs())

    def show_chapters(self):
        """章节目录：点章节 → 跳转并开始朗读。

        用 RecycleView 只创建可见的十几行 —— 本书 1142 章，
        一次性 new 出全部按钮在手机上要卡 1~2 秒。
        固定行高正是 RecycleView 擅长的场景（正文那种高度不定的文本才不能用它）。
        """
        if not self._chapters:
            self._toast("本书没有识别到章节")
            return
        popup = Popup(title="目录（共 %d 章）" % len(self._chapters),
                      size_hint=(0.92, 0.85))
        rv = RecycleView(**self._scroll_kwargs())
        # ⚠️ default_size 不能传 None（ReferenceListProperty 只收 list/tuple）
        layout = RecycleBoxLayout(
            orientation="vertical", spacing=dp(2),
            default_size=(0, dp(46)),      # 固定行高
            default_size_hint=(1, None),
            size_hint_y=None,
        )
        layout.bind(minimum_height=layout.setter("height"))
        rv.add_widget(layout)
        # ★ viewclass 必须在 add_widget(layout) **之后**设！
        #   RecycleView.viewclass 是 AliasProperty，setter 里是
        #       a = self.layout_manager
        #       if a is not None: a.viewclass = value
        #   layout_manager 由第一个子控件决定；先设 viewclass 的话
        #   layout_manager 还是 None，值被静默丢弃 → viewclass 变 None
        #   → 一行都渲染不出来（这就是「内容不显示」的真凶）。
        rv.viewclass = "ChapterRow"
        rv.data = [{"text": "%s    (第 %d 段)" % (t, s + 1), "index": i}
                   for i, (t, s) in enumerate(self._chapters)]

        box = BoxLayout(orientation="vertical")
        box.add_widget(rv)
        popup.content = box
        self._chapter_popup = popup
        popup.open()

    def goto_chapter_index(self, chapter_index):
        """目录里点了第 chapter_index 章 → 跳过去并开始朗读。"""
        if not self._chapters:
            return
        idx = max(0, min(int(chapter_index), len(self._chapters) - 1))
        title, start = self._chapters[idx]
        if self._chapter_popup is not None:
            try:
                self._chapter_popup.dismiss()
            except Exception:
                pass
            self._chapter_popup = None
        self._follow = True          # 目录跳章并开始朗读 → 恢复「高亮跟随」
        self._engine.play(start)
        self._set_highlight(start)
        self._save_position(persist=True)   # 目录选章立刻落盘，杀掉也不丢
        self._toast("从「%s」开始朗读" % title[:18])


    def show_settings(self):
        """设置面板：音色 / 音调 / 语速 / 语调起伏 / 字号 / 定时休眠。"""
        from kivy.uix.checkbox import CheckBox
        from kivy.uix.spinner import Spinner

        popup = Popup(title="设置", size_hint=(0.92, 0.88))
        # 内容较多（尤其小屏），放进 ScrollView，避免最下面的按钮被挤出弹窗
        box = BoxLayout(orientation="vertical", spacing=dp(8), padding=dp(12),
                        size_hint_y=None)
        box.bind(minimum_height=box.setter("height"))

        # 自检信息：无 adb 时让用户截图即可定位（装的是哪版 / 推进是否在跑 / 唤醒锁是否生效）
        _eng_state = self._engine.get_state() if self._engine is not None else "-"
        try:
            _utter_ok = ("已挂" if self._engine.utter_listener_ok()
                         else "未挂（用轮询推进）")
        except Exception:
            _utter_ok = "-"
        # 正文区布局数值：定位「顶部黑空区」这类问题靠它（截图即可判断）。
        # ⚠️ Kivy 的 ScrollView 不是移动子控件坐标，而是用 canvas 的 g_translate
        # 平移 —— 所以要看 gty（实际位移），而不是 tb.y（恒为 0）。
        try:
            _gt = getattr(self._scroll, "g_translate", None)
            _gty = int(round(_gt.xy[1])) if _gt is not None else -99999
            try:
                from kivy.core.window import Window as _W
                _wh = int(_W.height)
                # ⚠️ 普通告警：to_parent/to_window 对普通控件是**空操作**（只有
                # RelativeLayout 才引入新坐标系），返回的是本地坐标回声，毫无
                # 绝对位置信息 —— 必须直接读 .y（普通控件 pos 即窗口绝对坐标）。
                _b_y = int(self._body.y)
                _b_top_y = int(self._body.y + self._body.height)
                _root = self._body.parent
                # root 子控件顺序（类型首字母，后添加的在前）：
                # 正确应为 "BBFB" —— F(loatLayout)=正文区，排在两个 B 之间
                _kids = "".join(type(c).__name__[0] for c in _root.children)
                _t_bot_y = int(self._top_w.y)
                _root_h = int(_root.height)
                _root_y = int(_root.y)
            except Exception:
                _wh = _b_top_y = _t_bot_y = _root_h = _root_y = -1
                _b_y = -1
                _kids = "?"
            _gap_px = -1 if _b_top_y < 0 or _t_bot_y < 0 else (_b_top_y - _t_bot_y)
            _layout = ("vp=%.0f content=%.0f sy=%.2f gty=%d body=%.0f winH=%d "
                       "rootH=%d rootY=%d tBot=%d bTop=%d gap2=%d "
                       "bY=%d svY=%.0f fix=%d kids=%s") % (
                self._scroll.height, self._text_box.height,
                self._scroll.scroll_y, _gty,
                getattr(self._body, "height", -1), _wh, _root_h, _root_y,
                _t_bot_y, _b_top_y, _gap_px,
                _b_y, self._scroll.y, self._layout_fixes, _kids)
        except Exception:
            _layout = "-"
        # 上次打开的书 + 已存断点（验证「记忆功能」是否生效）
        try:
            _lb = str(self._config.get("last_book", ""))
            _book = os.path.basename(_lb) if _lb else "无"
            _pos = (self._config.get_position(self._book_key)
                    if self._book_key else None)
            _pos_s = ("第%d段" % _pos[0]) if _pos else "无"
        except Exception:
            _book, _pos_s = "-", "-"
        # 崩溃留痕：只显示 traceback 的最后一行（异常信息本身）
        try:
            _crash_last = ""
            if _CRASH.get("text"):
                _crash_last = _CRASH["text"].strip().splitlines()[-1][:170]
        except Exception:
            _crash_last = ""
        _diag_text = (
            "版本 %s\n"
            "推进 %s   tick=%d\n"
            "监听器 %s   媒体控制 %s\n"
            "唤醒锁 %s%s\n"
            "前台服务 %s%s\n"
            "引擎 %s\n"
            "正文 %s\n"
            "记忆 书=%s  断点=%s\n"
            "崩溃 %s\n"
            "最近错误 %s"
            % (BUILD_TAG, self._tick_mode, self._tick_count,
               _utter_ok,
               ("已挂" if self._media_ok
                else ("未挂 " + self._media_error) if self._media_error
                else "未挂"),
               "已持有" if self._wake_lock is not None else "未持有",
               ("  " + self._wake_error) if self._wake_error else "",
               "已启动" if self._fg_started else "未启动",
               ("  " + self._fg_error) if self._fg_error else "",
               _eng_state, _layout,
               _book, _pos_s,
               (_crash_last or "无"),
               (str(self._last_error)[:120] or "无"))
        )
        _diag = Label(text=_diag_text, size_hint_y=None, height=dp(92),
                      font_size="11sp", color=C_DIM, halign="left", valign="top")
        _diag.bind(size=lambda w, *_: setattr(w, "text_size", (w.width, None)))
        # 高度随文字自适应：否则错误信息一长就会溢出、盖住下面的控件
        _diag.bind(texture_size=lambda w, *_: setattr(w, "height", w.texture_size[1]))
        box.add_widget(_diag)

        # ---- 音色 ----
        box.add_widget(Label(text="音色（系统引擎 + Edge 在线）", size_hint_y=None,
                             height=dp(24), font_size="13sp"))
        voice_labels = {v["label"]: v["name"] for v in getattr(self, "_voice_list", [])}
        current = str(self._config.get("voice_name", ""))
        current_label = next((lbl for lbl, n in voice_labels.items() if n == current), None)
        spinner = Spinner(text=current_label or (list(voice_labels)[0] if voice_labels else "无可用音色"),
                          values=list(voice_labels), size_hint_y=None, height=dp(44),
                          background_normal="", background_color=C_BTN,
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

        # ---- 行距 ----
        box.add_widget(self._slider_row("行距", 10, 20,
                                        int(round(self.line_height * 10)),
                                        self._on_line_height,
                                        fmt=lambda v: "%.1fx" % (v / 10.0)))

        # ---- 定时休眠 ----
        sleep_row = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(6))
        sleep_row.add_widget(Label(text="定时休眠（分钟）", font_size="13sp"))
        spin_sleep = Spinner(text="30", values=["15", "30", "45", "60", "90", "120"],
                             size_hint_x=None, width=dp(90), background_normal="",
                             background_color=C_BTN, color=(1, 1, 1, 1))
        btn_sleep = Button(text="启动", size_hint_x=None, width=dp(70),
                           background_normal="", background_color=C_PRIMARY,
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
                                  background_color=C_BTN,
                                  color=(1, 1, 1, 1))
        btn_cancel_sleep.bind(on_release=_cancel_sleep)
        sleep_row.add_widget(spin_sleep)
        sleep_row.add_widget(btn_sleep)
        sleep_row.add_widget(btn_cancel_sleep)
        box.add_widget(sleep_row)

        btn_help = Button(text="使用说明", size_hint_y=None, height=dp(44),
                          background_normal="",
                          background_color=C_BTN, color=(1, 1, 1, 1))
        def _show_help(*_):
            popup.dismiss()
            self.show_usage_hint()
        btn_help.bind(on_release=_show_help)
        box.add_widget(btn_help)

        # 熄屏/后台朗读要靠系统「不冻结本应用」——一键跳到省电白名单设置页。
        btn_power = Button(text="省电白名单（后台不被冻结）", size_hint_y=None,
                           height=dp(44), background_normal="",
                           background_color=C_PRIMARY, color=(1, 1, 1, 1))
        btn_power.bind(on_release=lambda *_: self._open_power_settings())
        box.add_widget(btn_power)

        # 自启动 / 后台运行权限（各 ROM 是隐藏页，这里按包名逐个试跳转）
        btn_auto = Button(text="自启动 / 后台权限", size_hint_y=None, height=dp(44),
                          background_normal="", background_color=C_BTN,
                          color=C_TEXT)
        btn_auto.bind(on_release=lambda *_: self._open_autostart_settings())
        box.add_widget(btn_auto)

        # 主界面去掉了停止键（播放键改成暂停/继续切换），
        # 停止功能放这里，需要时还能用。
        btn_stop = Button(text="停止朗读", size_hint_y=None, height=dp(44),
                          background_normal="",
                          background_color=C_BTN, color=C_TEXT)
        def _do_stop(*_):
            popup.dismiss()
            self._engine.stop()
            self._save_position()
            self._toast("已停止朗读")
        btn_stop.bind(on_release=_do_stop)
        box.add_widget(btn_stop)

        # ---- 备份 / 诊断 ----
        tools_row = BoxLayout(size_hint_y=None, height=dp(46), spacing=dp(8))
        btn_export = Button(text="导出书签/进度", size_hint_x=1,
                            background_normal="", background_color=C_BTN,
                            color=(1, 1, 1, 1), font_size="13sp")
        btn_export.bind(on_release=lambda *_: self._export_backup())
        btn_share_log = Button(text="分享诊断日志", size_hint_x=1,
                               background_normal="", background_color=C_BTN,
                               color=(1, 1, 1, 1), font_size="13sp")
        btn_share_log.bind(on_release=lambda *_: self._share_diag_log())
        tools_row.add_widget(btn_export)
        tools_row.add_widget(btn_share_log)
        box.add_widget(tools_row)

        btn_close = Button(text="关闭", size_hint_y=None, height=dp(46),
                           background_normal="", background_color=C_PRIMARY,
                           color=(1, 1, 1, 1))
        btn_close.bind(on_release=lambda *_: popup.dismiss())
        box.add_widget(btn_close)
        _sv = ScrollView()
        _sv.add_widget(box)
        popup.content = _sv
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
        """调整正文字号。

        直接改每个段落控件的 reader_font_size 即可 —— KV 里
        `font_size: str(int(root.reader_font_size)) + 'sp'` 是绑定关系，
        改了属性 Kivy 会自动重算文字纹理和段落高度。

        **不要**用「整章重建 + 防抖」：重建一次要 0.48 秒，
        拖动过程中完全没有反馈，松手后才变，用起来跟坏了一样。
        """
        size = float(value)
        self.reader_font = size
        self._config.set("font_size", int(value))
        if self._text_box is None:
            return
        for widget in self._text_box.children:
            widget.reader_font_size = size
        # 字号变了每段高度都变，重算一下滚动位置，别让当前段跑出视野
        self._scroll_to_local(self.highlight_index - self._view_start)

    def _on_line_height(self, value):
        """调整正文行距（1.0~2.0 倍）。同字号：直接改子控件属性即可，
        line_height 变化会带动 texture_size 重算，段落高度自动更新。"""
        lh = max(1.0, min(2.0, float(value)))
        self.line_height = lh
        self._config.set("line_height", round(lh, 2))
        if self._text_box is None:
            return
        for widget in self._text_box.children:
            widget.line_height_factor = lh
        self._scroll_to_local(self.highlight_index - self._view_start)

    def _apply_font_refresh(self, *_):
        """兼容保留：若外部仍调用，按当前字号重建一次。"""
        self._font_event = None
        if not self._paragraphs:
            return
        self._view_chapter = -1
        self._refresh_view()

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
        # 关掉兜底时钟线程、释放 wakelock，避免退出后还在空转耗电
        try:
            self._stop_tick_loop()
            self._release_wake()
        except Exception:
            pass


if __name__ == "__main__":
    try:
        AudioBookApp().run()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
