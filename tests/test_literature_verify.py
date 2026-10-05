# -*- coding: utf-8 -*-
"""verify 的单元测试：命中判定、年份护栏、失败隔离、字段回填与防全败上报。

全部用假查询（不发网络）；SourceData 用真实的 markdown 解析器构造。
"""

from primer.literature.importers import parse_source
from primer.literature.verify import verify_source


def _source():
    md = (
        "# 参考\n"
        "## A\n"
        "[1] Author A. Title One. Journal X, 2003.\n"
        "[2] 只有一段没有句点\n"
    ).encode("utf-8")
    return parse_source("refs.md", md)


def test_verify_fills_fields_on_confident_match():
    def fake_lookup(title):
        if "title one" in title.lower():
            return [{
                "title": "Title One", "authors": ["Author A"], "year": 2003,
                "venue": "Journal X", "type": "journal-article", "doi": "10.1000/xyz",
            }]
        return []

    source = _source()
    report = verify_source(source, [0, 1], fake_lookup)

    assert report.matched == 1 and report.unmatched == 1 and report.failed == 0
    assert source.rows[0]["DOI"] == "10.1000/xyz"
    assert source.rows[0]["出处"] == "Journal X"
    assert source.rows[0]["作者"] == "Author A"
    assert "类型" in source.columns
    assert source.suggested["类型"] == "type"


def test_verify_year_guard_rejects_mismatch():
    def fake_lookup(title):
        return [{
            "title": "Title One", "authors": [], "year": 1999,
            "venue": "V", "type": "journal-article", "doi": "10.1/x",
        }]

    source = _source()
    report = verify_source(source, [0], fake_lookup)

    assert report.matched == 0 and report.unmatched == 1
    assert source.rows[0]["DOI"] == ""


def test_verify_partial_failure_counts_without_raising():
    def flaky_lookup(title):
        if "title one" in title.lower():
            return []
        raise RuntimeError("network hiccup")

    source = _source()
    report = verify_source(source, [0, 1], flaky_lookup)

    assert report.matched == 0 and report.unmatched == 1 and report.failed == 1
    assert source.rows[0]["标题"] == "Title One"


def test_verify_all_failures_raise_first_error():
    def boom(title):
        raise RuntimeError("network down")

    source = _source()
    raised = ""
    try:
        verify_source(source, [0, 1], boom)
    except RuntimeError as exc:
        raised = str(exc)
    assert raised == "network down"


def test_verify_engine_chain_fallbacks():
    good = {
        "title": "Title One", "authors": ["Author A"], "year": 2003,
        "venue": "Journal X", "type": "journal-article", "doi": "10.1000/xyz",
    }

    def broken(title):
        raise RuntimeError("engine down")

    def empty(title):
        return []

    def hit(title):
        return [good]

    source = _source()
    report = verify_source(source, [0], [broken, hit])
    assert report.matched == 1 and report.failed == 0
    assert source.rows[0]["DOI"] == "10.1000/xyz"

    source = _source()
    report = verify_source(source, [0], [empty, hit])
    assert report.matched == 1 and source.rows[0]["DOI"] == "10.1000/xyz"

    source = _source()
    report = verify_source(source, [0], [broken, empty])
    assert (report.matched, report.unmatched, report.failed) == (0, 1, 0)

    source = _source()
    raised = ""
    try:
        verify_source(source, [0, 1], [broken, broken])
    except RuntimeError as exc:
        raised = str(exc)
    assert raised == "engine down"
