# -*- coding: utf-8 -*-
"""primer.claims —— 论断—引用链路：把书稿里的引用落到原文，判定论断是否真被支撑。

这一包分两半。**前半程是确定性的**，不联网、不调模型：

* :func:`primer.claims.extract.extract_claims`：从正文抽出"带引用的句子"，
  展开 ``[111–113]`` 这类范围，一条 (论断, 引用编号) 一条记录；
* :class:`primer.claims.resolve.Resolver`：把编号走完
  （文献条目 → 本地 PDF → 转换文本 → 可用文本）四跳，断在哪一跳就记在哪一跳；
* :func:`primer.claims.retrieve.retrieve_passages`：在原文里取回得分最高的若干候选段落，
  中文二元组 + 逆文档频率打分，纯标准库；
* :func:`primer.claims.report.build_payload` / :func:`...write_claims_json`：出
  ``claims.json``（机器读）与 ``claims.md``（人读）。

**后半程调用模型**，做三件事：

* :mod:`primer.claims.translate`：把论断句子译成英文。本工程的原文全是英文、论断全是
  中文，289 条能取到原文的对里 151 条与原文零 token 重合——翻译是把这个检索器从
  "基本不工作"变成"能用"的最省一步。译文批量请求、按句子哈希缓存到磁盘。
* :func:`primer.claims.retrieve.retrieve_merged`：原句与译文**各检索一次**并按区间合并
  候选，记下每个候选是哪一路捞到的，因此"译文救回了多少"是可核的。
* :mod:`primer.claims.judge`：逐对判定 ``supported | partial | unsupported |
  source-missing | unverifiable``。四道硬规矩——没有逐字引文不准下判定、没有候选只准
  判 ``unverifiable``、``partial``/``unsupported`` 必须跑一遍反向的第二遍、``source-missing``
  只给链路断掉的对。取值与证据要求同时写在产物里的 ``verdict_contract``。

命令行：

* ``python -m primer.claims extract --project-root <工程根>``（前半程，不花钱）；
* ``python -m primer.claims verify --project-root <工程根> [--limit N] [--only 291]
  [--dry-run] [--force]``（后半程，可续跑）。
"""

from .client import (
    DEFAULT_BASE_URL,
    DEFAULT_KEY_ENV,
    DEFAULT_MAX_TOKENS,
    DEFAULT_MODEL,
    ChatClient,
    ChatReply,
    HttpRequest,
    LlmCallError,
    LlmReplyError,
    UsageTotals,
    parse_json_reply,
)
from .extract import (
    DEFAULT_BODY_FILES,
    ClaimRecord,
    ExtractionResult,
    extract_claims,
    strip_citations,
)
from .judge import (
    Judge,
    JudgeInput,
    PairVerdict,
    PassResult,
    apply_rules,
    build_first_pass_prompt,
    build_second_pass_prompt,
    locate_quote,
)
from .report import (
    CLAIMS_JSON,
    CLAIMS_MD,
    VERDICT_CONTRACT,
    VERDICT_CONTRACT_RUN,
    VERDICT_PENDING,
    VERDICTS,
    VERDICTS_JSON,
    VERDICTS_MD,
    build_payload,
    build_verdict_payload,
    print_summary,
    print_verify_summary,
    write_claims_json,
    write_claims_md,
    write_verdicts_json,
    write_verdicts_md,
)
from .resolve import (
    DEFAULT_LITERATURE_FLAT,
    DEFAULT_LITERATURE_STATE,
    DEFAULT_LOCAL_ROOT,
    DEFAULT_REFERENCE_MD,
    DEFAULT_REFLIB_LOG,
    DEFAULT_RESCUE_CACHE,
    STATUS_EMPTY_TEXT,
    STATUS_ENTRY_MISSING,
    STATUS_NOT_CONVERTED,
    STATUS_NO_LOCAL_FILE,
    STATUS_OK,
    STATUSES,
    Resolution,
    Resolver,
    SourceText,
    build_resolver,
    load_entries,
)
from .retrieve import (
    MIN_SCORE,
    OVERLAP,
    QUERY_ORIGINAL,
    QUERY_TRANSLATION,
    TOP_K,
    WINDOW,
    Passage,
    QueryStat,
    RetrievalResult,
    SourceIndex,
    build_index,
    estimate_tokens,
    retrieve_merged,
    retrieve_passages,
    score_distribution,
    tokenize,
)
from .translate import (
    TRANSLATIONS_JSON,
    Translation,
    TranslationCache,
    Translator,
    claim_key,
    plan_batches,
)
from .verify import (
    LEDGER_JSONL,
    RunStats,
    Verifier,
    VerifyPlan,
    append_ledger,
    build_client,
    load_ledger,
    pair_key,
    select_pairs,
)

__all__ = [
    "CLAIMS_JSON",
    "CLAIMS_MD",
    "DEFAULT_BASE_URL",
    "DEFAULT_BODY_FILES",
    "DEFAULT_KEY_ENV",
    "DEFAULT_LITERATURE_FLAT",
    "DEFAULT_LITERATURE_STATE",
    "DEFAULT_LOCAL_ROOT",
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_MODEL",
    "DEFAULT_REFERENCE_MD",
    "DEFAULT_REFLIB_LOG",
    "DEFAULT_RESCUE_CACHE",
    "LEDGER_JSONL",
    "MIN_SCORE",
    "OVERLAP",
    "QUERY_ORIGINAL",
    "QUERY_TRANSLATION",
    "STATUSES",
    "STATUS_EMPTY_TEXT",
    "STATUS_ENTRY_MISSING",
    "STATUS_NOT_CONVERTED",
    "STATUS_NO_LOCAL_FILE",
    "STATUS_OK",
    "TOP_K",
    "TRANSLATIONS_JSON",
    "VERDICTS",
    "VERDICTS_JSON",
    "VERDICTS_MD",
    "VERDICT_CONTRACT",
    "VERDICT_CONTRACT_RUN",
    "VERDICT_PENDING",
    "WINDOW",
    "ChatClient",
    "ChatReply",
    "ClaimRecord",
    "ExtractionResult",
    "HttpRequest",
    "Judge",
    "JudgeInput",
    "LlmCallError",
    "LlmReplyError",
    "PairVerdict",
    "PassResult",
    "Passage",
    "QueryStat",
    "Resolution",
    "Resolver",
    "RetrievalResult",
    "RunStats",
    "SourceIndex",
    "SourceText",
    "Translation",
    "TranslationCache",
    "Translator",
    "UsageTotals",
    "Verifier",
    "VerifyPlan",
    "append_ledger",
    "apply_rules",
    "build_client",
    "build_first_pass_prompt",
    "build_index",
    "build_payload",
    "build_resolver",
    "build_second_pass_prompt",
    "build_verdict_payload",
    "claim_key",
    "estimate_tokens",
    "extract_claims",
    "load_entries",
    "load_ledger",
    "locate_quote",
    "pair_key",
    "parse_json_reply",
    "plan_batches",
    "print_summary",
    "print_verify_summary",
    "retrieve_merged",
    "retrieve_passages",
    "score_distribution",
    "select_pairs",
    "strip_citations",
    "tokenize",
    "write_claims_json",
    "write_claims_md",
    "write_verdicts_json",
    "write_verdicts_md",
]
