# -*- coding: utf-8 -*-
"""联网比对的引擎实现（OpenAlex 的历史实现在 verify.py，新引擎都放这里）。

当前提供两个引擎：

* :func:`crossref_lookup`——Crossref REST API（DOI 官方注册库，无 key）：
  按标题检索（``query.title``）；
* :func:`ads_lookup`——NASA ADS API（天文／行星／空间科学覆盖最好；需要免费
  token，从环境变量 :data:`ADS_TOKEN_ENV` 读）。

候选形状（各引擎一致）：``{"title", "authors"（list[str]）, "year"（int|None）,
"venue", "type", "doi"}``。用途：记录补全「联网查询」的引擎链（见
:mod:`primer.literature.enrich`）。
"""

from __future__ import annotations

import html
import json
import os
import re
import time
import urllib.parse
import urllib.request
from typing import Any, Optional

__all__ = [
    "ADS_TOKEN_ENV",
    "ADS_URL",
    "CROSSREF_URL",
    "CROSSREF_TYPE_MAP",
    "ads_lookup",
    "crossref_lookup",
]

CROSSREF_URL = "https://api.crossref.org/works"
USER_AGENT = "primer-literature-web/0.1 (research)"
PAUSE_SECONDS = 0.12
REQUEST_TIMEOUT = 20.0

# Crossref 的 type → 库的 type 词表。
CROSSREF_TYPE_MAP = {
    "journal-article": "journal-article",
    "proceedings-article": "conference-paper",
    "book": "book",
    "book-chapter": "book",
    "monograph": "book",
    "reference-book": "book",
    "report": "report",
    "posted-content": "preprint",
    "dissertation": "thesis",
    "standard": "standard",
    "dataset": "other",
}


def crossref_lookup(title: str) -> list[dict[str, Any]]:
    """在 Crossref 按标题检索（无 key）；返回前 10 个候选的规范化字段。"""
    query = urllib.parse.urlencode({
        "query.title": title,
        "rows": 10,
        "select": (
            "DOI,title,author,editor,issued,type,container-title,"
            "volume,issue,page,publisher,ISBN,ISSN,URL"
        ),
    })
    request = urllib.request.Request(f"{CROSSREF_URL}?{query}", headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
        payload = json.loads(response.read().decode("utf-8"))
    time.sleep(PAUSE_SECONDS)
    items = (payload.get("message") or {}).get("items") or []
    return [_crossref_item(item) for item in items]


def _crossref_item(item: Any) -> dict[str, Any]:
    item = item if isinstance(item, dict) else {}
    return {
        "title": _clean_title(item.get("title")),
        "doi": str(item.get("DOI") or "").strip().lower() or None,
        "year": _issued_year(item.get("issued")),
        "authors": _authors(item.get("author")),
        "editor": _authors(item.get("editor")),
        "venue": _venue(item.get("container-title")),
        "type": CROSSREF_TYPE_MAP.get(str(item.get("type") or ""), "other"),
        "volume": _clean_text(item.get("volume")),
        "number": _clean_text(item.get("issue")),
        "pages": _clean_text(item.get("page")),
        "publisher": _clean_text(item.get("publisher")),
        "isbn": _first_text(item.get("ISBN")),
        "issn": _first_text(item.get("ISSN")),
        "url": _clean_text(item.get("URL")),
        "institution": _first_text(item.get("institution")),
        "eventtitle": _event_field(item.get("event"), "name"),
        "location": _event_field(item.get("event"), "location"),
    }


def _clean_text(value: Any) -> Optional[str]:
    text = str(value).strip() if value is not None else ""
    return text or None


def _first_text(value: Any) -> Optional[str]:
    """列表（ISBN／ISSN／institution）或标量取第一个非空；字典先看 name。"""
    values = value if isinstance(value, list) else [value]
    for item in values:
        if isinstance(item, dict):
            text = str(item.get("name") or "").strip()
        else:
            text = str(item or "").strip()
        if text:
            return text
    return None


def _event_field(event: Any, key: str) -> Optional[str]:
    if isinstance(event, dict):
        text = str(event.get(key) or "").strip()
        return text or None
    return None


def _clean_title(value: Any) -> str:
    titles = value if isinstance(value, list) else [value]
    text = str(titles[0]) if titles and titles[0] is not None else ""
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _issued_year(value: Any) -> Optional[int]:
    parts = value.get("date-parts") if isinstance(value, dict) else None
    try:
        return int(parts[0][0])
    except (TypeError, IndexError, ValueError):
        return None


def _authors(value: Any) -> list[str]:
    authors: list[str] = []
    for author in value or []:
        if not isinstance(author, dict):
            continue
        name = " ".join(
            part for part in (
                str(author.get("given") or "").strip(),
                str(author.get("family") or "").strip(),
            ) if part
        ).strip()
        if not name:
            name = str(author.get("name") or "").strip()
        if name:
            authors.append(name)
    return authors


def _venue(value: Any) -> Optional[str]:
    venues = value if isinstance(value, list) else [value]
    text = str(venues[0]).strip() if venues and venues[0] else ""
    return text or None


# ----------------------------------------------------------------- NASA ADS

ADS_URL = "https://api.adsabs.harvard.edu/v1/search/query"
ADS_TOKEN_ENV = "PRIMER_NASA_ADS_API_KEY"

# NASA ADS 的 doctype → 库的 type 词表。
ADS_TYPE_MAP = {
    "article": "journal-article",
    "inproceedings": "conference-paper",
    "proceedings": "conference-paper",
    "eprint": "preprint",
    "book": "book",
    "phdthesis": "thesis",
    "mastersthesis": "thesis",
    "techreport": "report",
    "dataset": "other",
    "software": "other",
}


def ads_lookup(title: str) -> list[dict[str, Any]]:
    """在 NASA ADS 按标题检索；token 从 :data:`ADS_TOKEN_ENV` 读，缺了抛错。"""
    token = os.environ.get(ADS_TOKEN_ENV, "").strip()
    if not token:
        raise RuntimeError(
            f"environment variable {ADS_TOKEN_ENV} is not set: register a free "
            "NASA ADS token and put it in .env"
        )
    query = urllib.parse.urlencode({
        "q": _ads_query(title),
        "fl": "title,author,year,pub,doi,doctype,volume,issue,page,issn,isbn,publisher",
        "rows": 10,
    })
    request = urllib.request.Request(
        f"{ADS_URL}?{query}",
        headers={"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
        payload = json.loads(response.read().decode("utf-8"))
    time.sleep(PAUSE_SECONDS)
    docs = (payload.get("response") or {}).get("docs") or []
    return [_ads_doc(doc) for doc in docs]


def _ads_query(title: str) -> str:
    escaped = str(title).replace("\\", "\\\\").replace('"', '\\"')
    return f'title:"{escaped}"'


def _ads_doc(doc: Any) -> dict[str, Any]:
    doc = doc if isinstance(doc, dict) else {}
    doi_values = doc.get("doi") or []
    return {
        "title": _clean_title(doc.get("title")),
        "doi": (str(doi_values[0]).strip().lower() or None) if doi_values else None,
        "year": _ads_year(doc.get("year")),
        "authors": [str(name).strip() for name in (doc.get("author") or []) if str(name).strip()],
        "venue": _venue(doc.get("pub")),
        "type": ADS_TYPE_MAP.get(str(doc.get("doctype") or "").strip().lower(), "other"),
        "volume": _clean_text(doc.get("volume")),
        "number": _clean_text(doc.get("issue")),
        "pages": _first_text(doc.get("page")),
        "issn": _first_text(doc.get("issn")),
        "isbn": _first_text(doc.get("isbn")),
        "publisher": _clean_text(doc.get("publisher")),
    }


def _ads_year(value: Any) -> Optional[int]:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None
