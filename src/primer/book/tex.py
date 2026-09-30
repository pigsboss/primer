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


# 链接的两档渲染。成书把 ``[标签](url)`` 排成 ``标签\footnote{\texttt{url}}``——纸面上
# 脚注是标准做法；幻灯片的版面上，一行 URL 脚注既多余又吃掉版面，所以那一档只留标签文字，
# 丢弃的目标由调用方记进发现（不静默丢信息）。
LINK_FOOTNOTE = "footnote"
LINK_LABEL = "label"


def render_inline(
    text: str,
    links: str = LINK_FOOTNOTE,
    dropped_links: Optional[List[Tuple[str, str]]] = None,
) -> str:
    """行内 markdown → LaTeX。

    先把链接、行内代码、行内公式换成哨兵字符（避免其内容被后续转义与强调处理
    波及），再转义、替换 unicode、处理强调，最后还原三批哨兵。``links`` 选
    :data:`LINK_FOOTNOTE` 时链接渲染为 ``\\footnote{\\texttt{url}}``（成书）；
    选 :data:`LINK_LABEL` 时只发射标签文字，目标追加进 ``dropped_links``（幻灯片）。
    """
    if links not in (LINK_FOOTNOTE, LINK_LABEL):
        raise ValueError(f"unknown link rendering mode: {links!r}")
    mode = links
    found: List[tuple] = []

    def stash_link(matched: re.Match) -> str:
        found.append((matched.group(1), matched.group(2)))
        return f"\x00{len(found) - 1}\x00"

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
        label, url = found[int(matched.group(1))]
        rendered = break_long_runs(escape_latex(label))
        if mode == LINK_LABEL:
            if dropped_links is not None:
                dropped_links.append((label, url))
            return rendered
        return rendered + r"\footnote{" + render_url(url) + "}"

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


# 单元格末尾的界线记号，供 xeCJK 的孤字控制（CheckSingle）认出行末位置。
#
# CheckSingle 只在能"看见"段末/行末时，才在段末三字之间插 WidowPenalty。行内非末列
# 的单元格以对齐符 & 收尾，而 & 是 catcode 4、CheckSingle 不把它当界线；末列以 \\
# 收尾，\\ 可以经 NewLineCS 声明（见导言区模板），但行内的 & 不行。于是发射器在
# 每个 Y 列的单元格末尾放一个记号，导言区把记号名加进 NewLineCS——孤字控制这才覆盖
# 到表格的每一个单元格。记号展开为空（见模板里的 \primercellend 定义），不动段落
# 结构，也不改行高（实测：p{1.2cm}、10 字单元格，加与不加重排结果与行位置逐字相同）。
CELL_BOUNDARY = r"\primercellend"


def _xltabular(
    rows: Sequence[Sequence[str]], caption: str, label: Optional[str], layout, columns: int
) -> List[str]:
    weights = [width * columns for width in layout.columns]
    spec = "@{}" + "".join(rf"Y{{{weight:.4f}}}" for weight in weights) + "@{}"
    header = " & ".join(_cell(cell) + CELL_BOUNDARY for cell in rows[0]) + r" \\"
    body = [
        " & ".join(_cell(cell) + CELL_BOUNDARY for cell in row) + r" \\" for row in rows[1:]
    ]

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

# ---------------------------------------------------------------- 章标题断行：常量

# 章标题的字号：导言区模板给 ``chapter/format`` 定的 ``\zihao{2}``（二号）。
# 与 :func:`primer.book.preamble.body_points` 是同一张字号表，不写死磅值。
CHAPTER_ZIHAO = "2"
# 自动编号的文本形态：非附录章是 ``第七章``／``第十四章``（ctex 的
# ``\CTEXthechapter``），附录章是 ``附录 A``（``\appendix`` 后重新编号）。
CHAPTER_NUMBER_PREFIX = "第"
CHAPTER_NUMBER_SUFFIX = "章"
APPENDIX_NUMBER_PREFIX = "附录 "
# 编号与题名之间的 ``\quad``（ctex 的 ``chapter/aftername``）：一个全角字宽。
CHAPTER_AFTERNAME_UNITS = 2
# 最后一行只剩这么多显示单位（一个全角字）就是"标题末尾吊了一个孤字"。
HEADING_ORPHAN_UNITS = 2
CHINESE_DIGITS = "一二三四五六七八九十"
# 行首禁则：这些字符不许出现在一行的开头，因此断点不能落在它们之前（、和 ，同属
# 此列——"、"起行是中文排版里最刺眼的一种断法）。
LINE_START_FORBIDDEN = "，。、；：？！）］｝」』》〉”’…—·%,.;:!?)]}"
# 断行的优先落点：标点与常见连词之后。中文里那正是句子自然停顿的地方，按它断行
# 读起来是"短语 + 短语"；没有这样的落点时才退回按宽度均分，宁可"劈词"也不要溢出。
PREFERRED_BREAK_AFTER = "，。、；：？！）］｝」』》〉”’…—" + "与和及或"
# 标题里不能从中断开的构造：markdown 链接、行内代码、行内公式、强调。断点落在
# 它们中间会把标记劈成两半，所以这些位置一律不算"安全断点"。
UNSAFE_BREAK_SPANS = (
    re.compile(r"\[[^\]]*\]\([^)]*\)"),
    re.compile(r"`[^`]*`"),
    re.compile(r"\$[^$\n]*\$"),
    re.compile(r"\*\*[^*]*\*\*"),
    re.compile(r"\*[^*\n]*\*"),
)


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


# ---------------------------------------------------------------- 章标题断行：判据与发射


def chinese_number(number: int) -> str:
    """1—99 的汉字写法：章号用（``1`` → ``一``、``14`` → ``十四``）。"""
    if number <= 0:
        return str(number)
    if number <= 10:
        return CHINESE_DIGITS[number - 1]
    if number < 20:
        return "十" + CHINESE_DIGITS[number - 11]
    tens, ones = divmod(number, 10)
    return CHINESE_DIGITS[tens - 1] + "十" + (CHINESE_DIGITS[ones - 1] if ones else "")


def chapter_number_text(number: int) -> str:
    """非附录章的自动编号文本（与 ctex 的 ``\\CTEXthechapter`` 一致）。"""
    return f"{CHAPTER_NUMBER_PREFIX}{chinese_number(number)}{CHAPTER_NUMBER_SUFFIX}"


def appendix_number_text(index: int) -> str:
    """附录章的自动编号文本：``附录 A``、``附录 B``…（``\\appendix`` 后重编号）。"""
    return APPENDIX_NUMBER_PREFIX + chr(ord("A") + index)


def chapter_title_capacity(typography: Typography, number_text: str) -> int:
    """一章标题的**一行**能容纳的显示单位数（已扣掉自动编号与 ``\\quad``）。

    字号取模板给章标题定的 :data:`CHAPTER_ZIHAO`，版心宽走
    :func:`primer.book.tables._metrics` 的度量——与页眉标记预算
    (:func:`head_mark_budget`) 同一套算术。编号本身占掉的单位由
    :func:`primer.book.tables.display_width` 给出：``第七章`` 是 6、``第十四章``
    是 8，差出的一格正是"同一个题名在不同章号下换行不同"的原因。量标题之前必须
    先扣掉它——漏掉这一项会把每个标题都算宽一格，把本来好好的标题误判成孤字。
    """
    font = preamble.body_points(CHAPTER_ZIHAO)
    capacity = tables._capacity(tables._metrics(typography, "portrait"), font)
    return int(capacity - tables.display_width(number_text) - CHAPTER_AFTERNAME_UNITS)


def _safe_break_positions(title: str) -> List[bool]:
    """``title[i]`` 之前能不能插 ``\\``（返回下标 0…len(title) 的布尔表）。

    落在链接／行内代码／行内公式／强调内部的字符一律不许断——它们在 markdown 里
    是一个整体，劈开就渲染不成。
    """
    safe = [True] * (len(title) + 1)
    for pattern in UNSAFE_BREAK_SPANS:
        for matched in pattern.finditer(title):
            for position in range(matched.start() + 1, matched.end()):
                safe[position] = False
    return safe


def _wrap_by_units(title: str, capacity: int) -> List[str]:
    """按显示宽度贪心折行——用来预演 LaTeX 的自然换行。"""
    lines: List[str] = []
    current = ""
    used = 0
    for char in title:
        width = tables.display_width(char)
        if current and used + width > capacity:
            lines.append(current)
            current = ""
            used = 0
        current += char
        used += width
    if current:
        lines.append(current)
    return lines


def _balanced_lines(title: str, capacity: int, count: int) -> Optional[List[str]]:
    """把标题按显示宽度均分成 ``count`` 行；断点只能落在安全位置。

    候选断点要同时满足三件事：不在链接／代码／公式／强调内部（:func:`_safe_break_positions`）、
    不使下一行以 :data:`LINE_START_FORBIDDEN` 里的标点起行、且前一段放得下
    ``capacity``。在候选里先看 :data:`PREFERRED_BREAK_AFTER`（标点与连词之后），
    再退回按累计宽度最接近均分点的位置。

    有任何一段放不下 ``capacity``、或找不到安全断点，就返回 ``None``——宁可不改，
    也不发射一个可能溢出版心的断行。
    """
    widths = [tables.display_width(char) for char in title]
    cumulative = [0] * (len(title) + 1)
    for index, width in enumerate(widths):
        cumulative[index + 1] = cumulative[index] + width
    safe = _safe_break_positions(title)
    total = cumulative[len(title)]
    parts: List[str] = []
    start = 0
    for index in range(1, count):
        target = total * index / count
        candidates = [
            position
            for position in range(start + 1, len(title))
            if safe[position]
            and title[position] not in LINE_START_FORBIDDEN
            and cumulative[position] - cumulative[start] <= capacity
        ]
        if not candidates:
            return None
        preferred = [position for position in candidates if title[position - 1] in PREFERRED_BREAK_AFTER]
        pool = preferred or candidates
        position = min(pool, key=lambda item: abs(cumulative[item] - target))
        parts.append(title[start:position])
        start = position
    parts.append(title[start:])
    if any(tables.display_width(part) > capacity for part in parts):
        return None
    if tables.display_width(parts[-1]) <= HEADING_ORPHAN_UNITS:
        return None
    return parts


def chapter_title_lines(title: str, capacity: int) -> Optional[List[str]]:
    r"""标题自然换行会把最后一行留成孤字时，返回显式断行后的各行；否则 ``None``。

    判据是**预演**：按 :func:`_wrap_by_units` 贪心折行，若折出的最后一行不超过
    :data:`HEADING_ORPHAN_UNITS`（一个全角字）且确实折成了多行，就按行数把标题重新
    均分（:func:`_balanced_lines`）。只在真需要时返回 ``None`` 以外的东西，所以绝大
    多数标题的 ``\chapter{...}`` 一个字节都不变。

    这是**预判**，不是测量：编号宽度按 ctex 的规则算（见
    :func:`chapter_title_capacity`），而实际排版由 LaTeX 决定。真正的裁决权在
    构建后跑 :func:`primer.book.checks.heading_orphan_line`——那条检查量的是印出来
    的 PDF。
    """
    natural = _wrap_by_units(title, capacity)
    if len(natural) < 2 or tables.display_width(natural[-1]) > HEADING_ORPHAN_UNITS:
        return None
    return _balanced_lines(title, capacity, len(natural))


def _heading_command_line(command: str, raw_title: str, number_text: Optional[str], typography: Typography) -> str:
    r"""一条结构命令及其花括号标题，必要时改发带可选参数的显式断行版本。

    ``\chapter[<完整题名>]{<断了行的题名>}``：可选参数供目录与页眉使用（保持完整、
    不折行），强制参数只控制版面上的显示。只对**确定要断**的章标题这么做。
    """
    if number_text is None:
        return rf"\{command}{{{render_inline(raw_title)}}}"
    parts = chapter_title_lines(raw_title, chapter_title_capacity(typography, number_text))
    if parts is None:
        return rf"\{command}{{{render_inline(raw_title)}}}"
    display = r"\\".join(render_inline(part) for part in parts)
    return rf"\{command}[{render_inline(raw_title)}]{{{display}}}"


# ---------------------------------------------------------------- 块


def render_blocks(
    blocks: Sequence[object],
    typography: Typography,
    findings: List[Finding],
    part_label: Optional[str] = None,
    appendix: bool = False,
    location: str = "",
    marks: Optional[List[str]] = None,
    chapter_start: int = 1,
    appendix_number_start: int = 0,
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

    ``chapter_start``／``appendix_number_start`` 是本篇第一个章号的起点（连续
    编号由 LaTeX 给，这里只为**量**自动编号占多宽：见
    :func:`chapter_title_capacity`）。缺省值等于"全书第一篇"，与只装配单篇的
    调用方一致。
    """
    out: List[str] = []
    labelled = False
    budget = head_mark_budget(typography)
    chapter_number = chapter_start
    appendix_number = appendix_number_start
    for index, block in enumerate(blocks):
        if isinstance(block, PartBanner):
            raw_title = part_title(block.title, appendix)
            command = "chapter" if appendix else "part"
            number_text = None
            if command == "chapter":
                number_text = appendix_number_text(appendix_number)
                appendix_number += 1
            before, after = _head_mark_lines(command, raw_title, budget, marks)
            out.extend(before)
            out.append(_heading_command_line(command, raw_title, number_text, typography))
            out.extend(after)
            if part_label and not labelled:
                out.append(rf"\label{{{part_label}}}")
                labelled = True
        elif isinstance(block, Heading):
            level = block.level + 1 if appendix else block.level
            command = HEADING_COMMANDS.get(level)
            if command:
                raw_title = strip_heading_number(block.text)
                number_text = None
                if command == "chapter":
                    number_text = chapter_number_text(chapter_number)
                    chapter_number += 1
                before, after = _head_mark_lines(command, raw_title, budget, marks)
                out.extend(before)
                out.append(_heading_command_line(command, raw_title, number_text, typography))
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
