# -*- coding: utf-8 -*-
"""运行编排：编目结果 → 待办筛选 → 分块 → 暂存 → 调用后端 → 后处理 → 记账。

一次 ``run`` 的流程固定为：先由 :func:`plan_run` 算出分块与档位分布，再逐块
:func:`stage_batch` 暂存、按 **(块, 档位)** 成对调用一次后端（同一块里可能因为
``tier_rules`` 混着多个档位，而 MinerU 的 ``--tier`` 是整次调用的参数，所以必须
按档位拆开调用），最后逐个后处理并写账本。调用计划由 :func:`plan_run` 唯一地
产出，实际执行直接消费同一份分组，不在运行时二次分组。

分块有**两个**上限：文件数（``chunk_size``）与累计预算页数（``chunk_max_pages``）。
只看文件数是不够的——生产的语料里 20 个文件可能是 2067 页（含 529 页的 Ice
Giants），一块要跑一整夜，中途既没有产出也没有进度行，看起来就像挂了。页数上限
让每一块的预期墙钟时间大体相当；单个文档自己就超过上限时它自成一"超限块"。

时限：每次调用的墙钟时限由 :func:`invocation_timeout` 按该次调用的**预算页数**
（:func:`page_span`：页数与体积折算取大者，页数未知则按满额块）缩放，到点即算这次
调用失败（写盘的东西照收），再由下面的二分定位具体是哪个输入。超时的原因里带上
页数与预算，好让"假超时"与"真挂死"一眼可辨。

暂存隔离：语料是别人正在编辑的目录，编目之后、暂存之前文件可能已被改名或删除。
:func:`stage_batch` 逐份隔离失败，进不去的那份记一条 ``failed``（``stale source:
…``）并继续，同块其余文件照常跑；``keep_going`` 管的是"这一块之后还继续吗"。

失败隔离：批量调用里任意一个输入解析失败都会让 MinerU 整批非零退出，因此某次
调用只要有输入没产出归档，就对**缺失的那些**输入二分递归（成对拆半）直到单文件
调用，把失败定位到具体文件；这次调用中已经写出的归档照常收下。二分只作用在缺失
集合上，所以坏文件数量少时只多付 ``O(log n)`` 次调用（每次调用都有一次模型加载
的固定开销，故失败代价不低）。超时与阻塞走的是同一条路：整批没有产出，二分把
"环境慢"与"某个文件坏"区分开。

记账纪律：每次调用前先为参与的文件追加 ``running``（已经在跑的**不再**追加第二
条），产出归档并后处理成功后立刻追加 ``done``，失败追加 ``failed``（带日志尾巴）。
进程被 Ctrl-C / SIGTERM 打断时，未收尾的 ``running`` 会先补成 ``failed`` 再退出；
被 SIGKILL 硬杀留下的孤儿 ``running`` 由下一次开跑时的
:meth:`primer.literature.state.Ledger.close_orphans` 收尾，账本里因此不会出现解释
不清的"永远在跑"。错误信息来自后端，里面可能带着它自己写下的绝对路径，入账前一
律用 :func:`strip_root_prefix` 抹掉工程根前缀。
"""

from __future__ import annotations

import re
import shutil
import signal
import subprocess
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from .backends import ExtractionBackend, MineruBackend, ParseOutcome
from .catalog import DocRecord
from .config import RunConfig
from .paths import relative_to_root, strip_root_prefix
from .postprocess import postprocess_document
from .stage import StagedDoc, cleanup, resolve_tier, select_pending, stage_batch
from .state import DONE, FAILED, RUNNING, JobRecord, Ledger, now_iso

# 仅在 pdfinfo 不可用时才用到的兜底估计：实测语料 1.5 GiB / 13821 页的均值。
BYTES_PER_PAGE = 115_717
PDFINFO_TIMEOUT_SECONDS = 30.0
# 单次调用的时限下限：小调用里模型加载占大头，实测一页文件也要十几秒，留足一分钟级
# 的余地，避免把"本来就慢的小调用"误判成挂死。
TIMEOUT_FLOOR_SECONDS = 600.0
# 被 Ctrl-C / SIGTERM 打断时写给未收尾文件的原因。
INTERRUPTED_ERROR = "interrupted: the run was stopped before this file produced output"
# 上一次运行被硬杀（SIGKILL / 断电）后遗留的 running，由下一次开跑时收尾。
ABANDONED_ERROR = "abandoned: a previous run left this file in the running state"


@dataclass(frozen=True)
class TierGroup:
    """一块内生效档位相同的若干文件，对应一次后端调用。"""

    tier: str
    records: list[DocRecord]


@dataclass(frozen=True)
class PageBudget:
    """一份文档的页数依据。

    ``pages`` 是估算页数，**0 表示页数未知**（``pdfinfo`` 与 ``stat`` 都问不出来）；
    ``span`` 是分块与时限里按它计的页数，保守取值（见 :func:`page_span`）。
    两者分开是因为"一页"与"不知道多少页"必须走不同的预算：前者可以给最小的一档，
    后者只能往多了给。
    """

    pages: int
    span: int

    @property
    def unknown(self) -> bool:
        """页数是否未知。"""
        return self.pages <= 0


@dataclass(frozen=True)
class Chunk:
    """一个暂存批次：一次暂存，按档位拆成若干次调用。

    ``pages`` 是该块的**预算页数**合计（见 :func:`page_span`），``unknown_pages``
    是有几份文档页数未知——两者一起决定这次调用的时限，也一起报给 ``--dry-run``。
    """

    groups: list[TierGroup]
    pages: int = 0
    unknown_pages: int = 0

    @property
    def records(self) -> list[DocRecord]:
        return [record for group in self.groups for record in group.records]


@dataclass(frozen=True)
class RunPlan:
    """一次 ``run`` 的静态计划（不写任何文件）。"""

    pending: list[DocRecord]
    chunks: list[Chunk]
    per_tier: dict[str, int]
    estimated_pages: int
    estimated_from_size: int
    done_already: int
    duplicates: int
    deferred: int
    budgets_by_md5: dict[str, PageBudget] = field(default_factory=dict)
    unknown_pages: int = 0
    pdfinfo_available: bool = True

    @property
    def invocations(self) -> int:
        """计划内的后端调用次数 = 各块的档位分组数之和。"""
        return sum(len(chunk.groups) for chunk in self.chunks)


@dataclass
class RunStats:
    """一次 ``run`` 的实测统计。"""

    processed: int = 0
    failed: int = 0
    skipped_already_done: int = 0
    duplicates: int = 0
    deferred: int = 0
    pages: int = 0
    bytes_written: int = 0
    dropped_bytes: int = 0
    wall_seconds: float = 0.0
    invocations: int = 0
    engine_pages: int = 0
    engine_seconds: float = 0.0
    per_tier: dict[str, int] = field(default_factory=dict)
    failures: list[tuple[str, str]] = field(default_factory=list)

    @property
    def pages_per_second(self) -> Optional[float]:
        """按整段墙钟时间计算的实际吞吐（含模型加载与后处理）。"""
        return self.pages / self.wall_seconds if self.wall_seconds > 0 else None

    @property
    def engine_speed(self) -> Optional[float]:
        """引擎日志自报的纯推理吞吐，用以区分固定加载开销与按页开销。"""
        return self.engine_pages / self.engine_seconds if self.engine_seconds > 0 else None


@dataclass(frozen=True)
class ProgressLine:
    """一个 (块, 档位) 调用完成后的进度行。"""

    chunk_index: int
    chunk_total: int
    tier: str
    files: int
    pages: int
    failed: int
    elapsed: float


def tier_of(record: DocRecord, config: RunConfig) -> str:
    """该文件本次运行生效的档位（``tier_rules`` 命中则覆盖默认档位）。"""
    return resolve_tier(record, config.tier, config.tier_rules)


def plan_run(
    records: Sequence[DocRecord],
    ledger: Ledger,
    config: RunConfig,
    *,
    limit: Optional[int] = None,
) -> RunPlan:
    """算出待办清单、分块与档位分布；只读账本与文件系统，不写任何东西。"""
    tier_for = _tier_for(config)
    candidates = select_pending(records, ledger, tier_for)
    pending = candidates if limit is None else candidates[:limit]
    duplicates = sum(1 for record in records if record.duplicate_of is not None)
    done_already = len(records) - duplicates - len(candidates)

    per_tier: dict[str, int] = {}
    budgets_by_md5: dict[str, PageBudget] = {}
    estimated_pages = 0
    estimated_from_size = 0
    unknown_pages = 0
    for record in pending:
        tier = tier_for(record)
        per_tier[tier] = per_tier.get(tier, 0) + 1
        pages, from_size = estimate_pages(record.path)
        if pages <= 0:
            unknown_pages += 1
        budgets_by_md5[record.md5] = PageBudget(
            pages=pages, span=page_span(pages, record.size, config)
        )
        estimated_pages += pages
        estimated_from_size += 1 if from_size else 0

    return RunPlan(
        pending=pending,
        chunks=build_chunks(pending, budgets_by_md5, config, tier_for),
        per_tier=per_tier,
        estimated_pages=estimated_pages,
        estimated_from_size=estimated_from_size,
        done_already=done_already,
        duplicates=duplicates,
        deferred=len(candidates) - len(pending),
        budgets_by_md5=budgets_by_md5,
        unknown_pages=unknown_pages,
        pdfinfo_available=pdfinfo_available(),
    )


def build_chunks(
    pending: Sequence[DocRecord],
    budgets: Mapping[str, PageBudget],
    config: RunConfig,
    tier_for: Callable[[DocRecord], str],
) -> list[Chunk]:
    """按文件数与累计**预算页数**双上限贪心分块，块内再按档位拆成若干次调用。

    顺序就是 ``pending`` 的顺序（已按 ``rel_path`` 排好），因此分块完全可复现。
    单个文档自己就超过页数上限时它自成一"超限块"：页数上限是给批量调用加的闸，
    不是把大文档拒之门外；页数未知的文档同样自成一"说不准"块。
    """
    chunks: list[Chunk] = []
    block: list[DocRecord] = []
    block_pages = 0
    block_unknown = 0
    for record in pending:
        budget = budget_for(budgets, record.md5, config)
        if block and (
            len(block) >= config.chunk_size or block_pages + budget.span > config.chunk_max_pages
        ):
            chunks.append(_chunk_of(block, block_pages, block_unknown, tier_for))
            block, block_pages, block_unknown = [], 0, 0
        block.append(record)
        block_pages += budget.span
        block_unknown += 1 if budget.unknown else 0
    if block:
        chunks.append(_chunk_of(block, block_pages, block_unknown, tier_for))
    return chunks


def _chunk_of(
    block: Sequence[DocRecord],
    pages: int,
    unknown_pages: int,
    tier_for: Callable[[DocRecord], str],
) -> Chunk:
    """把一块文件按生效档位分组，封成一个 :class:`Chunk`。"""
    grouped: dict[str, list[DocRecord]] = {}
    for record in block:
        grouped.setdefault(tier_for(record), []).append(record)
    return Chunk(
        groups=[TierGroup(tier=tier, records=grouped[tier]) for tier in sorted(grouped)],
        pages=pages,
        unknown_pages=unknown_pages,
    )


def unknown_page_budget(config: RunConfig) -> PageBudget:
    """页数未知时用的依据：按满额块算，于是时限也取满额。"""
    return PageBudget(pages=0, span=config.chunk_max_pages)


def budget_for(budgets: Mapping[str, PageBudget], md5: str, config: RunConfig) -> PageBudget:
    """取某文件的页数依据；查不到或没有可用页数时按"未知"处理。

    这是"页数凭什么算"的唯一入口（分块与时限都走它），因此"查不到"与"有记录但
    ``span <= 0``"必须是同一种处理：都按满额块算——宁可多等，也别拿最小的预算去跑
    一个说不准多大的文件。
    """
    budget = budgets.get(md5)
    if budget is None or budget.span <= 0:
        return unknown_page_budget(config)
    return budget


def page_span(pages: int, size: int, config: RunConfig) -> int:
    """一份文档在分块与时限里算几页：取页数与体积折算里**大**的那个。

    时限是"挂死探测器"而不是成本估算：给窄了会把好文件误判成坏的，给宽了只是晚
    一点开始二分。实测语料均值 115 KB/页，图片密集的报告能到 0.8 MB/页，于是同一份
    文档"按页数算"和"按体积算"能差一个数量级——生产里那份 45 MB 的 planet2023.pdf
    pdfinfo 只报 57 页，只按页数算就落到了 600 s 的最小预算，对一份 0.8 MB/页的
    报告来说这个预算没有依据。两者都问不出来时按满额块算。
    """
    by_size = max(1, round(size / BYTES_PER_PAGE)) if size > 0 else 0
    if pages <= 0 and by_size <= 0:
        return config.chunk_max_pages
    return max(pages, by_size)


def invocation_timeout(config: RunConfig, pages: int) -> float:
    """一次后端调用的墙钟时限：按页数缩放 ``parse_timeout_seconds``，并保底。

    ``parse_timeout_seconds`` 是"满额块"（``chunk_max_pages`` 页）的预算：页数已知
    时按比例缩放，好让二分定位的子集只等一半时间，否则一次挂死会被二分的每一层各
    自放大一遍；``pages <= 0`` 表示页数未知，直接给满额——宁可多等，也不能拿最小的
    一档去跑一个说不准多大的文件。下限 ``TIMEOUT_FLOOR_SECONDS`` 保证小调用也有模型
    加载与最小推理的时间，但不会超过配置的满额预算。
    """
    if pages <= 0:
        return config.parse_timeout_seconds
    scaled = config.parse_timeout_seconds * pages / config.chunk_max_pages
    floor = min(TIMEOUT_FLOOR_SECONDS, config.parse_timeout_seconds)
    return max(floor, min(config.parse_timeout_seconds, scaled))


def run_conversion(
    config: RunConfig,
    records: Sequence[DocRecord],
    *,
    backend: Optional[ExtractionBackend] = None,
    limit: Optional[int] = None,
    keep_going: bool = False,
    on_progress: Optional[Callable[[ProgressLine], None]] = None,
) -> RunStats:
    """执行转换，返回统计；``keep_going`` 为假时遇到失败即停止后续分块。

    单份文件的任何问题都**只波及它自己**：暂存不上（源被改名/删除、读不了）记一条
    ``failed`` 就走，同块其余文件照常跑；``keep_going`` 决定的是"这一块之后还继续
    吗"，不是"这一块要不要整个放弃"。被 Ctrl-C / SIGTERM 打断时，先把还没收尾的
    ``running`` 补成 ``failed`` 再抛出，因此账本在任何时刻都是可解释的（硬杀的孤儿
    由下一次开跑时收尾）。
    """
    if config.output_format != "zip":
        raise ValueError("run requires output_format: zip (the postprocess step needs the archive)")
    active = backend if backend is not None else backend_from_config(config)
    if not active.is_available():
        raise ValueError(f"extraction backend {active.name!r} is not available in this environment")

    config.raw_dir.mkdir(parents=True, exist_ok=True)
    config.flat_dir.mkdir(parents=True, exist_ok=True)
    ledger = Ledger(config.state_path)
    ledger.close_orphans(ABANDONED_ERROR)
    plan = plan_run(records, ledger, config, limit=limit)
    stats = RunStats(
        skipped_already_done=plan.done_already,
        duplicates=plan.duplicates,
        deferred=plan.deferred,
    )
    session = _Session(
        config=config,
        backend=active,
        ledger=ledger,
        stats=stats,
        budgets=plan.budgets_by_md5,
    )
    started = time.monotonic()
    total_chunks = len(plan.chunks)
    previous_handler = _install_sigterm_handler()
    try:
        for index, chunk in enumerate(plan.chunks, start=1):
            batch = stage_batch(chunk.records, config.staging_dir)
            for failure in batch.failures:
                session.fail_unstaged(failure.record, failure.reason)
            staged = {item.record.md5: item for item in batch.staged}
            try:
                for group in chunk.groups:
                    items = [
                        staged[record.md5] for record in group.records if record.md5 in staged
                    ]
                    if not items:
                        continue
                    before_pages, before_failed = stats.pages, stats.failed
                    group_started = time.monotonic()
                    session.run_group(items, group.tier)
                    if on_progress is not None:
                        on_progress(
                            ProgressLine(
                                chunk_index=index,
                                chunk_total=total_chunks,
                                tier=group.tier,
                                files=len(items),
                                pages=stats.pages - before_pages,
                                failed=stats.failed - before_failed,
                                elapsed=time.monotonic() - group_started,
                            )
                        )
            finally:
                cleanup(config.staging_dir)
            if stats.failed and not keep_going:
                break
    except KeyboardInterrupt:
        # 先把"再次被打断"的机会按原样还回去，再收尾未完成的记录。
        _restore_sigterm_handler(previous_handler)
        session.abandon_open(INTERRUPTED_ERROR)
        raise
    finally:
        _restore_sigterm_handler(previous_handler)
        stats.wall_seconds = time.monotonic() - started
    return stats


def _install_sigterm_handler() -> Optional[Any]:
    """把 SIGTERM 变成 ``KeyboardInterrupt``，返回原来的处理器（装不上时返回 ``None``）。

    默认的 SIGTERM 会当场终止进程，来不及给未完成的文件补终态，账本里就会留下
    解释不清的 ``running``。装不上（非主线程）时照常运行：那种场合由下一次开跑时
    的孤儿收尾兜底。
    """
    try:
        previous = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGTERM, _raise_interrupt)
    except (ValueError, OSError):
        return None
    return previous


def _restore_sigterm_handler(previous: Optional[Any]) -> None:
    """还原 SIGTERM 处理器；``None`` 表示当初就没装上。"""
    if previous is None:
        return
    try:
        signal.signal(signal.SIGTERM, previous)
    except (ValueError, OSError):
        pass


def _raise_interrupt(signum: int, frame: Any) -> None:
    """信号处理器：把 SIGTERM 走成和 Ctrl-C 一样的路径。"""
    raise KeyboardInterrupt


def count_pdf_pages(path: Path) -> Optional[int]:
    """用 ``pdfinfo`` 数页；不可用或解析失败时返回 ``None``。"""
    if not pdfinfo_available():
        return None
    try:
        completed = subprocess.run(
            ["pdfinfo", str(path)],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=PDFINFO_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"^Pages:\s+(\d+)", completed.stdout or "", re.MULTILINE)
    return int(match.group(1)) if match else None


def pdfinfo_available() -> bool:
    """``pdfinfo`` 是否可用；不可用时页数只能按体积估，计划里要说明这一点。"""
    return shutil.which("pdfinfo") is not None


def estimate_pages(path: Path) -> tuple[int, bool]:
    """估算页数，返回 ``(页数, 是否来自体积估算)``。

    ``pdfinfo`` 拿不到精确页数时才退回按体积折算；两个来源都拿不到（源文件不在、
    读不了，或长度为零）时返回 **0**，表示"页数未知"。0 必须与"一页"分开：未知的
    文档走满额预算，而按一页算会让它落到最小的那一档。
    """
    exact = count_pdf_pages(path)
    if exact is not None:
        return exact, False
    try:
        size = Path(path).stat().st_size
    except OSError:
        size = 0
    if size <= 0:
        return 0, True
    return max(1, round(size / BYTES_PER_PAGE)), True


def backend_from_config(config: RunConfig) -> MineruBackend:
    """按配置构造默认后端（MinerU CLI）。"""
    return MineruBackend(
        command=config.mineru_command,
        model_source=config.model_source,
        ocr_mode=config.ocr_mode,
        extra_args=config.extra_args,
        timeout=config.parse_timeout_seconds,
    )


@dataclass(frozen=True)
class _OpenJob:
    """一条已经写下 ``running``、还没写终态的作业。"""

    rel_path: str
    tier: str
    started_at: str


class _Session:
    """一次运行的可变状态：只在这里写账本与统计。"""

    def __init__(
        self,
        *,
        config: RunConfig,
        backend: ExtractionBackend,
        ledger: Ledger,
        stats: RunStats,
        budgets: Optional[Mapping[str, PageBudget]] = None,
    ):
        self.config = config
        self.backend = backend
        self.ledger = ledger
        self.stats = stats
        self._budgets = dict(budgets or {})
        self._open: dict[str, _OpenJob] = {}

    def run_group(self, items: Sequence[StagedDoc], tier: str) -> None:
        """处理一个 (块, 档位) 分组，必要时二分到单文件。"""
        self._invoke(list(items), tier)

    def fail_unstaged(self, record: DocRecord, reason: str) -> None:
        """记录一份连暂存都没过的文件。

        它从没交给过后端，所以没有（也不该有）``running`` 记录，直接落一条 ``failed``。
        """
        self._fail_record(record.md5, record.rel_path, tier_of(record, self.config), reason, None)

    def abandon_open(self, error: str) -> int:
        """把还没收尾的 ``running`` 补成 ``failed``，返回补了几条。"""
        jobs = list(self._open.items())
        for md5, job in jobs:
            self._fail_record(md5, job.rel_path, job.tier, error, None)
        return len(jobs)

    def _invoke(self, items: list[StagedDoc], tier: str) -> None:
        if not items:
            return
        for item in items:
            self._start(item, tier)
        budgets = [self._budget_of(item.record.md5) for item in items]
        pages = sum(budget.span for budget in budgets)
        unknown = sum(1 for budget in budgets if budget.unknown)
        timeout = invocation_timeout(self.config, pages)
        outcome = self.backend.parse(
            [item.link_path for item in items],
            self.config.raw_dir,
            tier,
            timeout=timeout,
        )
        self.stats.invocations += 1
        if outcome.pages is not None:
            self.stats.engine_pages += outcome.pages
        if outcome.engine_seconds is not None:
            self.stats.engine_seconds += outcome.engine_seconds

        produced = [item for item in items if item.link_path in outcome.outputs]
        missing = [item for item in items if item.link_path not in outcome.outputs]
        share = outcome.seconds / len(items)
        for item in produced:
            self._finish(item, tier, outcome.outputs[item.link_path], share)
        if not missing:
            return
        if len(items) == 1:
            self._fail(
                items[0],
                tier,
                self._failure_reason(outcome, pages, unknown, timeout),
                outcome.seconds,
            )
            return

        middle = len(missing) // 2
        self._invoke(missing[:middle], tier)
        self._invoke(missing[middle:], tier)

    def _budget_of(self, md5: str) -> PageBudget:
        """该文件的页数依据；查不到（比如计划里没有它）就当"页数未知"处理。"""
        return budget_for(self._budgets, md5, self.config)

    def _failure_reason(
        self, outcome: ParseOutcome, pages: int, unknown: int, timeout: float
    ) -> str:
        """把后端给的原因补上这次调用的上下文。

        超时必须能一眼分辨"假超时"（预算给窄了）与"真挂死"：写清这次调用算了几页、
        给了几秒、页数是不是估的。没有这些数字，看到 ``timed out`` 只能靠猜。
        """
        reason = (
            f"rc={outcome.returncode}: {outcome.error}"
            if outcome.error
            else f"rc={outcome.returncode}: no output"
        )
        if not outcome.timed_out:
            return reason
        detail = f"pages={pages} budget={timeout:g}s"
        if unknown:
            detail += " page-count-unknown"
        return f"{reason} [{detail}]"

    def _finish(self, item: StagedDoc, tier: str, archive: Path, seconds: float) -> None:
        record = item.record
        try:
            result = postprocess_document(
                record,
                archive,
                stem=item.stem,
                raw_dir=self.config.raw_dir,
                flat_dir=self.config.flat_dir,
                keep_model_output=self.config.keep_model_output,
            )
        except (OSError, ValueError, zipfile.BadZipFile) as exc:
            self._fail(item, tier, f"postprocess failed: {exc}", seconds)
            return
        job = self._open.pop(record.md5, None)
        self.ledger.append(
            JobRecord(
                md5=record.md5,
                rel_path=record.rel_path,
                tier=tier,
                status=DONE,
                started_at=job.started_at if job else None,
                finished_at=now_iso(),
                seconds=seconds,
                output_dir=relative_to_root(result.doc_dir, self.config.project_root),
                pages=result.pages,
                output_bytes=result.total_bytes,
                model_output_bytes=result.dropped_bytes,
            )
        )
        stats = self.stats
        stats.processed += 1
        stats.pages += result.pages or 0
        stats.bytes_written += result.total_bytes
        stats.dropped_bytes += result.dropped_bytes
        stats.per_tier[tier] = stats.per_tier.get(tier, 0) + 1

    def _fail(self, item: StagedDoc, tier: str, error: str, seconds: float) -> None:
        self._fail_record(item.record.md5, item.record.rel_path, tier, error, seconds)

    def _fail_record(
        self, md5: str, rel_path: str, tier: str, error: str, seconds: Optional[float]
    ) -> None:
        job = self._open.pop(md5, None)
        error = strip_root_prefix(error, self.config.project_root)
        self.ledger.append(
            JobRecord(
                md5=md5,
                rel_path=rel_path,
                tier=tier,
                status=FAILED,
                started_at=job.started_at if job else None,
                finished_at=now_iso(),
                seconds=seconds,
                error=error,
            )
        )
        self.stats.failed += 1
        self.stats.failures.append((rel_path, error))

    def _start(self, item: StagedDoc, tier: str) -> None:
        """为一份文档开一条 ``running``；它已经在跑时直接复用，不写第二条。

        二分定位失败时会拿**缺失子集**重入 :meth:`_invoke`，那些文件早就开着
        ``running`` 了。再写一条会让账本里出现同一 md5 的重复 running（生产事故里
        那 30 条 running 就是 20 + 10 这么来的），而这次重入并没有改变它的开始时间
        与档位，所以复用是唯一说得通的写法。
        """
        md5 = item.record.md5
        if md5 in self._open:
            return
        job = _OpenJob(rel_path=item.record.rel_path, tier=tier, started_at=now_iso())
        self.ledger.append(
            JobRecord(
                md5=md5,
                rel_path=job.rel_path,
                tier=tier,
                status=RUNNING,
                started_at=job.started_at,
            )
        )
        self._open[md5] = job


def _tier_for(config: RunConfig) -> Callable[[DocRecord], str]:
    return lambda record: tier_of(record, config)
