# -*- coding: utf-8 -*-
"""装配期的直引号规整：ASCII 双引号 → 中文弯引号（只改形式，不动源文件）。

五个源文件里的引号清一色是 ASCII 直引号（U+0022），而 CJK 字体会把 U+0022
渲染成全角**收拢**形，成书后每处引文都成了 ``从”描述行星”`` 这种方向错误的
形态。引号方向属于形式，由排版系统决定：本模块在装配阶段把成对的直引号配成
``“`` / ``”``。

配对按行进行（markdown 子集里一个段落、一条标题、一个列表项都是一行），这样
奇偶方案不会跨块泄漏。奇数个引号的行**原样保留**并记为发现，交给人工判断，
绝不猜测哪一个是收尾。

扫描时跳过的区段（不做配对）：行内代码（反引号）、行内公式（``$…$``）、
markdown 图片／链接的目标括号（含链接标题）、裸 URL。ASCII 单引号与撇号
（``D'Abramo``）一概不动。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Sequence, Tuple

OPEN_QUOTE = "\u201c"
CLOSE_QUOTE = "\u201d"
STRAIGHT_QUOTE = '"'

_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
_MATH_RE = re.compile(r"\$[^$\n]+?\$")
_BARE_URL_RE = re.compile(r"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+")
_FENCE_RE = re.compile(r"^\s*```")


@dataclass(frozen=True)
class QuoteOutcome:
    """一次配对的结果。

    ``pairs`` 是配成的对数；``unpaired`` 是未能配对的直引号个数（0 表示成功）。
    未配对时 ``text`` 原样返回。
    """

    text: str
    pairs: int
    unpaired: int


@dataclass
class QuoteTally:
    """整本书的引号规整记录：配成的对数与未配对块的坐标。"""

    enabled: bool = True
    pairs: int = 0
    unpaired: List[Tuple[str, int]] = field(default_factory=list)

    def record(self, outcome: QuoteOutcome, location: str) -> None:
        if outcome.unpaired:
            self.unpaired.append((location, outcome.unpaired))
        else:
            self.pairs += outcome.pairs


def _link_target_spans(text: str) -> List[Tuple[int, int]]:
    """markdown 图片／链接目标括号的跨度（``](…)`` 里括号内的部分，含链接标题）。

    括号内的路径本身可能带括号（``报告 (1).pdf``），所以要按层级配对到闭合括号。
    """
    spans: List[Tuple[int, int]] = []
    for matched in re.finditer(r"\]\(", text):
        start = matched.end()
        depth = 1
        index = start
        while index < len(text) and depth:
            char = text[index]
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    break
            elif char == "\n":
                break
            index += 1
        if depth == 0:
            spans.append((start, index))
    return spans


def protected_spans(text: str) -> List[Tuple[int, int]]:
    """不应参与引号配对的字符区段，已合并重叠。"""
    spans = [(m.start(), m.end()) for m in _INLINE_CODE_RE.finditer(text)]
    spans += [(m.start(), m.end()) for m in _MATH_RE.finditer(text)]
    spans += _link_target_spans(text)
    spans += [(m.start(), m.end()) for m in _BARE_URL_RE.finditer(text)]
    spans.sort()
    merged: List[List[int]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def normalize_double_quotes(text: str) -> QuoteOutcome:
    """把一行散文里的 ASCII 双引号按出现顺序配成 ``“`` / ``”``。

    奇数个直引号时不猜：原样返回并把个数记在 ``unpaired``。
    """
    if STRAIGHT_QUOTE not in text:
        return QuoteOutcome(text, 0, 0)
    spans = protected_spans(text)
    positions: List[int] = []
    span_index = 0
    for index, char in enumerate(text):
        if char != STRAIGHT_QUOTE:
            continue
        while span_index < len(spans) and spans[span_index][1] <= index:
            span_index += 1
        if span_index < len(spans) and spans[span_index][0] <= index:
            continue
        positions.append(index)
    if len(positions) % 2:
        return QuoteOutcome(text, 0, len(positions))
    characters = list(text)
    for index in range(0, len(positions), 2):
        characters[positions[index]] = OPEN_QUOTE
        characters[positions[index + 1]] = CLOSE_QUOTE
    return QuoteOutcome("".join(characters), len(positions) // 2, 0)


def normalize_line(text: str, tally: QuoteTally, location: str) -> str:
    """规整一行文本并把结果记进 ``tally``。"""
    if not tally.enabled:
        return text
    outcome = normalize_double_quotes(text)
    tally.record(outcome, location)
    return outcome.text


def normalize_lines(lines: Sequence[str], tally: QuoteTally, location: str) -> List[str]:
    """逐行规整；围栏代码块（````` ``` `````）之内整块跳过。"""
    if not tally.enabled:
        return list(lines)
    out: List[str] = []
    in_fence = False
    for number, line in enumerate(lines, 1):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            out.append(line)
            continue
        if in_fence:
            out.append(line)
            continue
        out.append(normalize_line(line, tally, f"{location}:{number}"))
    return out
