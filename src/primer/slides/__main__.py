# -*- coding: utf-8 -*-
"""命令行入口：``python -m primer.slides <command>``。

六个子命令，构成"生成—人改—校验—生成"的一圈，外加两道可选的模型辅助、一道版面质检与
一道金句递料：

* ``outline``：读 ``<deck>/deck.yaml`` 与它列的源 markdown（**不读成书产物**），产出
  ``candidates.md``（逐章主题句候选表）与 ``outline.yaml``（幻灯片骨架）。已存在的骨架
  **绝不静默覆盖**：默认拒绝，``--force`` 丢弃人改重生成，``--merge`` 只刷新派生量、保留
  人改过的预算、讲不讲与候选。
* ``check``：拿当前规格与源文件校验一份骨架，只读不写；``stale-outline`` 等 error 级发现会以
  非零码退出，``build`` 据此拒绝生成。
* ``build``：把校验过的骨架编成 ``slides.tex``，跑 xelatex 与 xdvipdfmx，再拿成书同一
  个日志解析器复核：缺字是硬失败，overfull 报警告并点出页码，最后核对 PDF 页数是否
  等于排出的帧数。
* ``select``：让模型为各帧圈要点的一遍**初稿**（一章一次请求），写进骨架的 ``picks``、
  并把出处记进 ``select.jsonl``。只填不覆盖：``review == ok`` 永不触碰，``picks`` 非空的
  帧不覆盖，``review == redo`` 只在 ``--redo`` 时重写。``--dry-run`` 一个字节都不写、
  也不读密钥。
* ``inspect``：对**已生成**的 ``slides.pdf`` 做版面质检（封面横线穿字、段落误居中、内容挤在
  上半区、正文误用衬线、行距过挤、要点残留原文编号）。确定性层只用 poppler 命令行，不联网；
  视觉层按 ``roles.vision`` 逐页提问。发现按页面角色给判据，并带上"该改哪里"；账目追加进
  ``inspect.jsonl``（只记 host，不写密钥）。
* ``golden``：从 ``candidates.md`` 里**确定性**筛"读起来像结论"的句子，落成 ``golden.md``
  递给**人**拍板（粘进帧的 ``picks`` 或章／节 ``hint``）。不联网、不调用模型、不写骨架。

除 ``select`` 外的五步全程确定性：不用模型、不联网。控制模型只有一句话：**每章的预算页数是
唯一的控制项**——``chapters[].budget`` 由人定，总页数 = 各章预算之和 + 固定页（开场 2、
目录 1、横向 3、讨论 3、备份 N），加出来是多少就是多少；``outline`` 只按候选材料占比给出
默认建议，``check`` 只报告每章的预算来自人工还是默认。**没有时长参数**：页数与时长的换算率
没有经过检验，场合与时长留给 ``outline.yaml`` 的注释由人自己填。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence

from .build import run as build_run
from .golden import GOLDEN_SUMMARY
from .golden import add_arguments as add_golden_arguments
from .golden import command as golden_command
from .inspect import INSPECT_SUMMARY
from .inspect import add_arguments as add_inspect_arguments
from .inspect import command as inspect_command
from .outline import DEFAULT_DECK, report_lines, run
from .plan import (
    DEFAULT_BACKUP,
    DEFAULT_CAPACITY_PER_PAGE,
    DEFAULT_MAX_PER_CHAPTER,
    DEFAULT_MIN_PER_CHAPTER,
    DeckConfig,
    SlidesError,
)
from .select import MODE_CHOICES, report_lines as select_report_lines
from .select import run as select_run
from .spec import DeckSpecError
from .validate import check_outline, check_report_lines

PROG = "python -m primer.slides"

OUTLINE_SUMMARY = (
    "a chapter's budget is the only control parameter: each chapter's pages are written by hand "
    "in outline.yaml, the total is their sum plus the fixed pages (opening 2, contents 1, "
    "cross-cutting 3, discussion 3, backup N), and the default suggestion is the material share "
    "-- the chapters' candidate characters spread over capacity_per_page characters per page, "
    "within --min/--max-per-chapter. Nothing is reconciled against a total afterwards"
)

CHECK_SUMMARY = (
    "validate an outline.yaml against the current deck.yaml and source markdown before generating "
    "slides; reads only, writes nothing, and exits non-zero when any check fails (errors block "
    "generation, warnings do not)"
)

BUILD_SUMMARY = (
    "turn a validated outline.yaml into a Beamer deck: write slides.tex and a Makefile next to "
    "the outline, run xelatex and xdvipdfmx, then gate the result. Errors in the outline block "
    "generation before anything is written; after compiling, a Missing character is a hard "
    "failure, any `!` error in the log is a hard failure too (a PDF that came out is not the "
    "same as a page that is right), and Overfull boxes are reported with their page numbers. "
    "The deck's canvas, type scale and colours come from the outline's theme block; its text is "
    "the book's own sentences (the outline's picks, rendered by the book's inline renderer); the "
    "tool only selects, lays out and checks, and never writes prose of its own."
)

SELECT_SUMMARY = (
    "let a model circle the points for an outline's frames: one request per chapter, drawing "
    "only from that chapter's candidate table. Mode 1 (a hint was given: the frame's note, its "
    "section's hint or the chapter's hint) and mode 2 (no hint) return original candidate ids; "
    "mode 3 (only with --mode 3) may compose a sentence from stated candidates, with every digit "
    "checked against their text. Only frames whose picks are empty and whose review is pending "
    "are filled: review == ok is never touched, review == redo only with --redo, and a frame "
    "that already has picks is never overwritten (mark it redo first). Every run appends the "
    "provenance to select.jsonl next to the outline; no key is ever written. --dry-run reports "
    "the requests it would send and writes nothing at all."
)

# 弃用别名：旧命令行里的 --manifest 一律报错指向 --spec，不静默映射到别的东西上。
MANIFEST_DEPRECATED = (
    "--manifest 已弃用：幻灯片不再读成书的清单，输入规格改由 deck.yaml 给出，"
    "请改用 --spec <deck 目录>/deck.yaml"
)


def _spec_path(args: argparse.Namespace) -> Optional[Path]:
    """命令行的 ``--spec``；用了弃用的 ``--manifest`` 就报错指路。"""
    if getattr(args, "manifest", None):
        raise SlidesError(MANIFEST_DEPRECATED)
    return Path(args.spec).expanduser() if getattr(args, "spec", None) else None


def _outline_command(args: argparse.Namespace) -> int:
    config = DeckConfig(
        backup=args.backup,
        min_per_chapter=args.min_per_chapter,
        max_per_chapter=args.max_per_chapter,
        capacity_per_page=args.capacity_per_page,
    )
    project_root = Path(args.project_root).expanduser().resolve()
    spec_path = _spec_path(args)
    out_dir = Path(args.out_dir).expanduser().resolve() if args.out_dir else None
    plan, written = run(
        project_root,
        config,
        deck=args.deck,
        spec_path=spec_path,
        out_dir=out_dir,
        force=args.force,
        merge=args.merge,
    )
    for line in report_lines(plan, project_root, written):
        print(line)
    return 0


def _check_command(args: argparse.Namespace) -> int:
    outline_path = Path(args.outline).expanduser().resolve()
    project_root = Path(args.project_root).expanduser().resolve()
    if not outline_path.is_file():
        raise SlidesError(f"outline not found: {outline_path}")
    candidates_path = Path(args.candidates).expanduser().resolve() if args.candidates else None
    document, candidates, findings = check_outline(
        outline_path, project_root, candidates_path, _spec_path(args)
    )
    for line in check_report_lines(
        document, findings, candidates, outline_path=outline_path, project_root=project_root
    ):
        print(line)
    return 2 if any(finding.fatal for finding in findings) else 0


def _build_command(args: argparse.Namespace) -> int:
    _, lines, code = build_run(
        Path(args.outline),
        Path(args.project_root),
        spec_path=_spec_path(args),
        tex_only=args.tex_only,
    )
    for line in lines:
        print(line)
    return code


def _select_command(args: argparse.Namespace) -> int:
    result = select_run(
        Path(args.outline),
        Path(args.project_root),
        candidates_path=Path(args.candidates) if args.candidates else None,
        only=args.only,
        limit=args.limit,
        mode=args.mode,
        force=args.force,
        redo=args.redo,
        dry_run=args.dry_run,
        max_tokens=args.max_tokens,
        timeout=args.timeout,
        config_path=Path(args.config) if args.config else None,
    )
    for line in select_report_lines(result):
        print(line)
    return 2 if result.failures else 0


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。"""
    parser = argparse.ArgumentParser(
        prog=PROG,
        description=(
            "Turn the finished book into a slide deck. outline, check, build and golden are "
            "deterministic and never touch the network; select asks a model to circle the points "
            "(one request per chapter); inspect checks the built PDF's layout with a deterministic "
            "geometry/font layer plus an optional per-page vision pass."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    commands = parser.add_subparsers(dest="command", required=True)

    outline = commands.add_parser(
        "outline",
        help="read deck.yaml and its source markdown, write candidates.md and outline.yaml",
        description=OUTLINE_SUMMARY,
        epilog=(
            "examples:\n"
            f"  {PROG} outline --project-root ~/Documents/kimi/Workspaces/行星探测工程\n"
            f"  {PROG} outline --project-root . --deck seminar\n"
            f"  {PROG} outline --spec _primer/slides/review-seminar/deck.yaml --merge\n"
            "the spec defaults to <project-root>/_primer/slides/<deck>/deck.yaml and is never "
            "written; the output goes to the same directory. --merge keeps the human-set "
            "budgets, speak flags, picks and theme and only refreshes the derived fields. "
            "there is no time parameter and no total page count: the per-chapter budgets in "
            "outline.yaml are the only control, and the occasion and slot are recorded by hand "
            "in its header"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    outline.add_argument(
        "--project-root", default=".", metavar="DIR",
        help="project root; the deck spec and its source markdown are read from it, "
             "nothing is written outside it",
    )
    outline.add_argument(
        "--spec", metavar="FILE",
        help="deck spec (deck.yaml); defaults to <project-root>/_primer/slides/<deck>/deck.yaml",
    )
    outline.add_argument(
        "--manifest", metavar="FILE",
        help="deprecated, does nothing but error: use --spec",
    )
    outline.add_argument(
        "--deck", default=DEFAULT_DECK, metavar="NAME",
        help=f"deck name, used as the output directory (default {DEFAULT_DECK})",
    )
    outline.add_argument(
        "--backup", type=int, default=DEFAULT_BACKUP, metavar="N",
        help=f"backup pages (default {DEFAULT_BACKUP})",
    )
    outline.add_argument(
        "--min-per-chapter", type=int, default=DEFAULT_MIN_PER_CHAPTER, metavar="N",
        help=f"floor on a chapter's *default* budget (default {DEFAULT_MIN_PER_CHAPTER}); "
             "a budget written by hand is taken as it is",
    )
    outline.add_argument(
        "--max-per-chapter", type=int, default=DEFAULT_MAX_PER_CHAPTER, metavar="N",
        help=f"cap on a chapter's *default* budget (default {DEFAULT_MAX_PER_CHAPTER})",
    )
    outline.add_argument(
        "--capacity-per-page", type=int, default=None, metavar="N",
        help=(
            "text capacity of one slide in characters; by default it is measured from the "
            "outline's own theme block (frame_metrics: 24 chars per line x 14 lines = "
            f"{DEFAULT_CAPACITY_PER_PAGE} chars for the default 22 pt / 30 pt body on the "
            "960 x 540 bp canvas), so changing the theme changes it; pass N to override "
            "that measurement explicitly"
        ),
    )
    outline.add_argument("--out-dir", metavar="DIR", help="override the output directory")
    safety = outline.add_mutually_exclusive_group()
    safety.add_argument(
        "--force",
        action="store_true",
        help=(
            "overwrite an existing outline.yaml, discarding your edits to budget, "
            "sections[].speak and picks (the theme block goes back to the defaults too)"
        ),
    )
    safety.add_argument(
        "--merge",
        action="store_true",
        help=(
            "rewrite an existing outline.yaml, keeping budget, sections[].speak and picks "
            "(and a hand-edited theme block, verbatim) and refreshing the derived fields "
            "(chapter & section identity, candidate counts, capacity, fingerprint); "
            "this is also the way to migrate a version-1 skeleton"
        ),
    )
    outline.set_defaults(handler=_outline_command)
    check = commands.add_parser(
        "check",
        help="validate an outline.yaml against the current deck spec (writes nothing)",
        description=CHECK_SUMMARY,
        epilog=(
            "examples:\n"
            f"  {PROG} check _primer/slides/review-seminar/outline.yaml\n"
            f"  {PROG} check outline.yaml --project-root ~/Documents/kimi/Workspaces/行星探测工程\n"
            "the candidate table defaults to candidates.md next to the outline; "
            "errors (severity error) exit non-zero and block generation, warnings do not"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    check.add_argument("outline", metavar="OUTLINE", help="path to the outline.yaml to validate")
    check.add_argument(
        "--project-root", default=".", metavar="DIR",
        help="project root that the outline's relative paths are resolved against (default .)",
    )
    check.add_argument(
        "--candidates", metavar="FILE",
        help="candidate table to validate picks against; defaults to candidates.md next to the outline",
    )
    check.add_argument(
        "--spec", metavar="FILE",
        help="deck spec (deck.yaml); defaults to the one the outline records, or the one next to it",
    )
    check.add_argument(
        "--manifest", metavar="FILE",
        help="deprecated, does nothing but error: use --spec",
    )
    check.set_defaults(handler=_check_command)

    build = commands.add_parser(
        "build",
        help="generate slides.tex from a validated outline, compile it, and gate the result",
        description=BUILD_SUMMARY,
        epilog=(
            "examples:\n"
            f"  {PROG} build _primer/slides/review-seminar/outline.yaml "
            "--project-root ~/Documents/kimi/Workspaces/行星探测工程\n"
            f"  {PROG} build outline.yaml --project-root . --tex-only\n"
            "the outline is validated first with the same rules as check; errors block "
            "generation and nothing is written. picks are candidate ids from candidates.md "
            "next to the outline; an entry without picks gets a visible 〔待选句〕 marker "
            "instead of an error, so the layout can be judged before the sentences are "
            "circled. slides.tex, the Makefile and all compile artifacts stay in the deck "
            "directory next to the outline"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    build.add_argument("outline", metavar="OUTLINE", help="path to the outline.yaml to build")
    build.add_argument(
        "--project-root", default=".", metavar="DIR",
        help="project root that the outline's relative paths and graphics are resolved against (default .)",
    )
    build.add_argument(
        "--spec", metavar="FILE",
        help="deck spec (deck.yaml); defaults to the one the outline records, or the one next to it",
    )
    build.add_argument(
        "--manifest", metavar="FILE",
        help="deprecated, does nothing but error: use --spec",
    )
    build.add_argument(
        "--tex-only", action="store_true",
        help="write slides.tex and the Makefile, but do not run the typesetting engine",
    )
    build.set_defaults(handler=_build_command)

    select = commands.add_parser(
        "select",
        help="ask a model to circle the points for the outline's frames (one request per chapter)",
        description=SELECT_SUMMARY,
        epilog=(
            "examples:\n"
            f"  {PROG} select _primer/slides/review-seminar/outline.yaml --dry-run\n"
            f"  {PROG} select outline.yaml --project-root ~/Documents/kimi/Workspaces/行星探测工程\n"
            f"  {PROG} select outline.yaml --only 8 --only A --mode 3\n"
            "one request per chapter; mode 1 (hint given) and mode 2 (no hint) return candidate "
            "ids from that chapter's candidates.md, mode 3 may compose a sentence from stated "
            "candidates with every digit checked against them; a frame is filled only when its "
            "picks are empty and its review is pending, review == ok is never touched, review == "
            "redo only with --redo; the endpoint and model come from roles.select (roles.distill "
            "for mode 3) in <project-root>/_primer/config.yaml, and the key only from the "
            "environment variable that config names; provenance is appended to select.jsonl next "
            "to the outline"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    select.add_argument("outline", metavar="OUTLINE", help="path to the outline.yaml to fill")
    select.add_argument(
        "--project-root", default=".", metavar="DIR",
        help=(
            "project root: the endpoint configuration is read from "
            "<root>/_primer/config.yaml and the candidate table from "
            "<root>/_primer/slides/<deck>/ when it is not next to the outline (default .)"
        ),
    )
    select.add_argument(
        "--candidates", metavar="FILE",
        help="candidate table to draw from; defaults to candidates.md next to the outline",
    )
    select.add_argument(
        "--only", action="append", default=[], metavar="CHAPTER",
        help="only this chapter number (repeatable, e.g. --only 8 --only A)",
    )
    select.add_argument(
        "--limit", type=int, metavar="N", help="process only the first N chapters (after --only)",
    )
    select.add_argument(
        "--mode", type=int, choices=MODE_CHOICES, metavar="{1,2,3}",
        help=(
            "force one selection mode on every frame: 1 picks original sentences to a given "
            "hint, 2 picks original sentences with no hint, 3 may compose new sentences from "
            "stated candidates (uses roles.distill); without it the mode is decided per frame"
        ),
    )
    select.add_argument(
        "--redo", action="store_true",
        help="also rewrite frames whose review is redo (their note is kept)",
    )
    select.add_argument(
        "--force", action="store_true",
        help=(
            "rewrite every frame except those whose review is ok, ignoring non-empty picks; "
            "careless, and the report says so"
        ),
    )
    select.add_argument(
        "--dry-run", action="store_true",
        help=(
            "write nothing at all: report the requests that would be sent with their size and "
            "estimated tokens, the writable frames, and the skipped ones by reason"
        ),
    )
    select.add_argument(
        "--max-tokens", type=int, metavar="N",
        help="output budget per request; defaults to the role's max_tokens, or 8192",
    )
    select.add_argument(
        "--timeout", type=float, metavar="SECONDS", help="HTTP timeout per request (default 180)",
    )
    select.add_argument(
        "--config", metavar="FILE",
        help="explicit endpoint configuration file, merged on top of the machine and project layers",
    )
    select.set_defaults(handler=_select_command)

    inspect = commands.add_parser(
        "inspect",
        help="lay out the quality check of a built slides.pdf (layout defects, two layers)",
        description=INSPECT_SUMMARY,
        epilog=(
            "examples:\n"
            f"  {PROG} inspect _primer/slides/review-seminar/outline.yaml\n"
            f"  {PROG} inspect outline.yaml --no-vision --project-root .\n"
            f"  {PROG} inspect outline.yaml --pages 1-8 --workers 4\n"
            "the pdf defaults to slides.pdf next to the outline; the page roles come from the "
            "outline's frames, and the role decides which checks apply (the cover and the quote "
            "page are never judged top-heavy); the deterministic layer never touches the network, "
            "the vision layer asks roles.vision one request per page; errors (severity error) exit "
            "non-zero and warnings do not; --dry-run writes nothing and never reads the key"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_inspect_arguments(inspect)
    inspect.set_defaults(handler=inspect_command)

    golden = commands.add_parser(
        "golden",
        help="shortlist golden-quote candidates from candidates.md into golden.md",
        description=GOLDEN_SUMMARY,
        epilog=(
            "examples:\n"
            f"  {PROG} golden _primer/slides/review-seminar/outline.yaml\n"
            f"  {PROG} golden outline.yaml --top 20\n"
            "candidates.md defaults to the file next to the outline; skipped sentences "
            "(too long, a pure question, no CJK, or too few signals) are listed with their "
            "reason on stdout; no network, no model, and outline.yaml is never touched"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_golden_arguments(golden)
    golden.set_defaults(handler=golden_command)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """命令行入口。"""
    args = build_parser().parse_args(argv)
    try:
        return args.handler(args)
    except (SlidesError, DeckSpecError) as exc:
        print(f"slides error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
