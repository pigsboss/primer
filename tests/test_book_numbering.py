# -*- coding: utf-8 -*-
"""编号方案与文献引用：标题重写、引用区间归一化、册内编号压缩。"""

import re

from primer.book import numbering
from primer.book.manifest import HeadingRule, HeadingScheme, load_manifest

from book_fixtures import tiny_book


def plain_scheme(rules, replacements=()):
    return HeadingScheme(name="test", rules=rules, replacements=replacements)


# ---------------------------------------------------------------- 章节方案


def test_heading_rule_applies_number_offset_and_keeps_title():
    scheme = plain_scheme(
        (
            HeadingRule(
                pattern=re.compile(r"^## 2\.(\d+)\s*(.*)"),
                template="## 第 {g1} 章　{g2}",
                numbers=("g1",),
                offsets={"g1": 5},
            ),
        )
    )

    assert numbering.apply_scheme(["## 2.3　三十年回顾", "正文"], scheme) == [
        "## 第 8 章　三十年回顾",
        "正文",
    ]


def test_scheme_replacements_only_touch_lines_without_heading_rule():
    scheme = plain_scheme(
        (
            HeadingRule(
                pattern=re.compile(r"^## 1\.(\d+)\s*(.*)"),
                template="## 第 {g1} 章　{g2}",
                numbers=("g1",),
            ),
        ),
        replacements=(("1.2 节", "第 2 章"),),
    )

    assert numbering.apply_scheme(["## 1.2　见 1.2 节", "见 1.2 节"], scheme) == [
        "## 第 2 章　见 1.2 节",
        "见 第 2 章",
    ]


def test_scheme_from_manifest_condenses_the_source_headings(tmp_path):
    manifest = load_manifest(tiny_book(tmp_path))
    lines = ["## 1.2　导言", "### 1.2.3　小节", "1.2 节（交叉引用）"]

    assert numbering.apply_scheme(lines, manifest.scheme("plain")) == [
        "## 第 2 章　导言",
        "### 2.3　小节",
        "第 2 章（交叉引用）",
    ]


# ---------------------------------------------------------------- 引用


def test_split_range_notation_is_normalized():
    assert numbering.normalize_cite_ranges("见 [3]–[7] 与 [9]—[11]。") == "见 [3–7] 与 [9–11]。"


def test_collect_citations_expands_ranges_in_first_seen_order():
    order = numbering.collect_citations("先 [3]，再 [1][2]，区间 [5–7]，重复 [1]，降序 [9–8]")

    assert order == [3, 1, 2, 5, 6, 7, 8, 9]


def test_renumber_citations_compresses_contiguous_ranges():
    order = [10, 11, 12, 20, 30, 31]

    assert numbering.renumber_citations("[10–12] 与 [20] 与 [30][31]", order) == "[1–3] 与 [4] 与 [5][6]"
    assert numbering.renumber_citations("[20][30] 单条 [10]", [10, 20, 30]) == "[2][3] 单条 [1]"


def test_renumber_citations_keeps_single_and_pair_ranges_explicit():
    assert numbering.renumber_citations("[10]", [10]) == "[1]"
    assert numbering.renumber_citations("[10][11]", [10, 11]) == "[1][2]"
    assert numbering.renumber_citations("[10–11]", [10, 11]) == "[1][2]"
    assert numbering.renumber_citations("[10][12]", [10, 11, 12]) == "[1][3]"


def test_renumber_citations_keeps_unmapped_references_as_is():
    assert numbering.renumber_citations("见 [99] 与 [10–11]", [10, 11]) == "见 [99] 与 [1][2]"
