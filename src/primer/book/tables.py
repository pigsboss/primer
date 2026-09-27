# -*- coding: utf-8 -*-
"""超宽表格的版面决策。

作者只管把表格写成 markdown，排版系统负责让它落进页面。本模块按"先竖排、
再横向页、最后缩字降档"的次序给出每张表的排版方案，并把决定与理由记进发现。

宽度模型是**估计**而非测量（真测量要编译一遍）：CJK 记 2 个显示单位、其余记 1 个，
1 个显示单位 ≈ 0.5 em；于是给定纸面与字号，一行的容量就是可算的。模型只用来选
方案，真溢出由 LaTeX 的 overfull 警告兜底（``check`` 会报出来）。

取舍次序：

1. 竖排按自然宽度排得下 → 用它；
2. 竖排等比缩一点（缩幅不小于 ``MIN_SCALE``、缩后字号仍可读）→ 缩；
3. 竖排换行排得下且每列不太窄、整表不高于版心 85% → 用它；
4. 横向页按自然宽度排得下 → 转横向；
5. 横向页缩一点 → 缩；
6. 横向页换行排得下 → 用它；
7. 以上在基准字号都不行，就依次降字号（``FONT_LADDER``）重来；
8. 仍不行则横向页换行排，允许跨页，并如实上报。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from .findings import Finding
from .manifest import Typography

MM_PER_PT = 25.4 / 72.0
CJK_THRESHOLD = 0x2E7F

# \zihao 代码 → 字号（磅）。只列成书用得到的档位。
ZHIHAO_POINTS = {
    "0": 42.0, "1": 26.0, "2": 22.0, "2*": 18.0, "-0": 36.0, "3": 16.0,
    "3*": 15.0, "-1": 24.0, "-2": 21.0, "-3": 18.0, "4": 14.0, "4*": 13.0,
    "-4": 12.0, "5": 10.5, "5*": 9.5, "-5": 9.0, "-6": 7.5,
}
# 表格字号阶梯：按 ``table_font_size`` 起，放不下就依次降档。
FONT_LADDER = ("-4", "-5", "-6")
# 缩字下限：再小就不如换行排。
MIN_READABLE_PT = 8.0
# 每列至少要有的显示单位（10 单位 ≈ 5 个汉字）：低于此不再排。
MIN_COLUMN = 12.0
# 竖排"舒展"的下限（16 单位 ≈ 8 个汉字）：基准字号下竖排列宽够宽就留在竖排，
# 否则转横向页——把宽表硬挤进竖排会把每列压得只剩两三个字。
COMFORT_COLUMN = 16.0
# 等比缩幅下限，低于此宁可横向页换行。
MIN_SCALE = 0.7
# 整表高度上限（占版心高度的比例）。
HEIGHT_LIMIT = 0.95

_PAPER_MM = {
    "a4paper": (210.0, 297.0),
    "a5paper": (148.0, 210.0),
    "letterpaper": (215.9, 279.4),
    "b5paper": (176.0, 250.0),
}
_LENGTH_RE = re.compile(r"^\s*([0-9.]+)\s*(mm|cm|in|pt|bp)?\s*$")


@dataclass(frozen=True)
class TableLayout:
    """一张表的排版方案。

    ``mode`` 为 ``fit``（按自然宽度分列）、``wrap``（换行排）或 ``scale``
    （整表等比缩小，缩幅见 ``scale``）。
    """

    orientation: str
    mode: str
    font_code: str
    columns: Tuple[float, ...]
    reason: str
    scale: float = 1.0


@dataclass(frozen=True)
class _Metrics:
    """一个版面上的容量。"""

    width: float  # 版心宽（磅）
    height: float  # 版心高（磅）


def length_to_pt(value: str) -> float:
    """把 ``2.54cm`` 一类的长度解析成磅；认不出时按 2.54cm 处理。"""
    matched = _LENGTH_RE.match(str(value))
    if not matched:
        return 72.0
    number = float(matched.group(1))
    unit = matched.group(2) or "pt"
    return {
        "mm": number / MM_PER_PT,
        "cm": number * 10.0 / MM_PER_PT,
        "in": number * 72.0,
        "bp": number * 72.0 / 72.27,
        "pt": number,
    }[unit]


def font_points(code: str) -> float:
    """``\\zihao`` 代码 → 磅；认不出时按 12pt 处理。"""
    return ZHIHAO_POINTS.get(str(code).strip(), 12.0)


def display_width(text: str) -> int:
    """显示宽度（CJK 记 2，其余记 1）。"""
    return sum(2 if ord(char) > CJK_THRESHOLD else 1 for char in text)


def plan_table(rows: Sequence[Sequence[str]], typography: Typography, location: str = "") -> Tuple[TableLayout, List[Finding]]:
    """给出表格排版方案与相关发现。"""
    findings: List[Finding] = []
    columns = max((len(row) for row in rows), default=1)
    rows = [list(row) + [""] * (columns - len(row)) for row in rows]
    natural = [
        max(display_width(_plain(cell)) for cell in (row[index] for row in rows))
        for index in range(columns)
    ]
    natural_total = sum(natural) + _padding(columns)

    portrait = _metrics(typography, "portrait")
    landscape = _metrics(typography, "landscape")
    ladder = _ladder_from(str(typography.table_font_size))
    base = font_points(ladder[0])

    for code in ladder:
        if natural_total <= _capacity(portrait, font_points(code)):
            return _fitted("portrait", code, natural, portrait, "table fits the text block"), findings

    scale = _scale_for(natural_total, portrait, base)
    if MIN_SCALE <= scale < 1.0 and base * scale >= MIN_READABLE_PT:
        findings.append(_note(location, f"table is scaled to {scale * 100:.0f}% to fit the text block"))
        return _scaled("portrait", ladder[0], natural, scale, "shrunk to the text block"), findings

    if _share(natural, portrait, base) >= COMFORT_COLUMN:
        for code in ladder:
            wrapped = _wrap(rows, natural, portrait, code, "portrait", MIN_COLUMN)
            if wrapped is not None:
                return wrapped, findings

    for code in ladder:
        if natural_total <= _capacity(landscape, font_points(code)):
            findings.append(_note(location, "table is wider than the text block; set on a landscape page"))
            return _fitted("landscape", code, natural, landscape, "wider than the text block"), findings

    scale = _scale_for(natural_total, landscape, base)
    if MIN_SCALE <= scale < 1.0 and base * scale >= MIN_READABLE_PT:
        findings.append(_note(location, f"table is scaled to {scale * 100:.0f}% on a landscape page"))
        return _scaled("landscape", ladder[0], natural, scale, "shrunk on a landscape page"), findings

    for code in ladder:
        wrapped = _wrap(rows, natural, landscape, code, "landscape", MIN_COLUMN)
        if wrapped is not None:
            findings.append(_note(location, "table is too wide or too tall for the text block; set on a landscape page"))
            return wrapped, findings

    for code in ladder:
        wrapped = _wrap(rows, natural, portrait, code, "portrait", MIN_COLUMN)
        if wrapped is not None:
            return wrapped, findings

    findings.append(_note(location, "table does not fit any single page; allowed to break across pages"))
    return _wrapped("landscape", ladder[-1], natural, landscape, "allowed to break across pages"), findings


# ---------------------------------------------------------------- 方案构造


def _fitted(orientation: str, code: str, natural, metrics: _Metrics, reason: str) -> TableLayout:
    font = font_points(code)
    return TableLayout(orientation, "fit", code, _fractions(natural, _capacity(metrics, font) - _padding(len(natural))), reason)


def _wrapped(orientation: str, code: str, natural, metrics: _Metrics, reason: str) -> TableLayout:
    font = font_points(code)
    return TableLayout(orientation, "wrap", code, _fractions(natural, _capacity(metrics, font) - _padding(len(natural))), reason)


def _scaled(orientation: str, code: str, natural, scale: float, reason: str) -> TableLayout:
    return TableLayout(orientation, "scale", code, _fractions(natural, sum(natural)), reason, scale)


def _wrap(rows, natural, metrics: _Metrics, code: str, orientation: str, min_column: float) -> Optional[TableLayout]:
    """在给定版面上按 ``code`` 换行排，条件不满足时返回 ``None``。"""
    font = font_points(code)
    widths = _column_widths(natural, metrics, font)
    if min(widths) < min_column:
        return None
    if not _fits_height(rows, widths, metrics, font):
        return None
    return _wrapped(orientation, code, natural, metrics, "wrapped to the text block")


def _share(natural, metrics: _Metrics, font: float) -> float:
    """均分列宽（显示单位），用来判断竖排是否舒展。"""
    return (_capacity(metrics, font) - _padding(len(natural))) / max(len(natural), 1)


# ---------------------------------------------------------------- 度量


def _plain(text: str) -> str:
    """去掉 markdown 强调标记后再量宽度。"""
    return re.sub(r"\*+", "", text)


def _padding(columns: int) -> float:
    """列间距占掉的显示单位（tabcolsep 6pt ≈ 1 单位/侧）。"""
    return 2.0 * columns + 2.0


def _ladder_from(code: str) -> List[str]:
    """从配置字号起，沿阶梯向下取所有不小于可读下限的档位。"""
    if code in FONT_LADDER:
        ladder = list(FONT_LADDER[FONT_LADDER.index(code):])
    else:
        ladder = [code] + [item for item in FONT_LADDER if font_points(item) < font_points(code)]
    return [item for item in ladder if font_points(item) >= MIN_READABLE_PT] or [FONT_LADDER[-1]]


def _metrics(typography: Typography, orientation: str) -> _Metrics:
    width_mm, height_mm = _PAPER_MM.get(str(typography.paper).lower(), _PAPER_MM["a4paper"])
    margin = length_to_pt(str(typography.margin))
    long_side = height_mm / MM_PER_PT
    short_side = width_mm / MM_PER_PT
    if orientation == "portrait":
        return _Metrics(short_side - 2 * margin, long_side - 2 * margin)
    return _Metrics(long_side - 2 * margin, short_side - 2 * margin)


def _capacity(metrics: _Metrics, font: float) -> float:
    """给定字号下，一行能容纳的显示单位数。"""
    return metrics.width / (0.5 * font)


def _scale_for(natural_total: float, metrics: _Metrics, font: float) -> float:
    """把整表缩到版心宽所需的缩幅。"""
    return metrics.width / (0.5 * font * natural_total)


def _column_widths(natural: Sequence[float], metrics: _Metrics, font: float) -> List[float]:
    """按自然宽度分配列宽（显示单位），每列不低于 ``MIN_COLUMN``。"""
    capacity = _capacity(metrics, font) - _padding(len(natural))
    return _proportional(natural, max(capacity, 1.0))


def _proportional(natural: Sequence[float], capacity: float) -> List[float]:
    """先把 ``MIN_COLUMN`` 分给每列，余量按自然宽度加权分配。"""
    count = len(natural)
    floor = min(MIN_COLUMN, capacity / count)
    remainder = max(capacity - floor * count, 0.0)
    total = sum(natural) or float(count)
    return [floor + remainder * (value / total) for value in natural]


def _fractions(natural: Sequence[float], capacity: float) -> Tuple[float, ...]:
    widths = _proportional(natural, max(capacity, 1.0))
    total = sum(widths) or 1.0
    return tuple(width / total for width in widths)


def _fits_height(rows, widths, metrics: _Metrics, font: float) -> bool:
    total_lines = 0
    for row in rows:
        lines = 1
        for index, cell in enumerate(row):
            if widths[index] <= 0:
                continue
            lines = max(lines, math.ceil(display_width(_plain(cell)) / widths[index]))
        total_lines += lines
    height = total_lines * font * 1.25 + len(rows) * 1.0
    return height <= metrics.height * HEIGHT_LIMIT


def _note(location: str, message: str) -> Finding:
    return Finding(code="table-layout", severity="info", message=message, location=location)
