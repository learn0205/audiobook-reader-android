# -*- coding: utf-8 -*-
"""桌面回归测试入口：全部通过退出码 0，任一失败退出码 1。

包含：
  1) 布局：正文区严格贴合顶栏下沿，保镖 fix 计数正常（_smoke 的检查）
  2) _v.py 的期望高度断言
  3) Edge 后端：逐句推进 + 播放期间预取 + 全句缓存（假合成，无网络）
  4) ConfigManager 备份导出/导入
  5) 设置弹窗：文字不被裁切 + 深浅主题对比度达标（见 test_settings_popup）
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
    import edge_tts_client
    import tts_engine
    from tts_android import STATE_STOPPED

    calls = []

    def fake_synth(text, voice, path, rate="+0%", pitch="+0Hz", volume="+0%"):
        calls.append(text)
        with open(path, "wb") as f:
            f.write(b"ID3fake")
        time.sleep(0.02)
        return 7

    edge_tts_client.synthesize_to_file = fake_synth

    # ⚠️ 以前这里另起一个线程循环 Clock.tick()，好让「合成完 → 起播」这条
    #    Clock 投递被处理。但 Clock **不是线程安全的**：子线程 tick() 与合成
    #    线程的 schedule_once 会互相踩，CI 慢机器上回调整条丢失 → 预取永远不触发
    #    （`early=1` 偶发失败）。现在桌面端 _post_to_main 是同步直调，不需要
    #    后台泵时钟；这里只在主线程自己 tick，保持确定性。
    from kivy.clock import Clock

    e = tts_engine.EdgeTTS()
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


def main_run():
    # ⚠️ 顺序不能随便调：test_edge_flow 依赖后台线程 tick Clock 的节奏，
    #    前面跑过多 App 实例/频繁 Clock.tick() 会让它偶发 early=1。
    #    所以「设置弹窗」这组不碰异步节奏的测试放在最后跑。
    test_layout()
    test_edge_flow()
    test_config_backup()
    test_history()
    test_settings_popup()
    test_jump_default_paused()
    print("TOTAL: %d/%d passed" % (sum(1 for r in RESULTS if r), len(RESULTS)))
    sys.exit(0 if all(RESULTS) else 1)


if __name__ == "__main__":
    main_run()
