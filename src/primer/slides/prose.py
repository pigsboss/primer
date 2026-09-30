# -*- coding: utf-8 -*-
"""正文的确定性清洗：只做"形式"上的剥离与规整，不写一个字。

两部分，都只动形式：

* :func:`strip_lead_enumerator` 剥掉句子行首的原文编号。候选句常以**原文编号**开头——
  书稿里的 ``（一）``／``一、``／``1.``／``①`` 是章节体例的一部分，进到幻灯片上却和
  :func:`primer.slides.beamer._body_item` 已经画好的短横 bullet 撞车，看着像两个编号。
  编号是**排版系统给的形式**，不是句子内容，所以在出页面之前剥掉；源文件
  （``candidates.md`` 与 ``outline.yaml``）一个字不改。剥的时候**宁少勿多**：数字、年份、
  书名里的序号都不是编号。``1995—2026``、``1.5 亿公里``、``第一章``、``（NASA）`` 必须
  原样保留，所以每一类编号都带一个明确的边界（分隔符或括号），点号后跟数字不算编号。

* :func:`normalize_pick_text` 把半角标点、直引号与多余空格规整成成书同一套的排印形式。
  原文句走 ``book`` 的行内渲染链（引号归一、``[n]``、链接处理），**模式 3 的凝练句与
  人写的自由文本却不经过那条链**，于是半角引号、与汉字相邻的半角标点、数字与单位之间的
  空格会直接进 tex。形式由排版系统定，这里也一并规整，三种 pick 形状因此一视同仁。
"""

from __future__ import annotations

import re
from typing import List, Tuple

from ..book import quotes

# 中文数字与阿拉伯数字的编号主体。都限制长度：太长的串是内容（年份、数字），不是编号。
_NUMERAL = r"(?:[0-9]{1,3}|[一二三四五六七八九十百零两]{1,6})"

# 行首括号编号：（一） (1) （１２） …。括号里只认数字，所以 （NASA） （见 §8.6） 安全。
_PAREN_ENUM_RE = re.compile(r"^[（(]\s*" + _NUMERAL + r"\s*[）)]\s*")
# 行首裸编号：一、 1. 1、 1) 1．…。点号后跟数字不算（1.5 保留）；句点必须收在一处，
# 免得把 "1.5" 的 "1." 也当成编号。顿号、全角顿点、右括号都在分隔符之列。
_BARE_ENUM_RE = re.compile(
    r"^" + _NUMERAL + r"\s*(?:、|．|\.(?![0-9])|\)|）)\s*"
)
# 行首圈号：①–⑳（U+2460–U+2473，连续的一段）。
_CIRCLED_ENUM_RE = re.compile(r"^[\u2460-\u2473]\s*")
# 行首 markdown 式标记："- " "* " "• "。
_MARKDOWN_MARK_RE = re.compile(r"^(?:-|\*|•)\s+")

_LEAD_STRIPPERS = (
    _PAREN_ENUM_RE,
    _BARE_ENUM_RE,
    _CIRCLED_ENUM_RE,
    _MARKDOWN_MARK_RE,
)


def _strip_one(text: str) -> str:
    """从行首剥掉**一层**编号；一层都不是就返回 ``None``。"""
    for pattern in _LEAD_STRIPPERS:
        matched = pattern.match(text)
        if matched:
            return text[matched.end():]
    return None


def strip_lead_enumerator(text: str) -> str:
    """剥掉句子行首的原文编号，循环剥到不变为止。

    ``（一）我国水平`` → ``我国水平``；``1) 第二点`` → ``第二点``；``① 首先`` → ``首先``。
    没有编号时**原样返回**（连行首空白也不动），号码串、年份、书名里的序号因此不受影响。
    """
    value = text
    while True:
        stripped = _strip_one(value.lstrip(" \t\u3000"))
        if stripped is None:
            return value
        value = stripped


# ---------------------------------------------------------------- 排印规整


# 半角标点 → 全角：只在与中日韩字符相邻时替换，纯拉丁串里的半角标点一概不动。
_PUNCT_MAP = {
    ",": "，",
    ".": "。",
    ";": "；",
    ":": "：",
    "!": "！",
    "?": "？",
    "(": "（",
    ")": "）",
}

# 中日韩**字母**（汉字、假名、谚文、扩展区）。用来判断半角标点"有没有 CJK 邻居"。
# 刻意不含 CJK 标点（U+3000–303F）与全角形式：它们是标点不是字，拿标点当邻居会把
# ``[报告](url)。`` 这种链接目标的收尾括号也转成全角，反而不是要的。
_CJK_RE = re.compile(
    r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
    r"\U00020000-\U0003ffff]"
)

# 单位：符号单位与中文单位词。**只收单位，不收量词以外的普通字**——列在这里的记号
# 前面若紧挨数字、中间又只有空格，就把空格去掉（``460 °C`` → ``460°C``、``12 %`` → ``12%``、
# ``187 项`` → ``187项``）。``亿``／``万`` 不是单位而是数词，所以 ``1.5 亿公里`` 不动。
_UNIT = (
    r"(?:%|‰|‱|°C|°F|℃|℉|公里|千米|米|厘米|毫米|微米|纳米|光年|天文单位|"
    r"千克|公斤|克|吨|秒|分钟|小时|天|年|月|日|页|条|项|字|帧|幅|张|台|颗|个|次|"
    r"届|版|份|倍|层|类|组|名|位|人|岁|圈|米/秒|公里/秒)"
)
_DIGIT_UNIT_SPACE_RE = re.compile(r"(?<=\d)[ \t]+(?=" + _UNIT + r")")
_MULTI_SPACE_RE = re.compile(r"[ \t]{2,}")


def _is_cjk(char: str) -> bool:
    """单个字符是不是中日韩字符（空串与拉丁、数字、符号都不是）。"""
    return bool(char) and _CJK_RE.fullmatch(char) is not None


def _transform_run(run: str) -> str:
    """规整一段**不在保护区内**的文本：半角标点、数字与单位间的空格、连续空格。

    半角标点看**原始邻居**：左边或右边紧挨着中日韩字符才转全角（``行星,宜居`` →
    ``行星，宜居``；``Hello, world`` 一动也不动）。``1.5``／``§2.1``／``p.17``／``2020–2023``
    的点号两侧都是数字或拉丁字母，没有 CJK 邻居，因此安全。
    """
    characters = list(run)
    for index, char in enumerate(characters):
        full = _PUNCT_MAP.get(char)
        if full is None:
            continue
        left = characters[index - 1] if index else ""
        right = characters[index + 1] if index + 1 < len(characters) else ""
        if _is_cjk(left) or _is_cjk(right):
            characters[index] = full
    value = _DIGIT_UNIT_SPACE_RE.sub("", "".join(characters))
    return _MULTI_SPACE_RE.sub(" ", value)


def _runs(text: str) -> List[Tuple[bool, str]]:
    """按引号归一自己的保护区（行内代码、公式、链接目标、裸 URL）把文本切成若干段。

    保护区**原样**带过，不参与标点与空格规整——URL 里的逗点、路径里的点号不能动。
    """
    spans = quotes.protected_spans(text)
    runs: List[Tuple[bool, str]] = []
    cursor = 0
    for start, end in spans:
        if start > cursor:
            runs.append((False, text[cursor:start]))
        runs.append((True, text[start:end]))
        cursor = end
    if cursor < len(text):
        runs.append((False, text[cursor:]))
    return runs


def normalize_pick_text(text: str) -> str:
    """把一条 pick 的显示文本规整成排印形式，**不改一个字**，确定性、可重复。

    规则（都只关乎形式）：

    1. ASCII 直引号 ``"`` 按出现顺序配成中文弯引号，复用成书同一套配对器
       （:func:`primer.book.quotes.normalize_double_quotes`）；奇数个直引号时不猜，原样保留。
    2. 与中日韩字符相邻的半角 ``，`` 一类标点转全角（``, . ; : ! ? ( )``）。纯拉丁句里的
       半角逗号没有 CJK 邻居，因此不动。
    3. 数字与**单位**之间的空格去掉（``460 °C`` → ``460°C``、``12 %`` → ``12%``）。单位是
       一张闭合的表（:data:`_UNIT`），``HR 8799``（空格在字母与数字之间）与 ``1.5 亿公里``
       （``亿`` 是数词不是单位）都不在其中。
    4. 连续空格折叠为一个。

    保护区（行内代码、公式、markdown 链接目标、裸 URL）整段带过，一个字符也不动。
    函数是幂等的：规整过的文本再规整一次结果逐字相同。
    """
    pieces: List[str] = []
    for protected, run in _runs(text):
        pieces.append(run if protected else _transform_run(run))
    joined = "".join(pieces)
    return quotes.normalize_double_quotes(joined).text


__all__ = ["normalize_pick_text", "strip_lead_enumerator"]
