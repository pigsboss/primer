# -*- coding: utf-8 -*-
"""端到端装配与命令行。

除最后一个用真实排版引擎的集成测试外，其余测试都不需要装 TeX：``--tex-only``
只到 ``.tex`` 为止，``check`` 只读清单、源文件与日志。
"""

import json
import shutil
import subprocess

import pytest

from book_fixtures import MANIFEST, ONE_MD, tiny_book

from primer.book import BookBuilder, BuildError, load_manifest
from primer.book.__main__ import main
from primer.book.makefile import BuildPlan

XELATEX = shutil.which("xelatex")


def test_tex_only_build_writes_the_tex_makefile_and_findings(tmp_path):
    manifest = load_manifest(tiny_book(tmp_path))

    result = BookBuilder(manifest, tex_only=True).build()
    text = result.tex.read_text(encoding="utf-8")

    assert result.pdf is None
    assert result.tex == tmp_path / "_primer" / "book" / "tiny.tex"
    assert result.makefile == tmp_path / "_primer" / "book" / "Makefile"
    assert result.max_reference == 12
    assert text.startswith("% !TeX program = xelatex")
    assert text.endswith(r"\end{document}")
    # 插图路径相对工程根，不落绝对路径
    assert r"\graphicspath{{sources/}}" in text
    assert str(tmp_path) not in text
    assert r"\part{科学篇}" in text
    assert r"\chapter{导言}" in text
    assert r"\section{小节}" in text
    assert r"\appendix" in text
    assert r"\chapter{示例附录}" in text
    assert r"\label{appA}" in text
    assert r"\caption[示例图]{示例图。资料来源}\label{fig:1-1}" in text
    assert r"\caption{示例表}\label{tab:1-1}\\" in text
    assert r"正文里提一次图~\ref{fig:1-1} 与表~\ref{tab:1-1}。" in text
    assert r"\begin{lstlisting}" in text
    assert r"\noindent 一、示例凡例，引用编号上限 [12]。\par\medskip" in text
    assert "（第一篇完）" not in text
    assert [finding for finding in result.findings if finding.severity != "info"] == []


def test_build_writes_a_machine_readable_findings_report(tmp_path):
    manifest = load_manifest(tiny_book(tmp_path))

    BookBuilder(manifest, tex_only=True).build()

    payload = json.loads((tmp_path / "_primer" / "book" / "tiny.findings.json").read_text())
    assert payload["jobname"] == "tiny"
    assert payload["summary"] == [
        {"code": "table-layout", "severity": "info", "count": 2}
    ]
    assert all(item["severity"] == "info" for item in payload["findings"])


def test_tex_only_build_writes_standalone_markdown(tmp_path):
    manifest = load_manifest(tiny_book(tmp_path))

    BookBuilder(manifest, tex_only=True).build()

    chapter = (tmp_path / "_primer" / "book" / "第一篇_科学篇.md").read_text(encoding="utf-8")
    combined = (tmp_path / "_primer" / "book" / "tiny.md").read_text(encoding="utf-8")

    assert chapter.startswith("# 第一篇　科学篇")
    assert "## 第 1 章　导言" in chapter
    assert combined.startswith("# 微型书（示例）")
    assert "## 第一篇　科学篇" in combined
    assert "## 总参考文献列表" in combined
    assert "[1] First entry." in combined


def test_local_citation_mode_renumbers_and_appends_a_volume_bibliography(tmp_path):
    text = MANIFEST.replace("    standalone: true", "    standalone: true\n    citation_mode: local")
    manifest = load_manifest(tiny_book(tmp_path, text))

    BookBuilder(manifest, tex_only=True).build()

    chapter = (tmp_path / "_primer" / "book" / "第一篇_科学篇.md").read_text(encoding="utf-8")
    book = (tmp_path / "_primer" / "book" / "tiny.tex").read_text(encoding="utf-8")

    assert "[1] Third entry.（全书编号 [3]）" in chapter
    assert "## 本册参考文献" in chapter
    # 合订本始终沿用全书编号
    assert "引用 [3][1]" in book


def test_an_absolute_source_link_never_reaches_the_artifacts(tmp_path):
    """源文件里的"本地存档"链接可能是绝对路径，而且大小写与真实工程根不同。

    生产里源文件写的是 ``/Users/huo/Documents/Kimi/…``，工程根却是小写 ``kimi``：
    文件系统认为两者是同一个目录，字符串比较却不认，绝对路径就这样进了 markdown
    与 ``.tex``。与参考文献包那处泄漏同一类，判据同 :func:`primer.paths.strip_root_prefix`。
    """
    root = tmp_path.resolve()
    other_spelling = str(root).swapcase()
    source = ONE_MD.replace(
        "[站点](https://example.org/a_b)",
        f'[存档]({other_spelling}/sources/refs.md "citation")',
    )
    manifest = load_manifest(tiny_book(tmp_path, one_md=source))

    result = BookBuilder(manifest, tex_only=True).build()

    tex = result.tex.read_text(encoding="utf-8")
    combined = (tmp_path / "_primer" / "book" / "tiny.md").read_text(encoding="utf-8")
    assert other_spelling not in tex
    assert other_spelling not in combined
    # 前缀抹掉后目标成了相对路径，链接照常变脚注——原样留着 markdown 语法会印进 PDF。
    assert r"存档\footnote{\texttt{sources/\allowbreak{}refs.\allowbreak{}md}}" in tex
    assert "[存档](" not in tex
    assert "sources/refs.md" in combined


def test_out_dir_override_keeps_the_default_tree_empty(tmp_path):
    manifest = load_manifest(tiny_book(tmp_path))

    result = BookBuilder(manifest, out_dir=tmp_path / "elsewhere", tex_only=True).build()

    assert result.tex == tmp_path / "elsewhere" / "tiny.tex"
    assert not (tmp_path / "_primer").exists()


def test_project_root_override_rebases_the_output_and_the_graphics_path(tmp_path):
    manifest = load_manifest(tiny_book(tmp_path))

    result = BookBuilder(
        manifest, project_root=tmp_path / "sources", tex_only=True
    ).build()

    assert result.tex == (tmp_path / "sources" / "_primer" / "book" / "tiny.tex")
    assert r"\graphicspath{{./}}" in result.tex.read_text(encoding="utf-8")


def test_dangling_reference_and_missing_image_are_reported(tmp_path):
    manifest = load_manifest(
        tiny_book(tmp_path, one_md=ONE_MD.replace("图 1-1 与表 1-1", "图 9-9 与表 1-1"))
    )

    result = BookBuilder(manifest, tex_only=True).build()

    codes = [finding.code for finding in result.findings]
    assert codes.count("dangling-figure-reference") == 1
    assert codes.count("unreferenced-figure") == 1


def test_missing_engine_is_reported_as_a_build_error(tmp_path):
    text = MANIFEST.replace("engine: xelatex", "engine: definitely-not-a-typesetter")
    manifest = load_manifest(tiny_book(tmp_path, text))

    with pytest.raises(BuildError, match="engine not found"):
        BookBuilder(manifest).build()


def test_small_pdf_is_rejected(tmp_path, monkeypatch):
    """引擎"编译"出一个空文件时按失败处理，并把日志里的报错行带回。"""
    text = MANIFEST.replace("min_pdf_bytes: 1", "min_pdf_bytes: 1000")
    manifest = load_manifest(tiny_book(tmp_path, text))

    def fake_run(command, cwd, capture_output):
        out = tmp_path / "_primer" / "book"
        (out / "tiny.log").write_text("! Fatal error\n", encoding="utf-8")
        (out / "tiny.pdf").write_bytes(b"tiny")

    monkeypatch.setattr("primer.book.builder.subprocess.run", fake_run)

    with pytest.raises(BuildError) as excinfo:
        BookBuilder(manifest).build()

    assert "min 1000 bytes" in str(excinfo.value)
    assert "! Fatal error" in str(excinfo.value)


def test_build_plan_drives_xelatex_twice_then_the_pdf_driver(tmp_path):
    plan = BuildPlan(project_root=tmp_path, out_dir=tmp_path / "_primer" / "book", jobname="tiny")

    commands = plan.commands()

    assert [command[0][0] for command in commands] == ["xelatex", "xelatex", "xdvipdfmx"]
    assert all(command[1] == tmp_path for command in commands)
    assert "-no-pdf" in commands[0][0]
    assert "-output-directory=_primer/book" in commands[0][0]
    assert commands[1][0][-1] == "_primer/book/tiny"
    assert commands[2][0][1:3] == ["-o", "_primer/book/tiny.pdf"]

    makefile = plan.makefile()
    assert "all : note" in makefile
    assert makefile.count("-no-pdf") == 2
    assert "xdvipdfmx" in makefile
    assert "clean :" in makefile and "distclean : clean" in makefile


# ---------------------------------------------------------------- 命令行


def test_cli_build_tex_only_and_out_dir(tmp_path, capsys):
    path = tiny_book(tmp_path)

    assert main(["build", str(path), "--tex-only", "--out-dir", str(tmp_path / "cli")]) == 0

    assert (tmp_path / "cli" / "tiny.tex").is_file()
    assert "skipping the typesetting engine" in capsys.readouterr().out


def test_cli_font_overrides_reach_the_preamble(tmp_path, capsys):
    path = tiny_book(tmp_path)

    assert main(
        ["build", str(path), "--tex-only", "--font", "Heiti SC", "--sans-font", "PingFang SC",
         "--fontsize", "12pt"]
    ) == 0

    text = (tmp_path / "_primer" / "book" / "tiny.tex").read_text(encoding="utf-8")
    assert r"\setCJKmainfont[AutoFakeBold=2.5,AutoFakeSlant=0.15]{Heiti SC}" in text
    assert r"\setsansfont{PingFang SC}" in text
    assert r"\renewcommand\normalsize{\fontsize{12pt}{15pt}\selectfont}" in text


def test_cli_check_reports_no_findings_and_exits_zero(tmp_path, capsys):
    path = tiny_book(tmp_path)
    main(["build", str(path), "--tex-only"])

    assert main(["check", str(path)]) == 0

    out = capsys.readouterr().out
    assert "no findings" not in out  # --tex-only build leaves no log, so one warning is expected
    assert "missing-log" in out
    report = tmp_path / "_primer" / "book" / "tiny.check.json"
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["failed"] is False


def test_cli_check_fails_on_a_fatal_finding(tmp_path, capsys):
    path = tiny_book(tmp_path, one_md=ONE_MD.replace("图 1-1 与表 1-1", "图 9-9 与表 1-1"))

    assert main(["check", str(path)]) == 1

    out = capsys.readouterr().out
    assert "dangling-figure-reference" in out


def test_cli_check_fails_on_an_undefined_reference_in_the_log(tmp_path):
    path = tiny_book(tmp_path)
    out_dir = tmp_path / "_primer" / "book"
    out_dir.mkdir(parents=True)
    (out_dir / "tiny.log").write_text(
        "LaTeX Warning: Reference `fig:1-1' on page 3 undefined on input line 9.\n",
        encoding="utf-8",
    )

    assert main(["check", str(path)]) == 1


def test_cli_check_strict_fails_on_warnings(tmp_path):
    path = tiny_book(tmp_path)
    out_dir = tmp_path / "_primer" / "book"
    out_dir.mkdir(parents=True)
    (out_dir / "tiny.log").write_text(
        "Overfull \\hbox (3.0pt too wide) in paragraph at lines 1--2\n", encoding="utf-8"
    )

    assert main(["check", str(path)]) == 0
    assert main(["check", str(path), "--strict"]) == 1


def test_cli_reports_manifest_errors(tmp_path, capsys):
    path = tiny_book(tmp_path, MANIFEST.replace("source_root: sources", "source_root: nowhere"))

    assert main(["check", str(path)]) == 2
    assert "source_root is not a directory" in capsys.readouterr().err


# ---------------------------------------------------------------- 集成


@pytest.mark.skipif(XELATEX is None, reason="xelatex is not installed")
def test_real_engine_builds_a_pdf(tmp_path):
    """真正跑一遍 xelatex + xdvipdfmx，并用 check 复核产物。"""
    path = tiny_book(tmp_path)

    result = BookBuilder(load_manifest(path)).build()

    assert result.pdf is not None and result.pdf.is_file()
    assert result.pdf.stat().st_size > 2000
    assert result.pdf.with_suffix(".log").is_file()
    assert not [finding for finding in result.findings if finding.severity == "error"]
    # 目录里没有"目录"自己的条目；前置页里另有插图目录、表格目录两条。
    toc = result.pdf.with_suffix(".toc").read_text(encoding="utf-8")
    assert r"\contentsline {chapter}{目录}{" not in toc
    assert r"\contentsline {chapter}{插图目录}{" in toc
    assert r"\contentsline {chapter}{表格目录}{" in toc
    from primer.book.__main__ import main as cli

    assert cli(["check", str(path)]) == 0
