# -*- coding: utf-8 -*-
"""编排后半程：翻译 → 双路检索 → 判定，逐对记账，随时可停可续。

一次 ``verify`` 做四件事，顺序是有讲究的：

1. **先解析链路**（确定性、不花钱）。断在取原文那几跳上的对直接判 ``source-missing``，
   既不翻译也不判定——为一份取不到的原文花钱是最冤的花法。
2. **再把论断译成英文**（批量、带缓存、只译链路通了的那些句子）。
3. **然后分对检索**：原句与译文各查一次并合并候选，记下每个候选是哪一路捞到的。
4. **最后判定**：链路通的每对一次调用；``partial`` / ``unsupported`` 追加一次反向提问。

**幂等与可续**靠 ``ledger.jsonl``：每判完一对就追加一行，键是 ``(论断 id, 引用编号)``。
重跑时已完成的对直接跳过（``--force`` 才重做），因此一次被打断的全量跑不会白丢；
``--limit`` 截的是**选取范围**（先按 ``--only`` 过滤、再取前 N 个），所以 ``--limit 5``
重跑一次就是"5 对已完成、0 对要跑"，这正是它该有的样子。

产物都落在 ``<工程根>/_primer/claims/`` 下：``ledger.jsonl``（逐对账本）、
``translations.json``（译文缓存）、``verdicts.json`` / ``verdicts.md``（人读机读两份报告）。
报告覆盖的是**选取范围**内的全部对：这次没轮到判的记 ``pending``，而不是从报告里消失。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional, Sequence

from .client import (
    DEFAULT_BASE_URL,
    DEFAULT_KEY_ENV,
    DEFAULT_MAX_TOKENS,
    DEFAULT_MODEL,
    ChatClient,
    Transport,
)
from .extract import ClaimRecord, strip_citations
from .judge import (
    SOURCE_MISSING,
    Judge,
    JudgeInput,
    PairVerdict,
    build_first_pass_prompt,
)
from .report import VERDICT_PENDING, build_verdict_payload
from .resolve import Resolution, Resolver, STATUS_OK
from .retrieve import (
    MIN_SCORE,
    QUERY_ORIGINAL,
    QUERY_TRANSLATION,
    TOP_K,
    SourceIndex,
    build_index,
    estimate_tokens,
    retrieve_merged,
    score_distribution,
)
from .translate import (
    MAX_BATCH_CHARS,
    MAX_BATCH_ITEMS,
    MAX_RETRIES,
    TRANSLATIONS_JSON,
    Translation,
    TranslationCache,
    Translator,
    build_translation_prompt,
    plan_batches,
)

LEDGER_JSONL = "ledger.jsonl"


def pair_key(claim_id: str, citation: int) -> str:
    """一对的稳定键：``论断 id#引用编号``。"""
    return f"{claim_id}#{citation}"


def pair_key_of(record: ClaimRecord) -> str:
    return pair_key(record.id, record.citation)


def load_ledger(path: Path) -> dict[str, dict]:
    """读账本；同一键出现多次时以**最后一条**为准（``--force`` 的重复就是这样覆盖的）。"""
    entries: dict[str, dict] = {}
    path = Path(path)
    if not path.is_file():
        return entries
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            # 半截行（上次运行被打断时写坏的）只该影响它自己，别的记录照读。
            continue
        if isinstance(entry, dict) and isinstance(entry.get("pair"), str):
            entries[entry["pair"]] = entry
    return entries


def append_ledger(path: Path, entry: Mapping) -> None:
    """追加一条账本记录并落盘——逐对落盘才有"随时可停"这回事。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(dict(entry), ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def match_only(record: ClaimRecord, tokens: Sequence[str]) -> bool:
    """``--only`` 的一项是否命中这一对：引用编号、论断 id、或 ``id#编号`` 键都认。"""
    if not tokens:
        return True
    key = pair_key_of(record)
    for token in tokens:
        text = token.strip()
        if not text:
            continue
        if text == key or text == record.id:
            return True
        if text.isdigit() and record.citation == int(text):
            return True
    return False


def select_pairs(
    records: Sequence[ClaimRecord], *, only: Sequence[str] = (), limit: Optional[int] = None
) -> list[ClaimRecord]:
    """先按 ``--only`` 过滤，再按 ``--limit`` 取前 N 个；顺序沿用抽取顺序。"""
    scope = [record for record in records if match_only(record, only)]
    return scope[:limit] if limit is not None else scope


def build_client(
    *,
    base_url: str = DEFAULT_BASE_URL,
    model: str = DEFAULT_MODEL,
    key_env: str = DEFAULT_KEY_ENV,
    transport: Optional[Transport] = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    environ: Optional[Mapping[str, str]] = None,
) -> ChatClient:
    """按环境变量装配客户端；密钥只在内存里，不落盘、不打印。"""
    source = environ if environ is not None else os.environ
    return ChatClient(
        base_url=base_url,
        model=model,
        key=source.get(key_env, ""),
        transport=transport,
        max_tokens=max_tokens,
    )


@dataclass
class VerifyPlan:
    """一次运行的范围划分：全部对、范围内的对、本次要处理的对、已完成的数。"""

    all_pairs: list[ClaimRecord]
    scope: list[ClaimRecord]
    selected: list[ClaimRecord]
    pending: list[ClaimRecord]
    resolutions: dict[int, Resolution]
    ledger: dict[str, dict]

    @property
    def skipped(self) -> int:
        return len(self.selected) - len(self.pending)

    @property
    def blocked_citations(self) -> int:
        """本地没有原文的**编号**个数——这是语料天花板，不是判定结论。"""
        return sum(1 for item in self.resolutions.values() if item.status != STATUS_OK)

    def judgeable(self, records: Sequence[ClaimRecord]) -> list[ClaimRecord]:
        return [record for record in records if self.resolutions[record.citation].status == STATUS_OK]

    def as_dict(self, *, limit: Optional[int], only: Sequence[str], force: bool) -> dict:
        return {
            "pairs": len(self.all_pairs),
            "scope": len(self.scope),
            "selected": len(self.selected),
            "skipped": self.skipped,
            "to_process": len(self.pending),
            "judgeable": len(self.judgeable(self.all_pairs)),
            "blocked_by_corpus": len(self.all_pairs) - len(self.judgeable(self.all_pairs)),
            "judgeable_in_scope": len(self.judgeable(self.scope)),
            "blocked_in_scope": len(self.scope) - len(self.judgeable(self.scope)),
            "blocked_citations": self.blocked_citations,
            "citations": len(self.resolutions),
            "limit": limit,
            "only": list(only),
            "force": force,
        }


@dataclass
class RunStats:
    """一次运行的实时账：判了多少对、花了多少请求与 token。"""

    processed: int = 0
    verdicts: dict = field(default_factory=dict)
    requests: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0

    def count(self, verdict: str) -> None:
        self.verdicts[verdict] = self.verdicts.get(verdict, 0) + 1

    def as_dict(self) -> dict:
        return {
            "pairs_processed": self.processed,
            "verdicts": dict(sorted(self.verdicts.items())),
            "requests": self.requests,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.prompt_tokens + self.completion_tokens,
        }


class Verifier:
    """把解析器、翻译器、判定器与账本收在一处；同一份原文的索引只建一次。"""

    def __init__(
        self,
        *,
        project_root: Path,
        resolver: Resolver,
        client: ChatClient,
        out_dir: Path,
        top_k: int = TOP_K,
        min_score: float = MIN_SCORE,
        force: bool = False,
        batch_chars: int = MAX_BATCH_CHARS,
        batch_items: int = MAX_BATCH_ITEMS,
        max_retries: int = MAX_RETRIES,
    ):
        self.project_root = Path(project_root)
        self.resolver = resolver
        self.client = client
        self.out_dir = Path(out_dir)
        self.top_k = top_k
        self.min_score = min_score
        self.force = force
        self.cache = TranslationCache(self.out_dir / TRANSLATIONS_JSON, model=client.model)
        self.translator = Translator(
            client, self.cache, max_chars=batch_chars, max_items=batch_items, max_retries=max_retries
        )
        self.judge = Judge(client, max_retries=max_retries)
        self.ledger_path = self.out_dir / LEDGER_JSONL
        self._indexes: dict[str, SourceIndex] = {}

    # ---- 范围与账本 ----

    def plan(
        self,
        records: Sequence[ClaimRecord],
        *,
        numbers: Sequence[int],
        only: Sequence[str] = (),
        limit: Optional[int] = None,
    ) -> VerifyPlan:
        """划分范围：解析全部被引编号（不花钱），再挑出本次要处理的那些对。"""
        resolutions = self.resolver.resolve_all(numbers)
        ledger = load_ledger(self.ledger_path)
        scope = select_pairs(records, only=only)
        selected = scope[:limit] if limit is not None else scope
        pending = [
            record
            for record in selected
            if self.force or pair_key_of(record) not in ledger
        ]
        return VerifyPlan(
            all_pairs=list(records),
            scope=scope,
            selected=selected,
            pending=pending,
            resolutions=resolutions,
            ledger=ledger,
        )

    # ---- 主流程 ----

    def run(self, plan: VerifyPlan) -> RunStats:
        """处理 ``plan.pending``：译一批、判一对、落一行账。"""
        stats = RunStats()
        before = self.client.usage
        snapshot = (before.requests, before.prompt_tokens, before.completion_tokens)
        translatable = [
            strip_citations(record.claim)
            for record in plan.pending
            if plan.resolutions[record.citation].status == STATUS_OK
        ]
        translations = self.translator.translate_all(translatable)
        for record in plan.pending:
            entry = self.process(record, plan.resolutions[record.citation], translations)
            append_ledger(self.ledger_path, entry)
            stats.processed += 1
            stats.count(entry["verdict"])
        after = self.client.usage
        # 用量按客户端的累计差值记：翻译、判定、重试都在里面，比逐对求和更实在。
        stats.requests = after.requests - snapshot[0]
        stats.prompt_tokens = after.prompt_tokens - snapshot[1]
        stats.completion_tokens = after.completion_tokens - snapshot[2]
        return stats

    def process(
        self,
        record: ClaimRecord,
        resolution: Resolution,
        translations: Mapping[str, Translation],
    ) -> dict:
        """判一对（链路断的不调用模型），返回可以直接进账本的一条记录。"""
        if resolution.status != STATUS_OK:
            return _source_missing_record(record, resolution)
        query = strip_citations(record.claim)
        translation = translations.get(query)
        try:
            index = self._index(resolution)
        except OSError as error:
            return _source_missing_record(
                record, resolution, extra_note=f"the converted markdown could not be read: {error}"
            )
        queries = [(QUERY_ORIGINAL, query)]
        if translation is not None and translation.usable:
            queries.append((QUERY_TRANSLATION, translation.translation or ""))
        retrieval = retrieve_merged(queries, index, k=self.top_k, min_score=self.min_score)
        item = JudgeInput(
            pair_id=pair_key_of(record),
            claim_id=record.id,
            citation=record.citation,
            claim=record.claim,
            translation=translation.translation if translation and translation.usable else None,
            entry_title=_entry_field(resolution, "title"),
            entry_venue=_entry_field(resolution, "venue_year"),
            source_markdown=resolution.source.markdown if resolution.source else "",
            source_chars=resolution.source.text_chars if resolution.source else 0,
            candidates_status=retrieval.status,
            passages=tuple(retrieval.passages),
            queries=retrieval.queries,
        )
        verdict = self.judge.judge(item, index.text)
        return _judged_record(record, resolution, translation, retrieval, verdict)

    # ---- 报价（--dry-run） ----

    def cached_translations(self, plan: VerifyPlan) -> dict[str, Translation]:
        """只取缓存里已有的译文——``--dry-run`` 用它报价，绝不发请求。"""
        out: dict[str, Translation] = {}
        for record in plan.pending:
            if plan.resolutions[record.citation].status != STATUS_OK:
                continue
            text = strip_citations(record.claim)
            if text in out:
                continue
            value = self.cache.get(text)
            if value:
                out[text] = Translation(
                    source=text, translation=value, status=STATUS_OK, cached=True
                )
        return out

    def estimate(self, plan: VerifyPlan, translations: Mapping[str, Translation]) -> dict:
        """不花钱地报一次价：要发多少请求、提示词大约多少 token、得分分布如何。

        报价按**现在就能算出来的东西**算：译文缓存里已有的句子照用；还没译的句子在判定
        提示词里按"与中文论断等量 token"占位（同一段意思，中英 token 数相当）。因此提示词
        一项是**下界**——译文到位后检索可能多捞出几个窗口，每个窗口最多
        :data:`primer.claims.retrieve.WINDOW` 字。这条假设写在返回值的 ``assumptions`` 里。
        """
        needs = [
            strip_citations(record.claim)
            for record in plan.pending
            if plan.resolutions[record.citation].status == STATUS_OK
            and not _usable(translations.get(strip_citations(record.claim)))
        ]
        unique = sorted(set(needs))
        items = [(f"{index}", text) for index, text in enumerate(unique)]
        batches = plan_batches(items)
        translation_tokens = sum(
            estimate_tokens(build_translation_prompt(batch)) for batch in batches
        )

        first_tokens = 0
        second_tokens = 0
        scores: list[float] = []
        judgeable = 0
        for record in plan.pending:
            resolution = plan.resolutions[record.citation]
            if resolution.status != STATUS_OK:
                continue
            try:
                index = self._index(resolution)
            except OSError:
                continue
            judgeable += 1
            query = strip_citations(record.claim)
            translation = translations.get(query)
            queries = [(QUERY_ORIGINAL, query)]
            if _usable(translation):
                queries.append((QUERY_TRANSLATION, translation.translation or ""))
            retrieval = retrieve_merged(queries, index, k=self.top_k, min_score=self.min_score)
            scores.append(retrieval.best_score)
            item = JudgeInput(
                pair_id=pair_key_of(record),
                claim_id=record.id,
                citation=record.citation,
                claim=record.claim,
                translation=translation.translation if _usable(translation) else record.claim,
                entry_title=_entry_field(resolution, "title"),
                entry_venue=_entry_field(resolution, "venue_year"),
                source_markdown=resolution.source.markdown if resolution.source else "",
                source_chars=resolution.source.text_chars if resolution.source else 0,
                candidates_status=retrieval.status,
                passages=tuple(retrieval.passages),
                queries=retrieval.queries,
            )
            first_tokens += estimate_tokens(build_first_pass_prompt(item))
            # 反向那一遍的提示词比第一遍只多几行话术与第一遍的结论，加一成计。
            second_tokens += int(estimate_tokens(build_first_pass_prompt(item)) * 1.1)
        return {
            "to_process": len(plan.pending),
            "judgeable_to_process": judgeable,
            "blocked_to_process": len(plan.pending) - judgeable,
            "translation": {
                "distinct_sentences_needing_translation": len(unique),
                "sentences_served_from_cache": sum(1 for item in translations.values() if item.cached),
                "batches": len(batches),
                "approx_prompt_tokens": translation_tokens,
            },
            "judge": {
                "first_pass_calls": judgeable,
                "second_pass_calls_at_most": judgeable,
                "approx_prompt_tokens_first_pass": first_tokens,
                "approx_prompt_tokens_second_pass_at_most": second_tokens,
            },
            "min_score_recheck": score_distribution(scores, self.min_score),
            "approx_prompt_tokens_total": translation_tokens + first_tokens + second_tokens,
            "assumptions": [
                "prompt tokens are estimated with primer.claims.retrieve.estimate_tokens "
                "(CJK by characters, Latin by words)",
                "a not-yet-translated claim stands in for its own English translation in the judge "
                "prompt (same content, comparable token count)",
                "passages are priced from the retrieval actually run at dry-run time; with the "
                "translation in hand the merged retrieval can return more windows, so this is a "
                "lower bound",
                "completion tokens are not estimated: deepseek-flash is an always-thinking model "
                "whose reasoning chain also draws on the output budget",
                "the second pass is priced at the first pass's size plus 10% for its extra framing",
            ],
        }

    # ---- 报告 ----

    def render(
        self,
        plan: VerifyPlan,
        *,
        inputs: Mapping,
        limit,
        only,
        force,
        usage: Mapping,
        translation: Mapping,
    ) -> dict:
        """把账本与本次运行的状态装配成 ``verdicts.json`` 的内容。"""
        ledger = load_ledger(self.ledger_path)
        records = [_record_for(record, plan, ledger) for record in plan.scope]
        return build_verdict_payload(
            records=records,
            plan=plan.as_dict(limit=limit, only=only, force=force),
            inputs=dict(inputs),
            translation={
                **dict(translation),
                "note": (
                    "these counters cover this run only; every pair record carries its own "
                    "translation and translation_status"
                ),
            },
            usage=dict(usage),
        )

    def _index(self, resolution: Resolution) -> SourceIndex:
        """按原文缓存索引：一份原文被几百条论断引用时只建一次。"""
        assert resolution.source is not None
        key = resolution.source.local_file
        index = self._indexes.get(key)
        if index is None:
            text = self.resolver.text_for(key, self.project_root / resolution.source.markdown)
            index = build_index(text)
            self._indexes[key] = index
        return index


def _usable(translation: Optional[Translation]) -> bool:
    """这份译文能不能当查询串用；``None`` 与翻译失败都算不能。"""
    return translation is not None and translation.usable


def _entry_field(resolution: Resolution, name: str) -> str:
    entry = resolution.entry or {}
    value = entry.get(name)
    return str(value) if value else ""


def _base_record(record: ClaimRecord, resolution: Resolution) -> dict:
    return {
        "pair": pair_key_of(record),
        "claim_id": record.id,
        "citation": record.citation,
        "file": record.file,
        "line": record.line,
        "origin": record.origin,
        "token": record.token,
        "claim": record.claim,
        "resolution": resolution.status,
        "breakpoint": resolution.evidence[-1] if resolution.evidence else "",
    }


def _source_missing_record(
    record: ClaimRecord, resolution: Resolution, extra_note: Optional[str] = None
) -> dict:
    """链路断掉：判 ``source-missing``，既不翻译也不调用模型。"""
    notes = [resolution.evidence[-1] if resolution.evidence else f"resolution status {resolution.status}"]
    if extra_note:
        notes.append(extra_note)
    return {
        **_base_record(record, resolution),
        "translation": None,
        "translation_status": None,
        "candidates_status": None,
        "retrieval": None,
        "verdict": SOURCE_MISSING,
        "quote": None,
        "quote_start": None,
        "quote_end": None,
        "quote_line": None,
        "rationale": "",
        "notes": notes,
        "first_pass": None,
        "second_pass": None,
        "agree": None,
        "needs_review": False,
        "calls": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
    }


def _judged_record(
    record: ClaimRecord,
    resolution: Resolution,
    translation: Optional[Translation],
    retrieval,
    verdict: PairVerdict,
) -> dict:
    """把一次判定落成账本记录：链路、译文、双路检索的账与两遍判定都在里面。"""
    payload = verdict.as_dict()
    query_stats = {
        stat.name: {
            "query_tokens": stat.query_tokens,
            "matched_tokens": stat.matched_tokens,
            "status": stat.status,
            "best_score": stat.best_score,
        }
        for stat in retrieval.queries
    }
    return {
        **_base_record(record, resolution),
        "translation": translation.translation if translation and translation.usable else None,
        "translation_status": translation.status if translation else None,
        "translation_error": translation.error if translation and translation.error else None,
        "candidates_status": retrieval.status,
        "retrieval": {
            "query_tokens": retrieval.query_tokens,
            "matched_tokens": retrieval.matched_tokens,
            "best_score": retrieval.best_score,
            "passages": len(retrieval.passages),
            "queries": query_stats,
        },
        **{key: payload[key] for key in ("verdict", "quote", "quote_start", "quote_end", "quote_line")},
        "rationale": payload["rationale"],
        "notes": payload["notes"],
        "first_pass": payload["first_pass"],
        "second_pass": payload["second_pass"],
        "agree": payload["agree"],
        "needs_review": payload["needs_review"],
        "calls": payload["calls"],
        "prompt_tokens": payload["prompt_tokens"],
        "completion_tokens": payload["completion_tokens"],
    }


def _record_for(record: ClaimRecord, plan: VerifyPlan, ledger: Mapping[str, dict]) -> dict:
    """报告里的一行：账本里有就用账本，没有就记 ``pending``（范围里的对不该消失）。"""
    entry = ledger.get(pair_key_of(record))
    if entry is not None:
        return dict(entry)
    resolution = plan.resolutions[record.citation]
    return {
        **_base_record(record, resolution),
        "translation": None,
        "translation_status": None,
        "candidates_status": None,
        "retrieval": None,
        "verdict": VERDICT_PENDING,
        "quote": None,
        "quote_start": None,
        "quote_end": None,
        "quote_line": None,
        "rationale": "",
        "notes": [],
        "first_pass": None,
        "second_pass": None,
        "agree": None,
        "needs_review": False,
        "calls": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
    }


__all__ = [
    "LEDGER_JSONL",
    "RunStats",
    "Verifier",
    "VerifyPlan",
    "append_ledger",
    "build_client",
    "load_ledger",
    "match_only",
    "pair_key",
    "pair_key_of",
    "select_pairs",
]
