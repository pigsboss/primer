# -*- coding: utf-8 -*-
"""导入解析：CSV／JSON／BibTeX／Markdown → 库记录（或待映射的表格行）。

配合两个端点使用：

* :func:`parse_source` 按扩展名解析上传的字节流。JSON 若整体通过记录校验，
  按**库格式**处理（直接合并，无需映射）；否则作为**表格**（每项一个对象）
  交给映射对话框。CSV、BibTeX、Markdown 一律按表格处理。
* Markdown 优先按**参考文献清单**解析（复用 :mod:`primer.references.entries`
  的 ``parse_markdown``：``[n] 作者. 标题. 出处`` 条目与 ``［原文：…］`` 尾标，
  抽成 编号／分类／作者／标题／年份／出处／DOI／arXiv／原文 九列）；没有编号
  条目时退化为解析 markdown 管道表格。
* 表格的每一列由用户确认映射到 :data:`TARGET_FIELDS` 之一（或忽略）；
  :func:`rows_to_payloads` 按映射把行转成记录草稿——**缺标题的行被跳过并计数**
  （标题是库的必填字段）。

列名到目标字段的自动建议见 :data:`SUGGESTIONS`：先做小写精确匹配，
再做包含匹配（别名长度 ≥3 时才允许包含，避免 "doi" 之类的误伤）。
"""

from __future__ import annotations

import csv
import io
import json
import re
import uuid as _uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from ..references.entries import parse_markdown
from .library import FileRecord, LibraryError, Record, now_iso

__all__ = [
    "IGNORE",
    "SAMPLE_ROWS",
    "SUGGESTIONS",
    "SourceData",
    "TARGET_FIELDS",
    "parse_source",
    "rows_to_payloads",
    "split_authors",
]

SAMPLE_ROWS = 3
MAX_COLUMNS = 200
IGNORE = "ignore"

# 可映射的目标字段（顺序即界面顺序；标题必填）。
TARGET_FIELDS = (
    "title", "authors", "year", "type", "venue", "doi",
    "editor", "translator",
    "volume", "number", "pages", "eid", "publisher", "location",
    "institution", "organization", "series", "edition", "isbn", "issn",
    "url", "eprint", "eventtitle", "eventdate", "keywords",
    "projects", "notes",
)

# 源列名 → 目标字段的自动建议（先精确匹配，再做包含匹配）。
SUGGESTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("title", ("title", "标题", "篇名", "题目", "题名")),
    ("authors", ("author", "authors", "作者", "creator", "creators")),
    ("editor", ("editor", "editors", "编者", "主编")),
    ("translator", ("translator", "译者", "翻译")),
    ("year", ("year", "date", "issued", "publication_year", "年份", "出版年")),
    ("type", ("type", "entrytype", "itemtype", "document_type", "文献类型", "类型")),
    ("venue", ("venue", "journal", "booktitle", "publisher", "container", "期刊", "会议", "来源", "出处")),
    ("volume", ("volume", "卷")),
    ("number", ("number", "issue", "期号", "期", "编号")),
    ("pages", ("pages", "page", "页码", "页")),
    ("eid", ("eid", "文章号", "article_number")),
    ("publisher", ("publisher", "出版者", "出版社")),
    ("location", ("location", "address", "出版地", "地址")),
    ("institution", ("institution", "机构", "授予单位")),
    ("organization", ("organization", "主办", "主办单位")),
    ("series", ("series", "丛书", "系列")),
    ("edition", ("edition", "版次", "版本")),
    ("isbn", ("isbn",)),
    ("issn", ("issn",)),
    ("url", ("url", "链接", "网址")),
    ("eprint", ("eprint", "arxiv", "arxiv_id")),
    ("eventtitle", ("eventtitle", "event", "会议名称")),
    ("eventdate", ("eventdate", "会议日期", "会期")),
    ("keywords", ("keywords", "keyword", "tags", "关键词", "标签")),
    ("doi", ("doi",)),
    ("projects", ("projects", "project", "项目")),
    ("notes", ("notes", "note", "abstract", "备注", "摘要")),
)


@dataclass
class SourceData:
    """一次解析的产物：库格式记录，或待映射的表格行。

    ``raws`` 是逐条的原文（Markdown 清单解析时保留；其它格式为空），
    LLM 精析用它作为条目原文输入。``file_records`` 仅在库格式导入时携带
    （数组里 ``kind="file"`` 的本地文件记录）。
    """

    kind: str  # "library" | "table"
    total: int
    columns: list[str] = field(default_factory=list)
    sample: dict[str, list[str]] = field(default_factory=dict)
    suggested: dict[str, str] = field(default_factory=dict)
    rows: list[dict[str, Any]] = field(default_factory=list)
    records: list[Record] = field(default_factory=list)
    raws: list[str] = field(default_factory=list)
    file_records: list[FileRecord] = field(default_factory=list)


def parse_source(name: str, data: bytes) -> SourceData:
    """按扩展名解析上传的字节流；不支持或空内容抛 :class:`LibraryError`。"""
    suffix = Path(name).suffix.lower()
    if suffix == ".json":
        source = _parse_json(data)
    elif suffix == ".csv":
        source = _parse_csv(data)
    elif suffix in (".bib", ".bibtex"):
        source = _parse_bib(data)
    elif suffix in (".md", ".markdown"):
        source = _parse_md(data)
    else:
        raise LibraryError(
            f"unsupported import format: {name or '(no name)'} "
            "(expected .csv/.json/.bib/.md)"
        )
    if source.total == 0:
        raise LibraryError(f"no records found in {name}")
    return source


def row_text(row: dict[str, Any], columns: list[str]) -> str:
    """把一行渲染成单行文本（``列: 值 ｜ …``）——备注与 LLM 的兜底原文。"""
    parts: list[str] = []
    for column in columns:
        text = _stringify(row.get(column)).strip()
        if text:
            parts.append(f"{column}: {text}")
    return " ｜ ".join(parts)


def rows_to_payloads(
    rows: list[dict[str, Any]],
    columns: list[str],
    mapping: dict[str, str],
    raws: Optional[list[str]] = None,
) -> tuple[list[dict[str, Any]], int]:
    """按映射把表格行转成记录草稿；返回 ``(草稿列表, 被跳过行数)``。

    只有 :data:`TARGET_FIELDS` 里的目标会被采纳；标题为空的行被跳过。
    同一目标字段被多列映射时，按列顺序、非空值后者覆盖前者。
    ``notes`` 一律追加一行 ``原始记录：…``（``raws`` 里有该行原文就用原文，
    否则用 :func:`row_text` 的行渲染）——备注按"每行一个键值对"约定使用。
    """
    payloads: list[dict[str, Any]] = []
    skipped = 0
    for index, row in enumerate(rows):
        payload: dict[str, Any] = {}
        for column in columns:
            target = mapping.get(column, IGNORE)
            if target not in TARGET_FIELDS:
                continue
            value = row.get(column)
            if target in ("authors", "editor", "translator"):
                people = split_authors(value)
                if people:
                    payload[target] = people
            elif target == "projects":
                projects = _split_projects(value)
                if projects:
                    payload["projects"] = projects
            elif target == "year":
                year = _coerce_year(value)
                if year is not None:
                    payload["year"] = year
            else:
                text = _stringify(value).strip()
                if text:
                    payload[target] = text
        title = payload.get("title")
        if not isinstance(title, str) or not title.strip():
            skipped += 1
            continue
        if raws and index < len(raws) and raws[index]:
            original = raws[index]
        else:
            original = row_text(row, columns)
        line = f"原始记录：{original}"
        existing = payload.get("notes")
        payload["notes"] = f"{existing}\n{line}" if existing else line
        payload["uuid"] = str(_uuid.uuid4())
        payload["created_at"] = now_iso()
        payload["updated_at"] = payload["created_at"]
        payloads.append(payload)
    return payloads, skipped


def split_authors(value: Any) -> list[str]:
    """作者字段拆成列表：支持 ``A and B``（BibTeX）、分号／换行与已有列表。

    逗号**不**拆（``Last, First`` 属于同一位作者的 BibTeX 写法）。
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    parts = re.split(r"\s+and\s+|[;；\n]", str(value), flags=re.IGNORECASE)
    return [part.strip() for part in parts if part.strip()]


# ------------------------------------------------------------------ 解析器


def _parse_json(data: bytes) -> SourceData:
    try:
        payload = json.loads(_decode(data))
    except json.JSONDecodeError as exc:
        raise LibraryError(
            f"invalid JSON at line {exc.lineno} column {exc.colno}: {exc.msg}"
        ) from exc
    if not isinstance(payload, list):
        raise LibraryError("JSON import expects an array (of records or of objects)")
    records: list[Record] = []
    file_records: list[FileRecord] = []
    library_ok = True
    for index, item in enumerate(payload):
        if isinstance(item, dict) and item.get("kind") == "file":
            try:
                file_records.append(FileRecord.from_dict(item, f"file_records[{index}]"))
            except LibraryError:
                library_ok = False
                break
            continue
        try:
            records.append(Record.from_dict(item, f"records[{index}]"))
        except LibraryError:
            library_ok = False
            break
    if library_ok:
        return SourceData(
            kind="library", total=len(records), records=records, file_records=file_records
        )
    rows: list[dict[str, Any]] = []
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            raise LibraryError(f"records[{index}]: expected an object for table import")
        rows.append(item)
    return _table_source(rows)


def _parse_csv(data: bytes) -> SourceData:
    text = _decode(data)
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    raw_rows = [row for row in csv.reader(io.StringIO(text), dialect) if any(cell.strip() for cell in row)]
    if not raw_rows:
        return _table_source([])
    seen: dict[str, int] = {}
    header: list[str] = []
    for index, cell in enumerate(raw_rows[0]):
        name = cell.strip() or f"列{index + 1}"
        count = seen.get(name, 0) + 1
        seen[name] = count
        header.append(name if count == 1 else f"{name}_{count}")
    rows: list[dict[str, Any]] = []
    for cells in raw_rows[1:]:
        row: dict[str, Any] = {}
        for index, cell in enumerate(cells):
            key = header[index] if index < len(header) else f"列{index + 1}"
            row[key] = cell
        rows.append(row)
    return _table_source(rows)


def _parse_bib(data: bytes) -> SourceData:
    entries = _parse_bib_entries(_decode(data))
    rows: list[dict[str, Any]] = []
    for fields in entries:
        rows.append(dict(fields))
    return _table_source(rows)


_BIB_ENTRY_RE = re.compile(r"@([A-Za-z]+)\s*([{(])")


def _parse_bib_entries(text: str) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    position = 0
    while True:
        match = _BIB_ENTRY_RE.search(text, position)
        if match is None:
            break
        entry_type = match.group(1).lower()
        opener = match.group(2)
        body, position = _balanced_body(text, match.end(), opener)
        if entry_type in ("comment", "string", "preamble"):
            continue
        fields = _split_bib_fields(body)
        if fields:
            fields["entrytype"] = entry_type
            entries.append(fields)
    return entries


def _balanced_body(text: str, start: int, opener: str) -> tuple[str, int]:
    """取配对括号内的条目正文，返回 ``(正文, 结束位置)``。"""
    depth = 1
    closer = "}" if opener == "{" else ")"
    index = start
    while index < len(text):
        char = text[index]
        if opener == "{" and char == "{":
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return text[start:index], index + 1
        index += 1
    raise LibraryError("unbalanced braces in BibTeX entry")


def _split_bib_fields(body: str) -> dict[str, str]:
    parts: list[str] = []
    depth = 0
    in_quotes = False
    start = 0
    for index, char in enumerate(body):
        if char == '"':
            in_quotes = not in_quotes
        elif not in_quotes and char == "{":
            depth += 1
        elif not in_quotes and char == "}":
            if depth > 0:
                depth -= 1
        elif char == "," and depth == 0 and not in_quotes:
            parts.append(body[start:index])
            start = index + 1
    parts.append(body[start:])
    fields: dict[str, str] = {}
    for part in parts[1:]:  # 第一段是引用键（key,）
        match = re.match(r"\s*([A-Za-z0-9_\-]+)\s*=\s*(.*)$", part, re.S)
        if match is None:
            continue
        value = _clean_bib_value(match.group(2))
        if value:
            fields[match.group(1).lower()] = value
    return fields


def _clean_bib_value(raw: str) -> str:
    value = raw.strip()
    if "#" in value:  # 拼接："A" # "B"
        return "".join(_clean_bib_value(part) for part in value.split("#"))
    if len(value) >= 2 and value.startswith("{") and value.endswith("}"):
        value = value[1:-1]
    elif len(value) >= 2 and value.startswith('"') and value.endswith('"'):
        value = value[1:-1]
    value = re.sub(r"\s+", " ", value).strip()
    return value.replace("{", "").replace("}", "").strip()


# --------------------------------------------------------------- Markdown

_YEAR_RE = re.compile(r"(?<!\d)(1[89]\d{2}|20\d{2})(?!\d)")


def _parse_md(data: bytes) -> SourceData:
    """Markdown：先按参考文献清单解析；没有编号条目时退化为管道表格。"""
    text = _decode(data)
    entries = [
        entry
        for ref_list in parse_markdown(text, source="<import>")
        for entry in ref_list.entries
    ]
    if entries:
        source = _table_source([_entry_row(entry) for entry in entries])
        source.raws = [entry.raw for entry in entries]
        return source
    return _table_source(_parse_markdown_tables(text))


def _entry_row(entry: Any) -> dict[str, Any]:
    """把 :class:`primer.references.entries.RefEntry` 摊成一行九列。"""
    class_label = " ".join(part for part in (entry.class_letter, entry.class_title) if part)
    years = _YEAR_RE.findall(entry.raw)
    return {
        "编号": entry.number,
        "分类": class_label,
        "作者": entry.authors,
        "标题": entry.title,
        "年份": years[-1] if years else "",
        "出处": entry.venue_year,
        "DOI": entry.doi or "",
        "arXiv": entry.arxiv or "",
        "原文": entry.tag or "",
    }


def _parse_markdown_tables(text: str) -> list[dict[str, Any]]:
    """解析 markdown 管道表格（可多张，列名取并集）；非表格内容忽略。"""
    rows: list[dict[str, Any]] = []
    header: Optional[list[str]] = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|") and len(stripped) >= 2:
            cells = [cell.strip() for cell in stripped.strip("|").split("|")]
            if header is None:
                header = [cell or f"列{index + 1}" for index, cell in enumerate(cells)]
                continue
            if all(re.fullmatch(r":?-{2,}:?", cell) for cell in cells if cell):
                continue  # 分隔行 | --- | --- |
            row: dict[str, Any] = {}
            for index, cell in enumerate(cells):
                key = header[index] if index < len(header) else f"列{index + 1}"
                row[key] = cell
            rows.append(row)
        else:
            header = None
    return rows


# ------------------------------------------------------------------ 表格


def _table_source(rows: list[dict[str, Any]]) -> SourceData:
    columns = _collect_columns(rows)
    if len(columns) > MAX_COLUMNS:
        raise LibraryError(f"too many columns: {len(columns)} (max {MAX_COLUMNS})")
    sample = {
        column: [_short(_stringify(row.get(column))) for row in rows[:SAMPLE_ROWS]]
        for column in columns
    }
    return SourceData(
        kind="table",
        total=len(rows),
        columns=columns,
        sample=sample,
        suggested=_suggest(columns),
        rows=rows,
    )


def _collect_columns(rows: list[dict[str, Any]]) -> list[str]:
    columns: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row.keys():
            name = str(key)
            if name not in seen:
                seen.add(name)
                columns.append(name)
    return columns


def _suggest(columns: list[str]) -> dict[str, str]:
    suggested = {column: IGNORE for column in columns}
    taken: set[str] = set()
    for field_name, aliases in SUGGESTIONS:
        if field_name in taken:
            continue
        for column in columns:
            if suggested[column] != IGNORE:
                continue
            lowered = column.strip().lower()
            if lowered in aliases or any(
                len(alias) >= 3 and alias in lowered for alias in aliases
            ):
                suggested[column] = field_name
                taken.add(field_name)
                break
    return suggested


# ------------------------------------------------------------------ 值转换


def _decode(data: bytes) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("gb18030", errors="replace")


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return "; ".join(_stringify(item) for item in value)
    return str(value)


def _short(text: str, limit: int = 60) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _split_projects(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        items = [str(item) for item in value]
    else:
        items = re.split(r"[;；\n]", str(value))
    return [item.strip() for item in items if item.strip()]


def _coerce_year(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    match = re.search(r"(1[5-9]\d{2}|2\d{3})", _stringify(value))
    return int(match.group(1)) if match else None
