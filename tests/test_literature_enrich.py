# -*- coding: utf-8 -*-
"""primer.literature.enrich：记录补全（联网置信匹配 / AI 清洗护栏 / 字段应用）。"""

from __future__ import annotations

import pytest

from primer.literature.enrich import (
    ai_parse,
    apply_fields,
    clean_ai_fields,
    match_candidate,
    preview_project_infer,
    verify_records,
)
from primer.literature.library import Record


def _record(uuid: str = "u1", **overrides) -> Record:
    payload = {
        "uuid": uuid,
        "title": "",
        "authors": [],
        "year": None,
        "venue": "",
        "doi": None,
        "notes": "",
    }
    payload.update(overrides)
    return Record.from_dict(payload)


def test_match_candidate_confidence():
    record = _record(
        title="Kepler planet detection mission introduction and first results", year=2010
    )
    good = {
        "title": "Kepler Planet-Detection Mission: Introduction and First Results",
        "year": 2010,
    }
    wrong_year = dict(good, year=2013)
    far = {"title": "A completely different paper about galaxies", "year": 2010}
    assert match_candidate(record, [far, wrong_year]) is None
    assert match_candidate(record, [far, wrong_year, good]) == good


def test_verify_records_updates_and_raises_on_total_failure():
    hit = _record(
        "u1", title="Kepler planet detection mission introduction and first results", year=2010
    )
    miss = _record("u2", title="行星探测三十年综述", year=2023)

    def lookup(title):
        if title.startswith("Kepler"):
            return [{
                "title": "Kepler Planet-Detection Mission: Introduction and First Results",
                "authors": ["William J. Borucki", "David Koch"],
                "year": 2010,
                "venue": "Science",
                "type": "journal-article",
                "doi": "10.1126/science.1185402",
            }]
        return []

    report = verify_records([hit, miss], lookup)
    assert (report.updated, report.skipped, report.failed) == (1, 1, 0)
    assert hit.doi == "10.1126/science.1185402"
    assert hit.venue == "Science"
    assert hit.authors == ["William J. Borucki", "David Koch"]
    assert hit.updated_at

    def broken(title):
        raise ConnectionError("offline")

    with pytest.raises(ConnectionError):
        verify_records([hit, miss], broken)


def test_clean_ai_fields_guards():
    record = _record("u3", title="Some title", notes="原始记录：DOI: 10.1000/abc 等等")
    fields = clean_ai_fields(record, {
        "title": "Some title",
        "authors": ["A", " B "],
        "year": "1999",
        "type": "journal-article",
        "venue": "Journal X",
        "doi": "https://doi.org/10.1000/abc",
    })
    assert fields["doi"] == "10.1000/abc"
    assert fields["year"] == 1999
    assert fields["authors"] == ["A", "B"]
    assert fields["venue"] == "Journal X"

    bad = clean_ai_fields(record, {"doi": "10.9999/made-up", "type": "poem", "year": "1200"})
    assert "doi" not in bad and "type" not in bad and "year" not in bad


def test_apply_fields_counts_only_real_changes():
    record = _record("u4", title="T", venue="V")
    assert apply_fields(record, {"venue": "V", "title": "T"}) == 0
    assert apply_fields(record, {"venue": "W", "year": None, "doi": ""}) == 1
    assert record.venue == "W"


def test_ai_parse_updates_and_chunk_failure():
    records = [_record("a", title="T1"), _record("b", title="T2")]

    def chat(system, user):
        if "i=0" in user:
            return '[{"i": 0, "title": "T1", "venue": "Journal X"}]'
        raise RuntimeError("boom")

    report = ai_parse(records, [0, 1], chat, chunk_size=1)
    assert (report.updated, report.failed) == (1, 1)
    assert records[0].venue == "Journal X"
    assert records[1].venue == ""

    def broken(system, user):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        ai_parse(records, [0, 1], broken, chunk_size=1)


def test_verify_records_engine_chain_fallbacks():
    title = "Kepler planet-detection mission: introduction and first results"
    good = [{
        "title": title,
        "year": 2010,
        "authors": ["William J. Borucki"],
        "venue": "Science",
        "type": "journal-article",
        "doi": "10.1126/science.1185402",
    }]

    def broken(text):
        raise ConnectionError("down")

    def empty(text):
        return []

    def hit(text):
        return good

    first = _record("u6", title=title, year=2010)
    report = verify_records([first], [broken, hit])
    assert report.updated == 1 and first.doi == "10.1126/science.1185402"

    second = _record("u7", title=title, year=2010)
    report = verify_records([second], [empty, hit])
    assert report.updated == 1 and second.doi == "10.1126/science.1185402"

    third = _record("u8", title=title, year=2010)
    report = verify_records([third], [broken, empty])
    assert (report.updated, report.skipped, report.failed) == (0, 1, 0)

    with pytest.raises(ConnectionError):
        verify_records([_record("u9", title=title, year=2010)], [broken, broken])


def test_enrich_applies_biblatex_fields():
    from primer.literature.enrich import apply_candidate, record_text

    record = _record("u10", title="T", year=2020)
    changed = apply_candidate(record, {
        "title": "T", "volume": "12", "number": "3", "pages": "45-67",
        "publisher": "P", "issn": "1234-5678", "editor": ["E. Ditor"],
        "location": "北京",
    })
    assert changed == 7
    assert record.volume == "12" and record.pages == "45-67"
    assert record.editor == ["E. Ditor"] and record.location == "北京"
    assert "45-67" in record_text(record)

    fields = clean_ai_fields(record, {"keywords": "适航；复用", "eid": "e123", "volume": "13"})
    assert fields["keywords"] == "适航；复用" and fields["eid"] == "e123"
    assert apply_fields(record, fields) >= 2
    assert record.keywords == "适航；复用" and record.eid == "e123"


def test_preview_verify_records_does_not_mutate():
    from primer.literature.enrich import apply_pending, preview_verify_records

    title = "Kepler planet-detection mission: introduction and first results"
    record = _record("p1", title=title, year=2010)

    def hit(text):
        return [{
            "title": title, "authors": ["Borucki"], "year": 2010,
            "venue": "Science", "type": "journal-article", "doi": "10.1126/science.1185402",
        }]

    report, pending = preview_verify_records([record], [hit])
    assert report.updated == 1
    assert record.doi is None  # 预演不改记录
    assert [item["uuid"] for item in pending] == ["p1"]
    fields = {change["field"]: change["new"] for change in pending[0]["changes"]}
    assert fields["doi"] == "10.1126/science.1185402"

    assert apply_pending([record], pending) == 1
    assert record.doi == "10.1126/science.1185402"


def test_preview_ai_parse_and_apply():
    from primer.literature.enrich import apply_pending, preview_ai_parse

    record = _record("p2", title="T", year=2000)

    def chat(system, user):
        return '[{"i": 0, "title": "T", "year": 1999, "venue": "V"}]'

    report, pending = preview_ai_parse([record], [0], chat, chunk_size=1)
    assert report.updated == 1
    assert record.year == 2000 and record.venue == ""  # 预演不改记录
    assert apply_pending([record], pending) == 1
    assert record.year == 1999 and record.venue == "V"


def test_verify_preview_fills_download_url_chain():
    from primer.literature.enrich import apply_pending, preview_verify_records

    record = _record("u9", title="Some paper about detectors", year=2020, doi="10.1000/self")

    def hit(text):
        return [{
            "title": "Some paper about detectors", "authors": ["A"], "year": 2020,
            "doi": "10.1000/self",
        }]

    report, pending = preview_verify_records([record], [hit])
    assert report.updated == 1
    fields = {change["field"]: change["new"] for change in pending[0]["changes"]}
    assert fields["download_url"] == "https://doi.org/10.1000/self"  # 兜底：DOI 页

    assert apply_pending([record], pending) == 1
    assert record.download_url == "https://doi.org/10.1000/self"


def test_verify_preview_prefers_engine_download_url_and_eprint():
    from primer.literature.enrich import preview_verify_records

    record = _record("u10", title="Some paper about detectors", year=2020)

    def hit(text):
        return [{
            "title": "Some paper about detectors", "authors": ["A"], "year": 2020,
            "download_url": "https://example.org/oa.pdf",
            "eprint": "2001.00001",
        }]

    report, pending = preview_verify_records([record], [hit])
    assert report.updated == 1
    fields = {change["field"]: change["new"] for change in pending[0]["changes"]}
    assert fields["download_url"] == "https://example.org/oa.pdf"  # 引擎直给优先
    assert fields["eprint"] == "2001.00001"

    record2 = _record("u11", title="Some paper about detectors", year=2020)

    def hit_no_url(text):
        return [{
            "title": "Some paper about detectors", "authors": ["A"], "year": 2020,
            "eprint": "2001.00001",
        }]

    report2, pending2 = preview_verify_records([record2], [hit_no_url])
    assert report2.updated == 1
    fields2 = {change["field"]: change["new"] for change in pending2[0]["changes"]}
    assert fields2["download_url"] == "https://arxiv.org/pdf/2001.00001"  # arXiv PDF 兜底


def test_preview_project_infer_validates_candidates():
    records = [
        _record("p1", title="Seismic nucleation experiment", notes="原始记录：亚失稳理论与实验研究"),
        _record("p2", title="Storm surge modelling", notes="原始记录：全球风暴潮模式"),
        _record("p3", title="Decadal survey", notes="原始记录：行星科学十年调查"),
    ]

    def chat(system, user):
        assert "候选项目" in user
        return (
            '[{"i": 0, "projects": ["地震前兆探测"]},'
            ' {"i": 1, "projects": ["风暴海啸预报", "不存在的项目"]},'
            ' {"i": 2, "projects": []}]'
        )

    report, pending = preview_project_infer(
        records, [0, 1, 2], ["地震前兆探测", "风暴海啸预报"], chat
    )
    assert report.updated == 2 and report.skipped == 1 and report.failed == 0
    assert pending[0]["uuid"] == "p1" and pending[0]["projects"] == ["地震前兆探测"]
    # 不在候选里的路径被逐字校验丢弃
    assert pending[1]["projects"] == ["风暴海啸预报"]

    with pytest.raises(ValueError):
        preview_project_infer(records, [0], [], chat)


def test_preview_project_infer_chunk_failure_and_dedupe():
    records = [_record("p1", title="A", notes="n", projects=["地震前兆探测"])]

    reply = '[{"i": 0, "projects": ["地震前兆探测"]}]'
    report, pending = preview_project_infer(
        records, [0], ["地震前兆探测"], lambda system, user: reply, chunk_size=1
    )
    assert report.skipped == 1 and not pending  # 已有项目 → 无变更

    def broken(system, user):
        raise RuntimeError("LLM down")

    with pytest.raises(RuntimeError):
        preview_project_infer(records, [0], ["地震前兆探测"], broken, chunk_size=1)
