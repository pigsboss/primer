# -*- coding: utf-8 -*-
"""命令行入口：``python -m primer.scene <command>``。

四个子命令，构成"备料—生成—验收—看数"的一圈：

* ``fetch``：把规格声明的贴图备齐。**唯一联网的一步**。已有的文件对得上摘要就跳过，
  真正的下载结果逐条记进 ``assets/credits.yaml``（文件名、URL、sha256、字节数、许可、
  署名行、下载日期）——CC BY 4.0 要求署名，账本就是署名的凭据。
* ``build``：发射 ``build/scene.py``，交给无头 Blender 跑，日志落在 ``build/``，然后
  立刻跑一遍验收。``--preview`` 是缺省；``--final`` 另需 ``--allow-final``——正式渲染
  是人的决定，不是命令行顺手就能带上的开关。
* ``check``：只读的一步。拿规格核对现场报告、成图与账本，error 级发现以非零码退出。
* ``report``：把布局算出来的数字摊开给人看（显示轨道半径、放大倍数、节点坐标）。

本地 Blender 的位置、说明与限制见 ``_primer/scene/blender.md``。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence

from ..paths import output_dir_for, relative_to_root
from .assets import fetch as fetch_assets
from .build import run as build_run
from .checks import check_report_lines, check_scene, layout_report_lines
from .spec import SceneError, load_spec

PROG = "python -m primer.scene"

FETCH_SUMMARY = (
    "download the textures the spec declares and record where each one came from; the only "
    "step that touches the network. A file already on disk whose sha256 matches the credits "
    "ledger is skipped and keeps its original download date, so re-running changes nothing. "
    "Where the spec's filename is not published upstream (several 4k names are not), the "
    "asset manager probes the size variants in a fixed order and records the URL it really "
    "used in the ledger; check reports that as a warning rather than hiding it."
)

BUILD_SUMMARY = (
    "emit build/scene.py from the spec (deterministic: no timestamps, sorted keys, stable "
    "ordering), run it through headless Blender, keep the raw log in build/, then gate the "
    "result with the same checks check runs. --preview is the default; --final additionally "
    "requires --allow-final because the final render is a human decision."
)

CHECK_SUMMARY = (
    "validate the spec and the artifacts it produced: the naming contract, every body's "
    "custom properties against the layout module, the trajectory's own hard rules (it must "
    "not cut through any body's disc, the incidental flybys must neither bend nor pass far "
    "from their bodies, and the assist periapsis and turn must be the declared ones), the "
    "mission layer (a placeholder dot must not fall inside a body, dots and mission labels "
    "must stay in frame, and every mission line must be declared and used), the "
    "preview PNG's size and brightness (a black frame is a failure, not a look), the credits "
    "ledger, and the fingerprint. Reads only, writes nothing, exits non-zero when any "
    "finding is an error."
)

REPORT_SUMMARY = (
    "print the numbers the layout module resolved: display orbit radius and world units per "
    "body, the magnification each body is drawn at, the band radii, and every trajectory "
    "node's coordinates with the body it is pinned to. Reads only, writes nothing."
)


def _fetch_command(args: argparse.Namespace) -> int:
    project_root = Path(args.root).expanduser()
    spec = load_spec(_spec_path(args, project_root))
    result = fetch_assets(spec, project_root, force=args.force, timeout=args.timeout)
    print(f"贴图备料：{spec.meta.title}（{len(spec.assets.files)} 张）")
    print(f"  新下载 {len(result.downloaded)} 张，沿用 {len(result.skipped)} 张")
    for entry in result.entries:
        note = "" if entry.source_file == entry.texture else f"（上游文件 {entry.source_file}）"
        print(f"  {entry.texture:<28} {entry.bytes:>9} 字节  sha256 {entry.sha256[:12]}{note}")
    print(f"  出处账本：{relative_to_root(result.ledger, project_root)}")
    print(f"  落盘目录：{relative_to_root(output_dir_for(project_root, 'scene') / 'assets' / 'textures', project_root)}")
    return 0


def _build_command(args: argparse.Namespace) -> int:
    project_root = Path(args.root).expanduser()
    final = bool(args.final) and not bool(args.preview)
    result = build_run(
        project_root,
        spec_path=_spec_path(args, project_root),
        final=final,
        allow_final=args.allow_final,
        blender=args.blender,
        timeout=args.timeout,
    )
    spec = load_spec(result.plan.spec_path)
    print(f"场景构建：{spec.meta.title}（{'正式' if final else '预览'}档）")
    print(f"  发射脚本：{relative_to_root(result.plan.script, project_root)}")
    print(f"  Blender：{result.plan.blender}")
    print(f"  日志：{relative_to_root(result.log, project_root)}")
    print(f"  来源文件：{relative_to_root(result.plan.blend, project_root)}")
    print(f"  指纹：{result.fingerprint.digest}（规格 {result.fingerprint.spec_sha256}）")
    for line in check_report_lines(spec, result.findings, project_root):
        print(line)
    return 2 if result.fatal else 0


def _check_command(args: argparse.Namespace) -> int:
    project_root = Path(args.root).expanduser()
    spec = load_spec(_spec_path(args, project_root))
    findings = check_scene(spec, project_root)
    for line in check_report_lines(spec, findings, project_root):
        print(line)
    return 2 if any(finding.fatal for finding in findings) else 0


def _report_command(args: argparse.Namespace) -> int:
    project_root = Path(args.root).expanduser()
    spec = load_spec(_spec_path(args, project_root))
    for line in layout_report_lines(spec):
        print(line)
    return 0


def _spec_path(args: argparse.Namespace, project_root: Path) -> Path:
    if getattr(args, "spec", None):
        return Path(args.spec).expanduser()
    from .build import default_spec_path

    return default_spec_path(project_root)


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。"""
    parser = argparse.ArgumentParser(
        prog=PROG,
        description=(
            "Turn a declarative scene spec into a Blender scene and render it headlessly. "
            "fetch is the only step that touches the network; build, check and report are "
            "deterministic. Everything is written under <root>/_primer/scene/."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    commands = parser.add_subparsers(dest="command", required=True)

    fetch = commands.add_parser(
        "fetch",
        help="download and verify the textures the spec declares, and write the credits ledger",
        description=FETCH_SUMMARY,
        epilog=(
            "examples:\n"
            f"  {PROG} fetch --root ~/Documents/kimi/Workspaces/行星探测工程\n"
            f"  {PROG} fetch --root . --force\n"
            "files land in <root>/_primer/scene/assets/textures/ and the provenance is "
            "recorded in <root>/_primer/scene/assets/credits.yaml; re-running is idempotent"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    fetch.add_argument("--root", default=".", metavar="DIR", help="project root (default .)")
    fetch.add_argument("--spec", metavar="FILE", help="scene spec; defaults to _primer/scene/mission_layout.yaml")
    fetch.add_argument("--force", action="store_true", help="download again even if the file matches the ledger")
    fetch.add_argument("--timeout", type=float, default=300.0, metavar="SECONDS", help="HTTP timeout per request (default 300)")
    fetch.set_defaults(handler=_fetch_command)

    build = commands.add_parser(
        "build",
        help="emit the Blender script, run it headlessly, and gate the result",
        description=BUILD_SUMMARY,
        epilog=(
            "examples:\n"
            f"  {PROG} build --preview --root ~/Documents/kimi/Workspaces/行星探测工程\n"
            f"  {PROG} build --final --allow-final --root .\n"
            "the emitted script is written to <root>/_primer/scene/build/scene.py so it can be "
            "read and diffed; the .blend is saved next to it; the render goes to the preview "
            "or final directory named by the spec"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    build.add_argument("--root", default=".", metavar="DIR", help="project root (default .)")
    build.add_argument("--spec", metavar="FILE", help="scene spec; defaults to _primer/scene/mission_layout.yaml")
    mode = build.add_mutually_exclusive_group()
    mode.add_argument("--preview", action="store_true", help="render the low-sample preview (default)")
    mode.add_argument("--final", action="store_true", help="render the final frame (needs --allow-final)")
    build.add_argument(
        "--allow-final",
        action="store_true",
        help="confirm that a human approved the preview; without it --final is refused before Blender starts",
    )
    build.add_argument("--blender", metavar="PATH", help="Blender executable; defaults to $PRIMER_BLENDER or the macOS install")
    build.add_argument("--timeout", type=float, default=3600.0, metavar="SECONDS", help="wall-clock limit for the Blender run (default 3600)")
    build.set_defaults(handler=_build_command)

    check = commands.add_parser(
        "check",
        help="validate the spec and everything the last build produced (writes nothing)",
        description=CHECK_SUMMARY,
        epilog=(
            "examples:\n"
            f"  {PROG} check --root ~/Documents/kimi/Workspaces/行星探测工程\n"
            f"  {PROG} check --root . --spec _primer/scene/mission_layout.yaml\n"
            "errors block delivery and exit non-zero; warnings (a texture taken at a "
            "different resolution than the spec names, a changed texture) only report"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    check.add_argument("--root", default=".", metavar="DIR", help="project root (default .)")
    check.add_argument("--spec", metavar="FILE", help="scene spec; defaults to _primer/scene/mission_layout.yaml")
    check.set_defaults(handler=_check_command)

    report = commands.add_parser(
        "report",
        help="print the resolved layout numbers (display radii, magnification, node coordinates)",
        description=REPORT_SUMMARY,
        epilog=(
            "examples:\n"
            f"  {PROG} report --root ~/Documents/kimi/Workspaces/行星探测工程\n"
            "the numbers come from the same layout module the emitter consumes, so they are "
            "what the scene was built from"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    report.add_argument("--root", default=".", metavar="DIR", help="project root (default .)")
    report.add_argument("--spec", metavar="FILE", help="scene spec; defaults to _primer/scene/mission_layout.yaml")
    report.set_defaults(handler=_report_command)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """命令行入口。"""
    args = build_parser().parse_args(argv)
    try:
        return args.handler(args)
    except SceneError as exc:
        print(f"scene error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
