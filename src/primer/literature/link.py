# -*- coding: utf-8 -*-
"""批量自动关联：文献记录 × 本地文件的离线匹配核心（纯计算，不碰网络、不改库）。

产出两样东西、同一个匹配器：

* **关联提案**——文件 → 记录（标识符／题名／包容判定，按置信分档）；
* **缺文清单**——没有可靠配对记录的**反向筛查**结果（哪些文献还缺本地文件）。

文件侧信号取自 MinerU 解析产物（``md_path`` 的 markdown）：题名候选由
:func:`markdown_title_candidates` 抽取（还原 ``[标题](URL)`` 链接、剥 HTML 标签、
跳过 URL／DOI／期刊页眉等样板行、相邻行拼接治断行、多候选择优）；记录侧把题名
归一化后比对，相似度按"与次名的领先量"分档，另以"包容判定"（一方为另一方子串）
兜底截断题名。DOI／arXiv 与记录同名字段精确一致时直接判确定级。

分档（供预览对话框默认勾选）：``strong``（确定）／``suggest``（建议）／
``weak``（存疑）／``none``（不提案）。
"""

from __future__ import annotations

import difflib
import re
from typing import Any, Callable, Optional, Sequence

__all__ = [
    "MARGIN_OK",
    "MARGIN_STRONG",
    "TITLE_OK",
    "TITLE_STRONG",
    "TITLE_WEAK",
    "build_index",
    "markdown_title_candidates",
    "match_file",
    "norm_text",
    "propose_links",
    "tier_for",
]

TITLE_STRONG = 0.97  # 确定级：相似度与领先量
MARGIN_STRONG = 0.10
TITLE_OK = 0.90  # 建议级
MARGIN_OK = 0.05
TITLE_WEAK = 0.80  # 存疑级

_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_BOILERPLATE_RE = re.compile(
    r"(?i)^(https?://|doi[:：]|www\.|edited by|reviewed by|received|accepted"
    r"|published|abstract|keywords|contents)"
)
_NOISE_RE = re.compile(r"(?i)doi\.org|arxiv:|issn|isbn")
_HAS_WORD_RE = re.compile(r"[A-Za-z]{3,}|[\u4e00-\u9fff]{4,}")


def norm_text(text: Any) -> str:
    """题名归一化：只留字母数字与汉字，去空白、标点与大小写差异。"""
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", str(text or "")).lower()


def _norm_eprint(value: Any) -> str:
    """arXiv 编号归一：去 ``arXiv:`` 前缀与 ``vN`` 版本后缀。"""
    text = str(value or "").strip().lower()
    text = re.sub(r"^arxiv[:\s]*", "", text)
    return re.sub(r"v\d+$", "", text)


def _clean_markdown(text: str) -> str:
    """把链接 ``[标题](URL)`` 还原为标题文本，剥掉 ``<sup>`` 一类 HTML 标签。"""
    text = _MD_LINK_RE.sub(r"\1", text)
    text = _HTML_TAG_RE.sub(" ", text)
    return text.strip("*_ ")


def markdown_title_candidates(
    text: str, *, max_lines: int = 12, max_candidates: int = 26
) -> list[str]:
    """从解析产物 markdown 抽题名候选（样板过滤／断行拼接／多候选择优）。"""
    lines: list[str] = []
    for raw in text.splitlines()[:120]:
        stripped = raw.strip()
        if not stripped or stripped.startswith(("!", "|", ">", "<")):
            continue
        candidate = _clean_markdown(re.sub(r"^#+\s*", "", stripped))
        if not 14 <= len(candidate) <= 320:
            continue
        if _BOILERPLATE_RE.match(candidate) or _NOISE_RE.search(candidate):
            continue
        if not _HAS_WORD_RE.search(candidate):
            continue
        lines.append(candidate)
        if len(lines) >= max_lines:
            break
    candidates = list(lines)
    for i in range(len(lines) - 1):
        joined = lines[i] + " " + lines[i + 1]
        if len(joined) <= 340:
            candidates.append(joined)
    return candidates[:max_candidates]


def build_index(records: Sequence[Any]) -> dict[str, Any]:
    """记录侧索引：归一化题名、重名分组、DOI／arXiv 映射。"""
    norms: list[str] = []
    groups: dict[str, list[int]] = {}
    by_doi: dict[str, int] = {}
    by_eprint: dict[str, int] = {}
    for i, record in enumerate(records):
        norm = norm_text(getattr(record, "title", ""))
        norms.append(norm)
        if norm:
            groups.setdefault(norm, []).append(i)
        doi = str(getattr(record, "doi", "") or "").strip().lower()
        if doi:
            by_doi.setdefault(doi, i)
        eprint = _norm_eprint(getattr(record, "eprint", ""))
        if eprint:
            by_eprint.setdefault(eprint, i)
    return {"norms": norms, "groups": groups, "by_doi": by_doi, "by_eprint": by_eprint}


def _containment_hits(index: dict[str, Any], candidates: Sequence[str]) -> set[int]:
    hits: set[int] = set()
    norms = index["norms"]
    for cand in candidates:
        nc = norm_text(cand)
        if len(nc) < 14:
            continue
        for i, rn in enumerate(norms):
            if not rn:
                continue
            if nc in rn and len(rn) - len(nc) <= 120:
                hits.add(i)
            elif rn in nc and len(rn) >= 16 and len(nc) - len(rn) <= 200:
                hits.add(i)
        if len(hits) > 4:
            return hits
    return hits


def match_file(
    index: dict[str, Any],
    *,
    doi: Any = "",
    eprint: Any = "",
    candidates: Sequence[str] = (),
) -> Optional[dict[str, Any]]:
    """对一个文件的全部信号做匹配；无可用信号返回 ``None``。

    返回 ``{"idx", "ratio", "margin", "how", "tie", "candidate", "hits"?}``；
    ``how`` ∈ ``doi``／``eprint``／``title``／``containment``／``containment-multi``。
    """
    key = str(doi or "").strip().lower()
    if key and key in index["by_doi"]:
        return {"idx": index["by_doi"][key], "ratio": 1.0, "margin": 1.0, "how": "doi", "tie": False, "candidate": ""}
    ekey = _norm_eprint(eprint)
    if ekey and ekey in index["by_eprint"]:
        return {"idx": index["by_eprint"][ekey], "ratio": 1.0, "margin": 1.0, "how": "eprint", "tie": False, "candidate": ""}
    norms = index["norms"]
    best_ratio, best_idx, best_cand = 0.0, -1, ""
    top: list[tuple[float, int]] = []
    for cand in candidates:
        nc = norm_text(cand)
        if len(nc) < 10:
            continue
        sm = difflib.SequenceMatcher()
        sm.set_seq1(nc)
        for i, rn in enumerate(norms):
            if not rn:
                continue
            if 2 * min(len(nc), len(rn)) <= best_ratio * (len(nc) + len(rn)):
                continue
            sm.set_seq2(rn)
            if sm.quick_ratio() <= best_ratio:
                continue
            ratio = sm.ratio()
            if ratio > best_ratio:
                best_ratio, best_idx, best_cand = ratio, i, cand
            top.append((ratio, i))
    if best_idx < 0:
        return None
    tie = len(index["groups"].get(norms[best_idx], [best_idx])) > 1
    second = 0.0
    for ratio, i in top:
        if norms[i] != norms[best_idx]:
            second = max(second, ratio)
    margin = (best_ratio - second) if second else best_ratio
    result = {
        "idx": best_idx,
        "ratio": best_ratio,
        "margin": margin,
        "how": "title",
        "tie": tie,
        "candidate": best_cand,
    }
    if best_ratio < TITLE_OK:
        hits = _containment_hits(index, candidates)
        if len(hits) == 1:
            return {
                "idx": next(iter(hits)),
                "ratio": best_ratio,
                "margin": margin,
                "how": "containment",
                "tie": False,
                "candidate": best_cand,
            }
        if len(hits) > 1:
            result["how"] = "containment-multi"
            result["hits"] = sorted(hits)
    return result


def tier_for(match: Optional[dict[str, Any]]) -> str:
    """把匹配结果折成分档：``strong``／``suggest``／``weak``／``none``。"""
    if not match:
        return "none"
    how = match["how"]
    ratio, margin = match["ratio"], match["margin"]
    if how in ("doi", "eprint"):
        return "strong"
    if how == "containment":
        return "suggest"
    if ratio >= TITLE_STRONG and margin >= MARGIN_STRONG and not match.get("tie"):
        return "strong"
    if ratio >= TITLE_OK and margin >= MARGIN_OK:
        return "suggest"
    if how == "containment-multi" or ratio >= TITLE_WEAK:
        return "weak"
    return "none"


def _nature_for(how: str) -> str:
    if how == "doi":
        return "doi-consistent"
    if how == "eprint":
        return "preprint-substitute"
    return "title-match"


def propose_links(
    records: Sequence[Any],
    files: Sequence[Any],
    *,
    scope: str = "unlinked",
    read_markdown: Callable[[Any], str],
    online_lookup: Optional[Callable[[Any, Sequence[str]], Sequence[str]]] = None,
    on_progress: Optional[Callable[[int, int], None]] = None,
) -> dict[str, Any]:
    """跑一整轮匹配；返回 ``{"proposals", "missing", "stats"}``。

    ``scope``：``unlinked``（仅未关联文件；默认）｜``all``（全部文件，已关联的
    计入 ``stats["skipped_linked"]``、不重复提案）。只评估 ``status == "done"``
    且有 ``md_path`` 的文件，其余计入 ``stats["skipped_unparsed"]``。

    ``online_lookup``（可选）：对本地未命中（``none``／``weak``）的文件做**联网
    增强**——回调收 ``(文件记录, 本地题名候选)``，返回额外题名候选（如按 DOI 取
    回的规范题名、或题名检索结果）；用扩充后的候选择优，命中即升级为提案
    （``how="online"``）；回调异常按"该文件无结果"处理。
    """
    if scope not in ("unlinked", "all"):
        raise ValueError(f"unknown scope: {scope!r} (expected 'unlinked' or 'all')")
    index = build_index(records)
    targets: list[Any] = []
    skipped_linked = 0
    skipped_unparsed = 0
    for f in files:
        if getattr(f, "status", "") != "done" or not getattr(f, "md_path", ""):
            skipped_unparsed += 1
            continue
        if getattr(f, "record_uuid", "") and scope == "unlinked":
            skipped_linked += 1
            continue
        targets.append(f)

    stats = {
        "evaluated": 0,
        "strong": 0,
        "suggest": 0,
        "weak": 0,
        "none": 0,
        "skipped_linked": skipped_linked,
        "skipped_unparsed": skipped_unparsed,
    }
    rank = {"none": 0, "weak": 1, "suggest": 2, "strong": 3}
    proposals: list[dict[str, Any]] = []
    covered: set[int] = set()
    best_candidates: list[tuple[Any, str]] = []
    tier_by_uuid: dict[str, str] = {}
    candidate_cache: dict[str, list[str]] = {}
    total = len(targets)

    def emit(f: Any, match: dict[str, Any], tier: str, how: str) -> None:
        record = records[match["idx"]]
        group = index["groups"].get(index["norms"][match["idx"]], [match["idx"]])
        if tier in ("strong", "suggest"):
            covered.update(group)
        proposals.append(
            {
                "file_uuid": getattr(f, "uuid", ""),
                "name": getattr(f, "name", "") or getattr(f, "path", ""),
                "record_uuid": getattr(record, "uuid", ""),
                "record_title": getattr(record, "title", ""),
                "record_year": getattr(record, "year", ""),
                "tier": tier,
                "how": how,
                "ratio": round(match["ratio"], 3),
                "margin": round(match["margin"], 3),
                "tie": bool(match.get("tie")),
                "nature": _nature_for(how),
            }
        )

    for done, f in enumerate(targets, start=1):
        text = read_markdown(f) or ""
        candidates = markdown_title_candidates(text)
        candidate_cache[getattr(f, "uuid", "")] = candidates
        match = match_file(
            index,
            doi=getattr(f, "doi", ""),
            eprint=getattr(f, "eprint", ""),
            candidates=candidates,
        )
        tier = tier_for(match)
        stats["evaluated"] += 1
        stats[tier] += 1
        tier_by_uuid[getattr(f, "uuid", "")] = tier
        if match is not None:
            candidate = match.get("candidate") or ""
            if candidate:
                best_candidates.append((f, norm_text(candidate)))
            if tier in ("strong", "suggest", "weak"):
                emit(f, match, tier, match["how"])
        if on_progress is not None:
            on_progress(done, total)

    if online_lookup is not None:
        stats["online_checked"] = 0
        stats["online_upgraded"] = 0
        online_targets = [
            f for f in targets if tier_by_uuid.get(getattr(f, "uuid", "")) in ("none", "weak")
        ]
        online_total = len(online_targets)
        for step, f in enumerate(online_targets, start=1):
            stats["online_checked"] += 1
            local = candidate_cache.get(getattr(f, "uuid", ""), [])
            try:
                extra = [
                    str(item).strip()
                    for item in (online_lookup(f, list(local)) or [])
                    if str(item or "").strip()
                ]
            except Exception:
                extra = []
            if extra:
                match = match_file(index, doi="", eprint="", candidates=list(local) + extra)
                tier = tier_for(match)
                current = tier_by_uuid.get(getattr(f, "uuid", ""), "none")
                # 联网升级一律按「存疑」呈现：DOI 抽取可能拿到文内引用的 DOI，
                # 自动升级只把候选端到人前，是否挂链由人工在预览里勾选。
                if match is not None and tier != "none" and rank["weak"] > rank.get(current, 0):
                    tier_by_uuid[getattr(f, "uuid", "")] = "weak"
                    stats["weak"] += 1
                    stats[current] -= 1
                    stats["online_upgraded"] += 1
                    emit(f, match, "weak", "online")
                    candidate = match.get("candidate") or ""
                    if candidate:
                        best_candidates.append((f, norm_text(candidate)))
            if on_progress is not None:
                on_progress(total + step, total + online_total)

    tier_order = {"strong": 0, "suggest": 1, "weak": 2}
    proposals.sort(key=lambda item: (tier_order.get(item["tier"], 9), -item["ratio"]))

    missing: list[dict[str, Any]] = []
    for i, record in enumerate(records):
        if i in covered:
            continue
        missing.append(
            {
                "uuid": getattr(record, "uuid", ""),
                "title": getattr(record, "title", ""),
                "year": getattr(record, "year", ""),
                "venue": getattr(record, "venue", ""),
                "closest_file": "",
                "closest_ratio": 0.0,
            }
        )
    # 给缺文记录找"最像的文件"（人工抽查入口；≥0.80 会提示"疑似有文件"）
    if best_candidates and missing:
        pos = {getattr(r, "uuid", ""): i for i, r in enumerate(records)}
        for item in missing:
            idx = pos.get(item["uuid"])
            if idx is None:
                continue
            rn = index["norms"][idx]
            if not rn:
                continue
            best_ratio, best_name = 0.0, ""
            for f, nc in best_candidates:
                if len(nc) < 10:
                    continue
                if 2 * min(len(nc), len(rn)) <= 0.70 * (len(nc) + len(rn)):
                    continue
                sm = difflib.SequenceMatcher(None, nc, rn)
                if sm.quick_ratio() <= 0.70:
                    continue
                ratio = sm.ratio()
                if ratio > best_ratio:
                    best_ratio = ratio
                    best_name = getattr(f, "name", "") or ""
            item["closest_file"] = best_name
            item["closest_ratio"] = round(best_ratio, 3)

    return {"proposals": proposals, "missing": missing, "stats": stats}
