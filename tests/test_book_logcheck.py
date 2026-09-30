# -*- coding: utf-8 -*-
"""日志解析与发现分级：盒子溢出、缺字、未定义引用、找不到文件。"""

from primer.book import logcheck
from primer.book.findings import Finding, fatal, is_fatal, summarize

LOG = """\
(./tiny.aux)
[1] [2]
Overfull \\hbox (12.34567pt too wide) in paragraph at lines 45--47
[]\\TU/SongtiSC(0)/m/n/14 一段很长的正文
[3]
Missing character: There is no ᰀ in font SongtiSC!
[4]
LaTeX Warning: Reference `fig:1-1' on page 5 undefined on input line 88.
LaTeX Warning: Citation `7' on page 5 undefined.
LaTeX Warning: File `图/missing.png' not found on input line 12.
LaTeX Font Warning: Font shape `OMS/TimesNewRoman(0)/m/n' undefined
(Font)              using `OMS/cmsy/m/n' instead on input line 568.
LaTeX Warning: Label(s) may have changed. Rerun to get cross-references right.
"""


def codes(findings):
    return [finding.code for finding in findings]


def test_parse_log_reports_every_signal():
    findings = logcheck.parse_log(LOG)
    found = codes(findings)

    assert "overfull-hbox" in found
    assert "missing-character" in found
    assert "undefined-font-shape" in found
    assert "undefined-reference" in found
    assert "undefined-citation" in found
    assert "missing-file" in found
    assert "rerun-required" in found


def test_undefined_font_shape_names_the_shape_and_the_substitute():
    finding = next(
        item for item in logcheck.parse_log(LOG) if item.code == "undefined-font-shape"
    )

    assert finding.severity == "warning"
    assert "OMS/TimesNewRoman(0)/m/n" in finding.message
    assert "OMS/cmsy/m/n" in finding.message
    # 字仍在，引擎换了字体顶上：排版细节，不是内容错误，不该让 check 失败
    assert not is_fatal(finding)


def test_undefined_font_shape_without_a_substitute_line_still_reports_the_shape():
    text = "LaTeX Font Warning: Font shape `OT1/Unknown(0)/m/n' undefined\n"

    finding = next(item for item in logcheck.parse_log(text) if item.code == "undefined-font-shape")

    assert "OT1/Unknown(0)/m/n" in finding.message
    assert not is_fatal(finding)


def test_overfull_box_carries_the_page_and_the_source_lines():
    finding = next(item for item in logcheck.parse_log(LOG) if item.code == "overfull-hbox")

    assert finding.severity == "warning"
    assert "page 2" in finding.location
    assert "45--47" in finding.location
    assert "12.34567pt" in finding.message


def test_missing_character_is_an_error():
    finding = next(item for item in logcheck.parse_log(LOG) if item.code == "missing-character")

    assert finding.severity == "error"
    assert is_fatal(finding)


# beamer 的日志：一帧一页，帧内溢出在 ``\end{frame}`` 装箱时报出来，而 ``[n]`` 由随后的
# ``\shipout`` 打出——消息因此写在它所属那一页的标记**之前**。这一段逐字取自一个真编译过
# 的四帧 xelatex 文档（第二帧里放了一根 400pt 的竖线）。
BEAMER_LOG = """\
[1

]
Overfull \\vbox (160.43008pt too high) detected at line 9
 []

[2

]

[3

]
"""


def test_a_beamer_frame_overfull_belongs_to_the_page_after_the_marker():
    """日志行号 9 在一帧即一页的 beamer 里是第 2 页，不是消息之前那个 [1] 的第 1 页。"""
    finding = next(
        item
        for item in logcheck.parse_log(BEAMER_LOG, pages=logcheck.PAGE_AFTER)
        if item.code == "overfull-vbox"
    )

    assert "page 2" in finding.location
    assert "line(s) 9" in finding.location
    assert finding.severity == "warning"


def test_the_default_page_attribution_keeps_the_book_convention():
    """成书连续正文沿用至今的口径：取消息之前最近的那个标记。"""
    finding = next(
        item for item in logcheck.parse_log(BEAMER_LOG) if item.code == "overfull-vbox"
    )

    assert "page 1" in finding.location


def test_parse_log_rejects_an_unknown_page_attribution():
    try:
        logcheck.parse_log("", pages="sideways")
    except ValueError as error:
        assert "sideways" in str(error)
    else:  # pragma: no cover - 正常路径下不会走到
        raise AssertionError("expected ValueError")


def test_unwrap_joins_lines_broken_at_the_print_width():
    first = "Overfull \\hbox (1.0pt too wide) in paragraph at lines " + "1--2"
    wrapped = first + " " * (79 - len(first) % 79)
    log = wrapped + "\n" + "x" * 10 + "\n"

    joined = logcheck.unwrap(log)

    assert joined.startswith(first)
    assert "x" * 10 in joined
    assert "\n" not in joined


def test_overfull_is_a_warning_while_a_dangling_reference_is_fatal():
    text = "Overfull \\hbox (1.0pt too wide) in paragraph at lines 1--1"
    text += "\nLaTeX Warning: Reference `x' on page 1 undefined on input line 2.\n"

    findings = logcheck.parse_log(text)

    assert not is_fatal(next(item for item in findings if item.code == "overfull-hbox"))
    assert is_fatal(next(item for item in findings if item.code == "undefined-reference"))
    assert len(fatal(findings)) == 1


def test_summarize_counts_by_code_in_first_seen_order():
    findings = [
        Finding(code="b", message="second", severity="warning"),
        Finding(code="a", message="first", severity="info"),
        Finding(code="b", message="second again", severity="warning"),
    ]

    rows = summarize(findings)

    assert rows == [
        {"code": "b", "severity": "warning", "count": 2},
        {"code": "a", "severity": "info", "count": 1},
    ]


def test_finding_rejects_an_unknown_severity():
    try:
        Finding(code="x", message="m", severity="nope")
    except ValueError as error:
        assert "nope" in str(error)
    else:  # pragma: no cover - 正常路径下不会走到
        raise AssertionError("expected ValueError")
