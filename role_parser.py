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
          "回答道", "强调道", "继续道", "赞叹道", "夸赞道", "称赞道",
          "感叹道", "惊叹道", "咆哮道", "沉吟道", "解释道", "总结道",
          "赞叹", "夸赞", "称赞", "感叹", "惊叹", "振振有词", "言简意赅",
          # 「人名 + 动作短语 + 道」是网文最高频的标签形态，必须整体成词，
          # 否则「路鸣泽抬起头道」会被切成「路鸣泽抬起头」这种碎片人名
          "抬起头", "低下头", "点点头", "摇摇头", "侧过头", "回过头",
          "偏过头", "转过头", "挠了挠", "摸了摸", "揉了揉", "眨了眨",
          "顿了顿", "看了看", "竖起大拇指", "挑了挑眉", "皱了皱眉",
          "叹了口气", "深吸一口气", "深吸口气", "应了一声",
          "微笑", "冷笑", "大笑", "傻笑", "干笑", "苦笑", "怒喝",
          "说", "问", "答", "喊", "叫", "笑", "叹", "吼", "骂", "念",
          "喝", "道", "叮嘱", "抱怨", "催促")
_VERB_RE = re.compile(
    "(?:" + "|".join(sorted(_VERBS, key=len, reverse=True)) + ")")

# 强动词单字：标签兜底路径里，候选「人名」只要包含这些字就不算人名
# （「起大拇指」「泽抬起头」「明非瞪眼」「女孩哽咽」这类动词碎片全靠它拦）
_STRICT_VERB_CHARS = set("说问答喊叫骂念喝笑哭叹吼诵读讲谈议论评批夸赞"
                         "训斥责催逼指点竖举抬压按捏握攥揪扯拽推拉拖挪"
                         "瞪瞅盯瞧眯眨抿咬舔吐吞呼喘愣怔醒闭睁摸搓揉"
                         "拂捂掩遮跺蹲蹦窜躬俯仰摆搁翻掀揭敲叩拍击撞砸"
                         "摔扔掷抛投抢夺偷屏咧努撇缩蜷弓挺直绷合拢分掰"
                         "扒撑扛扫挥揽搡晃怒冷顿住默停歇呆滞僵们眉哼轻不皱凝反惊痛心一双忽蹙先耸挠面苦沉突扶额"
    "瘫倒僵稳站坐卧翻滚冒滚淋晒僵躺靠")

# 引号前的标签：动词之前的结尾 2~4 个汉字是人名（先剥掉修饰语再取尾）
_NAME_TAIL_RE = re.compile(r"([\u4e00-\u9fa5]{2,4})$")
# 引号后的标签：动词之前的开头 2~4 个汉字是人名（其余部分须是修饰语）
_NAME_HEAD_RE = re.compile(r"^([\u4e00-\u9fa5]{2,4})")

# 代词（按性别分组）
_PRONOUN_MALE = {"他", "他们"}
_PRONOUN_FEMALE = {"她", "她们"}
_PRONOUNS = _PRONOUN_MALE | _PRONOUN_FEMALE

# 动词前常见的修饰成分（剥掉它们才能露出人名）；同时也是「人名之后、
# 动词之前」剩余文本的白名单 —— 剩余部分必须能被这些词完全覆盖，
# 才认为开头取到的是人名（「楚子航神色平静道」✓，「路明非语重心长道」✓，
# 而「路明非抬起被血渍…」这种余量覆盖不了的就不算标签）。
_ADVERBS = {
    "缓缓", "淡淡", "微微", "悄悄", "默默", "静静", "慢慢", "匆匆", "轻轻",
    "深深", "紧紧", "冷冷", "笑着", "点头", "摇头", "沉声", "冷声", "低声",
    "轻声", "高声", "大声", "小声", "厉声", "柔声", "连忙", "急忙", "顿时",
    "随即", "忽然", "突然", "接着", "然后", "终于", "半天", "片刻", "良久",
    "说道", "笑道", "想了", "想了想", "叹气", "抬头", "低头", "转身", "回头",
    "又", "再", "还", "先", "便", "刚", "刚刚",
    # 神态 / 语态（网文「XX道」前的高频状语，来自真书实测）
    "平静", "淡漠", "漠然", "认真", "严肃", "温和", "冷淡", "平淡", "木然",
    "怔怔", "呆呆", "直直", "死死", "淡然", "黯然", "茫然", "悻悻", "讪讪",
    "嘿嘿", "呵呵", "哈哈", "嘻嘻", "一笑", "苦笑", "莞尔", "哑然",
    "神色", "面色", "表情", "目光", "眼神", "语气", "脸色", "嗓音", "口吻",
    "一脸", "赶紧", "赶忙", "委婉", "老实", "无奈", "无辜", "面无表情",
    "站了起来", "走了过来", "走了过去", "凑了过来", "回过神来", "回过神",
    "站起身来", "抬起头来", "低下头来", "转过头来", "转过头去", "抬起头",
    "低下头", "回过身", "侧过身", "迎了上去", "走上前去", "深吸口气",
    "点了点头", "摇了摇头", "一本正经", "一字一顿", "语重心长", "自言自语",
    "挤眉弄眼", "转移话题", "愤愤不平", "神色平静", "神色不变", "神色复杂",
    "神色如常", "面色难看", "面色平静", "面色不善", "难以置信", "郑重其事",
    "若有所思", "意味深长", "不置可否", "言简意赅", "振振有词", "目不斜视",
    "笑眯眯", "笑呵呵", "郑重", "诚恳", "无奈", "无辜", "轻描淡写",
    "沉默", "沉吟", "默然", "解释", "总结", "交代", "表示", "补充",
    "强调", "咬牙", "轻笑", "幽幽", "叹息", "低语", "耸肩", "摊手",
    "挠头", "扬眉", "挑眉", "愕然", "诧异", "震惊", "凝重", "失声",
    "欣然", "闷声", "狐疑", "疑惑", "表情", "微微一笑", "无语", "感慨",
    "惊讶", "讶然", "怔住", "愣住", "苦涩", "骄傲", "沙哑", "迟疑",
    "遗憾", "试探", "随意", "前方", "低沉", "漫不经心", "小心翼翼",
    "痛心疾首", "脱口而出", "懒洋洋", "恍惚间", "含糊不清", "不紧不慢",
    "忍不住", "先生", "女士", "教授", "校长", "部长", "副部长", "主任",
    "队长", "耸了耸肩", "摊了摊手", "歪头", "挑了挑眉", "扶额", "挠了挠",
    "一怔", "一愣", "怔了怔", "愣了愣", "挠头",
}
_PARTICLES = set("的地了着")

# 人名形状约束：2~4 个汉字（或 2~3 段以「·」相连的外国名），首字不能是虚词
_BAD_NAME_HEAD = set("很太更最又再就都也还被把将向从在是有没不这那哪每某各另其本该第和与或但")
_BAD_NAME_HEAD |= set("无不没非未勿而即仅只已正却倒竟皆均亦虽纵若当可对")

# 动作句人名的首/尾字不能是这些字（高频单字动词/方向词/副词关联字）：
# 「路明非抬起…」误切成「路明非抬」、「接下了…」误切成「接下」就是这么来的。
# 只作用于动作句路径；对话标签路径证据更强，不受此限制（否则「王刚说道」
# 里的「王刚」会被误杀）。
_ACTION_EDGE_REJECT = set(
    # 单字动词
    "说看想问答喊叫骂念喝笑哭叹走跑冲站坐蹲跪躺靠倚望盯瞧扫瞄抬低转回"
    "挥指摸捏握拿放推拉接递揉皱拍敲翻合收掏抓扶背扛拎提吃嚼咽听停等迈"
    "跨退跟带领挡拦躲避闪伸缩颤抖垂扬举搬踩踏踢跳趴爬挑眯眨抿咬舔吐吞"
    "呼喘愣怔醒闭睁竖弯摆晃撇咧扯拽揪捧抱搂搭揽挽梳洗刷浇洒淋赞夸训"
    "斥责催逼讯"
    # 方向词
    "上下进出回来去起过到至达离"
    # 副词 / 虚词关联字
    "以刚而且却即便只才又再还都很太更最轻重深浅急缓慢紧松冷淡微顿从向往朝同跟透忽男女"
    # 代词
    "他她它你我"
)
_NAME_RE = re.compile(r"^[\u4e00-\u9fa5]{2,4}$|^[\u4e00-\u9fa5]{1,3}(?:·[\u4e00-\u9fa5]{1,3}){1,2}$")

# 动作句：「人名 + 动作描写」文本（无引号；允许少量逗号）
_ACTION_RE = re.compile(r"^([\u4e00-\u9fa5·]{2,4})([\u4e00-\u9fa5，,、\s]{1,24})[。！？…～\s]*$")
_ACTION_VERB_RE = re.compile(
    "(想|说|看|望|盯|瞧|扫|瞄|走|跑|冲|跳|站|坐|蹲|跪|躺|靠|倚|笑|哭|叹|点|摇|抬|低|转|回|挥|指|摸|挠|"
    "咬|抿|眨|握|拿|放|推|拉|接|递|皱|眯|耸|拍|敲|翻|合|收|掏|抓|扶|背|扛|拎|提|喝|吃|嚼|咽|听|等|"
    "停|顿|沉|迈|跨|绕|退|跟|带|领|挡|拦|躲|避|闪|伸|缩|僵|颤|抖|扬|垂|歪|偏|凑|俯|仰|弯|挪|踱|"
    "点头|摇头|起身|抬头|低头|开口|深吸|吐气|呼气|喘|愣|怔|醒|闭|睁|耸肩|鼓掌|皱眉|挑眉)")

# 高频非人名词语（动作句 / 标签误匹配时剔除）。
# 同时用作「人名子串」黑名单：候选名里含这些词（如「心中一凛」含「心中」）
# 一律不算人名 —— 这是识别精度的第一道闸。
_COMMON_WORDS = {
    # 时间/转折/语气副词
    "今天", "昨天", "明天", "此时", "此刻", "突然", "顿时", "然后", "接着",
    "于是", "但是", "然而", "其实", "不过", "因为", "所以", "如果", "虽然",
    "只见", "听到", "说完", "原来", "终于", "依然", "仍然", "依旧", "已经",
    "曾经", "正在", "刚刚", "刚才", "马上", "立刻", "忽然", "果然", "竟然",
    "居然", "当然", "显然", "始终", "渐渐", "缓缓", "淡淡", "轻轻", "悄悄",
    "默默", "静静", "慢慢", "匆匆", "微微", "狠狠", "连忙", "急忙", "随即",
    "一时", "半晌", "片刻", "许久", "很久", "这时", "那时", "半空", "半路",
    # 代词/泛指
    "两人", "三人", "四人", "众人", "大家", "自己", "别人", "对方", "人家",
    "彼此", "他们", "她们", "我们", "你们", "咱们", "这个", "那个", "每个",
    "某个", "各种", "所有", "全部", "整个", "整片", "其他", "其余", "有的",
    # 场所/方位/身体/声音（「XX道：」前误提取的高发词）
    "声音", "话音", "话语", "气氛", "空气", "时间", "周围", "四周", "心中",
    "心里", "心头", "脑海", "脑中", "眼前", "眼中", "身后", "身旁", "身边",
    "身前", "面前", "眼里", "怀里", "怀中", "眉头", "嘴角", "脸上", "头上",
    "身上", "手中", "口中", "嘴里", "车里", "屋里", "屋外", "房间", "教室",
    "客厅", "门外", "窗外", "楼上", "楼下", "一旁", "对面", "前面", "后面",
    "里面", "外面", "上面", "下面", "远处", "近处", "地点", "地方", "现场",
    # 动作/神态（动作句开头误判的高发词）
    "低头", "抬头", "点头", "摇头", "转身", "回头", "起身", "开口", "沉默",
    "苦笑", "微笑", "大笑", "冷笑", "一笑", "眨眼", "皱眉", "挑眉", "耸肩",
    "鼓掌", "深吸", "吐气", "侧身", "弯腰", "站起", "坐下", "蹲下", "跪下",
    "躺下", "爬起", "离开", "回来", "过来", "出去", "出来", "起来", "上去",
    "下来", "上前", "逼近", "靠近", "跟着", "带着", "半天", "一眼", "一声",
    "一步", "一句", "一拳", "一脚", "一剑", "一刀",
    # 身份泛称
    "少年", "少女", "男人", "女人", "老头", "老者", "大叔", "阿姨", "孩子",
    "大人", "老板", "老师", "医生", "警察", "司机", "学生", "青年", "女子",
    "男子", "女孩", "男孩", "姑娘", "小姐", "太太", "夫人", "老爷", "大爷",
    "大妈", "大婶", "大妈", "兄弟", "大哥", "大姐", "小弟", "小子", "丫头",
    # 连词 / 假设递进（「即使…」「无声而…」这类碎片的高发来源）
    "即使", "既然", "虽然", "尽管", "除非", "无论", "只要", "只有", "哪怕",
    "万一", "要是", "不但", "不仅", "而且", "并且", "况且", "何况", "那么",
    "这样", "那样", "这般", "那般", "无声", "旋即", "继而", "转而", "却说",
    "且说", "可见", "由此", "顷刻", "刹那", "须臾", "未几", "少顷", "俄而",
    # 动词性习语（「随口道」「接下了」被误当人名的来源）
    "随口", "顺口", "闭口", "住口", "接下", "接下来", "回过", "转过头",
    # 形容词/名词性碎片（真书《龙族：重启人生》实测误报）
    "巨大", "熟悉", "旁边", "好奇", "咆哮", "电话", "重心", "明白", "清楚",
    "突兀", "明显", "神情", "声响", "动静", "情绪", "反应", "模样", "庞然",
    "难怪", "犹豫", "突如其来", "曾几何时", "领队", "委托人", "扩音器",
    "卡塞尔", "抿嘴", "挠头", "两个人", "几个人", "蓦然", "蓦地", "哽咽",
    "抽泣", "颤声", "一行", "满脸", "满眼", "满口", "突如其", "曾几何",
    "为什", "委托", "扩音", "卡塞", "两个", "三个", "四个", "几个",
    "安慰", "眼巴巴", "眼睁睁", "直勾勾", "气呼呼", "芝加哥",
    "突如", "曾几", "芝加", "所谓", "时至今日", "明明", "尤其", "同样",
    "恍惚", "仿佛", "副校", "老男", "漆黑", "夜风", "王座", "龙类", "先前",
    "方才", "为首",
    "所谓", "尤其", "同样", "突然", "显然", "竟然", "居然", "固然", "诚然",
    "快速", "尴尬", "简单", "短暂", "事实", "继续", "转头", "委屈",
    "宽敞", "之一", "全场", "画面", "黑暗中", "屏幕", "一切", "年轻人",
    "老男人", "中年男人", "中年男", "为一", "所谓",
    # 《龙族：重启人生》实测误报（正则贪婪切片）
    "决定", "良好", "浑然不", "浑然", "全当", "此情此", "衣装笔", "衣装",
    "一个戴", "手伸入", "包厢门", "包厢内", "记忆瞬", "汽车猛", "夕阳",
    "锐利", "叔叔小", "徐岩岩小", "唐威瘫", "唐威身", "唐威一", "路非明",
}

# 性别 / 年龄归类线索
_FEMALE_HINTS = ("女孩", "少女", "姑娘", "小姐", "女人", "女子", "女生",
                 "阿姨", "夫人", "太太", "女士", "姐姐", "妹妹")
_MALE_HINTS = ("男孩", "少年", "男人", "男子", "男生", "大叔", "大哥",
               "兄弟", "先生", "青年", "小子")
_OLD_FEMALE_HINTS = ("奶奶", "婆婆", "老太太", "姥姥", "外婆", "老妇人",
                     "大妈", "大婶")
_OLD_MALE_HINTS = ("爷爷", "老爷子", "老者", "老头", "伯父", "中年",
                   "大爷", "大叔", "叔叔", "老伯")
# 单字亲属称谓：只在**人名本身**里查（佟姨 / 王大爷 / 张婶）；
# 不做上下文共现投票 —— 「路明非和叔叔」这种邻句会把主角投成中年叔叔
_KIN_FEMALE = ("姨", "婶", "嫂", "婆", "妈", "姐", "妹", "女", "姬", "衣")
_KIN_MALE = ("叔", "伯", "爷", "爹", "哥")
# 女性名常用字（人名内出现即强女票）；叠字名（诺诺/柳淼淼）同理
_FEMALE_NAME_CHARS = "雯婷娜丽芳娟静蕾雪琳慧敏燕莉倩薇丹霞梅兰玉凤洁妍"                      "娴媛婧黛瑶瑾璇琪珊芙蓉蕊萍颖馨悦怡欣彤绮媚娇婉妙"

# 常见姓氏（标签兜底路径的门槛：候选名必须以姓氏开头）
_SURNAMES = set("李王张刘陈杨黄赵吴周徐孙马朱胡郭何高林罗郑梁谢宋唐许韩冯邓曹彭曾肖田董袁潘于蒋蔡余杜叶程苏魏吕丁任沈姚卢姜崔钟谭陆汪范金石廖贾夏韦付方白邹孟熊秦邱江尹薛闫段雷侯龙史陶黎贺顾毛郝龚邵万钱严覃武戴莫孔向汤俞章鲁路楚慕源樱宫风酒傅凌戴盛麦唐菲洛")

STACK_LIMIT = 12          # 角色栈最深保留人数
# 成为「出场人物」的门槛：至少确认为说话人/动作句主语的次数。
# 只出现一次的候选大多是「缓缓」「心中一凛」这类漏网误报；
# 真正有台词的角色一本书里几乎不可能只出现一次。
MIN_SPEAKER_MENTIONS = 4

# 人名里不该出现的字（真实中文人名基本不含这些虚词/助词）
_FUNCTION_CHARS = set("着了过的地得吗呢吧啊呀哦嘛么也")

# 子串黑名单：候选名里包含这些词就不算人名（心中一凛 / 一片叶子…）。
# 称谓词（太太/大爷/奶奶/大叔…）**豁免** —— 「老太太」「王大爷」这类
# 名字本身就是称谓+姓氏，不能因为含「太太」就被拒。
_HINT_WORDS = (set(_FEMALE_HINTS) | set(_MALE_HINTS)
               | set(_OLD_FEMALE_HINTS) | set(_OLD_MALE_HINTS))
_SUBSTR_BLACKLIST = tuple(
    w for w in list(_COMMON_WORDS) + list(_ADVERBS)
    if len(w) >= 2 and w not in _HINT_WORDS)


def _is_plausible_name(name: str) -> bool:
    """形状上像人名：2~4 汉字 / 带·的外国名，排除虚词与高频词。

    精度三道闸：
      ① 整词命中黑名单（缓缓/低头/心中…）直接排除；
      ② 候选名**包含**黑名单词（心中一凛、一片叶子）排除；
      ③ 名字里含助词/虚词字（着了过的地得…）排除。
    """
    if not name or name in _PRONOUNS:
        return False
    if not _NAME_RE.match(name):
        return False
    if name[0] in _BAD_NAME_HEAD:
        return False
    if any(ch in _FUNCTION_CHARS for ch in name):
        return False
    # 名字里不该有代词字（「他已」「她俩」这类碎片）
    if any(ch in "他她它你我" for ch in name):
        return False
    if name in _COMMON_WORDS or name in _ADVERBS:
        return False
    for w in _SUBSTR_BLACKLIST:
        if w in name:
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
        if not stripped and prefix[-1] in _STRICT_VERB_CHARS:
            # 「凯撒冷[笑]」「老人顿[住]」——动词短语前半被并进了前缀
            prefix = prefix[:-1]
            stripped = True
        if not stripped:
            break
    return prefix


def _covered_by_modifiers(rest):
    """剩余文本是否全部由已知修饰语/助词/标点构成。

    「楚子航【神色平静】道」→ rest=神色平静 ✓；
    「路明非【抬起被血渍…】」→ 覆盖不了 ✗（说明取到的不是人名边界）。
    """
    rest = rest.strip("，。、！？：；…—\u3000 \t")
    while rest:
        for w in _ADVERBS:
            if rest.startswith(w):
                rest = rest[len(w):]
                break
        else:
            if rest[0] in _PARTICLES or rest[0] in "，。、！？：；…—\u3000 \t地得":
                rest = rest[1:]
                continue
            return False
    return True


def _tag_before_quote(text, open_idx, registry=None):
    """在引号前的窗口里找「人名/代词 + 动词」（如：楚子航沉声道：「…」）。

    取引号前最多 16 字的窗口，找**第一个**说话动词。人名有两种位置：
      ① 名字在窗口短语的**开头**，名字之后到动词之间必须全是已知修饰语
         （楚子航【点点头，缓缓】道 / 施耐德【平静】道）；
      ② 名字在动词短语的**结尾**（听到这话，【楚子航】缓缓道），
         此时候选里不允许出现强动词字（防「泽抬起头」这类碎片）。
    新人名（registry 里没登记过）必须以常见姓氏开头 —— 这是
    「决定/良好/浑然不」这类贪婪碎片的最强一道闸（真书实测）。
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
    # 前缀里出现代词（他握紧小拳头…）→ 这不是人名短语
    if any(ch in "他她它" for ch in prefix):
        return None
    # ① 名字在开头 + 剩余全是修饰语；候选本身不得含强动词字
    for length in (2, 3, 4):
        if len(prefix) < length:
            break
        cand, rest = prefix[:length], prefix[length:]
        if _is_plausible_name(cand) and not any(
                v in cand for v in _STRICT_VERB_CHARS):
            if _covered_by_modifiers(rest):
                if _known_or_surnamed(cand, registry):
                    return cand
    # ② 名字在结尾（兜底）：候选必须以常见姓氏开头（听到这话，【楚子航】
    # 缓缓道），否则「胸有成竹」「头也不抬」这类碎片会从这里漏进来；
    # 尾字再过一遍动作残片拦截（「唐威瘫」的「瘫」）
    nm = _NAME_TAIL_RE.search(prefix)
    if nm:
        cand = nm.group(1)
        if (cand[0] in _SURNAMES and _is_plausible_name(cand)
                and not any(v in cand for v in _STRICT_VERB_CHARS)
                and cand[-1] not in _ACTION_EDGE_REJECT):
            return cand
    return None


def _known_or_surnamed(cand, registry=None):
    """新人名必须有姓氏背书；已登记过的名字（含诺诺这类无姓昵称）
    与叠字/女性字昵称放行。registry 为 None 时按姓氏要求。

    称谓词（老太太/王大爷/佟姨…）本身就是书中身份，直接放行 ——
    它们在 _HINT_WORDS 里，不会被黑名单误杀。
    """
    if registry is None:
        return cand[0] in _SURNAMES or cand in _HINT_WORDS
    if cand in registry.gender_votes:
        return True
    if cand[0] in _SURNAMES or cand in _HINT_WORDS:
        return True
    # 无姓但形状像昵称：叠字名 / 女性字名（诺诺、小魔鬼里的魔鬼…）
    if len(cand) == 2 and cand[0] == cand[1]:
        return True
    if any(ch in _FEMALE_NAME_CHARS for ch in cand):
        return True
    return False


def _tag_after_quote(text, close_idx, registry=None):
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
    # 剥掉尾部强动词字：「路明非愣[道]」→「路明非」
    head = _strip_tail_decorations(head)
    # 名字在开头，剩余必须是修饰语（夏弥【笑着回答】/ 老太太【说道】）；
    # 候选本身不得含强动词字（拦「凯撒冷[笑]」「楚子航怒[喝]」碎片）
    for length in (2, 3, 4):
        if len(head) < length:
            break
        cand, rest = head[:length], head[length:]
        if _is_plausible_name(cand) and not any(
                v in cand for v in _STRICT_VERB_CHARS):
            if _covered_by_modifiers(rest):
                if _known_or_surnamed(cand, registry):
                    return cand
    return None


class _Registry:
    """出场人物登记表：按首次出场排序 + 性别/年龄投票。"""

    def __init__(self):
        self.order = []           # [人名]，按首次登记顺序
        self.gender_votes = {}    # 人名 -> [男票, 女票]
        self.age_votes = {}       # 人名 -> {"old_f": n, "old_m": n}
        self.speaker_count = {}   # 人名 -> 确认为说话人/动作主语的次数

    def register(self, name):
        if name not in self.gender_votes:
            self.gender_votes[name] = [0, 0]
            self.age_votes[name] = {"old_f": 0, "old_m": 0}
            self.speaker_count[name] = 0
            self.order.append(name)

    def confirm(self, name):
        """确认为说话人/动作句主语一次（进出场人物清单的频率门槛）。"""
        if name in self.speaker_count:
            self.speaker_count[name] += 1

    def vote(self, name, context_text):
        """给人物投性别/年龄票。

        证据只取两类（真书实测：上下文共现的称谓全是噪音 ——
        「路明非和叔叔」会把主角投成中年叔叔）：
          ① 称谓直接出现在人名里（老太太 / 王大爷 / 佟姨）→ 强票；
          ② 人名邻近 ±12 字的代词「他/她」→ 弱票各记一笔，
            男性证据必须有，否则男主角会被成段的「她」误判成女的。
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
        for w in _FEMALE_HINTS + _KIN_FEMALE:
            if w in name:
                votes[1] += 2
                break
        for w in _MALE_HINTS + _KIN_MALE:
            if w in name:
                votes[0] += 2
                break
        if any(ch in _FEMALE_NAME_CHARS for ch in name):
            votes[1] += 2
        if len(name) == 2 and name[0] == name[1]:
            votes[1] += 2          # 叠字名（诺诺）多为女性
        if (len(name) == 3 and name[1] == name[2]
                and name[0] in _SURNAMES):
            votes[1] += 2          # 三字叠音名（柳淼淼）多为女性
        idx = context_text.find(name)
        if idx >= 0:
            window = context_text[max(0, idx - 12): idx + len(name) + 12]
            if "她" in window:
                votes[1] += 1
            if "他" in window:
                votes[0] += 1

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
        """出场人物清单：按首次出场排序，只保留达到频率门槛的人名。

        包含关系合并：「昂热一」「恺撒挠」「曼施坦」这类碎片必然是
        真名（昂热/恺撒/曼施坦因）的超集 —— 互为子串的两个候选只保留
        确认次数高的那个。
        """
        kept = [(name, self.speaker_count.get(name, 0))
                for name in self.order
                if self.speaker_count.get(name, 0) >= MIN_SPEAKER_MENTIONS]
        kept.sort(key=lambda x: -x[1])
        dropped = set()
        for i, (a, ca) in enumerate(kept):
            if a in dropped:
                continue
            for j, (b, cb) in enumerate(kept[i + 1:], i + 1):
                if b in dropped or b == a:
                    continue
                winner = None
                if b.startswith(a):
                    # b = a + 附加字：附加字全是动词残片（恺撒+挠）→ 留短名；
                    # 是名字的一部分（曼施坦+因）→ 留长名
                    extra = b[len(a):]
                    winner = (a if all(ch in _STRICT_VERB_CHARS
                                       for ch in extra) else b)
                elif a.startswith(b):
                    extra = a[len(b):]
                    winner = (b if all(ch in _STRICT_VERB_CHARS
                                       for ch in extra) else a)
                elif a in b:
                    winner = a if ca >= cb else b
                elif b in a:
                    winner = a if ca >= cb else b
                if winner is not None:
                    dropped.add(b if winner is a else a)
                    if winner is b:      # 当前 a 被合并 → 换下一个 a
                        break
        return [(name, self.category(name)) for name in self.order
                if name in set(n for n, _ in kept) - dropped]


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

    名字只取 2~3 字（4 字档在真书里几乎全是「路明非抬」「神色平静」
    这类贪婪碎片；真复姓名会由对话标签路径兜住）。
    """
    seg = segment.strip()
    if not seg or len(seg) > 40:
        return None
    m = _ACTION_RE.match(seg)
    if not m or "：" in segment or ":" in segment:
        return None
    head, tail = m.group(1), m.group(2)
    for length in (3, 2):
        if length > len(head):
            continue
        name, rest = head[:length], head[length:] + tail
        if not _is_plausible_name(name):
            continue
        if any(ch in _ACTION_NAME_BLACKLIST for ch in name):
            continue
        # 首尾字是动词/方向词/虚词 → 不是人名（「路明非抬」「接下」这类
        # 贪婪切分碎片全靠这条拦截）；真名留给出更长/更短档
        if name[0] in _ACTION_EDGE_REJECT or name[-1] in _ACTION_EDGE_REJECT:
            continue
        if not _ACTION_VERB_RE.search(rest):
            continue
        registry.register(name)
        stack.push(name)                 # 标记为后续对话的说话人
        return name
    return None


def _process_paragraph(pi, para, registry, stack, speakers, light=False):
    """在整段原文上定位引号与说话人，再把说话人映射回切分片段。

    light=True（第一遍登记/投票）时只做引号标签与动作句识别，
    不做切分对齐和说话人表 —— 那部分占大头，留给第二遍。
    """
    if not light:
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
            if not light:
                registry.confirm(act)
        token = _tag_before_quote(para, o, registry)
        if token is None:
            token = _tag_after_quote(para, c, registry)
        spk = _apply_tag(token, registry, stack)
        if spk is not None:
            touched.append(spk)
            # 频率门槛只在完整遍计数（两遍都计等于门槛减半）。
            # 代词解析出的说话人也算 —— 「……」她说。是一个人开口的
            # 真实证据，只靠代词出场的角色同样该进清单。
            if not light:
                registry.confirm(spk)
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
            if not light:
                registry.confirm(act)

    # 3) 切分片段 → 说话人：与某个引号区间相交（且片段起点在对话结束前）
    #    的片段用该引号的说话人；其余片段是旁白，不进表。（轻量遍跳过）
    if not light:
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
      characters = [(人名, 类别), ...] 按首次出场排序（只含达到
                   MIN_SPEAKER_MENTIONS 门槛的人物）；
      speakers   = {(段落下标, 切分片段): 说话人名}，旁白片段不进表。

    为什么两遍：代词「他说道/她说道」的性别消歧依赖**全书**的投票结果
    （夏弥是女这件事可能到很后面才有「她」的证据）。第一遍只登记人物、
    收集性别/年龄票（轻量：不做切分对齐和说话人表）；第二遍人物性别
    已定，再生成最终说话人标注。

    性能注意：本函数在后台线程跑，纯 Python 全程持有 GIL ——
    每几百段 sleep 一下把 CPU 完全让给 UI 线程，避免手机上
    扫描期间点任何按钮都卡。
    """
    import time as _time

    registry = _Registry()

    # 第一遍：登记 + 投票（轻量，说话人结果丢弃）
    probe_stack = _Stack(registry)
    probe_speakers = {}
    for pi, para in enumerate(paragraphs):
        try:
            _process_paragraph(pi, para, registry, probe_stack,
                               probe_speakers, light=True)
        except Exception:
            continue
        if pi % 400 == 399:
            _time.sleep(0.001)       # 完全释放 GIL，让 UI 喘口气

    # 第二遍：性别已定 → 生成最终逐句说话人
    stack = _Stack(registry)
    speakers = {}
    for pi, para in enumerate(paragraphs):
        try:
            _process_paragraph(pi, para, registry, stack, speakers)
        except Exception:
            # 单段解析失败不影响全书：跳过该段（相当于当旁白处理）
            continue
        if pi % 400 == 399:
            _time.sleep(0.001)       # 完全释放 GIL，让 UI 喘口气

    return registry.result(), speakers
