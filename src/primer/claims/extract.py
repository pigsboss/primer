# -*- coding: utf-8 -*-
"""从书稿正文里抽出"带引用的论断"：正文 → 引用标记 → (论断, 编号) 记录。

判定单元（claim unit）是**承载引用的句子**。一条记录对应一个 (论断, 引用编号)
对：``[111–113]`` 展开成三条，``[245][253]`` 拆成两条，同一句里的多处引用各出一条。
记录带稳定的 ``id``、来源文件（相对工程根）与行号、句子本身、所在段落（有界上下文）、
展开后的编号，以及原文里那个引用标记的原始写法（``token``）。

正文里出现 ``[n]`` 的形状不止一种，本模块按下面的规矩分辨：

* **引用标记**严格限定为 ``[n]``、``[a–b]``（破折号可用 en/em dash 或连字符）以及
  紧邻的 ``[a]–[b]``；方括号里必须是纯编号。因此 ``[图 1-2]``（插图编号）、
  ``[199 类]``（编号后还有字）、``[arXiv:2401.0001]`` 都不算引用；它们连同
  ``[12](https://…)`` 这类链接文本一起被记进 ``rejected`` 供人核对。
* **表格行**（以 ``|`` 起头）里的引用是货真价实的论断，但表格单元格不是句子，
  默认不计入 ``claims``。本模块仍按 ``|`` 切出单元格并算好记录，放进 ``excluded``，
  用 ``--include-tables`` 才并入。这样"少算了多少"是可见的，而不是悄悄丢掉。
* **标题**（``#`` 起头）、**代码围栏**内的行、以及**列 0 的 ``[n] 正文`` 参考文献
  列表项**不产生记录。列表项与正文引用的分辨办法见 :func:`_classify_line`：
  列 0 的 ``[n] `` 后面若带尾标 ``［原文：…］`` 或含两个以上 ``". "`` 分节，判为
  文献条目；列 0 的 ``[n] `` 但两者都不满足，判为**存疑**（``ambiguous``），
  只报告、不猜。
* ``origin`` 记下论断所在的上下文类型：``prose``（正文，含编号列表项）、
  ``blockquote``（``>`` 说明块）、``caption``（``*…*`` 图注）、``table``（表格单元格）。

句子切分以中文句末标点 ``。！？`` 为准；ASCII 的 ``.!?`` 只在后面跟空白或行尾、
且前面既不是数字也不是大写字母时才断句（``1. **要点**`` 这种列表序号、``PIA17595``
这类编号因此不会被误断）。句子不跨段落，段落内允许跨硬换行。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional, Sequence

from ..literature.paths import relative_to_root

# 本工程（行星探测工程）的默认正文，可由命令行覆盖。
DEFAULT_BODY_FILES = (
    "成果文件/综述升级版_第一篇_科学篇.md",
    "成果文件/综述升级版_第二篇_工程篇.md",
    "成果文件/综述升级版_第三篇_战略篇.md",
    "成果文件/科产融合专题_深空探测专用技术产业链调研.md",
)

MAX_PARAGRAPH_CHARS = 1200
"""段落上下文的字符上限；超限时以论断为中心截取并加省略号。"""

# 纯编号的引用标记：`[12]`、`[12–15]`（en/em dash 或连字符）。
CITATION_TOKEN = re.compile(r"\[(\d{1,3})(?:\s*([-–—]+)\s*(\d{1,3}))?\]")
# 相邻的两个引用标记被破折号连起来时（`[351]–[360]`）算一个范围。
TOKEN_DASH = re.compile(r"[ \t]*[-–—]+[ \t]*")
# 方括号里含数字、却被上面的严格式否掉的东西，登记下来供人核对。
BRACKET_DIGIT = re.compile(r"\[[^\]\n]{0,40}\d[^\]\n]{0,40}\]")

HEADING = re.compile(r"^#{1,6}\s")
FENCE = re.compile(r"^\s*(?:```|~~~)")
TABLE_ROW = re.compile(r"^\s*\|")
TABLE_RULE = re.compile(r"^\s*\|[\s:|-]+\|\s*$")
CAPTION = re.compile(r"^\s*\*(?!\*).*\*\s*$")
QUOTE = re.compile(r"^\s*>")
# 列 0 的 `[n] 正文`：参考文献列表项的形状。
BIB_LINE = re.compile(r"^\[(\d{1,3})\]\s+(?P<body>\S.*)$")
BIB_TAG = re.compile(r"［原文：")
BIB_SEGMENT = re.compile(r"\.\s")

ORIGIN_PROSE = "prose"
ORIGIN_BLOCKQUOTE = "blockquote"
ORIGIN_CAPTION = "caption"
ORIGIN_TABLE = "table"

SKIP_TABLE = "table-row"
SKIP_HEADING = "heading"
SKIP_FENCE = "code-fence"
SKIP_BIB = "bibliography-line"

# ASCII 句末标点：前面不能是数字（列表序号）或大写字母（缩写、编号）。
_ASCII_END = re.compile(r"(?<![0-9A-Z])[.!?](?=\s|$)")


@dataclass(frozen=True)
class ClaimRecord:
    """一条 (论断, 引用编号) 记录。``claim_start`` / ``claim_end`` 是句子在源文件里的字符偏移。"""

    id: str
    file: str
    line: int
    citation: int
    token: str
    claim: str
    paragraph: str
    origin: str
    claim_start: int
    claim_end: int
    range_size: int = 1


@dataclass
class ExcludedBatch:
    """一个被排除的批次：某文件某类上下文里发现了多少引用，附几个例子。"""

    reason: str
    file: str
    tokens: int
    pairs: int
    examples: list[str] = field(default_factory=list)


@dataclass
class ExtractionResult:
    """一次抽取的全部产物。``claims`` 是默认口径（不含表格行）。"""

    claims: list[ClaimRecord] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    pairs_by_file: dict[str, int] = field(default_factory=dict)
    tokens_by_file: dict[str, int] = field(default_factory=dict)
    excluded: list[ExcludedBatch] = field(default_factory=list)
    ambiguous: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)

    @property
    def table_pairs(self) -> int:
        return sum(item.pairs for item in self.excluded if item.reason == SKIP_TABLE)

    @property
    def table_tokens(self) -> int:
        return sum(item.tokens for item in self.excluded if item.reason == SKIP_TABLE)


@dataclass(frozen=True)
class _Line:
    number: int
    text: str
    offset: int
    kind: str


@dataclass(frozen=True)
class _Token:
    start: int
    end: int
    numbers: tuple[int, ...]


def extract_claims(
    paths: Sequence[Path],
    *,
    project_root: Optional[Path] = None,
    include_tables: bool = False,
    example_limit: int = 5,
) -> ExtractionResult:
    """抽取若干正文 markdown 里的 (论断, 引用编号) 记录。

    ``paths`` 的顺序决定记录顺序，因此同一个输入集合总能得到同样的 ``id``。
    """
    result = ExtractionResult()
    for path in paths:
        rel = relative_to_root(path, project_root)
        text = Path(path).read_text(encoding="utf-8")
        _extract_file(rel, text, result, include_tables, example_limit)
        result.files.append(rel)
        for token in _rejected(text):
            if token not in result.rejected:
                result.rejected.append(token)
    position = 0
    records: list[ClaimRecord] = []
    for record in result.claims:
        position += 1
        records.append(
            ClaimRecord(
                id=f"c{position:04d}",
                file=record.file,
                line=record.line,
                citation=record.citation,
                token=record.token,
                claim=record.claim,
                paragraph=record.paragraph,
                origin=record.origin,
                claim_start=record.claim_start,
                claim_end=record.claim_end,
                range_size=record.range_size,
            )
        )
    result.claims = records
    return result


def citation_tokens(text: str) -> list[_Token]:
    """找出 ``text`` 里的引用标记；``[a]–[b]`` 就近合并成一个范围。"""
    found: list[_Token] = []
    index = 0
    while index < len(text):
        match = CITATION_TOKEN.search(text, index)
        if not match:
            break
        if match.group(2):
            low, high = int(match.group(1)), int(match.group(3))
            if high < low:
                low, high = high, low
            found.append(_Token(match.start(), match.end(), tuple(range(low, high + 1))))
            index = match.end()
            continue
        cursor = match.end()
        numbers = (int(match.group(1)),)
        span = TOKEN_DASH.match(text, cursor)
        if span:
            second = CITATION_TOKEN.match(text, span.end())
            if second and not second.group(2):
                low, high = int(match.group(1)), int(second.group(1))
                if high < low:
                    low, high = high, low
                numbers = tuple(range(low, high + 1))
                cursor = second.end()
        found.append(_Token(match.start(), cursor, numbers))
        index = cursor
    return found


def strip_citations(text: str) -> str:
    """把引用标记从论断里抹掉，供检索当查询串用。

    ``[111–113]`` 这样的标记进查询只会带进一串与原文无关的数字；抹掉之后查询串就是
    论断本身。抹掉的位置用空格补齐，字符数不变，便于与原文对照。
    """
    chars = list(text)
    for token in citation_tokens(text):
        for index in range(token.start, min(token.end, len(chars))):
            chars[index] = " "
    return "".join(chars)


def _rejected(text: str, limit: int = 20) -> list[str]:
    """方括号里带数字、却不是合法引用标记的片段（插图编号、链接文本等）。"""
    rejected: list[str] = []
    for match in BRACKET_DIGIT.finditer(text):
        token = match.group(0)
        if text[match.end() : match.end() + 1] == "(":
            continue
        if CITATION_TOKEN.fullmatch(token):
            continue
        if token not in rejected:
            rejected.append(token)
        if len(rejected) >= limit:
            break
    return rejected


def _extract_file(
    rel: str,
    text: str,
    result: ExtractionResult,
    include_tables: bool,
    example_limit: int,
) -> None:
    result.tokens_by_file.setdefault(rel, 0)
    result.pairs_by_file.setdefault(rel, 0)
    batches: dict[str, ExcludedBatch] = {}

    def note(reason: str, tokens: int, pairs: int, example: str) -> None:
        result.tokens_by_file[rel] += tokens
        result.pairs_by_file[rel] += pairs
        batch = batches.get(reason)
        if batch is None:
            batch = ExcludedBatch(reason=reason, file=rel, tokens=0, pairs=0)
            batches[reason] = batch
        batch.tokens += tokens
        batch.pairs += pairs
        if len(batch.examples) < example_limit:
            batch.examples.append(f"{rel}:{example}")

    for group in _group_lines(_scan_lines(text)):
        kind = group[0].kind
        if kind == "bib":
            for item in group:
                tokens = citation_tokens(item.text)
                note(SKIP_BIB, len(tokens), _width(tokens), f"{item.number} {item.text[:80]}")
            continue
        if kind == "ambiguous-bib":
            for item in group:
                result.ambiguous.append(
                    f"{rel}:{item.number} line starts with a bracketed number but is not a "
                    f"bibliography entry: {item.text[:120]}"
                )
            continue
        if kind in ("heading", "fence"):
            reason = SKIP_HEADING if kind == "heading" else SKIP_FENCE
            for item in group:
                tokens = citation_tokens(item.text)
                if tokens:
                    note(reason, len(tokens), _width(tokens), f"{item.number} {item.text[:80]}")
            continue
        if kind == "table":
            _extract_table(group, rel, result, note, include_tables)
            continue
        if kind in ("quote", "caption"):
            # 引文块与图注按行处理：去掉 `>` / 外层 `*` 之后行内偏移仍然对得上，
            # 句子也就不再跨行——每行正好是一句或一个说明。
            origin = ORIGIN_BLOCKQUOTE if kind == "quote" else ORIGIN_CAPTION
            for item in group:
                body, shift = _strip_markers(item.text, kind)
                if body.strip():
                    _extract_block(
                        body, item.offset + shift, item.number, rel, origin, result, note, include_tables
                    )
            continue
        joined = "\n".join(item.text for item in group)
        _extract_block(
            joined, group[0].offset, group[0].number, rel, ORIGIN_PROSE, result, note, include_tables
        )

    for batch in batches.values():
        result.excluded.append(batch)


def _strip_markers(line: str, kind: str) -> tuple[str, int]:
    """去掉引文块的 ``>`` 前缀或图注的外层 ``*``，返回 ``(正文, 前移量)``。"""
    shift = 0
    if kind == "quote":
        while True:
            match = re.match(r"^\s*>[ \t]?", line[shift:])
            if not match:
                break
            shift += match.end()
        return line[shift:], shift
    trimmed = line.lstrip()
    shift = len(line) - len(trimmed)
    if trimmed.startswith("*") and not trimmed.startswith("**") and trimmed.endswith("*"):
        return trimmed[1:-1], shift + 1
    return line[shift:], shift


def _width(tokens: Sequence[_Token]) -> int:
    return sum(len(token.numbers) for token in tokens)


def _extract_table(group, rel, result, note, include_tables) -> None:
    """表格按 ``|`` 切成单元格，每个单元格单独当论断单元。"""
    for item in group:
        if TABLE_RULE.match(item.text):
            continue
        for cell_text, cell_offset in _split_cells(item.text, item.offset):
            if not cell_text.strip():
                continue
            _extract_block(
                cell_text, cell_offset, item.number, rel, ORIGIN_TABLE, result, note, include_tables
            )


def _split_cells(line: str, offset: int) -> list[tuple[str, int]]:
    cells: list[tuple[str, int]] = []
    start = 0
    for index, char in enumerate(line):
        if char == "|":
            cells.append((line[start:index], offset + start))
            start = index + 1
    cells.append((line[start:], offset + start))
    return cells


def _extract_block(
    text: str,
    base: int,
    line_no: int,
    rel: str,
    origin: str,
    result: ExtractionResult,
    note,
    include_tables: bool,
) -> None:
    """把一个段落（或表格单元格）切成句子，为每个带引用的句子出记录。"""
    for start, end in _iter_sentences(text):
        sentence = text[start:end]
        tokens = citation_tokens(sentence)
        if not tokens:
            continue
        pairs = _width(tokens)
        if origin == ORIGIN_TABLE and not include_tables:
            note(SKIP_TABLE, len(tokens), pairs, f"{line_no} {sentence[:100]}")
            continue
        result.tokens_by_file[rel] += len(tokens)
        result.pairs_by_file[rel] += pairs
        claim, claim_start, claim_end = _trim(sentence, start, end)
        paragraph = _bound(text, claim_start, claim_end)
        for token in tokens:
            token_text = sentence[token.start : token.end]
            for number in token.numbers:
                result.claims.append(
                    ClaimRecord(
                        id="",
                        file=rel,
                        line=line_no,
                        citation=number,
                        token=token_text,
                        claim=claim,
                        paragraph=paragraph,
                        origin=origin,
                        claim_start=base + claim_start,
                        claim_end=base + claim_end,
                        range_size=len(token.numbers),
                    )
                )


def _trim(sentence: str, start: int, end: int) -> tuple[str, int, int]:
    """去掉句子两端的空白，返回 ``(正文, 起, 止)``（起止是块内偏移）。"""
    lead = len(sentence) - len(sentence.lstrip())
    body = sentence.strip()
    return body, start + lead, start + lead + len(body)


def _iter_sentences(text: str) -> list[tuple[int, int]]:
    """句子区间列表；纯空白区间不产生句子。

    判定单元就是**句子**（``。！？`` 断句，标点保留在句内），不再按分号细分：
    "第一步…[112][113]；第二步…[122–126]" 这样由分号串起来的长句确实会同时挂上
    多组编号，判定时靠 ``partial`` 表达"只对上一部分"；反过来按分号切开虽然查询串
    更集中，却已经不是"句子"了，与判定单元的定义不符。
    """
    spans: list[tuple[int, int]] = []
    start = 0
    for index, char in enumerate(text):
        if char in "。！？" or (char in ".!?" and _ASCII_END.match(text, index)):
            if text[start : index + 1].strip():
                spans.append((start, index + 1))
            start = index + 1
    if text[start:].strip():
        spans.append((start, len(text)))
    return spans


def _scan_lines(text: str) -> list[_Line]:
    """按 ``\\n`` 切行并记下每行在文件里的偏移；``\\r`` 留在行内参与偏移计算。"""
    items: list[_Line] = []
    offset = 0
    in_fence = False
    for number, raw in enumerate(text.split("\n"), start=1):
        stripped = raw.strip()
        if in_fence:
            kind = "fence"
            if FENCE.match(raw):
                in_fence = False
        elif FENCE.match(raw):
            kind = "fence"
            in_fence = True
        elif not stripped:
            kind = "blank"
        else:
            kind = _classify_line(raw, stripped)
        items.append(_Line(number=number, text=raw, offset=offset, kind=kind))
        offset += len(raw) + 1
    return items


def _classify_line(raw: str, stripped: str) -> str:
    """给一行定类。列 0 的 ``[n] `` 要靠形状而不是靠位置来分辨是不是文献条目。"""
    if HEADING.match(stripped):
        return "heading"
    bibliography = BIB_LINE.match(raw)
    if bibliography:
        body = bibliography.group("body")
        if BIB_TAG.search(body) or len(BIB_SEGMENT.split(body)) >= 3:
            return "bib"
        return "ambiguous-bib"
    if TABLE_ROW.match(raw):
        return "table"
    if QUOTE.match(raw):
        return "quote"
    if CAPTION.match(raw):
        return "caption"
    return "prose"


def _group_lines(lines: Iterable[_Line]) -> list[list[_Line]]:
    """把同类的连续行并成一个块；空行只作分隔。"""
    groups: list[list[_Line]] = []
    current: list[_Line] = []
    current_kind = ""
    for item in lines:
        if item.kind == "blank":
            if current:
                groups.append(current)
                current, current_kind = [], ""
            continue
        if item.kind != current_kind and current:
            groups.append(current)
            current = []
        current_kind = item.kind
        current.append(item)
    if current:
        groups.append(current)
    return groups


def _bound(text: str, start: int, end: int, limit: int = MAX_PARAGRAPH_CHARS) -> str:
    """把上下文截到 ``limit`` 字符，以论断为中心，两端加省略号。"""
    if len(text) <= limit:
        return text
    half = max((limit - (end - start)) // 2, 0)
    low = max(start - half, 0)
    high = min(low + limit, len(text))
    low = max(high - limit, 0)
    piece = text[low:high]
    if low > 0:
        piece = "…" + piece
    if high < len(text):
        piece = piece + "…"
    return piece
