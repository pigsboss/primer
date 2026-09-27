# -*- coding: utf-8 -*-
"""命令行入口：``python -m primer.book <command>``。

* ``build``：按清单装配成书，写出 ``.tex`` 与 ``Makefile``，必要时驱动引擎编译；
* ``check``：读回装配与 ``.log``，产出机器可读的发现报告（JSON）与人读表；加
  ``--deep`` 时再跑一遍确定性的文本层检查（:mod:`primer.book.checks`），它读的是
  上一次成书的 ``.tex``／``.toc``／``.lof``／``.lot``／``.pdf``
  ——不看模型、没有随机性；
* ``inspect``：把已排好的 PDF 逐页渲染，交给多模态模型挑出排版缺陷——每条发现都
  指向最可能该改的 markdown→TeX 转换器部件（``converter_hint``）。

产物一律落在 ``<工程根>/_primer/book/``（``inspect`` 的图像与报告在其 ``inspect/``
子目录）。字体与字号可以在命令行上试，不必改清单：``--font`` 同时改正文的拉丁与中文
衬线字，``--sans-font`` 改标题的黑体字，``--fontsize`` 改正文字号（``\\zihao`` 代码或磅值）。
``inspect`` 的端点与两个旋钮（每请求页数、回信 token 上限）也从清单的 ``vision:`` 块读：
命令行显式给出的值覆盖清单，清单覆盖内置默认——模型自己的可靠取值记在清单里，
不必靠人记住一条魔法参数。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Optional, Sequence

from ..paths import relative_to_root
from . import checks, logcheck
from .builder import BookBuilder, BuildError
from .findings import Finding, fatal, is_fatal, summarize
from .inspect import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_KEY_ENV,
    DEFAULT_MAX_PAGES,
    DEFAULT_MAX_TOKENS,
    DEFAULT_RESOLUTION,
    InspectError,
    run_inspect,
)
from .manifest import BookManifest, ManifestError, load_manifest

PROG = "python -m primer.book"


def _effective_manifest(manifest: BookManifest, args: argparse.Namespace) -> BookManifest:
    """把命令行上的字体／字号覆盖叠加到清单上。"""
    fonts = manifest.fonts
    typography = manifest.typography
    if getattr(args, "font", None):
        fonts = replace(fonts, main=args.font, cjk_main=args.font)
    if getattr(args, "sans_font", None):
        fonts = replace(fonts, sans=args.sans_font, cjk_sans=args.sans_font)
    if getattr(args, "fontsize", None):
        typography = replace(typography, body_font_size=args.fontsize)
    if getattr(args, "no_normalize_quotes", False):
        typography = replace(typography, normalize_quotes=False)
    return replace(manifest, fonts=fonts, typography=typography)


def _builder(manifest: BookManifest, args: argparse.Namespace, tex_only: bool = False) -> BookBuilder:
    return BookBuilder(
        manifest,
        out_dir=Path(args.out_dir) if getattr(args, "out_dir", None) else None,
        project_root=Path(args.project_root) if getattr(args, "project_root", None) else None,
        tex_only=tex_only,
    )


def _build_command(args: argparse.Namespace) -> int:
    manifest = _effective_manifest(load_manifest(Path(args.manifest)), args)
    builder = _builder(manifest, args, tex_only=args.tex_only)
    result = builder.build()
    if result.pdf is None:
        print("[book] tex-only build, skipping the typesetting engine")
    return 0


def _check_command(args: argparse.Namespace) -> int:
    manifest = _effective_manifest(load_manifest(Path(args.manifest)), args)
    builder = _builder(manifest, args)
    findings = builder.inspect()
    log = builder.plan.log
    # 产物里记的是路径，按约定一律相对工程根——把报告自己写下的绝对路径也算进去。
    shown_log = relative_to_root(log, builder.project_root)
    if log.is_file():
        findings.extend(logcheck.parse_log(log.read_text(errors="ignore")))
    else:
        findings.append(
            Finding(
                code="missing-log",
                severity="warning",
                message="no build log found; run build first to check the typeset output",
                location=shown_log,
            )
        )
    # 深度检查读的是上一次成书的产物（.tex/.toc/.lof/.lot/.pdf），与读 .log 同一个约定。
    if args.deep:
        findings.extend(checks.run_checks(manifest, builder.plan))

    _print_report(findings, manifest, shown_log, log.is_file())
    destination = Path(args.json) if args.json else builder.out_dir / f"{manifest.output.jobname}.check.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "jobname": manifest.output.jobname,
        "log": shown_log,
        "findings": [item.as_dict() for item in findings],
        "summary": summarize(findings),
        "failed": bool(fatal(findings)) or (args.strict and any(item.severity == "warning" for item in findings)),
    }
    destination.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\nreport: {destination}")

    if fatal(findings):
        return 1
    if args.strict and any(item.severity == "warning" for item in findings):
        return 1
    return 0


def _print_report(
    findings: Sequence[Finding], manifest: BookManifest, log: str, log_exists: bool
) -> None:
    print(f"book: {manifest.book.title}{manifest.book.subtitle}")
    print(f"log: {log}{'' if log_exists else '  (missing)'}")
    rows = summarize(findings)
    if not rows:
        print("\nno findings")
        return
    print(f"\n{'code':<28}{'severity':<10}{'count':>5}")
    print("-" * 43)
    for row in rows:
        print(f"{row['code']:<28}{row['severity']:<10}{row['count']:>5}")
    errors = [item for item in findings if is_fatal(item)]
    if errors:
        print("\ndetails:")
        for item in errors[:40]:
            where = f" ({item.location})" if item.location else ""
            print(f"  [{item.code}] {item.message}{where}")
        if len(errors) > 40:
            print(f"  ... and {len(errors) - 40} more")


def _inspect_command(args: argparse.Namespace) -> int:
    manifest = load_manifest(Path(args.manifest))
    return run_inspect(
        manifest,
        out_dir=Path(args.out_dir) if args.out_dir else None,
        project_root=Path(args.project_root) if args.project_root else None,
        pages_spec=args.pages,
        max_pages=args.max_pages,
        resolution=args.resolution,
        base_url=args.vision_base_url,
        model=args.vision_model,
        key_env=args.vision_key_env,
        batch_size=args.batch_size,
        max_tokens=args.max_tokens,
        strict=args.strict,
        json_path=Path(args.json) if args.json else None,
    )


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。"""
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Manifest-driven book builder: markdown -> LaTeX -> PDF",
        epilog=(
            f"examples:\n"
            f"  {PROG} build book.yaml\n"
            f"  {PROG} build book.yaml --tex-only\n"
            f"  {PROG} build book.yaml --font 'Songti SC' --fontsize -4\n"
            f"  {PROG} check book.yaml --strict\n"
            f"  {PROG} check book.yaml --deep\n"
            f"  {PROG} inspect book.yaml\n"
            f"  {PROG} inspect book.yaml --pages 1-5,79,200 --max-pages 10\n"
            f"  {PROG} inspect book.yaml --vision-base-url https://api.kimi.com/coding/v1 --vision-model k3\n"
            "    (k3 accepts image input; the API key is read from the environment variable\n"
            f"     named by --vision-key-env, default {DEFAULT_KEY_ENV}; no credential file is ever read)"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    commands = parser.add_subparsers(dest="command", required=True)

    build = commands.add_parser("build", help="assemble the book, and compile it unless --tex-only")
    _common(build)
    build.add_argument("--tex-only", action="store_true", help="stop after writing the .tex file")
    build.set_defaults(handler=_build_command)

    check = commands.add_parser("check", help="inspect the assembled book and its typesetting log")
    _common(check)
    check.add_argument("--strict", action="store_true", help="also fail on warnings (overfull boxes)")
    check.add_argument(
        "--deep", action="store_true",
        help=(
            "also run the deterministic text-level checks (markdown residue, quote "
            "direction, list self-listing, entry counts, page and figure geometry); "
            "they read the last build's artifacts"
        ),
    )
    check.add_argument("--json", metavar="FILE", help="where to write the findings report")
    check.set_defaults(handler=_check_command)

    inspect = commands.add_parser(
        "inspect", help="rasterize the built PDF and ask a vision model what is wrong"
    )
    _paths(inspect)
    inspect.add_argument(
        "--pages", metavar="SPEC", help="explicit pages, e.g. 1-5,79,200 (overrides the selection policy)"
    )
    inspect.add_argument(
        "--max-pages", type=int, default=DEFAULT_MAX_PAGES, metavar="N",
        help=f"upper bound on rendered pages (default {DEFAULT_MAX_PAGES})",
    )
    inspect.add_argument(
        "--resolution", type=int, default=DEFAULT_RESOLUTION, metavar="DPI",
        help=f"rasterization resolution (default {DEFAULT_RESOLUTION})",
    )
    inspect.add_argument(
        "--batch-size", type=int, default=None, metavar="N",
        help=(
            "pages per vision request; the manifest's vision.batch_size wins over the "
            f"built-in default {DEFAULT_BATCH_SIZE}, and this flag wins over both "
            "(more pages save round-trips but make a truncated reply more likely)"
        ),
    )
    inspect.add_argument(
        "--max-tokens", type=int, default=None, metavar="N",
        help=(
            "cap on the model reply; the manifest's vision.max_tokens wins over the "
            f"built-in default {DEFAULT_MAX_TOKENS}, and this flag wins over both "
            "(thinking models spend this budget on reasoning too, so a low value "
            "silently truncates the findings)"
        ),
    )
    inspect.add_argument("--vision-base-url", metavar="URL", help="OpenAI-compatible base URL")
    inspect.add_argument("--vision-model", metavar="NAME", help="model name; it must accept image input")
    inspect.add_argument(
        "--vision-key-env", metavar="VAR",
        help=f"name of the environment variable holding the API key (default {DEFAULT_KEY_ENV})",
    )
    inspect.add_argument("--strict", action="store_true", help="also fail on warnings")
    inspect.add_argument("--json", metavar="FILE", help="where to write the machine-readable report")
    inspect.set_defaults(handler=_inspect_command)
    return parser


def _paths(command: argparse.ArgumentParser) -> None:
    command.add_argument("manifest", help="path to the book manifest (YAML)")
    command.add_argument("--project-root", metavar="DIR", help="override the project root")
    command.add_argument("--out-dir", metavar="DIR", help="override the output directory")


def _common(command: argparse.ArgumentParser) -> None:
    _paths(command)
    command.add_argument("--font", metavar="NAME", help="override the body serif family (Latin + CJK)")
    command.add_argument("--sans-font", metavar="NAME", help="override the heading sans family (Latin + CJK)")
    command.add_argument("--fontsize", metavar="SIZE", help="override the body size (zihao code or pt)")
    command.add_argument(
        "--no-normalize-quotes", action="store_true",
        help="leave ASCII double quotes untouched (default: pair them into Chinese curly quotes)",
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    """命令行入口。"""
    args = build_parser().parse_args(argv)
    try:
        return args.handler(args)
    except ManifestError as exc:
        print(f"manifest error: {exc}", file=sys.stderr)
        return 2
    except InspectError as exc:
        print(f"inspect error: {exc}", file=sys.stderr)
        return 2
    except BuildError as exc:
        print(f"build error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
