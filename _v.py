import os
os.environ["KIVY_NO_ARGS"]="1"; os.environ["KIVY_NO_CONSOLELOG"]="1"; os.environ["KIVY_NO_FILELOG"]="1"
os.environ["KIVY_METRICS_DENSITY"]="1"
from kivy.config import Config
Config.set("graphics","width","400"); Config.set("graphics","height","800")
import main
from kivy.lang import Builder
Builder.load_string(main.KV)
app = main.AudioBookApp()
root = app._build_ui()
root.size=(400, 800)
from kivy.clock import Clock
for _ in range(8): Clock.tick(); root.do_layout()
b = app._body; top = app._top_w
print("body.h=%.0f(exp 618) body.top=%.0f top.bot=%.0f touch=%s" % (b.height, b.y+b.height, top.y, abs((b.y+b.height)-top.y)<1))
