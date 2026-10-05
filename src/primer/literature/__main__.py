# -*- coding: utf-8 -*-
"""命令行入口：``python -m primer.literature <command>``。

* ``scan``：发现、取哈希、去重并打印扫描报告；
* ``status``：读取账本并打印状态摘要；
* ``run``：调用 MinerU 后端真正转换（分块、失败隔离、断点续跑、后处理）；
* ``web``：启动文献库本地服务（WebUI）——库文件默认 ``./primer.literature.json``，
  端口被占用时回退随机空闲端口（见 :mod:`primer.literature.web.server`）；
  启动前自动加载 ``.env``（``--env-file`` > ``./.env`` > primer 源码仓库根；
  只注入未设置的变量，任何值不打印）。

``scan`` / ``status`` / ``run`` 都可以在完全不提供配置文件的情况下，仅靠 ``--root`` /
``--project-root`` 跑通。``run --dry-run`` 只打印计划，不写任何文件。工程目录一律只读，
产物只会落在 ``<project-root>/_primer/literature/`` 里，报告与账本里的路径都相对工程根书写。
``web`` 与工程目录无关：它只读写用户指定的库文件。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Optional, Sequence

from .backends import MineruBackend
from .catalog import Catalog, DocRecord, build_catalog
from .config import TIERS, RunConfig, apply_overrides, load_config
from .paths import relative_to_root
from .report import render_run_plan, render_run_summary, render_scan, render_status
from .runner import ProgressLine, plan_run, run_conversion
from .state import Ledger

PROG = "python -m primer.literature"


def _resolve_config(args: argparse.Namespace) -> RunConfig:
    """配置文件（可选）叠加显式命令行参数。"""
    base = load_config(Path(args.config) if args.config else None)
    overrides: dict = {}
    if getattr(args, "project_root", None):
        overrides["project_root"] = args.project_root
    if getattr(args, "root", None):
        overrides["roots"] = args.root
    if getattr(args, "output_dir", None):
        overrides["output_dir"] = args.output_dir
    if getattr(args, "tier", None):
        overrides["tier"] = args.tier
    if getattr(args, "chunk_max_pages", None):
        overrides["chunk_max_pages"] = args.chunk_max_pages
    if getattr(args, "parse_timeout", None):
        overrides["parse_timeout_seconds"] = args.parse_timeout
    return apply_overrides(base, **overrides)


def _find_reflib_log(roots: Sequence[Path]) -> Optional[Path]:
    """在根目录下寻找 reflib 下载账本 CSV。"""
    for root in roots:
        candidate = Path(root) / "reflib_download_log.csv"
        if candidate.is_file():
            return candidate
    return None


def _progress_printer(label: str = "scan"):
    """构造逐文件进度回调，写 stderr。"""

    def report(done: int, total: int, path: Path) -> None:
        name = path.name[:60]
        sys.stderr.write(f"\r[{label}] {done}/{total} {name:<60}")
        sys.stderr.flush()
        if done == total:
            sys.stderr.write("\n")
            sys.stderr.flush()

    return report


def _chunk_printer():
    """构造 (块, 档位) 进度回调，每个调用组一行，写 stderr。"""

    def report(line: ProgressLine) -> None:
        sys.stderr.write(
            f"[run] chunk {line.chunk_index}/{line.chunk_total} tier={line.tier} "
            f"files={line.files} pages={line.pages} failed={line.failed} "
            f"elapsed={line.elapsed:.1f}s\n"
        )
        sys.stderr.flush()

    return report


def _scan_command(args: argparse.Namespace) -> int:
    config = _resolve_config(args)
    on_progress = None if args.quiet else _progress_printer()
    started = time.monotonic()
    catalog = build_catalog(
        config.roots,
        project_root=config.project_root,
        reflib_log=_find_reflib_log(config.roots),
        on_progress=on_progress,
        hash_files=not args.no_hash,
    )
    elapsed = time.monotonic() - started
    ledger = Ledger(config.state_path).load()
    sys.stdout.write(render_scan(catalog, ledger))
    if args.json is not None:
        _write_json(Path(args.json) if args.json else config.index_path, catalog)
    if not args.quiet:
        print(f"[scan] {_catalog_line(catalog)} in {elapsed:.2f}s", file=sys.stderr)
    return 0


def _status_command(args: argparse.Namespace) -> int:
    config = _resolve_config(args)
    ledger = Ledger(config.state_path).load()
    sys.stdout.write(render_status(ledger, project_root=config.project_root))
    return 0


def _run_command(args: argparse.Namespace) -> int:
    config = _resolve_config(args)
    started = time.monotonic()
    catalog = build_catalog(
        config.roots,
        project_root=config.project_root,
        reflib_log=_find_reflib_log(config.roots),
        on_progress=_progress_printer("catalog"),
    )
    print(f"[run] catalog: {_catalog_line(catalog)} in {time.monotonic() - started:.2f}s", file=sys.stderr)
    ledger = Ledger(config.state_path)
    if args.dry_run:
        plan = plan_run(catalog.records, ledger, config, limit=args.limit)
        backend = MineruBackend(command=config.mineru_command, model_source=config.model_source)
        sys.stdout.write(
            render_run_plan(plan, config=config, backend=backend.name, backend_available=backend.is_available())
        )
        return 0
    stats = run_conversion(
        config,
        catalog.records,
        limit=args.limit,
        keep_going=args.keep_going,
        on_progress=_chunk_printer(),
    )
    sys.stdout.write(render_run_summary(stats, config=config))
    return 1 if stats.failed else 0


def _web_command(args: argparse.Namespace) -> int:
    """启动文献库本地服务；web 模块延迟导入，不动其它子命令的启动开销。

    启动前加载 ``.env``（``--env-file`` > ``./.env`` > primer 源码仓库根）：
    只注入尚未设置的变量，报告只含变量名，任何值不打印。
    """
    from ..envfile import discover_env_file, load_env_file

    env_file = Path(args.env_file) if args.env_file else None
    env_path = discover_env_file(env_file)
    if env_path is not None and not env_path.is_file():
        print(f"error: env file not found: {env_path}", file=sys.stderr)
        return 1
    if env_path is not None:
        report = load_env_file(env_path)
        parts = [f"注入 {len(report.injected)} 项"]
        if report.injected:
            parts[0] += "：" + "、".join(report.injected)
        if report.skipped:
            parts.append(f"已存在跳过 {len(report.skipped)} 项")
        if report.ignored_lines:
            parts.append(f"未识别 {len(report.ignored_lines)} 行")
        print(f"已加载环境变量文件：{env_path}（{'；'.join(parts)}）")

    from .web.server import serve

    return serve(db=args.db, port=args.port, open_browser=not args.no_open)


def _catalog_line(catalog: Catalog) -> str:
    """catalog 的一行摘要；扫描时读不动的文件必须点名，不能悄悄少几个。"""
    line = f"{len(catalog.records)} PDFs"
    if catalog.unreadable:
        line += f" ({len(catalog.unreadable)} unreadable at scan time, skipped)"
    return line


def _write_json(path: Path, catalog: Catalog) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_catalog_payload(catalog), ensure_ascii=False, indent=2), encoding="utf-8")


def _catalog_payload(catalog: Catalog) -> dict:
    project_root = catalog.project_root
    return {
        "project_root": ".",
        "roots": [relative_to_root(root, project_root) for root in catalog.roots],
        "total_pdfs": len(catalog.records),
        "total_bytes": catalog.total_bytes,
        "unique_pdfs": len(catalog.unique),
        "unique_bytes": catalog.unique_bytes,
        "duplicate_groups": catalog.duplicate_groups,
        "redundant_bytes": catalog.redundant_bytes,
        "unreadable": [
            {"path": relative_to_root(item.path, project_root), "reason": item.reason}
            for item in catalog.unreadable
        ],
        "files": [_record_payload(record, project_root) for record in catalog.records],
    }


def _record_payload(record: DocRecord, project_root: Optional[Path]) -> dict:
    citation = record.citation
    return {
        "path": relative_to_root(record.path, project_root),
        "rel_path": relative_to_root(record.rel_path, project_root),
        "size": record.size,
        "md5": record.md5,
        "duplicate_of": (
            relative_to_root(record.duplicate_of, project_root) if record.duplicate_of else None
        ),
        "citation": (
            None
            if citation is None
            else {
                "ref": citation.ref,
                "cls": citation.cls,
                "title": citation.title,
                "arxiv": citation.arxiv,
                "source": citation.source,
            }
        ),
    }


def _add_project_root(parsers: Sequence[argparse.ArgumentParser]) -> None:
    """给各子命令都加上 `--project-root`：工程目录，primer 只读它。"""
    for parser in parsers:
        parser.add_argument(
            "--project-root",
            metavar="PATH",
            help="project root, read-only (default: the working directory, or the config file's dir)",
        )


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。"""
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Scan an academic PDF corpus: discovery, dedup and resume ledger",
        epilog=f"examples:\n  {PROG} scan --project-root . --root ./参考资料\n"
        f"  {PROG} scan --config literature.yaml --json\n"
        f"  {PROG} run --config literature.yaml --dry-run\n"
        f"  {PROG} run --project-root . --root ./参考资料 --tier standard\n"
        f"  {PROG} status --project-root .\n"
        f"  {PROG} web --db ./primer.literature.json",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    commands = parser.add_subparsers(dest="command", required=True)

    scan = commands.add_parser("scan", help="discover PDFs, hash them, dedup and report")
    scan.add_argument("--root", action="append", metavar="PATH", help="corpus root (repeatable)")
    scan.add_argument("--config", metavar="FILE", help="path to the YAML config")
    scan.add_argument("--output-dir", metavar="DIR", help="override the output directory")
    scan.add_argument("--no-hash", action="store_true", help="skip md5 hashing (fast count-only scan)")
    scan.add_argument(
        "--json",
        nargs="?",
        const="",
        metavar="FILE",
        help="also write the catalog as JSON (default: <output-dir>/index.json)",
    )
    scan.add_argument("--quiet", action="store_true", help="suppress progress output on stderr")
    scan.set_defaults(handler=_scan_command)

    run = commands.add_parser("run", help="convert pending PDFs with the MinerU backend")
    run.add_argument("--config", metavar="FILE", help="path to the YAML config")
    run.add_argument("--root", action="append", metavar="PATH", help="corpus root (repeatable)")
    run.add_argument("--output-dir", metavar="DIR", help="override the output directory")
    run.add_argument(
        "--tier",
        choices=TIERS,
        help="MinerU parse tier (one of: flash, basic, standard, advanced)",
    )
    run.add_argument("--limit", type=int, metavar="N", help="process at most N pending files")
    run.add_argument(
        "--chunk-max-pages",
        type=int,
        metavar="PAGES",
        help="split a chunk when its page total would exceed this (default: 400)",
    )
    run.add_argument(
        "--parse-timeout",
        type=float,
        metavar="SECONDS",
        help="parse-time budget for a full-size chunk, scaled by page count (default: 2400)",
    )
    run.add_argument("--dry-run", action="store_true", help="print the plan and write nothing")
    run.add_argument(
        "--keep-going", action="store_true", help="continue with later chunks after a failure"
    )
    run.set_defaults(handler=_run_command)

    status = commands.add_parser("status", help="read the resume ledger and print a summary")
    status.add_argument("--config", metavar="FILE", help="path to the YAML config")
    status.add_argument("--output-dir", metavar="DIR", help="override the output directory")
    status.set_defaults(handler=_status_command)

    web = commands.add_parser("web", help="serve the local literature library web UI")
    web.add_argument(
        "--db",
        metavar="FILE",
        help="library JSON file (default: ./primer.literature.json)",
    )
    web.add_argument(
        "--port",
        type=int,
        default=8801,
        metavar="N",
        help="listen port (default: 8801; a free port is used when it is busy)",
    )
    web.add_argument("--no-open", action="store_true", help="do not open the browser automatically")
    web.add_argument(
        "--env-file",
        metavar="FILE",
        help="load KEY=VALUE pairs from FILE before serving "
        "(default: ./.env, then the primer checkout's .env)",
    )
    web.set_defaults(handler=_web_command)

    _add_project_root((scan, run, status))
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """命令行入口。

    Ctrl-C / SIGTERM 走 130：编排层已经先把未收尾的 ``running`` 补成 ``failed``，
    这里只负责安静地报一句并以中断码退出，不再吐 traceback。
    """
    args = build_parser().parse_args(argv)
    try:
        return args.handler(args)
    except KeyboardInterrupt:
        print("error: interrupted", file=sys.stderr)
        return 130
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
