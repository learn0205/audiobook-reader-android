# -*- coding: utf-8 -*-
"""tts_android.py —— 安卓原生 TTS 封装（pyjnius 调用 android.speech.tts.TextToSpeech）

与桌面版（Windows SAPI5）的关键差异
-----------------------------------
1. 安卓 TextToSpeech 必须在"带 Looper 的线程"上创建和调用，实际上就是主线程。
   因此本模块所有 TTS 调用都必须在 Kivy 主线程执行，不用子线程。
2. 安卓 TTS **没有原生暂停**。本模块用"逐句朗读 + 记住句子下标"来实现暂停/续读，
   好处是进度可以精确到句，跳转响应也快。
3. 音色来自系统已安装的 TTS 引擎（Google 语音服务 / 厂商引擎），用 getVoices() 枚举。
4. 音调与语速都是浮点倍率：setPitch(1.0) / setSpeechRate(1.0) 为正常值。
   音调有效范围约 0.5~2.0，语速约 0.5~2.0（超出部分系统可能自行截断）。
5. 语调起伏：安卓没有 SSML 音调标签，改成"每句单独设置 pitch"，
   按句子类型（陈述/疑问/感叹）与段内位置生成抑扬顿挫的轮廓。

本模块在非安卓环境下 import 也不会报错（jnius 不可用时自动进入"未就绪"状态），
方便在电脑上做语法检查与逻辑测试。
"""

import random
import re
import time

# ---- 尝试导入 pyjnius（仅安卓打包后存在） ----
try:
    from jnius import autoclass, cast, PythonJavaClass, java_method
    _JNIUS_OK = True
    _JNIUS_ERR = ""
except Exception as _e:                      # 桌面环境：不影响模块导入
    autoclass = None
    cast = None
    PythonJavaClass = object
    java_method = None
    _JNIUS_OK = False
    _JNIUS_ERR = str(_e)

# ---- 播放状态 ----
STATE_STOPPED = "stopped"
STATE_PLAYING = "playing"
STATE_PAUSED = "paused"

# 句末标点：用于把段落切成"逐句朗读"的单位（标点保留在句尾）
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？!?…；;])")

# 音调倍率的可调范围（用户滑块 -10~+10 映射到这里）
PITCH_MIN = 0.70
PITCH_MAX = 1.45

# 语调起伏的强度：句子类型带来的偏移（单位是音调倍率，0.10 约合 1.7 个半音）
_CONTOUR_BY_PUNCT = {
    "？": +0.11, "?": +0.11,      # 疑问句：句尾上扬
    "！": +0.09, "!": +0.09,      # 感叹句：整体偏高
    "…": -0.04,                   # 省略号：拖沓下沉
    "；": +0.03, ";": +0.03,      # 分号：未完待续，略提
    "，": +0.05, ",": +0.05,      # 逗号：句中停顿，略提
    "。": -0.05, ".": -0.05,      # 句号：收尾下落
}
# 段内音调整体衰减幅度（读得越靠后越沉，模拟自然语气下倾）
_CONTOUR_DECLINE = 0.10
# 每句的随机微扰幅度，避免机械感
_CONTOUR_JITTER = 0.03

# 语速校准：实测"倍速 1.0 时每秒朗读多少字"的初始经验值，
# 随后按实际朗读时长用指数滑动平均不断逼近真实值（不同引擎/音色差异很大）。
BASE_CPS = 4.2
_CPS_SAMPLE_MIN = 0.8          # 单次采样下限，防止个别句子抖动
_CPS_SAMPLE_MAX = 12.0         # 单次采样上限
_CPS_ALPHA = 0.25              # 新样本权重：越大跟得越快、越不稳


def split_sentences(text: str, max_len: int = 60) -> list:
    """把一段文本切成句子列表；过长的句子再按最大长度硬切。"""
    pieces = [p for p in _SENTENCE_SPLIT_RE.split(text) if p.strip()]
    if not pieces:
        return [text] if text.strip() else []
    out = []
    for piece in pieces:
        while len(piece) > max_len:
            out.append(piece[:max_len])
            piece = piece[max_len:]
        if piece:
            out.append(piece)
    return out


class AndroidTTS:
    """安卓 TTS 播放器：逐句朗读，支持暂停/续读/跳段/语速/音调/音色/语调起伏。

    所有公开方法都必须在主线程调用（安卓 TTS 的硬性要求）。
    回调也都发生在主线程，UI 可以直接更新。
    """

    def __init__(self, on_progress=None, on_paragraph=None, on_state=None,
                 on_finished=None, on_error=None, on_voices=None):
        # ---- 回调（全部在主线程触发） ----
        self.on_progress = on_progress or (lambda pos, total: None)
        self.on_paragraph = on_paragraph or (lambda index: None)
        self.on_state = on_state or (lambda state: None)
        self.on_finished = on_finished or (lambda: None)
        self.on_error = on_error or (lambda msg: None)
        self.on_voices = on_voices or (lambda voices: None)

        # ---- 书籍数据 ----
        self._paragraphs = []
        self._sentences = []        # [(段落下标, 句子文本), ...] 扁平化后的朗读单位
        self._para_start = []       # 每段第一句在 _sentences 中的下标
        self._para_last = []        # 每段最后一句在 _sentences 中的下标
        self._offsets = [0]         # 每段起始字符偏移（前缀和），用于进度换算
        self._total_chars = 0

        # ---- 播放状态 ----
        self._state = STATE_STOPPED
        self._index = 0             # 当前朗读到第几句
        self._char_pos = 0          # 当前全书字符进度
        self._generation = 0        # 代际号：stop/seek 后旧回调自动失效

        # ---- 用户参数 ----
        self._speed = 1.0
        self._pitch = 0             # -10 ~ +10
        self._intonation = True     # 语调起伏开关
        self._voice_name = ""

        # ---- 语速实测校准（用于估算剩余/总时长） ----
        self._cps = BASE_CPS        # 倍速 1.0 下每秒朗读字数
        self._sent_started = 0.0    # 当前句子开始发声的时刻
        self._error_count = 0       # 朗读失败次数（用于收敛错误提示）

        # ---- Java 对象 ----
        self._tts = None
        self._ready = False
        self._init_error = ""
        self._voices = []           # [{"name":..., "label":..., "locale":...}]
        self._init_listener = None
        self._utter_listener = None

        if not _JNIUS_OK:
            self._init_error = f"pyjnius 不可用（{_JNIUS_ERR}）"

    # ==================== 生命周期 ====================
    def start(self):
        """创建 TextToSpeech 实例并异步初始化（必须主线程调用）。"""
        if not _JNIUS_OK:
            self.on_error("当前环境没有 pyjnius，无法使用安卓语音引擎。")
            return
        try:
            TextToSpeech = autoclass("android.speech.tts.TextToSpeech")
            PythonActivity = autoclass("org.kivy.android.PythonActivity")
            activity = PythonActivity.mActivity

            self._init_listener = _InitListener(self)
            self._tts = TextToSpeech(activity, self._init_listener)
            self._utter_listener = _UtteranceListener(self)
        except Exception as err:
            self._init_error = f"初始化安卓语音引擎失败：{err}"
            self.on_error(self._init_error)

    def _on_init_done(self, status):
        """TextToSpeech 初始化回调（status=0 表示成功）。"""
        if status != 0:
            self._init_error = "系统语音引擎初始化失败，请到「设置 → 语言和输入 → 文字转语音」检查是否已安装语音数据。"
            self.on_error(self._init_error)
            return
        try:
            self._tts.setOnUtteranceProgressListener(self._utter_listener)
            self._apply_language()
            self._ready = True
            self._collect_voices()
        except Exception as err:
            self._init_error = f"语音引擎就绪后配置失败：{err}"
            self.on_error(self._init_error)

    def _apply_language(self):
        """优先中文；失败则退回系统默认语言。"""
        try:
            Locale = autoclass("java.util.Locale")
            result = self._tts.setLanguage(Locale.CHINESE)
            # LANG_MISSING_DATA=-1 / LANG_NOT_SUPPORTED=-2
            if result in (-1, -2):
                self._tts.setLanguage(Locale.getDefault())
        except Exception:
            pass

    def _collect_voices(self):
        """枚举系统已安装的音色，生成给 UI 用的列表。"""
        voices = []
        try:
            raw = self._tts.getVoices()
            iterator = raw.iterator()
            while iterator.hasNext():
                voice = iterator.next()
                name = str(voice.getName())
                locale = voice.getLocale()
                lang = str(locale.getLanguage())
                country = str(locale.getCountry())
                label = self._voice_label(name, lang, country)
                voices.append({"name": name, "label": label, "locale": f"{lang}-{country}"})
        except Exception as err:
            self.on_error(f"读取音色列表失败：{err}")
        # 中文音色排前面，方便挑
        voices.sort(key=lambda v: (0 if v["locale"].startswith("zh") else 1, v["label"]))
        self._voices = voices
        self.on_voices(voices)

    @staticmethod
    def _voice_label(name: str, lang: str, country: str) -> str:
        """把引擎内部音色名整理成好读的中文标签。"""
        lang_cn = {"zh": "中文", "en": "英语", "ja": "日语", "ko": "韩语"}.get(lang, lang or "其他")
        region = {"CN": "普通话", "TW": "台湾", "HK": "粤语"}.get(country, "")
        # 引擎名形如 zh-cn-x-ccc-network / cmn-cn-... ，取末段做短名
        short = name.split("-")[-1]
        short = short.replace("network", "").replace("local", "").strip("-") or name
        parts = [p for p in (lang_cn, region) if p]
        return f"{short}（{'·'.join(parts)}）"

    def shutdown(self):
        """退出前释放引擎。"""
        try:
            if self._tts is not None:
                self._tts.stop()
                self._tts.shutdown()
        except Exception:
            pass
        self._tts = None
        self._ready = False

    def is_ready(self):
        return self._ready

    def get_state(self):
        """当前播放状态：stopped / playing / paused。"""
        return self._state

    def get_cps(self):
        """实测朗读速度（倍速 1.0 下每秒字数），用于估算总时长。

        初值是经验值，随着实际朗读不断校准，越读越准。
        """
        return self._cps

    def get_voices(self):
        return list(self._voices)

    # ==================== 书籍数据 ====================
    def load(self, paragraphs: list):
        """载入新书的段落列表，重建句子索引并回到开头。"""
        self.stop()
        self._paragraphs = list(paragraphs)
        sentences = []
        para_start = []
        para_last = []
        offsets = [0]
        total = 0
        for para_index, para in enumerate(self._paragraphs):
            para_start.append(len(sentences))
            for sentence in split_sentences(para):
                sentences.append((para_index, sentence))
            # 空段也要占位，避免 para_last 与段落下标错位
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
        """返回 (当前段落下标, 全书字符进度, 全书总字符数)。"""
        return self._para_index_at(self._index), self._char_pos, self._total_chars

    def _para_index_at(self, sent_index):
        """由句子下标反查所属段落下标。"""
        if not self._sentences:
            return 0
        sent_index = max(0, min(sent_index, len(self._sentences) - 1))
        return self._sentences[sent_index][0]

    def _sent_index_of_para(self, para_index):
        """由段落下标找到它的第一句。"""
        if not self._para_start:
            return 0
        para_index = max(0, min(int(para_index), len(self._para_start) - 1))
        return self._para_start[para_index]

    # ==================== 播放控制 ====================
    def play(self, para_index=None):
        """开始播放；暂停中等价于继续。para_index 指定则从该段开头播放。"""
        if not self._ready:
            self.on_error(self._init_error or "语音引擎尚未就绪")
            return
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
        """暂停：停掉当前朗读并记住句子位置（安卓 TTS 无原生暂停）。"""
        if self._state != STATE_PLAYING:
            return
        self._generation += 1
        self._silence()
        self._set_state(STATE_PAUSED)

    def resume(self):
        """继续：从记住的句子接着读。"""
        if self._state != STATE_PAUSED:
            return
        self._generation += 1
        self._set_state(STATE_PLAYING)
        self._speak_current()

    def stop(self):
        """停止朗读，位置保留在当前句。"""
        self._generation += 1
        self._silence()
        self._set_state(STATE_STOPPED)

    def seek_paragraph(self, para_index):
        """跳转到指定段落（播放中立即生效）。"""
        self._index = self._sent_index_of_para(para_index)
        self._char_pos = self._offsets[max(0, min(int(para_index), len(self._offsets) - 2))]
        self.on_progress(self._char_pos, self._total_chars)
        self.on_paragraph(self._para_index_at(self._index))
        if self._state == STATE_PLAYING:
            self._generation += 1
            self._speak_current()

    def set_speed(self, value):
        self._speed = max(0.3, min(3.0, float(value)))
        try:
            if self._tts is not None:
                self._tts.setSpeechRate(self._speed)
        except Exception:
            pass

    def set_pitch(self, value):
        """value 为 -10 ~ +10 的档位（与桌面版一致），内部换算成倍率。"""
        self._pitch = max(-10, min(10, int(value)))

    def set_intonation(self, enabled: bool):
        """语调起伏开关：开则每句按句型和段内位置微调音调。"""
        self._intonation = bool(enabled)

    def set_voice(self, name: str):
        """按音色内部名切换。"""
        self._voice_name = str(name)
        if not self._ready or not name:
            return
        try:
            raw = self._tts.getVoices()
            iterator = raw.iterator()
            while iterator.hasNext():
                voice = iterator.next()
                if str(voice.getName()) == name:
                    self._tts.setVoice(voice)
                    break
        except Exception as err:
            self.on_error(f"切换音色失败：{err}")

    # ==================== 内部实现 ====================
    def _set_state(self, state):
        if state != self._state:
            self._state = state
            self.on_state(state)

    def _silence(self):
        """立刻停掉当前朗读（并丢弃队列）。"""
        try:
            if self._tts is not None:
                self._tts.stop()
        except Exception:
            pass

    def _speak_current(self):
        """朗读当前句子；读完由 onDone 回调推进到下一句。"""
        if self._index >= len(self._sentences):
            self._finish()
            return
        para_index, sentence = self._sentences[self._index]
        if not sentence.strip():
            self._advance()
            return

        # 进入新段落时通知 UI（高亮 + 滚动）
        if self._index == self._sent_index_of_para(para_index):
            self._char_pos = self._offsets[para_index]
            self.on_paragraph(para_index)
            self.on_progress(self._char_pos, self._total_chars)

        try:
            if self._tts is not None:
                self._tts.setPitch(self._pitch_multiplier(para_index, self._index))
                self._tts.setSpeechRate(self._speed)
                # 记下时刻：万一引擎没回调 onStart，也能用这里兜底计时
                self._sent_started = time.time()
                self._speak_text(sentence, "s%d" % self._index)
        except Exception as err:
            self.on_error(f"朗读调用失败：{err}")
            self.stop()

    def _speak_text(self, text, utterance_id):
        """调用 speak()。

        ⚠️ 关键坑：pyjnius **无法**把 Python 的 str 自动匹配到 speak() 的
        CharSequence 重载，会直接报
            No methods called speak in android/speech/tts/TextToSpeech
            matching your arguments
        必须显式 new 一个 java.lang.String，再 cast 成 java.lang.CharSequence。
        （这是实机报错截图定位出来的，桌面环境测不到。）

        Bundle 参数在个别老版本上不接受 null，故保留一层兜底。
        """
        QUEUE_FLUSH = 0
        jstring = autoclass("java.lang.String")(text)
        seq = cast("java.lang.CharSequence", jstring)
        try:
            self._tts.speak(seq, QUEUE_FLUSH, None, utterance_id)
        except Exception:
            Bundle = autoclass("android.os.Bundle")
            self._tts.speak(seq, QUEUE_FLUSH, Bundle(), utterance_id)

    def _pitch_multiplier(self, para_index, sent_index):
        """计算这一句该用的音调倍率（1.0 为正常）。

        语调起伏开启时叠加三部分：
          · 段内下倾：越靠段落末尾越沉
          · 句型偏移：疑问上扬、感叹提高、句号收落
          · 随机微扰：避免机械感
        """
        base = PITCH_MIN + (self._pitch + 10) / 20.0 * (PITCH_MAX - PITCH_MIN)
        if not self._intonation:
            return max(0.4, min(2.0, base))

        # 1) 段内位置带来的下倾（_para_last 是预计算好的，这里是 O(1)）
        first = self._sent_index_of_para(para_index)
        last = (self._para_last[para_index]
                if para_index < len(self._para_last) else first)
        span = max(1, last - first)
        # 夹到 0~1：即便调用方传进来的下标超出本段范围，也不会让音调跑飞
        progress = max(0.0, min(1.0, (sent_index - first) / span))
        offset = -_CONTOUR_DECLINE * progress

        # 2) 句型带来的偏移
        text = self._sentences[sent_index][1].rstrip()
        if text:
            offset += _CONTOUR_BY_PUNCT.get(text[-1], 0.0)

        # 3) 随机微扰
        offset += random.uniform(-_CONTOUR_JITTER, _CONTOUR_JITTER)

        return max(0.4, min(2.0, base + offset))

    # ---- Java 回调入口（主线程） ----
    def _on_utterance_start(self, utterance_id, generation):
        """引擎真正开始发声：以这个时刻为计时起点最准。"""
        if generation != self._generation:
            return
        self._sent_started = time.time()

    def _on_utterance_done(self, utterance_id, generation):
        """某句读完：先校准语速，再推进到下一句。

        generation 不符说明已被 stop/seek 作废，直接忽略。
        """
        if generation != self._generation or self._state != STATE_PLAYING:
            return
        # 用这一句的实际耗时校准语速（暂停/跳转后 generation 变了不会走到这）
        if 0 <= self._index < len(self._sentences):
            self._note_cps(len(self._sentences[self._index][1]),
                           time.time() - self._sent_started)
        self._advance()

    def _on_utterance_error(self, utterance_id, error_code, generation):
        if generation != self._generation:
            return
        # 单句失败不该中断整本书：记一次错继续往下读。
        # 提示语故意不带句号，方便上层按内容去重，避免连续失败时刷屏。
        self._error_count += 1
        self.on_error(f"有句子朗读失败（错误码 {error_code}），已自动跳过")
        self._advance()

    def _note_cps(self, chars, seconds):
        """用实测朗读时长校准语速（指数滑动平均，越读越准）。

        引擎的 setSpeechRate 是在"自然语速"上乘一个倍率，
        所以先把实测值除以当前倍速，换算回倍速 1.0 的基准值再平滑。
        """
        if seconds <= 0.25 or chars <= 0:
            return                       # 太短的样本噪声大，丢弃
        sample = chars / seconds
        sample = max(_CPS_SAMPLE_MIN, min(_CPS_SAMPLE_MAX, sample))
        base_sample = sample / max(0.1, self._speed)
        self._cps = (1.0 - _CPS_ALPHA) * self._cps + _CPS_ALPHA * base_sample

    def _advance(self):
        """推进到下一句并继续朗读。"""
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
        self.on_finished()


# ==================== Java 接口实现 ====================
if _JNIUS_OK:

    class _InitListener(PythonJavaClass):
        """TextToSpeech.OnInitListener"""
        __javainterfaces__ = ["android/speech/tts/TextToSpeech$OnInitListener"]
        __javacontext__ = "app"

        def __init__(self, owner):
            super().__init__()
            self.owner = owner

        @java_method("(I)V")
        def onInit(self, status):
            self.owner._on_init_done(status)

    class _UtteranceListener(PythonJavaClass):
        """UtteranceProgressListener：逐句朗读的推进靠这里的 onDone。"""
        __javainterfaces__ = ["android/speech/tts/UtteranceProgressListener"]
        __javacontext__ = "app"

        def __init__(self, owner):
            super().__init__()
            self.owner = owner

        @java_method("(Ljava/lang/String;)V")
        def onStart(self, utterance_id):
            owner = self.owner
            owner._on_utterance_start(utterance_id, owner._generation)

        @java_method("(Ljava/lang/String;)V")
        def onDone(self, utterance_id):
            owner = self.owner
            owner._on_utterance_done(utterance_id, owner._generation)

        @java_method("(Ljava/lang/String;I)V")
        def onError(self, utterance_id, error_code):
            owner = self.owner
            owner._on_utterance_error(utterance_id, error_code, owner._generation)

else:                       # 桌面环境下提供同名占位，保证可导入
    class _InitListener:    # pragma: no cover
        pass

    class _UtteranceListener:   # pragma: no cover
        pass
