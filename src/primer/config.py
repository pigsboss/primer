# -*- coding: utf-8 -*-
"""工作区端点配置：provider 表 + 角色表，密钥只从环境变量读。

成书的多模态质检、论断核验与幻灯片选点各要一个模型，端点却一度散在两处——一处是
书稿清单的 ``vision:`` 块，一处是命令行默认值。本模块是读配置的唯一入口：端点与模型
放**工作区文件**，密钥只从**环境变量**读（不读任何凭据文件、不写日志、不进产物），
与 aider 的机制相同。

四层，优先级从低到高：

1. 内置默认：provider 表里只有 ``deepseek`` 一项，只有 ``base_url``。未核实过的端点
   一律不硬编码——让用户写在配置里，比把一个道听途说的 base_url 当默认值便宜得多。
2. ``~/.config/primer/config.yaml``（设了 ``XDG_CONFIG_HOME`` 则用
   ``$XDG_CONFIG_HOME/primer/config.yaml``）：机器级，多台机器共用的那部分。
3. ``<project_root>/_primer/config.yaml``：项目级。
4. ``--config <file>`` 显式指定的文件（给了就必须存在，否则报错）。

合并是**按键深合并**：后一层覆盖前一层同名键，但不整块替换，于是项目级只改
``roles.select.model`` 时不必重抄 provider 表。第五层是命令行标量覆盖，本模块把它做成
:func:`resolve_role` 的关键字参数，由调用方传入。

本工程的密钥命名约定
--------------------

每个 provider 一把钥匙，环境变量名是 ``PRIMER_<PROVIDER>_API_KEY``：provider 名转大写、
非字母数字换成下划线（``deepseek`` → ``PRIMER_DEEPSEEK_API_KEY``，``moonshot`` →
``PRIMER_MOONSHOT_API_KEY``）。provider 没写 ``key_env`` 时就按这条约定派生，所以配置里
只有要破例的 provider 才写它；写了就以写的为准。真正要读的那个变量名一路带到
:class:`Provider` 与 :class:`Endpoint` 上，缺密钥时报错点名的就是它。密钥本身只存在于
环境里——本模块记的、报的、打印的，永远是变量名。

认得的键
--------

``providers.<name>`` 认 ``base_url``、``key_env``、``temperature``；``roles.<name>`` 认
``provider``、``model``、``max_tokens``、``batch_size``、``temperature``。未知键一律忽略
（为将来留位置），但**值类型不对要报错**，消息点名是哪一层（给文件路径）、哪个键、当前
是什么类型。语言照 ``docs/guides/CODING_STANDARDS.md`` v1.1 §2.2 分成两条线：诊断与
异常消息（走 stderr）一律英文；:func:`show_report` 是人读报告散文，用中文。

``temperature`` 是**端点级事实**而不是偏好：有的端点只接受一个值（Kimi Code 的
``https://api.kimi.com/coding/v1`` 上所有模型只接受 ``1``，给 ``0`` 会被 400 拒掉），
所以它写在 provider 上；某个 role 确有例外时可以在 role 上覆盖 provider 的取值。两边都
没写就是 ``None``，由调用方兜底自己的默认值。

密钥的取用只有 :func:`api_key` 一处：只读给定的环境映射，缺变量就报错并**点名缺的是
哪个变量**，绝不回显密钥值。:func:`show_report` 与命令行 ``--show`` 同理——只报"密钥
在不在环境里"，任何情况下不打印密钥本身。
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import yaml

from .paths import OUTPUT_DIRNAME, normalize_project_root

__all__ = [
    "BUILTIN_PROVIDERS",
    "CONFIG_DIRNAME",
    "CONFIG_FILENAME",
    "Config",
    "ConfigError",
    "Endpoint",
    "Provider",
    "Role",
    "api_key",
    "load_config",
    "main",
    "resolve_role",
    "show_report",
]

# 项目级配置的位置：``<工程根>/_primer/config.yaml``。目录名取自 primer.paths 的
# OUTPUT_DIRNAME——primer 在工程里只认这一个目录，配置与产物都落在它下面。
CONFIG_DIRNAME = OUTPUT_DIRNAME
CONFIG_FILENAME = "config.yaml"
MACHINE_DIRNAME = "primer"

# 派生密钥变量名用的前后缀，见模块文档的"命名约定"。
KEY_ENV_PREFIX = "PRIMER_"
KEY_ENV_SUFFIX = "_API_KEY"

# 内置 provider 表。只有 deepseek 一项，只有一个 base_url；key_env 按命名约定派生。
BUILTIN_PROVIDERS: Mapping[str, Mapping[str, str]] = {
    "deepseek": {
        "base_url": "https://api.deepseek.com",
    },
}


class ConfigError(Exception):
    """配置文件缺失或非法、provider/role 解析不出来、密钥不在环境里。"""


@dataclass(frozen=True)
class Provider:
    """一个端点：地址、密钥所在的环境变量名（不是密钥）、以及端点级的说话温度。"""

    name: str
    base_url: str = ""
    key_env: str = ""
    temperature: Optional[float] = None


@dataclass(frozen=True)
class Role:
    """一个用途（select／distill／…）点名要用哪个 provider 与哪个模型。

    五个字段都可缺：缺 ``provider``／``model`` 时由 :func:`resolve_role` 的命令行覆盖
    补上，补不全就报错；``max_tokens``／``batch_size`` 缺省即 ``None``，由调用方自己
    决定兜底值；``temperature`` 没写就跟着 provider 走（见模块文档）。
    """

    name: str
    provider: Optional[str] = None
    model: Optional[str] = None
    max_tokens: Optional[int] = None
    batch_size: Optional[int] = None
    temperature: Optional[float] = None


@dataclass(frozen=True)
class Endpoint:
    """一个角色解析到底之后的端点：调用方拿它建客户端、取密钥。"""

    role: str
    provider: str
    base_url: str
    model: str
    key_env: str
    max_tokens: Optional[int] = None
    batch_size: Optional[int] = None
    temperature: Optional[float] = None


@dataclass(frozen=True)
class Config:
    """合并后的配置。

    ``files`` 是真正读到的那几层文件，按优先级从低到高——不存在的层不出现在这里，
    因此它同时是"哪些层生效了"的账目。``origins`` 记每个叶子键最后由哪个文件写定，
    出问题时用来把消息指到具体文件；内置默认的 origin 是 ``None``。
    """

    project_root: Path = field(default_factory=Path.cwd)
    providers: Mapping[str, Provider] = field(default_factory=dict)
    roles: Mapping[str, Role] = field(default_factory=dict)
    files: Tuple[Path, ...] = ()
    origins: Mapping[Tuple[str, ...], Optional[Path]] = field(default_factory=dict)


# ---------------------------------------------------------------- 读配置


def load_config(
    project_root: Any,
    config_path: Any = None,
    *,
    environ: Optional[Mapping[str, str]] = None,
) -> Config:
    """读内置默认、机器级、项目级与显式文件四层，深合并成一个 :class:`Config`。

    ``config_path`` 给了就必须存在，否则报错；机器级与项目级不存在就跳过（不是错误）。
    ``environ`` 只用来定位 ``XDG_CONFIG_HOME``／``HOME``，注入它是为了让调用方（尤其
    测试）不碰进程的真实环境。
    """
    env: Mapping[str, str] = os.environ if environ is None else environ
    root = normalize_project_root(project_root)

    merged: Dict[str, Any] = {}
    origins: Dict[Tuple[str, ...], Optional[Path]] = {}
    _merge_into(merged, {"providers": BUILTIN_PROVIDERS}, None, origins)

    files: List[Path] = []
    layers: List[Path] = [
        _machine_config_path(env),
        root / CONFIG_DIRNAME / CONFIG_FILENAME,
    ]
    if config_path is not None:
        explicit = Path(config_path).expanduser()
        if not explicit.is_file():
            raise ConfigError(
                f"config file {explicit} was named with --config but does not exist"
            )
        layers.append(explicit)

    for layer in layers:
        if not layer.is_file():
            continue
        document = _read_layer(layer)
        _merge_into(merged, document, layer, origins)
        files.append(layer)

    return _build(root, merged, origins, tuple(files))


def _read_layer(path: Path) -> Mapping[str, Any]:
    """读一层配置文件并**就地**校验它的取值类型（消息里带这个文件的路径）。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"config file {path} cannot be read: {exc}") from exc
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"config file {path} is not valid YAML: {exc}") from exc
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ConfigError(
            f"config file {path} must be a mapping at the top level, got {_type_name(raw)}"
        )
    _validate_providers(raw.get("providers"), path)
    _validate_roles(raw.get("roles"), path)
    return raw


def _validate_providers(section: Any, path: Path) -> None:
    """provider 表：名字要是可用的（能派生出变量名），值只认三个键。"""
    if section is None:
        return
    if not isinstance(section, Mapping):
        raise ConfigError(
            f"`providers` in {path} must be a mapping, got {_type_name(section)}"
        )
    for name, block in section.items():
        _require_name(name, f"`providers` in {path}")
        if not isinstance(block, Mapping):
            raise ConfigError(
                f"`providers.{name}` in {path} must be a mapping, got {_type_name(block)}"
            )
        if not _derive_stem(name):
            raise ConfigError(
                f"`providers.{name}` in {path}: the name has no letters or digits, so no key "
                "environment variable can be derived from it; rename the provider "
                f"({KEY_ENV_PREFIX}<PROVIDER>{KEY_ENV_SUFFIX})"
            )
        for key, value in block.items():
            if key in ("base_url", "key_env"):
                _require_text(value, f"`providers.{name}.{key}` in {path}")
            elif key == "temperature":
                _require_number(value, f"`providers.{name}.temperature` in {path}")


def _validate_roles(section: Any, path: Path) -> None:
    """角色表：只校验类型，完整性留给解析时（高一层或命令行可以补全）。"""
    if section is None:
        return
    if not isinstance(section, Mapping):
        raise ConfigError(
            f"`roles` in {path} must be a mapping, got {_type_name(section)}"
        )
    for name, block in section.items():
        _require_name(name, f"`roles` in {path}")
        if not isinstance(block, Mapping):
            raise ConfigError(
                f"`roles.{name}` in {path} must be a mapping, got {_type_name(block)}"
            )
        for key, value in block.items():
            if key in ("provider", "model"):
                _require_text(value, f"`roles.{name}.{key}` in {path}")
            elif key in ("max_tokens", "batch_size"):
                _require_positive_int(value, f"`roles.{name}.{key}` in {path}")
            elif key == "temperature":
                _require_number(value, f"`roles.{name}.temperature` in {path}")


def _merge_into(
    base: Dict[str, Any],
    overlay: Mapping[str, Any],
    origin: Optional[Path],
    origins: Dict[Tuple[str, ...], Optional[Path]],
    path: Tuple[str, ...] = (),
) -> None:
    """按键深合并 ``overlay`` 到 ``base``，并记下每个叶子键最后由谁写定。

    两边同名键都是映射时继续往里合；否则后到的值整个替换掉先前的值（包括"用一个映射
    换掉一个标量"的情形，此时映射里的每个叶子都归这一层）。新造出来的块本身也记一笔
    来源：块里某个键一层都没写过时（比如 provider 只写了 base_url、key_env 靠派生），
    报错要指向**写这个块的那一层**，而不是最高一层。
    """
    for key, value in overlay.items():
        here = path + (str(key),)
        child = base.get(key)
        if isinstance(value, Mapping):
            if isinstance(child, Mapping):
                _merge_into(child, value, origin, origins, here)
            else:
                fresh: Dict[str, Any] = {}
                base[key] = fresh
                origins[here] = origin
                _merge_into(fresh, value, origin, origins, here)
        else:
            base[key] = value
            origins[here] = origin


def _build(
    root: Path,
    merged: Mapping[str, Any],
    origins: Mapping[Tuple[str, ...], Optional[Path]],
    files: Tuple[Path, ...],
) -> Config:
    """把合并后的原始映射收成 :class:`Provider` 与 :class:`Role`（未知键就此丢掉）。

    派生只在这里做一次：没写 ``key_env`` 的 provider 拿到按约定算出的变量名，于是下游
    （:func:`resolve_role`、:func:`api_key`、:func:`show_report`）看到的永远是"实际会去
    读的那个名字"，不区分它是写来的还是算来的。
    """
    providers: Dict[str, Provider] = {}
    for name, block in _section(merged.get("providers")).items():
        providers[name] = Provider(
            name=name,
            base_url=block.get("base_url") or "",
            key_env=block.get("key_env") or _derive_key_env(name),
            temperature=block.get("temperature"),
        )
    roles: Dict[str, Role] = {}
    for name, block in _section(merged.get("roles")).items():
        roles[name] = Role(
            name=name,
            provider=block.get("provider"),
            model=block.get("model"),
            max_tokens=block.get("max_tokens"),
            batch_size=block.get("batch_size"),
            temperature=block.get("temperature"),
        )
    return Config(
        project_root=root, providers=providers, roles=roles, files=files, origins=origins
    )


def _section(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _derive_key_env(provider_name: str) -> str:
    """provider 名 → 约定的密钥环境变量名 ``PRIMER_<NAME>_API_KEY``（见模块文档）。"""
    return f"{KEY_ENV_PREFIX}{_derive_stem(provider_name)}{KEY_ENV_SUFFIX}"


def _derive_stem(provider_name: str) -> str:
    """约定里夹在前后缀之间的那段：大写，非字母数字一律换成下划线，首尾下划线去掉。"""
    return re.sub(r"[^A-Za-z0-9]+", "_", provider_name).strip("_").upper()


def _machine_config_path(environ: Mapping[str, str]) -> Path:
    """机器级配置的路径：``$XDG_CONFIG_HOME/primer/config.yaml``，否则 ``~/.config/…``。"""
    xdg = (environ.get("XDG_CONFIG_HOME") or "").strip()
    if xdg:
        return Path(xdg).expanduser() / MACHINE_DIRNAME / CONFIG_FILENAME
    home = (environ.get("HOME") or "").strip()
    base = Path(home).expanduser() if home else Path.home()
    return base / ".config" / MACHINE_DIRNAME / CONFIG_FILENAME


# ---------------------------------------------------------------- 角色 → 端点


def resolve_role(
    config: Config,
    role: str,
    *,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    base_url: Optional[str] = None,
    key_env: Optional[str] = None,
    max_tokens: Optional[int] = None,
) -> Endpoint:
    """把一个角色解析成生效的端点。

    覆盖值（命令行给的）优先于配置文件；``max_tokens``、``batch_size`` 与 ``temperature``
    取自角色定义，其中 ``temperature`` 是"role 覆盖 provider"，两边都没写就是 ``None``。
    角色不存在、或 provider／model 缺一个又补不上，都在这里报错，消息带上是哪个文件该改。
    """
    _check_overrides(
        provider=provider, model=model, base_url=base_url, key_env=key_env, max_tokens=max_tokens
    )
    defined = config.roles.get(role)
    target = _target_file(config)

    provider_name = _first_text(provider, defined.provider if defined else None)
    if provider_name is None:
        if defined is None:
            raise ConfigError(
                f"role '{role}' is not defined: add it under `roles` in {target} "
                "(provider and model are both required), or pass --provider and --model"
            )
        raise ConfigError(
            f"role '{role}' has no provider: add `provider` under `roles.{role}` in {target}, "
            "or pass --provider"
        )

    entry = config.providers.get(provider_name)
    if entry is None:
        raise ConfigError(
            f"provider '{provider_name}' is not defined: add base_url and key_env under "
            f"`providers` in {target}"
        )

    effective_base_url = _first_text(base_url, entry.base_url)
    if effective_base_url is None:
        raise ConfigError(
            f"provider '{provider_name}' has no base_url: add `base_url` under "
            f"`providers.{provider_name}` in "
            f"{_origin_of(config, target, 'providers', provider_name, 'base_url')}, "
            "or pass --base-url"
        )

    effective_model = _first_text(model, defined.model if defined else None)
    if effective_model is None:
        if defined is None:
            raise ConfigError(
                f"role '{role}' is not defined: pass --provider and --model, or add it under "
                f"`roles` in {target}"
            )
        raise ConfigError(
            f"role '{role}' has no model: add `model` under `roles.{role}` in {target}, "
            "or pass --model"
        )

    effective_key_env = _first_text(key_env, entry.key_env)
    if effective_key_env is None:
        raise ConfigError(
            f"provider '{provider_name}' has no key_env: add `key_env` under "
            f"`providers.{provider_name}` in "
            f"{_origin_of(config, target, 'providers', provider_name, 'key_env')} "
            "(an environment variable name, never the key itself)"
        )

    effective_max_tokens = max_tokens
    if effective_max_tokens is None and defined is not None:
        effective_max_tokens = defined.max_tokens

    effective_temperature = None
    if defined is not None:
        effective_temperature = defined.temperature
    if effective_temperature is None:
        effective_temperature = entry.temperature

    return Endpoint(
        role=role,
        provider=provider_name,
        base_url=effective_base_url,
        model=effective_model,
        key_env=effective_key_env,
        max_tokens=effective_max_tokens,
        batch_size=defined.batch_size if defined is not None else None,
        temperature=effective_temperature,
    )


def api_key(endpoint: Endpoint, environ: Mapping[str, str] = os.environ) -> str:
    """从环境变量取密钥；缺失就报错，消息点名缺哪个变量，且**不回显密钥值**。"""
    name = (endpoint.key_env or "").strip()
    if not name:
        raise ConfigError(
            "this endpoint has no key_env: the provider it came from must name the "
            "environment variable holding the key (never the key itself)"
        )
    value = environ.get(name)
    if value is None or not value.strip():
        raise ConfigError(
            f"environment variable {name} is not set: the endpoint's key_env names it; "
            "export it in your shell"
        )
    return value


# ---------------------------------------------------------------- 自检报告


def show_report(
    config: Config, environ: Optional[Mapping[str, str]] = None
) -> Sequence[str]:
    """``--show`` 用的只读报告行：配置来源、每个 provider、每个 role。

    这是**人读报告散文**，照 ``docs/guides/CODING_STANDARDS.md`` v1.1 §2.2 用中文，与它
    服务的成书/幻灯片报告同语种；技术标识符保持原样——provider 名、role 名、``base_url=``
    / ``key_env=`` 这类键、模型名、环境变量名与路径都是英文。诊断与异常仍走英文
    （stderr），两条线不混。

    provider 一行报 base_url、key_env 名与"密钥在不在环境里"、以及端点级的 temperature；
    role 一行报 provider、model 与各旋钮（指着一个没定义的 provider 时多一句标记，这种
    错误在解析时才会炸，自检就该先看见）。任何情况下不打印密钥——这里只碰环境变量的
    **名字**与是否存在。
    """
    env: Mapping[str, str] = os.environ if environ is None else environ
    lines: List[str] = ["primer 端点配置（低 → 高）"]
    builtin = ", ".join(sorted(BUILTIN_PROVIDERS))
    lines.append(f"  [内置默认] providers: {builtin}")
    for path in config.files:
        lines.append(f"  {path}")
    if not config.files:
        lines.append(
            "  （没有读到任何配置文件；只有内置默认与命令行覆盖生效）"
        )

    lines.extend(["", "端点"])
    if not config.providers:
        lines.append("  （无）")
    for name in sorted(config.providers):
        entry = config.providers[name]
        parts = [f"base_url={entry.base_url or '（未设置）'}"]
        if not entry.key_env:
            parts.append("key_env=（未设置）")
        elif _present(env, entry.key_env):
            parts.append(f"key_env={entry.key_env}（在环境里）")
        else:
            parts.append(f"key_env={entry.key_env}（不在环境里）")
        if entry.temperature is not None:
            parts.append(f"temperature={entry.temperature}")
        lines.append(f"  {name}：" + "，".join(parts))

    lines.extend(["", "角色"])
    if not config.roles:
        lines.append("  （无）")
    for name in sorted(config.roles):
        role = config.roles[name]
        undefined = bool(role.provider) and role.provider not in config.providers
        parts = [
            # 自检就该看出来：role 指着的 provider 没定义，解析到它的时候会报错。
            f"provider={role.provider or '（未设置）'}"
            + ("（provider 未定义）" if undefined else ""),
            f"model={role.model or '（未设置）'}",
        ]
        if role.temperature is not None:
            parts.append(f"temperature={role.temperature}")
        if role.max_tokens is not None:
            parts.append(f"max_tokens={role.max_tokens}")
        if role.batch_size is not None:
            parts.append(f"batch_size={role.batch_size}")
        lines.append(f"  {name}：" + "，".join(parts))
    return tuple(lines)


# ---------------------------------------------------------------- 命令行


def main(
    argv: Optional[Sequence[str]] = None, *, environ: Optional[Mapping[str, str]] = None
) -> int:
    """``python3 -m primer.config --show``：只读自检，不联网、不调模型、不写盘。"""
    parser = argparse.ArgumentParser(
        prog="python -m primer.config",
        description="Inspect the workspace endpoint configuration (read-only).",
    )
    parser.add_argument(
        "--show",
        action="store_true",
        help="print the merged configuration and whether each API-key variable is in the environment",
    )
    parser.add_argument(
        "--project-root", metavar="PATH", help="project root (default: current directory)"
    )
    parser.add_argument(
        "--config", metavar="FILE", help="explicit config file, merged on top of the others"
    )
    args = parser.parse_args(argv)
    if not args.show:
        parser.error("nothing to do: pass --show")

    try:
        config = load_config(args.project_root or Path.cwd(), args.config, environ=environ)
    except ConfigError as exc:
        print(f"primer-config: {exc}", file=sys.stderr)
        return 2
    for line in show_report(config, environ=environ):
        print(line)
    return 0


# ---------------------------------------------------------------- 小工具


def _present(environ: Mapping[str, str], name: str) -> bool:
    value = environ.get(name)
    return isinstance(value, str) and bool(value.strip())


def _first_text(*values: Optional[str]) -> Optional[str]:
    """第一个非空字符串；全空（``None`` 或空白）返回 ``None``。"""
    for value in values:
        if isinstance(value, str) and value.strip():
            return value
    return None


def _origin_of(config: Config, fallback: Path, *key: str) -> Path:
    """某个键最后由哪个文件写定；没有就顺着往上找它所在的块，再没有则退回 ``fallback``。

    内置默认记的 origin 是 ``None``，与"没有记录"同样处理——那时该改的是 ``fallback``
    （读到过的最高一层，一层都没有时是项目级那个位置）。
    """
    for length in range(len(key), 0, -1):
        origin = config.origins.get(key[:length])
        if origin is not None:
            return origin
    return fallback


def _target_file(config: Config) -> Path:
    """该改哪个文件：读到过的最高一层；一层都没有时是项目级那个位置。"""
    if config.files:
        return config.files[-1]
    return config.project_root / CONFIG_DIRNAME / CONFIG_FILENAME


def _check_overrides(
    provider: Optional[str],
    model: Optional[str],
    base_url: Optional[str],
    key_env: Optional[str],
    max_tokens: Optional[int],
) -> None:
    """命令行覆盖值的类型检查；消息点名是哪个参数。"""
    for name, value in (
        ("provider", provider),
        ("model", model),
        ("base_url", base_url),
        ("key_env", key_env),
    ):
        if value is not None:
            _require_text(value, f"resolve_role's `{name}` override")
    if max_tokens is not None:
        _require_positive_int(max_tokens, "resolve_role's `max_tokens` override")


def _require_name(value: Any, where: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where} has a key that is not a non-empty string: {value!r}")


def _require_text(value: Any, where: str) -> None:
    if not isinstance(value, str):
        raise ConfigError(f"{where} must be a string, got {_type_name(value)}: {value!r}")
    if not value.strip():
        raise ConfigError(f"{where} must be a non-empty string")


def _require_positive_int(value: Any, where: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{where} must be an integer, got {_type_name(value)}: {value!r}")
    if value < 1:
        raise ConfigError(f"{where} must be >= 1, got {value}")


def _require_number(value: Any, where: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{where} must be a number, got {_type_name(value)}: {value!r}")


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "string"
    if isinstance(value, Mapping):
        return "mapping"
    if isinstance(value, (list, tuple)):
        return "list"
    return type(value).__name__


if __name__ == "__main__":  # pragma: no cover - 由命令行入口走到
    raise SystemExit(main())
