# -*- coding: utf-8 -*-
"""桌面回归测试入口：全部通过退出码 0，任一失败退出码 1。

包含：
  1) 布局：正文区严格贴合顶栏下沿，保镖 fix 计数正常（_smoke 的检查）
  2) _v.py 的期望高度断言
  3) Edge 后端：逐句推进 + 播放期间预取 + 全句缓存（假合成，无网络）
  4) ConfigManager 备份导出/导入
  5) 设置弹窗：文字不被裁切 + 深浅主题对比度达标（见 test_settings_popup）
  6) 「卡死自检」信号源：位置前进才算在出声 / 句中停住不切句 / 坏缓存能重开
     （见 test_edge_progress_signal、test_edge_advance_not_cut）
  7) 外部播放/暂停命令按语义幂等执行，不再反转状态（见 test_media_cmd_semantics）
"""
import os
import sys
import tempfile
import time

os.environ["KIVY_NO_ARGS"] = "1"
os.environ["KIVY_NO_CONSOLELOG"] = "1"
os.environ["KIVY_NO_FILELOG"] = "1"
os.environ["KIVY_METRICS_DENSITY"] = "1"

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append(ok)
    print("[{}] {} {}".format("PASS" if ok else "FAIL", name, detail))


_TEST_APP = None
_TEST_ROOT = None


def shared_app():
    """整个测试进程只建一个 AudioBookApp。

    ⚠️ 不能建第二个：KV 类规则里的 `app.*` 引用的 App 实例是按规则缓存的，
       第二个 App 建出来的控件仍然绑在第一个 App 的属性上 —— 换色检查会全部
       假阳性（真机上永远只有一个 App，所以这只是测试环境的坑）。
    """
    global _TEST_APP, _TEST_ROOT
    if _TEST_APP is None:
        from kivy.config import Config
        Config.set("graphics", "width", "400")
        Config.set("graphics", "height", "800")
        import main
        _TEST_APP = main.AudioBookApp()
        # ⚠️ 只调 build() 不会给 app.root 赋值（那是 run() 干的），自己存起来
        _TEST_ROOT = _TEST_APP.build()
    return _TEST_APP


def shared_root():
    shared_app()
    return _TEST_ROOT


def test_layout():
    from kivy.clock import Clock
    app = shared_app()
    root = shared_root()
    root.size = (400, 800)
    for _ in range(10):
        Clock.tick()
        root.do_layout()
    b, t = app._body, app._top_w
    check("layout: body 贴合顶栏下沿",
          abs((b.y + b.height) - t.y) < 1 and b.height > 100,
          "gap=%.1f" % ((b.y + b.height) - t.y))
    check("layout: scroll 被摆进 body", abs(app._scroll.y - b.y) < 1,
          "svY=%.0f" % app._scroll.y)
    # 保镖自愈：把 body 拉回 0 再 tick 一帧必须纠正
    app._body.pos = (0, 0)
    Clock.tick()
    check("layout: 保镖自愈复位错位",
          abs((b.y + b.height) - t.y) < 1,
          "fixes=%d" % app._layout_fixes)
    app._stop_tick_loop()


def test_edge_flow():
    import vits_tts
    import tts_engine
    from tts_android import STATE_STOPPED

    calls = []

    def fake_synth(text, voice, path, rate="+0%", pitch="+0Hz", volume="+0%"):
        calls.append(text)
        with open(path, "wb") as f:
            f.write(b"ID3fake")
        time.sleep(0.02)
        return 7

    vits_tts.synthesize_to_file = fake_synth

    # ⚠️ 以前这里另起一个线程循环 Clock.tick()，好让「合成完 → 起播」这条
    #    Clock 投递被处理。但 Clock **不是线程安全的**：子线程 tick() 与合成
    #    线程的 schedule_once 会互相踩，CI 慢机器上回调整条丢失 → 预取永远不触发
    #    （`early=1` 偶发失败）。现在桌面端 _post_to_main 是同步直调，不需要
    #    后台泵时钟；这里只在主线程自己 tick，保持确定性。
    from kivy.clock import Clock

    e = tts_engine.LocalTTS()
    e._cache_dir = tempfile.mkdtemp()
    e.set_voice("zh-CN-XiaoxiaoNeural")
    e.load(["甲句。乙句！丙句？丁句；戊句…己句没有标点结尾",
            "第二段开头，逗号不断句。第二段结束！"])
    total = len(e._sentences)
    e.play(0)
    # 等待预取生效：CI 机器慢，固定 sleep 会偶发超时误报 —— 轮询最多等 10 秒
    deadline = time.time() + 10
    while time.time() < deadline and len(calls) < 2:
        Clock.tick()
        time.sleep(0.05)
    early = len(calls)
    deadline = time.time() + 90
    while time.time() < deadline:
        Clock.tick()
        e.poll_advance()
        time.sleep(0.02)
        if e.get_state() == STATE_STOPPED and e._index >= total:
            break
    cached = sum(1 for _, s in e._sentences
                 if os.path.exists(e._cache_path(s)))
    check("edge: 播放期间预取生效", early >= 2, "early=%d" % early)
    check("edge: 全部句子缓存并播完", cached == total and e._index >= total,
          "cached=%d/%d" % (cached, total))
    e._stop_tick_loop() if hasattr(e, "_stop_tick_loop") else None


def test_edge_prefetch_parallel():
    """预取逻辑回归：从当前句 n 触发预取，应并行合成后面 1/2/3 句（共 3 句），
    且**不**包含当前句本身；每句独立线程并行（不串行等上一句）。

    这是「Edge 在线语音偶尔较长间隔」修复的核心：预取提前到当前句一开始合成就
    触发、深度 3、并行，使推进时后续句基本命中本地缓存、无现场合成停顿。"""
    import vits_tts
    import tts_engine
    from tts_engine import LocalTTS

    calls = []

    def fake_synth(text, voice, path, rate="+0%", pitch="+0Hz", volume="+0%"):
        calls.append(text)
        with open(path, "wb") as f:
            f.write(b"FAKEMP3")
        time.sleep(0.02)
        return 7

    vits_tts.synthesize_to_file = fake_synth

    e = LocalTTS()
    e._cache_dir = tempfile.mkdtemp()        # 等同 _set_cache_dir 的底层目录
    e.set_voice("zh-CN-YunxiNeural")
    # 6 句，当前句 index=0
    e.load(["当前句第零。", "后续一。", "后续二。", "后续三。",
            "后续四。", "后续五。"])
    e._generation = 1
    e._prefetch_ahead(0, 1)                  # 模拟 _speak_current 触发的预取

    # 并行预取，轮询等缓存落盘（最多 5 秒）
    deadline = time.time() + 5
    while time.time() < deadline:
        if len(calls) >= 3:
            break
        time.sleep(0.03)
    cached = sum(1 for _, s in e._sentences if os.path.exists(e._cache_path(s)))
    check("edge: 预取并行写出后续3句(不含当前句)",
          cached == 3 and "当前句第零。" not in calls,
          "cached=%d calls=%s" % (cached, calls))
    check("edge: 预取覆盖 index+1/+2/+3",
          ("后续一。" in calls) and ("后续二。" in calls) and ("后续三。" in calls),
          "calls=%s" % calls)


def test_config_backup():
    from config_manager import ConfigManager
    p = os.path.join(tempfile.mkdtemp(), "config.json")
    cm = ConfigManager(p)
    cm.set("last_book", "/tmp/x.txt")
    cm.set_position("k1", 7, 42)
    cm.add_bookmark("k1", 7, "书签甲") if hasattr(cm, "add_bookmark") else None
    text = cm.export_data()
    cm2 = ConfigManager(os.path.join(tempfile.mkdtemp(), "c2.json"))
    cm2.import_data(text)
    ok = cm2.get("last_book") == "/tmp/x.txt" and cm2.get_position("k1")[0] == 7
    check("config: 备份导出/导入", ok, "")

def test_history():
    import time as _t
    from config_manager import ConfigManager
    cm = ConfigManager(os.path.join(tempfile.mkdtemp(), "c.json"))
    cm.touch_history("k1", "/p/a.txt", "甲书"); cm.set_position("k1", 3, 10)
    cm.touch_history("k2", "/p/b.txt", "乙书"); cm.set_position("k2", 1, 0)
    order = [it[2] for it in cm.get_history_items()]
    check("history: 最近打开的排前面", order == ["乙书", "甲书"], str(order))
    cm.remove_book_data("k1")
    left = [it[2] for it in cm.get_history_items()]
    check("history: 单本删除", left == ["乙书"], str(left))
    cm.clear_book_data(keep_key="k2")
    hist = cm.get_history_items()
    pos_ok = cm.get_position("k2") == (1, 0)
    hist_core = [(k, path, title) for k, path, title, _ in hist]
    check("history: 清空后当前书保留(书名/断点)",
          hist_core == [("k2", "/p/b.txt", "乙书")] and pos_ok,
          "hist=%s" % hist)


def _luminance(c):
    def f(v):
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    return 0.2126 * f(c[0]) + 0.7152 * f(c[1]) + 0.0722 * f(c[2])


def _contrast(fg, bg):
    a, b = _luminance(fg), _luminance(bg)
    return (max(a, b) + 0.05) / (min(a, b) + 0.05)


def _walk(w):
    yield w
    for c in getattr(w, "children", []):
        yield from _walk(c)


def _open_settings(app):
    from kivy.core.window import Window
    from kivy.uix.popup import Popup
    app.show_settings()
    from kivy.clock import Clock
    for _ in range(6):
        Clock.tick()
    for w in Window.children:
        if isinstance(w, Popup) and w.title == "设置":
            for kid in _walk(w):
                # 非布局控件（Label/Button/Slider…）没有 do_layout，别硬调
                getattr(kid, "do_layout", lambda: None)()
            return w
    return None


def test_settings_popup():
    """设置弹窗两条硬约束：

    1) 文字不许被裁切 —— 每个有文字的控件都必须自己
       「text_size 按宽度重排 + 高度跟着纹理长」，且不能超出内容区边界；
       之前「定时休眠（分」被吃掉就是写死 height=dp(24) + 无 text_size 导致的。
    2) 主题对比度 —— 两套主题下，文字相对它所在的底色都要 ≥ 4.5:1（WCAG AA），
       否则浅色模式下会出现「浅底浅字」。
    """
    app = shared_app()
    root = shared_root()
    root.size = (400, 800)
    from kivy.core.window import Window
    from kivy.uix.label import Label
    import main

    try:
        for width in (400, 360, 320):
            Window.size = (width, int(width * 2.0))
            Window.dispatch("on_resize", *Window.size)
            for theme in ("dark", "light"):
                if app.theme != theme:
                    app._toggle_theme()
                popup = _open_settings(app)
                if popup is None:
                    check("popup@%d/%s: 能打开设置弹窗" % (width, theme), False)
                    continue
                sv = popup.content
                box = sv.children[0] if sv.children else None
                off = (sv.x - box.x) if box is not None else 0.0
                inside = set(id(w) for w in _walk(box)) if box is not None else set()

                bad = []
                for w in _walk(popup):
                    if not hasattr(w, "text") or not (w.text or "").strip():
                        continue
                    tex = w.texture_size
                    ts = w.text_size
                    if ts[0] is None:
                        bad.append("无 text_size: %r" % w.text[:12])
                    elif tex[0] > ts[0] + 1:
                        bad.append("横向被切 %r %.0f>%.0f" % (w.text[:12], tex[0], ts[0]))
                    if tex[1] > w.height + 1:
                        bad.append("纵向被切 %r h=%.0f 文字=%.0f"
                                   % (w.text[:12], w.height, tex[1]))
                    ax = w.x + (off if id(w) in inside else 0.0)
                    if ax + w.width > sv.x + sv.width + 1 or ax < sv.x - 1:
                        bad.append("超出边界 %r" % w.text[:12])

                ratios = [
                    ("主文字/弹窗底", _contrast(app.theme_text, app.theme_surface)),
                    ("次要文字/弹窗底", _contrast(app.theme_dim, app.theme_surface)),
                    ("按钮字/按钮底", _contrast(app.theme_text, app.theme_btn)),
                    ("主按钮字/主色底", _contrast((1, 1, 1), app.theme_primary)),
                ]
                low = [n for n, v in ratios if v < 4.5]
                check("popup@%d/%s: 文字未被裁切" % (width, theme), not bad,
                      "; ".join(bad[:3]))
                check("popup@%d/%s: 对比度≥4.5" % (width, theme), not low,
                      " ".join("%s=%.1f" % kv for kv in ratios))

                # 开着弹窗切主题：所有文字必须跟着换色（KV 绑定有效）
                before = app.theme_text
                app._toggle_theme()
                stale = []
                for w in _walk(popup):
                    if isinstance(w, Label) and hasattr(w, "color"):
                        if tuple(w.color) == tuple(before):
                            stale.append(w.text[:8])
                check("popup@%d/%s: 切主题即时换色" % (width, theme), not stale,
                      "未刷新: %s" % ",".join(stale[:3]))
                popup.dismiss()
    finally:
        app._stop_tick_loop()


def test_jump_default_paused():
    """上一章/下一章、书签跳转 —— 默认只定位，不自动开始朗读。

    · 没在朗读 → 跳过去后保持暂停（state 仍是 stopped / paused），
      由用户点底部 ▶ 决定何时开始念；
    · 正在朗读 → 也不去重调 play()，靠 seek_paragraph 从新位置接着念，
      不打断正在听的这一段。
    """
    import tempfile
    from tts_android import STATE_PLAYING, STATE_STOPPED
    from kivy.clock import Clock
    app = shared_app()
    root = shared_root()
    root.size = (400, 800)

    path = os.path.join(tempfile.mkdtemp(), "t.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write("第一章 开头\n甲句。乙句。\n丙句。丁句。\n"
                "第二章 后续\n戊句。己句。\n")
    app.open_book(path)
    for _ in range(4):
        Clock.tick()
    check("jump: 章节识别正常", len(app._chapters) == 2,
          "chapters=%s" % (app._chapters,))

    # ---- 静止状态跳章：位置过去，但不自动念 ----
    app.jump_chapter(1)
    Clock.tick()
    pos = app._engine.get_position()[0]
    check("jump: 下一章只定位不朗读",
          pos == app._chapters[1][1] and app._engine.get_state() != STATE_PLAYING,
          "pos=%d state=%s" % (pos, app._engine.get_state()))

    # ---- 书签跳转同样只定位 ----
    class _FakePopup(object):
        def dismiss(self):
            pass
    app._goto_bookmark(_FakePopup(), 1)
    Clock.tick()
    pos = app._engine.get_position()[0]
    check("jump: 书签跳转只定位不朗读",
          pos == 1 and app._engine.get_state() != STATE_PLAYING,
          "pos=%d state=%s" % (pos, app._engine.get_state()))

    # ---- 目录选章同样只定位 ----
    app.goto_chapter_index(1)
    Clock.tick()
    pos = app._engine.get_position()[0]
    check("jump: 目录选章只定位不朗读",
          pos == app._chapters[1][1] and app._engine.get_state() != STATE_PLAYING,
          "pos=%d state=%s" % (pos, app._engine.get_state()))

    # ---- 两种状态下都不应该调 play() ----
    calls = []
    orig_play, orig_state = app._engine.play, app._engine.get_state
    try:
        app._engine.play = lambda *a, **k: calls.append(a)
        for state in (STATE_STOPPED, STATE_PLAYING):
            app._engine.get_state = lambda: state
            app.jump_chapter(-1)
            app.jump_chapter(1)
            app.goto_chapter_index(0)
            app.goto_chapter_index(1)
            app._goto_bookmark(_FakePopup(), 2)
        check("jump: 跳转不再调用 play()", not calls, "calls=%s" % calls)

        # ---- 正在朗读时跳转：也必须先停下（不能跳过去接着念）----
        pauses = []
        orig_pause = app._engine.pause
        try:
            app._engine.get_state = lambda: STATE_PLAYING   # 假装正在念
            app._engine.pause = lambda: pauses.append(1)
            app.goto_chapter_index(1)      # 目录选章
            app.jump_chapter(-1)           # 上一章
            check("jump: 朗读中跳转先暂停、且不调 play",
                  len(pauses) == 2 and not calls,
                  "pause=%d play=%s" % (len(pauses), calls))
        finally:
            app._engine.pause = orig_pause
    finally:
        app._engine.play = orig_play
        app._engine.get_state = orig_state


def test_long_press_play_index():
    """长按菜单「从这里开始朗读」必须读**长按的那一段**。

    ParaView 只渲染当前章节，它给的 index 是**章内**下标；长按菜单里已经
    换算成全书下标了。以前这句写的是 tap_paragraph(index)，等于把全书下标
    又当成章内下标加了一次 _view_start —— 表现就是「跳到后面章节的随机一段」。
    """
    import tempfile
    from kivy.clock import Clock
    from kivy.core.window import Window
    from kivy.uix.popup import Popup
    app = shared_app()
    root = shared_root()
    root.size = (400, 800)

    path = os.path.join(tempfile.mkdtemp(), "t.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write("第一章 开头\n甲句。乙句。\n丙句。丁句。\n"
                "第二章 后续\n戊句。己句。\n庚句。辛句。\n")
    app.open_book(path)
    for _ in range(4):
        Clock.tick()

    # 切到第二章并等正文重建（_view_start 变成第二章的起始段）
    app.goto_chapter_index(1)
    for _ in range(6):
        Clock.tick()
    view_start = app._view_start
    if view_start == 0:
        check("longpress: 视图已切到第二章", False, "view_start=0")
        return

    played = []
    app._engine.play = lambda i, *a, **k: played.append(i)
    # 长按第二章里的第 1 段（章内下标 = 1）
    app.long_press_paragraph(1)
    popup = None
    for w in Window.children:
        if isinstance(w, Popup) and w.title.startswith("第"):
            popup = w
            break
    if popup is None:
        check("longpress: 长按菜单弹出", False)
        return
    # 菜单第一个按钮就是「▶ 从这里开始朗读」
    btns = [w for w in _walk(popup) if getattr(w, "text", "").startswith("▶ 从")]
    if not btns:
        check("longpress: 找到「从这里开始朗读」按钮", False)
        return
    btns[0].dispatch("on_release")
    Clock.tick()
    want = view_start + 1          # 长按的那一段（全书下标）
    check("longpress: 从长按的段落开始（不再多加 _view_start）",
          played == [want], "played=%s want=[%d] view_start=%d"
          % (played, want, view_start))
    popup.dismiss()


def test_toc_follow():
    """目录「选择后跟随」：打开时高亮并定位到当前正在读/播的章节，
    且播放换章时实时跟随。

    1) show_chapters 打开后，data 里只有当前章 `current=True`；
    2) 章节切换后 _toc_mark_current 能把高亮/跟随切到新章；
    3) 弹窗关闭后 _chapter_rv 必须清空，避免后续误跟随。
    """
    from kivy.clock import Clock
    from kivy.core.window import Window
    app = shared_app()
    root = shared_root()
    root.size = (400, 800)
    Window.size = (400, 800)
    Window.dispatch("on_resize", *Window.size)

    # 造一份假章节表，并把「当前章」设成第 6 章
    app._chapters = [("第%d章" % i, i * 5) for i in range(20)]
    app._view_chapter = 6

    try:
        app.show_chapters()
        rv = app._chapter_rv
        check("toc_follow: 打开目录后持有 RecycleView", rv is not None)
        if rv is not None:
            cur_flags = [d.get("current") for d in rv.data]
            check("toc_follow: 仅当前章(6)被高亮",
                  cur_flags.count(True) == 1 and cur_flags[6] is True,
                  "flags=%s" % cur_flags)
            # 模拟播放换章到第 12 章 → 高亮/跟随应切过去
            app._toc_mark_current(12)
            cur_flags = [d.get("current") for d in rv.data]
            check("toc_follow: 换章后跟随到新章(12)",
                  cur_flags.count(True) == 1 and cur_flags[12] is True
                  and cur_flags[6] is False,
                  "flags=%s" % cur_flags)
        if app._chapter_popup is not None:
            app._chapter_popup.dismiss()
        check("toc_follow: 弹窗关闭后清空 _chapter_rv", app._chapter_rv is None)
    finally:
        app._chapters = []
        app._view_chapter = -1
        app._stop_tick_loop()


def test_edge_progress_signal():
    """卡死自检的两个信号源回归（桌面用假播放器，不需要安卓设备）。

    1. `media_position()` 判定「在出声」**不能只看 isPlaying()**：部分 ROM 在
       正常播 mp3 时它恒为 false，必须靠「位置比上次前进了」认定 —— 否则长句
       会被判成卡死；
    2. 毫秒位置**不能**因为 playing=False 就被丢成 -1（丢掉等于退回「整句恒定」
       的 (段,字符) 判定，正是当初误暂停的根因）；
    3. `recover()` 要顺手删掉当前句的坏缓存（坏 mp3 会让「重开本句」永远无声）；
    4. `skip_current()` 必须能把卡住的一句跳过去，而不是让朗读停死。
    """
    import vits_tts
    import tts_engine
    from tts_engine import LocalTTS
    from tts_android import STATE_PLAYING

    def fake_synth(text, voice, path, rate="+0%", pitch="+0Hz", volume="+0%"):
        with open(path, "wb") as f:
            f.write(b"FAKEMP3")
        return 7

    vits_tts.synthesize_to_file = fake_synth

    class _FakePlayer(object):
        """模拟「正在播、但 isPlaying() 恒 false」的 ROM。"""

        def __init__(self):
            self.pos = 0

        def getCurrentPosition(self):
            return self.pos

        def isPlaying(self):
            return False          # ★ 关键：恒 false

        def stop(self):
            pass

        def reset(self):
            pass

        def release(self):
            pass

    e = LocalTTS()
    e._cache_dir = tempfile.mkdtemp()
    e.set_voice("zh-CN-YunxiNeural")
    e.load(["第一句。", "第二句。", "第三句。"])
    e._state = STATE_PLAYING
    e._player = _FakePlayer()
    e._synthesizing = False

    # 1) 位置在前进 → 必须认定「在出声」（isPlaying() 不可信）
    e._player.pos = 100
    e.media_position()
    e._player.pos = 300
    playing, pos = e.media_position()
    check("edge: 位置前进即认定在出声(isPlaying 恒false也不误判)",
          playing and pos == 300, "playing=%s pos=%s" % (playing, pos))
    check("edge: 位置在走 → 无进展计时归零",
          e.progress_age() < 0.05, "age=%.3f" % e.progress_age())

    # 2) 位置冻结 + isPlaying() false → 判定「没出声」，但**位置照样原样返回**
    playing2, pos2 = e.media_position()
    check("edge: 位置冻结时仍原样返回毫秒位置（不丢成 -1）",
          (not playing2) and pos2 == 300, "playing=%s pos=%s" % (playing2, pos2))
    time.sleep(0.25)
    check("edge: 无声音进展计时会往上走（真卡死能检出）",
          e.progress_age() >= 0.2, "age=%.3f" % e.progress_age())

    # 3) 合成中一律算「有进展」（弱网合成十几秒不能被当卡死）
    e._synthesizing = True
    check("edge: 合成等待中不算卡死", e.progress_age() == 0.0,
          "age=%.3f" % e.progress_age())

    # 4) recover() 删掉当前句坏缓存 + 重启链路
    #    注意：recover 会立刻重新合成这一句，所以缓存文件可能**又出现**（这正是
    #    期望行为）—— 要断言的是「那份坏的没了」（不存在，或已被新内容覆盖）。
    e._synthesizing = False
    e._index = 1
    bad = e._cache_path(e._sentences[1][1])
    os.makedirs(os.path.dirname(bad), exist_ok=True)
    with open(bad, "wb") as f:
        f.write(b"BAD")
    gen = e._generation
    e.recover()
    deadline = time.time() + 3
    while time.time() < deadline:
        try:
            if (not os.path.exists(bad)) or open(bad, "rb").read() != b"BAD":
                break
        except OSError:
            break
        time.sleep(0.05)
    try:
        now_content = open(bad, "rb").read() if os.path.exists(bad) else None
    except OSError:
        now_content = None
    check("edge: recover 清掉当前句坏缓存并重开链路",
          (now_content != b"BAD") and e._generation > gen,
          "content=%s gen=%d->%d" % (now_content, gen, e._generation))

    # 5) skip_current() 跳过卡死的一句（继续往下念，不停死）
    e._index = 1
    e._speak_current = lambda: None      # 别真起合成线程（_advance 会调它）
    e.skip_current()
    check("edge: skip_current 跳过当前句继续往下",
          e._index >= 2, "index=%d" % e._index)


def test_edge_advance_not_cut():
    """「这句播完了没有」的判定回归 —— 别在 `isPlaying()` 恒 false 的机器上切句子。

    根因回顾：部分 ROM 在正常播 mp3 时 `isPlaying()` 恒为 false。旧逻辑一旦看到
    false 且「播过」就认为播完 → 立刻推进下一句 → 句子被拦腰切断、与音频错位。
    新逻辑要求「位置走到末尾」才算播完；中途停住属于卡住，交给卡死自检去重开本句。

    另外还要保证「时长/位置不可信」（拿不到 -1）时仍有兜底：静止约 0.6s 后推进，
    绝不会因为读不到位置就永远停死。
    """
    import vits_tts
    import tts_engine
    from tts_engine import LocalTTS
    from tts_android import STATE_PLAYING

    def fake_synth(text, voice, path, rate="+0%", pitch="+0Hz", volume="+0%"):
        with open(path, "wb") as f:
            f.write(b"FAKEMP3")
        return 7

    vits_tts.synthesize_to_file = fake_synth

    class _LyingPlayer(object):
        """isPlaying() 恒 false（模拟问题 ROM），位置/时长可控。"""

        def __init__(self):
            self.pos = 0
            self.dur = 5000

        def getCurrentPosition(self):
            return self.pos

        def getDuration(self):
            return self.dur

        def isPlaying(self):
            return False

        def stop(self):
            pass

        def reset(self):
            pass

        def release(self):
            pass

    orig_jnius = tts_engine._JNIUS_OK
    tts_engine._JNIUS_OK = True          # 让 poll_advance 走「真机分支」
    try:
        e = LocalTTS()
        e._cache_dir = tempfile.mkdtemp()
        e.set_voice("zh-CN-YunxiNeural")
        e.load(["第一句。", "第二句。", "第三句。"])
        e._speak_current = lambda: None   # 不要把推进变成真合成
        e._state = STATE_PLAYING
        e._synthesizing = False
        e._spoken_uid = "e1"
        e._sent_started = time.time()
        e._player = _LyingPlayer()
        e._index = 0

        # ① 位置在前进 → 不许推进（还在出声）
        e._player.pos = 500
        e.poll_advance()
        e._player.pos = 900
        e.poll_advance()
        check("advance: 位置在走时绝不推进(isPlaying 恒false)",
              e._index == 0 and e._seen_playing, "index=%d" % e._index)

        # ② 位置停在句中（0.5s，远未到 5000）→ 仍不许推进（是卡住，不是播完）
        e._player.pos = 900
        for _ in range(8):
            e.poll_advance()
        check("advance: 句中停住不切句（交给卡死自检重开本句）",
              e._index == 0, "index=%d" % e._index)

        # ③ 位置走到末尾 → 位置停住的下一个 tick 判定「播完」并推进
        #    （真机上末尾那一两个 tick 仍是「位置在走」，所以推进晚 0.2~0.4s；
        #      正常情况下 OnCompletionListener 早就推进过了，这里只是兜底）
        e._player.pos = 5000
        e.poll_advance()                 # 位置 900→5000：仍算「在出声」
        e.poll_advance()                 # 位置停在末尾：播完 → 推进
        check("advance: 位置到末尾才推进", e._index == 1, "index=%d" % e._index)

        # ④ 位置/时长都不可信（-1）→ 静止约 0.6s 后兜底推进（不停死）
        e._player.pos = -1
        e._player.dur = -1
        e._index = 1
        e._spoken_uid = "e2"
        e._seen_playing = True
        e._pos_frozen = 0
        e._last_media_pos = 999          # 先让「位置没变」
        for _ in range(4):
            e.poll_advance()
        check("advance: 读不到位置时长时静止约0.6s兜底推进",
              e._index == 2, "index=%d frozen=%d" % (e._index, e._pos_frozen))
    finally:
        tts_engine._JNIUS_OK = orig_jnius


def test_switch_backend_stops_old():
    """切换音色（系统引擎 ↔ Edge）时，旧后端必须被**真正停掉**。

    曾经的 bug：`_switch_backend()` 只换指针就直接起播新后端，旧的系统引擎还
    在念那一句 —— 用户听到**两个声音同时读小说**。

    另外还要保证停旧后端时**不广播 STOPPED**：那会让 main 把前台服务 / 通知栏
    媒体卡片 / 唤醒锁整条拆掉，紧接着新后端起播又重建一遍（通知闪一下、
    锁屏媒体卡片被清）。切换对外应该是一次原子的转移。
    """
    import vits_tts
    from tts_engine import LocalTTS, ReaderTTS
    from tts_android import STATE_PLAYING, STATE_STOPPED

    def fake_synth(text, voice, path, rate="+0%", pitch="+0Hz", volume="+0%"):
        with open(path, "wb") as f:
            f.write(b"FAKEMP3")
        return 7

    vits_tts.synthesize_to_file = fake_synth

    class _StubBackend(object):
        """假的系统引擎：只记录被调用了什么。"""

        def __init__(self, state=STATE_PLAYING):
            self.state = state
            self.stopped = 0
            self.played = []
            self.on_state = None
            self._para = 1
            # _ensure_edge 会从 android 后端拷这些参数
            self._speed = 1.0
            self._pitch = 0
            self._intonation = True

        def get_state(self):
            return self.state

        def get_position(self):
            return (self._para, 0, 100)

        def load(self, ps):
            pass

        def set_voice(self, n):
            pass

        def seek_paragraph(self, p):
            self._para = p

        def play(self, p=None):
            self.played.append(p)
            self.state = STATE_PLAYING

        def stop(self):
            self.stopped += 1
            self.state = STATE_STOPPED

    events = []
    r = ReaderTTS(on_state=events.append, user_data_dir=tempfile.mkdtemp())
    stub = _StubBackend(STATE_PLAYING)
    r._android = stub
    r._paragraphs = ["第一段甲句。", "第二段乙句。"]
    r._merged_voices = [{"name": "sys-voice", "source": "android"},
                        {"name": "vits:5", "source": "local"}]

    # 系统引擎正在念第 1 段 → 切到本地 VITS 音色
    r.set_voice("vits:5")

    check("switch: 旧后端被真正停掉（不会再两个声音同时念）",
          stub.stopped == 1, "stopped=%d" % stub.stopped)
    check("switch: 停旧后端时不广播 STOPPED（前台服务/通知不闪）",
          STATE_STOPPED not in events, "events=%s" % events)
    check("switch: 新后端接上并继续朗读",
          r._active == "local" and STATE_PLAYING in events,
          "active=%s events=%s" % (r._active, events))
    check("switch: 进度搬到新后端同一段",
          r._local is not None and r._local.get_position()[0] == 1,
          "para=%s" % (r._local.get_position()[0] if r._local else None))


def test_freeze_detect_and_keepalive():
    """「熄屏播放一段时间后停止、打开软件又恢复」对应的两件事：

    ① 冻结检测：推进循环正常 0.2 秒一次；被系统冻结时 wall clock 照走，解冻后
       第一次 tick 的间隔会异常大 —— 据此确认「是系统冻结，不是我们的逻辑卡住」，
       并在自检屏留下「冻结N次 最近Xs」作为证据。
    ② 白名单引导：未进「电池不优化」白名单时提示**一次**（带一键去设置），
       已白名单 / 取不到状态时都不打扰用户。
    """
    from kivy.clock import Clock

    app = shared_app()

    # ---- ① 冻结检测 ----
    app._freeze_events = 0
    app._freeze_play_events = 0
    app._last_freeze_gap = 0.0
    app._freeze_during_play = False
    app._freeze_hint_ts = time.time()
    app._is_playing = False
    app._last_tick_wall = time.time() - 0.2
    app._tick()
    check("freeze: 正常 0.2s 间隔不算冻结", app._freeze_events == 0,
          "events=%d" % app._freeze_events)

    # 闲置（没在朗读）时被冻：总数记，但**不能**算进「播放中冻结」——
    # 否则白名单生效了也会看到「冻结N次」而误判
    app._last_tick_wall = time.time() - 90.0
    app._tick()
    check("freeze: 闲置被冻只记总数、不计入播放中",
          app._freeze_events == 1 and app._freeze_play_events == 0,
          "all=%d play=%d" % (app._freeze_events, app._freeze_play_events))

    app._is_playing = True
    app._last_tick_wall = time.time() - 65.0
    app._tick()
    check("freeze: 播放中被冻单独计数（这才是熄屏停读的成因）",
          app._freeze_events == 2 and app._freeze_play_events == 1
          and app._last_freeze_gap >= 60,
          "all=%d play=%d gap=%.1f" % (app._freeze_events,
                                       app._freeze_play_events,
                                       app._last_freeze_gap))
    check("freeze: 记录「播放中被冻结」标志", app._freeze_during_play, "")
    app._is_playing = False

    # ---- ② 白名单引导（只在未白名单时提示一次）----
    # ⚠️ 不能靠 `Clock.tick(0.7)` 去触发那个 0.6s 延迟：启动期还排了
    #    `_restore_last_book`(0.6s)，在无书环境里它会让进程静默退出（测试环境的坑）。
    #    这里把 main 模块里的 Clock 换成立刻执行的替身，确定性驱动。
    import main as _main_mod

    class _NowClock(object):
        @staticmethod
        def schedule_once(f, dt=0, **kw):
            f(dt)

    shown = []
    orig_bat, orig_popup, orig_clock = (app.battery_allowlisted,
                                        app._show_keepalive_popup,
                                        _main_mod.Clock)
    try:
        _main_mod.Clock = _NowClock()
        app._show_keepalive_popup = lambda: shown.append(1)

        app._keepalive_prompted = False
        app._config.set("keepalive_prompted", False)
        app.battery_allowlisted = lambda: False
        app._maybe_prompt_keepalive()
        app._maybe_prompt_keepalive()          # 第二次不该再弹
        check("keepalive: 未白名单时提示一次且只一次",
              len(shown) == 1 and app._config.get("keepalive_prompted", False),
              "shown=%d" % len(shown))

        shown[:] = []
        app._keepalive_prompted = False
        app._config.set("keepalive_prompted", False)
        app.battery_allowlisted = lambda: True
        app._maybe_prompt_keepalive()
        check("keepalive: 已白名单不打扰", not shown, "shown=%s" % shown)

        shown[:] = []
        app._keepalive_prompted = False
        app._config.set("keepalive_prompted", False)
        app.battery_allowlisted = lambda: None   # 取不到（桌面 / ROM 不支持）
        app._maybe_prompt_keepalive()
        check("keepalive: 状态未知时不打扰",
              (not shown) and not app._config.get("keepalive_prompted", False),
              "shown=%s" % shown)
    finally:
        app.battery_allowlisted = orig_bat
        app._show_keepalive_popup = orig_popup
        _main_mod.Clock = orig_clock
        app._keepalive_prompted = True            # 后续测试不再弹


def test_edge_play_retry():
    """起播失败要**自动重试一次**（全新播放器），并把失败步骤写进错误信息。

    真机证据（用户截图）：`Edge 播放失败：JVM exception occured:
    java.lang.IllegalStateException … android.media.MediaPlayer` —— 只给这一条信息
    既分不清是 setDataSource 还是 start 出的问题，也看不出会不会自愈。
    现在：① 错误信息带步骤名；② 失败自动重开一次（被系统冻结打断后残留的异常
    状态往往一次就好）；③ 两次都失败才跳过这一句 —— 绝不让朗读停住。
    """
    import tts_engine
    from tts_engine import LocalTTS
    from tts_android import STATE_PLAYING

    errors = []
    e = LocalTTS(on_error=errors.append)
    e._cache_dir = tempfile.mkdtemp()
    e.set_voice("zh-CN-YunxiNeural")
    e.load(["第一句。", "第二句。"])
    e._state = STATE_PLAYING
    e._generation = 1
    e._speak_current = lambda: None      # 别真起合成线程（_advance 会调它）

    class _BadPlayer(object):
        """第一个实例在 setDataSource 抛异常（模拟冻结残留的 IllegalState）；
        之后新建的都正常 —— 用来验证「重试一次就好」。"""
        made = []

        def __init__(self):
            self.ok = bool(_BadPlayer.made)
            _BadPlayer.made.append(self)

        def setDataSource(self, p):
            if not self.ok:
                raise RuntimeError("java.lang.IllegalStateException")

        def setOnCompletionListener(self, l):
            pass

        def prepare(self):
            pass

        def start(self):
            pass

        def stop(self):
            pass

        def reset(self):
            pass

        def release(self):
            pass

    class _FakeCb(object):
        def __init__(self, *a, **k):
            pass

    orig_ac = tts_engine.autoclass
    orig_jnius = tts_engine._JNIUS_OK
    orig_cb = tts_engine._PlayerCompletion
    tts_engine.autoclass = (lambda name: _BadPlayer
                            if name == "android.media.MediaPlayer"
                            else (lambda *a: a))
    tts_engine._PlayerCompletion = _FakeCb
    tts_engine._JNIUS_OK = True
    try:
        e._play_file("/tmp/x.mp3", 1)
        check("play: 起播失败自动重试一次即成功",
              len(_BadPlayer.made) == 2 and e._player is not None and not errors,
              "made=%d err=%s" % (len(_BadPlayer.made), errors))

        # 两次都失败 → 报错里要带步骤名，并且跳过这一句继续（不停住）
        _BadPlayer.made = []
        errors[:] = []
        e._index = 0

        class _AlwaysBad(_BadPlayer):
            def setDataSource(self, p):
                raise RuntimeError("java.lang.IllegalStateException")

        tts_engine.autoclass = (lambda name: _AlwaysBad
                                if name == "android.media.MediaPlayer"
                                else (lambda *a: a))
        e._state = STATE_PLAYING
        e._index = 0
        before = e._index
        e._play_file("/tmp/y.mp3", e._generation)
        check("play: 连续失败时报错带步骤名并跳过继续",
              errors and "setDataSource" in errors[0] and e._index > before,
              "err=%s index=%d" % (errors[:1], e._index))
    finally:
        tts_engine.autoclass = orig_ac
        tts_engine._PlayerCompletion = orig_cb
        tts_engine._JNIUS_OK = orig_jnius


def test_voice_editor_opens():
    """音色参数编辑器（模板/角色自定义共用）必须能打开。

    回归：曾把 BoxLayout 塞进 _safe_text（它只接受有 text 属性的控件），
    AttributeError → 点开「音色模板→编辑编号」/「角色→自定义音色」必闪退。
    """
    app = shared_app()
    from kivy.core.window import Window
    from kivy.uix.popup import Popup
    from kivy.clock import Clock

    opened = False
    try:
        app._voice_param_editor(
            title="测试编辑器", cur_voice="zh-CN-XiaoxiaoNeural", cur_pitch=0,
            cur_rate=1.0, on_save=lambda *a: None)
        opened = True
    except Exception as e:
        check("voice_editor: 打开不抛异常", False, repr(e))
        return
    for _ in range(6):
        Clock.tick()
    found = None
    for w in Window.children:
        if isinstance(w, Popup) and w.title == "测试编辑器":
            found = w
            break
    check("voice_editor: 打开不抛异常", opened and found is not None)
    if found is not None:
        found.dismiss()
        Clock.tick()


def test_ai_roles_logic():
    """离线 AI 角色分析（纯逻辑部分）：提示词构建 / 输出解析 / 门类映射。

    模型与运行器只在设备上存在，这里只测可离线验证的部分：
    JSON 解析、正则兜底、解析失败的可读报错、模型校验闸门。
    """
    app = shared_app()
    import tempfile
    import ai_roles
    chars = [("张三", "男角色", ["“你们都退下。”"]),
             ("夏弥", "女角色", ["“喂。”"])]
    p = ai_roles.build_prompt(chars)
    check("ai: 提示词含角色与格式要求",
          "张三" in p and "夏弥" in p and "JSON" in p)
    r = ai_roles.parse_output(
        '[{"name":"张三","gender":"男","age":"中年"},'
        '{"name":"夏弥","gender":"女","age":"少年"}]', chars)
    check("ai: JSON 输出解析并映射门类",
          r == {"张三": "中年叔叔", "夏弥": "少女"}, str(r))
    r2 = ai_roles.parse_output("张三是男的，青年。夏弥：女性、少年。", chars)
    check("ai: 跑飞输出的正则兜底", r2 == {"张三": "男角色", "夏弥": "少女"},
          str(r2))
    try:
        ai_roles.parse_output("对不起我不知道", chars)
        check("ai: 解析失败给可读报错", False, "未抛异常")
    except ai_roles.AIError as e:
        check("ai: 解析失败给可读报错", "无法解析" in str(e), str(e))
    d = tempfile.mkdtemp()
    ok, _why = ai_roles.model_ready(d)
    check("ai: 未导入模型给可读状态", not ok)
    os.makedirs(os.path.join(d, "ai"), exist_ok=True)
    fp = ai_roles.model_path(d)
    with open(fp, "wb") as f:
        f.write(b"GGUF" + b"0" * (600 * 1024 * 1024))
    ok, _why = ai_roles.model_ready(d)
    check("ai: 合法 GGUF 通过校验", ok)
    merged = ai_roles.merge_detected(chars, {"张三": "中年叔叔"})
    check("ai: 未覆盖角色保持原猜测",
          merged == [("张三", "中年叔叔"), ("夏弥", "女角色")])


def test_media_cmd_semantics():
    """外部播放/暂停命令必须**按语义幂等执行**，绝不能反转状态。

    曾经的 bug：通知栏/耳机/蓝牙命令全接到 toggle_play()，而 Java 侧把 onPlay()
    与 onPause() 都映射成同一个 "playpause" —— 只要「播放」请求在播放中重复到达
    （蓝牙耳机重连自动续播、锁屏/ROM 重发媒体键），就会被反转成**暂停**：
    表现就是「念着念着自己停下，而且没有任何提示」。这条测试锁住语义。
    """
    from kivy.clock import Clock
    from tts_android import STATE_PLAYING, STATE_PAUSED, STATE_STOPPED

    app = shared_app()
    path = os.path.join(tempfile.mkdtemp(), "t_media.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write("第一章\n甲句。乙句。\n")
    app.open_book(path)
    for _ in range(4):
        Clock.tick()

    log = []
    state = {"v": STATE_PLAYING}
    orig = (app._engine.get_state, app._engine.pause,
            app._engine.resume, app._engine.play)

    def _set_play():
        log.append("play")
        state["v"] = STATE_PLAYING

    try:
        app._engine.get_state = lambda: state["v"]

        def _pause():
            log.append("pause")
            state["v"] = STATE_PAUSED

        def _resume():
            log.append("resume")
            state["v"] = STATE_PLAYING

        app._engine.pause = _pause
        app._engine.resume = _resume
        app._engine.play = lambda *a: _set_play()

        # ① 播放中来「播放」→ 必须什么都不做（以前会反转成暂停）
        app.media_play("通知栏/耳机")
        check("media: 播放中的重复播放命令不再反转成暂停",
              state["v"] == STATE_PLAYING and not log,
              "state=%s log=%s" % (state["v"], log))

        # ② 播放中来「暂停」→ 暂停；重复到达也只暂停一次
        app.media_pause("通知栏/耳机")
        app.media_pause("通知栏/耳机")
        check("media: 暂停命令幂等",
              state["v"] == STATE_PAUSED and log == ["pause"],
              "state=%s log=%s" % (state["v"], log))

        # ③ 暂停中来「播放」→ 续播（而不是又切回暂停）
        app.media_play("通知栏/耳机")
        check("media: 暂停中收到播放命令会续播",
              state["v"] == STATE_PLAYING and log[-1] == "resume",
              "state=%s log=%s" % (state["v"], log))

        # ④ 停止态收到「播放」→ 开始朗读
        log[:] = []
        state["v"] = STATE_STOPPED
        app.media_play("通知栏/耳机")
        check("media: 停止态收到播放命令开始朗读",
              state["v"] == STATE_PLAYING and log == ["play"], "log=%s" % log)

        # ⑤ 命令留痕（自检屏可见），便于定位「谁让它停的」
        check("media: 外部命令留痕含来源",
              app._media_events >= 5 and "通知栏/耳机" in app._last_media_event,
              "n=%d last=%s" % (app._media_events, app._last_media_event))

        # ⑥ 旧版 Java 的 "playpause" 兼容入口：1 秒内重复命令直接忽略
        log[:] = []
        state["v"] = STATE_PLAYING
        app._last_playpause_ts = 0.0
        app._media_playpause_compat()      # 第一次：正常切换（→ 暂停）
        app._media_playpause_compat()      # 紧接着的重复：必须被忽略
        check("media: 旧 playpause 重复命令被去重（不会被反成播放）",
              state["v"] == STATE_PAUSED and log == ["pause"],
              "state=%s log=%s" % (state["v"], log))
    finally:
        (app._engine.get_state, app._engine.pause,
         app._engine.resume, app._engine.play) = orig


def main_run():
    # ⚠️ 顺序不能随便调：test_edge_flow 依赖后台线程 tick Clock 的节奏，
    #    前面跑过多 App 实例/频繁 Clock.tick() 会让它偶发 early=1。
    #    所以「设置弹窗」这组不碰异步节奏的测试放在最后跑。
    test_layout()
    test_edge_flow()
    test_edge_prefetch_parallel()
    test_edge_progress_signal()
    test_edge_advance_not_cut()
    test_config_backup()
    test_history()
    test_settings_popup()
    test_jump_default_paused()
    test_long_press_play_index()
    test_toc_follow()
    test_switch_backend_stops_old()
    test_freeze_detect_and_keepalive()
    test_edge_play_retry()
    test_media_cmd_semantics()
    test_voice_editor_opens()
    test_ai_roles_logic()
    print("TOTAL: %d/%d passed" % (sum(1 for r in RESULTS if r), len(RESULTS)))
    sys.exit(0 if all(RESULTS) else 1)


if __name__ == "__main__":
    main_run()
