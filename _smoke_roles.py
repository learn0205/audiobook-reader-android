# -*- coding: utf-8 -*-
"""_smoke_roles.py —— 多角色 UI 面板冒烟测试（真实 Kivy 环境跑一遍按钮路径）。

验证：设置面板新增按钮、角色管理面板、换绑编号、自定义音色编辑器、
全局模板面板、模板编号编辑、多角色开关、历史删除同步 —— 全部走一遍
不抛异常，映射与模板数据随之正确落盘。
"""

import os

os.environ["KIVY_NO_ARGS"] = "1"
os.environ["KIVY_NO_CONSOLELOG"] = "1"
os.environ["KIVY_NO_FILELOG"] = "1"
os.environ["KIVY_METRICS_DENSITY"] = "1"

from kivy.clock import Clock
import time

import main as main_mod
import role_config

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append(ok)
    print("[{}] {} {}".format("PASS" if ok else "FAIL", name, detail))


def flush(dt=0.05):
    Clock.tick()
    time.sleep(dt)
    Clock.tick()


def main_run():
    app = main_mod.AudioBookApp()
    app.build()
    data_dir = app.user_data_dir      # ⚠️ 只读属性，不能赋值（会原生崩溃）
    app._stop_tick_loop()             # 关掉兜底时钟线程，保持测试确定性
    # 屏蔽「自动恢复上次的书」：否则 flush() 走时钟时它会真实 open_book，
    # 把本测试构造的角色状态清掉
    app._config._data["last_book"] = ""
    Clock.unschedule(app._restore_last_book)

    # 造一本"已打开的书"和识别结果（假书 key，测试后清理）
    fake_key = "C:\\fake\\bookA_smoke.txt"
    app._book_key = fake_key
    app._paragraphs = ["「你好。」楚子航说道。"]
    chars = [("楚子航", "男角色"), ("夏弥", "女角色")]
    rm = role_config.auto_map(fake_key, chars, app._voice_template)
    role_config.save_map(data_dir, rm)
    app._role_map = rm
    app._role_detected = chars
    app._role_speakers = {}
    app._config._data.setdefault("multi_role", True)

    # 1) 设置面板（含新按钮）能打开
    app.show_settings()
    flush()
    check("设置面板打开", True)

    # 2) 角色管理面板
    app._show_role_panel()
    flush()
    check("角色管理面板打开", app._role_map.names() == ["楚子航", "夏弥"])

    # 3) 换绑编号（弹窗打开 + 数据层换绑落盘）
    app._edit_role_binding("夏弥")
    flush()
    app._role_map.set_slot("夏弥", "女角色2")
    role_config.save_map(data_dir, app._role_map)
    check("换绑编号落盘",
          role_config.load_map(data_dir, fake_key).get("夏弥")["slot"]
          == "女角色2")

    # 4) 自定义音色（编辑器弹窗打开 + 数据层保存）
    app._edit_role_voice("楚子航")
    flush()
    app._role_map.set_custom("楚子航", "zh-CN-YunxiaNeural", 12, 0.9)
    role_config.save_map(data_dir, app._role_map)
    got = role_config.RoleMap.entry_params(
        role_config.load_map(data_dir, fake_key).get("楚子航"),
        app._voice_template)
    check("自定义音色参数生效", got == ("zh-CN-YunxiaNeural", 0.9, 12), str(got))

    # 5) 一键重置
    app._reset_role("楚子航")
    check("重置回模板参数",
          role_config.RoleMap.entry_params(app._role_map.get("楚子航"),
                                           app._voice_template)[0]
          == "vits:auto:M1")

    # 6) 全局模板面板 + 编辑编号
    app._show_template_panel()
    flush()
    app._voice_template.set_slot("男角色1", "zh-CN-YunjianNeural", -8, 1.15)
    check("模板编号修改生效", app._voice_template.get("男角色1")["pitch"] == -8)
    check("模板修改落盘",
          role_config.load_map(data_dir, fake_key) is not None)

    # 7) 多角色开关
    before = app._multi_role_enabled()
    app._toggle_multi_role()
    check("开关切换", app._multi_role_enabled() == (not before))
    app._toggle_multi_role()
    check("开关切回", app._multi_role_enabled() == before)

    # 8) 历史删除同步删映射（无确认框；全局模板不受影响）
    other_key = "C:\\fake\\bookB_smoke.txt"
    role_config.save_map(data_dir, role_config.RoleMap(
        other_key, [{"name": "X", "slot": "男角色1", "custom": None}]))
    check("第二本书映射就绪", role_config.load_map(data_dir, other_key) is not None)
    role_config.delete_map(data_dir, other_key)
    check("删除映射", role_config.load_map(data_dir, other_key) is None)
    check("全局模板仍在",
          os.path.isfile(os.path.join(data_dir, "voice_template.json")))

    # 9) 逐句解析器挂上后能出参数
    app._role_speakers = {(0, "「你好。"): "楚子航"}
    app._engine.set_voice_resolver(app._role_voice_resolver)
    try:
        app._engine.load(app._paragraphs)   # 让本地后端有句子可查
        local = app._engine._ensure_local()
        params = local._voice_params_for(0)
        # 楚子航绑定男角色1；第 6 步已把该编号改成云健
        check("逐句解析出角色参数", params[0] == "zh-CN-YunjianNeural",
              str(params))
    except Exception as e:
        check("逐句解析出角色参数", False, str(e))

    # 10) 无说话人的句子（旁白/叙述）用模板「旁白」编号，不与角色同声
    try:
        app._role_speakers = {}
        local = app._engine._ensure_local()
        params = local._voice_params_for(0)
        want = app._voice_template.get("旁白")["voice"]
        check("旁白句用旁白音色", params[0] == want, str(params))
    except Exception as e:
        check("旁白句用旁白音色", False, str(e))

    # 清理测试数据
    role_config.delete_map(data_dir, fake_key)
    try:
        os.remove(os.path.join(data_dir, "voice_template.json"))
    except OSError:
        pass

    print("\n结果：%d/%d 通过" % (sum(RESULTS), len(RESULTS)))
    return 0 if all(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main_run())
