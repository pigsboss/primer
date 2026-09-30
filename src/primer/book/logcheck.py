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

# 一条消息归给哪个出页标记。TeX 是**装箱**时报 overfull／underfull 的，``[n]`` 由随后的
# ``\shipout`` 打出，所以消息写在它所属那一页的标记**之前**——``after`` 取"之后最近的
# 标记"，这才是装帧的真相。``before`` 取"之前最近的标记"，是成书构建器沿用至今的口径；
# 幻灯片的 ``build`` 用 ``after``（见 :func:`primer.slides.build.read_log_findings`）。
PAGE_BEFORE = "before"
PAGE_AFTER = "after"


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


def parse_log(text: str, pages: str = PAGE_BEFORE) -> List[Finding]:
    """解析日志文本，返回发现列表。

    ``pages`` 决定一条消息归给哪个出页标记：:data:`PAGE_BEFORE` 取消息之前最近的标记，
    :data:`PAGE_AFTER` 取之后最近的标记。两者相差一页的情形出现在**帧即页**的 beamer
    幻灯片上——帧的盒子在 ``\\end{frame}`` 装箱时报出来，``[n]`` 之后才打出，所以那个
    消息属于**后**一页。
    """
    if pages not in (PAGE_BEFORE, PAGE_AFTER):
        raise ValueError(f"unknown page attribution: {pages!r}")
    log = unwrap(text)
    pages_at = [(matched.start(), int(matched.group(1))) for matched in PAGE_RE.finditer(log)]
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
                location=f"page {_page_at(pages_at, matched.start(), pages)}, line(s) {lines}",
            )
        )

    for matched in MISSING_CHAR_RE.finditer(log):
        char, font = matched.group(1), matched.group(2)
        findings.append(
            Finding(
                code="missing-character",
                severity="error",
                message=f"no glyph for {char!r} in font {font}",
                location=f"page {_page_at(pages_at, matched.start(), pages)}",
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
                location=f"page {_page_at(pages_at, matched.start(), pages)}",
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


def _page_at(pages: Sequence[Tuple[int, int]], position: int, mode: str) -> int:
    """把一条消息的位置映射到页码。

    ``mode`` 为 :data:`PAGE_BEFORE` 时取位置**之前**最近的出页标记（成书的既有口径）；
    为 :data:`PAGE_AFTER` 时取位置**之后**最近的标记——装帧的真相，见 :func:`parse_log`。
    之前一个标记都没有、或之后一个都没有时，退回能找到的那一个（都没有则 0）。
    """
    seen = 0
    for offset, page in pages:
        if offset > position:
            if mode == PAGE_AFTER:
                return page
            break
        seen = page
    return seen
