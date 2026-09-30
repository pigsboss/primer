# -*- coding: utf-8 -*-
"""``build``：把校验过的骨架编成一套 beamer 幻灯片。

一条链，环环相扣：

1. **读骨架与规格**：``outline.yaml`` 加上它旁边的 ``candidates.md``（圈选的句子在那张表里，
   与骨架只有 ``id`` 相连），再加旁边的 ``deck.yaml`` 与它列的源 markdown（书目、篇名、
   插图由它们提供）。
2. **过闸**：调 :func:`primer.slides.validate.validate_outline`（与 ``check`` 同一份判据）。
   报出 error 就**什么都不写**、非零码退出；warning 照常生成。
3. **排帧**：固定页照骨架的逐页清单排（开场 2、目录 1、横向 3、讨论 3、备份 N），正文
   **一帧一页**照骨架的 ``chapters[].frames`` 排——每帧的要点就是它自己的 ``picks``，不再
   在这里切分；不足的位置打 :data:`primer.slides.plan.PLACEHOLDER`，空 ``picks`` 不是错误，
   是待圈的位置。备份帧**恒等于**骨架预留的页数：那里放的是正片所有要点的**出处查找表**
   （一条一行），装不下只收摘要长度，不缩字号、不添帧——见 :func:`fit_backup`。
4. **发射**：交给 :mod:`primer.slides.beamer` 变成 ``slides.tex``，旁边写一份 ``Makefile``
   （与成书同一套 xelatex + xdvipdfmx 的编译计划）。
5. **编译与复核**：跑 xelatex 与 xdvipdfmx，再拿成书同一个日志解析器
   (:func:`primer.book.logcheck.parse_log`) 读 ``slides.log``：缺字与日志里的 ``!`` 排版错误
   是**硬失败**（投影上少一个字形、或少一道短横都是错的），overfull／underfull 报警告并点出
   页码；随后核对 PDF 页数是否等于排出的帧数、帧号是否自洽。

**工具不写正文。** 页面上的句子来自 ``candidates.md`` 的候选句，或骨架里作者手写的自由
文本（含机器提炼句）——两类都经成书同一个行内渲染器 :func:`primer.book.tex.render_inline`；
工具自己写的只有页码、指示、目录表这些算术，它从不自己造句子。**页面上没有书的页码**：
帧脚与备份表的出处写的是节号（``§7.4``），目录页也不列页码——幻灯片不假装知道纸面页数。
"""

from __future__ import annotations

import math
import re
import subprocess
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple, TypeVar

from ..book import logcheck
from ..book import pdf as pdf_tools
from ..book import tables
from ..book import tex
from ..book.makefile import BuildPlan
from ..paths import relative_to_root
from . import candidates as cand
from .beamer import (
    DECK_JOBNAME,
    DeckContext,
    Frame,
    LOOKUP_CHIP_MM,
    LookupCell,
    TABLE_GAP_PT,
    lookup_columns,
    lookup_units,
    render_document,
)
from .fingerprint import current_fingerprint, fingerprint_differences
from .outline import OUTLINE_NAME, resolve_spec
from .prose import normalize_pick_text, strip_lead_enumerator
from .plan import (
    PLACEHOLDER,
    POINTS_PER_CONTENT_PAGE,
    PageEntry,
    SlidesError,
    frame_slots,
    read_page_entries,
)
from .spec import DeckSpec
from .structure import DeckStructure, FigureRef
from .theme import MM_PER_PT, Theme, read_theme
from .validate import (
    CANDIDATES_NAME,
    Finding,
    load_outline_document,
    project_root_hint,
    severity_lines,
    validate_outline,
)

MAKEFILE_NAME = "Makefile"
# 编译遍数：帧号与 \inserttotalframenumber 要两遍才收敛，第三遍保险。
ENGINE_RUNS = 3
# 排版器在产物目录里留下的中间文件（复核后列进"写出"清单）。
LATEX_SUFFIXES = ("aux", "log", "nav", "out", "snm", "toc", "vrb", "xdv", "pdf")
# 页脚里的帧号：`12/43`。
FRAME_NUMBER_RE = re.compile(r"(?<!\d)(\d{1,4})\s*/\s*(\d{1,4})(?!\d)")
# 备份页的摘要阶梯（字）：先 16 字，装不下就一档一档收。收到最短一档仍装不下就是预留
# （--backup）太小——那是配置问题，由 deck-slides 报出来，不靠缩字号或添帧圆场。
BACKUP_GIST_LADDER = (16, 12, 10, 8)
# 摘要里要摘掉的标记：强调 ``**`` 与引用 ``[n]``（范围算一个）。摘要只回答"这条要点
# 讲什么"，引用编号在正片页脚上已经有了。
GIST_MARK_RE = re.compile(r"\*\*|\[\d+(?:\s*[–—-]\s*\d+)*\]")
# 目录表每列在自然宽度之外留的净空（显示单位，1 单位 ≈ 0.5 em）。
INDEX_COLUMN_PAD_UNITS = 2.0


class BuildGateError(SlidesError):
    """编译出来了，但没通过复核（缺字、没出 PDF）。"""


T = TypeVar("T")


@dataclass(frozen=True)
class DeckPlan:
    """一次 ``build`` 排出来的全部帧。"""

    frames: Tuple[Frame, ...]
    context: DeckContext
    counts: Mapping[str, int]

    @property
    def total(self) -> int:
        return len(self.frames)


@dataclass(frozen=True)
class BuildResult:
    """一次 ``build`` 的结果：写出的文件、日志发现、页数与帧号。"""

    tex: Path
    makefile: Path
    pdf: Optional[Path]
    log_findings: Tuple[Finding, ...]
    pages: Optional[int]
    frame_numbers: Tuple[int, ...]
    frames: int
    files: Tuple[Path, ...]


@dataclass(frozen=True)
class BackupEntry:
    """正片用到的一条要点：备份查找表的一行（摘要还没截）。

    ``sentence`` 是候选表里的**原句**（带 markdown 的 ``**`` 与 ``[n]``），截摘要要在
    行内渲染之前做，所以这里存原句而不是渲染结果。
    """

    volume: str
    label: str
    pointer: str
    sentence: str


@dataclass(frozen=True)
class BackupFit:
    """备份查找表装不装得下：一档档试出来的摘要长度与行数。

    ``lines_needed`` 是**最满一帧**的行数——条目按 :func:`frame_slots` 均衡摊到各帧后取最大
    的那个。**一条一行**：条目排成一段，行高就是字号下限的行距，排不下的只是摘要太宽，
    所以还有第二个条件 ``gist_units_needed <= gist_units``（最宽的那条摘要放得进摘要列）。
    两个条件都成立才算 ``fits``；不成立由 ``deck-slides`` 报出，不靠缩字号或添帧补救。
    """

    frames: int
    entries: int
    lines_per_frame: int
    gist_units: int
    gist_chars: int
    lines_needed: int
    gist_units_needed: int

    @property
    def capacity_lines(self) -> int:
        """预留的备份帧一共能排几行。"""
        return self.frames * self.lines_per_frame

    @property
    def width_fits(self) -> bool:
        """最宽的那条摘要放得进摘要列——放不进就会压出版心。"""
        return self.gist_units_needed <= self.gist_units

    @property
    def fits(self) -> bool:
        return self.width_fits and self.lines_needed <= self.capacity_lines

    @property
    def required_frames(self) -> int:
        """真要装下这些条目，--backup 至少得给几帧。"""
        if self.lines_per_frame <= 0:
            return 0
        return -(-self.lines_needed // self.lines_per_frame)


# ---------------------------------------------------------------- 候选与指针


def read_candidate_rows(path: Path) -> Mapping[str, cand.CandidateRow]:
    """读回 ``candidates.md``：``id -> 行``（句子与页面指针都在里面）。"""
    if not path.is_file():
        raise SlidesError(f"candidate table not found: {path} (run outline first)")
    return cand.parse_markdown_table(path.read_text(encoding="utf-8"))


def pointer_text(pointer: str) -> str:
    """候选表里的指针 → 帧脚上那一行（``详见 §8.6``）。

    指针本身就是节号（``§8.6``）或章标签（``第一章``）——**没有页码**，所以这里只加一句
    "详见"，重排都免了。空指针给空串（``deck.yaml`` 的 ``pointers: none``）。
    """
    return f"详见 {pointer.strip()}" if pointer.strip() else ""


def chapter_pointer(chapter: Mapping[str, object]) -> str:
    """一章的默认指针：第一个讲得上的节的节号，没有节就用这一章的章标签。"""
    for section in chapter.get("sections") or []:
        if not isinstance(section, Mapping) or not section.get("speak", True):
            continue
        number = section.get("number")
        if isinstance(number, str) and number:
            return f"详见 §{number}"
    label = chapter.get("label") or chapter.get("chapter") or ""
    return f"详见 {label}" if label else ""


# ---------------------------------------------------------------- 插图


def figure_index(structure: DeckStructure) -> Dict[str, FigureRef]:
    """全 Deck 的插图：``编号 -> 插图``，编号是 ``章号-章内序号``（``8-1``、``A-3``）。

    来源只有一处——各章的源 markdown（``![…](…)`` 图片行 + 紧随的题注）。**不读成书的
    插图目录**：成书按章重编，这里也按章重编，同一个语义不必假手他人，源文件改了也不会
    与书对不上。同一编号先来者占位。
    """
    figures: Dict[str, FigureRef] = {}
    for chapter in structure.chapters:
        for figure in chapter.figures():
            figures.setdefault(figure.number, figure)
    return figures


def figure_label(figure: FigureRef) -> str:
    """帧内插图的题注行：``图 8-1　行星际运输的比冲阶梯``（短题取自源题注）。"""
    return f"图 {figure.number}　{figure.caption}".rstrip("　")


# ---------------------------------------------------------------- 备份查找表


def gist_text(sentence: str, chars: int) -> str:
    """一句话的摘要：摘掉强调与引用标记，截到 ``chars`` 字（截短时末位是省略号）。

    摘要只回答"这条要点讲的是什么"，好让人拿着它回书里翻到那一节；**完整的句子在每位
    听众的讲义里都有**，不必在这一页再抄一遍。长度按 ``chars`` 计，截短时省略号占一格。

    行首的原文编号（``（一）``、``1.`` 这类）与要点同一条规矩：这一行的出处已经写在
    指针列里，编号是形式，由这里剥掉——先剥再量宽度，摘要的 em 数与印出来的字一致。
    """
    plain = strip_lead_enumerator(GIST_MARK_RE.sub("", sentence).strip())
    if chars <= 0 or len(plain) <= chars:
        return plain
    return plain[: chars - 1].rstrip() + "…"


def _gist_units(sentence: str, chars: int) -> float:
    """一条摘要的宽度（em）：宽度按 em 的上界算（见 :func:`primer.slides.beamer.lookup_units`）。"""
    return lookup_units(gist_text(sentence, chars))


def _backup_windows(items: Sequence[T], frames: int) -> List[Sequence[T]]:
    """把条目按均衡分法摊到各帧上（多出来的先给前面的帧），保持次序。

    与正文用同一条 :func:`frame_slots`：一帧几句话是均衡的，"前面塞满、后面留空"会让人
    以为后面几页坏了。条目按正片次序进来，章与篇因此天然成组（见 :func:`compose_frames`）。
    """
    if frames <= 0:
        return []
    counts = frame_slots(len(items), frames) if items else (0,) * frames
    windows: List[Sequence[T]] = []
    offset = 0
    for count in counts:
        windows.append(items[offset : offset + count])
        offset += count
    return windows


def fit_backup(
    sentences: Sequence[str], frames: int, lines_per_frame: int, gist_units: int
) -> BackupFit:
    """挑一档摘要长度，把查找表装进预留的备份帧：16 → 12 → 10 → 8 字。

    条目按 :func:`_backup_windows` 均衡摊到 ``frames`` 帧上，**一条一行**（行高就是字号
    下限的行距）。某一档同时满足两件事就取它：

    1. 最宽的那条摘要放得进摘要列（放不进就压出版心，没有第二个动作可以救）；
    2. 最满一帧的行数不超过一帧的可用行数，即条目装得进预留的帧数。

    最短一档仍不能满足就返回 ``fits=False``——装不下的是条目数（预留太小）或摘要列
    （版心太紧），两种都由 :func:`_backup_reserve_message` 如实报出。

    页数是唯一的控制参数，所以内容必须装进预留：这里只有"收摘要"一个动作，字号已经在
    主题的下限上，帧数是骨架给的，两样都不动。
    """
    capacity = frames * lines_per_frame
    needed = max((len(window) for window in _backup_windows(sentences, frames)), default=0)
    chosen = BACKUP_GIST_LADDER[-1]
    widest = 0.0
    for chars in BACKUP_GIST_LADDER:
        chosen = chars
        widest = max((_gist_units(sentence, chars) for sentence in sentences), default=0.0)
        if widest <= gist_units and needed <= capacity:
            break
    return BackupFit(
        frames=frames,
        entries=len(sentences),
        lines_per_frame=lines_per_frame,
        gist_units=gist_units,
        gist_chars=chosen,
        lines_needed=needed,
        gist_units_needed=math.ceil(widest),
    )


def _backup_reserve_message(fit: BackupFit) -> str:
    """装不下时的那一句：帧数、行数、条目数、摘要长度，一个数字都不藏。

    两种装不下各自说各自的数：条目多过预留帧数（把 ``--backup`` 加大或减少 picks），
    或摘要列连最短的摘要都放不下（那是版心与字号下限的事，加帧也救不了）。
    """
    if not fit.width_fits:
        return (
            f"备份页的摘要列一行只有 {fit.gist_units} 个 em，连最短的 "
            f"{BACKUP_GIST_LADDER[-1]} 字摘要也要 {fit.gist_units_needed} 个 em——"
            "这份主题的版心排不下这张查找表（画布更宽或字号下限更小才放得下）"
        )
    return (
        f"备份页预留 {fit.frames} 帧、每帧 {fit.lines_per_frame} 行"
        f"（共 {fit.capacity_lines} 行），装不下正片用到的 {fit.entries} 条要点："
        f"最满一帧要 {fit.lines_needed} 行。"
        f"把 --backup 加到 {fit.required_frames} 帧以上，或减少 picks"
    )


def _lookup_entries(
    picks: Sequence[object],
    rows: Mapping[str, cand.CandidateRow],
    volume: str,
    label: str,
) -> List[BackupEntry]:
    """一串 pick → 查找表条目。

    正片讲到的句子在这里合成出处表：候选 id 取候选行的指针与句子；自由文本没有书的出处，
    指针留空；提炼句取它第一个出处的指针（``derived_from`` 里第一个还在表里的）。形状
    读不出来或候选 id 不在表里的跳过——那是过闸时已经报过的 ``pick-shape``／``pick-missing``。
    """
    found: List[BackupEntry] = []
    for value in picks:
        try:
            pick = cand.parse_pick(value, rows)
        except cand.PickError:
            continue
        if pick.is_candidate:
            row = rows.get(pick.identifier)
            if row is None:
                continue
            pointer = row.pointer
        else:
            pointer = _text_pick_pointer(pick, rows)
        # 句子取 :func:`primer.slides.candidates.pick_display`——三种形状到显示文本只有那
        # 一处，引号也已在那一处规整；备份页的摘要因此与正片用同一份文本。
        found.append(
            BackupEntry(
                volume=volume,
                label=label,
                pointer=pointer_text(pointer),
                sentence=cand.pick_display(pick, rows),
            )
        )
    return found


def _text_pick_pointer(pick: cand.Pick, rows: Mapping[str, cand.CandidateRow]) -> str:
    """一句话的出处指针：提炼句取第一个还在表里的 ``derived_from``，自由文本为空。"""
    for identifier in pick.derived_from:
        row = rows.get(identifier)
        if row is not None:
            return row.pointer
    return ""


def _entry_label(chapter: Mapping[str, object]) -> str:
    return str(chapter.get("label") or chapter.get("chapter") or "")


# ---------------------------------------------------------------- 行内口径


@dataclass
class _DroppedLinks:
    """幻灯片丢弃的链接目标账本。

    幻灯片上 ``[标签](url)`` 只留标签文字——投影的版面上，一行 URL 脚注既多余又吃掉版面
    （脚注是**成书**的规矩，见 :func:`primer.book.tex.render_inline`）。丢弃的目标在这里逐条
    攒下来，``where`` 是当前的渲染位置（章／帧或固定页名）。攒下的东西有两个去向：这一帧的
    讲者备注 ``\\note{}``（只在讲义里出现），以及一条 ``links-dropped`` 发现——**不静默丢信息**。
    """

    where: str = ""
    dropped: List[Tuple[str, str, str]] = field(default_factory=list)

    def take(self, label: str, url: str) -> None:
        self.dropped.append((self.where, label, url))

    def for_place(self, where: str) -> List[Tuple[str, str]]:
        """某个位置丢弃的链接目标：``[(标签, URL)]``，按丢弃次序。"""
        return [
            (label, url)
            for place, label, url in self.dropped
            if place == where
        ]


def _inline(text: str, links: _DroppedLinks) -> str:
    """幻灯片上的行内渲染：链接只留标签，目标记进 ``links`` 账本。"""
    taken: List[Tuple[str, str]] = []
    rendered = tex.render_inline(text, links=tex.LINK_LABEL, dropped_links=taken)
    for label, url in taken:
        links.take(label, url)
    return rendered


def _link_note(notes: str, targets: Sequence[Tuple[str, str]]) -> str:
    """把丢弃的链接目标并进这一帧的讲者备注（``\\note{}``，只出现在讲义里）。

    URL 走 :func:`primer.book.tex.render_url`：URL 里 ``%`` ``_`` ``&`` 都是 LaTeX 特殊字符，
    裸写会把这一帧编坏。
    """
    if not targets:
        return notes
    line = "链接目标（幻灯片上不印）：" + "、".join(
        tex.render_url(url) for _, url in targets
    )
    return f"{notes}\n{line}" if notes else line


def _link_findings(links: _DroppedLinks) -> List[Finding]:
    """丢弃的链接目标按位置汇总成一条 ``links-dropped``（提示级，不挡生成）。"""
    places: List[str] = []
    grouped: Dict[str, List[Tuple[str, str]]] = {}
    for where, label, url in links.dropped:
        if where not in grouped:
            grouped[where] = []
            places.append(where)
        grouped[where].append((label, url))
    return [
        Finding(
            "links-dropped",
            "info",
            f"{where} 丢弃了链接目标（幻灯片上链接只留标签文字，目标已写进这一帧的"
            f"讲者备注）："
            + "、".join(f"{label}→{url}" for label, url in grouped[where]),
            where,
        )
        for where in places
    ]


# ---------------------------------------------------------------- 排帧


def graphics_path(spec: DeckSpec) -> str:
    """``\\graphicspath`` 里的那一条：源根相对工程根、末尾带斜杠。

    编译的工作目录就是工程根（见 :func:`build_plan`），图片路径写在源 markdown 里、相对
    ``source_root``，所以图形搜索路径取源根一条即可。
    """
    root = spec.source_root.strip()
    if root in ("", "."):
        return "./"
    return root.rstrip("/") + "/"


def compose_frames(
    outline: Mapping[str, object],
    spec: DeckSpec,
    structure: DeckStructure,
    rows: Mapping[str, cand.CandidateRow],
    project_root: Path,
) -> Tuple[DeckPlan, List[Finding]]:
    """把骨架排成一份帧序列。返回（deck，插图找不到之类的发现）。"""
    findings: List[Finding] = []
    deck = _mapping(outline.get("deck"))
    pages = read_page_entries(outline)
    chapters = [
        chapter for chapter in outline.get("chapters") or [] if isinstance(chapter, Mapping)
    ]
    figures = figure_index(structure)
    theme, _ = read_theme(outline)
    context = DeckContext(
        title=spec.title,
        subtitle=spec.subtitle,
        presenter=_text(deck.get("presenter")),
        occasion=_text(deck.get("occasion")),
        institute=spec.institution,
        graphics_path=graphics_path(spec),
        theme=theme,
    )
    home = theme.volume_keys()[0]  # 与某一篇无关的页（封面、目录、备份）用第一篇的颜色

    frames: List[Frame] = []
    # 与 frames 平行：这一帧覆盖的页区间（空元组表示不画进度条）
    spans: List[Tuple[int, int]] = []
    # 与 frames 平行：这一帧的渲染位置（章／帧或固定页名），用来把丢弃的链接目标归到帧上
    places: List[str] = []
    counts: Dict[str, int] = {}
    # 正片讲到的每一条要点，按出现次序——备份页就是它们的出处查找表。
    backup_entries: List[BackupEntry] = []
    # 幻灯片上的行内口径：链接只留标签，丢弃的目标进账本（见 _DroppedLinks）。
    links = _DroppedLinks()

    def add(
        frame: Frame, kind: str, span: Tuple[int, int] = (), place: str = ""
    ) -> None:
        frames.append(frame)
        spans.append(span)
        places.append(place)
        counts[kind] = counts.get(kind, 0) + 1

    # 开场：封面 + 主旨。封面帧的副题行取自 pages[0].picks[:1]（人写的一句话）；
    # 主旨是一句候选，没圈就是待选记号。
    for index, entry in enumerate(_of_kind(pages, "opening")):
        links.where = entry.label
        if index == 0:
            add(
                Frame(
                    kind="title",
                    title=_inline(entry.label, links),
                    cover_subline=_pick_text(entry.picks[:1], rows, links),
                    notes=_inline(entry.notes, links) if entry.notes else "",
                    volume=home,
                ),
                "opening",
                place=entry.label,
            )
            continue
        main_line = _pick_text(entry.picks[:1], rows, links)
        add(
            Frame(
                kind="quote",
                title=_inline(entry.label, links),
                points=(main_line or PLACEHOLDER,),
                footer=_entry_footer(entry, rows),
                notes=_inline(entry.notes, links) if entry.notes else "",
                volume=home,
            ),
            "opening",
            place=entry.label,
        )

    # 目录：封面之后的那一页，列出篇与章（行数据与旧版的"章节索引"同一份来源，但**不列
    # 页码**——幻灯片不假装知道纸面页数）。这一页是工具自己的算术，不含意见。
    for entry in _of_kind(pages, "navigation"):
        links.where = entry.label
        rows_, row_volumes = _toc_rows(chapters, spec)
        add(
            Frame(
                kind="table",
                title=_inline(entry.label, links),
                rows=rows_,
                table_align=("l", "l", "r"),
                table_fractions=_toc_fractions(rows_, theme),
                row_volumes=row_volumes,
                volume=home,
            ),
            "navigation",
            place=entry.label,
        )

    # 正文：骨架里一帧一页（frames 的长度已在 check 里对着 budget 核过）。每一帧的要点就是
    # 它自己的 picks，不再在这里切分；空帧照默认 4 个待选位置排，版面上要看得出该放几句。
    for chapter in chapters:
        label = _entry_label(chapter)
        links.where = label
        title = _inline(
            f"{chapter.get('label') or chapter.get('chapter')}　{chapter.get('title') or ''}".strip(),
            links,
        )
        chapter_frames = [
            frame for frame in chapter.get("frames") or [] if isinstance(frame, Mapping)
        ]
        available = _chapter_figure_list(chapter, figures)
        volume = _theme_volume(chapter, theme)
        start = len(frames) + 1
        for index, frame in enumerate(chapter_frames):
            where = f"{label} 第 {index + 1} 页"
            links.where = where
            picks = frame.get("picks")
            picks = list(picks) if isinstance(picks, list) else []
            slot = len(picks) if picks else POINTS_PER_CONTENT_PAGE
            figure = _resolve_figure(chapter, index, available, figures, findings)
            backup_entries.extend(_lookup_entries(picks, rows, volume, label))
            add(
                Frame(
                    kind="points",
                    title=title,
                    points=_frame_points(picks, slot, rows, links),
                    figure=figure,
                    figure_label=figure_label(figure) if figure is not None else "",
                    footer=_chapter_footer(picks, chapter, rows),
                    notes=_chapter_notes(chapter, index, links),
                    volume=volume,
                ),
                "content",
                (start, start + len(chapter_frames) - 1),
                place=where,
            )

    # 横向与讨论：与正文同一种版式，句子来自固定页自己的 picks；讨论页的短横走强调色。
    for kind in ("cross_cutting", "discussion"):
        for entry in _of_kind(pages, kind):
            links.where = entry.label
            figure = _lookup_figure(entry.figure, figures, findings, entry.label)
            picks = list(entry.picks)
            slot = max(len(picks), POINTS_PER_CONTENT_PAGE)
            backup_entries.extend(_lookup_entries(picks, rows, home, entry.label))
            add(
                Frame(
                    kind="points",
                    title=_inline(entry.label, links),
                    points=_frame_points(picks, slot, rows, links),
                    figure=figure,
                    figure_label=figure_label(figure) if figure is not None else "",
                    footer=_entry_footer(entry, rows),
                    notes=_inline(entry.notes, links) if entry.notes else "",
                    volume=home,
                    marker="thmaccent" if kind == "discussion" else "",
                ),
                kind,
                (len(frames) + 1, len(frames) + 1),
                place=entry.label,
            )

    # 备份：正片讲到的句子在这里合成一张**出处查找表**——帧数恒等于骨架预留的页数，
    # 每条一行（篇色小块 + 章号｜出处｜摘要）。条目按正片次序攒下来，篇与章因此天然成组
    # （横向页与讨论页的句子排在各章之后，带它们自己的页名与首篇的颜色）；摊到各帧时保持
    # 这个次序，一章的点不会散到两处。装不下只收摘要长度（16 → 12 → 10 → 8 字），字号已在
    # 主题下限、帧数是骨架给的，两样都不动。整句话在每位听众的讲义里都有，这一页的职责是
    # 回答"这条要点出自哪一节"，不是把正片再抄一遍；连最短的摘要都装不下，就是预留太小，
    # 由 deck-slides 报出来。
    backup_pages = _of_kind(pages, "backup")
    if backup_pages:
        columns = lookup_columns(
            [entry.label for entry in backup_entries],
            [entry.pointer for entry in backup_entries],
            theme,
        )
        fit = fit_backup(
            [entry.sentence for entry in backup_entries],
            len(backup_pages),
            columns.lines_per_frame,
            columns.gist_units,
        )
        if backup_entries and not fit.fits:
            findings.append(Finding("deck-slides", "error", _backup_reserve_message(fit)))
        for page, window in zip(backup_pages, _backup_windows(backup_entries, len(backup_pages))):
            links.where = page.label
            lookup = tuple(
                LookupCell(
                    volume=entry.volume,
                    label=_inline(entry.label, links),
                    pointer=_inline(entry.pointer, links),
                    gist=_inline(gist_text(entry.sentence, fit.gist_chars), links),
                )
                for entry in window
            )
            add(
                Frame(
                    kind="backup",
                    title=_inline(page.label, links),
                    lookup=lookup,
                    lookup_columns=columns,
                    volume=home,
                ),
                "backup",
                place=page.label,
            )

    findings.extend(_link_findings(links))

    # 页序与页区间：排完帧才知道全片多长，页脚的分母与进度条的位置都在这一趟里定。
    # 丢弃的链接目标在这里并进该帧的讲者备注（``\note{}``，只出现在讲义里）。
    wired = tuple(
        replace(
            frame,
            page=index + 1,
            span=spans[index],
            notes=_link_note(frame.notes, links.for_place(places[index])),
        )
        for index, frame in enumerate(frames)
    )
    return DeckPlan(frames=wired, context=context, counts=dict(counts)), findings


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _of_kind(pages: Sequence[PageEntry], kind: str) -> List[PageEntry]:
    return [page for page in pages if page.kind == kind]


def _pick_text(
    picks: Sequence[object], rows: Mapping[str, cand.CandidateRow], links: _DroppedLinks
) -> str:
    """一串 pick 里第一条能显示的 → 行内 LaTeX；一条都给不出就返回空串（调用方改打待选记号）。

    三种形状都走这里：候选 id 取候选表的句子，自由文本与提炼句取它们自己的句子，两者
    与候选句同走成书的行内渲染器（``tex.render_inline``，引号已在
    :func:`primer.slides.candidates.pick_display` 规整，链接只留标签）。形状读不出来、
    或候选 id 不在表里的跳过——它们是过闸时会挡下的 ``pick-shape``／``pick-missing``。

    显示的句子先经 :func:`primer.slides.prose.strip_lead_enumerator` 剥掉行首的原文编号
    （``（一）``／``一、``／``1.``…）：短横 bullet 是排版系统给的形式，编号再印一遍就是
    两个编号。随后经 :func:`primer.slides.prose.normalize_pick_text` 把半角标点、直引号与
    数字—单位间的空格规整成排印形式——**三种形状一视同仁**，模式 3 的凝练句与人写的自由
    文本因此不再以裸形式进 tex。清洗只作用于 picks 变成的文本，标题、表格与备份表不碰。
    """
    for value in picks:
        try:
            pick = cand.parse_pick(value, rows)
        except cand.PickError:
            continue
        display = normalize_pick_text(strip_lead_enumerator(cand.pick_display(pick, rows)))
        if display:
            return _inline(display, links)
    return ""


def _frame_points(
    picks: Sequence[object],
    slot: int,
    rows: Mapping[str, cand.CandidateRow],
    links: _DroppedLinks,
) -> Tuple[str, ...]:
    """一帧的要点：这一帧摊到 ``slot`` 条，取了句子就渲染，不足打待选记号。

    ``slot`` 为 0 只在"要点比帧数还少"时出现（``check`` 会报 ``pick-count`` warning）——
    那种帧照默认的 4 个待选位置排，不留空页。
    """
    if slot <= 0:
        slot = POINTS_PER_CONTENT_PAGE
    points = [_pick_text([value], rows, links) for value in picks[:slot]]
    while len(points) < slot:
        points.append(PLACEHOLDER)
    return tuple(points)


def _chapter_notes(chapter: Mapping[str, object], index: int, links: _DroppedLinks) -> str:
    """一章的备注只发在第一帧上：同一段话在每一帧重复进入讲义没有意义。"""
    notes = chapter.get("notes")
    if index == 0 and isinstance(notes, str) and notes:
        return _inline(notes, links)
    return ""


def _chapter_figure_list(
    chapter: Mapping[str, object], figures: Mapping[str, FigureRef]
) -> List[FigureRef]:
    """一章可用的插图：编号以章号打头（``8-3`` 属第 8 章，``A-2`` 属附录 A）。"""
    number = str(chapter.get("chapter") or "")
    if not number:
        return []
    prefix = f"{number}-"
    ordered = sorted(
        (key for key in figures if key.startswith(prefix)), key=_figure_order
    )
    return [figures[key] for key in ordered]


def _figure_order(number: str) -> Tuple[str, int]:
    head, _, tail = number.partition("-")
    return (head, int(tail) if tail.isdigit() else 0)


def _resolve_figure(
    chapter: Mapping[str, object],
    index: int,
    available: Sequence[FigureRef],
    figures: Mapping[str, FigureRef],
    findings: List[Finding],
) -> Optional[FigureRef]:
    """这一帧配哪幅图。

    章一级 ``figure`` 留空表示按默认：第 n 帧配该章的第 n 幅图（幅数不够就绕回来），
    于是"默认取该章第一幅"只是这句在 n=0 时的特例。填了编号就整章都用那一幅；
    填空串表示这一章不上图。
    """
    setting = chapter.get("figure")
    if isinstance(setting, str):
        if not setting:
            return None
        return _lookup_figure(setting, figures, findings, chapter.get("label") or "")
    if not available:
        return None
    return available[index % len(available)]


def _lookup_figure(
    number: Optional[str],
    figures: Mapping[str, FigureRef],
    findings: List[Finding],
    where: object,
) -> Optional[FigureRef]:
    if not number:
        return None
    figure = figures.get(number)
    if figure is None:
        findings.append(
            Finding(
                "figure-missing",
                "warning",
                f"{where} 指定了插图 {number!r}，但成书插图目录里没有这一幅；这一页不上图",
            )
        )
    return figure


def _chapter_footer(
    window: Sequence[object],
    chapter: Mapping[str, object],
    rows: Mapping[str, cand.CandidateRow],
) -> str:
    """帧脚上的出处：这一帧有候选就用第一个候选的节，没有就退回该章第一个讲得上的节。

    自由文本与提炼句不是书里的原句，这里不取它们的指针（提炼句的出处进了备份查找表）。
    """
    for value in window:
        try:
            pick = cand.parse_pick(value, rows)
        except cand.PickError:
            continue
        if not pick.is_candidate:
            continue
        row = rows.get(pick.identifier)
        if row is not None:
            return pointer_text(row.pointer)
    return chapter_pointer(chapter)


def _entry_footer(entry: PageEntry, rows: Mapping[str, cand.CandidateRow]) -> str:
    for value in entry.picks:
        try:
            pick = cand.parse_pick(value, rows)
        except cand.PickError:
            continue
        if not pick.is_candidate:
            continue
        row = rows.get(pick.identifier)
        if row is not None:
            return pointer_text(row.pointer)
    return ""


# ---------------------------------------------------------------- 目录页


def _theme_volume(chapter: Mapping[str, object], theme) -> str:
    """章所在篇的卷键；主题不认得的卷退回第一篇（框架里没有的颜色宏画不出来）。"""
    volume = str(chapter.get("volume") or "")
    return volume if volume in theme.volume_keys() else theme.volume_keys()[0]


def _toc_rows(
    chapters: Sequence[Mapping[str, object]], spec: DeckSpec
) -> Tuple[Tuple[Tuple[str, ...], ...], Tuple[str, ...]]:
    """目录页的行：逐篇一行篇名，篇下逐章一行（章号、章名、正片页）。

    章名取自骨架（它是从源 markdown 的标题机械搬运来的，不是谁另写的），篇名取自
    ``deck.yaml`` 的 ``sources[].title``。**不列页码**——幻灯片不假装知道纸面页数。
    返回（表行，数据行各自的卷键）：篇行与它下面各章行都带篇色，行首那枚小块因此
    把"这几章属于哪一篇"标出来。
    """
    volumes = {source.id: source.title for source in spec.sources}
    rows: List[Tuple[str, ...]] = [("章号", "篇 / 章", "正片页")]
    # 与**数据行**平行（表头那一行不在这里），见 beamer._table_body 的索引方式。
    row_volumes: List[str] = []
    current = None
    for chapter in chapters:
        volume = str(chapter.get("volume") or "")
        if volume != current:
            current = volume
            rows.append(("", volumes.get(volume, volume), ""))
            row_volumes.append(volume)
        rows.append(
            (
                str(chapter.get("label") or chapter.get("chapter") or ""),
                str(chapter.get("title") or ""),
                str(_int(chapter.get("budget"))),
            )
        )
        row_volumes.append(volume)
    return tuple(rows), tuple(row_volumes)


def _toc_fractions(
    rows: Sequence[Sequence[str]], theme: Theme
) -> Tuple[float, ...]:
    """目录表的列宽（占版心宽的比例）：数字列按内容收紧，余量全部给"篇 / 章"列。

    成书表格那套按自然宽度分列，每一列至少拿到半页——目录页的数字列因此白占大片版面。
    这里反过来：除"篇 / 章"列外的列各拿"最宽一格 + 一点净空"，那一列吃掉余下的版心宽，
    于是每一章仍是一行，表也铺满版心。行首的篇色小块占掉章号列一点宽，一并算进去。
    """
    columns = len(rows[0])
    unit_mm = 0.5 * theme.level("table_body").size * MM_PER_PT
    gap_mm = TABLE_GAP_PT * (columns - 1) / columns * MM_PER_PT
    budget_mm = theme.text_width_mm - columns * gap_mm
    needs: List[float] = []
    for index in range(columns):
        widest = max(
            (tables.natural_width(str(row[index])) for row in rows), default=0
        )
        width = (widest + INDEX_COLUMN_PAD_UNITS) * unit_mm
        if index == 0:
            width += LOOKUP_CHIP_MM
        needs.append(width)
    widths_mm = list(needs)
    if sum(needs) <= budget_mm:
        widths_mm[1] += budget_mm - sum(needs)
    else:
        widths_mm[1] = max(budget_mm - (sum(needs) - needs[1]), unit_mm)
    return tuple((width + gap_mm) / theme.text_width_mm for width in widths_mm)


# ---------------------------------------------------------------- 写盘与编译


def build_plan(project_root: Path, out_dir: Path) -> BuildPlan:
    """编译计划：与成书同一套 xelatex + xdvipdfmx，工作目录是工程根。"""
    return BuildPlan(
        project_root=project_root,
        out_dir=out_dir,
        jobname=DECK_JOBNAME,
        engine="xelatex",
        engine_runs=ENGINE_RUNS,
    )


def makefile_text(plan: BuildPlan) -> str:
    """Makefile：沿用成书那份（同一个编译计划），只把"由谁生成"说成 slides。"""
    return plan.makefile().replace("由 primer-book 生成", "由 primer-slides 生成")


def compile_deck(plan: BuildPlan, deck_dir: Path) -> Path:
    """跑 xelatex 若干遍再 xdvipdfmx；返回 PDF 路径（没出来就报错）。"""
    for suffix in LATEX_SUFFIXES + ("fls",):
        (deck_dir / f"{plan.jobname}.{suffix}").unlink(missing_ok=True)
    for argv, cwd in plan.commands():
        try:
            subprocess.run(argv, cwd=cwd, capture_output=True)
        except FileNotFoundError as exc:
            raise BuildGateError(f"engine not found: {exc.filename or argv[0]}") from exc
    if not plan.pdf.is_file() or plan.pdf.stat().st_size == 0:
        errors = []
        if plan.log.is_file():
            errors = [
                line
                for line in plan.log.read_text(errors="ignore").splitlines()
                if line.startswith("!")
            ]
        raise BuildGateError(
            "typesetting produced no pdf" + ("\n" + "\n".join(errors[:20]) if errors else "")
        )
    return plan.pdf


def read_log_findings(log_path: Path) -> List[Finding]:
    """日志发现：成书同一个解析器（overfull／underfull／缺字／未定义引用）。

    页码归属取 :data:`primer.book.logcheck.PAGE_AFTER`：幻灯片的页面由 beamer 的
    ``\\end{frame}`` 触发，帧的盒子在出页**之前**就报出来——日志里那条消息写在它所属
    那一页的 ``[n]`` 标记**之前**，取"之前最近的那个"会整整少一页（成书的连续正文不走
    这条路径，那里仍是 :data:`primer.book.logcheck.PAGE_BEFORE`）。
    """
    if not log_path.is_file():
        return []
    return [
        Finding(item.code, item.severity, item.message, item.location)
        for item in logcheck.parse_log(
            log_path.read_text(errors="ignore"), pages=logcheck.PAGE_AFTER
        )
    ]


def latex_error_findings(log_path: Path) -> List[Finding]:
    """排版错误的发现：日志里以 ``!`` 开头的行，每一条不重复的错误报一条。

    **编译没报错与编出来是对的，不是一回事**：少一道短横、表格错行，PDF 照样出得来，
    页数、缺字、overfull 三条闸门一条都不响。所以这一类发现是 error 级（``gate_build``
    把它们与缺字同等看待），只是缺字另有专门判据，不在这里重复报。
    """
    if not log_path.is_file():
        return []
    seen: Dict[str, Finding] = {}
    for line in log_path.read_text(errors="ignore").splitlines():
        if not line.startswith("!"):
            continue
        message = " ".join(line[1:].split())
        if not message or message.startswith("Missing character"):
            continue
        seen.setdefault(message, Finding("latex-error", "error", message))
    return list(seen.values())


def log_summary(
    findings: Sequence[Finding],
) -> Tuple[Mapping[str, int], Mapping[str, List[str]]]:
    """按代码计数，并把每条的位置集合起来（overfull 的页码要报出来）。"""
    counts: Dict[str, int] = {}
    places: Dict[str, List[str]] = {}
    for finding in findings:
        counts[finding.code] = counts.get(finding.code, 0) + 1
        if finding.location:
            places.setdefault(finding.code, []).append(finding.location)
    return counts, places


def frame_numbers(texts: Sequence[str]) -> List[int]:
    """从 PDF 文本层读出每页右下角的帧号（分子序列）。

    只认页脚那个形状的 ``n/N``：分母等于总页数、分子等于该页的页序。别处的斜杠数字
    （分数、日期）因此落不进来。
    """
    found: List[int] = []
    for index, text in enumerate(texts, 1):
        for numerator, denominator in FRAME_NUMBER_RE.findall(text):
            if int(denominator) == len(texts) and int(numerator) == index:
                found.append(int(numerator))
    return found


def gate_build(
    result_pdf: Path,
    expected: int,
    slide_count: int,
    log_findings: Sequence[Finding],
    root: Path,
) -> Tuple[int, List[int], List[Finding]]:
    """复核：缺字是硬失败，页数与帧号必须自洽。

    ``expected`` 是这次**排出来的帧数**（页序的真相）。它恒等于骨架的 ``deck.slides``：
    备份帧不再随句子多长而长（见 :func:`fit_backup`），所以这里没有"deck 长了"这一说。
    ``deck-slides`` 唯一还说得上话的情形——预留装不下正片要点——在排帧时就报出来了。
    """
    findings: List[Finding] = []
    for item in log_findings:
        if item.code == "latex-error":
            findings.append(
                Finding(
                    "latex-error",
                    "error",
                    f"日志报排版错误：{item.message}——PDF 出得来不代表排对了，请核对",
                )
            )
        if item.code == "missing-character":
            findings.append(
                Finding(
                    "missing-character",
                    "error",
                    f"日志报缺字：{item.message}（{item.location or '页码不详'}）",
                )
            )
    pages = pdf_tools.page_count(result_pdf)
    if pages != expected:
        findings.append(
            Finding(
                "deck-pages",
                "error",
                f"PDF 共 {pages} 页，排出的帧是 {expected} 帧——多出或短少的页来自跨页帧或"
                "空帧，请核对上面的表与日志",
                relative_to_root(result_pdf, root),
            )
        )
    numbers = frame_numbers(pdf_tools.page_texts(result_pdf))
    if not numbers:
        findings.append(
            Finding("deck-frame-number", "warning", "PDF 文本层里读不到帧号，无法核对帧序")
        )
    elif max(numbers) != slide_count:
        findings.append(
            Finding(
                "deck-frame-number",
                "error",
                f"帧号最大到 {max(numbers)}，与排出的 {slide_count} 帧不符；"
                "可能有帧跨页，页序与帧序因此不再一一对应",
            )
        )
    return pages, numbers, findings


# ---------------------------------------------------------------- 端到端


def run(
    outline_path: Path,
    project_root: Path,
    *,
    spec_path: Optional[Path] = None,
    tex_only: bool = False,
) -> Tuple[Optional[BuildResult], Tuple[str, ...], int]:
    """完整跑一次 ``build``：返回（结果、报告行、退出码）。

    error 级发现一律挡在写盘之前：报告照打、盘上一个字节不写。编译通过但复核不过，
    退出码同样非零——"生成了但不对"与"没生成"都不算成功。
    """
    root = Path(project_root).expanduser().resolve()
    if not root.is_dir():
        raise SlidesError(f"project root is not a directory: {root}")
    path = Path(outline_path).expanduser().resolve()
    if not path.is_file():
        raise SlidesError(f"outline not found: {path}")
    if path.name != OUTLINE_NAME:
        raise SlidesError(f"the outline file must be named {OUTLINE_NAME}: {path.name}")
    destination = path.parent

    outline = load_outline_document(path)
    rows = read_candidate_rows(destination / CANDIDATES_NAME)
    lengths = {identifier: row.length for identifier, row in rows.items()}
    current, problems, structure = current_fingerprint(outline, root, spec_path)
    stored = outline.get("fingerprint")
    differences = list(problems) + fingerprint_differences(
        stored if isinstance(stored, Mapping) else None, current
    )
    known_sections = (
        tuple(
            section.number
            for chapter in structure.chapters
            for section in chapter.sections
        )
        if structure is not None
        else ()
    )
    findings = validate_outline(
        outline,
        candidates=lengths,
        candidate_rows=rows,
        known_sections=known_sections,
        fingerprint_differences=differences,
    )
    report: List[str] = [f"primer-slides build: {path.name}", f"deck: {destination.name}"]
    report.extend(severity_lines(findings))
    hint = project_root_hint(outline, root)

    if any(finding.fatal for finding in findings):
        errors = sum(1 for finding in findings if finding.fatal)
        report.extend(
            [
                "",
                f"不生成：骨架有 {errors} 个错误，改完再跑一次 build。盘上未写任何文件。",
            ]
        )
        report.extend([hint] if hint else [])
        return None, tuple(report), 2

    spec = resolve_spec(root, destination.name, spec_path)
    if structure is None:
        # 指纹比对已经报过"复算不出结构"；这里回到原路再抛一次，消息对人更有用。
        raise SlidesError(f"cannot read the deck structure from {spec.path}")
    plan, extra = compose_frames(outline, spec, structure, rows, root)
    findings.extend(extra)

    # 排帧阶段的 error 同样挡在写盘之前：备份预留装不下正片要点属于这一类（deck-slides）。
    if any(finding.fatal for finding in extra):
        errors = sum(1 for finding in extra if finding.fatal)
        report.extend(severity_lines(extra))
        report.extend(
            [
                "",
                f"不生成：排帧有 {errors} 个错误，改完再跑一次 build。盘上未写任何文件。",
            ]
        )
        report.extend([hint] if hint else [])
        return None, tuple(report), 2

    document, emit_findings, slide_count, table_notes = render_document(
        plan.frames, plan.context
    )

    tex_path = destination / f"{DECK_JOBNAME}.tex"
    tex_path.write_text(document, encoding="utf-8")
    compile_plan = build_plan(root, destination)
    makefile_path = destination / MAKEFILE_NAME
    makefile_path.write_text(makefile_text(compile_plan), encoding="utf-8")

    report.extend(_deck_report(plan))
    report.extend(severity_lines(extra))
    files = [tex_path, makefile_path]

    pdf_path: Optional[Path] = None
    log_findings: List[Finding] = []
    pages: Optional[int] = None
    numbers: Tuple[int, ...] = ()
    exit_code = 0
    if tex_only:
        report.extend(["", "--tex-only：只写到 .tex，未编译。"])
    else:
        pdf_path = compile_deck(compile_plan, destination)
        log_findings = read_log_findings(compile_plan.log)
        log_findings.extend(latex_error_findings(compile_plan.log))
        declared = _int(_mapping(outline.get("model")).get("total_pages"))
        pages, numbers, gate = gate_build(
            pdf_path, plan.total, slide_count, log_findings, root
        )
        findings.extend(gate)
        if any(finding.fatal for finding in gate):
            exit_code = 1
        report.extend(
            _log_report(log_findings, emit_findings, table_notes, compile_plan.log, root)
        )
        report.extend(
            _gate_report(pages, numbers, slide_count, plan.total, declared, gate)
        )
        report.extend(severity_lines([f for f in gate if f.severity != "info"]))
        files.append(compile_plan.log)
        files.extend(
            destination / f"{DECK_JOBNAME}.{suffix}"
            for suffix in LATEX_SUFFIXES
            if suffix != "log" and (destination / f"{DECK_JOBNAME}.{suffix}").is_file()
        )

    report.extend(["", "写出:"])
    report.extend(f"  {relative_to_root(item, root)}" for item in files)
    report.extend([hint] if hint else [])
    return (
        BuildResult(
            tex=tex_path,
            makefile=makefile_path,
            pdf=pdf_path,
            log_findings=tuple(log_findings),
            pages=pages,
            frame_numbers=numbers,
            frames=plan.total,
            files=tuple(files),
        ),
        tuple(report),
        exit_code,
    )


def _deck_report(plan: DeckPlan) -> List[str]:
    context = plan.context
    counts = plan.counts
    order = ("opening", "navigation", "content", "cross_cutting", "discussion", "backup")
    summary = " + ".join(f"{name} {counts[name]}" for name in order if counts.get(name))
    lines = [
        "",
        f"幻灯片: {plan.total} 帧（{summary}）",
        f"  书目: {context.title}{context.subtitle}",
    ]
    stamp = "　".join(part for part in (context.presenter, context.occasion) if part)
    lines.append(f"  封面: {stamp}" if stamp else "  封面: 未填报告人与场合（deck.presenter / deck.occasion）")
    return lines


def _log_report(
    log_findings: Sequence[Finding],
    emit_findings: Sequence[Finding],
    table_notes: Sequence[str],
    log_path: Path,
    root: Path,
) -> List[str]:
    counts, places = log_summary(log_findings)
    lines = ["", f"日志: {relative_to_root(log_path, root)}"]
    if not counts:
        lines.append("  没有发现。")
    for code in sorted(counts):
        where = places.get(code, [])
        detail = f"  位置: {'、'.join(sorted(set(where))[:12])}" if where else ""
        lines.append(f"  {code} {counts[code]} 条{detail}")
    tables = [item for item in emit_findings if item.code == "table-layout"]
    if tables or table_notes:
        lines.append("表:")
        lines.extend(f"  {note}" for note in table_notes)
        for item in tables:
            lines.append(f"  [{item.code}] {item.message}（{item.location}）")
    return lines


def _gate_report(
    pages: Optional[int],
    numbers: Sequence[int],
    slide_count: int,
    expected: int,
    declared: Optional[int],
    gate: Sequence[Finding],
) -> List[str]:
    verdict = "相符" if pages == expected else "不符"
    against = f"，骨架 total_pages {declared}" if declared else ""
    lines = [
        "",
        "复核",
        f"  页数: PDF {pages} 页 vs 排出 {expected} 帧{against}（{verdict}）",
        (
            f"  帧号: 文本层读到 {len(numbers)} 个自洽的帧号，最大 {max(numbers)}（共 {slide_count} 帧）"
            if numbers
            else "  帧号: 文本层里没有读到"
        ),
    ]
    errors = [item for item in gate if item.fatal]
    lines.append("  结论: " + ("通过" if not errors else f"{len(errors)} 项未过"))
    return lines


__all__ = [
    "BACKUP_GIST_LADDER",
    "BackupEntry",
    "BackupFit",
    "BuildGateError",
    "BuildResult",
    "DeckPlan",
    "compose_frames",
    "figure_index",
    "fit_backup",
    "frame_numbers",
    "gist_text",
    "pointer_text",
    "read_candidate_rows",
    "run",
]
