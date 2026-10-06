# -*- coding: utf-8 -*-
"""voice_labeler.py —— 电脑端 VITS 音色试听标注管理器（可随机选/改/删）

用法
----
  pip install sherpa-onnx            # 仅首次
  python voice_labeler.py --zip vits-zh-hf-fanchen-C.zip   # 首次解压
  python voice_labeler.py                                  # 日常

命令（启动后随时输入）
----------------------
  回车 / n          下一个未标注的音色（合成+播放+进入标注）
  12                跳到 sid=12：合成+播放+进入标注
  e 12 m 3 沉稳     直接修改 sid=12 的标注（m/f + 1少年2青年3中年4老年 + 备注）
  d 12              删除 sid=12 的标注
  r 12              重播 sid=12
  L                 列出全部已标注
  F 男              列出标注为「男」的所有 sid（F 女 同理）
  S                 统计（已标/未标/男女数）
  G                 立即导出 voice_labels.json（传手机）+ voice_notes.csv
  a                 自动模式：连续「下一个」直到全部标完
  q                 保存退出

产出
----
  voice_labels.json → 传到手机，设置 →「导入音色标注」→ 模板自动音色生效
  voice_notes.csv   → 全量标注表（Excel 可开）
"""

import argparse
import csv
import json
import os
import shutil
import sys
import time
import zipfile

SENTENCE = "你好，这是一段音色试听，今天天气不错，我们出门走走吧。"
AGES = {"1": "少年", "2": "青年", "3": "中年", "4": "老年"}
STATE_FILE = "voice_state.json"      # 本机进度（完整明细）
EXPORT_FILE = "voice_labels.json"    # 导出给手机（扁平 男/女）
NOTES_FILE = "voice_notes.csv"
MODEL_DIR = "vits-zh-hf-fanchen-C"

_tts = None
_num_speakers = 187


# ---------------------------------------------------------------------------
# 模型准备与合成（桌面 sherpa-onnx）
# ---------------------------------------------------------------------------
def prepare_model(args):
    if os.path.isdir(args.model) and os.path.isfile(
            os.path.join(args.model, "tokens.txt")):
        return args.model
    if args.zip and os.path.isfile(args.zip):
        print("解压语音包 %s → %s/ ..." % (args.zip, MODEL_DIR))
        tops = ("vits-zh-hf-fanchen-C/", "./")
        with zipfile.ZipFile(args.zip) as z:
            for zi in z.infolist():
                rel = zi.filename.replace("\\", "/")
                for top in tops:
                    if rel.startswith(top):
                        rel = rel[len(top):]
                        break
                rel = rel.lstrip("./")
                if not rel or os.path.basename(rel) in ("rule.far",):
                    continue
                dest = os.path.join(MODEL_DIR, *rel.split("/"))
                if zi.is_dir():
                    os.makedirs(dest, exist_ok=True)
                else:
                    os.makedirs(os.path.dirname(dest) or MODEL_DIR,
                                exist_ok=True)
                    with z.open(zi) as src, open(dest, "wb") as dst:
                        shutil.copyfileobj(src, dst)
        return MODEL_DIR
    sys.exit("找不到模型：请把 fanchen-C 语音包解压到 %s/ 或用 --zip 指定"
             % MODEL_DIR)


def get_tts(model_dir):
    global _tts, _num_speakers
    if _tts is not None:
        return _tts
    import sherpa_onnx
    onnx = lexicon = tokens = dict_dir = None
    for root, dirs, files in os.walk(model_dir):
        for f in files:
            p = os.path.join(root, f)
            lf = f.lower()
            if lf.endswith(".onnx") and os.path.getsize(p) > 50 * 1024 * 1024:
                onnx = p
            elif f == "tokens.txt":
                tokens = p
            elif f == "lexicon.txt":
                lexicon = p
        for d in dirs:
            if d == "dict":
                dict_dir = os.path.join(root, d)
    if not (onnx and tokens and lexicon and dict_dir):
        sys.exit("模型文件不完整（onnx/tokens/lexicon/dict 缺一不可）")
    print("加载 VITS 引擎（首次较慢）...")
    t0 = time.time()
    vits = sherpa_onnx.OfflineTtsVitsModelConfig(
        model=onnx, lexicon=lexicon, tokens=tokens, data_dir="",
        dict_dir=dict_dir)
    fsts = []
    for root, _d, files in os.walk(model_dir):
        for f in files:
            if f.endswith(".fst"):
                fsts.append(os.path.join(root, f))
    config = sherpa_onnx.OfflineTtsConfig(
        model=sherpa_onnx.OfflineTtsModelConfig(
            vits=vits, num_threads=2, debug=0, provider="cpu"),
        rule_fsts=",".join(sorted(fsts)))
    _tts = sherpa_onnx.OfflineTts(config)
    _num_speakers = _tts.num_speakers
    print("引擎就绪：%d 音色，耗时 %.1fs" % (_num_speakers, time.time() - t0))
    return _tts


def synth(sid, model_dir):
    """合成试听句 → wav（同 sid 有缓存，重播秒开）。"""
    tts = get_tts(model_dir)
    cache = os.path.join("preview", "sid_%03d.wav" % sid)
    if os.path.isfile(cache) and os.path.getsize(cache) > 10000:
        return cache
    t0 = time.time()
    audio = tts.generate(SENTENCE, sid=sid, speed=1.0)
    import wave
    os.makedirs(os.path.dirname(cache), exist_ok=True)
    with wave.open(cache, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(audio.sample_rate)
        w.writeframes(b"".join(
            int(max(-1, min(1, s)) * 32767).to_bytes(2, "little", signed=True)
            for s in audio.samples))
    return cache


def play(path):
    try:
        import winsound
        winsound.PlaySound(path, winsound.SND_FILENAME)
    except Exception as e:
        print("（播放失败：%s——可手动播放 %s）" % (e, path))


# ---------------------------------------------------------------------------
# 标注状态
# ---------------------------------------------------------------------------
def load_state():
    if os.path.isfile(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_FILE)          # ⚠️ 只写状态文件，绝不覆盖导出文件


def export_notes(state):
    try:
        with open(NOTES_FILE, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["sid", "性别", "年龄段", "备注"])
            for sid in sorted(int(k) for k in state):
                e = state[str(sid)]
                w.writerow([sid, e.get("gender", ""), e.get("age", ""),
                            e.get("note", "")])
    except Exception as e:
        print("（CSV 导出失败：%s）" % e)


def export_app_labels(state, quiet=False):
    out = {str(sid): e["gender"] for sid, e in state.items()
           if e.get("gender") in ("男", "女")}
    with open(EXPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    if not quiet:
        m = sum(1 for v in out.values() if v == "男")
        print("已导出 voice_labels.json（男 %d / 女 %d）——传到手机 → "
              "设置 →「导入音色标注」" % (m, len(out) - m))


def stats(state):
    labeled = len(state)
    m = sum(1 for e in state.values() if e.get("gender") == "男")
    f = sum(1 for e in state.values() if e.get("gender") == "女")
    return labeled, _num_speakers - labeled, m, f


def next_unlabeled(state, after=-1):
    for sid in range(after + 1, _num_speakers):
        if str(sid) not in state:
            return sid
    for sid in range(0, _num_speakers):        # 全标完则从头找可改的
        if str(sid) not in state:
            return sid
    return None


def show(sid, state):
    e = state.get(str(sid))
    if e:
        print("sid=%d → %s/%s%s" % (sid, e.get("gender", "?"),
                                    e.get("age", ""),
                                    ("（" + e.get("note", "") + "）")
                                    if e.get("note") else ""))
    else:
        print("sid=%d 未标注" % sid)


def parse_label_args(args):
    """e 命令参数：[m|f] [1-4] [备注...] → (gender, age, note)。"""
    gender, age, note = None, "", ""
    if args and args[0].lower() in ("m", "f"):
        gender = "男" if args[0].lower() == "m" else "女"
        args = args[1:]
    if args and args[0] in AGES:
        age = AGES[args[0]]
        args = args[1:]
    if args:
        note = " ".join(args)
    if gender is None:
        print("  缺性别：e <sid> m|f [1-4] [备注]")
        return None
    return {"gender": gender, "age": age or "青年", "note": note}


HELP = """最简用法：输入 m 或 f —— 播放下一个未标注音色并立即标上性别
  （可带年龄段：m 3 = 男·中年；f 2 = 女·青年；后面还能空格加备注）

其他命令：
  回车/n   下一个未标注（进入标注）    <编号>    播放并标注该音色
  e <编号> m|f [1-4] [备注]  修改任意标注      d <编号>  删除标注
  r <编号> 重播              L   列出全部已标注
  F 男|女  按性别列出         S   统计
  a        自动连续模式      G   立即导出      q  保存退出"""


def main():
    ap = argparse.ArgumentParser(description="电脑端 VITS 音色标注管理器")
    ap.add_argument("--model", default=MODEL_DIR)
    ap.add_argument("--zip", help="语音包 ZIP（首次自动解压）")
    args = ap.parse_args()

    model_dir = prepare_model(args)
    get_tts(model_dir)
    state = load_state()
    last_sid = None                    # 最近一次播放的 sid（m/f 可直接补标）
    labeled, unlab, m, f = stats(state)
    print("已标 %d / 未标 %d（男 %d 女 %d）" % (labeled, unlab, m, f))
    print(HELP)

    while True:
        try:
            raw = input("\n[vits-labeler] > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n保存退出")
            save_state(state)
            break
        if not raw:
            raw = "n"
        parts = raw.split()
        cmd = parts[0].lower()

        try:
            # ⭐ 最常用动作：裸 m/f = 播放下一个未标注音色并立即标性别
            # （m 3 沉稳 = 男·中年·备注；对刚播过的未标注音色也生效）
            if cmd in ("m", "f") and (len(parts) == 1 or
                                      parts[1] in AGES or
                                      parts[1] not in ("男", "女")):
                sid = None
                if last_sid is not None and str(last_sid) not in state:
                    sid = last_sid            # 刚播放还没标的，优先补标
                if sid is None:
                    sid = next_unlabeled(state)
                if sid is None:
                    print("187 个全部标注完成！G 导出即可")
                    continue
                path = synth(sid, model_dir)
                show(sid, state)
                play(path)
                entry = {"sid": sid,
                         "gender": "男" if cmd == "m" else "女",
                         "age": "青年", "note": ""}
                if len(parts) > 1 and parts[1] in AGES:
                    entry["age"] = AGES[parts[1]]
                if len(parts) > 2:
                    entry["note"] = " ".join(parts[2:])
                state[str(sid)] = entry
                save_state(state)
                last_sid = sid
                show(sid, state)
                continue
            if cmd in ("n", "下一个"):
                sid = next_unlabeled(state)
                if sid is None:
                    print("187 个全部标注完成！G 导出即可")
                    continue
                path = synth(sid, model_dir)
                show(sid, state)
                play(path)
                last_sid = sid
                res = parse_label_args(parts[1:])
                if res:
                    state[str(sid)] = {"sid": sid, **res}
                    save_state(state)
                    show(sid, state)
            elif cmd == "a":
                while True:
                    sid = next_unlabeled(state)
                    if sid is None:
                        print("全部完成！")
                        break
                    path = synth(sid, model_dir)
                    print("[sid=%d] 播放中..." % sid)
                    play(path)
                    res = parse_label_args(input(
                        "sid=%d 性别 m/f（s=跳过 q=结束自动模式）: "
                        % sid).strip().split())
                    if res is None:
                        break
                    if res.get("gender"):
                        state[str(sid)] = {"sid": sid, **res}
                        save_state(state)
                        show(sid, state)
            elif cmd.isdigit():
                sid = int(cmd)
                if not 0 <= sid < _num_speakers:
                    print("sid 范围 0-%d" % (_num_speakers - 1))
                    continue
                path = synth(sid, model_dir)
                show(sid, state)
                play(path)
                last_sid = sid
                res = parse_label_args(parts[1:])
                if res:
                    state[str(sid)] = {"sid": sid, **res}
                    save_state(state)
                    show(sid, state)
            elif cmd == "e" and len(parts) > 1 and parts[1].isdigit():
                sid = int(parts[1])
                res = parse_label_args(parts[2:])
                if res:
                    state[str(sid)] = {"sid": sid, **res}
                    save_state(state)
                    show(sid, state)
            elif cmd == "d" and len(parts) > 1 and parts[1].isdigit():
                state.pop(parts[1], None)
                save_state(state)
                print("sid=%s 已删除" % parts[1])
            elif cmd == "r" and len(parts) > 1 and parts[1].isdigit():
                play(synth(int(parts[1]), model_dir))
            elif cmd == "l":
                for sid in sorted(int(k) for k in state):
                    show(sid, state)
            elif cmd == "f" and len(parts) > 1 and parts[1] in ("男", "女"):
                sids = sorted(int(k) for k, e in state.items()
                              if e.get("gender") == parts[1])
                print("%s（%d 个）: %s" % (parts[1], len(sids), sids))
            elif cmd == "s":
                labeled, unlab, m, f = stats(state)
                print("已标 %d / 未标 %d（男 %d 女 %d）"
                      % (labeled, unlab, m, f))
            elif cmd == "g":
                export_app_labels(state)
                export_notes(state)
            elif cmd in ("h", "help", "?"):
                print(HELP)
            elif cmd in ("q", "quit", "exit"):
                save_state(state)
                export_app_labels(state)
                break
            else:
                print("未知命令，h 查看帮助")
        except Exception as e:
            print("命令出错：%s" % e)


if __name__ == "__main__":
    main()
