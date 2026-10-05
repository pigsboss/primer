# -*- coding: utf-8 -*-
"""refine 的单元测试：疑似检测、字段落位、DOI 防幻觉、块失败隔离与建议刷新。

全部用假 chat（不发网络）；SourceData 用真实的 markdown 解析器构造。
"""

import json

from primer.literature.importers import parse_source
from primer.literature.refine import refine_source, suspicious_rows


def _source():
    md = (
        "# 参考\n"
        "## A 战略\n"
        "[1] Author A. Title One. Journal X, 2003.\n"
        "[2] 只有一段没有句点\n"
    ).encode("utf-8")
    return parse_source("refs.md", md)


def test_suspicious_rows_flags_split_failures():
    source = _source()
    assert suspicious_rows(source.rows, source.columns, source.suggested) == [1]


def test_refine_applies_fields_and_creates_type_column():
    source = _source()
    reply = json.dumps(
        [
            {"i": 0, "title": "Title One", "authors": ["Author A"], "year": 2003,
             "venue": "Journal X", "type": "journal-article", "doi": None},
            {"i": 1, "title": "修正后的标题", "authors": ["某甲"], "year": 2020,
             "venue": "期刊Y", "type": "report", "doi": "10.9999/编造"},
        ],
        ensure_ascii=False,
    )

    report = refine_source(source, [0, 1], lambda system, user: reply)

    assert report.refined == 2 and report.failed == 0 and report.chunks_failed == 0
    assert source.rows[0]["出处"] == "Journal X"
    assert source.rows[1]["标题"] == "修正后的标题"
    assert source.rows[1]["作者"] == "某甲"
    assert "类型" in source.columns
    assert source.suggested["类型"] == "type"
    # 编造的 DOI 原文里没有 → 丢弃，保留原值（空）
    assert source.rows[1]["DOI"] == ""


def test_refine_keeps_doi_only_when_literal_in_text():
    md = "# 参考\n## A\n[1] Author B. Title B. Journal Y, 2021. doi:10.1000/xyz\n".encode("utf-8")
    source = parse_source("refs.md", md)

    good = json.dumps([{"i": 0, "title": "Title B", "authors": ["Author B"], "year": 2021,
                        "venue": "Journal Y", "type": "journal-article", "doi": "10.1000/xyz"}])
    report = refine_source(source, [0], lambda system, user: good)
    assert report.refined == 1
    assert source.rows[0]["DOI"] == "10.1000/xyz"

    # 换成原文没有的 DOI → 丢弃，行里的原值保持不变
    bad = json.dumps([{"i": 0, "title": "Title B", "authors": ["Author B"], "year": 2021,
                       "venue": "Journal Y", "type": "journal-article", "doi": "10.1000/other"}])
    refine_source(source, [0], lambda system, user: bad)
    assert source.rows[0]["DOI"] == "10.1000/xyz"


def test_refine_chunk_failure_keeps_rows_untouched():
    source = _source()
    before = [dict(row) for row in source.rows]

    def boom(system, user):
        raise RuntimeError("network down")

    raised = ""
    try:
        refine_source(source, [0, 1], boom, chunk_size=1)
    except RuntimeError as exc:
        raised = str(exc)
    assert raised == "network down"
    assert source.rows == before


def test_refine_parses_fenced_reply_and_reports_progress():
    source = _source()
    fenced = "```json\n" + json.dumps(
        [
            {"i": 0, "title": "T0", "authors": [], "year": 2003, "venue": "V",
             "type": "report", "doi": None},
            {"i": 1, "title": "T1", "authors": [], "year": 2020, "venue": "V",
             "type": "report", "doi": None},
        ],
        ensure_ascii=False,
    ) + "\n```"
    seen: list[int] = []

    report = refine_source(
        source, [0, 1], lambda system, user: fenced, chunk_size=2, on_progress=seen.append
    )

    assert report.refined == 2
    assert seen == [2]


def test_refine_only_touches_given_indices():
    source = _source()
    reply = json.dumps([{"i": 1, "title": "只改第二条", "authors": ["某甲"], "year": 2020,
                         "venue": "V", "type": "report", "doi": None}], ensure_ascii=False)

    report = refine_source(source, [1], lambda system, user: reply)

    assert report.refined == 1
    assert source.rows[0]["标题"] == "Title One"  # 未在范围内，保持原值
    assert source.rows[1]["标题"] == "只改第二条"
