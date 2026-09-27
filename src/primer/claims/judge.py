# -*- coding: utf-8 -*-
"""判定一条论断是否**真的**被它引用的原文支撑：LLM 判定，外加四道硬规矩。

判定单元是 (论断, 引用编号) 对。链路断的（``resolution != ok``）判
``source-missing``；链路通、检索给出候选的，把论断（中文）、它的英文译文（取证辅助）、
被引作品与候选段落一起交给模型，要它给一个 JSON。

**四道硬规矩就是本模块存在的意义**——判定结论会被拿去质问作者，判错的方向必须只能是
"说不知道"，不能是"指控"：

1. **除 ``source-missing`` / ``unverifiable`` 外，任何判定都必须附一段逐字抄自原文
   的引文**（连同它在原文里的偏移与行号）。模型没给引文，或给的引文在原文里找不到
   逐字对应，一律降级为 ``unverifiable`` 并记下降级原因。没有引文的指控不入账。
2. **没有候选（或候选太弱、模型用不上）只能判 ``unverifiable``，绝不判 ``unsupported``**。
   检索落空是检索的事，不是作者的错。
3. **``partial`` / ``unsupported`` 一律跑第二遍**：同一对，换一个**反向**的提问方式
   （"请尽力找出最强的支撑／请列出究竟缺哪一句"）。两遍都记下来并标出是否一致，
   不一致就**显式留着**，绝不取平均、绝不用第二遍覆盖第一遍。
4. **``source-missing`` 只给链路断掉的对**——断点由前半程判定，不交给模型猜。模型若
   在链路通畅时说 ``source-missing``，按 ``unverifiable`` 记账并注明。

回信解析沿用 :mod:`primer.claims.client` 的"只要 JSON + 围栏/平衡括号修复"；空正文是
可重试的调用失败，不是判定。调用失败或回信彻底解析不出时，判 ``unverifiable`` 并记下
原因——任何失败都不该变成一个结论。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping, Optional, Sequence, Tuple

from .client import ChatClient, LlmCallError, LlmReplyError, UsageTotals, parse_json_reply
from .report import VERDICTS
from .retrieve import QueryStat, STATUS_WEAK, Passage

SUPPORTED = "supported"
PARTIAL = "partial"
UNSUPPORTED = "unsupported"
SOURCE_MISSING = "source-missing"
UNVERIFIABLE = "unverifiable"

FIRST_PASS = "first"
SECOND_PASS = "second"

MAX_RETRIES = 1
"""一次判定调用的重试次数；空正文与残缺 JSON 都按可重试处理。"""

# 模型可能用的别名 → 本工程的五个判定。只认得出方向明确的那几种，其余落 unverifiable。
_VERDICT_ALIASES = {
    "yes": SUPPORTED,
    "fullysupported": SUPPORTED,
    "support": SUPPORTED,
    "mostlysupported": SUPPORTED,
    "partiallysupported": PARTIAL,
    "partlysupported": PARTIAL,
    "inpart": PARTIAL,
    "partly": PARTIAL,
    "notsupported": UNSUPPORTED,
    "nosupport": UNSUPPORTED,
    "contradicted": UNSUPPORTED,
    "refuted": UNSUPPORTED,
    "false": UNSUPPORTED,
    "unclear": UNVERIFIABLE,
    "unknown": UNVERIFIABLE,
    "insufficient": UNVERIFIABLE,
    "insufficientevidence": UNVERIFIABLE,
    "canttell": UNVERIFIABLE,
    "cannotdetermine": UNVERIFIABLE,
    "notverifiable": UNVERIFIABLE,
    "sourcemissing": SOURCE_MISSING,
    "missingsource": SOURCE_MISSING,
}

_ELLIPSIS = ("...", "…", "[...]", "[…]")
_VERDICT_KEYS = ("verdict", "judgement", "judgment", "result", "label", "assessment")
_QUOTE_KEYS = ("quote", "quotation", "evidence", "verbatim", "supporting_quote")
_RATIONALE_KEYS = ("rationale", "reason", "explanation", "comment", "why", "analysis")

PROMPT_HEADER = """\
You are auditing the citations of a Chinese planetary-science review manuscript. For one claim
you are given the sentence as printed (Chinese), a literal English translation of it, and
passages retrieved from the open-access work the sentence cites. Decide whether the cited work
actually supports the claim.

Choose exactly one verdict:
- "supported": the source states the claim, or a statement the claim accurately summarises.
- "partial": the source supports part of the claim but not all of it.
- "unsupported": the source contradicts the claim, or clearly does not support it.
- "unverifiable": the passages are not enough to decide either way.

Rules:
1. For "supported", "partial" and "unsupported" you MUST give a "quote": one contiguous span
   copied character-for-character from the passages below. No ellipsis, no paraphrase, no
   editing, no translation into Chinese. A verdict without such a quote is discarded.
2. If the passages are insufficient or are about something else, answer "unverifiable".
   Never answer "unsupported" merely because the search found nothing.
3. Judge only the claim and the passages; do not use outside knowledge about the topic.
4. Keep "rationale" to one or two sentences.

Reply with JSON only:
{"verdict": "...", "quote": "...", "rationale": "..."}
"""

SECOND_PASS_UNSUPPORTED = """\
## Second review, opposite framing

A first reviewer judged this claim "unsupported" by the cited work. Your job is the opposite:
argue as strongly as the source honestly allows that the claim IS supported. Re-read the
passages for the best supporting statement, including wording that is looser than the claim.
If the strongest support you can find covers only part of the claim, answer "partial".
If the passages genuinely contain no support, keep "unsupported" — and quote the passage that
comes closest, so the gap is visible.
"""

SECOND_PASS_PARTIAL = """\
## Second review, opposite framing

A first reviewer judged this claim only "partial" against the cited work. Your job is the
opposite: check whether the part said to be missing is in fact present in the passages,
under different wording, a different section, or a figure caption. If the passages do support
the whole claim, answer "supported". If they still cover only part of it, keep "partial" and
say precisely which part is missing.
"""


@dataclass(frozen=True)
class JudgeInput:
    """一次判定需要的全部材料：论断、译文、被引作品与检索到的候选段落。"""

    pair_id: str
    claim_id: str
    citation: int
    claim: str
    translation: Optional[str]
    entry_title: str
    entry_venue: str
    source_markdown: str
    source_chars: int
    candidates_status: str
    passages: Tuple[Passage, ...] = ()
    queries: Tuple[QueryStat, ...] = ()


@dataclass(frozen=True)
class PassResult:
    """一遍（第一遍或第二遍）判定的完整记录，含被判废时的原因。"""

    name: str
    verdict: str
    raw_verdict: str
    quote: Optional[str]
    quote_start: Optional[int]
    quote_end: Optional[int]
    quote_line: Optional[int]
    rationale: str
    notes: Tuple[str, ...] = ()
    error: Optional[str] = None
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0

    def as_dict(self) -> dict:
        return {
            "pass": self.name,
            "verdict": self.verdict,
            "raw_verdict": self.raw_verdict,
            "quote": self.quote,
            "quote_start": self.quote_start,
            "quote_end": self.quote_end,
            "quote_line": self.quote_line,
            "rationale": self.rationale,
            "notes": list(self.notes),
            "error": self.error,
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
        }


@dataclass(frozen=True)
class PairVerdict:
    """一对的最终记账：以第一遍为准，第二遍与两遍的分歧一并留着。"""

    item: JudgeInput
    verdict: str
    quote: Optional[str]
    quote_start: Optional[int]
    quote_end: Optional[int]
    quote_line: Optional[int]
    rationale: str
    notes: Tuple[str, ...]
    first: PassResult
    second: Optional[PassResult] = None
    agree: Optional[bool] = None
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def needs_review(self) -> bool:
        """两遍不一致的对要人看；这是"把分歧留着"的落地方式。"""
        return self.agree is False

    def as_dict(self) -> dict:
        return {
            "pair": self.item.pair_id,
            "claim_id": self.item.claim_id,
            "citation": self.item.citation,
            "claim": self.item.claim,
            "translation": self.item.translation,
            "entry_title": self.item.entry_title,
            "source_markdown": self.item.source_markdown,
            "candidates_status": self.item.candidates_status,
            "verdict": self.verdict,
            "quote": self.quote,
            "quote_start": self.quote_start,
            "quote_end": self.quote_end,
            "quote_line": self.quote_line,
            "rationale": self.rationale,
            "notes": list(self.notes),
            "first_pass": self.first.as_dict(),
            "second_pass": self.second.as_dict() if self.second else None,
            "agree": self.agree,
            "needs_review": self.needs_review,
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
        }


def build_first_pass_prompt(item: JudgeInput) -> str:
    """第一遍（中立提问）的提示词。"""
    return "\n".join([PROMPT_HEADER, *_material(item), ""])


def build_second_pass_prompt(item: JudgeInput, first: PassResult) -> str:
    """第二遍（反向提问）的提示词：按第一遍判的是 ``unsupported`` 还是 ``partial`` 换话术。"""
    framing = (
        SECOND_PASS_PARTIAL if first.verdict == PARTIAL else SECOND_PASS_UNSUPPORTED
    )
    lines = [PROMPT_HEADER, *_material(item), framing]
    lines.append(f"A first reviewer answered {first.verdict!r}. Its rationale was:")
    lines.append(f"{first.rationale or '(none given)'}")
    lines.append("")
    return "\n".join(lines)


def _material(item: JudgeInput) -> list[str]:
    """提示词里两遍共用的材料：被引作品、论断、译文、候选段落。"""
    lines = [
        "## The cited work",
        f"{item.entry_title or '(title unavailable)'} — {item.entry_venue or '(venue unavailable)'}",
        f"file: {item.source_markdown} ({item.source_chars} characters of text)",
        "",
        "## The claim as printed (Chinese)",
        item.claim,
        "",
        "## The claim in English (machine translation, an evidence aid only — not the text under audit)",
        item.translation if item.translation else "(translation unavailable; judge from the Chinese and the passages)",
        "",
        "## Passages retrieved from the cited work (offsets are into the file's text body)",
    ]
    if not item.passages:
        lines.append("(none)")
    for index, passage in enumerate(item.passages, 1):
        matched = "+".join(passage.queries) if passage.queries else "original"
        lines.append(
            f"[passage {index}] offset {passage.start}-{passage.end} score {passage.score:.4f} "
            f"matched by: {matched}"
        )
        lines.append(passage.text)
        lines.append("")
    return lines


def normalise_verdict(value: object) -> Optional[str]:
    """把模型给的判定归一；认不出返回 ``None``（调用方落 ``unverifiable``）。"""
    text = re.sub(r"[^a-z]", "", str(value or "").lower())
    if text in VERDICTS:
        return text
    return _VERDICT_ALIASES.get(text)


def locate_quote(source: str, quote: str) -> Optional[tuple[int, int, bool]]:
    """在原文里定位引文，返回 ``(起, 止, 是否逐字命中)``；定位不到返回 ``None``。

    先找逐字命中；找不到时允许**空白差异**（模型把换行折成空格）再找一次，并把
    "这里做了归一"如实标出来——除此之外一律不算逐字引文。
    """
    if not quote:
        return None
    start = source.find(quote)
    if start != -1:
        return start, start + len(quote), True
    tokens = quote.split()
    if not tokens:
        return None
    pattern = r"\s+".join(re.escape(token) for token in tokens)
    match = re.search(pattern, source)
    if match:
        return match.start(), match.end(), False
    return None


def _clean_quote(value: object) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _pick(payload: Mapping, keys: Sequence[str]) -> object:
    for key in keys:
        if key in payload:
            return payload[key]
    return None


def extract_verdict(payload: object) -> tuple[Optional[str], Optional[str], str]:
    """从回信 JSON 里取 ``(判定, 引文, 说明)``；结构认不出则判定为 ``None``。"""
    if isinstance(payload, list):
        for entry in payload:
            if isinstance(entry, dict):
                payload = entry
                break
    if not isinstance(payload, Mapping):
        return None, None, ""
    raw = _pick(payload, _VERDICT_KEYS)
    quote = _clean_quote(_pick(payload, _QUOTE_KEYS))
    rationale = _pick(payload, _RATIONALE_KEYS)
    return (
        str(raw).strip() if raw is not None else None,
        quote,
        re.sub(r"\s+", " ", str(rationale)).strip() if rationale else "",
    )


def apply_rules(
    raw_verdict: Optional[str],
    quote: Optional[str],
    source_text: str,
) -> tuple[str, Optional[str], Optional[int], Optional[int], Optional[int], list[str]]:
    """四道硬规矩的落地：把模型给的东西变成可以入账的判定。

    返回 ``(判定, 引文, 起, 止, 行号, 备注)``。引文一律换成**原文里的那一段**——
    记的是原文，不是模型的转述。
    """
    notes: list[str] = []
    verdict = normalise_verdict(raw_verdict)
    if verdict is None:
        return (
            UNVERIFIABLE,
            None,
            None,
            None,
            None,
            notes + [f"unrecognised verdict {raw_verdict!r}; recorded as unverifiable"],
        )
    if verdict == SOURCE_MISSING:
        return (
            UNVERIFIABLE,
            None,
            None,
            None,
            None,
            notes
            + [
                "the model answered source-missing although the citation chain resolves to usable "
                "text; recorded as unverifiable (source-missing is decided by the chain, not the model)"
            ],
        )
    if verdict == UNVERIFIABLE:
        if quote:
            notes.append("a quote was offered but no verdict rests on it, so it was not kept")
        return UNVERIFIABLE, None, None, None, None, notes
    if not quote:
        return (
            UNVERIFIABLE,
            None,
            None,
            None,
            None,
            notes + ["verdict returned without a verbatim quote; downgraded to unverifiable"],
        )
    if any(marker in quote for marker in _ELLIPSIS):
        return (
            UNVERIFIABLE,
            None,
            None,
            None,
            None,
            notes
            + [
                "quote carries an ellipsis, so it is not one contiguous verbatim span; "
                "downgraded to unverifiable"
            ],
        )
    located = locate_quote(source_text, quote)
    if located is None:
        return (
            UNVERIFIABLE,
            None,
            None,
            None,
            None,
            notes + ["quote is not found verbatim in the cited source; downgraded to unverifiable"],
        )
    start, end, exact = located
    if not exact:
        notes.append("quote matched after collapsing whitespace differences")
    return (
        verdict,
        source_text[start:end],
        start,
        end,
        source_text.count("\n", 0, start) + 1,
        notes,
    )


class Judge:
    """判定器：一次判一对，第二遍只跑在 ``partial`` / ``unsupported`` 上。"""

    def __init__(self, client: ChatClient, *, max_retries: int = MAX_RETRIES):
        self.client = client
        self.max_retries = max_retries

    def judge(self, item: JudgeInput, source_text: str) -> PairVerdict:
        """判一对。``source_text`` 是去掉引用头之后的原文全文（核对引文用）。"""
        if not item.passages:
            # 没有候选就不问模型：检索落空不能变成对作者的指控。
            notes = (
                "retrieval returned no candidate passages for this pair; a failed search is not "
                "evidence, so it is recorded as unverifiable",
            )
            empty = PassResult(
                name=FIRST_PASS,
                verdict=UNVERIFIABLE,
                raw_verdict="",
                quote=None,
                quote_start=None,
                quote_end=None,
                quote_line=None,
                rationale="",
                notes=notes,
            )
            return PairVerdict(
                item=item,
                verdict=UNVERIFIABLE,
                quote=None,
                quote_start=None,
                quote_end=None,
                quote_line=None,
                rationale="",
                notes=notes,
                first=empty,
            )

        first = self._run_pass(item, FIRST_PASS, build_first_pass_prompt(item), source_text)
        second: Optional[PassResult] = None
        if first.raw_verdict in (PARTIAL, UNSUPPORTED) or first.verdict in (PARTIAL, UNSUPPORTED):
            second = self._run_pass(
                item, SECOND_PASS, build_second_pass_prompt(item, first), source_text
            )
        notes = list(first.notes)
        if item.candidates_status == STATUS_WEAK:
            notes.append("verdict reached from weak (below-threshold) candidates")
        agree = None if second is None else (second.verdict == first.verdict)
        if agree is False:
            notes.append(
                f"the two passes disagree ({first.verdict} vs {second.verdict}); both are kept "
                "and the pair is flagged for human review"
            )
        calls = first.calls + (second.calls if second else 0)
        return PairVerdict(
            item=item,
            verdict=first.verdict,
            quote=first.quote,
            quote_start=first.quote_start,
            quote_end=first.quote_end,
            quote_line=first.quote_line,
            rationale=first.rationale,
            notes=tuple(notes),
            first=first,
            second=second,
            agree=agree,
            calls=calls,
            prompt_tokens=first.prompt_tokens + (second.prompt_tokens if second else 0),
            completion_tokens=first.completion_tokens + (second.completion_tokens if second else 0),
        )

    # ---- 内部 ----

    def _run_pass(
        self, item: JudgeInput, name: str, prompt: str, source_text: str
    ) -> PassResult:
        before = self.client.usage
        snapshot = (before.requests, before.prompt_tokens, before.completion_tokens)
        payload: Optional[object] = None
        error: Optional[str] = None
        for _ in range(self.max_retries + 1):
            try:
                reply = self.client.complete(prompt)
            except LlmCallError as failure:
                error = str(failure)
                continue
            try:
                payload = parse_json_reply(reply.content)
            except LlmReplyError as failure:
                error = str(failure)
                continue
            error = None
            break
        calls, prompt_tokens, completion_tokens = _delta(self.client.usage, snapshot)
        if payload is None:
            return PassResult(
                name=name,
                verdict=UNVERIFIABLE,
                raw_verdict="",
                quote=None,
                quote_start=None,
                quote_end=None,
                quote_line=None,
                rationale="",
                notes=(f"{name} pass could not be used: {error}",),
                error=error,
                calls=calls,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )
        raw_verdict, quote, rationale = extract_verdict(payload)
        verdict, kept_quote, start, end, line, notes = apply_rules(
            raw_verdict, quote, source_text
        )
        return PassResult(
            name=name,
            verdict=verdict,
            raw_verdict=str(raw_verdict or ""),
            quote=kept_quote,
            quote_start=start,
            quote_end=end,
            quote_line=line,
            rationale=rationale,
            notes=tuple(notes),
            calls=calls,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )


def _delta(usage: UsageTotals, snapshot: tuple[int, int, int]) -> tuple[int, int, int]:
    return (
        usage.requests - snapshot[0],
        usage.prompt_tokens - snapshot[1],
        usage.completion_tokens - snapshot[2],
    )


__all__ = [
    "FIRST_PASS",
    "MAX_RETRIES",
    "PARTIAL",
    "SECOND_PASS",
    "SOURCE_MISSING",
    "SUPPORTED",
    "UNSUPPORTED",
    "UNVERIFIABLE",
    "Judge",
    "JudgeInput",
    "PairVerdict",
    "PassResult",
    "apply_rules",
    "build_first_pass_prompt",
    "build_second_pass_prompt",
    "extract_verdict",
    "locate_quote",
    "normalise_verdict",
]
