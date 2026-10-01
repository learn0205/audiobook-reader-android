# -*- coding: utf-8 -*-
"""role_parser.py —— 小说多角色识别：正则解析 + 简易上下文角色栈

目标
----
给定整本书的段落列表，输出：
  1. 出场人物清单（按首次出场排序 + 性别/年龄归类，供自动绑定音色编号）；
  2. 逐句说话人标注 {(段落下标, 句子文本): 说话人名}，供引擎逐句选音色。

识别规则（与需求一一对应）
--------------------------
· 「XXX道」类对话：引号前/后附近的「人名 + 说话动词」（说道/问/喊/…）
  → 说话人 = 该人名，压入角色栈顶。
· 代词对话：他说道 / 她说道 / 他道 / 她道…
  → 从角色栈顶向下找第一个性别匹配的人（他=男，她=女）；
    找不到匹配就退回栈顶；栈空则无说话人（当旁白）。
· 【人名 + 动作描写】单行句（如「楚子航想了想。」「夏弥低头踩过一片叶子。」）
  → 把人名存入角色栈，标记为后续对话的说话人。
  动作句里的人名即使此前未出现过也接受（凭「动作动词 + 非常见词」启发式判断），
  这是新角色出场的主要途径之一。
· 连续多段不带姓名的引号对话 → 沿用栈内最近保存的说话人（栈顶）。
· 纯旁白句子 → 不修改角色栈；只有遇到新的人名动作句 / 人名对话才更新。

实现关键：**先在整段原文上定位引号区间与说话人，再映射回切分片段**。
不能用「逐个切分句子独立分析」——split_sentences 会在引号内的句号处切开
（「你迟到了。 / 」楚子航说道。），把一对引号拆成两个"句子"，
逐句独立分析会丢失引号配对和前后置标签。

失败策略
--------
任何异常都让 analyze() 抛给调用方处理；调用方捕获后应降级为单音色朗读
（不设 resolver 即可），绝不影响正常朗读。单段解析失败只跳过该段。

对齐注意
--------
说话人标注的 key 是 (段落下标, 切分片段文本)。片段文本必须与引擎所用的
split_sentences()（tts_android）完全一致 —— 这里直接 import 同一个函数。
"""

import re

from tts_android import split_sentences

# ---------------------------------------------------------------------------
# 引号
# ---------------------------------------------------------------------------
_QUOTE_OPEN = "「『“\"‘"
_QUOTE_CLOSE = "」』”\"’"
_OPEN_TO_CLOSE = {"「": "」", "『": "』", "“": "”", '"': '"', "‘": "’"}


def _find_all_quotes(text):
    """找出文本里全部成对引号区间 [(open, close), ...]，按位置升序。

    简单非嵌套扫描：从左到右遇到开引号就找下一个同型闭引号。
    """
    spans = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch in _QUOTE_OPEN:
            close = _OPEN_TO_CLOSE[ch]
            j = text.find(close, i + 1)
            if j < 0:
                break                   # 引号不成对：后面按旁白处理
            spans.append((i, j))
            i = j + 1
        else:
            i += 1
    return spans


# ---------------------------------------------------------------------------
# 说话动词、人名标签
# ---------------------------------------------------------------------------
_VERBS = ("说道", "问道", "答道", "喊道", "叫道", "笑道", "叹道", "吼道",
          "骂道", "念道", "喝道", "急道", "忙道", "又道", "再道", "接道",
          "回道", "怒道", "低声道", "沉声道", "冷声道", "轻声道", "高声道",
          "大声道", "小声道", "开口道", "吩咐道", "命令道", "反驳道",
          "补充道", "嘀咕道", "嘟囔道", "呢喃道", "喃喃道", "追问道",
          "回答道", "强调道", "继续道", "说", "问", "答", "喊", "叫",
          "笑", "叹", "吼", "骂", "念", "喝", "道", "叮嘱", "抱怨", "催促")
_VERB_RE = re.compile(
    "(?:" + "|".join(sorted(_VERBS, key=len, reverse=True)) + ")")

# 引号前的标签：动词之前的结尾 2~4 个汉字是人名（先剥掉修饰语再取尾）
_NAME_TAIL_RE = re.compile(r"([\u4e00-\u9fa5]{2,4})$")
# 引号后的标签：动词之前的开头 2~4 个汉字是人名（其余部分须是修饰语）
_NAME_HEAD_RE = re.compile(r"^([\u4e00-\u9fa5]{2,4})")

# 代词（按性别分组）
_PRONOUN_MALE = {"他", "他们"}
_PRONOUN_FEMALE = {"她", "她们"}
_PRONOUNS = _PRONOUN_MALE | _PRONOUN_FEMALE

# 动词前常见的修饰成分（剥掉它们才能露出人名）
_ADVERBS = {
    "缓缓", "淡淡", "微微", "悄悄", "默默", "静静", "慢慢", "匆匆", "轻轻",
    "深深", "紧紧", "冷冷", "笑着", "点头", "摇头", "沉声", "冷声", "低声",
    "轻声", "高声", "大声", "小声", "厉声", "柔声", "连忙", "急忙", "顿时",
    "随即", "忽然", "突然", "接着", "然后", "终于", "半天", "片刻", "良久",
    "说道", "笑道", "想了", "想了想", "叹气", "抬头", "低头", "转身", "回头",
}
_PARTICLES = set("的地了着")

# 人名形状约束：2~4 个汉字（或 2~3 段以「·」相连的外国名），首字不能是虚词
_BAD_NAME_HEAD = set("很太更最又再就都也还被把将向从在是有没不这那哪每某各另其本该第和与或但")
_NAME_RE = re.compile(r"^[\u4e00-\u9fa5]{2,4}$|^[\u4e00-\u9fa5]{1,3}(?:·[\u4e00-\u9fa5]{1,3}){1,2}$")

# 动作句：「人名 + 动作描写」文本（无引号；允许少量逗号）
_ACTION_RE = re.compile(r"^([\u4e00-\u9fa5·]{2,4})([\u4e00-\u9fa5，,、\s]{1,24})[。！？…～\s]*$")
_ACTION_VERB_RE = re.compile(
    "(想|说|看|望|盯|瞧|扫|瞄|走|跑|冲|跳|站|坐|蹲|跪|躺|靠|倚|笑|哭|叹|点|摇|抬|低|转|回|挥|指|摸|挠|"
    "咬|抿|眨|握|拿|放|推|拉|接|递|皱|眯|耸|拍|敲|翻|合|收|掏|抓|扶|背|扛|拎|提|喝|吃|嚼|咽|听|等|"
    "停|顿|沉|迈|跨|绕|退|跟|带|领|挡|拦|躲|避|闪|伸|缩|僵|颤|抖|扬|垂|歪|偏|凑|俯|仰|弯|挪|踱|"
    "点头|摇头|起身|抬头|低头|开口|深吸|吐气|呼气|喘|愣|怔|醒|闭|睁|耸肩|鼓掌|皱眉|挑眉)")

# 高频非人名词语（动作句 / 标签误匹配时剔除）
_COMMON_WORDS = {
    "今天", "昨天", "明天", "此时", "此刻", "突然", "顿时", "然后", "接着",
    "于是", "但是", "然而", "其实", "不过", "因为", "所以", "如果", "虽然",
    "只见", "听到", "说完", "两人", "三人", "众人", "大家", "自己", "别人",
    "对方", "声音", "气氛", "空气", "时间", "周围", "四周", "心中", "心里",
    "脑海", "眼前", "身后", "身旁", "身边", "半晌", "片刻", "许久", "很久",
    "一时", "这时", "那时", "这里", "那里", "哪里", "什么", "怎么", "为何",
    "原来", "终于", "依然", "仍然", "依旧", "已经", "曾经", "正在", "刚刚",
    "刚才", "马上", "立刻", "房间", "教室", "客厅", "门外", "窗外", "屋里",
    "屋外", "楼上", "楼下", "手中", "口中", "脸上", "头上", "身上", "车里",
    "嘴里", "一旁", "对面", "前面", "后面", "里面", "外面", "上面", "下面",
    "远处", "近处", "一眼", "一声", "一步", "半天", "少年", "少女", "男人",
    "女人", "老头", "老者", "大叔", "阿姨", "孩子", "大人", "老板", "老师",
    "医生", "警察", "司机", "他们", "她们", "我们", "你们", "咱们", "地点",
}

# 性别 / 年龄归类线索（与姓名同段共现时投票）
_FEMALE_HINTS = ("女孩", "少女", "姑娘", "小姐", "女人", "女子", "女生",
                 "阿姨", "夫人", "太太", "女士", "姐姐", "妹妹")
_MALE_HINTS = ("男孩", "少年", "男人", "男子", "男生", "大叔", "大哥",
               "兄弟", "先生", "青年", "小子")
_OLD_FEMALE_HINTS = ("奶奶", "婆婆", "老太太", "姥姥", "外婆", "老妇人",
                     "大妈", "大婶")
_OLD_MALE_HINTS = ("爷爷", "老爷子", "老者", "老头", "伯父", "中年",
                   "大爷", "大叔", "叔叔", "老伯")

STACK_LIMIT = 12          # 角色栈最深保留人数


def _is_plausible_name(name: str) -> bool:
    """形状上像人名：2~4 汉字 / 带·的外国名，排除虚词开头与高频词。"""
    if not name or name in _PRONOUNS or name in _COMMON_WORDS:
        return False
    if not _NAME_RE.match(name):
        return False
    if name[0] in _BAD_NAME_HEAD:
        return False
    # 全部由单字代词/虚词构成的（如「的话」「就是」）直接排除
    if all(ch in "的地得了他她它你我就是也是有着在和与把将被很太" for ch in name):
        return False
    return True


def _strip_tail_decorations(prefix):
    """把「楚子航点点头，缓缓」剥成「楚子航」：反复去掉结尾的标点/修饰语/助词。"""
    for _ in range(8):
        if not prefix:
            break
        if prefix[-1] in "，。、！？：；\u3000 \t":
            prefix = prefix[:-1]
            continue
        if prefix[-1] in _PARTICLES:
            prefix = prefix[:-1]
            continue
        stripped = False
        for adv in _ADVERBS:
            if prefix.endswith(adv):
                prefix = prefix[:-len(adv)]
                stripped = True
                break
        if not stripped:
            break
    return prefix


def _tag_before_quote(text, open_idx):
    """在引号前的窗口里找「人名/代词 + 动词」（如：楚子航沉声道：「…」）。

    取引号前最多 16 字的窗口，找**第一个**说话动词（它前面通常就是
    说话人），动词前剥掉修饰语后取结尾 2~4 个汉字当人名。
    返回人名/代词或 None。
    """
    window = text[max(0, open_idx - 16):open_idx]
    m = _VERB_RE.search(window)
    if not m:
        return None
    prefix = _strip_tail_decorations(window[:m.start()])
    if not prefix:
        return None
    # 代词（他说道 / 她们喊道 …）：先于普通人名检查
    if prefix[-2:] in ("他们", "她们"):
        return prefix[-2:]
    if prefix[-1] in ("他", "她"):
        return prefix[-1]
    nm = _NAME_TAIL_RE.search(prefix)
    if not nm:
        return None
    return nm.group(1)


def _tag_after_quote(text, close_idx):
    """在引号后的窗口里找「人名/代词 + 动词」（如：……。」夏弥说）。"""
    window = text[close_idx + 1:close_idx + 16]
    m = _VERB_RE.search(window)
    if not m:
        return None
    head = window[:m.start()]
    # 代词开头（……。”他说。）
    if head[:2] in ("他们", "她们"):
        return head[:2]
    if head[:1] in ("他", "她"):
        return head[:1]
    nm = _NAME_HEAD_RE.match(head)
    if not nm:
        return None
    # 开头 2~4 个汉字里挑人名：剩余部分必须是修饰语（夏弥笑着→夏弥）
    cand = nm.group(1)
    for length in (2, 3, 4):
        if length > len(cand):
            break
        rest = cand[length:]
        if rest == "" or rest in _ADVERBS or all(
                ch in _PARTICLES for ch in rest):
            return cand[:length]
    return None


class _Registry:
    """出场人物登记表：按首次出场排序 + 性别/年龄投票。"""

    def __init__(self):
        self.order = []           # [人名]，按首次登记顺序
        self.gender_votes = {}    # 人名 -> [男票, 女票]
        self.age_votes = {}       # 人名 -> {"old_f": n, "old_m": n}

    def register(self, name):
        if name not in self.gender_votes:
            self.gender_votes[name] = [0, 0]
            self.age_votes[name] = {"old_f": 0, "old_m": 0}
            self.order.append(name)

    def vote(self, name, context_text):
        """姓名与代词/称谓同段共现 → 给该人物投性别/年龄票。

        证据强度：称谓在人名里（老太太 / 王大爷）> 称谓同段共现 >
        人名邻近 ±12 字的代词（他/她）。代词只看邻近范围 —— 整段里
        出现的「她」多半指的是别人，全段投票会把男生误判成女生。
        """
        votes = self.gender_votes.get(name)
        if votes is None:
            return
        for w in _OLD_FEMALE_HINTS:
            if w in name:
                self.age_votes[name]["old_f"] += 2
                break
        for w in _OLD_MALE_HINTS:
            if w in name:
                self.age_votes[name]["old_m"] += 2
                break
        for w in _OLD_FEMALE_HINTS:
            if w in context_text:
                self.age_votes[name]["old_f"] += 1
                break
        for w in _OLD_MALE_HINTS:
            if w in context_text:
                self.age_votes[name]["old_m"] += 1
                break
        idx = context_text.find(name)
        if idx >= 0:
            # 只统计邻近的「她」：男是默认值，「他」的邻近共现大多是
            # 指别人的宾语（「夏弥看了他一眼」），投男票只会帮倒忙
            window = context_text[max(0, idx - 12): idx + len(name) + 12]
            if "她" in window:
                votes[1] += 1
        for w in _FEMALE_HINTS:
            if w in context_text:
                votes[1] += 2
                break
        for w in _MALE_HINTS:
            if w in context_text:
                votes[0] += 2
                break

    def vote_pronoun(self, name, gender):
        """代词标签（他说道/她说道）解析出的说话人 → 强性别票。"""
        votes = self.gender_votes.get(name)
        if votes is None:
            return
        votes[0 if gender == "male" else 1] += 2

    def category(self, name):
        """归类：奶奶 / 中年叔叔 / 女角色 / 男角色（默认男）。"""
        votes = self.gender_votes.get(name, [0, 0])
        ages = self.age_votes.get(name, {"old_f": 0, "old_m": 0})
        if ages["old_f"] >= 2 and votes[1] >= votes[0]:
            return "奶奶"
        if ages["old_m"] >= 2 and votes[0] >= votes[1]:
            return "中年叔叔"
        if votes[1] > votes[0]:
            return "女角色"
        return "男角色"

    def gender(self, name):
        v = self.gender_votes.get(name, [0, 0])
        return "female" if v[1] > v[0] else "male"

    def result(self):
        return [(name, self.category(name)) for name in self.order]


class _Stack:
    """上下文角色栈：栈顶 = 最近一次确认的说话人。"""

    def __init__(self, registry):
        self.items = []
        self.registry = registry

    def push(self, name):
        if name in self.items:
            self.items.remove(name)
        self.items.append(name)
        if len(self.items) > STACK_LIMIT:
            self.items.pop(0)

    def top(self):
        return self.items[-1] if self.items else None

    def resolve_pronoun(self, pronoun):
        """他说 → 从栈顶向下找第一个男性；她 → 女性；都没有 → 栈顶。"""
        want = "male" if pronoun in _PRONOUN_MALE else "female"
        for name in reversed(self.items):
            if self.registry.gender(name) == want:
                return name
        return self.top()


def _apply_tag(token, registry, stack):
    """处理对话标签 token（人名或代词），返回该引号的说话人。"""
    if not token:
        return None
    if token in _PRONOUNS:
        # 代词：从角色栈解析（性别匹配优先）；解析出的人就是当前说话人，
        # 压到栈顶，后续不带姓名的对话继续沿用他/她。
        # 「她说道」本身是强女性证据 → 给解析出的人投女票（男是默认值，
        # 「他」不投票 —— 第一遍性别未定时投男票会形成自我强化的误判）。
        speaker = stack.resolve_pronoun(token)
        if speaker:
            stack.push(speaker)
            if token in _PRONOUN_FEMALE:
                registry.vote_pronoun(speaker, "female")
        return speaker
    if not _is_plausible_name(token):
        return None
    registry.register(token)
    stack.push(token)
    return token


# 动作句人名里不该出现的字（防止「他知道王」「上山的路」这类误判）
_ACTION_NAME_BLACKLIST = set("说道问答题想看走来的了吗呢吧嘛啊呀的很是有着在和与或但被把就点")


def _maybe_action_name(segment, registry, stack):
    """旁白片段若匹配「人名 + 动作描写」→ 登记人名并入栈。返回人名或 None。

    名字取 2~4 字，从长到短拆分，取「余下部分含动作动词」的最长拆法：
    「楚子航推开…」→楚子航（3字），「夏弥低头踩过…」→夏弥（2字）。
    """
    seg = segment.strip()
    if not seg or len(seg) > 40:
        return None
    m = _ACTION_RE.match(seg)
    if not m or "：" in segment or ":" in segment:
        return None
    head, tail = m.group(1), m.group(2)
    for length in (4, 3, 2):
        if length > len(head):
            continue
        name, rest = head[:length], head[length:] + tail
        if not _is_plausible_name(name):
            continue
        if any(ch in _ACTION_NAME_BLACKLIST for ch in name):
            continue
        if not _ACTION_VERB_RE.search(rest):
            continue
        registry.register(name)
        stack.push(name)                 # 标记为后续对话的说话人
        return name
    return None


def _process_paragraph(pi, para, registry, stack, speakers):
    """在整段原文上定位引号与说话人，再把说话人映射回切分片段。"""
    frags = split_sentences(para)
    if not frags:
        return
    # 1) 切分片段在原文中的位置（split_sentences 只是拆分/过滤纯标点片段，
    #    保留的片段内容与原文逐字一致，可以 find 对齐）
    frag_spans = []
    pos = 0
    for f in frags:
        idx = para.find(f, pos)
        if idx < 0:
            idx = pos
        frag_spans.append((idx, idx + len(f)))
        pos = idx + len(f)

    # 2) 找出全部引号区间，逐个确定说话人（按位置顺序处理，
    #    旁白动作句在遇到引号前先入栈，保证「楚子航点点头，「走吧。」」正确归人）
    quotes = _find_all_quotes(para)
    quote_speakers = []
    touched = []                    # 本段出现的候选人物（性别/年龄投票用）
    cur = 0
    for (o, c) in quotes:
        act = _maybe_action_name(para[cur:o], registry, stack)
        if act:
            touched.append(act)
        token = _tag_before_quote(para, o)
        if token is None:
            token = _tag_after_quote(para, c)
        spk = _apply_tag(token, registry, stack)
        if spk is not None:
            touched.append(spk)
        else:
            # 引号对话没有识别出人名/代词标签 → 沿用栈顶（连续对话规则）
            spk = stack.top()
        quote_speakers.append(spk)
        cur = c + 1
    # 动作句只认「全段旁白」或段首（引号前的部分在循环里已处理）。
    # 引号**后面**的尾巴绝不能再当动作句 —— 那里是「夏弥笑着回答」这类
    # 标签文本，会把「夏弥笑」当成新人物入栈。
    if not quotes:
        act = _maybe_action_name(para[cur:], registry, stack)
        if act:
            touched.append(act)

    # 3) 切分片段 → 说话人：与某个引号区间相交（且片段起点在对话结束前）
    #    的片段用该引号的说话人；其余片段是旁白，不进表。
    for (fs, fe), frag in zip(frag_spans, frags):
        spk = None
        for (o, c), sp in zip(quotes, quote_speakers):
            if fs < c and fe > o:
                spk = sp
                break
        if spk:
            speakers[(pi, frag)] = spk

    # 4) 性别/年龄投票：本段出现过的人物，用整段文本里的代词/称谓投票
    #    （只投本段出现过的少数人物，避免全书 O(段×人名) 的扫描）
    for name in set(touched):
        if name and name in para:
            registry.vote(name, para)


def analyze(paragraphs):
    """扫描整本书（两遍）。

    返回 (characters, speakers)：
      characters = [(人名, 类别), ...] 按首次出场排序；
      speakers   = {(段落下标, 切分片段): 说话人名}，旁白片段不进表。

    为什么两遍：代词「他说道/她说道」的性别消歧依赖**全书**的投票结果
    （夏弥是女这件事可能到很后面才有「她」的证据）。第一遍只登记人物、
    收集性别/年龄票；第二遍人物性别已定，再生成最终说话人标注。
    """
    registry = _Registry()

    # 第一遍：登记 + 投票（说话人结果丢弃）
    probe_stack = _Stack(registry)
    probe_speakers = {}
    for pi, para in enumerate(paragraphs):
        try:
            _process_paragraph(pi, para, registry, probe_stack, probe_speakers)
        except Exception:
            continue

    # 第二遍：性别已定 → 生成最终逐句说话人
    stack = _Stack(registry)
    speakers = {}
    for pi, para in enumerate(paragraphs):
        try:
            _process_paragraph(pi, para, registry, stack, speakers)
        except Exception:
            # 单段解析失败不影响全书：跳过该段（相当于当旁白处理）
            continue

    return registry.result(), speakers
