# -*- coding: utf-8 -*-
"""把 deck 的中间表示发射成 ctexbeamer 的 ``slides.tex``。

这一层只做**发射**：帧怎么排、表格缩不缩、图的路径写什么。句子与标题早就定好了——
它们要么来自成书的句子（经 :func:`primer.book.tex.render_inline` 走成书同一套行内渲染），
要么是工具自己的算术（页号、指示、边栏）。**这里不写正文**。

**版式取自主题（数据），不取默认值。** 画布、页边、字号梯级、配色 token 全部来自
``outline.yaml`` 的 ``theme:`` 块（见 :mod:`primer.slides.theme`），导言区里只写"怎么排"：
帧标题带、标题下短横、页脚与进度条、表格样式。这几段的写法与实测常量照抄
``_primer/slides/theme-samples/samples.tex``（Palette A · 学术极简那一套）。

三处必须"翻译"而不是照搬成书的做法：

* **横向页**：成书放不下宽表就转一页横向；beamer 的页面是固定的 16:9，没有可转的页。
  所以书里的 landscape 档在这里翻成"缩到版心宽"，并且如实记一条发现——被折过的帧
  不该让人去猜。
* **表格缩放**：用 ``adjustbox`` 的 ``max width``／``max height``，一张表放不进一帧时
  先等比缩；缩到 :data:`MIN_LEGIBLE_SCALE` 还放不下，才让这一帧 ``allowframebreaks``
  跨页（那会让 PDF 多出页来，页数闸门会发现）。
* **备注**：``\\note{}`` 只在开了 ``show notes`` 的编译里出现，默认什么都不印——所以
  它既进得了讲义，又不会把正片撑长。

排版度量复用 :mod:`primer.book.tables`：把这一帧的版心宽塞进它"纸面 − 2×边距"的模型里，
表格的列宽与字号阶梯就是它算出来的那一套（见 :func:`frame_typography`）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import List, Optional, Sequence, Tuple

from ..book import tables, tex
from .plan import PLACEHOLDER, SlidesError
from .structure import FigureRef
from .theme import (
    BODY_FOOTER_CLEARANCE_MM,
    COVER_SUBLINE_GAP_MM,
    DEFAULT_CJK_FAKE_BOLD,
    DEFAULT_CJK_FAKE_SLANT,
    DEFAULT_CJK_SANS_FAKE_BOLD,
    FOOTER_INK_FROM_BOTTOM_MM,
    MM_PER_PT,
    Theme,
    default_theme,
    floor_level_name,
    frame_metrics,
)
from .validate import Finding

# 一帧正文的可用高度与版心宽：都由主题算出来（见 theme.frame_metrics）。这里只留
# 「表格缩到多小就不如跨页」与「缩表的高度上限」两个与主题无关的经验值。
ADJUST_MAX_HEIGHT = 0.82
# 缩到多小就不如让这一帧跨页。0.62 是"投影上还认得出"的经验线，不是换算率。
MIN_LEGIBLE_SCALE = 0.62
# book 的度量表里最接近"一页"的那张纸（版心宽超过它时模型取它，见 frame_typography）。
A4_SHORT_MM = 210.0

DECK_JOBNAME = "slides"

# 表格列间距（pt）：列宽要预扣它的份额，所以定死而不取默认。A 套不填斑马纹，线是发丝线。
TABLE_GAP_PT = 5.0
# 表格正文与表头的 ``\zihao`` 档位由主题的磅值换算出最接近的一档（见 :func:`zihao_for`）。

# 备份页的出处查找表：三列——篇色块 + 章号｜出处｜要点摘要。前两列按内容收紧，摘要列
# 吃掉余下的版心宽；字号取主题梯级里最小的一档（页脚那一级），不在它之下再缩。
LOOKUP_PAD_MM = 2.0
# 行尾留的余量：三列宽取整后加起来不至于挤出版心（一挤就是 overfull hbox）。
LOOKUP_SLACK_MM = 1.0
# ``\RowCap`` 画的篇色小块宽（``\tmTabCapW`` = 1.3mm）加上它与章号之间那点空（1.4mm）。
LOOKUP_CHIP_MM = 2.7
# 字号档位 → 导言区里的字号宏。备份表用哪一档就调哪一个宏，点数只写在那六处。
LEVEL_COMMANDS = {
    "title": "FzTitle",
    "body": "FzBody",
    "table_head": "FzTabHead",
    "table_body": "FzTab",
    "caption": "FzCap",
    "footer": "FzFoot",
}
UNICODE_LINES = r"""\newunicodechar{⁻}{\textsuperscript{-}}
\newunicodechar{⁰}{\textsuperscript{0}}\newunicodechar{¹}{\textsuperscript{1}}
\newunicodechar{²}{\textsuperscript{2}}\newunicodechar{³}{\textsuperscript{3}}
\newunicodechar{⁴}{\textsuperscript{4}}\newunicodechar{⁵}{\textsuperscript{5}}
\newunicodechar{⁶}{\textsuperscript{6}}\newunicodechar{⁷}{\textsuperscript{7}}
\newunicodechar{⁸}{\textsuperscript{8}}\newunicodechar{⁹}{\textsuperscript{9}}
\newunicodechar{₀}{\textsubscript{0}}\newunicodechar{₁}{\textsubscript{1}}
\newunicodechar{₂}{\textsubscript{2}}\newunicodechar{₃}{\textsubscript{3}}
\newunicodechar{₄}{\textsubscript{4}}\newunicodechar{₅}{\textsubscript{5}}
\newunicodechar{₆}{\textsubscript{6}}\newunicodechar{₇}{\textsubscript{7}}
\newunicodechar{₈}{\textsubscript{8}}\newunicodechar{₉}{\textsubscript{9}}
\newunicodechar{⊕}{$\oplus$}
\newunicodechar{℃}{$^{\circ}$C}
\newunicodechar{→}{$\rightarrow$}   % 无衬线拉丁字里没有箭头，落到 CJK 面又不合算：走数学箭头
\newunicodechar{①}{\textcircled{\scriptsize 1}}\newunicodechar{②}{\textcircled{\scriptsize 2}}
\newunicodechar{③}{\textcircled{\scriptsize 3}}\newunicodechar{④}{\textcircled{\scriptsize 4}}
\newunicodechar{⑤}{\textcircled{\scriptsize 5}}\newunicodechar{⑥}{\textcircled{\scriptsize 6}}
\newunicodechar{⑦}{\textcircled{\scriptsize 7}}\newunicodechar{⑧}{\textcircled{\scriptsize 8}}
\newunicodechar{⑨}{\textcircled{\scriptsize 9}}\newunicodechar{⑩}{\textcircled{\scriptsize 10}}"""

# 导言区。占位符用 string.Template 的机制、分隔符换成 @（LaTeX 里 $ 与 \ 遍地都是）。
# 「配色 token 之外的样式」——表格线粗细、篇色小块尺寸、封面标题块的位置与净距——照抄样张
# theme/A 块，它们是 A 的样式层；颜色本身一律取上面几个 token 的派生值，所以改 token 就跟着变。
# 封面色带高不写死：帧里按标题块实测（见 _cover_frame），这里只给最小高与净距。
PREAMBLE = r"""% !TeX program = xelatex
% 本文件由 primer-slides 生成，请勿手工维护；改完 outline.yaml 后重新运行 build。
% 画布、字号、字体栈与配色取自 outline.yaml 的 theme: 块——这里只写「怎么排」。
\PassOptionsToPackage{table}{xcolor}   % \rowcolors / \rowcolor（表格的行底）
\documentclass[UTF8,fontset=none]{ctexbeamer}

\usepackage{graphicx}
\usepackage{array}
\usepackage{adjustbox}
\usepackage{newunicodechar}
\usepackage{ragged2e}   % \justifying（正文两端对齐）
\usepackage{tikz}

% ---------------------------------------------------------------- 字体
% 幻灯片**自己的**字体栈（theme.fonts，可在 outline.yaml 的 theme.fonts 块里覆盖）：
% 全无衬线——正文与结构都走拉丁 Helvetica Neue、中文冬青黑体，等宽 Menlo。
% 它**不**跟成书清单的 fonts 块走：那套是正文 Times + 宋体的成书面孔，投影上既不是
% 用户要的无衬线，也与这里的字号梯级不搭。
% 默认家族取罗马（\rmdefault）——它在这里**也是无衬线**：拉丁走 \setmainfont（Helvetica Neue），
% 中文走 \setCJKmainfont（Hiragino Sans GB **W3**，常规字重）；结构与标题显式 \sffamily，
% 落到 W6（半粗）。用户定的层次：标题 W6、正文 W3。
% W6 是真实半粗字面，再叠合成粗体会发糊，所以无衬线族 AutoFakeBold=0；
% W3 没有粗体字面，主、等宽族的 \textbf 靠 2.5 合成粗体。
\setmainfont{@main_font}
\setsansfont{@sans_font}
\setmonofont{@mono_font}
\setCJKmainfont[AutoFakeBold=@cjk_fake_bold,AutoFakeSlant=@cjk_fake_slant]{@cjk_main_font}
\setCJKsansfont[AutoFakeBold=@cjk_sans_fake_bold]{@cjk_sans_font}
\setCJKmonofont[AutoFakeBold=@cjk_fake_bold]{@cjk_mono_font}
\renewcommand{\familydefault}{\rmdefault}

@unicode_lines

% ---------------------------------------------------------------- 画布（theme.canvas）
% PowerPoint 标准 16:9：@canvas_width × @canvas_height bp = 338.67 × 190.5 mm。
% 单位必须写 bp（1 bp = 1/72 in = 一个 PDF 点）：geometry 的 pt 是 TeX 点（1/72.27 in），
% paperwidth=960pt 出来的是 956.41 × 537.98 bp 的纸。类选项里也不写 aspectratio=169：
% 它会把纸面拉回 beamer 自己的 160 × 90 mm。页边 = 纸宽的 @margin_percent，于是版心 = 纸宽的 90%。
\geometry{paperwidth=@canvas_width bp,paperheight=@canvas_height bp}
\newlength{\tmMargin}\setlength{\tmMargin}{@margin_ratio\paperwidth}
\setbeamersize{text margin left=\tmMargin, text margin right=\tmMargin}
% ---------------------------------------------------------------- 版面
\graphicspath{{@graphics_path}}
\setbeamertemplate{navigation symbols}{}
\setbeamertemplate{footline}{}   % 页脚一律由 \PageTop 画在绝对位置

% ---------------------------------------------------------------- 字号（theme.type）
% 全份文件只在这六处写点数：@type_scale_line
\newcommand{\FzTitle}{\fontsize{@fz_title_size}{@fz_title_leading}\selectfont}
\newcommand{\FzBody}{\fontsize{@fz_body_size}{@fz_body_leading}\selectfont}
\newcommand{\FzTabHead}{\fontsize{@fz_table_head_size}{@fz_table_head_leading}\selectfont}
\newcommand{\FzTab}{\fontsize{@fz_table_body_size}{@fz_table_body_leading}\selectfont}
\newcommand{\FzCap}{\fontsize{@fz_caption_size}{@fz_caption_leading}\selectfont}
\newcommand{\FzFoot}{\fontsize{@fz_footer_size}{@fz_footer_leading}\selectfont}
\newcommand{\FzSecond}{\FzCap}   % 题注层的别名，落在上面某一级上

% ---------------------------------------------------------------- 配色（theme.palette）
\definecolor{thmink}{HTML}{@ink}
\definecolor{thmmuted}{HTML}{@muted}
\definecolor{thmhair}{HTML}{@hairline}
\definecolor{thmaccent}{HTML}{@accent}
\definecolor{thmV1}{HTML}{@vol1}
\definecolor{thmV2}{HTML}{@vol2}
\definecolor{thmV3}{HTML}{@vol3}
\definecolor{thmVA}{HTML}{@vol4}
% 「A · 学术极简」的样式层（照抄 theme/A 块）：层次交给字重与一道发丝线。
% 标题带底、表头底、斑马纹都是白色——A 不填色，那几行留着是为了换配色时只改这里一行。
\colorlet{thmtitlebg}{white}
\colorlet{thmtitlefg}{thmink}
\colorlet{thmtabheadfg}{thmink}
\colorlet{thmtabrule}{thmhair}
\colorlet{thmfootfg}{thmmuted}
\colorlet{thmtrack}{thmhair}
\colorlet{thmcoverbg}{white}
\colorlet{thmcovertitlefg}{thmink}
\colorlet{thmcoversubfg}{thmmuted}
\colorlet{thmcoveredge}{thmhair}
\def\tmTabRuleW{0.6pt}
\def\tmTabCapW{1.3mm}
\def\tmTitleRuleW{0.5pt}
\def\tmTitleRuleLen{\textwidth}
\newlength{\tmCoverH}       % 封面色带高：帧里按标题块实测再定（\sbox 量 \ht+\dp）
\newlength{\tmCoverTop}\setlength{\tmCoverTop}{13.5mm}   % 标题块顶到纸面顶边
\newlength{\tmCoverClear}\setlength{\tmCoverClear}{6mm}  % 标题块底到色带下沿的净距
\newlength{\tmCoverMinH}\setlength{\tmCoverMinH}{46mm}   % 色带最小高（没有副题时的原高）
\newlength{\tmCoverFootY}\setlength{\tmCoverFootY}{16mm} % 署名块底到纸面下沿（下部居中）
\def\tmCoverRuleW{3.2mm}
\def\tmCoverRuleLen{26mm}
\def\tmCoverEdgeW{0.4pt}

% 篇色表：卷键 → 颜色 token + 篇的短名（页面只说 \SetVolume{V1}，不挑颜色）。
% 表中没有的卷退回第一篇：框架里没有的颜色宏画不出来，宁可退一步也不让编译失败。
\def\defvolcolor#1#2{\expandafter\gdef\csname volcol#1\endcsname{#2}}
\defvolcolor{V1}{thmV1}\defvolcolor{V2}{thmV2}\defvolcolor{V3}{thmV3}\defvolcolor{VA}{thmVA}
\def\VolColor#1{\csname volcol#1\endcsname}
% 切篇：页面只调用这一个宏，派生色在这里一次算好。
%   volmain   = 当前篇色（正文条目短横、进度条填色、篇名）
%   volhead   = 表头底（A 是白色：不填色）
%   volzebra  = 斑马纹（A 是白色：不填色）
\newcommand{\SetVolume}[1]{%
  \colorlet{volmain}{\VolColor{#1}}%
  \colorlet{volhead}{white}%
  \colorlet{volzebra}{white}%
}

% ---------------------------------------------------------------- 版心常量（与样张同一套实测值）
\newlength{\tmTopPad}\setlength{\tmTopPad}{2.03mm}      % 帧正文起点到纸面顶边（实测）
\newlength{\tmTitlePre}\setlength{\tmTitlePre}{1.6mm}
\newlength{\tmTitlePost}\setlength{\tmTitlePost}{3.6mm}
\newlength{\tmFootBarY}\setlength{\tmFootBarY}{1.0mm}   % 进度条底边距纸面底边
\newlength{\tmFootTextY}\setlength{\tmFootTextY}{2.6mm} % 页脚字盒底边距纸面底边
% 正文区底部护栏：页脚墨迹顶（纸面下沿上 7.8mm）再让 2mm 净距——正文不许压到页脚与进度条。
% 正文页、主旨页、表格页都在收尾加 \vspace*{\tmBodyGuard}。
\newlength{\tmBodyGuard}\setlength{\tmBodyGuard}{@body_guard mm}
% 正文区：帧盒是整页（plain 帧按 \paperheight 排版），所以正文要在 \tmTmpA（标题带下沿，
% \PageTop 量出）与页脚护栏之间居中。居中的容器是**固定高度**的 \vbox，里面两个 \vfil
% 平分余量；不能只用 \vfill——它与 beamer 帧尾的 1fill 同阶，空白会三分，正文落到整页的
% 三分之一处，页面下段留一条约 114pt 的死区（实测：140pt 的上净空配 250pt 的下净空）。
% 盒高再让 1pt（\tmBodySlack）：标题盒在竖直列表里还带一点行间胶，实测每帧比 \tmTmpA 多
% 0.92pt，不留这点余量，每一帧都会报一条 0.91pt 的 Overfull \vbox。护栏有 9.8mm，让掉
% 一点不影响观感，也不遮蔽真正的溢出（那是要报出来的）。
\newlength{\tmBodySlack}\setlength{\tmBodySlack}{1pt}
\def\tmBarWmm{46}                                       % 进度条总宽（mm）
\def\tmBarTotal{@page_total}                            % 全片页数
\newsavebox{\tmTitleBoxS}
\newsavebox{\tmCoverBoxS}
\newsavebox{\tmFootLBoxS}\newsavebox{\tmFootRBoxS}
\newlength{\tmTmpA}\newlength{\tmTmpB}

% ---------------------------------------------------------------- 版式模型（页面代码，配色通行）

% 正文条目：行首一个短横做标记，正文挂在它右边、两端对齐（用户要的版式；\justifying 出自
% ragged2e）。颜色默认取当前篇色。
\newcommand{\BodyItem}[2][volmain]{%
  \noindent{\color{#1}\rule[5pt]{3.0mm}{1.2pt}}\hspace{1.7mm}%
  \parbox[t]{\dimexpr\linewidth-4.7mm\relax}{\justifying\FzBody\strut#2}\par
  \vspace{2.4mm}%
}

% 表头与篇色小块。p 列单元格里必须用 \textcolor 而不是 \color：在单元格开头写 \color，
% 那一行会白顶出 0.7 倍行距；\rule 裹在 \mbox 里同理（p 列以竖排开始，裸 \rule 会另起一段）。
\newcommand{\Hd}[1]{{\FzTabHead\sffamily\textcolor{thmtabheadfg}{#1}}}
\newcommand{\RowCap}[1]{\mbox{\textcolor{\VolColor{#1}}{\rule[-0.1mm]{\tmTabCapW}{3.8mm}}}\hspace{1.4mm}}

% 进度条数据：页面先说清这一帧落在全片第几页到第几页，再由 \PageTop 画。
\gdef\tmBarOn{0}
\newcommand{\SetPageBar}[2]{\gdef\tmBarOn{1}\gdef\tmBarF{#1}\gdef\tmBarT{#2}}

% 帧的上下装饰与页脚：#1 帧标题　#2 页脚左（出处指针）　#3 页脚右（页序）
%   · 标题带：永远画，高度按标题实测（两行标题也盖得住）；
%   · 标题下短横：永远画，发丝线，横贯版心；
%   · 页脚行：左＝指针、右＝页序，都从纸面下沿往上排；
%   · 进度条：贴纸面下沿画出这一帧覆盖的页区间，填色取当前篇色（翻到哪一篇一眼能认）。
\newcommand{\PageTop}[3]{%
  \sbox{\tmTitleBoxS}{\parbox[t]{\textwidth}{\raggedright\FzTitle\sffamily\color{thmtitlefg}#1}}%
  \sbox{\tmFootLBoxS}{\FzFoot\color{thmfootfg}#2}%
  \sbox{\tmFootRBoxS}{\FzFoot\color{thmfootfg}#3}%
  \setlength{\tmTmpA}{\dimexpr\tmTopPad+\tmTitlePre+\ht\tmTitleBoxS+\dp\tmTitleBoxS+\tmTitlePost\relax}%
  \setlength{\tmTmpB}{\dimexpr\tmTmpA-1.4mm\relax}%
  \begin{tikzpicture}[remember picture,overlay]
    \fill[thmtitlebg] (current page.north west) rectangle ([yshift=-\tmTmpA]current page.north east);
    \node[anchor=west,inner sep=0pt] at ([xshift=\tmMargin,yshift=-\tmTmpB]current page.north west)
      {\color{thmhair}\rule{\tmTitleRuleLen}{\tmTitleRuleW}};
    \node[anchor=south west,inner sep=0pt] at ([xshift=\tmMargin,yshift=\tmFootTextY]current page.south west)
      {\usebox{\tmFootLBoxS}};
    \node[anchor=south east,inner sep=0pt] at ([xshift=-\tmMargin,yshift=\tmFootTextY]current page.south east)
      {\usebox{\tmFootRBoxS}};
    \ifnum\tmBarOn=1
      \pgfmathsetmacro{\bxa}{(\tmBarF-1)/\tmBarTotal*\tmBarWmm}%
      \pgfmathsetmacro{\bxb}{\tmBarT/\tmBarTotal*\tmBarWmm}%
      \node[anchor=south east,inner sep=0pt] at ([xshift=-\tmMargin,yshift=\tmFootBarY]current page.south east) {%
        \begin{tikzpicture}[x=1mm,y=1mm]
          \fill[thmtrack] (0,0) rectangle (\tmBarWmm,1.4);
          \fill[volmain] (\bxa,0) rectangle (\bxb,1.4);
        \end{tikzpicture}};
    \fi
  \end{tikzpicture}%
  \gdef\tmBarOn{0}%   用过就复位，下一页要画得重新说
  \vspace*{\tmTitlePre}%
  \noindent\usebox{\tmTitleBoxS}\par
  \vspace*{\tmTitlePost}%
}

\title{@title}
\subtitle{@subtitle}
\author{@author}
\institute{@institute}
\date{}
"""


@dataclass(frozen=True)
class FrameTypography:
    """表格度量要的那三项版面参数（book 的度量模型只认 ``paper``／``margin``／字号档位）。

    ``primer.book.tables.plan_table`` 按鸭子类型读这三个字段，所以幻灯片可以自己带一份，
    不必去读成书清单里那份 ``Typography``。
    """

    paper: str
    margin: str
    table_font_size: str


@dataclass(frozen=True)
class DeckContext:
    """发射一帧所需的全部版面信息。

    字体取自 ``theme.fonts``（幻灯片自己的无衬线栈），不在这里单列；
    ``typography`` 只留给 book 的度量模型算表格列宽（见 :func:`frame_typography`）。
    """

    title: str
    subtitle: str
    presenter: str
    occasion: str
    institute: str
    graphics_path: str
    typography: FrameTypography = field(default_factory=lambda: FRAME_TYPOGRAPHY)
    theme: Theme = field(default_factory=default_theme)
    # 全片的帧数：排完帧才知道，由 compose_frames 填（见 with_layout_geometry）。
    page_total: int = 0

    @property
    def frame_typography(self) -> FrameTypography:
        return frame_typography(self.typography, self.theme)

    @property
    def figure_max_height_mm(self) -> float:
        """图与图注一栏的高度上限：一帧正文的可用高度再扣掉图注那两行。"""
        return max(frame_text_height_mm(self.theme) - 18.0, 20.0)

    def volume_key(self, key: str) -> str:
        """骨架里的卷键 → 主题认得的一个卷键（不认得时退回第一篇）。"""
        if key in self.theme.volume_keys():
            return key
        return self.theme.volume_keys()[0]


def zihao_for(points: float) -> str:
    """最接近给定磅值的 ``\\zihao`` 档位。

    book 的度量表只认档位，而主题给的是磅值（表格正文 16 pt）；取最近的一档用于高度估算，
    真正排出来的字号仍由主题的 ``\\FzTab`` 定，所以这里差一档只影响"估高准不准"。
    """
    return min(
        tables.ZHIHAO_POINTS,
        key=lambda code: (abs(tables.ZHIHAO_POINTS[code] - points), code),
    )


def frame_text_height_mm(theme: Theme) -> float:
    """一帧正文的可用高度（mm）：标题带下沿到页脚净距——与容量算术同一个口径。"""
    return frame_metrics(theme).body_area_one_line_mm


# 度量模型的底稿：纸面与边距都会被 frame_typography 改掉，这里给的是"最窄的纸"。
FRAME_TYPOGRAPHY = FrameTypography(paper="a4paper", margin="0mm", table_font_size="-4")


def frame_typography(typography: FrameTypography, theme: Theme) -> FrameTypography:
    """给 :func:`primer.book.tables.plan_table` 用的版面。

    book 的度量只认 ``paper`` 与 ``margin``，纸面尺寸写在它自己的表里。而这一帧的版心宽
    （960 bp 画布下 304.8 mm）比它那张表里任何一张纸的短边都宽（a4 210 mm），边距又只能
    往窄里压（读不出负边距），所以模型取 a4、边距 0：模型眼里的版心比真实版心**窄**一档，
    判"太宽"时先按最窄的纸算——偏保守，宽表只会被多折一次，不会被放到溢出。
    表格字号取主题的表格正文磅值（换算成最接近的 ``\\zihao`` 档位），高度估算因此与
    实际排出的字号一致。
    """
    usable_mm = theme.text_width_mm
    margin_mm = max(0.0, (A4_SHORT_MM - usable_mm) / 2.0)
    return replace(
        typography,
        paper="a4paper",
        margin=f"{margin_mm:.4f}mm",
        table_font_size=zihao_for(theme.level("table_body").size),
    )


def wrapped_height_mm(
    rows: Sequence[Sequence[str]], fractions: Sequence[float], font_pt: float, capacity_mm: float
) -> float:
    """按列宽权重换行排之后的高度估算（mm）。

    与 book 的 ``_fits_height`` 同一个算法，只是把"版心高"换成"一帧的版心高"：按权重定
    列宽，逐行数某格里最多要几行，再把行数折成高度。长表、窄列因此会在生成之前就被算出来，
    不必等 LaTeX 用 overfull vbox 告诉人。
    """
    capacity = capacity_mm / MM_PER_PT / (0.5 * font_pt)
    lines = 0
    for row in rows:
        rows_needed = 1
        for cell, fraction in zip(row, fractions):
            width = max(fraction * capacity, 1.0)
            text = str(cell).replace("*", "")
            rows_needed = max(rows_needed, math.ceil(tables.natural_width(text) / width))
        lines += rows_needed
    return (lines * font_pt * 1.25 + len(rows)) * MM_PER_PT


@dataclass(frozen=True)
class LookupCell:
    """备份查找表的一行：篇色块 + 章号｜出处｜要点摘要。

    三个文本字段**已经是行内 LaTeX**（由 :mod:`primer.slides.build` 经成书的
    ``render_inline`` 渲染），摘要也已截到选定长度——这一层只负责把它排成一行。
    """

    volume: str
    label: str
    pointer: str
    gist: str


@dataclass(frozen=True)
class LookupColumns:
    """备份查找表的三列宽度（mm）、每帧可用行数与摘要列的一行容量（em）。

    ``lines_per_frame`` 是按**字号下限的行距**量出来的：正文区高 ÷ 下限行距，标题按
    一行算。条目多到装不下时只收摘要长度（由 build 侧的阶梯决定），这里的三样量不动。
    """

    label_mm: float
    pointer_mm: float
    gist_mm: float
    lines_per_frame: int
    gist_units: int


def lookup_level_command(theme: Theme) -> str:
    """主题梯级里最小的一档对应的字号宏名——备份查找表用的就是这一档。"""
    return LEVEL_COMMANDS[floor_level_name(theme)]


def lookup_units(text: str) -> float:
    """一段查找表文本的宽度（em）：汉字 1 em、其余按 0.6 em。

    比成书度量表那套"其余按 0.5 em"宽一点——量出来的是**上界**。列宽与行数都用它：
    装不下会在版面上露出来，算宽了只是少放几个字，两个方向都不该反过来。
    """
    return sum(1.0 if ord(char) > tables.CJK_THRESHOLD else 0.6 for char in text)


def lookup_lines_per_frame(theme: Theme) -> int:
    """一帧能排几行查找表：正文区高 ÷ 字号下限的行距（标题按一行算）。"""
    leading_mm = theme.level(floor_level_name(theme)).leading * MM_PER_PT
    if leading_mm <= 0:
        return 0
    return max(int(frame_text_height_mm(theme) // leading_mm), 0)


def lookup_columns(
    labels: Sequence[str], pointers: Sequence[str], theme: Theme
) -> LookupColumns:
    """三列宽度：章号列与出处列按内容收紧，摘要列吃掉余下的版心宽。

    章号列的宽 = 篇色小块 + 最长的章号 + 一点净空，出处列同理。**不**按成书表格那套
    自然宽度比例分：那样每一列至少拿半页，数字与短栏会白占大片版面。摘要列因此拿到余量，
    十六个字的摘要一行放得下；行尾再留一点余量，宽度取整后不至于挤出版心。
    """
    unit_mm = theme.level(floor_level_name(theme)).size * MM_PER_PT
    label_mm = (
        LOOKUP_CHIP_MM
        + max((lookup_units(text) for text in labels), default=0.0) * unit_mm
        + LOOKUP_PAD_MM
    )
    pointer_mm = (
        max((lookup_units(text) for text in pointers), default=0.0) * unit_mm + LOOKUP_PAD_MM
    )
    gist_mm = max(theme.text_width_mm - label_mm - pointer_mm - LOOKUP_SLACK_MM, 1.0)
    return LookupColumns(
        label_mm=label_mm,
        pointer_mm=pointer_mm,
        gist_mm=gist_mm,
        lines_per_frame=lookup_lines_per_frame(theme),
        gist_units=max(int(gist_mm / unit_mm), 1) if unit_mm > 0 else 1,
    )


@dataclass(frozen=True)
class Frame:
    """一帧幻灯片。

    五种版式，各自的字段互不干扰：``title`` 是封面帧；``quote`` 是主旨帧（整宽一句话）；
    ``points`` 是要点帧（正文、横向、讨论、主旨）；``rows`` 是表格帧（目录页的篇／章表）；
    ``lookup`` 是备份帧（逐条的出处查找表）。

    ``points``／``rows`` 里的字符串**已经是行内 LaTeX**——由调用方经成书的
    ``render_inline`` 渲染，本模块不再碰它们的内容。``volume``／``page``／``span`` 是
    主题的接线：当前篇（决定篇色与进度条颜色）、这一帧在正片里的页序、它覆盖的页区间。
    """

    kind: str
    title: str = ""
    points: Tuple[str, ...] = ()
    figure: Optional[FigureRef] = None
    figure_label: str = ""
    footer: str = ""
    notes: str = ""
    # 封面帧（kind == "title"）的副题行：书目副题（书名下的年份）之下再写的一行，
    # 句子来自 pages[0].picks[:1]（人写的自由文本或候选句，已渲染成行内 LaTeX）。空串不发。
    cover_subline: str = ""
    rows: Tuple[Tuple[str, ...], ...] = ()
    # 主题与版式的接线（由 compose_frames 填）
    volume: str = ""
    page: int = 0
    span: Tuple[int, int] = ()
    table_align: Tuple[str, ...] = ()
    row_volumes: Tuple[str, ...] = ()
    # 表格帧的显式列宽（占版心宽的比例）。空表示按成书的自然宽度分配（见 plan_beamer_table）；
    # 目录页自己算一套——数字列按内容收紧、余量给章名，见 build._toc_fractions。
    table_fractions: Tuple[float, ...] = ()
    # 备份页的出处查找表（kind == "backup"）：一行一条，组内条目由 compose_frames 摊好。
    lookup: Tuple[LookupCell, ...] = ()
    lookup_columns: Optional[LookupColumns] = None
    # 逐帧的篇色短横：讨论页走强调色，其余走篇色（None 表示用篇色）
    marker: str = ""


@dataclass
class _Sink:
    """发射过程中的发现与表格折法的记账。"""

    findings: List[Finding] = field(default_factory=list)
    table_notes: List[str] = field(default_factory=list)


# ---------------------------------------------------------------- 表格


def plan_beamer_table(
    rows: Sequence[Sequence[str]],
    location: str,
    context: DeckContext,
    sink: _Sink,
    *,
    align: Sequence[str] = (),
    row_volumes: Sequence[str] = (),
    fractions: Sequence[float] = (),
) -> Tuple[List[str], bool]:
    """一张表在 beamer 里的落版：返回（行序列，是否 allowframebreaks）。

    :func:`primer.book.tables.plan_table` 是为**一页纸**写的：它在竖排与横向页之间挑，
    还在字号阶梯上往下退。一帧 16:9 没有可转的页，缩到 7.5pt 在投影上也没法读。所以这里
    只取它两样东西——**方向判定**（落到这里就是"自然宽度放不放得下"）与**列宽权重**——
    字号一律用主题给的表格正文档，放不下就按版心宽换行排；换行还嫌高才套 adjustbox 等比缩；
    缩到 :data:`MIN_LEGIBLE_SCALE` 以下才让这一帧 ``allowframebreaks`` 跨页。

    ``fractions`` 非空时不再问 :func:`plan_table`：列宽就是给的那一份（目录页自己按内容
    算好的一套，见 :func:`primer.slides.build._toc_fractions`），高度仍在这里量。

    表的样子（不画三线、表头 18 pt 半粗、数据 16 pt、数字列右对齐、行首一枚篇色小块）取自
    样张的表格页；``align`` 是各列的对齐（``l`` / ``r``，缺省全左）。
    """
    rows = [list(row) for row in rows]
    reasons: List[str] = []
    if fractions:
        columns: Tuple[float, ...] = tuple(float(value) for value in fractions)
    else:
        layout, _ = tables.plan_table(rows, context.frame_typography, location)
        columns = layout.columns
        if layout.orientation == "landscape":
            reasons.append("成书里的横向档在 beamer 无页可转，改按版心宽换行排")
        elif layout.mode == "scale":
            reasons.append("按自然宽度排不下（成书那里会缩字），改按版心宽换行排")

    font_code = str(context.frame_typography.table_font_size)
    font_pt = tables.font_points(font_code)
    theme = context.theme
    scale = 1.0
    height_mm = wrapped_height_mm(rows, columns, font_pt, theme.text_width_mm)
    frame_height_mm = frame_text_height_mm(theme)
    if height_mm > frame_height_mm:
        fit = frame_height_mm / height_mm
        scale = min(scale, fit)
        reasons.append(
            f"换行后估高 {height_mm:.0f}mm 超过一帧的 {frame_height_mm:.0f}mm，"
            f"按高度缩到 {fit * 100:.0f}%"
        )

    if reasons:
        sink.findings.append(
            Finding("table-layout", "info", f"表被折过：{'；'.join(reasons)}", location)
        )

    shrink = scale < 0.995
    if shrink and scale < MIN_LEGIBLE_SCALE:
        sink.table_notes.append(
            f"{location}：缩到 {scale * 100:.0f}% 已低于可读下限 "
            f"{MIN_LEGIBLE_SCALE * 100:.0f}%，改用 allowframebreaks（这一帧会跨页）"
        )
        return _scaled_table(rows, row_volumes), True
    if shrink:
        sink.table_notes.append(f"{location}：缩放到 {scale * 100:.0f}%")
        return _scaled_table(rows, row_volumes), False

    sink.table_notes.append(
        f"{location}：{'；'.join(reasons)}" if reasons else f"{location}：按版心宽排得下，未缩"
    )
    return _styled_table(rows, columns, align, row_volumes), False


def _table_preamble() -> List[str]:
    """表前后两段共用的一组设定：发丝线、无列间距、表头与行底由 token 定。"""
    return [
        r"\arrayrulecolor{thmhair}",
        r"\setlength{\arrayrulewidth}{\tmTabRuleW}",
        r"\setlength{\tabcolsep}{0pt}",
        r"\rowcolors{2}{volzebra}{white}",
    ]


def _styled_table(
    rows: Sequence[Sequence[str]],
    fractions: Sequence[float],
    align: Sequence[str],
    row_volumes: Sequence[str],
) -> List[str]:
    """按版心宽分列：固定列宽 + 换行，不缩放。

    列宽写成 ``p{\\dimexpr <权重>\\textwidth-<列间距份额>pt}``：权重是各列占版心宽的比例
    （成书表格自然宽度算出来的，或索引页自己按内容算的），一帧的版心宽是定值，权重乘上去
    就是列宽。每个列宽里预扣的是它在列间距里该摊的那一份，于是各列宽加上列间距正好等于
    版心宽——差一点就是 overfull，多出一点就是 underfull，两种都躲开。表格的样子（无三线、
    表头 18 pt、数据 16 pt）见 :func:`_table_head` 与 :func:`_table_body`。
    """
    columns = len(rows[0])
    gap = TABLE_GAP_PT * (columns - 1) / columns
    separator = r"@{\hspace{" + f"{TABLE_GAP_PT:.1f}" + r"pt}}"
    spec = "@{}" + separator.join(
        ">{\\" + ("raggedleft" if _align_of(align, index) == "r" else "raggedright")
        + r"\arraybackslash}p{\dimexpr "
        + f"{weight:.4f}"
        + r"\textwidth-"
        + f"{gap:.4f}"
        + r"pt\relax}"
        for index, weight in enumerate(fractions)
    ) + "@{}"
    out = [r"\begingroup\FzTab", *_table_preamble(), r"\begin{tabular}{" + spec + "}"]
    out.extend(_table_head(rows[0]))
    out.append(r"\ifdim\tmTabRuleW>0pt\hline\fi")
    out.extend(_table_body(rows[1:], row_volumes))
    out.extend([r"\end{tabular}", r"\endgroup"])
    return out


def _scaled_table(
    rows: Sequence[Sequence[str]], row_volumes: Sequence[str]
) -> List[str]:
    """整表等比缩小：自然列宽的 tabular 套 adjustbox，缩到版心宽与版心高之内。

    ``max width`` 与 ``max height`` 同时给出，adjustbox 取两个方向里更紧的那个比例，
    所以横向过宽与纵向过高的表都能落到一帧里；真落到不可读的地步才改跨页（调用方决定）。
    """
    out = [
        r"\begin{adjustbox}{max width=\textwidth,max height="
        + f"{ADJUST_MAX_HEIGHT}\\textheight" + "}",
        r"\begingroup\FzTab",
        *_table_preamble(),
        r"\begin{tabular}{@{}" + "l" * len(rows[0]) + "@{}}",
    ]
    out.extend(_table_head(rows[0]))
    out.append(r"\ifdim\tmTabRuleW>0pt\hline\fi")
    out.extend(_table_body(rows[1:], row_volumes))
    out.extend([r"\end{tabular}", r"\endgroup", r"\end{adjustbox}"])
    return out


def _table_head(cells: Sequence[str]) -> List[str]:
    return [r"\rowcolor{volhead}" + " & ".join("\\Hd{" + _cell(cell) + "}" for cell in cells) + r" \\"]


def _table_body(
    rows: Sequence[Sequence[str]], row_volumes: Sequence[str]
) -> List[str]:
    """数据行；``row_volumes`` 给到哪一行，哪一行的头一格就加一枚篇色小块。

    末行不写 ``\\\\``：那会多排一个空行，还被 ``\\rowcolors`` 着上斑马纹。
    """
    out: List[str] = []
    for index, row in enumerate(rows):
        cells = [_cell(cell) for cell in row]
        volume = row_volumes[index] if index < len(row_volumes) else ""
        if volume and cells:
            cells[0] = "\\RowCap{" + volume + "}" + cells[0]
        out.append(" & ".join(cells) + (r" \\" if index < len(rows) - 1 else ""))
    return out


def _align_of(align: Sequence[str], index: int) -> str:
    return align[index] if index < len(align) else "l"


def _inline(text: str) -> str:
    """幻灯片上的行内渲染：与成书共用 :func:`primer.book.tex.render_inline`，链接只留标签。

    成书把 ``[标签](url)`` 排成一枚 URL 脚注；投影的版面上那行脚注既多余又吃版面，所以
    幻灯片这一档不发脚注。正文要点与标题的链接在 ``build`` 侧另有账本，会把丢弃的目标
    记进发现（:func:`primer.slides.build._inline`）；这里只渲染工具自己的算术文本（表格
    单元、封面字段），它们取自成书目录与清单，不含链接。
    """
    return tex.render_inline(str(text), links=tex.LINK_LABEL)


def _cell(cell: str) -> str:
    return _inline(cell)


# ---------------------------------------------------------------- 帧


def emit_frames(
    frames: Sequence[Frame], context: DeckContext
) -> Tuple[List[str], List[Finding], int, List[str]]:
    """逐帧发射，返回（行序列，发现，帧数，表格折过哪些帧的说明）。"""
    sink = _Sink()
    lines: List[str] = []
    for frame in frames:
        lines.extend(_emit_frame(frame, context, sink))
    return lines, sink.findings, len(frames), sink.table_notes


def _emit_frame(frame: Frame, context: DeckContext, sink: _Sink) -> List[str]:
    if frame.kind == "title":
        return _cover_frame(frame, context)
    if frame.kind == "table":
        body, breakes = plan_beamer_table(
            frame.rows,
            frame.title or "table",
            context,
            sink,
            align=frame.table_align,
            row_volumes=frame.row_volumes,
            fractions=frame.table_fractions,
        )
        head = _page_head(frame, context)
        return _open(frame, breakes) + _indent([*head, *body]) + _close(frame)
    if frame.kind == "quote":
        # 主旨页：整宽一句话，装在版心高的 \vbox 里（两个 \vfil 平分余量，见导言区的说明），
        # 于是在标题带与页脚护栏之间纵向居中；两端对齐（左对齐的整宽段落），不再用
        # \begin{center} 把它缩在中间。
        body = [
            *_page_head(frame, context),
            r"\vbox to \dimexpr\paperheight-\tmTmpA-\tmBodyGuard-\tmBodySlack\relax{%",
            r"\vfil",
        ]
        if frame.points:
            body.append(r"\noindent{\FzBody\justifying " + frame.points[0] + r"\par}")
        body.extend([r"\vfil", r"}%", r"\vspace*{\tmBodyGuard}"])
        return _open(frame, False) + _indent(body) + _close(frame)
    if frame.kind == "backup":
        return _open(frame, False) + _indent(_backup_frame(frame, context)) + _close(frame)
    if frame.kind == "points":
        return _open(frame, False) + _indent(_points_frame(frame, context)) + _close(frame)
    raise SlidesError(f"unknown frame kind: {frame.kind!r}")


def _backup_frame(frame: Frame, context: DeckContext) -> List[str]:
    """备份帧：出处查找表，一行一条——篇色小块 + 章号｜出处｜要点摘要。

    每一条是**一段**（``\\par`` 收尾）：行高因此就是字号下限的行距，一行占一行——
    这正是 :func:`lookup_lines_per_frame` 量出来的那个数。不用 ``tabular`` 的 ``p{}``
    列：p 列的行高由它自己的 strut 撑到约 1.7 倍行距（实测 14/17 pt 的 p 行是 28.9 pt），
    同一帧能放的行数立刻少掉四成，而这里要的只是三列对齐，``\\makebox`` 就够。

    字号取主题梯级里最小的一档（:func:`lookup_level_command`）：这一页是给人查的，不是
    给人念的，条目多就用下限字号塞满一页，绝不再往下缩、也不添帧。

    一条也没有时不留空表：打一个待选记号，意思和正文页上那个一样——位置留着，句子等人圈。
    """
    lines = _page_head(frame, context)
    level = lookup_level_command(context.theme)
    if not frame.lookup:
        lines.extend(
            [
                r"\vspace*{6mm}",
                "{\\" + level + r"\color{thmmuted}" + PLACEHOLDER + "}",
                r"\vfill",
            ]
        )
        return lines
    columns = frame.lookup_columns
    label_mm = max(columns.label_mm - LOOKUP_CHIP_MM, 1.0)
    lines.append("\\begingroup\\" + level + r"\raggedright")
    for cell in frame.lookup:
        lines.append(
            r"\noindent\RowCap{" + cell.volume + "}"
            + r"\makebox[" + f"{label_mm:.2f}mm" + r"][l]{" + cell.label + "}"
            + r"\makebox[" + f"{columns.pointer_mm:.2f}mm"
            + r"][l]{\color{thmmuted}" + cell.pointer + "}"
            + r"\makebox[" + f"{columns.gist_mm:.2f}mm" + r"][l]{" + cell.gist + "}"
            + r"\par"
        )
    lines.extend([r"\endgroup", r"\vfill"])
    return lines


def _page_head(frame: Frame, context: DeckContext) -> List[str]:
    """一帧的篇色、进度条与标题带（标题、指针、页序三段）。

    进度条只在内容页、横向页与讨论页画（``span`` 有值的那几种）——封面、主旨、导航与备份
    不落进度，这是样张的做法：条的宽度即"读到全片的哪一段"，不该到处都是。
    """
    lines = [r"\SetVolume{" + (frame.volume or "V1") + "}"]
    if len(frame.span) == 2:
        lines.append(r"\SetPageBar{" + str(frame.span[0]) + "}{" + str(frame.span[1]) + "}")
    lines.append(
        r"\PageTop{"
        + frame.title
        + "}{"
        + frame.footer
        + "}{"
        + (f"{frame.page}/{context.page_total}" if frame.page else "")
        + "}"
    )
    return lines


def _points_frame(frame: Frame, context: DeckContext) -> List[str]:
    """要点帧：标题带下是正文条目，右侧一栏是图与图注（有图时）。

    **一行两块** ``[t]`` minipage：左 0.63\\textwidth 是正文（容量算术按它算），
    ``\\hfill`` 之后右 0.36\\textwidth 是图与图注。两块都在文本流里、以 ``\\vspace{0pt}``
    定参考点——图片的基线在底边，不这么钉住会把整列往下拖，[t] 的顶对齐就看不出来。
    整行装在版心高的 ``\\vbox`` 里（两个 ``\\vfil`` 平分余量），内容于是在标题带与页脚
    之间真正纵向居中；收尾一道护栏 ``\\vspace*{\\tmBodyGuard}`` 保证不压页脚与进度条。
    """
    lines = _page_head(frame, context)
    lines.append(r"\vbox to \dimexpr\paperheight-\tmTmpA-\tmBodyGuard-\tmBodySlack\relax{%")
    lines.append(r"\vfil")
    width = "0.63" if frame.figure is not None else "1.0"
    lines.append(r"\noindent")
    lines.append(r"\begin{minipage}[t]{" + width + r"\textwidth}\vspace{0pt}")
    for point in frame.points:
        lines.append(_body_item(point, frame.marker))
    if frame.figure is not None:
        # \hfill 与前一个 minipage 同处一行、行尾的 % 吃掉换行空格——两块的宽度加起来
        # 本就逼近版心，留一个行间空格就会顶出 1pt 的 overfull。
        lines.append(r"\end{minipage}\hfill%")
        lines.extend(
            [
                r"\begin{minipage}[t]{0.36\textwidth}\vspace{0pt}\centering",
                # 图铺满列宽、以高度封顶；外包 adjustbox 的 max width/max height 再兜一道，
                # 免得 keepaspectratio 的取整把图撑出列宽 1pt（就是一列 overfull）。
                r"\adjustbox{max width=\linewidth,max height="
                + f"{context.figure_max_height_mm:.1f}mm"
                + r"}{\includegraphics[width=\linewidth]{"
                + frame.figure.path
                + "}}",
                r"\par\vspace{1.6mm}",
                # 图注保持左对齐（整栏 \centering 只管图，不拉伸题注）
                r"\begin{minipage}{\linewidth}\raggedright\FzSecond\color{thmmuted}"
                + frame.figure_label
                + r"\end{minipage}\par",
                r"\end{minipage}",
            ]
        )
    else:
        lines.append(r"\end{minipage}")
    lines.extend([r"\par", r"\vfil", r"}%", r"\vspace*{\tmBodyGuard}"])
    return lines


def _body_item(point: str, marker: str) -> str:
    """一条要点：待选记号走页脚灰（它是待办，不是正文），其余走篇色或强调色。"""
    if point == PLACEHOLDER:
        return r"\BodyItem[thmmuted]{{\color{thmmuted}" + point + r"}}"
    if marker:
        return r"\BodyItem[" + marker + "]{" + point + r"}"
    return r"\BodyItem{" + point + r"}"


def _cover_frame(frame: Frame, context: DeckContext) -> List[str]:
    """封面：短横 + 书目在上，单位与报告人在页面**下部居中**，页序在右下。

    首屏高不再写死 46mm：先把标题块（短横 + 主旨 + 书目副题 + 可选副题）用 ``\\sbox``
    排出来，量出它的 ``\\ht+\\dp``，色带高 = ``max(46mm, 标题块顶 + 块高 + 净距)``，
    再画色带的底与下沿发丝线，最后把盒子放上去。加不加副题、标题几行，发丝线都不会穿字。

    ``frame.cover_subline`` 非空时，在书目副题（书名下的年份）之下加一行副题：字号与正文
    同级（``\\FzBody``）、走灰阶（``thmmuted``）、与上一行留一个小净距
    （:data:`primer.slides.theme.COVER_SUBLINE_GAP_MM`）。空串整行不发。

    署名块（institute 与可选的 presenter／occasion）锚在 ``current page.south`` 上移
    ``\\tmCoverFootY`` 处、整宽居中：两行居中排，institute 走正文墨色、stamp 走灰阶。
    """
    volume = context.volume_key(frame.volume or "")
    lines = [
        r"\begin{frame}[plain,t]",
        r"  \SetVolume{" + volume + "}",
        # 1. 先排标题块并量高（\ht+\dp 就是块占的纵向尺寸）
        r"  \sbox{\tmCoverBoxS}{%",
        r"    \begin{minipage}[t]{\textwidth}",
        r"      {\color{thmaccent}\rule{\tmCoverRuleLen}{\tmCoverRuleW}}\par",
        r"      \vspace{4.2mm}",
        r"      {\FzTitle\sffamily\color{thmcovertitlefg}" + _inline(context.title) + r"}\par",
        r"      \vspace{1.4mm}",
        r"      {\FzBody\color{thmcoversubfg}" + _inline(context.subtitle) + r"}\par",
    ]
    if frame.cover_subline:
        lines.extend(
            [
                r"      \vspace{" + f"{COVER_SUBLINE_GAP_MM:g}" + r"mm}",
                r"      {\FzBody\color{thmmuted}" + frame.cover_subline + r"}\par",
            ]
        )
    lines.extend(
        [
            r"    \end{minipage}}",
            # 2. 色带高 = max(最小高, 标题块顶 + 块高 + 净距)——有副题、标题两行都盖得住
            r"  \setlength{\tmCoverH}{\dimexpr\tmCoverTop+\ht\tmCoverBoxS+\dp\tmCoverBoxS+\tmCoverClear\relax}",
            r"  \ifdim\tmCoverH<\tmCoverMinH\relax\setlength{\tmCoverH}{\tmCoverMinH}\fi",
            r"  \begin{tikzpicture}[remember picture,overlay]",
            r"    \fill[thmcoverbg] (current page.north west) rectangle ([yshift=-\tmCoverH]current page.north east);",
            r"    \ifdim\tmCoverEdgeW>0pt",
            r"      \fill[thmcoveredge] ([yshift=-\tmCoverH]current page.north west)",
            r"        rectangle ([yshift=-\dimexpr\tmCoverH+\tmCoverEdgeW\relax]current page.north east);",
            r"    \fi",
            # 3. 色带定好再把标题盒放到它该在的位置
            r"    \node[anchor=north west,inner sep=0pt] at ([xshift=\tmMargin,yshift=-\tmCoverTop]current page.north west) {\usebox{\tmCoverBoxS}};",
        ]
    )
    lower = [(_inline(context.institute), "thmink")]
    stamp = "　".join(
        part for part in (context.presenter, context.occasion) if part
    )
    if stamp:
        lower.append((_inline(stamp), "thmmuted"))
    lower = [(text, colour) for text, colour in lower if text]
    if lower:
        # 署名：页面下部居中（anchor=south 钉在 \tmCoverFootY，整宽 minipage 里居中排）
        lines.extend(
            [
                r"    \node[anchor=south,inner sep=0pt] at ([yshift=\tmCoverFootY]current page.south) {%",
                r"      \begin{minipage}{\textwidth}\centering",
            ]
        )
        for index, (text, colour) in enumerate(lower):
            if index:
                lines.append(r"        \vspace{1.2mm}")
            lines.append(r"        {\FzSecond\color{" + colour + "}" + text + r"}\par")
        lines.append(r"      \end{minipage}};")
    lines.extend(
        [
            r"    \node[anchor=south east,inner sep=0pt] at ([xshift=-\tmMargin,yshift=\tmFootTextY]current page.south east)",
            r"      {\FzFoot\color{thmfootfg}"
            + (f"{frame.page}/{context.page_total}" if frame.page else "")
            + r"};",
            r"  \end{tikzpicture}%",
            r"  \vfill",
            r"\end{frame}",
        ]
    )
    if frame.notes:
        lines.insert(-1, r"  \note{" + frame.notes + r"}")
    return lines


def _open(frame: Frame, breakes: bool) -> List[str]:
    options = "[allowframebreaks]" if breakes else "[plain,t]"
    return [r"\begin{frame}" + options]


def _close(frame: Frame) -> List[str]:
    if not frame.notes:
        return [r"\end{frame}"]
    return [r"\note{" + frame.notes + r"}", r"\end{frame}"]


def _indent(lines: Sequence[str]) -> List[str]:
    return ["  " + line for line in lines]


# ---------------------------------------------------------------- 文档


def render_preamble(context: DeckContext) -> str:
    """导言区：画布、字号、字体栈与配色全部取自主题；书目取自规格（经 context）。

    字体取自主题里那套无衬线的幻灯片字体栈（见 :data:`primer.slides.theme.DEFAULT_FONTS`），
    成书字体与它无关。导言区里只剩下版面：它不再需要"排完帧才知道"的量——页轴与结构图的
    几何随结构图那一页一起去掉了。
    """
    theme = context.theme
    template = tex.TexTemplate(PREAMBLE)
    return template.substitute(
        main_font=theme.font("main"),
        sans_font=theme.font("sans"),
        mono_font=theme.font("mono"),
        cjk_main_font=theme.font("cjk_main"),
        cjk_sans_font=theme.font("cjk_sans"),
        cjk_mono_font=theme.font("cjk_mono"),
        cjk_fake_bold=DEFAULT_CJK_FAKE_BOLD,
        cjk_sans_fake_bold=DEFAULT_CJK_SANS_FAKE_BOLD,
        cjk_fake_slant=DEFAULT_CJK_FAKE_SLANT,
        graphics_path=context.graphics_path,
        unicode_lines=UNICODE_LINES,
        canvas_width=f"{theme.width_bp:g}",
        canvas_height=f"{theme.height_bp:g}",
        margin_ratio=f"{theme.margin_ratio:g}",
        margin_percent=f"{theme.margin_ratio * 100:.0f}%",
        body_guard=f"{FOOTER_INK_FROM_BOTTOM_MM + BODY_FOOTER_CLEARANCE_MM:g}",
        type_scale_line=_type_scale_line(theme),
        fz_title_size=f"{theme.level('title').size:g}",
        fz_title_leading=f"{theme.level('title').leading:g}",
        fz_body_size=f"{theme.level('body').size:g}",
        fz_body_leading=f"{theme.level('body').leading:g}",
        fz_table_head_size=f"{theme.level('table_head').size:g}",
        fz_table_head_leading=f"{theme.level('table_head').leading:g}",
        fz_table_body_size=f"{theme.level('table_body').size:g}",
        fz_table_body_leading=f"{theme.level('table_body').leading:g}",
        fz_caption_size=f"{theme.level('caption').size:g}",
        fz_caption_leading=f"{theme.level('caption').leading:g}",
        fz_footer_size=f"{theme.level('footer').size:g}",
        fz_footer_leading=f"{theme.level('footer').leading:g}",
        ink=_hex(theme.palette.get("ink")),
        muted=_hex(theme.palette.get("muted")),
        hairline=_hex(theme.palette.get("hairline")),
        accent=_hex(theme.palette.get("accent")),
        vol1=_hex(theme.volume_colour("V1")),
        vol2=_hex(theme.volume_colour("V2")),
        vol3=_hex(theme.volume_colour("V3")),
        vol4=_hex(theme.volume_colour("VA")),
        page_total=str(context.page_total),
        title=_inline(context.title),
        subtitle=_inline(context.subtitle),
        author=_inline(context.presenter),
        institute=_inline(context.institute),
    )


def _hex(value: object) -> str:
    """``#1a1a1a`` → ``1a1a1a``（xcolor 的 HTML 模型要六位码，不要 #）。"""
    text = str(value or "").lstrip("#")
    return text or "000000"


def _type_scale_line(theme: Theme) -> str:
    return "、".join(
        f"{name} {theme.level(name).size:g}/{theme.level(name).leading:g}"
        for name in ("title", "body", "table_head", "table_body", "caption", "footer")
    )


def render_document(
    frames: Sequence[Frame], context: DeckContext
) -> Tuple[str, List[Finding], int, List[str]]:
    """整份 ``slides.tex`` 的文本，以及发射过程中的发现与帧数。"""
    context = with_layout_geometry(context, frames)
    lines, findings, count, table_notes = emit_frames(frames, context)
    document = "\n".join(
        [render_preamble(context), r"\begin{document}", *lines, r"\end{document}", ""]
    )
    return document, findings, count, table_notes


def with_layout_geometry(context: DeckContext, frames: Sequence[Frame]) -> DeckContext:
    """把"排完帧才知道"的那个量填进 context：全片帧数（页脚的分母）。"""
    return replace(context, page_total=len(frames))
