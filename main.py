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
# SpinnerOption（下拉展开后的选项按钮）默认**不在** Factory 里，
# KV 里写 <SpinnerOption> 规则必须先导入，否则加载 KV 会抛「Unknown class」。
from kivy.uix.spinner import Spinner, SpinnerOption

from book_parser import BookParseError
from book_parser import load_book as parse_book_file
from config_manager import ConfigManager
from app_diag import diag, _DIAG, _CRASH, _record_crash, _install_crash_handlers
from app_wakelock_fg import WakelockFgMixin
# ---- 多角色朗读（新增模块，全部只增不改）----
import role_config
import role_parser
import ai_roles
from voice_template import VOICE_FRIENDLY, VoiceTemplate, voice_friendly
# ⚠️ STATE_STOPPED 也必须导入：_on_state 里用它决定「是否关闭前台服务」。
#    漏了它会抛 NameError，而异常从按钮回调冒出 → Kivy 重新抛出 → **一点暂停就闪退**。
from tts_android import (STATE_PAUSED, STATE_PLAYING, STATE_STOPPED,
                         split_sentences)
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
REQUEST_EXPORT_LOG = 1003   # 导出诊断日志（ACTION_CREATE_DOCUMENT）
REQUEST_PICK_AI_MODEL = 1004  # 选择 AI 模型文件（.gguf）导入
REQUEST_PICK_VITS_MODEL = 1005  # 选择 VITS 语音包（.tar.bz2）导入

KV = """
# ============================================================
#  全局控件样式：统一圆角 + 扁平配色
#  按钮底色统一由 background_color 决定（下面用 canvas 自己画圆角矩形），
#  这样各处 new Button(background_color=...) 的写法不用改。
# ============================================================
<Button>:
    background_normal: ''
    background_down: ''
    font_size: '14sp'

# 主题兜底色：所有 Label / Button 的文字都跟着主题走（KV 里 app.theme_* 是
# 绑定关系，切主题时**已创建**的控件全部即时刷新，不用重建界面）。
# ⚠️ 顺序很重要：KV 规则是「后写的压先写的」，所以这条兜底规则**必须排在所有
#    具体类之前**。写在最后会把 ABPrimaryButton 的白字也刷成主题色 ——
#    主按钮就变成「浅字压蓝底 / 深字压蓝底」，两种主题的对比度都不达标。
<Label>:
    color: app.theme_text

# 复选框是无文字的纯图标控件：默认图本身是浅色的，浅色主题（白底）下整个隐形。
# CheckBox 的 canvas 用 self.color 给图着色，绑到主文字色即可两边都看得见。
<CheckBox>:
    color: app.theme_text

# 主题感知控件：具体颜色绑定 app.theme_*。
# ⚠️ 上面 <Button> 把 background_normal 置空后，Kivy 自带的按钮底图不再绘制，
#    background_color 只剩一个“没人画它”的数值 —— 按钮其实是**全透明**的，
#    深色/浅色底上都等于没有背景。所以这里必须自己用 canvas 画圆角矩形，
#    否则弹窗里的字是直接压在弹窗底色上，永远谈不上有对比度。
<ABButton>:
    background_color: app.theme_btn
    color: app.theme_text
    canvas.before:
        Color:
            rgba: self.background_color
        RoundedRectangle:
            pos: self.pos
            size: self.size
            # 圆角与目录章节行（ChapterRow）一致，全 app 按钮统一观感
            radius: [dp(6)]

<ABPrimaryButton>:
    background_color: app.theme_primary
    color: 1, 1, 1, 1

<ABDangerButton>:
    background_color: app.theme_danger
    color: 1, 1, 1, 1

<ABDimLabel>:
    color: app.theme_dim

# ⚠️ 弹窗底色是 Kivy 自带的一张**灰色 9-patch 图**（atlas 里的
#    modalview-background），它既不跟深色也跟浅色主题走：
#    深色主题里它是块灰盘子（浅字压浅底 → 看不清），浅色主题里它是脏灰。
#    改法：去掉这张图（background: ''），底色改由 canvas 按主题自己画。
<Popup>:
    background: ''
    background_color: app.theme_surface
    title_color: app.theme_text
    title_size: '15sp'
    separator_color: app.theme_border
    separator_height: dp(1)
    canvas.before:
        Color:
            rgba: app.theme_surface
        RoundedRectangle:
            pos: self.pos
            size: self.size
            radius: [dp(14)]

# 下拉展开后的选项：同样是 Button（透明底 + 墨色字），不刷主题必定看不清。
<SpinnerOption>:
    size_hint_y: None
    height: dp(46)
    background_color: app.theme_btn
    color: app.theme_text
    canvas.before:
        Color:
            rgba: self.background_color
        RoundedRectangle:
            pos: self.pos
            size: self.size
            radius: [dp(6)]

# 正文容器：背景随主题（与按钮同一绑定路径，设备上已验证可靠；
# 之前用 python self.bind 回调在设备上没有生效）
<ABBody>:
    canvas.before:
        Color:
            rgba: app.theme_bg
        Rectangle:
            pos: self.pos
            size: self.size

<Spinner>:
    background_color: app.theme_btn
    color: app.theme_text
    canvas.before:
        Color:
            rgba: self.background_color
        RoundedRectangle:
            pos: self.pos
            size: self.size
            radius: [dp(6)]

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
    # background_normal='' 让按钮不画默认 9-patch，底色改由 canvas.before 自绘，
    # 这样「当前章」高亮（primary 填充）才不会被默认背景盖住。
    background_normal: ''
    background_color: (0, 0, 0, 0)
    color: (1, 1, 1, 1) if root.current else app.theme_text
    canvas.before:
        Color:
            rgba: app.theme_primary if root.current else app.theme_btn
        RoundedRectangle:
            pos: self.x + dp(2), self.y + dp(1)
            size: self.width - dp(4), self.height - dp(2)
            radius: [dp(6)]

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
C_SURFACE = (0.14, 0.16, 0.20, 1)   # 顶栏 / 弹窗底色（比背景略亮，浮起来）
C_BTN = (0.20, 0.23, 0.28, 1)       # 普通按钮
C_PRIMARY = (0.16, 0.42, 0.88, 1)   # 主按钮（播放）：白字对比度 ≥ 4.5:1
C_DANGER = (0.50, 0.22, 0.24, 1)    # 危险按钮（清空书签）
C_TEXT = (0.92, 0.94, 0.97, 1)      # 主文字（对背景 ≥ 12:1）
C_DIM = (0.62, 0.66, 0.73, 1)       # 次要文字（对背景 ≥ 6:1）
C_BORDER = (0.24, 0.27, 0.33, 1)    # 分隔线 / 弹窗标题下划线

# ---- 主题（设置里可切「深色 / 浅色」）----
# 颜色不再直接写死给控件，而是绑定到 App 的 theme_* 属性（见 AudioBookApp），
# KV 规则里用 app.theme_xxx 引用 —— 切换主题时全部控件即时刷新，无需重建界面。
# ⚠️ 每个主题都必须同时给出「文字」与「它所在的底色」，两者要成对改动：
#    只改背景不连带文字（或反之），就会出现「浅底浅字 / 深底深字」看不清。
_THEMES = {
    "dark": {"bg": C_BG, "surface": C_SURFACE, "border": C_BORDER,
             "btn": C_BTN, "primary": C_PRIMARY, "danger": C_DANGER,
             "text": C_TEXT, "dim": C_DIM},
    "light": {"bg": (0.955, 0.957, 0.965, 1), "surface": (1, 1, 1, 1),
              "border": (0.84, 0.86, 0.89, 1),
              "btn": (0.88, 0.90, 0.94, 1), "primary": (0.13, 0.39, 0.85, 1),
              "danger": (0.78, 0.24, 0.26, 1),
              "text": (0.09, 0.11, 0.15, 1), "dim": (0.40, 0.44, 0.50, 1)},
}

# 弹窗内的安全边距（布局用）：留给文字的缓冲，避免贴着弹窗边界被裁切。
# 注意单位是 dp → 必须在运行时调用 dp() 换算，不能提前算死到模块常量里。
def _popup_pad_x():      return dp(14)
def _popup_pad_y():      return dp(12)
def _popup_spacing():    return dp(12)


class ABButton(Button):
    """主题感知普通按钮：颜色由 KV 规则绑定 app.theme_*，切主题即时刷新。"""
    pass


class ABPrimaryButton(ABButton):
    """主按钮（播放 / 关闭）：主色底 + 白字。"""
    pass


class ABDangerButton(ABButton):
    """危险按钮（清空书签等）。"""
    pass


class ABDimLabel(Label):
    """次要文字标签（提示、说明、自检信息）。"""
    pass


class ABBody(FloatLayout):
    """正文容器：背景色由 KV 规则绑定 app.theme_bg，随主题即时切换。"""
    pass

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
    current = BooleanProperty(False) # 是否为「当前正在读/播」的章（打开目录时高亮）

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
    theme = StringProperty("dark")       # 深色 / 浅色主题（设置里切换）
    theme_bg = ListProperty(C_BG)
    theme_surface = ListProperty(C_SURFACE)   # 弹窗 / 面板底色
    theme_border = ListProperty(C_BORDER)     # 分隔线
    theme_btn = ListProperty(C_BTN)
    theme_primary = ListProperty(C_PRIMARY)
    theme_danger = ListProperty(C_DANGER)
    theme_text = ListProperty(C_TEXT)
    theme_dim = ListProperty(C_DIM)
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
        self._chapter_rv = None     # 章节目录的 RecycleView（打开时跟随当前章用）
        self._status_popup = None
        self._voice_list = []       # 系统音色列表（引擎初始化后填充）
        self._shown_errors = set()  # 已提示过的错误，避免连续失败时刷屏
        self._font_event = None     # 字号刷新防抖用

        # ---- 多角色朗读（人名识别 + 全局音色模板 + 单书映射）----
        self._voice_template = None   # 全局音色模板（build 时创建）
        self._role_map = None         # 本书的人名↔编号映射（role_config.RoleMap）
        self._role_speakers = {}      # {(段落, 句子): 说话人}（role_parser 扫描结果）
        self._role_detected = []      # [(人名, 类别)] 最近一次识别结果（面板展示）
        self._role_scan_version = 0   # 扫描版本号：换书/重识别后作废旧线程结果
        self._role_scan_force_new = False  # 重新识别时忽略已有映射文件
        self._role_scan_pending = None     # 后台线程扫完暂存，由 _tick 主线程取用
        self._ai_running = False          # AI 分析进行中（防重复触发）
        self._ai_annot = {}               # Qwen 逐句标注 {(段,句): (说话人,情感)}
        self._ai_annot_cache = {}         # 窗口级缓存（进程内）
        self._ai_annot_toast_shown = False

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
        # ---- 卡死自检：播放态但长时间没有任何「有声进展」 → 自恢复（绝不静默暂停）----
        self._frozen_last = None          # 最近一次 (段, 字符, 媒体毫秒) 位置键
        self._frozen_since_tick = None    # 位置键停止变化起算的 _tick 序号
        self._freeze_ticks = 60           # 0.2s × 60 ≈ 12s 无推进视为卡死（自检屏显示用）
        self._freeze_secs = 12.0          # 同上，秒（引擎 progress_age 判定用）
        self._freeze_recovers = 0         # 连续自恢复次数（连续 3 次仍无声音 → 跳句）
        self._skips = 0                   # 累计跳句次数
        self._last_freeze_note = ""       # 上次自恢复/跳句的时间与原因（自检屏）
        self._last_freeze_toast = 0.0     # 自恢复提示限流（避免每 12s 弹一次）
        # ---- 外部播放/暂停命令留痕（排查「念着念着自己停下」的关键证据）----
        self._media_events = 0            # 收到外部（通知栏/耳机/蓝牙/媒体键）命令次数
        self._last_media_event = ""       # 形如 "暂停@18:31:02 来源=通知栏/耳机"
        self._last_playpause_ts = 0.0     # 旧版 Java "playpause" 去重用
        # ---- 系统冻结检测（熄屏后「念着念着停了、亮屏又接上」的成因）----
        self._last_tick_wall = 0.0        # 上次 _tick 的墙上时间
        self._freeze_gap_secs = 30.0      # tick 间隔 > 它 = 进程刚被系统冻结过
        self._freeze_events = 0           # 累计被冻结次数（含闲置时被冻，属正常）
        self._freeze_play_events = 0      # ★ 播放中被冻结的次数（这个才有害）
        self._last_freeze_gap = 0.0       # 最近一次冻结时长（秒）
        self._freeze_during_play = False  # 是否有过「播放中被冻结」（就是熄屏停读）
        self._freeze_hint_ts = 0.0        # 冻结提示限流（避免刷屏）
        self._keepalive_prompted = False  # 本次运行是否已提示过白名单

    # ============================================================
    #                        启动
    # ============================================================
    def build(self):
        self.title = "有声书朗读 " + BUILD_TAG
        # ⚠️ 页面背景：C_BG 之前**只定义了却从未使用**，界面露出的是 Window 默认的
        # **纯黑**，看起来就像「顶部压了一层黑色覆盖层」。这里真正把它用上。
        try:
            from kivy.core.window import Window
            Window.clearcolor = C_BG   # 初值；_apply_theme_colors 随主题直接刷新
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
        # 全局音色模板（多角色朗读；独立于 config.json，所有书共用）
        self._voice_template = VoiceTemplate(self.user_data_dir)
        self.reader_font = float(self._config.get("font_size", 15))
        self.line_height = max(1.0, min(2.0, float(self._config.get("line_height", 1.0))))
        self.theme = "light" if self._config.get("theme", "dark") == "light" else "dark"
        self._apply_theme_colors()

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
            颜色由 KV 规则绑定主题属性（bg 仅用于选控件类别）。
            """
            cls = (ABPrimaryButton if bg is C_PRIMARY
                   else ABDangerButton if bg is C_DANGER else ABButton)
            btn = cls(text=text, font_size=font_size, **kw)
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
        body = ABBody(size_hint_y=1)  # KV 绑定背景色随主题；BoxLayout 撑满中间剩余空间
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

        self.lbl_hint = ABDimLabel(
            text="尚未打开书籍\n\n"
                 "点右上角「打开」选择 txt / epub\n\n"
                 "打开之后：\n"
                 "· 点正文任意一段  →  从该处开始朗读\n"
                 "· 长按任意一段    →  加书签 / 选段朗读\n"
                 "· 顶部「目录」    →  按章节跳转（点 ▶ 才开始念）\n\n"
                 "（第一次打开书籍时会自动弹出提示）",
            halign="center", valign="middle", font_size="14sp",
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
        # ⚠️ 不再预留系统导航栏高度：窗口没有延伸到导航条下方的设备上，
        # 这段预留只会把进度行和按钮之间的空隙撑大（实测反馈）。
        # 正文区因此多出导航栏高度的显示空间。
        ctrl = BoxLayout(size_hint_y=None, height=dp(58), spacing=dp(8),
                         padding=[dp(12), dp(5), dp(12), dp(5)])
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
        if request_code not in (REQUEST_PICK_BOOK, REQUEST_EXPORT_CFG,
                                REQUEST_EXPORT_LOG, REQUEST_PICK_AI_MODEL,
                                REQUEST_PICK_VITS_MODEL):
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
            if request_code == REQUEST_EXPORT_LOG:
                ok = self._write_uri_text(uri, self._diag_text(limit=200000))
                self._post_to_main(lambda: self._toast("诊断日志已导出" if ok else "导出失败"))
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
            if request_code == REQUEST_PICK_AI_MODEL:
                # AI 模型导入：复制到私有目录（约1.1GB，后台线程 + 进度提示）
                self._import_ai_model(uri)
                return
            if request_code == REQUEST_PICK_VITS_MODEL:
                # VITS 语音包导入：SAF 给的是单文件流，复制成 tar.bz2 再解压
                self._import_vits_model(uri)
                return
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

    # ---------------- 离线 AI 角色分析 ----------------
    def _pick_ai_model_file(self):
        """系统文件选择器：挑 AI 模型 .gguf（电脑下载后传到手机再选它）。"""
        if not self._android():
            self._toast("桌面环境请把 .gguf 放到应用数据目录 ai/ 下")
            return
        try:
            from android import activity
            from jnius import autoclass
            Intent = autoclass("android.content.Intent")
            PythonActivity = autoclass("org.kivy.android.PythonActivity")
            intent = Intent(Intent.ACTION_OPEN_DOCUMENT)
            intent.addCategory(Intent.CATEGORY_OPENABLE)
            intent.setType("*/*")
            activity.bind(on_activity_result=self._on_activity_result)
            PythonActivity.mActivity.startActivityForResult(
                intent, REQUEST_PICK_AI_MODEL)
        except Exception as err:
            self._toast(f"打开文件选择器失败：{err}")

    def _import_ai_model(self, uri):
        """复制所选 .gguf 到私有目录并校验（约1.1GB，后台线程）。"""
        self._toast("正在导入 AI 模型（约1.1GB，请勿关闭应用）…")

        def _work():
            try:
                part = os.path.join(self.user_data_dir, "ai_model.part")
                try:
                    if os.path.isfile(part):
                        os.remove(part)
                except OSError:
                    pass
                self._copy_uri_to_file(uri, part)
                try:
                    ai_roles.import_model(part, self.user_data_dir)
                    self._post_to_main(lambda: self._toast(
                        "AI 模型导入成功，可以点「AI 精细识别人物」了"))
                finally:
                    try:
                        os.remove(part)
                    except OSError:
                        pass
            except Exception as err:
                msg = str(err)
                self._post_to_main(lambda: self._on_error(
                    "AI 模型导入失败：%s" % msg))
        threading.Thread(target=_work, daemon=True).start()

    def _download_ai_model(self):
        """App 内下载 AI 模型（hf-mirror 断点续传，后台线程 + 进度 toast）。"""
        self._toast("开始下载 AI 模型（%s）…" % ai_roles.MODEL_LABEL)
        self._ai_dl_last = 0

        def _prog(p):
            now = time.time()
            if now - getattr(self, "_ai_dl_last", 0) > 20 or p >= 1.0:
                self._ai_dl_last = now
                pct = int(p * 100)
                self._post_to_main(lambda: self._toast(
                    "AI 模型下载中 %d%%…" % pct))

        def _work():
            try:
                ai_roles.download_model(self.user_data_dir,
                                        progress_cb=_prog)
                self._post_to_main(lambda: self._toast(
                    "AI 模型下载完成，可以点「AI 精细识别人物」了"))
            except Exception as err:
                msg = str(err)
                self._post_to_main(lambda: self._on_error(
                    "AI 模型下载失败：%s" % msg))
        threading.Thread(target=_work, daemon=True).start()

    def _pick_vits_model_file(self):
        """系统文件选择器：挑 VITS 语音包 .tar.bz2（电脑下载后传手机）。"""
        if not self._android():
            self._toast("桌面环境请把语音包解压到应用数据目录 vits/ 下")
            return
        try:
            from android import activity
            from jnius import autoclass
            Intent = autoclass("android.content.Intent")
            PythonActivity = autoclass("org.kivy.android.PythonActivity")
            intent = Intent(Intent.ACTION_OPEN_DOCUMENT)
            intent.addCategory(Intent.CATEGORY_OPENABLE)
            intent.setType("*/*")
            activity.bind(on_activity_result=self._on_activity_result)
            PythonActivity.mActivity.startActivityForResult(
                intent, REQUEST_PICK_VITS_MODEL)
        except Exception as err:
            self._toast(f"打开文件选择器失败：{err}")

    def _import_vits_model(self, uri):
        """复制所选语音包到私有目录并解压校验（后台线程）。"""
        self._toast("正在导入 VITS 语音包（约290MB，请勿关闭应用）…")

        def _work():
            try:
                import vits_tts as _vt
                part = os.path.join(self.user_data_dir, "vits_pack.tar.bz2")
                try:
                    if os.path.isfile(part):
                        os.remove(part)
                except OSError:
                    pass
                self._copy_uri_to_file(uri, part)
                try:
                    _vt.import_model(part, self.user_data_dir)
                    self._post_to_main(lambda: self._toast(
                        "VITS 语音包导入成功，本地合成已可用"))

                    def _refresh():
                        try:
                            self._engine.refresh_local_voices()
                        except Exception:
                            pass
                    self._post_to_main(_refresh)
                finally:
                    try:
                        os.remove(part)
                    except OSError:
                        pass
            except Exception as err:
                msg = str(err)
                self._post_to_main(lambda: self._on_error(
                    "VITS 语音包导入失败：%s" % msg))
        threading.Thread(target=_work, daemon=True).start()

    def _download_vits_model(self):
        """App 内下载 VITS 语音包（hf-mirror 逐文件断点续传）。"""
        self._toast("开始下载 VITS 语音包（%s）…" % "fanchen-C")
        self._v_dl_last = 0

        def _prog(p):
            now = time.time()
            if now - getattr(self, "_v_dl_last", 0) > 15 or p >= 1.0:
                self._v_dl_last = now
                pct = int(p * 100)
                self._post_to_main(lambda: self._toast(
                    "VITS 语音包下载中 %d%%…" % pct))

        def _work():
            try:
                import vits_tts as _vt
                _vt.download_model(self.user_data_dir, progress_cb=_prog)
                self._post_to_main(lambda: self._toast(
                    "VITS 语音包下载完成，本地合成已可用"))

                def _refresh():
                    try:
                        self._engine.refresh_local_voices()
                    except Exception:
                        pass
                self._post_to_main(_refresh)
            except Exception as err:
                msg = str(err)
                self._post_to_main(lambda: self._on_error(
                    "VITS 语音包下载失败：%s" % msg))
        threading.Thread(target=_work, daemon=True).start()

    def _show_voice_label_panel(self):
        """音色试听与性别标注：187 个 VITS 音色过一遍，标好永久生效。"""
        import vits_tts as _vt
        _vt._current_data_dir[0] = self.user_data_dir
        n = _vt.get_instance(self.user_data_dir).num_speakers()
        labels = _vt.get_labels(self.user_data_dir)
        state = {"sid": 0}

        popup = Popup(title="音色试听与性别标注（%d 个音色，已标 %d 个）"
                            % (n, len(labels)), size_hint=(0.92, 0.8))
        box = BoxLayout(orientation="vertical", spacing=dp(8), padding=dp(10))
        box.add_widget(self._auto_label(
            "点试听听效果，标性别后角色自动映射才能分对男女；"
            "标注存全局，一次即可。", font_size="12sp", min_height=dp(36)))
        spin = Spinner(text="音色 1号",
                       values=["音色 %d号" % (i + 1) for i in range(n)],
                       size_hint_y=None, height=dp(44))
        box.add_widget(self._safe_text(spin, min_height=dp(44)))
        lbl_state = self._auto_label("当前标注：未标注", font_size="12sp",
                                     min_height=dp(24))
        box.add_widget(lbl_state)

        def _refresh_state():
            g = _vt.get_labels(self.user_data_dir).get(state["sid"])
            lbl_state.text = "音色 %d号 · 当前标注：%s" % (
                state["sid"] + 1, g or "未标注")
        spin.bind(text=lambda _s, t: (
            state.update(sid=int(t.replace("音色", "").replace("号", "")) - 1),
            _refresh_state()))

        row = BoxLayout(size_hint_y=None, height=dp(46), spacing=dp(6))
        btn_listen = ABButton(text="▶ 试听", font_size="12sp")
        btn_listen.bind(on_release=lambda *_: self._preview_voice(
            "vits:%d" % state["sid"]))
        btn_m = ABPrimaryButton(text="男", font_size="12sp")
        btn_f = ABPrimaryButton(text="女", font_size="12sp")
        btn_skip = ABButton(text="跳过", font_size="12sp")
        row.add_widget(btn_listen)
        row.add_widget(btn_m)
        row.add_widget(btn_f)
        row.add_widget(btn_skip)
        box.add_widget(row)
        box.add_widget(self._auto_label(
            "标注建议：先听 20-40 个把男女分开即可，"
            "其余保持未标注（不会被自动映射选中）。",
            font_size="11sp", min_height=dp(30)))

        def _mark(gender):
            _vt.set_label(self.user_data_dir, state["sid"], gender)
            _refresh_state()
            popup.title = ("音色试听与性别标注（%d 个音色，已标 %d 个）"
                           % (n, len(_vt.get_labels(self.user_data_dir))))
        btn_m.bind(on_release=lambda *_: _mark("男"))
        btn_f.bind(on_release=lambda *_: _mark("女"))
        btn_skip.bind(on_release=lambda *_: _mark(None))

        btn_close = ABPrimaryButton(text="关闭", font_size="12sp",
                                    size_hint_y=None, height=dp(44))
        btn_close.bind(on_release=lambda *_: (
            popup.dismiss(), self._stop_preview_player()))
        box.add_widget(btn_close)
        popup.bind(on_dismiss=lambda *_: self._stop_preview_player())
        popup.content = box
        popup.open()

    def _collect_role_samples(self, max_chars=3, max_len=60):
        """把逐句说话人标注整理成 [(name, 原猜测类别, [对话片段...])]。"""
        samples = {}
        for (para_idx, sentence), name in (self._role_speakers or {}).items():
            lst = samples.setdefault(name, [])
            if len(lst) < max_chars:
                s = str(sentence).strip()
                if s:
                    lst.append(s[:max_len])
        guess = {n: c for n, c in (self._role_detected or [])}
        out = []
        for name, lines in samples.items():
            out.append((name, guess.get(name, ""), lines))
        return out

    def _ai_identify_roles(self):
        """AI 精细识别人物：后台跑本地模型，成功后按结果重排音色映射。"""
        if not self._paragraphs:
            self._toast("请先打开一本书")
            return
        if getattr(self, "_ai_running", False):
            self._toast("AI 分析正在进行中，请稍候")
            return
        characters = self._collect_role_samples()
        if not characters:
            self._toast("本书还没有识别到角色，无法分析")
            return
        ok, reason = ai_roles.model_ready(self.user_data_dir)
        if not ok:
            self._on_error("AI 模型未就绪：%s" % reason)
            return
        if not self._android():
            self._toast("AI 分析仅支持安卓设备（桌面无运行器）")
            return
        self._ai_running = True
        self._toast("AI 分析中…（%d 个角色，约1分钟，请勿关闭应用）"
                    % len(characters))

        def _work():
            try:
                result = ai_roles.analyze(self.user_data_dir, characters)
                self._post_to_main(lambda: self._apply_ai_result(
                    characters, result))
            except Exception as err:
                msg = str(err)
                self._post_to_main(lambda: self._on_error(
                    "AI 角色分析失败：%s" % msg))
            finally:
                self._ai_running = False
        threading.Thread(target=_work, daemon=True).start()

    def _apply_ai_result(self, characters, result):
        """主线程：把 AI 判断的门类写进本书角色映射（保留自定义音色）。"""
        try:
            detected = ai_roles.merge_detected(characters, result)
            self._role_detected = [(n, c) for n, c in detected]
            rm = role_config.auto_map(self._book_key, detected,
                                      self._voice_template)
            # 已有自定义音色的人物不跟随新映射
            old = self._role_map
            if old is not None:
                for entry in old.roles:
                    if entry.get("custom"):
                        ne = rm.get(entry.get("name"))
                        if ne is not None:
                            ne["custom"] = entry["custom"]
            self._role_map = rm
            role_config.save_map(self.user_data_dir, rm)
            if self._multi_role_enabled():
                self._engine.set_voice_resolver(self._role_voice_resolver)
            ai_cnt = sum(1 for n, _c in detected if n in result)
            self._toast("AI 识别完成：判定了 %d/%d 个角色，多角色配音已重排"
                        % (ai_cnt, len(detected)))
        except Exception as err:
            self._on_error("AI 结果应用失败：%s" % err)

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

    def _copy_uri_to_file(self, uri, dest):
        """把 SAF content:// 流式复制到本地文件（语音包数百MB，防 OOM）。"""
        from jnius import autoclass
        PythonActivity = autoclass("org.kivy.android.PythonActivity")
        resolver = PythonActivity.mActivity.getContentResolver()
        pfd = resolver.openFileDescriptor(uri, "r")
        try:
            with os.fdopen(os.dup(pfd.getFd()), "rb") as src,                     open(dest, "wb") as dst:
                while True:
                    chunk = src.read(1024 * 512)
                    if not chunk:
                        break
                    dst.write(chunk)
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

    def _open_history_popup(self, parent_popup=None):
        """历史播放书籍：浏览 → 点击续播 / 单本删除 / 清空全部。"""
        if parent_popup is not None:
            parent_popup.dismiss()
        items = self._config.get_history_items()
        if not items:
            self._toast("还没有历史播放记录")
            return
        popup = Popup(title="历史播放书籍（点书名续播，✕ 删除记录）",
                      size_hint=(0.94, 0.8))
        box = BoxLayout(orientation="vertical", spacing=dp(6), padding=dp(10))
        scroll = ScrollView()
        inner = BoxLayout(orientation="vertical", size_hint_y=None, spacing=dp(4))
        inner.bind(minimum_height=inner.setter("height"))

        def _reopen(*_):
            Clock.schedule_once(lambda _dt: self._open_history_popup(), 0)

        for key, path, title, tstr in items:
            exists = os.path.isfile(path)
            is_cur = (path == self._book_path)
            sub = []
            if is_cur:
                sub.append("当前书")
            if not exists:
                sub.append("文件缺失")
            pos = self._config.get_position(key)
            if pos:
                sub.append("读到第 %d 段" % (pos[0] + 1))
            sub.append(tstr if tstr else "时间未知")
            row = BoxLayout(size_hint_y=None, height=dp(56), spacing=dp(6))
            book_btn = ABButton(
                text=title + ("（当前）" if is_cur else "")
                     + chr(10) + "  ".join(sub),
                halign="left", font_size="12sp")
            book_btn.bind(size=lambda w, *_: setattr(
                w, "text_size", (w.width - dp(16), None)))
            book_btn.bind(on_release=lambda _b, p=path, pp=popup:
                          self._open_history_book(p, pp))
            # 当前书不允许删除（它的断点正被使用；删除也会被巡检立即重建）
            del_btn = ABDangerButton(text="当前" if is_cur else "删除",
                                     size_hint_x=None, width=dp(64),
                                     font_size="12sp", disabled=is_cur)
            del_btn.bind(on_release=lambda _b, k=key, pp=popup:
                         self._remove_history_item(k, pp))
            row.add_widget(book_btn)
            row.add_widget(del_btn)
            inner.add_widget(row)
        scroll.add_widget(inner)

        bottom = BoxLayout(size_hint_y=None, height=dp(46), spacing=dp(6))
        btn_clear = ABDangerButton(text="清空全部记录", size_hint_x=1,
                                   font_size="13sp")
        btn_clear.bind(on_release=lambda *_: self._clear_play_history(popup))
        bottom.add_widget(btn_clear)
        inner.add_widget(bottom)

        box.add_widget(scroll)
        btn_close = ABPrimaryButton(text="关闭", size_hint_y=None, height=dp(46))
        btn_close.bind(on_release=lambda *_: popup.dismiss())
        box.add_widget(btn_close)
        popup.content = box
        popup.open()

    def _remove_history_item(self, key, popup):
        """删除一条历史（连同该书的断点/书签/角色映射/音频缓存）。

        当前书在列表里禁用删除。删除小说时同步删除：
        · 该书的人名映射配置（role_maps/<md5>.json）
        · 该书的本地合成音频缓存（tts_cache/<书哈希>/，约节省几十MB）
        全局音色清单模板与全局语速音调设置不受影响。
        """
        self._config.remove_book_data(key)
        self._config.save()
        role_config.delete_map(self.user_data_dir, key)
        self._purge_book_audio_cache(key)
        if popup is not None:
            popup.dismiss()
        Clock.schedule_once(lambda _dt: self._open_history_popup(), 0)
        self._toast("已从历史移除（含角色配置与音频缓存）")

    def _purge_book_audio_cache(self, book_key):
        """删除某本书的本地合成音频缓存目录（tts_cache/<书哈希>/）。"""
        import hashlib
        tag = hashlib.md5(book_key.encode("utf-8", "replace")).hexdigest()
        d = os.path.join(self.user_data_dir, "tts_cache", tag)
        if os.path.isdir(d):
            shutil.rmtree(d, ignore_errors=True)

    def _clear_play_history(self, popup):
        """清空全部历史；当前书（若在阅读）保留断点/书签防丢进度，
        因此清空后列表里可能仍显示当前书——这是有意设计。"""
        self._config.clear_book_data(keep_key=self._book_key)
        self._config.save()
        # 同步清掉各书的人名映射配置（当前书保留）；全局模板不受影响
        role_config.delete_all_maps(self.user_data_dir,
                                    keep_book_key=self._book_key)
        if popup is not None:
            popup.dismiss()
        Clock.schedule_once(lambda _dt: self._open_history_popup(), 0)
        self._toast("历史已清空（当前书保留）")

    def _open_history_book(self, path, popup=None):
        """点击历史书名 → 打开并续播到它自己的断点。"""
        if popup is not None:
            popup.dismiss()
        target = path
        if not os.path.isfile(target):
            alt = os.path.join(self.user_data_dir, "books",
                               os.path.basename(target))
            target = alt if os.path.isfile(alt) else None
        if target is None:
            self._toast("该书的文件已不存在，无法打开")
            return
        self.open_book(target)

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

    def _diag_text(self, limit=1500):
        """收集 diag.log + 最近崩溃记录（截尾保留最近的 limit 字符）。

        ⚠️ 分享用的纯文本千万别太大：OriginOS 等国产 ROM 的分享面板会给每个
        渠道渲染内容预览，EXTRA_TEXT 一大（几千字符）面板就会卡死整个界面
        —— 实测「选完分享渠道后软件卡死」就是这个原因。
        """
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
        return content[-limit:]

    def _export_diag_log(self):
        """把诊断日志用 SAF「另存为」导出（与备份导出同款可靠路径）。"""
        if not self._android():
            self._toast("桌面日志文件：" + (_DIAG.get("path") or "-"))
            return
        try:
            from android import activity
            from jnius import autoclass
            Intent = autoclass("android.content.Intent")
            PythonActivity = autoclass("org.kivy.android.PythonActivity")
            it = Intent(Intent.ACTION_CREATE_DOCUMENT)
            it.addCategory(Intent.CATEGORY_OPENABLE)
            it.setType("text/plain")
            it.putExtra(Intent.EXTRA_TITLE, "audiobook_diag.log")
            activity.bind(on_activity_result=self._on_activity_result)
            PythonActivity.mActivity.startActivityForResult(it, REQUEST_EXPORT_LOG)
        except Exception as err:
            self._toast(f"导出失败：{err}")

    def _share_diag_log(self):
        """把 diag.log（含最近崩溃）以纯文本分享出去，方便反馈问题。"""
        content = self._diag_text(limit=1500)
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
            # ⚠️ createChooser 的第二参是 CharSequence（不是 String），
            # pyjnius 不会自动把 Python str 匹配到 CharSequence 重载，
            # 必须显式构造 java.lang.String（与 TTS speak() 同款坑）。
            jtitle = autoclass("java.lang.String")("分享诊断日志")
            PythonActivity.mActivity.startActivity(
                Intent.createChooser(send, jtitle))
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
        # 本地合成音频缓存按书隔离（删书时整目录清理）
        self._engine.set_book_tag(self._book_key)
        self._ai_annot = {}          # {(段, 句): (说话人, 情感)}，换书即作废
        self._ai_annot_pending = set()

        # ---- 多角色朗读：换书即作废上一本的角色上下文 ----
        # 先摘掉逐句音色钩子（找不到映射/识别完成前按全局单音色朗读），
        # 再后台扫描全书识别角色；完成后加载或自动生成映射（见 _apply_role_scan）。
        self._role_map = None
        self._role_speakers = {}
        self._role_detected = []
        self._role_scan_force_new = False
        self._engine.set_voice_resolver(None)
        self._scan_roles_async()
        self._chapters = [(t, s) for t, s in doc.chapters
                          if 0 <= s < len(doc.paragraphs)]

        self.book_title = doc.title

        # ⚠️ 「上次打开的书」必须**尽早落盘**：以前这句放在方法最后，只要后面
        #    任何一步（_refresh_view / _set_highlight …）抛异常，就永远存不上，
        #    表现为「每次打开 App 都要重新选书」。解析成功就立刻存。
        self._config.touch_history(self._book_key, doc.path, doc.title)
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
        tips = ABDimLabel(
            text=                 "· 点正文任意一段  →  从该处开始朗读\n\n"
                 "· 长按任意一段    →  加书签 / 选段朗读\n\n"
                 "· 顶部「目录」    →  按章节跳转（点 ▶ 才开始念）\n\n"
                 "· 底部进度条      →  拖动跳转任意位置\n\n"
                 "· 底部 ▶          →  播放 / 暂停（熄屏也会继续念）",
            halign="left", valign="top", font_size="13sp")
        tips.bind(size=lambda w, *_: setattr(w, "text_size", (w.width, None)))
        box.add_widget(tips)
        btn = ABPrimaryButton(text="知道了", size_hint_y=None, height=dp(46))
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
        # 目录弹窗开着时，让高亮/滚动实时跟随当前章
        self._toc_mark_current(ci)
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
        self._play_from(index)

    def _play_from(self, index):
        """从**全书**第 index 段开始朗读（入参不许再补 _view_start）。

        ⚠️ 以前长按菜单的「从这里开始朗读」直接调 tap_paragraph()，
        而它传进去的是已经换算好的全书下标 —— tap_paragraph 又当成章内
        下标加了一次 _view_start，结果跳到后面章节的某一段（看起来像随机）。
        现在章内 → 全书的换算只发生在 tap_paragraph 里，朗读统一走这里。
        """
        if not self._paragraphs:
            return
        index = max(0, min(int(index), len(self._paragraphs) - 1))
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
                        valign="top")
        preview.bind(size=lambda w, *_: setattr(w, "text_size", (w.width, w.height)))
        box.add_widget(preview)

        def _action(text, callback, color=None):
            btn = ABButton(text=text, size_hint_y=None, height=dp(46),
                           font_size="13sp")
            def _run(*_):
                popup.dismiss()
                callback()
            btn.bind(on_release=_run)
            return btn

        # ⚠️ index 已经是全书下标，必须走 _play_from()，不能调 tap_paragraph()
        #    （那会把入参当章内下标、再加一次 _view_start → 跳到后面的章节）
        box.add_widget(_action("▶ 从这里开始朗读",
                               lambda: self._play_from(index)))
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
    #    多角色朗读：识别 / 映射 / 角色管理 / 音色模板（新增模块接入层）
    # ============================================================
    def _multi_role_enabled(self):
        """多角色总开关（设置里可切；默认开）。"""
        return bool(self._config.get("multi_role", True))

    def _scan_roles_async(self, force_new=False):
        """后台线程扫描全书：识别人物 + 逐句说话人（不阻塞界面）。

        几百万字的书正则扫描要数秒，绝不能在主线程做。
        force_new=True（「重新识别角色」）时忽略已有映射文件，
        按出场顺序重新自动分配编号。
        """
        version = self._role_scan_version + 1
        self._role_scan_version = version
        self._role_scan_force_new = force_new
        paras = list(self._paragraphs)
        if not paras:
            return
        # 提前告诉用户「没卡死，是在扫描」：大部头在手机上要跑几十秒，
        # 期间后台线程占 CPU，界面会有可感知的顿挫
        self._toast("正在识别本书角色…（完成后自动生效，大部头需稍等）")

        def _work():
            result = None
            try:
                result = role_parser.analyze(paras)
            except Exception:
                result = None          # 解析失败 → 调用方降级为单音色
            # ⚠️ 不能从后台线程 Clock.schedule_once（本项目实测会丢回调，
            #    见 tts_engine._post_to_main 的注释）；暂存结果，由 _tick
            #    每 0.2 秒在主线程取用（_tick 恰好负责各类定时回调）。
            self._role_scan_pending = (version, result, force_new)

        threading.Thread(target=_work, daemon=True).start()

    def _poll_role_scan(self):
        """_tick 调用（主线程）：后台扫描完成 → 应用结果。"""
        pending = self._role_scan_pending
        if pending is None:
            return
        self._role_scan_pending = None
        try:
            self._apply_role_scan(*pending)
        except Exception as err:
            self._on_error("角色映射应用失败：%s" % err)

    def _apply_role_scan(self, version, result, force_new=False):
        """扫描完成（已回 Kivy 主线程）：加载/生成映射并挂上逐句音色钩子。"""
        if version != self._role_scan_version:
            return                      # 期间换书/重新识别过了 → 丢弃旧结果
        if result is None:
            # 解析失败自动降级：不设钩子，整本书按全局单音色朗读
            self._role_speakers = {}
            self._role_map = None
            self._engine.set_voice_resolver(None)
            self._on_error("角色识别失败，本书已降级为单音色朗读")
            return
        characters, speakers = result
        self._role_detected = characters
        self._role_speakers = speakers or {}

        # 历史书籍打开：自动查找该书的映射配置文件；找到就加载，
        # 找不到（或点了「重新识别」）才重新识别并自动生成新映射
        rm = None if force_new else role_config.load_map(
            self.user_data_dir, self._book_key)
        if rm is None:
            rm = role_config.auto_map(self._book_key, characters,
                                      self._voice_template)
            role_config.save_map(self.user_data_dir, rm)
        self._role_map = rm
        if self._multi_role_enabled():
            self._engine.set_voice_resolver(self._role_voice_resolver)
        if characters:
            self._toast("已识别 %d 个角色，多角色配音就绪" % len(characters))

    def _role_voice_resolver(self, para_index, sentence):
        """引擎逐句回调（本地合成子线程，绝不能碰 UI）。

        这句是谁在说 → 该人物的声音参数 (voice, rate倍率, pitch Hz)；
        解析顺序：① 正则命中（role_speakers）② Qwen 逐句标注（代词/连续
        对话）③ 返回 None → 引擎按全局音色读。
        情感标签映射为语速微调（VITS 无原生情感维度，明示近似）。
        """
        try:
            if not self._multi_role_enabled() or self._role_map is None:
                return None
            name = self._role_speakers.get((para_index, sentence))
            emotion = "平静"
            if not name:
                annot = getattr(self, "_ai_annot", {}).get(
                    (para_index, sentence))
                if not annot:
                    return None
                name, emotion = annot
            if not name or name in ("旁白", "未知"):
                return None
            params = role_config.RoleMap.entry_params(
                self._role_map.get(name), self._voice_template)
            if params and emotion in ai_roles.EMOTION_RATE:
                v, mult, phz = params
                mult = max(0.5, min(2.0, mult *
                                    ai_roles.EMOTION_RATE[emotion]))
                params = (v, mult, phz)
            return params
        except Exception:
            return None

    # ---------------- Qwen 逐句标注（正则判不出的代词/连续对话） ----------------
    _ANNOT_WINDOW = 80          # 一次标注的段落数（块太大幻觉多、太慢）

    def _ensure_ai_annotation(self, para_index):
        """播放进入新段落时调用：本窗口句子若还没标注过就后台跑 Qwen。

        绝不阻塞播放（后台线程）；失败只记诊断不弹窗刷屏（正则顶着）。
        标注结果按（书+窗口）缓存到 ai_annot/*.json，二次进章零延迟。
        """
        if not self._multi_role_enabled() or not self._paragraphs:
            return
        ok, _reason = ai_roles.model_ready(self.user_data_dir)
        if not ok:
            return
        start = max(0, int(para_index))
        end = min(len(self._paragraphs), start + self._ANNOT_WINDOW)
        win = (start, end)
        if not hasattr(self, "_ai_annot_cache"):
            self._ai_annot_cache = {}
        if win in getattr(self, "_ai_annot_pending", set()):
            return
        # 磁盘缓存命中 → 直接加载
        cpath = self._annot_cache_path(win)
        if os.path.isfile(cpath):
            try:
                with open(cpath, "r", encoding="utf-8") as f:
                    cached = json.load(f)
                by_key = {(int(k.split("|", 1)[0]),
                           k.split("|", 1)[1]): tuple(v)
                          for k, v in cached.items()}
                self._ai_annot.update(by_key)
                self._ai_annot_cache[win] = by_key
                return
            except Exception:
                pass
        # 收集窗口内句子（与引擎同一套分句逻辑，保证键对齐）；
        # 送 Qwen 用自增整数 id（元组没法进提示词），结果按键映射还原
        lines, keymap = [], {}
        seq = 0
        for pi in range(start, end):
            for sent in split_sentences(self._paragraphs[pi]):
                if sent.strip():
                    lines.append((seq, sent[:80]))
                    keymap[seq] = (pi, sent)
                    seq += 1
        if not lines:
            return
        if not hasattr(self, "_ai_annot_pending"):
            self._ai_annot_pending = set()
        self._ai_annot_pending.add(win)
        known = list(self._role_map.names()) if self._role_map else []

        def _work():
            try:
                result = ai_roles.annotate_sentences(
                    self.user_data_dir, lines, known)
                by_key = {}
                for seq_id, (sp, em) in result.items():
                    if seq_id in keymap and sp not in ("旁白", "未知"):
                        by_key[keymap[seq_id]] = (sp, em)

                def _apply():
                    self._ai_annot.update(by_key)
                    self._ai_annot_cache[win] = by_key
                    # 落盘缓存（键 "段|句文本"）
                    try:
                        os.makedirs(os.path.dirname(cpath), exist_ok=True)
                        with open(cpath, "w", encoding="utf-8") as f:
                            json.dump({"%d|%s" % k: v
                                       for k, v in by_key.items()},
                                      f, ensure_ascii=False)
                    except Exception:
                        pass
                    if not getattr(self, "_ai_annot_toast_shown", False) \
                            and by_key:
                        self._ai_annot_toast_shown = True
                        self._toast("Qwen 标注完成：代词/连续对话已可识别"
                                    "（后续章节播放时自动预标注）")
                self._post_to_main(_apply)
            except Exception as err:
                msg = str(err)

                def _err():
                    self._on_error("Qwen 逐句标注失败（正则模式不受影响）：%s"
                                   % msg[:80])
                self._post_to_main(_err)
            finally:
                self._ai_annot_pending.discard(win)
        threading.Thread(target=_work, daemon=True).start()

    def _annot_cache_path(self, win):
        import hashlib
        h = hashlib.md5(("%s|%s" % (self._book_key, win)).encode(
            "utf-8", "replace")).hexdigest()
        return os.path.join(self.user_data_dir, "ai_annot", h + ".json")

    def _multi_role_btn_text(self):
        return "已开启" if self._multi_role_enabled() else "已关闭"

    def _warn_multi_role_backend(self, voice_name, force=False):
        """选了系统音色时提示：多角色只在本地离线音色下生效。

        系统引擎不支持逐句换音色——全局选系统音色时，即使多角色开着、
        角色也绑了编号，整本书仍是同一个声音（这是引擎能力边界，不是 bug）。
        """
        if not (self._multi_role_enabled() and self._role_map is not None):
            return
        # 系统音色：不在已合并音色列表的 edge 分组里
        src = None
        try:
            src = self._engine._voice_source_of(voice_name)
        except Exception:
            pass
        if force or src == "android":
            self._toast("提示：多角色朗读需选择 VITS 离线音色"
                        "（当前是系统音色，整本书会是同一个声音）")

    def _toggle_multi_role(self, btn=None):
        enabled = not self._multi_role_enabled()
        self._config.set("multi_role", enabled)
        self._config.save()
        self._apply_multi_role_setting()
        if btn is not None:
            btn.text = self._multi_role_btn_text()

    def _apply_multi_role_setting(self):
        """开关变化后重挂/摘掉逐句音色钩子（下一句立刻生效）。"""
        if self._multi_role_enabled() and self._role_map is not None:
            self._engine.set_voice_resolver(self._role_voice_resolver)
            cur = str(self._config.get("voice_name", ""))
            if cur:
                self._warn_multi_role_backend(cur)
        else:
            self._engine.set_voice_resolver(None)

    # ---------------- 角色管理面板（本书） ----------------
    def _show_role_panel(self, parent_popup=None):
        """本书角色列表：人名 → 编号（模板/自定义），可换绑 / 覆盖 / 重置。"""
        if parent_popup is not None:
            parent_popup.dismiss()
        if self._role_map is None or not self._role_map.names():
            self._toast("还没有识别到本书角色（打开书籍后自动识别）")
            return
        popup = Popup(title="角色管理（本书）", size_hint=(0.94, 0.82))
        box = BoxLayout(orientation="vertical", spacing=dp(6), padding=dp(10))
        scroll = self._make_scroll()
        inner = BoxLayout(orientation="vertical", size_hint_y=None, spacing=dp(4))
        inner.bind(minimum_height=inner.setter("height"))

        def _reopen(*_):
            Clock.schedule_once(lambda _dt: self._show_role_panel(), 0)

        for entry in list(self._role_map.roles):
            name = entry.get("name", "")
            btn = ABButton(
                text="%s　→　%s" % (name, role_config.describe_entry(
                    entry, self._voice_template)),
                size_hint_y=None, height=dp(48), halign="left",
                valign="middle", font_size="12sp")
            btn.bind(size=lambda w, *_: setattr(
                w, "text_size", (w.width - dp(16), None)))
            btn.bind(on_release=lambda _b, n=name: self._open_role_menu(n))
            inner.add_widget(btn)
        scroll.add_widget(inner)
        box.add_widget(scroll)

        bottom = BoxLayout(size_hint_y=None, height=dp(46), spacing=dp(6))
        btn_rescan = ABButton(text="重新识别角色", font_size="12sp")
        btn_rescan.bind(on_release=lambda *_: self._reidentify_roles(popup))
        btn_ai = ABButton(text="AI 精细识别", font_size="12sp")
        btn_ai.bind(on_release=lambda *_: (popup.dismiss(),
                                           self._ai_identify_roles()))
        btn_close = ABPrimaryButton(text="关闭", font_size="12sp")
        btn_close.bind(on_release=lambda *_: popup.dismiss())
        bottom.add_widget(btn_rescan)
        bottom.add_widget(btn_ai)
        bottom.add_widget(btn_close)
        box.add_widget(bottom)
        popup.content = box
        popup.open()

    def _open_role_menu(self, name):
        """单个人物的操作菜单：换绑编号 / 自定义参数 / 重置。"""
        popup = Popup(title="角色：%s" % name, size_hint=(0.9, None),
                      height=dp(340))
        box = BoxLayout(orientation="vertical", spacing=dp(6), padding=dp(10))

        def _action(text, callback):
            btn = ABButton(text=text, size_hint_y=None, height=dp(46),
                           font_size="13sp")

            def _run(*_):
                popup.dismiss()
                callback()
            btn.bind(on_release=_run)
            return btn

        box.add_widget(_action("↔ 换绑音色编号（只换人名绑定）",
                               lambda: self._edit_role_binding(name)))
        box.add_widget(_action("✎ 自定义音色/语速/音调（不再跟随模板）",
                               lambda: self._edit_role_voice(name)))
        box.add_widget(_action("⟲ 重置为模板参数",
                               lambda: self._reset_role(name)))
        btn_back = ABButton(text="返回角色列表", size_hint_y=None, height=dp(46))
        btn_back.bind(on_release=lambda *_: (popup.dismiss(),
                                             self._show_role_panel()))
        box.add_widget(btn_back)
        popup.content = box
        popup.open()

    def _save_role_map(self):
        if self._role_map is not None:
            role_config.save_map(self.user_data_dir, self._role_map)

    def _edit_role_binding(self, name):
        """换绑编号：只改「人名 → 编号」的映射，编号底层音色参数不动。"""
        if self._role_map is None:
            return
        entry = self._role_map.get(name)
        current = entry.get("slot") if entry else ""
        slot_list = list(self._voice_template.all_slots().keys())
        popup = Popup(title="为「%s」选择编号（当前：%s）"
                      % (name, current or "未绑定"), size_hint=(0.9, 0.75))
        box = BoxLayout(orientation="vertical", spacing=dp(8), padding=dp(10))
        box.add_widget(self._auto_label(
            "选择编号后，这个人物就用该编号模板里的音色；"
            "其他小说不受影响。", font_size="12sp", min_height=dp(40)))
        spinner = Spinner(text=current or "点击选择…", values=slot_list,
                          size_hint_y=None, height=dp(46))

        def _pick(_s, slot_id):
            if slot_id in slot_list:
                self._role_map.set_slot(name, slot_id)
                self._save_role_map()
                popup.dismiss()
                self._toast("「%s」已绑定 %s（下一句起生效）" % (name, slot_id))
                self._show_role_panel()
        spinner.bind(text=_pick)
        box.add_widget(self._safe_text(spinner, min_height=dp(46)))
        btn_cancel = ABButton(text="取消", size_hint_y=None, height=dp(46))
        btn_cancel.bind(on_release=lambda *_: (popup.dismiss(),
                                               self._show_role_panel()))
        box.add_widget(btn_cancel)
        popup.content = box
        popup.open()

    def _edit_role_voice(self, name):
        """给单个人物覆盖自定义音色/语速/音调（之后不跟随全局模板）。"""
        if self._role_map is None:
            return
        entry = self._role_map.get(name)
        custom = entry.get("custom") if entry else None
        base = self._voice_template.get(entry.get("slot")) if entry else None
        ref = custom or base or {"voice": "", "pitch": 0, "rate": 1.0}

        def _save(voice, pitch, rate):
            self._role_map.set_custom(name, voice, pitch, rate)
            self._save_role_map()
            self._toast("「%s」已自定义音色，不再跟随模板" % name)
            self._show_role_panel()
        self._voice_param_editor(
            title="自定义：%s" % name, cur_voice=ref["voice"],
            cur_pitch=ref["pitch"], cur_rate=ref["rate"], on_save=_save)

    def _reset_role(self, name):
        """一键重置：清掉自定义参数，恢复跟随模板编号自带参数。"""
        if self._role_map is None:
            return
        self._role_map.reset_custom(name)
        self._save_role_map()
        self._toast("「%s」已重置为模板参数" % name)
        self._show_role_panel()

    def _reidentify_roles(self, popup=None):
        """重新识别角色：忽略旧映射，按出场顺序重新自动分配编号。"""
        if popup is not None:
            popup.dismiss()
        if not self._paragraphs:
            return
        self._toast("正在重新识别角色……")
        self._scan_roles_async(force_new=True)

    # ---------------- 全局音色模板面板 ----------------
    def _show_template_panel(self, parent_popup=None):
        """全局模板编辑：每个编号的 Edge 音色/音调/语速，改完全书同步。"""
        if parent_popup is not None:
            parent_popup.dismiss()
        popup = Popup(title="音色模板（全局 · 所有小说共用）",
                      size_hint=(0.94, 0.85))
        box = BoxLayout(orientation="vertical", spacing=dp(6), padding=dp(10))
        box.add_widget(self._auto_label(
            "修改编号参数后，所有小说里绑定该编号的人物立即同步；"
            "已单独自定义的角色不受影响。",
            font_size="12sp", min_height=dp(40)))
        scroll = self._make_scroll()
        inner = BoxLayout(orientation="vertical", size_hint_y=None, spacing=dp(3))
        inner.bind(minimum_height=inner.setter("height"))
        for slot_id, params in self._voice_template.all_slots().items():
            btn = ABButton(
                text="%s：%s · 调%+dHz · %.2fx" % (
                    slot_id, voice_friendly(params["voice"]),
                    int(params["pitch"]), float(params["rate"])),
                size_hint_y=None, height=dp(42), halign="left",
                valign="middle", font_size="12sp")
            btn.bind(size=lambda w, *_: setattr(
                w, "text_size", (w.width - dp(16), None)))
            btn.bind(on_release=lambda _b, sid=slot_id:
                     self._edit_template_slot(sid))
            inner.add_widget(btn)
        scroll.add_widget(inner)
        box.add_widget(scroll)

        bottom = BoxLayout(size_hint_y=None, height=dp(46), spacing=dp(6))
        btn_reset = ABDangerButton(text="全部恢复默认", font_size="12sp")
        btn_reset.bind(on_release=lambda *_: (
            self._voice_template.reset_all(),
            popup.dismiss(), self._show_template_panel(),
            self._toast("模板已全部恢复默认")))
        btn_close = ABPrimaryButton(text="关闭", font_size="12sp")
        btn_close.bind(on_release=lambda *_: popup.dismiss())
        bottom.add_widget(btn_reset)
        bottom.add_widget(btn_close)
        box.add_widget(bottom)
        popup.content = box
        popup.open()

    def _edit_template_slot(self, slot_id):
        """编辑一个模板编号的 Edge 音色 / 音调 / 语速（保存即全局生效）。"""
        params = self._voice_template.get(slot_id)
        if not params:
            return

        def _save(voice, pitch, rate):
            self._voice_template.set_slot(slot_id, voice, pitch, rate)
            self._toast("模板「%s」已更新，绑定该编号的角色全部同步"
                        "（自定义角色不受影响）" % slot_id)
            self._show_template_panel()

        def _reset():
            self._voice_template.reset_slot(slot_id)
            self._toast("模板「%s」已恢复默认" % slot_id)
            self._show_template_panel()
        self._voice_param_editor(
            title="编辑模板：%s" % slot_id, cur_voice=params["voice"],
            cur_pitch=params["pitch"], cur_rate=params["rate"],
            on_save=_save, on_reset=_reset)

    # ---------------- 音色参数编辑器（角色/模板共用） ----------------
    _PREVIEW_TEXT = "你好，这是这把音色的试听效果，今天天气不错。"

    def _stop_preview_player(self):
        """停掉并释放上一个试听播放器（重复点试听 / 退出时调用）。"""
        p = getattr(self, "_preview_player", None)
        if p is not None:
            try:
                p.stop()
                p.reset()
                p.release()
            except Exception:
                pass
            self._preview_player = None

    def _play_preview_file(self, path):
        """主线程：用 MediaPlayer 播放试听音频。"""
        self._stop_preview_player()
        from jnius import autoclass
        MediaPlayer = autoclass("android.media.MediaPlayer")
        player = MediaPlayer()
        try:
            jpath = autoclass("java.lang.String")(path)
            player.setDataSource(jpath)
            player.prepare()
            player.start()
        except Exception:
            try:
                player.release()
            except Exception:
                pass
            self._toast("试听播放失败")
            return
        self._preview_player = player

    def _preview_voice(self, voice):
        """后台合成一句固定文本并播放（试听 Edge 音色效果）。"""
        voice = str(voice or "")
        if not voice:
            self._toast("请先选择音色")
            return
        if not os.path.isdir(self.user_data_dir):
            self.user_data_dir = "."
        self._stop_preview_player()

        def _work():
            try:
                path = os.path.join(self.user_data_dir, "preview.wav")
                if os.path.exists(path):
                    try:
                        os.remove(path)
                    except OSError:
                        pass
                import vits_tts as _vt
                _vt._current_data_dir[0] = self.user_data_dir
                if not str(voice).startswith("vits:"):
                    self._post_to_main(lambda: self._toast(
                        "试听仅支持 VITS 离线音色"))
                    return
                _vt.synthesize_to_file(
                    self._PREVIEW_TEXT, voice, path,
                    rate="+0%", pitch="+0Hz", volume="+0%")

                def _play():
                    if os.path.exists(path):
                        self._play_preview_file(path)
                self._post_to_main(_play)
            except Exception as err:
                msg = str(err)
                self._post_to_main(lambda: self._toast(
                    "试听生成失败：%s" % msg[:60]))
        threading.Thread(target=_work, daemon=True).start()

    def _tts_voice_choices(self, current=""):
        """Edge 音色下拉选项：返回 (标签列表, 标签→ShortName 映射)。

        优先用运行时拉到的 Edge 音色列表；还没拉到（网络慢/失败）时
        退回内置已知清单，保证面板永远可用。
        """
        names = []
        for v in getattr(self, "_voice_list", []):
            n = v.get("name")
            if v.get("source") == "local" and n and n not in names:
                names.append(n)
        if not names:
            names = list(VOICE_FRIENDLY.keys())
        if current and current not in names:
            names.insert(0, current)
        labels, mapping, taken = [], {}, set()
        for n in names:
            lbl = voice_friendly(n)
            while lbl in taken:
                lbl += "·"
            taken.add(lbl)
            labels.append(lbl)
            mapping[lbl] = n
        return labels, mapping

    def _voice_param_editor(self, title, cur_voice="", cur_pitch=0,
                            cur_rate=1.0, on_save=None, on_reset=None):
        """弹窗编辑一组「音色 + 音调(Hz) + 语速(倍率)」。

        on_save(voice, pitch, rate) 保存回调；on_reset 提供时显示
        「恢复默认」按钮。
        """
        labels, mapping = self._tts_voice_choices(cur_voice)
        cur_label = next((l for l, n in mapping.items() if n == cur_voice),
                         labels[0] if labels else "无可用音色")
        state = {"voice": cur_voice, "pitch": int(cur_pitch),
                 "rate": float(cur_rate)}

        popup = Popup(title=title, size_hint=(0.92, None), height=dp(436))
        box = BoxLayout(orientation="vertical", spacing=_popup_spacing(),
                        padding=[_popup_pad_x(), _popup_pad_y(),
                                 _popup_pad_x(), _popup_pad_y()])

        box.add_widget(self._auto_label("音色（点「试听」先听效果再决定）",
                                        font_size="13sp",
                                        min_height=dp(22)))
        voice_row = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(6))
        spinner = Spinner(text=cur_label, values=labels or [cur_label],
                          size_hint_x=1, height=dp(44))
        spinner.bind(text=lambda _s, lbl: state.update(
            voice=mapping.get(lbl, state["voice"])))
        voice_row.add_widget(spinner)
        btn_preview = ABButton(text="▶ 试听", size_hint_x=None, width=dp(84),
                               font_size="12sp")
        btn_preview.bind(on_release=lambda *_: self._preview_voice(
            state["voice"]))
        voice_row.add_widget(btn_preview)
        # ⚠️ 这行是普通布局（没有 text/texture_size 属性），绝不能塞进
        # _safe_text（那是给 Button/Spinner/Label 等文字控件做自动换行的，
        # 塞布局进去会 AttributeError → 打开编辑器必闪退）
        box.add_widget(voice_row)

        box.add_widget(self._slider_row(
            "音调(Hz)", -50, 50, state["pitch"],
            lambda v: state.update(pitch=int(v))))
        box.add_widget(self._slider_row(
            "语速(倍)", 50, 200, int(round(state["rate"] * 100)),
            lambda v: state.update(rate=v / 100.0),
            fmt=lambda v: "%.2fx" % (v / 100.0)))

        btns = BoxLayout(size_hint_y=None, height=dp(46), spacing=dp(6))
        if on_reset is not None:
            btn_reset = ABButton(text="恢复默认", font_size="12sp")
            btn_reset.bind(on_release=lambda *_: (popup.dismiss(), on_reset()))
            btns.add_widget(btn_reset)
        btn_cancel = ABButton(text="取消", font_size="12sp")
        btn_cancel.bind(on_release=lambda *_: popup.dismiss())
        btns.add_widget(btn_cancel)
        btn_ok = ABPrimaryButton(text="保存", font_size="12sp")

        def _ok(*_):
            popup.dismiss()
            if on_save is not None:
                on_save(state["voice"], state["pitch"], state["rate"])
        btn_ok.bind(on_release=_ok)
        btns.add_widget(btn_ok)
        box.add_widget(btns)
        # 弹窗关闭（保存/取消）时停掉可能还在播的试听
        popup.bind(on_dismiss=lambda *_: self._stop_preview_player())
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
            btn = ABButton(text="第 %d 段 · %s" % (para + 1, bookmark.get("label", "")),
                           size_hint_y=None, height=dp(46), halign="left",
                           valign="middle", font_size="13sp")
            btn.bind(size=lambda w, *_: setattr(w, "text_size",
                                                (w.width - dp(20), None)))
            btn.bind(on_release=lambda _b, p=para: self._goto_bookmark(popup, p))
            inner.add_widget(btn)
        scroll.add_widget(inner)
        box.add_widget(scroll)

        btn_clear = ABDangerButton(text="清空本书书签", size_hint_y=None,
                                   height=dp(46))
        def _clear(*_):
            popup.dismiss()
            self.clear_bookmarks()
        btn_clear.bind(on_release=_clear)
        box.add_widget(btn_clear)
        popup.content = box
        popup.open()

    def _goto_bookmark(self, popup, para):
        """跳到书签处 —— 同样只定位（默认暂停），点 ▶ 才开始念。"""
        popup.dismiss()
        para = max(0, min(int(para), len(self._paragraphs) - 1))
        self._seek_to(para, "已跳转到书签：第 %d 段" % (para + 1))

    def toggle_play(self):
        if not self._paragraphs:
            self._toast("请先打开一本书")
            return
        self._note_media_event("界面切换", "底部按钮")
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

    # ============================================================
    #      外部播放/暂停命令（通知栏按钮 / 耳机线控 / 蓝牙 / 系统媒体键）
    # ============================================================
    def media_play(self, source="外部"):
        """外部要求「播放」：**已经在播就什么都不做**（幂等）。

        ⚠️ 这里以前接的是 `toggle_play()`：只要外面来一个「播放」请求，在播状态
        就会被**反转成暂停** —— 蓝牙耳机重连自动续播、锁屏/系统重发 PLAY、
        ROM 重复投递媒体键都会命中，表现就是「念着念着自己停下，且没有任何
        提示」。播放/暂停类命令必须按**语义**执行，绝不能反转。
        """
        self._note_media_event("播放", source)
        try:
            if not self._paragraphs:
                return
            state = self._engine.get_state()
            if state == STATE_PLAYING:
                return                    # 已在播放：忽略重复命令（不再反转成暂停）
            self._follow = True
            if state == STATE_PAUSED:
                self._engine.resume()
            else:
                para, _, _ = self._engine.get_position()
                self._engine.play(para)
        except Exception as e:
            self._on_error("外部播放命令执行失败：%s" % e)

    def media_pause(self, source="外部"):
        """外部要求「暂停」：只在正在播时才暂停（幂等），并提示命令来源。

        提示不是装饰：万一以后又出现「自己停下」，这条提示会直接告诉我们
        是耳机/通知栏/系统的哪一次命令触发的，而不是继续靠猜。
        """
        self._note_media_event("暂停", source)
        try:
            if self._engine.get_state() != STATE_PLAYING:
                return
            self._engine.pause()
            self._toast("已暂停朗读（%s）" % source)
        except Exception as e:
            self._on_error("外部暂停命令执行失败：%s" % e)

    def _media_playpause_compat(self):
        """旧版 Java 的 "playpause" 兼容入口（打包未更新时不至于反向升级）。

        旧实现是「无脑反转」：同一个播放请求重复到达两次就会被反成暂停。
        这里加 1 秒去重：重复命令直接忽略，其余仍按切换处理。
        """
        now = time.time()
        if now - self._last_playpause_ts < 1.0:
            self._note_media_event("忽略重复", "通知栏/耳机")
            return
        self._last_playpause_ts = now
        self.toggle_play()

    def _note_media_event(self, what, source):
        """记录播放/暂停命令到自检屏（无 adb 时靠它定位「谁按的」）。"""
        try:
            self._media_events += 1
            self._last_media_event = "%s@%s 来源=%s" % (
                what, time.strftime("%H:%M:%S"), source)
        except Exception:
            pass

    # ============================================================
    #      后台保活：电池不优化白名单（熄屏不被系统冻结的前提）
    # ============================================================
    def _maybe_prompt_keepalive(self):
        """开始播放时，若本应用还没进「电池不优化」白名单 → 提示一次。

        ⚠️ 这就是「熄屏播放一段时间后停止、打开软件又恢复播放」的成因：
        熄屏后系统把本应用的**主进程**当缓存应用冻结（Android 11+ 的
        Cached Apps Freezer，vivo 还叠加自家后台管控），我们的 Python 推进
        循环与联网合成都被挂起 —— 当前这句 mp3 播完，就再没人推进下一句。
        唤醒锁只保证 CPU 不睡，**不能**免冻结；按 UID 判定的「电池不优化」
        白名单是第三方应用唯一可靠的办法（同 services/playback.py 的注释）。
        """
        if self._keepalive_prompted or self._config.get("keepalive_prompted", False):
            return
        try:
            ok = self.battery_allowlisted()
        except Exception:
            ok = None
        if ok is None:              # 桌面环境 / ROM 取不到 → 不打扰用户
            return
        self._keepalive_prompted = True
        try:
            self._config.set("keepalive_prompted", True)
            self._config.save()
        except Exception:
            pass
        if ok:                      # 已经白名单了：不用提示
            return
        Clock.schedule_once(lambda _dt: self._show_keepalive_popup(), 0.6)

    def _show_keepalive_popup(self):
        """一次性引导：把应用加入「电池不优化 / 后台不被冻结」白名单。"""
        try:
            popup = Popup(title="让朗读熄屏后不中断", size_hint=(0.9, 0.62))
            box = BoxLayout(orientation="vertical", spacing=dp(12),
                            padding=[dp(14), dp(14), dp(14), dp(14)],
                            size_hint_y=None)
            box.bind(minimum_height=box.setter("height"))
            box.add_widget(self._auto_label(
                "系统会把熄屏后的后台进程冻结：朗读会在当前这句读完后停住，"
                "重新打开软件才接上。\n\n"
                "下一屏请把本应用设为「允许 / 不优化」，之后熄屏听书就不会"
                "再被打断。",
                font_size="13sp", min_height=dp(130), halign="left"))
            row = BoxLayout(size_hint_y=None, spacing=dp(10))
            btn_later = ABButton(text="暂不")
            btn_go = ABPrimaryButton(text="一键允许")

            def _later(*_):
                popup.dismiss()

            def _go(*_):
                popup.dismiss()
                self._open_power_settings()

            btn_later.bind(on_release=_later)
            btn_go.bind(on_release=_go)
            row.add_widget(btn_later)
            row.add_widget(btn_go)
            self._auto_row(row)
            box.add_widget(row)
            popup.content = box
            popup.open()
        except Exception as e:
            self._on_error("保活提示打开失败：%s" % e)

    def jump_paragraph(self, delta):
        if not self._paragraphs:
            return
        para, _, _ = self._engine.get_position()
        target = max(0, min(para + delta, len(self._paragraphs) - 1))
        self._engine.seek_paragraph(target)
        self._set_highlight(target)
        self._save_position()

    def _seek_to(self, index, tip):
        """把进度挪到 index —— **一律停在暂停状态**，点 ▶ 才开始念。

        上一章/下一章、目录选章、书签跳转都走这里。跳转本身是一次「重新
        选位置」的操作，所以先把朗读停掉再定位：不管跳转前是不是在念，
        跳完都是暂停态，由用户点底部 ▶ 决定什么时候开始。

        ⚠️ 顺序不能反：pause() 之后再 seek —— pause 保留位置、并把状态置为
        paused，此时 seek_paragraph 不会发声（它只在 PLAYING 时才接着念）。
        """
        index = max(0, min(int(index), len(self._paragraphs) - 1))
        self._engine.pause()        # 正在念就停下；没在念是空操作（状态不变）
        self._engine.seek_paragraph(index)
        self._set_highlight(index)
        self._save_position(persist=True)   # 跳转立刻落盘，杀掉也不丢
        self._toast(tip + "（点 ▶ 开始朗读）")

    def jump_chapter(self, delta):
        """跳到上一章 / 下一章的开头（默认暂停，点 ▶ 才开始念）。

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
        self._seek_to(start, "已跳到「%s」" % title[:18])

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
        # Qwen 逐句标注：进入新窗口时后台预标注（不阻塞播放）
        try:
            self._ensure_ai_annotation(index)
        except Exception:
            pass

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
            # 重新进入播放态：清零卡死计速，避免把「开头合成等待」误判为卡死
            self._frozen_last = None
            self._frozen_since_tick = None
            self._freeze_recovers = 0
            # 首次播放时提示一次「电池不优化白名单」（熄屏不被冻结的前提）
            self._maybe_prompt_keepalive()
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
        if current.startswith("kokoro:"):
            # 离线引擎已移除：老版本存的 kokoro 音色回落到系统默认音色
            current = ""
            self._config.set("voice_name", "")
            self._config.save()
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
        #
        # ★ 冻结检测（熄屏后「念着念着停了、亮屏又接上」的成因就在这里）：
        #   本循环是「Python 线程 sleep 0.2s → Handler 投递主线程」。熄屏一段时间
        #   后，系统会把本应用的主进程放进**缓存冻结**（Android 11+ 的
        #   Cached Apps Freezer；vivo 还会叠加自家后台管控），整条循环连同联网
        #   合成一起被挂起 —— 当前这句 mp3 播完就再没人推进下一句。
        #   被冻结期间 wall clock 照走，所以解冻后的第一次 tick 会出现**异常大的
        #   间隔**：正常 0.2s，被冻结则几十秒到几分钟。据此就能确认「是系统冻结，
        #   不是我们的逻辑卡住」，并顺便告诉用户该去加白名单。
        _now = time.time()
        if self._last_tick_wall > 0:
            _gap = _now - self._last_tick_wall
            if _gap > self._freeze_gap_secs:
                self._freeze_events += 1
                self._last_freeze_gap = _gap
                if self._is_playing:
                    # ★ 只有「播放中被冻结」才会真的造成「熄屏后不念」；
                    #   闲置时（没在朗读）被冻是系统的正常行为，不该混在一起数，
                    #   否则白名单生效了也可能看到「冻结N次」而误判。
                    self._freeze_play_events += 1
                    self._freeze_during_play = True
                    # 提示限流：连续多次冻结不要刷屏
                    if _now - self._freeze_hint_ts > 180:
                        self._freeze_hint_ts = _now
                        self._toast("系统把后台朗读冻结了 %.0f 秒（已自动续读）"
                                    % _gap)
        self._last_tick_wall = _now
        self._tick_count += 1
        self._poll_role_scan()
        self._engine.poll_advance()

        if self._engine.get_state() == STATE_PLAYING:
            self._save_position()
            # 用引擎的插值位置刷新进度条：否则它只在每读完一句时跳一格
            _para, char_now, total_chars = self._engine.get_position()
            self._on_progress(char_now, total_chars)
            # 卡死自检：播放态但长时间没有任何「有声进展」 → 自恢复并刷新媒体通知。
            # ⚠️ 绝不能因为 playing=False 就把毫秒位置丢掉：部分 ROM 在**正常播放**
            #    时 isPlaying() 恒为 false，丢掉位置就等于退回「整句恒定」的
            #    (段,字符) 判定 —— 长句又会被误判成卡死。
            _mpos = -1
            try:
                _playing, _mpos = self._engine.media_position()
            except Exception:
                _mpos = -1
            self._check_playback_freeze(_para, char_now, _mpos)
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

    def _check_playback_freeze(self, para, char, media_pos=-1):
        """卡死自检：播放态下长时间没有任何「有声进展」→ 自恢复，**绝不静默暂停**。

        信号优先级（缺一层就会误判，每一层都对应一次真实的误停）：
          1. `is_busy()` —— 正在联网合成当前句（弱网可达十几秒），位置不动属
             正常等待，直接清零计时；
          2. `progress_age()` —— 引擎自报「距上次有声进展」的秒数。有进展 =
             媒体位置前进 / isPlaying 真 / 开始播放 / 句末推进 / 正在合成。
             这是唯一可信的信号：Edge 的 (段, 字符) 整句播放期间恒定，
             部分 ROM 的 `isPlaying()` 在正常播放时也恒为 false；
          3. 后端不提供 progress_age 时，退回「(段, 字符, 媒体毫秒) 变没变」。

        ⚠️ 判定卡死后只做**自恢复**（重开当前句），连续 3 次仍无声音才**跳句**。
        以前这里会 `pause()`：用户听到的就是「念着念着自己停下、还得手动点 ▶」，
        而且暂停是"没声音"的升级版（没声音 + 要人工干预）—— 两个都不可接受。
        宁可多试几次 / 跳过一句，也不让朗读停住。
        """
        # 第一层：正在合成当前句（慢网络/抖动）→ 不算卡死，清掉计时重新算
        try:
            if self._engine.is_busy():
                self._frozen_last = None
                self._frozen_since_tick = None
                return
        except Exception:
            pass

        # 第二层：引擎自报「多久没有声音进展」（最可靠）
        try:
            age = self._engine.progress_age()
        except Exception:
            age = None

        if age is not None:
            if age < self._freeze_secs:
                self._frozen_last = (para, char, media_pos)
                self._frozen_since_tick = None
                self._freeze_recovers = 0      # 声音又动了 → 自恢复计数清零
                return
            stalled = True
        else:
            # 第三层兜底：位置键变没变（旧判定的等价形式）
            key = (para, char, media_pos)
            if key != self._frozen_last:
                self._frozen_last = key
                self._frozen_since_tick = self._tick_count
                return
            if self._frozen_since_tick is None:
                self._frozen_since_tick = self._tick_count
                return
            stalled = (self._tick_count - self._frozen_since_tick
                       >= self._freeze_ticks)
        if not stalled:
            return

        # 真卡死：先自恢复（重开本句），连续失败才跳句 —— 任何情况都不暂停
        self._frozen_last = None
        self._frozen_since_tick = None
        _age_s = 0.0 if age is None else float(age)
        try:
            self._last_freeze_note = "%s 无声音%.0fs" % (
                time.strftime("%H:%M:%S"), _age_s)
        except Exception:
            pass
        if self._freeze_recovers >= 3:
            self._freeze_recovers = 0
            self._skips += 1
            self._last_freeze_note += " 跳过1句(累计%d)" % self._skips
            self._toast("这一句读不出来，已跳过继续")
            try:
                self._engine.skip_current()
            except Exception:
                pass
            return
        self._freeze_recovers += 1
        self._last_freeze_note += " 自恢复第%d次" % self._freeze_recovers
        # 提示限流：卡顿反复出现时不要每 12 秒弹一次
        _now = time.time()
        if _now - self._last_freeze_toast > 30:
            self._last_freeze_toast = _now
            self._toast("检测到播放卡顿，已自动重试本句")
        try:
            self._engine.recover()
        except Exception as e:
            # 自恢复本身失败也**不暂停**：改为跳句继续往下念
            self._freeze_recovers = 3
            try:
                self._engine.skip_current()
            except Exception:
                pass
            self._on_error("自恢复失败，已跳过：%s" % e)

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
        # 「选择后跟随」：打开目录时自动高亮并定位到当前正在读/播的章节
        cur = self._view_chapter
        rv.data = [{"text": "%s    (第 %d 段)" % (t, s + 1),
                    "index": i, "current": (i == cur)}
                   for i, (t, s) in enumerate(self._chapters)]

        box = BoxLayout(orientation="vertical")
        box.add_widget(rv)
        popup.content = box
        self._chapter_popup = popup
        self._chapter_rv = rv
        popup.bind(on_dismiss=lambda *_: setattr(self, "_chapter_rv", None))

        def _scroll_to_cur(_dt):
            # 弹窗布局完成后再滚，确保 RecycleView 已算好尺寸；
            # 尺寸还没就绪（首帧 height 仍为 0）就下一帧再试。
            if self._chapter_rv is not rv or cur < 0:
                return
            if not self._toc_scroll_to_top(rv, cur):
                Clock.schedule_once(_scroll_to_cur, 0)
        popup.open()
        Clock.schedule_once(_scroll_to_cur, 0)

    def goto_chapter_index(self, chapter_index):
        """目录里点了第 chapter_index 章 → 跳过去（默认暂停，点 ▶ 才开始念）。"""
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
        # 与「上一章/下一章」「书签跳转」同一套规则：只定位，不自动朗读
        self._seek_to(start, "已跳到「%s」" % title[:18])

    def _toc_scroll_to_top(self, rv, cur):
        """把目录滚动到「第 cur 章位于列表最顶部」。

        固定行高（dp(46)+间距 dp(2)）下，scroll_y 与内容偏移是线性映射：
        scroll_y=1 显示第 0 章（顶部），scroll_y=0 显示末章（底部）。要让第 cur
        章顶格，需 scroll_y = 1 - cur*行高/(内容高-视口高)。比 scroll_to_index
        （只保证可见、常把目标停在视口底部）更符合「当前章在最上面」的预期。

        返回 True 表示已滚动（或无需滚动）；返回 False 表示尺寸未就绪，调用方
        应下一帧重试。
        """
        try:
            line_h = dp(46) + dp(2)
            n = len(self._chapters)
            if n <= 0 or cur < 0 or cur >= n:
                return True
            content_h = n * line_h
            view_h = rv.height
            if view_h <= 0:
                return False          # 还没布局好
            if content_h <= view_h:
                rv.scroll_y = 1.0     # 全装得下，直接置顶
            else:
                top_off = cur * line_h
                max_off = content_h - view_h
                rv.scroll_y = 1.0 - min(top_off, max_off) / max_off
            return True
        except Exception:
            return True

    def _toc_mark_current(self, cur):
        """目录弹窗开着时，把高亮与滚动位置同步到当前章（播放换章后实时跟随）。

        只在章节真正切换时调用（见 _refresh_view），不会每读一段都触发，
        因此不会和用户在目录里的手动滚动打架。换章时把当前章顶格到列表最上方，
        与「打开目录即定位到当前章」行为一致。
        """
        rv = self._chapter_rv
        if rv is None or cur < 0 or cur >= len(self._chapters):
            return
        for i, item in enumerate(rv.data):
            item["current"] = (i == cur)
        rv.refresh_from_data()
        self._toc_scroll_to_top(rv, cur)


    # ============================================================
    #      排版辅助：给文本留安全边距（弹窗专用）
    #
    # 之前弹窗里到处是 `Label(...) + size_hint_y=None, height=dp(24)` 的固定高度：
    # 文字一多、屏幕一窄，Label 既不换行也不长高，多出来的字就横向/纵向溢出，
    # 被 ScrollView 的裁剪框和弹窗边界切掉（典型就是「定时休眠（分」被吃掉一半）。
    # 下面两个工具把这件事统一掉：**宽度变了按框重排，文字变高就让控件跟着长高**。
    # ============================================================
    def _auto_label(self, text, font_size="13sp", size_hint_x=1, width=None,
                    min_height=0, halign="left", valign="center",
                    margin=None):
        """建一个不会被裁切的 Label：留边距 + 自动换行 + 自动长高。"""
        # ⚠️ dp() 必须在**运行时**算：写进默认参数会在 import 时求值，那会儿
        #    屏幕密度还没确定（真机上是 1 以外的值），等于写死了一个错误倍数。
        margin = dp(6) if margin is None else margin
        _min = max(dp(20), float(min_height or 0))
        kw = dict(text=text, font_size=font_size, halign=halign,
                  valign=valign, size_hint_y=None, height=_min)
        if width is not None:
            kw.update(size_hint_x=None, width=width)
        else:
            kw["size_hint_x"] = size_hint_x
        lbl = Label(**kw)

        def _fit(w, *_):
            # 宽度变了立刻按新宽度重排：文字只在框内换行，不会横向溢出
            w.text_size = (max(0.0, w.width - 2 * margin), None)

        def _grow(w, *_):
            # 换行几行就长多高：写死 height 会把第二行压到框外看不见
            h = max(_min, float(w.texture_size[1]) + 2 * margin)
            if abs(h - w.height) > 0.5:
                w.height = h

        lbl.bind(size=_fit, texture_size=_grow)
        _fit(lbl)
        _grow(lbl)
        return lbl

    def _safe_text(self, w, min_height=None, margin=None):
        """给已有文字控件（Button / Spinner / Label）套上同一套安全排版规则。

        原本 `size_hint_y=None, height=dp(44)` 写死的按钮，屏幕窄或字多时
        会直接溢出；改成自在换行 + 自动长高后，内容永远不会跑到框外面。
        """
        if min_height is None:
            min_height = dp(44)
        if margin is None:
            margin = dp(10)
        # 布局容器（BoxLayout 等）没有 text/text_size/texture_size 属性，
        # 套这套排版规则会 AttributeError 闪退 —— 原样放行
        if not hasattr(w, "text"):
            return w
        try:
            if w.size_hint_y is not None:      # 原来由父布局拉满 → 改成自己算高度
                w.size_hint_y = None
                w.height = max(min_height, float(w.height or 0))
            else:
                w.height = max(min_height, float(w.height or 0))
        except Exception:
            pass

        def _fit(x, *_):
            x.text_size = (max(0.0, x.width - 2 * margin), None)

        def _grow(x, *_):
            h = max(min_height, float(x.texture_size[1]) + 2 * dp(7))
            if abs(h - x.height) > 0.5:
                x.height = h

        w.bind(size=_fit, texture_size=_grow)
        _fit(w)
        _grow(w)
        return w

    def _clip_text(self, w, margin=None):
        """宽度写死的控件（下拉框 / 定时休眠的两个按钮）：只约束横向排版。

        高度仍然由父布局决定（它们 size_hint_y=1 撑满行高），所以这里**不能**
        顺手改 size_hint_y —— 只做「文字不许横着跑出边框」。
        """
        margin = dp(6) if margin is None else margin
        w.bind(size=lambda x, *_: setattr(
            x, "text_size", (max(0.0, x.width - 2 * margin), None)))
        return w

    def _auto_row(self, row, min_height=None):
        """行高跟内容走：行内的 Label 换行后，这一行同时长高。

        只统计 size_hint_y=None 的子控件（它们的高度是自己算的）；
        撑满高度的（Slider / CheckBox / 按钮）跟随行高，不能反过来决定行高，
        否则行高与子控件高度互相依赖会死循环。
        """
        if min_height is None:
            min_height = dp(46)

        def _sync(*_):
            hs = [c.height for c in row.children
                  if getattr(c, "size_hint_y", None) is None]
            row.height = max([min_height] + hs)

        for c in row.children:
            if getattr(c, "size_hint_y", None) is None:
                c.bind(height=_sync)
        _sync()
        return row

    def show_settings(self):
        """设置面板：音色 / 音调 / 语速 / 语调起伏 / 字号 / 定时休眠。"""
        from kivy.uix.checkbox import CheckBox

        popup = Popup(title="设置", size_hint=(0.92, 0.88))
        # 内容较多（尤其小屏），放进 ScrollView，避免最下面的按钮被挤出弹窗
        # padding = 横向/纵向安全边距：文字不再紧贴弹窗边界（贴边就是被裁切的那一刀）
        # spacing = 排与排之间的垂直间距：挨太近时，Label 换行长出来的第二行会压到下一排
        box = BoxLayout(orientation="vertical", spacing=_popup_spacing(),
                        padding=[_popup_pad_x(), _popup_pad_y(),
                                 _popup_pad_x(), _popup_pad_y() + dp(4)],
                        size_hint_y=None)
        box.bind(minimum_height=box.setter("height"))

        # 自检信息：无 adb 时让用户截图即可定位（装的是哪版 / 推进是否在跑 / 唤醒锁是否生效）
        _eng_state = self._engine.get_state() if self._engine is not None else "-"
        # 卡死自检：直接显示引擎自报的「距上次有声进展」秒数 + 自恢复/跳句计数。
        # 播放中若「无进展」在涨，就是真卡死；停在 0.x 秒说明一直有声——
        # 这两个数字能一眼区分「推进链路挂了 / 引擎没出声 / 一切正常」。
        try:
            _age = self._engine.progress_age() if self._engine is not None else None
        except Exception:
            _age = None
        try:
            if _age is not None:
                _eng_extra = "  无进展%.1fs/%.0fs  自恢复%d  跳句%d" % (
                    float(_age), self._freeze_secs,
                    self._freeze_recovers, self._skips)
            elif _eng_state == "playing" and self._frozen_since_tick is not None:
                _stall = self._tick_count - self._frozen_since_tick
                _eng_extra = "  卡死计时%d/%d" % (min(_stall, self._freeze_ticks),
                                                  self._freeze_ticks)
            else:
                _eng_extra = ""
        except Exception:
            _eng_extra = ""
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
        # 保活：白名单状态 + 被系统冻结的次数/最近时长。
        # 「熄屏后念着念着停了、亮屏又接上」= 主进程被缓存冻结（Python 推进循环
        # 与联网合成一起挂起）。这两个数字就是它的证据：冻结>0 且最近时长很大。
        try:
            _bat = self.battery_allowlisted()
            _bat_s = ("已允许" if _bat else "未允许") if _bat is not None else "未知"
        except Exception:
            _bat_s = "未知"
        _diag_text = (
            "版本 %s\n"
            "推进 %s   tick=%d\n"
            "监听器 %s   媒体控制 %s\n"
            "唤醒锁 %s%s\n"
            "前台服务 %s%s\n"
            "保活 电池白名单=%s  冻结%d次(播放中%d) 最近%.0fs\n"
            "引擎 %s%s\n"
            "卡顿 %s\n"
            "媒体事件 %d  %s\n"
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
               _bat_s, self._freeze_events, self._freeze_play_events,
               self._last_freeze_gap,
               _eng_state, _eng_extra,
               (str(self._last_freeze_note)[:90] or "无"),
               self._media_events,
               (str(self._last_media_event)[:70] or "无"),
               _layout,
               _book, _pos_s,
               (_crash_last or "无"),
               (str(self._last_error)[:120] or "无"))
        )
        _diag = ABDimLabel(text=_diag_text, size_hint_y=None, height=dp(92),
                           font_size="11sp", halign="left", valign="top")
        # 左右各留 dp(10) 安全边距；高度随文字自适应，错误信息再长也不溢出
        _diag.bind(size=lambda w, *_: setattr(
            w, "text_size", (max(0.0, w.width - dp(10)), None)))
        _diag.bind(texture_size=lambda w, *_: setattr(
            w, "height", max(dp(92), w.texture_size[1] + dp(14))))
        box.add_widget(_diag)

        # ---- 音色 ----
        box.add_widget(self._auto_label("音色（系统引擎 + VITS 离线）",
                                        font_size="13sp", min_height=dp(24)))
        voice_labels = {v["label"]: v["name"] for v in getattr(self, "_voice_list", [])}
        current = str(self._config.get("voice_name", ""))
        current_label = next((lbl for lbl, n in voice_labels.items() if n == current), None)
        spinner = Spinner(text=current_label or (list(voice_labels)[0] if voice_labels else "无可用音色"),
                          values=list(voice_labels), size_hint_y=None, height=dp(44))

        def _pick_voice(_s, text):
            name = voice_labels.get(text)
            if name:
                self._engine.set_voice(name)
                self._config.set("voice_name", name)
                self._config.save()
                self._warn_multi_role_backend(name)
        spinner.bind(text=_pick_voice)
        # 音色名可能很长：同样按「留边距 + 自动长高」排，别让它顶出弹窗
        box.add_widget(self._safe_text(spinner, min_height=dp(44)))

        # ---- 音调 ----
        box.add_widget(self._slider_row("音调", -10, 10,
                                        int(self._config.get("pitch", 0)),
                                        self._on_pitch))
        # ---- 语速 ----
        box.add_widget(self._slider_row("语速", 5, 30,
                                        int(float(self._config.get("speed", 1.0)) * 10),
                                        self._on_speed, fmt=lambda v: "%.1fx" % (v / 10.0)))

        # ---- 语调起伏 ----
        row = BoxLayout(size_hint_y=None, spacing=dp(8))
        row.add_widget(self._auto_label("语调起伏（自然抑扬顿挫）",
                                        font_size="13sp", min_height=dp(24)))
        chk = CheckBox(active=bool(self._config.get("intonation", True)),
                       size_hint_x=None, width=dp(44))

        def _toggle(_c, value):
            self._engine.set_intonation(value)
            self._config.set("intonation", bool(value))
            self._config.save()
        chk.bind(active=_toggle)
        row.add_widget(chk)
        box.add_widget(self._auto_row(row, dp(46)))

        # ---- 字号 ----
        box.add_widget(self._slider_row("字号", 10, 30,
                                        int(self._config.get("font_size", 15)),
                                        self._on_font))

        # ---- 行距 ----
        box.add_widget(self._slider_row("行距", 10, 20,
                                        int(round(self.line_height * 10)),
                                        self._on_line_height,
                                        fmt=lambda v: "%.1fx" % (v / 10.0)))

        # ---- 多角色朗读（开关收进一行，说明见下方按钮文字本身）----
        multi_row = BoxLayout(size_hint_y=None, spacing=dp(8))
        multi_row.add_widget(self._auto_label("多角色朗读（对话按人物分声）",
                                              font_size="13sp",
                                              min_height=dp(24)))
        btn_multi = ABButton(text=self._multi_role_btn_text(),
                             size_hint_x=None, width=dp(96))
        btn_multi.bind(on_release=lambda *_: self._toggle_multi_role(btn_multi))
        self._clip_text(btn_multi)
        multi_row.add_widget(btn_multi)
        box.add_widget(self._auto_row(multi_row, dp(46)))

        btn_roles = ABButton(text="角色管理（本书 · 人名绑定音色编号）",
                             size_hint_y=None, height=dp(44))
        btn_roles.bind(on_release=lambda *_: self._show_role_panel(popup))
        box.add_widget(self._safe_text(btn_roles, min_height=dp(44)))

        btn_tpl = ABButton(text="音色模板（全局 · 编辑各编号音色参数）",
                           size_hint_y=None, height=dp(44))
        btn_tpl.bind(on_release=lambda *_: self._show_template_panel(popup))
        box.add_widget(self._safe_text(btn_tpl, min_height=dp(44)))

        # ---- 离线 AI 角色分析（可选：导入/下载模型后才能用）----
        box.add_widget(self._auto_label("离线 AI 角色分析（Qwen · 可选）",
                                        font_size="13sp",
                                        min_height=dp(24)))
        try:
            _ai_ok, _ai_reason = ai_roles.model_ready(self.user_data_dir)
            box.add_widget(self._auto_label("Qwen 模型：%s" % _ai_reason,
                                            font_size="12sp",
                                            min_height=dp(22)))
            btn_ai_import = ABButton(text="导入 Qwen 模型（电脑下载 .gguf 后传手机）",
                                     size_hint_y=None, height=dp(44))
            btn_ai_import.bind(on_release=lambda *_: self._pick_ai_model_file())
            box.add_widget(self._safe_text(btn_ai_import, min_height=dp(44)))
            btn_ai_dl = ABButton(text="在 App 内下载 Qwen 模型（%s，需网络）"
                                      % ai_roles.MODEL_LABEL,
                                 size_hint_y=None, height=dp(44))
            btn_ai_dl.bind(on_release=lambda *_: self._download_ai_model())
            box.add_widget(self._safe_text(btn_ai_dl, min_height=dp(44)))
        except Exception:
            pass
        btn_ai_run = ABPrimaryButton(text="AI 精细识别人物（本书 · 约1分钟）",
                                     size_hint_y=None, height=dp(44))
        btn_ai_run.bind(on_release=lambda *_: self._ai_identify_roles())
        box.add_widget(self._safe_text(btn_ai_run, min_height=dp(44)))

        # ---- 本地语音合成模型（VITS · 完全离线 · 替代在线TTS）----
        box.add_widget(self._auto_label("离线语音合成（VITS · 必需）",
                                        font_size="13sp",
                                        min_height=dp(24)))
        try:
            import vits_tts as _vt
            _vt._current_data_dir[0] = self.user_data_dir
            _v_ok, _v_reason = _vt.model_ready(self.user_data_dir)
            box.add_widget(self._auto_label("VITS 模型：%s" % _v_reason,
                                            font_size="12sp",
                                            min_height=dp(22)))
            btn_v_import = ABButton(text="导入 VITS 语音包（fanchen-C .tar.bz2）",
                                    size_hint_y=None, height=dp(44))
            btn_v_import.bind(on_release=lambda *_: self._pick_vits_model_file())
            box.add_widget(self._safe_text(btn_v_import, min_height=dp(44)))
            btn_v_dl = ABButton(text="在 App 内下载 VITS 语音包（%s，需网络）"
                                      % _vt.MODEL_TITLE,
                                size_hint_y=None, height=dp(44))
            btn_v_dl.bind(on_release=lambda *_: self._download_vits_model())
            box.add_widget(self._safe_text(btn_v_dl, min_height=dp(44)))
            btn_v_label = ABButton(text="音色试听与性别标注（一次性，改善多角色）",
                                   size_hint_y=None, height=dp(44))
            btn_v_label.bind(on_release=lambda *_: self._show_voice_label_panel())
            box.add_widget(self._safe_text(btn_v_label, min_height=dp(44)))
        except Exception:
            pass

        # ---- 主题（深色 / 浅色，点击即时切换并记忆）----
        btn_theme = ABButton(
            text=("当前：浅色（点按切换为深色）" if self.theme == "light"
                  else "当前：深色（点按切换为浅色）"),
            size_hint_y=None, height=dp(44))
        btn_theme.bind(on_release=lambda *_: self._toggle_theme(btn_theme))
        box.add_widget(self._safe_text(btn_theme, min_height=dp(44)))

        # ---- 定时休眠 ----
        # 这一排原来横向挤了 4 个控件（90+70+64 + 标签），窄屏上标签只剩二三十 dp，
        # 字纵向溢出就是这里被切成了「定时休眠（分」。现在：右边三个控件让出宽度，
        # 标签开启自动换行 + 自动长高，行高跟着内容走，宁可占两行也不切字。
        sleep_row = BoxLayout(size_hint_y=None, spacing=dp(8))
        sleep_row.add_widget(self._auto_label("定时休眠（分钟）", font_size="13sp",
                                              min_height=dp(28)))
        spin_sleep = Spinner(text="30", values=["15", "30", "45", "60", "90", "120"],
                             size_hint_x=None, width=dp(78))
        btn_sleep = ABPrimaryButton(text="启动", size_hint_x=None,
                                    width=dp(58))

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
        btn_cancel_sleep = ABButton(text="取消", size_hint_x=None,
                                    width=dp(54))
        btn_cancel_sleep.bind(on_release=_cancel_sleep)
        # 窄屏上就算换行全显示出来，标签若被压成一竖条（一两个字一行）也没法看 ——
        # 所以宽度不够时，右边三个控件整体按比例收一点，优先保证标签够写一行。
        _need_label = dp(120)        # 标签一行写完「定时休眠（分钟）」所需的最小宽度
        _base_w = {spin_sleep: dp(78), btn_sleep: dp(58), btn_cancel_sleep: dp(54)}

        def _fit_sleep_row(*_):
            avail = sleep_row.width
            if avail <= 0:
                return
            fixed = sum(_base_w.values()) + 3 * dp(8)
            free = avail - fixed
            k = (1.0 if free >= _need_label
                 else max(0.62, (avail - _need_label) / fixed))
            for w_, base in _base_w.items():
                w_.width = base * k     # text_size 由 _clip_text 绑 size 自动跟上

        sleep_row.bind(width=_fit_sleep_row)
        _fit_sleep_row()
        # 这三个控件的高度由行决定，只约束横向排版（文字不许跑出边框）
        self._clip_text(spin_sleep)
        self._clip_text(btn_sleep)
        self._clip_text(btn_cancel_sleep)
        sleep_row.add_widget(spin_sleep)
        sleep_row.add_widget(btn_sleep)
        sleep_row.add_widget(btn_cancel_sleep)
        box.add_widget(self._auto_row(sleep_row, dp(48)))

        btn_help = ABButton(text="使用说明", size_hint_y=None, height=dp(44))
        def _show_help(*_):
            popup.dismiss()
            self.show_usage_hint()
        btn_help.bind(on_release=_show_help)
        box.add_widget(self._safe_text(btn_help, min_height=dp(44)))

        # 熄屏/后台朗读要靠系统「不冻结本应用」——一键跳到省电白名单设置页。
        btn_power = ABPrimaryButton(text="省电白名单（后台不被冻结）",
                                    size_hint_y=None, height=dp(44))
        btn_power.bind(on_release=lambda *_: self._open_power_settings())
        box.add_widget(self._safe_text(btn_power, min_height=dp(44)))

        # 自启动 / 后台运行权限（各 ROM 是隐藏页，这里按包名逐个试跳转）
        btn_auto = ABButton(text="自启动 / 后台权限（可选）", size_hint_y=None,
                            height=dp(44))
        btn_auto.bind(on_release=lambda *_: self._open_autostart_settings())
        box.add_widget(self._safe_text(btn_auto, min_height=dp(44)))

        # 主界面播放键已含暂停/继续切换，不再单独放「停止朗读」；
        # 需要彻底停止时可切后台后由系统回收，或用定时休眠。

        # ---- 历史播放书籍 ----
        btn_hist = ABButton(text="历史播放书籍", size_hint_y=None, height=dp(44))
        btn_hist.bind(on_release=lambda *_: self._open_history_popup(popup))
        box.add_widget(self._safe_text(btn_hist, min_height=dp(44)))

        # ---- 备份 / 诊断 ----
        tools_row = BoxLayout(size_hint_y=None, spacing=dp(8))
        btn_export = ABButton(text="导出书签/进度", size_hint_x=1,
                              font_size="12sp")
        btn_export.bind(on_release=lambda *_: self._export_backup())
        btn_export_log = ABButton(text="导出日志", size_hint_x=1,
                                  font_size="12sp")
        btn_export_log.bind(on_release=lambda *_: self._export_diag_log())
        btn_share_log = ABButton(text="分享日志", size_hint_x=1,
                                 font_size="12sp")
        btn_share_log.bind(on_release=lambda *_: self._share_diag_log())
        tools_row.add_widget(self._safe_text(btn_export, min_height=dp(46)))
        tools_row.add_widget(self._safe_text(btn_export_log, min_height=dp(46)))
        tools_row.add_widget(self._safe_text(btn_share_log, min_height=dp(46)))
        box.add_widget(self._auto_row(tools_row, dp(46)))

        btn_close = ABPrimaryButton(text="关闭", size_hint_y=None,
                                    height=dp(46))
        btn_close.bind(on_release=lambda *_: popup.dismiss())
        box.add_widget(self._safe_text(btn_close, min_height=dp(46)))
        _sv = ScrollView()
        _sv.add_widget(box)
        popup.content = _sv
        popup.open()

    def _slider_row(self, title, lo, hi, value, callback, fmt=None):
        """一行「标题 + 滑块 + 当前值」。"""
        row = BoxLayout(size_hint_y=None, spacing=dp(8))
        row.add_widget(self._auto_label(title, size_hint_x=None, width=dp(64),
                                        font_size="13sp", min_height=dp(24)))
        slider = Slider(min=lo, max=hi, value=value)
        shown = fmt(value) if fmt else str(value)
        value_lbl = self._auto_label(shown, size_hint_x=None, width=dp(56),
                                     font_size="12sp", min_height=dp(24),
                                     halign="center")
        slider.bind(value=lambda _s, v: (
            setattr(value_lbl, "text", fmt(v) if fmt else str(int(v))),
            callback(v)))
        row.add_widget(slider)
        row.add_widget(value_lbl)
        # 行高跟着标题/数值的实际高度走（写死 dp(40) 时换行出的第二行会被切掉）
        return self._auto_row(row, dp(46))

    def _on_pitch(self, value):
        self._engine.set_pitch(int(value))
        self._config.set("pitch", int(value))

    def _apply_theme_colors(self, *_):
        """把当前主题的颜色刷进 theme_* 属性 —— KV 绑定会传播到全部控件。"""
        t = _THEMES[self.theme]
        self.theme_bg = t["bg"]
        self.theme_surface = t["surface"]
        self.theme_border = t["border"]
        self.theme_btn = t["btn"]
        self.theme_primary = t["primary"]
        self.theme_danger = t["danger"]
        self.theme_text = t["text"]
        self.theme_dim = t["dim"]
        try:
            from kivy.core.window import Window
            Window.clearcolor = t["bg"]
        except Exception:
            pass

    def _toggle_theme(self, btn=None):
        """深色 ↔ 浅色切换：改属性即全界面即时生效，配置落盘。"""
        self.theme = "light" if self.theme == "dark" else "dark"
        self._config.set("theme", self.theme)
        self._config.save()
        self._apply_theme_colors()
        if btn is not None:
            btn.text = ("当前：浅色（白底黑字）" if self.theme == "light"
                        else "当前：深色（黑底白字）")

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
        # 试听播放器还开着的话一并释放
        try:
            self._stop_preview_player()
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
