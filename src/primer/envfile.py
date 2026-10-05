# -*- coding: utf-8 -*-
"""``.env`` 文件的极小加载器：本地 CLI 启动时把密钥注入进程环境。

规则（最小集，够用即可）——每次读一个文件：

* 跳过空行与 ``#`` 注释，容忍 ``export `` 前缀与一层单／双引号；
* 以第一个 ``=`` 切分；变量名必须匹配 ``[A-Za-z_][A-Za-z0-9_]*``，
  不匹配的行只记行号（``ignored_lines``），**不记录内容**；
* **已存在于 os.environ 的变量不覆盖**（真实环境优先），计入 ``skipped``；
* 加载报告只含变量名与行号——任何值都不落日志、不进输出。

发现顺序（:func:`discover_env_file`）：显式路径 > ``./.env`` > 包安装位置附近
（editable 安装时即源码仓库根）的 ``.env``。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

__all__ = ["EnvLoadReport", "discover_env_file", "load_env_file"]

_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@dataclass
class EnvLoadReport:
    """一次 .env 加载的结果：注入／跳过的变量名与未识别行号（绝不含值）。"""

    path: Path
    injected: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    ignored_lines: list[int] = field(default_factory=list)


def load_env_file(path: Path) -> EnvLoadReport:
    """加载 ``path`` 并注入 ``os.environ``；已存在的变量不覆盖。

    ``path`` 不存在时抛 :class:`FileNotFoundError`，由调用方决定提示方式。
    """
    path = Path(path)
    text = path.read_text(encoding="utf-8-sig")
    report = EnvLoadReport(path=path)
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not _KEY_RE.fullmatch(key):
            report.ignored_lines.append(lineno)
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key in os.environ:
            report.skipped.append(key)
        else:
            os.environ[key] = value
            report.injected.append(key)
    return report


def discover_env_file(
    explicit: Optional[Path] = None,
    *,
    cwd: Optional[Path] = None,
    package_candidates: Optional[Sequence[Path]] = None,
) -> Optional[Path]:
    """找要加载的 ``.env``：显式路径 > 当前目录 > 包安装位置附近。

    显式给路径时原样返回（存在与否交给 :func:`load_env_file` 判定）；
    其余候选只返回**存在**的第一个，都没有时返回 ``None``。
    """
    if explicit is not None:
        return Path(explicit)
    base = Path(cwd) if cwd is not None else Path.cwd()
    candidates: list[Path] = [base / ".env"]
    if package_candidates is None:
        package_candidates = _package_env_candidates()
    candidates.extend(package_candidates)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _package_env_candidates() -> list[Path]:
    """包所在目录上一两级的 ``.env`` 候选（editable 安装时即源码仓库根）。"""
    package_dir = Path(__file__).resolve().parent
    return [parent / ".env" for parent in list(package_dir.parents)[:2]]
