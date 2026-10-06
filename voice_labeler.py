# -*- coding: utf-8 -*-
"""voice_labeler.py —— 电脑端 VITS 音色逐个试听与标注工具

用途
----
在电脑上把 fanchen-C 的 187 个音色**逐个听完**，为每个音色标注
性别（男/女）、年龄段（少年/青年/中年/老年）和备注，产出手机 App
可直接导入的 voice_labels.json —— 导入后全部 30 个角色槽位的
自动音色令牌（vits:auto:M1/F1...）立刻按你的标注生效。

使用前提
--------
  pip install sherpa-onnx        # 桌面推理（仅首次）
模型：fanchen-C 语音包（ZIP 或已解压目录）
  python voice_labeler.py --zip vits-zh-hf-fanchen-C.zip   # 首次
  python voice_labeler.py                                   # 之后

操作
----
  每个音色：自动合成并播放一句固定中文，然后输入标注：
    m = 男    f = 女    s = 跳过（以后再说）
    r = 重播  n = 下一个不标  b = 上一个改标注  q = 保存退出
  性别后可追年龄段与备注：  m 3 沉稳大叔   （3=中年，2=青年，1=少年，4=老年）
  已标过的自动跳过；--redo 11 可重标指定 sid。
产出
----
  voice_labels.json      → 传到手机，设置→导入音色标注
  voice_notes.csv        → 全部音色的完整标注/备注表（Excel 可开）
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
STATE_FILE = "voice_labels.json"
NOTES_FILE = "voice_notes.csv"
MODEL_DIR = "vits-zh-hf-fanchen-C"

_tts = None
_num_speakers = 187


# ---------------------------------------------------------------------------
# 模型准备与合成（桌面 sherpa-onnx）
# ---------------------------------------------------------------------------
def prepare_model(args):
    """确保模型目录可用。支持 --zip（自动解压，跳过 rule.far）。"""
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
    """懒加载桌面 VITS 引擎（首次约 5-15 秒）。"""
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
    """合成试听句 → wav 文件，返回 (文件名, 耗时秒)。"""
    tts = get_tts(model_dir)
    t0 = time.time()
    audio = tts.generate(SENTENCE, sid=sid, speed=1.0)
    import wave
    path = os.path.join("preview", "sid_%03d.wav" % sid)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(audio.sample_rate)
        w.writeframes(b"".join(
            int(max(-1, min(1, s)) * 32767).to_bytes(2, "little", signed=True)
            for s in audio.samples))
    return path, time.time() - t0


def play(path):
    try:
        import winsound
        winsound.PlaySound(path, winsound.SND_FILENAME)
    except Exception as e:
        print("（播放失败：%s——请手动播放 %s）" % (e, path))


# ---------------------------------------------------------------------------
# 标注状态与交互
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
    os.replace(tmp, STATE_FILE)
    export_notes(state)


def export_notes(state):
    """全部标注导出 CSV（Excel 可开，GBK 兼容 Windows Excel）。"""
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


def export_app_labels(state):
    """产出手机可导入的 voice_labels.json（sid → 男/女）。"""
    out = {str(sid): e["gender"] for sid, e in state.items()
           if e.get("gender") in ("男", "女")}
    with open("voice_labels.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    m = sum(1 for v in out.values() if v == "男")
    print("\n已导出 voice_labels.json（男 %d / 女 %d，共 %d 个）"
          % (m, len(out) - m, len(out)))
    print("把它传到手机 → 设置 → 「导入音色标注」→ 模板自动音色立刻生效")


def ask_label(sid):
    """交互问一个音色的标注。返回 dict 或 None(跳过)。"""
    while True:
        raw = input("性别 m=男 f=女 s=跳过 r=重播 q=退出: ").strip().lower()
        if raw == "r":
            return {"__replay__": True}
        if raw == "q":
            return {"__quit__": True}
        if raw == "s":
            return None
        if raw in ("m", "f"):
            gender = "男" if raw == "m" else "女"
            age, note = "", ""
            extra = input("年龄段 1少年 2青年 3中年 4老年（回车=青年），"
                          "可加备注（空格分隔）: ").strip()
            parts = extra.split(" ", 1)
            if parts and parts[0] in AGES:
                age = AGES[parts[0]]
            if len(parts) > 1:
                note = parts[1]
            return {"gender": gender,
                    "age": age or "青年", "note": note}
        print("  输入 m/f/s/r/q")


def main():
    ap = argparse.ArgumentParser(description="电脑端 VITS 音色试听标注工具")
    ap.add_argument("--model", default=MODEL_DIR, help="模型目录")
    ap.add_argument("--zip", help="语音包 ZIP（首次自动解压）")
    ap.add_argument("--start", type=int, default=0, help="起始 sid")
    ap.add_argument("--redo", type=int, action="append",
                    help="强制重标指定 sid（可多次）")
    args = ap.parse_args()

    model_dir = prepare_model(args)
    state = load_state()
    redo = set(args.redo or [])

    sid = args.start
    while sid < _num_speakers:
        if str(sid) in state and sid not in redo:
            sid += 1
            continue
        print("\n[%d/%d] sid=%d" % (sid + 1, _num_speakers, sid))
        try:
            path, secs = synth(sid, model_dir)
            print("合成 %.1fs，播放中..." % secs)
            play(path)
        except Exception as e:
            print("合成失败：%s" % e)
            if input("c=继续下一个 q=退出: ").strip().lower() == "q":
                break
            sid += 1
            continue
        res = ask_label(sid)
        if res and res.get("__quit__"):
            break
        if res and res.get("__replay__"):
            play(path)
            res = ask_label(sid)
            if res and (res.get("__quit__") or res.get("__replay__")):
                break
        if res and res.get("gender"):
            state[str(sid)] = {"sid": sid, **res}
            save_state(state)
            print("→ sid=%d: %s/%s%s" % (
                sid, res["gender"], res.get("age", ""),
                ("（" + res.get("note", "") + "）") if res.get("note") else ""))
        sid += 1

    save_state(state)
    export_app_labels(state)
    print("本次完成。已标 %d/%d 个；下次运行自动从上次进度继续。"
          % (len(state), _num_speakers))


if __name__ == "__main__":
    main()
