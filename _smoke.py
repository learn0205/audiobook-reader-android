import os
os.environ["KIVY_NO_ARGS"]="1"; os.environ["KIVY_NO_CONSOLELOG"]="1"; os.environ["KIVY_NO_FILELOG"]="1"
os.environ["KIVY_METRICS_DENSITY"]="1"
from kivy.config import Config
Config.set("graphics","width","400"); Config.set("graphics","height","800")
import main
app = main.AudioBookApp()
root = app.build()   # 完整启动路径（含配置/引擎/tick循环/布局保镖）
root.size = (400, 800)
from kivy.clock import Clock

def check(tag):
    b, t = app._body, app._top_w
    gap = (b.y + b.height) - t.y
    ok = abs(gap) < 1 and b.height > 100 and b.y > 100 and abs(app._scroll.y - b.y) < 1
    print(f"[{tag}] body.h={b.height:.0f} body.y={b.y:.0f} gap={gap:.1f} fixes={app._layout_fixes} -> {'OK' if ok else 'FAIL'}")
    return ok

results = []
for i in range(10):
    Clock.tick(); root.do_layout()
results.append(check("startup 400x800"))

# 模拟设备上的诡异复位：body.y 被拉回 0（上一版设备上的真实状态）
app._body.pos = (0, 0)
Clock.tick()
results.append(check("after device-like reset (enforcer must heal)"))

# 模拟顶栏也被拉走
app._top_w.pos = (0, 0)
Clock.tick()
results.append(check("after top-bar reset (enforcer must heal)"))

root.size = (360, 740)   # 窗口尺寸变化后依然正确
for i in range(5):
    Clock.tick()
results.append(check("resized 360x740"))

app._stop_tick_loop()
print("SMOKE:", "PASS" if all(results) else "FAIL")
