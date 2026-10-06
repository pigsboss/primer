# -*- coding: utf-8 -*-
"""从 md 文献清单恢复项目归属：解析标题层级，把记录匹配回条目行。

背景：文献清单（《行星探测三十年…参考文献》总库及补充／基础清单）以 md 维护，
条目行上方最近的标题（``#``～``####``）就是条目从属的项目／章节；清单被逐行导入
文献库时标题信息不在行文本里，因而丢失。本模块做**纯计算**恢复：

* :func:`parse_md_entries`：逐行扫描，记下「最近标题 → 条目行文本」；
* :func:`clean_heading`：标题 → 项目名（去括注／＝装饰／前导编号，去尾部「专题」）；
* :func:`resolve_md_files`：把「文件或目录」清单解析为 md 文件列表（目录取其下全部 ``*.md``）；
* :func:`propose_projects_from_md`：把库内记录与条目行匹配（规范化文本；精确优先，
  40 字以上前缀一致时回退），按项目分组返回（只计算、不改库）。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Sequence

__all__ = [
    "clean_heading",
    "normalize_line",
    "parse_md_entries",
    "propose_projects_from_md",
    "resolve_md_files",
]

_NUMBER = re.compile(r"^\[\d+[a-z]?\]\s*")
_ORIGIN = re.compile(r"［[^］]*］")
_HEADING = re.compile(r"^#{1,4}\s+(.*)$")
_PARENS = re.compile(r"（[^）]*）|\([^)]*\)")
_DECOR = re.compile(r"[＝=]+")
_LEAD = re.compile(r"^[A-Za-z][A-Za-z0-9+]*\s+")
_PREFIX_LEN = 40


def normalize_line(text: Any) -> str:
    """条目行／备注的规范化文本：去 ``[编号]`` 与 ``［原文：…］``、压缩空白。"""
    value = _ORIGIN.sub("", str(text or "").strip())
    value = _NUMBER.sub("", value)
    return re.sub(r"\s+", " ", value).strip()


def clean_heading(heading: Any) -> str:
    """标题 → 项目名：去括注、＝装饰与前导编号（``M ``／``B1 ``），去尾部「专题」。"""
    text = _PARENS.sub(" ", str(heading or ""))
    text = _DECOR.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = _LEAD.sub("", text)
    if text.endswith("专题"):
        text = text[:-2].strip(" 　·—")
    return text


def resolve_md_files(paths: str | Path | Sequence[str | Path]) -> list[Path]:
    """把「文件或目录」清单解析为 md 文件列表：目录取其下全部 ``*.md``（按名排序）。"""
    if isinstance(paths, (str, Path)):
        items: list[Any] = [paths]
    else:
        items = list(paths)
    files: list[Path] = []
    for item in items:
        if not isinstance(item, (str, Path)) or not str(item).strip():
            raise ValueError("md list path must be a non-empty string")
        target = Path(str(item).strip()).expanduser()
        if target.is_dir():
            files.extend(sorted(target.glob("*.md")))
        elif target.is_file():
            files.append(target)
        else:
            raise ValueError(f"md list not found: {target}")
    if not files:
        raise ValueError("no .md files found in the selection")
    return files


def parse_md_entries(paths: str | Path | Sequence[str | Path]) -> list[dict[str, str]]:
    """扫描一份或多份清单 md：返回 ``[{heading, project, key}]``（跳空行／标题行／非条目行）。

    条目行＝以 ``[编号]``（如 ``[558]``、``[572a]``）开头的行；``key`` 为规范化文本；
    标题层级按文件各自独立（跨文件不继承）。
    """
    entries: list[dict[str, str]] = []
    for path in resolve_md_files(paths):
        heading = ""
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line:
                continue
            match = _HEADING.match(line)
            if match:
                heading = match.group(1).strip()
                continue
            if not _NUMBER.match(line):
                continue
            key = normalize_line(line)
            project = clean_heading(heading)
            if key and project:
                entries.append({"heading": heading, "project": project, "key": key})
    return entries


def propose_projects_from_md(
    records: Sequence[Any],
    paths: str | Path | Sequence[str | Path],
    *,
    unmatched_limit: int = 100,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, str]]]:
    """把记录匹配回清单条目并按项目分组（纯计算，不改库）。

    匹配：规范化文本精确相等优先；否则找 40 字以上前缀一致、长度差最小的条目
    （同一行可能出现在多个标题下，全部计入）。``paths`` 可给一份／多份 md 文件或
    目录（目录取其下全部 ``*.md``），条目合并后统一匹配。返回 ``(stats, groups, unmatched)``：

    * ``stats``：``files``（文件列表）／``entries``／``projects``／``matched``／
      ``exact``／``prefix``／``unmatched``；
    * ``groups``：``[{"project", "heading", "count", "uuids"}]``，按条数降序；
    * ``unmatched``：前 ``unmatched_limit`` 条未命中记录 ``{uuid, title, note}``。
    """
    files = resolve_md_files(paths)
    entries = parse_md_entries(files)
    if not entries:
        raise ValueError("no entries found ([编号] rows) in the md list(s)")
    exact: dict[str, list[str]] = {}
    for entry in entries:
        names = exact.setdefault(entry["key"], [])
        if entry["project"] not in names:
            names.append(entry["project"])
    headings: dict[str, str] = {}
    for entry in entries:
        headings.setdefault(entry["project"], entry["heading"])

    def match(text: str) -> tuple[list[str], str]:
        if text in exact:
            return exact[text], "exact"
        needle = text[:_PREFIX_LEN]
        if len(text) < _PREFIX_LEN:
            return [], ""
        best: list[str] = []
        best_diff: int | None = None
        for entry in entries:
            key = entry["key"]
            if min(len(text), len(key)) < _PREFIX_LEN:
                continue
            if not (text.startswith(key[:_PREFIX_LEN]) or key.startswith(needle)):
                continue
            diff = abs(len(key) - len(text))
            if best_diff is None or diff < best_diff:
                best, best_diff = [entry["project"]], diff
            elif diff == best_diff and entry["project"] not in best:
                best.append(entry["project"])
        return best, ("prefix" if best else "")

    stats = {
        "files": [str(path) for path in files],
        "entries": len(entries),
        "exact": 0,
        "prefix": 0,
        "matched": 0,
        "unmatched": 0,
        "projects": 0,
    }
    grouped: dict[str, list[str]] = {}
    unmatched: list[dict[str, str]] = []
    for record in records:
        note = str(getattr(record, "notes", "") or "").strip()
        text = normalize_line(note[len("原始记录：") :] if note.startswith("原始记录：") else note)
        found, how = match(text)
        if not found:
            stats["unmatched"] += 1
            if len(unmatched) < unmatched_limit:
                unmatched.append(
                    {"uuid": record.uuid, "title": record.title or "", "note": text[:120]}
                )
            continue
        stats["matched"] += 1
        stats[how] += 1
        for name in found:
            grouped.setdefault(name, []).append(record.uuid)
    groups = [
        {
            "project": name,
            "heading": headings.get(name, ""),
            "count": len(uuids),
            "uuids": uuids,
        }
        for name, uuids in grouped.items()
    ]
    groups.sort(key=lambda item: (-item["count"], item["project"]))
    stats["projects"] = len(groups)
    return stats, groups, unmatched
