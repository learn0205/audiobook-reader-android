# -*- coding: utf-8 -*-
"""app_diag.py —— 诊断 / 崩溃留痕（从 main.py 拆出）。

职责
----
1. `diag(msg)`：把关键事件追加写到 `<user_data_dir>/diag.log`（Android 上 print 不
   一定进 logcat，文件最可靠）。正式发布前去掉调用即可。
2. 崩溃留痕：把闪退时的 traceback 存到 `diag.log.crash`，并保留最后一段；
   自检信息 → 「崩溃」行会显示最后一行，便于用户截图定位。
   接住三类异常：
   - 主线程未捕获（`sys.excepthook`）
   - 子线程未捕获（`threading.excepthook`）
   - Kivy 事件回调（`ExceptionManager` handler）—— Kivy 默认会吞掉
依赖：需要外部模块提供 `sys`, `traceback`, `threading`（Python 标准库）。
"""
import os
import sys
import threading
import traceback


# 调试：把关键触摸/滚动事件写进文件（Android 上 print 不一定进 logcat，
# 文件最可靠；正式发布前去掉 diag() 调用即可）。
_DIAG = {"path": None}


def diag(msg):
    p = _DIAG["path"]
    if not p:
        return
    try:
        with open(p, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:
        pass


# 崩溃留痕：把闪退时的 traceback 存下来，并显示在「设置 → 自检信息」里。
# 安卓上用户拿不到 logcat；而且 Kivy 的 ExceptionManager 会把事件回调里
# 抛出的异常吞掉（只打日志），所以「暂停闪退」这类问题在 App 里完全看不到。
# 这里接住三类异常：主线程未捕获、子线程未捕获、Kivy 事件回调里的。
_CRASH = {"text": ""}


def _record_crash(text):
    _CRASH["text"] = (text or "").strip()[-700:]
    p = _DIAG.get("path")
    if p:
        try:
            with open(p + ".crash", "a", encoding="utf-8") as f:
                f.write((text or "") + "\n" + "-" * 60 + "\n")
        except Exception:
            pass


def _install_crash_handlers():
    def _hook(exc_type, exc, tb):
        try:
            _record_crash("".join(traceback.format_exception(exc_type, exc, tb)))
        except Exception:
            pass
        try:
            sys.__excepthook__(exc_type, exc, tb)
        except Exception:
            pass

    sys.excepthook = _hook
    try:
        threading.excepthook = lambda a: _hook(a.exc_type, a.exc_value,
                                               a.exc_traceback)
    except Exception:
        pass
    # Kivy 会吞掉事件回调（按钮 on_release 等）里的异常 —— 挂 handler 留痕
    try:
        from kivy.base import ExceptionHandler, ExceptionManager

        class _CrashHandler(ExceptionHandler):
            def handle_exception(self, inst):
                _record_crash(traceback.format_exc())
                return ExceptionManager.RAISE

        ExceptionManager.add_handler(_CrashHandler())
    except Exception:
        pass
