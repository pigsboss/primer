# -*- coding: utf-8 -*-
"""批处理运行配置：数据类、YAML 载入与命令行覆盖合并。

配置优先级为：内置默认值 < YAML 文件 < 显式命令行参数。YAML 里的相对路径
一律相对 **YAML 文件自身所在目录** 解析，而不是进程当前工作目录。载入或覆盖
之后都会立刻校验，任何非法字段都抛出消息里点明字段名的 :class:`ValueError`。

``project_root`` 是工程目录（primer 对它只读），``roots`` 不给时就是它；
``output_dir`` 不给时取 ``<project_root>/_primer/literature``。
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any, Mapping, Optional

import yaml

from .paths import normalize_project_root, output_dir_for

# 解析档位与输出格式的允许取值，非法值在载入时即被拒绝。
# 档位词汇沿用 MinerU CLI 自己的 ``--tier`` 取值，不再另立同义词。
TIERS = ("flash", "basic", "standard", "advanced")
OUTPUT_FORMATS = ("zip", "md", "json")
# 模型来源：默认 modelscope，因为 HuggingFace 在受限网络里会先超时再回退。
MODEL_SOURCES = ("auto", "huggingface", "modelscope", "local")
# 单次后端调用的时限预算（秒），按该次调用的页数缩放，下限见 runner.TIMEOUT_FLOOR_SECONDS。
# 2400 s = 40 min：满额块（400 页）实测最慢档位约 0.28 页/s，即 ~24 min，留 1.6 倍余量；
# 单个超大文档自成一"超限块"时按页数等比放大（529 页的 Ice Giants ≈ 53 min，实测 ~31 min）。
# 旧值 3600 s 是拍脑袋的一小时，一次阻塞要赔满一小时才开始二分定位，故下调。
DEFAULT_PARSE_TIMEOUT_SECONDS = 2400.0
# 分块的页数上限：一次后端调用的解析量与"一页一页的墙钟时间"成正比，因此除文件数
# 之外还要卡住累计页数，否则 20 个文件可能就是 2067 页（生产事故里的第一块）。
DEFAULT_CHUNK_MAX_PAGES = 400

_CONFIG_KEYS = (
    "project_root",
    "roots",
    "output_dir",
    "mineru_command",
    "model_source",
    "tier",
    "output_format",
    "ocr_mode",
    "chunk_size",
    "chunk_max_pages",
    "parse_timeout_seconds",
    "keep_model_output",
    "extra_args",
    "tier_rules",
)


@dataclass(frozen=True)
class TierRule:
    """按 glob 命中路径并把该批文件改用指定档位的一条规则。"""

    glob: str
    tier: str


@dataclass(frozen=True)
class RunConfig:
    """一次批量转换运行的完整配置。

    ``project_root`` 是工程目录，primer 只读它；``roots`` 为 ``None`` 时取工程根本
    身，``output_dir`` 为 ``None`` 时取 ``<project_root>/_primer/literature``。
    两个 ``None`` 都在构造时立刻补全，因此实例上的字段永远是最终值（显式给的
    空 ``roots`` 会被校验拒绝，而不是被悄悄替换）。

    ``chunk_size`` / ``chunk_max_pages`` 是分块的双上限（文件数与累计页数），
    ``parse_timeout_seconds`` 是单次后端调用的时限预算，都见各自字段的说明。
    """

    project_root: Path = field(default_factory=Path.cwd)
    roots: Optional[list[Path]] = None
    output_dir: Optional[Path] = None
    mineru_command: str = "mineru-kit"
    model_source: str = "modelscope"
    tier: str = "standard"
    output_format: str = "zip"
    ocr_mode: str = "auto"
    chunk_size: int = 20
    chunk_max_pages: int = DEFAULT_CHUNK_MAX_PAGES
    parse_timeout_seconds: float = DEFAULT_PARSE_TIMEOUT_SECONDS
    keep_model_output: bool = False
    extra_args: list[str] = field(default_factory=list)
    tier_rules: list[TierRule] = field(default_factory=list)

    def __post_init__(self) -> None:
        root = normalize_project_root(self.project_root)
        object.__setattr__(self, "project_root", root)
        if self.roots is None:
            object.__setattr__(self, "roots", [root])
        if self.output_dir is None:
            object.__setattr__(self, "output_dir", output_dir_for(root, "literature"))

    @property
    def _output_root(self) -> Path:
        """产物根目录；``output_dir`` 为 ``None`` 时按工程根推导。"""
        if self.output_dir is None:
            return output_dir_for(self.project_root, "literature")
        return Path(self.output_dir)

    @property
    def state_path(self) -> Path:
        """断点续跑账本文件。"""
        return self._output_root / "state.jsonl"

    @property
    def staging_dir(self) -> Path:
        """扁平暂存目录（硬链接，保证文件名唯一）。"""
        return self._output_root / "staging"

    @property
    def raw_dir(self) -> Path:
        """MinerU 原始输出目录。"""
        return self._output_root / "raw"

    @property
    def flat_dir(self) -> Path:
        """整理后的扁平输出目录。"""
        return self._output_root / "flat"

    @property
    def index_path(self) -> Path:
        """语料索引（JSON）。"""
        return self._output_root / "index.json"


def validate(config: RunConfig) -> RunConfig:
    """校验配置，返回其本身；非法时抛出点明字段名的 :class:`ValueError`。"""
    if not config.roots:
        raise ValueError("roots must not be empty")
    if not Path(config.project_root).is_dir():
        raise ValueError(f"project_root must be an existing directory: {config.project_root}")
    for root in config.roots:
        if not Path(root).is_dir():
            raise ValueError(f"roots must all be existing directories: {root}")
    if isinstance(config.chunk_size, bool) or not isinstance(config.chunk_size, int):
        raise ValueError("chunk_size must be an integer")
    if config.chunk_size < 1:
        raise ValueError("chunk_size must be >= 1")
    if isinstance(config.chunk_max_pages, bool) or not isinstance(config.chunk_max_pages, int):
        raise ValueError("chunk_max_pages must be an integer")
    if config.chunk_max_pages < 1:
        raise ValueError("chunk_max_pages must be >= 1")
    if isinstance(config.parse_timeout_seconds, bool) or not isinstance(
        config.parse_timeout_seconds, (int, float)
    ):
        raise ValueError("parse_timeout_seconds must be a number")
    if config.parse_timeout_seconds <= 0:
        raise ValueError("parse_timeout_seconds must be > 0")
    if config.tier not in TIERS:
        raise ValueError(f"tier must be one of: {', '.join(TIERS)}")
    if config.output_format not in OUTPUT_FORMATS:
        raise ValueError(f"output_format must be one of: {', '.join(OUTPUT_FORMATS)}")
    if not isinstance(config.mineru_command, str) or not config.mineru_command.strip():
        raise ValueError("mineru_command must be a non-empty string")
    if not isinstance(config.model_source, str) or config.model_source not in MODEL_SOURCES:
        raise ValueError(f"model_source must be one of: {', '.join(MODEL_SOURCES)}")
    if not isinstance(config.keep_model_output, bool):
        raise ValueError("keep_model_output must be a boolean")
    for index, rule in enumerate(config.tier_rules):
        if rule.tier not in TIERS:
            raise ValueError(
                f"tier_rules[{index}].tier must be one of: {', '.join(TIERS)}"
            )
    return config


def load_config(path: Optional[Path] = None) -> RunConfig:
    """载入配置：``path`` 为 ``None`` 时返回内置默认值。

    YAML 中的 ``project_root``、``roots``、``output_dir`` 若为相对路径，按该
    YAML 文件所在目录解析。``project_root`` 不给时取 YAML 文件所在目录——配置文件
    通常就放在工程根上，这样``roots`` 与产物目录都能直接由它推导。
    """
    if path is None:
        return validate(RunConfig())

    config_path = Path(path).expanduser().resolve()
    if not config_path.is_file():
        raise ValueError(f"config file not found: {config_path}")
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"config is not valid YAML: {exc}") from exc
    if raw is None:
        raise ValueError(f"config is empty: {config_path}")
    if not isinstance(raw, dict):
        raise ValueError("config must be a mapping")

    unknown = sorted(set(raw) - set(_CONFIG_KEYS))
    if unknown:
        raise ValueError(f"config has unknown key(s): {', '.join(unknown)}")

    base = config_path.parent
    values: dict[str, Any] = {"project_root": base}
    if "project_root" in raw:
        values["project_root"] = _parse_path(raw["project_root"], base, "project_root")
    values["roots"] = _parse_roots(raw["roots"], base) if "roots" in raw else None
    values["output_dir"] = (
        _parse_path(raw["output_dir"], base, "output_dir") if "output_dir" in raw else None
    )
    if "mineru_command" in raw:
        values["mineru_command"] = str(raw["mineru_command"])
    if "model_source" in raw:
        values["model_source"] = str(raw["model_source"])
    if "tier" in raw:
        values["tier"] = str(raw["tier"])
    if "output_format" in raw:
        values["output_format"] = str(raw["output_format"])
    if "ocr_mode" in raw:
        values["ocr_mode"] = str(raw["ocr_mode"])
    if "chunk_size" in raw:
        values["chunk_size"] = raw["chunk_size"]
    if "chunk_max_pages" in raw:
        values["chunk_max_pages"] = raw["chunk_max_pages"]
    if "parse_timeout_seconds" in raw:
        values["parse_timeout_seconds"] = raw["parse_timeout_seconds"]
    if "keep_model_output" in raw:
        values["keep_model_output"] = raw["keep_model_output"]
    if "extra_args" in raw:
        values["extra_args"] = _parse_string_list(raw["extra_args"], "extra_args")
    if "tier_rules" in raw:
        values["tier_rules"] = _parse_tier_rules(raw["tier_rules"])

    return validate(replace(RunConfig(), **values))


def apply_overrides(config: RunConfig, **overrides: Any) -> RunConfig:
    """把显式参数（命令行）叠加到配置上并重新校验。

    ``None`` 值表示"未提供"，会被忽略；路径类字段相对当前工作目录解析。给了新的
    ``project_root`` 而没给 ``roots`` / ``output_dir`` 时，两者都按新工程根重新推
    导，而不是沿用旧值——命令行里"--project-root 但不给 --root"就是"扫描工程根"。
    """
    known = {item.name for item in fields(RunConfig)}
    unknown = sorted(set(overrides) - known)
    if unknown:
        raise ValueError(f"unknown override(s): {', '.join(unknown)}")

    values = {key: value for key, value in overrides.items() if value is not None}
    if "roots" in values:
        values["roots"] = [Path(item).expanduser().resolve() for item in values["roots"]]
    if "output_dir" in values:
        values["output_dir"] = Path(values["output_dir"]).expanduser().resolve()
    if "project_root" in values:
        values["project_root"] = normalize_project_root(values["project_root"])
        if "roots" not in values:
            values["roots"] = None
        if "output_dir" not in values:
            values["output_dir"] = None
    return validate(replace(config, **values))


def _parse_roots(value: Any, base: Path) -> list[Path]:
    if not isinstance(value, list) or not value:
        raise ValueError("roots must be a non-empty list of paths")
    return [_parse_path(item, base, "roots") for item in value]


def _parse_path(value: Any, base: Path, field_name: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty path string")
    raw = Path(value).expanduser()
    return raw.resolve() if raw.is_absolute() else (base / raw).resolve()


def _parse_string_list(value: Any, field_name: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{field_name} must be a list of strings")
    return list(value)


def _parse_tier_rules(value: Any) -> list[TierRule]:
    if not isinstance(value, list):
        raise ValueError("tier_rules must be a list")
    rules = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise ValueError(f"tier_rules[{index}] must be a mapping")
        unknown = sorted(set(item) - {"glob", "tier"})
        if unknown:
            raise ValueError(f"tier_rules[{index}] has unknown key(s): {', '.join(unknown)}")
        glob = item.get("glob")
        tier = item.get("tier")
        if not isinstance(glob, str) or not glob:
            raise ValueError(f"tier_rules[{index}].glob must be a non-empty string")
        if not isinstance(tier, str) or not tier:
            raise ValueError(f"tier_rules[{index}].tier must be a non-empty string")
        rules.append(TierRule(glob=glob, tier=tier))
    return rules
