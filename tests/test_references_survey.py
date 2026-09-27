# -*- coding: utf-8 -*-
"""survey 与图书馆清单合并的单元测试：日志筛选、标题键去重、构成与置信度。"""

import csv
import json
from pathlib import Path

from primer.references.audit import run_audit
from primer.references.entries import parse_markdown
from primer.references.export import (
    LIBRARY_COLUMNS,
    collect_body_citations,
    index_payload,
    library_confidence,
    library_rows,
    render_library_md,
    source_counts,
    write_library_csv,
)
from primer.references.ledger import build_ledger
from primer.references.survey import (
    SURVEY_SOURCE,
    needs_library,
    read_oa_log,
    title_key,
)

REF_MD = """# 参考文献库

> 共 2 条

## A 战略

[1] Alpha A. Title one. Journal 1, 1–2, 2001. ［原文：待图书馆获取］
[2] Beta B. Shared title. Journal 2, 3–4, 2002. ［原文：NASA 官网公开］
[3] Gamma C. Shared title. Journal 3, 5–6, 2003. ［原文：待图书馆获取］
"""

LOG_HEADER = "topic,list,title,year,url,status,saved\n"


def _write_log(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "oa_download_log.csv"
    path.write_text(LOG_HEADER + body, encoding="utf-8")
    return path


def _result(tmp_path: Path, body: str = ""):
    ledger = build_ledger(local_roots=[])
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


def test_title_key_matches_the_old_script_normalisation():
    # 旧脚本：re.sub(r"\W+", "", title.lower())[:60]
    assert title_key("Exoplanet Biosignatures: A Framework!") == "exoplanetbiosignaturesaframework"
    assert title_key("系外行星，宜居性：评价") == "系外行星宜居性评价"
    assert len(title_key("x" * 80)) == 60


def test_needs_library_only_accepts_fail_and_no_link():
    assert needs_library("fail(err:URLError)")
    assert needs_library("no-link")
    assert not needs_library("ok(1KB)")
    assert not needs_library("off-topic")
    assert not needs_library("")


def test_off_topic_last_row_supersedes_an_earlier_failure(tmp_path):
    log = _write_log(
        tmp_path,
        "宜居行星文献,01.csv,Paper one,2018,,fail(err:URLError),\n"
        "宜居行星文献,01.csv,Paper one,2018,,off-topic,\n",
    )

    items = read_oa_log(log)

    assert items == []


def test_failure_after_off_topic_still_enters_the_list(tmp_path):
    log = _write_log(
        tmp_path,
        "宜居行星文献,01.csv,Paper one,2018,,off-topic,\n"
        "宜居行星文献,01.csv,Paper one,2018,,no-link,\n",
    )

    items = read_oa_log(log)

    assert [item.title for item in items] == ["Paper one"]
    assert items[0].status == "no-link"
    assert items[0].line == 3


def test_repeated_failures_collapse_to_the_last_attempt(tmp_path):
    log = _write_log(
        tmp_path,
        "金星探测文献,02.csv,Paper two,2020,,fail(err:A),\n"
        "金星探测文献,02.csv,Paper two,2020,,fail(err:B),\n",
    )

    items = read_oa_log(log)

    assert len(items) == 1
    assert items[0].status == "fail(err:B)"
    assert items[0].line == 3


def test_ok_row_excludes_the_title_only_when_the_file_is_still_on_disk(tmp_path):
    saved = tmp_path / "saved.pdf"
    saved.write_bytes(b"%PDF-1.4 stub")
    log = _write_log(
        tmp_path,
        "宜居行星文献,01.csv,Got it,2018,,fail(err:X),\n"
        "宜居行星文献,01.csv,Got it,2018,,ok(1KB),%s\n"
        "宜居行星文献,01.csv,Gone,2018,,fail(err:X),\n"
        "宜居行星文献,01.csv,Gone,2018,,ok(1KB),%s\n"
        % (saved, tmp_path / "vanished.pdf"),
    )

    items = read_oa_log(log)

    assert [item.title for item in items] == ["Gone"]


def test_survey_rows_are_p2_with_a_distinct_source(tmp_path):
    log = _write_log(tmp_path, "金星探测文献,02.csv,Fresh paper,2020,,no-link,\n")
    result = _result(tmp_path, body="Only [1] is cited.\n")

    rows = library_rows(result, survey=read_oa_log(log))

    merged = [row for row in rows if row["source"].startswith(SURVEY_SOURCE)]
    assert len(merged) == 1
    assert merged[0]["priority"] == "P2"
    assert merged[0]["source"] == "文献调研清单[金星探测文献]"
    assert merged[0]["topic"] == "金星探测文献"
    assert merged[0]["year"] == "2020"
    assert merged[0]["status"] == "pending"
    assert "survey log row 2 status=no-link" in merged[0]["note"]
    assert merged[0]["note"].endswith("confidence: high")


def test_a_survey_row_is_merged_only_when_its_title_matches_an_entry(tmp_path):
    log = _write_log(
        tmp_path,
        "A 战略,01.csv,Shared title,2003,,no-link,\n"
        "A 战略,01.csv,A title nobody cites,2003,,no-link,\n",
    )
    result = _result(tmp_path)

    rows = library_rows(result, survey=read_oa_log(log))

    assert [row["source"] for row in rows if row["source"].startswith(SURVEY_SOURCE)] == [
        "文献调研清单[A 战略]"
    ]
    assert [row["no"] for row in rows] == ["1", "2", "3"]
    assert source_counts(rows) == {"参考文献库": 2, SURVEY_SOURCE: 1}


def test_numbered_entries_are_never_dropped_in_favour_of_a_log_row(tmp_path):
    log = _write_log(tmp_path, "A 战略,01.csv,Title one,2001,,no-link,\n")
    result = _result(tmp_path)

    rows = library_rows(result, survey=read_oa_log(log))

    # [1] 与日志同标题：编号行保留（能追到正文），日志行被合并掉。
    assert [row["source"] for row in rows] == ["参考文献库[1]", "参考文献库[3]"]


def test_confidence_is_low_without_year_and_url():
    assert library_confidence("high", year="", url="") == "low"
    assert library_confidence("high", year="1999", url="") == "high"
    assert library_confidence("medium", year="", url="https://x/1") == "medium"


def test_every_row_carries_a_confidence_indicator(tmp_path):
    log = _write_log(tmp_path, "主题,01.csv,No metadata,,,no-link,\n")
    result = _result(tmp_path)

    rows = library_rows(result, survey=read_oa_log(log))

    assert all("confidence: " in row["note"] for row in rows)
    assert [row["note"].rsplit("confidence: ", 1)[-1] for row in rows] == [
        "high",
        "high",
        "low",
    ]


def test_library_csv_keeps_the_schema_and_leaves_public_web_out(tmp_path):
    log = _write_log(tmp_path, "主题,01.csv,From the log,2019,,no-link,\n")
    result = _result(tmp_path)
    rows = library_rows(result, survey=read_oa_log(log))
    out = tmp_path / "out"

    write_library_csv(out / "list.csv", rows)

    with open(out / "list.csv", encoding="utf-8-sig", newline="") as handle:
        read = list(csv.DictReader(handle))
    assert list(read[0]) == list(LIBRARY_COLUMNS)
    assert [row["source"].split("[")[0] for row in read] == [
        "参考文献库",
        "参考文献库",
        SURVEY_SOURCE,
    ]
    assert all("官网公开" not in row["note"] for row in read)


def test_library_md_reports_the_composition_and_lists_public_web_separately(tmp_path):
    log = _write_log(tmp_path, "主题,01.csv,From the log,2019,,no-link,\n")
    result = _result(tmp_path)
    survey = read_oa_log(log)

    text = render_library_md(library_rows(result, survey=survey), result, survey=survey)

    assert "共 3 条（P1=2，P2=1）" in text
    assert "参考文献库 missing 2 条；调研日志合并 1 条" in text
    assert "## 无需图书馆获取（1 条，网上公开，仅供备查）" in text
    assert "不必向图书馆索取" in text
    assert text.count("Shared title") == 2
    assert "https://arxiv.org" not in text


def test_index_json_is_untouched_by_the_survey_source(tmp_path):
    log = _write_log(tmp_path, "主题,01.csv,From the log,2019,,no-link,\n")
    result = _result(tmp_path)

    payload = json.loads(json.dumps(index_payload(result)))

    assert payload["summary"]["entries"] == 3
    assert all("文献调研清单" not in entry["source"] for entry in payload["entries"])
    assert read_oa_log(log)[0].topic == "主题"
