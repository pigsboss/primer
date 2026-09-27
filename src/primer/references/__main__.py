# -*- coding: utf-8 -*-
"""命令行入口：``python -m primer.references <command>``。

* ``audit``：解析参考文献 markdown，核对本地原文，写出 index/audit 全套产物，
  外加 ``references.bib`` 与校核报告 ``references-bib-review.md``；
* ``list``：只写图书馆索取清单（``missing`` 条目 + ``--oa-log`` 里的 fail/no-link，
  可传 ``--body`` 定优先级）。

两个子命令共用同一套解析与判定，``list`` 不会另算一遍结论。工程目录一律只读，
产物只会落在 ``<project-root>/_primer/references/`` 里（``--out`` 可覆盖），
产物里的路径一律相对工程根书写。

``.bib`` 只是另存一份供人工/以后换 biblatex 用，**不参与出书流程**：书稿目前仍把参考
文献当手写编号列表渲染。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence

from ..literature.paths import output_dir_for, relative_to_root, resolve_project_root
from .audit import AuditResult, run_audit
from .bib import build_records, write_bib, write_review
from .entries import all_entries, discover_lists
from .export import (
    collect_body_citations,
    library_rows,
    source_counts,
    write_audit_md,
    write_index_csv,
    write_index_json,
    write_library_csv,
    write_library_md,
)
from .ledger import build_ledger
from .survey import SurveyItem, load_oa_log

PROG = "python -m primer.references"
INDEX_JSON = "index.json"
INDEX_CSV = "index.csv"
AUDIT_MD = "audit.md"
LIBRARY_CSV = "图书馆文献获取清单.new.csv"
LIBRARY_MD = "图书馆文献获取清单.new.md"
BIB = "references.bib"
BIB_REVIEW = "references-bib-review.md"


def _common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--ref", action="append", metavar="MD", required=True,
        help="参考文献 markdown（可重复；每个文件可含多份清单）",
    )
    parser.add_argument("--reflib-log", metavar="CSV", help="reflib_download_log.csv")
    parser.add_argument("--rescue-cache", metavar="JSON", help="arxiv_rescue_cache.json")
    parser.add_argument(
        "--oa-log", metavar="CSV",
        help="文献调研的 oa_download_log.csv；其 fail/no-link 条目按 P2 并入图书馆清单",
    )
    parser.add_argument(
        "--local-root", action="append", metavar="DIR", default=[],
        help="本地原文目录（可重复）",
    )
    parser.add_argument(
        "--text-root", metavar="DIR",
        help="已抽取文本的目录；给了才检查 PDF 是否缺文本，否则标注为未核查",
    )
    parser.add_argument(
        "--body", action="append", metavar="MD", default=[],
        help="正文 markdown，用于判定 P1（可重复；不给则清单全部按 P1 处理）",
    )
    parser.add_argument(
        "--project-root", metavar="PATH",
        help="工程目录，只读（默认：当前工作目录）",
    )
    parser.add_argument(
        "--out", metavar="DIR",
        help="产物目录（默认：<工程根>/_primer/references）",
    )


def _resolve_paths(args: argparse.Namespace) -> tuple[Path, Path]:
    """返回 ``(工程根, 产物目录)``；工程根必须存在。"""
    project_root = resolve_project_root(args.project_root or Path.cwd())
    out = Path(args.out) if args.out else output_dir_for(project_root, "references")
    return project_root, out


def _run(args: argparse.Namespace, project_root: Path) -> tuple[AuditResult, list[SurveyItem]]:
    lists = discover_lists([Path(path) for path in args.ref], project_root=project_root)
    if not lists:
        raise ValueError("no reference entries found in the given markdown")
    ledger = build_ledger(
        reflib_log=Path(args.reflib_log) if args.reflib_log else None,
        rescue_cache=Path(args.rescue_cache) if args.rescue_cache else None,
        local_roots=[Path(root) for root in args.local_root],
        project_root=project_root,
    )
    cited: set[int] = set()
    sources: list[str] = []
    if getattr(args, "body", None):
        cited, sources = collect_body_citations(
            [Path(path) for path in args.body], project_root=project_root
        )
    result = run_audit(
        lists,
        ledger,
        text_root=Path(args.text_root) if args.text_root else None,
        body_citations=cited,
        body_sources=sources,
        project_root=project_root,
    )
    return result, load_oa_log(Path(args.oa_log) if args.oa_log else None)


def _print_summary(
    result: AuditResult,
    out: Path,
    written: Sequence[str],
    project_root: Path,
    library: Sequence[dict[str, str]],
) -> None:
    counts = result.counts()
    problems = result.problems
    entries = len(result.audits)
    print(f"[references] parsed {entries} entries from {len(result.lists)} list(s)")
    print(
        "[references] verdicts: "
        + " ".join(f"{verdict}={counts[verdict]}" for verdict in counts)
    )
    print(
        "[references] problems: "
        f"unnumbered_locals={len(problems.unnumbered_files)} "
        f"unregistered_locals={len(problems.unregistered_files)} "
        f"conflicts={len(problems.conflicts)} "
        f"rescue_mismatches={len(problems.rescue_mismatches)} "
        f"stale_rescue_paths={len(problems.stale_rescue_paths)} "
        f"claims_without_file={len(problems.claims_without_file)} "
        f"pdfs_without_text={len(problems.text_missing)}"
        + ("" if problems.text_root else " (text check not requested)")
    )
    if result.body_sources:
        print(
            f"[references] body citations: {len(result.body_citations)} numbers "
            f"from {len(result.body_sources)} file(s)"
        )
    else:
        print("[references] no --body given: every library row is P1")
    p1 = sum(1 for row in library if row["priority"] == "P1")
    families = source_counts(library)
    composition = ", ".join(f"{name}={count}" for name, count in sorted(families.items()))
    print(
        f"[references] library list rows: {len(library)} (P1={p1}, P2={len(library) - p1}; "
        f"by source: {composition or '-'})"
    )
    print(f"[references] wrote {', '.join(written)} to {relative_to_root(out, project_root)}")


def _audit_command(args: argparse.Namespace) -> int:
    project_root, out = _resolve_paths(args)
    result, survey = _run(args, project_root)
    write_index_json(out / INDEX_JSON, result)
    write_index_csv(out / INDEX_CSV, result)
    write_audit_md(out / AUDIT_MD, result)
    rows = library_rows(result, survey=survey)
    write_library_csv(out / LIBRARY_CSV, rows)
    write_library_md(out / LIBRARY_MD, rows, result, survey=survey)
    records = build_records(all_entries(result.lists))
    note = ", ".join(sorted({entry.source for entry in all_entries(result.lists)}))
    write_bib(out / BIB, records, source_note=note)
    write_review(out / BIB_REVIEW, records, source_note=note)
    uncertain = sum(1 for record in records if record.uncertain)
    print(
        f"[references] bib: {len(records)} entries, {uncertain} with guessed fields "
        f"(see {BIB_REVIEW})"
    )
    _print_summary(
        result,
        out,
        (INDEX_JSON, INDEX_CSV, AUDIT_MD, LIBRARY_CSV, LIBRARY_MD, BIB, BIB_REVIEW),
        project_root,
        rows,
    )
    return 0


def _list_command(args: argparse.Namespace) -> int:
    project_root, out = _resolve_paths(args)
    result, survey = _run(args, project_root)
    rows = library_rows(result, survey=survey)
    write_library_csv(out / LIBRARY_CSV, rows)
    write_library_md(out / LIBRARY_MD, rows, result, survey=survey)
    _print_summary(result, out, (LIBRARY_CSV, LIBRARY_MD), project_root, rows)
    return 0


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。"""
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Audit reference lists against locally available literature",
        epilog=f"examples:\n"
        f"  {PROG} audit --project-root . --ref 成果文件/参考文献.md "
        f"--reflib-log 参考资料/reflib_download_log.csv "
        f"--oa-log 参考资料/oa_download_log.csv "
        f"--rescue-cache 中间文件/arxiv_rescue_cache.json --local-root 参考资料/参考文献原文\n"
        f"  {PROG} list --project-root . --ref 成果文件/参考文献.md --body 成果文件/正文.md "
        f"--local-root 参考资料/参考文献原文",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    commands = parser.add_subparsers(dest="command", required=True)

    audit = commands.add_parser(
        "audit", help="full audit: index, audit.md, library list, references.bib"
    )
    _common_arguments(audit)
    audit.set_defaults(handler=_audit_command)

    listing = commands.add_parser("list", help="library request list only")
    _common_arguments(listing)
    listing.set_defaults(handler=_list_command)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """命令行入口。"""
    args = build_parser().parse_args(argv)
    try:
        return args.handler(args)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
