# -*- coding: utf-8 -*-
"""块对象与文档骨架的 LaTeX 渲染：剥离手写编号、题注带标签、表格落版。"""

import re

from primer.book import markdown as md
from primer.book import preamble, tex
from primer.book.manifest import (
    BookInfo,
    CatalogEntry,
    Fonts,
    FrontMatter,
    Typography,
)

TYPO = Typography()


def render(source, **kwargs):
    return tex.render_blocks(
        md.parse_blocks(source), TYPO, kwargs.pop("findings", []), **kwargs
    )


# ---------------------------------------------------------------- 行内


def test_inline_escapes_latex_specials_and_replaces_unicode():
    assert tex.render_inline("A&B 100% #1 a_b $5 α ≥ β") == (
        r"A\&B 100\% \#1 a\_b \$5 $\alpha$ $\geq$ $\beta$"
    )


def test_inline_keeps_math_verbatim_and_handles_emphasis():
    assert tex.render_inline("对比度 $10^{-6}$ 与 **粗**、*斜*") == (
        r"对比度 $10^{-6}$ 与 \textbf{粗}、\emph{斜}"
    )


def test_inline_renders_links_as_breakable_typewriter_footnotes():
    assert tex.render_inline("见 [站点](https://example.org/a_b) 与 `a_b`") == (
        r"见 站点\footnote{\texttt{https:\allowbreak{}/\allowbreak{}/\allowbreak{}"
        r"example.\allowbreak{}org/\allowbreak{}a\_\allowbreak{}b}} 与 \texttt{a\_b}"
    )


def test_inline_can_drop_the_link_target_for_slides():
    """幻灯片一档：只发射标签文字；丢弃的目标记进调用方给的账本，不静默丢。"""
    dropped = []

    rendered = tex.render_inline(
        "见 [站点](https://example.org/a_b) 与 [报告](参考资料/x.pdf)",
        links=tex.LINK_LABEL,
        dropped_links=dropped,
    )

    assert rendered == r"见 站点 与 报告"
    assert r"\footnote" not in rendered
    assert dropped == [
        ("站点", "https://example.org/a_b"),
        ("报告", "参考资料/x.pdf"),
    ]


def test_inline_rejects_an_unknown_link_mode():
    try:
        tex.render_inline("x", links="sideways")
    except ValueError as error:
        assert "sideways" in str(error)
    else:  # pragma: no cover - 正常路径下不会走到
        raise AssertionError("expected ValueError")


def test_inline_escapes_url_characters_that_would_break_the_footnote():
    rendered = tex.render_inline("[报告](https://x.test/a%20b#c)")

    assert r"\%" in rendered and r"\#" in rendered and r"\%20" not in rendered
    assert r"\texttt{" in rendered and rendered.endswith("}}")


def test_inline_leaves_links_whose_path_contains_spaces_untouched():
    """路径带空格的本地链接匹配不上链接正则，按源文件原样保留。"""
    source = '[报告](/Users/x/报告 (1).pdf "citation")'

    assert tex.render_inline(source) == source


def test_inline_renders_relative_path_links():
    """工程根前缀被抹掉后的存档链接仍要成脚注，否则 markdown 原文会印进 PDF。"""
    rendered = tex.render_inline('[报告](参考资料/国际规划/x.pdf "citation")')

    assert rendered.startswith(r"报告\footnote{\texttt{")
    assert "参考资料" in rendered


def test_inline_leaves_images_alone():
    """图片语法由块级处理成 figure，行内变换不许把 ``![…](…)`` 当链接。"""
    source = "![图 1-1](图/科学篇/fig01.png)"

    assert tex.render_inline(source) == source


# 四十字符的无分隔符末段：整块不可断，必须在其内部补断点，否则溢出到版心外。
LONG_RUN = "abcdefghij0123456789abcdefghij0123456789"


def test_url_gets_break_points_inside_a_long_unbroken_segment():
    rendered = tex.render_url("https://example.org/data/" + LONG_RUN)

    body = rendered[len(r"\texttt{") : -1]
    # 断点确实落在末段内部
    assert r"abcdefghij\allowbreak{}0123456789" in body
    # 一个字符都没丢
    assert body.replace(r"\allowbreak{}", "") == "https://example.org/data/" + LONG_RUN


def test_short_url_segments_gain_no_interior_break_points():
    rendered = tex.render_url("https://example.org/ab")

    # 只有分隔符 ://./ 后原有的五点，短段不再补
    assert rendered == (
        r"\texttt{https:\allowbreak{}/\allowbreak{}/\allowbreak{}"
        r"example.\allowbreak{}org/\allowbreak{}ab}"
    )


def test_url_break_points_keep_the_escaping_intact():
    url = "https://example.org/a_b%20c/" + LONG_RUN

    rendered = tex.render_url(url)

    body = rendered[len(r"\texttt{") : -1].replace(r"\allowbreak{}", "")
    assert r"\_" in body and r"\%" in body
    unescaped = (
        body.replace(r"\_", "_").replace(r"\%", "%").replace(r"\#", "#")
        .replace(r"\&", "&").replace(r"\$", "$")
    )
    assert unescaped == url


def test_long_ascii_token_in_prose_is_breakable():
    token = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz01234567"

    rendered = tex.render_inline("见 " + token + " 结束")

    assert r"\allowbreak{}" in rendered
    assert rendered.replace(r"\allowbreak{}", "") == "见 " + token + " 结束"


def test_short_ascii_run_in_prose_is_left_untouched():
    assert tex.render_inline("abc DEF123 短") == "abc DEF123 短"


# ---------------------------------------------------------------- 结构


def test_part_title_drops_the_hand_written_volume_number():
    assert tex.part_title("第一篇　科学篇") == "科学篇"
    assert tex.part_title("附录 A　科产融合专题", appendix=True) == "科产融合专题"
    assert tex.part_title("附录 A　科产融合专题") == "附录 A　科产融合专题"


def test_headings_become_chapter_section_subsection_without_numbers():
    rendered = render(
        [
            "@@PART@@第一篇　科学篇",
            "@@PART@@第二篇　工程篇",
            "## 第 6 章　导言",
            "### 6.1　范围划定",
            "#### 6.1.1　更细一层",
        ]
    )

    assert rendered == [
        r"\part{科学篇}",
        r"\part{工程篇}",
        r"\chapter{导言}",
        r"\section{范围划定}",
        r"\subsection{更细一层}",
    ]


def test_appendix_volume_becomes_one_lettered_chapter_with_sections():
    rendered = render(
        ["@@PART@@附录 A　示例附录", "## A.1　第一节", "### A.1.1　一节之一", "#### A.1.1.1　最细一层"],
        part_label="appA",
        appendix=True,
    )

    assert rendered == [
        r"\chapter{示例附录}",
        r"\label{appA}",
        r"\section{第一节}",
        r"\subsection{一节之一}",
        r"\subsubsection{最细一层}",
    ]


def test_renders_lists_code_and_quote():
    rendered = render(
        [
            "正文。",
            "- 甲",
            "- 乙",
            "1. 一",
            "```",
            "raw \\ line",
            "```",
            "> 引用",
        ]
    )

    assert rendered == [
        r"正文。\par",
        r"\begin{itemize}",
        r"\item 甲",
        r"\item 乙",
        r"\end{itemize}",
        r"\begin{enumerate}",
        r"\item 一",
        r"\end{enumerate}",
        r"\begin{lstlisting}",
        "raw \\ line",
        r"\end{lstlisting}",
        r"\par{\zihao{-4} 引用\par}",
    ]


def test_renders_figure_with_short_and_long_caption_and_label():
    rendered = render(
        [
            "![图 1-1](图/one.png)",
            "*图 1-1　简题*",
            "*说明行*",
            "![图 1-2](图/two.png)",
            "*图 1-2　只有简题*",
        ]
    )

    assert rendered == [
        r"\begin{figure}[htbp]\centering",
        r"\includegraphics[width=0.96\linewidth,keepaspectratio,height=0.68\textheight]{图/one.png}",
        r"\caption[简题]{简题。说明行}\label{fig:1-1}",
        r"\end{figure}",
        r"\begin{figure}[htbp]\centering",
        r"\includegraphics[width=0.96\linewidth,keepaspectratio,height=0.68\textheight]{图/two.png}",
        r"\caption{只有简题}\label{fig:1-2}",
        r"\end{figure}",
    ]


def test_renders_table_as_xltabular_with_caption_and_label():
    findings = []
    rendered = render(
        ["**表 1-1　示例表**", "", "| 列一 | 列二 |", "|---|---|", "| a | b |"],
        findings=findings,
    )

    assert rendered[0].startswith(r"\begingroup\zihao{-4}")
    assert rendered[1].startswith(r"\begin{xltabular}{\linewidth}{@{}Y{")
    assert rendered[2] == r"\caption{示例表}\label{tab:1-1}\\"
    assert rendered[3] == r"\toprule"
    # 每格末尾带界线记号：行内格子以 & 收尾，xeCJK 认不出那是行末，孤字控制要靠它
    assert rendered[4] == r"列一\primercellend & 列二\primercellend \\"
    assert rendered[5] == r"\midrule"
    assert rendered[6] == r"\endfirsthead"
    assert rendered[7] == r"\multicolumn{2}{r}{（续）}\\"
    assert r"\endlastfoot" in rendered
    assert rendered[-2] == r"\end{xltabular}"
    assert rendered[-1] == r"\endgroup"
    weights = [float(value) for value in re.findall(r"Y\{([\d.]+)\}", rendered[1])]
    assert len(weights) == 2
    assert abs(sum(weights) - 2) < 1e-6
    assert [finding.code for finding in findings].count("table-layout") == 1


def test_scaled_tables_do_not_carry_the_cell_boundary_marker():
    """整表缩小的 tabular 用 l 列、不换行，也就没有孤字，不该带记号。"""
    wide = "|" + "|".join(["很长的列标题文字"] * 9) + "|"
    separator = "|" + "|".join(["---"] * 9) + "|"
    rendered = render([wide, separator, wide])

    assert any(r"\begin{adjustbox}" in line for line in rendered)
    assert r"\primercellend" not in "\n".join(rendered)


def test_uncaptioned_table_gets_no_caption_or_label():
    rendered = render(["| a |", "|---|"])

    assert not any(line.startswith(r"\caption") for line in rendered)
    assert not any(r"\label{" in line for line in rendered)


def test_wide_table_is_wrapped_in_a_landscape_environment():
    wide = "|" + "|".join(["列"] * 9) + "|"
    separator = "|" + "|".join(["---"] * 9) + "|"
    body = "|" + "|".join(["甲" * 40] * 9) + "|"
    rendered = render([wide, separator] + [body] * 3)

    assert rendered[0] == r"\begin{landscape}"
    assert rendered[-1] == r"\end{landscape}"


# ---------------------------------------------------------------- 页眉标记


def test_head_mark_budget_comes_from_the_manifest_typography():
    r"""版心 452.97pt、``\small`` 12.6pt：0.47×452.97÷6.3≈33.8，扣编号预留 10 得 23。"""
    assert tex.head_mark_budget(TYPO) == 23


def test_shorten_head_mark_is_a_no_op_when_the_title_fits():
    assert tex.shorten_head_mark("短题", 10) is None


def test_shorten_head_mark_appends_an_ellipsis():
    assert tex.shorten_head_mark("能力基线：到得了——运输与能源", 10) == "能力基线…"


def test_shorten_head_mark_drops_an_unclosed_quote():
    """截断点落在开引号与闭引号之间时，连残缺的引文一起丢掉，不留半个引号。"""
    assert tex.shorten_head_mark("甲乙“丙丁戊", 10) == "甲乙…"


def test_a_long_heading_title_gets_an_explicit_short_mark():
    """章在 ``\\chapter`` 之后补一条短标记；节还要先压掉 ``\\section`` 自动发的那条
    （``\\rightmark`` 取本页第一条，见 :func:`primer.book.tex._head_mark_lines`）。"""
    marks = []
    rendered = render(
        [
            "@@PART@@第一篇　科学篇",
            "## 第 8 章　未来五至十年：能力基线、任务布局与竞争焦点",
            "### 8.1　能力基线：到得了——运输与能源与资源利用与行星保护",
        ],
        marks=marks,
    )

    # 完整标题照常进正文，短标记紧跟结构命令
    assert rendered[1] == r"\chapter{未来五至十年：能力基线、任务布局与竞争焦点}"
    assert rendered[2] == r"\chaptermark{未来五至十年：能力基…}"
    # 节：先让 \section 自己的标记闭嘴，排完正文再补短标记（宏名不带 @）
    assert rendered[3] == r"\let\primerheadsavemark\sectionmark"
    assert rendered[4] == r"\let\sectionmark\primerheadgobble"
    assert rendered[5] == r"\section{能力基线：到得了——运输与能源与资源利用与行星保护}"
    assert rendered[6] == r"\let\sectionmark\primerheadsavemark"
    assert rendered[7] == r"\sectionmark{能力基线：到得了——运…}"
    assert marks == ["未来五至十年：能力基线、任务布局与竞争焦点", "能力基线：到得了——运输与能源与资源利用与行星保护"]
    # 自动编号由 \chaptermark/\sectionmark 内部补，我们只传题名
    assert "CTEXthe" not in "".join(line for line in rendered if "mark{" in line)


def test_a_short_heading_title_gets_no_mark():
    rendered = render(["## 第 8 章　导言", "### 8.1　范围"])

    assert not any("mark{" in line for line in rendered)
    assert rendered == [r"\chapter{导言}", r"\section{范围}"]


# ---------------------------------------------------------------- 章标题孤字


def test_chapter_title_capacity_accounts_for_the_number_and_the_quad():
    """一行容量 = 版心能放的单位 − 编号 − \\quad：三字章号 33、四字章号 31。"""
    assert tex.chapter_title_capacity(TYPO, tex.chapter_number_text(7)) == 33
    assert tex.chapter_title_capacity(TYPO, tex.chapter_number_text(14)) == 31
    assert tex.chapter_title_capacity(TYPO, tex.appendix_number_text(0)) == 33


def test_the_chapter_number_is_part_of_the_line_capacity():
    """同一个题名：三字章号放得下、四字章号放不下——漏掉编号就会误判成孤字。"""
    title = "驱动机制研判与总体目标设定方法论"

    short = render(["## 第 9 章　" + title], chapter_start=9)
    long = render(["## 第 14 章　" + title], chapter_start=14)

    assert short[0] == rf"\chapter{{{title}}}"
    assert long[0] == r"\chapter[驱动机制研判与总体目标设定方法论]{驱动机制研判与\\总体目标设定方法论}"


def test_a_chapter_title_that_would_leave_one_character_gets_an_explicit_break():
    """第 57 页那一例：自然换行把"现"字单独甩在第二行；改在停顿处显式断行。

    可选参数留住完整题名（目录与页眉用），强制参数只控制版面上的显示——断点落在
    "、"之后，不落在它之前（"、"起行是中文排版的禁忌）。
    """
    rendered = render(["## 第 7 章　三十年回顾：代际、成就与判定性发现"], chapter_start=7)

    assert rendered[0] == (
        r"\chapter[三十年回顾：代际、成就与判定性发现]{三十年回顾：代际、\\成就与判定性发现}"
    )
    # 页眉里的短标记照旧（与断行无关）
    assert rendered[1] == r"\chaptermark{三十年回顾：代际、成…}"


def test_a_chapter_title_that_wraps_cleanly_is_left_alone():
    """第二行还剩五个字：一个字节都不改。"""
    rendered = render(["## 第 11 章　历史回顾：国家规划与使命驱动的三十年检验"], chapter_start=11)

    assert rendered[0] == r"\chapter{历史回顾：国家规划与使命驱动的三十年检验}"


def test_a_chapter_title_that_fits_one_line_is_left_alone():
    rendered = render(["## 第 1 章　导言：科学问题的坐标系"], chapter_start=1)

    assert rendered == [r"\chapter{导言：科学问题的坐标系}"]


def test_an_appendix_chapter_title_is_measured_against_its_lettered_number():
    """附录章的编号是"附录 A"（6 个单位），不是第十五章——两条都要量得对。"""
    title = "科产融合专题：深空探测专用技术产业链调研"

    rendered = render(["@@PART@@附录 A　" + title], appendix=True)

    assert rendered[0] == rf"\chapter{{{title}}}"


def test_a_break_never_lands_inside_a_markdown_link():
    """断点只能落在安全位置：链接是一个整体，劈开就渲染不成。"""
    title = "甲乙[资料](https://example.org/x)丙丁戊己庚辛壬癸"

    parts = tex._balanced_lines(title, 33, 2)

    assert parts is not None
    assert "".join(parts) == title
    assert parts[0].endswith(")")


def test_no_break_is_emitted_when_no_safe_position_fits():
    """整段都在链接里、放不下的位置又不安全时，宁可不改也不冒险溢出。"""
    title = "甲乙[很长很长很长很长很长很长的链接文字](https://example.org/x)丙丁"

    assert tex.chapter_title_lines(title, 8) is None


# ---------------------------------------------------------------- 文档骨架


def test_title_page_foreword_and_catalogs_come_from_the_manifest():
    book = BookInfo(title="书名", subtitle="（副题）", tagline="标签", institution="单位", date="某日")
    front_matter = FrontMatter(
        foreword_heading="凡　例",
        foreword=("一、首段，上限 [{max_reference}]。", "二、末段。"),
        catalog_heading="附录目录",
        appendix_catalog=(CatalogEntry(title="附录 A　示例", label="appA"),),
    )

    title_page = tex.render_title_page(book)
    assert r"{\sffamily\zihao{1}\bfseries 书名}\\[1.2em]" in title_page
    assert r"{\zihao{3} 单位}\\[1em]" in title_page

    foreword = tex.render_foreword(front_matter, 443)
    assert r"\chapter*{凡　例}" in foreword
    assert r"\addcontentsline{toc}{chapter}{凡例}" in foreword
    assert r"\noindent 一、首段，上限 [443]。\par\medskip" in foreword
    assert r"\noindent 二、末段。\par" in foreword

    catalog = tex.render_catalog_pages()
    assert r"\tableofcontents" in catalog
    assert r"\addcontentsline{toc}{chapter}{\contentsname}" not in catalog
    assert r"\listoffigures" in catalog and r"\listoftables" in catalog

    appendix = tex.render_appendix_catalog(front_matter)
    assert r"\noindent 附录 A　示例 \dotfill \pageref{appA}\par" in appendix


def test_the_table_of_contents_does_not_list_itself():
    """目录不把自己列成条目；插图目录、表格目录仍照常列出。"""
    catalog = tex.render_catalog_pages()

    assert r"\tableofcontents" in catalog
    assert r"\addcontentsline{toc}{chapter}{\contentsname}" not in catalog
    # 插图目录与表格目录的条目在（名称由导言区设定）
    assert catalog.count(r"\addcontentsline{toc}{chapter}") == 2


def test_bibliography_is_one_unnumbered_chapter():
    """文献表逐行发射，行首的 markdown 引用标记要剥掉；主题分隔线不发射任何东西。"""
    text = "# 参考文献库\n\n> 版本：v1\n>\n> 结构：A/B\n\n---\n\n## A 战略报告\n\n### A1 小节\n\n[1] 条目。"

    rendered = tex.render_bibliography(text, "总参考文献列表").splitlines()

    assert rendered[1] == r"\chapter*{总参考文献列表}"
    assert rendered[2] == r"\addcontentsline{toc}{chapter}{总参考文献列表}"
    # 引用标记去掉了，仍按同一条目段排；单独一行 ``>`` 什么都不排
    assert rendered[3] == r"\noindent\hangindent=2em 版本：v1\par"
    assert rendered[4] == r"\noindent\hangindent=2em 结构：A/B\par"
    # 主题分隔线与 render_blocks 对 HorizontalRule 的处理一致：什么都不发射
    assert r"\noindent\hangindent=2em ---\par" not in rendered
    assert "---" not in "".join(rendered)
    assert rendered[5] == r"\section*{A 战略报告}"
    assert rendered[6] == r"\subsection*{A1 小节}"
    assert rendered[7] == r"\noindent\hangindent=2em [1] 条目。\par"


# ---------------------------------------------------------------- 导言区


def test_preamble_wires_fonts_size_ladder_and_graphics_path():
    rendered = preamble.render_preamble(Fonts(), Typography(body_font_size="4"), "成果文件/")

    assert r"\documentclass[UTF8,fontset=none]{ctexbook}" in rendered
    assert r"\setCJKmainfont[AutoFakeBold=2.5,AutoFakeSlant=0.15]{Songti SC}" in rendered
    assert r"\setCJKsansfont[AutoFakeBold=2.5]{Heiti SC}" in rendered
    assert r"\setsansfont{Helvetica Neue}" in rendered
    assert r"\graphicspath{{成果文件/}}" in rendered
    assert r"\renewcommand\normalsize{\fontsize{14pt}{17.5pt}\selectfont}" in rendered
    assert r"\renewcommand\small{\fontsize{12.6pt}{15.75pt}\selectfont}" in rendered
    assert r"\newcolumntype{Y}[1]" in rendered
    assert r"\setcounter{tocdepth}{1}" in rendered


def test_preamble_ragged_bottom_keeps_slack_out_of_heading_skips():
    rendered = preamble.render_preamble(Fonts(), Typography(), "成果文件/")

    assert r"\raggedbottom" in rendered
    assert r"\flushbottom" not in rendered


def test_preamble_turns_on_xecjk_widow_control_inside_table_cells():
    r"""中文孤字控制：末行只剩一个汉字时整段重排。

    两种行末都要声明：``\\``（行末格子）与 ``\primercellend``（行内格子的界线记号，
    由发射器补在每个 Y 列格子末尾）。只给 ``\\`` 时行内各格漏掉——``&`` 是 catcode 4，
    CheckSingle 认不出来（实测：两列窄表格，首格末行仍是一个孤字）。
    """
    rendered = preamble.render_preamble(Fonts(), Typography(), "成果文件/")

    assert r"\newcommand{\primercellend}{\relax}" in rendered
    assert (
        r"\xeCJKsetup{CheckSingle=true,WidowPenalty=10000,"
        r"NewLineCS+={\\},NewLineCS+={\primercellend}}"
    ) in rendered


def test_preamble_accepts_point_sizes_and_zihao_codes():
    assert "\\fontsize{12pt}{15pt}" in preamble.render_size_ladder("12pt")
    assert "\\fontsize{10.5pt}{13.125pt}" in preamble.render_size_ladder("5")
    assert preamble.body_points("12") == 12
    assert preamble.body_points("-4") == 12


def test_preamble_sans_fake_bold_is_independent_of_main_and_mono():
    rendered = preamble.render_preamble(Fonts(), Typography(cjk_sans_fake_bold="0"), "成果文件/")

    assert r"\setCJKmainfont[AutoFakeBold=2.5,AutoFakeSlant=0.15]{Songti SC}" in rendered
    assert r"\setCJKsansfont[AutoFakeBold=0]{Heiti SC}" in rendered
    assert r"\setCJKmonofont[AutoFakeBold=2.5]{Songti SC}" in rendered


def test_preamble_sans_fake_bold_falls_back_to_the_shared_knob():
    rendered = preamble.render_preamble(Fonts(), Typography(cjk_fake_bold="3"), "成果文件/")

    assert r"\setCJKmainfont[AutoFakeBold=3,AutoFakeSlant=0.15]{Songti SC}" in rendered
    assert r"\setCJKsansfont[AutoFakeBold=3]{Heiti SC}" in rendered
    assert r"\setCJKmonofont[AutoFakeBold=3]{Songti SC}" in rendered
