# -*- coding: utf-8 -*-
"""扁平暂存：把待处理文件硬链接进一个文件名唯一的目录。

MinerU 4.0 的目录批处理按键于输入文件主干名写出**扁平**结果，若两个输入主干
相同会整批中止。因此暂存阶段用 ``<md5 前 8 位>_<净化后的原主干>`` 保证唯一，
跨文件系统无法硬链接时退回 ``shutil.copy2``。

语料是别人正在编辑的目录：从编目到暂存之间隔着几个小时，这期间文件被改名、
删除都属常态。所以暂存**逐份隔离失败**——进不去的那份记一句具体原因，其余照常
进暂存目录，不把整轮运行拖死（生产里一个被改名的文件就是这样崩掉了一轮 3.8
小时的运行）。
"""

from __future__ import annotations

import fnmatch
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from .catalog import DocRecord
from .config import TierRule
from .state import DONE

_INVALID = re.compile(r"[^A-Za-z0-9._-]+")
_STEM_LIMIT = 100

# 源文件在编目之后失效时写进账本的原因；两句分开，因为它们对不上号。
STALE_SOURCE_REASON = "stale source: no longer exists at stage time"
NOT_A_FILE_REASON = "unusable source: not a regular file at stage time"


@dataclass(frozen=True)
class StagedDoc:
    """一个已暂存文件：原记录、链接路径与唯一主干名。"""

    record: DocRecord
    link_path: Path
    stem: str


@dataclass(frozen=True)
class StageFailure:
    """一份没能进暂存目录的文档及原因；``reason`` 会原样进账本。"""

    record: DocRecord
    reason: str


@dataclass(frozen=True)
class StagedBatch:
    """一次暂存的结果：成功的链接与失败的原因。"""

    staged: list[StagedDoc]
    failures: list[StageFailure]


def unique_stem(record: DocRecord) -> str:
    """生成唯一主干名：``<md5[:8]>_<净化主干>``。

    主干中 ``[A-Za-z0-9._-]`` 之外的字符统一替换为 ``_``，连续替换合并为一个，
    两端去掉，最后截断到 100 字符。
    """
    sanitized = _INVALID.sub("_", record.path.stem).strip("_")
    return f"{record.md5[:8]}_{sanitized[:_STEM_LIMIT]}"


def stage_batch(records: Sequence[DocRecord], staging_dir: Path) -> StagedBatch:
    """把 ``records`` 硬链接进 ``staging_dir``（已存在则合并），逐份隔离失败。

    只有"暂存目录本身建不出来"这种整批级别的问题才会抛异常；单份文件读不到、
    源没了、链接与复制都不行，都只是这一份的失败。
    """
    staging = Path(staging_dir)
    staging.mkdir(parents=True, exist_ok=True)
    staged: list[StagedDoc] = []
    failures: list[StageFailure] = []
    for record in records:
        stem = unique_stem(record)
        link_path = staging / f"{stem}.pdf"
        reason = _stage_one(record.path, link_path)
        if reason is not None:
            failures.append(StageFailure(record=record, reason=reason))
            continue
        staged.append(StagedDoc(record=record, link_path=link_path, stem=stem))
    return StagedBatch(staged=staged, failures=failures)


def cleanup(staging_dir: Path) -> int:
    """清掉暂存目录里本次留下的暂存文件，返回删除数量。

    只删除目录直属的文件（硬链接/副本），不递归、不碰父目录；目录清空后一并移除。
    """
    staging = Path(staging_dir)
    if not staging.is_dir():
        return 0
    removed = 0
    for entry in staging.iterdir():
        if entry.is_file() or entry.is_symlink():
            entry.unlink()
            removed += 1
    try:
        staging.rmdir()
    except OSError:
        pass
    return removed


def select_pending(
    records: Sequence[DocRecord],
    ledger,
    tier_for,
    *,
    limit: int | None = None,
) -> list[DocRecord]:
    """挑出还需要转换的记录，保持按 ``rel_path`` 的确定顺序。

    丢弃重复项（``duplicate_of is not None``），也丢弃账本里同档位已 ``done`` 的记录。
    """
    stored = ledger.load()
    pending = []
    for record in records:
        if record.duplicate_of is not None:
            continue
        tier = tier_for(record)
        previous = stored.get(record.md5)
        if previous is not None and previous.status == DONE and previous.tier == tier:
            continue
        pending.append(record)
    pending.sort(key=lambda item: item.rel_path)
    if limit is not None:
        pending = pending[:limit]
    return pending


def resolve_tier(record: DocRecord, default_tier: str, rules: Sequence[TierRule]) -> str:
    """按 ``tier_rules`` 的 glob 匹配 ``rel_path``，命中则覆盖默认档位。"""
    for rule in rules:
        if fnmatch.fnmatch(record.rel_path, rule.glob):
            return rule.tier
    return default_tier


def _link_or_copy(source: Path, target: Path) -> None:
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)


def _stage_one(source: Path, target: Path) -> Optional[str]:
    """把一份源文件放进暂存目录；成功返回 ``None``，失败返回一句人读原因。

    链接之前重新 ``stat`` 一次：编目是几小时前的事，这期间源文件可能已被改名、删除
    或替换。先查一次能把"源没了 / 不再是普通文件"与"链接本身失败"分开，报出来的
    原因才对得上号——否则只能等到 ``os.link`` 抛一个光秃秃的 ``PermissionError``，
    连是哪一种情况都说不清。
    """
    source = Path(source)
    if target.exists():
        return None
    if not source.exists():
        return STALE_SOURCE_REASON
    if not source.is_file():
        return NOT_A_FILE_REASON
    try:
        _link_or_copy(source, target)
    except OSError as exc:
        return _link_reason(exc)
    return None


def _link_reason(exc: OSError) -> str:
    """链接与回退复制都不成时的人读原因。"""
    if isinstance(exc, FileNotFoundError):
        return STALE_SOURCE_REASON  # 查过之后、链接之前又被删掉
    if isinstance(exc, PermissionError):
        return "unreadable source: permission denied at stage time"
    return f"cannot stage source: {exc.strerror or exc}"
