# -*- coding: utf-8 -*-
"""``primer.slides.inspect``：确定性层的纯函数判据、角色表推演，以及注入假后端/假 poppler
的视觉层。

这一轮**不联网也不用真 PDF**：确定性层的检查函数吃合成 span/线段/行数据；视觉层注入一个
可调用的假传输层（:data:`primer.slides.client.Transport`）；poppler 封装整体换成一个假
:class:`FakeTools`。密钥用假的，机器级配置指到空的 ``XDG_CONFIG_HOME``，既碰不到真凭据，
也碰不到 ``~/.config``。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest
import yaml

from primer.slides import inspect as ins

CANVAS = ins.Canvas(width=960.0, height=540.0)
SANS = "LQUDWN+HiraginoSansGB-W6-Identity-H"
LATIN = "LJPZAU+HelveticaNeue"
SERIF = "LJPZAU+STSongti-SC"

CONFIG_YAML = """\
providers:
  demo:
    base_url: https://api.example.invalid/v1
    key_env: PRIMER_DEMO_API_KEY
roles:
  vision:
    provider: demo
    model: demo-vision
    max_tokens: 1024
"""

KEY = "secret-demo-key"


# ---------------------------------------------------------------- 合成数据


def sp(
    top: float,
    left: float,
    right: float,
    *,
    bottom: float | None = None,
    size: float = 22.0,
    family: str = SANS,
    text: str = "某字",
) -> ins.Span:
    return ins.Span(
        top=top,
        left=left,
        right=right,
        bottom=top + size if bottom is None else bottom,
        size=size,
        family=family,
        text=text,
    )


def title_span(text: str = "目录") -> ins.Span:
    return sp(4.7, 48.0, 200.0, size=28.0, text=text)


def body_lines(tops, lefts, rights, *, size=22.0, family=SANS, texts=None):
    return [
        sp(top, left, right, size=size, family=family, text=(texts or ["句子"] * 99)[index])
        for index, (top, left, right) in enumerate(zip(tops, lefts, rights))
    ]


def rows_with_line(*, line_y: int, line_x0: int, line_x1: int, width: int = 800, height: int = 40):
    """逐行 0/1 字节串：在中段画一条横线。坐标是像素。"""
    rows = []
    for y in range(height):
        row = bytearray(width)
        if y == line_y:
            for x in range(line_x0, min(line_x1, width)):
                row[x] = 1
        rows.append(bytes(row))
    return rows


def xml_page(spans, *, width_pt=960.0, height_pt=540.0):
    """构造一份最小的 pdftohtml -xml。span 坐标按 pt 写在 1.5 px/pt 的画布上。

    每个（族, 字号）组合一个 fontspec，和 poppler 的输出一致。
    """
    fonts = []
    for span in spans:
        key = (span.family, span.size)
        if key not in fonts:
            fonts.append(key)
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<pdf2xml producer="poppler" version="26.09.0">',
        f'<page number="1" position="absolute" top="0" left="0" '
        f'height="{int(height_pt * 1.5)}" width="{int(width_pt * 1.5)}">',
    ]
    for index, (family, size) in enumerate(fonts):
        lines.append(
            f'<fontspec id="{index}" size="{size * 1.5:g}" family="{family}" color="#000000"/>'
        )
    for span in spans:
        identifier = fonts.index((span.family, span.size))
        left = int(span.left * 1.5)
        top = int(span.top * 1.5)
        lines.append(
            f'<text top="{top}" left="{left}" width="{int(span.width * 1.5)}" '
            f'height="{int(span.height * 1.5)}" font="{identifier}">{span.text}</text>'
        )
    lines.append("</page>")
    lines.append("</pdf2xml>")
    return "\n".join(lines)


# ---------------------------------------------------------------- 解析与栅格


def test_parse_page_xml_scales_the_canvas_and_the_spans_to_points():
    xml = xml_page([sp(60.0, 48.0, 508.0, size=28.0, text="行星探测")])
    canvas, spans = ins.parse_page_xml(xml)
    assert (canvas.width, canvas.height) == (960.0, 540.0)
    assert len(spans) == 1
    span = spans[0]
    assert (span.top, span.left, span.right) == (60.0, 48.0, 508.0)
    assert span.text == "行星探测"
    assert span.size == 28.0


def test_a_page_without_a_page_element_is_an_error():
    with pytest.raises(ins.InspectError):
        ins.parse_page_xml("<pdf2xml></pdf2xml>")


def test_pgm_raster_decodes_and_thresholds():
    # 2×3 的 PGM：左边一列 0（墨），右边 255（白）。
    raw = b"P5\n2 3\n255\n" + bytes([0, 255, 0, 255, 0, 255])
    width, height, rows = ins.decode_raster(raw, threshold=230)
    assert (width, height) == (2, 3)
    assert list(rows[0]) == [1, 0]
    assert list(rows[2]) == [1, 0]


def test_pbm_raster_unpacks_bits():
    # 8×1 的 PBM：字节 0b10100000 → 第 0、2 位为墨。
    raw = b"P4\n8 1\n" + bytes([0b10100000])
    width, height, rows = ins.decode_raster(raw)
    assert (width, height) == (8, 1)
    assert list(rows[0]) == [1, 0, 1, 0, 0, 0, 0, 0]


def test_pnm_header_tolerates_comments_and_whitespace():
    raw = b"P5\n# comment here\n4   2\n255\n" + bytes([0] * 8)
    width, height, rows = ins.decode_raster(raw)
    assert (width, height) == (4, 2)


def test_an_unknown_raster_format_is_an_error():
    with pytest.raises(ins.InspectError):
        ins.decode_raster(b"P3\n1 1\n255\n0 0 0")


# ---------------------------------------------------------------- 横线扫描


def test_scan_finds_a_long_thin_line():
    rows = rows_with_line(line_y=10, line_x0=100, line_x1=700, width=800)
    hlines = ins.scan_hlines(rows, width=800, scale=1.0)
    assert len(hlines) == 1
    line = hlines[0]
    assert (line.x0, line.x1) == (100.0, 700.0)
    assert line.thickness == 1.0


def test_scan_ignores_a_run_shorter_than_thirty_percent():
    rows = rows_with_line(line_y=10, line_x0=100, line_x1=200, width=800)
    assert ins.scan_hlines(rows, width=800, scale=1.0) == []


def test_scan_merges_adjacent_rows_into_one_line():
    rows = []
    for y in range(5):
        row = bytearray(800)
        if y in (2, 3):
            for x in range(100, 700):
                row[x] = 1
        rows.append(bytes(row))
    hlines = ins.scan_hlines(rows, width=800, scale=1.0)
    assert len(hlines) == 1
    assert hlines[0].thickness == 2.0


# ---------------------------------------------------------------- 线穿字


def test_a_hairline_crossing_the_glyph_box_is_a_hit():
    """修复前封面的几何：全宽 0.4pt 的线在 y=154.8 压过署名盒（139.3–157.3）。"""
    spans = [
        title_span("行星探测三十年文献综述"),
        sp(114.7, 48.0, 385.7, text="科学、工程与战略研判"),
        sp(139.3, 48.0, 245.3, size=18.0, text="行星探测工程论证工作组"),
    ]
    line = ins.HLine(y0=154.8, y1=155.3, x0=0.0, x1=960.0)
    hits = ins.find_line_hits(spans, [line], CANVAS)
    assert [span.text for _, span in hits] == ["行星探测工程论证工作组"]


def test_a_line_in_the_gap_between_two_text_boxes_is_not_a_hit():
    spans = [
        title_span(),
        sp(60.0, 48.0, 500.0, text="第一行"),
        sp(100.0, 48.0, 500.0, text="第二行"),
    ]
    # 60–82 与 100–122 之间正好空着；线在 90。
    assert ins.find_line_hits(spans, [ins.HLine(90.0, 90.5, 0.0, 960.0)], CANVAS) == []


def test_a_line_that_only_grazes_the_top_of_the_box_is_not_a_hit():
    span = sp(100.0, 48.0, 500.0)  # 盒 100–122，内部（12% 余量）102.6–119.4
    assert ins.find_line_hits([span], [ins.HLine(100.4, 100.9, 0.0, 960.0)], CANVAS) == []
    assert ins.find_line_hits([span], [ins.HLine(101.5, 102.0, 0.0, 960.0)], CANVAS) == []
    assert ins.find_line_hits([span], [ins.HLine(110.0, 110.5, 0.0, 960.0)], CANVAS)


def test_a_line_confined_to_the_right_column_is_not_a_page_rule():
    """只活在右栏图里的横线是插图内部元素，不是穿字页级线。"""
    span = sp(200.0, 700.0, 900.0, text="图内标签")
    line = ins.HLine(210.0, 210.5, 700.0, 906.0)
    assert ins.find_line_hits([span], [line], CANVAS) == []


def test_a_thick_block_is_not_a_line():
    span = sp(200.0, 48.0, 400.0, text="正文")
    line = ins.HLine(205.0, 215.0, 0.0, 960.0)  # 厚 10pt
    assert ins.find_line_hits([span], [line], CANVAS) == []


def test_the_line_must_overlap_a_third_of_the_span_width():
    span = sp(100.0, 400.0, 900.0, text="很长的一行")  # 500pt 宽
    # 线只压到这一行的 100pt（< 30%）。
    line = ins.HLine(110.0, 110.5, 0.0, 500.0)
    assert ins.find_line_hits([span], [line], CANVAS) == []


# ---------------------------------------------------------------- 行分组


def test_rows_group_spans_within_the_tolerance():
    spans = [
        sp(100.0, 48.0, 200.0, text="行内甲"),
        sp(102.0, 210.0, 400.0, text="行内乙"),  # 顶边差 2pt：同一行
        sp(130.0, 48.0, 200.0, text="下一行"),
    ]
    rows = ins.group_rows(spans)
    assert [len(row) for row in rows] == [2, 1]


# ---------------------------------------------------------------- 居中


def test_a_centered_paragraph_is_flagged():
    spans = [title_span("主旨")] + body_lines(
        [71.3, 94.0, 116.7], [60.0, 52.7, 443.0], [915.0, 907.3, 531.0]
    )
    region = ins.body_region(CANVAS, spans)
    finding, metrics = ins.find_centered_body(spans, region, "quote")
    assert finding is not None and finding.code == "centered-body"
    assert metrics["sd_cent_pt"] < 6 and metrics["sd_left_pt"] > 12


def test_a_left_aligned_justified_paragraph_is_not_flagged():
    spans = [title_span("主旨")] + body_lines(
        [71.3, 94.0, 116.7], [48.0, 48.0, 48.0], [912.0, 912.0, 114.0]
    )
    region = ins.body_region(CANVAS, spans)
    finding, _ = ins.find_centered_body(spans, region, "quote")
    assert finding is None


def test_two_end_alignment_is_not_centered():
    """两端对齐：左右都齐，行中心离散度也大，不能判成居中。"""
    spans = [title_span()] + body_lines(
        [71.3, 94.0, 116.7], [48.0, 48.0, 48.0], [912.0, 700.0, 500.0]
    )
    region = ins.body_region(CANVAS, spans)
    finding, metrics = ins.find_centered_body(spans, region, "points")
    assert finding is None
    assert metrics["sd_cent_pt"] > ins.CENTER_SD_CENT_PT


def test_centering_is_never_reported_on_the_cover():
    spans = [title_span("封面")] + body_lines(
        [120.0, 150.0, 180.0], [300.0, 290.0, 320.0], [660.0, 670.0, 640.0]
    )
    region = ins.body_region(CANVAS, spans)
    finding, _ = ins.find_centered_body(spans, region, "title")
    assert finding is None


def test_centering_is_never_reported_on_the_table_page():
    spans = [title_span("目录")] + body_lines(
        [80.0, 110.0, 140.0], [300.0, 290.0, 320.0], [660.0, 670.0, 640.0]
    )
    region = ins.body_region(CANVAS, spans)
    assert ins.find_centered_body(spans, region, "table")[0] is None


# ---------------------------------------------------------------- 失衡


def test_a_top_heavy_navigation_page_is_flagged():
    spans = [title_span("目录")] + body_lines(
        [145.0, 185.0, 225.0], [60.0, 60.0, 60.0], [500.0, 500.0, 500.0]
    )
    region = ins.body_region(CANVAS, spans)
    finding, metrics = ins.find_vertical_imbalance(spans, region, CANVAS, "table")
    assert finding is not None and finding.code == "vertical-imbalance"
    assert metrics["fill_frac"] < 0.45


def test_the_cover_and_the_quote_are_never_flagged_for_imbalance():
    spans = [title_span("封面")] + body_lines([120.0], [60.0], [400.0])
    region = ins.body_region(CANVAS, spans)
    assert ins.find_vertical_imbalance(spans, region, CANVAS, "title")[0] is None
    assert ins.find_vertical_imbalance(spans, region, CANVAS, "quote")[0] is None


def test_a_figure_in_the_right_column_keeps_the_page_balanced():
    """左栏只有三条要点，右栏图注排在下方 —— 下半页不空，不该报失衡。"""
    spans = [title_span("第十二章")] + body_lines(
        [60.0, 96.0, 134.0], [61.0, 61.0, 61.0], [430.0, 390.0, 520.0]
    )
    spans.append(sp(286.0, 600.7, 900.0, size=18.0, text="图 12-2　土卫二全球视图"))
    spans.append(sp(453.0, 600.7, 900.0, size=18.0, text="流的艺术渲染（合成图）"))
    region = ins.body_region(CANVAS, spans)
    assert ins.find_vertical_imbalance(spans, region, CANVAS, "points")[0] is None


def test_a_page_that_fills_the_height_is_not_imbalanced():
    spans = [title_span()] + body_lines(
        [60.0, 150.0, 240.0, 330.0, 420.0], [61.0] * 5, [560.0] * 5
    )
    region = ins.body_region(CANVAS, spans)
    assert ins.find_vertical_imbalance(spans, region, CANVAS, "points")[0] is None


# ---------------------------------------------------------------- 字体


def test_songti_body_is_flagged():
    spans = [title_span()] + body_lines([80.0], [61.0], [400.0], family=SERIF, texts=["正文一句话"])
    region = ins.body_region(CANVAS, spans)
    finding, metrics = ins.find_serif_body(spans, region)
    assert finding is not None and finding.code == "serif-body"
    assert metrics["serif_count"] == 1


def test_allowed_sans_and_math_fonts_pass():
    spans = [title_span()]
    for index, family in enumerate(
        ["LQUDWN+HiraginoSansGB-W3-Identity-H", LATIN, "OTUMCP+Menlo-Regular", "PTNHDM+CMR17", "LMSans"]
    ):
        spans.append(body_lines([80.0 + index * 40.0], [61.0], [400.0], family=family, texts=["正文"])[0])
    region = ins.body_region(CANVAS, spans)
    assert ins.find_serif_body(spans, region)[0] is None


def test_serif_in_the_right_column_or_the_footer_is_not_body():
    spans = [
        title_span(),
        sp(250.0, 700.0, 900.0, family=SERIF, text="右栏图注"),
        sp(516.0, 48.0, 200.0, family=SERIF, size=14.0, text="页脚来源"),
        body_lines([80.0], [61.0], [400.0], texts=["正文"])[0],
    ]
    region = ins.body_region(CANVAS, spans)
    assert ins.find_serif_body(spans, region)[0] is None


def test_pure_ascii_latin_strings_do_not_count_as_body():
    spans = [title_span(), sp(250.0, 61.0, 130.0, family=SERIF, text="p.17")]
    region = ins.body_region(CANVAS, spans)
    assert ins.find_serif_body(spans, region)[0] is None


def test_font_helpers_strip_the_subset_prefix():
    assert ins.strip_subset("LQUDWN+HiraginoSansGB-W6-Identity-H") == "HiraginoSansGB-W6-Identity-H"
    assert ins.font_allowed("LQUDWN+HiraginoSansGB-W6-Identity-H")
    assert ins.font_allowed("PTNHDM+CMR17")
    assert not ins.font_allowed("LJPZAU+STSongti-SC")
    assert ins.font_is_serif("LJPZAU+STSongti-SC")
    assert ins.font_is_serif("RIKUZT+TimesNewRomanPSMT")
    assert not ins.font_is_serif("OTUMCP+Menlo-Regular")


# ---------------------------------------------------------------- 行距


def test_26_over_22_is_cramped():
    spans = [title_span("主旨")] + body_lines(
        [71.3, 97.3, 123.3, 149.3], [61.0] * 4, [560.0] * 4
    )
    region = ins.body_region(CANVAS, spans)
    finding, metrics = ins.find_cramped_leading(spans, region, "quote")
    assert finding is not None and finding.code == "cramped-leading"
    assert metrics["leading_ratio"] == pytest.approx(1.18, abs=0.01)


def test_30_over_22_is_not_cramped():
    spans = [title_span("主旨")] + body_lines(
        [71.3, 101.3, 131.3, 161.3], [61.0] * 4, [560.0] * 4
    )
    region = ins.body_region(CANVAS, spans)
    finding, metrics = ins.find_cramped_leading(spans, region, "quote")
    assert finding is None
    assert metrics["leading_ratio"] == pytest.approx(1.36, abs=0.01)


def test_paragraph_gaps_are_not_treated_as_leading():
    """三条单行要点（段间距 34pt）接一段三行正文（行距 30pt）：取段内中位数。"""
    spans = [title_span()]
    for top in (97.3, 134.7, 172.7):
        spans.append(body_lines([top], [61.0], [400.0], texts=["要点一句。"])[0])
    for top in (210.0, 240.0, 270.0):
        spans.append(body_lines([top], [61.0], [592.0], texts=["正文行。"])[0])
    region = ins.body_region(CANVAS, spans)
    finding, metrics = ins.find_cramped_leading(spans, region, "points")
    assert finding is None
    assert metrics["leading_pt"] == pytest.approx(30.0, abs=0.5)


def test_a_right_column_caption_does_not_poison_the_leading():
    """右栏图注与左栏正文顶边错开，若把它算进行分组会造出 10pt 的假行距。"""
    spans = [title_span()]
    for top in (97.3, 127.3, 157.3):
        spans.append(body_lines([top], [61.0], [592.0], texts=["正文行。"])[0])
    spans.append(sp(110.0, 600.7, 780.0, size=18.0, text="图 1-1　时间轴"))
    spans.append(sp(120.0, 600.7, 780.0, size=18.0, text="续行"))
    region = ins.body_region(CANVAS, spans)
    finding, metrics = ins.find_cramped_leading(spans, region, "points")
    assert finding is None
    assert metrics["leading_pt"] == pytest.approx(30.0, abs=0.5)


def test_leading_is_not_checked_on_the_table_page():
    spans = [title_span("目录")] + body_lines(
        [70.0, 80.0, 90.0], [61.0] * 3, [500.0] * 3
    )
    region = ins.body_region(CANVAS, spans)
    assert ins.find_cramped_leading(spans, region, "table")[0] is None


def test_the_body_region_starts_below_the_title_band_and_ends_above_the_footer():
    spans = [title_span(), sp(516.0, 48.0, 200.0, size=14.0, text="页脚")]
    region = ins.body_region(CANVAS, spans)
    assert region.top == pytest.approx(32.7 + ins.REGION_TOP_GAP_PT, abs=0.1)
    assert region.bottom == pytest.approx(540.0 * ins.REGION_BOTTOM_FRAC, abs=0.1)
    assert ins.body_spans(spans, region) == []


# ---------------------------------------------------------------- 帧序与角色


def _mini_document():
    return {
        "deck": {"deck": "demo"},
        "pages": [
            {"kind": "opening", "label": "封面", "picks": ["（一）被剥的句子"]},
            {"kind": "opening", "label": "主旨", "picks": []},
            {"kind": "navigation", "label": "目录", "auto": True},
            {"kind": "cross_cutting", "label": "横向议题 一", "picks": []},
            {"kind": "backup", "label": "备份 1", "auto": True},
        ],
        "chapters": [
            {"chapter": "1", "label": "第一章", "frames": [{"picks": []}, {"picks": []}]}
        ],
    }


def test_frame_roles_follow_the_build_order():
    refs = ins.frame_refs(_mini_document())
    assert [ref.role for ref in refs] == [
        "title", "quote", "table", "points", "points", "points", "backup"
    ]
    assert [ref.page for ref in refs] == list(range(1, 8))
    assert refs[0].label == "封面" and refs[0].picks == ("（一）被剥的句子",)


def test_lead_enumerator_returns_the_prefix_or_none():
    assert ins.lead_enumerator("（一）火星曾经宜居吗？") == "（一）"
    assert ins.lead_enumerator("1) 第二点") == "1)"
    assert ins.lead_enumerator("① 首先") == "①"
    assert ins.lead_enumerator("1995—2026") is None
    assert ins.lead_enumerator("第一章　导言") is None


def test_printed_enumerators_reads_the_body_rows():
    spans = [
        title_span("第二章"),
        sp(80.0, 48.0, 60.0, text="–"),  # 短横 bullet 单独一个 span
        sp(80.0, 61.0, 400.0, text="（一）火星曾经宜居吗？"),
        sp(110.0, 61.0, 400.0, text="（二）火星的水去了哪里？"),
        sp(140.0, 61.0, 400.0, text="这一段没有编号。"),
    ]
    region = ins.body_region(CANVAS, spans)
    assert ins.printed_enumerators(spans, region) == {"（一）", "（二）"}


def test_printed_enumerators_ignores_text_outside_the_body_region():
    spans = [
        title_span("第二章"),
        sp(516.0, 48.0, 200.0, size=14.0, text="（九）页脚里的编号"),
        sp(250.0, 700.0, 900.0, text="（八）右栏图注里的编号"),
    ]
    region = ins.body_region(CANVAS, spans)
    assert ins.printed_enumerators(spans, region) == set()


def test_numerator_residue_needs_the_page_to_print_it():
    refs = ins.frame_refs(_mini_document())  # 第 1 页的 picks 带「（一）」
    assert ins.numerator_residue_findings(refs, {}, {}) == []           # 页面干净 → 不报
    assert ins.numerator_residue_findings(refs, {}, {1: {"（二）"}}) == []
    findings = ins.numerator_residue_findings(refs, {}, {1: {"（一）"}})
    assert len(findings) == 1
    item = findings[0]
    assert item.code == "numerator-residue" and item.severity == "warning"
    assert item.page == 1 and "「（一）」" in item.evidence


@pytest.mark.parametrize(
    "text",
    ["1995—2026", "1.5 亿公里", "第一章　导言", "（NASA）", "到了 2030 年这个数翻倍。"],
)
def test_years_and_section_numbers_are_not_enumerators(text):
    document = _mini_document()
    document["pages"][0]["picks"] = [text]
    assert ins.numerator_residue_findings(ins.frame_refs(document), {}, {1: {"（一）"}}) == []


def test_fonts_outside_the_allowed_set_are_warned():
    findings = ins.font_findings(["LQUDWN+HiraginoSansGB-W6", "LJPZAU+STSongti-SC"])
    assert [item.code for item in findings] == ["font-substitution"]
    assert findings[0].severity == "warning" and "STSongti-SC" in findings[0].evidence


def test_allowed_fonts_produce_no_font_finding():
    assert ins.font_findings(["LQUDWN+HiraginoSansGB-W6", "PTNHDM+CMR17", "OTUMCP+Menlo-Regular"]) == []


# ---------------------------------------------------------------- 视觉层回信


def test_parse_vision_reply_maps_numbers_to_codes():
    text = "\n".join(
        [
            "1 | 有 | 封面横线压在署名上",
            "2 | 无 | ",
            "3 | 无 |",
            "4 | 有 | 右下角有方块字",
            "5 | 无 |",
            "6 | 无 |",
            "7 | 无 |",
        ]
    )
    findings = ins.parse_vision_reply(text)
    assert [item.code for item in findings] == ["line-through-text", "garbled-glyph"]
    assert all(item.severity == "warning" for item in findings)
    assert findings[0].message == "封面横线压在署名上"


def test_parse_vision_reply_ignores_non_conforming_lines():
    assert ins.parse_vision_reply("这是一段散文，没有编号。\n\n") == []


def test_the_prompt_carries_the_role_and_the_deterministic_facts():
    facts = ins.metrics_facts({"sd_left_pt": 158.2, "sd_cent_pt": 3.3, "lines": 4}, 0)
    prompt = ins.vision_prompt(CANVAS, "quote", facts)
    assert "主旨" in prompt
    assert "左边缘离散度 158.2pt" in prompt
    assert "编号 | 有/无" in prompt


# ---------------------------------------------------------------- 假 poppler


class FakeTools:
    """假的 poppler 封装：按页给回预置的 span/线段/PNG，不碰真文件。"""

    def __init__(self, pages, *, fonts=(), count=None):
        self.pages = pages
        self._fonts = list(fonts)
        self._count = count

    def ensure_available(self):
        pass

    def page_count(self):
        return self._count if self._count is not None else len(self.pages)

    def page_spans(self, page):
        canvas, spans, _ = self.pages[page]
        return canvas, spans

    def page_hlines(self, page):
        return self.pages[page][2]

    def page_png(self, page):
        return b"PNGDATA-" + str(page).encode()

    def font_names(self):
        return list(self._fonts)


def _demo_pages():
    quote = [title_span("主旨")] + body_lines(
        [71.3, 94.0, 116.7], [60.0, 52.7, 443.0], [915.0, 907.3, 531.0]
    )
    clean = [title_span("第一章")] + body_lines(
        [60.0, 120.0, 180.0, 240.0, 300.0, 360.0, 420.0, 470.0],
        [61.0] * 8,
        [560.0] * 8,
    )
    return {
        1: (CANVAS, [title_span("封面")], []),
        2: (CANVAS, quote, []),
        3: (CANVAS, clean, []),
        4: (CANVAS, clean, []),
    }


def _demo_document():
    return {
        "deck": {"deck": "demo"},
        "pages": [
            {"kind": "opening", "label": "封面", "picks": []},
            {"kind": "opening", "label": "主旨", "picks": []},
            {"kind": "navigation", "label": "目录", "auto": True},
            {"kind": "cross_cutting", "label": "横向议题 一"},
        ],
        "chapters": [],
    }


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "工程"
    directory = root / "_primer" / "slides" / "demo"
    directory.mkdir(parents=True)
    (root / "_primer" / "config.yaml").write_text(CONFIG_YAML, encoding="utf-8")
    (directory / "candidates.md").write_text("# empty\n", encoding="utf-8")
    outline = directory / "outline.yaml"
    outline.write_text(
        yaml.safe_dump(_demo_document(), allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    (directory / "slides.pdf").write_bytes(b"%PDF-1.4 stub\n")
    return root, outline


def _environ(tmp_path, key=KEY):
    env = {"XDG_CONFIG_HOME": str(tmp_path / "no-machine-config")}
    if key is not None:
        env["PRIMER_DEMO_API_KEY"] = key
    return env


# ---------------------------------------------------------------- 假传输层


def chat_completion(content, prompt_tokens=40, completion_tokens=12, reasoning=0):
    return json.dumps(
        {
            "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "completion_tokens_details": {"reasoning_tokens": reasoning},
            },
        },
        ensure_ascii=False,
    ).encode("utf-8")


class FakeTransport:
    """多模态假传输层：记下每次请求，按提示词给回信。"""

    def __init__(self, answer=None, delay=0.0):
        self.answer = answer or (lambda prompt, body: "1 | 无 |\n2 | 无 |\n")
        self.delay = delay
        self.requests = []
        self.threads = set()
        self.lock = threading.Lock()

    def __call__(self, request):
        body = json.loads(request.body.decode("utf-8"))
        parts = body["messages"][0]["content"]
        prompt = next(part["text"] for part in parts if part["type"] == "text")
        images = [part for part in parts if part["type"] == "image_url"]
        with self.lock:
            self.requests.append((body, prompt, images))
            self.threads.add(threading.current_thread().name)
        if self.delay:
            time.sleep(self.delay)
        result = self.answer(prompt, body)
        if isinstance(result, Exception):
            raise result
        return chat_completion(result)

    @property
    def prompts(self):
        return [prompt for _, prompt, _ in self.requests]


# ---------------------------------------------------------------- run：确定性 + 记账


def test_run_writes_a_ledger_without_the_key(project, tmp_path):
    root, outline = project
    fake = FakeTools(_demo_pages(), fonts=["LQUDWN+HiraginoSansGB-W6", "LJPZAU+STSongti-SC"])
    result = ins.run(
        outline, root, tools=fake, no_vision=True, environ=_environ(tmp_path)
    )
    text = (outline.parent / ins.INSPECT_LOG_NAME).read_text(encoding="utf-8")
    assert KEY not in text and "PRIMER_DEMO_API_KEY" not in text
    records = [json.loads(line) for line in text.splitlines()]
    assert [record["page"] for record in records] == [1, 2, 3, 4]
    for record in records:
        assert set(record) == {
            "time", "deck", "pdf", "page", "role", "label", "provider", "model",
            "base_url", "prompt_version", "metrics", "findings", "vision",
        }
        assert record["prompt_version"] == ins.PROMPT_VERSION
        assert record["vision"]["status"] == "disabled"
    assert result.written and result.log_path is not None


def test_run_finds_the_centered_quote_page(project, tmp_path):
    root, outline = project
    result = ins.run(
        outline, root, tools=FakeTools(_demo_pages()), no_vision=True,
        environ=_environ(tmp_path),
    )
    centered = [item for item in result.findings if item.code == "centered-body"]
    assert [item.page for item in centered] == [2]
    assert result.errors and not any(item.fatal is False for item in result.errors)


def test_the_frame_count_mismatch_is_a_warning(project, tmp_path):
    root, outline = project
    fake = FakeTools(_demo_pages(), count=7)  # PDF 有 7 页，骨架只有 4 帧
    result = ins.run(
        outline, root, tools=fake, no_vision=True, pages="1-4", environ=_environ(tmp_path)
    )
    assert result.frame_mismatch and result.pages_total == 7
    codes = [item.code for item in result.deck_findings]
    assert "frame-count-mismatch" in codes
    mismatch = [item for item in result.deck_findings if item.code == "frame-count-mismatch"]
    assert all(item.severity == "warning" for item in mismatch)


def test_pages_selects_a_subrange(project, tmp_path):
    root, outline = project
    result = ins.run(
        outline, root, tools=FakeTools(_demo_pages()), no_vision=True, pages="2-3",
        environ=_environ(tmp_path),
    )
    assert result.selected_pages == (2, 3)


def test_an_invalid_pages_value_is_rejected(project, tmp_path):
    root, outline = project
    with pytest.raises(ins.InspectError):
        ins.run(outline, root, tools=FakeTools(_demo_pages()), no_vision=True, pages="3-1",
                environ=_environ(tmp_path))


def test_a_missing_pdf_is_a_clear_error(project, tmp_path):
    root, outline = project
    (outline.parent / ins.DEFAULT_PDF_NAME).unlink()
    with pytest.raises(ins.InspectError, match="slides PDF not found"):
        ins.run(outline, root, no_vision=True, environ=_environ(tmp_path))


def test_a_missing_outline_is_a_clear_error(tmp_path):
    with pytest.raises(ins.InspectError, match="outline not found"):
        ins.run(tmp_path / "nowhere.yaml", tmp_path, no_vision=True, environ=_environ(tmp_path))


def test_missing_poppler_reports_the_missing_tools(monkeypatch, tmp_path):
    monkeypatch.setattr(ins.shutil, "which", lambda name: None)
    with pytest.raises(ins.InspectError, match="poppler tools not found"):
        ins.PdfTools(tmp_path / "x.pdf").ensure_available()


# ---------------------------------------------------------------- run：视觉层


def test_vision_requests_carry_the_prompt_and_the_image(project, tmp_path):
    root, outline = project
    transport = FakeTransport()
    ins.run(
        outline, root, tools=FakeTools(_demo_pages()), transport=transport,
        environ=_environ(tmp_path), timeout=1.0,
    )
    assert len(transport.requests) == 4
    body, prompt, images = next(item for item in transport.requests if "主旨" in item[1])
    assert body["model"] == "demo-vision"
    assert len(images) == 1 and images[0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert "左边缘离散度" in prompt  # 确定性层的数进了提示词


def test_vision_findings_are_attached_to_the_page(project, tmp_path):
    root, outline = project

    def answer(prompt, body):
        return "1 | 有 | 封面右下角有横线压字\n2 | 无 |\n"

    pages = _demo_pages()
    # 封面上真的有一条线穿过标题：视觉层的说法因此拿得到确定性层的旁证。
    pages[1] = (CANVAS, [title_span("封面")], [ins.HLine(20.0, 20.5, 0.0, 960.0)])
    transport = FakeTransport(answer)
    result = ins.run(
        outline, root, tools=FakeTools(pages), transport=transport,
        environ=_environ(tmp_path), timeout=1.0,
    )
    first = result.page_results[0]
    assert first.vision is not None and first.vision.status == "ok"
    assert [item.code for item in first.vision.findings] == ["line-through-text"]
    assert first.vision.findings[0].page == 1
    assert first.vision.findings[0].severity == "warning"
    assert first.vision.suppressed == ()


def test_an_empty_reply_is_retried_with_a_bigger_budget(project, tmp_path):
    root, outline = project
    seen = {"n": 0}

    def answer(prompt, body):
        seen["n"] += 1
        if seen["n"] == 1:
            return ""  # 空正文
        return "1 | 无 |\n"

    transport = FakeTransport(answer)
    ins.run(
        outline, root, tools=FakeTools(_demo_pages()), transport=transport,
        environ=_environ(tmp_path), timeout=1.0,
    )
    first_body, _, _ = transport.requests[0]
    second_body, _, _ = transport.requests[1]
    assert first_body["max_tokens"] == 1024
    assert second_body["max_tokens"] == 2048


def test_vision_failure_on_one_page_does_not_stop_the_others(project, tmp_path):
    root, outline = project

    def answer(prompt, body):
        if "目录" in prompt:
            raise ins.client_mod.SelectCallError("endpoint returned an empty message")
        return "1 | 无 |\n"

    transport = FakeTransport(answer)
    result = ins.run(
        outline, root, tools=FakeTools(_demo_pages()), transport=transport,
        environ=_environ(tmp_path), timeout=1.0,
    )
    statuses = {page.ref.page: page.vision.status for page in result.page_results}
    assert statuses[3] == "error"
    assert statuses[1] == "ok" and statuses[2] == "ok"
    assert result.failures and "page 3" in result.failures[0]


def test_vision_runs_concurrently_and_the_ledger_is_written_once(project, tmp_path):
    root, outline = project
    transport = FakeTransport(delay=0.05)
    result = ins.run(
        outline, root, tools=FakeTools(_demo_pages()), transport=transport, workers=3,
        environ=_environ(tmp_path), timeout=1.0,
    )
    assert len(transport.threads) >= 2, "3 线程 4 页，应至少落到两个线程上"
    records = (outline.parent / ins.INSPECT_LOG_NAME).read_text(encoding="utf-8").splitlines()
    assert len(records) == 4, "每页一条，且没有并发重复写"
    assert {json.loads(line)["page"] for line in records} == {1, 2, 3, 4}
    assert result.requests == 4


def test_no_vision_never_reads_the_key(project, tmp_path):
    root, outline = project
    transport = FakeTransport()
    result = ins.run(
        outline, root, tools=FakeTools(_demo_pages()), transport=transport,
        no_vision=True, environ=_environ(tmp_path, key=None),
    )
    assert transport.requests == []
    assert all(page.vision is None for page in result.page_results)
    assert result.endpoint is None


def test_a_missing_key_is_a_clear_error(project, tmp_path):
    root, outline = project
    with pytest.raises(ins.InspectError, match="PRIMER_DEMO_API_KEY"):
        ins.run(
            outline, root, tools=FakeTools(_demo_pages()), transport=FakeTransport(),
            environ=_environ(tmp_path, key=None), timeout=1.0,
        )


# ---------------------------------------------------------------- dry-run 与退出码


def test_dry_run_writes_nothing_and_needs_no_key(project, tmp_path):
    root, outline = project
    before = outline.read_bytes()
    transport = FakeTransport()
    result = ins.run(
        outline, root, tools=FakeTools(_demo_pages()), transport=transport, dry_run=True,
        environ=_environ(tmp_path, key=None),
    )
    assert result.dry_run and not result.written
    assert transport.requests == []
    assert outline.read_bytes() == before
    assert not (outline.parent / ins.INSPECT_LOG_NAME).exists()
    report = "\n".join(ins.report_lines(result))
    assert "一个字节都不写" in report and "视觉层" in report


def test_dry_run_through_main_writes_nothing(project, tmp_path, monkeypatch, capsys):
    root, outline = project
    fake = FakeTools(_demo_pages())
    monkeypatch.setattr(ins, "PdfTools", lambda pdf, dpi=None, threshold=None: fake)
    code = ins.main([str(outline), "--project-root", str(root), "--dry-run"])
    out = capsys.readouterr().out
    assert code == 0
    assert "dry-run" in out
    assert not (outline.parent / ins.INSPECT_LOG_NAME).exists()


def test_main_exits_two_with_an_error_and_zero_with_only_warnings(project, tmp_path, monkeypatch, capsys):
    root, outline = project
    pages = _demo_pages()
    # 只有 warning：把主旨页换成左对齐（无 error），封面页真的印出了原文编号 → 1 条 warning。
    document = _demo_document()
    document["pages"][0]["picks"] = ["（一）火星曾经宜居吗？"]
    outline.write_text(
        yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    clean = [title_span("主旨")] + body_lines(
        [71.3, 101.3, 131.3], [48.0] * 3, [912.0, 912.0, 114.0]
    )
    pages[2] = (CANVAS, clean, [])
    pages[1] = (
        CANVAS,
        [title_span("封面"), sp(120.0, 61.0, 420.0, text="（一）火星曾经宜居吗？")],
        [],
    )
    monkeypatch.setattr(ins, "PdfTools", lambda pdf, dpi=None, threshold=None: FakeTools(pages))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-machine-config"))
    assert ins.main([str(outline), "--project-root", str(root), "--no-vision"]) == 0
    text = capsys.readouterr().out
    assert "numerator-residue" in text and "error 0 条" in text

    # 换成封面带横向穿字线 → error → 2。
    crossed = pages[1][1] + [sp(120.0, 48.0, 245.3, size=18.0, text="行星探测工程论证工作组")]
    pages[1] = (CANVAS, crossed, [ins.HLine(128.0, 128.5, 0.0, 960.0)])
    assert ins.main([str(outline), "--project-root", str(root), "--no-vision"]) == 2
    assert "line-through-text" in capsys.readouterr().out


def test_main_tolerates_a_leading_inspect_token(project, tmp_path, monkeypatch, capsys):
    root, outline = project
    monkeypatch.setattr(ins, "PdfTools", lambda pdf, dpi=None, threshold=None: FakeTools(_demo_pages()))
    code = ins.main(["inspect", str(outline), "--project-root", str(root), "--no-vision"])
    assert code in (0, 2)
    assert "版面质检" in capsys.readouterr().out


def test_json_output_carries_english_keys(project, tmp_path, monkeypatch, capsys):
    root, outline = project
    monkeypatch.setattr(ins, "PdfTools", lambda pdf, dpi=None, threshold=None: FakeTools(_demo_pages()))
    ins.main([str(outline), "--project-root", str(root), "--no-vision", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) >= {"deck", "pages_total", "summary", "findings", "pages"}
    assert isinstance(payload["summary"]["errors"], int)


def test_the_markdown_report_is_written_in_chinese(project, tmp_path):
    root, outline = project
    report = tmp_path / "报告.md"
    ins.run(
        outline, root, tools=FakeTools(_demo_pages()), no_vision=True,
        report_path=report, environ=_environ(tmp_path),
    )
    text = report.read_text(encoding="utf-8")
    assert "版面质检" in text and "该改哪里" in text and "centered-body" in text


def test_a_deck_level_finding_shows_under_its_page(project, tmp_path):
    """挂在某一页上的整份发现（编号残留）要出现在那一页的条目里，不能写成"无发现"。"""
    root, outline = project
    document = _demo_document()
    document["pages"][0]["picks"] = ["（一）火星曾经宜居吗？"]
    outline.write_text(
        yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    pages = _demo_pages()
    pages[1] = (
        CANVAS,
        [title_span("封面"), sp(120.0, 61.0, 420.0, text="（一）火星曾经宜居吗？")],
        [],
    )
    result = ins.run(
        outline, root, tools=FakeTools(pages), no_vision=True, environ=_environ(tmp_path)
    )
    report = "\n".join(ins.report_lines(result))
    assert "第 1 页 封面（封面）：无发现" not in report
    assert "第 1 页 封面（封面）\n    - numerator-residue" in report


def test_run_does_not_report_enumerators_the_page_has_already_stripped(project, tmp_path):
    """picks 里带编号、但页面已经剥掉（生产 deck 的常态）→ 一条都不报。"""
    root, outline = project
    document = _demo_document()
    document["pages"][0]["picks"] = ["（一）火星曾经宜居吗？", "（二）行星有多普遍？"]
    outline.write_text(
        yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    result = ins.run(
        outline, root, tools=FakeTools(_demo_pages()), no_vision=True,
        environ=_environ(tmp_path),
    )
    assert [item.code for item in result.findings if item.code == "numerator-residue"] == []


# ---------------------------------------------------------------- 视觉层：旁证与豁免


def vf(code, message="视觉层的说法"):
    return ins.Finding(code=code, severity="warning", page=0, message=message)


def test_the_vision_confirm_applies_the_deterministic_role_exemption():
    spans = [title_span("封面")]
    hlines = []
    kept, dropped = ins._vision_confirm(
        "title", [vf("vertical-imbalance"), vf("overlap")], canvas=CANVAS, spans=spans, hlines=hlines
    )
    assert [item.code for item in kept] == ["overlap"]  # 定性发现照收
    assert [(item.code, item.reason, item.count) for item in dropped] == [
        ("vertical-imbalance", "role-exempt", 1)
    ]
    kept, dropped = ins._vision_confirm(
        "table", [vf("vertical-imbalance")], canvas=CANVAS, spans=spans, hlines=hlines
    )
    assert [item.code for item in kept] == ["vertical-imbalance"] and dropped == ()


def test_vision_clipped_text_needs_the_deterministic_edge():
    """p27 这一类：视觉说页码"距右缘约 9pt"，实测 48.2pt（正好是版心边距）→ 不报。"""
    page_number = sp(516.0, 876.1, 911.8, size=16.0, text="27/45")
    spans = [title_span("第十二章"), page_number]
    kept, dropped = ins._vision_confirm(
        "points", [vf("clipped-text")], canvas=CANVAS, spans=spans, hlines=[]
    )
    assert kept == []
    assert [(item.code, item.reason) for item in dropped] == [
        ("clipped-text", "geometry-unconfirmed")
    ]


def test_vision_clipped_text_is_kept_when_the_text_really_touches_the_edge():
    touching = sp(516.0, 876.1, 959.0, size=16.0, text="27/45")  # 距右缘 1.0pt
    kept, dropped = ins._vision_confirm(
        "points", [vf("clipped-text")], canvas=CANVAS,
        spans=[title_span("第十二章"), touching], hlines=[],
    )
    assert [item.code for item in kept] == ["clipped-text"] and dropped == ()
    assert "确定性层" in kept[0].evidence and "距画布边 1.0pt" in kept[0].evidence
    # 越界同样算数。
    outside = sp(516.0, 876.1, 965.0, size=16.0, text="27/45")
    kept, _ = ins._vision_confirm(
        "points", [vf("clipped-text")], canvas=CANVAS,
        spans=[title_span("第十二章"), outside], hlines=[],
    )
    assert "越界" in kept[0].evidence


def test_vision_line_through_text_needs_a_deterministic_line_hit():
    """视觉说线穿字，但确定性层没扫到线 → 不报。"""
    spans = [title_span("第十二章"), sp(120.0, 61.0, 400.0, text="正文一行")]
    kept, dropped = ins._vision_confirm(
        "points", [vf("line-through-text")], canvas=CANVAS, spans=spans, hlines=[]
    )
    assert kept == []
    assert [(item.code, item.reason) for item in dropped] == [
        ("line-through-text", "geometry-unconfirmed")
    ]
    # 扫到了同一条线 → 报，并把实测坐标补进证据。
    line = ins.HLine(y0=128.0, y1=128.5, x0=0.0, x1=960.0)
    kept, dropped = ins._vision_confirm(
        "points", [vf("line-through-text")], canvas=CANVAS, spans=spans, hlines=[line]
    )
    assert [item.code for item in kept] == ["line-through-text"] and dropped == ()
    assert "y=128.0pt" in kept[0].evidence


def test_edge_violations_measures_the_distance_to_the_canvas():
    body = sp(120.0, 48.0, 592.0, text="正文")
    mid = sp(200.0, 876.1, 911.8, size=16.0, text="27/45")
    assert ins.edge_distance(mid, CANVAS) == pytest.approx(48.2, abs=0.05)
    # 生产 deck 的页码在页脚带里：距右 48.2pt、距下 7.3pt，最近的边也还有 7pt 余量。
    page_number = sp(516.0, 876.1, 911.8, size=16.0, text="27/45")
    assert ins.edge_distance(page_number, CANVAS) == pytest.approx(8.0, abs=0.1)
    assert ins.edge_violations([body, page_number], CANVAS) == []
    touching = sp(200.0, 876.1, 959.5, size=16.0, text="27/45")
    found = ins.edge_violations([body, touching], CANVAS)
    assert [span.text for _, span in found] == ["27/45"]
    outside = sp(200.0, -3.0, 60.0, size=16.0, text="越界")
    assert ins.edge_violations([body, outside], CANVAS)


def test_vision_imbalance_is_dropped_on_the_cover_and_kept_on_navigation(project, tmp_path):
    root, outline = project

    def answer(prompt, body):
        return "5 | 有 | 下方空出 50%\n"

    transport = FakeTransport(answer)
    result = ins.run(
        outline, root, tools=FakeTools(_demo_pages()), transport=transport,
        environ=_environ(tmp_path), timeout=1.0,
    )
    cover = result.page_results[0]  # 第 1 页，封面（title）
    quote = result.page_results[1]  # 第 2 页，主旨（quote）
    navigation = result.page_results[2]  # 第 3 页，目录（table）
    assert cover.vision.findings == ()
    assert cover.vision.suppressed_count("vertical-imbalance") == 1
    assert quote.vision.suppressed_count("vertical-imbalance") == 1
    assert "vertical-imbalance" in [item.code for item in navigation.vision.findings]
    assert navigation.vision.suppressed == ()
    report = "\n".join(ins.report_lines(result))
    assert "视觉层丢掉了 2 条" in report
    assert "角色豁免 2 条（vertical-imbalance 2）" in report
    assert "封面/主旨这类上部重构图" in report


def test_vision_geometry_claims_are_confirmed_or_suppressed_through_run(project, tmp_path):
    """同一批页里：p1 的贴边说法站不住（边距 48pt）→ suppressed；p3 的站得住（1pt）→ 报。"""
    root, outline = project

    def answer(prompt, body):
        return "2 | 有 | 文字紧贴画布右缘\n"

    pages = _demo_pages()
    pages[1] = (
        CANVAS,
        [title_span("封面"), sp(516.0, 876.1, 911.8, size=16.0, text="1/45")],
        [],
    )
    pages[3] = (
        CANVAS,
        [title_span("横向议题 一"), sp(516.0, 876.1, 959.0, size=16.0, text="3/45")],
        [],
    )
    transport = FakeTransport(answer)
    result = ins.run(
        outline, root, tools=FakeTools(pages), transport=transport,
        environ=_environ(tmp_path), timeout=1.0,
    )
    first, second, third = result.page_results[0], result.page_results[1], result.page_results[2]
    assert first.vision.findings == ()
    assert first.vision.suppressed_count("clipped-text") == 1
    assert second.vision.suppressed_count("clipped-text") == 1
    assert result.page_results[3].vision.suppressed_count("clipped-text") == 1
    assert [item.code for item in third.vision.findings] == ["clipped-text"]
    assert "确定性层" in third.vision.findings[0].evidence
    report = "\n".join(ins.report_lines(result))
    assert "几何未证实 3 条（clipped-text 3）" in report
    payload = ins.result_json(result)
    assert payload["suppressed_total"] == 3
    recorded = ins._log_record(first, result, "2026-09-28T15:00:00+08:00")
    assert recorded["vision"]["suppressed"] == [
        {"code": "clipped-text", "reason": "geometry-unconfirmed", "count": 1}
    ]
    assert recorded["vision"]["suppressed_total"] == 1
    kept_record = ins._log_record(third, result, "2026-09-28T15:00:00+08:00")
    assert kept_record["vision"]["suppressed"] == []


# ---------------------------------------------------------------- 子命令注册


def test_the_registered_inspect_subcommand_runs(project, tmp_path, monkeypatch, capsys):
    from primer.slides.__main__ import main as cli_main

    root, outline = project
    monkeypatch.setattr(
        ins, "PdfTools", lambda pdf, dpi=None, threshold=None: FakeTools(_demo_pages())
    )
    code = cli_main(["inspect", str(outline), "--project-root", str(root), "--no-vision"])
    out = capsys.readouterr().out
    assert code in (0, 2)
    assert out.splitlines()[0] == "primer-slides inspect · 版面质检"
    assert "结论：" in out


def test_the_registered_parser_lists_inspect_with_its_options():
    from primer.slides.__main__ import build_parser

    help_text = build_parser().format_help()
    assert "inspect" in help_text
    parser = build_parser()
    sub = next(
        action for action in parser._actions if getattr(action, "choices", None)
    )
    inspect_parser = sub.choices["inspect"]
    option_strings = {
        option
        for action in inspect_parser._actions
        for option in action.option_strings
    }
    assert {"--pdf", "--no-vision", "--pages", "--workers", "--dry-run"} <= option_strings
