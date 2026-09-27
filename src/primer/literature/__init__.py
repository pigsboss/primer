# -*- coding: utf-8 -*-
"""primer.literature —— 学术 PDF 批处理（交给 MinerU 转 markdown）的批处理脚手架。

前半程与 MinerU 无关：语料发现、内容去重、断点续跑账本、扁平暂存、人读报告；
后半程通过子进程调用 MinerU CLI（``backends``）并把归档整理成扁平 markdown
（``postprocess``），由 ``runner`` 串成一次可断点续跑的 ``run``。

对外接口：

* :func:`load_config` / :class:`RunConfig`：运行配置与优先级合并；
* :func:`build_catalog` / :class:`Catalog`：语料编目与去重（读不动的文件记进
  ``Catalog.unreadable`` 并跳过，不让整轮扫描崩掉）；
* :class:`Ledger` / :class:`JobRecord`：断点续跑账本；
* :func:`stage_batch` / :func:`select_pending` / :func:`cleanup`：扁平暂存与待办筛选
  （暂存逐份隔离失败，源被改名/删除只影响那一份）；
* :func:`plan_run` / :func:`run_conversion` / :class:`RunStats`：运行计划与执行；
* :class:`MineruBackend` / :func:`postprocess_document`：MinerU 后端与结果后处理。

输出边界（工程目录只读、产物只在 ``<工程根>/_primer/<功能>/``、记录路径一律相对
工程根）由 ``primer.literature.paths`` 统一提供。

命令行：``python -m primer.literature scan|status|run``。
"""

from .backends import ExtractionBackend, MineruBackend, ParseOutcome, to_cli_tier
from .catalog import (
    Catalog,
    Citation,
    DocRecord,
    UnreadableFile,
    build_catalog,
    file_md5,
    iter_pdf_paths,
)
from .config import RunConfig, TierRule, apply_overrides, load_config, validate
from .postprocess import PostprocessResult, postprocess_document
from .report import render_run_plan, render_run_summary, render_scan, render_status
from .runner import (
    Chunk,
    ProgressLine,
    RunPlan,
    RunStats,
    TierGroup,
    plan_run,
    run_conversion,
)
from .stage import (
    StagedBatch,
    StageFailure,
    StagedDoc,
    cleanup,
    resolve_tier,
    select_pending,
    stage_batch,
    unique_stem,
)
from .state import (
    DONE,
    FAILED,
    PENDING,
    RUNNING,
    SKIPPED,
    STATUSES,
    JobRecord,
    Ledger,
)

__all__ = [
    "Catalog",
    "Chunk",
    "Citation",
    "DONE",
    "DocRecord",
    "ExtractionBackend",
    "FAILED",
    "JobRecord",
    "Ledger",
    "MineruBackend",
    "PENDING",
    "ParseOutcome",
    "PostprocessResult",
    "ProgressLine",
    "RUNNING",
    "RunConfig",
    "RunPlan",
    "RunStats",
    "SKIPPED",
    "STATUSES",
    "StagedBatch",
    "StagedDoc",
    "StageFailure",
    "TierGroup",
    "TierRule",
    "UnreadableFile",
    "apply_overrides",
    "build_catalog",
    "cleanup",
    "file_md5",
    "iter_pdf_paths",
    "load_config",
    "plan_run",
    "postprocess_document",
    "render_run_plan",
    "render_run_summary",
    "render_scan",
    "render_status",
    "resolve_tier",
    "run_conversion",
    "select_pending",
    "stage_batch",
    "to_cli_tier",
    "unique_stem",
    "validate",
]
