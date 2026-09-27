# -*- coding: utf-8 -*-
"""暂存与待办筛选的单元测试：唯名主干、硬链接、回退复制、逐份失败隔离、续跑筛选。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from primer.literature import stage as stage_module
from primer.literature.catalog import DocRecord
from primer.literature.config import TierRule
from primer.literature.state import DONE, JobRecord, Ledger
from primer.literature.stage import (
    NOT_A_FILE_REASON,
    STALE_SOURCE_REASON,
    cleanup,
    resolve_tier,
    select_pending,
    stage_batch,
    unique_stem,
)


def _record(name: str, md5: str = "a" * 32, rel_path: str | None = None, **kwargs) -> DocRecord:
    path = Path(name)
    return DocRecord(
        path=path,
        rel_path=rel_path if rel_path is not None else path.name,
        size=1,
        md5=md5,
        **kwargs,
    )


def test_unique_stem_sanitizes_and_prefixes_md5():
    record = _record("/tmp/weird name!?.pdf", md5="abcdef1234567890")

    assert unique_stem(record) == "abcdef12_weird_name"


def test_unique_stem_collapses_repeats():
    assert unique_stem(_record("/tmp/a   b.pdf")) == "a" * 8 + "_a_b"


def test_unique_stem_truncates_to_100_chars():
    stem = unique_stem(_record("/tmp/" + "x" * 200 + ".pdf"))

    assert stem == "a" * 8 + "_" + "x" * 100
    assert len(stem) == 109


def test_stage_batch_creates_hardlinks_and_unique_stems(tmp_path):
    source_a = tmp_path / "one" / "paper.pdf"
    source_b = tmp_path / "two" / "paper.pdf"
    for source, payload in ((source_a, b"one"), (source_b, b"two")):
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(payload)
    records = [_record(str(source_a), md5="1" * 32), _record(str(source_b), md5="2" * 32)]

    batch = stage_batch(records, tmp_path / "staging")

    assert batch.failures == []
    assert len({item.stem for item in batch.staged}) == 2
    assert len({item.link_path for item in batch.staged}) == 2
    for item in batch.staged:
        assert item.link_path.read_bytes() in {b"one", b"two"}
        assert os.path.samefile(item.record.path, item.link_path)


def test_stage_batch_merges_existing_directory(tmp_path):
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"content")
    record = _record(str(source))

    first = stage_batch([record], tmp_path / "staging").staged
    second = stage_batch([record], tmp_path / "staging").staged

    assert first[0].link_path == second[0].link_path
    assert second[0].link_path.read_bytes() == b"content"


def test_stage_batch_falls_back_to_copy(tmp_path, monkeypatch):
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"content")
    record = _record(str(source))

    def refuse(source_path, target_path):
        raise OSError("cross-device link")

    monkeypatch.setattr(stage_module.os, "link", refuse)

    staged = stage_batch([record], tmp_path / "staging").staged

    assert staged[0].link_path.read_bytes() == b"content"
    assert not os.path.samefile(source, staged[0].link_path)


def test_stage_batch_isolates_a_source_that_vanished_after_cataloguing(tmp_path):
    """编目是几小时前的事，那之后文件被改名/删除都属常态：只能失败这一份。"""
    gone = tmp_path / "renamed-away.pdf"
    gone.write_bytes(b"content")
    healthy = tmp_path / "paper.pdf"
    healthy.write_bytes(b"content")
    vanished = _record(str(gone), md5="1" * 32)
    kept = _record(str(healthy), md5="2" * 32)
    gone.unlink()

    batch = stage_batch([vanished, kept], tmp_path / "staging")

    assert [failure.record for failure in batch.failures] == [vanished]
    assert batch.failures[0].reason == STALE_SOURCE_REASON
    assert [item.record for item in batch.staged] == [kept]
    assert batch.staged[0].link_path.read_bytes() == b"content"


def test_stage_batch_reports_a_source_that_is_not_a_regular_file(tmp_path):
    """源被换成目录（或别的不是普通文件的东西）时，原因要说清是源的问题。"""
    source = tmp_path / "paper.pdf"
    source.mkdir()
    record = _record(str(source))

    batch = stage_batch([record], tmp_path / "staging")

    assert batch.staged == []
    assert batch.failures[0].reason == NOT_A_FILE_REASON


def test_stage_batch_reports_an_unreadable_source(tmp_path, monkeypatch):
    """硬链接与回退复制都失败时，原因要落在这一份文件上，而不是让整批炸掉。"""
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"content")
    record = _record(str(source))

    def denied(*args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(stage_module.os, "link", denied)
    monkeypatch.setattr(stage_module.shutil, "copy2", denied)

    batch = stage_batch([record], tmp_path / "staging")

    assert batch.staged == []
    assert batch.failures[0].reason == "unreadable source: permission denied at stage time"


def test_stage_batch_reports_an_unexpected_link_failure(tmp_path, monkeypatch):
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"content")
    record = _record(str(source))

    def broken(*args, **kwargs):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(stage_module.os, "link", broken)
    monkeypatch.setattr(stage_module.shutil, "copy2", broken)

    batch = stage_batch([record], tmp_path / "staging")

    assert batch.failures[0].reason == "cannot stage source: Input/output error"


def test_cleanup_removes_staged_files_and_directory(tmp_path):
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"content")
    staging = tmp_path / "staging"
    stage_batch([_record(str(source))], staging)

    removed = cleanup(staging)

    assert removed == 1
    assert not staging.exists()
    assert source.exists()


def test_select_pending_drops_done_and_duplicates(tmp_path):
    done_record = _record("/corpus/b.pdf", md5="1" * 32, rel_path="b.pdf")
    duplicate = _record("/corpus/a.pdf", md5="2" * 32, rel_path="a.pdf", duplicate_of=Path("/corpus/z.pdf"))
    rerun = _record("/corpus/c.pdf", md5="3" * 32, rel_path="c.pdf")
    ledger = Ledger(tmp_path / "state.jsonl")
    ledger.append(JobRecord(md5="1" * 32, rel_path="b.pdf", tier="standard", status=DONE))
    ledger.append(JobRecord(md5="3" * 32, rel_path="c.pdf", tier="advanced", status=DONE))

    pending = select_pending([done_record, duplicate, rerun], ledger, lambda r: "standard")

    assert [record.rel_path for record in pending] == ["c.pdf"]


def test_select_pending_reorders_deterministically_and_honours_limit(tmp_path):
    records = [
        _record(f"/corpus/{name}", md5=name * 8, rel_path=name) for name in ("c.pdf", "a.pdf", "b.pdf")
    ]
    ledger = Ledger(tmp_path / "state.jsonl")

    pending = select_pending(records, ledger, lambda r: "standard", limit=2)

    assert [record.rel_path for record in pending] == ["a.pdf", "b.pdf"]


def test_select_pending_reruns_on_tier_change(tmp_path):
    record = _record("/corpus/a.pdf", md5="1" * 32, rel_path="a.pdf")
    ledger = Ledger(tmp_path / "state.jsonl")
    ledger.append(JobRecord(md5="1" * 32, rel_path="a.pdf", tier="standard", status=DONE))

    assert select_pending([record], ledger, lambda r: "standard") == []
    assert select_pending([record], ledger, lambda r: "advanced") == [record]


def test_resolve_tier_uses_glob_rules():
    rules = [TierRule(glob="国际规划/**", tier="advanced")]
    matched = _record("/corpus/report.pdf", rel_path="国际规划/report.pdf")
    other = _record("/corpus/report.pdf", rel_path="参考文献原文/report.pdf")

    assert resolve_tier(matched, "standard", rules) == "advanced"
    assert resolve_tier(other, "standard", rules) == "standard"


@pytest.mark.parametrize("md5", ["", "short"])
def test_unique_stem_handles_short_md5(md5):
    stem = unique_stem(_record("/tmp/paper.pdf", md5=md5))

    assert stem.endswith("_paper")
