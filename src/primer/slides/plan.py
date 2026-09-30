# -*- coding: utf-8 -*-
"""控制模型：**每章的预算页数是唯一的控制参数**，总页数由它加出来。

没有"预期总页数"这个参数，也没有时长。``chapters[].budget`` 由人定，改一行就改一页；
``outline`` 只给出一个**默认建议**——把这一章的候选材料按一页 :data:`Capacity.per_page_chars`
字展示完需要几页，按候选字数占比分给各章，受 ``--min-per-chapter``／``--max-per-chapter``
约束。人改了 budget，那个数就是事实：``check`` 不再拿它跟任何"总数"对账，只报告每章的
预算来自人工还是默认、由此得到的总页数是多少。

**时间不在模型里。** 没有时长、没有语速、也没有"讲一页要几分钟"——页数与时长之间不存在
经过检验的换算率，多挂一个参数只会把假精度写进产物。场合与时长只作为记录写在
``outline.yaml`` 的注释里，由人自己填，工具不读。

固定页是**结构开销**：开场 2、目录 1、横向 3、讨论 3、备份 N。它们先被预留，剩下的才是
正文页（一帧一页，帧数 = 该章预算）。

分完之后推出的是**容量与取舍**（:class:`Capacity`），不是通过／没通过：显示容量是事实，
取舍比是人必须做的动作。工具不给"超了／没超"的裁决，因为那需要一条并不存在的换算率。

固定页还要落到**逐页**（:class:`PageEntry`）：那一页是开场、目录、横向、讨论还是备份，
纸面上写什么，要不要挑句子，配不配图，有没有备注。页数由模型定，内容由人填（目录与备份
的正文由工具算出，标 ``auto``）——生成器据此才知道第 3 页上该放什么。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from fractions import Fraction
from math import ceil
from statistics import median_low
from typing import List, Mapping, Optional, Sequence, Tuple

from .theme import Theme, default_capacity_per_page, default_theme, frame_metrics

DEFAULT_BACKUP = 6
DEFAULT_MIN_PER_CHAPTER = 1
DEFAULT_MAX_PER_CHAPTER = 5
# 一页幻灯片的文本容量（字）。**默认主题**的值由主题算出来，不再是按纸面估的数：
# 画布 960 × 540 bp（= 338.67 × 190.5 mm，PowerPoint 16:9），页边 5% → 版心 304.8 mm；
# 正文框 = 0.63 × 304.8 = 192.0 mm，22 pt 汉字宽 = 22/72 in = 7.7611 mm
#            → 每行 ⌊192.0 / 7.7611⌋ = 24 字；
# 标题带（实测，28/34 的帧标题）= 上下空 7.23 mm + 标题盒 0.7925 × 34 pt = 16.74 mm；
# 正文区 = 标题带下沿 → 「页脚墨迹顶 182.7 mm 再让 2 mm 净距」= 180.7 mm
#            两行标题：180.7 − 28.73 = 151.97 mm ÷ 10.5833 mm（30 pt 行距）= 14.36 → 14 行
#            一行标题：180.7 − 16.74 = 163.96 mm ÷ 10.5833 = 15.49 → 15 行
# 取保守的 24 × 14 = 336 字/页（两行标题的页）。旧值 176 是「22 字/行 × 8 行」.
#
# 这只是**默认主题**的值。真正写进骨架的容量在 outline 里按该骨架自己的 theme 块算
# （见 :func:`resolve_capacity`）——改主题（画布或正文字号）容量必须跟着改；显式
# --capacity-per-page 仍然覆盖主题算出的值。
DEFAULT_CAPACITY_PER_PAGE = default_capacity_per_page()
# 容量的两个来路，写进报告用（见 :func:`primer.slides.outline.report_lines`）。
CAPACITY_FROM_THEME = "theme"
CAPACITY_EXPLICIT = "explicit"
# 章预算的两个来路，写进报告用（见 :func:`primer.slides.validate._table_lines`）：人改过的
# 与本次算出的默认建议不同的，就是人工定的。
BUDGET_FROM_HUMAN = "human"
BUDGET_FROM_DEFAULT = "default"


def budget_origin(chapter: Mapping[str, object]) -> str:
    """这一章的预算来自人工还是默认建议：与 ``budget_default`` 不同就是人工定过的。"""
    budget = chapter.get("budget")
    default = chapter.get("budget_default")
    budget = budget if isinstance(budget, int) and not isinstance(budget, bool) else 0
    if isinstance(default, int) and not isinstance(default, bool) and budget != default:
        return BUDGET_FROM_HUMAN
    return BUDGET_FROM_DEFAULT
# 一页正文放几个要点：3–5 个，默认 4 个。这是排版惯例，不是从时长换算来的。
# 上限之外放不下（check 报 error），下限之内是刻意精简（check 报 warning）。
POINTS_PER_CONTENT_PAGE = 4
POINTS_PER_CONTENT_PAGE_RANGE = (3, 5)

# 结构开销：四种固定页的页数，是**常量**而不是选项。想换一套结构，直接改生成的
# ``outline.yaml``——那份产物本来就是给人改的，页数分配合不合意也由人自己调。
OPENING_PAGES = 2
# 目录一页：封面之后紧跟的那张"篇 / 章"表。它取代了旧版的结构图／阅读路径／章节索引三张
# 导航页——那三页画的是书页轴，而幻灯片不该再假装知道书的页码。
NAVIGATION_PAGES = 1
CROSS_CUTTING_PAGES = 3
DISCUSSION_PAGES = 3

# 固定页：种类、中文名、页数。
FIXED_PAGES = (
    ("opening", "开场", OPENING_PAGES),
    ("navigation", "目录", NAVIGATION_PAGES),
    ("cross_cutting", "横向", CROSS_CUTTING_PAGES),
    ("discussion", "讨论", DISCUSSION_PAGES),
)
BACKUP_LABEL = "备份"

# 固定页**逐页**的默认标签：纸面上的写法，人可改。次序即页序。
PAGE_LABELS = {
    "opening": ("封面", "主旨"),
    "navigation": ("目录",),
    "cross_cutting": ("横向议题 一", "横向议题 二", "横向议题 三"),
    "discussion": ("讨论 一", "讨论 二", "讨论 三"),
}
# 正文由工具算出、人不必填 picks 的固定页种类。
AUTO_KINDS = ("navigation", "backup")
# 还没圈候选时打在页面上的记号。它是**记号**不是句子：工具从不自己写正文，
# 空 picks 不是错误，而是一个明确标出的待办位置。
PLACEHOLDER = "〔待选句〕"

PAGE_KINDS = tuple(kind for kind, _, _ in FIXED_PAGES) + ("backup",)

# 一帧的人审三态。``pending`` 是生成时的默认，``ok`` 表示这一帧看过没问题，
# ``redo`` 表示这一帧要重来；``check`` 只认这三个值（``review-value``）。
REVIEW_STATES = ("pending", "ok", "redo")
DEFAULT_REVIEW = REVIEW_STATES[0]


def frame_entry(
    picks: Sequence[object] = (),
    review: object = DEFAULT_REVIEW,
    note: object = "",
) -> Mapping[str, object]:
    """一帧的骨架记录：这一帧要讲的句子、人审三态与定向提示。

    键序（``picks``／``review``／``note``）就是 YAML 里的落款次序，生成与合并两处共用，
    免得同一份骨架在不同路径下逐字节不同。
    """
    return {"picks": list(picks), "review": review, "note": note}


def frame_slots(total: int, frames: int, default: int = POINTS_PER_CONTENT_PAGE) -> Tuple[int, ...]:
    """把 ``total`` 条条目摊到 ``frames`` 帧上，**尽量均衡**（多出来的先给前面的帧）：

    6→3+3、7→4+3、8→4+4、9→5+4、10→5+5、11→4+4+3。均衡的一页看起来是"一页几句话"，
    而"前面几帧塞满、后面留空"会让最后一页空得莫名其妙。

    一条也没有（``total`` 为 0）时，每帧给 ``default`` 个待选位置：空 ``picks`` 不是
    错误，是要在版面上看出"这一页该放几句"。条目比帧数还少时（``total`` < ``frames``，
    ``check`` 会报 ``pick-count`` warning）排不上的帧记 0，发帧时同样补待选记号。

    **正文不再用它分帧**：一帧一页，各帧的 ``picks`` 由骨架直接给出（人写或 ``--merge``
    迁移）。还留着它的是两处：备份页把正片条目均衡摊到预留帧上（``build._backup_windows``），
    以及 ``outline --merge`` 把**旧骨架里章一级的平铺 picks** 摊进新骨架的各帧——旧骨架
    只有一串平铺的句子，没有逐帧结构，迁移时得照今天的分法还原成"哪几句同页"。
    """
    if frames <= 0:
        return ()
    if total <= 0:
        return (default,) * frames
    base, extra = divmod(total, frames)
    return tuple(base + 1 if index < extra else base for index in range(frames))


class SlidesError(Exception):
    """书的结构读不出来，或预算在给定约束下无解。"""


@dataclass(frozen=True)
class FixedPage:
    """一种固定页。"""

    kind: str
    label: str
    count: int

    def as_dict(self) -> Mapping[str, object]:
        return {"kind": self.kind, "label": self.label, "count": self.count}


def fixed_pages(backup: int) -> Tuple[FixedPage, ...]:
    """固定页清单：四种固定页加备份页。"""
    pages = [FixedPage(kind, label, count) for kind, label, count in FIXED_PAGES]
    pages.append(FixedPage("backup", BACKUP_LABEL, backup))
    return tuple(pages)


@dataclass(frozen=True)
class PageEntry:
    """固定页清单里的**一页**。

    ``pages`` 早先只记各有多少页，生成器因此不知道那一页上放什么。这里把它摊成一条条
    页记录：``kind`` 是种类（opening／navigation／cross_cutting／discussion／backup），
    ``label`` 是纸面上的写法，``auto`` 为真表示这一页的正文由工具算出（目录的篇／章表、
    备份的出处索引），人不必填 ``picks``。

    ``picks`` 是这一页要用的句子，与章一级同一条 ``pick-missing`` 判据：形如 ``s8-6p02``
    的候选 id 指着 ``candidates.md`` 的一行，作者手写的句子（或 ``{text, derived_from}``
    提炼句）照原样渲染。这里**原样保存** YAML 里的取值（可能是字符串，也可能是映射），
    形状的判断交给 :func:`primer.slides.candidates.parse_pick`——一旦在这里 ``str()``，
    映射形状就丢了。``figure`` 是插图编号（如 ``"2-1"``）或 ``None``；``notes`` 进
    ``\\note{}``，空字符串不发射任何东西。非 ``auto`` 的页三样都由人填，``outline`` 只写出
    空壳。
    """

    kind: str
    label: str
    auto: bool = False
    picks: Tuple[object, ...] = ()
    figure: Optional[str] = None
    notes: str = ""

    def as_dict(self) -> Mapping[str, object]:
        payload: dict = {"kind": self.kind, "label": self.label}
        if self.auto:
            payload["auto"] = True
        payload["picks"] = list(self.picks)
        payload["figure"] = self.figure
        payload["notes"] = self.notes
        return payload


def page_entries(backup: int) -> Tuple[PageEntry, ...]:
    """固定页的完整清单：2 开场 + 1 目录 + 3 横向 + 3 讨论 + ``backup`` 备份。"""
    entries: List[PageEntry] = []
    for kind, _, count in FIXED_PAGES:
        labels = PAGE_LABELS[kind]
        for index in range(count):
            entries.append(
                PageEntry(kind=kind, label=labels[index], auto=kind in AUTO_KINDS)
            )
    for index in range(max(backup, 0)):
        entries.append(
            PageEntry(kind="backup", label=f"{BACKUP_LABEL} {index + 1}", auto=True)
        )
    return tuple(entries)


def read_page_entries(outline: Mapping[str, object]) -> Tuple[PageEntry, ...]:
    """从骨架的 ``pages`` 读出逐页清单。

    兼容旧写法：一条 ``{kind, label, count}`` 只记有多少页的，按 ``count`` 展开成若干条
    默认标签的页记录。两种写法都读得出来，是因为人手上那份骨架可能还是旧格式，而
    ``pages`` 记的是结构开销的**分配**，读不出来就等于整个 deck 没有页清单。
    """
    entries: List[PageEntry] = []
    for raw in outline.get("pages") or []:
        if not isinstance(raw, Mapping):
            continue
        kind = str(raw.get("kind") or "")
        count = raw.get("count")
        if isinstance(count, int) and not isinstance(count, bool):
            labels = PAGE_LABELS.get(kind)
            for index in range(max(count, 0)):
                label = (
                    labels[index]
                    if labels and index < len(labels)
                    else f"{raw.get('label') or kind} {index + 1}"
                )
                entries.append(PageEntry(kind=kind, label=label, auto=kind in AUTO_KINDS))
            continue
        figure = raw.get("figure")
        entries.append(
            PageEntry(
                kind=kind,
                label=str(raw.get("label") or kind),
                auto=bool(raw.get("auto")) or kind in AUTO_KINDS,
                picks=tuple(raw.get("picks") or []),
                figure=str(figure) if isinstance(figure, str) else None,
                notes=str(raw.get("notes") or ""),
            )
        )
    return tuple(entries)


@dataclass(frozen=True)
class DeckConfig:
    """一次演示的控制参数。

    ``backup`` 是备份页数；``min_per_chapter``／``max_per_chapter`` 约束**默认建议**的单章
    页数（人写进 ``chapters[].budget`` 的值不受它们约束——那是人的决定）；
    ``capacity_per_page`` 是一页能显示的字数（排版事实，不是换算）。

    没有总页数参数：总页数 = 各章预算之和 + 固定页，由 ``chapters[].budget`` 加出来，
    只在报告里报出。

    ``capacity_per_page`` **默认 None**：表示"按这份骨架自己的主题算"，
    :func:`resolve_capacity` 在 ``outline`` 里把它落成具体数字（新骨架用默认主题的值，
    ``--merge`` 用旧骨架 theme 块的值）。显式给一个正整数就覆盖主题算出的值。
    """

    backup: int = DEFAULT_BACKUP
    min_per_chapter: int = DEFAULT_MIN_PER_CHAPTER
    max_per_chapter: int = DEFAULT_MAX_PER_CHAPTER
    capacity_per_page: Optional[int] = None

    def __post_init__(self) -> None:
        if self.backup < 0:
            raise SlidesError("--backup must not be negative")
        if self.capacity_per_page is not None and self.capacity_per_page <= 0:
            raise SlidesError("--capacity-per-page must be positive")
        if self.min_per_chapter < 0:
            raise SlidesError("--min-per-chapter must not be negative")
        if self.max_per_chapter < self.min_per_chapter:
            raise SlidesError("--max-per-chapter must be >= --min-per-chapter")

    @property
    def fixed_pages_non_backup(self) -> Tuple[FixedPage, ...]:
        return fixed_pages(0)[:-1]

    @property
    def pages(self) -> Tuple[FixedPage, ...]:
        return fixed_pages(self.backup)

    @property
    def overhead(self) -> int:
        """固定页总数（含备份页）。"""
        return sum(page.count for page in self.pages)


def default_budgets(
    materials: Sequence[int],
    capacity_per_page: int,
    min_per_chapter: int = DEFAULT_MIN_PER_CHAPTER,
    max_per_chapter: int = DEFAULT_MAX_PER_CHAPTER,
) -> Tuple[Tuple[int, ...], int]:
    """默认预算建议：返回（逐章页数，正文页合计）。

    材料量 ``materials`` 是各章的候选字数（要讲的料有多厚）。全书正文页的默认值 = "把全部
    候选材料按一页 ``capacity_per_page`` 字展示完"需要多少页，向上取整；再按各章材料的占比
    分下去（:func:`allocate`），受 min/max 约束。于是默认值只由材料与容量推出，**不含自由
    参数**；总数落在 [章数×min, 章数×max] 之内。

    这只是**建议**：人改了 ``chapters[].budget``，那个数就是事实，``check`` 只报告每章究竟是
    人工填的还是照默认值来的。
    """
    count = len(materials)
    if count == 0:
        raise SlidesError("no chapters to suggest a budget for")
    low = count * min_per_chapter
    high = count * max_per_chapter
    total = sum(materials)
    if total <= 0 or capacity_per_page <= 0:
        body = low
    else:
        body = ceil(Fraction(total, capacity_per_page))
    body = min(max(body, low), high)
    return allocate(materials, body, min_per_chapter, max_per_chapter), body


def allocate(
    materials: Sequence[int],
    body: int,
    min_per_chapter: int = DEFAULT_MIN_PER_CHAPTER,
    max_per_chapter: int = DEFAULT_MAX_PER_CHAPTER,
) -> Tuple[int, ...]:
    """按材料占比分配正文页，floor 到 min/max 之间后用"亏空最大者优先"补齐。

    占比用 :class:`~fractions.Fraction` 精确算，避免浮点边界上两份实现给出两种结果；
    补齐的次序是"配额减已分配"从大到小（最被亏待的章先补），同分按章的先后。
    结果之和恒等于 ``body``。
    """
    count = len(materials)
    if count == 0:
        raise SlidesError("no chapters to allocate the body budget over")
    if body < count * min_per_chapter:
        raise SlidesError(
            f"body budget {body} cannot cover {count} chapters at min-per-chapter {min_per_chapter}"
        )
    if body > count * max_per_chapter:
        raise SlidesError(
            f"body budget {body} exceeds {count} chapters at max-per-chapter {max_per_chapter}"
        )
    total = sum(materials)
    if total <= 0:
        raise SlidesError("chapter material must sum to a positive number")

    quota = [Fraction(body * amount, total) for amount in materials]
    allocated = [min(max(int(value), min_per_chapter), max_per_chapter) for value in quota]
    deficit = body - sum(allocated)
    while deficit > 0:
        room = [index for index, value in enumerate(allocated) if value < max_per_chapter]
        if not room:
            raise SlidesError("body budget cannot be distributed within max-per-chapter")
        pick = max(room, key=lambda index: (quota[index] - allocated[index], -index))
        allocated[pick] += 1
        deficit -= 1
    while deficit < 0:
        room = [index for index, value in enumerate(allocated) if value > min_per_chapter]
        if not room:
            raise SlidesError("body budget cannot be distributed within min-per-chapter")
        pick = min(room, key=lambda index: (quota[index] - allocated[index], index))
        allocated[pick] -= 1
        deficit += 1
    return tuple(allocated)


@dataclass(frozen=True)
class Capacity:
    """容量与取舍：全部由页数与候选长度推出，没有通过／不通过。

    三件事，都是陈述：

    * **显示容量**：``per_page_chars × content_pages``——这份骨架能显示的字数上限，是事实。
    * **取舍比**：正文页每页按 ``points_per_content_page`` 个要点计，得到需要的要点数；
      候选表里有 ``candidates`` 条，相除就是人必须做的取舍。每页 3–5 个要点是一个**范围**，
      所以要点数与取舍比都记成范围（``points_needed_range``），默认值单列一份。
      候选多不是错误，正是要人来挑。
    * **每页条数**：由候选长度的中位／最小／最大与一页字数直接换算，只作描述。

    没有阈值，也就没有"超了"这回事：判定超没超需要一条页数→时长的换算率，而那条换算率
    并不存在（见 :mod:`primer.slides.plan` 的模块文档）。
    """

    per_page_chars: int
    content_pages: int
    points_per_content_page: int
    points_per_content_page_range: Tuple[int, int]
    candidates: int
    median_chars: int
    min_chars: int
    max_chars: int

    @property
    def display_chars(self) -> int:
        """可显示容量 = 一页字数 × 内容页数。"""
        return self.per_page_chars * self.content_pages

    @property
    def points_needed(self) -> int:
        """这份骨架需要的要点数（按默认的每页 4 个）= 内容页数 × 每页要点数。"""
        return self.content_pages * self.points_per_content_page

    @property
    def points_needed_range(self) -> Tuple[int, int]:
        """每页 3–5 个要点时的要点数范围。"""
        low, high = self.points_per_content_page_range
        return (self.content_pages * low, self.content_pages * high)

    @property
    def candidates_per_point(self) -> Fraction:
        """取舍比（按默认的每页 4 个）= 候选条数 ÷ 需要的要点数。无可比时记 0。"""
        if self.points_needed <= 0 or self.candidates <= 0:
            return Fraction(0)
        return Fraction(self.candidates, self.points_needed)

    @property
    def candidates_per_point_range(self) -> Tuple[Fraction, Fraction]:
        """取舍比的范围：每页要点少 → 每条候选分到的篇幅多 → 比值小。"""
        low, high = self.points_needed_range
        if high <= 0 or low <= 0 or self.candidates <= 0:
            return (Fraction(0), Fraction(0))
        return (Fraction(self.candidates, high), Fraction(self.candidates, low))

    @property
    def discard_share(self) -> Fraction:
        """必须丢掉的比例（按默认的每页 4 个）= 1 − 要点数 ÷ 候选数；候选不够时记 0。"""
        if self.candidates <= 0 or self.points_needed <= 0:
            return Fraction(0)
        return max(Fraction(0), 1 - Fraction(self.points_needed, self.candidates))

    def points_per_page(self, chars: int) -> int:
        """换算：一页 ``per_page_chars`` 字放得下几条 ``chars`` 字的要点。"""
        if chars <= 0:
            return 0
        return self.per_page_chars // chars

    def as_dict(self) -> Mapping[str, object]:
        low, high = self.points_per_content_page_range
        ratio_low, ratio_high = self.candidates_per_point_range
        return {
            "per_page_chars": self.per_page_chars,
            "content_pages": self.content_pages,
            "display_chars": self.display_chars,
            "points_per_content_page": self.points_per_content_page,
            "points_per_content_page_min": low,
            "points_per_content_page_max": high,
            "points_needed": self.points_needed,
            "points_needed_min": self.points_needed_range[0],
            "points_needed_max": self.points_needed_range[1],
            "candidates": self.candidates,
            "candidates_per_point": round(float(self.candidates_per_point), 2),
            "candidates_per_point_min": round(float(ratio_low), 2),
            "candidates_per_point_max": round(float(ratio_high), 2),
            "discard_share": round(float(self.discard_share), 4),
            "median_candidate_chars": self.median_chars,
            "min_candidate_chars": self.min_chars,
            "max_candidate_chars": self.max_chars,
            "points_per_page_at_median": self.points_per_page(self.median_chars),
            "points_per_page_at_max": self.points_per_page(self.max_chars),
        }


def resolve_capacity(config: DeckConfig, theme: Theme) -> Tuple[DeckConfig, str]:
    """把 ``capacity_per_page`` 落成一个数，并说明它的来路。

    显式给了就原样用它（记 :data:`CAPACITY_EXPLICIT`）；没给（``None``）就按**这份骨架
    自己的 theme 块**量出来（记 :data:`CAPACITY_FROM_THEME`）。容量是排版事实，而排版
    事实由画布与字号定——改主题必须让容量跟着改，写死的默认值做不到这一点。

    返回的是**换了容量字段的配置**，所以下游（``build_outline_document``、报告）拿到的
    是同一个解析后的值，不必各自再读一次主题。
    """
    if config.capacity_per_page is not None:
        return config, CAPACITY_EXPLICIT
    return (
        replace(config, capacity_per_page=frame_metrics(theme).capacity_chars),
        CAPACITY_FROM_THEME,
    )


def capacity_report(
    config: DeckConfig,
    content_pages: int,
    lengths: Sequence[int],
    theme: Optional[Theme] = None,
) -> Capacity:
    """由正文页数（各章预算之和）与候选长度推出容量与取舍。

    中位数取**下中位**（:func:`statistics.median_low`）：候选条数是偶数时也不引入 .5，
    产物里因此全是整数，重生成逐字节相同。每页要点数与一页字数一样，是可以被主题
    （画布、正文字号）改掉的排版事实，所以按范围记。``config.capacity_per_page`` 允许是
    ``None``（"按主题算"），这里用 ``theme``（缺省是默认主题）把它落成具体数字。
    """
    config, _ = resolve_capacity(config, theme or default_theme())
    ordered: List[int] = sorted(lengths)
    return Capacity(
        per_page_chars=config.capacity_per_page,
        content_pages=content_pages,
        points_per_content_page=POINTS_PER_CONTENT_PAGE,
        points_per_content_page_range=POINTS_PER_CONTENT_PAGE_RANGE,
        candidates=len(ordered),
        median_chars=median_low(ordered) if ordered else 0,
        min_chars=ordered[0] if ordered else 0,
        max_chars=ordered[-1] if ordered else 0,
    )
