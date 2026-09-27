# -*- coding: utf-8 -*-
"""ctexbook 导言区：按清单渲染模板。

模板用 :class:`string.Template` 的机制，但分隔符换成 ``@``——LaTeX 里 ``$``
（数学）与 ``\\``（命令）遍地都是，沿用默认分隔符会让模板难以阅读。占位符与
清单字段一一对应，缺项立即报错，不做静默替换。

字号既接受 ``\\zihao`` 代码（``4``、``-4``），也接受磅值（``12``、``11pt``）：
前者是中文排版的习惯写法，后者便于从命令行快速试字号。
"""

from __future__ import annotations

import re
import string
from pathlib import Path
from typing import Union

from .manifest import Fonts, Typography
from . import tables

TEMPLATE_DIR = "templates"
TEMPLATE_NAME = "ctexbook.tex.tmpl"

_PT_RE = re.compile(r"^\s*([0-9.]+)\s*(pt|bp)?\s*$")


class TexTemplate(string.Template):
    """``@name`` 占位的模板。"""

    delimiter = "@"


def template_path() -> Path:
    """导言区模板在包内的路径。"""
    return Path(__file__).resolve().parent / TEMPLATE_DIR / TEMPLATE_NAME


def body_points(size: str) -> float:
    """字号 → 磅。``\\zihao`` 代码查表；磅值（``12``、``11pt``）直接取用。"""
    value = str(size).strip()
    if value in tables.ZHIHAO_POINTS:
        return tables.ZHIHAO_POINTS[value]
    matched = _PT_RE.match(value)
    return float(matched.group(1)) if matched else tables.ZHIHAO_POINTS["-4"]


# 相对正文字号的标准字号阶梯（沿用 LaTeX 10pt 类的比例）。
SIZE_LADDER = (
    ("small", 0.9),
    ("footnotesize", 0.8),
    ("scriptsize", 0.7),
    ("tiny", 0.5),
    ("large", 1.2),
    ("Large", 1.44),
    ("LARGE", 1.73),
    ("huge", 2.07),
    ("Huge", 2.49),
)
BASELINE_RATIO = 1.25


def render_size_ladder(size: str) -> str:
    """按正文字号重算整个字号阶梯。

    ctex 的 ``zihao`` 类选项只认五号与小四两种基准，正文字号一旦改由清单或命令行
    指定，``\\small``、``\\footnotesize`` 这些相对字号就会与正文脱节（题注只剩下
    9pt）。这里按正文字号把整条阶梯重写一遍——``\\zihao`` 本身也只是
    ``\\fontsize…\\selectfont``，xelatex 下中英文字号一起跟着走。
    """
    body = body_points(size)
    lines = [
        rf"\renewcommand\normalsize{{\fontsize{{{body:g}pt}}{{{body * BASELINE_RATIO:g}pt}}\selectfont}}"
    ]
    for name, ratio in SIZE_LADDER:
        points = body * ratio
        lines.append(
            rf"\renewcommand\{name}{{\fontsize{{{points:g}pt}}"
            rf"{{{points * BASELINE_RATIO:g}pt}}\selectfont}}"
        )
    return "\n".join(lines)


def render_preamble(
    fonts: Fonts, typography: Typography, graphics_path: Union[str, Path]
) -> str:
    """渲染导言区。

    ``graphics_path`` 是插图目录树相对**工程根**的路径（结尾带目录分隔符），
    编译时的工作目录即工程根，markdown 里的图片路径相对该根。
    """
    template = TexTemplate(template_path().read_text(encoding="utf-8"))
    cjk_sans_fake_bold = typography.cjk_sans_fake_bold or typography.cjk_fake_bold
    return template.substitute(
        paper=typography.paper,
        margin=typography.margin,
        main_font=fonts.main,
        sans_font=fonts.sans,
        mono_font=fonts.mono,
        cjk_main_font=fonts.cjk_main,
        cjk_sans_font=fonts.cjk_sans,
        cjk_mono_font=fonts.cjk_mono,
        cjk_fake_bold=typography.cjk_fake_bold,
        cjk_sans_fake_bold=cjk_sans_fake_bold,
        cjk_fake_slant=typography.cjk_fake_slant,
        graphics_path=graphics_path,
        caption_skip=typography.caption_skip,
        line_spread=typography.line_spread,
        par_skip=typography.par_skip,
        size_ladder=render_size_ladder(typography.body_font_size),
        figure_list_name=typography.figure_list_name,
        table_list_name=typography.table_list_name,
        toc_name=typography.toc_name,
        toc_depth=typography.toc_depth,
    )
