# -*- coding: utf-8 -*-
"""前台服务（p4a 的 services，声明为 :foreground）。

目的：让系统把本 App 视为「正在前台工作」的应用，从而在**熄屏 / 切到后台**时
不冻结、不回收它，保证朗读能一段接一段地继续。

⚠️ 重要：p4a 的服务跑在**独立进程**（`android:process=":Playback"`），
本进程**不负责朗读**（朗读在主进程里跑）。它只做三件事：
  1. 作为一个前台服务存在 —— 系统对「有前台服务的 App」整体更宽容，
     不会轻易把它整个冻结/杀掉；
  2. 自己持有一个 PARTIAL_WAKE_LOCK，保证 CPU 不睡；
  3. 提供常驻通知（Android 规定前台服务必须有通知；由 p4a 的 :foreground 自动创建）。

所以这里只需要「活着」即可，不做别的。
"""

import time


def _hold_wakelock():
    """在前台服务进程里再持有一个 PARTIAL_WAKE_LOCK（拿不到就算了）。"""
    try:
        from jnius import autoclass
        Context = autoclass("android.content.Context")
        PowerManager = autoclass("android.os.PowerManager")
        PythonService = autoclass("org.kivy.android.PythonService")
        service = PythonService.mService
        pm = service.getSystemService(Context.POWER_SERVICE)
        lock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK,
                              "AudioBookReader::service")
        lock.acquire()
        return lock
    except Exception:
        return None


_LOCK = _hold_wakelock()

# 前台服务进程不能退出：一直活着，直到 App 主动 stop 掉这个服务。
while True:
    time.sleep(5)
