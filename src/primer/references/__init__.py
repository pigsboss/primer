# -*- coding: utf-8 -*-
"""primer.references —— 参考文献审计：解析 → 核对本地原文 → 出报告与索取清单。

输入是 LLM 产出的参考文献 markdown（任意数量、任意文件，每个文件可含多份清单）、
下载账本、救援缓存与本地 PDF 目录；输出是逐条判定（结论 + 置信度 + 证据）、
工作区级问题清单、图书馆索取清单，以及供书稿外单独维护的 ``.bib`` 与校核报告。

对外接口：

* :func:`parse_markdown` / :func:`discover_lists` / :class:`RefEntry`：条目解析与校验；
* :func:`build_ledger` / :class:`Ledger`：本地可得性证据索引；
* :func:`run_audit` / :class:`AuditResult` / :class:`EntryAudit`：逐条判定与汇总；
* :func:`library_rows` / :func:`write_audit_md` 等导出函数；
* :func:`read_oa_log` / :class:`SurveyItem`：调研日志里"没拿到"的条目（图书馆清单第二来源）；
* :func:`build_records` / :func:`render_bib` / :func:`render_review`：bib 与校核报告。

产物落在 ``<工程根>/_primer/references/``（见 ``primer.literature.paths``），其中
每个路径都相对工程根书写；工程目录本身只读。

命令行：``python -m primer.references audit|list``。
"""

from .audit import (
    CONFIDENCE_HIGH,
    CONFIDENCE_MEDIUM,
    VERDICT_CONFLICT,
    VERDICT_LOCAL,
    VERDICT_LOCAL_UNREGISTERED,
    VERDICT_MISSING,
    VERDICT_PUBLIC_WEB,
    VERDICTS,
    AuditResult,
    EntryAudit,
    Problems,
    run_audit,
)
from .bib import (
    BibRecord,
    build_records,
    cite_key,
    render_bib,
    render_review,
    write_bib,
    write_review,
)
from .entries import (
    TAG_AWAITING,
    TAG_LOCAL,
    TAG_NONE,
    TAG_PUBLIC_WEB,
    ListValidation,
    RefEntry,
    RefList,
    all_entries,
    classify_tag,
    discover_lists,
    parse_markdown,
    split_citation,
    validate,
    validate_all,
)
from .export import (
    LIBRARY_COLUMNS,
    ROW_CONFIDENCE_LOW,
    collect_body_citations,
    index_payload,
    index_rows,
    library_confidence,
    library_rows,
    render_audit_md,
    render_library_md,
    source_counts,
    write_audit_md,
    write_index_csv,
    write_index_json,
    write_library_csv,
    write_library_md,
)
from .ledger import (
    SOURCE_BIB_ARXIV,
    SOURCE_BIB_TAG,
    SOURCE_LOCAL_FILE,
    SOURCE_REFLIB_LOG,
    SOURCE_RESCUE_CACHE,
    Evidence,
    Ledger,
    LocalFile,
    add_bibliography_claims,
    build_ledger,
    ref_key,
)
from .survey import (
    SURVEY_SOURCE,
    SurveyItem,
    load_oa_log,
    needs_library,
    read_oa_log,
    title_key,
)

__all__ = [
    "AuditResult",
    "BibRecord",
    "CONFIDENCE_HIGH",
    "CONFIDENCE_MEDIUM",
    "EntryAudit",
    "Evidence",
    "LIBRARY_COLUMNS",
    "Ledger",
    "ListValidation",
    "LocalFile",
    "Problems",
    "ROW_CONFIDENCE_LOW",
    "RefEntry",
    "RefList",
    "SOURCE_BIB_ARXIV",
    "SOURCE_BIB_TAG",
    "SOURCE_LOCAL_FILE",
    "SOURCE_REFLIB_LOG",
    "SOURCE_RESCUE_CACHE",
    "SURVEY_SOURCE",
    "SurveyItem",
    "TAG_AWAITING",
    "TAG_LOCAL",
    "TAG_NONE",
    "TAG_PUBLIC_WEB",
    "VERDICT_CONFLICT",
    "VERDICT_LOCAL",
    "VERDICT_LOCAL_UNREGISTERED",
    "VERDICT_MISSING",
    "VERDICT_PUBLIC_WEB",
    "VERDICTS",
    "add_bibliography_claims",
    "all_entries",
    "build_ledger",
    "build_records",
    "cite_key",
    "classify_tag",
    "collect_body_citations",
    "discover_lists",
    "index_payload",
    "index_rows",
    "library_confidence",
    "library_rows",
    "load_oa_log",
    "needs_library",
    "parse_markdown",
    "read_oa_log",
    "ref_key",
    "render_audit_md",
    "render_bib",
    "render_library_md",
    "render_review",
    "run_audit",
    "source_counts",
    "split_citation",
    "title_key",
    "validate",
    "validate_all",
    "write_audit_md",
    "write_bib",
    "write_index_csv",
    "write_index_json",
    "write_library_csv",
    "write_library_md",
    "write_review",
]
