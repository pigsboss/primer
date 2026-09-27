# -*- coding: utf-8 -*-
"""export 的单元测试：index、audit.md、图书馆清单的字段、筛选与优先级。"""

import csv
import json
from pathlib import Path

from primer.references.audit import run_audit
from primer.references.entries import parse_markdown
from primer.references.export import (
    LIBRARY_COLUMNS,
    collect_body_citations,
    library_rows,
    render_library_md,
    write_audit_md,
    write_index_csv,
    write_index_json,
    write_library_csv,
    write_library_md,
)
from primer.references.ledger import build_ledger

REF_MD = """# 参考文献库

> 共 3 条
> 结构：A 战略 [1–3]

## A 战略

[1] Alpha A. Title one. Journal 1, 1–2, 2001. ［原文：arXiv:1111.2222 已存本地］
[2] Beta B. Title two. Journal 2, 3–4, 2002. ［原文：待图书馆获取］
[3] Gamma C. Title three. Journal 3, 5–6, 2003. ［原文：待图书馆获取］
"""


def _result(tmp_path: Path, body: str = ""):
    refs = tmp_path / "refs"
    refs.mkdir()
    (refs / "001_A_Title_one.pdf").write_bytes(b"%PDF-1.4 stub")
    ledger = build_ledger(local_roots=[refs])
    cited: set[int] = set()
    sources: list[str] = []
    if body:
        body_path = tmp_path / "body.md"
        body_path.write_text(body, encoding="utf-8")
        cited, sources = collect_body_citations([body_path])
    return run_audit(
        parse_markdown(REF_MD, source="refs.md"),
        ledger,
        body_citations=cited,
        body_sources=sources,
    )


def test_index_json_shape(tmp_path):
    result = _result(tmp_path)
    out = tmp_path / "out"

    write_index_json(out / "index.json", result)

    payload = json.loads((out / "index.json").read_text(encoding="utf-8"))
    assert payload["summary"]["entries"] == 3
    assert payload["summary"]["counts"]["local"] == 1
    assert payload["validation"][0]["declared_total"] == 3
    assert payload["validation"][0]["actual_count"] == 3
    assert [item["number"] for item in payload["entries"]] == [1, 2, 3]
    assert payload["entries"][0]["verdict"] == "local"
    assert payload["entries"][1]["verdict"] == "missing"
    assert any("001_A_Title_one.pdf" in item for item in payload["entries"][0]["evidence"])
    assert payload["body"] == {"sources": [], "cited_numbers": []}


def test_index_csv_columns_and_rows(tmp_path):
    result = _result(tmp_path)
    out = tmp_path / "out"

    write_index_csv(out / "index.csv", result)

    with open(out / "index.csv", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 3
    assert list(rows[0]) == [
        "number",
        "class_letter",
        "class_title",
        "authors",
        "title",
        "venue_year",
        "arxiv",
        "doi",
        "tag_kind",
        "verdict",
        "confidence",
        "source",
        "line",
        "note",
        "evidence",
    ]
    assert rows[0]["number"] == "1"
    assert rows[0]["verdict"] == "local"
    assert rows[1]["verdict"] == "missing"
    assert "reflib" not in rows[0]["evidence"]


def test_audit_md_reports_summary_validation_and_problems(tmp_path):
    result = _result(tmp_path)
    out = tmp_path / "out"

    write_audit_md(out / "audit.md", result)

    text = (out / "audit.md").read_text(encoding="utf-8")
    assert "# Reference audit" in text
    assert "- entries parsed: 3 from 1 list(s)" in text
    assert "- local (copy on disk): 1" in text
    assert "- missing: 2" in text
    assert "declared total: 3 | actual entries: 3" in text
    assert "sections declared but absent: none" in text
    assert "### Local files without a reference number in the filename (0" in text
    assert "not checked (no --text-root given)" in text


def test_library_csv_schema_and_only_missing_entries(tmp_path):
    result = _result(tmp_path)
    out = tmp_path / "out"
    rows = library_rows(result)

    write_library_csv(out / "图书馆文献获取清单.new.csv", rows)
    write_library_md(out / "图书馆文献获取清单.new.md", rows, result)

    with open(out / "图书馆文献获取清单.new.csv", encoding="utf-8-sig", newline="") as handle:
        read = list(csv.DictReader(handle))
    assert list(LIBRARY_COLUMNS) == [
        "no",
        "priority",
        "source",
        "topic",
        "title",
        "authors",
        "year",
        "venue",
        "url",
        "status",
        "note",
    ]
    assert list(read[0]) == list(LIBRARY_COLUMNS)
    assert [row["source"] for row in read] == ["参考文献库[2]", "参考文献库[3]"]
    assert all(row["status"] == "pending" for row in read)
    assert all(row["priority"] == "P1" for row in read)
    assert read[0]["year"] == "2002"
    assert "未提供 --body，全部按 P1 处理" in render_library_md(rows, result)


def test_library_priority_follows_body_citations(tmp_path):
    result = _result(tmp_path, body="Only [3] is cited here.\n")
    rows = library_rows(result)

    assert [(row["source"], row["priority"]) for row in rows] == [
        ("参考文献库[3]", "P1"),
        ("参考文献库[2]", "P2"),
    ]
    assert rows[0]["note"].endswith("cited in a supplied body markdown; confidence: high")
    assert rows[1]["note"].endswith("not cited in any body markdown; confidence: high")
    assert "被 --body 正文引用者为 P1" in render_library_md(rows, result)


def test_collect_body_citations_expands_ranges(tmp_path):
    body = tmp_path / "body.md"
    body.write_text("See [12–15], also [3], and [7][8]. Ignore [x].\n", encoding="utf-8")

    cited, sources = collect_body_citations([body])

    assert cited == {3, 7, 8, 12, 13, 14, 15}
    assert sources == [str(body)]
