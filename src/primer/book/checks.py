# -*- coding: utf-8 -*-
r"""确定性质检（``check --deep``）：只看文本层就能抓到的那一类缺陷。

``check`` 原来只读 LaTeX 日志，而日志看不见"markdown 语法被原样印进 PDF"这类
缺陷——它们不产生任何警告，字也照排。视觉校对（``inspect``）看得见，却昂贵、有
随机性。本模块把手头已有的文本产物过一遍：``.tex``、``.toc``/``.lof``/``.lot``、
``.log`` 里的版心几何、以及 PDF 经 ``pdftotext -bbox`` 给出的词框。确定、免费、
召回率 100%。

十个检查各是一个纯函数，只吃字符串与路径，便于单测；:func:`run_checks` 负责读盘
并把它们汇总成发现列表：

* ``markdown-residue``（error）：正文里残留的 markdown 语法（含行首的 ``>`` 引用标记）；
* ``straight-quote``（warning）：正文里残留的 ASCII 直引号；
* ``quote-direction``（warning）：中文弯引号没有严格成对交替；
* ``toc-lists-itself``（warning）：目录／插图目录／表格目录把自身列成条目；
* ``list-entry-mismatch``（warning）：图／表标签数与插图目录／表格目录条目数不符；
* ``text-out-of-block``（warning）：词框越过版心左右边界（横向页改量纸张边距）；
* ``running-head-collision``（warning）：页眉里左右两条标记叠印——LaTeX 看不见的缺陷；
* ``table-orphan-line``（info）：表格某格断行后只剩一个汉字的行；
* ``page-near-blank``（info）：正文墨迹近乎空白的一页（页眉与页码不计）；
* ``figure-low-resolution``（info／warning）：插图的有效分辨率过低；插图文件缺失是
  ``figure-file-missing``（error）。

另外几条是"这项检查没做成"的说明，一律 info，不影响退出码：``figure-size-unknown``
（认不出的图片格式）、``tex-missing``／``pdf-missing``（产物不在，先 ``build``）、
``geometry-unknown``（``.log`` 里没有 geometry 报告）、``pdftotext-unavailable``
（poppler 缺席或调用失败）。

几何量一律从 ``.log`` 的 geometry 报告里读（不写死 A4）。日志里的单位是 TeX pt
（72.27/inch），``pdftotext -bbox`` 给的却是 PDF pt（72/inch），所以内部一律换算成
PDF pt（bp）再比：2.54cm 恰好是 72bp，A4 的 ``\paperwidth`` 597.50787 TeX pt 换算
过来正是 595.28 bp。

页级检查在 poppler 缺席时降级：只落一条指名缺哪个二进制的 ``info`` 发现，不抛异常。
"""

from __future__ import annotations

import html
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from ..paths import relative_to_root
from . import preamble
from .findings import Finding
from .makefile import BuildPlan
from .manifest import BookManifest, Typography

# ---------------------------------------------------------------- 阈值

# TeX pt → PDF pt（bp）。1bp 恰是 1/72 inch，1TeX pt 是 1/72.27 inch。
BP_PER_TEX_PT = 72.0 / 72.27

# 词框越过版心的容差：字形本身的左右边距（side bearing）可以有零点几 bp。
# 实测：正文某行最后一个拉丁字母的墨迹最远到版心右界外 0.98bp（第 133 页），
# 属于这一档，不算越界。
TEXT_BLOCK_TOLERANCE_BP = 2.0

# 行末／行首中文标点允许悬挂的宽度上限：一个字（em）。详见 text_out_of_block。
# 横向页的词框量的是纸张边距，这个悬挂宽度远小于该边距，不必再单独放宽。
CJK_PUNCTUATION_RANGES = ((0x3000, 0x303F), (0xFF00, 0xFFEF))
# 破折号／连接号不在这两个区段里，但实测同样按半个字宽悬挂（本书 35 处，3.7bp），
# 一并放行。
HANGING_PUNCTUATION = "\u2014\u2013"

# 横向页的判据：内容离纸张四边不得近于这个值。实测本书 6 张横向页的纸边距在
# 39.5bp（词框）／41.5bp（墨迹）以上，36bp 留了余量又足以抓出真正压边的表格。
LANDSCAPE_PAPER_MARGIN_BP = 36.0

# "近乎空白"的判据：词框面积之和不足页面的这个比例。本书记正经正文页覆盖约 25%，
# 1% 比它低一个半数量级，既能抓出真正空白的页（0%），也只会顺带把封面页（0.01%）
# 与篇题页（0.3%—0.8%）这类本来就没多少字的前置页报出来让人自己判断。
NEAR_BLANK_COVERAGE = 0.01

# 插图有效分辨率：低于 120dpi 提醒，低于 72dpi 报警（90dpi 以下肉眼可见发糊）。
LOW_RESOLUTION_DPI = 120.0
VERY_LOW_RESOLUTION_DPI = 72.0

# 页眉叠印：词框落在版心上边界之上（页眉在版心外），且不低于这个上界——页眉不会
# 贴到纸顶。没有版心几何量时的兜底带宽用到这个下界。
HEADER_BAND_TOP_BP = 20.0
# 没有几何量时的兜底带宽上界（版心上边界 72bp 之上一点点）。
HEADER_BAND_BOTTOM_BP = 95.0
# 同一视觉行的判据：两个词框的 y 区间重叠要超过较矮那个词框高度的一半。
HEADER_LINE_OVERLAP = 0.5
# 叠印的判据：两个词框的 x 区间重叠超过这个值（正常字距在 2bp 以内）。
RUNNING_HEAD_X_OVERLAP_BP = 2.0

# 直引号逐处位置的报告上限；超出只报一条总数。
MAX_STRAIGHT_QUOTE_LOCATIONS = 25

# ---------------------------------------------------------------- 表行拆段阈值

# "表格孤字行"（``table-orphan-line``）的判据：一条视觉行按 x 空隙拆成若干段，
# 若某一段的内容只剩一个汉字，就是单元格断行断到只剩一个字的瑕疵。
#
# 拆段的空隙阈值。本书表格的列间距是 2×tabcolsep ≈ 12bp；拉丁文的词间空格约
# 3—4bp、中文串约 0（见 :func:`text_out_of_block` 的悬挂说明）。8bp 落在两者
# 之间：既不会把一个词拆开，也不会把相邻两列并成一段。实测 8/9/10bp 结果一致。
TABLE_COLUMN_GAP_BP = 8.0
# 段首左缘与"表列"的对齐容差。
TABLE_COLUMN_TOLERANCE_BP = 3.0
# 一个左缘位置要算作表列，至少得有这么多条视觉行在它上面起段——正文各行的断点
# 互不相同，凑不出这样的重复列位。
TABLE_COLUMN_SUPPORT_LINES = 2
# 至少这么多段才算一条"表行"；本书表格都是三列以上。段数不足的行，只有当它紧
# 挨着一条表行、且每一段都落在表列上时才认作表行的续行（某格还在排、其余各列
# 已经排完）。
TABLE_ROW_MIN_SEGMENTS = 3
# 续行与表行之间允许的最大垂直空隙（bp）。表内行距实测 6bp 上下，表行与题注／
# 正文之间至少 16bp；22bp 足以把续行纳入，又不会把表外的一行正文拉进来。
TABLE_ROW_ADJACENT_GAP_BP = 22.0
# 孤字行逐处报告的上限；超出只报一条总数与分页统计（与 straight-quote 同一约定）。
MAX_TABLE_ORPHAN_LOCATIONS = 25

# 孤字段的 CJK 区段：基本汉字与扩展 A。
ORPHAN_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
# 段末的引用标记组（``[366][367][373]``）：它随单元格文字一起排出，判断"这一段
# 是不是孤字"时先剥掉——孤字行最常见的形态就是"化 [366][367][373]"。
CITATION_GROUP_RE = re.compile(r"\[[^\[\]]*\]")
# 表格序号列：行首带一个纯数字段的行是这一行的首行。
ROW_INDEX_RE = re.compile(r"\d+")


# ---------------------------------------------------------------- 版心几何


@dataclass(frozen=True)
class Geometry:
    """版心（text block）与纸张在 PDF 坐标里的边界，单位 bp，原点在纸张左上角。"""

    left: float
    right: float
    top: float
    bottom: float
    paper_width: float
    paper_height: float


H_PART_RE = re.compile(
    r"h-part:\(L,W,R\)=\s*\(\s*([\d.]+)pt,\s*([\d.]+)pt,\s*([\d.]+)pt\s*\)"
)
V_PART_RE = re.compile(
    r"v-part:\(T,H,B\)=\s*\(\s*([\d.]+)pt,\s*([\d.]+)pt,\s*([\d.]+)pt\s*\)"
)
PAPER_RE = re.compile(r"\\(?:paperwidth|paperheight)=([\d.]+)pt")


def parse_geometry(log_text: str) -> Optional[Geometry]:
    """从 ``.log`` 的 geometry 报告里读出纸张与版心，换算成 bp。

    读的是 geometry 包的 verbose 报告：

    ``* h-part:(L,W,R)=(72.2698pt, 452.96826pt, 72.2698pt)`` 给出左边距、正文宽、
    右边距；``* v-part:(T,H,B)=(...)`` 给出上边距、正文高、下边距；后两行是
    ``\\paperwidth``／``\\paperheight``。四者都是 TeX pt，乘 :data:`BP_PER_TEX_PT`
    换成 bp 后才与 ``pdftotext`` 的坐标同度量。缺任何一段就返回 ``None``——宁可
    不检查，也不要拿写死的 A4 去量一张别的纸。
    """
    h_part = H_PART_RE.search(log_text)
    v_part = V_PART_RE.search(log_text)
    paper = PAPER_RE.findall(log_text)
    if not h_part or not v_part or len(paper) < 2:
        return None
    left, width, _right = (float(value) * BP_PER_TEX_PT for value in h_part.groups())
    top, height, _bottom = (float(value) * BP_PER_TEX_PT for value in v_part.groups())
    return Geometry(
        left=left,
        right=left + width,
        top=top,
        bottom=top + height,
        paper_width=float(paper[0]) * BP_PER_TEX_PT,
        paper_height=float(paper[1]) * BP_PER_TEX_PT,
    )


# ---------------------------------------------------------------- 页级词框


@dataclass(frozen=True)
class Word:
    """一个词框；坐标单位 bp，``top``/``bottom`` 自页顶向下量。"""

    text: str
    left: float
    right: float
    top: float
    bottom: float


@dataclass(frozen=True)
class BboxPage:
    """``pdftotext -bbox`` 的一页。"""

    number: int
    width: float
    height: float
    words: Tuple[Word, ...]


PAGE_RE = re.compile(r'<page width="([\d.]+)" height="([\d.]+)">(.*?)</page>', re.S)
WORD_RE = re.compile(
    r'<word xMin="([\d.eE+-]+)" yMin="([\d.eE+-]+)" '
    r'xMax="([\d.eE+-]+)" yMax="([\d.eE+-]+)">(.*?)</word>',
    re.S,
)


def parse_bbox(text: str) -> List[BboxPage]:
    """解析 ``pdftotext -bbox`` 的 XHTML，返回按页码顺序的页列表。"""
    pages: List[BboxPage] = []
    for number, (width, height, body) in enumerate(PAGE_RE.findall(text), start=1):
        words = tuple(
            Word(
                text=html.unescape(label),
                left=float(x_min),
                right=float(x_max),
                top=float(y_min),
                bottom=float(y_max),
            )
            for x_min, y_min, x_max, y_max, label in WORD_RE.findall(body)
        )
        pages.append(BboxPage(number=number, width=float(width), height=float(height), words=words))
    return pages


def text_out_of_block(
    pages: Sequence[BboxPage],
    geometry: Geometry,
    em_bp: float,
    tolerance: float = TEXT_BLOCK_TOLERANCE_BP,
    landscape_margin_bp: float = LANDSCAPE_PAPER_MARGIN_BP,
) -> List[Finding]:
    """词框越过版心左右边界超过 ``tolerance`` bp 的发现；横向页改量纸张边距。

    这比 LaTeX 自己的 ``Overfull \\hbox`` 更严：``\\tolerance=2000`` 与
    ``\\emergencystretch=3em`` 允许引擎把行悄悄拉长而不报警。竖排版面只查左右
    （横向），上下不动：页眉与页码本来就落在版心之外的边距里，纵向要量也得量纸张
    边缘，而横向的越界才是"排不下"。

    **一个 em 的标点悬挂**。``pdftotext -bbox`` 的词框按**字形步进宽**拼出来，
    而 xeCJK 会把行末的全角标点（``，。：；、）》］`` 等）压缩并往版心内拉半个字：
    墨迹退回去了，步进宽度没有。于是每一行的行末标点都在词框里"越界"约 9—10bp。
    三条独立证据说明这不是真越界：(1) 本书 LaTeX 日志里 ``Overfull \\hbox`` 为 0；
    (2) 把第 3、17、24、26、28、41、59、61、64、114、133、160、170 页按 300dpi
    光栅化后扫版心外的像素条，墨迹从未越出版心超过 1.1bp（第 133 页 524.26bp，
    版心右界 523.28bp，低于 :data:`TEXT_BLOCK_TOLERANCE_BP`），其余各页都留在
    版心内；(3) 越界的那个字形按字符类统计 100% 是标点——本书 357 处候选，一个非
    标点都没有，全部发生在 ``，：、。–；（）］》？《`` 这 12 个字形上。

    因此：越界的那个字形若是 ``\\u3000-\\u303f``／``\\uff00-\\uffef`` 区段里的
    全角标点（或破折号／连接号），且越界量不超过一个正文字号（``em_bp``，由清单的
    正文字号推出，不是写死的 14pt），就按"悬挂"放行；**其余一律仍按
    :data:`TEXT_BLOCK_TOLERANCE_BP` 判**——非标点词越界超过 2bp 就是真缺陷，这正是
    本检查存在的理由，不会因为这条豁免而消失。本书的正文字号是四号，一个 em 合
    13.95bp，而实测最大越界量是 10.37bp——余量来自字号本身，不是调出来的。

    横向页（``pdflscape`` 转出来的表格页，``/Rotate`` 为 90）的坐标不在竖排版心
    那个坐标系里：``.log`` 里的几何量描述的是竖排版的版心，而这些页的词框 ``x``
    会超出 bbox 自己声明的页宽（实测 799.5 > 595.28）。它们改用**纸张边距**判据：
    词框离纸张四边不得近于 ``landscape_margin_bp``。横向页上真正压边的表格会被抓
    到，而页眉、页码这类本来就落在纸边附近的东西也不再产生假越界。横向页不再放宽
    一个 em——纸边距（36bp）远大于悬挂宽度。
    """
    findings: List[Finding] = []
    for page in pages:
        if _coordinates_rotated(page):
            findings.extend(_landscape_margin_findings(page, landscape_margin_bp))
            continue
        for word in page.words:
            for side, amount, edge, glyph in (
                ("left", geometry.left - word.left, geometry.left, word.text[:1]),
                ("right", word.right - geometry.right, geometry.right, word.text[-1:]),
            ):
                if amount <= tolerance:
                    continue
                if _may_hang(glyph, amount, em_bp):
                    continue
                findings.append(
                    Finding(
                        code="text-out-of-block",
                        severity="warning",
                        message=(
                            f"word box extends {amount:.1f} bp past the {side} edge of the "
                            f"text block (block {side} {edge:.2f} bp)"
                        ),
                        location=f"page {page.number}: {_shorten(word.text)}",
                    )
                )
    return findings


def _landscape_margin_findings(page: BboxPage, margin_bp: float) -> List[Finding]:
    """横向页：拿词框量纸张四边，离边近于 ``margin_bp`` 的报出来。

    坐标框是被转置的（bbox 声明的宽高与词框所在的那一轴相反），所以纸张边长取
    ``max``／``min`` 两个方向，与 ``/Rotate`` 是 90 还是 270 无关。
    """
    paper_width, paper_height = max(page.width, page.height), min(page.width, page.height)
    findings: List[Finding] = []
    for word in page.words:
        for side, distance in (
            ("left", word.left),
            ("right", paper_width - word.right),
            ("top", word.top),
            ("bottom", paper_height - word.bottom),
        ):
            if distance >= margin_bp:
                continue
            findings.append(
                Finding(
                    code="text-out-of-block",
                    severity="warning",
                    message=(
                        f"on a landscape page the word box comes within {distance:.1f} bp of the "
                        f"paper's {side} edge (limit {margin_bp:.0f} bp)"
                    ),
                    location=f"page {page.number}: {_shorten(word.text)}",
                )
            )
    return findings


def _may_hang(glyph: str, amount: float, em_bp: float) -> bool:
    """越界的那个字形是不是"允许悬挂"的全角标点，且越界量不超过一个 em。"""
    if not glyph or amount > em_bp:
        return False
    code = ord(glyph)
    if any(low <= code <= high for low, high in CJK_PUNCTUATION_RANGES):
        return True
    return glyph in HANGING_PUNCTUATION


def body_em_bp(typography: Typography) -> float:
    """一个正文字号的宽度（em），单位 bp。

    字号从清单的 ``typography.body_font_size`` 查（``\\zihao`` 代码或磅值），走
    :func:`primer.book.preamble.body_points` ——与导言区实际写进 ``\\fontsize`` 的
    是同一个换算，不另立一张表、也不写死字号。
    """
    return preamble.body_points(typography.body_font_size) * BP_PER_TEX_PT


def header_words(page: BboxPage, geometry: Optional[Geometry]) -> List[Word]:
    """该页页眉带里的词框：落在版心上边界之上的那些。

    页眉（``fancyhdr`` 的 ``\\leftmark``／``\\rightmark``）排在版心之外，实测
    y 在 42—57bp；正文第一行从版心上边界（72bp）往下开始。有几何量就按
    ``bottom <= geometry.top`` 切，没有几何量时退到固定带宽
    （:data:`HEADER_BAND_TOP_BP`—:data:`HEADER_BAND_BOTTOM_BP`）。

    注意不能把版心上边界之下的词算进来：那里是正文，词框之间的"重叠"只是全角
    标点的步进宽度（见 :func:`text_out_of_block` 的悬挂说明），不是叠印。
    """
    if geometry is not None:
        return [w for w in page.words if w.top > HEADER_BAND_TOP_BP and w.bottom <= geometry.top + 0.5]
    return [w for w in page.words if HEADER_BAND_TOP_BP < w.top and w.bottom < HEADER_BAND_BOTTOM_BP]


def running_head_collision(
    pages: Sequence[BboxPage],
    geometry: Optional[Geometry] = None,
    x_overlap_bp: float = RUNNING_HEAD_X_OVERLAP_BP,
) -> List[Finding]:
    """页眉里两条标记叠印：``fancyhdr`` 把左右标记各排进一个满版心的盒子再叠放。

    机制：``\\fancyhead[LO,RE]{\\rightmark}`` 与 ``\\fancyhead[LE,RO]{\\leftmark}``
    各自渲染成一个宽度等于 ``\\headwidth`` 的盒子，左标记从版心左缘起排、右标记
    贴版心右缘，二者**互相覆盖**。两条标记各自的宽度都小于 ``\\headwidth`` 时，
    LaTeX 不会报 ``Overfull \\hbox``——日志层对这类缺陷完全失明，只有 PDF 里
    文字的实际坐标看得见。

    判据（对页眉带里的词框两两比较）：

    * 同一视觉行——y 区间重叠超过较矮那个词框高度的一半；
    * x 区间重叠超过 ``x_overlap_bp``（:data:`RUNNING_HEAD_X_OVERLAP_BP`）。

    一页一条发现，取重叠量最大的那一对词框，报告两条词框的文字与重叠 bp。
    横向页（词框坐标系被转置）跳过；重排后的坐标不在页眉这个坐标系里。
    """
    findings: List[Finding] = []
    for page in pages:
        if _coordinates_rotated(page):
            continue
        words = header_words(page, geometry)
        best: Optional[Tuple[Word, Word, float]] = None
        for index, first in enumerate(words):
            for second in words[index + 1 :]:
                height = min(first.bottom - first.top, second.bottom - second.top)
                if min(first.bottom, second.bottom) - max(first.top, second.top) <= HEADER_LINE_OVERLAP * height:
                    continue
                overlap = min(first.right, second.right) - max(first.left, second.left)
                if overlap <= x_overlap_bp:
                    continue
                if best is None or overlap > best[2]:
                    best = (first, second, overlap)
        if best is not None:
            first, second, overlap = best
            findings.append(
                Finding(
                    code="running-head-collision",
                    severity="warning",
                    message=(
                        f"two runs of the running head are printed on top of each other "
                        f"(overlap {overlap:.1f} bp): {_shorten(first.text)} / {_shorten(second.text)}"
                    ),
                    location=f"page {page.number}",
                )
            )
    return findings


def page_near_blank(
    pages: Sequence[BboxPage], geometry: Optional[Geometry] = None
) -> List[Finding]:
    """**正文墨迹**覆盖面积不足页面的 :data:`NEAR_BLANK_COVERAGE`（1%）的页。

    覆盖面积 = 各词框面积之和／页面面积。本书正经正文页的覆盖约 25%，所以 1% 是
    "几乎没字"而不只是"字少"。已知合法命中：封面页、篇题页、章首页（它们本来就
    只有标题），以及 ``\\part`` 之后留下的空白 verso——检查的价值在于"没有理由
    空白的页"也会被一并列出，交给编者自己看。报告页码与词框数。

    只量**版心内的**词框：页眉（版心上边界之上的那条标记）与页码（版心下边界
    之下的那个数字）不算正文墨迹。否则页眉的长短会左右这个数字——实测缩短页眉
    曾把五个章末页从 1.1%—1.5% 压到 0.8%—0.9%，计数于是从 15 涨到 20，而书本身
    没动。排除这两条边距里的文字后，这个数只随正文变。没有几何量（缺 ``.log``）
    时退回"整页所有词框"，与旧行为一致。
    """
    findings: List[Finding] = []
    for page in pages:
        words = page.words if geometry is None else _body_words(page, geometry)
        area = page.width * page.height
        covered = sum(
            max(word.right - word.left, 0.0) * max(word.bottom - word.top, 0.0)
            for word in words
        )
        coverage = covered / area if area > 0 else 0.0
        if coverage < NEAR_BLANK_COVERAGE:
            findings.append(
                Finding(
                    code="page-near-blank",
                    severity="info",
                    message=(
                        f"page carries next to no text: {len(words)} word box(es), "
                        f"{coverage * 100:.2f}% of the page covered"
                    ),
                    location=f"page {page.number}",
                )
            )
    return findings


def _body_words(page: BboxPage, geometry: Geometry) -> List[Word]:
    """页面上落在版心内的词框：完全在版心之外（四个边距里）的词框都不要。

    页眉在版心上边界之上、页码在版心下边界之下，都落在边距里；页码甚至可能横向
    也在版心外（实测第 206 页的页码印在版心左缘以左）。横向页的坐标框是转置的，
    用不了竖排版的版心，原样返回全部词框。取"与版心有交叠"而非"完全落在版心
    内"，给悬挂标点与字形边距留余地。
    """
    if _coordinates_rotated(page):
        return list(page.words)
    return [
        word
        for word in page.words
        if word.right > geometry.left
        and word.left < geometry.right
        and word.bottom > geometry.top
        and word.top < geometry.bottom
    ]


def _visual_lines(words: Sequence[Word]) -> List[List[Word]]:
    """按 y 区间把词框聚成视觉行，用 :func:`running_head_collision` 的同一条判据。

    两个词框的 y 区间重叠超过较矮那个词框高度的一半，就算同一行。按 top 排序后
    逐个并入第一条满足的行，最后按行的 top 排序。
    """
    lines: List[List[Word]] = []
    for word in sorted(words, key=lambda item: (item.top, item.left)):
        for line in lines:
            shortest = min(word.bottom - word.top, min(b.bottom - b.top for b in line))
            top = max(word.top, max(b.top for b in line))
            bottom = min(word.bottom, min(b.bottom for b in line))
            if bottom - top > HEADER_LINE_OVERLAP * shortest:
                line.append(word)
                break
        else:
            lines.append([word])
    return sorted(lines, key=lambda line: min(word.top for word in line))


def _line_segments(line: Sequence[Word]) -> List[Tuple[float, float, str]]:
    """一条视觉行按 x 空隙拆段，返回每段的（左缘, 右缘, 文字）。

    相邻词框的空隙超过 :data:`TABLE_COLUMN_GAP_BP` 即认为跨了一个表列。段内文字
    是把该段各词框的文字直接拼起来——pdftotext 已经按空格切好了词。
    """
    ordered = sorted(line, key=lambda word: word.left)
    groups: List[List[Word]] = [[ordered[0]]]
    for previous, word in zip(ordered, ordered[1:]):
        if word.left - previous.right > TABLE_COLUMN_GAP_BP:
            groups.append([word])
        else:
            groups[-1].append(word)
    return [(group[0].left, group[-1].right, "".join(word.text for word in group)) for group in groups]


def _column_anchors(segment_lines: Sequence[Sequence[Tuple[float, float, str]]]) -> List[float]:
    """表列的左缘位置：至少 :data:`TABLE_COLUMN_SUPPORT_LINES` 条行在它上面起段。"""
    support: List[Tuple[float, set]] = []
    for index, segments in enumerate(segment_lines):
        for left, _right, _text in segments:
            for anchor, lines in support:
                if abs(anchor - left) <= TABLE_COLUMN_TOLERANCE_BP:
                    lines.add(index)
                    break
            else:
                support.append((left, {index}))
    return [anchor for anchor, lines in support if len(lines) >= TABLE_COLUMN_SUPPORT_LINES]


def _orphan_character(text: str) -> Optional[str]:
    """一段的全部 CJK 内容恰为一个字时返回那个字，否则 ``None``。

    段末的引用标记组先剥掉：单元格"电推主力化 [366][367][373]"断行后，最后一段的
    词框文字是 ``化[366][367][373]``，剥掉方括号才看得到那个孤零零的"化"。
    """
    core = CITATION_GROUP_RE.sub("", text).strip()
    if len(core) == 1 and ORPHAN_CJK_RE.fullmatch(core):
        return core
    return None


def _aligned_to_columns(
    segments: Sequence[Tuple[float, float, str]], anchors: Sequence[float]
) -> bool:
    """一行的每一段是否都落在某条表列的左缘上。"""
    return bool(segments) and all(
        any(abs(left - anchor) <= TABLE_COLUMN_TOLERANCE_BP for anchor in anchors)
        for left, _right, _text in segments
    )


def _line_gap(upper: Sequence[Word], lower: Sequence[Word]) -> float:
    """上下两条视觉行之间的垂直空隙（行框下缘到下一行框上缘，可正可负）。"""
    return min(word.top for word in lower) - max(word.bottom for word in upper)


def table_orphan_lines(
    pages: Sequence[BboxPage], geometry: Optional[Geometry] = None
) -> List[Finding]:
    r"""表格单元格断行后只剩一个汉字的行（``table-orphan-line``，info）。

    这一类瑕疵在 LaTeX 日志里看不见：xltabular 把一格排成两三行是正常换行，没有
    overfull 警告，字也照排；只有看印出来的版面，才发现某格的最后一行只有一个字。
    视觉校对报过 7 处，本检查给出确定性的计数。它**只量印出来的页**（``pdftotext
    -bbox`` 的词框），不预测也不重排。

    一个页面内的判据：

    1. 词框按 y 区间聚成视觉行（同 :func:`running_head_collision` 的同一条判据）；
       只取版心内的词框——页码落在边距里，会把一条行的最左段位顶掉。
    2. 行内按 x 空隙拆段，空隙超过 :data:`TABLE_COLUMN_GAP_BP` 即认为跨列。
    3. 表列 = 至少 :data:`TABLE_COLUMN_SUPPORT_LINES` 条行在它上面起段的左缘位置。
       段数达到 :data:`TABLE_ROW_MIN_SEGMENTS` 的行即表行——正文一行只有一段（段
       末的悬挂空隙最多再切出一段），三段的行只能是表格行。段数不足的行只有当它
       紧挨着（空隙不超过 :data:`TABLE_ROW_ADJACENT_GAP_BP`）一条表行、且每一段
       都落在表列上时才认，这是单元格断行后的续行（某格还在排、其余各列已经排
       完）。"段位落在表列上"就是"不是普通正文"的判据：正文各行的断点互不相同，
       凑不出重复的列位。
    4. 某段剥掉段末引用标记组后恰为一个 CJK 字，即为孤字段。
    5. 该孤字段所在的列，在前一条视觉行上必须有文字——孤字是单元格**断行后的
       尾巴**，上面必定还有同一格的前半截。整格内容就是一个"无"字的单元格不在此
       列，它不是断行造成的。
    6. 该行若带序号列（最左段是一个纯数字），说明它是这一行的首行；首行上的一个
       汉字是单元格的全部内容，不是断行尾巴，跳过。

    第 4 条比"整段恰为一个 CJK 字"放宽了一点：段末的引用标记组先剥掉，否则
    "化 [366][367][373]" 这类最常见的孤字行会被漏掉（本书第 90 页就是这一例）。

    每处一条发现，location 为 ``page N``，message 带孤字与该行其余各段的文字；
    最多列出 :data:`MAX_TABLE_ORPHAN_LOCATIONS` 处，其余汇总成一条带总数与分页
    统计的发现。
    """
    located: List[Finding] = []
    per_page: dict = {}
    total = 0
    for page in pages:
        words = page.words if geometry is None else _body_words(page, geometry)
        lines = _visual_lines(words)
        if not lines:
            continue
        segment_lines = [_line_segments(line) for line in lines]
        strong = [len(segments) >= TABLE_ROW_MIN_SEGMENTS for segments in segment_lines]
        if not any(strong):
            continue
        # 表列只从"段数够"的行上取：只有这些行才肯定是表行，候选续行不参与列位统计。
        anchors = _column_anchors(
            [segments if strong[index] else [] for index, segments in enumerate(segment_lines)]
        )
        if not anchors:
            continue
        aligned = [_aligned_to_columns(segments, anchors) for segments in segment_lines]
        table = list(strong)
        # 从"段数够"的行向外扩到紧邻的续行（某格断行后的尾巴行）。
        changed = True
        while changed:
            changed = False
            for index in range(len(lines)):
                if table[index] or not aligned[index]:
                    continue
                for neighbour in (index - 1, index + 1):
                    if not 0 <= neighbour < len(lines) or not table[neighbour]:
                        continue
                    upper, lower = (index, neighbour) if index < neighbour else (neighbour, index)
                    if _line_gap(lines[upper], lines[lower]) <= TABLE_ROW_ADJACENT_GAP_BP:
                        table[index] = True
                        changed = True
                        break
        for index, segments in enumerate(segment_lines):
            if not table[index] or not segments:
                continue
            if ROW_INDEX_RE.fullmatch(CITATION_GROUP_RE.sub("", segments[0][2]).strip() or "x"):
                continue
            if index == 0:
                continue
            previous = segment_lines[index - 1]
            for left, _right, text in segments:
                orphan = _orphan_character(text)
                if orphan is None:
                    continue
                if not any(abs(previous_left - left) <= TABLE_COLUMN_TOLERANCE_BP for previous_left, _, _ in previous):
                    continue
                total += 1
                per_page[page.number] = per_page.get(page.number, 0) + 1
                located.append(
                    Finding(
                        code="table-orphan-line",
                        severity="info",
                        message=(
                            f"a table line ends with a single CJK character {orphan!r}; "
                            f"the line's segments are: "
                            + " | ".join(_shorten(segment[2]) for segment in segments)
                        ),
                        location=f"page {page.number}",
                    )
                )
                break
    if total > MAX_TABLE_ORPHAN_LOCATIONS:
        breakdown = ", ".join(f"{page}×{count}" for page, count in sorted(per_page.items()))
        located = located[:MAX_TABLE_ORPHAN_LOCATIONS]
        located.append(
            Finding(
                code="table-orphan-line",
                severity="info",
                message=(
                    f"{total} table lines end with a single CJK character; only the first "
                    f"{MAX_TABLE_ORPHAN_LOCATIONS} are located (per page: {breakdown})"
                ),
            )
        )
    return located


def _coordinates_rotated(page: BboxPage) -> bool:
    """词框是否落在与"本页声明的纸张"不同的坐标系里（横向页的判据）。"""
    for word in page.words:
        if word.right > page.width + 0.5 or word.bottom > page.height + 0.5:
            return True
    return False


def extract_bbox(pdf: Path) -> List[BboxPage]:
    """调 ``pdftotext -bbox`` 取词框；二进制缺席或调用失败时降级返回空表。"""
    tool = shutil.which("pdftotext")
    if tool is None:
        raise BboxUnavailable("pdftotext is not on PATH")
    completed = subprocess.run([tool, "-bbox", str(pdf), "-"], capture_output=True)
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace").strip()
        raise BboxUnavailable(f"pdftotext -bbox failed with exit code {completed.returncode}: {detail[:200]}")
    return parse_bbox(completed.stdout.decode("utf-8", "replace"))


class BboxUnavailable(Exception):
    """``pdftotext -bbox`` 不可用（缺二进制，或调用失败）。"""


# ---------------------------------------------------------------- 文本层屏蔽

PREAMBLE_RE = re.compile(r"\A.*?\\begin\{document\}", re.S)
LSTLISTING_RE = re.compile(r"\\begin\{lstlisting\}.*?\\end\{lstlisting\}", re.S)
INCLUDEGRAPHICS_RE = re.compile(r"\\includegraphics\s*(\[[^\]]*\])?\s*\{([^{}]*)\}")
MATH_RE = re.compile(r"(?<!\\)\$[^$\n]*(?<!\\)\$")
TEXTTT_RE = re.compile(r"\\texttt\{[^{}]*\}")
URL_RE = re.compile(r"\\url\{[^{}]*\}")
HREF_RE = re.compile(r"\\href\{[^{}]*\}\{[^{}]*\}")
LABEL_RE = re.compile(r"\\(?:label|ref|pageref|cite|eqref)\{[^{}]*\}")
ADDCONTENTSLINE_RE = re.compile(r"\\addcontentsline\{[^{}]*\}\{[^{}]*\}\{[^{}]*\}")
CAPTION_OPT_RE = re.compile(r"\\caption\s*\[[^\]]*\]")
# 页眉标记：短标题是章／节标题的**截断副本**，同一段引号会再出现一次，扫描引号
# 方向时先屏蔽，免得把同一对引号数两遍（与 \addcontentsline 同理）。
HEADING_MARK_RE = re.compile(r"\\(?:chaptermark|sectionmark)\{[^{}]*\}")
MARKDOWN_TARGET_RE = re.compile(r"\]\([^)\n]*\)")


def blank(text: str) -> str:
    """等长、等行地把一段 LaTeX 抹成空格：换行原样保留，便于数行号。"""
    return "".join("\n" if char == "\n" else " " for char in text)


def mask_tex(text: str, keep_markdown_links: bool = False) -> str:
    """把不参与文本扫描的 LaTeX 区域抹成空格，行号与列位保持不变。

    屏蔽清单：导言区（``\\begin{document}`` 之前的一切）、``lstlisting`` 环境、
    ``\\includegraphics``、行内公式 ``$...$``、``\\texttt``/``\\url``/``\\href``、
    ``\\label``/``\\ref``/``\\cite`` 的键、``\\addcontentsline{...}{...}{...}``、
    ``\\caption[...]`` 的可选参数、``\\chaptermark{...}``/``\\sectionmark{...}``。
    最后三项是**同一段文字的副本**（目录条目、图目录的短题注、页眉里的短标题都
    会在正文里再出现一次），扫描引号方向时留着它们只会把同一对引号数两遍。

    ``keep_markdown_links`` 为真时保留 ``](...)`` 链接目标——查残留 markdown 语法
    时那正是要找的东西；其余检查用默认值，免得链接标题里的 ASCII 引号被当成
    正文直引号。
    """
    masked = PREAMBLE_RE.sub(lambda m: blank(m.group()), text)
    for pattern in (
        LSTLISTING_RE,
        INCLUDEGRAPHICS_RE,
        MATH_RE,
        TEXTTT_RE,
        URL_RE,
        HREF_RE,
        LABEL_RE,
        ADDCONTENTSLINE_RE,
        CAPTION_OPT_RE,
        HEADING_MARK_RE,
    ):
        masked = pattern.sub(lambda m: blank(m.group()), masked)
    if not keep_markdown_links:
        masked = MARKDOWN_TARGET_RE.sub(lambda m: blank(m.group()), masked)
    return masked


# ---------------------------------------------------------------- 1. markdown 残留

MARKDOWN_BOLD_RE = re.compile(r"\*\*")
TABLE_SEPARATOR_RE = re.compile(r"^[\s|:-]+$")
# 文献表逐行发射时给每行加的排版前缀（``\noindent\hangindent=2em ``）。它是排版
# 指令、不是内容，判"这行的内容是不是以引用标记开头"时先跳过。
BIBLIOGRAPHY_PREFIX_RE = re.compile(r"^[ \t]*\\noindent\\hangindent=[^ \t{}]*[ \t]+")


def markdown_residue(tex_text: str) -> List[Finding]:
    """正文里没被转换、原样印进 PDF 的 markdown 语法。

    五类信号，都在屏蔽过数学／代码／标签的正文上找：

    * ``](`` 与 ``![``：链接与插图的语法。生产里真出过这事——装配时先把工程根前缀
      抹掉，本地存档链接于是从绝对路径变成相对路径，链接正则匹配不上，整段
      ``[标题](目标 "title")`` 原样印进了三页 PDF；
    * ``**``：未成对的加粗标记（成对的已被 ``\\textbf`` 消化）；
    * 表格分隔行：整行只由 ``|``、``-``、``:``、空白组成且含 ``|``，至少一段
      ``--``。markdown 表格被原样印出来时，分隔行最显眼；
    * 行首的 ``>`` 引用标记：``render_bibliography`` 曾逐行原样发射文献表，
      ``> 版本：…`` 于是原样印进 PDF。判据是**行首**（可带前导空白，或跳过文献表
      的排版前缀）的 ``>``——不是"含 ``>``"：数学与 ``\\verb`` 里的 ``>`` 合法，
      由 :func:`mask_tex` 屏蔽，而 ``>450°C`` 这类正文里的 ``>`` 不在行首、不误报。

    每处一条发现，带行号与一小段上下文。
    """
    masked = mask_tex(tex_text, keep_markdown_links=True)
    findings: List[Finding] = []
    for pattern, what in (
        (re.compile(r"\]\("), "link syntax"),
        (re.compile(r"!\["), "image syntax"),
    ):
        for matched in pattern.finditer(masked):
            findings.append(
                Finding(
                    code="markdown-residue",
                    severity="error",
                    message=f"markdown {what} reached the typeset text",
                    location=f"line {_line_at(masked, matched.start())}: {_excerpt(masked, matched.start())}",
                )
            )
    for matched in MARKDOWN_BOLD_RE.finditer(masked):
        findings.append(
            Finding(
                code="markdown-residue",
                severity="error",
                message="markdown bold markers reached the typeset text",
                location=f"line {_line_at(masked, matched.start())}: {_excerpt(masked, matched.start())}",
            )
        )
    for number, line in enumerate(masked.splitlines(), start=1):
        if _is_table_separator(line):
            findings.append(
                Finding(
                    code="markdown-residue",
                    severity="error",
                    message="a markdown table separator row reached the typeset text",
                    location=f"line {number}: {line.strip()[:60]}",
                )
            )
        elif _is_blockquote_line(line):
            findings.append(
                Finding(
                    code="markdown-residue",
                    severity="error",
                    message="a markdown blockquote marker reached the typeset text",
                    location=f"line {number}: {line.strip()[:60]}",
                )
            )
    return findings


def _is_blockquote_line(line: str) -> bool:
    """这一行的内容是不是以 markdown 引用标记 ``>`` 开头。"""
    text = BIBLIOGRAPHY_PREFIX_RE.sub("", line, count=1)
    return text.lstrip().startswith(">")


def _is_table_separator(line: str) -> bool:
    stripped = line.strip()
    if "|" not in stripped:
        return False
    if not TABLE_SEPARATOR_RE.match(stripped):
        return False
    return re.search(r"-{2,}", stripped) is not None


# ---------------------------------------------------------------- 2. 直引号

STRAIGHT_QUOTE_RE = re.compile('"')


def straight_quotes(tex_text: str) -> List[Finding]:
    """正文里残留的 ASCII 直引号（U+0022）。

    清单要求成书一律用中文弯引号：CJK 字体把 ASCII 双引号画成全宽右引号，留着它
    就会**静默**印出方向错误的引号。装配时默认会把成对的直引号配成弯引号
    （:mod:`primer.book.quotes`），所以这里出现的要么是 ``--no-normalize-quotes``
    下放行的，要么是逃过配对的落单引号。

    每处一条发现（上限 :data:`MAX_STRAIGHT_QUOTE_LOCATIONS` 条），带行号与上下文；
    超限只报一条总数。计数即发现条数，见 :func:`summarize`。
    """
    masked = mask_tex(tex_text)
    hits = list(STRAIGHT_QUOTE_RE.finditer(masked))
    findings: List[Finding] = []
    for matched in hits[:MAX_STRAIGHT_QUOTE_LOCATIONS]:
        findings.append(
            Finding(
                code="straight-quote",
                severity="warning",
                message="ASCII double quote survives in the body; the CJK font renders it as a full-width right quote",
                location=f"line {_line_at(masked, matched.start())}: {_excerpt(masked, matched.start())}",
            )
        )
    if len(hits) > MAX_STRAIGHT_QUOTE_LOCATIONS:
        findings.append(
            Finding(
                code="straight-quote",
                severity="warning",
                message=(
                    f"{len(hits)} ASCII double quotes survive in the body; "
                    f"only the first {MAX_STRAIGHT_QUOTE_LOCATIONS} are located"
                ),
            )
        )
    return findings


# ---------------------------------------------------------------- 3. 引号方向

QUOTE_RE = re.compile("[“”]")


def quote_direction(tex_text: str) -> List[Finding]:
    """中文弯引号是否严格 ``“ ” “ ” …`` 交替。

    按文档顺序取出全部 ``“``／``”``，第奇数个必须是开引号、第偶数个必须是闭引号
    （从 1 起算），否则该处引号朝向错了。不用"``”`` 紧跟着 ``“`` 就是反的"那种
    邻近启发式——它会把每一个合法的 ``从“描述行星”到“理解过程”`` 都误报。

    两处**同一段文字的副本**先屏蔽掉再数：``\\addcontentsline{...}{...}{...}`` 的
    第三个参数，以及 ``\\caption[...]`` 的可选参数（插图目录用的短题注）。它们与
    正文里的题注是同一对引号的第二次出现，留着只会打乱交替。
    """
    masked = mask_tex(tex_text)
    findings: List[Finding] = []
    for index, matched in enumerate(QUOTE_RE.finditer(masked)):
        expected = "“" if index % 2 == 0 else "”"
        if matched.group() != expected:
            findings.append(
                Finding(
                    code="quote-direction",
                    severity="warning",
                    message=(
                        f"quote {matched.group()!r} is number {index + 1} in the body but "
                        f"{expected!r} was expected; a quote faces the wrong way"
                    ),
                    location=f"line {_line_at(masked, matched.start())}: {_excerpt(masked, matched.start())}",
                )
            )
    return findings


# ---------------------------------------------------------------- 4. 目录自列


def _read_group(text: str, start: int) -> Tuple[str, int]:
    """读一个以 ``text[start] == '{'`` 开头的花括号组，返回内容与组后下标。"""
    depth = 0
    for index in range(start, len(text)):
        char = text[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start + 1 : index], index + 1
    return text[start + 1 :], len(text)


def parse_contentslines(text: str) -> List[Tuple[str, str]]:
    """抽出 ``.toc``/``.lof``/``.lot`` 里的 ``\\contentsline``，返回（标题, 页码）。

    四个参数按花括号配对读，标题里的 ``\\numberline{...}``、``\\hspace{...}`` 都
    带嵌套花括号，正则数不对，所以手工配对。
    """
    entries: List[Tuple[str, str]] = []
    for matched in re.finditer(r"\\contentsline", text):
        index = matched.end()
        while index < len(text) and text[index] in " \t":
            index += 1
        groups: List[str] = []
        for _ in range(4):
            if index >= len(text) or text[index] != "{":
                break
            content, index = _read_group(text, index)
            groups.append(content)
            while index < len(text) and text[index] in " \t":
                index += 1
        if len(groups) >= 3:
            entries.append((groups[1], groups[2].strip()))
    return entries


def plain_title(title: str) -> str:
    """剥掉条目标题里的编号命令与花括号，留下可比对的纯文本。"""
    text = re.sub(r"\\hspace\s*\{[^{}]*\}", "", title)
    text = re.sub(r"\\numberline\s*\{[^{}]*\}", "", text)
    text = re.sub(r"\\[a-zA-Z@]+\s*", "", text)
    text = text.replace("{", "").replace("}", "")
    return " ".join(text.split())


def list_carries_itself(name: str, list_text: str) -> Optional[str]:
    """清单里是否有一条等于自己的名字；命中返回该条目的页码。"""
    for title, page in parse_contentslines(list_text):
        if plain_title(title) == name:
            return page
    return None


def toc_lists_itself(
    typography: Typography, toc: str = "", lof: str = "", lot: str = ""
) -> List[Finding]:
    """目录／插图目录／表格目录里有没有"把自己列成条目"。

    ``.toc`` 对 ``typography.toc_name``（目录）、``.lof`` 对
    ``typography.figure_list_name``（插图目录）、``.lot`` 对
    ``typography.table_list_name``（表格目录）。生产里真出过
    ``\\contentsline {chapter}{目录}{4}{section*.3}``。

    注意：``.toc`` 里合法地列着"插图目录""表格目录""附录目录"三条（它们是前置页里
    真正要翻回去找的清单），那不是缺陷——只有清单列**自己**才算。
    """
    findings: List[Finding] = []
    for name, text, label in (
        (typography.toc_name, toc, ".toc"),
        (typography.figure_list_name, lof, ".lof"),
        (typography.table_list_name, lot, ".lot"),
    ):
        page = list_carries_itself(name, text)
        if page is not None:
            findings.append(
                Finding(
                    code="toc-lists-itself",
                    severity="warning",
                    message=f"{label} carries an entry for the list itself ({name}); it should not list itself",
                    location=f"{label} entry points at page {page}",
                )
            )
    return findings


# ---------------------------------------------------------------- 5. 清单对账

CONTENTSLINE_KIND = {"fig:": ("figure", ".lof"), "tab:": ("table", ".lot")}


def list_entry_mismatch(
    tex_text: str, lof: Optional[str] = None, lot: Optional[str] = None
) -> List[Finding]:
    """正文里的图／表标签数与插图目录／表格目录的条目数对不对得上。

    一张有题注的图在正文里落一个 ``\\label{fig:…}``，在插图目录里落一条
    ``\\contentsline {figure}``——两边的数量必须相等；差一个就说明题注丢了或重了。
    只比数量与顺序，不比编号：``\\label`` 的键是作者在源文件里手写的旧编号
    （``fig:1-10``），而目录里的 ``\\numberline`` 是 LaTeX 按章自动编出的新号，
    两者本来就不是一套（本书 54 个标签、54 条目录，号却几乎全不对应）。

    数量不等时按出现顺序对位：多出来的是"没有目录条目的标签"，或是"没有标签的
    目录条目"。报告两边数量与对不上的那几名。

    某一侧传 ``None`` 表示那份清单**不存在**（引擎没跑过），此时跳过对账——把
    "没有清单"报成"清单里少了 N 条"是噪声；传空串则是一份真的空清单，照报。
    """
    body = tex_text.split(r"\begin{document}", 1)[-1]
    findings: List[Finding] = []
    for prefix, (kind, label) in CONTENTSLINE_KIND.items():
        list_text = lof if prefix == "fig:" else lot
        if list_text is None:
            continue
        labels = re.findall(r"\\label\{(" + re.escape(prefix) + r"[^}]*)\}", body)
        entries = parse_contentslines(list_text)
        if len(labels) == len(entries):
            continue
        if len(labels) > len(entries):
            offending = labels[len(entries) :]
            detail = "have no entry in the list: "
        else:
            offending = [plain_title(title) for title, _ in entries[len(labels) :]]
            detail = "have no caption label in the body: "
        shown = ", ".join(_shorten(item, 24) for item in offending[:10])
        if len(offending) > 10:
            shown += f", … ({len(offending)} in total)"
        findings.append(
            Finding(
                code="list-entry-mismatch",
                severity="warning",
                message=(
                    f"{len(labels)} {kind} label(s) in the body vs {len(entries)} entry(ies) in "
                    f"{label}; a caption was lost or duplicated"
                ),
                location=f"{detail}{shown}",
            )
        )
    return findings


# ---------------------------------------------------------------- 6. 插图分辨率

# tex.py 的默认排版框：宽 0.96\linewidth、高 0.68\textheight，等比缩到框内。
DEFAULT_WIDTH_FRACTION = 0.96
DEFAULT_HEIGHT_FRACTION = 0.68

WIDTH_FRACTION_RE = re.compile(r"width\s*=\s*([\d.]+)\s*\\[a-zA-Z]+width")
HEIGHT_FRACTION_RE = re.compile(r"height\s*=\s*([\d.]+)\s*\\[a-zA-Z]+height")
KEEP_ASPECT_RE = re.compile(r"keepaspectratio")


@dataclass(frozen=True)
class FigureRef:
    """一条 ``\\includegraphics``：相对图形根的路径与排版框。"""

    path: str
    width_fraction: float = DEFAULT_WIDTH_FRACTION
    height_fraction: float = DEFAULT_HEIGHT_FRACTION
    keep_aspect: bool = True


def parse_includegraphics(tex_text: str) -> List[FigureRef]:
    """抽出全部 ``\\includegraphics`` 及其排版框参数。

    排版框从选项里读（``width=0.96\\linewidth``、``height=0.68\\textheight``），
    读不到就用 :data:`DEFAULT_WIDTH_FRACTION`／:data:`DEFAULT_HEIGHT_FRACTION`
    ——与 :func:`primer.book.tex.render_blocks` 写出的那行保持一致。
    """
    figures: List[FigureRef] = []
    for options, path in INCLUDEGRAPHICS_RE.findall(tex_text):
        width = WIDTH_FRACTION_RE.search(options)
        height = HEIGHT_FRACTION_RE.search(options)
        figures.append(
            FigureRef(
                path=path.strip(),
                width_fraction=float(width.group(1)) if width else DEFAULT_WIDTH_FRACTION,
                height_fraction=float(height.group(1)) if height else DEFAULT_HEIGHT_FRACTION,
                keep_aspect=bool(KEEP_ASPECT_RE.search(options)),
            )
        )
    return figures


def printed_width_bp(
    pixel_width: int, pixel_height: int, box_width: float, box_height: float, keep_aspect: bool = True
) -> float:
    """等比缩到框内后，图实际印出来的宽度（bp）。

    ``keepaspectratio`` 且宽高都给定时 graphicx 取"能塞进框"的那个比例，于是
    有效宽 = ``min(框宽, 框高 × 宽高比)``；不给 ``keepaspectratio`` 时宽被拉满。
    """
    if not keep_aspect or pixel_height <= 0:
        return box_width
    return min(box_width, box_height * (pixel_width / pixel_height))


def figure_dpi(pixel_width: int, printed_width: float) -> float:
    """按"像素数 ÷ 印出来的英寸数"算有效分辨率。"""
    if printed_width <= 0:
        return 0.0
    return pixel_width * 72.0 / printed_width


def image_pixel_size(path: Path) -> Optional[Tuple[int, int]]:
    """只用标准库读 PNG／JPEG 的像素尺寸；别的格式返回 ``None``。

    PNG 读第一个 ``IHDR`` 数据块的宽高（固定在第 16—24 字节）；JPEG 扫到 ``SOF``
    标记（``0xC0``—``0xCF``，跳过 ``0xC4``／``0xC8``／``0xCC``）取高宽。不引入
    Pillow：只为拿两个整数不值得多一个依赖。
    """
    try:
        head = path.read_bytes()[: 1 << 16] if path.suffix.lower() == ".jpg" else path.read_bytes()
    except OSError:
        return None
    if head[:8] == b"\x89PNG\r\n\x1a\n" and len(head) >= 24 and head[12:16] == b"IHDR":
        return int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")
    if head[:2] == b"\xff\xd8":
        index = 2
        while index + 9 < len(head):
            if head[index] != 0xFF:
                index += 1
                continue
            marker = head[index + 1]
            if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                index += 2
                continue
            length = int.from_bytes(head[index + 2 : index + 4], "big")
            if marker in (
                0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
            ):
                height = int.from_bytes(head[index + 5 : index + 7], "big")
                width = int.from_bytes(head[index + 7 : index + 9], "big")
                return width, height
            if length <= 0:
                break
            index += 2 + length
    return None


def figure_resolution(
    tex_text: str, graphics_root: Path, geometry: Optional[Geometry]
) -> List[Finding]:
    """逐张插图核对文件存在性与有效分辨率。

    路径相对图形根（``\\graphicspath``，即 ``source_root`` 相对工程根）解析。
    有效分辨率 = 像素宽 ÷ 印出宽度（英寸），印出宽度按 :func:`printed_width_bp`
    考虑 ``keepaspectratio``；没有版心几何量时（缺 ``.log``）只查文件在不在。

    低于 :data:`LOW_RESOLUTION_DPI`（120dpi）报 ``info``，低于
    :data:`VERY_LOW_RESOLUTION_DPI`（72dpi）报 ``warning``；文件不存在报
    ``error``（``figure-file-missing``）；读不出像素尺寸的格式（PDF、EPS 等）报一条
    ``info``（``figure-size-unknown``），免得悄悄漏掉。
    每张图至多一条发现。
    """
    findings: List[Finding] = []
    for figure in parse_includegraphics(tex_text):
        path = graphics_root / figure.path
        if not path.is_file():
            findings.append(
                Finding(
                    code="figure-file-missing",
                    severity="error",
                    message=f"figure file {figure.path!r} does not exist under the graphics root",
                    location=str(path),
                )
            )
            continue
        if geometry is None:
            continue
        size = image_pixel_size(path)
        if size is None:
            findings.append(
                Finding(
                    code="figure-size-unknown",
                    severity="info",
                    message=f"cannot read the pixel size of {figure.path!r}; resolution not checked",
                    location=str(path),
                )
            )
            continue
        width, height = size
        box_width = figure.width_fraction * (geometry.right - geometry.left)
        box_height = figure.height_fraction * (geometry.bottom - geometry.top)
        printed = printed_width_bp(width, height, box_width, box_height, figure.keep_aspect)
        dpi = figure_dpi(width, printed)
        if dpi >= LOW_RESOLUTION_DPI:
            continue
        severity = "warning" if dpi < VERY_LOW_RESOLUTION_DPI else "info"
        findings.append(
            Finding(
                code="figure-low-resolution",
                severity=severity,
                message=(
                    f"figure prints at {dpi:.0f} dpi ({width}x{height} px across "
                    f"{printed:.0f} bp); below {LOW_RESOLUTION_DPI:.0f} dpi"
                ),
                location=figure.path,
            )
        )
    return findings


# ---------------------------------------------------------------- 汇总


def run_checks(manifest: BookManifest, plan: BuildPlan) -> List[Finding]:
    """``check --deep`` 的全套确定性质检。

    读盘只读**上一次成书的产物**（``plan`` 指出的 ``.tex``／``.toc``／``.lof``／
    ``.lot``／``.log``／``.pdf``）：与 ``check`` 读 ``.log`` 是同一个约定——先
    ``build`` 再 ``check``。poppler 的 ``pdftotext`` 缺席或调用失败时只落一条
    ``info`` 发现，页级检查跳过，不抛异常、不影响退出码。
    """
    findings: List[Finding] = []
    geometry = parse_geometry(_read_text(plan.log) or "")
    tex_text = _read_text(plan.tex)
    if tex_text is None:
        findings.append(
            Finding(
                code="tex-missing",
                severity="info",
                message="no assembled .tex found; run build first, text-level checks were skipped",
                location=_shown(plan.tex, plan),
            )
        )
    else:
        findings.extend(markdown_residue(tex_text))
        findings.extend(straight_quotes(tex_text))
        findings.extend(quote_direction(tex_text))
        findings.extend(
            toc_lists_itself(
                manifest.typography,
                _read_text(plan.toc) or "",
                _read_text(plan.lof) or "",
                _read_text(plan.lot) or "",
            )
        )
        findings.extend(list_entry_mismatch(tex_text, _read_text(plan.lof), _read_text(plan.lot)))
        graphics_root = plan.project_root / relative_to_root(manifest.source_root, plan.project_root)
        findings.extend(figure_resolution(tex_text, graphics_root, geometry))

    findings.extend(_page_checks(plan, geometry, body_em_bp(manifest.typography)))
    return findings


def _page_checks(plan: BuildPlan, geometry: Optional[Geometry], em_bp: float) -> List[Finding]:
    """页级检查（越界、页眉叠印、表格孤字行、近乎空白）；poppler 或几何量缺席时降级成一条 info。"""
    if not plan.pdf.is_file():
        return [
            Finding(
                code="pdf-missing",
                severity="info",
                message="no PDF found; page-level checks were skipped",
                location=_shown(plan.pdf, plan),
            )
        ]
    try:
        pages = extract_bbox(plan.pdf)
    except BboxUnavailable as exc:
        return [
            Finding(
                code="pdftotext-unavailable",
                severity="info",
                message=f"{exc}; page-level checks were skipped",
            )
        ]
    findings: List[Finding] = []
    if geometry is None:
        findings.append(
            Finding(
                code="geometry-unknown",
                severity="info",
                message="the .log carries no geometry report; the out-of-block check was skipped",
                location=_shown(plan.log, plan),
            )
        )
    else:
        findings.extend(text_out_of_block(pages, geometry, em_bp))
    findings.extend(running_head_collision(pages, geometry))
    findings.extend(table_orphan_lines(pages, geometry))
    findings.extend(page_near_blank(pages, geometry))
    return findings


# ---------------------------------------------------------------- 小工具


def _read_text(path: Path) -> Optional[str]:
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8", errors="ignore")


def _shown(path: Path, plan: BuildPlan) -> str:
    return relative_to_root(path, plan.project_root)


def _line_at(text: str, position: int) -> int:
    return text.count("\n", 0, position) + 1


def _excerpt(text: str, position: int, span: int = 44) -> str:
    start = max(0, position - span // 2)
    return _shorten(text[start : start + span].replace("\n", " ").strip(), span)


def _shorten(text: str, limit: int = 32) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"
