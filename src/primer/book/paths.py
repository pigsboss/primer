# -*- coding: utf-8 -*-
"""输出边界策略在成书包里的入口；实现只有一份，见 :mod:`primer.paths`。

成书与文献、参考文献共用 ``<工程根>/_primer/<功能>`` 与"路径记成相对工程根"这套
约定，策略本体在 :mod:`primer.paths`。本模块只为兼容 ``from .paths import ...``
而保留，新代码请直接 ``from ..paths import ...``。
"""

from __future__ import annotations

from ..paths import (
    FEATURES,
    OUTPUT_DIRNAME,
    PathLike,
    normalize_project_root,
    output_dir_for,
    relative_to_root,
    resolve_project_root,
    strip_root_prefix,
)

__all__ = [
    "FEATURES",
    "OUTPUT_DIRNAME",
    "PathLike",
    "normalize_project_root",
    "output_dir_for",
    "relative_to_root",
    "resolve_project_root",
    "strip_root_prefix",
]
