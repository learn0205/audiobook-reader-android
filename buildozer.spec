[app]

# ============================================================================
#  有声书朗读 · 安卓版  —— buildozer 打包配置
#
#  只打 arm64（绝大多数 2018 年后的安卓手机都是 arm64-v8a）。
#  如果你的手机是老款 32 位机型，把 android.archs 改成 armeabi-v7a。
#
#  打包命令（必须在 Linux / macOS 上执行，Windows 需要 WSL）：
#      pip install buildozer cython
#      buildozer -v android debug          # 产出 bin/audiobookreader-1.0-arm64_v8a-debug.apk
#      buildozer -v android release        # 正式包（未签名，需自行签名）
#
#  首次打包会下载 Android SDK / NDK，约 2~4 GB，耗时 20~40 分钟，属正常现象。
# ============================================================================

title = 有声书朗读
# ↑ 如果打包时报编码错误，把上面这行改成纯英文，例如：  title = AudioBookReader

package.name = audiobookreader
package.domain = org.audiobookreader

source.dir = .
source.include_exts = py,png,jpg,jpeg,kv,atlas,ttf

# 不打包这些开发期产物
source.exclude_dirs = bin,.buildozer,__pycache__,.github
source.exclude_patterns = *.pyc,*.pyo,*.spec

version = 1.0

# ---------------------------------------------------------------------------
#  依赖：只要 kivy + pyjnius（调用安卓原生 TTS）+ android（Activity / 权限接口）
#  书籍解析与配置管理是纯标准库实现，不需要额外依赖。
#  故意不锁 kivy 版本：让 python-for-android 挑它自带 recipe 支持的版本，
#  首次构建成功率最高。想锁版本就写成 kivy==2.3.1。
# ---------------------------------------------------------------------------
requirements = python3,kivy,pyjnius,android

orientation = portrait
fullscreen = 0
# 保持屏幕常亮（听书时很实用）
android.keep_screen_on = True

icon.filename = %(source.dir)s/icon.png

# ---------------------------------------------------------------------------
#  架构：只出 arm64
# ---------------------------------------------------------------------------
android.archs = arm64-v8a

# Android 版本：minapi 24 = Android 7.0，api 33 = Android 13
android.api = 33
android.minapi = 24
android.ndk = 25b

# ---------------------------------------------------------------------------
#  ⚠️ 关键：必须用 develop 分支的 python-for-android，否则 arm64 构建必失败
#
#  原因：p4a 在安装纯 Python 模块时，解析阶段传了 --platform=android_..._arm64_v8a，
#  但真正安装时漏传，导致 pip 用宿主平台（linux_x86_64）去校验，直接拒绝安卓轮子：
#      ERROR: charset_normalizer-3.5.1-cp314-cp314-android_24_arm64_v8a.whl
#             is not a supported wheel on this platform.
#  而 charset-normalizer 只发布了 arm64_v8a / x86_64 的安卓轮子、没有 v7a，
#  所以「32 位能过、64 位挂」—— 这正是我们要的 arm64 会踩的坑。
#
#  实测：master 分支和 v2026.05.09 等全部 tag 都还是有 bug 的旧代码，
#       只有 develop 分支在安装时补上了 --platform。见 kivy/buildozer#2051
# ---------------------------------------------------------------------------
p4a.branch = develop

# 不需要任何存储权限：
# 导入书籍走系统的 SAF 文件选择器（ACTION_OPEN_DOCUMENT），
# 选中的文件会被复制进应用私有目录，因此不必申请 READ_EXTERNAL_STORAGE。
android.permissions =

# CI 上必须自动接受 SDK 许可协议
android.accept_sdk_license = True

android.allow_backup = True
android.wakelock = True

# 日志：出问题时用 adb logcat 看输出
android.logcat_filters = *:S python:D

[buildozer]
log_level = 2
warn_on_root = 1
