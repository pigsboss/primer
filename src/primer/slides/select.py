# -*- coding: utf-8 -*-
"""``select``：让模型为骨架里的每一帧圈要点。

骨架是给人改的，``picks`` 本该由人圈。这一道子命令把"从每章的候选句里挑出该讲的那几句"
交给模型做一遍初稿，**人再审**——写回去的每一帧都留着 ``review``，模型只碰 ``picks``。

三种选点模式
------------

模式由**最具体的非空提示**决定，一层层往下找：

1. **模式 1（按图索骥）**：人给了方向，机器从该章候选池里挑出**原句**（逐字可回查）。
2. **模式 2（机器判断）**：整章都没有提示，机器自己从候选池里挑最值得讲的句子。
3. **模式 3（提炼）**：机器自己组织语言，输出 ``{text, derived_from}``；只在 ``--mode 3``
   强制时才会出现——按上面的次序找，模式 3 不会自然发生。

方向的来路按"最具体优先"：帧的 ``note`` > 该帧所属节的 ``hint`` > 章 ``hint``。
"帧所属节"由**节的序号**定：把该章的节清单依次均分给各帧，第 i 帧的中段落在哪一节就归
哪一节（帧写了 ``section`` 则听它的）。章里没有节时退回章一级。

调用粒度是**一章一次**，不是一帧一次：同一章几帧的要点要连贯、要不重复，一次成文才做得到。
模式 1/2 用 ``roles.select``（机械活），模式 3 用 ``roles.distill``（要自己组织语言，难活）。

只填不覆盖
----------

默认可写的帧只有一种：``picks`` 为空**且** ``review == pending``。``review == ok`` 的帧
永不触碰；``review == redo`` 只在 ``--redo`` 时重写；``picks`` 非空的帧一律不覆盖（要重做
先由人标 ``redo``）。``--force`` 是例外：除 ``review == ok`` 外全部重写，报告第一行就点明。
重写保留该帧的 ``note``——那正是人给它写的方向。

固定页（``pages[].picks``：封面主旨、横向议题、讨论页）**不在这一步的范围内**：它们没有
``review`` 可做闸门，也不是"从候选池里挑一句"能填的东西，仍然由人自己写。

写回时**只换可写帧的 ``picks`` 那一段文本**（:func:`rewrite_picks`），文件其余字节原样保留：
头部注释、每章 ``budget`` 的行尾注释、``theme`` 块都不重排。写回后再把文件读回来核对一次
目标帧的 ``picks``，对不上就整个不写（宁可没写，不要写坏）。

容量兜底
--------

一页只放得下固定的字数：骨架的 ``deck.capacity_per_page``（不显式给时由 ``outline``
按骨架自己的 theme 块量出来，默认主题是 336 字）。这个数**只有一处
读法**——:func:`primer.slides.validate.capacity_per_page`，``check`` 的 ``slide-overflow``
与这里调的是同一个函数，所以"挑选时算的容量"与"校验时报的容量"不会漂移。字数也按同一口径
计（:func:`primer.slides.candidates.pick_length`）。

提示词里把每一帧的字数预算写明，但模型的自觉只是第一道保障：

* 回信里**任何一帧**的句字数之和超过容量，整章就**重问一次**（只一次），提示词里追加一句
  "上一版第 N 帧超出容量 X 字，这次必须压到 336 以内"；重问的请求照旧计入用量。
* 重问回来仍超，就**确定性地丢**：该帧按字数从大到小丢到放得下为止，留下的保持原有次序，
  丢掉的记 ``over-capacity-drop`` 与被丢的 id——``select.jsonl`` 与报告的"容量对账"里都写着，
  绝不静默。

一处超容量只影响它自己那一帧的取舍：其余帧的选点照旧写回，报告里逐帧对账。

出处与安全
----------

每次运行往 ``outline.yaml`` 同目录的 ``select.jsonl`` **追加**记录，一帧一行：模式、方向、
选中的 picks、用量、结果。**绝不写密钥**；``base_url`` 只记 ``scheme://host``
（:func:`base_url_host`）——既避开可能嵌在 URL 里的凭据，也够认出是哪个端点。用量照
:class:`primer.slides.client.UsageTotals` 的口径，**每次请求与重试都计入**；一章一次请求，
所以增量记在**触发这次请求的那一帧**那一行上，把整份 jsonl 的用量相加即得本次运行的总账。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit

import yaml

from ..config import Config, ConfigError, Endpoint, api_key, load_config, resolve_role
from ..paths import relative_to_root
from . import candidates as cand
from .client import (
    DEFAULT_MAX_TOKENS,
    HTTP_TIMEOUT,
    ChatClient,
    SelectCallError,
    SelectReplyError,
    Transport,
    delta,
    parse_json_reply,
)
from .plan import (
    DEFAULT_REVIEW,
    POINTS_PER_CONTENT_PAGE,
    POINTS_PER_CONTENT_PAGE_RANGE,
    SlidesError,
)
from .prose import normalize_pick_text
from .validate import CANDIDATES_NAME, capacity_per_page, load_outline_document

PROMPT_VERSION = "slides-select-3"
"""提示词模板的版本号。模板一改就换号，select.jsonl 里的记录据此知道用的是哪一版。

``slides-select-3``：第 3 类（模式 3 提炼）加了**形状约束**——每帧 3–4 条、首条是
≤40 字的主题句、总字数有下限、同章各帧字数不要相差一倍以上。"""

ROLE_SELECT = "select"
ROLE_DISTILL = "distill"

MODE_HINT = 1
MODE_JUDGE = 2
MODE_DISTILL = 3
MODE_CHOICES = (MODE_HINT, MODE_JUDGE, MODE_DISTILL)
MODE_NAMES = {MODE_HINT: "按图索骥", MODE_JUDGE: "机器判断", MODE_DISTILL: "提炼"}

SELECT_LOG_NAME = "select.jsonl"
DEFAULT_DECK = "review-seminar"

MIN_PICKS_PER_FRAME = POINTS_PER_CONTENT_PAGE_RANGE[0]
MAX_PICKS_PER_FRAME = POINTS_PER_CONTENT_PAGE_RANGE[1]
DEFAULT_PICKS_PER_FRAME = POINTS_PER_CONTENT_PAGE

# 模式 3（提炼）的一帧形状。比通用上限更紧：一帧 3–4 条，**首条是主题句**。
DISTILL_MIN_PICKS = 3
DISTILL_MAX_PICKS = 4
# 主题句（首条）的字数上限：一条要点约占一屏的一行半，40 字足够立住判断又不致铺开。
DISTILL_LEAD_MAX_CHARS = 40
# 一帧总字数的下限。一页容量 336 字，但只挑 3–4 条短句时常常写到一百字上下就停笔，
# 与同章别的帧相差一倍以上，看着像没讲完。取 140 字（≈容量 42%）作下限，逼每帧把
# 主题句之外的支撑句也写够，跨页颗粒度才对得上。
DISTILL_MIN_TOTAL_CHARS = 140

MAX_RETRIES = 1
"""一次调用的重试次数；空正文、残缺 JSON 与网络错误都按可重试处理。"""

CHARS_PER_TOKEN = 1.5
"""估算用：中文大致 1.5 字一个 token。只是下单前的粗估，实账以回信里的 usage 为准。"""

TOKENS_PER_PICK = 30
"""估算用：回信里一条 pick 大致占的 token 数（id 或提炼句 + 理由）。"""

# 帧的归属：note > 节 hint > 章 hint；来源码记进报告与日志。
SOURCE_NOTE = "note"
SOURCE_SECTION = "section"
SOURCE_CHAPTER = "chapter"
SOURCE_NONE = "none"

# 跳过与结果的码（英文，供机器读；报告里翻成中文）。
SKIP_OK = "ok"
SKIP_PICKS = "picks"
SKIP_REDO = "redo"
SKIP_NO_CANDIDATES = "no-candidates"
OUTCOME_FILLED = "filled"
OUTCOME_SKIPPED = "skipped"
OUTCOME_EMPTY = "empty"
OUTCOME_DROPPED = "dropped"
OUTCOME_FAILED = "failed"

SKIP_LABELS = {
    SKIP_OK: "review 已是 ok",
    SKIP_PICKS: "picks 非空",
    SKIP_REDO: "review 是 redo，未给 --redo",
    SKIP_NO_CANDIDATES: "该章没有候选句",
}

CAPACITY_DROP = "over-capacity-drop"
"""按容量丢句的原因码：重问一次仍超容量，这一帧就这样被丢到放得下。"""

DROP_LABELS = {
    "unknown-id": "候选表里没有这个 id",
    "no-derived-from": "提炼句没有 derived_from",
    "derived-unknown": "derived_from 指了候选表里没有的 id",
    "shape": "pick 的形状读不出来",
    "empty": "pick 是空的",
    "text-not-in-pool": "模式 1/2 只能回候选 id，回了一句池子里没有的话",
    "digit-mismatch": "句子里的数字没在 derived_from 的原文里出现",
    "truncated": "超过每帧上限，截掉了多出来的几条",
    CAPACITY_DROP: "这一帧放不下：字数之和超过容量，按字数从大到小丢到放得下",
}


# ---------------------------------------------------------------- 模式判定


@dataclass(frozen=True)
class FrameMode:
    """一帧的选点模式与它拿到的方向。``source`` 是方向从哪来（note／section／chapter／none）。"""

    mode: int
    hint: str
    source: str


def frame_section(
    chapter: Mapping[str, object], index: int, frames: int
) -> Optional[Mapping[str, object]]:
    """第 ``index`` 帧（共 ``frames`` 帧）归属哪一节，看不出时给 ``None``。

    帧是演示页、节是纸面结构，两者没有天然的对应，所以用一条能写出来、能复算的规则：
    **按节的序号均分**——把该章的 ``sections`` 依次均分给各帧，第 i 帧取它那一段的中点，
    落在哪一节就归哪一节（``math.floor((i + 0.5) * 节数 / 帧数)``）。以前这条规则靠成书页码
    来算（把印刷页区间均分），现在直接按节的次序来——书不重编了，节的次序仍是一样。

    帧自己写了 ``section``（节号）时以它为准：那是人明确指定的归属，工具不猜。指了一个
    本章没有的节号时退回均分，不当成错误——``unknown-section`` 由 ``check`` 负责。
    """
    sections = [
        section
        for section in chapter.get("sections") or []
        if isinstance(section, Mapping) and section.get("number")
    ]
    if not sections or frames <= 0:
        return None
    frame_list = chapter.get("frames") or []
    if 0 <= index < len(frame_list) and isinstance(frame_list[index], Mapping):
        wanted = frame_list[index].get("section")
        if isinstance(wanted, str) and wanted:
            named = next(
                (section for section in sections if str(section.get("number")) == wanted),
                None,
            )
            if named is not None:
                return named
    position = int((index + 0.5) * len(sections) / frames)
    return sections[min(max(position, 0), len(sections) - 1)]


def frame_mode(
    chapter: Mapping[str, object],
    index: int,
    frames: int,
    forced: Optional[int] = None,
) -> FrameMode:
    """一帧的模式与方向：帧 ``note`` > 该帧所属节 ``hint`` > 章 ``hint`` > 模式 2。

    ``forced`` 给了就覆盖自然判定。强制模式 2 时方向一并清空——模式 2 的含义就是
    "不看人给的提示，自己判断"；强制模式 1/3 时方向照旧带上。
    """
    frame_list = chapter.get("frames") or []
    frame = frame_list[index] if 0 <= index < len(frame_list) else {}
    note = frame.get("note") if isinstance(frame, Mapping) else ""
    note = note if isinstance(note, str) else ""
    section = frame_section(chapter, index, frames)
    section_hint = ""
    if section is not None and isinstance(section.get("hint"), str):
        section_hint = section["hint"]
    chapter_hint = chapter.get("hint") if isinstance(chapter.get("hint"), str) else ""

    if note.strip():
        natural = FrameMode(MODE_HINT, note.strip(), SOURCE_NOTE)
    elif section_hint.strip():
        natural = FrameMode(MODE_HINT, section_hint.strip(), SOURCE_SECTION)
    elif chapter_hint.strip():
        natural = FrameMode(MODE_HINT, chapter_hint.strip(), SOURCE_CHAPTER)
    else:
        natural = FrameMode(MODE_JUDGE, "", SOURCE_NONE)

    if forced is None or forced == natural.mode:
        return natural
    hint = "" if forced == MODE_JUDGE else natural.hint
    return FrameMode(forced, hint, natural.source)


# ---------------------------------------------------------------- 候选池


def chapter_rows(
    rows: Mapping[str, cand.CandidateRow], number: str
) -> List[cand.CandidateRow]:
    """一章自己的候选池：id 形如 ``s<章>-<节>p<句>``，按章号前缀取。

    前缀带上那个 ``-``，所以 ``s1-`` 不会把 ``s11-…`` 收进来。表的次序就是
    ``candidates.md`` 的次序（信号降序 → 引用降序 → 章内位置），原样保留。
    """
    prefix = f"s{number}-"
    return [row for identifier, row in rows.items() if identifier.startswith(prefix)]


# ---------------------------------------------------------------- 字数与容量


def pick_lengths(rows: Mapping[str, cand.CandidateRow]) -> Dict[str, int]:
    """候选表 → ``id -> 字数``；正是 :func:`primer.slides.validate.load_candidates` 的形状。"""
    return {identifier: row.length for identifier, row in rows.items()}


def frame_chars(picks: Sequence[object], lengths: Mapping[str, int]) -> int:
    """一帧选中的字数之和，与 ``check`` 的 ``slide-overflow`` **同一条**口径。

    每条先 :func:`primer.slides.candidates.parse_pick` 归一再按
    :func:`primer.slides.candidates.pick_length` 计：候选 id 取候选表「字数」列，
    提炼句（与作者自由文本）取 ``len(text)``。形状读不出来的一条按 0 计——它进不了骨架，
    也就不占容量。
    """
    total = 0
    for pick in picks:
        try:
            parsed = cand.parse_pick(pick, lengths)
        except cand.PickError:
            continue
        total += cand.pick_length(parsed, lengths)
    return total


def pick_name(pick: object) -> str:
    """丢掉一条时报告与日志里怎么称呼它：候选 id 用它自己，句子取前 24 字。"""
    if isinstance(pick, str):
        return pick
    text = pick.get("text") if isinstance(pick, Mapping) else ""
    return f"提炼句「{str(text)[:24]}」"


def trim_to_capacity(
    picks: Sequence[object], lengths: Mapping[str, int], capacity: int
) -> Tuple[List[object], List[Tuple[str, str]]]:
    """一帧超容量时按字数从大到小丢到放得下，返回（留下的，丢掉的）。

    丢最长的几条：一页放不下时先砍长的，短的才挤得进去。留下的**保持原有次序**，
    模型"先结论后细节"的排法不会被重排。丢掉的记 :data:`CAPACITY_DROP`、它自己的名字与
    字数，报告的"容量对账"里逐条点出来。容量比最短的一句还小时会丢空，调用方按
    "这一帧没填"处理——照样记在日志里，不静默。
    """
    total = frame_chars(picks, lengths)
    sizes = [frame_chars([pick], lengths) for pick in picks]
    order = sorted(range(len(picks)), key=lambda index: (-sizes[index], index))
    dropped: List[Tuple[str, str]] = []
    removed = set()
    for index in order:
        if total <= capacity:
            break
        removed.add(index)
        total -= sizes[index]
        dropped.append((CAPACITY_DROP, f"{pick_name(picks[index])}（{sizes[index]} 字）"))
    kept = [pick for index, pick in enumerate(picks) if index not in removed]
    return kept, dropped


def overflow_lines(overflow: Sequence[Tuple[int, int]], capacity: int) -> List[str]:
    """重问时追加在提示词末尾的那几句：哪一帧超了多少、必须压回多少。

    ``overflow`` 是（帧下标，这一帧的字数）。提示词里写的是帧下标而不是"第几页"——
    回信的 ``index`` 用的也是它，两处对得上。
    """
    lines = ["", "上一版超了容量，请重挑这些帧（只重发这一封）："]
    for index, chars in overflow:
        lines.append(
            f"- 帧 {index}：上一版第 {index} 帧超出容量 {chars - capacity} 字"
            f"（{chars} 字 > {capacity} 字），这次必须压到 {capacity} 以内。"
        )
    lines.append(
        "字数多的句子宁可留到别的帧单独用，也不要挤在同一帧里；条数不够就少挑几条。"
    )
    return lines


# ---------------------------------------------------------------- 计划


@dataclass(frozen=True)
class FramePlan:
    """一帧的处置计划：模式、方向、可写与否、跳过的原因。"""

    index: int
    mode: int
    hint: str
    source: str
    note: str
    writable: bool
    skip: str = ""
    section: str = ""


@dataclass(frozen=True)
class ChapterPlan:
    """一章的处置计划：要问模型什么、哪些帧会变、每帧的字数预算有多大。"""

    ordinal: int
    number: str
    label: str
    title: str
    budget: int
    capacity: int
    role: str
    frames: Tuple[FramePlan, ...]
    rows: Tuple[cand.CandidateRow, ...]
    chapter: Mapping[str, object]
    prompt: str

    @property
    def writable(self) -> Tuple[FramePlan, ...]:
        return tuple(frame for frame in self.frames if frame.writable)

    @property
    def skipped(self) -> Tuple[FramePlan, ...]:
        return tuple(frame for frame in self.frames if not frame.writable)


def frame_writable(frame: Mapping[str, object], *, force: bool, redo: bool) -> Tuple[bool, str]:
    """这一帧默认可写吗，不可写是为什么。

    ``review == ok`` 永不触碰；``review == redo`` 只在 ``--redo`` 时重写；``picks`` 非空
    的不覆盖（要先重做得由人标 ``redo``）。``--force`` 只放过 ``ok`` 之外的帧。
    """
    review = frame.get("review", DEFAULT_REVIEW)
    picks = frame.get("picks") or []
    if review == "ok":
        return False, SKIP_OK
    if force:
        return True, ""
    if review == "redo":
        return (True, "") if redo else (False, SKIP_REDO)
    if picks:
        return False, SKIP_PICKS
    return True, ""


def plan_chapter(
    ordinal: int,
    chapter: Mapping[str, object],
    rows: Mapping[str, cand.CandidateRow],
    *,
    capacity: int,
    forced: Optional[int],
    force: bool,
    redo: bool,
) -> ChapterPlan:
    """把一章的骨架记录翻成计划，并把要发给模型的提示词拼好。

    ``capacity`` 是一页的字数上限，由 :func:`primer.slides.validate.capacity_per_page`
    从**骨架**读出来（``check`` 报 ``slide-overflow`` 用的是同一个数），写进提示词，
    也留着给解析后的容量兜底用。
    """
    number = str(chapter.get("chapter"))
    budget = len(chapter.get("frames") or [])
    own = chapter_rows(rows, number)
    has_candidates = bool(own)
    frames: List[FramePlan] = []
    for index in range(budget):
        raw = chapter["frames"][index]
        raw = raw if isinstance(raw, Mapping) else {}
        resolved = frame_mode(chapter, index, budget, forced)
        note = raw.get("note")
        note = note.strip() if isinstance(note, str) else ""
        writable, skip = frame_writable(raw, force=force, redo=redo)
        if writable and not has_candidates:
            writable, skip = False, SKIP_NO_CANDIDATES
        owner = frame_section(chapter, index, budget)
        section = str(owner.get("number")) if owner is not None else ""
        frames.append(
            FramePlan(
                index=index,
                mode=resolved.mode,
                hint=resolved.hint,
                source=resolved.source,
                note=note,
                writable=writable,
                skip=skip,
                section=section,
            )
        )
    mode_list = {frame.mode for frame in frames}
    role = ROLE_DISTILL if MODE_DISTILL in mode_list else ROLE_SELECT
    label = str(chapter.get("label") or number)
    title = str(chapter.get("title") or "")
    prompt = build_prompt(chapter, frames, own, capacity)
    return ChapterPlan(
        ordinal=ordinal,
        number=number,
        label=label,
        title=title,
        budget=budget,
        capacity=capacity,
        role=role,
        frames=tuple(frames),
        rows=tuple(own),
        chapter=chapter,
        prompt=prompt,
    )


def select_chapters(
    outline: Mapping[str, object], only: Sequence[str] = (), limit: Optional[int] = None
) -> List[Tuple[int, Mapping[str, object]]]:
    """按 ``--only``／``--limit`` 从骨架里挑章，返回（章在骨架里的次序，章记录）。

    次序取骨架里的位置而不是筛选后的位置——写回时按它定位。``--only`` 点名了骨架里
    没有的章号就报错，免得人以为自己圈上了。
    """
    chapters = [
        chapter for chapter in outline.get("chapters") or [] if isinstance(chapter, Mapping)
    ]
    wanted = {str(value) for value in only}
    if wanted:
        present = {str(chapter.get("chapter")) for chapter in chapters}
        missing = sorted(wanted - present)
        if missing:
            raise SlidesError(
                f"--only names chapter(s) the outline does not have: {', '.join(missing)}"
            )
    picked = [
        (ordinal, chapter)
        for ordinal, chapter in enumerate(chapters)
        if not wanted or str(chapter.get("chapter")) in wanted
    ]
    if limit is not None:
        if limit < 0:
            raise SlidesError("--limit must not be negative")
        picked = picked[:limit]
    return picked


# ---------------------------------------------------------------- 提示词


def _source_label(frame: FramePlan) -> str:
    """帧的方向是从哪儿来的，写进提示词，让模型知道这是"这一页的要求"还是"整章的方向"。"""
    if frame.source == SOURCE_NOTE:
        return "本帧的 note"
    if frame.source == SOURCE_SECTION:
        return f"节 {frame.section} 的 hint" if frame.section else "该节 hint"
    if frame.source == SOURCE_CHAPTER:
        return "章的 hint"
    return "人工指定"


def build_prompt(
    chapter: Mapping[str, object],
    frames: Sequence[FramePlan],
    rows: Sequence[cand.CandidateRow],
    capacity: int,
) -> str:
    """一章一封信：节清单、容量、候选表、各帧的方向与约束，最后是回信的 JSON 形状。

    提示词用中文（书是中文的，句子要逐字比对），回信的键名用英文。只列**要填的**帧，
    并且用它们在骨架里的真实下标，好让回信的 ``index`` 直接对上。

    章 ``hint`` 与节 ``hint`` 都排在清单里，帧的 ``note`` 挂在帧那一行上——"各帧的 note、
    章/节 hint"这一组方向因此一件不落地进了信里。强制模式 2（``--mode 2``）时整封信不带
    任何方向：模式 2 的含义就是"不看人给的提示，自己判断"。

    ``capacity``（一页的字数上限）写在信头与约束里两处：信头说清它是版面的硬约束、
    怎么用候选表的「字数」列估这一帧的预算，约束里再钉一遍"之和不得超过"。写两遍不是
    啰嗦——这是一条字数一多就会被触发的硬边界，退回重挑的代价是一次请求。
    """
    number = str(chapter.get("chapter"))
    label = str(chapter.get("label") or number)
    title = str(chapter.get("title") or "")
    todo = [frame for frame in frames if frame.writable]
    # 有帧要看方向（模式 1）或可以提炼（模式 3）时，才把章／节的方向排进信里；
    # 全是模式 2 就不带方向。
    show_hints = any(frame.mode != MODE_JUDGE for frame in todo)
    chapter_hint = chapter.get("hint") if isinstance(chapter.get("hint"), str) else ""
    head = f"章：{label}　{title}"
    if show_hints and chapter_hint.strip():
        head += f"　（章方向：{chapter_hint.strip()}）"
    lines: List[str] = [
        "你在为一套中文技术演示（幻灯片）圈要点。下面给你**一章**的候选句表，"
        "以及这一章要讲的几页幻灯片（帧）。请为每一帧挑出该讲的句子。",
        "",
        head,
        f"帧数：本章共 {len(frames)} 帧，本次只需为下面 {len(todo)} 帧圈要点。",
        f"容量：每帧一页，一页只放得下 {capacity} 字（capacity_per_page）；"
        f"每一帧挑中的句子，**字数之和不得超过 {capacity} 字**。",
        "候选表的「字数」列就是每句的字数，请把这一帧的字数预算当成选择依据："
        "一百多字的长句优先留给别的帧单独用，别在同一个帧里放两条长句。",
        f"3–{MAX_PICKS_PER_FRAME} 条仍是目标，但**宁可少而精**——够不到 "
        f"{MIN_PICKS_PER_FRAME} 条也要先守住容量：超了会被退回重挑一次，"
        f"重挑仍超就由工具按字数从大到小丢掉，直到放得下。",
        "",
    ]
    sections = [
        section for section in chapter.get("sections") or [] if isinstance(section, Mapping)
    ]
    lines.append("节清单（节号与源文件里的标题层级一致）：")
    if sections:
        for section in sections:
            hint = section.get("hint") if isinstance(section.get("hint"), str) else ""
            direction = f"（方向：{hint.strip()}）" if show_hints and hint.strip() else ""
            lines.append(
                f"- §{section.get('number')}　{section.get('title') or ''}{direction}"
            )
    else:
        lines.append("- （本章没有节清单，按章整体讲）")
    lines.extend(
        [
            "",
            "候选句表（**只能**从这张表里取 id；「句子」列就是原文，"
            "粗体 `**` 与引用标记 `[n]` 都是原文的一部分）：",
            "| id | 类型 | 信号 | 引用 | 字数 | 句子 |",
            "|:--|:--|:--|--:|--:|:--|",
        ]
    )
    for row in rows:
        cell = row.display.replace("|", "\\|").replace("\n", " ")
        lines.append(
            f"| {row.id} | {row.type} | {row.signal_codes} | {row.citations} | "
            f"{row.length} | {cell} |"
        )
    lines.extend(["", "各帧的任务："])
    for frame in todo:
        head = f"- 帧 {frame.index}（模式 {frame.mode} · {MODE_NAMES[frame.mode]}）"
        if frame.hint:
            head += f"：方向（{_source_label(frame)}）：{frame.hint}"
        lines.append(head)
        if frame.mode == MODE_DISTILL:
            lines.append(
                "  这一帧允许你**自己组织语言**写出一条新句："
                '{"text": "……", "derived_from": ["'
                + (rows[0].id if rows else "s1-1p01")
                + '"], "verbatim": false}'
            )
            lines.append(
                "  derived_from 必须引用上表里的 id；新句里的每个数字都要逐字出现在"
                "这些句子的原文里。给不出出处、或数字对不上的句子会被丢弃。"
            )
            lines.append(
                f"  这一帧按**形状**写：共 {DISTILL_MIN_PICKS}–{DISTILL_MAX_PICKS} 条；"
                f"**首条是主题句**（提炼句，≤{DISTILL_LEAD_MAX_CHARS} 字，要下判断，"
                "不能是疑问句或话题罗列）；其余各条取自**同一节**，做它的支撑。"
            )
            lines.append(
                f"  这一帧的总字数要在 {DISTILL_MIN_TOTAL_CHARS}–{capacity} 字之间；"
                "**同一章各帧的字数不要相差一倍以上**——跳过简的一帧、写满难的一帧，"
                "排出来一页挤一页空，听众会觉得你漏讲了。"
            )
    example_id = rows[0].id if rows else "s1-1p01"
    first = todo[0].index if todo else 0
    constraints = [
        "",
        "约束：",
        f"1. 每帧 {MIN_PICKS_PER_FRAME}–{MAX_PICKS_PER_FRAME} 条，默认 {DEFAULT_PICKS_PER_FRAME} 条。",
        "2. 模式 1/2 的帧只能回候选 id，不要改写原句；同一章的帧之间不要重复同一句。",
        "3. 每帧按“先结论、后细节”的次序排；why 写一句话说明这一帧为什么这么选。",
        f"4. 每帧所有句子的字数之和不得超过 {capacity} 字。这是版面的硬约束，"
        "先于第 1 条：预算不够就少挑几条，不许靠改写原句来省字数。",
    ]
    if any(frame.mode == MODE_DISTILL for frame in todo):
        constraints.append(
            f"5. 模式 3 的帧（提炼）：{DISTILL_MIN_PICKS}–{DISTILL_MAX_PICKS} 条，"
            f"**首条是主题句**（≤{DISTILL_LEAD_MAX_CHARS} 字，要下判断）；"
            f"其余各条取自同一节；总字数 ≥ {DISTILL_MIN_TOTAL_CHARS} 字且 ≤ {capacity} 字；"
            "同一章各帧的字数不要相差一倍以上。"
        )
    constraints.extend(
        [
            "",
            "回 JSON，不要别的话：",
            '{"frames": [{"index": '
            + str(first)
            + ', "picks": [{"id": "'
            + example_id
            + '"}], "why": "一句话"}]}',
        ]
    )
    lines.extend(constraints)
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- 回信


@dataclass(frozen=True)
class FrameReply:
    """模型对一帧的答复（原样，尚未校验）。"""

    index: int
    picks: Tuple[object, ...]
    why: str


def parse_frames_reply(payload: object) -> List[FrameReply]:
    """把回信 JSON 收成逐帧答复；``index`` 缺失就按位置排，重复的取先到的那个。

    容错只做这一层：形状不对的帧跳过（调用方按"这一帧没回"处理），不因为一条坏帧
    丢掉整章。
    """
    raw_frames: object = payload.get("frames") if isinstance(payload, Mapping) else payload
    if not isinstance(raw_frames, (list, tuple)):
        return []
    out: List[FrameReply] = []
    seen = set()
    for position, entry in enumerate(raw_frames):
        if not isinstance(entry, Mapping):
            continue
        index = entry.get("index", entry.get("frame", entry.get("i", position)))
        if isinstance(index, bool) or not isinstance(index, int):
            index = position
        if index in seen:
            continue
        seen.add(index)
        raw_picks = entry.get("picks")
        if isinstance(raw_picks, (str, Mapping)):
            raw_picks = [raw_picks]
        if not isinstance(raw_picks, (list, tuple)):
            raw_picks = []
        why = entry.get("why", entry.get("reason", entry.get("rationale", "")))
        out.append(
            FrameReply(
                index=index,
                picks=tuple(raw_picks),
                why=why.strip() if isinstance(why, str) else "",
            )
        )
    return out


# 数字串：提炼句里的每个数字串都必须逐字出现在 derived_from 的原文里。
DIGIT_RUN_RE = re.compile(r"\d+")


def digit_mismatch(text: str, originals: Sequence[str]) -> Optional[str]:
    """句子里的数字里第一个没在原文中出现的那一段；全都在就返回 ``None``。

    比对的是**原文**（候选表「句子」列的原样，带上 `**` 与 `[n]`），不做归一化——
    这是一条确定性判据，"逐字"就得是逐字。
    """
    joined = "\n".join(originals)
    for run in DIGIT_RUN_RE.findall(text):
        if run not in joined:
            return run
    return None


def _match_row(text: str, rows: Mapping[str, cand.CandidateRow]) -> Optional[str]:
    """一句话与候选表里的哪一行**逐字相同**（去首尾空白后），返回那个 id。"""
    wanted = text.strip()
    for identifier, row in rows.items():
        if row.display.strip() == wanted:
            return identifier
    return None


def clean_picks(
    raw_picks: Sequence[object],
    rows: Mapping[str, cand.CandidateRow],
    mode: int,
) -> Tuple[List[object], List[Tuple[str, str]]]:
    """把模型回的 picks 校验成能写进骨架的三种形状，返回（留下的，丢弃的）。

    留下的就是 ``outline.yaml`` 里的取值：候选 id 是字符串，提炼句是
    ``{text, derived_from}``。丢弃的记 (原因码, 细节)，报告里按原因计数。

    * 模式 1/2 只收原句：回 ``{"id": …}``，或回一句与候选表逐字相同的话（救成那个 id）；
      回别的话一律丢弃——机器不许替作者改字。
    * 模式 3 收 ``{text, derived_from}``：出处必须是本章候选，且句里的数字要逐字出现在
      出处的原文里（:func:`digit_mismatch`）。``verbatim: true`` 且与某条候选逐字相同的，
      按原句记成它的 id（那是更强的出处）。新写的文本落盘前经
      :func:`primer.slides.prose.normalize_pick_text` 规整（半角标点、直引号、数字—单位
      间的空格），yaml 里存的是排印形式，与 ``build`` 出页面时的文本同一套。
    * 重复的 picks 去重；多过 :data:`MAX_PICKS_PER_FRAME` 条的截掉。
    """
    kept: List[object] = []
    dropped: List[Tuple[str, str]] = []
    seen = set()

    for raw in raw_picks:
        value: Optional[object] = None
        if isinstance(raw, str):
            if raw.strip() in rows:
                value = raw.strip()
            elif cand.PICK_ID_RE.match(raw.strip()):
                dropped.append(("unknown-id", raw.strip()))
            elif not raw.strip():
                dropped.append(("empty", ""))
            else:
                # 不是已知 id：可能是与原句逐字相同的一句话（救成那个 id），也可能是
                # 模式 1/2 里不该出现的改写句。
                matched = _match_row(raw, rows)
                if matched:
                    value = matched
                else:
                    dropped.append(("text-not-in-pool", raw.strip()[:40]))
        elif isinstance(raw, Mapping):
            identifier = raw.get("id")
            if isinstance(identifier, str) and identifier.strip():
                if identifier.strip() in rows:
                    value = identifier.strip()
                else:
                    dropped.append(("unknown-id", identifier.strip()))
            else:
                text = raw.get("text")
                derived = raw.get("derived_from")
                if not isinstance(text, str) or not text.strip():
                    dropped.append(("shape", ""))
                    continue
                if not isinstance(derived, (list, tuple)) or not derived:
                    dropped.append(("no-derived-from", text.strip()[:40]))
                    continue
                if not all(isinstance(item, str) for item in derived):
                    dropped.append(("derived-unknown", text.strip()[:40]))
                    continue
                missing = [item for item in derived if item not in rows]
                if missing:
                    dropped.append(("derived-unknown", ", ".join(missing[:3])))
                    continue
                matched = _match_row(text, rows)
                if matched:
                    value = matched
                elif mode != MODE_DISTILL:
                    dropped.append(("text-not-in-pool", text.strip()[:40]))
                else:
                    bad = digit_mismatch(text, [rows[item].display for item in derived])
                    if bad is not None:
                        dropped.append(("digit-mismatch", bad))
                    else:
                        value = {
                            "text": normalize_pick_text(text.strip()),
                            "derived_from": list(derived),
                        }
        else:
            dropped.append(("shape", type(raw).__name__))

        if value is None:
            continue
        marker = json.dumps(value, ensure_ascii=False, sort_keys=True)
        if marker in seen:
            continue
        seen.add(marker)
        kept.append(value)

    if len(kept) > MAX_PICKS_PER_FRAME:
        kept = kept[:MAX_PICKS_PER_FRAME]
        dropped.append(("truncated", f">{MAX_PICKS_PER_FRAME}"))
    return kept, dropped


# ---------------------------------------------------------------- 写回


CHAPTER_ITEM_RE = re.compile(r"^(?P<pad>[ ]*)- chapter: (?P<value>.+?)\s*$")
FRAMES_KEY_RE = re.compile(r"^(?P<pad>[ ]*)frames:\s*$")
PICKS_KEY_RE = re.compile(r"^(?P<pad>[ ]*)(?P<dash>- )?picks:(?P<rest>.*)$")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _frame_items(
    lines: Sequence[str], frames_key: int, field_indent: int, stop: int
) -> List[Tuple[int, int]]:
    """``frames:`` 底下每个帧条目的（起行，止行）。止行不含在条目里。"""
    items: List[Tuple[int, int]] = []
    cursor = frames_key + 1
    while cursor < stop:
        line = lines[cursor]
        if not line.strip():
            cursor += 1
            continue
        if _indent(line) != field_indent or not line.lstrip().startswith("- "):
            break
        end = cursor + 1
        while end < stop:
            following = lines[end]
            if following.strip() and _indent(following) <= field_indent:
                break
            end += 1
        items.append((cursor, end))
        cursor = end
    return items


def _picks_region(
    lines: Sequence[str], item_start: int, item_end: int, field_indent: int
) -> Optional[Tuple[int, int, bool, str]]:
    """帧条目里 ``picks`` 那个键的（起行，止行，是不是 ``- picks:`` 行，缩进）。"""
    for cursor in range(item_start, item_end):
        matched = PICKS_KEY_RE.match(lines[cursor])
        if not matched:
            continue
        pad = matched.group("pad")
        key_column = len(pad) + (2 if matched.group("dash") else 0)
        if key_column != field_indent + 2:
            continue
        if matched.group("rest").strip():
            return (cursor, cursor + 1, bool(matched.group("dash")), pad)
        end = cursor + 1
        while end < item_end:
            following = lines[end]
            if (
                following.strip()
                and _indent(following) <= field_indent + 2
                and not following.lstrip().startswith("- ")
            ):
                break
            end += 1
        return (cursor, end, bool(matched.group("dash")), pad)
    return None


def _render_picks(pad: str, marker: bool, picks: Sequence[object], block_indent: int) -> List[str]:
    """一个 ``picks:`` 键换成新值后应有的文本行。"""
    head = pad + ("- " if marker else "") + "picks:"
    if not picks:
        return [head + " []"]
    dumped = yaml.safe_dump(
        list(picks),
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        width=10**6,
    )
    body = [((" " * block_indent) + line) for line in dumped.rstrip("\n").split("\n")]
    return [head] + body


def rewrite_picks(
    text: str, updates: Mapping[int, Mapping[int, Sequence[object]]]
) -> str:
    """只换指定帧的 ``picks`` 那几段文本，其余字节原样返回。

    ``updates`` 是"章在骨架里的次序 → 帧下标 → 新 picks"。帧条目、``picks`` 键的位置都在
    文本里现找（:func:`_frame_items`／:func:`_picks_region`），因此头部注释、每章
    ``budget`` 的行尾注释与 ``theme`` 块都不会被重排——写回之后这份文件与刚才那份的差别
    只在那几个 ``picks`` 值上。
    """
    lines = text.split("\n")
    starts = [index for index, line in enumerate(lines) if CHAPTER_ITEM_RE.match(line)]
    plan: List[Tuple[int, int, List[str]]] = []
    for ordinal, frame_updates in updates.items():
        if not frame_updates:
            continue
        if ordinal >= len(starts):
            raise SlidesError(
                f"cannot write picks back: the outline has {len(starts)} chapters, "
                f"chapter #{ordinal + 1} was requested"
            )
        start = starts[ordinal]
        stop = starts[ordinal + 1] if ordinal + 1 < len(starts) else len(lines)
        field_indent = len(CHAPTER_ITEM_RE.match(lines[start]).group("pad")) + 2
        frames_key = None
        for cursor in range(start + 1, stop):
            matched = FRAMES_KEY_RE.match(lines[cursor])
            if matched and len(matched.group("pad")) == field_indent:
                frames_key = cursor
                break
        if frames_key is None:
            raise SlidesError(
                f"cannot write picks back: chapter #{ordinal + 1} has no frames block"
            )
        items = _frame_items(lines, frames_key, field_indent, stop)
        for frame_index, picks in frame_updates.items():
            if frame_index >= len(items):
                raise SlidesError(
                    f"cannot write picks back: chapter #{ordinal + 1} has {len(items)} frames, "
                    f"frame {frame_index} was requested"
                )
            item_start, item_end = items[frame_index]
            region = _picks_region(lines, item_start, item_end, field_indent)
            if region is None:
                raise SlidesError(
                    f"cannot write picks back: chapter #{ordinal + 1} frame {frame_index} "
                    "has no picks key"
                )
            region_start, region_end, marker, pad = region
            plan.append(
                (
                    region_start,
                    region_end,
                    _render_picks(pad, marker, picks, field_indent + 2),
                )
            )
    for region_start, region_end, replacement in sorted(plan, key=lambda item: -item[0]):
        lines[region_start:region_end] = replacement
    return "\n".join(lines)


def verify_picks(text: str, updates: Mapping[int, Mapping[int, Sequence[object]]]) -> None:
    """把写回后的文本读回来核对目标帧的 picks；对不上就报错（调用方据此不写盘）。"""
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise SlidesError(f"written outline is not valid YAML: {exc}") from exc
    chapters = document.get("chapters") if isinstance(document, Mapping) else None
    if not isinstance(chapters, list):
        raise SlidesError("written outline has no chapters list")
    for ordinal, frame_updates in updates.items():
        frames = chapters[ordinal].get("frames") or []
        for frame_index, picks in frame_updates.items():
            actual = frames[frame_index].get("picks")
            if list(actual or []) != list(picks):
                raise SlidesError(
                    f"write-back check failed for chapter #{ordinal + 1} frame {frame_index}: "
                    f"wrote {picks!r}, read {actual!r}"
                )


# ---------------------------------------------------------------- 成本估算


def estimate_tokens(chars: int) -> int:
    """字符数 → token 的粗估（:data:`CHARS_PER_TOKEN`）。只用于下单前估钱。"""
    return int(chars / CHARS_PER_TOKEN) + 1


# ---------------------------------------------------------------- 结果


@dataclass(frozen=True)
class FrameRecord:
    """一帧的出处记录，落进 ``select.jsonl``。"""

    chapter: str
    frame: int
    mode: int
    hint: str
    picks: Tuple[object, ...]
    why: str
    outcome: str
    reason: str = ""
    usage: Tuple[int, int, int, int] = (0, 0, 0, 0)
    dropped: Tuple[Tuple[str, str], ...] = ()
    chars: int = 0
    retried: bool = False

    def as_dict(self, *, deck: str, endpoint: Endpoint, when: str) -> dict:
        return {
            "time": when,
            "deck": deck,
            "role": endpoint.role,
            "provider": endpoint.provider,
            "model": endpoint.model,
            "prompt_version": PROMPT_VERSION,
            "chapter": self.chapter,
            "frame": self.frame,
            "mode": self.mode,
            "hint": self.hint,
            "picks": list(self.picks),
            "why": self.why,
            # 这一帧选中的字数（与 check 的 slide-overflow 同一口径），以及整章因为容量
            # 重问过没有；两条都是"为什么这一页是这几句"的一部分。
            "chars": self.chars,
            "usage": {
                "requests": self.usage[0],
                "prompt_tokens": self.usage[1],
                "completion_tokens": self.usage[2],
                "reasoning_tokens": self.usage[3],
            },
            "outcome": self.outcome,
            "reason": self.reason,
            "retried": self.retried,
            # 丢掉的一条一条记下来（原因码 + 它自己）：容量兜底丢的句子绝不能只活在
            # 内存里，报告与这份日志都要能查出"少了哪几句、为什么"。
            "dropped": [{"code": code, "detail": detail} for code, detail in self.dropped],
            # 只记 scheme://host：URL 里可能嵌着凭据，端点身份到 host 就够了。
            "base_url": base_url_host(endpoint.base_url),
        }


@dataclass
class ChapterOutcome:
    """一章跑完的账：发了没有、成了没有、逐帧的结果。"""

    plan: ChapterPlan
    endpoint: Optional[Endpoint] = None
    error: Optional[str] = None
    usage: Tuple[int, int, int, int] = (0, 0, 0, 0)
    records: Tuple[FrameRecord, ...] = ()

    @property
    def failed(self) -> bool:
        return self.error is not None

    @property
    def filled(self) -> int:
        return sum(1 for record in self.records if record.outcome == OUTCOME_FILLED)


@dataclass
class SelectResult:
    """一次 ``select`` 的全部结果。"""

    outline_path: Path
    project_root: Path
    deck: str
    dry_run: bool
    plans: Tuple[ChapterPlan, ...]
    outcomes: Tuple[ChapterOutcome, ...]
    endpoints: Mapping[str, Endpoint] = field(default_factory=dict)
    written: bool = False
    log_path: Optional[Path] = None
    force: bool = False

    @property
    def failures(self) -> Tuple[ChapterOutcome, ...]:
        return tuple(outcome for outcome in self.outcomes if outcome.failed)

    @property
    def writable_frames(self) -> int:
        return sum(len(plan.writable) for plan in self.plans)


def base_url_host(url: str) -> str:
    """``base_url`` 记进日志时的写法：``scheme://host``（不含路径、查询与可能的凭据）。"""
    parts = urlsplit(url or "")
    if parts.scheme and parts.netloc:
        return f"{parts.scheme}://{parts.netloc}"
    return url or ""


# ---------------------------------------------------------------- 运行


def resolve_candidates_path(
    outline_path: Path, project_root: Path, deck: str, explicit: Optional[Path]
) -> Path:
    """候选表在哪：显式给的 → 骨架旁边那份 → ``<工程根>/_primer/slides/<deck>/``。"""
    if explicit is not None:
        path = Path(explicit).expanduser().resolve()
        if not path.is_file():
            raise SlidesError(f"candidate table not found: {path}")
        return path
    beside = outline_path.parent / CANDIDATES_NAME
    if beside.is_file():
        return beside
    from_root = project_root / "_primer" / "slides" / deck / CANDIDATES_NAME
    if from_root.is_file():
        return from_root
    raise SlidesError(
        f"candidate table not found: looked for {beside} and {from_root} "
        "(pass --candidates, or run outline first)"
    )


def _endpoint_for(
    role: str,
    config: Optional[Config],
    *,
    max_tokens: Optional[int],
) -> Endpoint:
    """解析角色；配置读不出来时抛 :class:`SlidesError`（消息英文）。"""
    if config is None:
        raise SlidesError(f"role {role} is needed but no configuration was loaded")
    try:
        return resolve_role(config, role, max_tokens=max_tokens)
    except ConfigError as exc:
        raise SlidesError(str(exc)) from exc


def _ask(
    client: ChatClient, prompt: str
) -> Tuple[List[FrameReply], Optional[str]]:
    """发一次请求（含重试），返回（逐帧答复，最后一次的错误）。"""
    error: Optional[str] = None
    for _ in range(MAX_RETRIES + 1):
        try:
            reply = client.complete(prompt)
        except SelectCallError as failure:
            error = str(failure)
            continue
        try:
            payload = parse_json_reply(reply.content)
        except SelectReplyError as failure:
            error = str(failure)
            continue
        error = None
        return parse_frames_reply(payload), None
    return [], error


@dataclass(frozen=True)
class Dispatch:
    """一章问下来的经过：逐帧答复、错误、第一次超容量的帧、重问过没有。"""

    replies: Tuple[FrameReply, ...] = ()
    error: Optional[str] = None
    retried: bool = False
    overflow: Tuple[Tuple[int, int], ...] = ()


def overflowing_frames(
    plan: ChapterPlan, replies: Sequence[FrameReply]
) -> List[Tuple[int, int]]:
    """回信里超了容量的可写帧：（帧下标，这一帧的字数），按帧序。

    跳过的帧、没回信的帧、形状读不出来的 picks 都不算——只有真会写进骨架的句子才占容量。
    """
    rows = {row.id: row for row in plan.rows}
    lengths = pick_lengths(rows)
    by_index = {reply.index: reply for reply in replies}
    out: List[Tuple[int, int]] = []
    for frame in plan.writable:
        reply = by_index.get(frame.index)
        if reply is None:
            continue
        kept, _ = clean_picks(reply.picks, rows, frame.mode)
        total = frame_chars(kept, lengths)
        if total > plan.capacity:
            out.append((frame.index, total))
    return out


def ask_chapter(client: ChatClient, plan: ChapterPlan) -> Dispatch:
    """问一章：第一次回信有帧超容量就整章重问一次（只一次），并把超容量的帧点出来。

    重问不是把第一封原样重发，而是接在原提示词后面追加"上一版第 N 帧超出容量 X 字"——
    模型看得到自己上一版超在哪一帧、超了多少。两次请求都计入用量（调用方按快照取增量）。

    重问也失败（端点不通或回信不成形）时整章记失败：这一章的结果要么是模型自己压进容量的，
    要么是工具按容量丢过的，不存在"带病写回"。第一版答复就此作废，不静默采用。
    """
    replies, error = _ask(client, plan.prompt)
    if error is not None:
        return Dispatch(error=error)
    overflow = overflowing_frames(plan, replies)
    if not overflow:
        return Dispatch(replies=tuple(replies))
    prompt = plan.prompt + "\n".join(overflow_lines(overflow, plan.capacity)) + "\n"
    again, error = _ask(client, prompt)
    if error is not None:
        return Dispatch(
            error=f"capacity retry failed: {error}", retried=True, overflow=tuple(overflow)
        )
    return Dispatch(replies=tuple(again), retried=True, overflow=tuple(overflow))


def run(
    outline_path: Path,
    project_root: Path,
    *,
    candidates_path: Optional[Path] = None,
    only: Sequence[str] = (),
    limit: Optional[int] = None,
    mode: Optional[int] = None,
    force: bool = False,
    redo: bool = False,
    dry_run: bool = False,
    max_tokens: Optional[int] = None,
    timeout: Optional[float] = None,
    transport: Optional[Transport] = None,
    environ: Optional[Mapping[str, str]] = None,
    config_path: Optional[Path] = None,
) -> SelectResult:
    """跑一次 ``select``：计划 → （真跑时）逐章请求 → 写回 → 记账。

    一章失败不影响其它章；失败章在 :attr:`SelectResult.failures` 里点名，退出码由调用方
    据此取非零。``--dry-run`` 只做计划与估算，一个字节都不写，也不读密钥。
    """
    outline = Path(outline_path).expanduser().resolve()
    if mode is not None and mode not in MODE_CHOICES:
        raise SlidesError(f"unknown --mode: {mode}")
    if not outline.is_file():
        raise SlidesError(f"outline not found: {outline}")
    root = Path(project_root).expanduser().resolve()
    document = load_outline_document(outline)
    deck_block = document.get("deck")
    deck_block = deck_block if isinstance(deck_block, Mapping) else {}
    deck = str(deck_block.get("deck") or DEFAULT_DECK)
    rows_path = resolve_candidates_path(outline, root, deck, candidates_path)
    rows = cand.parse_markdown_table(rows_path.read_text(encoding="utf-8"))

    picked = select_chapters(document, only, limit)
    capacity = capacity_per_page(document)
    plans = tuple(
        plan_chapter(
            ordinal, chapter, rows, capacity=capacity, forced=mode, force=force, redo=redo
        )
        for ordinal, chapter in picked
    )

    env: Mapping[str, str] = os.environ if environ is None else environ
    needs_request = any(plan.writable for plan in plans)
    endpoints: Dict[str, Endpoint] = {}
    if needs_request:
        try:
            config: Optional[Config] = load_config(root, config_path, environ=env)
        except ConfigError as exc:
            raise SlidesError(str(exc)) from exc
        for role in sorted({plan.role for plan in plans if plan.writable}):
            endpoints[role] = _endpoint_for(role, config, max_tokens=max_tokens)

    if dry_run:
        return SelectResult(
            outline_path=outline,
            project_root=root,
            deck=deck,
            dry_run=True,
            plans=plans,
            outcomes=(),
            endpoints=endpoints,
            force=force,
        )

    clients: Dict[str, ChatClient] = {}
    outcomes: List[ChapterOutcome] = []
    updates: Dict[int, Dict[int, Sequence[object]]] = {}
    for plan in plans:
        if not plan.writable:
            outcomes.append(
                ChapterOutcome(plan=plan, endpoint=endpoints.get(plan.role), records=_skip_records(plan))
            )
            continue
        endpoint = endpoints[plan.role]
        if plan.role not in clients:
            try:
                key = api_key(endpoint, env)
            except ConfigError as exc:
                raise SlidesError(str(exc)) from exc
            clients[plan.role] = ChatClient(
                base_url=endpoint.base_url,
                model=endpoint.model,
                key=key,
                transport=transport,
                timeout=timeout if timeout is not None else HTTP_TIMEOUT,
                max_tokens=max_tokens or endpoint.max_tokens or DEFAULT_MAX_TOKENS,
                temperature=endpoint.temperature,
            )
        client = clients[plan.role]
        snapshot = client.usage.snapshot()
        dispatch = ask_chapter(client, plan)
        delta_usage = delta(client.usage, snapshot)
        if dispatch.error is not None:
            outcomes.append(
                ChapterOutcome(
                    plan=plan,
                    endpoint=endpoint,
                    error=dispatch.error,
                    usage=delta_usage,
                    records=_failed_records(plan, dispatch.error, delta_usage),
                )
            )
            continue
        records, frame_updates = _fill_records(
            plan,
            dispatch.replies,
            delta_usage,
            retried=dispatch.retried,
            overflow=dispatch.overflow,
        )
        if frame_updates:
            updates[plan.ordinal] = frame_updates
        outcomes.append(
            ChapterOutcome(plan=plan, endpoint=endpoint, usage=delta_usage, records=records)
        )

    result = SelectResult(
        outline_path=outline,
        project_root=root,
        deck=deck,
        dry_run=False,
        plans=plans,
        outcomes=tuple(outcomes),
        endpoints=endpoints,
        force=force,
    )
    log_path = outline.parent / SELECT_LOG_NAME
    _append_log(log_path, result)
    result.log_path = log_path
    if updates:
        text = outline.read_text(encoding="utf-8")
        new_text = rewrite_picks(text, updates)
        verify_picks(new_text, updates)
        outline.write_text(new_text, encoding="utf-8")
        result.written = True
    return result


def _skip_records(plan: ChapterPlan) -> Tuple[FrameRecord, ...]:
    """整章没有可写帧时的逐帧记录（全是跳过）。"""
    return tuple(
        FrameRecord(
            chapter=plan.number,
            frame=frame.index,
            mode=frame.mode,
            hint=frame.hint,
            picks=(),
            why="",
            outcome=OUTCOME_SKIPPED,
            reason=frame.skip,
        )
        for frame in plan.frames
    )


def _failed_records(
    plan: ChapterPlan, error: str, usage: Tuple[int, int, int, int]
) -> Tuple[FrameRecord, ...]:
    """一章失败：它的可写帧全部记 failed，用量记在第一帧上（那一次请求因它而发）。"""
    records: List[FrameRecord] = []
    first = True
    for frame in plan.frames:
        if not frame.writable:
            records.append(
                FrameRecord(
                    chapter=plan.number,
                    frame=frame.index,
                    mode=frame.mode,
                    hint=frame.hint,
                    picks=(),
                    why="",
                    outcome=OUTCOME_SKIPPED,
                    reason=frame.skip,
                )
            )
            continue
        records.append(
            FrameRecord(
                chapter=plan.number,
                frame=frame.index,
                mode=frame.mode,
                hint=frame.hint,
                picks=(),
                why="",
                outcome=OUTCOME_FAILED,
                reason=error,
                usage=usage if first else (0, 0, 0, 0),
            )
        )
        first = False
    return tuple(records)


def _fill_records(
    plan: ChapterPlan,
    replies: Sequence[FrameReply],
    usage: Tuple[int, int, int, int],
    *,
    retried: bool = False,
    overflow: Sequence[Tuple[int, int]] = (),
) -> Tuple[Tuple[FrameRecord, ...], Dict[int, Sequence[object]]]:
    """把逐帧答复收成记录与写回计划。用量记在触发请求的第一帧上。

    ``retried`` 说这一章因为容量重问过一次（两次请求的用量都在 ``usage`` 里），
    ``overflow`` 是第一次回信超了容量的帧——重问是由它们引起的，记录里标出来，报告的
    "容量对账"据此说明这一帧为什么重来过。

    重问之后留下的句子**仍超容量**时在这里确定性地丢（:func:`trim_to_capacity`），
    丢掉的进 ``dropped``：报告与 ``select.jsonl`` 里都能查出少了哪几句。
    """
    rows = {row.id: row for row in plan.rows}
    lengths = pick_lengths(rows)
    retried_frames = {index for index, _ in overflow}
    by_index = {reply.index: reply for reply in replies}
    records: List[FrameRecord] = []
    updates: Dict[int, Sequence[object]] = {}
    first_writable = True
    for frame in plan.frames:
        if not frame.writable:
            records.append(
                FrameRecord(
                    chapter=plan.number,
                    frame=frame.index,
                    mode=frame.mode,
                    hint=frame.hint,
                    picks=(),
                    why="",
                    outcome=OUTCOME_SKIPPED,
                    reason=frame.skip,
                )
            )
            continue
        record_usage = usage if first_writable else (0, 0, 0, 0)
        first_writable = False
        frame_retried = retried and frame.index in retried_frames
        reply = by_index.get(frame.index)
        if reply is None:
            records.append(
                FrameRecord(
                    chapter=plan.number,
                    frame=frame.index,
                    mode=frame.mode,
                    hint=frame.hint,
                    picks=(),
                    why="",
                    outcome=OUTCOME_EMPTY,
                    reason="the reply has no entry for this frame",
                    usage=record_usage,
                    retried=frame_retried,
                )
            )
            continue
        kept, dropped = clean_picks(reply.picks, rows, frame.mode)
        dropped = list(dropped)
        if kept and frame_chars(kept, lengths) > plan.capacity:
            kept, extra = trim_to_capacity(kept, lengths, plan.capacity)
            dropped.extend(extra)
        if not kept:
            records.append(
                FrameRecord(
                    chapter=plan.number,
                    frame=frame.index,
                    mode=frame.mode,
                    hint=frame.hint,
                    picks=(),
                    why=reply.why,
                    outcome=OUTCOME_DROPPED,
                    reason=", ".join(sorted({code for code, _ in dropped})) or "no picks returned",
                    usage=record_usage,
                    dropped=tuple(dropped),
                    retried=frame_retried,
                )
            )
            continue
        chars = frame_chars(kept, lengths)
        updates[frame.index] = kept
        records.append(
            FrameRecord(
                chapter=plan.number,
                frame=frame.index,
                mode=frame.mode,
                hint=frame.hint,
                picks=tuple(kept),
                why=reply.why,
                outcome=OUTCOME_FILLED,
                reason=", ".join(sorted({code for code, _ in dropped})),
                usage=record_usage,
                dropped=tuple(dropped),
                chars=chars,
                retried=frame_retried,
            )
        )
    return tuple(records), updates


def _append_log(path: Path, result: SelectResult) -> None:
    """把每一帧的来源追加进 ``select.jsonl``。密钥不进这份文件（见 :func:`base_url_host`）。"""
    when = datetime.now().astimezone().isoformat(timespec="seconds")
    lines: List[str] = []
    for outcome in result.outcomes:
        endpoint = outcome.endpoint or _fallback_endpoint(outcome.plan.role)
        for record in outcome.records:
            lines.append(
                json.dumps(
                    record.as_dict(deck=result.deck, endpoint=endpoint, when=when),
                    ensure_ascii=False,
                    sort_keys=False,
                )
            )
    if not lines:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for line in lines:
            handle.write(line + "\n")


def _fallback_endpoint(role: str) -> Endpoint:
    """没有解析出端点时（整章没有可写帧）日志里的占位记录。"""
    return Endpoint(role=role, provider="", base_url="", model="", key_env="")


# ---------------------------------------------------------------- 报告


def report_lines(result: SelectResult) -> Tuple[str, ...]:
    """终端报告：模式分布、逐章账目、容量对账、跳过与失败、用量。全部中文散文。"""
    lines: List[str] = [
        f"outline: {relative_to_root(result.outline_path, result.project_root)}",
        f"deck: {result.deck}",
    ]
    if result.force:
        lines.append(
            "注意：--force 已开——除 review == ok 的帧外全部重写，"
            "**包括已经填过 picks 的帧**（它们的原 picks 会被这次结果覆盖）"
        )
    modes: Dict[int, int] = {}
    for plan in result.plans:
        for frame in plan.frames:
            if frame.writable:
                modes[frame.mode] = modes.get(frame.mode, 0) + 1
    mode_text = "、".join(
        f"模式 {mode} {MODE_NAMES[mode]} {modes[mode]} 帧" for mode in sorted(modes)
    )
    lines.append(f"可写帧的模式：{mode_text or '（没有可写帧）'}")
    if result.endpoints:
        for role in sorted(result.endpoints):
            endpoint = result.endpoints[role]
            lines.append(
                f"端点 [{role}]：{endpoint.provider}/{endpoint.model}"
                f"（temperature {endpoint.temperature if endpoint.temperature is not None else 0}）"
            )
    else:
        lines.append("端点：本次不会发请求")

    if result.dry_run:
        lines.extend(_dry_run_lines(result))
        return tuple(lines)

    lines.extend(["", f"{'章':<4}{'帧':>4}{'可写':>6}{'已填':>6}{'跳过':>6}  结果"])
    for outcome in result.outcomes:
        plan = outcome.plan
        skipped = len(plan.skipped)
        status = "失败" if outcome.failed else "完成"
        lines.append(
            f"{plan.label:<4}{plan.budget:>4}{len(plan.writable):>6}"
            f"{outcome.filled:>6}{skipped:>6}  {status}"
        )
    failures = result.failures
    if failures:
        lines.append("")
        lines.append("失败的章（其余章不受影响，填好的已写回）：")
        for outcome in failures:
            lines.append(f"  {outcome.plan.label}：{outcome.error}")
    lines.extend(["", *capacity_lines(result)])
    drops: Dict[str, int] = {}
    for outcome in result.outcomes:
        for record in outcome.records:
            for code, _ in record.dropped:
                drops[code] = drops.get(code, 0) + 1
    if drops:
        lines.append("")
        lines.append("丢弃的 picks（模型回过、但校验不过或放不下的，一条也没写进骨架）：")
        for code in sorted(drops):
            lines.append(f"  {DROP_LABELS.get(code, code)}：{drops[code]} 条")
    totals = _totals(result)
    lines.append("")
    lines.append(
        f"用量：请求 {totals[0]}，输入 {totals[1]} token，输出 {totals[2]} token，"
        f"推理 {totals[3]} token"
    )
    lines.append(f"写回：{'outline.yaml 已更新' if result.written else '没有帧要改，未写盘'}")
    if result.log_path is not None:
        lines.append(f"出处：{relative_to_root(result.log_path, result.project_root)}")
    return tuple(lines)


def capacity_lines(result: SelectResult) -> List[str]:
    """容量对账：逐帧写出这一帧选中的字数与容量、重问过没有、有没有按容量丢句。

    只列**这次真动过的**帧（填过、丢过、没回信）：跳过的帧一句没选，不占容量。一段话里
    把"为什么这一页是这几句、少了哪几句"讲清楚——容量兜底丢句子时，这一节是唯一的交代。
    """
    values = sorted({plan.capacity for plan in result.plans})
    head = "、".join(str(value) for value in values) if values else "?"
    lines: List[str] = [
        f"容量对账（capacity_per_page {head} 字/页，字数是这一帧所有句子的字数之和）："
    ]
    listed = 0
    for outcome in result.outcomes:
        for record in outcome.records:
            if record.outcome == OUTCOME_SKIPPED:
                continue
            notes: List[str] = []
            if record.retried:
                notes.append("第一次回信超容量，整章重问过一次")
            if record.outcome == OUTCOME_EMPTY:
                notes.append("模型没回这一帧，没有可选")
            gone = [detail for code, detail in record.dropped if code == CAPACITY_DROP]
            if gone:
                notes.append("放不下，按字数从大到小丢了 " + "、".join(gone))
            tail = "；" + "；".join(notes) if notes else ""
            lines.append(
                f"  {outcome.plan.label} 第 {record.frame + 1} 页：{record.chars} 字 / "
                f"容量 {outcome.plan.capacity} 字{tail}"
            )
            listed += 1
    if not listed:
        lines.append("  （这次没有真动过的帧，也就没有容量上的取舍）")
    return lines


def _dry_run_lines(result: SelectResult) -> Tuple[str, ...]:
    """``--dry-run`` 的报告：会发几次、每次多长、哪些帧可写、跳过的按原因分。"""
    lines: List[str] = ["", "--dry-run：一个字节都不写（也不读密钥）"]
    for value in sorted({plan.capacity for plan in result.plans}):
        lines.append(
            f"  每帧容量 {value} 字（capacity_per_page）：超了这一章会重问一次，"
            "重问仍超就按字数从大到小丢到放得下（每次重问都是一次请求）"
        )
    requests = 0
    total_chars = 0
    for plan in result.plans:
        if not plan.writable:
            continue
        requests += 1
        chars = len(plan.prompt)
        total_chars += chars
        lines.append(
            f"  {plan.label}（{plan.number}）：{len(plan.writable)} 帧，"
            f"prompt {chars} 字（估 {estimate_tokens(chars)} token）"
        )
    writable = result.writable_frames
    lines.append(
        f"  合计：会发 {requests} 次请求（一章一次），prompt {total_chars} 字"
        f"（估 {estimate_tokens(total_chars)} token）"
    )
    lines.append(
        f"  输出估算：{writable} 帧 × 默认 {DEFAULT_PICKS_PER_FRAME} 条 × "
        f"约 {TOKENS_PER_PICK} token ≈ {writable * DEFAULT_PICKS_PER_FRAME * TOKENS_PER_PICK} token"
    )
    lines.append(f"  可写帧 {writable}，跳过帧 {sum(len(plan.skipped) for plan in result.plans)}：")
    counts: Dict[str, int] = {}
    for plan in result.plans:
        for frame in plan.skipped:
            counts[frame.skip] = counts.get(frame.skip, 0) + 1
    for code in sorted(counts):
        lines.append(f"    {SKIP_LABELS.get(code, code)}：{counts[code]} 帧")
    if not counts:
        lines.append("    （没有跳过的帧）")
    return tuple(lines)


def _totals(result: SelectResult) -> Tuple[int, int, int, int]:
    requests = sum(outcome.usage[0] for outcome in result.outcomes)
    prompt = sum(outcome.usage[1] for outcome in result.outcomes)
    completion = sum(outcome.usage[2] for outcome in result.outcomes)
    reasoning = sum(outcome.usage[3] for outcome in result.outcomes)
    return (requests, prompt, completion, reasoning)


__all__ = [
    "CAPACITY_DROP",
    "CHAPTER_ITEM_RE",
    "DEFAULT_PICKS_PER_FRAME",
    "ChapterOutcome",
    "ChapterPlan",
    "Dispatch",
    "FrameMode",
    "FramePlan",
    "FrameRecord",
    "FrameReply",
    "MAX_PICKS_PER_FRAME",
    "MAX_RETRIES",
    "MIN_PICKS_PER_FRAME",
    "MODE_CHOICES",
    "MODE_DISTILL",
    "MODE_HINT",
    "MODE_JUDGE",
    "MODE_NAMES",
    "PROMPT_VERSION",
    "ROLE_DISTILL",
    "ROLE_SELECT",
    "SELECT_LOG_NAME",
    "SelectResult",
    "ask_chapter",
    "base_url_host",
    "build_prompt",
    "capacity_lines",
    "chapter_rows",
    "clean_picks",
    "digit_mismatch",
    "estimate_tokens",
    "frame_chars",
    "frame_mode",
    "frame_section",
    "frame_writable",
    "overflow_lines",
    "overflowing_frames",
    "parse_frames_reply",
    "pick_lengths",
    "pick_name",
    "plan_chapter",
    "report_lines",
    "rewrite_picks",
    "run",
    "select_chapters",
    "trim_to_capacity",
    "verify_picks",
]
