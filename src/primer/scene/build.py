# -*- coding: utf-8 -*-
"""构建：把发射出来的脚本交给无头 Blender 跑，抓日志，再交给验收关卡。

调排版引擎的那一套（``primer.book.makefile.BuildPlan``）在这里换成了"调 Blender"，
但形状是一样的：**命令行只有一处**（:meth:`SceneBuildPlan.commands`），产物目录只有一处
（``_primer/scene/``），失败时给的是日志里的原话而不是一句"构建失败"。

``--final`` 是一道需要人来开的门。工作说明里写得很直白——预览经审查通过才允许升级正式
渲染——所以这里把它变成代码：不带 ``--allow-final`` 时 :func:`run` 直接抛
:class:`SceneError`，连 Blender 都不会被启动。一道只写在文档里的规矩会被忘记，一道写在
代码里的不会。

Blender 不在 ``PATH`` 上，二进制位置按"显式参数 → 环境变量 ``PRIMER_BLENDER`` →
macOS 默认安装路径"三级查找，找不到就报出找过的所有位置。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Mapping, Optional, Sequence, Tuple

from ..paths import output_dir_for, relative_to_root
from .checks import Finding, check_scene, read_report
from .emit import REPORT_NAME, SCRIPT_NAME, write_script
from .fingerprint import Fingerprint, compute_fingerprint
from .spec import SceneError, load_spec

__all__ = [
    "BLENDER_ENV",
    "DEFAULT_BLENDER",
    "DEFAULT_SPEC_NAME",
    "LOG_NAME",
    "BuildError",
    "BuildResult",
    "SceneBuildPlan",
    "default_spec_path",
    "find_blender",
    "log_path",
    "run",
]

# Blender 不在 PATH 上：默认安装路径 + 一个可以覆盖它的环境变量。
BLENDER_ENV = "PRIMER_BLENDER"
DEFAULT_BLENDER = "/Applications/Blender.app/Contents/MacOS/Blender"
# 规格的默认位置：``_primer/scene/`` 下唯一的那份 ``*.yaml``。
DEFAULT_SPEC_NAME = "mission_layout.yaml"
LOG_NAME = "build.log"
FINAL_LOG_NAME = "build-final.log"
FINGERPRINT_NAME = "fingerprint.json"
# 单次构建的墙钟上限：854x480 预览是几十秒的事，10 分钟没回来一定是卡住了。
DEFAULT_TIMEOUT = 3600.0
# Blender 的失败信号：退出码之外还要看日志，因为脚本里的异常有时不影响退出码。
_ERROR_MARKERS = (
    "Traceback (most recent call last)",
    "Error: Python script failed",
    "Error: Script failed to run",
    "Segmentation fault",
)


class BuildError(SceneError):
    """构建失败：Blender 没跑起来，或者跑出来的日志里有错误。"""


@dataclass(frozen=True)
class SceneBuildPlan:
    """一次构建的全部参数（对应 :class:`primer.book.makefile.BuildPlan`）。"""

    project_root: Path
    spec_path: Path
    final: bool = False
    blender: Path = Path(DEFAULT_BLENDER)
    timeout: float = DEFAULT_TIMEOUT

    @property
    def scene_dir(self) -> Path:
        return output_dir_for(self.project_root, "scene")

    @property
    def build_dir(self) -> Path:
        return self.scene_dir / "build"

    @property
    def script(self) -> Path:
        return self.build_dir / SCRIPT_NAME

    @property
    def report(self) -> Path:
        return self.build_dir / REPORT_NAME

    @property
    def fingerprint(self) -> Path:
        return self.build_dir / FINGERPRINT_NAME

    @property
    def blend(self) -> Path:
        return self.scene_dir / f"{self.spec_path.stem}.blend"

    @property
    def log(self) -> Path:
        return self.build_dir / (FINAL_LOG_NAME if self.final else LOG_NAME)

    def commands(self) -> List[Tuple[List[str], Path]]:
        """按顺序执行的命令及其工作目录——只有一条：无头 Blender 跑发射出来的脚本。"""
        return [([str(self.blender), "-b", "-P", str(self.script)], self.project_root)]


@dataclass(frozen=True)
class BuildResult:
    """一次构建的结果：日志、现场报告与验收发现。"""

    plan: SceneBuildPlan
    fingerprint: Fingerprint
    log: Path
    findings: Tuple[Finding, ...]
    report: Optional[Mapping[str, object]]

    @property
    def fatal(self) -> Tuple[Finding, ...]:
        return tuple(finding for finding in self.findings if finding.fatal)


def find_blender(explicit: Optional[str] = None) -> Path:
    """按"显式参数 → ``PRIMER_BLENDER`` → macOS 默认安装路径"查找 Blender 可执行文件。"""
    candidates: List[str] = []
    if explicit:
        candidates.append(explicit)
    from_env = os.environ.get(BLENDER_ENV)
    if from_env:
        candidates.append(from_env)
    candidates.append(DEFAULT_BLENDER)
    for candidate in candidates:
        path = Path(candidate).expanduser()
        if path.is_file() and os.access(str(path), os.X_OK):
            return path
    resolved = [str(Path(item).expanduser()) for item in candidates]
    raise SceneError(
        "blender executable not found; looked at: " + ", ".join(resolved)
        + f" (set {BLENDER_ENV} to override)"
    )


def default_spec_path(project_root: Path) -> Path:
    """规格默认取 ``_primer/scene/`` 下唯一的那份 ``*.yaml``。

    多份就报错并举出全部候选——"随便挑一份"会让图无声地变成另一张。
    """
    scene_dir = output_dir_for(project_root, "scene")
    preferred = scene_dir / DEFAULT_SPEC_NAME
    if preferred.is_file():
        return preferred
    candidates = sorted(path for path in scene_dir.glob("*.yaml") if path.is_file())
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise SceneError(f"no scene spec found in {scene_dir}")
    raise SceneError(
        "several scene specs found, pass --spec: "
        + ", ".join(relative_to_root(path, project_root) for path in candidates)
    )


def log_path(project_root: Path, final: bool = False) -> Path:
    return output_dir_for(project_root, "scene") / "build" / (FINAL_LOG_NAME if final else LOG_NAME)


def _write_log(path: Path, command: Sequence[str], completed: "subprocess.CompletedProcess[str]") -> None:
    """原始日志一字不改地存下来：排查时它比任何摘要都有用。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    header = "$ " + " ".join(command) + f"\n# exit code: {completed.returncode}\n"
    path.write_text(header + (completed.stdout or "") + (completed.stderr or ""), encoding="utf-8")


def _log_tail(text: str, limit: int = 12) -> str:
    lines = [line for line in text.splitlines() if line.strip()]
    return "\n".join(lines[-limit:])


def _scan_for_errors(text: str, context: int = 3) -> List[str]:
    """找出日志里的失败信号，并把紧跟着的几行一起带上。

    只报 "Traceback (most recent call last):" 这一行没有用——原因在下面几行里（``KeyError:
    'color'`` 才是要读的那句）。所以命中信号后连后面 ``context`` 行原文照收。
    """
    lines = text.splitlines()
    found: List[str] = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("Error:") or any(marker in line for marker in _ERROR_MARKERS):
            block = [stripped]
            block.extend(
                following.strip()
                for following in lines[index + 1 : index + 1 + context]
                if following.strip()
            )
            found.append(" / ".join(block))
    return found


def run(
    project_root: Path,
    spec_path: Optional[Path] = None,
    final: bool = False,
    allow_final: bool = False,
    blender: Optional[str] = None,
    timeout: float = DEFAULT_TIMEOUT,
    runner=None,
) -> BuildResult:
    """发射脚本、交给无头 Blender、写日志、跑验收。

    ``runner`` 是给测试用的注入点（测试不启动 Blender）：它收到
    ``(command, cwd, timeout)`` 并返回一个带 ``returncode``/``stdout``/``stderr`` 的对象。
    """
    project_root = Path(project_root)
    if final and not allow_final:
        raise SceneError(
            "refusing to render the final frame: --final needs --allow-final "
            "(previews are reviewed first; the final render is a human decision)"
        )
    resolved_spec = Path(spec_path) if spec_path else default_spec_path(project_root)
    spec = load_spec(resolved_spec)
    binary = find_blender(blender)
    plan = SceneBuildPlan(
        project_root=project_root, spec_path=resolved_spec, final=final, blender=binary, timeout=timeout
    )

    fingerprint = compute_fingerprint(spec, project_root)
    plan.build_dir.mkdir(parents=True, exist_ok=True)
    write_script(spec, project_root, fingerprint, plan.script, final=final)
    plan.fingerprint.write_text(
        json.dumps(fingerprint.as_mapping(), ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )

    command, cwd = plan.commands()[0]
    if runner is None:
        print(f"scene: running {' '.join(command)}", file=sys.stderr)
    execute = runner or _subprocess_runner
    completed = execute(command, cwd, plan.timeout)
    _write_log(plan.log, command, completed)

    combined = (completed.stdout or "") + (completed.stderr or "")
    if completed.returncode != 0:
        raise BuildError(
            f"blender exited with code {completed.returncode}; see "
            f"{relative_to_root(plan.log, project_root)}:\n{_log_tail(combined)}"
        )
    errors = _scan_for_errors(combined)
    if errors:
        detail = "\n".join(errors[:5])
        raise BuildError(
            f"blender reported errors; see {relative_to_root(plan.log, project_root)}:\n{detail}"
        )

    findings = tuple(check_scene(spec, project_root))
    return BuildResult(
        plan=plan,
        fingerprint=fingerprint,
        log=plan.log,
        findings=findings,
        report=read_report(project_root),
    )


def _subprocess_runner(command: Sequence[str], cwd: Path, timeout: float):
    return subprocess.run(
        list(command),
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
