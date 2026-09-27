# -*- coding: utf-8 -*-
"""``primer.claims.resolve`` 的单元测试：四跳链路与断点定位。

夹具是一棵微型工程树（参考文献 markdown + 本地 PDF + 下载账本 + 转换账本 + 扁平
markdown），不读真实语料、不联网。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from primer.claims import resolve as rs

BIBLIOGRAPHY = """\
# 参考文献库

> 共 5 条

[A 战略规划]

[1] Alpha A. Ice giants in the outer solar system. Icarus 1, 1–2, 2001. ［原文：arXiv:2511.13946 已存本地］
[2] Beta B. Title two. Icarus 2, 3–4, 2002. ［原文：待图书馆获取］
[3] Gamma C. Habitable zones around main-sequence stars. ApJ 3, 5–6, 2003. ［原文：arXiv:1301.6674 已存本地］
[4] Delta D. Habitable zones around main-sequence stars: new estimates. ApJ 4, 7–8, 2004. ［原文：arXiv:1301.6674 已存本地］
[5] Epsilon E. Title five. Icarus 5, 9–10, 2005. ［原文：arXiv:1301.6674 已存本地］
"""

LOCAL_ROOT = "参考资料/参考文献原文"
STATE_PATH = "_primer/literature/state.jsonl"
FLAT_DIR = "_primer/literature/flat"

BODY = "The ice giant systems have never been revisited since Voyager 2 in 1989. " * 40
SHORT_BODY = "too short"


def _state(md5: str, rel_path: str, stem: str, status: str = "done") -> dict:
    return {
        "md5": md5,
        "rel_path": rel_path,
        "tier": "standard",
        "status": status,
        "output_dir": f"_primer/literature/raw/{stem}",
        "pages": 4,
    }


@pytest.fixture
def project(tmp_path):
    """001 全通、002 无本地原文、003 未转换、004 扁平文本太短、005 无本地原文。

    003/004/005 共享同一个 arXiv 号，用来验证共享 arXiv 时按编号解析而不串台。
    """
    root = tmp_path / "行星探测工程"
    (root / "成果文件").mkdir(parents=True)
    (root / "成果文件" / "参考文献.md").write_text(BIBLIOGRAPHY, encoding="utf-8")
    local = root / LOCAL_ROOT
    local.mkdir(parents=True)
    names = {
        1: "001_A_Ice_giants.pdf",
        3: "003_B_Habitable_zones.pdf",
        4: "004_B_Habitable_zones_new_estimates.pdf",
        5: "005_B_Title_five.pdf",
    }
    for name in names.values():
        (local / name).write_bytes(b"%PDF-1.4 " + name.encode())

    log = root / "参考资料" / "reflib_download_log.csv"
    log.parent.mkdir(parents=True, exist_ok=True)
    lines = ["ref,class,title,arxiv,status,saved"]
    for number, name in names.items():
        arxiv = "2511.13946" if number == 1 else "1301.6674"
        lines.append(f"{number},A,title,{arxiv},ok(1KB),{local / name}")
    lines.append("2,A,Title two,no-arxiv,not_found,")
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")

    records = [
        _state("11111111111111111111111111111111", f"{LOCAL_ROOT}/{names[1]}", "11111111_001_A_Ice_giants"),
        _state("22222222222222222222222222222222", f"{LOCAL_ROOT}/{names[4]}", "22222222_004_B_Habitable"),
    ]
    literature = root / "_primer" / "literature"
    (literature / "flat").mkdir(parents=True)
    (literature / "state.jsonl").write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records), encoding="utf-8"
    )
    (literature / "flat" / "11111111_001_A_Ice_giants.md").write_text(
        "> **Citation** — ref 001 · class A\n> **Source** — x\n\n" + BODY, encoding="utf-8"
    )
    (literature / "flat" / "22222222_004_B_Habitable.md").write_text(
        "> **Citation** — ref 004 · class B\n\n" + SHORT_BODY, encoding="utf-8"
    )
    return root


def _resolver(project: Path) -> rs.Resolver:
    return rs.build_resolver(
        project,
        reference_md=project / "成果文件" / "参考文献.md",
        reflib_log=project / "参考资料" / "reflib_download_log.csv",
        local_roots=[project / LOCAL_ROOT],
        literature_state=project / STATE_PATH,
        flat_dir=project / FLAT_DIR,
        audit_index=project / "_primer" / "references" / "index.csv",
    )


def test_the_whole_chain_resolves(project):
    result = _resolver(project).resolve(1)
    assert result.status == rs.STATUS_OK
    assert result.usable and not result.broken
    assert result.entry["title"] == "Ice giants in the outer solar system"
    assert result.source.local_file == f"{LOCAL_ROOT}/001_A_Ice_giants.pdf"
    assert result.source.markdown == f"{FLAT_DIR}/11111111_001_A_Ice_giants.md"
    assert result.source.md5 == "11111111111111111111111111111111"
    assert result.source.text_chars == len(BODY)


def test_the_flat_file_is_found_through_the_ledger_not_by_guessing_the_stem(project):
    """扁平产物的名字是 md5 前缀，只有读账本才拿得到——文件名里没有线索。"""
    flat = sorted((project / FLAT_DIR).glob("*.md"))
    assert [path.name for path in flat] == [
        "11111111_001_A_Ice_giants.md",
        "22222222_004_B_Habitable.md",
    ]
    result = _resolver(project).resolve(1)
    assert result.source.markdown.endswith("11111111_001_A_Ice_giants.md")


def test_entry_missing_is_reported_as_its_own_hop(project):
    result = _resolver(project).resolve(99)
    assert result.status == rs.STATUS_ENTRY_MISSING
    assert result.entry is None
    assert result.evidence == ["bibliography: no entry [99]"]


def test_no_local_file_names_the_evidence_it_looked_at(project):
    result = _resolver(project).resolve(2)
    assert result.status == rs.STATUS_NO_LOCAL_FILE
    assert result.entry is not None
    assert any("not_found" in line for line in result.evidence)
    assert result.evidence[-1] == "local: no usable local copy of [2]"


def test_a_pdf_without_a_ledger_record_breaks_at_not_converted(project):
    result = _resolver(project).resolve(3)
    assert result.status == rs.STATUS_NOT_CONVERTED
    assert any(line.startswith(f"local: chosen copy {LOCAL_ROOT}/003_B") for line in result.evidence)
    assert any("no done record for" in line for line in result.evidence)


def test_a_converted_file_with_too_little_text_breaks_at_empty_text(project):
    result = _resolver(project).resolve(4)
    assert result.status == rs.STATUS_EMPTY_TEXT
    assert any("only 9 chars of body" in line for line in result.evidence)


def test_a_running_record_is_not_mistaken_for_a_conversion(project):
    """转换进行中的 ``running`` 记录不算完成，链路照旧断在 ``not-converted``。"""
    state = project / STATE_PATH
    state.write_text(
        state.read_text(encoding="utf-8")
        + json.dumps(_state("33333333333333333333333333333333", f"{LOCAL_ROOT}/005_B_Title_five.pdf",
                            "33333333_005_B_Title_five", status="running"), ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )
    result = _resolver(project).resolve(5)
    assert result.status == rs.STATUS_NOT_CONVERTED


def test_a_shared_arxiv_id_does_not_cross_assign_local_files(project):
    """3/4/5 共享同一个 arXiv 号：按编号解析，每条都落到自己那份 PDF。"""
    resolver = _resolver(project)
    three = resolver.resolve(3)
    four = resolver.resolve(4)
    assert three.shared_arxiv == "1301.6674 is also claimed by [4, 5]"
    assert three.entry["arxiv"] == four.entry["arxiv"] == "1301.6674"
    assert three.evidence[-2].endswith("003_B_Habitable_zones.pdf")
    assert any("004_B_Habitable_zones_new_estimates.pdf" in line for line in four.evidence)
    assert four.status == rs.STATUS_EMPTY_TEXT
    assert four.source is None


def test_resolution_is_cached_per_number(project):
    resolver = _resolver(project)
    assert resolver.resolve(1) is resolver.resolve(1)


def test_audit_verdicts_are_carried_alongside_without_deciding_the_hop(project):
    index = project / "_primer" / "references" / "index.csv"
    index.parent.mkdir(parents=True, exist_ok=True)
    index.write_text(
        "number,verdict,confidence,note\n1,local,high,\n4,conflict,high,shared arXiv\n",
        encoding="utf-8",
    )
    result = _resolver(project).resolve(4)
    assert result.audit_verdict == "conflict"
    assert result.audit_confidence == "high"
    assert result.status == rs.STATUS_EMPTY_TEXT


def test_describe_inputs_points_at_absent_defaults(project):
    described = rs.describe_inputs(
        {"literature_state": project / STATE_PATH, "rescue_cache": None}, project
    )
    assert described["literature_state"] == {"path": STATE_PATH, "exists": True}
    assert described["rescue_cache"] == {"path": "-", "exists": False}


def test_dump_state_summary_counts_statuses_and_keeps_paths_relative(project):
    summary = rs.dump_state_summary(project / STATE_PATH, project)
    assert summary["records"] == 2
    assert summary["statuses"] == {"done": 2}
    assert summary["path"] == STATE_PATH
    assert rs.dump_state_summary(None) == {"path": None, "records": 0, "statuses": {}}


def test_a_bibliography_without_entries_is_an_error(tmp_path):
    path = tmp_path / "empty.md"
    path.write_text("# 空的\n\n没有条目。\n", encoding="utf-8")
    with pytest.raises(ValueError, match="no reference entries"):
        rs.load_entries(path, project_root=tmp_path)


def test_statuses_are_exactly_the_documented_five():
    assert rs.STATUSES == ("ok", "entry-missing", "no-local-file", "not-converted", "empty-text")
