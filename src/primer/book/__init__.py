# -*- coding: utf-8 -*-
"""primer.book —— 清单驱动的"markdown 装配 → LaTeX → PDF"成书构建器。

对外接口：

* :func:`primer.book.load_manifest` / :class:`primer.book.BookManifest`：读入并校验清单；
* :class:`primer.book.BookBuilder`：按清单装配、编译成书；
* :class:`primer.book.Bibliography`：参考文献库；
* :mod:`primer.book.checks`：``check --deep`` 的确定性质检（纯文本层，不看模型）。

命令行：``python -m primer.book build <manifest> [--tex-only] [--out-dir DIR]``、
``python -m primer.book check <manifest> [--deep]``、
``python -m primer.book inspect <manifest>``（后者把已排好的 PDF 渲染成页面图，
交给多模态模型挑排版缺陷）。
"""

from .builder import BookBuilder, BuildError, BuildResult, VolumeText
from .manifest import BookManifest, ManifestError, load_manifest
from .references import Bibliography

__all__ = [
    "Bibliography",
    "BookBuilder",
    "BookManifest",
    "BuildError",
    "BuildResult",
    "ManifestError",
    "VolumeText",
    "load_manifest",
]
