# -*- coding: utf-8 -*-
"""后端与运行编排的单元测试：命令构造、分块分档、二分隔离、续跑与幂等。

真实 MinerU 不在单元测试里：每个用例用一段假 ``mineru-kit`` 脚本（写进
``tmp_path``）产出结构一致的 ``<stem>.zip``，并按需模拟"某个输入解析失败"、
"超时"与"归档带越界成员"三种故障。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from primer.literature.backends.mineru import CLI_TIERS, MineruBackend, to_cli_tier
from primer.literature.catalog import Citation, DocRecord, build_catalog
from primer.literature.config import TIERS, RunConfig, TierRule
from primer.literature.postprocess import HEADER_MARK, MODEL_OUTPUT_NAME
from primer.literature.report import render_run_plan, render_run_summary
from primer.literature.runner import count_pdf_pages, estimate_pages, plan_run, run_conversion
from primer.literature.stage import unique_stem
from primer.literature.state import DONE, FAILED, RUNNING, Ledger

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIR = REPO_ROOT / "src"
FAKE_MODEL_OUTPUT_BYTES = 4096

FAKE_MINERU = r'''#!/bin/bash
set -u
log="${FAKE_LOG:-/dev/null}"
out=""
tier=""
inputs=()
while [ $# -gt 0 ]; do
  case "$1" in
    parse) shift ;;
    -o|--output) out="$2"; shift 2 ;;
    -f|--format) shift 2 ;;
    --tier) tier="$2"; shift 2 ;;
    --ocr-mode) shift 2 ;;
    *) inputs+=("$1"); shift ;;
  esac
done
{
  printf 'argv: %s\n' "$*"
  printf 'env: MINERU_MODEL_SOURCE=%s\n' "${MINERU_MODEL_SOURCE:-unset}"
  printf 'tier: %s\n' "$tier"
} >> "$log"
if [ "${FAKE_SLEEP:-0}" != "0" ]; then sleep "$FAKE_SLEEP"; fi
mkdir -p "$out"
for src in "${inputs[@]}"; do
  stem="$(basename "$src")"
  stem="${stem%.pdf}"
  name="${stem#*_}"
  printf 'input: %s\n' "$name" >> "$log"
  failed=0
  for bad in ${FAKE_FAIL_STEMS:-}; do
    if [ "$bad" = "$name" ]; then failed=1; fi
  done
  if [ "$failed" = "1" ]; then
    echo "fake mineru: cannot parse $src (unsupported structure)" >&2
    exit 3
  fi
  if [ "${FAKE_ZIP_BROKEN:-0}" = "1" ]; then
    echo "this is not a zip archive" > "$out/$stem.zip"
    continue
  fi
  "$FAKE_PYTHON" - "$out/$stem.zip" "$stem" "${FAKE_ZIP_SLIP:-0}" "${tier:-unknown}" <<'PY'
import json
import sys
import zipfile

dest, stem, slip, tier = sys.argv[1], sys.argv[2], sys.argv[3] == "1", sys.argv[4]
pages = [{"page_idx": index} for index in range(2)]
with zipfile.ZipFile(dest, "w") as handle:
    handle.writestr("markdown.md", f"# {stem}\n\nbody of {stem}\n\n![](images/page_1_image_0.png)\n")
    handle.writestr("images/page_1_image_0.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    handle.writestr(f"images/{tier}_only.png", b"\x89PNG" + tier.encode())
    handle.writestr("middle_json.json", json.dumps({"pages": pages}))
    handle.writestr("structured_content.json", json.dumps({"pages": pages}))
    handle.writestr("model_output.json", "x" * 4096)
    if slip:
        handle.writestr("../escape.txt", "boom")
PY
  printf 'model_list infer finished, file_suffix=pdf, pages=2, cost=1.500000s, speed=1.333 page/s\n' >&2
done
echo "Parsed ${#inputs[@]} input(s)."
'''


@dataclass
class FakeMineru:
    """假后端：脚本路径、调用日志与语料目录。"""

    command: Path
    log: Path
    corpus: Path

    def calls(self) -> list[list[str]]:
        """按调用顺序返回每次调用的输入主干名。"""
        calls: list[list[str]] = []
        for line in self.lines():
            if line.startswith("argv:"):
                calls.append([])
            elif line.startswith("input:"):
                calls[-1].append(line.split(":", 1)[1].strip())
        return calls

    def lines(self) -> list[str]:
        if not self.log.is_file():
            return []
        return [line for line in self.log.read_text(encoding="utf-8").splitlines() if line]


@pytest.fixture
def fake_mineru(tmp_path, monkeypatch) -> FakeMineru:
    """装一个假 mineru-kit，并在语料目录里铺上五个假 PDF。"""
    script = tmp_path / "mineru-kit"
    script.write_text(FAKE_MINERU, encoding="utf-8")
    script.chmod(0o755)
    log = tmp_path / "calls.log"
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setenv("FAKE_PYTHON", sys.executable)
    for name in ("FAKE_FAIL_STEMS", "FAKE_ZIP_SLIP", "FAKE_ZIP_BROKEN", "FAKE_SLEEP"):
        monkeypatch.delenv(name, raising=False)

    corpus = tmp_path / "corpus"
    corpus.mkdir()
    for name in ("a.pdf", "b.pdf", "c.pdf", "d.pdf", "053_R_arxiv0811_3583.pdf"):
        (corpus / name).write_bytes(b"%PDF-1.4 fake " + name.encode())
    return FakeMineru(command=script, log=log, corpus=corpus)


def _config(fake: FakeMineru, tmp_path: Path, **kwargs) -> RunConfig:
    values = dict(
        roots=[fake.corpus],
        output_dir=tmp_path / "out",
        mineru_command=str(fake.command),
        chunk_size=2,
    )
    values.update(kwargs)
    return RunConfig(**values)


def _records(fake: FakeMineru) -> list[DocRecord]:
    return build_catalog([fake.corpus]).records


def _cell(table: str, label: str) -> str:
    """取出 markdown 表格里某一行最后一列的值。"""
    for line in table.splitlines():
        if line.startswith(f"| {label}"):
            return line.split("|")[-2].strip()
    raise AssertionError(f"no row for {label!r} in:\n{table}")


def test_to_cli_tier_passes_through_every_mineru_tier():
    for tier in CLI_TIERS:
        assert to_cli_tier(tier) == tier


def test_to_cli_tier_rejects_unknown_tier_and_lists_the_valid_ones():
    with pytest.raises(ValueError, match="unknown tier"):
        to_cli_tier("turbo")
    with pytest.raises(ValueError, match="flash, basic, standard, advanced"):
        to_cli_tier("fast")


def test_config_and_cli_share_one_tier_vocabulary():
    assert TIERS == CLI_TIERS


def test_backend_builds_argv_and_environment(tmp_path):
    staged = []
    for name in ("one", "two"):
        path = tmp_path / f"{name}.pdf"
        path.write_bytes(b"%PDF")
        staged.append(path)
    backend = MineruBackend(command="mineru-kit", model_source="modelscope", ocr_mode="auto")

    argv = backend.build_command(staged, tmp_path / "raw", "advanced")

    assert argv[:2] == ("mineru-kit", "parse")
    assert argv[argv.index("-f") + 1] == "zip"
    assert argv[argv.index("--tier") + 1] == "advanced"
    assert argv[argv.index("--ocr-mode") + 1] == "auto"
    assert argv[argv.index("-o") + 1] == str(tmp_path / "raw")
    assert str(staged[0]) in argv and str(staged[1]) in argv
    assert backend.build_env()["MINERU_MODEL_SOURCE"] == "modelscope"


def test_backend_refuses_an_empty_input_list(tmp_path):
    with pytest.raises(ValueError, match="at least one input"):
        MineruBackend().build_command([], tmp_path / "raw", "standard")


def test_backend_reports_unavailable_command_and_parse_failure(tmp_path):
    missing = MineruBackend(command=str(tmp_path / "nope"), timeout=None)

    assert missing.is_available() is False
    with pytest.raises(ValueError, match="command not found"):
        missing.parse([tmp_path / "a.pdf"], tmp_path / "raw", "standard")


def test_backend_collects_written_archives_and_engine_metrics(fake_mineru, tmp_path):
    backend = MineruBackend(command=str(fake_mineru.command))
    inputs = [fake_mineru.corpus / "a.pdf", fake_mineru.corpus / "b.pdf"]

    outcome = backend.parse(inputs, tmp_path / "raw", "standard")

    assert outcome.ok
    assert set(outcome.outputs) == set(inputs)
    assert set(outcome.outputs.values()) == {tmp_path / "raw" / "a.zip", tmp_path / "raw" / "b.zip"}
    assert outcome.pages == 4
    assert outcome.engine_seconds == pytest.approx(3.0)
    assert outcome.engine_speed == pytest.approx(4 / 3)
    assert "env: MINERU_MODEL_SOURCE=modelscope" in fake_mineru.lines()


def test_backend_returns_partial_outputs_and_an_error_tail(fake_mineru, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_FAIL_STEMS", "b")
    backend = MineruBackend(command=str(fake_mineru.command))
    inputs = [fake_mineru.corpus / name for name in ("a.pdf", "b.pdf")]

    outcome = backend.parse(inputs, tmp_path / "raw", "standard")

    assert outcome.ok is False
    assert outcome.returncode == 3
    assert set(outcome.outputs) == {inputs[0]}
    assert "cannot parse" in outcome.error


def test_backend_times_out_instead_of_hanging(fake_mineru, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_SLEEP", "5")
    backend = MineruBackend(command=str(fake_mineru.command), timeout=0.4)

    outcome = backend.parse([fake_mineru.corpus / "a.pdf"], tmp_path / "raw", "standard")

    assert outcome.returncode == -1
    assert "timed out after 0.4s" in outcome.error
    assert outcome.outputs == {}


def test_backend_accepts_a_per_call_timeout_override(fake_mineru, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_SLEEP", "5")
    backend = MineruBackend(command=str(fake_mineru.command), timeout=600)

    outcome = backend.parse(
        [fake_mineru.corpus / "a.pdf"], tmp_path / "raw", "standard", timeout=0.4
    )

    assert outcome.returncode == -1
    assert "timed out after 0.4s" in outcome.error


def test_estimate_pages_falls_back_to_file_size_for_unreadable_pdfs(fake_mineru):
    pages, from_size = estimate_pages(fake_mineru.corpus / "a.pdf")

    assert count_pdf_pages(fake_mineru.corpus / "a.pdf") is None
    assert from_size is True
    assert pages >= 1


def test_plan_run_splits_chunks_and_groups_by_tier(fake_mineru, tmp_path):
    config = _config(
        fake_mineru, tmp_path, chunk_size=3, tier_rules=[TierRule(glob="a*", tier="advanced")]
    )

    plan = plan_run(_records(fake_mineru), Ledger(config.state_path), config)

    assert len(plan.pending) == 5
    assert [len(chunk.records) for chunk in plan.chunks] == [3, 2]
    assert [len(chunk.groups) for chunk in plan.chunks] == [2, 1]
    assert [group.tier for group in plan.chunks[0].groups] == ["advanced", "standard"]
    assert plan.per_tier == {"standard": 4, "advanced": 1}
    assert plan.invocations == 3
    assert plan.estimated_from_size == 5
    assert (plan.done_already, plan.duplicates, plan.deferred) == (0, 0, 0)


def test_plan_run_honours_limit_and_reports_deferred(fake_mineru, tmp_path):
    config = _config(fake_mineru, tmp_path)

    plan = plan_run(_records(fake_mineru), Ledger(config.state_path), config, limit=2)

    assert len(plan.pending) == 2
    assert len(plan.chunks) == 1
    assert plan.deferred == 3


def test_plan_run_skips_duplicate_content(fake_mineru, tmp_path):
    (fake_mineru.corpus / "copy_of_a.pdf").write_bytes((fake_mineru.corpus / "a.pdf").read_bytes())
    config = _config(fake_mineru, tmp_path, chunk_size=10)

    plan = plan_run(_records(fake_mineru), Ledger(config.state_path), config)

    assert plan.duplicates == 1
    assert len(plan.pending) == 5


def test_run_conversion_processes_postprocesses_and_ledgers(fake_mineru, tmp_path):
    config = _config(fake_mineru, tmp_path, chunk_size=3)
    records = _records(fake_mineru)

    stats = run_conversion(config, records)

    assert (stats.processed, stats.failed) == (5, 0)
    assert stats.pages == 10
    assert stats.engine_pages == 10 and stats.engine_seconds == pytest.approx(7.5)
    assert stats.invocations == 2
    assert stats.failures == []
    assert stats.bytes_written > 0
    assert stats.dropped_bytes == 5 * FAKE_MODEL_OUTPUT_BYTES
    assert (stats.pages_per_second or 0) > 0
    assert len(fake_mineru.calls()) == 2
    assert sum(len(call) for call in fake_mineru.calls()) == 5

    stored = Ledger(config.state_path).load()
    assert len(stored) == 5
    for record in records:
        job = stored[record.md5]
        assert (job.status, job.tier) == (DONE, "standard")
        assert job.pages == 2
        assert job.output_bytes and job.output_bytes > 0
        assert job.model_output_bytes == FAKE_MODEL_OUTPUT_BYTES
        assert job.output_dir and Path(job.output_dir).is_dir()
        assert job.started_at and job.finished_at


def test_run_conversion_writes_flat_markdown_and_cleans_staging(fake_mineru, tmp_path):
    config = _config(fake_mineru, tmp_path, chunk_size=5)

    run_conversion(config, _records(fake_mineru))

    flat_files = sorted(path.name for path in config.flat_dir.glob("*.md"))
    assert len(flat_files) == 5
    flat = (config.flat_dir / flat_files[0]).read_text(encoding="utf-8")
    assert flat.startswith(HEADER_MARK)
    link = flat.split("](")[1].split(")")[0]
    assert (config.flat_dir / link).resolve().is_file()
    assert not config.staging_dir.exists()

    doc_dirs = [path for path in config.raw_dir.iterdir() if path.is_dir()]
    assert len(doc_dirs) == 5
    assert len(list(config.raw_dir.glob("*.zip"))) == 5
    for doc_dir in doc_dirs:
        assert (doc_dir / "markdown.md").is_file()
        assert not (doc_dir / MODEL_OUTPUT_NAME).exists()


def test_run_conversion_clears_stale_assets_when_the_tier_changes(fake_mineru, tmp_path):
    records = _records(fake_mineru)
    flash = _config(fake_mineru, tmp_path, chunk_size=5, tier="flash")
    standard = _config(fake_mineru, tmp_path, chunk_size=5, tier="standard")
    doc_dir = flash.raw_dir / unique_stem(records[0])

    run_conversion(flash, records)
    assert (doc_dir / "images" / "flash_only.png").is_file()

    run_conversion(standard, records)

    assert not (doc_dir / "images" / "flash_only.png").exists()
    assert (doc_dir / "images" / "standard_only.png").is_file()


def test_run_conversion_keeps_model_output_when_configured(fake_mineru, tmp_path):
    config = _config(fake_mineru, tmp_path, chunk_size=5, keep_model_output=True)

    stats = run_conversion(config, _records(fake_mineru))

    assert stats.dropped_bytes == 0
    doc_dirs = [path for path in config.raw_dir.iterdir() if path.is_dir()]
    assert len(doc_dirs) == 5
    for doc_dir in doc_dirs:
        assert (doc_dir / MODEL_OUTPUT_NAME).is_file()


def test_run_conversion_records_running_before_parse(fake_mineru, tmp_path):
    config = _config(fake_mineru, tmp_path, chunk_size=5)

    run_conversion(config, _records(fake_mineru))

    statuses = [
        json.loads(line)["status"]
        for line in config.state_path.read_text(encoding="utf-8").splitlines()
    ]
    assert statuses[:5] == [RUNNING] * 5
    assert statuses[5:] == [DONE] * 5


def test_run_conversion_resume_skips_everything_done(fake_mineru, tmp_path):
    config = _config(fake_mineru, tmp_path, chunk_size=5)
    run_conversion(config, _records(fake_mineru))
    calls_after_first = len(fake_mineru.calls())

    stats = run_conversion(config, _records(fake_mineru))
    second = run_conversion(config, _records(fake_mineru))

    assert (stats.processed, stats.failed, stats.invocations) == (0, 0, 0)
    assert stats.skipped_already_done == 5
    assert second.processed == 0
    assert len(fake_mineru.calls()) == calls_after_first
    assert stats.wall_seconds >= 0


def test_run_conversion_reruns_when_the_tier_changes(fake_mineru, tmp_path):
    config = _config(fake_mineru, tmp_path, chunk_size=5)
    records = _records(fake_mineru)
    run_conversion(config, records)

    stats = run_conversion(
        _config(fake_mineru, tmp_path, chunk_size=5, tier="flash"), records
    )

    assert stats.processed == 5
    assert stats.skipped_already_done == 0
    assert "tier: flash" in fake_mineru.lines()


def test_run_conversion_bisects_a_failing_chunk_down_to_one_file(fake_mineru, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_FAIL_STEMS", "c")
    config = _config(fake_mineru, tmp_path, chunk_size=5)
    records = _records(fake_mineru)

    stats = run_conversion(config, records)

    assert stats.processed == 4
    assert stats.failed == 1
    assert stats.failures[0][0] == "c.pdf"
    assert "cannot parse" in stats.failures[0][1]
    calls = fake_mineru.calls()
    # 假后端像 MinerU 一样在坏输入处立刻退出，因此这一次调用的日志止于 c。
    assert calls[0] == ["053_R_arxiv0811_3583", "a", "b", "c"]
    assert calls[1] == ["c"]
    assert calls[2] == ["d"]
    assert stats.invocations == 3

    ledger = Ledger(config.state_path).load()
    failed = ledger[records[3].md5]
    assert failed.status == FAILED and failed.error
    for record in records[:3]:
        assert ledger[record.md5].status == DONE


def test_run_conversion_stops_after_failure_unless_keep_going(fake_mineru, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_FAIL_STEMS", "a")
    records = _records(fake_mineru)

    stopped = run_conversion(_config(fake_mineru, tmp_path, chunk_size=2), records)

    assert (stopped.processed, stopped.failed) == (1, 1)
    assert stopped.invocations == 2
    assert all(not ({"b", "c", "d"} & set(call)) for call in fake_mineru.calls())

    fake_mineru.log.unlink()
    resumed = run_conversion(_config(fake_mineru, tmp_path, chunk_size=2), records, keep_going=True)

    assert (resumed.processed, resumed.failed) == (3, 1)
    assert resumed.invocations == 4
    assert len(resumed.failures) == 1
    assert any("c" in call for call in fake_mineru.calls())


def test_run_conversion_records_postprocess_failure_without_bisecting(
    fake_mineru, tmp_path, monkeypatch
):
    monkeypatch.setenv("FAKE_ZIP_SLIP", "1")
    config = _config(fake_mineru, tmp_path, chunk_size=2)

    stats = run_conversion(config, _records(fake_mineru), keep_going=True)

    assert stats.processed == 0
    assert stats.failed == 5
    assert stats.invocations == 3
    assert all("postprocess failed" in error for _, error in stats.failures)


def test_run_conversion_records_a_corrupt_archive_as_a_failure(fake_mineru, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_ZIP_BROKEN", "1")
    config = _config(fake_mineru, tmp_path, chunk_size=5)

    stats = run_conversion(config, _records(fake_mineru))

    assert (stats.processed, stats.failed, stats.invocations) == (0, 5, 1)
    assert all("postprocess failed" in error for _, error in stats.failures)


def test_run_conversion_rejects_non_zip_output_format(fake_mineru, tmp_path):
    config = _config(fake_mineru, tmp_path, output_format="md")

    with pytest.raises(ValueError, match="output_format"):
        run_conversion(config, _records(fake_mineru))


def test_run_conversion_reports_unavailable_backend(fake_mineru, tmp_path):
    config = _config(fake_mineru, tmp_path, mineru_command=str(tmp_path / "missing-mineru"))

    with pytest.raises(ValueError, match="not available"):
        run_conversion(config, _records(fake_mineru))


def test_run_summary_and_plan_render_expected_tables(fake_mineru, tmp_path):
    config = _config(fake_mineru, tmp_path)
    records = _records(fake_mineru)
    plan = plan_run(records, Ledger(config.state_path), config)

    plan_text = render_run_plan(plan, config=config, backend="mineru", backend_available=True)
    assert _cell(plan_text, "Files to process") == "5"
    assert _cell(plan_text, "Chunks") == "3"
    assert _cell(plan_text, "Backend invocations") == "3"
    assert _cell(plan_text, "Available") == "yes"
    assert "## Files per tier" in plan_text

    stats = run_conversion(config, records)
    summary = render_run_summary(stats, config=config)
    assert _cell(summary, "Processed") == "5"
    assert _cell(summary, "Skipped (already done)") == "0"
    assert _cell(summary, "Failed") == "0"
    assert _cell(summary, "Pages") == "10"
    assert re.fullmatch(r"\d+\.\d+", _cell(summary, "Effective pages/s"))
    assert "Bytes written" in summary
    assert "model_output.json dropped" in summary


def test_failure_summary_lists_the_error(fake_mineru, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_FAIL_STEMS", "a")
    config = _config(fake_mineru, tmp_path, chunk_size=4)

    stats = run_conversion(config, _records(fake_mineru), keep_going=True)

    assert "## Failures" in render_run_summary(stats, config=config)


def test_cli_run_dry_run_writes_nothing_and_then_converts(fake_mineru, tmp_path):
    out_dir = tmp_path / "out"
    config_path = tmp_path / "literature.yaml"
    config_path.write_text(
        f"roots:\n  - {fake_mineru.corpus}\n"
        f"output_dir: {out_dir}\n"
        f"mineru_command: {fake_mineru.command}\n"
        "chunk_size: 2\n",
        encoding="utf-8",
    )
    env = {**os.environ, "PYTHONPATH": str(SOURCE_DIR)}

    def run(*extra: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "primer.literature", "run", "--config", str(config_path), *extra],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(REPO_ROOT),
        )

    dry = run("--dry-run")

    assert dry.returncode == 0, dry.stderr
    assert "# Literature run plan" in dry.stdout
    assert _cell(dry.stdout, "Files to process") == "5"
    assert _cell(dry.stdout, "Chunks") == "3"
    assert _cell(dry.stdout, "Already done (same tier)") == "0"
    assert not out_dir.exists()
    assert fake_mineru.calls() == []

    real = run()

    assert real.returncode == 0, real.stderr
    assert "# Literature run summary" in real.stdout
    assert "[run] chunk 1/3 tier=standard" in real.stderr
    assert len(list((out_dir / "flat").glob("*.md"))) == 5

    again = run()

    assert again.returncode == 0, again.stderr
    assert _cell(again.stdout, "Processed") == "0"
    assert _cell(again.stdout, "Skipped (already done)") == "5"


def test_doc_record_citation_flows_into_the_flat_header(fake_mineru, tmp_path):
    config = _config(fake_mineru, tmp_path, chunk_size=5)
    records = [
        DocRecord(
            path=fake_mineru.corpus / "053_R_arxiv0811_3583.pdf",
            rel_path="053_R_arxiv0811_3583.pdf",
            size=100,
            md5="f" * 32,
            citation=Citation(ref="053", cls="R", title=None, arxiv="0811.3583"),
        )
    ]

    run_conversion(config, records)

    flat = list(config.flat_dir.glob("*.md"))[0].read_text(encoding="utf-8")
    assert flat.startswith(f"{HEADER_MARK} — ref 053 · class R")
    assert "> **arXiv** — 0811.3583" in flat
    assert "> **Source** — 053_R_arxiv0811_3583.pdf" in flat
