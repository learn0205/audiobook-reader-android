# -*- coding: utf-8 -*-
"""
book_parser.py —— 书籍文件解析模块（txt / epub → 段落列表）

职责
----
1. 识别并加载 txt / epub 文件，损坏文件抛出带可读信息的 BookParseError；
2. txt：多编码自动探测（UTF-8 / GB18030 / Big5 / UTF-16）；
3. epub：纯标准库解析（zipfile + ElementTree + HTMLParser），无需第三方库；
4. 文本预处理：去 BOM/零宽字符、统一换行、过滤空白行、超长段落按句子拆分。

段落约定：返回的 paragraphs 是"一行一段"的字符串列表，
         传入 QTextEdit 后每个字符串恰好对应一个文本块（block），
         供高亮与滚动模块按下标定位。
"""

import os
import posixpath
import re
import zipfile
import xml.etree.ElementTree as ET
from html.parser import HTMLParser

# 单段最大字符数：超过则按句子标点拆分成多段，保证高亮粒度和进度条平滑
MAX_PARAGRAPH_LEN = 600

# 需要剔除的零宽字符（网页复制来的文本常见）
_ZERO_WIDTH_RE = re.compile(r"[\u200b\u200c\u200d\ufeff\u00ad]")

# 句末标点（用于拆分超长段落，标点保留在前一段末尾）
_SENTENCE_RE = re.compile(r"(?<=[。！？!?…；;])")

# 章节标题识别（txt / 古腾堡下载文本）：
# 匹配 "第一章" "第12回" "第三节" "卷二" "Chapter 3" 等，允许带章名
_CHAPTER_RE = re.compile(
    r"^【?\s*("
    r"第[0-9零〇一二三四五六七八九十百千两]+\s*[章回节节卷部集篇]"
    r"|[0-9零〇一二三四五六七八九十百千两]{1,4}\s*[章回节卷部集篇]"
    r"|[卷部集篇]\s*[0-9零〇一二三四五六七八九十百千两]{1,4}"
    r"|[上中下]\s*[章卷部篇集]"
    r"|Chapter\s+\d+|CHAPTER\s+[0-9IVXLC]+"
    r")】?"
    r"(\s*[：:．.、\s]\s*\S.*)?$"
)
# 无编号的特殊章节名
_SPECIAL_HEADINGS = {"楔子", "序", "序言", "序章", "前言", "引子",
                     "尾声", "后记", "终章", "附录", "跋"}


def _is_chapter_heading(text: str) -> bool:
    """判断一个段落是否是章节标题（要求短小，避免误判正文）。"""
    if not text or len(text) > 50:
        return False
    if text.strip() in _SPECIAL_HEADINGS:
        return True
    return bool(_CHAPTER_RE.match(text.strip()))


class BookParseError(Exception):
    """书籍解析失败异常，异常信息可直接展示给用户。"""


class BookDocument:
    """解析结果：书籍路径、标题、段落列表、章节目录。

    chapters 为 [(章节标题, 起始段落下标), ...]，按位置升序；
    可能为空列表（书籍没有可识别的章节结构）。
    """

    def __init__(self, path: str, title: str, paragraphs: list, chapters: list = None):
        self.path = path
        self.title = title
        self.paragraphs = paragraphs
        self.chapters = chapters or []


# ============================ 通用工具 ============================

def _clean_text(text: str) -> str:
    """文本清洗：去零宽字符、全角空格转普通空格、去除首尾空白。"""
    text = _ZERO_WIDTH_RE.sub("", text)
    text = text.replace("\u3000", " ")
    return text.strip()


def _decode_bytes(data: bytes) -> str:
    """多编码尝试解码：BOM 优先，然后 utf-8 → gb18030 → big5，最后容错替换。"""
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        return data.decode("utf-16")          # 带 BOM 的 UTF-16
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "big5"):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")  # 兜底：坏字节用替换符


def _split_long_paragraph(text: str, max_len: int = MAX_PARAGRAPH_LEN) -> list:
    """超长段落按句末标点拆分成多个短段，避免高亮/进度条粒度过粗。"""
    if len(text) <= max_len:
        return [text]
    parts = [p for p in _SENTENCE_RE.split(text) if p]
    result, buf = [], ""
    for piece in parts:
        if buf and len(buf) + len(piece) > max_len:
            result.append(buf)
            buf = ""
        # 单句仍然超长（无标点长文本）：按最大长度硬切
        while len(piece) > max_len:
            result.append(piece[:max_len])
            piece = piece[max_len:]
        buf += piece
    if buf:
        result.append(buf)
    return result


# ============================ TXT 解析 ============================

def load_txt(path: str):
    """解析 txt：多编码解码 → 统一换行 → 过滤空白行 → 超长段拆分 → 章节识别。
    返回 (段落列表, 章节列表)。"""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as err:
        raise BookParseError(f"无法读取文件：{err}")

    text = _decode_bytes(data)
    if not text.strip():
        raise BookParseError("文件内容为空")

    paragraphs = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = _clean_text(line)
        if not line:
            continue  # 过滤空白行 / 纯空格行
        paragraphs.extend(_split_long_paragraph(line))

    # 扫描章回式标题（"第三章 xxx"、"楔子"等），生成章节目录
    chapters = [(p, i) for i, p in enumerate(paragraphs)
                if _is_chapter_heading(p)]
    return paragraphs, chapters


# ============================ EPUB 解析 ============================

class _HTMLTextExtractor(HTMLParser):
    """从 XHTML 中提取块级元素文本为段落的轻量解析器（标准库实现）。

    同时记录 h1~h6 标题的位置，用于生成章节目录。
    """

    BLOCK_TAGS = {"p", "h1", "h2", "h3", "h4", "h5", "h6",
                  "li", "blockquote", "div", "tr", "figcaption"}
    HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
    SKIP_TAGS = {"script", "style", "head", "title"}

    def __init__(self):
        super().__init__(convert_charrefs=True)  # 自动把 &amp; 等实体转成字符
        self.paragraphs = []
        self.headings = []      # [(段落下标, 标题文字)]，用于章节定位
        self._buf = []
        self._in_heading = False
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1
        elif tag == "br":
            self._buf.append(" ")  # 换行标签当作空格，避免单词粘连
        elif tag in self.HEADING_TAGS:
            self._in_heading = True

    def handle_endtag(self, tag):
        if tag in self.SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag in self.HEADING_TAGS:
            self._in_heading = False
        if tag in self.BLOCK_TAGS:
            self._flush()

    def handle_data(self, data):
        if self._skip_depth == 0:
            self._buf.append(data)

    def _flush(self):
        """当前缓冲内容作为候选段落提交（空段过滤、超长拆分）。

        标题块（h1~h6）除提交为段落外，还记录位置供章节目录使用。
        """
        text = _clean_text("".join(self._buf))
        self._buf = []
        if not text:
            return
        if self._in_heading:
            self.headings.append((len(self.paragraphs), text))
            self.paragraphs.append(text)
            return
        self.paragraphs.extend(_split_long_paragraph(text))

    def close(self):
        super().close()
        self._flush()  # 文档结尾未闭合的块级元素也要提交


def load_epub(path: str):
    """解析 epub：container.xml → OPF → spine 顺序逐章提取文本。
    返回 (标题, 段落列表)。全程使用标准库，无第三方依赖。"""
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        raise BookParseError("EPUB 文件损坏或受 DRM 版权保护，无法打开")
    except OSError as err:
        raise BookParseError(f"无法读取文件：{err}")

    with zf:
        # 1) 读取 container.xml 定位 OPF 包描述文件
        try:
            container = ET.fromstring(zf.read("META-INF/container.xml"))
        except (KeyError, ET.ParseError):
            raise BookParseError("EPUB 结构损坏：缺少 META-INF/container.xml")

        opf_path = None
        for elem in container.iter():
            if elem.tag.split("}")[-1] == "rootfile":  # 忽略命名空间取本地名
                opf_path = elem.attrib.get("full-path")
                break
        if not opf_path:
            raise BookParseError("EPUB 结构损坏：未找到 OPF 描述文件")

        # 2) 解析 OPF：取书名、manifest 资源表、spine 阅读顺序
        title = os.path.splitext(os.path.basename(path))[0]  # 默认用文件名
        try:
            opf = ET.fromstring(zf.read(opf_path))
        except (KeyError, ET.ParseError):
            raise BookParseError("EPUB 结构损坏：OPF 文件解析失败")

        for elem in opf.iter():
            if elem.tag.split("}")[-1] == "title" and (elem.text or "").strip():
                title = elem.text.strip()
                break

        manifest = {}  # id -> (相对路径, media-type)
        for elem in opf.iter():
            if elem.tag.split("}")[-1] == "item":
                manifest[elem.attrib.get("id")] = (
                    elem.attrib.get("href", ""),
                    elem.attrib.get("media-type", ""),
                )
        spine_ids = [elem.attrib.get("idref")
                     for elem in opf.iter() if elem.tag.split("}")[-1] == "itemref"]

        # 3) 按 spine 顺序逐个提取章节文本，并汇总章节目录
        base_dir = posixpath.dirname(opf_path)
        paragraphs = []
        chapters = []
        spine_docs = 0
        for idref in spine_ids:
            if idref not in manifest:
                continue
            href, media_type = manifest[idref]
            is_html = ("html" in media_type
                       or href.lower().endswith((".xhtml", ".html", ".htm")))
            if not is_html:
                continue  # 跳过图片、css、字体等资源
            full_path = posixpath.normpath(posixpath.join(base_dir, href)) if base_dir else href
            try:
                data = zf.read(full_path)
            except KeyError:
                continue  # 清单与实际文件不一致时跳过该章
            extractor = _HTMLTextExtractor()
            try:
                extractor.feed(_decode_bytes(data))
                extractor.close()
            except Exception:
                continue  # 单章损坏不影响整本书
            doc_start = len(paragraphs)
            paragraphs.extend(extractor.paragraphs)
            spine_docs += 1
            # 章节定位：优先用文档内 h1~h6 标题；否则整个 spine 文件算一节
            if extractor.headings:
                chapters.extend((doc_start + idx, title)
                                for idx, title in extractor.headings)
            elif spine_docs > 1:
                chapters.append((doc_start, f"第 {spine_docs} 节"))

        if not paragraphs:
            raise BookParseError("EPUB 中未解析到文本内容（可能是纯图片书籍）")

        # 章节去重、排序；只有一节且在开头时视为无章节
        chapters = sorted(set(chapters))
        if len(chapters) <= 1:
            chapters = []
        return title, paragraphs, chapters


# ============================ 统一入口 ============================

def load_book(path: str) -> BookDocument:
    """根据扩展名分发到对应解析器，返回 BookDocument。

    所有可预期的失败都以 BookParseError 抛出，异常信息可直接弹窗展示。
    """
    if not os.path.isfile(path):
        raise BookParseError("文件不存在，可能已被移动或删除")

    ext = os.path.splitext(path)[1].lower()
    name = os.path.splitext(os.path.basename(path))[0]

    if ext == ".txt":
        paragraphs, chapters = load_txt(path)
        title = name
    elif ext == ".epub":
        title, paragraphs, chapters = load_epub(path)
    else:
        raise BookParseError("暂不支持的文件格式，目前支持 txt 和 epub")

    if not paragraphs:
        raise BookParseError("未解析到正文内容，文件可能是空的或已损坏")

    return BookDocument(path, title, paragraphs, chapters)
