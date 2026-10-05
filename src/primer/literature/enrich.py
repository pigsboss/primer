# -*- coding: utf-8 -*-
"""记录补全（编辑菜单）：对库中选中的记录做联网查询或 LLM 分析，补全／修正字段。

* 联网（:func:`verify_records`）：标题依次查**引擎链**（OpenAlex → NASA ADS → Crossref，
  前者不通或未命中自动落后者），置信匹配（相似度 ≥ 0.85、年份差 ≤ 1）后回填；
  DOI 由引擎给出，不受"原文子串"护栏限制；
* AI（:func:`ai_parse`）：``chat`` 注入（服务端接真实客户端，测试接假客户端）；
  按块发送（默认 :data:`CHUNK_SIZE` 条），块失败只影响该块；DOI 只认记录自身
  文本（含 notes）里出现过的字面量——防幻觉；
* 两条路径都只碰**书目字段**（见 :data:`FIELDS`，biblatex 规范名）；
  uuid／files／projects／notes／created_at 一概不动，有变化才刷新 updated_at；
* 统计：updated（有字段变化）／skipped（未命中或无需变化）／failed（查询或块失败）。
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Sequence

from .importers import split_authors
from .library import Record, now_iso
from .refine import _DOI_PREFIX_RE, _parse_reply, _year_value
from .verify import TITLE_RATIO, YEAR_TOLERANCE, _norm

__all__ = [
    "AI_SYSTEM_PROMPT",
    "CHUNK_SIZE",
    "EnrichReport",
    "FIELDS",
    "TYPES",
    "ai_parse",
    "apply_candidate",
    "apply_fields",
    "apply_pending",
    "candidate_fields",
    "clean_ai_fields",
    "diff_fields",
    "match_candidate",
    "preview_ai_parse",
    "preview_verify_records",
    "record_text",
    "verify_records",
]

CHUNK_SIZE = 10

#: 六个既定字段之外、纯文本落盘的书目字段（与 library 的 _RECORD_TEXT_FIELDS 一致）。
_EXTRA_TEXT_FIELDS = (
    "volume", "number", "pages", "eid", "publisher", "location",
    "institution", "organization", "series", "edition", "isbn", "issn",
    "url", "eprint", "eventtitle", "eventdate", "keywords",
)

FIELDS = ("title", "authors", "year", "type", "venue", "doi", "editor", "translator") + _EXTRA_TEXT_FIELDS

TYPES = (
    "journal-article",
    "conference-paper",
    "book-chapter",
    "standard",
    "report",
    "preprint",
    "book",
    "thesis",
    "patent",
    "other",
)

AI_SYSTEM_PROMPT = """你是文献题录的校订器。输入是若干条文献记录，每条以 "i=<序号>:" 开头，
后跟若干 "字段: 值" 行（notes 里可能包含原始题录文本）。
对每一条输出一个 JSON 对象（不确定的字段给 null，宁可空着也不编造）：
{"i": <序号>, "title": <字符串>, "authors": [<字符串>, ...], "year": <四位整数或 null>,
 "type": <字符串>, "venue": <字符串>, "doi": <字符串或 null>,
 "editor": [<字符串>, ...] 或 null, "translator": [<字符串>, ...] 或 null,
 "volume": <字符串>, "number": <字符串>, "pages": <字符串>, "eid": <字符串>,
 "publisher": <字符串>, "location": <字符串>, "institution": <字符串>,
 "organization": <字符串>, "series": <字符串>, "edition": <字符串>,
 "isbn": <字符串>, "issn": <字符串>, "url": <字符串>, "eprint": <字符串>,
 "eventtitle": <字符串>, "eventdate": <字符串>, "keywords": <字符串>}
规则：
- 只输出一个 JSON 数组，包含输入里的每一条；不要解释，不要 markdown 代码围栏；
- 已有且正确的字段原样返回；缺失或明显错误的字段补全／修正；无法确定就给 null；
- title：完整标题，去掉作者名、出处与多余标注；
- authors／editor／translator：列表，按原文顺序；机构名可作为作者；没有给 null；
- year：出版年份（四位整数）；venue：期刊名／文集名／出版社等"容器"，不含年份；
- type：在 journal-article / conference-paper / book-chapter / standard / report /
  preprint / book / thesis / patent / other 中择一；
- volume 卷；number 期号（期刊）或编号（报告／标准／专利）；pages 页码（如 45-67，
  文章号也可写这）；eid 电子文章号；publisher 出版者；location 出版地；
  institution 机构（学位授予单位／报告机构）；organization 会议主办单位；
  series 丛书／系列；edition 版次；isbn／issn；url 链接；eprint arXiv 编号；
  eventtitle 会议名称；eventdate 会议日期；keywords 关键词（分号分隔）；
- doi：只能逐字取自输入文本（包括 notes）；输入中没有或拿不准就给 null——绝不猜测；
- 不要输出 uuid／files／projects／notes／created_at／updated_at 等其它字段。
"""


@dataclass
class EnrichReport:
    """一次记录补全的结果统计。"""

    updated: int = 0
    skipped: int = 0
    failed: int = 0


def record_text(record: Record) -> str:
    """记录的整文本（含备注）：AI 输入展示与 DOI 护栏都用它。"""
    parts: list[str] = []
    for key in FIELDS:
        value = getattr(record, key, "")
        if isinstance(value, (list, tuple)):
            value = "; ".join(str(item) for item in value if str(item).strip())
        text = str(value).strip() if value not in (None, "") else ""
        if text:
            parts.append(text)
    notes = str(record.notes or "").strip()
    if notes:
        parts.append(notes)
    return " ｜ ".join(parts)


def match_candidate(
    record: Record, candidates: Sequence[Mapping[str, Any]]
) -> Optional[Mapping[str, Any]]:
    """置信匹配：标题相似度 ≥ :data:`TITLE_RATIO`；记录已有年份时年份差 ≤ ``YEAR_TOLERANCE``。"""
    needle = _norm(record.title)
    best: Optional[Mapping[str, Any]] = None
    best_ratio = 0.0
    for candidate in candidates:
        title = str(candidate.get("title") or "")
        if not title:
            continue
        ratio = difflib.SequenceMatcher(None, needle, _norm(title)).ratio()
        if ratio < TITLE_RATIO:
            continue
        year = candidate.get("year")
        if (
            record.year is not None
            and isinstance(year, int)
            and not isinstance(year, bool)
            and abs(year - record.year) > YEAR_TOLERANCE
        ):
            continue
        if ratio > best_ratio:
            best, best_ratio = candidate, ratio
    return best


def _clean_list(value: Any) -> Optional[list[str]]:
    """列表型字段的清洗：兼容列表与字符串，空则 None。"""
    items = split_authors(value)
    return items or None


def candidate_fields(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """把引擎候选清洗为可应用字段（引擎直采，DOI 不受"原文子串"护栏限制）。"""
    fields: dict[str, Any] = {
        "title": _clean_text(candidate.get("title")),
        "authors": _clean_list(candidate.get("authors")),
        "year": _year_value(candidate.get("year")),
        "type": _clean_type(candidate.get("type")),
        "venue": _clean_text(candidate.get("venue")),
        "doi": _clean_doi(candidate.get("doi")),
    }
    for key in ("editor", "translator"):
        if key in candidate:
            fields[key] = _clean_list(candidate.get(key))
    for key in _EXTRA_TEXT_FIELDS:
        if key in candidate:
            fields[key] = _clean_text(candidate.get(key))
    return fields


def apply_candidate(record: Record, candidate: Mapping[str, Any]) -> int:
    """引擎候选直接可信：就地写入各字段，返回变化的字段数。"""
    return apply_fields(record, candidate_fields(candidate))


def diff_fields(record: Record, fields: Mapping[str, Any]) -> list[dict[str, Any]]:
    """算出"若应用 ``fields``，哪些字段会变"：返回 [{field, old, new}]，只含真变化。"""
    changes: list[dict[str, Any]] = []
    for field, value in fields.items():
        if field not in FIELDS or value is None or value == "":
            continue
        if field in ("authors", "editor", "translator"):
            new_value = [str(item).strip() for item in value if str(item).strip()]
            if not new_value:
                continue
            old_value = list(getattr(record, field) or [])
            if new_value != old_value:
                changes.append({"field": field, "old": old_value, "new": new_value})
        elif field == "year":
            if isinstance(value, bool) or not isinstance(value, int):
                continue
            if record.year != value:
                changes.append({"field": field, "old": record.year, "new": value})
        else:
            new_value = str(value).strip()
            old_raw = str(getattr(record, field) or "")
            if old_raw.strip() != new_value:
                changes.append({"field": field, "old": old_raw, "new": new_value})
    return changes


def clean_ai_fields(record: Record, item: Mapping[str, Any]) -> dict[str, Any]:
    """把 LLM 返回的单个对象清洗为可应用字段（含护栏；notes 原文可作 DOI 依据）。"""
    raw = record_text(record)
    fields: dict[str, Any] = {}
    if "title" in item:
        title = _clean_text(item.get("title"))
        if title and len(title) >= 2:
            fields["title"] = title
    if "authors" in item:
        authors = split_authors(item.get("authors"))
        if authors:
            fields["authors"] = authors
    for key in ("editor", "translator"):
        if key in item:
            items = split_authors(item.get(key))
            if items:
                fields[key] = items
    if "year" in item:
        year = _year_value(item.get("year"))
        if year is not None:
            fields["year"] = year
    if "type" in item:
        kind = _clean_type(item.get("type"))
        if kind:
            fields["type"] = kind
    if "venue" in item:
        venue = _clean_text(item.get("venue"))
        if venue:
            fields["venue"] = venue
    if "doi" in item:
        doi = _clean_doi(item.get("doi"))
        if doi and _identifier_in_text(doi, raw):
            fields["doi"] = doi
    for key in _EXTRA_TEXT_FIELDS:
        if key in item:
            text = _clean_text(item.get(key))
            if text:
                fields[key] = text
    return fields


def apply_fields(record: Record, fields: Mapping[str, Any]) -> int:
    """把清洗后的字段合并进记录；空值跳过；返回实际变化的字段数。"""
    changed = 0
    for field, value in fields.items():
        if field not in FIELDS or value is None or value == "":
            continue
        if field in ("authors", "editor", "translator"):
            new_value = [str(item).strip() for item in value if str(item).strip()]
            if not new_value:
                continue
            if new_value != list(getattr(record, field) or []):
                setattr(record, field, new_value)
                changed += 1
        elif field == "year":
            if isinstance(value, bool) or not isinstance(value, int):
                continue
            if record.year != value:
                record.year = value
                changed += 1
        else:
            new_value = str(value).strip()
            current = str(getattr(record, field) or "").strip()
            if current != new_value:
                setattr(record, field, new_value)
                changed += 1
    if changed:
        record.updated_at = now_iso()
    return changed


def preview_verify_records(
    records: Sequence[Record],
    engines: Callable[[str], list[dict[str, Any]]]
    | Sequence[Callable[[str], list[dict[str, Any]]]],
    *,
    on_progress: Optional[Callable[[int], None]] = None,
) -> tuple[EnrichReport, list[dict[str, Any]]]:
    """联网查询的**预演**：只计算变更、不改记录；返回 (统计, 待应用清单)。

    清单元素形如 ``{"uuid", "title", "changes": [{"field", "old", "new"}]}``；
    引擎链语义：依次尝试、先命中者生效；**所有引擎都抛错**的记录计 ``failed``，
    全部记录都 failed 且有原始异常时抛回第一个异常。
    """
    if callable(engines):
        engines = [engines]
    report = EnrichReport()
    pending: list[dict[str, Any]] = []
    first_error: Optional[BaseException] = None
    attempted = 0
    for index, record in enumerate(records):
        title = str(record.title or "").strip()
        if not title:
            report.skipped += 1
        else:
            attempted += 1
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
                best = match_candidate(record, candidates)
                if best is not None:
                    matched = best
                    break
            if matched is None:
                if answered:
                    report.skipped += 1
                else:
                    report.failed += 1
            else:
                changes = diff_fields(record, candidate_fields(matched))
                if changes:
                    pending.append(
                        {"uuid": record.uuid, "title": record.title, "changes": changes}
                    )
                    report.updated += 1
                else:
                    report.skipped += 1
        if on_progress is not None:
            on_progress(index + 1)
    if attempted and report.failed == attempted and first_error is not None:
        raise first_error
    return report, pending


def apply_pending(records: Sequence[Record], pending: Sequence[Mapping[str, Any]]) -> int:
    """把预演清单应用到记录上；返回实际更新的记录数。"""
    by_uuid = {record.uuid: record for record in records}
    updated = 0
    for item in pending:
        record = by_uuid.get(str(item.get("uuid") or ""))
        if record is None:
            continue
        fields = {
            str(change.get("field")): change.get("new")
            for change in (item.get("changes") or [])
            if isinstance(change, Mapping) and change.get("field")
        }
        if fields and apply_fields(record, fields):
            updated += 1
    return updated


def verify_records(
    records: Sequence[Record],
    engines: Callable[[str], list[dict[str, Any]]]
    | Sequence[Callable[[str], list[dict[str, Any]]]],
    *,
    on_progress: Optional[Callable[[int], None]] = None,
) -> EnrichReport:
    """逐条联网查询并回填（预演＋立即应用；行为与旧版一致）。"""
    report, pending = preview_verify_records(records, engines, on_progress=on_progress)
    apply_pending(records, pending)
    return report


def preview_ai_parse(
    records: Sequence[Record],
    indices: Sequence[int],
    chat: Callable[[str, str], str],
    *,
    chunk_size: int = CHUNK_SIZE,
    on_progress: Optional[Callable[[int], None]] = None,
) -> tuple[EnrichReport, list[dict[str, Any]]]:
    """AI 校订的**预演**：只计算变更、不改记录；返回 (统计, 待应用清单)。

    块失败（调用报错或回信不可解析）只影响该块；**全部块都失败**时抛回第一个
    原始异常——缺密钥／端点不可达这类系统性错误直接可见。
    """
    report = EnrichReport()
    pending: list[dict[str, Any]] = []
    order = [index for index in indices if 0 <= index < len(records)]
    done = 0
    first_error: Optional[BaseException] = None
    for start in range(0, len(order), chunk_size):
        chunk = order[start:start + chunk_size]
        try:
            reply = chat(AI_SYSTEM_PROMPT, _build_user(records, chunk))
            items = _parse_reply(reply)
        except Exception as exc:
            if first_error is None:
                first_error = exc
            report.failed += len(chunk)
            done += len(chunk)
            if on_progress is not None:
                on_progress(done)
            continue
        by_index = {item["i"]: item for item in items}
        for index in chunk:
            item = by_index.get(index)
            record = records[index]
            if item is None:
                report.failed += 1
                continue
            changes = diff_fields(record, clean_ai_fields(record, item))
            if changes:
                pending.append({"uuid": record.uuid, "title": record.title, "changes": changes})
                report.updated += 1
            else:
                report.skipped += 1
        done += len(chunk)
        if on_progress is not None:
            on_progress(done)
    if report.updated == 0 and report.failed > 0 and first_error is not None:
        raise first_error
    return report, pending


def ai_parse(
    records: Sequence[Record],
    indices: Sequence[int],
    chat: Callable[[str, str], str],
    *,
    chunk_size: int = CHUNK_SIZE,
    on_progress: Optional[Callable[[int], None]] = None,
) -> EnrichReport:
    """对 ``indices`` 指定的记录做 LLM 校订（预演＋立即应用；行为与旧版一致）。"""
    report, pending = preview_ai_parse(
        records, indices, chat, chunk_size=chunk_size, on_progress=on_progress
    )
    apply_pending(records, pending)
    return report


# ---------------------------------------------------------------- 内部


def _build_user(records: Sequence[Record], chunk: Sequence[int]) -> str:
    blocks = []
    for index in chunk:
        record = records[index]
        lines = [f"i={index}:"]
        for label, value in (
            ("title", record.title),
            ("authors", "; ".join(record.authors or [])),
            ("editor", "; ".join(record.editor or [])),
            ("translator", "; ".join(record.translator or [])),
            ("year", record.year),
            ("venue", record.venue),
            ("type", record.type),
            ("doi", record.doi),
            *((key, getattr(record, key)) for key in _EXTRA_TEXT_FIELDS),
            ("notes", record.notes),
        ):
            text = str(value).strip() if value is not None else ""
            if text:
                lines.append(f"  {label}: {text}")
        blocks.append("\n".join(lines))
    return "\n".join(blocks)


def _clean_text(value: Any) -> Optional[str]:
    text = str(value).strip() if value is not None else ""
    return text or None


def _clean_type(value: Any) -> Optional[str]:
    text = str(value or "").strip().lower()
    return text if text in TYPES else None


def _clean_doi(value: Any) -> Optional[str]:
    text = _DOI_PREFIX_RE.sub("", str(value or "").strip()).rstrip(".,;")
    return text or None


def _identifier_in_text(needle: str, raw: str) -> bool:
    """DOI 只认记录文本里出现过的字面量（比对忽略 doi.org 前缀与大小写）。"""
    haystack = raw.lower().replace("https://doi.org/", "").replace("http://doi.org/", "")
    return needle.lower() in haystack
