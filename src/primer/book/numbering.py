# -*- coding: utf-8 -*-
"""重编号方案与文献引用的处理。

成书编号全部交给 LaTeX 自动完成（章由 ``ctexbook`` 连续编号，图、表按章编号），
本模块只负责两件不能再交给 LaTeX 的事：

* 按清单里的重编号方案重写标题与正文里的字面交叉引用（如"本篇 1.2 节"→"第 2 章"）
  ——这些是作者手写的旧节号，方案把它们换算成新体例下仍然正确的章号；
* 文献引用的区间写法归一化与册内压缩编号。

本模块只处理 markdown 行序列，不涉及 LaTeX。
"""

from __future__ import annotations

import re
from typing import List, Optional, Sequence

from .manifest import HeadingScheme

CITATION_RE = re.compile(r"\[(\d+)(?:\s*[–—-]\s*(\d+))?\]")
CITATION_RANGE_RE = re.compile(r"\[(\d+)\]\s*[–—-]\s*\[(\d+)\]")


def apply_scheme(lines: Sequence[str], scheme: HeadingScheme) -> List[str]:
    """按方案重写标题与正文交叉引用。

    逐行处理：命中标题规则的行按模板重写；其余行做字面替换后原样输出。
    """
    out: List[str] = []
    for line in lines:
        matched: Optional[re.Match] = None
        for rule in scheme.rules:
            matched = rule.pattern.match(line)
            if matched:
                out.append(rule.render(matched))
                break
        if matched:
            continue
        for old, new in scheme.replacements:
            line = line.replace(old, new)
        out.append(line)
    return out


def normalize_cite_ranges(text: str) -> str:
    """``[1]–[3]`` 这类拆开的区间写法合并为 ``[1–3]``。"""
    return CITATION_RANGE_RE.sub(r"[\1–\2]", text)


def collect_citations(text: str) -> List[int]:
    """按首次出现顺序返回引用编号（区间展开，重复只记一次）。"""
    seen, order = set(), []
    for matched in CITATION_RE.finditer(text):
        first = int(matched.group(1))
        if matched.group(2):
            last = int(matched.group(2))
            numbers = list(range(min(first, last), max(first, last) + 1))
        else:
            numbers = [first]
        for number in numbers:
            if number not in seen:
                seen.add(number)
                order.append(number)
    return order


def renumber_citations(text: str, order: Sequence[int]) -> str:
    """把全书编号替换为册内编号（按 ``order`` 的顺序映射），区间尽量压缩。"""
    mapping = {global_number: local for local, global_number in enumerate(order, 1)}

    def replace(matched: re.Match) -> str:
        first = int(matched.group(1))
        if not matched.group(2):
            return f"[{mapping.get(first, first)}]"
        last = int(matched.group(2))
        numbers = [mapping.get(n) for n in range(min(first, last), max(first, last) + 1)]
        if any(number is None for number in numbers):
            return matched.group(0)
        if numbers == list(range(numbers[0], numbers[0] + len(numbers))):
            if len(numbers) > 2:
                return f"[{numbers[0]}–{numbers[-1]}]"
            if len(numbers) == 2:
                return f"[{numbers[0]}][{numbers[-1]}]"
            return f"[{numbers[0]}]"
        return "".join(f"[{number}]" for number in numbers)

    return CITATION_RE.sub(replace, text)
