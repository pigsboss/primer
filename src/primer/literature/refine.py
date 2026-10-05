# -*- coding: utf-8 -*-
"""LLM 精析（路径 3）：把启发式抽出的表格行交给模型重抽字段。

定位与边界（与主笔共识）：

* 只做**字段重抽**（title／authors／year／type／venue／doi），不碰编号、分类、
  项目等；**DOI 必须逐字出现在条目原文里**，否则丢弃——防幻觉；
* 输入按块发送（默认 :data:`CHUNK_SIZE` 条），块失败只影响该块（行保持原值）；
* 结果仍走"表格行 → 列映射 → 提交"的既有流程，用户在回路里复核。

本模块不依赖 ``primer.llm``：``chat`` 是 ``(system, user) -> str`` 的注入点——
服务端接真实客户端，测试接假客户端（测试不碰网络）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence

from .importers import SAMPLE_ROWS, SourceData, _short, _stringify, _suggest, split_authors

__all__ = ["CHUNK_SIZE", "RefineReport", "refine_source", "suspicious_rows"]

CHUNK_SIZE = 25

#: 目标字段 → 新建列时用的列名（新值没有现成列可用时）。
_TARGET_COLUMNS = {
    "title": "标题",
    "authors": "作者",
    "editor": "编者",
    "translator": "译者",
    "year": "年份",
    "type": "类型",
    "venue": "出处",
    "volume": "卷",
    "number": "期号",
    "pages": "页码",
    "eid": "文章号",
    "publisher": "出版者",
    "location": "出版地",
    "institution": "机构",
    "organization": "主办",
    "series": "丛书",
    "edition": "版次",
    "isbn": "ISBN",
    "issn": "ISSN",
    "url": "链接",
    "eprint": "arXiv",
    "eventtitle": "会议",
    "eventdate": "会期",
    "keywords": "关键词",
    "doi": "DOI",
}
_FIELDS = tuple(_TARGET_COLUMNS)

_DOI_PREFIX_RE = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", re.IGNORECASE)
_YEAR_RE = re.compile(r"(?<!\d)(1[4-9]\d{2}|20\d{2})(?!\d)")

SYSTEM_PROMPT = """你是文献题录的字段抽取器。输入是若干条来源各异的题录，每条以 "i=<序号>:" 开头。
对每一条输出一个 JSON 对象，除序号 i 外含以下字段（无法确定的给 null）：
{"i": <序号>, "title": <字符串>, "authors": [<字符串>, ...], "year": <四位整数或 null>,
 "venue": <字符串或 null>, "type": <字符串>, "doi": <字符串或 null>,
 "editor": [<字符串>, ...] 或 null, "translator": [<字符串>, ...] 或 null,
 "volume": <字符串>, "number": <字符串>, "pages": <字符串>, "eid": <字符串>,
 "publisher": <字符串>, "location": <字符串>, "institution": <字符串>,
 "organization": <字符串>, "series": <字符串>, "edition": <字符串>,
 "isbn": <字符串>, "issn": <字符串>, "url": <字符串>, "eprint": <字符串>,
 "eventtitle": <字符串>, "eventdate": <字符串>, "keywords": <字符串>}
规则：
- 只输出一个 JSON 数组，包含输入里的每一条；不要解释，不要 markdown 代码围栏；
- title：完整标题，去掉作者名、出处与"［原文：…］"之类的标注；
- authors／editor／translator：按原文顺序保留写法；机构名可作为作者；没有就给 []；
- year：出版年份（四位整数）；无法确定给 null；
- venue：期刊名／文集名等"容器"，不含年份；type：在 journal-article / conference-paper /
  book-chapter / standard / report / preprint / book / thesis / patent / other 中择一；
- volume 卷；number 期号或编号；pages 页码；publisher 出版者；location 出版地；
  isbn／issn；url 链接；eprint arXiv 编号；keywords 关键词（分号分隔）；其余同名字段同理；
- doi：只能逐字复制原文中出现的 DOI；原文没有或拿不准就给 null——绝不猜测、绝不补全。
"""


@dataclass
class RefineReport:
    """一次精析的结果统计：更新行数、保留原值行数、失败块数。"""

    refined: int = 0
    failed: int = 0
    chunks_failed: int = 0


def suspicious_rows(
    rows: list[dict[str, Any]], columns: list[str], suggested: dict[str, str]
) -> list[int]:
    """启发式判疑：字段缺失、标题疑似没切开、年份不在合理范围。"""
    reverse = _reverse_map(columns, suggested)
    flagged: list[int] = []
    for index, row in enumerate(rows):
        title = _cell(row, reverse.get("title"))
        authors = _cell(row, reverse.get("authors"))
        venue = _cell(row, reverse.get("venue"))
        year = _cell(row, reverse.get("year"))
        if (
            not title
            or len(title) < 4
            or (authors and title == authors)
            or not authors
            or not venue
            or not _year_ok(year)
        ):
            flagged.append(index)
    return flagged


def refine_source(
    source: SourceData,
    indices: Sequence[int],
    chat: Callable[[str, str], str],
    *,
    chunk_size: int = CHUNK_SIZE,
    on_progress: Optional[Callable[[int], None]] = None,
) -> RefineReport:
    """对 ``indices`` 指定的行做 LLM 重抽；原地更新行，返回统计。

    块级失败（调用报错或回信不可解析）只记入 ``chunks_failed`` / ``failed``，
    对应行保持原值并继续后续块；``on_progress`` 收到的是已处理行数。
    **全部块都失败**时抛回第一个原始异常——调用方（服务端任务）据此把整个
    任务标成 failed，让"缺密钥／端点不可达"这类系统性错误直接可见。
    """
    report = RefineReport()
    reverse = _reverse_map(source.columns, source.suggested)
    order = [index for index in indices if 0 <= index < len(source.rows)]
    done = 0
    first_error: Optional[BaseException] = None
    for start in range(0, len(order), chunk_size):
        chunk = order[start:start + chunk_size]
        texts = {index: _raw_text(source, index) for index in chunk}
        try:
            reply = chat(SYSTEM_PROMPT, _build_user(chunk, texts))
            items = _parse_reply(reply)
        except Exception as exc:
            if first_error is None:
                first_error = exc
            report.chunks_failed += 1
            report.failed += len(chunk)
            done += len(chunk)
            if on_progress is not None:
                on_progress(done)
            continue
        by_index = {item["i"]: item for item in items}
        for index in chunk:
            item = by_index.get(index)
            if item is None or not _apply_row(source, reverse, index, item, texts[index]):
                report.failed += 1
            else:
                report.refined += 1
        done += len(chunk)
        if on_progress is not None:
            on_progress(done)
    if report.refined:
        _refresh(source)
    if report.refined == 0 and report.failed > 0 and first_error is not None:
        raise first_error
    return report


# ---------------------------------------------------------------- 内部


def _reverse_map(columns: list[str], suggested: dict[str, str]) -> dict[str, str]:
    """目标字段 → 现有列（按列顺序取第一个映射到该字段的列）。"""
    reverse: dict[str, str] = {}
    for column in columns:
        target = suggested.get(column, "")
        if target in _FIELDS and target not in reverse:
            reverse[target] = column
    return reverse


def _cell(row: dict[str, Any], column: Optional[str]) -> str:
    if column is None:
        return ""
    return _stringify(row.get(column)).strip()


def _year_ok(text: str) -> bool:
    return bool(text) and _year_value(text) is not None


def _year_value(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 1400 <= value <= 2100 else None
    match = _YEAR_RE.search(str(value))
    return int(match.group(1)) if match else None


def _raw_text(source: SourceData, index: int) -> str:
    """给模型看的条目原文：优先用解析时保留的原文，否则把行渲染成 "列: 值" 行。"""
    if index < len(source.raws) and source.raws[index]:
        return source.raws[index]
    row = source.rows[index]
    parts = []
    for column in source.columns:
        text = _stringify(row.get(column)).strip()
        if text:
            parts.append(f"{column}: {text}")
    return " ｜ ".join(parts)


def _build_user(chunk: Sequence[int], texts: dict[int, str]) -> str:
    return "\n".join(f"i={index}: {texts[index]}" for index in chunk)


def _parse_reply(reply: str) -> list[dict[str, Any]]:
    text = reply.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[A-Za-z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end <= start:
        raise ValueError("reply has no JSON array")
    payload = json.loads(text[start:end + 1])
    if not isinstance(payload, list):
        raise ValueError("reply JSON is not an array")
    items: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        index = item.get("i")
        if isinstance(index, bool):
            continue
        if isinstance(index, str) and index.lstrip("-").isdigit():
            index = int(index)
        if isinstance(index, int):
            item = dict(item)
            item["i"] = index
            items.append(item)
    return items


def _clean_field(field: str, value: Any, raw: str) -> Optional[Any]:
    if value is None:
        return None
    if field == "title":
        text = str(value).strip()
        return text if len(text) >= 2 else None
    if field in ("authors", "editor", "translator"):
        people = split_authors(value)
        return "; ".join(people) if people else None
    if field == "year":
        return _year_value(value)
    if field == "type":
        text = str(value).strip().lower()
        return text or None
    if field == "doi":
        text = _DOI_PREFIX_RE.sub("", str(value).strip()).rstrip(".,;")
        if not text:
            return None
        return text if _identifier_in_text(text, raw) else None
    # 其余文本字段（venue／volume／number／pages／publisher／location… 一律按原样收下）
    text = str(value).strip()
    return text or None


def _identifier_in_text(needle: str, raw: str) -> bool:
    """DOI 只认原文出现过的字面量（比对时忽略 doi.org 前缀与大小写）。"""
    haystack = raw.lower().replace("https://doi.org/", "").replace("http://doi.org/", "")
    return needle.lower() in haystack


def _apply_row(
    source: SourceData, reverse: dict[str, str], index: int, item: dict[str, Any], raw: str
) -> bool:
    row = source.rows[index]
    applied = False
    for field in _FIELDS:
        if field not in item:
            continue
        value = _clean_field(field, item.get(field), raw)
        if value is None:
            continue
        column = reverse.get(field)
        if column is None:
            column = _TARGET_COLUMNS[field]
            _ensure_column(source, column)
            reverse[field] = column
        row[column] = value
        applied = True
    return applied


def _ensure_column(source: SourceData, column: str) -> None:
    if column in source.columns:
        return
    source.columns.append(column)
    for row in source.rows:
        row.setdefault(column, "")


def _refresh(source: SourceData) -> None:
    """重算列、样本与建议映射（精析可能新建了列）。"""
    columns: list[str] = []
    seen: set[str] = set()
    for row in source.rows:
        for key in row.keys():
            name = str(key)
            if name not in seen:
                seen.add(name)
                columns.append(name)
    source.columns = columns
    source.sample = {
        column: [_short(_stringify(row.get(column))) for row in source.rows[:SAMPLE_ROWS]]
        for column in columns
    }
    source.suggested = _suggest(columns)
