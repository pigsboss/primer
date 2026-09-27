# -*- coding: utf-8 -*-
"""markdown 子集解析：块切分、题注配对、标题编号剥离、正文落盘。"""

from book_fixtures import ONE_MD, REFS_MD

from primer.book import markdown as md
from primer.book.references import Bibliography


def types_of(blocks):
    return [type(block).__name__ for block in blocks]


def test_parses_headings_paragraphs_and_rules():
    blocks = md.parse_blocks(
        ["## 第 1 章　导言", "", "一段正文。", "---", "# 一级标题被忽略", "#### 四级标题"]
    )

    assert types_of(blocks) == ["Heading", "Paragraph", "HorizontalRule", "Heading", "Heading"]
    assert blocks[0].level == 2 and blocks[0].text == "第 1 章　导言"
    assert blocks[1].text == "一段正文。"
    assert blocks[3].level == 1
    assert blocks[4].level == 4 and blocks[4].text == "四级标题"


def test_heading_commands_map_markdown_levels_to_book_structure():
    assert md.HEADING_COMMANDS == {2: "chapter", 3: "section", 4: "subsection", 5: "subsubsection"}


def test_strip_heading_number_covers_the_author_conventions():
    assert md.strip_heading_number("第 6 章　导言") == "导言"
    assert md.strip_heading_number("6.1　范围划定") == "范围划定"
    assert md.strip_heading_number("A.3.3.1 生产成熟期：可依托成熟产业基础的环节") == (
        "生产成熟期：可依托成熟产业基础的环节"
    )
    assert md.strip_heading_number("导言：战略研究的范围、对象与方法") == "导言：战略研究的范围、对象与方法"
    assert md.strip_heading_number("本附录主要资料来源") == "本附录主要资料来源"


def test_strip_heading_number_keeps_year_like_headings():
    assert md.strip_heading_number("2020 年代以后") == "2020 年代以后"


def test_strip_heading_number_keeps_a_heading_that_is_only_a_number():
    assert md.strip_heading_number("2.1") == "2.1"


def test_parses_figure_with_two_line_caption_and_label():
    blocks = md.parse_blocks(
        [
            "![图 1-1](图/one.png)",
            "",
            "*图 1-1　简题*",
            "",
            "*说明行*",
            "",
            "后续正文。",
        ]
    )

    assert types_of(blocks) == ["Figure", "Paragraph"]
    figure = blocks[0]
    assert figure.path == "图/one.png"
    assert figure.caption == "简题"
    assert figure.note == "说明行"
    assert figure.label_key == "1-1"
    assert figure.label == "fig:1-1"


def test_figure_without_caption_falls_back_to_the_alt_text():
    blocks = md.parse_blocks(["![图 1-1](图/one.png)", "", "*不是题注（缺全角空格）*"])

    assert types_of(blocks) == ["Figure", "Paragraph"]
    assert blocks[0].caption == "图 1-1" and blocks[0].label_key == "1-1"
    assert blocks[0].note == ""
    assert blocks[1].text == "*不是题注（缺全角空格）*"


def test_figure_note_must_not_be_another_label():
    blocks = md.parse_blocks(["![图 2-1](p.png)", "*图 2-1　简题*", "*图 2-2　另一题注*"])

    assert blocks[0].caption == "简题"
    assert blocks[0].note == ""


def test_table_takes_the_preceding_caption_and_its_label():
    blocks = md.parse_blocks(
        ["**表 1-1　示例表**", "", "| 列一 | 列二 |", "|---|---|", "| a | b |"]
    )

    assert types_of(blocks) == ["Table"]
    assert blocks[0].caption == "示例表"
    assert blocks[0].label_key == "1-1"
    assert blocks[0].label == "tab:1-1"
    assert blocks[0].rows == [["列一", "列二"], ["a", "b"]]


def test_table_without_caption_and_stray_caption_is_dropped():
    plain = md.parse_blocks(["| a |", "|---|"])
    assert plain == [md.Table(rows=[["a"]], caption="", label_key=None)]
    assert plain[0].label is None
    # 没有表格取用的题注行不进入成书（与构书脚本一致）
    assert md.parse_blocks(["**表 9-9　孤立题注**", "", "正文。"]) == [md.Paragraph("正文。")]


def test_lists_break_on_blank_lines_and_switch_environment():
    blocks = md.parse_blocks(["- 一", "- 二", "", "- 三", "1. 甲", "2. 乙"])

    assert [(type(b).__name__, b.ordered, b.items) for b in blocks] == [
        ("ListBlock", False, ["一", "二"]),
        ("ListBlock", False, ["三"]),
        ("ListBlock", True, ["甲", "乙"]),
    ]


def test_code_block_quote_and_part_banner():
    blocks = md.parse_blocks(["```", "raw \\ line", "```", "> 引用一行", "@@PART@@第一篇　科学篇"])

    assert types_of(blocks) == ["CodeBlock", "Quote", "PartBanner"]
    assert blocks[0].lines == ["raw \\ line"]
    assert blocks[1].text == "引用一行"
    assert blocks[2].title == "第一篇　科学篇"


def test_empty_lines_inside_code_block_are_kept():
    blocks = md.parse_blocks(["```", "a", "", "b", "```"])

    assert blocks[0].lines == ["a", "", "b"]


def test_real_document_parses_to_expected_block_sequence():
    blocks = md.parse_blocks(md.strip_preamble(ONE_MD.splitlines()))

    names = types_of(blocks)
    assert names[0] == "HorizontalRule"
    assert names.count("Heading") == 2
    assert names.count("Figure") == 1
    assert names.count("Table") == 1
    assert names.count("ListBlock") == 2
    assert names.count("CodeBlock") == 1
    assert names.count("Quote") == 1


def test_strip_preamble_drops_title_and_note_block():
    lines = ["# 标题", "", "> 说明一", "> 说明二", "", "---", "## 第一节"]

    assert md.strip_preamble(lines) == ["---", "## 第一节"]


def test_parts_to_headings_and_volume_document():
    text = "\n@@PART@@第一篇　科学篇\n正文\n"

    assert md.parts_to_headings(text) == "\n\n## 第一篇　科学篇\n\n正文\n"
    document = md.as_volume_document(text, "第一篇　科学篇")
    assert document.startswith("# 第一篇　科学篇")
    assert document.endswith("\n")


def test_local_bibliography_maps_numbers_and_marks_missing_entries():
    bibliography = Bibliography.from_text(REFS_MD)

    text = md.local_bibliography([12, 3], bibliography)

    assert text.startswith("## 本册参考文献")
    assert "[1] Twelfth entry.（全书编号 [12]）" in text
    assert "[2] Third entry.（全书编号 [3]）" in text
    assert md.local_bibliography([99], bibliography).count("条目缺失") == 1
