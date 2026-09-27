# -*- coding: utf-8 -*-
"""长不可断串的断行与含 ``%`` 链接的脚注：真实排版引擎的端到端回归。

历史上 URL 走 ``\\url{}``，它嵌套在 ``\\footnote{}`` 里时 ``%`` 会把整行变成注释，
把文档从那里截断（丢了约二十页）；现在 URL 由 :func:`primer.book.tex.render_url`
手工转义，并在无分隔符的长段内补 ``\\allowbreak{}``。本测试的三条硬串各对应一处：
(a) 四十字符无分隔符的 URL 末段，(b) 六十字符的正文技术记号，(c) 含 ``%`` 的脚注
链接。断言排版日志里没有 overfull hbox，且文末标记仍在（文档未被截断）。

没装 TeX 时整组跳过；文末标记要靠 ``pdftotext`` 抽取，缺该工具时只跳过那一条。
"""

import shutil

import pytest

from primer.book import BookBuilder, load_manifest
from primer.book import pdf as pdf_tools

XELATEX = shutil.which("xelatex")
PDFTOTEXT = shutil.which("pdftotext")

END_MARKER = "LINEBREAK-FIXTURE-END-MARKER"
LONG_URL_RUN = "abcdefghij0123456789abcdefghij0123456789"  # 四十字符，无分隔符
LONG_PROSE_TOKEN = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz01234567"  # 六十字符
PERCENT_URL = "https://example.org/report/a%20b%25c"

ONE_MD = f"""# 断行示例

## 1.1 长串

正文里放一个六十字符的技术记号 {LONG_PROSE_TOKEN}，后面接一个长链接
[长链接](https://example.org/archive/2026/data/{LONG_URL_RUN})。

含百分号的链接 [百分号]({PERCENT_URL}) 走脚注，其后的内容不得被截断。
"""

REFS_MD = f"""# 参考文献库（示例）

> 版本：v1

---

## A 示例

[1] First entry.
[99] 结束标记 {END_MARKER}。
"""

MANIFEST = """\
book:
  title: 断行测试
  subtitle: （示例）

source_root: sources

bibliography:
  file: refs.md
  section_title: 总参考文献列表

output:
  jobname: linebreak
  engine: xelatex
  engine_runs: 2
  min_pdf_bytes: 1
  emit_markdown: false

fonts:
  cjk_main: Songti SC
  main: Times New Roman

typography:
  body_font_size: "4"

volumes:
  - id: V1
    title: 第一篇　断行示例
    part_label: linebreakPart
    appendix: true
    sources:
      - file: one.md

body_order: [V1, "@bibliography"]
"""


def write_book(root):
    """写出微型书（源文件 + 清单），返回清单路径。"""
    sources = root / "sources"
    sources.mkdir(parents=True)
    (sources / "one.md").write_text(ONE_MD, encoding="utf-8")
    (sources / "refs.md").write_text(REFS_MD, encoding="utf-8")
    path = root / "book.yaml"
    path.write_text(MANIFEST, encoding="utf-8")
    return path


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    """真跑一遍 xelatex + xdvipdfmx，产出可检查的 PDF 与日志。"""
    if XELATEX is None:
        pytest.skip("xelatex is not installed")
    root = tmp_path_factory.mktemp("linebreak")
    return BookBuilder(load_manifest(write_book(root))).build()


def test_hard_tokens_leave_no_overfull_box(built):
    log = built.pdf.with_suffix(".log").read_text(errors="ignore")

    assert "Overfull \\hbox" not in log


@pytest.mark.skipif(PDFTOTEXT is None, reason="pdftotext is not installed")
def test_percent_url_in_a_footnote_does_not_truncate_the_document(built):
    """``%`` 曾被当成注释吃掉后文：文末标记必须还在 PDF 里。"""
    pages = pdf_tools.page_texts(built.pdf)

    assert any(END_MARKER in page for page in pages)
