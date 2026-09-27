# -*- coding: utf-8 -*-
"""audit 的单元测试：逐条判定、置信度、证据行与工作区级问题清单。"""

import json
from pathlib import Path

from primer.references.audit import (
    VERDICT_CONFLICT,
    VERDICT_LOCAL,
    VERDICT_LOCAL_UNREGISTERED,
    VERDICT_MISSING,
    VERDICT_PUBLIC_WEB,
    run_audit,
)
from primer.references.entries import parse_markdown
from primer.references.ledger import build_ledger

REF_MD = """# 参考文献库

> 共 7 条
> 结构：A 战略 [1–3] / B 系外行星 [4–7]

## A 战略

[1] Alpha A. Title one. Journal 1, 1–2, 2001. ［原文：arXiv:1111.2222 已存本地］
[2] Beta B. Title two. arXiv:2202.0001, 2002. ［原文：待图书馆获取］
[3] Gamma C. Title three. Journal 3, 5–6, 2003. ［原文：NASA 官网公开］

## B 系外行星

[4] Delta D. Title four. Journal 4, 7–8, 2004. ［原文：arXiv:1301.6674 已存本地］
[5] Epsilon E. Title five. Journal 5, 9–10, 2005. ［原文：arXiv:1301.6674 已存本地］
[6] Zeta F. Title six. Journal 6, 11–12, 2006. ［原文：待图书馆获取］
[7] Eta G. Title seven. Journal 7, 13–14, 2007. ［原文：已存本地］
"""


def _workspace(tmp_path: Path):
    """搭一个微型工作区：文献表 + 7 个 PDF + 两份账本 + 抽取文本目录。"""
    refs = tmp_path / "refs"
    refs.mkdir()
    names = [
        "001_A_Title_one.pdf",
        "MISC_R_arxiv2202_0001.pdf",
        "004_B_Title_four.pdf",
        "005_B_Title_five.pdf",
        "MISC_R_arxiv3333_4444.pdf",
        "007_A_Title_seven.pdf",
    ]
    for name in names:
        (refs / name).write_bytes(b"%PDF-1.4 stub")
    reflib = tmp_path / "reflib_download_log.csv"
    reflib.write_text(
        "ref,class,title,arxiv,status,saved\n"
        "6,B,Title six,,no-arxiv,\n"
        f"7,B,Title seven,4444.5555,ok(1KB),{refs / '007_A_Title_seven.pdf'}\n",
        encoding="utf-8",
    )
    rescue = tmp_path / "arxiv_rescue_cache.json"
    rescue.write_text(
        json.dumps(
            {
                "2": {
                    "status": "rescued",
                    "arxiv": "2202.0001",
                    "file": "002_R_arxiv2202_0001.pdf",
                },
                "6": {
                    "status": "mismatch_misc",
                    "arxiv": "3333.4444",
                    "file": "006_R_arxiv3333_4444.pdf",
                },
            }
        ),
        encoding="utf-8",
    )
    text_root = tmp_path / "text"
    text_root.mkdir()
    (text_root / "001_A_Title_one.txt").write_text("extracted", encoding="utf-8")
    ledger = build_ledger(reflib_log=reflib, rescue_cache=rescue, local_roots=[refs])
    return ledger, text_root, reflib, rescue, refs


def _audit(tmp_path, **kwargs):
    ledger, text_root, _, _, _ = _workspace(tmp_path)
    kwargs.setdefault("text_root", text_root)
    return run_audit(parse_markdown(REF_MD, source="refs.md"), ledger, **kwargs)


def test_verdicts_and_confidence(tmp_path):
    result = _audit(tmp_path)
    verdicts = {audit.number: audit.verdict for audit in result.audits}
    confidence = {audit.number: audit.confidence for audit in result.audits}

    assert verdicts == {
        1: VERDICT_LOCAL,
        2: VERDICT_LOCAL_UNREGISTERED,
        3: VERDICT_PUBLIC_WEB,
        4: VERDICT_CONFLICT,
        5: VERDICT_CONFLICT,
        6: VERDICT_MISSING,
        7: VERDICT_LOCAL,
    }
    assert confidence[6] == "medium"
    assert confidence[4] == "high"


def test_every_entry_carries_named_evidence(tmp_path):
    result = _audit(tmp_path)
    audits = {audit.number: audit for audit in result.audits}

    assert any("001_A_Title_one.pdf" in item for item in audits[1].evidence)
    assert any("reflib_download_log.csv" in item for item in audits[7].evidence)
    assert any("arxiv_rescue_cache.json" in item for item in audits[2].evidence)
    assert audits[2].note.startswith("the bibliography still says")
    assert audits[3].note.startswith("tag 'NASA 官网公开'")
    assert "arXiv 1301.6674" in audits[4].note
    assert "1401" not in audits[4].note


def test_rescue_cache_resolves_by_arxiv_not_by_recorded_filename(tmp_path):
    result = _audit(tmp_path)
    audits = {audit.number: audit for audit in result.audits}

    assert audits[2].verdict == VERDICT_LOCAL_UNREGISTERED
    assert any(
        "MISC_R_arxiv2202_0001.pdf" in item and "recorded filename stale" in item
        for item in audits[2].evidence
    )


def test_mismatched_rescue_record_is_reported_but_not_accepted(tmp_path):
    result = _audit(tmp_path)
    audits = {audit.number: audit for audit in result.audits}

    assert audits[6].verdict == VERDICT_MISSING
    assert "could not be accepted" in audits[6].note
    assert result.problems.rescue_mismatches
    assert any("ref=6" in item for item in result.problems.rescue_mismatches)


def test_problem_lists(tmp_path):
    result = _audit(tmp_path)
    problems = result.problems

    assert [item.path.name for item in problems.unnumbered_files] == [
        "MISC_R_arxiv2202_0001.pdf",
        "MISC_R_arxiv3333_4444.pdf",
    ]
    assert [item.path.name for item in problems.unregistered_files] == [
        "MISC_R_arxiv3333_4444.pdf"
    ]
    assert problems.unnumbered_notes["MISC_R_arxiv2202_0001.pdf"].startswith("arXiv:2202.0001")
    assert problems.unregistered_files[0].path.name in problems.unnumbered_notes
    assert len(problems.conflicts) == 1
    assert "1301.6674" in problems.conflicts[0]
    assert len(problems.stale_rescue_paths) == 2
    assert all("recorded filename stale" in item for item in problems.stale_rescue_paths)
    assert any("ref=2" in item for item in problems.stale_rescue_paths)
    assert any("ref=6" in item for item in problems.stale_rescue_paths)
    assert [item for item in problems.claims_without_file if "[4]" in item]
    assert [item.path.name for item in problems.text_missing] == [
        "004_B_Title_four.pdf",
        "005_B_Title_five.pdf",
        "007_A_Title_seven.pdf",
        "MISC_R_arxiv2202_0001.pdf",
        "MISC_R_arxiv3333_4444.pdf",
    ]


def test_text_check_is_marked_unverified_without_text_root(tmp_path):
    result = _audit(tmp_path, text_root=None)

    assert result.problems.text_root is None
    assert result.problems.text_missing == []


def test_unnumbered_local_file_yields_local_unregistered(tmp_path):
    refs = tmp_path / "refs"
    refs.mkdir()
    (refs / "MISC_R_arxiv2101_0001.pdf").write_bytes(b"%PDF-1.4 stub")
    ledger = build_ledger(local_roots=[refs])

    result = run_audit(parse_markdown("[5] Alpha A. Title five. arXiv:2101.0001, 2021."), ledger)

    assert result.audits[0].verdict == VERDICT_LOCAL_UNREGISTERED
    assert "without a reference number" in result.audits[0].note


def test_reflib_ok_row_whose_saved_path_is_gone_is_not_usable(tmp_path):
    refs = tmp_path / "refs"
    refs.mkdir()
    log = tmp_path / "reflib_download_log.csv"
    log.write_text(
        "ref,class,title,arxiv,status,saved\n"
        f"9,A,Title nine,9999.0001,ok(1KB),{tmp_path / 'gone.pdf'}\n",
        encoding="utf-8",
    )
    ledger = build_ledger(reflib_log=log, local_roots=[refs])

    result = run_audit(parse_markdown("[9] Alpha A. Title nine. Journal 9, 2009."), ledger)

    assert result.audits[0].verdict == VERDICT_MISSING
    assert any("saved path missing on disk" in item for item in result.audits[0].evidence)


def test_counts_and_by_class(tmp_path):
    result = _audit(tmp_path)
    counts = result.counts()

    assert counts == {
        VERDICT_LOCAL: 2,
        VERDICT_LOCAL_UNREGISTERED: 1,
        VERDICT_PUBLIC_WEB: 1,
        VERDICT_MISSING: 1,
        VERDICT_CONFLICT: 2,
    }
    assert result.by_class()["A"] == {
        VERDICT_LOCAL: 1,
        VERDICT_LOCAL_UNREGISTERED: 1,
        VERDICT_PUBLIC_WEB: 1,
        VERDICT_MISSING: 0,
        VERDICT_CONFLICT: 0,
    }
    assert [audit.number for audit in result.missing()] == [6]


def test_audit_is_idempotent_when_run_twice_on_one_ledger(tmp_path):
    ledger, text_root, _, _, _ = _workspace(tmp_path)
    lists = parse_markdown(REF_MD, source="refs.md")

    first = run_audit(lists, ledger, text_root=text_root)
    second = run_audit(lists, ledger, text_root=text_root)

    assert first.counts() == second.counts()
    assert [len(audit.evidence) for audit in first.audits] == [
        len(audit.evidence) for audit in second.audits
    ]
