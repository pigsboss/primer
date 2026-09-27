# -*- coding: utf-8 -*-
"""LaTeX 日志解析：把 ``.log`` 变成机器可读的发现。

xelatex 的日志按 ``max_print_line``（默认 79 字符）硬折行，一条消息常被切成两三行
且续行没有标记。因此先把"长度为 79 的整倍数的那几行接回去"，再在接好的整段上
匹配消息。页码不在消息里，靠日志流里 ``[n]`` 的出页标记定位——匹配位置之前最近
的那个出页标记就是该消息所在页。

只认五类信号：盒子溢出（overfull/underfull）、缺字、字体形状回退、未定义引用／文献、
找不到文件。
"""

from __future__ import annotations

import re
from typing import List, Sequence, Tuple

from .findings import Finding

WRAP_WIDTH = 79

OVERFULL_RE = re.compile(
    r"(Overfull|Underfull) \\([hv])box \(([^)]*)\)"
    r"(?:[^.]*?) at lines? (\d+)(?:--(\d+))?"
)
MISSING_CHAR_RE = re.compile(r"Missing character: There is no (.+?) in font\s*([^!\n]*?)\s*!")
UNDEFINED_FONT_SHAPE_RE = re.compile(
    r"LaTeX Font Warning: Font shape `([^']+)' undefined"
    r"(?:\s*\(Font\)\s*using `([^']+)' instead)?"
)
UNDEFINED_REF_RE = re.compile(
    r"LaTeX Warning: Reference `([^']+)' on page (\d+) undefined on input line (\d+)"
)
UNDEFINED_REF_SHORT_RE = re.compile(r"LaTeX Warning: Reference `([^']+)' undefined")
UNDEFINED_CITE_RE = re.compile(
    r"LaTeX Warning: Citation `([^']+)' on page (\d+) undefined"
)
MISSING_FILE_RE = re.compile(r"(?:LaTeX Warning: File|! LaTeX Error: File) `([^']+)' not found")
RERUN_RE = re.compile(
    r"LaTeX Warning: Label\(s\) may have changed|"
    r"Rerun to get cross-references right|"
    r"Please \(re\)run LaTeX|"
    r"Package rerunfilecheck Warning"
)
PAGE_RE = re.compile(r"\[(\d+)(?:[^\]\d][^\]]*)?\]")


def unwrap(log: str) -> str:
    """把日志按 ``max_print_line`` 折出来的续行接回去。"""
    lines = log.splitlines()
    joined: List[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        while len(line) > 0 and len(line) % WRAP_WIDTH == 0 and index + 1 < len(lines):
            index += 1
            line += lines[index]
        joined.append(line)
        index += 1
    return "\n".join(joined)


def parse_log(text: str) -> List[Finding]:
    """解析日志文本，返回发现列表。"""
    log = unwrap(text)
    pages = [(matched.start(), int(matched.group(1))) for matched in PAGE_RE.finditer(log)]
    findings: List[Finding] = []

    for matched in OVERFULL_RE.finditer(log):
        kind, axis, amount, first, last = matched.groups()
        lines = f"{first}--{last}" if last else first
        code = f"{kind.lower()}-{axis}box"
        findings.append(
            Finding(
                code=code,
                severity="warning",
                message=f"{axis}box {kind.lower()} ({amount})",
                location=f"page {_page_at(pages, matched.start())}, line(s) {lines}",
            )
        )

    for matched in MISSING_CHAR_RE.finditer(log):
        char, font = matched.group(1), matched.group(2)
        findings.append(
            Finding(
                code="missing-character",
                severity="error",
                message=f"no glyph for {char!r} in font {font}",
                location=f"page {_page_at(pages, matched.start())}",
            )
        )

    # 字体形状回退：字仍是字（引擎换了另一种字体顶上），排版却不是清单要求的样子，
    # 因此只报到 warning，不进 FATAL_CODES——同 overfull box，只有 --strict 才拦。
    for matched in UNDEFINED_FONT_SHAPE_RE.finditer(log):
        shape, substitute = matched.group(1), matched.group(2)
        message = f"font shape {shape!r} is undefined"
        if substitute:
            message += f"; the engine substituted {substitute!r}"
        findings.append(
            Finding(code="undefined-font-shape", severity="warning", message=message)
        )

    for matched in UNDEFINED_REF_RE.finditer(log):
        findings.append(
            Finding(
                code="undefined-reference",
                severity="error",
                message=f"reference to label {matched.group(1)!r} is undefined",
                location=f"page {matched.group(2)}, input line {matched.group(3)}",
            )
        )
    for matched in UNDEFINED_REF_SHORT_RE.finditer(log):
        findings.append(
            Finding(
                code="undefined-reference",
                severity="error",
                message=f"reference to label {matched.group(1)!r} is undefined",
            )
        )

    for matched in UNDEFINED_CITE_RE.finditer(log):
        findings.append(
            Finding(
                code="undefined-citation",
                severity="error",
                message=f"citation {matched.group(1)!r} is undefined",
                location=f"page {matched.group(2)}",
            )
        )

    for matched in MISSING_FILE_RE.finditer(log):
        findings.append(
            Finding(
                code="missing-file",
                severity="error",
                message=f"file {matched.group(1)!r} was not found",
                location=f"page {_page_at(pages, matched.start())}",
            )
        )

    if RERUN_RE.search(log):
        findings.append(
            Finding(
                code="rerun-required",
                severity="warning",
                message="the engine asked for another pass; cross-references or the "
                "table of contents may be one run behind",
            )
        )
    return findings


def _page_at(pages: Sequence[Tuple[int, int]], position: int) -> int:
    """匹配位置之前最近的出页标记所给的页码（找不到时返回 0）。"""
    current = 0
    for offset, page in pages:
        if offset > position:
            break
        current = page
    return current
