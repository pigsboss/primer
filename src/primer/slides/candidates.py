# -*- coding: utf-8 -*-
"""候选主题句：从各章正文里确定性地抽出"可能要点到的那句话"。

三步，都是规则：

1. **切段**：用成书同款的块解析器 :func:`primer.book.markdown.parse_blocks` 解析每一段
   正文，只留 ``Paragraph``——标题、表格、引用、图表题注、分隔线一律跳过（题注即使没
   跟在图后也会被题注正则拦下）。
2. **取句**：段落按 ``(?<=[。；])`` 切成句子，取**第一句**。中文技术写作把主题句放在
   段首，本书对这一体例执行得格外整齐，所以"段首句"就是候选。
3. **打信号、定类型、排序**：五个信号（粗体、数字、引用标记、预报短语、长度 ≤ 120）
   记在候选上；类型按"小结 → 框定 → 断言 → 数据"的次序取第一个命中的规则，一条都不
   命中的段首句按 **断言** 计（本书这种句子是在陈述，不是在提问）。判数字信号与"数据"
   类型前先摘掉 ``[n]`` 引用标记——引用编号里的数字不是句子的数据，不摘掉的话 (b) 与
   (c) 就分不开了。章内排序：**信号数（长句降一档）降序** → 引用数降序 → 章内位置升序。

**长句降一档**：一页幻灯片只放得下 336 字（默认主题；见
:data:`primer.slides.plan.DEFAULT_CAPACITY_PER_PAGE`），
每页排 4 个要点时一个要点约合 84 字，130 字以上的句子即使信号最多也放不满一条要点、
只能拆开或改写成两条，所以它在排序里按"少一个信号"计（:data:`LONG_SENTENCE_CHARS`）。
长句不被丢掉——人可能仍旧想看它，只是不会被摆在首选位置。

**引用数用正则数 ``[n]`` 标记，不读 ``claims.json``**：那份产物的形状是按段落的
``file``/``line`` 存"论断—引用对"，一句话可以散成多对、也可以只截取半句，拿它反推
"这句有几十个引用标记"要另做一次对齐，而本模块要的只是标记个数。口径写在这里，
也写在 ``candidates.md`` 的表头。

页面指针来自 :mod:`primer.slides.structure` 切好的段：指针是该段所属**节的节号**
（``§2.1``；章首、第一节之前的正文写章标签），不再声称页码。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from ..book import markdown as md
from ..book import quotes
from .structure import Chapter, Segment

# 段首句长度上限：一句话要放得进幻灯片的一行预算。
MAX_LENGTH = 120
# 长句阈值：一页 336 字（默认主题）、每页 4 个要点时一个要点约 84 字，超过 130 字的句子放不满
# 一条要点（幻灯片的一格），排序时降一档。
LONG_SENTENCE_CHARS = 130

FORECAST_RE = re.compile(
    r"^(以下|如下|三条|两个|判据|清单|小结|综上|至此|本节|本章|第一|第二|第三)"
)
# 引用标记：``[14]``、``[96–101]``（范围）、``[384]``。范围记号按一个标记计。
CITATION_RE = re.compile(r"\[\d+(?:\s*[–—-]\s*\d+)*\]")
STRONG_RE = re.compile(r"首次|首个|唯一|尚未|从未|最|没有")
SUMMARY_RE = re.compile(r"综上|至此|三条|小结|结论")
DIGIT_RE = re.compile(r"\d")
SENTENCE_END_RE = re.compile(r"(?<=[。；])")

# 信号的短码与中文名，表头会用到。
SIGNAL_CODES = (
    ("bold", "B", "粗体"),
    ("digit", "D", "数字"),
    ("cite", "C", "引用"),
    ("forecast", "F", "预报"),
    ("short", "S", "短句"),
)

TYPE_FRAME = "框定"
TYPE_CLAIM = "断言"
TYPE_DATA = "数据"
TYPE_SUMMARY = "小结"


@dataclass(frozen=True)
class Candidate:
    """一个候选主题句。"""

    id: str
    chapter: str
    chapter_label: str
    type: str
    signals: Tuple[str, ...]
    citations: int
    length: int
    pointer: str
    sentence: str
    position: int

    @property
    def signal_count(self) -> int:
        return len(self.signals)

    @property
    def rank_signals(self) -> int:
        """排序用的信号数：超过 :data:`LONG_SENTENCE_CHARS` 的长句降一档。"""
        return self.signal_count - (1 if self.length > LONG_SENTENCE_CHARS else 0)

    @property
    def display(self) -> str:
        """展示用的句子：清掉跨句残留的粗体标记。``sentence`` 本身不动。

        按 ``(?<=[。；])`` 切句时，``**主题句。** 后半句`` 的收尾 ``**`` 会落在下一句
        的开头，而 ``**主题句。`` 这一句就缺了配对的一侧。计数为奇数即知这一句的强调
        标记不配对，剥掉首尾的 ``*`` 串；成对出现的粗体原样保留。
        """
        return balance_emphasis(self.sentence)

    @property
    def signal_codes(self) -> str:
        lookup = {name: code for name, code, _ in SIGNAL_CODES}
        return "".join(lookup[name] for name in self.signals)


def balance_emphasis(sentence: str) -> str:
    """句子里的粗体标记不配对时，剥掉首尾的 ``*`` 串。"""
    core = sentence.strip()
    if core.count("**") % 2 == 0:
        return core
    return core.lstrip("*").strip().rstrip("*").strip()


def split_sentences(text: str) -> List[str]:
    """按句末标点切句，保留标点；空片段丢弃。"""
    return [part.strip() for part in SENTENCE_END_RE.split(text) if part.strip()]


def is_caption(text: str) -> bool:
    """题注、图片行、说明行、分隔线——这些不是正文段落。"""
    stripped = text.strip()
    return bool(
        md.FIGURE_CAPTION_RE.match(stripped)
        or md.TABLE_CAPTION_RE.match(stripped)
        or md.NOTE_RE.match(stripped)
        or md.IMAGE_RE.match(stripped)
        or stripped == md.HORIZONTAL_RULE
        or stripped.startswith("|")
        or stripped.startswith(">")
    )


def measure(sentence: str) -> int:
    """句子长度：去掉强调标记 ``*`` 后的字符数；引用标记算数（它要占幻灯片的位置）。"""
    return len(sentence.replace("*", "").strip())


def prose(sentence: str) -> str:
    """摘掉引用标记后的正文。

    数字信号与"数据"类型都只看这段正文：``[368]`` 里的数字是引用编号，不是句子的
    数据；不摘掉的话，任何带引用的句子都会白得一个"数字"信号，(b) 与 (c) 也就分不开了。
    """
    return CITATION_RE.sub("", sentence)


def classify(sentence: str, digit: bool, cite: bool, forecast: bool) -> str:
    """按固定次序定类型，第一条命中的规则胜出。

    次序是有意的：**小结 → 框定 → 断言 → 数据 → 断言（兜底）**。两张词表在
    "综上／至此／三条／小结"上重叠，所以一句话只要命中小结词表就算小结——例如
    "以下三条是判据。" 既是预报短语又含"三条"，落在小结一侧。一条规则都不命中的
    段首句按断言计。
    """
    if SUMMARY_RE.search(sentence):
        return TYPE_SUMMARY
    if forecast and not cite and not digit:
        return TYPE_FRAME
    if cite or STRONG_RE.search(sentence):
        return TYPE_CLAIM
    if len(DIGIT_RE.findall(prose(sentence))) >= 2:
        return TYPE_DATA
    return TYPE_CLAIM


def signals_of(sentence: str, length: int) -> Tuple[str, ...]:
    """一句话命中的信号，按固定次序。数字信号先摘掉引用标记再判。"""
    core = sentence.lstrip("*").strip()
    flags = (
        ("bold", "**" in sentence),
        ("digit", bool(DIGIT_RE.search(prose(sentence)))),
        ("cite", bool(CITATION_RE.search(sentence))),
        ("forecast", bool(FORECAST_RE.match(core))),
        ("short", length <= MAX_LENGTH),
    )
    return tuple(name for name, hit in flags if hit)


def candidate_id(chapter: str, segment: Segment, paragraph_ordinal: int) -> str:
    """``s8-6p02``：章、节的章内序号、该节内的第几段。"""
    return f"s{chapter}-{segment.section_ordinal}p{paragraph_ordinal:02d}"


def extract(chapter: Chapter) -> List[Candidate]:
    """抽出一章的全部候选（按文档顺序，未排序）。"""
    out: List[Candidate] = []
    counters: Dict[int, int] = {}
    position = 0
    for segment in chapter.segments:
        for block in md.parse_blocks(segment.lines):
            if not isinstance(block, md.Paragraph):
                continue
            text = block.text.strip()
            if not text or is_caption(text):
                continue
            sentences = split_sentences(text)
            if not sentences:
                continue
            sentence = sentences[0]
            counters[segment.section_ordinal] = counters.get(segment.section_ordinal, 0) + 1
            position += 1
            length = measure(sentence)
            signals = signals_of(sentence, length)
            out.append(
                Candidate(
                    id=candidate_id(chapter.number, segment, counters[segment.section_ordinal]),
                    chapter=chapter.number,
                    chapter_label=chapter.label,
                    type=classify(
                        sentence,
                        digit="digit" in signals,
                        cite="cite" in signals,
                        forecast="forecast" in signals,
                    ),
                    signals=signals,
                    citations=len(CITATION_RE.findall(sentence)),
                    length=length,
                    pointer=segment.pointer,
                    sentence=sentence,
                    position=position,
                )
            )
    return out


def rank(candidates: Sequence[Candidate]) -> List[Candidate]:
    """章内排序：长句降一档后的信号数降序 → 引用数降序 → 章内位置升序。

    第一键是**降档后的**信号数：130 字以上的句子按"少一个信号"计，所以一条 213 字、
    三信号的句子排在一条 68 字、三信号的句子后面——它放不满一页，不该被当作首选。
    """
    return sorted(
        candidates,
        key=lambda item: (-item.rank_signals, -item.citations, item.position),
    )


def extract_all(chapters: Sequence[Chapter]) -> Dict[str, List[Candidate]]:
    """逐章抽取并排序，按章号索引。"""
    return {chapter.number: rank(extract(chapter)) for chapter in chapters}


def count_by_type(candidates: Sequence[Candidate]) -> Dict[str, int]:
    """按类型计数（固定类型次序，便于渲染）。"""
    order = (TYPE_SUMMARY, TYPE_FRAME, TYPE_CLAIM, TYPE_DATA)
    return {name: sum(1 for item in candidates if item.type == name) for name in order}


def find(candidates: Sequence[Candidate], identifier: str) -> Optional[Candidate]:
    """按 id 取候选（给将来的 ``picks`` 校验留的入口）。"""
    return next((item for item in candidates if item.id == identifier), None)


# ---------------------------------------------------------------- pick 的三种形状

# 候选 id 的语法：``s2-4p01``（章序号-节序号p句序号）。与 :func:`candidate_id` 同一套写法
# ——章是短号（附录 A 因此是 ``sA-3p02``），节序号与句序号是十进制——所以章段收
# ``[0-9A-Za-z]+``。这是一条**模块级**正则：``check`` 与 ``build`` 都拿它来判断一条 pick
# 是候选 id 还是作者手写的句子，两处判断必然一致。
PICK_ID_RE = re.compile(r"^s[0-9A-Za-z]+-[0-9]+p[0-9]+$")

# 一条 pick 的两种去向：候选表里的一行（``candidate``），或一句话本身（``text``）。
PICK_CANDIDATE = "candidate"
PICK_TEXT = "text"


class PickError(ValueError):
    """一条 pick 读不出来。

    ``code`` 是稳定的机器可读标识：``pick-empty`` 是空串或空句子，``pick-shape`` 是既
    不是字符串、也不是 ``{text, derived_from}`` 映射，``pick-missing`` 是提炼句的
    ``derived_from`` 指着候选表里没有的 id。``check`` 把它原样翻成 finding。``message``
    是短语，由调用方接在人读位置（章／固定页）之后。
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Pick:
    """一条 pick 解析后的形状——三种写法归一到这里。

    一条 pick 可以是**候选 id**（``s2-4p01``，指着 ``candidates.md`` 的一行）、**自由
    文本**（作者手写的句子），或**提炼句**（机器从若干候选句提炼出的新句，出处记在
    ``derived_from`` 里）。后两者渲染上没有区别：句子经 :func:`pick_display` 取成显示文本
    再送 :func:`primer.book.tex.render_inline`，所以 ``kind`` 只有 ``candidate`` 与 ``text``
    两种，提炼句由非空的 ``derived_from`` 标出。``derived_from: []`` 或省略键即人写的自由
    文本，等同形状二。
    """

    kind: str
    text: str = ""
    identifier: str = ""
    derived_from: Tuple[str, ...] = ()

    @property
    def is_candidate(self) -> bool:
        return self.kind == PICK_CANDIDATE

    @property
    def derived(self) -> bool:
        """是不是机器提炼句：与自由文本同走渲染，区别只在这句话有若干候选句做出处。"""
        return self.kind == PICK_TEXT and bool(self.derived_from)

    def refs(self) -> Tuple[str, ...]:
        """这一条 pick 指着的候选 id：候选 id 是它自己，提炼句是它的出处，自由文本为空。"""
        return (self.identifier,) if self.is_candidate else self.derived_from


def parse_pick(value: object, candidates: Mapping[str, object]) -> Pick:
    """把骨架里的一条 pick 解析成 :class:`Pick`；形状读不出来时抛 :class:`PickError`。

    三种写法：匹配 :data:`PICK_ID_RE` 的字符串是候选 id；不匹配的字符串是自由文本
    （空串与纯空白除外）；映射取 ``text``（非空字符串）与 ``derived_from``（候选 id
    列表），其余键一律忽略，为下一轮留位置。``candidates`` 只需提供键，用来核对
    ``derived_from``——候选 id 本身在不在表里由调用方另报，好让 ``pick-missing`` 的消息
    保持原样。
    """
    if isinstance(value, str):
        if PICK_ID_RE.match(value):
            return Pick(kind=PICK_CANDIDATE, identifier=value)
        if not value.strip():
            raise PickError("pick-empty", "有一条 pick 是空的：既不是候选 id，也不是一句话")
        return Pick(kind=PICK_TEXT, text=value)
    if isinstance(value, Mapping):
        return _parse_pick_mapping(value, candidates)
    raise PickError(
        "pick-shape",
        "有一条 pick 的形状读不出来：既不是候选 id，也不是句子，"
        f"也不是 {{text, derived_from}}（得到 {type(value).__name__}）",
    )


def _parse_pick_mapping(value: Mapping, candidates: Mapping[str, object]) -> Pick:
    """``{text, derived_from}`` 这一种形状：``text`` 是非空字符串，出处须在候选表里。"""
    text = value.get("text")
    if not isinstance(text, str) or not text.strip():
        raise PickError("pick-empty", "有一条提炼句/自由文本的 text 不是非空字符串")
    raw = value.get("derived_from")
    if raw is None or raw == []:
        derived: Tuple[str, ...] = ()
    elif isinstance(raw, (list, tuple)) and all(isinstance(item, str) for item in raw):
        derived = tuple(raw)
        for identifier in derived:
            if identifier not in candidates:
                raise PickError(
                    "pick-missing",
                    f"提炼句 derived_from 列出了候选 {identifier!r}，但候选表里没有这个 id",
                )
    else:
        raise PickError("pick-missing", "提炼句 derived_from 不是候选 id 列表")
    return Pick(kind=PICK_TEXT, text=text, derived_from=derived)


def pick_display(pick: Pick, rows: Mapping[str, "CandidateRow"]) -> str:
    """一条 pick 的显示文本：候选 id 取候选表里的句子，句子取它自己。

    **三种形状到显示文本的换算只有这一处**；调用方再把它送行内渲染器。候选 id 不在
    表里（``pick-missing``）时给空串，调用方照旧打待选记号。

    显示文本在这里接上**成书同一套引号规整**（:mod:`primer.book.quotes`）：源 markdown 的
    ASCII 直引号（U+0022）在 CJK 字体下会排成全角收拢形，看起来像方向错的引号。配对按行
    进行，奇数个的那一行原样保留——那是 ``quotes-unpaired`` 警告要人来看的情形。这是
    pick 变幻灯片文字的**唯一**咽喉点，``check`` 与 ``build`` 因此看到同一份文本。
    """
    if pick.is_candidate:
        row = rows.get(pick.identifier)
        text = row.display if row is not None else ""
    else:
        text = pick.text
    return quotes.normalize_double_quotes(text).text


def pick_length(pick: Pick, lengths: Mapping[str, int]) -> int:
    """一条 pick 计入的选取字数：候选 id 用候选表的口径，句子用 ``len(text)``。

    与 :func:`pick_display` 同在一处，保证 ``check`` 的容量核算与 ``build`` 实际放上页
    的字数出自同一个判断。
    """
    if pick.is_candidate:
        return lengths.get(pick.identifier, 0)
    return len(pick.text)


# ---------------------------------------------------------------- 读回候选表


@dataclass(frozen=True)
class CandidateRow:
    """``candidates.md`` 里的一行，人圈选时看到的就是这些字段。

    ``display`` 是**句子**那一列的原样：抽取时已把跨句残留的强调标记配平，所以它带着
    markdown 的 ``**`` 与 ``[n]``，正是要送进成书同一个行内渲染器的文本。
    """

    id: str
    type: str
    signal_codes: str
    citations: int
    length: int
    pointer: str
    display: str


# 单元格分隔：被 ``escape_cell`` 转义过的竖线 ``\|`` 不算分隔符。
CELL_SPLIT_RE = re.compile(r"(?<!\\)\|")


# 候选表的列名 → 字段。表头驱动（而不是数第几列）：``deck.yaml`` 的 ``pointers: none``
# 时整列「页面指针」都不发，按位置读表会读串行。
COLUMN_FIELDS = {
    "id": "id",
    "类型": "type",
    "信号": "signal_codes",
    "引用": "citations",
    "字数": "length",
    "页面指针": "pointer",
    "句子": "sentence",
}
# 分隔行（``|---|---|``）：整行只由 : - 与空白组成。
SEPARATOR_RE = re.compile(r"^[\s:|-]+$")
# 没有表头时的兜底列位（旧版产物）：与 COLUMN_FIELDS 的次序一致。
LEGACY_COLUMNS = ("id", "type", "signal_codes", "citations", "length", "pointer", "sentence")


def _header_fields(cells: Sequence[str]) -> Optional[Tuple[str, ...]]:
    """一行是不是候选表的表头；是就给出逐列的字段名。"""
    names = [cell.strip() for cell in cells]
    if len(names) < 5 or names[1] != "id":
        return None
    fields: List[str] = []
    for name in names[1:]:
        if name not in COLUMN_FIELDS:
            return None
        fields.append(COLUMN_FIELDS[name])
    if "id" not in fields or "sentence" not in fields or "length" not in fields:
        return None
    return tuple(fields)


def parse_markdown_table(text: str) -> "Dict[str, CandidateRow]":
    """候选表 → ``id -> 行``。

    按**表头**认列：表头那一行写明 ``id / 类型 / 信号 / 引用 / 字数 / 页面指针 / 句子``，
    逐列按名字取（``pointers: none`` 的 deck 没有「页面指针」列，按位置读会读串行）。
    没有表头的旧产物退回按位置读（:data:`LEGACY_COLUMNS`）。只认候选行：首列以 ``s`` 开头、
    「字数」列是整数。句子里被转义的竖线还原，未转义的竖线按分隔符切开后再拼回。
    """
    found: Dict[str, CandidateRow] = {}
    header: Optional[Tuple[str, ...]] = None
    for line in text.splitlines():
        if not line.lstrip().startswith("|"):
            continue
        cells = CELL_SPLIT_RE.split(line)
        names = _header_fields(cells)
        if names is not None:
            header = names
            continue
        if SEPARATOR_RE.match(line.strip()):
            continue
        fields = header or LEGACY_COLUMNS
        values = [cell.strip() for cell in cells[1:]]
        if values and not values[-1] and line.rstrip().endswith("|"):
            values.pop()  # 行尾那一个空单元是结构性的（表格行末的竖线）
        if len(values) < len(fields):
            values += [""] * (len(fields) - len(values))
        row = dict(zip(fields, values))
        sentence = row.get("sentence", "")
        # 「句子」那一列的原文若自带未转义的竖线，会被切成多列：多出来的拼回句子。
        extra = values[len(fields):]
        if extra:
            sentence = "|".join([sentence, *extra])
        identifier = row.get("id", "")
        length = row.get("length", "")
        if not identifier.startswith("s") or not length.isdigit():
            continue
        citations = row.get("citations", "")
        found[identifier] = CandidateRow(
            id=identifier,
            type=row.get("type", ""),
            signal_codes=row.get("signal_codes", ""),
            citations=int(citations) if citations.isdigit() else 0,
            length=int(length),
            pointer=row.get("pointer", ""),
            display=sentence.strip().replace("\\|", "|"),
        )
    return found
