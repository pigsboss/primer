# -*- coding: utf-8 -*-
"""断点续跑账本：按内容 md5 追加写入的 JSONL。

账本只追加、不重写，最后一条同 md5 记录为准，因此一次被杀掉的运行最多留下
半行 JSON，载入时跳过无法解析的行即可，不会让整个续跑流程崩掉。是否已完成
以 **(md5, 档位)** 为准：同一文件换了档位就应当重跑。

状态机由写入方（:mod:`primer.literature.runner`）负责保持干净：同一个 md5 在
一次运行里至多有一条未收尾的 ``running``；进程被硬杀（SIGKILL / 断电）留下的
孤儿 ``running`` 由下一次开跑时的 :meth:`Ledger.close_orphans` 补成 ``failed``，
所以账本里不会出现"永远在跑"的记录。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

PENDING = "pending"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
SKIPPED = "skipped"
STATUSES = (PENDING, RUNNING, DONE, FAILED, SKIPPED)


def now_iso() -> str:
    """当前 UTC 时刻（秒级 ISO 8601），账本里所有时间戳都用它。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class JobRecord:
    """一个转换作业的一行状态。

    ``seconds`` 是产出该结果的解析调用耗时；批量调用里的文件共享按文件数均摊
    的值。``output_bytes`` 为该文档最终落盘的字节数（归档 + 解包目录 + 扁平
    markdown），``model_output_bytes`` 是被丢弃的 ``model_output.json`` 的字节数
    （保留时为 0），``pages`` 为解析出的页数。

    ``rel_path`` 与 ``output_dir`` 都相对**工程根**书写，账本被搬到别的机器上
    仍然指得清楚。
    """

    md5: str
    rel_path: str
    tier: str
    status: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    seconds: Optional[float] = None
    output_dir: Optional[str] = None
    error: Optional[str] = None
    pages: Optional[int] = None
    output_bytes: Optional[int] = None
    model_output_bytes: Optional[int] = None


def summarize(records: Mapping[str, JobRecord]) -> dict[str, int]:
    """统计各状态记录数；所有已知状态都会出现在结果里（含 0）。"""
    counts = {status: 0 for status in STATUSES}
    for record in records.values():
        counts[record.status] = counts.get(record.status, 0) + 1
    return counts


class Ledger:
    """一个 JSONL 账本文件。"""

    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> dict[str, JobRecord]:
        """载入账本，返回 ``{md5: JobRecord}``；同 md5 以最后一条为准。"""
        records: dict[str, JobRecord] = {}
        if not self.path.is_file():
            return records
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = _from_payload(json.loads(line))
                except (json.JSONDecodeError, TypeError, KeyError, ValueError):
                    continue
                records[record.md5] = record
        return records

    def append(self, record: JobRecord) -> None:
        """追加一条记录，必要时创建父目录。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")

    def is_done(self, md5: str, tier: str) -> bool:
        """仅当已存记录状态为 done 且档位一致时返回 ``True``。"""
        record = self.load().get(md5)
        return record is not None and record.status == DONE and record.tier == tier

    def close_orphans(self, error: str) -> int:
        """把上一次运行遗留的 ``running`` 补成 ``failed``，返回补了几条。

        上一次跑被 SIGKILL / 断电硬杀时来不及收尾，账本里会留下永远"正在跑"的记录。
        下一次开跑前补一条终态，账本在任何时刻都是可解释的；被补的文件仍是待办，
        照常会重跑。
        """
        orphans = [record for record in self.load().values() if record.status == RUNNING]
        for record in orphans:
            self.append(
                JobRecord(
                    md5=record.md5,
                    rel_path=record.rel_path,
                    tier=record.tier,
                    status=FAILED,
                    started_at=record.started_at,
                    finished_at=now_iso(),
                    error=error,
                )
            )
        return len(orphans)

    def summary(self) -> dict[str, int]:
        return summarize(self.load())


def _from_payload(payload: Any) -> JobRecord:
    if not isinstance(payload, dict):
        raise TypeError("ledger record must be a JSON object")
    md5 = payload["md5"]
    status = payload["status"]
    if not isinstance(md5, str) or not md5:
        raise ValueError("ledger record is missing md5")
    if not isinstance(status, str) or not status:
        raise ValueError("ledger record is missing status")
    return JobRecord(
        md5=md5,
        rel_path=str(payload.get("rel_path") or ""),
        tier=str(payload.get("tier") or ""),
        status=status,
        started_at=_optional_text(payload.get("started_at")),
        finished_at=_optional_text(payload.get("finished_at")),
        seconds=_optional_float(payload.get("seconds")),
        output_dir=_optional_text(payload.get("output_dir")),
        error=_optional_text(payload.get("error")),
        pages=_optional_int(payload.get("pages")),
        output_bytes=_optional_int(payload.get("output_bytes")),
        model_output_bytes=_optional_int(payload.get("model_output_bytes")),
    )


def _optional_text(value: Any) -> Optional[str]:
    if value is None or value == "":
        return None
    return str(value)


def _optional_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
