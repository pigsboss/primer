# -*- coding: utf-8 -*-
"""Ledger 的单元测试：JSONL 追加、最后一行为准、档位敏感与容错载入。"""

from primer.literature.state import (
    DONE,
    FAILED,
    PENDING,
    JobRecord,
    Ledger,
    summarize,
)


def _record(md5="a" * 32, status=PENDING, tier="standard", **kwargs):
    values = {
        "md5": md5,
        "rel_path": "sub/paper.pdf",
        "tier": tier,
        "status": status,
        "started_at": "2026-09-27T00:00:00",
        "finished_at": None,
        "seconds": 1.5,
        "output_dir": "raw/abc",
        "error": None,
    }
    values.update(kwargs)
    return JobRecord(**values)


def test_append_creates_parent_dirs_and_round_trips(tmp_path):
    ledger = Ledger(tmp_path / "nested" / "state.jsonl")
    record = _record(status=FAILED, error="boom", seconds=None)

    ledger.append(record)

    loaded = ledger.load()
    assert loaded == {record.md5: record}
    assert ledger.load()[record.md5].error == "boom"


def test_load_last_record_per_md5_wins(tmp_path):
    ledger = Ledger(tmp_path / "state.jsonl")
    ledger.append(_record(status=PENDING))
    ledger.append(_record(status=DONE, seconds=12.0))

    loaded = ledger.load()

    assert len(loaded) == 1
    assert loaded["a" * 32].status == DONE
    assert loaded["a" * 32].seconds == 12.0


def test_is_done_is_tier_sensitive(tmp_path):
    ledger = Ledger(tmp_path / "state.jsonl")
    ledger.append(_record(status=DONE, tier="standard"))

    assert ledger.is_done("a" * 32, "standard") is True
    assert ledger.is_done("a" * 32, "advanced") is False
    assert ledger.is_done("b" * 32, "standard") is False


def test_load_tolerates_truncated_last_line(tmp_path):
    path = tmp_path / "state.jsonl"
    ledger = Ledger(path)
    ledger.append(_record(status=DONE))
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"md5": "b' + "0" * 32 + '", "status": "run')

    loaded = ledger.load()

    assert list(loaded) == ["a" * 32]


def test_load_ignores_unparseable_and_blank_lines(tmp_path):
    path = tmp_path / "state.jsonl"
    path.write_text("not json\n\n[1, 2, 3]\n{}\n", encoding="utf-8")

    assert Ledger(path).load() == {}


def test_load_missing_file_is_empty(tmp_path):
    assert Ledger(tmp_path / "does-not-exist.jsonl").load() == {}


def test_summary_counts_every_status(tmp_path):
    ledger = Ledger(tmp_path / "state.jsonl")
    ledger.append(_record(md5="a" * 32, status=DONE))
    ledger.append(_record(md5="b" * 32, status=FAILED))
    ledger.append(_record(md5="c" * 32, status=FAILED))

    summary = summarize(ledger.load())

    assert summary[DONE] == 1
    assert summary[FAILED] == 2
    assert summary[PENDING] == 0
    assert set(summary) == {"pending", "running", "done", "failed", "skipped"}
