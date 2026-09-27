# -*- coding: utf-8 -*-
"""把引用编号走完整条链：文献条目 → 本地 PDF → 转换后的 markdown → 可用文本。

每一跳都显式记账，断在哪一跳就记在哪一跳，绝不静默丢步：

======================  ==========================================================
``entry-missing``       参考文献表里没有这个编号
``no-local-file``       编号有条目，但没有可用的本地原文（PDF 不存在或账本里没登记）
``not-converted``       本地 PDF 在，但文献转换账本里没有它的 done 记录 / 扁平文本不存在
``empty-text``          转换产物在，但去掉引用头之后正文不足 :data:`MIN_TEXT_CHARS`
``ok``                  四跳全通，``text_chars`` 是可用正文的字符数
======================  ==========================================================

三处"不自己实现"：

* 文献条目用 :func:`primer.references.discover_lists` / :func:`all_entries` 解析；
* 本地原文用 :func:`primer.references.build_ledger` 的证据索引（``usable_local``），
  取回的是账本自己写下的证据行，而不是这里再猜一遍文件名；
* 转换产物用 :class:`primer.literature.Ledger` 读 ``state.jsonl``：先按 ``rel_path``
  对上源 PDF，再取该记录的 ``output_dir``，扁平 markdown 就是 ``flat/<output_dir 名>.md``
  ——**不靠文件名前缀猜**，因此转换器换过 md5 前缀或档位也不影响。

审计结论（``index.csv`` 里的 ``verdict`` / ``confidence``）只作为附加字段带上，
不参与这里的分跳判定：84/85 共享同一个 arXiv 号、被审计标为 ``conflict``，本模块
按**编号**解析，各自落到各自的 PDF（084_… 与 085_…），不会互相串台；共享 arXiv
这件事记进 ``shared_arxiv`` 里随链路一起交代。
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Optional, Sequence

from ..literature import Ledger as LiteratureLedger
from ..literature.paths import relative_to_root
from ..references import RefEntry, all_entries, build_ledger, discover_lists
from .retrieve import strip_header

# 本工程（行星探测工程）的默认输入，均可由命令行覆盖。
DEFAULT_REFERENCE_MD = "成果文件/行星探测三十年综述_参考文献.md"
DEFAULT_LOCAL_ROOT = "参考资料/参考文献原文"
DEFAULT_REFLIB_LOG = "参考资料/reflib_download_log.csv"
DEFAULT_RESCUE_CACHE = "中间文件/arxiv_rescue_cache.json"
DEFAULT_LITERATURE_STATE = "_primer/literature/state.jsonl"
DEFAULT_LITERATURE_FLAT = "_primer/literature/flat"
DEFAULT_REFERENCES_INDEX = "_primer/references/index.csv"

STATUS_OK = "ok"
STATUS_ENTRY_MISSING = "entry-missing"
STATUS_NO_LOCAL_FILE = "no-local-file"
STATUS_NOT_CONVERTED = "not-converted"
STATUS_EMPTY_TEXT = "empty-text"
STATUSES = (
    STATUS_OK,
    STATUS_ENTRY_MISSING,
    STATUS_NO_LOCAL_FILE,
    STATUS_NOT_CONVERTED,
    STATUS_EMPTY_TEXT,
)

MIN_TEXT_CHARS = 1200
"""``ok`` 所需的可用正文字符下限；只够放下引用头的产物算 ``empty-text``。"""

_BROKEN = (STATUS_ENTRY_MISSING, STATUS_NO_LOCAL_FILE, STATUS_NOT_CONVERTED, STATUS_EMPTY_TEXT)


@dataclass(frozen=True)
class SourceText:
    """一跳到底之后的原文位置与规模。"""

    local_file: str
    markdown: str
    text_chars: int
    header_chars: int
    md5: str
    line_count: int


@dataclass(frozen=True)
class Resolution:
    """一个引用编号的完整解析结果。"""

    number: int
    status: str
    entry: Optional[dict]
    source: Optional[SourceText]
    evidence: list[str] = field(default_factory=list)
    audit_verdict: Optional[str] = None
    audit_confidence: Optional[str] = None
    shared_arxiv: Optional[str] = None

    @property
    def usable(self) -> bool:
        return self.status == STATUS_OK

    @property
    def broken(self) -> bool:
        return self.status in _BROKEN


def load_entries(path: Path, *, project_root: Optional[Path] = None) -> dict[int, RefEntry]:
    """解析参考文献 markdown，返回 ``{编号: 条目}``（同号以最后一条为准）。"""
    lists = discover_lists([Path(path)], project_root=project_root)
    if not lists:
        raise ValueError(f"no reference entries found in {path}")
    entries: dict[int, RefEntry] = {}
    for entry in all_entries(lists):
        entries[entry.number] = entry
    return entries


def load_audit_index(path: Optional[Path]) -> dict[int, dict]:
    """读 ``index.csv`` 的审计结论（可选）：``{编号: {verdict, confidence, note}}``。"""
    if path is None or not Path(path).is_file():
        return {}
    rows: dict[int, dict] = {}
    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            number = (row.get("number") or "").strip()
            if not number.isdigit():
                continue
            rows[int(number)] = {
                "verdict": (row.get("verdict") or "").strip() or None,
                "confidence": (row.get("confidence") or "").strip() or None,
                "note": (row.get("note") or "").strip() or None,
            }
    return rows


class Resolver:
    """一条链的解析器；同一编号只解析一次并缓存。"""

    def __init__(
        self,
        *,
        project_root: Path,
        entries: Mapping[int, RefEntry],
        ledger,
        literature_state: Optional[Path],
        flat_dir: Optional[Path],
        audit: Optional[Mapping[int, dict]] = None,
        min_text_chars: int = MIN_TEXT_CHARS,
    ):
        self.project_root = Path(project_root)
        self.entries = dict(entries)
        self.ledger = ledger
        self.literature_state = Path(literature_state) if literature_state else None
        self.flat_dir = Path(flat_dir) if flat_dir else None
        self.audit = dict(audit or {})
        self.min_text_chars = min_text_chars
        self._records = self._load_literature()
        self._shared = self._load_shared_arxiv()
        self._cache: dict[int, Resolution] = {}
        self._texts: dict[str, str] = {}

    # ---- 账本 ----

    def _load_literature(self) -> dict[str, dict]:
        """``{源 PDF 相对路径: 转换记录}``；同路径多条时取最后一条 done 记录。"""
        if self.literature_state is None or not self.literature_state.is_file():
            return {}
        records = LiteratureLedger(self.literature_state).load()
        by_rel: dict[str, dict] = {}
        for record in records.values():
            if record.status != "done" or not record.output_dir:
                continue
            by_rel[record.rel_path] = {
                "md5": record.md5,
                "rel_path": record.rel_path,
                "output_dir": record.output_dir,
                "pages": record.pages,
            }
        return by_rel

    def _load_shared_arxiv(self) -> dict[str, list[str]]:
        """同一个 arXiv 号被多个编号引用时登记进来，供链路里交代。"""
        shared: dict[str, list[str]] = {}
        for arxiv, evidence in self.ledger.by_arxiv.items():
            refs = sorted({item.ref for item in evidence if item.ref}, key=int)
            if arxiv and len(refs) > 1:
                shared[arxiv] = refs
        return shared

    # ---- 链路 ----

    def resolve(self, number: int) -> Resolution:
        if number in self._cache:
            return self._cache[number]
        result = self._resolve(number)
        self._cache[number] = result
        return result

    def resolve_all(self, numbers: Iterable[int]) -> dict[int, Resolution]:
        return {number: self.resolve(number) for number in numbers}

    def _resolve(self, number: int) -> Resolution:
        audit = self.audit.get(number, {})
        entry = self.entries.get(number)
        if entry is None:
            return Resolution(
                number=number,
                status=STATUS_ENTRY_MISSING,
                entry=None,
                source=None,
                evidence=[f"bibliography: no entry [{number}]"],
                audit_verdict=audit.get("verdict"),
                audit_confidence=audit.get("confidence"),
            )
        payload = _entry_payload(entry)
        shared = self._shared.get(entry.arxiv) if entry.arxiv else None
        shared_refs = [ref for ref in (shared or []) if ref != str(number)]

        evidence: list[str] = [f"entry: {entry.source}:{entry.line}"]
        usable = self.ledger.usable_local(ref=str(number), arxiv=entry.arxiv)
        chosen = next((item for item in usable if item.path and Path(item.path).is_file()), None)
        for item in self.ledger.evidence_for(ref=str(number), arxiv=entry.arxiv):
            evidence.append(f"local: {item.describe()}")
        if chosen is None:
            return Resolution(
                number=number,
                status=STATUS_NO_LOCAL_FILE,
                entry=payload,
                source=None,
                evidence=evidence + [f"local: no usable local copy of [{number}]"],
                audit_verdict=audit.get("verdict"),
                audit_confidence=audit.get("confidence"),
                shared_arxiv=_shared_note(entry.arxiv, shared_refs),
            )

        rel = relative_to_root(chosen.path, self.project_root)
        record = self._records.get(rel)
        if record is None:
            return Resolution(
                number=number,
                status=STATUS_NOT_CONVERTED,
                entry=payload,
                source=None,
                evidence=evidence
                + [
                    f"local: chosen copy {rel}",
                    f"converted: no done record for {rel} in "
                    f"{_rel(self.literature_state, self.project_root)}",
                ],
                audit_verdict=audit.get("verdict"),
                audit_confidence=audit.get("confidence"),
                shared_arxiv=_shared_note(entry.arxiv, shared_refs),
            )
        flat = (self.flat_dir or self.literature_state.parent / "flat") / f"{Path(record['output_dir']).name}.md"
        if not flat.is_file():
            return Resolution(
                number=number,
                status=STATUS_NOT_CONVERTED,
                entry=payload,
                source=None,
                evidence=evidence
                + [
                    f"local: chosen copy {rel}",
                    f"converted: ledger record md5={record['md5']} points at "
                    f"{record['output_dir']} but {_rel(flat, self.project_root)} is missing",
                ],
                audit_verdict=audit.get("verdict"),
                audit_confidence=audit.get("confidence"),
                shared_arxiv=_shared_note(entry.arxiv, shared_refs),
            )

        text = self.text_for(rel, flat)
        body, header_chars = strip_header(text)
        evidence.append(
            f"converted: {_rel(flat, self.project_root)} md5={record['md5']} chars={len(body)}"
        )
        if len(body) < self.min_text_chars:
            return Resolution(
                number=number,
                status=STATUS_EMPTY_TEXT,
                entry=payload,
                source=None,
                evidence=evidence + [f"text: only {len(body)} chars of body (< {self.min_text_chars})"],
                audit_verdict=audit.get("verdict"),
                audit_confidence=audit.get("confidence"),
                shared_arxiv=_shared_note(entry.arxiv, shared_refs),
            )
        return Resolution(
            number=number,
            status=STATUS_OK,
            entry=payload,
            source=SourceText(
                local_file=rel,
                markdown=_rel(flat, self.project_root),
                text_chars=len(body),
                header_chars=header_chars,
                md5=record["md5"],
                line_count=body.count("\n") + 1,
            ),
            evidence=evidence,
            audit_verdict=audit.get("verdict"),
            audit_confidence=audit.get("confidence"),
            shared_arxiv=_shared_note(entry.arxiv, shared_refs),
        )

    def text_for(self, rel: str, flat: Path) -> str:
        """读一份转换产物并缓存；同一份原文被多条论断引用时只读一次。"""
        if rel not in self._texts:
            self._texts[rel] = flat.read_text(encoding="utf-8")
        return self._texts[rel]


def _rel(path: Optional[Path], project_root: Path) -> str:
    return "-" if path is None else relative_to_root(path, project_root)

def _entry_payload(entry: RefEntry) -> dict:
    return {
        "number": entry.number,
        "authors": entry.authors,
        "title": entry.title,
        "venue_year": entry.venue_year,
        "arxiv": entry.arxiv,
        "doi": entry.doi,
        "tag_kind": entry.tag_kind,
        "class_letter": entry.class_letter,
        "class_title": entry.class_title,
        "source": entry.source,
        "line": entry.line,
        "raw": entry.raw,
    }


def _shared_note(arxiv: Optional[str], others: Sequence[str]) -> Optional[str]:
    if not arxiv or not others:
        return None
    return f"{arxiv} is also claimed by [{', '.join(others)}]"


def build_resolver(
    project_root: Path,
    *,
    reference_md: Optional[Path] = None,
    reflib_log: Optional[Path] = None,
    rescue_cache: Optional[Path] = None,
    local_roots: Sequence[Path] = (),
    literature_state: Optional[Path] = None,
    flat_dir: Optional[Path] = None,
    audit_index: Optional[Path] = None,
    min_text_chars: int = MIN_TEXT_CHARS,
) -> Resolver:
    """按工程根的常规布局装配解析器；传 None 的输入按"不存在"处理并记进 :func:`describe_inputs`。"""
    root = Path(project_root)
    entries = load_entries(reference_md or root / DEFAULT_REFERENCE_MD, project_root=root)
    ledger = build_ledger(
        reflib_log=reflib_log,
        rescue_cache=rescue_cache,
        local_roots=list(local_roots),
        project_root=root,
    )
    return Resolver(
        project_root=root,
        entries=entries,
        ledger=ledger,
        literature_state=literature_state,
        flat_dir=flat_dir,
        audit=load_audit_index(audit_index),
        min_text_chars=min_text_chars,
    )


def describe_inputs(paths: Mapping[str, Optional[Path]], project_root: Path) -> dict:
    """列出本次解析用到的输入及其存在性——默认路径缺了要看得见，不能悄悄当没有。"""
    described: dict[str, dict] = {}
    for name, path in paths.items():
        described[name] = {
            "path": "-" if path is None else relative_to_root(path, project_root),
            "exists": bool(path) and Path(path).exists(),
        }
    return described


def dump_state_summary(state_path: Optional[Path], project_root: Optional[Path] = None) -> dict:
    """文献转换账本的规模（总数 / 各状态计数），供报告交代"还有多少在转"。

    路径按全项目约定记成相对工程根的形式——产物里不出现绝对路径。
    """
    if state_path is None or not Path(state_path).is_file():
        return {"path": None, "records": 0, "statuses": {}}
    records = LiteratureLedger(Path(state_path)).load()
    statuses: dict[str, int] = {}
    for record in records.values():
        statuses[record.status] = statuses.get(record.status, 0) + 1
    return {
        "path": relative_to_root(state_path, project_root),
        "records": len(records),
        "statuses": statuses,
    }
