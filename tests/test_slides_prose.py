# -*- coding: utf-8 -*-
"""``primer.slides.prose``：行首原文编号的剥离与排印规整——只动形式，不误伤内容。

正例是一张"能剥／能改"的表（各类编号与它的分隔符、半角标点与直引号），反例是一张
"绝不能动"的表（年份、小数、书名序号、括号里的西文、URL、纯拉丁句）。反例与正例同等
重要：剥错一个字、改错一个标点，幻灯片上的句子就少一层意思或换一个意思。
"""

from __future__ import annotations

import pytest

from primer.slides.prose import normalize_pick_text, strip_lead_enumerator

# （输入，剥完的样子）。编号后面常带一个空格，剥的时候一并吃掉。
POSITIVE = [
    ("（一）我国水平", "我国水平"),
    ("(1) 第一点", "第一点"),
    ("（12）第十二项", "第十二项"),
    ("一、引言", "引言"),
    ("二．方法", "方法"),
    ("1. 第一步", "第一步"),
    ("1、第一步", "第一步"),
    ("1) 第一步", "第一步"),
    ("① 首先", "首先"),
    ("⑳ 最后", "最后"),
    ("- 一条要点", "一条要点"),
    ("* 一条要点", "一条要点"),
    ("• 一条要点", "一条要点"),
    ("（一）1. 嵌套编号", "嵌套编号"),  # 循环剥到不变
    ("　一、全角空格也吃得下", "全角空格也吃得下"),
]

# 一个都不许动：号码串、小数、年份、书名序号、括号里的西文、没有分隔符的中文数字。
NEGATIVE = [
    "1995—2026",
    "1.5 亿公里",
    "0.5 倍",
    "11 个目标",
    "3–5 条要点",
    "第一章　宇宙的尺度",
    "第 8 章",
    "2026 年了",
    "（NASA）的行星探测",
    "（见 §8.6）",
    "科学、工程与战略研判",
]


@pytest.mark.parametrize("text, expected", POSITIVE)
def test_strip_lead_enumerator_removes_the_original_numbering(text, expected):
    assert strip_lead_enumerator(text) == expected


@pytest.mark.parametrize("text", NEGATIVE)
def test_strip_lead_enumerator_never_touches_content(text):
    assert strip_lead_enumerator(text) == text


def test_strip_lead_enumerator_leaves_a_clean_line_byte_identical():
    """没有编号时原样返回：连行首空白都不动（不是 lstrip 的代理）。"""
    text = "  普通的一句话。"

    assert strip_lead_enumerator(text) == text


def test_strip_lead_enumerator_can_empty_a_bare_enumerator():
    assert strip_lead_enumerator("（一）") == ""


# ---------------------------------------------------------------- 排印规整


# （输入，规整后）。三条规则各给正例：直引号配对、与 CJK 相邻的半角标点转全角、
# 数字与单位之间的空格去掉（外加连续空格折叠）。
NORMALIZE_POSITIVE = [
    ('从"描述行星"到"理解过程"', "从“描述行星”到“理解过程”"),
    ('他问:"为什么?"于是继续.', "他问：“为什么？”于是继续。"),
    ("行星,宜居,生命", "行星，宜居，生命"),
    ("一个,两个.三个;四个:五个!", "一个，两个。三个；四个：五个！"),
    ("共性是:昔日假说.焦点已转向.", "共性是：昔日假说。焦点已转向。"),
    ("真的吗?", "真的吗？"),
    ("(三判据)", "（三判据）"),
    ("460 °C", "460°C"),
    ("12 %", "12%"),
    ("共 54 项", "共 54项"),
    ("1995 年是分界。", "1995年是分界。"),
    ("0.5 倍", "0.5倍"),
    (
        "STMD 2024 年将 187 项技术缺口归入 20 个门类。",
        "STMD 2024年将 187项技术缺口归入 20个门类。",
    ),
    ("多重  空格 折叠", "多重 空格 折叠"),
    ("沿2M1207b、HR 8799、绘架座βb到51 Eridani b。", "沿2M1207b、HR 8799、绘架座βb到51 Eridani b。"),
]

# 一个字符都不许动：小数、年份区间、节号、页码、型号、姓氏缩写、URL、纯拉丁句、
# 数字里的逗号、单位是拉丁字母的情形。
NORMALIZE_NEGATIVE = [
    "1.5",
    "1995—2026",
    "§2.1",
    "p.17",
    "A-1",
    "2M1207b",
    "HR 8799",
    "2020–2023",
    "https://exoplanet.eu/catalog/2m1207_b/",
    "[227]",
    "OSIRIS-REx",
    "Hello, world. This is fine; really: yes?",
    "Fig. 2 shows it.",
    "1,000 km",
    "C/2023 A3",
    "arXiv:2401.12345",
    "Kepler-452b",
    "JPL/Caltech (USA)",
    "v=12.5 km/s",
    "λ = 10 µm",
    "NASA's DART",
    "±0.5 mag",
    "O₂/H₂O",
    "M⊙",
    "e = 0.85",
    "TRL 6",
    "s8-6p02",
    "2:1 共振",
    "1.5 亿公里",
]


@pytest.mark.parametrize("text, expected", NORMALIZE_POSITIVE)
def test_normalize_pick_text_fixes_the_form(text, expected):
    assert normalize_pick_text(text) == expected


@pytest.mark.parametrize("text", NORMALIZE_NEGATIVE)
def test_normalize_pick_text_never_touches_content(text):
    assert normalize_pick_text(text) == text


def test_normalize_pick_text_leaves_an_odd_quote_alone():
    """奇数个直引号不猜方向：原样保留，交给 quotes-unpaired 那条警告。"""
    assert normalize_pick_text('只有"一个引号') == '只有"一个引号'


def test_normalize_pick_text_leaves_url_and_link_target_alone():
    text = "见 https://a.example/c,d?x=1 与 [报告](http://b.example/x,y)。"
    # 链接标签外的句号仍在保护的目标括号之外，句号前面是 CJK 邻居才转全角；
    # URL 与链接目标里的逗号、问号、点号一个都不动。
    assert normalize_pick_text(text) == text


def test_normalize_pick_text_is_idempotent():
    text = '行星,共 12 项:"已改写".'
    once = normalize_pick_text(text)
    assert normalize_pick_text(once) == once


def test_normalize_pick_text_does_not_reflow_without_cjk_neighbour():
    """只有 CJK 邻居才配对／转全角；纯粹的一句话原样返回。"""
    latin = "A short English sentence. Nothing to normalise here."
    assert normalize_pick_text(latin) == latin
