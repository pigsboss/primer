# -*- coding: utf-8 -*-
"""编译计划：把"怎么调排版引擎"收敛成一处，供构建器与 Makefile 共用。

按工程主人的体例，PDF 不是一条命令出来的：``xelatex -no-pdf`` 跑若干遍（把目录、
交叉引用、页码收敛），再 ``xdvipdfmx`` 把 ``.xdv`` 转成 PDF。清单里的
``engine_runs`` 决定第一段的遍数。

编译的工作目录是**工程根**而不是产物目录：.tex 里记录的插图路径相对工程根
（``成果文件/``），这样产物搬到哪里都还读得出来。Makefile 因此先 ``cd`` 回工程根，
再用 ``-output-directory`` 把中间文件与 PDF 关在产物目录里——工程根一个字节不写。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple

PDF_DRIVER = "xdvipdfmx"


def _relative(path: Path, base: Path) -> str:
    return os.path.relpath(str(path), str(base)).replace(os.sep, "/")


@dataclass(frozen=True)
class BuildPlan:
    """一次编译的全部参数。"""

    project_root: Path
    out_dir: Path
    jobname: str
    engine: str = "xelatex"
    engine_runs: int = 2

    @property
    def out_rel(self) -> str:
        """产物目录相对工程根（Makefile 里 ``$(OUT)`` 的取值）。"""
        try:
            return _relative(self.out_dir, self.project_root)
        except ValueError:
            return str(self.out_dir)

    @property
    def source(self) -> str:
        """传给排版引擎的 .tex 路径（相对工程根，不带扩展名）。"""
        return f"{self.out_rel}/{self.jobname}"

    @property
    def tex(self) -> Path:
        return self.out_dir / f"{self.jobname}.tex"

    @property
    def pdf(self) -> Path:
        return self.out_dir / f"{self.jobname}.pdf"

    @property
    def log(self) -> Path:
        return self.out_dir / f"{self.jobname}.log"

    # 引擎写下的三个清单文件（``\contentsline`` 在里面）。深度检查要拿它们与正文里的
    # 标签对账，路径推导收在这里，免得调用方各自拼 ``f"{jobname}.toc"``。
    @property
    def toc(self) -> Path:
        return self.out_dir / f"{self.jobname}.toc"

    @property
    def lof(self) -> Path:
        return self.out_dir / f"{self.jobname}.lof"

    @property
    def lot(self) -> Path:
        return self.out_dir / f"{self.jobname}.lot"

    def commands(self) -> List[Tuple[List[str], Path]]:
        """按顺序执行的命令及其工作目录。"""
        flags = ["-interaction=nonstopmode", "-no-pdf", f"-output-directory={self.out_rel}"]
        steps: List[Tuple[List[str], Path]] = [
            ([self.engine, *flags, self.source], self.project_root)
            for _ in range(max(self.engine_runs, 1))
        ]
        steps.append(
            (
                [PDF_DRIVER, "-o", f"{self.out_rel}/{self.jobname}.pdf",
                 f"{self.out_rel}/{self.jobname}.xdv"],
                self.project_root,
            )
        )
        return steps

    def makefile(self) -> str:
        """按工程主人的写法生成 Makefile：``all``/``note``/``clean``。"""
        root = _relative(self.project_root, self.out_dir)
        latex = f"$(LATEX) -interaction=nonstopmode -no-pdf -output-directory=$(OUT)"
        lines = [
            "# 由 primer-book 生成；改完 .tex 可直接 make 重编。",
            f"JOB  = {self.jobname}",
            f"ROOT = {root}",
            f"OUT  = {self.out_rel}",
            f"LATEX = {self.engine}",
            "PDF  = xdvipdfmx",
            "",
            "all : note",
            "",
            "note :",
        ]
        for _ in range(max(self.engine_runs, 1)):
            lines.append(f"\tcd $(ROOT) && {latex} $(OUT)/$(JOB)")
        lines.append(f"\tcd $(ROOT) && $(PDF) -o $(OUT)/$(JOB).pdf $(OUT)/$(JOB).xdv")
        lines.extend(
            [
                "",
                "clean :",
                "\trm -f $(OUT)/$(JOB).log $(OUT)/$(JOB).aux $(OUT)/$(JOB).toc",
                "\trm -f $(OUT)/$(JOB).lof $(OUT)/$(JOB).lot $(OUT)/$(JOB).out $(OUT)/$(JOB).xdv",
                "",
                "distclean : clean",
                "\trm -f $(OUT)/$(JOB).pdf",
                "",
            ]
        )
        return "\n".join(lines)
