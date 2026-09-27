# -*- coding: utf-8 -*-
"""成书装配：源 markdown → ``jobname.tex`` → 引擎编译 → PDF 校验。

装配顺序：导言区、封面、凡例、目录（全文／插图／表格）、附录目录，然后是清单
``body_order`` 声明的正文各篇与总参考文献列表。

编号模型（与旧体例的根本差别）：篇、章、节、小节、图、表、公式全部由 LaTeX 自动
编号——章跨篇连续（第一篇 1—5 章，第二篇 6—9 章，第三篇 10—14 章），图、表按章
编号（``图 10-1``）。作者在源文件里手写的章号、图表号一律在内存中剥掉：章号只是
标题文本的一部分，图表号转成 ``\\label`` 的键，正文里的交叉引用改写成 ``\\ref``。
源文件一个字节都不改。

产物只落在 ``<工程根>/_primer/book/``：``.tex``、``Makefile``、``.log``、``.pdf``
以及（可选的）markdown 与发现清单。
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from ..paths import relative_to_root, strip_root_prefix
from . import crossrefs, logcheck, markdown as md, numbering, quotes, tex
from .findings import Finding, summarize
from .makefile import BuildPlan
from .manifest import (
    BIBLIOGRAPHY_KEY,
    BookInfo,
    BookManifest,
    CatalogEntry,
    FrontMatter,
    VolumeSpec,
)
from .preamble import render_preamble
from .references import Bibliography

MARKDOWN_LINK_RE = re.compile(r"\[[^\]]*\]\([^)]*\)")


class BuildError(Exception):
    """成书构建失败（引擎缺失、编译不通过或 PDF 未达预期）。"""


@dataclass
class VolumeText:
    """一篇的处理结果。"""

    spec: VolumeSpec
    text: str
    citations: List[int] = field(default_factory=list)


@dataclass
class BuildResult:
    """构建产物。"""

    tex: Path
    pdf: Optional[Path]
    makefile: Path
    volumes: List[VolumeText]
    max_reference: int
    findings: List[Finding] = field(default_factory=list)


class BookBuilder:
    """按清单装配成书。"""

    def __init__(
        self,
        manifest: BookManifest,
        out_dir: Optional[Path] = None,
        tex_only: bool = False,
        project_root: Optional[Path] = None,
    ) -> None:
        self.manifest = manifest
        self.project_root = (
            Path(project_root).expanduser().resolve()
            if project_root is not None
            else manifest.project_root
        )
        if out_dir is not None:
            self.out_dir = Path(out_dir).resolve()
        elif self.project_root == manifest.project_root:
            self.out_dir = manifest.output.directory
        else:
            # 换了工程根却没显式给产物目录：按新工程根把清单里的相对位置重新推导。
            try:
                relative = manifest.output.directory.relative_to(manifest.project_root)
            except ValueError:
                self.out_dir = manifest.output.directory
            else:
                self.out_dir = self.project_root / relative
        self.tex_only = tex_only
        self.bibliography = (
            Bibliography.load(manifest.bibliography)
            if manifest.bibliography is not None
            else Bibliography()
        )
        self.plan = BuildPlan(
            project_root=self.project_root,
            out_dir=self.out_dir,
            jobname=manifest.output.jobname,
            engine=manifest.output.engine,
            engine_runs=manifest.output.engine_runs,
        )
        self.findings: List[Finding] = []
        self.volumes: List[VolumeText] = []
        self._quote_tally = quotes.QuoteTally()
        self._bibliography_text = self.bibliography.text
        self._book = manifest.book
        self._front_matter = manifest.front_matter

    # ------------------------------------------------------------ 对外接口

    def build(self) -> BuildResult:
        """装配并（除 ``tex_only`` 外）编译成书。"""
        self.out_dir.mkdir(parents=True, exist_ok=True)
        volumes, by_id, document = self._document()

        tex_path = self.out_dir / f"{self.manifest.output.jobname}.tex"
        tex_path.write_text("\n".join(document), encoding="utf-8")
        print(f"[book] tex: {tex_path}")

        makefile_path = self.out_dir / "Makefile"
        makefile_path.write_text(self.plan.makefile(), encoding="utf-8")
        print(f"[book] makefile: {makefile_path}")

        if self.manifest.output.emit_markdown:
            self._write_markdown(volumes, by_id)

        pdf = None
        if not self.tex_only:
            pdf = self._compile(tex_path)
            self.findings.extend(logcheck.parse_log(self.plan.log.read_text(errors="ignore")))
        self._report_findings()
        self._write_findings()
        return BuildResult(
            tex=tex_path,
            pdf=pdf,
            makefile=makefile_path,
            volumes=volumes,
            max_reference=self.bibliography.max_number,
            findings=list(self.findings),
        )

    def inspect(self) -> List[Finding]:
        """不落盘、不编译，只把装配过程中的发现跑一遍（``check`` 用）。"""
        self.findings = []
        self._document()
        return list(self.findings)

    def _document(self) -> Tuple[List[VolumeText], Dict[str, VolumeText], List[str]]:
        """装配成 LaTeX 行序列；发现写入 ``self.findings``，各篇文本留在 ``self.volumes``。"""
        self.findings = []
        self._quote_tally = quotes.QuoteTally(
            enabled=self.manifest.typography.normalize_quotes
        )
        volumes = self._process_volumes()
        self.volumes = volumes
        by_id = {volume.spec.id: volume for volume in volumes}
        self._bibliography_text = "\n".join(
            quotes.normalize_lines(
                self.bibliography.text.splitlines(), self._quote_tally, "bibliography"
            )
        )
        self._book = self._normalize_book(self.manifest.book)
        self._front_matter = self._normalize_front_matter(self.manifest.front_matter)
        document = self._assemble(by_id)
        self.findings.extend(self._quote_findings())
        return volumes, by_id, document

    def _normalize_book(self, book: BookInfo) -> BookInfo:
        """封面各字段按散文规整引号（引号方向是形式，源清单不改）。"""
        if not self._quote_tally.enabled:
            return book
        return replace(
            book,
            title=quotes.normalize_line(book.title, self._quote_tally, "book.title"),
            subtitle=quotes.normalize_line(book.subtitle, self._quote_tally, "book.subtitle"),
            tagline=quotes.normalize_line(book.tagline, self._quote_tally, "book.tagline"),
            institution=quotes.normalize_line(
                book.institution, self._quote_tally, "book.institution"
            ),
            date=quotes.normalize_line(book.date, self._quote_tally, "book.date"),
        )

    def _normalize_front_matter(self, front: FrontMatter) -> FrontMatter:
        """凡例、附录目录等前置页文本按散文规整引号。"""
        if not self._quote_tally.enabled:
            return front
        entries = tuple(
            CatalogEntry(
                title=quotes.normalize_line(
                    entry.title, self._quote_tally, f"front_matter.appendix_catalog[{index}]"
                ),
                label=entry.label,
            )
            for index, entry in enumerate(front.appendix_catalog)
        )
        return replace(
            front,
            foreword_heading=quotes.normalize_line(
                front.foreword_heading, self._quote_tally, "front_matter.foreword_heading"
            ),
            foreword=tuple(
                quotes.normalize_line(text, self._quote_tally, f"front_matter.foreword[{index}]")
                for index, text in enumerate(front.foreword)
            ),
            catalog_heading=quotes.normalize_line(
                front.catalog_heading, self._quote_tally, "front_matter.catalog_heading"
            ),
            appendix_catalog=entries,
        )

    def _quote_findings(self) -> List[Finding]:
        """引号规整的汇总发现：配成的对数，以及原样保留的未配对行。"""
        tally = self._quote_tally
        if not tally.enabled:
            return []
        findings: List[Finding] = []
        if tally.pairs:
            findings.append(
                Finding(
                    code="quotes-normalized",
                    severity="info",
                    message=(
                        f"normalized {tally.pairs * 2} straight double quotes into Chinese curly "
                        f"quotes ({tally.pairs} pair(s); form-level, the sources are untouched)"
                    ),
                )
            )
        for location, count in tally.unpaired:
            findings.append(
                Finding(
                    code="quotes-unpaired",
                    severity="warning",
                    message=(
                        f"odd number of straight double quotes ({count}); the line was left "
                        "untouched for a human to check"
                    ),
                    location=location,
                )
            )
        return findings

    # ------------------------------------------------------------ 逐篇处理

    def _ingest(self, spec: VolumeSpec) -> List[str]:
        """读源文件、剥抬头、抹工程根前缀、丢编辑性尾注、按方案重写标题与交叉引用。

        源文件里的"本地存档"链接写的常是绝对路径，而且可能用另一种大小写拼工程根
        （``Documents/Kimi/…`` 对上小写 ``kimi`` 的工程根）。这类路径会随正文进入
        markdown、``.tex`` 与发现清单，换台机器就失效，所以在这里统一改写成相对
        工程根——大小写不敏感，判据见 :func:`primer.paths.strip_root_prefix`。

        最后把散文里的 ASCII 直引号配成中文弯引号（:mod:`primer.book.quotes`）：
        引号方向是形式，由排版系统决定，源文件不改。
        """
        lines: List[str] = []
        for index, source in enumerate(spec.sources):
            text = strip_root_prefix(source.path.read_text(encoding="utf-8"), self.project_root)
            body = md.strip_preamble(text.splitlines())
            body = md.drop_matching(body, self.manifest.drop_lines)
            if source.scheme:
                body = numbering.apply_scheme(body, self.manifest.scheme(source.scheme))
            banner = spec.title if index == 0 else source.banner
            if banner:
                lines.extend(["", md.PART_MARKER + banner, ""])
            lines.extend(
                quotes.normalize_lines(body, self._quote_tally, f"{spec.id} {source.path.name}")
            )
        return lines

    def _process_volumes(self) -> List[VolumeText]:
        """两遍处理：先全书收集标签（跨篇引用也认得），再改写正文引用。"""
        ingested = {spec.id: self._ingest(spec) for spec in self.manifest.volumes}

        labels: Dict[str, str] = {}
        findings: List[Finding] = []
        for spec in self.manifest.volumes:
            blocks = md.parse_blocks(ingested[spec.id])
            volume_labels, volume_findings = crossrefs.collect_labels(blocks)
            for key, kind in volume_labels.items():
                labels.setdefault(key, kind)
            findings.extend(self._locate(volume_findings, spec.id))

        volumes: List[VolumeText] = []
        referenced: set = set()
        for spec in self.manifest.volumes:
            lines, refs, ref_findings = crossrefs.rewrite_references(
                ingested[spec.id], labels, spec.id
            )
            referenced |= refs
            findings.extend(ref_findings)
            text = numbering.normalize_cite_ranges("\n".join(lines))
            blocks = md.parse_blocks(lines)
            findings.extend(self._check_images(blocks, spec.id))
            citations = numbering.collect_citations(_strip_links(text))
            findings.extend(self._check_citations(citations, spec.id))
            volumes.append(VolumeText(spec=spec, text=text, citations=citations))
        findings.extend(crossrefs.unreferenced(labels, referenced))
        findings.extend(self._check_uncaptioned(volumes))
        self.findings = findings
        return volumes

    def _locate(self, findings: Sequence[Finding], volume_id: str) -> List[Finding]:
        return [replace(item, location=f"{volume_id} {item.location}".strip()) for item in findings]

    def _check_images(self, blocks: Sequence[object], volume_id: str) -> List[Finding]:
        """插图路径必须在 ``source_root`` 下真的存在。"""
        findings = []
        for block in blocks:
            if not isinstance(block, md.Figure):
                continue
            if not (self.manifest.source_root / block.path).is_file():
                findings.append(
                    Finding(
                        code="missing-image",
                        severity="error",
                        message=f"figure file {block.path!r} does not exist under the source root",
                        location=volume_id,
                    )
                )
        return findings

    def _check_citations(self, citations: Sequence[int], volume_id: str) -> List[Finding]:
        findings = []
        for number in citations:
            if self.bibliography.get(number) is None:
                findings.append(
                    Finding(
                        code="citation-without-entry",
                        severity="error",
                        message=f"citation [{number}] has no entry in the bibliography",
                        location=volume_id,
                    )
                )
        return findings

    def _check_uncaptioned(self, volumes: Sequence[VolumeText]) -> List[Finding]:
        findings = []
        for volume in volumes:
            for block in md.parse_blocks(volume.text.splitlines()):
                if isinstance(block, md.Table) and not block.caption:
                    findings.append(
                        Finding(
                            code="uncaptioned-table",
                            severity="info",
                            message="table has no caption, so it is unnumbered and absent from the list of tables",
                            location=volume.spec.id,
                        )
                    )
        return findings

    # ------------------------------------------------------------ 合订 tex

    def _assemble(self, by_id: Dict[str, VolumeText]) -> List[str]:
        manifest = self.manifest
        graphics = relative_to_root(manifest.source_root, self.project_root) + "/"
        doc = [
            render_preamble(manifest.fonts, manifest.typography, graphics),
            r"\begin{document}",
            tex.render_title_page(self._book),
            tex.render_foreword(self._front_matter, self.bibliography.max_number),
            tex.render_catalog_pages(),
            tex.render_appendix_catalog(self._front_matter),
        ]
        appendix_started = False
        short_marks: List[str] = []
        for key in manifest.body_order:
            if key == BIBLIOGRAPHY_KEY:
                doc.append(
                    tex.render_bibliography(
                        self._bibliography_text, manifest.bibliography_section_title
                    )
                )
                continue
            volume = by_id[key]
            if volume.spec.appendix and not appendix_started:
                doc.append(r"\appendix")
                appendix_started = True
            blocks = md.parse_blocks(volume.text.splitlines())
            doc.append(
                "\n".join(
                    tex.render_blocks(
                        blocks,
                        manifest.typography,
                        self.findings,
                        part_label=volume.spec.part_label,
                        appendix=volume.spec.appendix,
                        location=volume.spec.id,
                        marks=short_marks,
                    )
                )
            )
        if short_marks:
            self.findings.append(
                Finding(
                    code="running-head-truncated",
                    severity="info",
                    message=(
                        f"shortened {len(short_marks)} running-head mark(s) to fit the header "
                        "width; the cut is marked with an ellipsis"
                    ),
                )
            )
        doc.append(r"\end{document}")
        return doc

    # ------------------------------------------------------------ 落盘

    def _write_markdown(
        self, volumes: Sequence[VolumeText], by_id: Dict[str, VolumeText]
    ) -> None:
        for volume in volumes:
            if not volume.spec.standalone:
                continue
            path = self.out_dir / (volume.spec.title.replace("　", "_") + ".md")
            path.write_text(self._standalone_text(volume), encoding="utf-8")
            print(f"[book] markdown: {path}")

        full: List[str] = [f"# {self._book.title}{self._book.subtitle}\n"]
        for key in self.manifest.body_order:
            if key == BIBLIOGRAPHY_KEY:
                full.append(
                    f"\n## {self.manifest.bibliography_section_title}\n\n{self._bibliography_text}"
                )
            else:
                full.append(md.parts_to_headings(by_id[key].text))
        path = self.out_dir / f"{self.manifest.output.jobname}.md"
        path.write_text("\n\n".join(full) + "\n", encoding="utf-8")
        print(f"[book] markdown: {path}")

    def _standalone_text(self, volume: VolumeText) -> str:
        """篇级 markdown：册内编号的篇另附册内文献表。"""
        body = volume.text
        if volume.spec.citation_mode == "local" and volume.citations:
            body = numbering.renumber_citations(body, volume.citations)
            body += "\n\n" + md.local_bibliography(volume.citations, self.bibliography)
        return md.as_volume_document(body, volume.spec.title)

    def _write_findings(self) -> None:
        path = self.out_dir / f"{self.manifest.output.jobname}.findings.json"
        payload = {
            "jobname": self.manifest.output.jobname,
            "findings": [item.as_dict() for item in self.findings],
            "summary": summarize(self.findings),
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"[book] findings: {path}")

    def _report_findings(self) -> None:
        for item in self.findings:
            if item.severity == "error":
                print(f"[book] error: {item.code}: {item.message} ({item.location})")
        errors = sum(1 for item in self.findings if item.severity == "error")
        warnings = sum(1 for item in self.findings if item.severity == "warning")
        infos = sum(1 for item in self.findings if item.severity == "info")
        print(f"[book] findings: {errors} error(s), {warnings} warning(s), {infos} note(s)")

    # ------------------------------------------------------------ 编译

    def _compile(self, tex_path: Path) -> Path:
        output = self.manifest.output
        for suffix in ("aux", "toc", "lof", "lot", "out", "xdv", "pdf", "fls"):
            (self.out_dir / f"{output.jobname}.{suffix}").unlink(missing_ok=True)
        for argv, cwd in self.plan.commands():
            try:
                subprocess.run(argv, cwd=cwd, capture_output=True)
            except FileNotFoundError as exc:
                raise BuildError(f"engine not found: {exc.filename or argv[0]}") from exc

        pdf = self.plan.pdf
        if pdf.is_file() and pdf.stat().st_size >= output.min_pdf_bytes:
            print(f"[book] pdf: {pdf} ({pdf.stat().st_size} bytes)")
            return pdf

        errors = []
        if self.plan.log.is_file():
            errors = [
                line
                for line in self.plan.log.read_text(errors="ignore").splitlines()
                if line.startswith("!")
            ]
        raise BuildError(
            f"{output.jobname} did not produce a usable pdf (min {output.min_pdf_bytes} bytes)"
            + ("\n" + "\n".join(errors[:20]) if errors else "")
        )


def _strip_links(text: str) -> str:
    """去掉 markdown 链接，避免把链接文字误判成文献编号。"""
    return MARKDOWN_LINK_RE.sub("", text)
