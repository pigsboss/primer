# -*- coding: utf-8 -*-
"""markdown 子集解析：把 LLM 产出的 markdown 行序列解析为块对象。

只覆盖成书流水线用到的子集：二级至四级标题、段落、有序／无序列表、代码块、
引用块、分隔线、图片（含两行题注之一：斜体简题与紧随的说明行）、表格（含粗体
题注），以及构书过程插入的分篇标记 ``@@PART@@``。

题注一律由作者手写编号（``*图 2-13　短题*``、``**表 A-1　标题**``）。解析时把
手写编号剥出来存进 ``label_key``，由 LaTeX 自动编号；正文里指向该编号的交叉引用
由 :mod:`primer.book.crossrefs` 换成 ``\\ref``。源文件一个字节都不改。

两条与源文件体例有关的约定照搬原先的构书脚本，不要"顺手修正"：

* 表格题注先入待配队列，由紧随其后的表格取用；没有表格取用的题注行会被丢弃；
* 列表在空行处断开，因此空行分隔的两个列表会渲染成两个独立环境。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Union

from .references import Bibliography

PART_MARKER = "@@PART@@"
HORIZONTAL_RULE = "---"
QUOTE_PREFIX = "> "

IMAGE_RE = re.compile(r"^!\[(.*?)\]\((.*?)\)\s*$")
# 手写编号：``图 1-1``/``图 A-1``；短题紧随其后，用全角空格或空白分隔。
FIGURE_CAPTION_RE = re.compile(
    r"^\*(?P<body>图\s*(?P<key>[0-9A-Z]+-[0-9]+)[　\s]*(?P<title>.*?))\*$"
)
TABLE_CAPTION_RE = re.compile(
    r"^\*\*(?P<body>表\s*(?P<key>[0-9A-Z]+-[0-9]+)[　\s]*(?P<title>.*?))\*\*$"
)
FIGURE_ALT_RE = re.compile(r"^图\s*([0-9A-Z]+-[0-9]+)$")
NOTE_RE = re.compile(r"^\*([^*]+)\*$")
LABEL_PREFIX_RE = re.compile(r"^(图|表)\s*[0-9A-Z]+-")
HEADING_RE = re.compile(r"^(#{1,4})\s+(.*)")
LIST_ITEM_RE = re.compile(r"^(-|\d+\.)\s+(.*)")
TABLE_SEPARATOR_RE = re.compile(r"^\|[\s:|-]+\|")
TABLE_ROW_SEPARATOR_RE = re.compile(r"^[\s:-]+$")

LIST_ENVIRONMENTS = {False: "itemize", True: "enumerate"}
# markdown 标题级别 → LaTeX 结构命令。二级标题是"章"，篇由清单给出；附录篇里
# 各级整体下沉一级（见 tex.render_blocks），所以多备一档 subsubsection。
HEADING_COMMANDS = {2: "chapter", 3: "section", 4: "subsection", 5: "subsubsection"}

# 标题里由作者手写的编号前缀：``第 6 章``、``1.2``、``A.3.3.1``、``6.``。
# 只剥离"看起来像章节号"的形态（含点的多段数字或字母开头），避免误伤
# ``2020 年代``这类真正的标题。
HEADING_NUMBER_RE = re.compile(
    r"^(?:"
    r"第\s*[0-9]+\s*[章节篇][　\s]*"
    r"|第\s*[一二三四五六七八九十]+\s*[章节篇][　\s]*"
    r"|(?:[0-9]+(?:\.[0-9]+)+|[A-Z](?:\.[0-9]+)+)[　\s.．、]*"
    r")"
)


def strip_heading_number(text: str) -> str:
    """剥掉标题开头的手写编号；剥完为空时保留原样。"""
    stripped = HEADING_NUMBER_RE.sub("", text, count=1).strip()
    return stripped or text.strip()


@dataclass
class Heading:
    level: int
    text: str


@dataclass
class Paragraph:
    text: str


@dataclass
class ListBlock:
    ordered: bool
    items: List[str] = field(default_factory=list)


@dataclass
class CodeBlock:
    lines: List[str] = field(default_factory=list)


@dataclass
class Quote:
    text: str


@dataclass
class HorizontalRule:
    pass


@dataclass
class PartBanner:
    title: str


@dataclass
class Figure:
    """插图。

    ``caption`` 是剥去手写编号后的短题，``label_key`` 是手写编号（如 ``2-13``），
    ``label`` 是成书用的 ``fig:2-13``；无编号时为 ``None``。
    """

    path: str
    caption: str = ""
    note: str = ""
    label_key: Optional[str] = None

    @property
    def label(self) -> Optional[str]:
        return f"fig:{self.label_key}" if self.label_key else None


@dataclass
class Table:
    rows: List[List[str]] = field(default_factory=list)
    caption: str = ""
    label_key: Optional[str] = None

    @property
    def label(self) -> Optional[str]:
        return f"tab:{self.label_key}" if self.label_key else None


Block = Union[
    Heading, Paragraph, ListBlock, CodeBlock, Quote, HorizontalRule, PartBanner, Figure, Table
]


def _next_non_blank(lines: Sequence[str], start: int) -> int:
    """返回 ``start`` 之后第一个非空行的下标（越界则返回行数）。"""
    index = start
    while index < len(lines) and not lines[index].strip():
        index += 1
    return index


def parse_blocks(lines: Sequence[str]) -> List[Block]:
    """解析 markdown 行序列。

    编号剥离与交叉引用替换在此之前完成，本函数只做语法切分。
    """
    blocks: List[Block] = []
    pending_captions: List[re.Match] = []
    total = len(lines)
    index = 0
    previous_was_item = False

    while index < total:
        line = lines[index].rstrip()
        if not line.strip():
            previous_was_item = False
            index += 1
            continue

        if line.startswith(PART_MARKER):
            blocks.append(PartBanner(line[len(PART_MARKER):].strip()))
            previous_was_item = False
            index += 1
            continue

        if line.startswith("```"):
            body: List[str] = []
            index += 1
            while index < total and not lines[index].startswith("```"):
                body.append(lines[index])
                index += 1
            index += 1
            blocks.append(CodeBlock(body))
            previous_was_item = False
            continue

        heading = HEADING_RE.match(line)
        if heading:
            blocks.append(Heading(level=len(heading.group(1)), text=heading.group(2).strip()))
            previous_was_item = False
            index += 1
            continue

        if line.strip() == HORIZONTAL_RULE:
            blocks.append(HorizontalRule())
            previous_was_item = False
            index += 1
            continue

        if line.strip().startswith(QUOTE_PREFIX):
            blocks.append(Quote(line.strip()[len(QUOTE_PREFIX):]))
            previous_was_item = False
            index += 1
            continue

        image = IMAGE_RE.match(line.strip())
        if image:
            caption = ""
            note = ""
            label_key: Optional[str] = None
            consumed = index
            caption_index = _next_non_blank(lines, index + 1)
            if caption_index < total:
                matched = FIGURE_CAPTION_RE.match(lines[caption_index].strip())
                if matched:
                    caption = matched.group("title").strip()
                    label_key = matched.group("key")
            if caption or label_key:
                consumed = caption_index
                note_index = _next_non_blank(lines, caption_index + 1)
                if note_index < total:
                    matched = NOTE_RE.match(lines[note_index].strip())
                    if matched and not LABEL_PREFIX_RE.match(matched.group(1).strip()):
                        note = matched.group(1).strip()
                        consumed = note_index
            else:
                # 无题注时退回图片替代文本（``![图 1-1](...)``）作简题。
                alt = FIGURE_ALT_RE.match(image.group(1).strip())
                if alt:
                    label_key = alt.group(1)
                    caption = image.group(1).strip()
            blocks.append(Figure(path=image.group(2), caption=caption, note=note, label_key=label_key))
            previous_was_item = False
            index = consumed + 1
            continue

        if (
            line.strip().startswith("|")
            and index + 1 < total
            and TABLE_SEPARATOR_RE.match(lines[index + 1].strip())
        ):
            rows: List[List[str]] = []
            while index < total and lines[index].strip().startswith("|"):
                cells = [cell.strip() for cell in lines[index].strip().strip("|").split("|")]
                if not TABLE_ROW_SEPARATOR_RE.match("".join(cells)):
                    rows.append(cells)
                index += 1
            caption = pending_captions.pop(0) if pending_captions else None
            blocks.append(
                Table(
                    rows=rows,
                    caption=caption.group("title").strip() if caption else "",
                    label_key=caption.group("key") if caption else None,
                )
            )
            previous_was_item = False
            continue

        caption = TABLE_CAPTION_RE.match(line.strip())
        if caption:
            pending_captions.append(caption)
            previous_was_item = False
            index += 1
            continue

        item = LIST_ITEM_RE.match(line.strip())
        if item:
            ordered = item.group(1) != "-"
            if previous_was_item and isinstance(blocks[-1], ListBlock) and blocks[-1].ordered == ordered:
                blocks[-1].items.append(item.group(2))
            else:
                blocks.append(ListBlock(ordered=ordered, items=[item.group(2)]))
            previous_was_item = True
            index += 1
            continue

        blocks.append(Paragraph(line))
        previous_was_item = False
        index += 1

    return blocks


# ---------------------------------------------------------------- markdown 落盘


def strip_preamble(lines: Sequence[str]) -> List[str]:
    """剥除各源文件抬头（一级标题与说明块），从正文第一行起返回。"""
    out: List[str] = []
    started = False
    for line in lines:
        if not started:
            if line.startswith("# ") or line.startswith(QUOTE_PREFIX) or not line.strip():
                continue
            started = True
        out.append(line)
    return out


def drop_matching(lines: Sequence[str], patterns: Sequence[re.Pattern]) -> List[str]:
    """丢弃命中任一正则的整行（编辑性尾注、工作记录等）。"""
    return [line for line in lines if not any(p.match(line.strip()) for p in patterns)]


def parts_to_headings(text: str) -> str:
    """把分篇标记转成二级标题（markdown 落盘时用）。"""
    return re.sub(
        r"^" + PART_MARKER + r"(.*)$",
        lambda match: f"\n## {match.group(1).strip()}\n",
        text,
        flags=re.M,
    )


def as_volume_document(text: str, title: str) -> str:
    """篇级 markdown：篇题由二级标题提升为一级标题。"""
    body = parts_to_headings(text)
    return body.replace(f"## {title}", f"# {title}", 1).strip() + "\n"


def local_bibliography(citations: Sequence[int], bibliography: Bibliography) -> str:
    """册内参考文献列表：册内编号在前，全书编号在括号内对照。"""
    out = ["## 本册参考文献", ""]
    for number, global_number in enumerate(citations, 1):
        entry = bibliography.get(global_number) or f"（全书编号 [{global_number}] 条目缺失）"
        out.append(f"[{number}] {entry}（全书编号 [{global_number}]）")
        out.append("")
    return "\n".join(out)
