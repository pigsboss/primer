# -*- coding: utf-8 -*-
"""输出边界策略在文献包里的入口；实现只有一份，见 :mod:`primer.paths`。

本模块只为兼容既有导入而保留——文献包与参考文献包历来从这里取策略，改成再导出
之后调用方一行都不用动。新代码请直接 ``from primer.paths import ...``。
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
