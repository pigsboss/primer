# -*- coding: utf-8 -*-
"""联网比对（路径 2）：拿行标题去学术引擎链（OpenAlex → NASA ADS → Crossref）比对，命中即回填字段。

定位与边界（与主笔共识）：

* 只对**通过置信门槛**的候选回填：标题相似度 ≥ :data:`TITLE_RATIO`，且行内已有
  年份时与候选年份相差 ≤ :data:`YEAR_TOLERANCE`；未命中的行保持原值；
* 回填字段：title／authors／year／type／venue／doi——DOI 来自引擎本身，不受
  "原文子串"护栏限制（那条只针对模型输出）；
* ``engines`` 是引擎的注入点：单个 ``(title) -> list[候选]`` 或一组（依次尝试、
  先命中者生效；单个引擎抛错不阻断，全部引擎抛错才计失败）；服务端接
  :func:`openalex_lookup` 等现成引擎（见 :mod:`primer.literature.engines`），
  测试接假查询（不碰网络）；**全部行都失败**时抛回首异常（服务端据此把整任务
  标 failed）。

候选形状：``{"title", "authors"（list[str]）, "year"（int|None）, "venue", "type", "doi"}``。
"""

from __future__ import annotations

import difflib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Sequence

from .importers import SourceData
from .refine import _TARGET_COLUMNS, _cell, _ensure_column, _refresh, _reverse_map, _year_value

__all__ = [
    "OPENALEX_KEY_ENV",
    "OPENALEX_URL",
    "TITLE_RATIO",
    "VerifyReport",
    "openalex_lookup",
    "verify_source",
]

OPENALEX_URL = "https://api.openalex.org/works"
OPENALEX_KEY_ENV = "PRIMER_OPENALEX_API_KEY"
USER_AGENT = "primer-literature-web/0.1 (research)"
PAUSE_SECONDS = 0.12
REQUEST_TIMEOUT = 20.0
TITLE_RATIO = 0.85
YEAR_TOLERANCE = 1

# OpenAlex 的 type → 库的 type 词表。
TYPE_MAP = {
    "article": "journal-article",
    "review": "journal-article",
    "editorial": "journal-article",
    "proceedings-article": "conference-paper",
    "book": "book",
    "book-chapter": "book",
    "report": "report",
    "preprint": "preprint",
    "dissertation": "thesis",
    "standard": "standard",
    "dataset": "other",
}


@dataclass
class VerifyReport:
    """一次联网比对的结果统计：命中回填、未命中、查询失败。"""

    matched: int = 0
    unmatched: int = 0
    failed: int = 0


def openalex_lookup(title: str) -> list[dict[str, Any]]:
    """在 OpenAlex 按标题检索：**公共池优先，公共池限流时回落个人 key**。

    先不带 key 请求（公共池）；返回 HTTP 429（公共额度用完／被限流）时，读取
    :data:`OPENALEX_KEY_ENV`（``.env`` 注入）里的个人 key 重试同一查询一次；
    未配置 key 时抛出点名该变量的可操作错误。非 429 的错误不重试、原样上抛。
    """
    try:
        return _openalex_title_search(title, api_key="")
    except urllib.error.HTTPError as exc:
        if exc.code != 429:
            raise
        api_key = os.environ.get(OPENALEX_KEY_ENV, "").strip()
        if not api_key:
            raise RuntimeError(
                f"OpenAlex rate limited (HTTP 429) and {OPENALEX_KEY_ENV} is not set: "
                "register a free OpenAlex API key and put it in .env"
            ) from exc
    return _openalex_title_search(title, api_key=api_key)


def _openalex_title_search(title: str, *, api_key: str) -> list[dict[str, Any]]:
    """一次 OpenAlex 检索（``api_key`` 为空即公共池）；返回前 5 个候选的规范化字段。"""
    params = {
        "search": title,
        "per-page": 5,
        "select": (
            "title,doi,publication_year,authorships,primary_location,type,biblio,"
            "open_access,best_oa_location,locations"
        ),
    }
    if api_key:
        params["api_key"] = api_key
    query = urllib.parse.urlencode(params)
    request = urllib.request.Request(
        f"{OPENALEX_URL}?{query}", headers={"User-Agent": USER_AGENT}
    )
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
        payload = json.loads(response.read().decode("utf-8"))
    time.sleep(PAUSE_SECONDS)
    return [_openalex_work(work) for work in payload.get("results") or []]


def _openalex_work(work: Any) -> dict[str, Any]:
    """把一篇 OpenAlex work 映射为规范化候选（纯映射，供单测）。"""
    work = work if isinstance(work, dict) else {}
    location = work.get("primary_location") or {}
    source = location.get("source") or {}
    biblio = work.get("biblio") or {}
    first_page = str(biblio.get("first_page") or "").strip()
    last_page = str(biblio.get("last_page") or "").strip()
    pages = f"{first_page}-{last_page}" if first_page and last_page else (first_page or None)
    doi = str(work.get("doi") or "").replace("https://doi.org/", "").lower()
    open_access = work.get("open_access") or {}
    best_oa = work.get("best_oa_location") or {}
    arxiv_pdfs: list[str] = []
    other_pdfs: list[str] = []
    for loc in work.get("locations") or []:
        if not isinstance(loc, dict) or not loc.get("is_oa"):
            continue
        pdf = str(loc.get("pdf_url") or "").strip()
        if not pdf:
            continue
        (arxiv_pdfs if "arxiv.org" in pdf.lower() else other_pdfs).append(pdf)
    best_pdf = str(best_oa.get("pdf_url") or "").strip()
    ordered = (
        arxiv_pdfs
        + ([best_pdf] if best_pdf else [])
        + other_pdfs
        + [
            str(open_access.get("oa_url") or "").strip(),
            str(best_oa.get("landing_page_url") or "").strip(),
        ]
    )
    download_url = next((url for url in ordered if url), None)
    return {
        "title": re.sub(r"\s+", " ", str(work.get("title") or "")).strip(),
        "doi": doi or None,
        "year": work.get("publication_year"),
        "authors": [
            str((author.get("author") or {}).get("display_name") or "").strip()
            for author in (work.get("authorships") or [])
        ],
        "venue": str((source or {}).get("display_name") or "").strip() or None,
        "type": TYPE_MAP.get(str(work.get("type") or ""), "other"),
        "volume": str(biblio.get("volume") or "").strip() or None,
        "number": str(biblio.get("issue") or "").strip() or None,
        "pages": pages,
        "download_url": download_url,
    }


def verify_source(
    source: SourceData,
    indices: Sequence[int],
    engines: Callable[[str], list[dict[str, Any]]]
    | Sequence[Callable[[str], list[dict[str, Any]]]],
    *,
    on_progress: Optional[Callable[[int], None]] = None,
) -> VerifyReport:
    """对 ``indices`` 指定的行做联网比对：引擎链依次尝试，先命中者生效。

    * 每行对 ``engines`` 依次查询；某引擎给出置信匹配即采用（不再问后面的）；
    * 抛错的引擎只影响它自己，继续试下一个；
    * 一行**所有引擎都抛错**才计 ``failed``；有引擎应答（含未命中）计
      ``unmatched``；全部行都 failed 且有原始异常时抛回第一个异常。
    """
    if callable(engines):
        engines = [engines]
    report = VerifyReport()
    reverse = _reverse_map(source.columns, source.suggested)
    order = [index for index in indices if 0 <= index < len(source.rows)]
    first_error: Optional[BaseException] = None
    done = 0
    for index in order:
        row = source.rows[index]
        title = _cell(row, reverse.get("title"))
        if not title:
            report.unmatched += 1
        else:
            matched: Optional[Mapping[str, Any]] = None
            answered = False
            for lookup in engines:
                try:
                    candidates = lookup(title) or []
                except Exception as exc:
                    if first_error is None:
                        first_error = exc
                    continue
                answered = True
                best = _best_candidate(title, row, reverse, candidates)
                if best is not None:
                    matched = best
                    break
            if matched is None:
                if answered:
                    report.unmatched += 1
                else:
                    report.failed += 1
            else:
                _apply_candidate(source, reverse, index, matched)
                report.matched += 1
        done += 1
        if on_progress is not None:
            on_progress(done)
    if report.matched:
        _refresh(source)
    if order and report.failed == len(order) and first_error is not None:
        raise first_error
    return report


def _best_candidate(title, row, reverse, candidates):
    row_year = _year_value(_cell(row, reverse.get("year")))
    needle = _norm(title)
    best = None
    best_ratio = 0.0
    for candidate in candidates:
        candidate_title = str(candidate.get("title") or "")
        if not candidate_title:
            continue
        ratio = difflib.SequenceMatcher(None, needle, _norm(candidate_title)).ratio()
        if ratio < TITLE_RATIO:
            continue
        candidate_year = candidate.get("year")
        if (
            row_year is not None
            and isinstance(candidate_year, int)
            and abs(candidate_year - row_year) > YEAR_TOLERANCE
        ):
            continue
        if ratio > best_ratio:
            best, best_ratio = candidate, ratio
    return best


def _apply_candidate(source, reverse, index, candidate) -> None:
    row = source.rows[index]

    def people(key: str) -> Optional[str]:
        items = [str(item).strip() for item in (candidate.get(key) or []) if str(item).strip()]
        return "; ".join(items) if items else None

    fields: list[tuple[str, Any]] = [
        ("title", candidate.get("title")),
        ("authors", people("authors")),
        ("editor", people("editor")),
        ("translator", people("translator")),
        ("year", candidate.get("year")),
        ("type", candidate.get("type")),
        ("venue", candidate.get("venue")),
        ("doi", candidate.get("doi")),
    ]
    for key in _TARGET_COLUMNS:
        if key in ("title", "authors", "editor", "translator", "year", "type", "venue", "doi"):
            continue
        fields.append((key, candidate.get(key)))
    for field, value in fields:
        if value in (None, ""):
            continue
        column = reverse.get(field)
        if column is None:
            column = _TARGET_COLUMNS[field]
            _ensure_column(source, column)
            reverse[field] = column
        row[column] = value


def _norm(text: Any) -> str:
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", str(text or "")).lower()
