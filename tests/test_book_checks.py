# -*- coding: utf-8 -*-
"""``check --deep`` 的确定性质检：回归夹具与纯函数单测。

前四个测试是本次生产里真实出过的缺陷：原样印进 PDF 的 markdown 链接、CJK 字体
画错方向的 ASCII 直引号、目录把自己列成条目、当表格分隔行印出来的 ``---``。
它们全都只靠 ``.tex``／``.toc`` 这类文本层就能抓到：不装 TeX、不请视觉模型。
后面是几何换算与插图分辨率的算术单测——纯函数，连微型书都不用拼。
"""

import os

import pytest

from book_fixtures import MANIFEST, ONE_MD, ONE_PIXEL_PNG, tiny_book

from primer.book import checks
from primer.book.__main__ import main
from primer.book.builder import BookBuilder
from primer.book.manifest import Typography, load_manifest

# 与生产里 review_full.log 的那一段逐字一致（单位 TeX pt）。
LOG_GEOMETRY = (
    "* h-part:(L,W,R)=(72.2698pt, 452.96826pt, 72.2698pt)\n"
    "* v-part:(T,H,B)=(72.2698pt, 700.50723pt, 72.2698pt)\n"
    "* \\paperwidth=597.50787pt\n"
    "* \\paperheight=845.04684pt\n"
)

FIGURE_OPTIONS = r"[width=0.96\linewidth,keepaspectratio,height=0.68\textheight]"

GEOMETRY = checks.parse_geometry(LOG_GEOMETRY)
# 正文字号的一个 em（本书清单里 body_font_size 是四号 = 14pt）
EM_BP = checks.body_em_bp(Typography())


def _deep(tmp_path, *, one_md=ONE_MD, manifest_text=MANIFEST, patch_tex=None, toc=None):
    """装配微型书（只到 ``.tex``），按需改写产物，再跑一遍深度检查。"""
    path = tiny_book(tmp_path, manifest_text, one_md)
    manifest = load_manifest(path)
    builder = BookBuilder(manifest, tex_only=True)
    builder.build()
    if patch_tex is not None:
        tex = builder.plan.tex.read_text(encoding="utf-8")
        builder.plan.tex.write_text(patch_tex(tex), encoding="utf-8")
    if toc is not None:
        builder.plan.toc.write_text(toc, encoding="utf-8")
    return checks.run_checks(manifest, builder.plan), builder


def _codes(findings):
    return [item.code for item in findings]


# ---------------------------------------------------------------- 四个回归夹具


def test_a_link_whose_target_is_relative_survives_and_is_flagged(tmp_path):
    """链接目标里带空格时链接正则匹配不上，整段 markdown 会原样印进 PDF。

    生产里的成因是"装配先抹掉工程根前缀，绝对路径变相对路径"，这里换成同一种
    落点：正文里留下了 ``](``。
    """
    source = ONE_MD.replace(
        "[站点](https://example.org/a_b)", "[资料存档](参考资料/存档 目录/x.pdf)"
    )

    findings, builder = _deep(tmp_path, one_md=source)

    assert "[资料存档](参考资料/存档 目录/x.pdf)" in builder.plan.tex.read_text(encoding="utf-8")
    residue = [item for item in findings if item.code == "markdown-residue"]
    assert residue
    assert "link syntax" in residue[0].message
    assert "line " in residue[0].location


def test_an_ascii_quote_left_in_the_body_is_flagged(tmp_path):
    """关掉引号规整后，源里的 ASCII 直引号会原样进正文——CJK 字体把它画成右引号。"""
    manifest_text = MANIFEST.replace(
        'body_font_size: "4"', 'body_font_size: "4"\n  normalize_quotes: false'
    )
    source = ONE_MD.replace("正文一段", '正文一段，他说"你好"')

    findings, builder = _deep(tmp_path, manifest_text=manifest_text, one_md=source)

    assert '"你好"' in builder.plan.tex.read_text(encoding="utf-8")
    # 每个残留的 ASCII 引号各一条发现：``"你好"`` 是两处
    quotes = [item for item in findings if item.code == "straight-quote"]
    assert len(quotes) == 2
    assert "line " in quotes[0].location


def test_a_toc_that_lists_itself_is_flagged(tmp_path):
    """``\\contentsline {chapter}{目录}{4}{section*.3}``——目录把自己列了一条。"""
    toc = (
        "\\contentsline {chapter}{凡例}{3}{chapter*.2}%\n"
        "\\contentsline {chapter}{目录}{4}{section*.3}%\n"
        "\\contentsline {chapter}{插图目录}{8}{section*.4}%\n"
    )

    findings, _builder = _deep(tmp_path, toc=toc)

    self_listing = [item for item in findings if item.code == "toc-lists-itself"]
    assert len(self_listing) == 1
    assert "目录" in self_listing[0].message


def test_the_other_catalogs_in_the_toc_are_not_self_listing(tmp_path):
    """目录合法地列着插图目录、表格目录；只有列自己才算缺陷。"""
    toc = (
        "\\contentsline {chapter}{插图目录}{8}{section*.4}%\n"
        "\\contentsline {chapter}{表格目录}{11}{section*.5}%\n"
        "\\contentsline {chapter}{附录目录}{13}{chapter*.7}%\n"
    )

    findings, _builder = _deep(tmp_path, toc=toc)

    assert "toc-lists-itself" not in _codes(findings)


def test_a_markdown_separator_row_in_the_tex_is_flagged(tmp_path):
    """``|---|---|`` 是 markdown 表格的分隔行；它出现在 .tex 里就是没转干净。"""

    def patch(text):
        return text.replace(r"\begin{document}", "\\begin{document}\n\n|---|---|\n", 1)

    findings, builder = _deep(tmp_path, patch_tex=patch)

    assert "|---|---|" in builder.plan.tex.read_text(encoding="utf-8")
    residue = [item for item in findings if item.code == "markdown-residue"]
    assert any("separator" in item.message for item in residue)


def test_a_blockquote_marker_in_the_body_is_flagged(tmp_path):
    """文献表逐行发射时行首的 ``>`` 会原样印进 PDF——文本层必须抓得到。"""
    text = "\\noindent\\hangindent=2em > 版本：v1\\par\n> 结构：A/B\n"

    findings = checks.markdown_residue(text)

    assert [item.code for item in findings] == ["markdown-residue", "markdown-residue"]
    assert all("blockquote" in item.message for item in findings)
    assert findings[0].location.startswith("line 1: ")
    assert findings[1].location.startswith("line 2: ")


def test_a_greater_than_in_body_text_is_not_a_blockquote_marker(tmp_path):
    """``>450°C`` 里的 ``>`` 不在行首，也不是引用标记——不许误报。"""
    assert checks.markdown_residue("温度 >450°C 与 >1 km 的阈值\n") == []


# ---------------------------------------------------------------- 清单对账与目录


def test_a_label_without_a_list_entry_is_flagged(tmp_path):
    """正文有 ``\\label{fig:…}``、插图目录却是空的：题注丢了。"""
    _findings, builder = _deep(tmp_path)
    tex = builder.plan.tex.read_text(encoding="utf-8")

    findings = checks.list_entry_mismatch(tex, lof="", lot="")

    assert [item.code for item in findings].count("list-entry-mismatch") == 2
    figures = [item for item in findings if "figure" in item.message][0]
    assert "2 figure label(s) in the body vs 0 entry(ies)" in figures.message
    assert "fig:1-1" in figures.location


def test_an_absent_list_is_not_a_mismatch(tmp_path):
    """引擎没跑过、根本没有 .lof 时不该报"少了 N 条"。"""
    _findings, builder = _deep(tmp_path)

    findings = checks.list_entry_mismatch(
        builder.plan.tex.read_text(encoding="utf-8"), lof=None, lot=None
    )

    assert findings == []


def test_parse_contentslines_reads_nested_titles():
    text = (
        "\\contentsline {chapter}{目录}{4}{section*.3}%\n"
        "\\contentsline {figure}{\\numberline {1-1}{\\ignorespaces 行星时间轴}}"
        "{18}{figure.caption.8}%\n"
    )

    entries = checks.parse_contentslines(text)

    assert [page for _title, page in entries] == ["4", "18"]
    assert checks.plain_title(entries[0][0]) == "目录"
    assert checks.plain_title(entries[1][0]) == "行星时间轴"


# ---------------------------------------------------------------- 引号


def test_quote_direction_accepts_alternating_quotes():
    assert checks.quote_direction("从“描述行星”到“理解过程”\n") == []


def test_quote_direction_flags_a_reversed_pair():
    findings = checks.quote_direction("他”说“你好\n")

    assert [item.code for item in findings] == ["quote-direction", "quote-direction"]


def test_mask_tex_blanks_the_duplicated_caption_and_toc_arguments():
    """``\\addcontentsline`` 与 ``\\caption[...]`` 是同一段引号的副本，先屏蔽掉。"""
    text = (
        "\\addcontentsline{toc}{chapter}{“目录”}\n"
        "\\caption[“短题注”]{“短题注”。来源}\n"
        "正文里“引用”一次。\n"
    )

    masked = checks.mask_tex(text)

    assert "\\addcontentsline" not in masked
    assert "[“短题注”]" not in masked
    # 长题注是正文本身，留着；行号与长度也不许变
    assert "“短题注”。来源" in masked
    assert "正文里“引用”一次。" in masked
    assert len(masked) == len(text)
    assert masked.count("\n") == text.count("\n")


def test_mask_tex_blanks_the_heading_marks():
    """``\\chaptermark``/``\\sectionmark`` 是标题的截断副本，先屏蔽掉再数引号。"""
    text = "\\chaptermark{“短题”}\n\\sectionmark{“短题”}\n正文“引用”结束。\n"

    masked = checks.mask_tex(text)

    assert "“短题”" not in masked
    assert "正文“引用”结束。" in masked


def test_mask_tex_blanks_the_heading_optional_argument():
    """``\\chapter[完整题名]{断了行的题名}`` 的可选参数是同一段文字的副本。"""
    text = "\\chapter[完整题名]{完整题名}\n\\section[短题]{短题}\n正文“引用”结束。\n"

    masked = checks.mask_tex(text)

    assert masked.count("完整题名") == 1  # 可选参数被抹掉，只剩强制参数那一份
    assert masked.count("短题") == 1
    assert len(masked) == len(text)
    assert masked.count("\n") == text.count("\n")


def test_quote_direction_accepts_a_title_repeated_by_addcontentsline():
    """标题在 ``\\chapter*`` 与 ``\\addcontentsline`` 里各出现一次，仍是合法交替。"""
    text = "\\chapter*{“凡例”}\n\\addcontentsline{toc}{chapter}{“凡例”}\n正文“引用”结束。\n"

    assert checks.quote_direction(text) == []


# ---------------------------------------------------------------- 几何换算


def test_geometry_is_converted_from_tex_points_to_pdf_points():
    geometry = checks.parse_geometry(LOG_GEOMETRY)

    assert geometry is not None
    # 2.54cm = 1inch = 72 TeX pt = 72bp：四条边距都该正好落在 72bp
    assert geometry.left == pytest.approx(72.0, abs=0.01)
    assert geometry.right == pytest.approx(523.28, abs=0.01)
    assert geometry.top == pytest.approx(72.0, abs=0.01)
    assert geometry.bottom == pytest.approx(769.89, abs=0.01)
    assert geometry.paper_width == pytest.approx(595.28, abs=0.01)
    assert geometry.paper_height == pytest.approx(841.89, abs=0.01)
    assert geometry.paper_width - geometry.right == pytest.approx(72.0, abs=0.01)
    assert geometry.right - geometry.left == pytest.approx(451.28, abs=0.01)


def test_geometry_is_absent_without_a_report():
    assert checks.parse_geometry("no geometry report in this log") is None


def test_parse_bbox_reads_pages_and_words():
    xml = (
        '<page width="595.280000" height="841.890000">'
        '<word xMin="72.000000" yMin="100.000000" xMax="523.000000" yMax="112.000000">正文</word>'
        "</page>"
    )

    pages = checks.parse_bbox(xml)

    assert len(pages) == 1
    assert pages[0].width == pytest.approx(595.28)
    assert pages[0].words[0].text == "正文"
    assert pages[0].words[0].right == pytest.approx(523.0)


def _page(*words):
    body = "".join(
        f'<word xMin="{left}" yMin="{top}" xMax="{right}" yMax="{bottom}">{text}</word>'
        for left, top, right, bottom, text in words
    )
    return checks.parse_bbox(f'<page width="595.280000" height="841.890000">{body}</page>')


def test_a_word_crossing_the_text_block_is_flagged():
    pages = _page(
        ("72.000000", "100.000000", "523.000000", "112.000000", "正常"),
        ("60.000000", "200.000000", "130.000000", "212.000000", "越界"),
    )

    findings = checks.text_out_of_block(pages, GEOMETRY, EM_BP)

    assert [item.code for item in findings] == ["text-out-of-block"]
    assert "left edge" in findings[0].message
    assert findings[0].location.startswith("page 1: ")


def test_a_line_final_cjk_punctuation_protrusion_is_exempt():
    """行末全角标点按半个字宽悬挂：词框的步进宽度越界，墨迹并没有越界。"""
    pages = _page(("100.000000", "100.000000", "532.700000", "112.000000", "行末标点，"))

    assert checks.text_out_of_block(pages, GEOMETRY, EM_BP) == []


def test_a_line_initial_full_width_bracket_protrusion_is_exempt():
    pages = _page(("63.690000", "100.000000", "200.000000", "112.000000", "（PIA21068）"))

    assert checks.text_out_of_block(pages, GEOMETRY, EM_BP) == []


def test_the_same_protrusion_on_a_non_punctuation_word_still_fires():
    """同一个越界量，词尾不是标点就是真缺陷——豁免只认标点。"""
    pages = _page(("100.000000", "100.000000", "532.700000", "112.000000", "行末文字"))

    findings = checks.text_out_of_block(pages, GEOMETRY, EM_BP)

    assert len(findings) == 1
    assert "right edge" in findings[0].message


def test_a_punctuation_protrusion_wider_than_one_em_still_fires():
    """一个字宽是 14pt≈13.95bp；越界 30bp 的标点已经不是悬挂，是排不下。"""
    pages = _page(("100.000000", "100.000000", "553.300000", "112.000000", "行末标点，"))

    findings = checks.text_out_of_block(pages, GEOMETRY, EM_BP)

    assert len(findings) == 1
    assert "extends 30.0 bp" in findings[0].message


def test_rotated_pages_are_measured_against_the_paper_edge():
    """横向页的词框不在竖排版心的坐标系里（x 超出本页声明的页宽），改量纸张边距。

    这一页的坐标框是转置的：纸张 841.89×595.28bp，词框离右纸边 42.89bp > 36bp，
    干净——页眉、页码本来就在纸边附近，不算缺陷。
    """
    pages = _page(("700.000000", "100.000000", "799.000000", "112.000000", "横向页"))

    assert checks.text_out_of_block(pages, GEOMETRY, EM_BP) == []


def test_a_rotated_page_that_comes_too_close_to_the_paper_edge_is_flagged():
    pages = _page(("820.000000", "100.000000", "838.000000", "112.000000", "压边表格"))

    findings = checks.text_out_of_block(pages, GEOMETRY, EM_BP)

    assert len(findings) == 1
    assert "landscape page" in findings[0].message
    assert "within 3.9 bp of the paper's right edge" in findings[0].message


def test_the_em_allowance_follows_the_manifest_font_size():
    assert checks.body_em_bp(Typography()) == pytest.approx(14.0 * 72 / 72.27)
    assert checks.body_em_bp(Typography(body_font_size="-4")) == pytest.approx(12.0 * 72 / 72.27)
    assert checks.body_em_bp(Typography(body_font_size="11pt")) == pytest.approx(11.0 * 72 / 72.27)


def test_two_overlapping_running_head_marks_are_flagged():
    """页眉左右两条标记都太长，在版心两端叠印；y 在同一行、x 重叠 30bp。"""
    pages = _page(
        ("72.000000", "40.000000", "290.000000", "56.000000", "能力基线：到得了——运输与能源"),
        ("260.000000", "40.000000", "523.270000", "56.000000", "未来五至十年：能力基线"),
    )

    findings = checks.running_head_collision(pages, GEOMETRY)

    assert [item.code for item in findings] == ["running-head-collision"]
    assert findings[0].severity == "warning"
    assert "overlap 30.0 bp" in findings[0].message
    assert findings[0].location == "page 1"


def test_two_running_head_marks_that_do_not_meet_are_left_alone():
    pages = _page(
        ("72.000000", "40.000000", "200.000000", "56.000000", "第八章"),
        ("300.000000", "40.000000", "523.270000", "56.000000", "第一节"),
    )

    assert checks.running_head_collision(pages, GEOMETRY) == []


def test_a_body_line_below_the_text_block_is_not_a_header_collision():
    """版心上边界（72bp）以下是正文；全角标点的步进宽度会让相邻词框稍稍重叠，
    那不是叠印——页眉带只取版心之上的词框。"""
    pages = _page(
        ("72.000000", "78.000000", "232.000000", "90.000000", "业格局呈“系统强、器件弱”"),
        ("228.000000", "75.000000", "523.000000", "90.000000", "：整机与系统集成"),
    )

    assert checks.running_head_collision(pages, GEOMETRY) == []


def test_a_page_with_next_to_no_text_is_reported():
    pages = checks.parse_bbox('<page width="595.280000" height="841.890000"></page>')

    findings = checks.page_near_blank(pages)

    assert [item.code for item in findings] == ["page-near-blank"]
    assert findings[0].severity == "info"
    assert "0 word box" in findings[0].message
    assert findings[0].location == "page 1"


def test_a_full_text_page_is_not_reported():
    xml = (
        '<page width="595.280000" height="841.890000">'
        '<word xMin="72.000000" yMin="100.000000" xMax="523.000000" yMax="700.000000">整页</word>'
        "</page>"
    )

    assert checks.page_near_blank(checks.parse_bbox(xml)) == []


def test_page_near_blank_measures_body_ink_only():
    """页眉与页码落在版心外的边距里，不算正文墨迹——缩页眉不会挪动这个数字。

    全页墨迹 11600bp²（2.31%），只算正文是 4000bp²（0.80%）：整页口径不报、
    正文口径报。页眉词框排得再长也压不动这个数。
    """
    xml = (
        '<page width="595.280000" height="841.890000">'
        '<word xMin="72.000000" yMin="40.000000" xMax="572.000000" yMax="55.000000">页眉标记很长</word>'
        '<word xMin="292.000000" yMin="792.000000" xMax="302.000000" yMax="802.000000">1</word>'
        '<word xMin="72.000000" yMin="400.000000" xMax="472.000000" yMax="410.000000">正文一行</word>'
        "</page>"
    )
    pages = checks.parse_bbox(xml)

    assert checks.page_near_blank(pages) == []
    found = checks.page_near_blank(pages, GEOMETRY)
    assert [item.code for item in found] == ["page-near-blank"]
    assert "1 word box(es)" in found[0].message
    assert "0.80% of the page covered" in found[0].message


def test_a_page_with_title_and_one_line_is_reported_at_two_percent():
    """附录目录那类"标题加一行字"的页：1.44% 覆盖——旧阈值 1% 漏掉，新阈值 2% 报出。"""
    xml = (
        '<page width="595.280000" height="841.890000">'
        '<word xMin="72.000000" yMin="152.000000" xMax="160.000000" yMax="174.000000">附录目录</word>'
        '<word xMin="72.000000" yMin="200.000000" xMax="520.000000" yMax="212.000000">附录 A 科产融合专题</word>'
        "</page>"
    )
    pages = checks.parse_bbox(xml)

    found = checks.page_near_blank(pages, GEOMETRY)

    assert [item.code for item in found] == ["page-near-blank"]
    assert found[0].severity == "info"
    assert checks.NEAR_BLANK_COVERAGE == 0.02


def test_page_near_blank_skips_recorded_float_pages():
    """载有浮动图表的页即使正文墨迹稀薄也不报——满页大图没有词框不代表页面空白。"""
    pages = _page(("72.000000", "400.000000", "100.000000", "410.000000", "图 1"))

    assert [item.code for item in checks.page_near_blank(pages)] == ["page-near-blank"]
    assert checks.page_near_blank(pages, None, {1}) == []
    assert [item.code for item in checks.page_near_blank(pages, None, set())] == ["page-near-blank"]


def test_float_page_numbers_read_only_arabic_caption_pages():
    """从 ``.lof``／``.lot`` 的 ``\\contentsline`` 取题注页；非数字页码忽略；缺文件返回空。"""
    lof = (
        "\\contentsline {figure}{\\numberline {A-1}{图}}{182}{figure.caption.106}%\n"
        "\\contentsline {figure}{\\numberline {i}{前图}}{xii}{figure.caption.1}%\n"
    )

    assert checks._float_page_numbers(lof, None) == {182}
    assert checks._float_page_numbers(None, None) == set()
    assert checks._float_page_numbers("", "") == set()


# ---------------------------------------------------------------- 表格孤字行


def _table_page(rows):
    """把 ``[(y, [(x, text), ...]), ...]`` 的合成表行写成一份单页 bbox。"""
    body = []
    for top, cells in rows:
        for left, text in cells:
            body.append(
                f'<word xMin="{left:.1f}" yMin="{top:.1f}" '
                f'xMax="{left + 60:.1f}" yMax="{top + 12:.1f}">{text}</word>'
            )
    return checks.parse_bbox(f'<page width="595.280000" height="841.890000">{"".join(body)}</page>')


def test_a_table_cell_wrapping_to_one_character_is_flagged():
    """三列表行的中列断行后只剩一个"子"字——这就是 table-orphan-line。"""
    pages = _table_page(
        [
            (100, [(72, "甲乙丙"), (200, "丁戊己"), (400, "庚辛壬")]),
            (120, [(72, "癸卯"), (200, "子"), (400, "丑寅")]),
            (140, [(72, "寅卯"), (200, "辰巳"), (400, "午未")]),
        ]
    )

    findings = checks.table_orphan_lines(pages, GEOMETRY)

    assert [item.code for item in findings] == ["table-orphan-line"]
    assert findings[0].severity == "info"
    assert findings[0].location == "page 1"
    # message 既要有孤字，也要有同一行其余各段的文字
    assert "'子'" in findings[0].message
    assert "癸卯" in findings[0].message and "丑寅" in findings[0].message


def test_a_table_row_is_recognised_by_at_least_three_segments():
    """中列只剩一个字、其余各列已经排完（整行只有两段）：落在表列上仍算表行。"""
    pages = _table_page(
        [
            (100, [(72, "甲乙丙"), (200, "丁戊己"), (400, "庚辛壬")]),
            (140, [(72, "寅卯"), (200, "辰巳"), (400, "午未")]),
            (160, [(72, "癸卯"), (200, "子")]),
        ]
    )

    findings = checks.table_orphan_lines(pages, GEOMETRY)

    assert [item.location for item in findings] == ["page 1"]
    assert "'子'" in findings[0].message


def test_a_prose_line_is_not_a_table_row():
    """正文各行是一段；哪怕最后一行只剩一个字，也不能算表格孤字行。"""
    pages = _table_page([(100, [(72, "这是一行普通正文，没有任何表格结构")]), (120, [(72, "尾")])])

    assert checks.table_orphan_lines(pages, GEOMETRY) == []


def test_a_row_start_cell_holding_one_character_is_not_an_orphan():
    """序号列带纯数字的行是这一行的首行；首行上一个"甲"字是整格内容，不是断行尾巴。"""
    pages = _table_page(
        [
            (100, [(72, "序号"), (200, "名称"), (400, "备注")]),
            (120, [(72, "1"), (200, "甲"), (400, "乙丙")]),
            (140, [(72, "2"), (200, "丁戊"), (400, "己庚")]),
        ]
    )

    assert checks.table_orphan_lines(pages, GEOMETRY) == []


def test_the_orphan_list_is_capped_with_a_summary(monkeypatch):
    monkeypatch.setattr(checks, "MAX_TABLE_ORPHAN_LOCATIONS", 1)
    pages = _table_page(
        [
            (100, [(72, "甲乙丙"), (200, "丁戊己"), (400, "庚辛壬")]),
            (120, [(72, "子"), (200, "丑"), (400, "寅")]),
            (160, [(72, "卯"), (200, "辰"), (400, "巳")]),
        ]
    )

    findings = checks.table_orphan_lines(pages, GEOMETRY)

    assert len(findings) == 2
    assert findings[0].location == "page 1"
    assert findings[-1].location == ""
    assert "2 table lines end with a single CJK character" in findings[-1].message
    assert "per page: 1×2" in findings[-1].message


# ---------------------------------------------------------------- 标题孤字行


def test_a_heading_that_wraps_to_one_character_is_flagged():
    """第 57 页那一例：``第七章 / 三十年回顾：代际、成就与判定性发`` 之后吊一个"现"。"""
    pages = _page(
        ("72.000000", "152.940000", "161.500000", "174.940000", "第七章"),
        ("161.500000", "152.940000", "523.260000", "174.940000", "三十年回顾：代际、成就与判定性发"),
        ("72.000000", "185.940000", "94.000000", "207.940000", "现"),
        ("72.000000", "300.000000", "523.260000", "313.950000", "正文一行。"),
    )

    findings = checks.heading_orphan_line(pages, Typography(), GEOMETRY)

    assert [item.code for item in findings] == ["heading-orphan-line"]
    assert findings[0].severity == "info"
    assert findings[0].location == "page 1"
    assert "'现'" in findings[0].message
    assert "三十年回顾" in findings[0].message


def test_a_heading_whose_last_line_has_two_characters_is_left_alone():
    pages = _page(
        ("72.000000", "152.940000", "523.260000", "174.940000", "第四章小天体：行星系统的旅行者与家园的"),
        ("72.000000", "185.940000", "116.000000", "207.940000", "风险"),
    )

    assert checks.heading_orphan_line(pages, Typography(), GEOMETRY) == []


def test_a_body_line_holding_one_character_is_not_a_heading():
    """正文的字高（13.95bp）不过阈值；哪怕它末尾也吊着一个字。"""
    pages = _page(
        ("72.000000", "152.940000", "523.260000", "166.890000", "这是一行普通正文，末尾不能只有一个字。"),
        ("72.000000", "174.740000", "86.700000", "188.690000", "字"),
    )

    assert checks.heading_orphan_line(pages, Typography(), GEOMETRY) == []


def test_a_subsection_heading_is_out_of_reach():
    """小节标题与正文同为 14pt（词框 13.95bp），本检查分辨不出——这是它的盲区。"""
    pages = _page(
        ("72.000000", "152.940000", "523.260000", "166.890000", "3.1.1　三种驱动机制的制度形态甲"),
        ("72.000000", "174.740000", "86.700000", "188.690000", "乙"),
    )

    assert checks.heading_orphan_line(pages, Typography(), GEOMETRY) == []


def test_a_centred_heading_wrapping_to_one_character_is_not_matched():
    """封面、篇题页是居中的：续行不与上一行同左缘，这条检查刻意不认。"""
    pages = _page(
        ("200.000000", "152.940000", "400.000000", "174.940000", "行星探测三十年文献综述"),
        ("290.000000", "185.940000", "312.000000", "207.940000", "述"),
    )

    assert checks.heading_orphan_line(pages, Typography(), GEOMETRY) == []


def test_the_heading_height_threshold_follows_the_manifest_font_size():
    """阈值是正文 em 的 1.1 倍，不是写死的 15.35bp——正文字号变了它跟着变。"""
    pages = _page(
        ("72.000000", "152.940000", "523.260000", "166.890000", "正文一行结尾"),
        ("72.000000", "174.740000", "86.700000", "188.690000", "字"),
    )

    # 五号（10.5pt）时正文 em ≈ 10.46bp，阈值 ≈ 11.5bp：13.95bp 的"字号"于是过线
    findings = checks.heading_orphan_line(pages, Typography(body_font_size="5"), GEOMETRY)

    assert [item.code for item in findings] == ["heading-orphan-line"]


# ---------------------------------------------------------------- 插图分辨率


def _png(width, height):
    """只有 IHDR 的最小 PNG 头——读尺寸只需要前 24 字节。"""
    return (
        b"\x89PNG\r\n\x1a\n"
        + (13).to_bytes(4, "big")
        + b"IHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + b"\x08\x06\x00\x00\x00"
    )


def _jpeg(width, height):
    sof = (
        b"\xff\xc0"
        + (17).to_bytes(2, "big")
        + b"\x08"
        + height.to_bytes(2, "big")
        + width.to_bytes(2, "big")
        + b"\x03\x01\x11\x00\x02\x11\x01\x03\x11\x01"
    )
    return b"\xff\xd8" + sof + b"\xff\xd9"


def test_image_pixel_size_reads_png_and_jpeg(tmp_path):
    png = tmp_path / "one.png"
    png.write_bytes(_png(1204, 720))
    jpeg = tmp_path / "one.jpg"
    jpeg.write_bytes(_jpeg(448, 438))
    other = tmp_path / "one.pdf"
    other.write_bytes(b"%PDF-1.4\n")

    assert checks.image_pixel_size(png) == (1204, 720)
    assert checks.image_pixel_size(jpeg) == (448, 438)
    assert checks.image_pixel_size(other) is None
    assert checks.image_pixel_size(png.parent / "missing.png") is None


def test_a_real_one_pixel_png_is_read(tmp_path):
    path = tmp_path / "tiny.png"
    path.write_bytes(ONE_PIXEL_PNG)

    assert checks.image_pixel_size(path) == (1, 1)


def test_printed_width_honours_keepaspectratio():
    box_width, box_height = 0.96 * 451.28, 0.68 * 697.89

    # 高瘦图：高度顶住，宽度按比例缩
    assert checks.printed_width_bp(640, 1200, box_width, box_height) == pytest.approx(
        box_height * 640 / 1200
    )
    # 宽图：宽度顶住
    assert checks.printed_width_bp(640, 393, box_width, box_height) == pytest.approx(box_width)
    # 不保持比例：宽度拉满
    assert checks.printed_width_bp(640, 1200, box_width, box_height, keep_aspect=False) == pytest.approx(
        box_width
    )


def test_figure_dpi_matches_the_hand_computed_value():
    box_width, box_height = 0.96 * 451.28, 0.68 * 697.89
    printed = checks.printed_width_bp(1204, 720, box_width, box_height)

    assert printed == pytest.approx(box_width)  # 1204:720 比版心框宽得多，宽度顶住
    assert checks.figure_dpi(1204, printed) == pytest.approx(1204 * 72 / 433.23, abs=0.1)


def test_parse_includegraphics_reads_the_box_from_the_options():
    text = f"\\includegraphics{FIGURE_OPTIONS}{{图/科学篇/fig01.png}}\n"

    figures = checks.parse_includegraphics(text)

    assert len(figures) == 1
    assert figures[0].path == "图/科学篇/fig01.png"
    assert figures[0].width_fraction == pytest.approx(0.96)
    assert figures[0].height_fraction == pytest.approx(0.68)
    assert figures[0].keep_aspect is True


def test_a_missing_figure_is_an_error(tmp_path):
    text = f"\\includegraphics{FIGURE_OPTIONS}{{图/nowhere.png}}\n"

    findings = checks.figure_resolution(text, tmp_path, checks.parse_geometry(LOG_GEOMETRY))

    assert [item.code for item in findings] == ["figure-file-missing"]
    assert findings[0].severity == "error"


def test_low_resolution_figures_are_reported_with_their_dpi(tmp_path):
    (tmp_path / "图").mkdir()
    (tmp_path / "图" / "small.png").write_bytes(_png(400, 300))
    (tmp_path / "图" / "medium.jpg").write_bytes(_jpeg(640, 393))
    (tmp_path / "图" / "fine.png").write_bytes(_png(1204, 720))
    text = (
        f"\\includegraphics{FIGURE_OPTIONS}{{图/small.png}}\n"
        f"\\includegraphics{FIGURE_OPTIONS}{{图/medium.jpg}}\n"
        f"\\includegraphics{FIGURE_OPTIONS}{{图/fine.png}}\n"
    )

    findings = checks.figure_resolution(text, tmp_path, checks.parse_geometry(LOG_GEOMETRY))

    assert [item.location for item in findings] == ["图/small.png", "图/medium.jpg"]
    assert findings[0].severity == "warning"  # 400px / 433bp ≈ 66dpi
    assert findings[1].severity == "info"  # 640px / 433bp ≈ 106dpi
    assert "66 dpi" in findings[0].message


def test_figure_resolution_without_geometry_only_checks_existence(tmp_path):
    (tmp_path / "图").mkdir()
    (tmp_path / "图" / "small.png").write_bytes(_png(400, 300))
    text = f"\\includegraphics{FIGURE_OPTIONS}{{图/small.png}}\n"

    assert checks.figure_resolution(text, tmp_path, None) == []


# ---------------------------------------------------------------- 命令行


def test_cli_deep_runs_the_text_level_checks(tmp_path, capsys):
    path = tiny_book(tmp_path)
    main(["build", str(path), "--tex-only"])
    # 分辨率要用版心几何量算，所以得有一份带 geometry 报告的日志
    (tmp_path / "_primer" / "book" / "tiny.log").write_text(LOG_GEOMETRY, encoding="utf-8")

    assert main(["check", str(path), "--deep"]) == 0

    out = capsys.readouterr().out
    # 微型书里的两张插图都是 1×1 像素，分辨率必然过低
    assert "figure-low-resolution" in out
    assert "pdf-missing" in out


def test_cli_plain_check_is_unchanged(tmp_path, capsys):
    path = tiny_book(tmp_path)
    main(["build", str(path), "--tex-only"])

    assert main(["check", str(path)]) == 0

    out = capsys.readouterr().out
    assert "missing-log" in out
    assert "figure-low-resolution" not in out
    assert "pdftotext" not in out


def test_the_page_checks_degrade_when_pdftotext_is_missing(tmp_path, monkeypatch):
    path = tiny_book(tmp_path)
    manifest = load_manifest(path)
    builder = BookBuilder(manifest, tex_only=True)
    builder.build()
    builder.plan.toc.write_text("", encoding="utf-8")
    builder.plan.log.write_text(LOG_GEOMETRY, encoding="utf-8")
    builder.plan.pdf.write_bytes(b"%PDF-1.4\n")
    monkeypatch.setattr(checks.shutil, "which", lambda name: None)

    findings = checks.run_checks(manifest, builder.plan)

    unavailable = [item for item in findings if item.code == "pdftotext-unavailable"]
    assert len(unavailable) == 1
    assert unavailable[0].severity == "info"
    assert "pdftotext" in unavailable[0].message
    assert "text-out-of-block" not in _codes(findings)


# ---------------------------------------------------------------- 构建新鲜度


def _make_newer(path, reference, seconds=10.0):
    """把 ``path`` 的 mtime 挪到 ``reference`` 之后（不碰内容）。"""
    stamp = reference.stat().st_mtime + seconds
    os.utime(path, (stamp, stamp))


def test_stale_build_fires_when_a_source_is_newer_than_the_tex(tmp_path):
    """改了稿没重编：报告描述的是上一版书，唯一该做的是先 build。"""
    _, builder = _deep(tmp_path)
    source = builder.manifest.source_root / "one.md"
    _make_newer(source, builder.plan.tex)

    findings = checks.stale_build(builder.manifest, builder.plan)

    assert [item.code for item in findings] == ["stale-build"]
    assert findings[0].severity == "warning"
    assert findings[0].location == "sources/one.md"
    assert "newer than the emitted .tex" in findings[0].message
    assert "rebuild before trusting this report" in findings[0].message
    # 两个时刻都在消息里：源文件自己的与产物 .tex 的
    assert checks.format_mtime(source) in findings[0].message
    assert checks.format_mtime(builder.plan.tex) in findings[0].message


def test_stale_build_reports_every_newer_source(tmp_path):
    """两个源文件都比 .tex 新就报两条，各自带自己的相对路径。"""
    _, builder = _deep(tmp_path)
    _make_newer(builder.manifest.source_root / "one.md", builder.plan.tex)
    _make_newer(builder.manifest.bibliography, builder.plan.tex)

    findings = checks.stale_build(builder.manifest, builder.plan)

    assert sorted(item.location for item in findings) == ["sources/one.md", "sources/refs.md"]
    assert {item.severity for item in findings} == {"warning"}


def test_stale_build_is_silent_when_every_source_is_older(tmp_path):
    """刚 build 完，源文件都旧于 .tex——当前树上必须一条都不报。"""
    _, builder = _deep(tmp_path)

    assert checks.stale_build(builder.manifest, builder.plan) == []


def test_stale_build_says_nothing_without_a_tex(tmp_path):
    """产物不在是 tex-missing 的事，新鲜度不越俎代庖。"""
    _, builder = _deep(tmp_path)
    builder.plan.tex.unlink()

    assert checks.stale_build(builder.manifest, builder.plan) == []


def test_format_mtime_reads_a_time_and_survives_a_missing_file(tmp_path):
    path = tmp_path / "a.md"
    path.write_text("x", encoding="utf-8")

    assert len(checks.format_mtime(path)) == 19  # YYYY-MM-DD HH:MM:SS
    assert checks.format_mtime(tmp_path / "nope.md") == "?"


def test_deep_checks_report_stale_build_first(tmp_path):
    """run_checks 把新鲜度排在第一位：后面每条都在描述 .tex 代表的那一版书。"""
    _, builder = _deep(tmp_path)
    _make_newer(builder.manifest.source_root / "two.md", builder.plan.tex)

    findings = checks.run_checks(builder.manifest, builder.plan)

    assert _codes(findings)[0] == "stale-build"


def test_plain_check_does_not_mention_a_stale_build(tmp_path, capsys):
    """``stale-build`` 只在 --deep 里：普通 check 的输出保持逐字不变。"""
    path = tiny_book(tmp_path)
    main(["build", str(path), "--tex-only"])
    _make_newer(tmp_path / "sources" / "one.md", tmp_path / "_primer" / "book" / "tiny.tex")

    assert main(["check", str(path)]) == 0

    assert "stale-build" not in capsys.readouterr().out

