# -*- coding: utf-8 -*-
"""构建与校验的机器可读发现（findings）。

成书过程中凡是"编者需要知道但不应打断成书"的事情——正文引了不存在的图、
题注没人引用、表格宽到必须横向排、日志里的 overfull box——都落成 :class:`Finding`。
``check`` 把它们汇总成 JSON 与一张人读表；``error`` 级发现决定退出码。

代码（``code``）是稳定的机器可读标识，人读消息用英文，位置信息随种类而异：
markdown 源行号、LaTeX 日志页码、或章节/文件路径。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Mapping, Sequence

SEVERITIES = ("error", "warning", "info")

# 会让 check 以非零码退出的发现代码。overfull/underfull box 与字体形状回退不在其中：
# 它们是排版细节，除非 --strict。
FATAL_CODES = frozenset(
    {
        "dangling-figure-reference",
        "dangling-table-reference",
        "duplicate-label",
        "missing-image",
        "missing-file",
        "missing-character",
        "undefined-reference",
        "undefined-citation",
        "citation-without-entry",
        "figure-range-reference",
        "table-range-reference",
    }
)


@dataclass(frozen=True)
class Finding:
    """一条发现。"""

    code: str
    message: str
    location: str = ""
    severity: str = "warning"

    def __post_init__(self) -> None:
        if self.severity not in SEVERITIES:
            raise ValueError(f"unknown severity: {self.severity}")

    def as_dict(self) -> Mapping[str, str]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "location": self.location,
        }


def is_fatal(finding: Finding) -> bool:
    """该发现是否应当让 check 以非零码退出。"""
    if finding.severity == "error":
        return True
    return finding.code in FATAL_CODES


def summarize(findings: Sequence[Finding]) -> List[Mapping[str, object]]:
    """按代码汇总计数，供人读表使用。"""
    order: List[str] = []
    buckets: dict = {}
    for finding in findings:
        if finding.code not in buckets:
            buckets[finding.code] = {"code": finding.code, "severity": finding.severity, "count": 0}
            order.append(finding.code)
        buckets[finding.code]["count"] += 1
    return [buckets[code] for code in order]


def fatal(findings: Iterable[Finding]) -> List[Finding]:
    """筛选出会让 check 失败的发现。"""
    return [finding for finding in findings if is_fatal(finding)]
