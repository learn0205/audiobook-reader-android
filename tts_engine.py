# -*- coding: utf-8 -*-
"""tts_engine.py —— 统一语音引擎路由器 + 本地离线 TTS（VITS）后端。

为什么需要这一层？
  原 app 只有一个「系统原生 TTS」引擎（tts_android.AndroidTTS），main.py 直接持有它。
  现在接入微软 Edge 在线 TTS（免费、无需密钥、约 14 个中文神经音色），但 Edge 的
  播放方式和系统 TTS 完全不同（先合成 mp3 文件 → 用 MediaPlayer 播放 → 播完再读下一句），
  无法直接塞进 AndroidTTS。于是这里做一层路由器：

    ReaderTTS（本模块）
      ├── _android : AndroidTTS   （系统引擎，默认，离线）
      └── _local   : LocalTTS     （本地离线合成，懒加载）

  main.py 仍然只持有唯一的 self._engine（即 ReaderTTS），调用接口与 AndroidTTS 完全一致，
  路由器根据当前所选音色的「来源」自动在 two 后端之间切换，并对 main.py 透明。

LocalTTS 后端（LocalTTS 类）要点：
  · 合成走 vits_tts（sherpa-onnx VITS，完全离线，无任何网络请求）；
  · 合成是阻塞网络 IO，放子线程；合成完回到主线程用 android.media.MediaPlayer 播放；
  · 播放推进用轮询 MediaPlayer.isPlaying() 兜底（与系统引擎一样，vivo 的回调不可靠）；
  · 全部 jnius / MediaPlayer 调用都做桌面环境降级，桌面可 import、可跑逻辑测试。
"""

import hashlib
import os
import threading
import time

# ---- 复用系统引擎的常量与句子切分工具（避免重复实现） ----
from tts_android import (STATE_STOPPED, STATE_PLAYING, STATE_PAUSED,
                         split_sentences, AndroidTTS)

try:
    from jnius import autoclass, cast, PythonJavaClass, java_method
    _JNIUS_OK = True
except Exception:                      # 桌面环境
    autoclass = None
    cast = None
    PythonJavaClass = object
    java_method = None
    _JNIUS_OK = False


if _JNIUS_OK:
    class _MainRunnable(PythonJavaClass):
        """把回调投递到安卓主线程执行（Runnable）。"""
        __javainterfaces__ = ["java/lang/Runnable"]
        __javacontext__ = "app"

        def __init__(self, fn):
            super().__init__()
            self.fn = fn

        @java_method("()V")
        def run(self):
            try:
                self.fn()
            except Exception:
                pass
else:
    class _MainRunnable(object):
        pass


if _JNIUS_OK:
    class _PlayerCompletion(PythonJavaClass):
        """MediaPlayer 播完事件监听器（OnCompletionListener 是接口，pyjnius 可实现）。

        有了它，句尾不再等 0.2s 轮询才发现「播完了」，事件一到立刻推进；
        回调从 MediaPlayer 的事件线程投递回主线程再处理。
        """
        __javainterfaces__ = ["android/media/MediaPlayer$OnCompletionListener"]
        __javacontext__ = "app"

        def __init__(self, owner, generation):
            super().__init__()
            self.owner = owner
            self.generation = generation

        @java_method("(Landroid/media/MediaPlayer;)V")
        def onCompletion(self, mp):
            owner = self.owner
            gen = self.generation
            _post_to_main(lambda: owner._on_player_complete(gen))
else:
    class _PlayerCompletion(object):   # pragma: no cover
        pass


_MAIN_HANDLER = [None]


def _post_to_main(fn):
    """把 fn 投递到「安卓主线程」执行。

    ⚠️ 关键：不能只用 Kivy 的 `Clock.schedule_once`——**熄屏后 Kivy 的逐帧时钟停摆**，
    Clock 里的回调永远不会触发。Edge 后端「合成完 → 用 MediaPlayer 播放」这一步
    原来走的是 Clock，于是熄屏后 Edge 音色会卡住不动。改用主线程 Handler，
    熄屏也能照常播放（与主程序 _tick 的做法一致）。
    """
    if _JNIUS_OK:
        try:
            if _MAIN_HANDLER[0] is None:
                _Handler = autoclass("android.os.Handler")
                _Looper = autoclass("android.os.Looper")   # ⚠️ android.os 不是 java.util
                _MAIN_HANDLER[0] = _Handler(_Looper.getMainLooper())
            _MAIN_HANDLER[0].post(_MainRunnable(fn))
            return
        except Exception:
            pass
    # ⚠️ 桌面（没 jnius）**不要再退化成 Clock.schedule_once**：
    #    那是从合成子线程往 Kivy 的 Clock 塞事件，而 Clock **不是线程安全的** ——
    #    子线程塞事件和主线程 tick() 撞上时，回调整条会被丢掉。桌面端平时看不出来，
    #    但「合成完 → 起播」和「起播前预取下一句」都走这条投递，丢了就等于预取
    #    永远不触发（CI 上的表现：`edge: 播放期间预取生效 early=1` 偶发失败）。
    #    桌面没有安卓主线程约束，直接同步调用即可（_play_file / _on_synth_error
    #    都不碰图形指令），结果完全确定。
    fn()


# ============================================================================
#  Edge 在线 TTS 后端
# ============================================================================
class LocalTTS:
    """Edge 在线语音引擎：逐句合成 mp3 → MediaPlayer 播放 → 播完推进下一句。

    公开方法与 AndroidTTS 完全一致（main.py / ReaderTTS 都按这个接口调用）。
    """
    CACHE_LIMIT = 200 * 1024 * 1024   # 磁盘缓存上限（字节），按 LRU 清理

    def __init__(self, on_progress=None, on_paragraph=None, on_state=None,
                 on_finished=None, on_error=None, on_voices=None):
        self.on_progress = on_progress or (lambda *a: None)
        self.on_paragraph = on_paragraph or (lambda *a: None)
        self.on_state = on_state or (lambda *a: None)
        self.on_finished = on_finished or (lambda: None)
        self.on_error = on_error or (lambda *a: None)
        self.on_voices = on_voices or (lambda *a: None)

        # ---- 书籍数据（与 AndroidTTS 同构） ----
        self._paragraphs = []
        self._sentences = []        # [(段落下标, 句子文本), ...]
        self._para_start = []
        self._para_last = []
        self._offsets = [0]
        self._total_chars = 0

        # ---- 播放状态 ----
        self._state = STATE_STOPPED
        self._index = 0
        self._char_pos = 0
        self._generation = 0

        # ---- 用户参数 ----
        self._speed = 1.0
        self._pitch = 0
        self._intonation = True
        self._voice_name = ""
        # 多角色朗读钩子：main.py 注入 fn(para_index, sentence_text) -> (voice, rate倍率, pitch Hz)
        # 或 None。返回 None 表示这句没有角色绑定（旁白/未识别）→ 用全局音色；
        # 整个钩子为 None 或解析抛异常时自动退回全局单音色（解析失败降级）。
        self._voice_resolver = None

        # ---- 语速校准（用于估算时长） ----
        self._cps = 4.2
        self._sent_started = 0.0
        self._cur_chars = 0
        self._seen_playing = False
        self._spoken_uid = None
        self._error_count = 0
        self._timeout_factor = 2.0

        # ---- MediaPlayer / 线程 ----
        self._player = None
        self._cache_dir = ""
        self._synth_thread = None
        self._synthesizing = False   # 当前句是否在后台合成（合成本完成前绝不按超时推进）
        # ---- 「有声进展」时间戳（卡死自检的唯一可信信号）----
        # ⚠️ (段,字符) 整句播放期间恒定、isPlaying() 在部分 ROM 上恒 false，
        #    唯一能证明「真的在出声」的是「媒体位置在前进」。
        self._progress_ts = 0.0      # 上次确认有声进展的时刻
        self._last_media_pos = -1    # 上次读到的媒体位置（毫秒）
        self._pos_frozen = 0         # 媒体位置连续没变化的 tick 数（判「播完」用）
        self._last_player_step = ""  # 起播失败时卡在哪一步（错误信息里带上）
        self._prefetch_inflight = set()   # 预取中的缓存键（去重，防止同句并发合成）
        self._ready = True           # Edge 不需要离线初始化，构造即可用
        self._init_error = ""
        self._user_data_dir = ""     # 由 _set_cache_dir 传入（缓存目录）

    # ==================== 生命周期 ====================
    def start(self):
        """无需离线初始化；缓存目录在首次合成时按 user_data_dir 创建。"""
        pass

    def is_ready(self):
        return self._ready

    def get_state(self):
        return self._state

    def is_busy(self):
        """是否在后台合成当前句（网络 IO 进行中）。

        卡死自检靠它区分两类「位置不动」：
          · True  → 正在等 Edge 返回 mp3（慢网络/抖动），是正常等待，不该判卡死；
          · False → 既没在合成也没在播，位置还不动 → 才是真卡死，应自动暂停恢复。
        """
        return bool(self._synthesizing)

    def media_position(self):
        """播放器的真实媒体位置：返回 (是否确实在出声, 毫秒)。

        ⚠️ 两个坑（都踩过）：
          1. `get_position()` 的 (段, 字符) **整句播放期间是恒定的**（只在句尾
             推进时跳一格），拿它判「有没有在动」会把长句误判成卡死；
          2. 不能只看 `isPlaying()`：部分 ROM 在正常播 mp3 时它恒为 false。
             所以判定「在出声」的条件是「**位置比上次前进了** 或 isPlaying 为真」。

        返回的毫秒位置**与第二个字段无关地原样返回** —— 调用方绝不能因为
        「playing=False」就把位置丢掉（丢掉等于退回坑 1）。位置前进时同时刷新
        `_progress_ts`，供 `progress_age()` 判断「多久没有声音进展」。
        """
        if self._player is None:
            return False, -1
        try:
            pos = int(self._player.getCurrentPosition())
        except Exception:
            return False, -1
        playing = False
        if pos != self._last_media_pos:
            # 位置在走 = 确实在出声（不依赖 isPlaying 的可靠性）
            self._last_media_pos = pos
            self._progress_ts = time.time()
            self._seen_playing = True
            playing = True
        else:
            try:
                playing = bool(self._player.isPlaying())
            except Exception:
                playing = False
        if playing:
            self._progress_ts = time.time()
        return playing, pos

    def progress_age(self):
        """距上次「有声进展」的秒数（正在合成 / 从未播放时返回 0）。

        有进展 = 媒体位置前进 / isPlaying 真 / 开始播放 / 句末推进 / 正在合成。
        卡死自检**应该只看这一个信号**：它既不会把长句误判（位置在走），
        又不会漏掉真卡死（位置、状态全冻住时它就会一直涨）。
        """
        if self._synthesizing:
            return 0.0
        if self._progress_ts <= 0:
            return 0.0
        return max(0.0, time.time() - self._progress_ts)

    def recover(self):
        """卡死自恢复：不动用断点/不切换状态，把当前句整条链路重开一遍。

        与「自动暂停」相比体验好得多：合成线程、播放器全部弃旧换新，
        相当于无感重试当前句。

        ⚠️ 顺手删掉这一句的缓存文件：命中缓存就**跳过合成**，如果那份 mp3
        本身是坏的（截断 / 只有几字节 / 静音），重开多少次都还是同一份坏文件
        —— 表现就是「卡在某一句上永远念不出声」，删掉后这次会重新联网合成。
        """
        if self._state != STATE_PLAYING:
            return
        self._generation += 1
        self._synthesizing = False
        self._stop_player()
        try:
            if self._cache_dir and self._index < len(self._sentences):
                bad = self._cache_path(self._sentences[self._index][1],
                                       params=self._voice_params_for(self._index))
                if os.path.exists(bad):
                    os.remove(bad)
        except Exception:
            pass
        self._last_media_pos = -1
        self._progress_ts = time.time()
        self._set_state(STATE_PLAYING)      # 状态不变，仅触发通知刷新兜底
        self._speak_current()

    def skip_current(self):
        """跳过当前句（自恢复连续失败时的最后手段，不会让朗读停住）。

        典型场景：这一句的音频链路怎么重开都没声（坏缓存 / 文本异常 /
        ROM 播放器对该 mp3 不认）。以前只能一直卡在这一句上，现在跳过它继续
        往下念，用户最多丢一句，朗读不会停。
        """
        if self._state != STATE_PLAYING:
            return
        self._generation += 1
        self._synthesizing = False
        self._stop_player()
        self._last_media_pos = -1
        self._progress_ts = time.time()
        self._advance()

    def get_cps(self):
        return self._cps

    def get_voices(self):
        """返回 Edge 音色列表（含 source 标记），由 ReaderTTS 负责与系统音色合并。"""
        # 真正的列表在 ReaderTTS 层合并（本地 VITS 音色）。
        # 这里只返回「已选音色名」，合并逻辑在 Router 层。
        return []

    def _set_cache_dir(self, base):
        if base:
            self._user_data_dir = base
            self._cache_dir = os.path.join(base, "tts_cache")
            try:
                os.makedirs(self._cache_dir, exist_ok=True)
            except Exception:
                pass

    def set_book_tag(self, tag):
        """当前书的缓存子目录（按书隔离，删书时可整目录清理）。"""
        self._book_tag = str(tag or "_common")

    # ==================== 书籍数据 ====================
    def load(self, paragraphs):
        self._stop_player()
        self._paragraphs = list(paragraphs)
        sentences, para_start, para_last, offsets, total = [], [], [], [0], 0
        for pi, para in enumerate(self._paragraphs):
            para_start.append(len(sentences))
            for sent in split_sentences(para):
                sentences.append((pi, sent))
            para_last.append(max(para_start[-1], len(sentences) - 1))
            total += len(para)
            offsets.append(total)
        self._sentences = sentences
        self._para_start = para_start
        self._para_last = para_last
        self._offsets = offsets
        self._total_chars = total
        self._index = 0
        self._char_pos = 0
        self._state = STATE_STOPPED

    def get_position(self):
        return self._para_index_at(self._index), self._char_pos, self._total_chars

    def _para_index_at(self, sent_index):
        if not self._sentences:
            return 0
        sent_index = max(0, min(sent_index, len(self._sentences) - 1))
        return self._sentences[sent_index][0]

    def _sent_index_of_para(self, para_index):
        if not self._para_start:
            return 0
        para_index = max(0, min(int(para_index), len(self._para_start) - 1))
        return self._para_start[para_index]

    # ==================== 参数 ====================
    def set_speed(self, value):
        self._speed = max(0.3, min(3.0, float(value)))

    def set_pitch(self, value):
        self._pitch = max(-10, min(10, int(value)))

    def set_intonation(self, enabled):
        self._intonation = bool(enabled)

    def set_voice(self, name):
        self._voice_name = str(name)

    # ==================== 多角色逐句音色 ====================
    def set_voice_resolver(self, fn):
        """注入「(段落下标, 句子文本) → (voice, rate倍率, pitch Hz)」解析器。

        fn 返回 None 或抛异常都视为「这句按全局音色读」；fn 本身为 None
        则彻底回到单音色模式（系统 TTS 后端不支持多角色，本来就不设钩子）。
        """
        self._voice_resolver = fn

    def _voice_params_for(self, sent_index):
        """某一句最终生效的合成参数 (voice, rate字符串, pitch字符串, volume)。

        无钩子 / 钩子返回 None / 钩子抛异常 → 全局参数（原行为，自动降级）。
        """
        voice = self._voice_name
        rate, pitch, volume = self._synth_params()
        try:
            resolver = self._voice_resolver
            if resolver is not None and self._sentences:
                idx = max(0, min(int(sent_index), len(self._sentences) - 1))
                para_index, sentence = self._sentences[idx]
                r = resolver(para_index, sentence)
                if r:
                    v, mult, phz = r
                    if v:
                        voice = str(v)
                    mult = max(0.5, min(2.0, float(mult)))
                    phz = max(-50, min(50, int(phz)))
                    rate = "%+d%%" % round((mult - 1.0) * 100)
                    pitch = "%+dHz" % phz
        except Exception:
            voice = self._voice_name
            rate, pitch, volume = self._synth_params()
        return voice, rate, pitch, volume

    # ==================== 播放控制 ====================
    def play(self, para_index=None):
        if self._state == STATE_PAUSED and para_index is None:
            self.resume()
            return
        if para_index is not None:
            self._index = self._sent_index_of_para(para_index)
        if self._index >= len(self._sentences):
            self._index = 0
        self._char_pos = self._offsets[self._para_index_at(self._index)]
        self._generation += 1
        self._set_state(STATE_PLAYING)
        self._speak_current()

    def pause(self):
        if self._state != STATE_PLAYING:
            return
        self._generation += 1
        self._synthesizing = False
        self._pause_player()
        self._set_state(STATE_PAUSED)

    def resume(self):
        if self._state != STATE_PAUSED:
            return
        self._generation += 1
        # 若播放器还停在上一句，直接继续；否则重新朗读当前句
        if self._player is not None:
            try:
                if _JNIUS_OK:
                    self._player.start()
                self._set_state(STATE_PLAYING)
                return
            except Exception:
                self._player = None
        self._set_state(STATE_PLAYING)
        self._speak_current()

    def stop(self):
        self._generation += 1
        self._synthesizing = False
        self._stop_player()
        self._set_state(STATE_STOPPED)

    def seek_paragraph(self, para_index):
        self._index = self._sent_index_of_para(para_index)
        self._char_pos = self._offsets[max(0, min(int(para_index), len(self._offsets) - 2))]
        self.on_progress(self._char_pos, self._total_chars)
        self.on_paragraph(self._para_index_at(self._index))
        if self._state == STATE_PLAYING:
            self._generation += 1
            self._speak_current()

    def poll_advance(self):
        """兜底推进：判断「这一句播完了没有」。

        ⚠️ 不能只问 `isPlaying()`：部分 ROM 在正常播 mp3 时它**恒为 false**。
        以前一旦这样的设备上报 false，就会被当成「播完了」立刻推进 —— 句子被
        拦腰切断、句子与音频错位。所以判定顺序改为：

          1. **位置在前进** → 确定还在出声，什么都不做；
          2. `isPlaying()` 为真 → 同上（位置粒度粗时兜住）；
          3. 位置停住且「已到 mp3 末尾」(`pos >= duration-400ms`) → 这句真播完了；
          4. 位置停住但**没到末尾** → 是卡住不是播完：不推进，交给 main 的卡死
             自检（12s 后自恢复），免得把没播完的句子切掉；
          5. 从没拿到任何「在播」信号（位置与 isPlaying 都不可信）→ 按预估时长
             超时后强制推进（最后的保底，宁可错也不停死）。
        """
        if self._state != STATE_PLAYING:
            return
        if self._spoken_uid is None:
            return
        if not _JNIUS_OK:
            # 桌面环境无播放器：纯逻辑测试用超时推进
            if time.time() - self._sent_started > self._sentence_timeout():
                self._complete_current()
            return
        # 设备上：合成中或还没拿到播放器 → 坚决不推进，等合成线程
        if self._synthesizing or self._player is None:
            return
        pos, dur = -1, -1
        try:
            pos = int(self._player.getCurrentPosition())
        except Exception:
            pos = -1
        try:
            dur = int(self._player.getDuration())
        except Exception:
            dur = -1
        # 1) 位置在前进 = 确定在出声（不依赖 isPlaying 的可靠性）
        if pos >= 0 and pos != self._last_media_pos:
            self._last_media_pos = pos
            self._pos_frozen = 0
            self._seen_playing = True
            self._progress_ts = time.time()
            return
        self._pos_frozen += 1
        # 2) isPlaying() 仍可作为补充信号
        try:
            if bool(self._player.isPlaying()):
                self._seen_playing = True
                self._progress_ts = time.time()
                self._pos_frozen = 0
                return
        except Exception:
            pass
        # 5) 从头到尾没得到「在播」信号 → 只能按预估时长兜底
        if not self._seen_playing:
            if time.time() - self._sent_started > self._sentence_timeout():
                self._timeout_factor = 1.3
                self._complete_current()
            return
        # 3)/4) 播完了，还是卡住了？
        if pos >= 0 and dur > 0:
            # 位置与时长都可信：只有走到末尾才算这句播完
            if pos < dur - 400:
                # 没到末尾又不动 → 是卡住（ROM 级卡死 / 输出设备异常），
                # 不推进、不切句子，交给 main 的卡死自检 12s 后自恢复重开本句
                return
        elif self._pos_frozen < 3:
            # 位置/时长不可靠（-1）：至少连续 3 个 tick（≈0.6s）静止才算播完，
            # 免得位置粒度粗时把还在播的句子拦腰切断
            return
        self._timeout_factor = 2.0
        self._complete_current()

    # ==================== 内部实现 ====================
    def _set_state(self, state):
        if state != self._state:
            self._state = state
            self.on_state(state)

    def _speak_current(self):
        if self._index >= len(self._sentences):
            self._finish()
            return
        para_index, sentence = self._sentences[self._index]
        if not sentence.strip():
            self._advance()
            return
        if self._index == self._sent_index_of_para(para_index):
            self._char_pos = self._offsets[para_index]
            self.on_paragraph(para_index)
            self.on_progress(self._char_pos, self._total_chars)

        self._spoken_uid = "e%d" % self._generation
        self._seen_playing = False
        self._synthesizing = True            # 合成本完成前 poll_advance 不得按超时推进
        self._progress_ts = time.time()      # 合成中即算「有进展」，卡死计时清零
        self._last_media_pos = -1
        self._pos_frozen = 0
        self._cur_chars = len(sentence)
        self._sent_started = time.time()
        gen = self._generation

        # 合成在子线程做（网络 IO），完成后再回主线程播放。
        # ⚠️ 这里必须传**原始句子字符串**：_synthesize_and_play 内部会调
        # _synth_text_for(sentence) 做换算。之前误传了 self._synth_text_for(sentence)
        # 的**返回值（元组）**，于是子线程里又换算一次 → text 变成嵌套元组
        # → 合成入口拿到元组再转义时抛
        #   "'tuple' object has no attribute 'replace'"
        # → 表现为「选中 Edge 音色提示合成失败」，而且**所有 Edge 音色都失败**。
        args = (self._index, sentence, gen)
        self._synth_thread = threading.Thread(
            target=self._synthesize_and_play, args=args, daemon=True)
        self._synth_thread.start()
        # 提前预取：当前句还没合成就先在后台合成后面几句（各自独立连接并行），
        # 比「等本句开始播放才预取」早了一个合成周期，避免短句快播时下一句来不及
        # 合成而现场等待——这是 Edge 在线语音「偶尔较长间隔」的主要原因。
        # 用 self._index（当前句）作基准；generation 变化会自行停止旧的预取。
        threading.Thread(target=self._prefetch_ahead,
                         args=(self._index, gen), daemon=True).start()

    def _synth_params(self):
        """把 app 的 speed/pitch 档位换算成 Edge 的 rate/pitch/volume 字符串。"""
        # Edge rate 范围约 -50%~+100%；pitch 档位 -10~+10 映射到 ±50Hz
        # （Edge 支持约 ±100Hz，取一半更自然、不刺耳）
        rate = "%+d%%" % round((self._speed - 1.0) * 100)
        pitch = "%+dHz" % (self._pitch * 5)
        volume = "+0%"
        return rate, pitch, volume

    def _synth_text_for(self, sentence):
        rate, pitch, volume = self._synth_params()
        return sentence, rate, pitch, volume

    def _cache_path(self, sentence, params=None):
        """句子缓存文件路径。params=(voice, rate, pitch, volume) 时按该句
        实际参数入键（不同角色的同一句话是不同的 mp3）；None 时用全局参数。"""
        if params is None:
            voice = self._voice_name
            rate, pitch, volume = self._synth_params()
        else:
            voice, rate, pitch, volume = params
        key = hashlib.md5(
            ("%s|%s|%s|%s|%s" % (voice, sentence, rate, pitch, volume)
             ).encode("utf-8")).hexdigest()
        # 按书分子目录（set_book_tag 注入，删书时整目录清理）
        sub = getattr(self, "_book_tag", "_common")
        return os.path.join(self._cache_dir or ".", sub, key + ".wav")

    def _synthesize_and_play(self, sent_index, sentence, generation):
        if generation != self._generation:
            return
        try:
            import vits_tts
            vits_tts._current_data_dir[0] = self._user_data_dir or "."
            # 多角色：这句用说话人自己的音色/语速/音调合成（无绑定则全局参数）
            params = self._voice_params_for(sent_index)
            voice, rate, pitch, volume = params
            path = self._cache_path(sentence, params=params)
            # 命中缓存则跳过合成（本地合成有音频缓存，避免重复推理）
            if not (self._cache_dir and os.path.exists(path)):
                try:
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    vits_tts.synthesize_to_file(
                        sentence, voice, path, rate=rate, pitch=pitch,
                        volume=volume)
                except Exception as serr:
                    # 本地合成失败：跳过当前片段 + 记录日志 + 继续下一段
                    # （规格要求：绝不卡死 App；连续失败由 _on_synth_error 停机）
                    self._error_count += 1
                    self.on_error("本地合成失败已跳过该句：%s" % serr)
                    _post_to_main(lambda: self._on_synth_error(generation))
                    return
                self._prune_cache(keep_path=path)
            if generation != self._generation:
                return
            # 回主线程播放（走 Handler，不用 Clock —— 熄屏后 Clock 不触发）
            _post_to_main(lambda: self._play_file(path, generation))
        except Exception as e:
            if generation != self._generation:
                return
            self._error_count += 1
            self.on_error("Edge 合成失败：%s" % e)
            _post_to_main(lambda: self._on_synth_error(generation))

    def _on_synth_error(self, generation):
        if generation != self._generation:
            return
        self._synthesizing = False
        self._spoken_uid = None
        if self._error_count >= 10:
            self.stop()
        else:
            self._advance()

    def _start_player(self, path, generation):
        """新建 MediaPlayer 并起播；失败时抛异常，由调用方决定重试 / 跳过。

        出错时把「走到哪一步」记进 `_last_player_step` —— 真机上只给一条
        `IllegalStateException` 根本分不清是 setDataSource、prepare 还是 start
        出的问题（用户截图里正是这样一条截断的错误信息）。
        """
        MediaPlayer = autoclass("android.media.MediaPlayer")
        self._last_player_step = "new"
        player = MediaPlayer()
        try:
            # setDataSource(String) 与 setDataSource(FileDescriptor) 重载并存，
            # 显式塞 java.lang.String 避免 pyjnius 选错重载（与 tts_android 同款坑）
            jpath = autoclass("java.lang.String")(path)
            self._last_player_step = "setDataSource"
            player.setDataSource(jpath)
            # 播完即时推进：MediaPlayer 播完事件走监听器（pyjnius 可实现接口），
            # 不再等 0.2s 一次的 poll_advance 轮询发现「不播了」——
            # 这个轮询延迟是句间停顿的组成部分之一。
            self._last_player_step = "setOnCompletionListener"
            self._completion_cb = _PlayerCompletion(self, generation)
            player.setOnCompletionListener(self._completion_cb)
            self._last_player_step = "prepare"
            player.prepare()       # 短 mp3，同步 prepare 可接受
            self._last_player_step = "start"
            player.start()
        except Exception:
            try:
                player.release()
            except Exception:
                pass
            raise
        self._player = player
        self._synthesizing = False
        self._seen_playing = False
        self._sent_started = time.time()
        self._spoken_uid = "e%d" % generation
        self._progress_ts = time.time()   # 起播 = 有进展
        self._last_media_pos = -1
        self._pos_frozen = 0
        self._last_player_step = ""

    def _play_file(self, path, generation):
        if generation != self._generation:
            return
        self._stop_player()
        # 预取已在 _speak_current（当前句一开始合成）时启动，这里不再重复触发；
        # 真正起播时若发现后面某句仍没缓存，推进时还有兜底合成（_synth_text_for）。
        if not _JNIUS_OK:
            # 桌面环境无播放器，直接按预估时长推进（仅用于逻辑测试）
            self._synthesizing = False
            self._seen_playing = False
            return
        try:
            self._start_player(path, generation)
            return
        except Exception as e:
            _step = self._last_player_step or "?"
        # ★ 重试一次（全新的播放器）：真机上见过「进程被系统冻结过之后，
        #   MediaPlayer 抛 IllegalStateException」——那是被冻结打断后残留的
        #   异常状态，整条重开基本就好。**不能让一句播放失败把朗读停住**。
        if generation != self._generation:
            return
        try:
            self._stop_player()
            self._start_player(path, generation)
            return
        except Exception as e2:
            self._synthesizing = False
            self._spoken_uid = None
            self.on_error("Edge 播放失败(%s→重试也失败)：%s" % (_step, e2))
            self._advance()

    def _prune_cache(self, keep_path=None):
        """磁盘缓存超过 CACHE_LIMIT 时按 LRU（mtime 最旧先删）清理。

        听长篇时每句一个 mp3 只进不出会一直涨；keep_path 是刚写入/正在
        播放的文件，绝不删除。清理失败静默忽略（不能影响朗读）。
        """
        if not self._cache_dir:
            return
        try:
            entries = []
            total = 0
            for name in os.listdir(self._cache_dir):
                fp = os.path.join(self._cache_dir, name)
                try:
                    st = os.stat(fp)
                except OSError:
                    continue
                total += st.st_size
                entries.append((st.st_mtime, fp, st.st_size))
            if total <= self.CACHE_LIMIT:
                return
            entries.sort()
            for _mtime, fp, size in entries:
                if total <= self.CACHE_LIMIT:
                    break
                if keep_path and os.path.abspath(fp) == os.path.abspath(keep_path):
                    continue
                try:
                    os.remove(fp)
                    total -= size
                except OSError:
                    pass
        except Exception:
            pass

    def _on_player_complete(self, generation):
        """本句 mp3 播完（OnCompletionListener 事件，已在主线程）。

        立即推进下一句 —— 这是消除句间停顿的关键路径；
        poll_advance 轮询保留作兜底（_spoken_uid 清空后它自然不再推进）。
        """
        if generation != self._generation or self._state != STATE_PLAYING:
            return
        self._stop_player()
        self._seen_playing = True
        if self._spoken_uid is None:
            return
        self._complete_current()

    def _prefetch_ahead(self, sent_index, generation):
        """当前句还在合成/播放时，后台并行预合成后面的若干句。

        Edge 是逐句合成 mp3：以前播完本句才发现下一句要**现合成**，
        一次网络往返（握手 + 合成 0.5~3s）全部变成了停顿 —— 这就是
        「Edge 在线语音偶尔较长间隔」的大头。预取命中后推进直接播本地
        缓存 mp3，完全不走网络、无握手开销，间隔即消失。

        改进点（与早期版本相比）：
          · 触发时机提前到 _speak_current（当前句一开始合成就并行预取），
            比「本句开始播放才预取」早了一个合成周期，短句快播也能命中；
          · 深度 1→3，且**每句独立线程并行**合成（而非串行等上一句跑完），
            避免「+1 慢 → +2/+3 全被拖慢」；
          · 单句失败只放弃该句（不再整批 return），其他句照常预取。

        任何异常都直接放弃该句（网络抖动不该刷屏，推进时还有兜底合成）。
        """
        if not (self._cache_dir and self._voice_name):
            return

        def _one(idx, sentence, path, key):
            try:
                import vits_tts
                vits_tts._current_data_dir[0] = self._user_data_dir or "."
                if generation != self._generation:
                    return
                params = self._voice_params_for(idx)
                voice, rate, pitch, volume = params
                os.makedirs(os.path.dirname(path), exist_ok=True)
                vits_tts.synthesize_to_file(
                    sentence, voice, path, rate=rate, pitch=pitch,
                    volume=volume)
                self._prune_cache(keep_path=path)
            except Exception:
                pass
            finally:
                self._prefetch_inflight.discard(key)

        for k in (1, 2, 3):
            if generation != self._generation:
                return
            idx = sent_index + k
            if idx >= len(self._sentences):
                return
            sentence = self._sentences[idx][1]
            if not sentence.strip():
                continue
            path = self._cache_path(sentence, params=self._voice_params_for(idx))
            if os.path.exists(path):
                continue
            key = os.path.basename(path)
            if key in self._prefetch_inflight:
                continue
            self._prefetch_inflight.add(key)
            # 每句一个线程并行预取（各自独立 WS 连接，互不阻塞）
            threading.Thread(
                target=_one, args=(idx, sentence, path, key), daemon=True).start()

    def _stop_player(self):
        if self._player is not None:
            try:
                self._player.stop()
                self._player.reset()
                self._player.release()
            except Exception:
                pass
            self._player = None

    def _pause_player(self):
        if self._player is not None and _JNIUS_OK:
            try:
                if bool(self._player.isPlaying()):
                    self._player.pause()
            except Exception:
                pass

    def _sentence_timeout(self):
        cps = max(0.8, self._cps) * max(0.1, self._speed)
        estimated = self._cur_chars / cps if cps > 0 else 0
        return estimated * self._timeout_factor + 1.5

    def _complete_current(self):
        if self._spoken_uid is None:
            return
        self._spoken_uid = None
        self._synthesizing = False
        self._seen_playing = False
        self._error_count = 0
        self._progress_ts = time.time()   # 句末推进 = 有进展
        self._last_media_pos = -1
        self._note_cps(self._cur_chars, time.time() - self._sent_started)
        self._advance()

    def _note_cps(self, chars, seconds):
        if seconds <= 0.25 or chars <= 0:
            return
        sample = chars / seconds
        sample = max(0.8, min(12.0, sample))
        self._cps = (1.0 - 0.25) * self._cps + 0.25 * (sample / max(0.1, self._speed))

    def _advance(self):
        if self._index < len(self._sentences):
            self._char_pos += len(self._sentences[self._index][1])
            self.on_progress(self._char_pos, self._total_chars)
        self._index += 1
        if self._index >= len(self._sentences):
            self._finish()
            return
        self._speak_current()

    def _finish(self):
        self._char_pos = self._total_chars
        self.on_progress(self._char_pos, self._total_chars)
        self._set_state(STATE_STOPPED)
        self._stop_player()
        self.on_finished()

    def shutdown(self):
        self._synthesizing = False
        self._stop_player()
        self._ready = False


# ============================================================================
#  统一路由器：对 main.py 暴露与 AndroidTTS 完全一致的接口
# ============================================================================
class ReaderTTS:
    """把「系统 TTS」和「Edge 在线 TTS」合并成一个引擎，对 main.py 透明。

    调用方（main.py）只感知一个 ReaderTTS，方法签名与 AndroidTTS 一致。
    内部根据当前所选音色的来源，把请求转发给 _android 或 _local 后端。
    """

    def __init__(self, on_progress=None, on_paragraph=None, on_state=None,
                 on_finished=None, on_error=None, on_voices=None,
                 user_data_dir=""):
        self.on_progress = on_progress or (lambda *a: None)
        self.on_paragraph = on_paragraph or (lambda *a: None)
        self.on_state = on_state or (lambda *a: None)
        self.on_finished = on_finished or (lambda: None)
        self.on_error = on_error or (lambda *a: None)
        self.on_voices = on_voices or (lambda *a: None)

        self._user_data_dir = user_data_dir
        # 直接把路由器的音色收口函数传给系统引擎：系统引擎在 _collect_voices
        # 里回调 self.on_voices（即本函数），无需再包一层子类去偷换属性。
        self._android = AndroidTTS(
            on_progress=self.on_progress,
            on_paragraph=self.on_paragraph,
            on_state=self.on_state,
            on_finished=self.on_finished,
            on_error=self.on_error,
            on_voices=self._on_android_voices,
        )
        self._local = None                 # 懒加载
        self._active = "android"
        self._paragraphs = []
        self._position = (0, 0)           # (段落, 字符) 切换后端时用于恢复
        self._voice_name = ""
        self._voice_source = "android"
        # 多角色逐句音色解析器（main.py 注入；仅 Edge 后端支持多角色）
        self._voice_resolver = None
        self._local_voices = []             # Edge 音色缓存（网络拉取后填充）
        self._merged_voices = []           # 合并后的音色列表（供 UI）

    # ---- 引擎代理：把 on_voices 收口，合并后再上报给 main.py ----
    def _on_android_voices(self, voices):
        # 系统音色补上 source 标记
        sys_voices = [dict(v, source="android") for v in voices]
        self._merge_and_report(sys_voices)

    @staticmethod
    def _mandarin_or_english(v):
        """音源白名单：只保留 普通话中文（zh-CN*）与英文（en-*）。
        系统引擎的粤语（yue-*）、其他语言音色一律不进列表。"""
        locale = str(v.get("locale", ""))
        return (locale.startswith("zh-CN") or locale.startswith("en"))

    def _merge_and_report(self, sys_voices):
        # 系统音色补上 source 标记（防御式：即便调用方没标也保证有）
        tagged = [dict(v, source="android") if "source" not in v else v
                  for v in sys_voices]
        # 统一过滤：普通话中文 + 英文（系统引擎里的粤语/其他语言剔除）
        tagged = [v for v in tagged if self._mandarin_or_english(v)]
        edge_voices = self._local_voices
        # 系统音色在前、本地离线音色在后
        merged = tagged + edge_voices
        self._merged_voices = merged
        self.on_voices(merged)

    # ==================== 生命周期 ====================
    def start(self):
        self._android.start()
        # Edge 音色是网络拉取，后台异步获取，拿到后合并进列表
        threading.Thread(target=self._build_local_voices, daemon=True).start()

    def _build_local_voices(self):
        """本地 VITS 音色列表（fanchen-C，模型导入后可用；无网络请求）。"""
        try:
            import vits_tts
            vits_tts._current_data_dir[0] = self._user_data_dir or "."
            local = []
            for name, label in vits_tts.get_instance(
                    self._user_data_dir or ".").list_speakers():
                local.append({
                    "name": name, "label": "%s（VITS·离线）" % label,
                    "locale": "zh-CN", "source": "local", "gender": "离线",
                })
            self._local_voices = local
        except Exception as e:
            self._local_voices = []
            self.on_error("本地音色列表获取失败：%s" % e)
        from kivy.clock import Clock
        Clock.schedule_once(lambda dt: self._merge_and_report(
            [dict(x, source="android") for x in self._android.get_voices()]), 0)

    def refresh_local_voices(self):
        """VITS 模型导入/下载完成后调用：重算音色列表并通知 UI。"""
        self._build_local_voices()

    def is_ready(self):
        # 当前活跃后端是否就绪；系统引擎初始化较慢，Edge 始终就绪
        if self._active == "local":
            return True
        return self._android.is_ready()

    def get_state(self):
        return self._backend().get_state()

    def is_busy(self):
        """透传当前活跃后端的「合成中」状态（供 main.py 卡死自检区分慢网络与真死锁）。"""
        return self._backend().is_busy()

    def media_position(self):
        """透传播放器真实媒体位置 (是否在播, 毫秒)；后端不支持时返回 (False, -1)。"""
        be = self._backend()
        fn = getattr(be, "media_position", None)
        return fn() if fn is not None else (False, -1)

    def recover(self):
        """卡死自恢复：让当前活跃后端把当前句整条链路重开（无感重试）。"""
        be = self._backend()
        fn = getattr(be, "recover", None)
        if fn is not None:
            fn()
        else:
            # 系统引擎没有专用 recover：从当前段重播（等价于无感重试）
            para, _c, _t = be.get_position()
            be.play(para)

    def progress_age(self):
        """距上次「有声进展」的秒数。

        ⚠️ 返回 **None** 而不是 0 表示「该后端不提供这个信号」——0 的含义是
        「刚刚还有进展」，会让卡死自检永不触发；返回 None 时 main.py 会退回
        「(段, 字符, 媒体毫秒) 是否变化」的旧判定。
        """
        be = self._backend()
        fn = getattr(be, "progress_age", None)
        if fn is None:
            return None
        try:
            return float(fn())
        except Exception:
            return None

    def skip_current(self):
        """跳过当前句（自恢复连续失败时的兜底，保证朗读不会停死在一句上）。"""
        be = self._backend()
        fn = getattr(be, "skip_current", None)
        if fn is not None:
            try:
                fn()
                return
            except Exception:
                return
        # 系统引擎：直接跳到下一段
        try:
            para, _c, _t = be.get_position()
            nxt = min(int(para) + 1, max(0, len(self._paragraphs) - 1))
            be.play(nxt)
        except Exception:
            pass

    def utter_listener_ok(self):
        """进度监听器是否成功挂上（AndroidTTS 专有；自检显示用）。"""
        return bool(getattr(self._android, "utter_listener_ok", False))

    def get_cps(self):
        return self._backend().get_cps()

    def get_voices(self):
        return self._merged_voices

    def _backend(self):
        return self._android if self._active == "android" else self._ensure_local()

    def _ensure_local(self):
        if self._local is None:
            self._local = LocalTTS(
                on_progress=self.on_progress,
                on_paragraph=self.on_paragraph,
                on_state=self.on_state,
                on_finished=self.on_finished,
                on_error=self.on_error,
                on_voices=lambda *a: None,
            )
            self._local.start()
            self._local._set_cache_dir(self._user_data_dir)
            self._local.set_speed(self._android._speed)
            self._local.set_pitch(self._android._pitch)
            self._local.set_intonation(self._android._intonation)
            self._local.set_voice(self._voice_name)
            self._local.set_voice_resolver(self._voice_resolver)
            self._local.load(self._paragraphs)
        return self._local

    # ==================== 书籍数据 ====================
    def load(self, paragraphs):
        self._paragraphs = list(paragraphs)
        self._position = (0, 0)
        self._android.load(paragraphs)
        if self._local is not None:
            self._local.load(paragraphs)

    def get_position(self):
        return self._backend().get_position()

    # ==================== 参数 ====================
    def set_speed(self, value):
        self._android.set_speed(value)
        if self._local is not None:
            self._local.set_speed(value)

    def set_pitch(self, value):
        self._android.set_pitch(value)
        if self._local is not None:
            self._local.set_pitch(value)

    def set_intonation(self, enabled):
        self._android.set_intonation(enabled)
        if self._local is not None:
            self._local.set_intonation(enabled)

    def set_voice(self, name):
        """切换音色：按 name 在合并列表里查来源，必要时切换后端并恢复进度。"""
        self._voice_name = str(name)
        src = self._voice_source_of(name)
        if src is None:
            return
        if src != self._active:
            self._switch_backend(src)
        self._backend().set_voice(name)

    def set_book_tag(self, tag):
        """当前书的缓存子目录标签（透传给本地合成后端）。"""
        self._book_tag = str(tag or "_common")
        for be in (self._android, self._local):
            if be is not None and hasattr(be, "set_book_tag"):
                be.set_book_tag(tag)

    def set_voice_resolver(self, fn):
        """注入多角色逐句音色解析器（仅本地合成后端消费；系统引擎忽略）。

        切换到本地后端时由 _ensure_local 带过去，因此先切后端再注入/
        先注入再切后端都能生效。
        """
        self._voice_resolver = fn
        if self._local is not None:
            self._local.set_voice_resolver(fn)

    def _voice_source_of(self, name):
        for v in self._merged_voices:
            if v.get("name") == name:
                return v.get("source")
        # 没在列表里（例如启动时还没拉到 Edge 列表）时，用默认 android
        return self._active

    def _switch_backend(self, target):
        """切换后端（系统引擎 ↔ Edge），并把进度原样搬过去。

        ⚠️ 切换前必须把**旧后端真正停掉**：以前只换指针就直接起播新后端，于是
        「系统引擎还在念这一句、Edge 又从同一起点念一遍」—— 用户听到**两个
        声音同时读小说**。

        ⚠️ 停旧后端时要**临时摘掉它的 on_state**：`stop()` 会广播 STOPPED，
        那会把前台服务 / 通知栏媒体卡片 / 唤醒锁整条拆掉，紧接着新后端起播又
        重建一遍（通知闪一下、锁屏媒体卡片被清掉）。切换对外是一次原子转移，
        不该让外部状态先经历一次「彻底停止」。
        """
        # 记下当前进度与播放态，把新后端载入同一本书并 seek 回去
        prev_state = self._backend().get_state()
        para, char, _ = self._backend().get_position()
        old = self._backend()
        self._active = target
        be = self._backend()              # 触发 _ensure_local（若切到 local）
        # ★ 先停旧后端（且不广播 STOPPED），再起新后端 —— 杜绝两个引擎同时发声
        try:
            if old is not None and old is not be:
                saved = getattr(old, "on_state", None)
                old.on_state = lambda *a: None
                try:
                    old.stop()
                finally:
                    if saved is not None:
                        old.on_state = saved
        except Exception:
            pass
        be.load(self._paragraphs)
        be.set_voice(self._voice_name)
        be.seek_paragraph(para)
        self._voice_source = target
        # 切换前正在朗读 → 切到新后端后从同一段继续读
        if prev_state == STATE_PLAYING:
            be.play(para)

    # ==================== 播放控制 ====================
    def play(self, para_index=None):
        self._backend().play(para_index)

    def pause(self):
        self._backend().pause()

    def resume(self):
        self._backend().resume()

    def stop(self):
        self._backend().stop()

    def seek_paragraph(self, para_index):
        self._backend().seek_paragraph(para_index)

    def poll_advance(self):
        self._backend().poll_advance()

    def shutdown(self):
        self._android.shutdown()
        if self._local is not None:
            self._local.shutdown()
