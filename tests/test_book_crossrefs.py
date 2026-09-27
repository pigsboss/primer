# -*- coding: utf-8 -*-
"""交叉引用：标签收集、正文 ``\\ref`` 改写、悬空引用与未引用题注。"""

from primer.book import crossrefs, markdown as md


def labels_of(source):
    return crossrefs.collect_labels(md.parse_blocks(source))


def codes(findings):
    return [finding.code for finding in findings]


def test_collects_labels_in_separate_figure_and_table_namespaces():
    labels, findings = labels_of(
        [
            "![图 2-1](a.png)",
            "*图 2-1　插图*",
            "**表 2-1　表格**",
            "",
            "| a |",
            "|---|",
        ]
    )

    assert labels == {"fig:2-1": "figure", "tab:2-1": "table"}
    assert findings == []


def test_duplicate_labels_are_reported():
    labels, findings = labels_of(
        ["![图 2-1](a.png)", "*图 2-1　一*", "![图 2-1](b.png)", "*图 2-1　二*"]
    )

    assert labels == {"fig:2-1": "figure"}
    assert codes(findings) == ["duplicate-label"]
    assert findings[0].severity == "error"


def test_rewrites_figure_and_table_references_and_keeps_the_words():
    labels = {"fig:2-13": "figure", "tab:A-1": "table"}

    lines, referenced, findings = crossrefs.rewrite_references(
        ["如图 2-13 所示，见表 A-1。"], labels
    )

    assert lines == [r"如图~\ref{fig:2-13} 所示，见表~\ref{tab:A-1}。"]
    assert referenced == {"fig:2-13", "tab:A-1"}
    assert findings == []


def test_dangling_reference_is_left_alone_and_reported():
    lines, referenced, findings = crossrefs.rewrite_references(["见图 9-9。"], {})

    assert lines == ["见图 9-9。"]
    assert referenced == set()
    assert codes(findings) == ["dangling-figure-reference"]
    assert findings[0].severity == "error"
    assert findings[0].location == "line 1"


def test_range_reference_is_not_rewritten_and_is_reported():
    labels = {"fig:1-1": "figure"}

    lines, _, findings = crossrefs.rewrite_references(["见图 1-1～1-17。"], labels, "V1")

    assert lines == ["见图 1-1～1-17。"]
    assert codes(findings) == ["figure-range-reference"]


def test_caption_lines_image_lines_and_code_fences_are_untouched():
    labels = {"fig:1-1": "figure"}
    source = [
        "![图 1-1](a.png)",
        "*图 1-1　见 图 1-1*",
        "```",
        "见图 1-1",
        "```",
        "正文见图 1-1。",
    ]

    lines, _, _ = crossrefs.rewrite_references(source, labels)

    assert lines[0] == "![图 1-1](a.png)"
    assert lines[1] == "*图 1-1　见 图 1-1*"
    assert lines[3] == "见图 1-1"
    assert lines[5] == r"正文见图~\ref{fig:1-1}。"


def test_unreferenced_captions_are_reported_once_per_label():
    labels = {"fig:1-1": "figure", "tab:2-1": "table"}

    findings = crossrefs.unreferenced(labels, {"fig:1-1"})

    assert codes(findings) == ["unreferenced-table"]
    assert findings[0].severity == "info"
    assert findings[0].location == "tab:2-1"
