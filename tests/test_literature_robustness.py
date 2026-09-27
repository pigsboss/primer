# -*- coding: utf-8 -*-
"""生产事故四条防线的回归测试：stdin 隔离、调用时限、页数分块、账本状态机。

事故原样是：一次 ``run`` 的 20 个文件里塞了 2067 页，子进程继承了一个没人写的
stdin（后台任务框架的 socket），0% CPU 挂了 34 分钟；账本里留下 30 条 ``running``
（第一块 20 条 + 二分头一回又给前 10 个各写一条），既没有产出也没有进度行。

这里的假后端是一段可执行的 Python：它会**先读一次 stdin**（真 MinerU 就会读），
再按 ``<stem>.zip`` 产出结果；用环境变量控制"睡多久"与"哪个文件失败"，好把超时、
二分与打断都演出来。
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import pytest

from primer.literature.backends import MineruBackend
from primer.literature.catalog import DocRecord, build_catalog
from primer.literature.config import RunConfig
from primer.literature.report import render_run_plan
from primer.literature.runner import (
    TIMEOUT_FLOOR_SECONDS,
    PageBudget,
    build_chunks,
    invocation_timeout,
    page_span,
    plan_run,
    run_conversion,
)
from primer.literature.state import DONE, FAILED, RUNNING, JobRecord, Ledger
from primer.literature.stage import STALE_SOURCE_REASON

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIR = REPO_ROOT / "src"

STUB_BACKEND = '''#!/usr/bin/env python3
"""假 MinerU：照真 MinerU 的样子先读一次 stdin，再写出 <stem>.zip。"""
import json
import os
import sys
import time
import zipfile
from pathlib import Path

log = Path(os.environ.get("STUB_LOG", "/dev/null"))
args = sys.argv[1:]
output = Path(args[args.index("-o") + 1])
inputs = [Path(item) for item in args if item.endswith(".pdf")]

data = sys.stdin.read()
with log.open("a", encoding="utf-8") as handle:
    handle.write("argv: %s\\n" % " ".join(args))
    handle.write("stdin-eof: %s\\n" % (data == ""))

sleep_all = float(os.environ.get("STUB_SLEEP", "0") or 0)
sleep_stem = os.environ.get("STUB_SLEEP_STEM", "")
fail_stem = os.environ.get("STUB_FAIL_STEM", "")

for src in inputs:
    stem = src.stem
    name = stem.split("_", 1)[1] if "_" in stem else stem
    try:
        with open(src, "rb") as handle:
            handle.read(8)  # 真 MinerU 必须读得动输入，读不动就整批非零退出
    except OSError as exc:
        print("cannot read %s: %s" % (src, exc.strerror or exc), file=sys.stderr)
        sys.exit(3)
    if fail_stem and name == fail_stem:
        print("fake mineru: cannot parse %s (unsupported structure)" % src, file=sys.stderr)
        sys.exit(3)
    if sleep_all or (sleep_stem and name == sleep_stem):
        time.sleep(sleep_all or 30)
    with zipfile.ZipFile(output / ("%s.zip" % stem), "w") as handle:
        handle.writestr("markdown.md", "# %s\\n\\nbody of %s\\n" % (stem, stem))
        handle.writestr("images/page_1_image_0.png", b"\\x89PNG\\r\\n\\x1a\\n")
        handle.writestr("middle_json.json", json.dumps({"pages": [{}, {}]}))
        handle.writestr("structured_content.json", json.dumps({"pages": [{}, {}]}))
        handle.writestr("model_output.json", "x" * 512)
print("Parsed %d input(s)." % len(inputs))
'''


@dataclass
class Stub:
    """假后端：脚本路径与调用日志。"""

    command: Path
    log: Path

    def lines(self) -> list[str]:
        if not self.log.is_file():
            return []
        return [line for line in self.log.read_text(encoding="utf-8").splitlines() if line]

    def argv_lines(self) -> list[str]:
        """每次调用的 argv 行，按调用顺序。"""
        return [line.split(":", 1)[1].strip() for line in self.lines() if line.startswith("argv:")]


@pytest.fixture
def stub(tmp_path, monkeypatch) -> Stub:
    """装一个假 mineru-kit，并把它的日志指向 tmp_path。"""
    script = tmp_path / "stub-mineru"
    script.write_text(STUB_BACKEND, encoding="utf-8")
    script.chmod(0o755)
    fake = Stub(command=script, log=tmp_path / "stub.log")
    monkeypatch.setenv("STUB_LOG", str(fake.log))
    for name in ("STUB_SLEEP", "STUB_SLEEP_STEM", "STUB_FAIL_STEM"):
        monkeypatch.delenv(name, raising=False)
    return fake


def _corpus(tmp_path: Path, names: Sequence[str]) -> Path:
    corpus = tmp_path / "corpus"
    corpus.mkdir(parents=True, exist_ok=True)
    for name in names:
        (corpus / f"{name}.pdf").write_bytes(b"%PDF-1.4 stub " + name.encode())
    return corpus


def _config(corpus: Path, output_dir: Path, stub: Stub, **kwargs) -> RunConfig:
    values = dict(
        project_root=corpus.parent,
        roots=[corpus],
        output_dir=output_dir,
        mineru_command=str(stub.command),
        chunk_size=10,
    )
    values.update(kwargs)
    return RunConfig(**values)


def _records(corpus: Path) -> list[DocRecord]:
    return build_catalog([corpus]).records


def _raw_statuses(config: RunConfig, md5: str) -> list[str]:
    """账本文件里某个 md5 的**全部**状态（不是只看最后一条）。"""
    statuses = []
    for line in config.state_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        if payload["md5"] == md5:
            statuses.append(payload["status"])
    return statuses


def _all_raw_statuses(config: RunConfig) -> list[str]:
    return [
        json.loads(line)["status"]
        for line in config.state_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _write_cli_config(config: RunConfig) -> Path:
    """把假后端与时限写进一份 YAML，供子进程里的 CLI 读取。"""
    path = config.project_root / "literature-run.yaml"
    path.write_text(
        f"mineru_command: {config.mineru_command}\n"
        f"chunk_size: {config.chunk_size}\n"
        f"parse_timeout_seconds: {config.parse_timeout_seconds:g}\n",
        encoding="utf-8",
    )
    return path


def _cli_command(config: RunConfig, command: str = "run") -> list[str]:
    return [
        sys.executable,
        "-m",
        "primer.literature",
        command,
        "--project-root", str(config.project_root),
        "--root", str(config.roots[0]),
        "--output-dir", str(config.output_dir),
        "--config", str(_write_cli_config(config)),
    ]


def _cli_env(stub: Stub) -> dict[str, str]:
    return {**os.environ, "PYTHONPATH": str(SOURCE_DIR), "STUB_LOG": str(stub.log)}


# --- 1. stdin 隔离 ---------------------------------------------------------


def test_child_backend_never_waits_on_the_parent_stdin(tmp_path, stub):
    """fd 0 是没人写的管道时，子进程必须立刻拿到 EOF，而不是一直等到天荒地老。

    这里**不能**用 ``communicate()``：它会顺手把我们这一端的管道关掉，等于给子进程
    送了个 EOF，正好把要验的东西验没了。所以只 ``wait()``，让管道一直开着。
    """
    corpus = _corpus(tmp_path, ["a", "b"])
    output = tmp_path / "out"
    config = _config(corpus, output, stub, parse_timeout_seconds=8)

    started = time.monotonic()
    process = subprocess.Popen(
        _cli_command(config),
        stdin=subprocess.PIPE,  # 故意不写也不关：这正是生产里那个 socket 的行为
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_cli_env(stub),
        cwd=str(REPO_ROOT),
    )
    try:
        try:
            process.wait(timeout=25)
        except subprocess.TimeoutExpired:
            raise AssertionError("the run hung: the child inherited a stdin that never ends")
        elapsed = time.monotonic() - started
        stdout = process.stdout.read() if process.stdout else ""
        stderr = process.stderr.read() if process.stderr else ""
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        for pipe in (process.stdin, process.stdout, process.stderr):
            if pipe is not None:
                pipe.close()

    assert process.returncode == 0, f"{stdout}\n{stderr}"
    assert elapsed < 20, f"the run took {elapsed:.1f}s"
    assert list((output / "flat").glob("*.md"))
    assert "stdin-eof: True" in stub.lines()


# --- 2. 单次调用的时限 -----------------------------------------------------


def test_invocation_timeout_scales_with_pages_and_keeps_a_floor(tmp_path, stub):
    config = _config(_corpus(tmp_path, ["a"]), tmp_path / "out", stub)
    assert config.parse_timeout_seconds == 2400.0
    assert config.chunk_max_pages == 400

    assert invocation_timeout(config, 400) == pytest.approx(2400.0)  # 满额块
    assert invocation_timeout(config, 200) == pytest.approx(1200.0)  # 半块
    assert invocation_timeout(config, 40) == pytest.approx(TIMEOUT_FLOOR_SECONDS)  # 保底
    assert invocation_timeout(config, 1) == pytest.approx(TIMEOUT_FLOOR_SECONDS)
    # 页数未知（0）与超限块都取满额，而不是被压到最小的一档
    assert invocation_timeout(config, 0) == pytest.approx(2400.0)
    assert invocation_timeout(config, 529) == pytest.approx(2400.0)

    small = _config(_corpus(tmp_path, ["b"]), tmp_path / "out2", stub, parse_timeout_seconds=0.5)
    assert invocation_timeout(small, 1) == pytest.approx(0.5)  # 保底不超过配置的预算


def test_page_span_takes_the_larger_of_pages_and_the_size_based_estimate(tmp_path, stub):
    """生产里那份 45 MB / 57 页的报告：只看页数会把它判成 600 s 的最小预算。"""
    config = _config(_corpus(tmp_path, ["a"]), tmp_path / "out", stub)
    report_size = 47_153_360

    assert page_span(57, report_size, config) == 407  # 体积折算说 407 页，取它
    assert invocation_timeout(config, page_span(57, report_size, config)) == pytest.approx(
        config.parse_timeout_seconds
    )
    assert page_span(371, report_size, config) == 407  # 两者都大时仍取大的
    assert page_span(371, 10_000_000, config) == 371  # 页数更大就用页数
    assert page_span(2, 200_000, config) == 2
    assert page_span(0, report_size, config) == 407  # 页数未知但体积在
    assert page_span(0, 0, config) == config.chunk_max_pages  # 什么都不知道：满额块


def test_an_unknown_page_count_is_not_charged_the_smallest_budget(tmp_path, stub, monkeypatch):
    """页数问不出来时按满额预算走，不是按最小的一档。

    下限压到 2 s 才看得出差别：满额预算 10 s 容得下睡 3 s 的假后端，"按一页算"那一
    档只有 2 s。源文件是 0 字节，于是页数与体积都问不出来（编目时就只能这样记）。
    """
    monkeypatch.setattr("primer.literature.runner.TIMEOUT_FLOOR_SECONDS", 2.0)
    monkeypatch.setattr("primer.literature.runner.estimate_pages", lambda path: (0, True))
    monkeypatch.setenv("STUB_SLEEP", "3")
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "empty.pdf").write_bytes(b"")
    config = _config(corpus, tmp_path / "out", stub, parse_timeout_seconds=10)
    records = _records(corpus)
    assert records[0].size == 0  # 页数与体积都不知道

    stats = run_conversion(config, records)

    assert (stats.processed, stats.failed) == (1, 0)
    assert stats.failures == []


def test_a_timeout_error_names_the_pages_and_the_budget(tmp_path, stub, monkeypatch):
    """超时信息要说清这次调用算了几页、给了几秒，否则假超时与真挂死分不开。"""
    monkeypatch.setenv("STUB_SLEEP", "30")
    corpus = _corpus(tmp_path, ["a"])
    config = _config(corpus, tmp_path / "out", stub, parse_timeout_seconds=0.5)

    stats = run_conversion(config, _records(corpus))

    assert stats.failed == 1
    reason = stats.failures[0][1]
    assert "timed out after 0.5s" in reason
    assert "pages=1" in reason and "budget=0.5s" in reason


def test_a_timed_out_invocation_fails_the_culprit_and_spares_the_rest(tmp_path, stub, monkeypatch):
    """一个文件把整批拖死：二分把它单独扣出来，其余文件照常完成。"""
    monkeypatch.setenv("STUB_SLEEP_STEM", "slow")
    corpus = _corpus(tmp_path, ["slow", "quick"])
    config = _config(corpus, tmp_path / "out", stub, chunk_size=2, parse_timeout_seconds=0.5)
    records = _records(corpus)
    slow = next(record for record in records if record.rel_path.startswith("slow"))
    quick = next(record for record in records if record is not slow)

    stats = run_conversion(config, records)

    assert (stats.processed, stats.failed) == (1, 1)
    assert stats.failures[0][0] == slow.rel_path
    assert "timed out after 0.5s" in stats.failures[0][1]

    ledger = Ledger(config.state_path).load()
    assert ledger[slow.md5].status == FAILED
    assert ledger[quick.md5].status == DONE
    assert _raw_statuses(config, slow.md5) == [RUNNING, FAILED]
    assert _raw_statuses(config, quick.md5) == [RUNNING, DONE]


def test_a_whole_run_that_times_out_leaves_every_file_failed(tmp_path, stub, monkeypatch):
    monkeypatch.setenv("STUB_SLEEP", "30")
    corpus = _corpus(tmp_path, ["a", "b"])
    config = _config(corpus, tmp_path / "out", stub, chunk_size=2, parse_timeout_seconds=0.4)

    stats = run_conversion(config, _records(corpus))

    assert (stats.processed, stats.failed) == (0, 2)
    ledger = Ledger(config.state_path).load()
    assert {record.status for record in ledger.values()} == {FAILED}
    assert all("timed out after" in (record.error or "") for record in ledger.values())
    assert RUNNING not in _all_raw_statuses(config)[-1:]


def test_the_runner_passes_the_page_scaled_timeout_down_to_the_backend(tmp_path, stub, monkeypatch):
    """时限由编排层按页数算好再传下去；后端自己那个默认值不该说话。"""
    monkeypatch.setenv("STUB_SLEEP", "30")
    corpus = _corpus(tmp_path, ["a"])
    config = _config(corpus, tmp_path / "out", stub, parse_timeout_seconds=0.5)
    backend = MineruBackend(command=str(stub.command), timeout=600)

    started = time.monotonic()
    stats = run_conversion(config, _records(corpus), backend=backend)

    assert stats.failed == 1
    assert "timed out after 0.5s" in stats.failures[0][1]
    assert time.monotonic() - started < 20


# --- 3. 页数分块 -----------------------------------------------------------


def _page_records(pages: Sequence[int]) -> list[DocRecord]:
    return [
        DocRecord(
            path=Path(f"/corpus/{index:03d}.pdf"),
            rel_path=f"{index:03d}.pdf",
            size=10,
            md5=f"{index:032d}",
        )
        for index, _ in enumerate(pages)
    ]


def _budget_map(pages: Sequence[int]) -> dict[str, PageBudget]:
    """按 ``_page_records`` 的 md5 约定造一份页数依据表；页数 0 表示未知。"""
    return {f"{index:032d}": PageBudget(pages=page, span=page) for index, page in enumerate(pages)}


def test_build_chunks_caps_the_cumulative_page_count(tmp_path, stub):
    records = _page_records([150, 150, 150])
    config = _config(_corpus(tmp_path, ["a"]), tmp_path / "out", stub, chunk_max_pages=400)

    chunks = build_chunks(records, _budget_map([150, 150, 150]), config, lambda r: "standard")

    assert [len(chunk.records) for chunk in chunks] == [2, 1]
    assert [chunk.pages for chunk in chunks] == [300, 150]


def test_build_chunks_lets_a_single_giant_document_stand_alone(tmp_path, stub):
    records = _page_records([50, 529, 50])
    config = _config(_corpus(tmp_path, ["a"]), tmp_path / "out", stub, chunk_max_pages=400)

    chunks = build_chunks(records, _budget_map([50, 529, 50]), config, lambda r: "standard")

    assert [chunk.pages for chunk in chunks] == [50, 529, 50]


def test_build_chunks_gives_a_file_without_any_page_count_its_own_chunk(tmp_path, stub):
    """页数未知的文件按满额块算，于是它自成一"说不准"块，也不被别人带累。"""
    records = _page_records([10, 0, 10])
    config = _config(_corpus(tmp_path, ["a"]), tmp_path / "out", stub, chunk_max_pages=400)
    budgets = _budget_map([10, 0, 10])  # 页数 0 = 未知，budget_for 会把它顶到满额块

    chunks = build_chunks(records, budgets, config, lambda r: "standard")

    assert [chunk.pages for chunk in chunks] == [10, 400, 10]
    assert [chunk.unknown_pages for chunk in chunks] == [0, 1, 0]


def test_build_chunks_still_honours_the_file_count_cap(tmp_path, stub):
    records = _page_records([1] * 7)
    config = _config(
        _corpus(tmp_path, ["a"]), tmp_path / "out", stub, chunk_size=3, chunk_max_pages=400
    )

    chunks = build_chunks(records, _budget_map([1] * 7), config, lambda r: "standard")

    assert [len(chunk.records) for chunk in chunks] == [3, 3, 1]


def test_plan_reports_pages_per_chunk_and_where_the_page_counts_came_from(
    tmp_path, stub, monkeypatch
):
    corpus = _corpus(tmp_path, ["a", "b", "c"])
    config = _config(corpus, tmp_path / "out", stub, chunk_max_pages=4)
    pages = {"a": 3, "b": 3, "c": 0}
    # 与 pdfinfo 不可用时 estimate_pages 的真实返回一致：页数来自体积估计。
    monkeypatch.setattr(
        "primer.literature.runner.estimate_pages", lambda path: (pages[path.stem], True)
    )
    monkeypatch.setattr("primer.literature.runner.pdfinfo_available", lambda: False)

    plan = plan_run(_records(corpus), Ledger(config.state_path), config)
    text = render_run_plan(plan, config=config, backend="mineru", backend_available=True)

    assert plan.pdfinfo_available is False
    assert plan.unknown_pages == 1  # c.pdf 的页数根本数不出来
    assert [chunk.pages for chunk in plan.chunks] == [3, 4]  # c 的预算页数来自体积折算
    assert [chunk.unknown_pages for chunk in plan.chunks] == [0, 1]
    assert "## Chunks" in text
    assert "pdfinfo not installed: all 3 page counts estimated from file size" in text
    assert "1 of 3 have no page count at all" in text
    assert "4 (incl. 1 unknown-page file)" in text
    assert "Largest chunk: 4 budget pages across 2 files" in text


# --- 4. 账本状态机 ---------------------------------------------------------


def test_bisect_never_writes_a_second_running_record(tmp_path, stub, monkeypatch):
    """二分拿缺失子集重入，不能给同一个 md5 再写一条 running（生产里那 30 条）。"""
    monkeypatch.setenv("STUB_FAIL_STEM", "c")
    corpus = _corpus(tmp_path, ["a", "b", "c"])
    config = _config(corpus, tmp_path / "out", stub, chunk_size=3, parse_timeout_seconds=30)
    records = _records(corpus)

    stats = run_conversion(config, records)

    assert (stats.processed, stats.failed) == (2, 1)
    assert len(stub.argv_lines()) == 2  # 整批一次 + 二分重入一次
    for record in records:
        assert _raw_statuses(config, record.md5).count(RUNNING) == 1


def test_ledger_close_orphans_gives_every_stale_running_a_terminal_state(tmp_path):
    ledger = Ledger(tmp_path / "state.jsonl")
    ledger.append(JobRecord(md5="a" * 32, rel_path="a.pdf", tier="standard", status=RUNNING))
    ledger.append(JobRecord(md5="b" * 32, rel_path="b.pdf", tier="standard", status=DONE))

    closed = ledger.close_orphans("abandoned: left running by a previous run")

    assert closed == 1
    records = ledger.load()
    assert records["a" * 32].status == FAILED
    assert records["a" * 32].error.startswith("abandoned")
    assert records["b" * 32].status == DONE


def test_a_new_run_closes_orphans_left_by_a_previous_hard_kill(tmp_path, stub):
    corpus = _corpus(tmp_path, ["a"])
    config = _config(corpus, tmp_path / "out", stub)
    records = _records(corpus)
    config.state_path.parent.mkdir(parents=True, exist_ok=True)
    config.state_path.write_text(
        json.dumps(
            {
                "md5": records[0].md5,
                "rel_path": records[0].rel_path,
                "tier": "standard",
                "status": RUNNING,
                "started_at": "2026-09-27T00:00:00+00:00",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    run_conversion(config, records)

    assert _raw_statuses(config, records[0].md5) == [RUNNING, FAILED, RUNNING, DONE]
    assert Ledger(config.state_path).load()[records[0].md5].status == DONE


def test_sigterm_closes_open_running_records_and_exits_130(tmp_path, stub):
    corpus = _corpus(tmp_path, ["a", "b"])
    config = _config(corpus, tmp_path / "out", stub, chunk_size=2, parse_timeout_seconds=120)
    records = _records(corpus)
    env = {**_cli_env(stub), "STUB_SLEEP": "60"}

    process = subprocess.Popen(
        _cli_command(config),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        cwd=str(REPO_ROOT),
    )
    try:
        _wait_for_running(config, len(records))
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=30)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()

    assert process.returncode == 130, f"{stdout}\n{stderr}"
    assert "error: interrupted" in stderr
    ledger = Ledger(config.state_path).load()
    assert {record.status for record in ledger.values()} == {FAILED}
    assert all("interrupted" in (record.error or "") for record in ledger.values())
    for record in records:
        assert _raw_statuses(config, record.md5)[-1] == FAILED


def _wait_for_running(config: RunConfig, expected: int, timeout: float = 20.0) -> None:
    """等账本里出现 ``expected`` 条 running，再动手打断。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if config.state_path.is_file():
            if _all_raw_statuses(config).count(RUNNING) >= expected:
                return
        time.sleep(0.05)
    raise AssertionError("the run never opened a running record; nothing to interrupt")


# --- 5. 语料在编目之后被改动 --------------------------------------------------
#
# 编目是快照，几小时后才逐块暂存；这期间 kimi 工作侧可能正在改同一批文件（生产里
# 就是一次改名崩掉了一轮 3.8 小时的运行）。以下用例按"编目 → 改动 → 运行"的顺序
# 演这几种改动，要求只有被动的那一份失败。


def test_a_source_deleted_between_catalog_and_staging_does_not_kill_the_run(tmp_path, stub):
    corpus = _corpus(tmp_path, ["a", "b"])
    config = _config(corpus, tmp_path / "out", stub, chunk_size=10)
    records = _records(corpus)
    gone = next(record for record in records if record.rel_path == "a.pdf")
    (corpus / "a.pdf").unlink()  # 编目之后、暂存之前被删掉

    stats = run_conversion(config, records)

    assert (stats.processed, stats.failed) == (1, 1)
    assert stats.failures[0][0] == "a.pdf"
    assert stats.failures[0][1] == STALE_SOURCE_REASON
    ledger = Ledger(config.state_path).load()
    assert ledger[gone.md5].status == FAILED
    survivor = next(record for record in records if record is not gone)
    assert ledger[survivor.md5].status == DONE
    assert stub.argv_lines()  # 同块里另一个文件照常送进后端


def test_a_source_renamed_between_catalog_and_staging_does_not_kill_the_run(tmp_path, stub):
    corpus = _corpus(tmp_path, ["a", "b"])
    config = _config(corpus, tmp_path / "out", stub, chunk_size=10)
    records = _records(corpus)
    vanished = next(record for record in records if record.rel_path == "a.pdf")
    (corpus / "a.pdf").rename(corpus / "a-renamed.pdf")

    stats = run_conversion(config, records)

    assert (stats.processed, stats.failed) == (1, 1)
    assert stats.failures[0][0] == "a.pdf"
    assert stats.failures[0][1] == STALE_SOURCE_REASON
    assert Ledger(config.state_path).load()[vanished.md5].status == FAILED


def test_a_source_that_became_unreadable_only_fails_that_file(tmp_path, stub):
    """chmod 000：硬链接仍做得成（链接不看读权限），失败落在解析那一层。"""
    corpus = _corpus(tmp_path, ["a", "b"])
    config = _config(corpus, tmp_path / "out", stub, chunk_size=10)
    records = _records(corpus)
    locked = next(record for record in records if record.rel_path == "a.pdf")
    locked_path = corpus / "a.pdf"
    os.chmod(locked_path, 0o000)
    try:
        stats = run_conversion(config, records)
    finally:
        os.chmod(locked_path, 0o644)

    assert (stats.processed, stats.failed) == (1, 1)
    assert stats.failures[0][0] == "a.pdf"
    assert "cannot read" in stats.failures[0][1]
    ledger = Ledger(config.state_path).load()
    assert ledger[locked.md5].status == FAILED
    survivor = next(record for record in records if record is not locked)
    assert ledger[survivor.md5].status == DONE


def test_a_stale_source_stops_the_run_after_the_chunk_unless_keep_going(tmp_path, stub):
    corpus = _corpus(tmp_path, ["a", "b"])
    config = _config(corpus, tmp_path / "out", stub, chunk_size=1)
    records = _records(corpus)
    (corpus / "a.pdf").unlink()

    stopped = run_conversion(config, records)

    assert (stopped.processed, stopped.failed) == (0, 1)
    assert stub.argv_lines() == []  # 没有 keep_going：第二块根本没开跑

    resumed = run_conversion(config, records, keep_going=True)

    assert (resumed.processed, resumed.failed) == (1, 1)  # b 补上了
    assert stub.argv_lines()


def test_a_source_that_cannot_be_scanned_is_reported_not_fatal(tmp_path, stub):
    """扫描阶段读不动的文件（chmod 000 / 正被写）不能整轮崩掉，也不能悄悄消失。"""
    corpus = _corpus(tmp_path, ["a", "b"])
    config = _config(corpus, tmp_path / "out", stub, chunk_size=10)
    locked = corpus / "a.pdf"
    os.chmod(locked, 0o000)
    try:
        scan = subprocess.run(
            _cli_command(config, "scan"),
            capture_output=True,
            text=True,
            env=_cli_env(stub),
            cwd=str(REPO_ROOT),
            timeout=120,
        )
        run = subprocess.run(
            _cli_command(config, "run"),
            capture_output=True,
            text=True,
            env=_cli_env(stub),
            cwd=str(REPO_ROOT),
            timeout=120,
        )
    finally:
        os.chmod(locked, 0o644)

    assert scan.returncode == 0, f"{scan.stdout}\n{scan.stderr}"
    assert "## Unreadable at scan time" in scan.stdout
    assert "permission denied while scanning" in scan.stdout
    assert "1 unreadable at scan time, skipped" in scan.stderr

    assert run.returncode == 0, f"{run.stdout}\n{run.stderr}"
    assert "1 unreadable at scan time, skipped" in run.stderr
    ledger = Ledger(config.state_path).load()
    assert {record.status for record in ledger.values()} == {DONE}
    assert len(ledger) == 1  # 只有 b 进了账本，a 留在扫描报告里点名
