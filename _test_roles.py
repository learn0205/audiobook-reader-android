# -*- coding: utf-8 -*-
"""_test_roles.py —— 多角色朗读新增模块的逻辑测试（桌面可直接运行）。

覆盖：
  1. role_parser：「XXX道」识别 / 代词他她 / 人名动作句入栈 /
     连续引号对话沿用栈顶 / 纯旁白不动栈 / 性别年龄归类；
  2. voice_template：加载/修改/恢复默认，模板修改全局生效；
  3. role_config：单书映射读写隔离、自定义覆盖、一键重置、删除；
  4. tts_engine.EdgeTTS：逐句音色钩子（含解析失败降级）与缓存键隔离；
  5. main 接入层可编译（py_compile 级别）。
运行：python _test_roles.py
"""

import os
import shutil
import sys
import tempfile
import traceback

PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ✓ %s" % name)
    else:
        FAIL += 1
        print("  ✗ %s  %s" % (name, extra))


def section(title):
    print("\n=== %s ===" % title)


# ---------------------------------------------------------------------------
def test_role_parser():
    from role_parser import analyze
    from tts_android import split_sentences

    def spk(speakers, pi, para):
        """说话人查询辅助：段落切成片段后逐片查找（与引擎相同的 key）。"""
        for f in split_sentences(para):
            if (pi, f) in speakers:
                return speakers[(pi, f)]
        return None

    section("role_parser 角色识别与角色栈")
    paras = [
        "楚子航推开咖啡馆的门，风铃轻响。",
        "「你迟到了。」楚子航说道。",
        "「抱歉，路上堵车。」夏弥笑着回答。",
        "夏弥低头踩过一片叶子。",
        "「下次注意。」他说道。",
        "窗外的雨越下越大，行人匆匆走过街角。",
        "「我们走吧。」",
        "「去哪里？」",
        "「随便你。」",
        "夏弥点了点头，她把叶子夹进了书里。",
        "上山的路很陡，老太太拄着拐杖慢慢往上爬。",
        "「小伙子，搭把手。」老太太说道。",
    ]
    characters, speakers = analyze(paras)
    names = [n for n, _c in characters]
    cat = dict(characters)

    check("识别出楚子航", "楚子航" in names, str(names))
    check("识别出夏弥", "夏弥" in names, str(names))
    check("识别出老太太", "老太太" in names, str(names))
    check("不会把「夏弥笑」当成新人物", "夏弥笑" not in names, str(names))
    check("按出场顺序排序", names[0] == "楚子航", str(names))

    check("「楚子航说道」→ 楚子航", spk(speakers, 1, paras[1]) == "楚子航",
          repr(spk(speakers, 1, paras[1])))
    check("「夏弥笑着回答」→ 夏弥", spk(speakers, 2, paras[2]) == "夏弥",
          repr(spk(speakers, 2, paras[2])))
    check("动作句「夏弥低头…」不产生说话人", spk(speakers, 3, paras[3]) is None)
    check("代词「他说道」→ 性别匹配到楚子航",
          spk(speakers, 4, paras[4]) == "楚子航",
          repr(spk(speakers, 4, paras[4])))
    check("纯旁白句无说话人", spk(speakers, 5, paras[5]) is None)
    check("连续无姓名引号沿用栈顶", spk(speakers, 6, paras[6]) == "楚子航",
          repr(spk(speakers, 6, paras[6])))
    check("连续第二句同样沿用", spk(speakers, 8, paras[8]) == "楚子航",
          repr(spk(speakers, 8, paras[8])))
    check("「老太太说道」→ 老太太", spk(speakers, 11, paras[11]) == "老太太",
          repr(spk(speakers, 11, paras[11])))
    check("老太太归类为奶奶", cat.get("老太太") == "奶奶", str(characters))
    check("夏弥归类为女角色（同段「她」投票）",
          cat.get("夏弥") == "女角色", str(characters))

    # 引号后置标签
    p2 = ["「站住。」路明非喊道。"]
    _c2, s2 = analyze(p2)
    check("引号后置「XX喊道」识别", spk(s2, 0, p2[0]) == "路明非", str(s2))

    # 旁白在前、对话在后（动作句入栈生效）
    p3 = [
        "夏弥想了想。",
        "「那就这么定了。」",
    ]
    _c3, s3 = analyze(p3)
    check("动作句把人名标记为后续对话说话人",
          spk(s3, 1, p3[1]) == "夏弥", str(s3))

    # 解析失败降级路径由调用方实现，这里只验证 analyze 不误抛
    try:
        analyze([])
        check("空书不抛异常", True)
    except Exception as e:
        check("空书不抛异常", False, str(e))


# ---------------------------------------------------------------------------
def test_voice_template():
    from voice_template import VoiceTemplate, DEFAULT_SLOTS

    section("voice_template 全局模板")
    tmp = tempfile.mkdtemp(prefix="vt_")
    try:
        t = VoiceTemplate(tmp)
        check("默认编号数量", len(t.all_slots()) == len(DEFAULT_SLOTS))
        p = t.get("男角色1")
        check("男角色1 默认音色", p["voice"] == "zh-CN-YunxiNeural", str(p))

        t.set_slot("男角色1", "zh-CN-YunjianNeural", -12, 1.1)
        t2 = VoiceTemplate(tmp)          # 重新加载验证落盘
        check("修改后落盘", t2.get("男角色1")["voice"] == "zh-CN-YunjianNeural")
        check("音调/语速同步", t2.get("男角色1")["pitch"] == -12
              and abs(t2.get("男角色1")["rate"] - 1.1) < 1e-6)

        t2.reset_slot("男角色1")
        t3 = VoiceTemplate(tmp)
        check("恢复默认", t3.get("男角色1")["voice"] == "zh-CN-YunxiNeural")

        used = {"男角色1", "男角色2"}
        check("next_free_slot 跳过已占用", t3.next_free_slot("男角色", used)
              == "男角色3")
        check("类别耗尽返回 None",
              t3.next_free_slot("奶奶", {("奶奶%d" % i) for i in range(1, 5)})
              is None)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
def test_role_config():
    import role_config
    from voice_template import VoiceTemplate

    section("role_config 单书映射存储")
    tmp = tempfile.mkdtemp(prefix="rc_")
    try:
        tpl = VoiceTemplate(tmp)
        detected = [("楚子航", "男角色"), ("夏弥", "女角色"),
                    ("老太太", "奶奶"), ("王大爷", "中年叔叔")]
        rm = role_config.auto_map("book-A", detected, tpl)
        check("自动映射男1", rm.get("楚子航")["slot"] == "男角色1")
        check("自动映射女1", rm.get("夏弥")["slot"] == "女角色1")
        check("自动映射奶奶1", rm.get("老太太")["slot"] == "奶奶1")
        check("自动映射中年叔叔1", rm.get("王大爷")["slot"] == "中年叔叔1")

        role_config.save_map(tmp, rm)
        rm2 = role_config.load_map(tmp, "book-A")
        check("重新加载成功", rm2 is not None
              and rm2.get("夏弥")["slot"] == "女角色1")

        # 单书隔离
        rm_b = role_config.auto_map("book-B", [("林一", "男角色")], tpl)
        role_config.save_map(tmp, rm_b)
        check("不同书互不干扰",
              role_config.load_map(tmp, "book-A").get("楚子航") is not None
              and role_config.load_map(tmp, "book-B").get("夏弥") is None)

        # 自定义覆盖：参数不再跟随模板
        rm2.set_custom("楚子航", "zh-CN-YunxiaNeural", 20, 0.8)
        role_config.save_map(tmp, rm2)
        rm3 = role_config.load_map(tmp, "book-A")
        vp = role_config.RoleMap.entry_params(rm3.get("楚子航"), tpl)
        check("自定义参数生效", vp == ("zh-CN-YunxiaNeural", 0.8, 20), str(vp))
        vp2 = role_config.RoleMap.entry_params(rm3.get("夏弥"), tpl)
        check("未自定义者仍跟随模板",
              vp2 == ("zh-CN-XiaoxiaoNeural", 1.0, 0), str(vp2))

        # 模板修改 → 未自定义者同步、自定义者不受影响
        tpl.set_slot("女角色1", "zh-CN-XiaoyiNeural", 5, 1.0)
        vp3 = role_config.RoleMap.entry_params(rm3.get("夏弥"), tpl)
        check("模板修改同步到跟随者", vp3[0] == "zh-CN-XiaoyiNeural", str(vp3))
        vp4 = role_config.RoleMap.entry_params(rm3.get("楚子航"), tpl)
        check("自定义者不受模板修改影响", vp4[0] == "zh-CN-YunxiaNeural")

        # 一键重置
        rm3.reset_custom("楚子航")
        vp5 = role_config.RoleMap.entry_params(rm3.get("楚子航"), tpl)
        check("重置后回到模板参数", vp5[0] == "zh-CN-YunxiNeural", str(vp5))

        # 删除（无确认框的存储层行为）
        role_config.delete_map(tmp, "book-A")
        check("删除后加载为 None", role_config.load_map(tmp, "book-A") is None)
        role_config.delete_all_maps(tmp)
        check("全部清空", role_config.load_map(tmp, "book-B") is None)
        check("全局模板文件仍在",
              os.path.isfile(os.path.join(tmp, "voice_template.json")))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
def test_engine_resolver():
    from tts_engine import EdgeTTS
    from tts_android import STATE_STOPPED

    section("EdgeTTS 逐句音色钩子（与 role_parser 联动）")
    from role_parser import analyze
    e = EdgeTTS()
    e.set_voice("zh-CN-XiaoxiaoNeural")
    e.set_speed(1.0)
    e.set_pitch(0)
    paras = ["「你好。」楚子航说道。", "「你是谁？」他答道。"]
    e.load(paras)

    # 无钩子：全局参数
    v0 = e._voice_params_for(0)
    check("无钩子回退全局音色", v0[0] == "zh-CN-XiaoxiaoNeural", str(v0))

    # 有钩子：按 role_parser 的说话人标注给参数
    _chars, speakers = analyze(paras)
    def resolver(para, sentence):
        name = speakers.get((para, sentence))
        if name == "楚子航":
            return "zh-CN-YunxiNeural", 0.8, -10
        return None
    e.set_voice_resolver(resolver)
    # 第 0 句切分片段均与引号相交 → 说话人楚子航
    v1 = e._voice_params_for(0)
    check("角色句用角色音色", v1[0] == "zh-CN-YunxiNeural", str(v1))
    check("角色句语速换算", v1[1] == "-20%", str(v1))
    check("角色句音调换算", v1[2] == "-10Hz", str(v1))
    v2 = e._voice_params_for(2)          # 第 1 段「你是谁？」→ 他→楚子航
    check("代词句同样解析", v2[0] == "zh-CN-YunxiNeural", str(v2))

    # 旁白句：说话人为 None → 全局音色
    e.load(["窗外的雨越下越大，行人匆匆走过街角。"])
    _c3, sp3 = analyze(["窗外的雨越下越大，行人匆匆走过街角。"])
    e.set_voice_resolver(lambda p, s: None if not sp3 else
                         ("zh-CN-YunxiNeural", 1.0, 0) if sp3.get((p, s))
                         else None)
    v3 = e._voice_params_for(0)
    check("旁白句回退全局音色", v3[0] == "zh-CN-XiaoxiaoNeural", str(v3))

    # 缓存键隔离：不同音色的同一句话是不同文件
    p_a = e._cache_path("你好", params=("zh-CN-YunxiNeural", "+0%", "+0Hz", "+0%"))
    p_b = e._cache_path("你好", params=("zh-CN-YunjianNeural", "+0%", "+0Hz", "+0%"))
    check("不同角色缓存隔离", p_a != p_b)

    # 解析失败（钩子抛异常）→ 降级全局单音色
    def bad_resolver(para, sentence):
        raise RuntimeError("boom")
    e.set_voice_resolver(bad_resolver)
    v3 = e._voice_params_for(0)
    check("钩子异常自动降级", v3[0] == "zh-CN-XiaoxiaoNeural", str(v3))

    # 钩子置 None：完全恢复单音色
    e.set_voice_resolver(None)
    check("摘除钩子恢复原行为",
          e._voice_params_for(0)[0] == "zh-CN-XiaoxiaoNeural")
    check("状态机不受影响", e.get_state() == STATE_STOPPED)


def test_compile_all():
    section("全部源码可编译")
    import py_compile
    here = os.path.dirname(os.path.abspath(__file__))
    for f in ("role_parser.py", "voice_template.py", "role_config.py",
              "tts_engine.py", "main.py"):
        try:
            py_compile.compile(os.path.join(here, f), doraise=True)
            check(f, True)
        except Exception as e:
            check(f, False, str(e))


if __name__ == "__main__":
    try:
        test_role_parser()
        test_voice_template()
        test_role_config()
        test_engine_resolver()
        test_compile_all()
    except Exception:
        traceback.print_exc()
        FAIL = FAIL + 1
    print("\n结果：%d 通过，%d 失败" % (PASS, FAIL))
    sys.exit(1 if FAIL else 0)
