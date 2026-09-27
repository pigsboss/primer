# -*- coding: utf-8 -*-
"""块对象与文献库文本 → LaTeX。

行内变换（转义、unicode 替换、链接与行内代码的脚注化、加粗斜体、过长 ASCII
串的断点）集中在 :func:`render_inline`；块渲染集中在 :func:`render_blocks`；封面、凡例、目录、
附录目录与总参考文献等文档骨架由本模块末尾的几个函数生成。

这里做三件"编者的活儿"：

* 标题剥掉作者手写的编号（``## 第 6 章　导言`` → ``\\chapter{导言}``），章号交给
  ``ctexbook`` 连续自动编号；
* 图、表题注剥掉手写编号，产出 ``\\caption`` 与 ``\\label{fig:2-13}``；
* 表格按 :mod:`primer.book.tables` 的方案落版（竖排、横向页、或等比缩小），
  代码块走 ``listings`` 的自动折行。
"""

from __future__ import annotations

import re
from typing import List, Optional, Sequence, Tuple

from .findings import Finding
from .manifest import BookInfo, CatalogEntry, FrontMatter, Typography
from .markdown import (
    HEADING_COMMANDS,
    HORIZONTAL_RULE,
    LIST_ENVIRONMENTS,
    CodeBlock,
    Figure,
    Heading,
    HorizontalRule,
    ListBlock,
    Paragraph,
    PartBanner,
    Quote,
    Table,
    strip_heading_number,
)
from . import tables
from . import preamble
from .preamble import TexTemplate

UNICODE_REPLACEMENTS = {
    "α": r"$\alpha$",
    "β": r"$\beta$",
    "γ": r"$\gamma$",
    "μ": r"$\mu$",
    "×": r"$\times$",
    "≥": r"$\geq$",
    "≤": r"$\leq$",
    "→": r"$\to$",
    "±": r"$\pm$",
    "Ⅰ": "I",
    "Ⅱ": "II",
    "Ⅲ": "III",
}

# URL → LaTeX 的转义表。URL 出现在 \footnote 的参数里，而 \footnote 先按正常
# catcode 扫描参数，因此不能用 \url 保护特殊字符（URL 里的 % 会把整行变成注释，
# 实测会把文档从那里截断）；手工转义 + 分隔符后插断行点才是可靠的做法。
URL_SPECIALS = {
    "\\": r"\textbackslash{}",
    "{": r"\{",
    "}": r"\}",
    "$": r"\$",
    "&": r"\&",
    "#": r"\#",
    "%": r"\%",
    "_": r"\_",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}
URL_BREAK_AFTER = "/.-_?&=+,:@)%"
# 断行阈值：分隔符（上面那串）后面本来就能断；这里再给"无分隔符的长串"补断点——
# URL 里每 10 个非分隔符字符插一个，正文里 ≥20 字符的 ASCII 字母数字串同样每 10 个插一个。
# 一条 32 位十六进制的路径末段就是一个整块，不补点就只能溢出到版心外。
URL_RUN_BREAK = 10
PROSE_RUN_MIN = 20
PROSE_RUN_BREAK = 10
ASCII_ALNUM_RUN_RE = re.compile(r"[A-Za-z0-9]+")


def render_url(url: str) -> str:
    """URL → 可断行的等宽文本，供脚注使用。

    逐字符转义（转义序列不参与段长计算），分隔符后与长段内都插 ``\\allowbreak{}``。
    """
    pieces: List[str] = []
    run = 0
    for index, char in enumerate(url):
        pieces.append(URL_SPECIALS.get(char, char))
        if char in URL_BREAK_AFTER:
            pieces.append(r"\allowbreak{}")
            run = 0
            continue
        run += 1
        if run >= URL_RUN_BREAK and index + 1 < len(url):
            pieces.append(r"\allowbreak{}")
            run = 0
    return r"\texttt{" + "".join(pieces) + "}"


def break_long_runs(text: str) -> str:
    """给正文里过长的 ASCII 字母数字串补断点。

    LaTeX 只在空格、连字符等位置断行，技术记号、长标识符这类整块串会直接溢出；
    长度达 ``PROSE_RUN_MIN`` 的串每 ``PROSE_RUN_BREAK`` 个字符插一个 ``\\allowbreak{}``。
    只认字母数字，反斜杠、花括号与哨兵字符都自然截断段，不会插进命令名里。
    """

    def replace(matched: re.Match) -> str:
        run = matched.group(0)
        if len(run) < PROSE_RUN_MIN:
            return run
        pieces: List[str] = []
        for index, char in enumerate(run):
            pieces.append(char)
            if (index + 1) % PROSE_RUN_BREAK == 0 and index + 1 < len(run):
                pieces.append(r"\allowbreak{}")
        return "".join(pieces)

    return ASCII_ALNUM_RUN_RE.sub(replace, text)


# 分篇标题里由清单给出的"第 X 篇　"前缀：篇号由 LaTeX 自动给，标题里不必再写。
PART_PREFIX_RE = re.compile(r"^第\s*[一二三四五六七八九十]+\s*篇[　\s]*")
APPENDIX_PREFIX_RE = re.compile(r"^附录\s*[A-Z][　\s]*")


def escape_latex(text: str) -> str:
    """转义 LaTeX 特殊字符。"""
    text = text.replace("&", r"\&").replace("%", r"\%").replace("#", r"\#")
    text = text.replace("_", r"\_").replace("$", r"\$")
    return text


def replace_unicode(text: str) -> str:
    """把正文里的希腊字母与几个符号换成 LaTeX 写法。"""
    for source, target in UNICODE_REPLACEMENTS.items():
        text = text.replace(source, target)
    return text


def render_inline(text: str) -> str:
    """行内 markdown → LaTeX。

    先把链接、行内代码、行内公式换成哨兵字符（避免其内容被后续转义与强调处理
    波及），再转义、替换 unicode、处理强调，最后还原三批哨兵。链接渲染为
    ``\\footnote{\\texttt{url}}``。
    """
    links: List[tuple] = []

    def stash_link(matched: re.Match) -> str:
        links.append((matched.group(1), matched.group(2)))
        return f"\x00{len(links) - 1}\x00"

    text = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)(?:\s+\"[^\"]*\")?\)", stash_link, text)
    text = re.sub(
        r"\[([^\]]+)\]\((/[^)\s]+(?:\([^)]*\))?[^)\s]*)(?:\s+\"[^\"]*\")?\)", stash_link, text
    )
    # 相对目标：装配前会把工程根前缀抹掉，本地存档链接于是从 ``/Users/…/x.pdf``
    # 变成 ``参考资料/…/x.pdf``。只认协议与绝对路径的话，这类链接就匹配不上，
    # markdown 原文会整段印进 PDF。图片语法 ``![…](…)`` 不在此列。
    text = re.sub(
        r"(?<!!)\[([^\]]+)\]\(([^)\s/][^)\s]*)(?:\s+\"[^\"]*\")?\)", stash_link, text
    )

    codes: List[str] = []

    def stash_code(matched: re.Match) -> str:
        codes.append(matched.group(1))
        return f"\x01{len(codes) - 1}\x01"

    text = re.sub(r"`([^`]+)`", stash_code, text)

    maths: List[str] = []

    def stash_math(matched: re.Match) -> str:
        maths.append(matched.group(1))
        return f"\x02{len(maths) - 1}\x02"

    text = re.sub(r"\$([^$\n]+?)\$", stash_math, text)

    text = replace_unicode(escape_latex(text))
    text = re.sub(r"\*\*(.+?)\*\*", r"\\textbf{\1}", text)
    text = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"\\emph{\1}", text)
    text = break_long_runs(text)
    text = re.sub("\x02(\\d+)\x02", lambda m: "$" + maths[int(m.group(1))] + "$", text)

    def restore_link(matched: re.Match) -> str:
        label, url = links[int(matched.group(1))]
        return break_long_runs(escape_latex(label)) + r"\footnote{" + render_url(url) + "}"

    text = re.sub("\x00(\\d+)\x00", restore_link, text)
    text = re.sub("\x01(\\d+)\x01", lambda m: r"\texttt{" + escape_latex(codes[int(m.group(1))]) + "}", text)
    return text


def part_title(title: str, appendix: bool = False) -> str:
    """篇标题：去掉清单里手写的"第 X 篇"／"附录 X"前缀（编号由 LaTeX 给）。"""
    stripped = PART_PREFIX_RE.sub("", title, count=1).strip()
    if appendix:
        stripped = APPENDIX_PREFIX_RE.sub("", stripped, count=1).strip()
    return stripped or title.strip()


# ---------------------------------------------------------------- 表格


def render_table(
    rows: Sequence[Sequence[str]],
    caption: str,
    label: Optional[str],
    typography: Typography,
    location: str,
    findings: List[Finding],
) -> List[str]:
    """表格 → xltabular（可换行、可跨页）／adjustbox 缩小，必要时套横向页。"""
    if not rows:
        return []
    layout, layout_findings = tables.plan_table(rows, typography, location)
    findings.extend(layout_findings)
    findings.append(
        Finding(
            code="table-layout",
            severity="info",
            message=f"{layout.orientation} {layout.mode} at zihao {layout.font_code} ({layout.reason})",
            location=location,
        )
    )
    columns = len(rows[0])
    out: List[str] = []
    if layout.orientation == "landscape":
        out.append(r"\begin{landscape}")
    if layout.mode == "scale":
        out.extend(_scaled_table(rows, caption, label, layout))
    else:
        out.extend(_xltabular(rows, caption, label, layout, columns))
    if layout.orientation == "landscape":
        out.append(r"\end{landscape}")
    return out


def _xltabular(
    rows: Sequence[Sequence[str]], caption: str, label: Optional[str], layout, columns: int
) -> List[str]:
    weights = [width * columns for width in layout.columns]
    spec = "@{}" + "".join(rf"Y{{{weight:.4f}}}" for weight in weights) + "@{}"
    header = " & ".join(_cell(cell) for cell in rows[0]) + r" \\"
    body = [" & ".join(_cell(cell) for cell in row) + r" \\" for row in rows[1:]]

    out = [r"\begingroup\zihao{" + layout.font_code + "}", r"\begin{xltabular}{\linewidth}{" + spec + "}"]
    if caption:
        out.append(r"\caption{" + render_inline(caption) + "}" + _label(label) + r"\\")
    out.extend([r"\toprule", header, r"\midrule", r"\endfirsthead"])
    if caption:
        out.append(rf"\multicolumn{{{columns}}}{{r}}{{（续）}}\\")
    out.extend([r"\toprule", header, r"\midrule", r"\endhead", r"\bottomrule", r"\endlastfoot"])
    out.extend(body)
    out.append(r"\end{xltabular}")
    out.append(r"\endgroup")
    return out


def _scaled_table(
    rows: Sequence[Sequence[str]], caption: str, label: Optional[str], layout
) -> List[str]:
    """整表等比缩小：用自然列宽的 tabular，套 adjustbox 缩到版心宽。"""
    spec = "@{}" + "l" * len(rows[0]) + "@{}"
    out = [r"\begin{table}[htbp]\centering"]
    if caption:
        out.append(r"\caption{" + render_inline(caption) + "}" + _label(label))
    out.append(rf"\begin{{adjustbox}}{{max width=\linewidth,scale={layout.scale:.3f}}}")
    out.append(r"{\zihao{" + layout.font_code)
    out.append(r"\begin{tabular}{" + spec + "}")
    out.append(r"\toprule")
    out.append(" & ".join(_cell(cell) for cell in rows[0]) + r" \\")
    out.append(r"\midrule")
    out.extend(" & ".join(_cell(cell) for cell in row) + r" \\" for row in rows[1:])
    out.append(r"\bottomrule")
    out.append(r"\end{tabular}")
    out.append("}")
    out.append(r"\end{adjustbox}")
    out.append(r"\end{table}")
    return out


def _cell(text: str) -> str:
    return render_inline(text)


def _label(label: Optional[str]) -> str:
    return rf"\label{{{label}}}" if label else ""


# ---------------------------------------------------------------- 页眉标记

# 页眉里一条标记（自动编号 + \quad + 题名）允许占用的版心宽度比例。左右两条标记
# 各占不到一半时，即使都排满也不会在版心两端叠印（fancyhdr 把两条标记各排进一个
# 满版心的盒子再叠放，见 checks.running_head_collision）。
MARK_HEAD_FRACTION = 0.47
# 自动编号与编号后的 \quad 预留的显示单位："第十四章" = 8，\quad = 1em = 2 单位。
MARK_NUMBER_RESERVE_UNITS = 10
# 截断标记末尾的省略号；CJK 字体里按一个全角字（2 个显示单位）算。
MARK_ELLIPSIS = "…"
MARK_ELLIPSIS_UNITS = 2
# 页眉用 \small，按正文字号的 0.9 倍计（与 preamble.SIZE_LADDER 的 small 一致）。
MARK_FONT_RATIO = 0.9
# 1 TeX pt = 72.27/72 bp：版心宽度走 tables 的度量（bp），而字号是 TeX pt。
TEX_PT_PER_BP = 72.27 / 72.0

# 哪些结构命令会在页眉里留下标记（fancyhdr 的 book 体例只认章与节）。
HEADING_MARK_COMMANDS = {"chapter": "chaptermark", "section": "sectionmark"}
MARK_STRIP_RE = re.compile(r"[`*]")
MARK_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")


def head_mark_budget(typography: Typography) -> int:
    """页眉里一条标记的**题名部分**可用的显示单位数。

    ``\\headwidth`` 就是版心宽：竖排版面宽 = 纸张短边 − 2×边距，走
    :func:`primer.book.tables._metrics` 的度量（bp）。``\\headwidth`` 与字号同为
    TeX pt，所以先把 bp 换回 TeX pt。字号取 ``\\small``（正文的 0.9 倍），一个
    显示单位是半个字宽；再扣掉自动编号与编号后的 ``\\quad`` 预留的
    :data:`MARK_NUMBER_RESERVE_UNITS`。
    """
    headwidth_pt = tables._metrics(typography, "portrait").width * TEX_PT_PER_BP
    font_pt = preamble.body_points(typography.body_font_size) * MARK_FONT_RATIO
    units = MARK_HEAD_FRACTION * headwidth_pt / (0.5 * font_pt)
    return max(int(units) - MARK_NUMBER_RESERVE_UNITS, 4)


def head_mark_source(text: str) -> str:
    """标题文本 → 页眉用的纯文本：去掉强调标记、行内代码与链接目标。

    页眉里的 ``\\chaptermark`` 由 ctex 的 ``\\MakeUppercase`` 包着，行内脚注之类
    的构造放进去只会坏事；标题里的链接在这里退化成它的文字。
    """
    return MARK_STRIP_RE.sub("", MARK_LINK_RE.sub(r"\1", text))


def shorten_head_mark(title: str, budget: int) -> Optional[str]:
    """题名截到 ``budget`` 个显示单位以内；放得下时返回 ``None``。

    省略号占 :data:`MARK_ELLIPSIS_UNITS` 个显示单位；截断点若落在一对中文引号
    中间（开引号多于闭引号），把那个未闭合的开引号连同其后残缺的内容一并丢掉，
    免得页眉里出现半个引号、让 ``quote-direction`` 在正文上误报。
    """
    if tables.display_width(title) <= budget:
        return None
    limit = budget - MARK_ELLIPSIS_UNITS
    used = 0
    cut = 0
    for index, char in enumerate(title):
        width = tables.display_width(char)
        if used + width > limit:
            break
        used += width
        cut = index + 1
    return _drop_unclosed_quote(title[:cut]).rstrip() + MARK_ELLIPSIS


def _drop_unclosed_quote(text: str) -> str:
    """丢掉最后一个未闭合的中文开引号及其后的内容。"""
    if text.count("“") <= text.count("”"):
        return text
    return text[: text.rfind("“")]


def _head_mark_lines(
    command: str, title: str, budget: int, marks: Optional[List[str]]
) -> Tuple[List[str], List[str]]:
    r"""返回（结构命令之前要发的行，结构命令之后要发的行）；不需要短标记时两边都空。

    走的是 ctexbook 自己那条宏：编号（``\\CTEXthechapter``／``\\CTEXthesection``）
    与 ``\\quad`` 由宏内部补上，所以自动编号不会丢，而我们只截题名。

    发的**位置**章与节不同，因为两者取标记的方向相反（\leftmark 取本页最后一条
    ``2e-left``，\rightmark 取本页**第一条** ``2e-right``）：

    * 章：紧跟 ``\\chapter`` 之后再发一条即可——它是本页最后一条 ``2e-left``；
    * 节：``\\section`` 自己会先发一条完整题名的标记，而 ``\\rightmark`` 认第一条，
      所以必须先把自动那条压掉（临时把 ``\\sectionmark`` 换成导言区定义的
      ``\\primerheadgobble``），我们那条短标记才排得上第一。
    """
    mark = HEADING_MARK_COMMANDS.get(command)
    if mark is None:
        return [], []
    short = shorten_head_mark(head_mark_source(title), budget)
    if short is None:
        return [], []
    if marks is not None:
        marks.append(title)
    call = rf"\{mark}{{{render_inline(short)}}}"
    if command == "chapter":
        return [], [call]
    # 宏名不带 @：正文里 @ 不是字母，\@gobble 会被拆成 \@ + "gobble"。
    return (
        [r"\let\primerheadsavemark\sectionmark", r"\let\sectionmark\primerheadgobble"],
        [r"\let\sectionmark\primerheadsavemark", call],
    )


# ---------------------------------------------------------------- 块


def render_blocks(
    blocks: Sequence[object],
    typography: Typography,
    findings: List[Finding],
    part_label: Optional[str] = None,
    appendix: bool = False,
    location: str = "",
    marks: Optional[List[str]] = None,
) -> List[str]:
    """块对象序列 → LaTeX 行序列。

    ``part_label`` 给出该篇标签时，篇标题附 ``\\label``，供附录目录用
    ``\\pageref`` 引用页码。``appendix`` 为真时这一篇按"一个附录"排版：篇题成为
    附录章（``\\chapter``，字母编号），篇内的二级标题降为节——这样"附录 A"独立
    编号、其下 A.1、A.2 自成体系的体例由 LaTeX 自动维持。

    ``marks`` 给出一张表时，被截短过页眉标记的章／节题名依次记进去，供调用方
    汇总成一条 ``running-head-truncated`` 发现。章、节的题名太长会与另一条标记
    叠印（LaTeX 不报 overfull），所以过长的题名在这里就截短，并紧接着结构命令
    发一条显式标记。
    """
    out: List[str] = []
    labelled = False
    budget = head_mark_budget(typography)
    for index, block in enumerate(blocks):
        if isinstance(block, PartBanner):
            raw_title = part_title(block.title, appendix)
            command = "chapter" if appendix else "part"
            before, after = _head_mark_lines(command, raw_title, budget, marks)
            out.extend(before)
            out.append(rf"\{command}{{{render_inline(raw_title)}}}")
            out.extend(after)
            if part_label and not labelled:
                out.append(rf"\label{{{part_label}}}")
                labelled = True
        elif isinstance(block, Heading):
            level = block.level + 1 if appendix else block.level
            command = HEADING_COMMANDS.get(level)
            if command:
                raw_title = strip_heading_number(block.text)
                before, after = _head_mark_lines(command, raw_title, budget, marks)
                out.extend(before)
                out.append(rf"\{command}{{{render_inline(raw_title)}}}")
                out.extend(after)
        elif isinstance(block, Paragraph):
            out.append(render_inline(block.text) + r"\par")
        elif isinstance(block, ListBlock):
            environment = LIST_ENVIRONMENTS[block.ordered]
            out.append(f"\\begin{{{environment}}}")
            out.extend(f"\\item {render_inline(item)}" for item in block.items)
            out.append(f"\\end{{{environment}}}")
        elif isinstance(block, CodeBlock):
            out.append(r"\begin{lstlisting}")
            out.extend(block.lines)
            out.append(r"\end{lstlisting}")
        elif isinstance(block, Quote):
            out.append(r"\par{\zihao{-4} " + render_inline(block.text) + r"\par}")
        elif isinstance(block, Figure):
            out.append("\\begin{figure}[htbp]\\centering")
            out.append(
                "\\includegraphics[width=0.96\\linewidth,keepaspectratio,"
                f"height=0.68\\textheight]{{{block.path}}}"
            )
            if block.caption or block.label:
                caption = render_inline(block.caption) if block.caption else "（无题）"
                if block.note:
                    out.append(
                        f"\\caption[{caption}]{{{caption}。{render_inline(block.note)}}}"
                        + _label(block.label)
                    )
                else:
                    out.append(f"\\caption{{{caption}}}" + _label(block.label))
            out.append("\\end{figure}")
        elif isinstance(block, Table):
            out.extend(
                render_table(
                    block.rows,
                    block.caption,
                    block.label,
                    typography,
                    f"{location}#{index + 1}",
                    findings,
                )
            )
        elif isinstance(block, HorizontalRule):
            continue
    return out


# ---------------------------------------------------------------- 文档骨架

_TITLE_PAGE = TexTemplate(
    r"""
\begin{titlepage}
\vspace*{3.5cm}
\begin{center}
{\sffamily\zihao{1}\bfseries @title}\\[1.2em]
{\sffamily\zihao{2}\bfseries @subtitle}\\[3em]
{\zihao{4} @tagline}\\[6em]
{\zihao{3} @institution}\\[1em]
{\zihao{4} @date}
\end{center}
\end{titlepage}
"""
)

_FOREWORD = TexTemplate(
    r"""
\clearpage\phantomsection
\chapter*{@heading}
\addcontentsline{toc}{chapter}{@toc_entry}
@paragraphs
"""
)

# 目录本身不列进目录（正文里"目录"这一页没有可翻查的价值，列出来只是多余的自我指涉）；
# 插图目录与表格目录仍列进去——它们是真正需要从前置页翻回去找的清单。
_CATALOG_PAGES = TexTemplate(
    r"""
\clearpage\phantomsection
\tableofcontents
\clearpage\phantomsection
\addcontentsline{toc}{chapter}{\listfigurename}
\listoffigures
\clearpage\phantomsection
\addcontentsline{toc}{chapter}{\listtablename}
\listoftables
"""
)

_APPENDIX_CATALOG = TexTemplate(
    r"""
\clearpage\phantomsection
\chapter*{@heading}
\addcontentsline{toc}{chapter}{@heading}
@entries
"""
)

MAX_REFERENCE_TOKEN = "{max_reference}"


def render_title_page(book: BookInfo) -> str:
    """封面。"""
    return _TITLE_PAGE.substitute(
        title=book.title,
        subtitle=book.subtitle,
        tagline=book.tagline,
        institution=book.institution,
        date=book.date,
    )


def render_foreword(front_matter: FrontMatter, max_reference: int) -> str:
    """凡例。段内的 ``{max_reference}`` 替换为文献库最大编号。"""
    paragraphs = []
    last = len(front_matter.foreword) - 1
    for index, text in enumerate(front_matter.foreword):
        text = text.replace(MAX_REFERENCE_TOKEN, str(max_reference))
        paragraphs.append(r"\noindent " + text + (r"\par" if index == last else r"\par\medskip"))
    return _FOREWORD.substitute(
        toc_entry=front_matter.foreword_heading.replace("　", ""),
        heading=front_matter.foreword_heading,
        paragraphs="\n".join(paragraphs),
    )


def render_catalog_pages() -> str:
    """目录、插图目录、表格目录（名称由导言区设定）。"""
    return _CATALOG_PAGES.substitute()


def render_appendix_catalog(front_matter: FrontMatter) -> str:
    """附录目录（页码经 ``\\pageref`` 引用正文中的标签）。"""
    entries = "\n".join(_appendix_catalog_entry(entry) for entry in front_matter.appendix_catalog)
    return _APPENDIX_CATALOG.substitute(heading=front_matter.catalog_heading, entries=entries)


def _appendix_catalog_entry(entry: CatalogEntry) -> str:
    return rf"\noindent {entry.title} \dotfill \pageref{{{entry.label}}}\par"


def render_bibliography(text: str, section_title: str) -> str:
    """总参考文献列表：一层无编号章，条目按 ``[n]`` 编号排列（沿用全书编号）。

    这里逐行读文献库（不走块解析器），所以行首的 markdown 引用标记 ``>`` 要手工
    剥掉——文献库开头的版本／结构说明就是引用块，原样发射会把 ``> 版本：…``
    印进 PDF。引用块仍按普通条目排（同一段 ``\\noindent\\hangindent=2em …\\par``），
    不另起环境。

    主题分隔线（``---``）在这里丢弃、什么都不发射——:func:`render_blocks` 撞到
    ``HorizontalRule`` 也是这样 ``continue`` 的，同一套 markdown 语法在一本书里
    不能两种排法。这是有意的编辑决定，不要"顺手恢复"它：原样发射会把它排成一条
    破折号，成书 PDF 里就留下整行只有 ``—`` 的空行。
    """
    out = [r"\clearpage\phantomsection"]
    out.append(r"\chapter*{" + render_inline(section_title) + "}")
    out.append(r"\addcontentsline{toc}{chapter}{" + render_inline(section_title) + "}")
    for line in text.splitlines():
        stripped = line.rstrip()
        if not stripped:
            continue
        if stripped.startswith(">"):
            stripped = stripped.lstrip(">").lstrip()
            if not stripped:
                continue
        if stripped.strip() == HORIZONTAL_RULE:
            continue
        if stripped.startswith("## "):
            out.append(r"\section*{" + render_inline(stripped[3:].strip()) + "}")
        elif stripped.startswith("### "):
            out.append(r"\subsection*{" + render_inline(stripped[4:].strip()) + "}")
        elif stripped.startswith("# "):
            continue
        else:
            out.append(r"\noindent\hangindent=2em " + render_inline(stripped) + r"\par")
    return "\n".join(out)
