# -*- coding: utf-8 -*-
"""提取后端：把一批 PDF 转成 markdown 归档。

对外接口：

* :class:`ExtractionBackend` / :class:`ParseOutcome`：后端协议与调用结果；
* :class:`MineruBackend` / :func:`to_cli_tier`：MinerU CLI 实现与档位校验。
"""

from .base import ExtractionBackend, ParseOutcome
from .mineru import CLI_TIERS, MineruBackend, to_cli_tier

__all__ = [
    "CLI_TIERS",
    "ExtractionBackend",
    "MineruBackend",
    "ParseOutcome",
    "to_cli_tier",
]
