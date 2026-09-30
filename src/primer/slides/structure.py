# -*- coding: utf-8 -*-
"""书的结构：**只看源 markdown 与规格**，不读成书清单，也不读成书的 ``.toc/.lof``。

幻灯片要的东西只有三样：章与节的身份（章号、章标签、节号、标题）、每段正文属于哪一节、
一章里按次序出现了哪几幅插图。三样都能从源 markdown 与 ``deck.yaml`` 直接复算，于是
"书没重编"不再挡住幻灯片，成书重编后页码变化也不再让候选表的指针作废。

规则（与源文件的实际情况一一对应）：

* **正常篇**：``## `` 开一章，``### `` 开一节，更深的标题是小节；章的**序号**是篇内序号，
  章号从 ``sources[].chapter_start`` 起数（第一篇 1—5、第二篇 6—9、第三篇 10—14）。
* **附录篇**（``appendix: true``）：整篇就是一章，章题取源文件的一级标题（去掉
  ``附录 A　`` 前缀）；篇内 ``## `` 是节、``### `` 是小节——整体**下沉一级**，与成书把
  附录篇题做成 ``\\chapter`` 是同一件事。
* **节号**分级编：章号 + 节在该章内的序号（第 2 章的第 1 节是 ``2.1``），小节再往下一级
  （``A.3.3.1``）；同一层的序号**在同级里各自从 1 数**（``A.3`` 底下是 ``A.3.1``，不是接着
  ``A.2`` 的数往下走）。比小节更深的标题不再单起节号，它以下的正文归最近的有号的祖先——
  与成书目录能给到的粒度一致。这套编号与成书目录里的节号逐字相同。
* **章标签**（纸面写法）由 ``deck.yaml`` 的 ``numbering`` 定：``第{cn}章``／``附录 {number}``。
* **标题文本**先剥掉行首的编号（``1.2 ``、``A.2.1 ``、``附录 A　``），再走成书同一套引号
  规整（:func:`primer.book.quotes.normalize_double_quotes`）——源文件里是 ASCII 直引号，
  成书与幻灯片上都应是中文弯引号，这属于形式，由排版系统决定。
* **指针**：正文段所属节的指针写作 ``§2.1``（章首、第一节标题之前的正文写作章标签
  ``第二章``）；不再声称页码。``deck.yaml`` 的 ``pointers: none`` 时一个指针都不发。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Mapping, Sequence, Tuple

from ..book import markdown as md
from ..book import quotes
from ..paths import relative_to_root
from .plan import SlidesError
from .spec import DeckSpec, SourceSpec

SOURCE_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
# 章题行首的编号（``1.2``／``3.1``）；附录不收，篇题由 :func:`_file_title` 剥。
CHAPTER_NUMBER_RE = re.compile(r"^\d+(?:\.\d+)*[\s　]*")
# 节题行首的编号：``1.2.1``／``A.2.1``／``A.3.3.1``。要求整段长得像编号（字母只带一个小节
# 点号），所以 ``NASA 的技术缺口`` 这类标题一个字都不会被剥掉。
SECTION_NUMBER_RE = re.compile(r"^(?:[A-Za-z]\.\d+(?:\.\d+)*|\d+(?:\.\d+)*)[\s　]*")
APPENDIX_TITLE_RE = re.compile(r"^附录\s*[0-9A-Za-z]+\s*[　\s]*")

# 目录／骨架里的标题层级名，与成书 ``.toc`` 的层级名一致（``section`` 是章的下一级）。
TOC_SECTION_LEVELS = ("section", "subsection", "subsubsection")
# 中文数字（章标签用）：1—99 够用，且比装一个库便宜。
_CN_DIGITS = "零一二三四五六七八九"


def chinese_numeral(number: int) -> str:
    """``1`` → ``一``、``10`` → ``十``、``14`` → ``十四``（1—99）。"""
    if not 1 <= number <= 99:
        raise SlidesError(f"chapter number out of range for a Chinese label: {number}")
    if number < 10:
        return _CN_DIGITS[number]
    if number < 20:
        return "十" + (_CN_DIGITS[number - 10] if number > 10 else "")
    tens, ones = divmod(number, 10)
    return _CN_DIGITS[tens] + "十" + (_CN_DIGITS[ones] if ones else "")


@dataclass(frozen=True)
class Section:
    """章下的一个节（含更深层级）。``level`` 取 :data:`TOC_SECTION_LEVELS` 之一。"""

    level: str
    number: str
    title: str


@dataclass(frozen=True)
class Segment:
    """章内一段连续正文，附带它所属节的指针与节的章内序号。

    指针是 ``§2.1`` 或章标签（章首、第一节之前的正文）；``section_ordinal`` 是候选 id 里的
    那个数（章首正文记 0）。
    """

    pointer: str
    section_ordinal: int
    lines: Tuple[str, ...]


@dataclass(frozen=True)
class FigureRef:
    """一章里的一幅插图：章号-章内序号（``2-3``）、短题、相对 ``source_root`` 的图片路径。"""

    number: str
    caption: str
    path: str


@dataclass(frozen=True)
class Chapter:
    """一章：编号身份、节清单、已按节切好的源文件正文与材料量。

    ``number`` 是短号（``"8"``、``"A"``），``label`` 是纸面写法（``第八章``、``附录 A``）。
    ``material_chars`` 是这一章正文的字符数（不算标题行与空行）——报告里的"体量"。
    """

    number: str
    label: str
    title: str
    volume_id: str
    appendix: bool
    source: str
    material_chars: int
    sections: Tuple[Section, ...]
    segments: Tuple[Segment, ...]

    def figures(self) -> Tuple[FigureRef, ...]:
        """本章按文档次序出现的插图，编号 ``{章号}-{章内序号}``（从 1 数）。

        走的是成书同一个块解析器：``![…](…)`` 图片行与紧随的 ``*图 x-y　短题*`` 题注，
        短题取源题注里那一段。**不读成书的插图目录**——成书按章重编，这里也按章重编，
        同一个语义不必假手他人；源文件改了而书没重编，插图也不会跟着失准。
        """
        found: List[FigureRef] = []
        seen = 0
        for segment in self.segments:
            for block in md.parse_blocks(segment.lines):
                if not isinstance(block, md.Figure):
                    continue
                seen += 1
                found.append(FigureRef(f"{self.number}-{seen}", block.caption, block.path))
        return tuple(found)


@dataclass(frozen=True)
class DeckStructure:
    """一份规格复算出来的结构：书的书目、各章、指针口径。"""

    title: str
    subtitle: str
    chapters: Tuple[Chapter, ...]
    pointers: str

    @property
    def material_chars(self) -> int:
        return sum(chapter.material_chars for chapter in self.chapters)

    @property
    def book_line(self) -> str:
        return f"{self.title}{self.subtitle}"


@dataclass(frozen=True)
class _SourceChapter:
    """源文件切出来的一"章"，还没配上章号。``lines`` 含它自己的标题行。"""

    volume_id: str
    path: Path
    title: str
    lines: Tuple[str, ...]


@dataclass(frozen=True)
class _Heading:
    """一章里一个有号的标题：在第几行、节号、在目录里的层级名、标题、所属节序号。"""

    index: int
    level: int
    number: str
    level_name: str
    title: str
    ordinal: int


def _clean(text: str) -> str:
    """按行规整引号（成书同一套），成书与幻灯片的标题因此逐字相同。"""
    return quotes.normalize_double_quotes(text.strip()).text.strip()


def _section_title(raw: str) -> str:
    return _clean(SECTION_NUMBER_RE.sub("", _clean(raw)))


def _file_title(raw_lines: Sequence[str], source: SourceSpec) -> str:
    """附录篇的章题：源文件的一级标题去掉 ``附录 A　`` 前缀。"""
    for line in raw_lines:
        heading = SOURCE_HEADING_RE.match(line)
        if heading and len(heading.group(1)) == 1:
            return _clean(APPENDIX_TITLE_RE.sub("", _clean(heading.group(2))))
    raise SlidesError(f"appendix source has no level-1 title: {source.path}")


def _split_source(spec: DeckSpec, source: SourceSpec, base: Path) -> List[_SourceChapter]:
    """把一篇源文件切成章（附录篇整篇一章）。"""
    path = base / source.path
    try:
        raw_lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise SlidesError(f"source file not readable: {path}: {exc}") from exc
    patterns = [re.compile(pattern) for pattern in spec.drop_lines]
    body = md.drop_matching(md.strip_preamble(raw_lines), patterns)

    if source.appendix:
        return [_SourceChapter(source.id, path, _file_title(raw_lines, source), tuple(body))]

    groups: List[List[str]] = []
    for line in body:
        heading = SOURCE_HEADING_RE.match(line)
        if heading and len(heading.group(1)) == spec.split.chapter:
            groups.append([line])
            continue
        if groups:
            groups[-1].append(line)
    if not groups:
        raise SlidesError(f"no chapter heading (level {spec.split.chapter}) in {source.path}")
    chapters: List[_SourceChapter] = []
    for group in groups:
        head = SOURCE_HEADING_RE.match(group[0])
        title = _clean(CHAPTER_NUMBER_RE.sub("", _clean(head.group(2))))
        chapters.append(_SourceChapter(source.id, path, title, tuple(group)))
    return chapters


def _headings(
    lines: Sequence[str], section_level: int, chapter_number: str
) -> Tuple[Tuple[_Heading, ...], Tuple[Section, ...]]:
    """走一遍一章的标题，给出有号的标题与节清单。

    同层的序号在同级里各自从 1 数（``A.3`` 底下从 ``A.3.1`` 起），靠"遇到更浅的标题就把
    更深的计数器清掉"实现；比小节更深的标题不编号（``offset`` 到 ``subsubsection`` 为止）。
    """
    headings: List[_Heading] = []
    sections: List[Section] = []
    counts: Dict[int, int] = {}
    prefixes: Dict[int, str] = {}
    ordinal = 0
    for index, line in enumerate(lines):
        matched = SOURCE_HEADING_RE.match(line)
        if not matched:
            continue
        level = len(matched.group(1))
        if level < section_level:
            continue
        counts[level] = counts.get(level, 0) + 1
        for deeper in [key for key in counts if key > level]:
            del counts[deeper]
        offset = level - section_level
        if offset == 0:
            ordinal = counts[level]
            parent = chapter_number
        elif offset < len(TOC_SECTION_LEVELS):
            parent = prefixes.get(level - 1, "")
            if not parent:
                continue
        else:
            continue
        number = f"{parent}.{counts[level]}"
        prefixes[level] = number
        for deeper in [key for key in prefixes if key > level]:
            del prefixes[deeper]
        title = _section_title(matched.group(2))
        level_name = TOC_SECTION_LEVELS[offset]
        headings.append(_Heading(index, level, number, level_name, title, ordinal))
        sections.append(Section(level_name, number, title))
    return tuple(headings), tuple(sections)


def _segments(
    lines: Sequence[str],
    section_level: int,
    headings: Sequence[_Heading],
    pointers: str,
    chapter_pointer: str,
) -> Tuple[Segment, ...]:
    """把一章的正文按标题切成段，每段带上所属节的指针与节的章内序号。"""
    by_index: Mapping[int, _Heading] = {heading.index: heading for heading in headings}
    pointers_on = pointers != "none"
    segments: List[Segment] = []
    buffered: List[str] = []
    pointer = chapter_pointer if pointers_on else ""
    ordinal = 0

    def emit() -> None:
        if buffered:
            segments.append(Segment(pointer, ordinal, tuple(buffered)))

    for index, line in enumerate(lines):
        matched = SOURCE_HEADING_RE.match(line)
        level = len(matched.group(1)) if matched else 0
        if not matched or level < section_level:
            buffered.append(line)
            continue
        emit()
        buffered = [line]
        heading = by_index.get(index)
        if heading is None:
            continue
        ordinal = heading.ordinal
        pointer = f"§{heading.number}" if pointers_on else ""
    emit()
    return tuple(segments)


def _material_chars(lines: Sequence[str], skip_head: int) -> int:
    """一章的正文材料量（字符数）：标题行不算，空行不算。"""
    total = 0
    for line in lines[skip_head:]:
        stripped = line.strip()
        if not stripped or SOURCE_HEADING_RE.match(line):
            continue
        total += len(stripped)
    return total


def read_structure(spec: DeckSpec) -> DeckStructure:
    """按规格读源文件，返回整份 deck 的结构。"""
    base = spec.root / spec.source_root
    chapters: List[Chapter] = []
    for source in spec.sources:
        appendix = source.appendix
        section_level = spec.split.chapter if appendix else spec.split.section
        for ordinal, raw in enumerate(_split_source(spec, source, base)):
            number = source.number_at(ordinal)
            if appendix:
                label = spec.numbering.appendix_name(str(source.chapter_start))
            else:
                label = spec.numbering.chapter_label(int(number), chinese_numeral(int(number)))
            headings, sections = _headings(raw.lines, section_level, number)
            chapters.append(
                Chapter(
                    number=number,
                    label=label,
                    title=raw.title,
                    volume_id=source.id,
                    appendix=appendix,
                    source=relative_to_root(raw.path, spec.root),
                    material_chars=_material_chars(raw.lines, 1),
                    sections=sections,
                    segments=_segments(
                        raw.lines, section_level, headings, spec.pointers, label
                    ),
                )
            )
    if not chapters:
        raise SlidesError(f"no chapter found in the sources of {spec.path}")
    return DeckStructure(
        title=spec.title,
        subtitle=spec.subtitle,
        chapters=tuple(chapters),
        pointers=spec.pointers,
    )
