# -*- coding: utf-8 -*-
"""审计产物导出：index.json / index.csv / audit.md，以及图书馆索取清单（CSV + MD）。

图书馆清单沿用既有 schema
``no,priority,source,topic,title,authors,year,venue,url,status,note``，
只收录判定为 ``missing`` 的条目；文件名带 ``.new`` 后缀，便于与旧清单逐行 diff。
优先级按 ``--body`` 传入的正文引用编号确定：被正文引用过者为 ``P1``，其余 ``P2``；
未传 ``--body`` 时全部 ``P1``，并在 markdown 里写明这一点。

清单有两个来源，合在一张表里：

* ``参考文献库[n]``——参考文献表里判定为 missing 的条目，编号链回正文引用；
* 调研日志（:mod:`primer.references.survey`，``--oa-log``）里 ``fail``/``no-link``
  的条目，一律 ``P2``，``source`` 写成 ``文献调研清单[主题]`` 以区分来源。

去重只发生在调研日志一侧：标题键与任何一条参考文献库行相同的日志行被合并掉，反之
**绝不因为日志里有同名条目就丢掉一条编号参考文献行**——编号行能追到正文，是更可靠的
索取理由。标题键沿用旧脚本的 :func:`primer.references.survey.title_key`。

每行的 ``note`` 末尾附 ``confidence: high|medium|low``，见 :func:`library_confidence`；
confidence 折进 ``note`` 而不是另开一列，是为了让 CSV 表头与既有
``参考资料/图书馆文献获取清单.csv`` 逐字节一致，既不破坏任何按列名读取的旧脚本，
也不影响 Excel 阅读。

判定为 ``public_web`` 的条目（官网/NTRS/白宫/CBO 等公开来源）不进 CSV——CSV 的每一行
都是要请图书馆去找的东西；它们只在 markdown 的独立小节里列出备查。

产物里的路径（``source``、``evidence``、本地文件路径、正文与文本目录）一律相对
``AuditResult.project_root`` 书写，产物随工程搬移后仍然可读。
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Optional, Sequence

from .audit import (
    CONFIDENCE_HIGH,
    VERDICT_CONFLICT,
    VERDICT_LOCAL,
    VERDICT_LOCAL_UNREGISTERED,
    VERDICT_MISSING,
    VERDICT_PUBLIC_WEB,
    VERDICTS,
    AuditResult,
    EntryAudit,
)
from ..literature.paths import relative_to_root
from .entries import ListValidation
from .ledger import LocalFile
from .survey import SURVEY_SOURCE, SurveyItem, title_key

BODY_CITATION = re.compile(r"\[(\d+)(?:\s*[–—-]\s*(\d+))?\]")
YEAR = re.compile(r"(1[6-9]\d{2}|20\d{2})")

REFERENCE_SOURCE = "参考文献库"

# 清单行的元数据置信度下限：没有年份也没有 URL 的行，馆员无从检索。
ROW_CONFIDENCE_LOW = "low"


LIBRARY_COLUMNS = (
    "no",
    "priority",
    "source",
    "topic",
    "title",
    "authors",
    "year",
    "venue",
    "url",
    "status",
    "note",
)

INDEX_COLUMNS = (
    "number",
    "class_letter",
    "class_title",
    "authors",
    "title",
    "venue_year",
    "arxiv",
    "doi",
    "tag_kind",
    "verdict",
    "confidence",
    "source",
    "line",
    "note",
    "evidence",
)

_VERDICT_LABELS = {
    VERDICT_LOCAL: "local (copy on disk)",
    VERDICT_LOCAL_UNREGISTERED: "local_unregistered (file exists, bibliography says otherwise)",
    VERDICT_PUBLIC_WEB: "public_web",
    VERDICT_MISSING: "missing",
    VERDICT_CONFLICT: "conflict",
}


def collect_body_citations(
    paths: Sequence[Path], *, project_root: Optional[Path] = None
) -> tuple[set[int], list[str]]:
    """从正文 markdown 收集引用编号，展开 ``[12–15]`` 这类范围。

    返回的来源文件清单相对 ``project_root`` 记录，会原样进 ``index.json``。
    """
    cited: set[int] = set()
    used: list[str] = []
    for path in paths:
        text = Path(path).read_text(encoding="utf-8")
        for match in BODY_CITATION.finditer(text):
            start = int(match.group(1))
            end = int(match.group(2)) if match.group(2) else start
            if end < start:
                start, end = end, start
            cited.update(range(start, end + 1))
        used.append(relative_to_root(path, project_root))
    return cited, used


def _entry_payload(audit: EntryAudit) -> dict:
    entry = audit.entry
    return {
        "number": entry.number,
        "class_letter": entry.class_letter,
        "class_title": entry.class_title,
        "authors": entry.authors,
        "title": entry.title,
        "venue_year": entry.venue_year,
        "arxiv": entry.arxiv,
        "doi": entry.doi,
        "tag": entry.tag,
        "tag_kind": entry.tag_kind,
        "raw": entry.raw,
        "verdict": audit.verdict,
        "confidence": audit.confidence,
        "note": audit.note,
        "evidence": list(audit.evidence),
        "source": entry.source,
        "line": entry.line,
    }


def index_payload(result: AuditResult) -> dict:
    """``index.json`` 的内容；其中所有路径都相对工程根。"""
    problems = result.problems
    project_root = result.project_root
    return {
        "summary": {
            "entries": len(result.audits),
            "counts": result.counts(),
            "by_class": result.by_class(),
        },
        "entries": [_entry_payload(audit) for audit in result.audits],
        "validation": [_validation_payload(item) for item in result.validations],
        "problems": {
            "unnumbered_files": [
                {
                    **_file_payload(item, project_root),
                    "resolution": problems.unnumbered_notes.get(item.path.name, ""),
                }
                for item in problems.unnumbered_files
            ],
            "unregistered_files": [
                _file_payload(item, project_root) for item in problems.unregistered_files
            ],
            "conflicts": list(problems.conflicts),
            "rescue_mismatches": list(problems.rescue_mismatches),
            "stale_rescue_paths": list(problems.stale_rescue_paths),
            "claims_without_file": list(problems.claims_without_file),
            "text_missing": [_file_payload(item, project_root) for item in problems.text_missing],
            "text_root": (
                relative_to_root(problems.text_root, project_root)
                if problems.text_root is not None
                else None
            ),
        },
        "body": {
            "sources": list(result.body_sources),
            "cited_numbers": sorted(result.body_citations),
        },
    }


def _file_payload(local: LocalFile, project_root: Optional[Path] = None) -> dict:
    return {
        "path": relative_to_root(local.path, project_root),
        "name": local.path.name,
        "ref": local.ref,
        "cls": local.cls,
        "arxiv": local.arxiv,
    }


def _validation_payload(item: ListValidation) -> dict:
    return {
        "source": item.source,
        "declared_total": item.declared_total,
        "actual_count": item.actual_count,
        "total_delta": item.total_delta,
        "declared_letters": list(item.declared_letters),
        "present_letters": list(item.present_letters),
        "missing_letters": list(item.missing_letters),
        "undeclared_letters": list(item.undeclared_letters),
        "duplicates": list(item.duplicates),
        "gaps": list(item.gaps),
    }


def write_index_json(path: Path, result: AuditResult) -> None:
    """写出 ``index.json``。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(index_payload(result), ensure_ascii=False, indent=2), encoding="utf-8"
    )


def index_rows(result: AuditResult) -> list[dict[str, str]]:
    """``index.csv`` 的行（每行一条条目，证据用 `` | `` 连接）。"""
    rows: list[dict[str, str]] = []
    for audit in result.audits:
        entry = audit.entry
        rows.append(
            {
                "number": str(entry.number),
                "class_letter": entry.class_letter or "",
                "class_title": entry.class_title or "",
                "authors": entry.authors,
                "title": entry.title,
                "venue_year": entry.venue_year,
                "arxiv": entry.arxiv or "",
                "doi": entry.doi or "",
                "tag_kind": entry.tag_kind,
                "verdict": audit.verdict,
                "confidence": audit.confidence,
                "source": entry.source,
                "line": str(entry.line),
                "note": audit.note,
                "evidence": " | ".join(audit.evidence),
            }
        )
    return rows


def write_index_csv(path: Path, result: AuditResult) -> None:
    """写出 ``index.csv``（utf-8-sig，便于 Excel 打开）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(INDEX_COLUMNS))
        writer.writeheader()
        writer.writerows(index_rows(result))


def library_rows(
    result: AuditResult, *, survey: Sequence[SurveyItem] = ()
) -> list[dict[str, str]]:
    """图书馆索取清单的行：``missing`` 条目 + 调研日志里没拿到的条目。

    按 ``P1/P2 → 主题 → 编号`` 排序，与既有清单"优先级优先"的习惯一致；``no`` 在排序后
    重新编号。``survey`` 里与任一条参考文献库行同标题键的会被合并掉，其余进表并记
    ``P2``。
    """
    cited = result.body_citations
    all_p1 = not result.body_sources
    builder: list[tuple[tuple, dict[str, str]]] = []
    seen: set[str] = set()

    for audit in result.missing():
        entry = audit.entry
        priority = "P1" if all_p1 or entry.number in cited else "P2"
        topic = " ".join(part for part in (entry.class_letter, entry.class_title) if part)
        title = entry.title or entry.raw
        year = _year_of(entry.venue_year or entry.raw)
        url = f"https://arxiv.org/abs/{entry.arxiv}" if entry.arxiv else ""
        row = {
            "no": "",
            "priority": priority,
            "source": f"{REFERENCE_SOURCE}[{entry.number}]",
            "topic": topic,
            "title": title,
            "authors": entry.authors,
            "year": year,
            "venue": entry.venue_year,
            "url": url,
            "status": "pending",
            "note": library_note(
                audit,
                cited=entry.number in cited if not all_p1 else None,
                confidence=library_confidence(audit.confidence, year=year, url=url),
            ),
        }
        seen.add(title_key(title))
        builder.append(((priority, topic, 0, entry.number), row))

    for position, item in enumerate(survey):
        key = title_key(item.title)
        if key in seen:
            continue
        seen.add(key)
        url = item.url if _is_url(item.url) else ""
        row = {
            "no": "",
            "priority": "P2",
            "source": item.source,
            "topic": item.topic,
            "title": item.title,
            "authors": "",
            "year": item.year,
            "venue": "",
            "url": url,
            "status": "pending",
            "note": survey_note(
                item, confidence=library_confidence(CONFIDENCE_HIGH, year=item.year, url=url)
            ),
        }
        builder.append((("P2", item.topic, 1, position), row))

    builder.sort(key=lambda pair: pair[0])
    rows: list[dict[str, str]] = []
    for index, (_, row) in enumerate(builder, start=1):
        row["no"] = str(index)
        rows.append(row)
    return rows


def _is_url(value: str) -> bool:
    token = (value or "").strip()
    return token.startswith(("http://", "https://"))


def library_confidence(verdict_confidence: str, *, year: str, url: str) -> str:
    """清单行的置信度：审计判据的把握程度，元数据太薄时降为 ``low``。

    ``high``——判据硬：参考文献库行没有任何本地证据指向该条，或日志行明确写了
    下载失败/无链接；
    ``medium``——判据软：判定为 missing 但有本地文件或尾标自述指向它，需人工再确认；
    ``low``——这一行既无年份也无 URL，馆员据此检索不到，先补全题录再说。
    """
    if not (year or "").strip() and not (url or "").strip():
        return ROW_CONFIDENCE_LOW
    return verdict_confidence


def library_note(audit: EntryAudit, *, cited: Optional[bool], confidence: str) -> str:
    """给图书馆的备注：尾标说法、是否被正文引用、置信度与已下载文件提示。"""
    entry = audit.entry
    parts: list[str] = []
    if entry.tag:
        parts.append(f"bibliography tag: {entry.tag}")
    if cited is not None:
        parts.append("cited in a supplied body markdown" if cited else "not cited in any body markdown")
    if "could not be accepted" in audit.note:
        parts.append("a downloaded PDF exists but was rejected (see audit.md)")
    parts.append(f"confidence: {confidence}")
    return "; ".join(parts)


def survey_note(item: SurveyItem, *, confidence: str) -> str:
    """调研日志来源行的备注：写明是日志第几行、原始状态，便于回查。"""
    return (
        f"survey log row {item.line} status={item.status or '-'}: "
        "the automated open-access attempt did not obtain a copy; "
        f"confidence: {confidence}"
    )



def _year_of(text: str) -> str:
    matches = YEAR.findall(text or "")
    return matches[-1] if matches else ""


def write_library_csv(path: Path, rows: Sequence[dict[str, str]]) -> None:
    """写出图书馆清单 CSV（utf-8-sig，schema 与既有文件一致）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(LIBRARY_COLUMNS))
        writer.writeheader()
        writer.writerows(rows)


def source_counts(rows: Sequence[dict[str, str]]) -> dict[str, int]:
    """按 ``source`` 的方括号前缀计数，用于报告清单构成。"""
    counts: dict[str, int] = {}
    for row in rows:
        family = row["source"].split("[", 1)[0]
        counts[family] = counts.get(family, 0) + 1
    return counts


def _citation_line(audit: EntryAudit, index: int) -> str:
    entry = audit.entry
    line = f"{index}. **{entry.title or entry.raw}**"
    meta = [value for value in (entry.authors, entry.venue_year) if value]
    if meta:
        line += " — " + "; ".join(meta)
    if entry.arxiv:
        line += f" — https://arxiv.org/abs/{entry.arxiv}"
    if entry.tag:
        line += f"（bibliography tag: {entry.tag}）"
    return line


def render_library_md(
    rows: Sequence[dict[str, str]], result: AuditResult, *, survey: Sequence[SurveyItem] = ()
) -> str:
    """图书馆清单的阅读版。

    ``public_web`` 条目在末尾单列一节（它们网上公开、不必向图书馆要，因此也不进 CSV）。
    """
    all_p1 = not result.body_sources
    p1 = sum(1 for row in rows if row["priority"] == "P1")
    counts = source_counts(rows)
    reference_rows = counts.get(REFERENCE_SOURCE, 0)
    survey_rows = counts.get(SURVEY_SOURCE, 0)
    lines = [
        "# 图书馆文献获取清单（审计版）",
        "",
        f"> 共 {len(rows)} 条（P1={p1}，P2={len(rows) - p1}）",
        f"> 构成：参考文献库 missing {reference_rows} 条；调研日志合并 {survey_rows} 条"
        + (
            f"（日志里 {len(survey)} 条 fail/no-link，其中 {len(survey) - survey_rows} 条"
            "与参考文献库同标题，已合并）"
            if survey
            else ""
        ),
        "> priority："
        + (
            "未提供 --body，全部按 P1 处理"
            if all_p1
            else "被 --body 正文引用者为 P1，其余 P2"
        ),
        "> note 末尾的 confidence 是「这一行确实没有本地原文」的把握：high/medium/low，"
        "low 表示题录缺年份与 URL，馆员无从检索",
        "> 生成工具：python -m primer.references list",
        "",
    ]
    current = None
    for row in rows:
        group = f"{row['priority']}｜{row['topic'] or row['source']}"
        if group != current:
            current = group
            lines += ["", f"## {group}", ""]
        citation = f"{row['no']}. **{row['title']}**"
        meta = [row["authors"]]
        meta.append(row["venue"] or row["year"])
        meta = [value for value in meta if value]
        if meta:
            citation += " — " + "; ".join(meta)
        if row["url"]:
            citation += f" — {row['url']}"
        if row["note"]:
            citation += f"（{row['note']}）"
        lines.append(citation)

    public = [audit for audit in result.audits if audit.verdict == VERDICT_PUBLIC_WEB]
    if public:
        lines += [
            "",
            f"## 无需图书馆获取（{len(public)} 条，网上公开，仅供备查）",
            "",
            "> 这些条目的文献表尾标注明官网/NTRS/白宫/CBO 等公开来源，可自行下载，"
            "不必向图书馆索取；因此它们不在上面的编号里，也不进 CSV。",
            "",
        ]
        lines += [_citation_line(audit, index) for index, audit in enumerate(public, start=1)]
    return "\n".join(lines) + "\n"


def render_audit_md(result: AuditResult) -> str:
    """审计报告：结论分布、问题清单与清单自洽性校验。"""
    counts = result.counts()
    lines = ["# Reference audit", "", "## Summary", ""]
    lines.append(f"- entries parsed: {len(result.audits)} from {len(result.lists)} list(s)")
    for verdict in VERDICTS:
        lines.append(f"- {_VERDICT_LABELS[verdict]}: {counts[verdict]}")
    library = result.missing()
    p1 = sum(1 for audit in library if not result.body_sources or audit.number in result.body_citations)
    lines.append(f"- library list rows (missing only): {len(library)} (P1={p1}, P2={len(library) - p1})")
    lines += ["", "## By class", "", "| class | " + " | ".join(VERDICTS) + " | total |"]
    lines.append("| --- | " + " | ".join("---" for _ in VERDICTS) + " | --- |")
    for letter, row in sorted(result.by_class().items()):
        total = sum(row.values())
        lines.append(
            f"| {letter} | " + " | ".join(str(row[verdict]) for verdict in VERDICTS) + f" | {total} |"
        )

    lines += ["", "## Entry list validation", ""]
    for item in result.validations:
        declared = "none declared" if item.declared_total is None else str(item.declared_total)
        lines.append(f"### {item.source}")
        lines.append("")
        delta = "" if item.total_delta is None else f" ({item.total_delta:+d})"
        lines.append(f"- declared total: {declared} | actual entries: {item.actual_count}{delta}")
        lines.append(f"- declared sections: {', '.join(item.declared_letters) or '-'}")
        lines.append(f"- sections present: {', '.join(item.present_letters) or '-'}")
        lines.append(
            f"- sections declared but absent: {', '.join(item.missing_letters) or 'none'}"
        )
        lines.append(
            f"- sections present but undeclared: {', '.join(item.undeclared_letters) or 'none'}"
        )
        lines.append(f"- duplicate numbers: {', '.join(map(str, item.duplicates)) or 'none'}")
        lines.append(f"- gaps in numbering: {', '.join(map(str, item.gaps)) or 'none'}")

    problems = result.problems
    lines += ["", "## Problems", ""]
    lines.append(
        f"### Local files without a reference number in the filename "
        f"({len(problems.unnumbered_files)}; {len(problems.unregistered_files)} of them "
        f"are referenced by nothing at all)"
    )
    lines.append("")
    for local in problems.unnumbered_files:
        note = problems.unnumbered_notes.get(local.path.name, "")
        lines.append(
            f"- `{local.path.name}` — arXiv:{local.arxiv or '-'} — "
            f"{note or 'unresolved'} — {relative_to_root(local.path, result.project_root)}"
        )
    if not problems.unnumbered_files:
        lines.append("- none")

    lines += ["", f"### Downloaded but matching no entry ({len(problems.unregistered_files)})", ""]
    for local in problems.unregistered_files:
        lines.append(
            f"- `{local.path.name}` — arXiv:{local.arxiv or '-'} — no entry claims this ref or arXiv id"
        )
    if not problems.unregistered_files:
        lines.append("- none")

    lines += ["", f"### Conflicts ({len(problems.conflicts)})", ""]
    for item in problems.conflicts:
        lines.append(f"- {item}")
    if not problems.conflicts:
        lines.append("- none")

    rescue_problems = list(dict.fromkeys([*problems.rescue_mismatches, *problems.stale_rescue_paths]))
    lines += ["", f"### Rescue record problems ({len(rescue_problems)})", ""]
    for item in rescue_problems:
        lines.append(f"- {item}")
    if not rescue_problems:
        lines.append("- none")

    lines += [
        "",
        f"### Entries claiming a local copy with no file ({len(problems.claims_without_file)})",
        "",
    ]
    for item in problems.claims_without_file:
        lines.append(f"- {item}")
    if not problems.claims_without_file:
        lines.append("- none")

    lines += ["", "### PDFs without extracted text", ""]
    if problems.text_root is None:
        lines.append("- not checked (no --text-root given): unverified, not asserted")
    else:
        lines.append(
            f"- {len(problems.text_missing)} of the local PDFs have no .txt under "
            f"{relative_to_root(problems.text_root, result.project_root)}"
        )
        for local in problems.text_missing:
            lines.append(f"- `{local.path.name}`")
    return "\n".join(lines) + "\n"


def write_audit_md(path: Path, result: AuditResult) -> None:
    """写出 ``audit.md``。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_audit_md(result), encoding="utf-8")


def write_library_md(
    path: Path,
    rows: Sequence[dict[str, str]],
    result: AuditResult,
    *,
    survey: Sequence[SurveyItem] = (),
) -> None:
    """写出图书馆清单的 markdown 版。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_library_md(rows, result, survey=survey), encoding="utf-8")
