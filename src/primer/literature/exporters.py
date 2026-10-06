# -*- coding: utf-8 -*-
"""导出：目的地化格式（纯计算）。

按《方案_导出.md》v1：

* :func:`to_biblatex`：BibLaTeX ``.bib``（可选经典 BibTeX 字段、备注、本地文件路径）；
* :func:`to_ris`：RIS（Zotero／EndNote／Word 插件）；
* :func:`to_library_list`：图书馆人工获取清单（人读 md ＋ 回填 CSV，返回两段文本）；
* :func:`citation_text`：人读者录拼装（字段缺失回退备注「原始记录：…」的原文）。

映射与类型见方案 §二；全部函数只读记录、不触库、不联网。
"""

from __future__ import annotations

import csv
import datetime as _dt
import io
import re
from typing import Any, Mapping, Optional, Sequence

__all__ = ["citation_text", "to_biblatex", "to_library_list", "to_ris"]

_BIB_TYPES = {
    "journal-article": "@article",
    "conference-paper": "@inproceedings",
    "book-chapter": "@incollection",
    "book": "@book",
    "report": "@report",
    "preprint": "@online",
    "thesis": "@thesis",
    "patent": "@patent",
    "standard": "@manual",
    "other": "@misc",
}

_RIS_TYPES = {
    "journal-article": "JOUR",
    "conference-paper": "CONF",
    "book-chapter": "CHAP",
    "book": "BOOK",
    "report": "RPRT",
    "preprint": "UNPB",
    "thesis": "THES",
    "patent": "PATENT",
    "standard": "STD",
    "other": "GEN",
}

_TYPE_LABELS = {
    "journal-article": "期刊",
    "conference-paper": "会议",
    "book-chapter": "文集论文",
    "book": "图书",
    "report": "报告",
    "preprint": "预印本",
    "thesis": "学位论文",
    "patent": "专利",
    "standard": "标准",
    "other": "其他",
}

_LATEX_MAP = {
    "\\": r"\textbackslash{}",
    "{": r"\{",
    "}": r"\}",
    "&": r"\&",
    "%": r"\%",
    "#": r"\#",
    "_": r"\_",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
    "$": r"\$",
}


def _today() -> str:
    return _dt.date.today().isoformat()


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _escape_latex(value: Any) -> str:
    return "".join(_LATEX_MAP.get(char, char) for char in _text(value))


def _bib_pages(value: Any) -> str:
    """页码里的各类破折号统一为 LaTeX 的 ``--``。"""
    return re.sub(r"\s*[–—-]\s*", "--", _text(value))


def _keywords(record: Any) -> list[str]:
    raw = _text(getattr(record, "keywords", ""))
    return [item.strip() for item in re.split(r"[;；,，]", raw) if item.strip()]


def bib_key(record: Any, taken: set[str]) -> str:
    """引文键：``姓氏年首词``（ASCII 净化）；中文／无作者回退 ``ref<uuid8>``；冲突加 a／b／c。"""
    author = ""
    authors = list(getattr(record, "authors", None) or [])
    if authors:
        first = str(authors[0]).strip()
        part = first.split(",")[0].split()
        author = re.sub(r"[^A-Za-z]", "", part[0]).lower() if part else ""
    year = re.sub(r"[^0-9]", "", _text(getattr(record, "year", "")))
    word = ""
    for token in re.split(r"[^A-Za-z]+", _text(getattr(record, "title", ""))):
        if len(token) >= 3:
            word = token.lower()
            break
    tokens: list[str] = []
    if author:
        tokens.append(author)
    if year and (author or word):
        tokens.append(year)
    if word:
        tokens.append(word)
    base = "".join(tokens) or ("ref" + str(getattr(record, "uuid", ""))[:8])
    key = base
    suffix = "a"
    while key in taken:
        key = base + suffix
        suffix = chr(ord(suffix) + 1)
    taken.add(key)
    return key


def _bib_fields(
    record: Any,
    *,
    entry_type: str,
    classic: bool,
    notes: bool,
    file_paths: Optional[Sequence[str]],
) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []

    def add(name: str, value: Any, *, raw: bool = False) -> None:
        text = _text(value)
        if text:
            out.append((name, text if raw else _escape_latex(text)))

    author = " and ".join(str(item).strip() for item in (record.authors or []) if str(item).strip())
    add("author", author)
    add("title", record.title)
    venue = _text(record.venue)
    if entry_type == "@article":
        add("journal" if classic else "journaltitle", venue)
    elif entry_type in ("@inproceedings", "@incollection"):
        add("booktitle", venue)
    elif entry_type == "@report" and not _text(getattr(record, "institution", "")):
        add("institution", venue)
    elif entry_type == "@thesis" and not _text(getattr(record, "institution", "")):
        add("institution", venue)
    elif entry_type == "@book" and not _text(getattr(record, "publisher", "")):
        add("publisher", venue)
    if classic:
        add("year", record.year)
    else:
        add("date", record.year)
    add("volume", getattr(record, "volume", ""))
    add("number", getattr(record, "number", ""))
    pages = _bib_pages(getattr(record, "pages", ""))
    if pages:
        add("pages", pages, raw=True)
    elif _text(getattr(record, "eid", "")):
        add("eid", getattr(record, "eid", ""))
    add("publisher", getattr(record, "publisher", ""))
    add("address" if classic else "location", getattr(record, "location", ""))
    add("series", getattr(record, "series", ""))
    add("edition", getattr(record, "edition", ""))
    add("institution", getattr(record, "institution", ""))
    add("organization", getattr(record, "organization", ""))
    add("eventtitle", getattr(record, "eventtitle", ""))
    add("eventdate", getattr(record, "eventdate", ""))
    add("doi", getattr(record, "doi", ""))
    add("url", getattr(record, "url", ""))
    eprint = _text(getattr(record, "eprint", ""))
    if eprint:
        add("eprint", eprint)
        add("archiveprefix" if classic else "eprinttype", "arXiv")
    add("issn", getattr(record, "issn", ""))
    add("isbn", getattr(record, "isbn", ""))
    keywords = _keywords(record)
    if keywords:
        add("keywords", ", ".join(keywords), raw=False)
    if notes:
        add("note", getattr(record, "notes", ""))
    if file_paths:
        add("file", "; ".join(f":{path}:PDF" for path in file_paths), raw=True)
    return out


def to_biblatex(
    records: Sequence[Any],
    *,
    classic: bool = False,
    notes: bool = False,
    files: Optional[Mapping[str, Sequence[str]]] = None,
) -> str:
    """导出 BibLaTeX ``.bib``；``files`` 提供 ``uuid → 本地文件路径``（可选 ``file`` 字段）。"""
    taken: set[str] = set()
    lines = [f"% PRIMER 文献库导出 · {len(records)} 条 · {_today()}"]
    for record in records:
        entry_type = _BIB_TYPES.get(_text(getattr(record, "type", "")), "@misc")
        key = bib_key(record, taken)
        fields = _bib_fields(
            record,
            entry_type=entry_type,
            classic=classic,
            notes=notes,
            file_paths=(files or {}).get(record.uuid),
        )
        lines.append("")
        lines.append(f"{entry_type}{{{key},")
        for name, value in fields:
            lines.append(f"  {name} = {{{value}}},")
        lines.append("}")
    lines.append("")
    return "\n".join(lines)


def _split_pages(value: Any) -> tuple[str, str]:
    parts = re.split(r"\s*[–—-]\s*", _text(value), maxsplit=1)
    if not parts or not parts[0]:
        return "", ""
    start = parts[0]
    end = parts[1] if len(parts) > 1 else ""
    return start, end


def to_ris(
    records: Sequence[Any],
    *,
    notes: bool = False,
    files: Optional[Mapping[str, Sequence[str]]] = None,
) -> str:
    """导出 RIS（Zotero／EndNote／Word 插件可直接导入）。"""
    blocks: list[str] = []
    for record in records:
        ty = _RIS_TYPES.get(_text(getattr(record, "type", "")), "GEN")
        lines = [f"TY  - {ty}"]
        for author in record.authors or []:
            if _text(author):
                lines.append(f"AU  - {_text(author)}")
        if _text(record.title):
            lines.append(f"TI  - {_text(record.title)}")
        venue = _text(record.venue)
        if venue:
            if ty in ("JOUR", "UNPB"):
                lines.append(f"JO  - {venue}")
            elif ty in ("CONF", "CHAP"):
                lines.append(f"T2  - {venue}")
            elif ty in ("BOOK", "RPRT", "THES"):
                pass  # 容器走 PB/CY
        if _text(getattr(record, "volume", "")):
            lines.append(f"VL  - {_text(record.volume)}")
        if _text(getattr(record, "number", "")):
            lines.append(f"IS  - {_text(record.number)}")
        start, end = _split_pages(getattr(record, "pages", ""))
        if start:
            lines.append(f"SP  - {start}")
        if end:
            lines.append(f"EP  - {end}")
        if not start and _text(getattr(record, "eid", "")):
            lines.append(f"M1  - {_text(record.eid)}")
        if _text(getattr(record, "doi", "")):
            lines.append(f"DO  - {_text(record.doi)}")
        if _text(getattr(record, "url", "")):
            lines.append(f"UR  - {_text(record.url)}")
        if _text(getattr(record, "eprint", "")):
            lines.append(f"AN  - {_text(record.eprint)}")
        publisher = _text(getattr(record, "publisher", "")) or _text(
            getattr(record, "institution", "")
        )
        if not publisher:
            publisher = venue if ty in ("BOOK", "RPRT", "THES") else ""
        if publisher:
            lines.append(f"PB  - {publisher}")
        if _text(getattr(record, "location", "")):
            lines.append(f"CY  - {_text(record.location)}")
        if _text(getattr(record, "edition", "")):
            lines.append(f"ET  - {_text(getattr(record, 'edition', ''))}")
        if _text(getattr(record, "eventdate", "")):
            lines.append(f"DA  - {_text(getattr(record, 'eventdate', ''))}")
        if _text(record.year):
            lines.append(f"PY  - {_text(record.year)}")
        serial = _text(getattr(record, "issn", "")) or _text(getattr(record, "isbn", ""))
        if serial:
            lines.append(f"SN  - {serial}")
        for keyword in _keywords(record):
            lines.append(f"KW  - {keyword}")
        paths = list((files or {}).get(record.uuid) or [])
        for path in paths:
            lines.append(f"L1  - {path}")
        if notes and _text(getattr(record, "notes", "")):
            lines.append(f"N1  - {_text(getattr(record, 'notes', ''))}")
        lines.append("ER  - ")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + ("\n" if blocks else "")


_MD_JUNK = re.compile(r"^\s*#{1,6}\s*")
_EMPHASIS = re.compile(r"\*\*|__")


def _display(value: Any) -> str:
    """人读展示清洗：去行首 ``#`` 标题记号与 ``**``／``__`` 强调记号。"""
    text = _MD_JUNK.sub("", _text(value))
    text = _EMPHASIS.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def citation_text(record: Any) -> str:
    """人读者录：``作者. 题名. 容器, 年, 卷(期): 页码.``；作者/年份/容器全缺时回退备注原文。"""
    authors = [_display(item) for item in (record.authors or []) if _display(item)]
    title = _display(record.title).rstrip(".")
    parts: list[str] = []
    if authors:
        joined = ", ".join(authors)
        parts.append(joined + ("" if joined.endswith(".") else "."))
    if title:
        parts.append(title + ".")
    tail: list[str] = []
    venue = _display(record.venue)
    if venue:
        tail.append(venue)
    if record.year:
        tail.append(str(record.year))
    volume = _text(getattr(record, "volume", ""))
    number = _text(getattr(record, "number", ""))
    pages = _text(getattr(record, "pages", ""))
    location = volume + (f"({number})" if number else "")
    if pages:
        location = (location + ": " if location else "") + pages
    if location:
        tail.append(location)
    if tail:
        parts.append(", ".join(tail) + ".")
    text = " ".join(parts).strip()
    if not authors and not record.year and not venue:
        note = _text(getattr(record, "notes", ""))
        fallback = note[len("原始记录：") :].strip() if note.startswith("原始记录：") else note
        if fallback:
            return _display(fallback)
    return text


def _library_group(record: Any, group_by: str) -> str:
    if group_by == "year":
        return str(record.year) if record.year else "年份未注"
    venue = _display(record.venue)
    return venue or "未注明刊名/出版者"


_CSV_HEADER = ["序号", "著录", "DOI", "URL", "类型", "年份", "获取结果", "备注"]


def to_library_list(
    records: Sequence[Any],
    *,
    group_by: str = "venue",
    links: bool = True,
) -> tuple[str, str]:
    """图书馆人工获取清单：返回 ``(md 文本, csv 文本)``（csv 不带 BOM，由调用方写 utf-8-sig）。"""
    groups: dict[str, list[Any]] = {}
    for record in records:
        groups.setdefault(_library_group(record, group_by), []).append(record)
    ordered: list[tuple[str, list[Any]]] = []
    for name in sorted(groups):
        items = sorted(groups[name], key=lambda item: (item.year or 0, _text(item.title)))
        ordered.append((name, items))

    md: list[str] = [
        "# 图书馆原文获取清单",
        "",
        f"> 用途：请协助获取以下文献的原文（PDF）。共 {len(records)} 条，导出日期 {_today()}。",
        "> 回填：请在「获取结果」处填写（已获取／无权限／需馆际互借等），「备注」可写说明。",
        "",
    ]
    rows: list[list[str]] = []
    index = 0
    for name, items in ordered:
        md.append(f"## {name}（{len(items)} 条）")
        md.append("")
        for record in items:
            index += 1
            cite = citation_text(record)
            meta: list[str] = []
            if _text(getattr(record, "doi", "")):
                meta.append("DOI：" + _text(record.doi))
            if links and _text(getattr(record, "url", "")):
                meta.append("链接：" + _text(record.url))
            meta.append("类型：" + _TYPE_LABELS.get(_text(record.type), _text(record.type) or "其他"))
            if record.year:
                meta.append("年份：" + str(record.year))
            md.append(f"{index}. {cite}")
            md.append("   " + " ｜ ".join(meta))
            md.append("   获取结果：＿＿＿＿＿＿　备注：＿＿＿＿＿＿")
            md.append("")
            rows.append(
                [
                    str(index),
                    cite,
                    _text(getattr(record, "doi", "")),
                    _text(getattr(record, "url", "")) if links else "",
                    _TYPE_LABELS.get(_text(record.type), _text(record.type) or "其他"),
                    str(record.year) if record.year else "",
                    "",
                    "",
                ]
            )
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(_CSV_HEADER)
    writer.writerows(rows)
    return "\n".join(md), buffer.getvalue()
