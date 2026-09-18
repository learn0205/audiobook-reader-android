import os
os.environ["KIVY_NO_ARGS"]="1"; os.environ["KIVY_NO_CONSOLELOG"]="1"; os.environ["KIVY_NO_FILELOG"]="1"
from kivy.config import Config
Config.set("graphics","width","400"); Config.set("graphics","height","800")
import main
app = main.AudioBookApp()

def probe(dt):
    from kivy.core.window import Window
    b, t = app._body, app._top_w
    gap = (b.y + b.height) - t.y
    print("[realwin] winH=%d bodyY=%.0f bodyTop=%.0f topBot=%.0f gap=%.1f bodyH=%.0f fixes=%d"
          % (Window.height, b.y, b.y+b.height, t.y, gap, b.height, app._layout_fixes))
    print("REALWIN:", "PASS" if abs(gap) < 1 and b.y > 100 else "FAIL")
    app.stop()

from kivy.clock import Clock
Clock.schedule_once(probe, 2)
app.run()
