# -*- coding: utf-8 -*-
"""交叉引用：把手写的图／表编号换成 ``\\ref``，并登记题注标签。

源文件里的编号是作者手写的（``*图 2-13　短题*``、正文"如图 2-13 所示"）。成书时
由 LaTeX 自动编号，因此：

* 题注行解析出 ``label_key``（如 ``2-13``），渲染成 ``\\label{fig:2-13}``；
* 正文里的 ``图 2-13`` 换成 ``图~\\ref{fig:2-13}``——保留"图"字，只用 ``\\ref``
  取号，这样不必给 ``\\autoref`` 配中文名，也不怕宏包对中文的处理差异。

找不到对应题注的正文引用**照原样保留**（不静默删改），并作为发现上报；题注没有
被任何正文引用也上报，属信息级。章节引用（"第 6 章"）不在这里处理：章号连续自动，
作者写的章号在新体例下仍然正确。
"""

from __future__ import annotations

import re
from typing import Dict, Iterable, List, Sequence, Set, Tuple

from .findings import Finding
from .markdown import (
    FIGURE_CAPTION_RE,
    IMAGE_RE,
    TABLE_CAPTION_RE,
    Figure,
    Table,
)

FIGURE_REF_RE = re.compile(r"图\s*([0-9A-Z]+-[0-9]+)")
TABLE_REF_RE = re.compile(r"表\s*([0-9A-Z]+-[0-9]+)")
RANGE_TAIL_RE = re.compile(r"^[～~至—–]\s*[0-9]")

KIND_FIGURE = "figure"
KIND_TABLE = "table"


def collect_labels(blocks: Sequence[object]) -> Tuple[Dict[str, str], List[Finding]]:
    """从已解析的块里收集标签：``fig:2-13`` → ``figure``，``tab:A-1`` → ``table``。

    图与表的编号各自独立，命名空间靠 ``fig:``／``tab:`` 前缀分开——同一个"2-1"
    在插图和表格里可以同时存在。

    只登记真正会渲染出 ``\\label`` 的题注（解析阶段已丢掉没有表格取用的孤立题注），
    因此不会出现"登记了标签却没有 label 命令"的悬空映射。重复编号会被上报。
    """
    labels: Dict[str, str] = {}
    findings: List[Finding] = []
    for block in blocks:
        if isinstance(block, Figure) and block.label_key:
            kind, key = KIND_FIGURE, block.label_key
        elif isinstance(block, Table) and block.label_key:
            kind, key = KIND_TABLE, block.label_key
        else:
            continue
        name = _label_name(kind, key)
        if name in labels:
            findings.append(
                Finding(
                    code="duplicate-label",
                    severity="error",
                    message=f"hand-written {kind} number {key} is used by more than one caption",
                    location=f"label {name}",
                )
            )
            continue
        labels[name] = kind
    return labels, findings


def rewrite_references(
    lines: Sequence[str], labels: Dict[str, str], location: str = ""
) -> Tuple[List[str], Set[str], List[Finding]]:
    """把正文里的手写编号换成 ``\\ref``；返回（新行序列, 被引用到的标签, 发现）。

    "题注没人引用"是全书层面的判断，由 :func:`unreferenced` 在所有篇处理完后统一给出。
    """
    out: List[str] = []
    referenced: Set[str] = set()
    findings: List[Finding] = []
    in_fence = False

    for number, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            out.append(line)
            continue
        if in_fence or _is_caption_line(stripped):
            out.append(line)
            continue
        rewritten, refs, line_findings = _rewrite_line(line, labels, location, number)
        referenced |= refs
        findings.extend(line_findings)
        out.append(rewritten)

    return out, referenced, findings


def _is_caption_line(stripped: str) -> bool:
    """题注行与图片行不参与替换：它们本身就是题注，替换会破坏编号解析。"""
    if not stripped:
        return False
    return bool(
        FIGURE_CAPTION_RE.match(stripped)
        or TABLE_CAPTION_RE.match(stripped)
        or IMAGE_RE.match(stripped)
    )


def _rewrite_line(
    line: str, labels: Dict[str, str], location: str, number: int
) -> Tuple[str, Set[str], List[Finding]]:
    referenced: Set[str] = set()
    findings: List[Finding] = []
    where = f"{location}:{number}" if location else f"line {number}"

    def replace(kind: str, prefix: str, pattern: re.Pattern, text: str) -> str:
        pieces: List[str] = []
        cursor = 0
        for matched in pattern.finditer(text):
            key = matched.group(1)
            tail = text[matched.end():]
            if RANGE_TAIL_RE.match(tail):
                findings.append(
                    Finding(
                        code=f"{kind}-range-reference",
                        severity="error",
                        message=f"range reference to {prefix} {key} is not rewritten (write each label out)",
                        location=where,
                    )
                )
                continue
            pieces.append(text[cursor:matched.start()])
            name = _label_name(kind, key)
            if labels.get(name) == kind:
                pieces.append(f"{prefix}~\\ref{{{name}}}")
                referenced.add(name)
            else:
                findings.append(
                    Finding(
                        code=f"dangling-{kind}-reference",
                        severity="error",
                        message=f"prose refers to {prefix} {key} but no caption carries that number",
                        location=where,
                    )
                )
                pieces.append(matched.group(0))
            cursor = matched.end()
        pieces.append(text[cursor:])
        return "".join(pieces)

    line = replace(KIND_FIGURE, "图", FIGURE_REF_RE, line)
    line = replace(KIND_TABLE, "表", TABLE_REF_RE, line)
    return line, referenced, findings


def _label_name(kind: str, key: str) -> str:
    return f"{'fig' if kind == KIND_FIGURE else 'tab'}:{key}"


def unreferenced(labels: Dict[str, str], referenced: Iterable[str]) -> List[Finding]:
    """题注有了、正文却从没引用过的图与表（信息级）。"""
    seen = set(referenced)
    findings: List[Finding] = []
    for name, kind in labels.items():
        if name in seen:
            continue
        prefix = "图" if kind == KIND_FIGURE else "表"
        number = name.split(":", 1)[1]
        findings.append(
            Finding(
                code=f"unreferenced-{kind}",
                severity="info",
                message=f"{prefix} {number} is captioned but never cited in prose",
                location=name,
            )
        )
    return findings
