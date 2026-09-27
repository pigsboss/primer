# -*- coding: utf-8 -*-
"""引号规整：配对、跳过受保护区段、未配对原样保留、源文件与撇号不动。"""

from book_fixtures import MANIFEST, ONE_MD, tiny_book

from primer.book import BookBuilder, load_manifest
from primer.book import quotes
from primer.book.__main__ import main


# ---------------------------------------------------------------- 纯函数


def test_pairs_straight_quotes_inside_a_paragraph():
    outcome = quotes.normalize_double_quotes('从"描述行星"到"理解过程"')

    assert outcome.text == "从“描述行星”到“理解过程”"
    assert (outcome.pairs, outcome.unpaired) == (2, 0)


def test_pairs_a_link_label_but_not_the_target_or_its_title():
    outcome = quotes.normalize_double_quotes('[他说"好"](https://x.test/a "标题")')

    assert outcome.text == '[他说“好”](https://x.test/a "标题")'
    assert outcome.pairs == 1


def test_link_target_with_parentheses_and_a_title_is_fully_protected():
    source = '[报告](/Users/x/报告 (1).pdf "citation")'

    assert quotes.normalize_double_quotes(source).text == source


def test_skips_inline_code_math_and_bare_urls():
    text = '正文 "说明"，`a "b" c`，公式 $x"y$，网址 https://example.org/p 结束'

    outcome = quotes.normalize_double_quotes(text)

    assert outcome.text == '正文 “说明”，`a "b" c`，公式 $x"y$，网址 https://example.org/p 结束'
    assert outcome.pairs == 1


def test_fenced_code_block_is_skipped_by_the_line_pass():
    tally = quotes.QuoteTally()
    lines = ['正文 "甲"', "```", 'code "raw"', "```", '正文 "乙"']

    result = quotes.normalize_lines(lines, tally, "V1")

    assert result == ["正文 “甲”", "```", 'code "raw"', "```", "正文 “乙”"]
    assert tally.pairs == 2


def test_odd_number_of_quotes_leaves_the_line_and_is_reported():
    tally = quotes.QuoteTally()

    result = quotes.normalize_lines(['只有"一个', '正常"一对"'], tally, "V1")

    assert result == ['只有"一个', "正常“一对”"]
    assert tally.pairs == 1
    assert tally.unpaired == [("V1:1", 1)]


def test_ascii_apostrophes_are_never_touched():
    outcome = quotes.normalize_double_quotes("D'Abramo's 'quoted' note")

    assert outcome.text == "D'Abramo's 'quoted' note"
    assert (outcome.pairs, outcome.unpaired) == (0, 0)


def test_disabled_tally_is_a_no_op():
    tally = quotes.QuoteTally(enabled=False)
    lines = ['正文 "甲"', "```", 'code "raw"', "```"]

    assert quotes.normalize_lines(lines, tally, "V1") == lines
    assert (tally.pairs, tally.unpaired) == (0, [])


# ---------------------------------------------------------------- 装配集成


def test_build_pairs_prose_quotes_in_the_tex_and_reports_the_count(tmp_path):
    source = ONE_MD.replace("正文一段，", '正文一段说"行星"与"世界"，')
    manifest = load_manifest(tiny_book(tmp_path, one_md=source))

    result = BookBuilder(manifest, tex_only=True).build()

    text = result.tex.read_text(encoding="utf-8")
    assert "说“行星”与“世界”" in text
    assert '"行星"' not in text
    summary = [finding for finding in result.findings if finding.code == "quotes-normalized"]
    assert len(summary) == 1 and summary[0].severity == "info"


def test_fenced_code_keeps_its_straight_quotes(tmp_path):
    source = ONE_MD.replace("verbatim 行", 'verbatim "行"')
    manifest = load_manifest(tiny_book(tmp_path, one_md=source))

    result = BookBuilder(manifest, tex_only=True).build()

    assert '"行"' in result.tex.read_text(encoding="utf-8")


def test_unpaired_quotes_are_left_alone_and_reported(tmp_path):
    source = ONE_MD.replace("正文一段，", '正文一段说"行星，')
    manifest = load_manifest(tiny_book(tmp_path, one_md=source))

    result = BookBuilder(manifest, tex_only=True).build()

    assert '"行星' in result.tex.read_text(encoding="utf-8")
    unpaired = [finding for finding in result.findings if finding.code == "quotes-unpaired"]
    assert len(unpaired) == 1
    assert unpaired[0].severity == "warning"
    assert unpaired[0].location.startswith("V1 one.md:")


def test_manifest_switch_disables_normalization(tmp_path):
    text = MANIFEST.replace('body_font_size: "4"', 'body_font_size: "4"\n  normalize_quotes: false')
    source = ONE_MD.replace("正文一段，", '正文一段说"行星"，')
    manifest = load_manifest(tiny_book(tmp_path, manifest_text=text, one_md=source))

    result = BookBuilder(manifest, tex_only=True).build()

    assert '"行星"' in result.tex.read_text(encoding="utf-8")
    assert not [finding for finding in result.findings if finding.code == "quotes-normalized"]


def test_cli_no_normalize_quotes_leaves_ascii(tmp_path):
    source = ONE_MD.replace("正文一段，", '正文一段说"行星"，')
    path = tiny_book(tmp_path, one_md=source)

    assert main(["build", str(path), "--tex-only", "--no-normalize-quotes"]) == 0

    text = (tmp_path / "_primer" / "book" / "tiny.tex").read_text(encoding="utf-8")
    assert '"行星"' in text
