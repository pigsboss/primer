# -*- coding: utf-8 -*-
"""在原文里找出能支撑（或反驳）论断的候选段落：纯标准库、可解释、面向召回。

检索方案只有一层，全部可手算：把原文切成**有重叠的定长窗口**（默认 800 字、重叠
200 字），窗口按"中文二字组 + 小写拉丁词"建索引，查询串用同一套切法；每个窗口的
得分是**命中查询词的逆文档频率之和 ÷ 查询词的逆文档频率总和**，逆文档频率按该原文
自己的窗口统计（``log(1 + N/(1+df))``），因此分数落在 0–1，含义是"覆盖了查询的多少
信息量"，不依赖任何外部模型、词表或训练数据。查询里没在原文出现过的词按 df=0 计权重
（权重最大），于是"大段都没对上"的论断得分自然被压低。

面向召回的三条约定：

* 窗口有重叠，跨窗口边界的句子不会被切碎；
* 取前 k 个时做一次抑制——与已选窗口重叠超过自身一半的候选让位，避免 top-k 全是
  同一段的近邻副本；
* 得分低于 :data:`MIN_SCORE` 时**不返回空**，照旧给出最好的几个并把状态标成
  ``candidates-weak``；一个窗口都没有命中才标 ``no-candidates``。两种状态都显式
  记录——"没找到证据"与"证据很弱"对后半程的判定含义完全不同。

:func:`retrieve_merged` 是给后半程用的：中文论断与它的英文译文各查一次，按区间合并
候选，并记下每个候选是哪一路捞到的。论断是中文、原文是英文时，原句查询多半一条都
命中不了（289 条能取到原文的对里有 151 条零重合），译文那一路才是真正干活的那一路；
两路各记各的账，因此"译文救回了多少"是可核的，而不是一句估计。

段落偏移一律相对**去掉引用头之后的原文**（转换产物顶部那段 ``> **Citation** …``
不算正文），``SourceIndex.header_chars`` 记下被去掉的字符数，需要回到文件原始偏移时
加上它即可。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Mapping, Sequence

WINDOW = 800
OVERLAP = 200
TOP_K = 5
MIN_SCORE = 0.02
"""得分下限：低于它仍返回最好的几个候选，但状态标 ``candidates-weak``。

取值是在本工程的真实分布上定的（289 条能取到原文的对，中位数约 0.013、最大值约 0.15）：
再高就只剩个位数的"达标"对，再低则把零星撞上的几个词当成命中。可用 ``--min-score`` 调整。
"""
CHARS_PER_TOKEN = 1.6
"""中英混排文本的粗估折算率（个字符/token），只用于给后半程报价。"""

STATUS_OK = "ok"
STATUS_WEAK = "candidates-weak"
STATUS_NONE = "no-candidates"

QUERY_ORIGINAL = "original"
QUERY_TRANSLATION = "translation"

_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")
_LATIN = re.compile(r"[a-z0-9]+")
_HEADER_LINE = re.compile(r"^\s*>")
_HEADER_KEY = re.compile(r"^\s*>\s*\*\*(?:Citation|Title|Source|Pages)\*\*")

@dataclass(frozen=True)
class Passage:
    """一个候选段落：去掉引用头之后的字符区间 + 得分 + 是哪几个查询串把它捞上来的。"""

    start: int
    end: int
    score: float
    text: str
    queries: tuple[str, ...] = ()


@dataclass(frozen=True)
class QueryStat:
    """一个查询串自己的检索结果——两个查询串各记各的，好让"译文救回了多少"可核。"""

    name: str
    query_tokens: int
    matched_tokens: int
    status: str
    best_score: float


@dataclass(frozen=True)
class RetrievalResult:
    """一次检索的结果。``status`` 取 ``ok`` / ``candidates-weak`` / ``no-candidates``。

    ``queries`` 是按查询串分列的小账（单查询串的旧路径留空）；``matched_tokens`` 与
    ``query_tokens`` 是合并后的口径：合并后的候选覆盖了所有查询串里的多少个不同词。
    """

    status: str
    passages: list[Passage]
    query_tokens: int
    matched_tokens: int
    queries: tuple[QueryStat, ...] = ()

    @property
    def best_score(self) -> float:
        return max((passage.score for passage in self.passages), default=0.0)


@dataclass
class SourceIndex:
    """一份原文的分窗索引；同一份原文被多条论断引用时只建一次。"""

    text: str
    header_chars: int
    spans: list[tuple[int, int]]
    tokens: list[frozenset[str]]
    idf: dict[str, float]


def tokenize(text: str) -> list[str]:
    """CJK 走字符二元组，拉丁走小写词（长度 ≥2）。

    单字 CJK 串（如"磷"）没有二元组，退回该字本身，否则一个字的查询词会被整个丢掉。
    """
    tokens: list[str] = []
    for match in _CJK.finditer(text):
        run = match.group(0)
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[index : index + 2] for index in range(len(run) - 1))
    for match in _LATIN.finditer(text.lower()):
        word = match.group(0)
        if len(word) >= 2:
            tokens.append(word)
    return tokens


def strip_header(text: str) -> tuple[str, int]:
    """去掉转换产物顶部的引用头，返回 ``(正文, 被去掉的字符数)``。

    没有引用头时原样返回，被去掉的字符数为 0——这一步不做任何猜测。
    """
    lines = text.split("\n")
    count = 0
    for line in lines:
        if not line.strip() or _HEADER_LINE.match(line):
            count += 1
        else:
            break
    if not any(_HEADER_KEY.match(line) for line in lines[:count]):
        return text, 0
    return "\n".join(lines[count:]), len("\n".join(lines[:count])) + 1


def chunk(text: str, window: int = WINDOW, overlap: int = OVERLAP) -> list[tuple[int, int]]:
    """定长重叠窗口；``overlap`` 必须小于 ``window``。"""
    if window <= 0 or overlap < 0 or overlap >= window:
        raise ValueError("requires 0 <= overlap < window")
    if not text:
        return []
    step = window - overlap
    spans: list[tuple[int, int]] = []
    start = 0
    while True:
        spans.append((start, min(start + window, len(text))))
        if start + window >= len(text):
            break
        start += step
    return spans


def build_index(text: str, window: int = WINDOW, overlap: int = OVERLAP) -> SourceIndex:
    """把一份原文去掉引用头、切成窗口，并算好每个词的逆文档频率。"""
    body, header_chars = strip_header(text)
    spans = chunk(body, window=window, overlap=overlap)
    tokens: list[frozenset[str]] = []
    document_frequency: dict[str, int] = {}
    for start, end in spans:
        unique = frozenset(tokenize(body[start:end]))
        tokens.append(unique)
        for token in unique:
            document_frequency[token] = document_frequency.get(token, 0) + 1
    count = len(spans)
    idf = {
        token: math.log(1.0 + count / (1.0 + frequency))
        for token, frequency in document_frequency.items()
    }
    return SourceIndex(text=body, header_chars=header_chars, spans=spans, tokens=tokens, idf=idf)


def retrieve_passages(
    query: str,
    index: SourceIndex,
    *,
    k: int = TOP_K,
    min_score: float = MIN_SCORE,
) -> RetrievalResult:
    """在索引里取前 k 个候选段落。"""
    query_tokens = sorted(set(tokenize(query)))
    if not query_tokens or not index.spans:
        return RetrievalResult(
            status=STATUS_NONE, passages=[], query_tokens=len(query_tokens), matched_tokens=0
        )
    unseen = _unseen_idf(len(index.spans))
    weights = {token: index.idf.get(token, unseen) for token in query_tokens}
    total = sum(weights.values())
    hits: list[tuple[float, int, frozenset[str]]] = []
    for position, token_set in enumerate(index.tokens):
        matched = [token for token in query_tokens if token in token_set]
        if not matched:
            continue
        hits.append((sum(weights[token] for token in matched) / total, position, token_set))
    if not hits:
        return RetrievalResult(
            status=STATUS_NONE, passages=[], query_tokens=len(query_tokens), matched_tokens=0
        )
    hits.sort(key=lambda item: (-item[0], index.spans[item[1]][0]))
    best = hits[0][0]
    kept = _suppress(hits, index, k)
    covered: set[str] = set()
    for _, position, token_set in kept:
        covered |= set(query_tokens) & token_set
    passages = [
        Passage(
            start=index.spans[position][0],
            end=index.spans[position][1],
            score=round(score, 6),
            text=index.text[index.spans[position][0] : index.spans[position][1]],
        )
        for score, position, _ in kept
    ]
    status = STATUS_OK if best >= min_score else STATUS_WEAK
    return RetrievalResult(
        status=status, passages=passages, query_tokens=len(query_tokens), matched_tokens=len(covered)
    )


def _unseen_idf(count: int) -> float:
    """查询词在原文里一次都没出现时的权重：df=0，权重最大。"""
    return math.log(1.0 + count / 1.0) if count else 0.0


def retrieve_merged(
    queries: Sequence[tuple[str, str]],
    index: SourceIndex,
    *,
    k: int = TOP_K,
    min_score: float = MIN_SCORE,
) -> RetrievalResult:
    """用多个查询串（原句、译文）各检索一次，合并候选并记下每个候选是哪路捞到的。

    合并按**字符区间**去重：同一段被两个查询串分别捞到时只留一份，得分取高者，
    ``queries`` 记下这一路来自哪个查询串。合并后按同一套"重叠过半让位"的规则截到
    ``k`` 个——两路各给 k 个，不截就会把提示词撑到两倍，而多出来的多半是同段近邻。
    空与弱的口径不变：一个窗口都没命中记 ``no-candidates``，最好的也不够
    ``min_score`` 记 ``candidates-weak``，照旧把最好的几个交出去，绝不静默丢。
    """
    stats: list[QueryStat] = []
    merged: dict[tuple[int, int], dict] = {}
    all_tokens: set[str] = set()
    for name, query in queries:
        result = retrieve_passages(query, index, k=k, min_score=min_score)
        all_tokens |= set(tokenize(query))
        stats.append(
            QueryStat(
                name=name,
                query_tokens=result.query_tokens,
                matched_tokens=result.matched_tokens,
                status=result.status,
                best_score=round(max((item.score for item in result.passages), default=0.0), 6),
            )
        )
        for passage in result.passages:
            slot = merged.setdefault(
                (passage.start, passage.end),
                {"score": 0.0, "text": passage.text, "queries": []},
            )
            slot["score"] = max(slot["score"], passage.score)
            if name not in slot["queries"]:
                slot["queries"].append(name)
    kept = _suppress_merged(merged, index, k)
    covered: set[str] = set()
    passages: list[Passage] = []
    for start, end, score, text, names in kept:
        covered |= all_tokens & set(tokenize(text))
        passages.append(
            Passage(start=start, end=end, score=score, text=text, queries=tuple(names))
        )
    if not passages:
        return RetrievalResult(
            status=STATUS_NONE,
            passages=[],
            query_tokens=len(all_tokens),
            matched_tokens=0,
            queries=tuple(stats),
        )
    best = passages[0].score
    return RetrievalResult(
        status=STATUS_OK if best >= min_score else STATUS_WEAK,
        passages=passages,
        query_tokens=len(all_tokens),
        matched_tokens=len(covered),
        queries=tuple(stats),
    )


def _suppress_merged(
    merged: Mapping[tuple[int, int], dict], index: SourceIndex, k: int
) -> list[tuple[int, int, float, str, list[str]]]:
    """合并后的候选：按得分排序，与已选窗口重叠过半的让位，最多 ``k`` 个。"""
    ordered = sorted(
        (
            (span[0], span[1], slot["score"], slot["text"], slot["queries"])
            for span, slot in merged.items()
        ),
        key=lambda item: (-item[2], item[0]),
    )
    kept: list[tuple[int, int, float, str, list[str]]] = []
    for candidate in ordered:
        if len(kept) >= k:
            break
        if any(
            _overlap_ratio((candidate[0], candidate[1]), (chosen[0], chosen[1])) > 0.5
            for chosen in kept
        ):
            continue
        kept.append(candidate)
    return kept


def score_distribution(scores: Sequence[float], min_score: float = MIN_SCORE) -> dict:
    """得分的分布摘要，用来复核 :data:`MIN_SCORE` 是否还合身。

    报的是分位数而不是均值：候选得分的长尾很重，均值会被几条高分拖着走。
    """
    values = sorted(float(score) for score in scores)
    if not values:
        return {
            "count": 0,
            "min": 0.0,
            "median": 0.0,
            "p75": 0.0,
            "p90": 0.0,
            "max": 0.0,
            "min_score": min_score,
            "at_or_above_min_score": 0,
        }

    def pick(ratio: float) -> float:
        index = min(int(round(ratio * (len(values) - 1))), len(values) - 1)
        return round(values[index], 6)

    return {
        "count": len(values),
        "min": round(values[0], 6),
        "median": pick(0.5),
        "p75": pick(0.75),
        "p90": pick(0.9),
        "max": round(values[-1], 6),
        "min_score": min_score,
        "at_or_above_min_score": sum(1 for value in values if value >= min_score),
    }


def _suppress(
    hits: Sequence[tuple[float, int, frozenset[str]]], index: SourceIndex, k: int
) -> list[tuple[float, int, frozenset[str]]]:
    """贪心取前 k 个，与已选窗口重叠过半的候选跳过。"""
    kept: list[tuple[float, int, frozenset[str]]] = []
    for candidate in hits:
        if len(kept) >= k:
            break
        if any(_overlap_ratio(index.spans[candidate[1]], index.spans[chosen[1]]) > 0.5 for chosen in kept):
            continue
        kept.append(candidate)
    return kept


def _overlap_ratio(left: tuple[int, int], right: tuple[int, int]) -> float:
    length = left[1] - left[0]
    if length <= 0:
        return 1.0
    overlap = min(left[1], right[1]) - max(left[0], right[0])
    return max(overlap, 0) / length


def estimate_tokens(text: str, chars_per_token: float = CHARS_PER_TOKEN) -> int:
    """粗估一段中英混排文本的 token 数：中文按每 token 若干字符折算，拉丁按词计。"""
    cjk = sum(len(match.group(0)) for match in _CJK.finditer(text))
    latin = len(_LATIN.findall(text.lower()))
    return int(round(cjk / chars_per_token + latin))
