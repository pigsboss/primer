# -*- coding: utf-8 -*-
"""提取后端的公共契约：一次调用的结果与后端协议。

后端只负责"把 PDF 变成上游原始产物"这一件事，不碰账本、暂存与后处理；
编排（分块、失败隔离、记账）在 :mod:`primer.literature.runner`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Protocol, Sequence


@dataclass(frozen=True)
class ParseOutcome:
    """一次后端调用的结果。

    ``outputs`` 只收录**确实存在于磁盘上**的产物，按输入文件索引；批量调用
    中途失败时先前写出的产物依旧有效，因此它可能只是输入的子集。``timed_out``
    是"到达时限被掐断"的显式标记：单看 ``returncode`` 分不清超时与信号杀进程。
    """

    command: tuple[str, ...]
    returncode: int
    outputs: dict[Path, Path] = field(default_factory=dict)
    seconds: float = 0.0
    error: str = ""
    pages: int | None = None
    engine_seconds: float | None = None
    engine_speed: float | None = None
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        """进程返回码为 0 时为真（不代表所有输入都产出了结果）。"""
        return self.returncode == 0


class ExtractionBackend(Protocol):
    """把一批 PDF 转成 markdown 归档的后端。

    档位 ``tier`` 使用本后端自己的档位词汇，由后端负责校验与翻译；调用方只
    保证同一档位的文件会被合并到同一次 :meth:`parse` 调用里。
    """

    name: str

    def is_available(self) -> bool:
        """后端可执行文件是否可用；不可用时 :meth:`parse` 必然失败。"""
        ...

    def parse(
        self,
        inputs: Sequence[Path],
        output_dir: Path,
        tier: str,
        *,
        timeout: Optional[float] = None,
    ) -> ParseOutcome:
        """解析 ``inputs`` 并把产物写进 ``output_dir``。

        ``timeout`` 是本次调用的墙钟时限（秒），``None`` 表示用后端自己的默认值。
        时限到了必须返回而**不是**抛异常：一次超时只是一次失败的调用，调用方还要
        靠它去二分定位到底是哪个输入把这一批拖死的。
        """
        ...
