# -*- coding: utf-8 -*-
"""app_wakelock_fg.py —— 唤醒锁 / 前台服务 / 省电白名单 & 自启动跳转（从 main.py 拆出）。

被 AudioBookApp 继承。所有方法在 Kivy / 音频主线程上调用；jnius 在桌面环境
不存在，与 main.py 一样做 try/except，_JNIUS_OK = False 时所有安卓调用
都直接 return / toast。
"""
import sys  # noqa: F401  保留以备后续需要；当前未直接使用
import threading  # noqa: F401  同上

try:
    from jnius import autoclass, PythonJavaClass, java_method
    _JNIUS_OK = True
except Exception:
    autoclass = None
    _JNIUS_OK = False


if _JNIUS_OK:
    class _MediaActionListener(PythonJavaClass):
        """实现 MediaActionListener 接口：通知栏/耳机按键动作 → Python。"""
        __javainterfaces__ = ["org/audiobookreader/android/MediaActionListener"]
        __javacontext__ = "app"

        def __init__(self, owner):
            super().__init__()
            self.owner = owner

        @java_method("(Ljava/lang/String;)V")
        def onAction(self, action):
            self.owner._on_media_action(action)
else:
    class _MediaActionListener:     # pragma: no cover
        pass


class WakelockFgMixin:
    """播放时持有 PARTIAL_WAKE_LOCK；启动/停止 p4a 前台服务；
    跳转系统「电池优化」和「自启动」管理页（省电白名单）。
    """

    def _acquire_wake(self):
        if not _JNIUS_OK or self._wake_lock is not None:
            return
        try:
            Context = autoclass("android.content.Context")
            PowerManager = autoclass("android.os.PowerManager")
            activity = autoclass("org.kivy.android.PythonActivity").mActivity
            pm = activity.getSystemService(Context.POWER_SERVICE)
            self._wake_lock = pm.newWakeLock(
                PowerManager.PARTIAL_WAKE_LOCK, self._wake_lock_tag)
            self._wake_lock.acquire()
            self._wake_error = ""
        except Exception as e:
            self._wake_lock = None
            self._wake_error = str(e)

    def _release_wake(self):
        if self._wake_lock is not None:
            try:
                if self._wake_lock.isHeld():
                    self._wake_lock.release()
            except Exception:
                pass
            self._wake_lock = None

    def _start_fg_service(self):
        if not _JNIUS_OK or self._fg_started:
            return
        try:
            mActivity = autoclass("org.kivy.android.PythonActivity").mActivity
            Svc = autoclass("org.audiobookreader.audiobookreader.ServicePlayback")
            Svc.start(mActivity, "")
            self._fg_started = True
            self._fg_error = ""
        except Exception as e:
            self._fg_error = str(e)

    def _stop_fg_service(self):
        if not _JNIUS_OK or not self._fg_started:
            return
        try:
            mActivity = autoclass("org.kivy.android.PythonActivity").mActivity
            Svc = autoclass("org.audiobookreader.audiobookreader.ServicePlayback")
            Svc.stop(mActivity)
        except Exception as e:
            self._fg_error = str(e)
        self._fg_started = False

    # 各 ROM 的「自启动管理」是隐藏页面，没有公开 Action，只能按包名/类名逐个试。
    # 本机是 vivo（OriginOS），vivo 的几个入口排在最前。
    _AUTOSTART_ENTRIES = [
        ("com.vivo.permissionmanager",
         "com.vivo.permissionmanager.activity.BgStartUpManagerActivity"),
        ("com.iqoo.secure", "com.iqoo.secure.ui.phoneoptimize.BgStartUpManager"),
        ("com.iqoo.secure", "com.iqoo.secure.ui.phoneoptimize.AddWhiteListActivity"),
        ("com.vivo.abe", "com.vivo.abe.ui.appdefaults.WhiteListActivity"),
        ("com.coloros.safecenter",
         "com.coloros.safecenter.permission.startup.StartupAppListActivity"),
        ("com.miui.securitycenter",
         "com.miui.permcenter.autostart.AutoStartManagementActivity"),
        ("com.huawei.systemmanager",
         "com.huawei.systemmanager.startupmgr.ui.StartupNormalAppListActivity"),
    ]

    def _open_power_settings(self):
        """打开系统「电池优化 / 后台管理」页，方便把本应用加入省电白名单。

        为什么关键：安卓的「缓存应用冻结」按 **UID** 判定白名单
        （`shouldNotFreeze = uidRec.isCurAllowListed()`）。加白名单后，
        本应用的**所有进程**都不会被冻结——这是第三方应用能拿到的、
        最可靠的「后台不被冻」手段（前台服务只能保住它自己那个进程）。
        """
        if not _JNIUS_OK:
            self._toast("桌面环境无此设置")
            return
        try:
            Intent = autoclass("android.content.Intent")
            Settings = autoclass("android.provider.Settings")
            Uri = autoclass("android.net.Uri")
            activity = autoclass("org.kivy.android.PythonActivity").mActivity
            pkg = str(activity.getPackageName())
            # ① 直达本应用的「电池优化」授权页（部分 ROM 需要权限，失败就往下退）
            try:
                it = Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS)
                it.setData(Uri.parse("package:" + pkg))
                activity.startActivity(it)
                self._toast("请在列表里把本应用设为「不优化 / 允许」")
                return
            except Exception:
                pass
            # ② 电池优化总列表
            try:
                activity.startActivity(Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS))
                self._toast("请把本应用设为「不优化」")
                return
            except Exception:
                pass
            # ③ 兜底：本应用详情页
            it = Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS)
            it.setData(Uri.parse("package:" + pkg))
            activity.startActivity(it)
            self._toast("请在「电池 / 耗电管理」里允许后台运行")
        except Exception as e:
            self._toast("无法打开系统设置：%s" % e)

    def _open_autostart_settings(self):
        """打开「自启动 / 后台运行」管理页（隐藏入口逐个尝试，全失败退到应用详情页）。"""
        if not _JNIUS_OK:
            self._toast("桌面环境无此设置")
            return
        try:
            Intent = autoclass("android.content.Intent")
            ComponentName = autoclass("android.content.ComponentName")
            Settings = autoclass("android.provider.Settings")
            Uri = autoclass("android.net.Uri")
            activity = autoclass("org.kivy.android.PythonActivity").mActivity
            pkg = str(activity.getPackageName())

            for comp_pkg, comp_cls in self._AUTOSTART_ENTRIES:
                try:
                    it = Intent()
                    it.setComponent(ComponentName(comp_pkg, comp_cls))
                    activity.startActivity(it)
                    self._toast("请在列表里允许本应用「自启动 / 后台运行」")
                    return
                except Exception:
                    continue

            # 兜底：应用详情页（从这里一般能找到电池 / 权限 / 自启动入口）
            it = Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS)
            it.setData(Uri.parse("package:" + pkg))
            activity.startActivity(it)
            self._toast("请在「电池 / 权限」里允许后台运行；或到设置顶部搜索“自启动”")
        except Exception as e:
            self._toast("无法打开系统设置：%s" % e)

    # ==================== 通知栏媒体控制 / MediaSession ====================
    def _media_setup(self):
        """创建 MediaSession + 通知栏控制（Java 桥未编译/失败时静默降级）。"""
        if not _JNIUS_OK or getattr(self, "_media_ok", False):
            return
        try:
            MediaControls = autoclass("org.audiobookreader.android.MediaControls")
            PythonActivity = autoclass("org.kivy.android.PythonActivity")
            self._media_cb = _MediaActionListener(self)
            MediaControls.setup(PythonActivity.mActivity, self._media_cb)
            self._media_ok = True
            self._media_error = ""
        except Exception as e:
            self._media_ok = False
            self._media_error = str(e)[:60]

    def _media_update(self, playing):
        """刷新通知（播放/暂停态 + 当前章节标题）。"""
        if not getattr(self, "_media_ok", False) or not _JNIUS_OK:
            return
        try:
            MediaControls = autoclass("org.audiobookreader.android.MediaControls")
            PythonActivity = autoclass("org.kivy.android.PythonActivity")
            MediaControls.update(PythonActivity.mActivity, bool(playing),
                                 str(getattr(self, "book_title", "")))
        except Exception:
            pass

    def _media_teardown(self):
        """彻底停止朗读时撤销通知和会话。"""
        if not getattr(self, "_media_ok", False) or not _JNIUS_OK:
            return
        try:
            MediaControls = autoclass("org.audiobookreader.android.MediaControls")
            PythonActivity = autoclass("org.kivy.android.PythonActivity")
            MediaControls.teardown(PythonActivity.mActivity)
        except Exception:
            pass
        self._media_ok = False

    def _on_media_action(self, action):
        """通知栏按钮 / 耳机按键 → Kivy 主线程执行（回调来自系统线程）。"""
        try:
            from kivy.clock import Clock
        except Exception:
            return
        if action == "playpause":
            Clock.schedule_once(lambda *_: self.toggle_play(), 0)
        elif action == "next":
            Clock.schedule_once(lambda *_: self.jump_chapter(1), 0)
        elif action == "prev":
            Clock.schedule_once(lambda *_: self.jump_chapter(-1), 0)

    @staticmethod
    def _nav_bar_dp():
        """安卓系统导航栏高度（dp）：手势条 / 三键导航都会压住应用底部。

        拿不到或没导航栏时返回 0，不影响布局。
        """
        if not _JNIUS_OK:
            return 0
        try:
            activity = autoclass("org.kivy.android.PythonActivity").mActivity
            res = activity.getResources()
            rid = res.getIdentifier("navigation_bar_height", "dimen", "android")
            if rid > 0:
                h = res.getDimensionPixelSize(rid)
                density = res.getDisplayMetrics().density
                if density:
                    nav = int(round(h / density))
                    # 防御：万一 density 取错、换算异常，nav 会变得很大，
                    # 控制条会被撑得极高、不透明底色盖住大片正文。
                    # 只在合理区间（0~60dp）内采用，否则当作 0。
                    if 0 < nav <= 60:
                        return nav
        except Exception:
            pass
        return 0
