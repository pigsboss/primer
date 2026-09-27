# -*- coding: utf-8 -*-
"""把抽取、解析、检索三件东西写成产物：``claims.json``（机器读）与 ``claims.md``（人读）。

``claims.json`` 的结构固定为 ``{summary, inputs, verdict_contract, resolution_index, claims}``。
``resolution_index`` 按引用编号去重存放链路（条目、本地 PDF、转换产物、证据行），
``claims`` 里的每条记录只带编号与状态，需要看链路时去索引里取——这样同一份原文被
几百条论断引用时不会把证据行抄几百遍。

**后半程（LLM 判定）不在本模块服务范围内**：``verdict`` / ``quote`` / ``rationale``
三个字段一律留空，取值约定写在 ``verdict_contract`` 里，随产物一起交付：

* ``supported | partial | unsupported | source-missing | unverifiable`` 五选一；
* 除 ``source-missing`` 以外的每一种判定都必须附一段**逐字抄自原文**的引文，
  不能改写、不能拼接；
* 候选段落为空（``no-candidates``）只能判 ``unverifiable``，**绝不可**判 ``unsupported``
  ——"没找到证据"不等于"证据不成立"；
* 链路没走通（``resolution != "ok"``）判 ``source-missing``，此判定优先于上面两条。

``claims.md`` 的顺序是有意的：先报"引用根本解析不到"的编号——一个解析不到原文的引用
本身就是最可疑的信号，比"证据弱"更值得先看；然后才是逐文件明细。
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Mapping, Optional, Sequence

from ..literature.paths import relative_to_root
from .extract import ClaimRecord, ExtractionResult
from .resolve import Resolution, STATUSES
from .retrieve import (
    CHARS_PER_TOKEN,
    TOP_K,
    STATUS_NONE,
    STATUS_OK,
    STATUS_WEAK,
    RetrievalResult,
    estimate_tokens,
    score_distribution,
)

CLAIMS_JSON = "claims.json"
CLAIMS_MD = "claims.md"
VERDICTS_JSON = "verdicts.json"
VERDICTS_MD = "verdicts.md"

VERDICTS = ("supported", "partial", "unsupported", "source-missing", "unverifiable")

VERDICT_PENDING = "pending"
"""还没判的对。``verify`` 可以分批跑，产物里因此必须有一个"未判"的口径。"""

VERDICT_CONTRACT = {
    "status": "not-run",
    "note": (
        "verdict/quote/rationale are intentionally empty: this artifact is the deterministic "
        "half (extraction, resolution, candidate retrieval). The LLM judgement pass fills them."
    ),
    "verdicts": list(VERDICTS),
    "rules": [
        "one of: supported | partial | unsupported | source-missing | unverifiable",
        "every verdict except source-missing requires a verbatim quote copied from the source text",
        "the quote must be an exact substring of the cited markdown (no rewording, no ellipsis)",
        "no candidate passages => unverifiable, never unsupported",
        "resolution != ok => source-missing, which overrides the two rules above",
    ],
    "fields": {
        "verdict": "one of verdicts, or null when the pass has not run",
        "quote": "verbatim passage from the cited source, or null",
        "rationale": "short free text, or null",
    },
}

VERDICT_CONTRACT_RUN = {
    **VERDICT_CONTRACT,
    "status": "run",
    "note": (
        "verdicts.json is the LLM judgement pass. The verdict is the first pass; the second pass "
        "runs only on partial/unsupported and its verdict is kept beside the first, never averaged."
    ),
    "rules": [
        "resolution != ok => source-missing; the break point comes from the deterministic half",
        "no candidate passages => unverifiable, never unsupported: a failed search is not evidence",
        "supported | partial | unsupported must carry a verbatim quote from the cited source, with "
        "its character offset (quote_start/quote_end) and line number",
        "a verdict returned without a usable verbatim quote is downgraded to unverifiable, and the "
        "reason is recorded in notes",
        "partial | unsupported are re-judged in a second, oppositely framed pass; both passes are "
        "recorded and agree=false marks a disagreement for human review",
        "a model-returned source-missing is recorded as unverifiable when the chain resolves: "
        "source-missing is decided by the chain, not by the model",
    ],
    "fields": {
        "verdict": "one of verdicts, or 'pending' when the pair has not been judged yet",
        "quote": "verbatim span copied from the cited source, or null",
        "rationale": "the model's one-or-two-sentence reason",
        "second_pass": "the opposite-framing re-judgement for partial/unsupported, or null",
        "agree": "true/false when a second pass ran, null otherwise",
    },
}


def build_payload(
    *,
    extraction: ExtractionResult,
    resolutions: Mapping[int, Resolution],
    retrievals: Mapping[str, RetrievalResult],
    inputs: Mapping[str, dict],
    literature: Mapping[str, object],
    source_language: Optional[Mapping[str, object]] = None,
    include_tables: bool,
    top_k: int = TOP_K,
) -> dict:
    """装配 ``claims.json`` 的内容。"""
    claims = [_claim_payload(record, resolutions, retrievals) for record in extraction.claims]
    resolution_counts = Counter(item.status for item in resolutions.values())
    candidate_counts = Counter(item["candidates_status"] for item in claims)
    by_file = _by_file(extraction, claims)
    cited = sorted({item["citation"] for item in claims})
    return {
        "summary": {
            "generated_by": "primer.claims extract",
            "include_tables": include_tables,
            "top_k": top_k,
            "files": list(extraction.files),
            "claim_pairs": len(claims),
            "claim_sentences": len({(item["file"], item["claim_start"]) for item in claims}),
            "citation_markers": sum(extraction.tokens_by_file.values()),
            "unique_citations": len(cited),
            "by_file": by_file,
            "resolution": {status: resolution_counts.get(status, 0) for status in STATUSES},
            "candidates": {
                STATUS_OK: candidate_counts.get(STATUS_OK, 0),
                STATUS_WEAK: candidate_counts.get(STATUS_WEAK, 0),
                STATUS_NONE: candidate_counts.get(STATUS_NONE, 0),
            },
            "excluded": {
                item.reason: {
                    "markers": sum(batch.tokens for batch in extraction.excluded if batch.reason == item.reason),
                    "pairs": sum(batch.pairs for batch in extraction.excluded if batch.reason == item.reason),
                }
                for item in extraction.excluded
            },
            "table_rows": {"markers": extraction.table_tokens, "pairs": extraction.table_pairs},
            "ambiguous": list(extraction.ambiguous),
            "rejected_brackets": list(extraction.rejected),
            "source_language": dict(source_language or {}),
            "llm_pass": _llm_pass(claims, top_k),
        },
        "inputs": dict(inputs),
        "literature_ledger": dict(literature),
        "verdict_contract": VERDICT_CONTRACT,
        "resolution_index": {
            str(number): _resolution_payload(resolutions[number]) for number in sorted(resolutions)
        },
        "claims": claims,
    }


def _claim_payload(
    record: ClaimRecord,
    resolutions: Mapping[int, Resolution],
    retrievals: Mapping[str, RetrievalResult],
) -> dict:
    resolution = resolutions.get(record.citation)
    status = resolution.status if resolution else "entry-missing"
    retrieval = retrievals.get(record.id)
    passages = list(retrieval.passages) if retrieval else []
    return {
        "id": record.id,
        "file": record.file,
        "line": record.line,
        "citation": record.citation,
        "token": record.token,
        "claim": record.claim,
        "paragraph": record.paragraph,
        "origin": record.origin,
        "claim_start": record.claim_start,
        "claim_end": record.claim_end,
        "resolution": status,
        "candidates_status": retrieval.status if retrieval else STATUS_NONE,
        "query_tokens": retrieval.query_tokens if retrieval else 0,
        "matched_tokens": retrieval.matched_tokens if retrieval else 0,
        "candidates": [
            {
                "start": passage.start,
                "end": passage.end,
                "score": passage.score,
                "text": passage.text,
            }
            for passage in passages
        ],
        "verdict": None,
        "quote": None,
        "rationale": None,
    }


def _resolution_payload(resolution: Resolution) -> dict:
    return {
        "number": resolution.number,
        "status": resolution.status,
        "entry": resolution.entry,
        "source": {
            "local_file": resolution.source.local_file,
            "markdown": resolution.source.markdown,
            "text_chars": resolution.source.text_chars,
            "header_chars": resolution.source.header_chars,
            "md5": resolution.source.md5,
            "line_count": resolution.source.line_count,
        }
        if resolution.source
        else None,
        "audit_verdict": resolution.audit_verdict,
        "audit_confidence": resolution.audit_confidence,
        "shared_arxiv": resolution.shared_arxiv,
        "evidence": list(resolution.evidence),
    }


def _by_file(extraction: ExtractionResult, claims: Sequence[dict]) -> dict:
    table: dict[str, dict] = {}
    for name in extraction.files:
        table[name] = {
            "markers": extraction.tokens_by_file.get(name, 0),
            "pairs": extraction.pairs_by_file.get(name, 0),
            "kept_pairs": 0,
            "unique_citations": 0,
            "resolution": {},
            "candidates": {},
        }
    citations: dict[str, set] = {name: set() for name in extraction.files}
    for item in claims:
        row = table[item["file"]]
        row["kept_pairs"] += 1
        citations[item["file"]].add(item["citation"])
        row["resolution"][item["resolution"]] = row["resolution"].get(item["resolution"], 0) + 1
        row["candidates"][item["candidates_status"]] = row["candidates"].get(item["candidates_status"], 0) + 1
    for name, numbers in citations.items():
        table[name]["unique_citations"] = len(numbers)
    return table


def _llm_pass(claims: Sequence[dict], top_k: int) -> dict:
    """给后半程报价：每条记录要送进去的字符数与 token 数（粗估，见 ``chars_per_token``）。"""
    claim_chars = 0
    passage_chars = 0
    reproducible = 0
    no_text = 0
    tokens = 0
    for item in claims:
        claim_chars += len(item["claim"])
        if item["resolution"] != "ok":
            no_text += 1
            continue
        reproducible += 1
        passage_chars += sum(len(passage["text"]) for passage in item["candidates"])
        tokens += estimate_tokens(item["claim"])
        tokens += sum(estimate_tokens(passage["text"]) for passage in item["candidates"])
    return {
        "pairs": len(claims),
        "pairs_with_usable_text": reproducible,
        "pairs_without_text": no_text,
        "claim_chars": claim_chars,
        "passage_chars": passage_chars,
        "total_chars": claim_chars + passage_chars,
        "top_k": top_k,
        "chars_per_token": CHARS_PER_TOKEN,
        "approx_tokens": tokens,
        "note": (
            "approx_tokens is a rough estimate over claim + candidate passages actually returned; "
            "multiply by the judge model's call overhead to price the second half."
        ),
    }


def write_claims_json(out_dir: Path, payload: Mapping) -> Path:
    """写 ``claims.json``。"""
    path = Path(out_dir) / CLAIMS_JSON
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return path


def write_claims_md(out_dir: Path, payload: Mapping) -> Path:
    """写 ``claims.md``：先报解析不到的引用，再逐文件明细。"""
    path = Path(out_dir) / CLAIMS_MD
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = _render_md(payload)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _render_md(payload: Mapping) -> list[str]:
    summary = payload["summary"]
    index = payload["resolution_index"]
    claims = payload["claims"]
    pass_size = summary["llm_pass"]
    lines: list[str] = [
        "# 论断—引用链路报告",
        "",
        "本报告由 `python -m primer.claims extract` 生成，只做**确定性**的三件事："
        "从正文抽出带引用的论断、沿"
        "（文献条目 → 本地 PDF → 转换文本 → 可用文本）解析每个编号、在原文里检索候选段落。"
        "判定（supported / partial / unsupported / source-missing / unverifiable）留给后半程的 LLM，"
        "本报告不含任何判定结论。",
        "",
        "## 一、计数",
        "",
        f"- 论断—引用对（claim × citation）：**{summary['claim_pairs']}**",
        f"- 引用标记（按原文写法计）：{summary['citation_markers']}",
        f"- 不同论断句子：{summary['claim_sentences']}",
        f"- 被引用的不同编号：{summary['unique_citations']}",
        f"- 表格行引用：默认{'已并入' if summary['include_tables'] else '未并入'}"
        f"（{summary['table_rows']['pairs']} 对，{summary['table_rows']['markers']} 个标记）",
        "",
        "### 链路解析（按引用编号去重）",
        "",
        "| 状态 | 编号数 | 含义 |",
        "|:--|--:|:--|",
    ]
    meaning = {
        "ok": "四跳全通，原文文本可用",
        "entry-missing": "参考文献表里没有这个编号",
        "no-local-file": "有条目，但没有可用的本地原文",
        "not-converted": "本地 PDF 在，但还没转成 markdown",
        "empty-text": "转出的 markdown 去掉引用头后正文太少",
    }
    for status in STATUSES:
        lines.append(f"| `{status}` | {summary['resolution'].get(status, 0)} | {meaning[status]} |")
    lines += [
        "",
        "### 候选段落（按论断—引用对计）",
        "",
        "| 状态 | 对数 | 含义 |",
        "|:--|--:|:--|",
        f"| `ok` | {summary['candidates'][STATUS_OK]} | 找到得分达标的候选 |",
        f"| `candidates-weak` | {summary['candidates'][STATUS_WEAK]} | 有命中但得分低，仍给出最好的几个 |",
        f"| `no-candidates` | {summary['candidates'][STATUS_NONE]} | 一个窗口都没命中（多为链路未通） |",
        "",
        "### 后半程规模（粗估）",
        "",
        f"- 需要判定的对：{pass_size['pairs_with_usable_text']}（另有 {pass_size['pairs_without_text']} 对无原文，"
        "只能判 `source-missing`）",
        f"- 送入内容：论断 {pass_size['claim_chars']} 字 + 候选段落 {pass_size['passage_chars']} 字 "
        f"= {pass_size['total_chars']} 字",
        f"- 约 **{pass_size['approx_tokens']:,} token**（中文按 {pass_size['chars_per_token']} 字/token 粗估）",
        "",
    ]
    language = summary.get("source_language") or {}
    if language.get("sources"):
        lines += [
            "### 原文语言（影响候选质量）",
            "",
            f"- 能取到原文的引用 {language['sources']} 条，其中**全英文**原文 {language['english_only']} 条。",
            "- 论断是中文、原文是英文时，字面检索只能靠论断里的拉丁词（任务名、仪器名、年份）命中，"
            "候选多半落在 `candidates-weak` 或 `no-candidates`——这是检索方法的口径，不是链路断了。"
            "后半程要么把论断译成英文再查，要么换多语种向量检索。",
            "",
        ]

    unresolved = [item for item in claims if item["resolution"] != "ok"]
    lines += [
        "## 二、解析不到底的引用（先看这里）",
        "",
        "一个引用如果连原文都取不到，它标注的论断就无从核对——这本身比「证据弱」更值得先看。"
        "下表按编号聚合，列出每个编号的链路断点与受影响的对数。",
        "",
    ]
    if not unresolved:
        lines.append("（无：所有引用都解析到了可用文本。）")
    else:
        grouped: dict[int, list[dict]] = {}
        for item in unresolved:
            grouped.setdefault(item["citation"], []).append(item)
        lines += ["| 编号 | 状态 | 断点 | 对数 | 例句 |", "|--:|:--|:--|--:|:--|"]
        for number in sorted(grouped, key=lambda key: (-len(grouped[key]), key)):
            items = grouped[number]
            entry = index.get(str(number), {})
            breakpoint = entry.get("evidence", [""])[-1] if entry.get("evidence") else ""
            example = items[0]["claim"].replace("|", "\\|")
            lines.append(
                f"| [{number}] | `{items[0]['resolution']}` | {breakpoint.replace('|', '\\|')} "
                f"| {len(items)} | {example[:60]} |"
            )

    lines += ["", "## 三、逐文件明细", ""]
    for name, row in summary["by_file"].items():
        lines += [
            f"### {name}",
            "",
            f"- 引用标记 {row['markers']} 个，展开为 {row['pairs']} 对；纳入本报告的 {row['kept_pairs']} 对，"
            f"涉及 {row['unique_citations']} 个不同编号",
            "- 链路：" + "，".join(f"{key} {value}" for key, value in sorted(row["resolution"].items())),
            "- 候选：" + ("，".join(f"{key} {value}" for key, value in sorted(row["candidates"].items())) or "无"),
            "",
        ]

    if summary["excluded"]:
        lines += ["### 被排除的上下文", "", "| 原因 | 标记 | 对 |", "|:--|--:|--:|"]
        for reason, row in sorted(summary["excluded"].items()):
            lines.append(f"| `{reason}` | {row['markers']} | {row['pairs']} |")
        lines.append("")
    if summary["rejected_brackets"]:
        lines += [
            "### 形似引用但不作引用的方括号",
            "",
            "下列片段方括号内含数字，但不是纯编号引用（插图编号、编号后带字的写法等），一律不计：",
            "",
            "　".join(f"`{token}`" for token in summary["rejected_brackets"]),
            "",
        ]
    if summary["ambiguous"]:
        lines += ["### 存疑行（只报告、不猜）", ""]
        lines += [f"- {item}" for item in summary["ambiguous"]]
        lines.append("")
    return lines


def print_summary(payload: Mapping, out_dir: Path, project_root: Optional[Path] = None) -> None:
    """把同样的计数打到 stdout（英文，见 CODING_STANDARDS 第 2.2 节）。

    产物路径一律相对工程根打印——stdout 里不出现绝对路径是全局约定。
    """
    summary = payload["summary"]
    resolution = summary["resolution"]
    candidates = summary["candidates"]
    pass_size = summary["llm_pass"]
    print("primer.claims extract")
    for name, row in summary["by_file"].items():
        print(f"  body: {name} markers={row['markers']} pairs={row['pairs']} kept={row['kept_pairs']}")
    print(f"  bodies: {len(summary['by_file'])} file(s)")
    print(f"  citation markers: {summary['citation_markers']}")
    print(f"  claim-citation pairs: {summary['claim_pairs']}"
          f" ({summary['claim_sentences']} distinct sentences, {summary['unique_citations']} distinct citations)")
    if not summary["include_tables"]:
        print(f"  table rows excluded: {summary['table_rows']['pairs']} pairs "
              f"({summary['table_rows']['markers']} markers); pass --include-tables to keep them")
    print("  resolution (per citation number):")
    for status in STATUSES:
        print(f"    {status:16s} {resolution.get(status, 0)}")
    print("  candidates (per claim-citation pair):")
    print(f"    {'ok':16s} {candidates[STATUS_OK]}")
    print(f"    {'candidates-weak':16s} {candidates[STATUS_WEAK]}")
    print(f"    {'no-candidates':16s} {candidates[STATUS_NONE]}")
    print("  estimated LLM pass (top-k passages per pair):")
    print(f"    pairs needing judgement: {pass_size['pairs_with_usable_text']}"
          f" (+{pass_size['pairs_without_text']} with no source text)")
    print(f"    characters: {pass_size['total_chars']}"
          f" (claims {pass_size['claim_chars']} + passages {pass_size['passage_chars']})")
    print(f"    approx tokens: {pass_size['approx_tokens']:,}")
    print(f"  wrote {relative_to_root(Path(out_dir) / CLAIMS_JSON, project_root)}")
    print(f"  wrote {relative_to_root(Path(out_dir) / CLAIMS_MD, project_root)}")


def _cell(text: object, limit: int = 0) -> str:
    """把一段文字放进 markdown 表格单元格：去换行、转义竖线，必要时截断。"""
    value = re.sub(r"\s+", " ", str(text or "")).strip().replace("|", "\\|")
    if limit and len(value) > limit:
        value = value[: limit - 1] + "…"
    return value


def _quote_block(text: object) -> list[str]:
    """一段引文按行渲染成引用块——引文里保留换行，才看得出原文的断行。"""
    lines = str(text or "").split("\n")
    return ["> " + line if line else ">" for line in lines or [""]]


def verdict_counts(records: Sequence[Mapping]) -> dict:
    """按判定计数；没有 ``verdict`` 的对记成 ``pending``。"""
    counts = {verdict: 0 for verdict in VERDICTS}
    counts[VERDICT_PENDING] = 0
    for record in records:
        verdict = record.get("verdict")
        key = verdict if verdict in counts else VERDICT_PENDING
        counts[key] += 1
    return counts


def _query_stat(record: Mapping, name: str) -> Mapping:
    retrieval = record.get("retrieval") or {}
    queries = retrieval.get("queries") if isinstance(retrieval, Mapping) else None
    if isinstance(queries, Mapping) and isinstance(queries.get(name), Mapping):
        return queries[name]
    return {}


def retrieval_summary(records: Sequence[Mapping]) -> dict:
    """译文对检索的贡献：多少对从"零重合"变成有重合，多少对仍然一条都没命中。

    只统计**真的检索过**的对（账本里有 ``retrieval`` 的）；还没判的对没有检索结果，
    把它们的零当成"检索落空"会让这张小账失真。
    """
    retrieved = [record for record in records if record.get("retrieval")]
    with_source = [record for record in records if record.get("resolution") == STATUS_OK]
    improved = 0
    still_empty = 0
    translation_only = 0
    original_empty = 0
    translation_matched_more = 0
    original_total = 0
    translation_total = 0
    best_scores: list[float] = []
    for record in retrieved:
        original = _query_stat(record, "original").get("matched_tokens", 0) or 0
        translation = _query_stat(record, "translation").get("matched_tokens", 0) or 0
        merged = (record.get("retrieval") or {}).get("matched_tokens", 0) or 0
        original_total += original
        translation_total += translation
        if translation > original:
            translation_matched_more += 1
        if original == 0:
            original_empty += 1
            if merged > 0:
                improved += 1
        if merged == 0:
            still_empty += 1
        if translation > 0 and original == 0:
            translation_only += 1
        score = (record.get("retrieval") or {}).get("best_score")
        if isinstance(score, (int, float)):
            best_scores.append(float(score))
    return {
        "pairs_with_source_text_in_report": len(with_source),
        "pairs_retrieved": len(retrieved),
        "pairs_with_zero_overlap_on_the_original_query": original_empty,
        "improved_to_non_zero_overlap_by_translation": improved,
        "improved_only_by_the_translation_query": translation_only,
        "still_zero_overlap_after_merging": still_empty,
        "pairs_where_the_translation_query_matched_more": translation_matched_more,
        "matched_tokens_original_query_total": original_total,
        "matched_tokens_translation_query_total": translation_total,
        "best_score_distribution": score_distribution(best_scores),
    }


def pass_summary(records: Sequence[Mapping]) -> dict:
    """两遍判定的账：跑了多少次第二遍、一致/不一致各多少。"""
    second = 0
    agree = 0
    disagree = 0
    for record in records:
        if not record.get("second_pass"):
            continue
        second += 1
        if record.get("agree") is False:
            disagree += 1
        elif record.get("agree") is True:
            agree += 1
    return {
        "second_pass_run": second,
        "agree": agree,
        "disagree": disagree,
        "needs_review": disagree,
        "second_pass_skipped": len(records) - second,
    }


def build_verdict_payload(
    *,
    records: Sequence[Mapping],
    plan: Mapping,
    inputs: Mapping,
    translation: Mapping,
    usage: Mapping,
) -> dict:
    """装配 ``verdicts.json``。所有计数都从 ``records`` 现算，因此分批跑也自洽。"""
    counts = verdict_counts(records)
    return {
        "summary": {
            "generated_by": "primer.claims verify",
            "plan": dict(plan),
            "verdicts": counts,
            "retrieval": retrieval_summary(records),
            "passes": pass_summary(records),
            "translation": dict(translation),
            "usage": dict(usage),
        },
        "inputs": dict(inputs),
        "verdict_contract": VERDICT_CONTRACT_RUN,
        "unjudgeable": _unjudgeable(records),
        "pairs": [dict(record) for record in records],
    }


def _unjudgeable(records: Sequence[Mapping]) -> list[dict]:
    """按引用编号汇总"没有原文可判"的对：这是本工程的语料天花板。"""
    grouped: dict[int, dict] = {}
    for record in records:
        status = record.get("resolution")
        if status == STATUS_OK:
            continue
        row = grouped.setdefault(
            int(record.get("citation") or 0),
            {"citation": int(record.get("citation") or 0), "resolution": status, "pairs": 0, "example": ""},
        )
        row["pairs"] += 1
        if not row["example"]:
            row["example"] = str(record.get("claim") or "")
    return [grouped[number] for number in sorted(grouped)]


def write_verdicts_json(out_dir: Path, payload: Mapping) -> Path:
    """写 ``verdicts.json``。"""
    path = Path(out_dir) / VERDICTS_JSON
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return path


def write_verdicts_md(out_dir: Path, payload: Mapping) -> Path:
    """写 ``verdicts.md``：先报 ``unsupported``，再 ``partial``，最后汇总表。"""
    path = Path(out_dir) / VERDICTS_MD
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(_render_verdicts(payload)) + "\n", encoding="utf-8")
    return path


def _render_verdicts(payload: Mapping) -> list[str]:
    summary = payload["summary"]
    plan = summary["plan"]
    counts = summary["verdicts"]
    retrieval = summary["retrieval"]
    passes = summary["passes"]
    usage = summary["usage"]
    records = payload["pairs"]
    judged = len(records)
    lines = [
        "# 论断—引用判定报告",
        "",
        "本报告由 `python -m primer.claims verify` 生成：论断（中文）先译成英文，再在引用的原文里"
        "检索候选段落，然后由模型判定论断是否真的被原文支撑。每条判定都附**逐字抄自原文的引文**"
        "与它在原文里的字符偏移、行号——没有引文的判定一律降级为 `unverifiable`。",
        "",
        "## 判不了与判得了：先说清楚天花板",
        "",
        f"- 抽取到的论断—引用对：**{plan.get('pairs', 0)}**",
        f"- 报告覆盖：**{plan.get('scope', 0)}** 对（范围内已判 "
        f"{judged - counts.get(VERDICT_PENDING, 0)}，待判 {counts.get(VERDICT_PENDING, 0)}）",
        f"- 本次运行选取 {plan.get('selected', 0)} 对，其中跳过已完成 {plan.get('skipped', 0)} 对",
        f"- 链路解析到可用原文、**可以判**的对：**{plan.get('judgeable', 0)}**"
        f"（报告范围内 {plan.get('judgeable_in_scope', 0)}）",
        f"- 本地没有原文、**判不了**的对：**{plan.get('blocked_by_corpus', 0)}**"
        f"（报告范围内 {plan.get('blocked_in_scope', 0)}，按断点分列见第五节）",
        "",
        "**判不了不等于有问题**：本工程 315 个被引编号里有 "
        f"{plan.get('blocked_citations', 0)} 个在本地没有 PDF，这些编号下的论断无从核对。"
        "报告里任何「没有找到证据」的结论都只对**取到了原文**的那部分负责。",
        "",
        "## 一、判定为「不成立」的论断（先看这里）",
        "",
        "这些是原文**明确不支持或相矛盾**的论断。每条都附引文全文、引文在原文里的位置，"
        "以及两遍判定（第二遍是**反向提问**：请尽力找出最强支撑）的结果。"
        "没有逐字引文的判定进不了这一节——它会被降级到第三节的 `unverifiable`。",
        "",
    ]
    unsupported = [record for record in records if record.get("verdict") == "unsupported"]
    judged_pairs = judged - counts.get(VERDICT_PENDING, 0)
    if not unsupported:
        lines.append(f"（无。已判 {judged_pairs} 对里没有判为 `unsupported` 的。）")
    for record in unsupported:
        lines += _pair_block(record)
    lines += ["", "## 二、只判到「部分成立」的论断", ""]
    partial = [record for record in records if record.get("verdict") == "partial"]
    if not partial:
        lines.append("（无。）")
    for record in partial:
        lines += _pair_block(record)
    lines += [
        "",
        "## 三、无法判定（`unverifiable`）",
        "",
        "包含四类：候选段落为空、模型给的引文不是原文里的逐字片段、模型自己说不确定、"
        "以及调用失败。**没有候选只能落在这里，绝不落 `unsupported`。**",
        "",
    ]
    unverifiable = [record for record in records if record.get("verdict") == "unverifiable"]
    if not unverifiable:
        lines.append("（无。）")
    else:
        lines += [
            "| 对 | 编号 | 候选 | 原因 |",
            "|:--|--:|:--|:--|",
        ]
        for record in unverifiable:
            reason = "；".join(record.get("notes") or [])
            if not reason:
                reason = str((record.get("first_pass") or {}).get("rationale") or "")
            lines.append(
                f"| `{record.get('pair')}` | [{record.get('citation')}] "
                f"| `{record.get('candidates_status')}` | {_cell(reason, 220) or '（模型判为不确定，未给理由）'} |"
            )
    lines += [
        "",
        "## 四、判不了：本地没有原文",
        "",
        "链路断在取原文的那几跳上。这些引用**不是**判为「不成立」，而是**无从核对**。",
        "",
    ]
    unjudgeable = payload.get("unjudgeable") or []
    if not unjudgeable:
        lines.append("（无。）")
    else:
        lines += ["| 编号 | 断点 | 对数 | 例句 |", "|--:|:--|--:|:--|"]
        for row in unjudgeable:
            lines.append(
                f"| [{row['citation']}] | `{row['resolution']}` | {row['pairs']} "
                f"| {_cell(row['example'], 70)} |"
            )
    lines += [
        "",
        "## 五、汇总表",
        "",
        "| 判定 | 对数 | 含义 |",
        "|:--|--:|:--|",
        f"| `supported` | {counts.get('supported', 0)} | 原文支撑该论断 |",
        f"| `partial` | {counts.get('partial', 0)} | 只支撑论断的一部分 |",
        f"| `unsupported` | {counts.get('unsupported', 0)} | 原文与论断不符（附引文） |",
        f"| `source-missing` | {counts.get('source-missing', 0)} | 链路断，本地没有原文可判 |",
        f"| `unverifiable` | {counts.get('unverifiable', 0)} | 候选不足、引文不可核或调用失败 |",
        f"| `pending` | {counts.get('pending', 0)} | 尚未判定（分批跑） |",
        "",
        "### 检索与判定的账",
        "",
        f"- 译文那一招救回多少（只算**已经检索过**的 {retrieval['pairs_retrieved']} 对）："
        f"原句查询下**零重合**的对 {retrieval['pairs_with_zero_overlap_on_the_original_query']} 条，"
        f"其中 **{retrieval['improved_to_non_zero_overlap_by_translation']}** 条合并译文后有了重合，"
        f"仍有 **{retrieval['still_zero_overlap_after_merging']}** 条一条都没命中。"
        f"另有 {retrieval['pairs_where_the_translation_query_matched_more']} 条原句也零星命中过、"
        f"但译文命中更多（原句共命中 {retrieval['matched_tokens_original_query_total']} 个词，"
        f"译文共命中 {retrieval['matched_tokens_translation_query_total']} 个词）。",
        f"- 候选得分分布（合并两路查询后的最好一段）："
        f"中位数 {retrieval['best_score_distribution']['median']}，"
        f"p90 {retrieval['best_score_distribution']['p90']}，"
        f"最大 {retrieval['best_score_distribution']['max']}，"
        f"达到 `MIN_SCORE={retrieval['best_score_distribution']['min_score']}` 的 "
        f"{retrieval['best_score_distribution']['at_or_above_min_score']}"
        f"/{retrieval['best_score_distribution']['count']}。"
        "得分低于门槛**不丢候选**，只是把这一对标成弱匹配，照旧交出去判——"
        "「没找到」与「证据弱」在判定里的含义完全不同。",
        f"- 第二遍判定：跑了 {passes['second_pass_run']} 对，"
        f"一致 {passes['agree']}，**不一致 {passes['disagree']}**（不一致的对两遍结论都留着，标为待人工复核）。",
        f"- 调用与用量：请求 {usage.get('requests', 0)} 次"
        f"（prompt {usage.get('prompt_tokens', 0):,} token + completion "
        f"{usage.get('completion_tokens', 0):,} token"
        f" = {usage.get('total_tokens', 0):,} token）。这一栏是**本次运行**的花费；"
        "分批跑时每批各报各的，账本里的逐对 token 才是总量。",
        "",
    ]
    return lines


def _pair_block(record: Mapping) -> list[str]:
    """一条判定为 ``unsupported`` / ``partial`` 的对的完整证据块。"""
    lines = [
        f"### `{record.get('pair')}` — [{record.get('citation')}] "
        f"{_cell(record.get('entry_title'), 120)}",
        "",
        f"- 位置：`{record.get('file')}:{record.get('line')}`",
        f"- 论断（原样）：{_cell(record.get('claim'))}",
    ]
    translation = record.get("translation")
    if translation:
        lines.append(f"- 英文译文（本工具所译，仅作取证辅助）：{_cell(translation)}")
    else:
        lines.append("- 英文译文：未取得（检索只用原句）")
    if record.get("quote"):
        lines += [
            f"- 引文位置：偏移 {record.get('quote_start')}–{record.get('quote_end')}"
            f"（相对去掉引用头之后的正文），第 {record.get('quote_line')} 行",
            "",
            "第一遍（中立提问）判 `%s`，引文：" % record.get("verdict"),
            "",
        ]
        lines += _quote_block(record.get("quote"))
        lines.append("")
        if record.get("rationale"):
            lines += [f"理由：{_cell(record.get('rationale'))}", ""]
    notes = record.get("notes") or []
    if notes:
        lines += ["备注：" + "；".join(str(note) for note in notes), ""]
    second = record.get("second_pass")
    if second:
        agree = record.get("agree")
        lines += [
            f"第二遍（反向提问：请尽力找出最强支撑）判 `{second.get('verdict')}`"
            f"，两遍{'一致' if agree else '不一致' if agree is False else '—'}。",
            "",
        ]
        if second.get("quote"):
            lines += _quote_block(second.get("quote"))
            lines.append("")
        if second.get("rationale"):
            lines += [f"第二遍理由：{_cell(second.get('rationale'))}", ""]
    lines.append("")
    return lines


def print_verify_summary(payload: Mapping, out_dir: Path, project_root: Optional[Path] = None) -> None:
    """把计数打到 stdout（英文）。路径相对工程根打印。"""
    summary = payload["summary"]
    plan = summary["plan"]
    counts = summary["verdicts"]
    retrieval = summary["retrieval"]
    passes = summary["passes"]
    usage = summary["usage"]
    print("primer.claims verify")
    print(f"  pairs: {plan.get('pairs', 0)} in the manuscript, {plan.get('selected', 0)} selected"
          f" ({plan.get('skipped', 0)} already done)")
    print(f"  judgeable: {plan.get('judgeable_in_scope', 0)} in scope"
          f" ({plan.get('judgeable', 0)} overall); blocked by corpus:"
          f" {plan.get('blocked_in_scope', 0)} in scope ({plan.get('blocked_by_corpus', 0)} overall)")
    print("  verdicts (per pair in this report):")
    for verdict in VERDICTS + (VERDICT_PENDING,):
        print(f"    {verdict:16s} {counts.get(verdict, 0)}")
    print(f"  translation: {retrieval['improved_to_non_zero_overlap_by_translation']} pair(s) went from"
          f" zero token overlap to non-zero; {retrieval['still_zero_overlap_after_merging']} still empty;"
          f" {retrieval['pairs_where_the_translation_query_matched_more']} pair(s) matched more words"
          f" through the translation query"
          f" ({retrieval['matched_tokens_original_query_total']}"
          f" -> {retrieval['matched_tokens_translation_query_total']} matched tokens)")
    distribution = retrieval["best_score_distribution"]
    print(f"  candidates: {distribution['count']} pair(s) with source text, best score median"
          f" {distribution['median']}, p90 {distribution['p90']}, max {distribution['max']};"
          f" at/above MIN_SCORE {distribution['min_score']} {distribution['at_or_above_min_score']}")
    print(f"  second pass: {passes['second_pass_run']} run, {passes['agree']} agree,"
          f" {passes['disagree']} disagree (kept for review)")
    print(f"  usage: {usage.get('requests', 0)} request(s), prompt {usage.get('prompt_tokens', 0):,}"
          f" + completion {usage.get('completion_tokens', 0):,}"
          f" = {usage.get('total_tokens', 0):,} tokens")
    print(f"  wrote {relative_to_root(Path(out_dir) / VERDICTS_JSON, project_root)}")
    print(f"  wrote {relative_to_root(Path(out_dir) / VERDICTS_MD, project_root)}")


__all__ = [
    "CLAIMS_JSON",
    "CLAIMS_MD",
    "VERDICT_CONTRACT",
    "VERDICT_CONTRACT_RUN",
    "VERDICT_PENDING",
    "VERDICTS_JSON",
    "VERDICTS_MD",
    "VERDICTS",
    "build_payload",
    "build_verdict_payload",
    "pass_summary",
    "print_summary",
    "print_verify_summary",
    "retrieval_summary",
    "verdict_counts",
    "write_claims_json",
    "write_claims_md",
    "write_verdicts_json",
    "write_verdicts_md",
]
