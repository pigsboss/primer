# -*- coding: utf-8 -*-
"""本地可得性证据台账：把下载账本、救援缓存、本地 PDF 与文献表自述放到同一张索引上。

每条证据都带来源（哪个账本、哪一行、哪个文件），并按 **ref 号** 与 **规范化 arXiv 号**
双向索引，审计时两路都查、都引用，避免只认文件名或只认账本导致的误判。

``usable`` 表示"这条证据本身构成一份可用的本地原文"：只有「账本 ok 且 saved 存在」、
「救援缓存 rescued 且按 arXiv 解析到真实文件」、「本地 PDF 编号/arXiv 命中」才算；
文献表尾标自述"已存本地"不算（自述不是证据），只作为待核对的线索。

``detail`` 里的路径一律写成相对**工程根**的形式（见 ``project_root`` 参数），
因为这条字符串会原样进 ``index.json`` / ``index.csv`` / ``audit.md``。
"""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional, Sequence

from ..literature.catalog import build_catalog
from ..literature.paths import relative_to_root
from .entries import TAG_LOCAL, RefEntry

SOURCE_LOCAL_FILE = "local_file"
SOURCE_REFLIB_LOG = "reflib_log"
SOURCE_RESCUE_CACHE = "rescue_cache"
SOURCE_BIB_TAG = "bibliography_tag"
SOURCE_BIB_ARXIV = "bibliography_arxiv"

USABLE_SOURCES = frozenset({SOURCE_LOCAL_FILE, SOURCE_REFLIB_LOG, SOURCE_RESCUE_CACHE})

_STATUS_OK = "ok"
_STATUS_RESCUED = "rescued"
_NUMBERED_NAME = re.compile(r"^\d{3}_[A-Z]_")


@dataclass(frozen=True)
class Evidence:
    """一条本地可得性证据。``detail`` 必须写清具体来源（行号或路径）。"""

    source: str
    detail: str
    usable: bool
    ref: Optional[str] = None
    arxiv: Optional[str] = None
    path: Optional[Path] = None
    unnumbered: bool = False

    def describe(self) -> str:
        marker = "" if self.usable else " [not usable as a local copy]"
        return f"{self.source}: {self.detail}{marker}"


@dataclass
class LocalFile:
    """本地 PDF 及其编号信息；``ref`` 为空说明文件名里没有编号。"""

    path: Path
    ref: Optional[str]
    cls: Optional[str]
    arxiv: Optional[str] = None
    title: Optional[str] = None

    @property
    def unnumbered(self) -> bool:
        return self.ref is None


@dataclass
class Ledger:
    """证据索引。``by_ref`` / ``by_arxiv`` 的键均已规范化。"""

    files: list[LocalFile] = field(default_factory=list)
    by_ref: dict[str, list[Evidence]] = field(default_factory=dict)
    by_arxiv: dict[str, list[Evidence]] = field(default_factory=dict)

    def add(self, evidence: Evidence) -> None:
        if evidence.ref:
            self.by_ref.setdefault(evidence.ref, []).append(evidence)
        if evidence.arxiv:
            self.by_arxiv.setdefault(evidence.arxiv, []).append(evidence)

    def evidence_for(self, *, ref: Optional[str] = None, arxiv: Optional[str] = None) -> list[Evidence]:
        """按 ref 号与 arXiv 号取回证据，保持各自的出现顺序且不重复。"""
        found: list[Evidence] = []
        for key, table in ((ref, self.by_ref), (arxiv, self.by_arxiv)):
            if not key:
                continue
            for evidence in table.get(key, []):
                if evidence not in found:
                    found.append(evidence)
        return found

    def usable_local(self, *, ref: Optional[str] = None, arxiv: Optional[str] = None) -> list[Evidence]:
        """取回构成可用本地原文的证据。"""
        return [
            evidence
            for evidence in self.evidence_for(ref=ref, arxiv=arxiv)
            if evidence.usable and evidence.source in USABLE_SOURCES
        ]


def ref_key(value: Optional[str]) -> Optional[str]:
    """把 ref 号规范成不带前导零的十进制字符串；非数字返回 ``None``。"""
    if value is None:
        return None
    token = str(value).strip()
    return str(int(token)) if token.isdigit() else None


def _numbered_in_name(path: Path) -> bool:
    return bool(_NUMBERED_NAME.match(path.name))


def build_ledger(
    *,
    reflib_log: Optional[Path] = None,
    rescue_cache: Optional[Path] = None,
    local_roots: Sequence[Path] = (),
    hash_files: bool = False,
    project_root: Optional[Path] = None,
) -> Ledger:
    """扫描本地 PDF 并合入两份账本，返回证据索引。

    ``project_root`` 用于把证据行里的路径记成相对工程根的形式；不传时按原样记录。
    """
    ledger = Ledger()
    roots = [Path(root) for root in local_roots]
    if roots:
        catalog = build_catalog(roots, reflib_log=reflib_log, hash_files=hash_files)
        for record in catalog.records:
            citation = record.citation
            ref = ref_key(citation.ref) if citation else None
            local = LocalFile(
                path=record.path,
                ref=ref,
                cls=citation.cls if citation else None,
                arxiv=citation.arxiv if citation else None,
                title=citation.title if citation else None,
            )
            ledger.files.append(local)
            unnumbered = not _numbered_in_name(record.path)
            ledger.add(
                Evidence(
                    source=SOURCE_LOCAL_FILE,
                    detail=relative_to_root(record.path, project_root),
                    usable=True,
                    ref=ref,
                    arxiv=local.arxiv,
                    path=record.path,
                    unnumbered=unnumbered,
                )
            )
    if reflib_log is not None:
        _add_reflib_log(ledger, Path(reflib_log), project_root)
    if rescue_cache is not None:
        _add_rescue_cache(ledger, Path(rescue_cache), roots, project_root)
    return ledger


def _add_reflib_log(ledger: Ledger, path: Path, project_root: Optional[Path]) -> None:
    """读 ``reflib_download_log.csv``：同一 ref 取最后一行，ok 且文件存在才算可用。"""
    last: dict[str, dict[str, str]] = {}
    order: list[str] = []
    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        for line_no, row in enumerate(csv.DictReader(handle), start=2):
            ref = ref_key(row.get("ref"))
            if ref is None:
                continue
            if ref not in last:
                order.append(ref)
            last[ref] = {**row, "_line": str(line_no)}
    for ref in order:
        row = last[ref]
        status = (row.get("status") or "").strip()
        saved = (row.get("saved") or "").strip()
        exists = bool(saved) and Path(saved).exists()
        usable = status.startswith(_STATUS_OK) and exists
        arxiv = (row.get("arxiv") or "").strip()
        ledger.add(
            Evidence(
                source=SOURCE_REFLIB_LOG,
                detail=(
                    f"{relative_to_root(path, project_root)}:{row['_line']} ref={ref} "
                    f"status={status or '-'} arxiv={arxiv or '-'} "
                    f"saved={relative_to_root(saved, project_root) if saved else '-'}"
                    + ("" if exists or not saved else " [saved path missing on disk]")
                ),
                usable=usable,
                ref=ref,
                arxiv=arxiv if arxiv and arxiv != "no-arxiv" else None,
                path=Path(saved) if exists else None,
            )
        )


def _add_rescue_cache(
    ledger: Ledger, path: Path, roots: Sequence[Path], project_root: Optional[Path]
) -> None:
    """读救援缓存：**按 arXiv 号** 解析真实文件，不信任记录里的旧文件名。"""
    payload = json.loads(path.read_text(encoding="utf-8"))
    for ref_raw, record in payload.items():
        ref = ref_key(ref_raw)
        if ref is None:
            continue
        status = str(record.get("status") or "")
        arxiv = record.get("arxiv")
        recorded = record.get("file")
        resolved = [item for item in ledger.files if arxiv and item.arxiv == arxiv]
        recorded_exists = _recorded_exists(recorded, roots)
        stale = bool(recorded) and recorded_exists is False
        detail = (
            f"{relative_to_root(path, project_root)} ref={ref} status={status or '-'} "
            f"arxiv={arxiv or '-'} recorded_file={recorded or '-'}"
        )
        if stale:
            detail += " [recorded filename stale]"
        detail += " resolved=" + (
            ", ".join(relative_to_root(item.path, project_root) for item in resolved) or "-"
        )
        usable = status == _STATUS_RESCUED and bool(resolved)
        ledger.add(
            Evidence(
                source=SOURCE_RESCUE_CACHE,
                detail=detail,
                usable=usable,
                ref=ref,
                arxiv=arxiv,
                path=resolved[0].path if resolved else None,
                unnumbered=bool(resolved) and all(item.unnumbered for item in resolved),
            )
        )


def _recorded_exists(recorded: Optional[str], roots: Sequence[Path]) -> Optional[bool]:
    """记录的文件名是否真的还在某个本地根目录下；无从判断时返回 ``None``。"""
    if not recorded or not roots:
        return None
    for root in roots:
        candidate = root / recorded
        if candidate.exists():
            return True
    return False


def add_bibliography_claims(ledger: Ledger, entries: Iterable[RefEntry]) -> None:
    """把文献表自身的说法登记为证据（``usable=False``，仅作待核对的线索）。

    重复调用不会重复登记（按 ``detail`` 去重）。
    """
    known = {evidence.detail for item in ledger.by_ref.values() for evidence in item}
    for entry in entries:
        ref = str(entry.number)
        claims: list[Evidence] = []
        if entry.tag_kind == TAG_LOCAL:
            claims.append(
                Evidence(
                    source=SOURCE_BIB_TAG,
                    detail=f"{entry.source}:{entry.line} tag '{entry.tag}' claims a local copy",
                    usable=False,
                    ref=ref,
                    arxiv=entry.arxiv,
                )
            )
        if entry.tag_arxiv:
            claims.append(
                Evidence(
                    source=SOURCE_BIB_ARXIV,
                    detail=f"{entry.source}:{entry.line} tag arXiv:{entry.tag_arxiv}",
                    usable=False,
                    ref=ref,
                    arxiv=entry.tag_arxiv,
                )
            )
        if entry.prose_arxiv and entry.prose_arxiv != entry.tag_arxiv:
            claims.append(
                Evidence(
                    source=SOURCE_BIB_ARXIV,
                    detail=f"{entry.source}:{entry.line} prose arXiv:{entry.prose_arxiv}",
                    usable=False,
                    ref=ref,
                    arxiv=entry.prose_arxiv,
                )
            )
        for evidence in claims:
            if evidence.detail not in known:
                known.add(evidence.detail)
                ledger.add(evidence)
