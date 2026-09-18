# -*- coding: utf-8 -*-
"""tts_engine.py —— 统一语音引擎路由器 + 微软 Edge 在线 TTS 后端。

为什么需要这一层？
  原 app 只有一个「系统原生 TTS」引擎（tts_android.AndroidTTS），main.py 直接持有它。
  现在接入微软 Edge 在线 TTS（免费、无需密钥、约 14 个中文神经音色），但 Edge 的
  播放方式和系统 TTS 完全不同（先合成 mp3 文件 → 用 MediaPlayer 播放 → 播完再读下一句），
  无法直接塞进 AndroidTTS。于是这里做一层路由器：

    ReaderTTS（本模块）
      ├── _android : AndroidTTS   （系统引擎，默认，离线）
      └── _edge    : EdgeTTS      （在线云端，按需懒加载）

  main.py 仍然只持有唯一的 self._engine（即 ReaderTTS），调用接口与 AndroidTTS 完全一致，
  路由器根据当前所选音色的「来源」自动在 two 后端之间切换，并对 main.py 透明。

EdgeTTS 后端（EdgeTTS 类）要点：
  · 合成走 edge_tts_client（纯标准库 WebSocket 实现，p4a 可直接打包）；
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
    try:
        from kivy.clock import Clock
        Clock.schedule_once(lambda _dt: fn(), 0)
    except Exception:
        fn()


# ============================================================================
#  Edge 在线 TTS 后端
# ============================================================================
class EdgeTTS:
    """Edge 在线语音引擎：逐句合成 mp3 → MediaPlayer 播放 → 播完推进下一句。

    公开方法与 AndroidTTS 完全一致（main.py / ReaderTTS 都按这个接口调用）。
    """

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
        self._ready = True           # Edge 不需要离线初始化，构造即可用
        self._init_error = ""

    # ==================== 生命周期 ====================
    def start(self):
        """无需离线初始化；缓存目录在首次合成时按 user_data_dir 创建。"""
        pass

    def is_ready(self):
        return self._ready

    def get_state(self):
        return self._state

    def get_cps(self):
        return self._cps

    def get_voices(self):
        """返回 Edge 音色列表（含 source 标记），由 ReaderTTS 负责与系统音色合并。"""
        # 真正的列表是网络拉取的，放在 edge_tts_client.list_voices()；
        # 这里只返回「已选音色名」，合并逻辑在 Router 层。
        return []

    def _set_cache_dir(self, base):
        if base:
            self._cache_dir = os.path.join(base, "edge_cache")
            try:
                os.makedirs(self._cache_dir, exist_ok=True)
            except Exception:
                pass

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
        """兜底推进：轮询 MediaPlayer.isPlaying()。

        ⚠️ 关键：合成本帧 mp3 是网络 IO（放子线程），在它完成之前，
        当前句还没真正开始播放。此时**绝不能**按超时把这句「判读完了」，
        否则会跳过整句、句子与音频错位。所以 `_synthesizing` 为真、
        或播放器还没拿到时，一律只等、不推进；兜底的超时推进只在
        「已经开始播过（_seen_playing）又停了」或「桌面无播放器逻辑测试」时生效。
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
        try:
            playing = bool(self._player.isPlaying())
        except Exception:
            playing = False
        if playing:
            self._seen_playing = True
            return
        if not self._seen_playing:
            # 引擎从头到尾没报告过「正在播放」（个别设备 isPlaying 恒 false）：
            # 退到第三层兜底，按预估时长超时后强制推进
            if time.time() - self._sent_started > self._sentence_timeout():
                self._timeout_factor = 1.3
                self._complete_current()
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
        self._cur_chars = len(sentence)
        self._sent_started = time.time()
        gen = self._generation

        # 合成在子线程做（网络 IO），完成后再回主线程播放。
        # ⚠️ 这里必须传**原始句子字符串**：_synthesize_and_play 内部会调
        # _synth_text_for(sentence) 做换算。之前误传了 self._synth_text_for(sentence)
        # 的**返回值（元组）**，于是子线程里又换算一次 → text 变成嵌套元组
        # → edge_tts_client 里 su.escape(tuple) 抛
        #   "'tuple' object has no attribute 'replace'"
        # → 表现为「选中 Edge 音色提示合成失败」，而且**所有 Edge 音色都失败**。
        args = (sentence, gen)
        self._synth_thread = threading.Thread(
            target=self._synthesize_and_play, args=args, daemon=True)
        self._synth_thread.start()

    def _synth_params(self):
        """把 app 的 speed/pitch 档位换算成 Edge 的 rate/pitch/volume 字符串。"""
        # Edge rate 范围约 -50%~+100%；pitch 档位 -10~+10 映射到 ±50Hz
        # （Edge 支持约 ±100Hz，取一半更自然、不刺耳）
        rate = "+%d%%" % round((self._speed - 1.0) * 100)
        pitch = "%+dHz" % (self._pitch * 5)
        volume = "+0%"
        return rate, pitch, volume

    def _synth_text_for(self, sentence):
        rate, pitch, volume = self._synth_params()
        return sentence, rate, pitch, volume

    def _cache_path(self, sentence):
        rate, pitch, volume = self._synth_params()
        key = hashlib.md5(
            ("%s|%s|%s|%s|%s" % (self._voice_name, sentence, rate, pitch, volume)
             ).encode("utf-8")).hexdigest()
        return os.path.join(self._cache_dir or ".", key + ".mp3")

    def _synthesize_and_play(self, sentence, generation):
        if generation != self._generation:
            return
        try:
            import edge_tts_client
            text, rate, pitch, volume = self._synth_text_for(sentence)
            path = self._cache_path(sentence)
            # 命中缓存则跳过网络合成
            if not (self._cache_dir and os.path.exists(path)):
                edge_tts_client.synthesize_to_file(
                    text, self._voice_name, path, rate=rate, pitch=pitch, volume=volume)
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

    def _play_file(self, path, generation):
        if generation != self._generation:
            return
        self._stop_player()
        if not _JNIUS_OK:
            # 桌面环境无播放器，直接按预估时长推进（仅用于逻辑测试）
            self._synthesizing = False
            self._seen_playing = False
            return
        try:
            MediaPlayer = autoclass("android.media.MediaPlayer")
            player = MediaPlayer()
            # setDataSource(String) 与 setDataSource(FileDescriptor) 重载并存，
            # 显式塞 java.lang.String 避免 pyjnius 选错重载（与 tts_android 同款坑）
            jpath = autoclass("java.lang.String")(path)
            player.setDataSource(jpath)
            player.prepare()       # 短 mp3，同步 prepare 可接受
            player.start()
            self._player = player
            self._synthesizing = False
            self._seen_playing = False
            self._sent_started = time.time()
            self._spoken_uid = "e%d" % generation
        except Exception as e:
            self._synthesizing = False
            self._spoken_uid = None
            self.on_error("Edge 播放失败：%s" % e)
            self._advance()

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
    内部根据当前所选音色的来源，把请求转发给 _android 或 _edge 后端。
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
        self._edge = None                 # 懒加载
        self._active = "android"
        self._paragraphs = []
        self._position = (0, 0)           # (段落, 字符) 切换后端时用于恢复
        self._voice_name = ""
        self._voice_source = "android"
        self._edge_voices = []             # Edge 音色缓存（网络拉取后填充）
        self._merged_voices = []           # 合并后的音色列表（供 UI）

    # ---- 引擎代理：把 on_voices 收口，合并后再上报给 main.py ----
    def _on_android_voices(self, voices):
        # 系统音色补上 source 标记
        sys_voices = [dict(v, source="android") for v in voices]
        self._merge_and_report(sys_voices)

    def _merge_and_report(self, sys_voices):
        # 系统音色补上 source 标记（防御式：即便调用方没标也保证有）
        tagged = [dict(v, source="android") if "source" not in v else v
                  for v in sys_voices]
        edge_voices = self._edge_voices
        # 系统音色在前、Edge 在线音色在后；各自内部已按中文优先排序
        merged = tagged + edge_voices
        self._merged_voices = merged
        self.on_voices(merged)

    # ==================== 生命周期 ====================
    def start(self):
        self._android.start()
        # Edge 音色是网络拉取，后台异步获取，拿到后合并进列表
        threading.Thread(target=self._fetch_edge_voices, daemon=True).start()

    def _fetch_edge_voices(self):
        try:
            import edge_tts_client
            raw = edge_tts_client.list_voices()
            edge = []
            for v in raw:
                locale = v.get("Locale", "")
                # 主推中文（zh-*）音色，其余也保留但排后面
                if not locale.startswith("zh"):
                    continue
                short = v.get("ShortName", "")
                # 从 ShortName 末段取好读的中文音色调名，例如
                # zh-CN-YunxiNeural -> "Yunxi"，比 FriendlyName 的 "Microsoft…" 更直观
                parts = short.split("-")
                neural = parts[-1] if parts else short
                friendly = neural.replace("Neural", "") or short
                gender = "女" if v.get("Gender") == "Female" else "男"
                label = "%s（Edge·%s·%s）" % (friendly, locale, gender)
                edge.append({
                    "name": short, "label": label, "locale": locale,
                    "source": "edge", "gender": gender,
                })
            edge.sort(key=lambda x: (0 if x["locale"].startswith("zh-CN") else 1, x["label"]))
            self._edge_voices = edge
            from kivy.clock import Clock
            Clock.schedule_once(lambda dt: self._merge_and_report(
                [dict(x, source="android") for x in self._android.get_voices()]), 0)
        except Exception as e:
            # 拉取失败不该影响系统音色；仅上报错误（去重由 main.py 处理）
            self.on_error("Edge 音色列表获取失败（可稍后重试）：%s" % e)

    def is_ready(self):
        # 当前活跃后端是否就绪；系统引擎初始化较慢，Edge 始终就绪
        if self._active == "edge":
            return True
        return self._android.is_ready()

    def get_state(self):
        return self._backend().get_state()

    def utter_listener_ok(self):
        """进度监听器是否成功挂上（AndroidTTS 专有；自检显示用）。"""
        return bool(getattr(self._android, "utter_listener_ok", False))

    def get_cps(self):
        return self._backend().get_cps()

    def get_voices(self):
        return self._merged_voices

    def _backend(self):
        return self._android if self._active == "android" else self._ensure_edge()

    def _ensure_edge(self):
        if self._edge is None:
            self._edge = EdgeTTS(
                on_progress=self.on_progress,
                on_paragraph=self.on_paragraph,
                on_state=self.on_state,
                on_finished=self.on_finished,
                on_error=self.on_error,
                on_voices=lambda *a: None,
            )
            self._edge.start()
            self._edge._set_cache_dir(self._user_data_dir)
            self._edge.set_speed(self._android._speed)
            self._edge.set_pitch(self._android._pitch)
            self._edge.set_intonation(self._android._intonation)
            self._edge.load(self._paragraphs)
        return self._edge

    # ==================== 书籍数据 ====================
    def load(self, paragraphs):
        self._paragraphs = list(paragraphs)
        self._position = (0, 0)
        self._android.load(paragraphs)
        if self._edge is not None:
            self._edge.load(paragraphs)

    def get_position(self):
        return self._backend().get_position()

    # ==================== 参数 ====================
    def set_speed(self, value):
        self._android.set_speed(value)
        if self._edge is not None:
            self._edge.set_speed(value)

    def set_pitch(self, value):
        self._android.set_pitch(value)
        if self._edge is not None:
            self._edge.set_pitch(value)

    def set_intonation(self, enabled):
        self._android.set_intonation(enabled)
        if self._edge is not None:
            self._edge.set_intonation(enabled)

    def set_voice(self, name):
        """切换音色：按 name 在合并列表里查来源，必要时切换后端并恢复进度。"""
        self._voice_name = str(name)
        src = self._voice_source_of(name)
        if src is None:
            return
        if src != self._active:
            self._switch_backend(src)
        self._backend().set_voice(name)

    def _voice_source_of(self, name):
        for v in self._merged_voices:
            if v.get("name") == name:
                return v.get("source")
        # 没在列表里（例如启动时还没拉到 Edge 列表）时，用默认 android
        return self._active

    def _switch_backend(self, target):
        # 记下当前进度与播放态，把新后端载入同一本书并 seek 回去
        prev_state = self._backend().get_state()
        para, char, _ = self._backend().get_position()
        self._active = target
        be = self._backend()              # 触发 _ensure_edge（若切到 edge）
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
        if self._edge is not None:
            self._edge.shutdown()
