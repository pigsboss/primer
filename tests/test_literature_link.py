# -*- coding: utf-8 -*-
"""link 匹配核心的单元测试：抽取增强、标识符、题名分档、包容判定、缺文清单。

全部离线（临时 markdown 文件 + SimpleNamespace 假记录），不碰网络。
"""

from types import SimpleNamespace

from primer.literature.link import (
    build_index,
    markdown_title_candidates,
    match_file,
    propose_links,
    tier_for,
)


def _record(uuid, title, *, year="", venue="", doi="", eprint=""):
    return SimpleNamespace(uuid=uuid, title=title, year=year, venue=venue, doi=doi, eprint=eprint)


def _file(uuid, name, md_path, *, doi="", eprint="", status="done", record_uuid=""):
    return SimpleNamespace(
        uuid=uuid,
        name=name,
        md_path=md_path,
        doi=doi,
        eprint=eprint,
        status=status,
        record_uuid=record_uuid,
        path=name,
    )


def test_markdown_candidates_restore_links_and_filter_boilerplate():
    text = (
        "![](images/page_0_image_2.jpg)\n"
        "# [The K2 Mission: Characterization and Early Results](https://www.example.org/articles/x)\n"
        "\n"
        "Author One<sup>1</sup>, Author Two<sup>2</sup>\n"
        "Edited by: Some Editor\n"
        "DOI: 10.1234/abc\n"
    )
    candidates = markdown_title_candidates(text)
    assert candidates[0] == "The K2 Mission: Characterization and Early Results"
    assert all("example.org" not in item for item in candidates)


def test_markdown_candidates_join_wrapped_lines():
    text = "# A very long title that wraps\n\nto the next line here\n"
    candidates = markdown_title_candidates(text)
    assert "A very long title that wraps to the next line here" in candidates


def test_match_identifier_wins_before_title():
    records = [_record("r1", "Some title", doi="10.1/x"), _record("r2", "Other title")]
    index = build_index(records)
    match = match_file(index, doi="10.1/X", candidates=["nothing similar at all"])
    assert match["idx"] == 0 and match["how"] == "doi"
    assert tier_for(match) == "strong"


def test_match_title_strong_and_containment_suggest():
    records = [
        _record("r1", "The K2 mission: Characterization and early results"),
        _record(
            "r2",
            "2022 Tonga volcanic eruption induced global propagation of ionospheric disturbances via Lamb waves",
        ),
    ]
    index = build_index(records)
    strong = match_file(index, candidates=["The K2 Mission: Characterization and Early Results"])
    assert strong["idx"] == 0 and strong["ratio"] == 1.0
    assert tier_for(strong) == "strong"
    truncated = match_file(
        index,
        candidates=[
            "2022 Tonga Volcanic Eruption Induced Global Propagation"
        ],
    )
    assert truncated["how"] == "containment" and truncated["idx"] == 1
    assert tier_for(truncated) == "suggest"


def test_duplicate_record_titles_demote_to_suggest():
    records = [_record("r1", "The climate of early Mars"), _record("r2", "The climate of early Mars")]
    index = build_index(records)
    match = match_file(index, candidates=["The Climate of Early Mars"])
    assert match["tie"] is True
    assert tier_for(match) == "suggest"


def test_propose_links_covers_scope_skips_and_missing_list(tmp_path):
    (tmp_path / "a.md").write_text("# Alpha Mission Study Report\n", encoding="utf-8")
    (tmp_path / "b.md").write_text("# Totally unrelated line of text here\n", encoding="utf-8")
    records = [
        _record("r1", "Alpha mission study report", year="2020"),
        _record("r2", "Completely different topic about oceans", year="2021"),
    ]
    files = [
        _file("f1", "a.md", str(tmp_path / "a.md")),
        _file("f2", "b.md", str(tmp_path / "b.md")),
        _file("f3", "pending.md", "", status="pending"),
        _file("f4", "linked.md", str(tmp_path / "a.md"), record_uuid="r1"),
    ]
    progress = []
    result = propose_links(
        records,
        files,
        scope="unlinked",
        read_markdown=lambda f: (tmp_path / f.name).read_text(encoding="utf-8"),
        on_progress=lambda done, total: progress.append((done, total)),
    )
    assert result["stats"]["skipped_unparsed"] == 1
    assert result["stats"]["skipped_linked"] == 1
    assert progress[-1] == (2, 2)
    proposals = result["proposals"]
    assert [item["file_uuid"] for item in proposals if item["tier"] != "none"] == ["f1"]
    hit = proposals[0]
    assert hit["record_uuid"] == "r1" and hit["tier"] == "strong"
    assert hit["nature"] == "title-match" and hit["how"] == "title"
    missing = result["missing"]
    assert [item["uuid"] for item in missing] == ["r2"]
    assert missing[0]["closest_ratio"] < 0.8


def test_propose_links_all_scope_rechecks_linked(tmp_path):
    (tmp_path / "a.md").write_text("# Alpha Mission Study Report\n", encoding="utf-8")
    records = [_record("r1", "Alpha mission study report")]
    files = [_file("f1", "a.md", str(tmp_path / "a.md"), record_uuid="r1")]
    result = propose_links(
        records,
        files,
        scope="all",
        read_markdown=lambda f: (tmp_path / f.name).read_text(encoding="utf-8"),
    )
    assert result["stats"]["skipped_linked"] == 0
    assert result["stats"]["evaluated"] == 1
    assert result["proposals"][0]["record_uuid"] == "r1"


def test_online_lookup_upgrades_unmatched_file(tmp_path):
    (tmp_path / "a.md").write_text("# SCIENTIFIC REPORTS\n", encoding="utf-8")
    records = [_record("r1", "Real time detection of tsunamigenic earthquakes using GNSS")]
    files = [_file("f1", "a.md", str(tmp_path / "a.md"))]
    seen = []

    def online(file_record, candidates):
        seen.append((file_record.uuid, list(candidates)))
        return ["Real time detection of tsunamigenic earthquakes using GNSS"]

    result = propose_links(
        records,
        files,
        scope="unlinked",
        read_markdown=lambda f: (tmp_path / f.name).read_text(encoding="utf-8"),
        online_lookup=online,
    )
    assert seen and seen[0][0] == "f1"
    assert result["stats"]["online_checked"] == 1
    assert result["stats"]["online_upgraded"] == 1
    proposal = result["proposals"][0]
    assert proposal["how"] == "online" and proposal["tier"] == "weak"
    assert proposal["record_uuid"] == "r1"
    assert [item["uuid"] for item in result["missing"]] == ["r1"]
    assert result["missing"][0]["closest_ratio"] == 1.0


def test_online_lookup_errors_are_ignored(tmp_path):
    (tmp_path / "a.md").write_text("# SCIENTIFIC REPORTS\n", encoding="utf-8")
    records = [_record("r1", "Some completely different title here")]
    files = [_file("f1", "a.md", str(tmp_path / "a.md"))]

    def broken_online(file_record, candidates):
        raise RuntimeError("engine down")

    result = propose_links(
        records,
        files,
        read_markdown=lambda f: (tmp_path / f.name).read_text(encoding="utf-8"),
        online_lookup=broken_online,
    )
    assert result["stats"]["online_checked"] == 1
    assert result["stats"]["online_upgraded"] == 0
    assert result["proposals"] == []
    assert [item["uuid"] for item in result["missing"]] == ["r1"]
