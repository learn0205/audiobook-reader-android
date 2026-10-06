# -*- coding: utf-8 -*-
"""F0 基频分析：对 187 个试听 wav 做说话人性别/年龄段声学判定。

原理：成人男声基频约 85-180Hz，女声约 165-255Hz；FFT 自相关逐帧提取
基频，取有声帧的中位数作为该音色的代表 F0，按声学区间分类。
"""
import os
import sys
import wave
import json
import numpy as np

FOLDER = r"C:\Users\houxinle\Desktop\vits试听"
FS = 16000
FRAME = 1024          # 64ms
HOP = 320             # 20ms
F0_MIN, F0_MAX = 60, 500
LAG_MIN, LAG_MAX = FS // F0_MAX, FS // F0_MIN   # 32 .. 266

def analyze_wav(path):
    with wave.open(path, "rb") as w:
        n = w.getnframes()
        pcm = np.frombuffer(w.readframes(n), dtype=np.int16).astype(np.float64)
        fs = w.getframerate()
    if len(pcm) < FRAME:
        return None
    frames = []
    for i in range(0, len(pcm) - FRAME, HOP):
        frames.append(pcm[i:i + FRAME])
    frames = np.array(frames)                       # (N, 1024)
    # 去直流 + 汉宁窗
    frames = frames - frames.mean(axis=1, keepdims=True)
    win = np.hanning(FRAME)
    frames = frames * win
    rms = np.sqrt((frames ** 2).mean(axis=1))
    if rms.max() <= 0:
        return None
    voiced_rms = rms[rms > rms.max() * 0.15]        # 能量门限：跳过静音帧
    # FFT 自相关（仅对能量前 60% 的帧，够稳且快）
    order = np.argsort(rms)[::-1][:max(1, int(len(frames) * 0.6))]
    sel = frames[order]
    spec = np.fft.rfft(sel, axis=1)
    ac = np.fft.irfft(spec * np.conj(spec), axis=1)  # (N, FRAME)
    ac = ac[:, :LAG_MAX + 1]
    norm = ac[:, 0].copy()
    norm[norm == 0] = 1
    ac = ac / norm[:, None]
    f0s = []
    for row, r in zip(ac, rms[order]):
        if r < voiced_rms.max() * 0.3:
            continue
        seg = row[LAG_MIN:LAG_MAX + 1]
        k = int(np.argmax(seg))
        if seg[k] < 0.30:                            # 相关峰太弱 = 无声/噪音
            continue
        lag = LAG_MIN + k
        # 抛物线插值细化
        if 0 < k < len(seg) - 1:
            a, b, c = seg[k - 1], seg[k], seg[k + 1]
            denom = a - 2 * b + c
            if denom != 0:
                lag += 0.5 * (a - c) / denom
        f0s.append(fs / lag)
    if len(f0s) < 10:
        return None
    f0s = np.array(f0s)
    f0s = f0s[(f0s > 60) & (f0s < 500)]
    if len(f0s) < 10:
        return None
    return {"median": float(np.median(f0s)),
            "p25": float(np.percentile(f0s, 25)),
            "p75": float(np.percentile(f0s, 75)),
            "n": int(len(f0s))}


def classify(m):
    """返回 (性别, 年龄倾向, 置信)。"""
    if m < 150:
        g = "男"
    elif m >= 180:
        g = "女"
    else:
        # 中间模糊带：看分布重心
        g = "男" if m < 168 else "女"
    conf = "高" if (m < 138 or m > 205) else "中"
    if g == "男":
        age = "中年" if m < 118 else "青年"
    else:
        age = "少女" if m >= 240 else ("中年" if m < 175 else "青年")
    return g, age, conf


def main():
    out_rows = []
    for sid in range(187):
        path = os.path.join(FOLDER, "%03d.wav" % (sid + 1))
        if not os.path.isfile(path):
            continue
        r = analyze_wav(path)
        if r is None:
            out_rows.append((sid, None, "?", "?", "?", 0))
            continue
        g, age, conf = classify(r["median"])
        out_rows.append((sid, r["median"], g, age, conf, r["n"]))
    # 汇总
    males = [(sid, m) for sid, m, g, a, c, n in out_rows if g == "男"]
    females = [(sid, m) for sid, m, g, a, c, n in out_rows if g == "女"]
    unknown = [(sid,) for sid, m, g, a, c, n in out_rows if g == "?"]
    males.sort(key=lambda x: x[1])          # 低音 → 高音
    females.sort(key=lambda x: x[1])
    print("男 %d | 女 %d | 无法分析 %d" % (len(males), len(females), len(unknown)))
    print("\n—— 男声（按基频从低到高：低=更沧桑/中年，高=更年轻）——")
    for sid, m in males:
        row = next(r for r in out_rows if r[0] == sid)
        print("sid=%-3d (%03d.wav)  F0=%3.0fHz  男·%s  置信%s"
              % (sid, sid + 1, m, row[3], row[4]))
    print("\n—— 女声（按基频从低到高：低=成熟，高=清亮/少女）——")
    for sid, m in females:
        row = next(r for r in out_rows if r[0] == sid)
        print("sid=%-3d (%03d.wav)  F0=%3.0fHz  女·%s  置信%s"
              % (sid, sid + 1, m, row[3], row[4]))
    if unknown:
        print("\n无法分析：", unknown)
    # 产出：自动标注 labels（性别）+ 全量表
    labels = {}
    detail = []
    for sid, m, g, age, conf, n in out_rows:
        if g in ("男", "女"):
            labels[str(sid)] = g
            detail.append({"sid": sid, "f0": round(m or 0), "gender": g,
                           "age": age, "confidence": conf})
    json.dump({"voice_labels": labels, "detail": detail},
              open(os.path.join(FOLDER, "自动识别结果.json"), "w",
                   encoding="utf-8"), ensure_ascii=False, indent=1)
    print("\n已生成：%s\\自动识别结果.json（labels + 全量明细）" % FOLDER)


if __name__ == "__main__":
    main()
