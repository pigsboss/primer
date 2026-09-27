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
