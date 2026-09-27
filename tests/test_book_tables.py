# -*- coding: utf-8 -*-
"""超宽表格的版面决策：容量模型、竖排／横向页／缩字三种出路。"""

from primer.book import tables
from primer.book.manifest import Typography

TINY = Typography()
NARROW = Typography(paper="a5paper", margin="1cm")


def rows_of(columns, width, count=3):
    header = [f"列{i}" for i in range(columns)]
    body = [["甲" * width] * columns for _ in range(count)]
    return [header] + body


def test_length_and_font_parsing():
    assert tables.length_to_pt("72pt") == 72
    assert abs(tables.length_to_pt("2.54cm") - 72) < 0.2
    assert abs(tables.length_to_pt("1in") - 72) < 0.001
    assert tables.font_points("-4") == 12
    assert tables.font_points("5") == 10.5
    assert tables.font_points("nonsense") == 12


def test_display_width_counts_cjk_as_two():
    assert tables.display_width("ab") == 2
    assert tables.display_width("中文") == 4


def test_small_table_fits_the_text_block():
    layout, findings = tables.plan_table(rows_of(3, 4), TINY)

    assert layout.orientation == "portrait"
    assert layout.mode == "fit"
    assert layout.font_code == "-4"
    assert abs(sum(layout.columns) - 1) < 1e-9
    assert findings == []


def test_very_wide_table_goes_to_a_landscape_page():
    layout, findings = tables.plan_table(rows_of(9, 40, count=3), TINY)

    assert layout.orientation == "landscape"
    assert layout.mode == "wrap"
    assert any("landscape" in finding.message for finding in findings)


def test_wide_but_tall_table_reduces_the_font_ladder():
    layout, findings = tables.plan_table(rows_of(5, 40, count=8), NARROW)

    assert layout.orientation in {"portrait", "landscape"}
    assert layout.mode == "wrap"
    assert tables.font_points(layout.font_code) <= tables.font_points("-4")


def test_column_widths_are_proportional_to_natural_width():
    layout, _ = tables.plan_table([["短", "很长很长很长的单元格"], ["a", "b"]], TINY)

    assert layout.columns[1] > layout.columns[0]


# ---------------------------------------------------------------- 长词元的计宽


def test_a_pathologically_long_token_is_measured_by_its_longest_piece():
    """43 字符的链接标签在连字符处断得开：最长的一段只有 9 个字。"""
    assert tables.longest_unbreakable_piece("civil-space-shortfall-ranking-july-2024") == 9
    # 无分隔符的长串靠 break_long_runs 每 10 个字符补的断点：最长段就是 10
    assert tables.longest_unbreakable_piece("a" * 43) == 10
    assert tables.longest_unbreakable_piece("Propulsion:") == 11


def test_natural_width_leaves_short_tokens_and_cjk_alone():
    """只有病态长的词元改口径；汉字与短词元的自然宽度一个单位都不变。"""
    for text in ("中文单元格，短字串", "Propulsion: Nuclear", "评分 7.17/9（2024 年 7 月）"):
        assert tables.natural_width(text) == tables.display_width(text)


def test_natural_width_counts_the_longest_piece_of_a_long_token():
    token = "civil-space-shortfall-ranking-july-2024"
    cell = f"属美国政府认定的全局性缺口 {token} 一览"

    assert tables.natural_width(cell) == tables.display_width(cell) - len(token) + 9


def test_a_long_token_no_longer_owns_the_table_width(monkeypatch):
    """同一个表：按"整串"计宽时第三列吃掉一半，按"最长不可断段"计宽时前两列松一口气。"""
    label = "civil-space-shortfall-ranking-july-2024"
    rows = [
        ["模块", "NASA 门类", "缺口编号与定位"],
        ["核电推组合体（载人探索主推）", "Propulsion: Nuclear", f"属全局性缺口 {label}"],
        ["低功率核电推（无人深空探测可用）", "Propulsion: Nuclear", "评分区间 4.86–6.81"],
    ]

    fixed, _ = tables.plan_table(rows, TINY)
    monkeypatch.setattr(tables, "natural_width", tables.display_width)
    squeezed, _ = tables.plan_table(rows, TINY)

    assert fixed.columns[0] > squeezed.columns[0]
    assert fixed.columns[1] > squeezed.columns[1]
    assert fixed.columns[2] < squeezed.columns[2]
    # 列宽仍然归一：Y 列权重的和必须等于列数
    assert abs(sum(fixed.columns) - 1) < 1e-9


def test_scale_tier_is_used_when_a_slight_shrink_beats_wrapping():
    # 单行、两列、自然宽度略超版心：等比缩到页宽比换行更保形。
    layout, findings = tables.plan_table([["甲" * 24, "乙" * 24]], TINY)

    assert layout.mode == "scale"
    assert 0 < layout.scale < 1
    assert any("scaled to" in finding.message for finding in findings)


def test_last_resort_marks_the_table_as_allowed_to_break():
    layout, findings = tables.plan_table(rows_of(12, 90, count=12), NARROW)

    assert layout.mode == "wrap"
    assert any("break across pages" in finding.message for finding in findings)
