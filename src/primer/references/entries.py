# -*- coding: utf-8 -*-
"""从任意 markdown 中解析参考文献列表：条目切分、尾标分类、字段抽取与清单校验。

条目边界是行首的 ``[n] ``；尾标 ``［原文：…］`` 说明原文的获取方式，
据此分成 ``local`` / ``public_web`` / ``awaiting`` / ``none`` 四类。

字段抽取是尽力而为的：``作者. 标题. 出处`` 按 ``. `` 切段，中文或机构条目
可能出现偏差，这些字段只用于报表与检索，不作为判据。判据是编号与 arXiv 号。

关于续行：本文件里条目之间用 ``---`` 分隔，并按 markdown 习惯偶尔插入说明行。
因此主题分隔线（``---`` / ``***`` / ``___``）与标题一律关闭当前条目且自身不作续行，
其余非空行只要当前条目仍打开就并入其正文。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional, Sequence

from ..literature.catalog import normalize_arxiv
from ..literature.paths import relative_to_root

ENTRY_START = re.compile(r"^\[(?P<number>\d+)\]\s+(?P<body>.*)$")
HEADING = re.compile(r"^(?P<hashes>#{1,6})\s+(?P<label>[A-Z]\+*)(?P<index>\d*)\s*(?P<title>.*)$")
THEMATIC_BREAK = re.compile(r"^(?:-{3,}|\*{3,}|_{3,})\s*$")
TAG = re.compile(r"［原文：(?P<tag>[^］]*)］")
ARXIV_PROSE = re.compile(r"arXiv[:\s]*(?P<id>\d{4}[._]\d{4,5})", re.IGNORECASE)
DOI_PROSE = re.compile(r"DOI[:\s]*(?P<doi>[^\s，。；;）)]+)", re.IGNORECASE)
DECLARED_TOTAL = re.compile(r"共\s*(?P<total>\d+)\s*条")
DECLARED_STRUCTURE = re.compile(r"^>\s*结构：\s*(?P<body>.+)$")
DECLARED_SECTION = re.compile(r"^(?P<label>[A-Z]\+*)\s")
_SEGMENT = re.compile(r"\.\s+")
_AUTHOR_TAIL = re.compile(r"^(?:et\s+al\.?|al\.?|and\s+others|[A-Z]\.?)$", re.IGNORECASE)

TAG_LOCAL = "local"
TAG_PUBLIC_WEB = "public_web"
TAG_AWAITING = "awaiting"
TAG_NONE = "none"

_LOCAL_MARKER = "已存本地"
_AWAITING_MARKER = "待图书馆获取"
_PUBLIC_MARKERS = ("官网公开", "NTRS 公开", "白宫", "CBO")


@dataclass
class RefEntry:
    """参考文献列表里的一条条目。``raw`` 是去掉尾标后的正文（不含 ``[n]`` 标记）。"""

    number: int
    raw: str
    tag: Optional[str]
    tag_kind: str
    authors: str
    title: str
    venue_year: str
    tag_arxiv: Optional[str]
    prose_arxiv: Optional[str]
    doi: Optional[str]
    class_letter: Optional[str]
    class_title: Optional[str]
    source: str
    line: int

    @property
    def arxiv(self) -> Optional[str]:
        """尾标里的 arXiv 号优先，其次取自正文。"""
        return self.tag_arxiv or self.prose_arxiv


@dataclass
class RefList:
    """一个 markdown 文件（或其中的一段）里发现的参考文献列表。"""

    source: str
    entries: list[RefEntry]
    declared_total: Optional[int] = None
    declared_sections: list[str] = field(default_factory=list)
    preamble: list[str] = field(default_factory=list)


@dataclass
class ListValidation:
    """单个列表的自洽性检查：申报总数、章节字母、重号与断号。

    ``missing_letters`` 是申报了却找不到条目的章节，``undeclared_letters``
    是条目里出现了但申报结构里没写的章节。
    """

    source: str
    declared_total: Optional[int]
    actual_count: int
    declared_letters: list[str]
    present_letters: list[str]
    missing_letters: list[str]
    undeclared_letters: list[str]
    duplicates: list[int]
    gaps: list[int]

    @property
    def total_delta(self) -> Optional[int]:
        return None if self.declared_total is None else self.actual_count - self.declared_total


def classify_tag(tag: Optional[str]) -> str:
    """按尾标文本判定原文获取方式。"""
    if not tag:
        return TAG_NONE
    if _LOCAL_MARKER in tag:
        return TAG_LOCAL
    if _AWAITING_MARKER in tag:
        return TAG_AWAITING
    if any(marker in tag for marker in _PUBLIC_MARKERS):
        return TAG_PUBLIC_WEB
    return TAG_NONE


def split_citation(text: str) -> tuple[str, str, str]:
    """把题录切成 ``(作者, 标题, 出处)``；切不出时退化为 ``(首段, 全文, "")``。"""
    segments = [segment.strip(" .") for segment in _SEGMENT.split(text)]
    segments = [segment for segment in segments if segment]
    if not segments:
        return "", text.strip(), ""
    authors = segments[0]
    title = ""
    for segment in segments[1:]:
        if _AUTHOR_TAIL.match(segment):
            continue
        title = segment
        break
    if not title:
        title = authors
    venue = segments[-1] if len(segments) > 1 else ""
    if venue == title:
        venue = ""
    return authors, title, venue


def _build_entry(
    number: int,
    text: str,
    line: int,
    source: str,
    class_letter: Optional[str],
    class_title: Optional[str],
) -> RefEntry:
    tag_match = TAG.search(text)
    tag = tag_match.group("tag").strip() if tag_match else None
    raw = re.sub(r"\s+", " ", TAG.sub("", text)).strip()
    tag_arxiv_match = ARXIV_PROSE.search(tag) if tag else None
    tag_arxiv = normalize_arxiv(tag_arxiv_match.group("id")) if tag_arxiv_match else None
    prose_match = ARXIV_PROSE.search(raw)
    prose_arxiv = normalize_arxiv(prose_match.group("id")) if prose_match else None
    doi_match = DOI_PROSE.search(raw)
    authors, title, venue = split_citation(raw)
    return RefEntry(
        number=number,
        raw=raw,
        tag=tag,
        tag_kind=classify_tag(tag),
        authors=authors,
        title=title,
        venue_year=venue,
        tag_arxiv=tag_arxiv,
        prose_arxiv=prose_arxiv,
        doi=doi_match.group("doi").rstrip(".,;") if doi_match else None,
        class_letter=class_letter,
        class_title=class_title,
        source=source,
        line=line,
    )


def parse_markdown(text: str, source: str = "<memory>") -> list[RefList]:
    """解析一份 markdown，返回其中发现的所有参考文献列表（可为空）。

    列表边界是一级标题（``# ``）：条目已出现后再遇到一级标题即另起一个列表，
    因此同一文件里可以并排放多份清单，不同文件也可各带一份。
    """
    lists: list[RefList] = []
    entries: list[RefEntry] = []
    preamble: list[str] = []
    declared_total: Optional[int] = None
    declared_sections: list[str] = []
    class_letter: Optional[str] = None
    class_title: Optional[str] = None
    current: Optional[int] = None
    number: Optional[int] = None
    buffer: list[str] = []

    def flush_entry() -> None:
        nonlocal current, number, buffer
        if current is not None and number is not None:
            entries[current] = _build_entry(
                number, " ".join(buffer), entry_line, source, entry_class[0], entry_class[1]
            )
        current, number, buffer = None, None, []

    def flush_list() -> None:
        nonlocal entries, preamble, declared_total, declared_sections
        flush_entry()
        if entries:
            lists.append(
                RefList(
                    source=source,
                    entries=entries,
                    declared_total=declared_total,
                    declared_sections=declared_sections,
                    preamble=preamble,
                )
            )
        entries, preamble, declared_total, declared_sections = [], [], None, []

    entry_line = 0
    entry_class: list[Optional[str]] = [None, None]

    for line_no, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        heading = HEADING.match(stripped)
        if heading:
            if heading.group("hashes") == "#":
                flush_list()
                class_letter, class_title = None, None
            else:
                flush_entry()
                class_letter = heading.group("label")
                class_title = heading.group("title").strip() or None
                entry_class = [class_letter, class_title]
            continue
        if THEMATIC_BREAK.match(stripped):
            flush_entry()
            continue
        if stripped.startswith(">"):
            flush_entry()
            preamble.append(stripped.lstrip("> ").strip())
            total = DECLARED_TOTAL.search(stripped)
            if total:
                declared_total = int(total.group("total"))
            structure = DECLARED_STRUCTURE.match(stripped)
            if structure:
                declared_sections = _parse_sections(structure.group("body"))
            continue
        if not stripped:
            continue
        start = ENTRY_START.match(line)
        if start:
            flush_entry()
            number = int(start.group("number"))
            buffer = [start.group("body").strip()]
            current = len(entries)
            entries.append(None)  # type: ignore[arg-type]  # 占位，flush 时回填
            entry_line = line_no
            entry_class = [class_letter, class_title]
            continue
        if current is not None:
            buffer.append(stripped)
    flush_list()
    return lists


def _parse_sections(body: str) -> list[str]:
    """从 ``A 战略规划 [1–32] / B 系外行星 [33–108] / …`` 里取出章节字母。"""
    labels: list[str] = []
    for part in body.split("/"):
        match = DECLARED_SECTION.match(part.strip())
        if match and match.group("label") not in labels:
            labels.append(match.group("label"))
    return labels


def discover_lists(
    paths: Sequence[Path], *, project_root: Optional[Path] = None
) -> list[RefList]:
    """读取若干 markdown 文件，汇总其中发现的所有参考文献列表。

    列表的 ``source`` 记成相对 ``project_root`` 的形式（产物要能跟着工程搬家）。
    """
    found: list[RefList] = []
    for path in paths:
        text = Path(path).read_text(encoding="utf-8")
        found.extend(parse_markdown(text, source=relative_to_root(path, project_root)))
    return found


def all_entries(lists: Iterable[RefList]) -> list[RefEntry]:
    """把所有列表的条目按出现顺序摊平。"""
    return [entry for ref_list in lists for entry in ref_list.entries]


def validate(ref_list: RefList) -> ListValidation:
    """校验单个列表：申报总数、章节字母、重号与断号。"""
    numbers = [entry.number for entry in ref_list.entries]
    seen: set[int] = set()
    duplicates: list[int] = []
    for number in numbers:
        if number in seen and number not in duplicates:
            duplicates.append(number)
        seen.add(number)
    present: list[str] = []
    for entry in ref_list.entries:
        if entry.class_letter and entry.class_letter not in present:
            present.append(entry.class_letter)
    gaps: list[int] = []
    if numbers:
        expected = set(range(min(numbers), max(numbers) + 1))
        gaps = sorted(expected - set(numbers))
    return ListValidation(
        source=ref_list.source,
        declared_total=ref_list.declared_total,
        actual_count=len(numbers),
        declared_letters=list(ref_list.declared_sections),
        present_letters=present,
        missing_letters=[letter for letter in ref_list.declared_sections if letter not in present],
        undeclared_letters=[letter for letter in present if letter not in ref_list.declared_sections],
        duplicates=duplicates,
        gaps=gaps,
    )


def validate_all(lists: Iterable[RefList]) -> list[ListValidation]:
    """校验一组列表。"""
    return [validate(ref_list) for ref_list in lists]
