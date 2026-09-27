# -*- coding: utf-8 -*-
"""poppler 命令行工具的薄封装：页数、每页旋转、页面文本、单页光栅化。

成书校对要"看见"版面，只有 LaTeX 日志不够。这里把三件 poppler 工具收进一处：

* ``pdfinfo``：总页数与每页的 ``/Rotate``——横向页是 ``pdflscape`` 转的页面，
  旋转角非零即可判定，不必看页面内容；
* ``pdftotext -layout``：按页切分的文本（``\\f`` 分页），用来把发现清单里的表格
  定位到页码、给人读的页选择理由；
* ``pdftoppm``：单页 PNG，供多模态模型看。

工具缺失、调用失败一律抛 :class:`PdfToolError`（消息英文），由调用方落成发现。
一次只渲染一页：选择性渲染省字节，比整段渲染再丢弃划算。
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Sequence

PDFINFO = "pdfinfo"
PDFTOTEXT = "pdftotext"
PDFTOPPM = "pdftoppm"

TOTAL_PAGES_RE = re.compile(r"^Pages:\s+(\d+)", re.M)
PAGE_ROT_RE = re.compile(r"^Page\s+(\d+)\s+rot:\s+(-?\d+)", re.M)


class PdfToolError(Exception):
    """poppler 工具不可用或调用失败。"""


def _run(argv: Sequence[str]) -> str:
    """执行一条 poppler 命令，返回 stdout 文本。"""
    tool = shutil.which(argv[0])
    if tool is None:
        raise PdfToolError(f"required tool not found on PATH: {argv[0]}")
    completed = subprocess.run([tool, *argv[1:]], capture_output=True)
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace").strip()
        raise PdfToolError(f"{argv[0]} failed with exit code {completed.returncode}: {detail[:400]}")
    return completed.stdout.decode("utf-8", "replace")


def page_count(pdf: Path) -> int:
    """PDF 的总页数。"""
    matched = TOTAL_PAGES_RE.search(_run([PDFINFO, str(pdf)]))
    if not matched:
        raise PdfToolError(f"pdfinfo reported no page count for {pdf}")
    return int(matched.group(1))


def page_rotations(pdf: Path, total: Optional[int] = None) -> Dict[int, int]:
    """每页的旋转角（度）。横向页由 ``pdflscape`` 旋转页面得到，角非零即横向。"""
    last = total if total is not None else page_count(pdf)
    text = _run([PDFINFO, "-f", "1", "-l", str(last), str(pdf)])
    return {int(page): int(angle) for page, angle in PAGE_ROT_RE.findall(text)}


def page_texts(pdf: Path) -> List[str]:
    """按页切分的文本；返回列表的下标 0 对应第 1 页。"""
    text = _run([PDFTOTEXT, "-layout", str(pdf), "-"])
    pages = text.split("\f")
    if pages and not pages[-1].strip():
        pages.pop()
    return pages


def rasterize_page(
    pdf: Path, page: int, directory: Path, resolution: int, prefix: str = "page"
) -> Path:
    """把某一页渲染成 PNG，返回图像路径。"""
    directory.mkdir(parents=True, exist_ok=True)
    stem = directory / f"{prefix}-{page:04d}"
    image = stem.with_suffix(".png")
    image.unlink(missing_ok=True)
    _run(
        [
            PDFTOPPM, "-png", "-r", str(resolution),
            "-f", str(page), "-l", str(page), "-singlefile",
            str(pdf), str(stem),
        ]
    )
    if not image.is_file():
        raise PdfToolError(f"pdftoppm produced no image for page {page}")
    return image


def byte_total(paths: Sequence[Path]) -> int:
    """一组文件的总字节数。"""
    return sum(path.stat().st_size for path in paths if path.is_file())
