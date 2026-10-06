# -*- coding: utf-8 -*-
"""mdlist：标题清洗、条目解析、匹配（精确／前缀回退）与项目分组。"""

from primer.literature.library import Record
from primer.literature.mdlist import clean_heading, parse_md_entries, propose_projects_from_md


def _record(uuid: str, title: str, notes: str) -> Record:
    return Record.from_dict(
        {"uuid": uuid, "title": title, "authors": [], "year": None, "notes": notes}
    )


def test_clean_heading_variants():
    assert (
        clean_heading("M 地震前兆探测专题（地球端·编制说明番外支撑，编号 558–）")
        == "地震前兆探测"
    )
    assert clean_heading("B1 发现与巡天") == "发现与巡天"
    assert (
        clean_heading("宜居性系统科学专题（2026-10-01 调研新增，编号 444–477）")
        == "宜居性系统科学"
    )
    assert clean_heading("G8 工程篇增补：未来任务计划清点（v3 增补）") == "工程篇增补：未来任务计划清点"
    assert clean_heading("＝ 科学篇（基础，336 条）＝") == "科学篇"
    assert clean_heading("") == ""


def test_parse_md_entries_tracks_headings(tmp_path):
    path = tmp_path / "list.md"
    path.write_text(
        "# 标题\n\n## M 地震前兆探测专题（编号 558–）\n"
        "[558] Byerlee J. Friction of rocks. Pure and Applied Geophysics, 1978. ［原文：待图书馆获取］\n"
        "\n### N 风暴海啸预报专题（编号 629–）\n"
        "[629] Sample N paper. Ocean Modelling, 2019.\n"
        "- 非条目行\n",
        encoding="utf-8",
    )
    entries = parse_md_entries(path)
    assert [entry["project"] for entry in entries] == ["地震前兆探测", "风暴海啸预报"]
    assert entries[0]["key"] == "Byerlee J. Friction of rocks. Pure and Applied Geophysics, 1978."


def test_propose_projects_exact_prefix_and_duplicates(tmp_path):
    path = tmp_path / "list.md"
    long_line = "Some very long citation about compound flooding " + "x" * 30
    cross = "Same cross-listed paper. Journal, 2021."
    path.write_text(
        "## M 地震前兆探测专题（编号 558–）\n"
        f"[1] {long_line}\n"
        "## B1 发现与巡天\n"
        "[2] Another paper about surveys. Journal, 2020.\n"
        f"[3] {cross}\n"
        "## 宜居性系统科学专题（编号 444–477）\n"
        f"[4] {cross}\n",
        encoding="utf-8",
    )
    records = [
        _record("r1", "t1", "原始记录：" + long_line + " 补充说明"),
        _record("r2", "t2", "原始记录：Another paper about surveys. Journal, 2020."),
        _record("rc", "tc", "原始记录：" + cross),
        _record("r3", "t3", "原始记录：完全对不上的短题录"),
    ]
    stats, groups, unmatched = propose_projects_from_md(records, path)
    assert stats["entries"] == 4 and stats["projects"] == 3
    assert stats["matched"] == 3 and stats["exact"] == 2 and stats["prefix"] == 1
    assert stats["unmatched"] == 1
    by_project = {group["project"]: group for group in groups}
    assert by_project["地震前兆探测"]["uuids"] == ["r1"]
    assert by_project["发现与巡天"]["uuids"] == ["r2", "rc"]
    assert by_project["宜居性系统科学"]["uuids"] == ["rc"]
    assert unmatched == [{"uuid": "r3", "title": "t3", "note": "完全对不上的短题录"}]


def test_propose_projects_respects_unmatched_limit(tmp_path):
    path = tmp_path / "list.md"
    path.write_text("## M 地震前兆探测专题（编号 558–）\n[1] Only entry. Journal, 2020.\n", encoding="utf-8")
    records = [_record(f"r{i}", f"t{i}", f"原始记录：对不上 {i}") for i in range(5)]
    stats, groups, unmatched = propose_projects_from_md(records, path, unmatched_limit=2)
    assert not groups and stats["unmatched"] == 5 and len(unmatched) == 2
