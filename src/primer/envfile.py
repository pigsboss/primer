# -*- coding: utf-8 -*-
"""``.env`` 文件的极小加载器：本地 CLI 启动时把密钥注入进程环境。

规则（最小集，够用即可）——每次读一个文件：

* 跳过空行与 ``#`` 注释，容忍 ``export `` 前缀与一层单／双引号；
* 以第一个 ``=`` 切分；变量名必须匹配 ``[A-Za-z_][A-Za-z0-9_]*``，
  不匹配的行只记行号（``ignored_lines``），**不记录内容**；
* **已存在于 os.environ 的变量不覆盖**（真实环境优先），计入 ``skipped``；
* 加载报告只含变量名与行号——任何值都不落日志、不进输出。

发现顺序（:func:`discover_env_file`，:func:`env_file_candidates` 给出同一份清单）：

1. 显式路径（``--env-file`` 这类）；
2. ``<cwd>/.env``——当前工程自己的那份；
3. 机器级 ``~/.config/primer/keys.env``（设了 ``XDG_CONFIG_HOME`` 则用
   ``$XDG_CONFIG_HOME/primer/keys.env``）——放**真实密钥**的那份，与
   :mod:`primer.config` 的 ``~/.config/primer/config.yaml`` 同一目录；
4. 包安装位置附近的 ``.env``（editable 安装时即源码仓库根）。

前两级是"工程自带、多半不进版本库的开发值"，第三级是用户自己保管的机器级密钥，
所以第三级的文件额外做一次**权限体检**：同组或他人可读（``mode & 0o077``）时，
在 :attr:`EnvLoadReport.warnings` 里追加一条警告——警告文本只含路径、权限位与
**变量名**，绝不回显任何值。体检只针对这一级（显式与工程级文件可能本来就归团队共享，
权限另说）；判定依据由 :func:`load_env_file` 的 ``warn_permissions`` 参数控制，
``None`` 表示"看路径是不是机器级那份"。
"""

from __future__ import annotations

import os
import re
import stat as stat_module
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional, Sequence

__all__ = [
    "EnvLoadReport",
    "KEYS_ENV_FILENAME",
    "USER_CONFIG_DIRNAME",
    "discover_env_file",
    "env_file_candidates",
    "is_user_level_env_file",
    "load_env_file",
    "permission_warning",
    "user_config_dir",
    "user_env_file",
]

_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# 机器级密钥文件的目录名与文件名，与 primer.config 的机器级配置同目录。
USER_CONFIG_DIRNAME = "primer"
KEYS_ENV_FILENAME = "keys.env"


@dataclass
class EnvLoadReport:
    """一次 .env 加载的结果：注入／跳过的变量名、未识别行号与安全警告（绝不含值）。

    ``warnings`` 是给人看的诊断（英文，见 CODING_STANDARDS v1.1 §2.2），每条只说
    路径、权限位与变量名——它比 ``injected`` 更容易被原样打出来，所以更不能带值。
    """

    path: Path
    injected: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    ignored_lines: list[int] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def user_config_dir(environ: Optional[Mapping[str, str]] = None) -> Path:
    """机器级配置目录 ``$XDG_CONFIG_HOME/primer``，否则 ``~/.config/primer``。

    取法与 :func:`primer.config._machine_config_path` 一致：两份配置（``config.yaml``
    与 ``keys.env``）必须落在同一个目录里，否则"用户级配置在哪"会有两个答案。
    """
    env = os.environ if environ is None else environ
    xdg = (env.get("XDG_CONFIG_HOME") or "").strip()
    if xdg:
        return Path(xdg).expanduser() / USER_CONFIG_DIRNAME
    home = (env.get("HOME") or "").strip()
    base = Path(home).expanduser() if home else Path.home()
    return base / ".config" / USER_CONFIG_DIRNAME


def user_env_file(environ: Optional[Mapping[str, str]] = None) -> Path:
    """机器级密钥文件 ``~/.config/primer/keys.env``。"""
    return user_config_dir(environ) / KEYS_ENV_FILENAME


def is_user_level_env_file(path: Path, environ: Optional[Mapping[str, str]] = None) -> bool:
    """``path`` 是否就是机器级那份密钥文件（同一个文件，允许符号链接之外的写法差异）。"""
    candidate = Path(path).expanduser()
    target = user_env_file(environ)
    return candidate == target or os.path.realpath(candidate) == os.path.realpath(target)


def permission_warning(path: Path, names: Sequence[str], mode: int) -> str:
    """组／他人可读时的警告文本：路径＋权限位＋变量名，**不含值**（英文诊断）。"""
    listed = ", ".join(names) if names else "(no variables)"
    return ("env file %s is readable by group or others (mode 0o%03o); variables: %s; "
            "tighten it with: chmod 600 %s" % (path, mode, listed, path))


def load_env_file(path: Path, *, warn_permissions: Optional[bool] = None,
                  environ: Optional[Mapping[str, str]] = None) -> EnvLoadReport:
    """加载 ``path`` 并注入 ``os.environ``；已存在的变量不覆盖。

    ``path`` 不存在时抛 :class:`FileNotFoundError`，由调用方决定提示方式。
    ``warn_permissions`` 为 ``None`` 时按路径自动判定（机器级密钥文件才体检），
    ``True``／``False`` 可强制开关（测试与"这份也算用户级"的调用方用得上）。
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
    check = is_user_level_env_file(path, environ) if warn_permissions is None else warn_permissions
    if check:
        mode = stat_module.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            report.warnings.append(
                permission_warning(path, report.injected + report.skipped, mode))
    return report


def env_file_candidates(
    *,
    cwd: Optional[Path] = None,
    user_candidates: Optional[Sequence[Path]] = None,
    package_candidates: Optional[Sequence[Path]] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> list[Path]:
    """待查的 ``.env`` 候选，**按发现顺序**列出（不含显式路径那一级）。

    三份 `None` 参数的缺省是"按规矩来"：当前目录、机器级密钥文件、包安装位置附近；
    测试与"我不想碰用户配置"的调用方给空列表即可把那一级摘掉——发现顺序因此是
    **可注入**的，不靠 monkeypatch ``HOME``。
    """
    base = Path(cwd) if cwd is not None else Path.cwd()
    user = [user_env_file(environ)] if user_candidates is None else list(user_candidates)
    package = _package_env_candidates() if package_candidates is None else list(package_candidates)
    return [base / ".env", *user, *package]


def discover_env_file(
    explicit: Optional[Path] = None,
    *,
    cwd: Optional[Path] = None,
    user_candidates: Optional[Sequence[Path]] = None,
    package_candidates: Optional[Sequence[Path]] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> Optional[Path]:
    """找要加载的 ``.env``：显式路径 > 当前目录 > 机器级密钥 > 包安装位置附近。

    显式给路径时原样返回（存在与否交给 :func:`load_env_file` 判定）；
    其余候选只返回**存在**的第一个，都没有时返回 ``None``。
    """
    if explicit is not None:
        return Path(explicit)
    for candidate in env_file_candidates(cwd=cwd, user_candidates=user_candidates,
                                         package_candidates=package_candidates,
                                         environ=environ):
        if candidate.is_file():
            return candidate
    return None


def _package_env_candidates() -> list[Path]:
    """包所在目录上一两级的 ``.env`` 候选（editable 安装时即源码仓库根）。"""
    package_dir = Path(__file__).resolve().parent
    return [parent / ".env" for parent in list(package_dir.parents)[:2]]
