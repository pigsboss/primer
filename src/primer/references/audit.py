# -*- coding: utf-8 -*-
"""逐条判定：把文献表条目与本地证据对上，给出结论、置信度与可核查的证据行。

判定只用 **标识符级** 证据（ref 号或规范化 arXiv 号），不做标题模糊匹配——
宁可把一条判成 ``missing``（置信度 ``medium``），也不制造假的 ``local``。

结论的含义：

* ``local``：本地确有该条原文（编号或 arXiv 命中，且证据可用）；
* ``local_unregistered``：本地有文件，但文献表尾标说"待图书馆获取"，或命中的
  文件文件名里根本没有编号——**必须大声报出来，且不得进图书馆清单**；
* ``public_web``：尾标声明官网/NTRS/白宫/CBO 等公开来源；
* ``missing``：没有任何标识符级本地证据；
* ``conflict``：证据自相矛盾，例如两条条目抢同一个 arXiv 号，
  或救援记录给出的 arXiv 号与条目尾标不一致。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional, Sequence

from .entries import (
    TAG_AWAITING,
    TAG_LOCAL,
    TAG_PUBLIC_WEB,
    ListValidation,
    RefEntry,
    RefList,
    all_entries,
    validate_all,
)
from .ledger import (
    SOURCE_RESCUE_CACHE,
    USABLE_SOURCES,
    Evidence,
    Ledger,
    LocalFile,
    add_bibliography_claims,
)

VERDICT_LOCAL = "local"
VERDICT_LOCAL_UNREGISTERED = "local_unregistered"
VERDICT_PUBLIC_WEB = "public_web"
VERDICT_MISSING = "missing"
VERDICT_CONFLICT = "conflict"
VERDICTS = (
    VERDICT_LOCAL,
    VERDICT_LOCAL_UNREGISTERED,
    VERDICT_PUBLIC_WEB,
    VERDICT_MISSING,
    VERDICT_CONFLICT,
)

CONFIDENCE_HIGH = "high"
CONFIDENCE_MEDIUM = "medium"


@dataclass
class EntryAudit:
    """一条条目的判定结果。``evidence`` 每项都写明来源与具体位置。"""

    entry: RefEntry
    verdict: str
    confidence: str
    evidence: list[str] = field(default_factory=list)
    note: str = ""

    @property
    def number(self) -> int:
        return self.entry.number

    @property
    def arxiv(self) -> Optional[str]:
        return self.entry.arxiv


@dataclass
class Problems:
    """工作区级问题清单。"""

    unnumbered_files: list[LocalFile] = field(default_factory=list)
    unnumbered_notes: dict[str, str] = field(default_factory=dict)
    unregistered_files: list[LocalFile] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    rescue_mismatches: list[str] = field(default_factory=list)
    stale_rescue_paths: list[str] = field(default_factory=list)
    claims_without_file: list[str] = field(default_factory=list)
    text_missing: list[LocalFile] = field(default_factory=list)
    text_root: Optional[Path] = None


@dataclass
class AuditResult:
    """一次审计的全部产物。

    ``project_root`` 只在导出时用到：``index.json`` / ``index.csv`` / ``audit.md``
    里的路径都由它换算成相对形式。
    """

    lists: list[RefList]
    audits: list[EntryAudit]
    validations: list[ListValidation]
    problems: Problems
    body_citations: set[int] = field(default_factory=set)
    body_sources: list[str] = field(default_factory=list)
    project_root: Optional[Path] = None

    @property
    def entries(self) -> list[RefEntry]:
        return [audit.entry for audit in self.audits]

    def counts(self) -> dict[str, int]:
        """按结论计数（含值为 0 的结论，便于报表稳定）。"""
        counts = {verdict: 0 for verdict in VERDICTS}
        for audit in self.audits:
            counts[audit.verdict] += 1
        return counts

    def by_class(self) -> dict[str, dict[str, int]]:
        """按章节字母 × 结论计数。"""
        table: dict[str, dict[str, int]] = {}
        for audit in self.audits:
            letter = audit.entry.class_letter or "(none)"
            row = table.setdefault(letter, {verdict: 0 for verdict in VERDICTS})
            row[audit.verdict] += 1
        return table

    def missing(self) -> list[EntryAudit]:
        """需要向图书馆索取原文的条目。"""
        return [audit for audit in self.audits if audit.verdict == VERDICT_MISSING]


def run_audit(
    lists: Sequence[RefList],
    ledger: Ledger,
    *,
    text_root: Optional[Path] = None,
    body_citations: Optional[Iterable[int]] = None,
    body_sources: Sequence[str] = (),
    project_root: Optional[Path] = None,
) -> AuditResult:
    """对一组参考文献列表做完整审计。会向 ``ledger`` 登记文献表自述（幂等）。

    ``project_root`` 只影响导出时记进产物的路径写法，不影响任何判定。
    """
    entries = all_entries(lists)
    add_bibliography_claims(ledger, entries)
    owners: dict[str, list[RefEntry]] = {}
    for entry in entries:
        if entry.arxiv:
            owners.setdefault(entry.arxiv, []).append(entry)
    audits = [_evaluate(entry, ledger, owners) for entry in entries]
    problems = _collect_problems(entries, ledger, audits, text_root)
    return AuditResult(
        lists=list(lists),
        audits=audits,
        validations=validate_all(lists),
        problems=problems,
        body_citations=set(body_citations or ()),
        body_sources=list(body_sources),
        project_root=project_root,
    )


def _dedupe(evidences: Iterable[Evidence]) -> list[Evidence]:
    seen: list[Evidence] = []
    for evidence in evidences:
        if evidence not in seen:
            seen.append(evidence)
    return seen


def _evaluate(entry: RefEntry, ledger: Ledger, owners: dict[str, list[RefEntry]]) -> EntryAudit:
    ref = str(entry.number)
    ref_evidence = ledger.by_ref.get(ref, [])
    arxiv_evidence = ledger.by_arxiv.get(entry.arxiv, []) if entry.arxiv else []
    all_evidence = _dedupe([*ref_evidence, *arxiv_evidence])
    usable = [item for item in all_evidence if item.usable and item.source in USABLE_SOURCES]
    described = [item.describe() for item in all_evidence]

    conflicts: list[str] = []
    if entry.arxiv and len(owners.get(entry.arxiv, [])) > 1:
        rivals = [owner for owner in owners[entry.arxiv] if owner.number != entry.number]
        where = ", ".join(f"[{owner.number}] at {owner.source}:{owner.line}" for owner in rivals)
        conflicts.append(f"arXiv {entry.arxiv} is also claimed by {where}")
    if entry.tag_arxiv and entry.prose_arxiv and entry.tag_arxiv != entry.prose_arxiv:
        conflicts.append(
            f"tag says arXiv:{entry.tag_arxiv} but the prose says arXiv:{entry.prose_arxiv}"
        )
    for item in ref_evidence:
        if item.source != SOURCE_RESCUE_CACHE or not item.arxiv or not entry.arxiv:
            continue
        if item.arxiv != entry.arxiv:
            conflicts.append(
                f"rescue cache records arXiv:{item.arxiv} for this ref, "
                f"but the entry says arXiv:{entry.arxiv}"
            )
        break

    if conflicts:
        return EntryAudit(
            entry=entry,
            verdict=VERDICT_CONFLICT,
            confidence=CONFIDENCE_HIGH,
            evidence=described,
            note="; ".join(conflicts),
        )

    if usable:
        numbered_hit = any(not item.unnumbered for item in usable)
        if entry.tag_kind == TAG_AWAITING:
            return EntryAudit(
                entry=entry,
                verdict=VERDICT_LOCAL_UNREGISTERED,
                confidence=CONFIDENCE_HIGH,
                evidence=described,
                note=(
                    "the bibliography still says '待图书馆获取' but a local copy exists: "
                    + "; ".join(item.detail for item in usable)
                ),
            )
        if not numbered_hit:
            return EntryAudit(
                entry=entry,
                verdict=VERDICT_LOCAL_UNREGISTERED,
                confidence=CONFIDENCE_HIGH,
                evidence=described,
                note=(
                    "matched only to files without a reference number in the filename: "
                    + "; ".join(item.detail for item in usable)
                ),
            )
        return EntryAudit(
            entry=entry,
            verdict=VERDICT_LOCAL,
            confidence=CONFIDENCE_HIGH,
            evidence=described,
            note="local copy confirmed by " + "; ".join(
                f"{item.source} ({item.path.name if item.path else item.detail})" for item in usable
            ),
        )

    if entry.tag_kind == TAG_PUBLIC_WEB:
        return EntryAudit(
            entry=entry,
            verdict=VERDICT_PUBLIC_WEB,
            confidence=CONFIDENCE_HIGH,
            evidence=described,
            note=f"tag '{entry.tag}' declares a public web source; no library request needed",
        )

    notes: list[str] = []
    ambiguous = False
    if any(item.path for item in all_evidence):
        ambiguous = True
        notes.append("a local file is pointed at but could not be accepted as this entry's copy")
    if entry.tag_kind == TAG_LOCAL:
        ambiguous = True
        notes.append("the tag claims a local copy but no matching file was found")
    if entry.tag_arxiv:
        notes.append(f"arXiv:{entry.tag_arxiv} is known, so this may still be fetchable online")
    return EntryAudit(
        entry=entry,
        verdict=VERDICT_MISSING,
        confidence=CONFIDENCE_MEDIUM if ambiguous else CONFIDENCE_HIGH,
        evidence=described,
        note="; ".join(notes) or "no local evidence by ref number or arXiv id",
    )


def _collect_problems(
    entries: Sequence[RefEntry],
    ledger: Ledger,
    audits: Sequence[EntryAudit],
    text_root: Optional[Path],
) -> Problems:
    problems = Problems(text_root=Path(text_root) if text_root else None)
    owners = {entry.arxiv: entry for entry in entries if entry.arxiv}
    rescued_by = {
        evidence.arxiv: evidence.ref
        for evidences in ledger.by_ref.values()
        for evidence in evidences
        if evidence.source == SOURCE_RESCUE_CACHE and evidence.usable and evidence.arxiv
    }
    for local in ledger.files:
        if not local.unnumbered:
            continue
        problems.unnumbered_files.append(local)
        owner = owners.get(local.arxiv) if local.arxiv else None
        if owner is not None:
            note = (
                f"arXiv:{local.arxiv} is also claimed by entry [{owner.number}] "
                f"at {owner.source}:{owner.line}"
            )
        elif local.arxiv in rescued_by:
            note = f"rescued for ref [{rescued_by[local.arxiv]}] by the rescue cache"
        else:
            note = "no entry and no rescue record claims this ref number or arXiv id"
            problems.unregistered_files.append(local)
        problems.unnumbered_notes[local.path.name] = note

    by_arxiv: dict[str, list[RefEntry]] = {}
    for entry in entries:
        if entry.arxiv:
            by_arxiv.setdefault(entry.arxiv, []).append(entry)
    for arxiv, group in sorted(by_arxiv.items()):
        if len(group) > 1:
            problems.conflicts.append(
                "arXiv {} claimed by {} (lines {})".format(
                    arxiv,
                    ", ".join(f"[{entry.number}]" for entry in group),
                    ", ".join(str(entry.line) for entry in group),
                )
            )

    for evidence in (item for items in ledger.by_ref.values() for item in items):
        if evidence.source != SOURCE_RESCUE_CACHE:
            continue
        if evidence.detail and "recorded filename stale" in evidence.detail:
            problems.stale_rescue_paths.append(evidence.detail)
        if evidence.path is not None and not evidence.usable:
            problems.rescue_mismatches.append(evidence.detail)

    for audit in audits:
        if audit.entry.tag_kind == TAG_LOCAL and audit.verdict not in (
            VERDICT_LOCAL,
            VERDICT_LOCAL_UNREGISTERED,
        ):
            problems.claims_without_file.append(
                f"[{audit.number}] {audit.entry.source}:{audit.entry.line} claims a local copy "
                f"(verdict: {audit.verdict})"
            )

    if text_root is not None:
        root = Path(text_root)
        for local in ledger.files:
            if not (root / f"{local.path.stem}.txt").exists():
                problems.text_missing.append(local)
    return problems
