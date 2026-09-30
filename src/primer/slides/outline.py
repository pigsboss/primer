# -*- coding: utf-8 -*-
"""``outline`` 的把关：读规格 → 读源文件算结构 → 建议预算 → 抽候选 → 推容量 → 落两件产物。

分页只依赖 :class:`primer.slides.plan.DeckConfig` 与各章的**候选材料**（默认预算按材料占比
给建议，人改即成事实），候选只依赖结构本身，容量与取舍依赖前两者（页数给分母、候选长度给
分子），所以每一项都能单独测。落盘位置固定为 ``<工程根>/_primer/slides/<deck>/``，文件只有
``candidates.md`` 与 ``outline.yaml`` 两个；规格 ``deck.yaml`` 由人写在同一个目录里，工具只读
不写。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

import yaml

from ..paths import output_dir_for, relative_to_root
from . import candidates as cand
from .fingerprint import compute_fingerprint
from .plan import (
    CAPACITY_FROM_THEME,
    DEFAULT_REVIEW,
    Capacity,
    DeckConfig,
    SlidesError,
    capacity_report,
    default_budgets as suggest_budgets,
    frame_entry,
    frame_slots,
    resolve_capacity,
)
from .render import build_candidates_md, build_outline_document, dump_outline
from .spec import DeckSpec, find_spec, load_spec
from .structure import DeckStructure, read_structure
from .terminal import clip, pad
from .theme import Theme, default_theme, read_theme, type_listing

DEFAULT_DECK = "review-seminar"
CANDIDATES_NAME = "candidates.md"
OUTLINE_NAME = "outline.yaml"
# 终端分配表的章名列宽（按显示宽度算，一个汉字占两列）。
CHAPTER_COLUMN = 42


@dataclass(frozen=True)
class SlidePlan:
    """一次 ``outline`` 的全部结果：结构、参数、逐章页数、容量与取舍、候选、指纹、主题。"""

    structure: DeckStructure
    config: DeckConfig
    deck: str
    budgets: Tuple[int, ...]
    default_budgets: Tuple[int, ...]
    capacity: Capacity
    by_chapter: Mapping[str, Tuple[cand.Candidate, ...]]
    fingerprint: Optional[Mapping[str, object]] = None
    merge_notes: Tuple[str, ...] = ()
    theme: Theme = field(default_factory=default_theme)
    # 容量是显式给的还是按主题算出来的（``CAPACITY_FROM_THEME`` / ``CAPACITY_EXPLICIT``），
    # 报告里要如实说出它的来路。
    capacity_source: str = CAPACITY_FROM_THEME

    @property
    def candidate_total(self) -> int:
        return sum(len(items) for items in self.by_chapter.values())

    @property
    def candidate_chars(self) -> Tuple[int, ...]:
        """逐章的候选材料量（字）——默认预算就是按它算的。"""
        return tuple(
            sum(item.length for item in self.by_chapter.get(chapter.number, ()))
            for chapter in self.structure.chapters
        )

    @property
    def candidate_lengths(self) -> Tuple[int, ...]:
        """全部候选句的字数（容量与取舍按它算）。"""
        return tuple(
            item.length for items in self.by_chapter.values() for item in items
        )

    @property
    def body_pages(self) -> int:
        """正文页数 = 各章预算之和（派生，不是控制项）。"""
        return sum(self.budgets)

    @property
    def total_pages(self) -> int:
        return self.body_pages + self.config.overhead

    @property
    def human_budgets(self) -> int:
        """有几章的预算与默认建议不同（= 人工定过的）。"""
        return sum(
            1
            for budget, default in zip(self.budgets, self.default_budgets)
            if budget != default
        )


def build_plan(
    spec: DeckSpec,
    config: DeckConfig,
    deck: str = DEFAULT_DECK,
    theme: Optional[Theme] = None,
) -> SlidePlan:
    """算出整份计划：规格与参数进来，结构、页数、候选、容量与取舍、指纹出去。

    ``config.capacity_per_page`` 允许是 ``None``（"按主题算"）：这里用 ``theme``
    （缺省是默认主题）把它落成具体数字，报告与产物拿到的因此是同一个解析后的值。默认预算
    也在这之后算——"把全部候选材料展示完要几页"里的"一页"就是它。
    """
    structure = read_structure(spec)
    theme = theme or default_theme()
    config, capacity_source = resolve_capacity(config, theme)
    by_chapter = {
        number: tuple(items) for number, items in cand.extract_all(structure.chapters).items()
    }
    materials = [
        sum(item.length for item in by_chapter.get(chapter.number, ()))
        for chapter in structure.chapters
    ]
    budgets, _body = suggest_budgets(
        materials, config.capacity_per_page, config.min_per_chapter, config.max_per_chapter
    )
    lengths = [item.length for items in by_chapter.values() for item in items]
    return SlidePlan(
        structure=structure,
        config=config,
        deck=deck,
        budgets=budgets,
        default_budgets=budgets,
        capacity=capacity_report(config, sum(budgets), lengths, theme),
        by_chapter=by_chapter,
        fingerprint=compute_fingerprint(spec, structure),
        theme=theme,
        capacity_source=capacity_source,
    )


def deck_dir(project_root: Path, deck: str = DEFAULT_DECK) -> Path:
    """产物目录 ``<工程根>/_primer/slides/<deck>/``。"""
    if not deck or "/" in deck or deck in (".", ".."):
        raise SlidesError(f"invalid deck name: {deck!r}")
    return output_dir_for(project_root, "slides") / deck


def resolve_spec(
    project_root: Path, deck: str = DEFAULT_DECK, spec_path: Optional[Path] = None
) -> DeckSpec:
    """读进这次要用的规格：显式给的 → ``<工程根>/_primer/slides/<deck>/deck.yaml``。"""
    root = Path(project_root).expanduser().resolve()
    if not root.is_dir():
        raise SlidesError(f"project root is not a directory: {root}")
    path = Path(spec_path).expanduser() if spec_path else find_spec(root, deck)
    return load_spec(path, root)


def load_outline_document(path: Path) -> Mapping[str, object]:
    """读回已存在的 ``outline.yaml``（供 ``--merge`` 取人改过的字段）。"""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise SlidesError(f"existing outline is not valid YAML: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise SlidesError(f"existing outline is not a YAML mapping: {path}")
    return raw


def merge_document(
    fresh: Mapping[str, object], old: Mapping[str, object]
) -> Tuple[Mapping[str, object], Tuple[str, ...]]:
    """把旧骨架里**人改过的**几处叠回新骨架上，返回（合并后的骨架，说明）。

    保留的是章一级的 ``budget``、``hint``、每帧的 ``picks``／``review``／``note``、每个
    ``sections[].speak``／``hint``、``figure`` 与 ``notes``，固定页逐页的 ``picks``、
    ``figure``、``notes``（以及没换过套路的那些页的 ``label``），以及整块 ``theme``；其余
    （章号、节清单、候选数、默认预算、容量、指纹、控制参数）一律以本次算出为准。章按短号
    配对，固定页按**种类 + 同类里的第几页**配对（页序由模型定，种类与同类序号才是身份——
    目录从三页并成一页时，位置配对会把封面的句子挪到主旨页上去）；旧骨架里已经没有的章丢掉，
    新书里新出现的章带上默认预算加进来，两者都在说明里点名。

    ``frames`` 按三种情形落到新骨架上，长度恒等于该章的有效预算：

    * 旧骨架有逐帧的 ``frames``（合并过一次的新骨架）→ 逐帧照原样搬过来（多出来的截掉、
      不够的补空帧），于是连跑两次 ``--merge`` 第二次与第一次逐字节相同；
    * 旧骨架只有章一级平铺的 ``picks``（迁移前的老骨架）→ 按均衡分法
      （:func:`primer.slides.plan.frame_slots`）摊进各帧，**一条不丢**，旧字段随之消失；
    * 两者都没有 → 全空帧。

    ``theme`` 整块缺失时（旧骨架是这次改动之前生成的）用默认值填上——默认值就在本次
    算出的 ``fresh`` 里，人不必自己去补。
    """
    old_chapters = {
        str(chapter.get("chapter")): chapter
        for chapter in old.get("chapters") or []
        if isinstance(chapter, Mapping)
    }
    new_chapters = [
        chapter for chapter in fresh.get("chapters") or [] if isinstance(chapter, Mapping)
    ]
    new_numbers = {str(chapter.get("chapter")) for chapter in new_chapters}

    merged: List[Mapping[str, object]] = []
    notes: List[str] = []
    kept_budget = kept_speak = kept_picks = 0
    frames_carried = frames_migrated = frames_defaulted = 0
    for chapter in new_chapters:
        number = str(chapter.get("chapter"))
        entry = dict(chapter)
        previous = old_chapters.get(number)
        if previous is None:
            notes.append(
                f"merge: chapter {chapter.get('label') or number} is new in the book; "
                f"added with its default budget {chapter.get('budget')}"
            )
            merged.append(entry)
            continue
        budget = entry.get("budget", 0)
        if isinstance(previous.get("budget"), int) and not isinstance(previous["budget"], bool):
            budget = previous["budget"]
            kept_budget += 1
        entry["budget"] = budget
        if isinstance(previous.get("hint"), str):
            entry["hint"] = previous["hint"]
        previous_frames = previous.get("frames")
        previous_picks = previous.get("picks")
        if isinstance(previous_frames, list) and previous_frames:
            carried = _carry_frames(previous_frames, budget)
            entry["frames"] = carried
            kept_picks += sum(len(frame["picks"]) for frame in carried)
            frames_carried += 1
        elif isinstance(previous_picks, list):
            entry["frames"] = _migrate_frames(previous_picks, budget)
            kept_picks += len(previous_picks)
            frames_migrated += 1
        else:
            entry["frames"] = _carry_frames(previous_frames, budget)
            frames_defaulted += 1
        for field in ("figure", "notes"):
            if field in previous:
                entry[field] = previous[field]
        speak = {
            str(section.get("number")): section.get("speak")
            for section in previous.get("sections") or []
            if isinstance(section, Mapping)
        }
        section_hints = {
            str(section.get("number")): section.get("hint")
            for section in previous.get("sections") or []
            if isinstance(section, Mapping)
        }
        sections: List[Mapping[str, object]] = []
        for section in chapter.get("sections") or []:
            updated = dict(section)
            key = str(section.get("number"))
            flag = speak.get(key)
            if isinstance(flag, bool):
                updated["speak"] = flag
                kept_speak += 1
            hint = section_hints.get(key)
            if isinstance(hint, str):
                updated["hint"] = hint
            sections.append(updated)
        entry["sections"] = sections
        merged.append(entry)

    for number, previous in old_chapters.items():
        if number not in new_numbers:
            notes.append(
                f"merge: chapter {previous.get('label') or number} dropped (no longer in the book)"
            )
    notes.insert(
        0,
        f"merge: kept budget {kept_budget}, speak flags {kept_speak}, picks {kept_picks}",
    )
    notes.insert(
        1,
        f"merge: frames carried over {frames_carried}, migrated from flattened picks "
        f"{frames_migrated}, defaulted {frames_defaulted}",
    )
    document = dict(fresh)
    document["chapters"] = merged
    pages, page_notes = _merge_pages(fresh.get("pages"), old.get("pages"))
    document["pages"] = pages
    notes.extend(page_notes)
    notes.extend(_merge_theme(document, old))
    return document, tuple(notes)


def _migrate_frames(picks: Sequence[object], budget: int) -> List[Mapping[str, object]]:
    """旧骨架章一级的平铺 ``picks`` → 逐帧 ``frames``，按均衡分法切，一条不丢。"""
    frames: List[Mapping[str, object]] = []
    offset = 0
    for count in frame_slots(len(picks), budget):
        frames.append(dict(frame_entry(picks[offset : offset + count])))
        offset += count
    return frames


def _carry_frames(previous: object, budget: int) -> List[Mapping[str, object]]:
    """逐帧的 ``picks``／``review``／``note`` 搬到预算长度的新帧表上（多截少补）。"""
    old = (
        [frame for frame in previous if isinstance(frame, Mapping)]
        if isinstance(previous, list)
        else []
    )
    frames: List[Mapping[str, object]] = []
    for index in range(max(budget, 0)):
        if index >= len(old):
            frames.append(dict(frame_entry()))
            continue
        entry = old[index]
        picks = entry.get("picks")
        note = entry.get("note")
        frames.append(
            dict(
                frame_entry(
                    picks if isinstance(picks, list) else [],
                    entry.get("review", DEFAULT_REVIEW),
                    note if isinstance(note, str) else "",
                )
            )
        )
    return frames


def _merge_theme(
    document: dict, old: Mapping[str, object]
) -> List[str]:
    """``theme`` 整块保留人改过的值；旧骨架里没有这一块就用默认值（已在 ``document`` 里）。"""
    previous = old.get("theme")
    if isinstance(previous, Mapping):
        document["theme"] = dict(previous)
        return ["merge: kept theme (canvas, type scale, palette)"]
    return ["merge: no theme block in the old outline; filled in the defaults"]


def _merge_pages(
    fresh: object, old: object
) -> Tuple[List[Mapping[str, object]], List[str]]:
    """固定页逐页合并：按**种类 + 同类里的第几页**配对，保留人填的 picks／figure／notes。

    位置配对在"目录从三页并成一页"这种改动上会把句子的归属挪错地方，所以身份用（种类，
    同类序号）。``label`` 只在同类页数没变时保留——页数变了，那一套标签就是旧套路的
    遗留（结构图／阅读路径／章节索引），该换成新的默认标签。

    旧骨架若是"每条只记页数"的写法，逐页没有可保留的东西，整块以新清单为准。
    """
    new_pages = [page for page in fresh or [] if isinstance(page, Mapping)]
    old_pages = [page for page in old or [] if isinstance(page, Mapping)]
    by_kind: Dict[str, List[Mapping[str, object]]] = {}
    for page in old_pages:
        by_kind.setdefault(str(page.get("kind") or ""), []).append(page)
    new_counts: Dict[str, int] = {}
    for page in new_pages:
        kind = str(page.get("kind") or "")
        new_counts[kind] = new_counts.get(kind, 0) + 1

    merged: List[Mapping[str, object]] = []
    kept = 0
    for page in new_pages:
        entry = dict(page)
        kind = str(page.get("kind") or "")
        previous = by_kind.get(kind, [])
        index = len([item for item in merged if str(item.get("kind") or "") == kind])
        same_shape = len(previous) == new_counts.get(kind, 0)
        if index < len(previous):
            old_page = previous[index]
            for name in ("picks", "figure", "notes"):
                if name in old_page:
                    entry[name] = old_page[name]
            if same_shape and "label" in old_page:
                entry["label"] = old_page["label"]
            if old_page.get("picks"):
                kept += len(old_page["picks"])
        merged.append(entry)
    notes = [f"merge: kept fixed-page picks/figure/notes, picks {kept}"]
    return merged, notes


def effective_budgets(document: Mapping[str, object]) -> Tuple[int, ...]:
    """骨架里各章**生效**的预算（``--merge`` 之后以文件里的人改值为准）。"""
    budgets: List[int] = []
    for chapter in document.get("chapters") or []:
        if not isinstance(chapter, Mapping):
            continue
        value = chapter.get("budget")
        budgets.append(value if isinstance(value, int) and not isinstance(value, bool) else 0)
    return tuple(budgets)


def refresh_derived(
    document: Mapping[str, object], plan: SlidePlan
) -> Mapping[str, object]:
    """用**合并后的**预算重算派生块（``model`` 与 ``capacity``）。

    人改过预算之后，"正文页数 = 各章预算之和"与"容量按内容页数算"这两块必须跟着走，
    否则骨架里就留着两个互相矛盾的数（旧的 model.body_pages 与新的 budget 表）。
    """
    budgets = effective_budgets(document)
    if len(budgets) != len(plan.budgets) or budgets == plan.budgets:
        return document
    body = sum(budgets)
    capacity = capacity_report(plan.config, body, plan.candidate_lengths, plan.theme)
    updated = dict(document)
    model = dict(updated.get("model") or {})
    model["body_pages"] = body
    model["total_pages"] = body + plan.config.overhead
    updated["model"] = model
    updated["capacity"] = capacity.as_dict()
    return updated


def write_outputs(
    plan: SlidePlan,
    project_root: Path,
    out_dir: Optional[Path] = None,
    *,
    force: bool = False,
    merge: bool = False,
) -> Tuple[Path, Path, Tuple[str, ...], Tuple[int, ...], Capacity]:
    """写出两件产物，返回（候选表、骨架、合并说明、生效的预算、容量）。

    **先骨架后候选表**：``--merge`` 保留的是人改过的预算，候选表里的页数分配与容量必须按
    同一份预算算——两份产物里的页数因此永远是同一个数。

    骨架已经存在、又没给 ``--force``／``--merge`` 时**拒绝写**：那份文件可能有人手改的
    预算、讲不讲与候选，覆盖掉就找不回来了。候选表是纯派生物，永远重写。
    """
    destination = out_dir or deck_dir(project_root, plan.deck)
    outline_path = destination / OUTLINE_NAME
    if outline_path.exists() and not (force or merge):
        raise SlidesError(
            f"{relative_to_root(outline_path, project_root)} 已存在；拒绝覆盖，因为它可能保存着"
            "你对 budget、sections[].speak、picks 与 theme 的修改。用 --force 重新生成（会丢弃这些"
            "修改），或用 --merge 保留它们并刷新派生字段。"
        )

    destination.mkdir(parents=True, exist_ok=True)
    document = build_outline_document(
        plan.structure, plan.config, plan.deck, plan.budgets, plan.default_budgets,
        plan.capacity, plan.by_chapter, fingerprint=plan.fingerprint, theme=plan.theme,
    )
    notes: Tuple[str, ...] = ()
    if merge and outline_path.exists():
        document, notes = merge_document(document, load_outline_document(outline_path))
        document = refresh_derived(document, plan)
    budgets = effective_budgets(document)
    if len(budgets) != len(plan.budgets):
        budgets = plan.budgets
    capacity = capacity_report(plan.config, sum(budgets), plan.candidate_lengths, plan.theme)
    model = dict(document.get("model") or {})
    model["body_pages"] = sum(budgets)
    model["total_pages"] = sum(budgets) + plan.config.overhead
    document = {**document, "model": model, "capacity": capacity.as_dict()}

    candidates_path = destination / CANDIDATES_NAME
    candidates_path.write_text(
        build_candidates_md(
            plan.structure,
            plan.config,
            budgets,
            capacity,
            plan.by_chapter,
            plan.theme,
        ),
        encoding="utf-8",
    )
    outline_path.write_text(
        dump_outline(
            document,
            default_budgets=plan.default_budgets,
            candidate_chars=plan.candidate_chars,
        ),
        encoding="utf-8",
    )
    return candidates_path, outline_path, notes, budgets, capacity


def run(
    project_root: Path,
    config: DeckConfig,
    deck: str = DEFAULT_DECK,
    spec_path: Optional[Path] = None,
    out_dir: Optional[Path] = None,
    *,
    force: bool = False,
    merge: bool = False,
) -> Tuple[SlidePlan, Tuple[Path, Path]]:
    """完整跑一次 ``outline``。

    容量没被显式给出时按**这份骨架自己的主题**算：``--merge`` 从旧骨架的 ``theme`` 块
    取，其余情形（新骨架、``--force``）用默认主题——``--force`` 会把 theme 块也退回默认值。
    """
    root = Path(project_root).expanduser().resolve()
    spec = resolve_spec(root, deck, spec_path)
    theme = _effective_theme(out_dir or deck_dir(root, deck), merge)
    plan = build_plan(spec, config, deck, theme=theme)
    candidates_path, outline_path, notes, budgets, capacity = write_outputs(
        plan, root, out_dir, force=force, merge=merge
    )
    # 报告以**盘上那份骨架**为准：--merge 保留了人改的预算，正文页数与容量就得按它报，
    # 不能拿本次算出的默认值去说事。
    plan = replace(plan, budgets=budgets, capacity=capacity, merge_notes=notes)
    return plan, (candidates_path, outline_path)


def _effective_theme(destination: Path, merge: bool) -> Theme:
    """这次 ``outline`` 生效的主题：``--merge`` 且旧骨架在时取它的 ``theme`` 块，否则默认值。"""
    if not merge:
        return default_theme()
    existing = destination / OUTLINE_NAME
    if not existing.is_file():
        return default_theme()
    theme, _ = read_theme(load_outline_document(existing))
    return theme


def report_lines(plan: SlidePlan, project_root: Path, written: Sequence[Path]) -> Tuple[str, ...]:
    """终端报告：分配表、容量与取舍、合并说明、写了哪两个文件。"""
    config = plan.config
    capacity = plan.capacity
    capacity_origin = (
        "from the theme" if plan.capacity_source == CAPACITY_FROM_THEME else "explicit"
    )
    materials = plan.candidate_chars
    lines = [
        f"book: {plan.structure.book_line}",
        f"deck: {plan.deck}   {plan.total_pages} slides / backup {config.backup} / "
        f"{config.capacity_per_page} chars per page ({capacity_origin})",
        f"theme: canvas {plan.theme.width_bp:g} x {plan.theme.height_bp:g} bp, "
        f"margin {plan.theme.margin_ratio:.0%} each side, {type_listing(plan.theme)} pt",
        "",
        f"allocation (body {plan.body_pages} pages over {len(plan.structure.chapters)} chapters, "
        f"{plan.structure.material_chars} material chars; "
        f"{plan.human_budgets} chapter(s) with a hand-set budget)",
        pad("order", 5, "right")
        + "  "
        + pad("chapter", CHAPTER_COLUMN)
        + pad("material", 9, "right")
        + pad("budget", 7, "right")
        + pad("from", 7, "right")
        + pad("cand.", 7, "right"),
    ]
    for order, (chapter, budget, default) in enumerate(
        zip(plan.structure.chapters, plan.budgets, plan.default_budgets), 1
    ):
        label = pad(clip(f"{chapter.label} {chapter.title}", CHAPTER_COLUMN), CHAPTER_COLUMN)
        lines.append(
            pad(str(order), 5, "right")
            + "  "
            + label
            + pad(str(materials[order - 1]), 9, "right")
            + pad(str(budget), 7, "right")
            + pad("hand" if budget != default else "default", 7, "right")
            + pad(str(len(plan.by_chapter.get(chapter.number, ()))), 7, "right")
        )
    lines.append(
        pad("", 5)
        + "  "
        + pad("TOTAL", CHAPTER_COLUMN)
        + pad(str(sum(materials)), 9, "right")
        + pad(str(plan.body_pages), 7, "right")
        + pad("", 7)
        + pad(str(plan.candidate_total), 7, "right")
    )
    lines.append(
        "  fixed pages: "
        + ", ".join(f"{page.kind} {page.count}" for page in config.pages)
        + f" = {config.overhead}"
    )
    lines.extend(
        [
            "",
            "capacity and trade-off",
            f"  display capacity   {capacity.per_page_chars} chars x {capacity.content_pages} "
            f"content pages = {capacity.display_chars} chars",
            f"  trade-off          {capacity.points_needed_range[0]}–{capacity.points_needed_range[1]} "
            f"points needed ({capacity.content_pages} content pages x "
            f"{capacity.points_per_content_page_range[0]}–{capacity.points_per_content_page_range[1]} "
            f"points, default {capacity.points_per_content_page}) "
            f"against {capacity.candidates} candidates "
            f"= {float(capacity.candidates_per_point_range[0]):.1f} : 1 … "
            f"{float(capacity.candidates_per_point_range[1]):.1f} : 1 "
            f"(default {float(capacity.candidates_per_point):.1f} : 1)",
            f"                     the human must discard about "
            f"{float(capacity.discard_share) * 100:.0f}% of the candidates "
            f"(at the default {capacity.points_needed} points)",
            f"  per page           median {capacity.median_chars} chars "
            f"(min {capacity.min_chars}, max {capacity.max_chars}); "
            f"{capacity.points_per_page(capacity.median_chars)} fit one page of "
            f"{capacity.per_page_chars} chars",
        ]
    )
    if plan.merge_notes:
        lines.extend(["", *plan.merge_notes])
    lines.extend(["", "written:"])
    for path in written:
        lines.append(f"  {relative_to_root(path, project_root)}")
    return tuple(lines)
