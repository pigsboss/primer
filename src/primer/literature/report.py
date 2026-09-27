# -*- coding: utf-8 -*-
"""人读报告：扫描报告与状态报告，均返回 markdown 文本。

输出统一为英文、等宽列对齐，便于在终端里快速扫读。报告里出现的每个路径都相对
**工程根**书写，报告被贴进别处时也不会泄露本机的绝对路径。
"""

from __future__ import annotations

from pathlib import Path
from typing import Mapping, Optional

from .catalog import Catalog, DocRecord
from .config import RunConfig
from .paths import relative_to_root
from .runner import Chunk, RunPlan, RunStats, invocation_timeout
from .state import FAILED, JobRecord, STATUSES, summarize

_LARGEST_FILES = 10
_DUPLICATE_GROUPS = 10
_FAILED_JOBS = 20
_MAX_FAILURE_ROWS = 20


def human_bytes(size: int) -> str:
    """把字节数格式化为人类可读的字符串。"""
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024.0 or unit == "TB":
            return f"{int(value)} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} PB"


def render_scan(catalog: Catalog, ledger: Optional[Mapping[str, JobRecord]] = None) -> str:
    """渲染扫描报告：总量、按根拆分、重复、最大文件，以及（若有账本）进度。"""
    root = catalog.project_root
    lines = ["# Literature scan", "", "## Corpus", ""]
    lines.extend(
        _table(
            ("Metric", "Value"),
            [
                ("PDF files", str(len(catalog.records))),
                ("Total size", f"{human_bytes(catalog.total_bytes)} ({catalog.total_bytes} bytes)"),
                ("Unique files", str(len(catalog.unique))),
                ("Unique size", human_bytes(catalog.unique_bytes)),
                ("Duplicate groups", str(catalog.duplicate_groups)),
                ("Redundant bytes", human_bytes(catalog.redundant_bytes)),
            ],
        )
    )

    lines.extend(["", "## By root", ""])
    by_root = catalog.by_root()
    lines.extend(
        _table(
            ("Root", "Files", "Size"),
            [
                (
                    relative_to_root(root_path, root),
                    str(len(records)),
                    human_bytes(sum(r.size for r in records)),
                )
                for root_path, records in by_root.items()
            ],
        )
    )

    lines.extend(["", "## Largest files", ""])
    largest = sorted(catalog.records, key=lambda item: item.size, reverse=True)[:_LARGEST_FILES]
    lines.extend(
        _table(
            ("Size", "Path"),
            [
                (human_bytes(record.size), relative_to_root(record.rel_path, root))
                for record in largest
            ],
        )
    )

    groups = catalog.duplicate_groups_map()
    if groups:
        ordered = sorted(groups.values(), key=_group_waste, reverse=True)[:_DUPLICATE_GROUPS]
        lines.extend(["", "## Duplicate groups", ""])
        lines.extend(
            _table(
                ("Copies", "Wasted", "Representative"),
                [
                    (
                        str(len(group)),
                        human_bytes(_group_waste(group)),
                        relative_to_root(group[0].rel_path, root),
                    )
                    for group in ordered
                ],
            )
        )

    if catalog.unreadable:
        lines.extend(["", "## Unreadable at scan time", ""])
        lines.extend(
            _table(
                ("Path", "Reason"),
                [
                    (relative_to_root(item.path, root), item.reason)
                    for item in catalog.unreadable
                ],
            )
        )
        lines.append(
            "\nThese files are not in the catalog (no content hash); they will be picked up "
            "by the next scan if they become readable again."
        )

    lines.extend(["", "## Ledger", ""])
    if ledger:
        counts = summarize(ledger)
        lines.extend(
            _table(
                ("Status", "Count"),
                [(status, str(counts.get(status, 0))) for status in STATUSES],
            )
        )
    else:
        lines.append("No ledger records yet.")
    return "\n".join(lines) + "\n"


def render_status(ledger: Mapping[str, JobRecord], *, project_root: Optional[Path] = None) -> str:
    """渲染状态报告：各状态计数，以及失败作业的错误信息。

    ``rel_path`` 由账本给出，多数情况下已经是相对工程根的；这里再过一道
    :func:`relative_to_root`，是为了让早期版本写下的绝对路径也能读得懂。
    """
    lines = ["# Literature status", ""]
    if not ledger:
        lines.append("No ledger records found.")
        return "\n".join(lines) + "\n"
    counts = summarize(ledger)
    lines.append(f"Records: {len(ledger)}")
    lines.extend(
        ["", *_table(("Status", "Count"), [(status, str(counts[status])) for status in STATUSES])]
    )

    failures = [record for record in ledger.values() if record.status == FAILED]
    if failures:
        ordered = sorted(failures, key=lambda item: relative_to_root(item.rel_path, project_root))
        lines.extend(["", "## Failed jobs", ""])
        for record in ordered[:_FAILED_JOBS]:
            error = record.error or "(no error message)"
            lines.append(
                f"- `{relative_to_root(record.rel_path, project_root)}` [{record.tier}]: {error}"
            )
        remaining = len(failures) - _FAILED_JOBS
        if remaining > 0:
            lines.append(f"- ... and {remaining} more")
    return "\n".join(lines) + "\n"


def render_run_plan(
    plan: RunPlan, *, config: RunConfig, backend: str, backend_available: bool
) -> str:
    """渲染 ``run --dry-run`` 的计划：后端、工作量、分块明细与分档明细。

    分块明细逐块给出文件数与页数合计，还有按页数算出的调用时限——真跑之前先看一眼
    是不是每块的工作量都相当，否则一次五小时的运行就只能靠进度行猜。
    """
    lines = ["# Literature run plan", "", "## Backend", ""]
    lines.extend(
        _table(
            ("Field", "Value"),
            [
                ("Backend", backend),
                ("Command", config.mineru_command),
                ("Available", "yes" if backend_available else "no"),
                ("Model source", config.model_source),
                ("Tier", config.tier),
                (
                    "Chunk limits",
                    f"{config.chunk_size} files / {config.chunk_max_pages} pages",
                ),
                (
                    "Parse timeout",
                    f"{_duration(invocation_timeout(config, 1))} for 1 page – "
                    f"{_duration(config.parse_timeout_seconds)} "
                    f"for {config.chunk_max_pages} pages",
                ),
                ("Output dir", relative_to_root(config.output_dir, config.project_root)),
            ],
        )
    )
    pages = str(plan.estimated_pages)
    if plan.estimated_from_size:
        pages += f" ({plan.estimated_from_size} of {len(plan.pending)} from file size)"
    lines.extend(["", "## Workload", ""])
    lines.extend(
        _table(
            ("Metric", "Value"),
            [
                ("Files to process", str(len(plan.pending))),
                ("Chunks", str(len(plan.chunks))),
                ("Backend invocations", str(plan.invocations)),
                ("Estimated pages", pages),
                ("Page counts", _page_count_provenance(plan)),
                ("Already done (same tier)", str(plan.done_already)),
                ("Duplicates ignored", str(plan.duplicates)),
                ("Deferred by --limit", str(plan.deferred)),
            ],
        )
    )
    if plan.chunks:
        lines.extend(["", "## Chunks", ""])
        lines.extend(
            _table(
                ("Chunk", "Files", "Budget pages", "Timeout"),
                [
                    (
                        str(index),
                        str(len(chunk.records)),
                        _chunk_pages_cell(chunk),
                        _duration(invocation_timeout(config, chunk.pages)),
                    )
                    for index, chunk in enumerate(plan.chunks, start=1)
                ],
            )
        )
        largest = max(plan.chunks, key=lambda chunk: chunk.pages)
        lines.append(
            f"\nLargest chunk: {_plural(largest.pages, 'budget page')} "
            f"across {_plural(len(largest.records), 'file')}"
        )
    if plan.per_tier:
        lines.extend(["", "## Files per tier", ""])
        lines.extend(
            _table(
                ("Tier", "Files"),
                [(tier, str(count)) for tier, count in sorted(plan.per_tier.items())],
            )
        )
    return "\n".join(lines) + "\n"


def _chunk_pages_cell(chunk: Chunk) -> str:
    """该块的预算页数；含页数未知的文件时点名，免得它看着像个普通的块。"""
    if chunk.unknown_pages:
        return f"{chunk.pages} (incl. {_plural(chunk.unknown_pages, 'unknown-page file')})"
    return str(chunk.pages)


def _page_count_provenance(plan: RunPlan) -> str:
    """说明页数是怎么来的：谁数出来的、谁按体积估的、谁的页数根本不知道。

    页数不知道的文件走**满额**时限（见 ``runner.invocation_timeout``），因此必须
    在这里点名，不能让它在本可以看穿一切的 ``--dry-run`` 里隐身。
    """
    total = len(plan.pending)
    if not total:
        return "nothing to count"
    if plan.estimated_from_size == 0:
        return f"pdfinfo exact for all {total} files"
    if not plan.pdfinfo_available:
        text = f"pdfinfo not installed: all {total} page counts estimated from file size"
    else:
        text = (
            f"pdfinfo unavailable or unreadable for {plan.estimated_from_size} of {total} files, "
            "those page counts are estimated from file size"
        )
    if plan.unknown_pages:
        text += (
            f"; {plan.unknown_pages} of {total} have no page count at all, "
            "their budget comes from file size or (when even that is missing) the full "
            "parse_timeout_seconds"
        )
    return text


def _duration(seconds: float) -> str:
    """把秒数写成便于扫读的时长。"""
    return f"{seconds / 60:.0f} min" if seconds >= 60 else f"{seconds:.0f} s"


def _plural(count: int, noun: str) -> str:
    """``1 file`` / ``3 files``。"""
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def render_run_summary(stats: RunStats, *, config: RunConfig) -> str:
    """渲染运行总结：产出计数、成本、分档明细与失败清单。"""
    lines = ["# Literature run summary", ""]
    lines.extend(
        _table(
            ("Metric", "Value"),
            [
                ("Processed", str(stats.processed)),
                ("Skipped (already done)", str(stats.skipped_already_done)),
                ("Failed", str(stats.failed)),
                ("Duplicates ignored", str(stats.duplicates)),
                ("Deferred by --limit", str(stats.deferred)),
            ],
        )
    )
    lines.extend(["", "## Cost", ""])
    lines.extend(
        _table(
            ("Metric", "Value"),
            [
                ("Pages", str(stats.pages)),
                ("Wall time", f"{stats.wall_seconds:.1f} s"),
                ("Effective pages/s", _rate(stats.pages_per_second)),
                ("Engine pages/s (reported)", _rate(stats.engine_speed)),
                ("Backend invocations", str(stats.invocations)),
                ("Bytes written", f"{human_bytes(stats.bytes_written)} ({stats.bytes_written} bytes)"),
                (
                    "model_output.json dropped",
                    f"{human_bytes(stats.dropped_bytes)} ({stats.dropped_bytes} bytes)",
                ),
                ("Output dir", relative_to_root(config.output_dir, config.project_root)),
            ],
        )
    )
    if stats.per_tier:
        lines.extend(["", "## Processed per tier", ""])
        lines.extend(
            _table(
                ("Tier", "Files"),
                [(tier, str(count)) for tier, count in sorted(stats.per_tier.items())],
            )
        )
    if stats.failures:
        lines.extend(["", "## Failures", ""])
        for rel_path, error in stats.failures[:_MAX_FAILURE_ROWS]:
            lines.append(f"- `{rel_path}`: {error}")
        remaining = len(stats.failures) - _MAX_FAILURE_ROWS
        if remaining > 0:
            lines.append(f"- ... and {remaining} more")
    return "\n".join(lines) + "\n"


def _rate(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def _group_waste(group: list[DocRecord]) -> int:
    return sum(record.size for record in group[1:])


def _table(header: tuple[str, ...], rows: list[tuple[str, ...]]) -> list[str]:
    """渲染一个等宽 markdown 表格。"""
    widths = [len(cell) for cell in header]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    lines = [_row(header, widths), _row(tuple("-" * width for width in widths), widths)]
    lines.extend(_row(row, widths) for row in rows)
    return lines


def _row(cells: tuple[str, ...], widths: list[int]) -> str:
    padded = (cell.ljust(widths[index]) for index, cell in enumerate(cells))
    return "| " + " | ".join(padded) + " |"
