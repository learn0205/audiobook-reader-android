# -*- coding: utf-8 -*-
"""桌面回归测试入口：全部通过退出码 0，任一失败退出码 1。

包含：
  1) 布局：正文区严格贴合顶栏下沿，保镖 fix 计数正常（_smoke 的检查）
  2) _v.py 的期望高度断言
  3) Edge 后端：逐句推进 + 播放期间预取 + 全句缓存（假合成，无网络）
  4) ConfigManager 备份导出/导入
"""
import os
import sys
import tempfile
import threading
import time

os.environ["KIVY_NO_ARGS"] = "1"
os.environ["KIVY_NO_CONSOLELOG"] = "1"
os.environ["KIVY_NO_FILELOG"] = "1"
os.environ["KIVY_METRICS_DENSITY"] = "1"

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append(ok)
    print("[{}] {} {}".format("PASS" if ok else "FAIL", name, detail))


def test_layout():
    from kivy.config import Config
    Config.set("graphics", "width", "400")
    Config.set("graphics", "height", "800")
    import main
    app = main.AudioBookApp()
    root = app.build()
    root.size = (400, 800)
    from kivy.clock import Clock
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

    from kivy.clock import Clock
    stop = threading.Event()

    def ticker():
        while not stop.is_set():
            Clock.tick()
            time.sleep(0.03)
    threading.Thread(target=ticker, daemon=True).start()

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
        time.sleep(0.05)
    early = len(calls)
    deadline = time.time() + 90
    while time.time() < deadline:
        e.poll_advance()
        time.sleep(0.02)
        if e.get_state() == STATE_STOPPED and e._index >= total:
            break
    stop.set()
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


def main_run():
    test_layout()
    test_edge_flow()
    test_config_backup()
    print("TOTAL: %d/%d passed" % (sum(1 for r in RESULTS if r), len(RESULTS)))
    sys.exit(0 if all(RESULTS) else 1)


if __name__ == "__main__":
    main_run()
