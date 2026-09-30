# -*- coding: utf-8 -*-
"""``check`` 的判据：一份 ``outline.yaml`` 能不能拿去生成幻灯片。

人在 ``outline`` 生成的骨架上改几处——每章预算、每节讲不讲、章／节的 ``hint``、逐帧的
``picks``／``review``／``note``——改完之后必须先过这一关，``build`` 才会照着生成。判据全部是
**确定性**的：只读骨架、候选表与 ``deck.yaml``／源 markdown（为了复算节号），不调用模型、
不联网、不写盘。

判据分两类。**能挡住生成的**（``error``）：骨架还停在 ``version: 1``（读成书 ``.toc``、页数
由预期总页数控制的那一版；只报**一条**、指向 ``outline --merge``）、``deck.yaml``
或某个源文件变了、章与节的结构因此可能对不上（``stale-outline`` 的 error 一侧）、``frames``
条数与该章预算不符（``frame-count``；整份骨架都还是旧写法（没有 ``frames`` 键）时也只报
**一条**，让人去跑 ``outline --merge``，不逐章各报一遍）、挑了一个不存在的候选
id（``pick-missing``）、一条 pick 是空的或形状读不出来（``pick-empty``／``pick-shape``）、
某一帧挑了超过上限的句数（``pick-count`` 的上界一侧）、选中的字总数超过容量
（``capacity-overflow``）、``review`` 不在三态之内（``review-value``）、``hint``／``note``
不是字符串或缺失（``hint-shape``）、某节号不在当前源文件里（``unknown-section``）、主题块里的
token 或字号读不出来（``theme-token``／``theme-type``／``theme-canvas``）。一条 pick 可以是
一个候选 id，也可以是作者手写的一句话（自由文本），或是 ``{text, derived_from}`` 形状的机器
提炼句；后两种在任何位置都合法，解析与判形只有一处
（:func:`primer.slides.candidates.parse_pick`）。**只提醒不挡的**（``warning``）：某个源文件的
内容已变、候选池可能变而节号仍有效（``stale-outline`` 的 warning 一侧）、某帧
挑得比预期少（可能是刻意精简）、一帧的几句加起来超过一页容量（生成时得压缩或拆开）、
某一句里的 ASCII 直引号是奇数个（``quotes-unpaired``，配对按成书同一套，奇数时不猜、原样
保留交人看——引号规整的咽喉点在 :func:`primer.slides.candidates.pick_display`）、某帧的
**颗粒度**（条数、首条长度、总字数）与全片同维度中位数差得太远（``frame-granularity``，
只提醒不挡：颗粒度是"好不好"的事，不是"能不能生成"的事）。

**预算没有对账。** ``chapters[].budget`` 是唯一的控制项：人填多少就是多少，``check`` 不拿它
跟"预期总页数"比，只报告每章预算**来自人工还是默认建议**、由此得到的总页数是多少。每页容量
（``capacity_per_page``）的账原样保留——那是排版事实，不是控制项。

判据**逐帧**看正文：一帧一页，每帧 3–5 句，所以 ``pick-count``／``slide-overflow`` 都落在
帧上，消息点得出是"哪一章的第几页"。

``validate_outline`` 是唯一的判据实现，纯函数、只吃数据：``check`` 从盘上读齐数据调它，
``build`` 拿自己刚算出的候选与结构调它，两处结论必然相同。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List, Mapping, Optional, Sequence, Tuple

import yaml

from ..book import quotes
from . import candidates as cand
from .fingerprint import (
    FINGERPRINT_KEY,
    FingerprintDifference,
    current_fingerprint,
    fingerprint_differences,
)
from .plan import (
    BUDGET_FROM_DEFAULT,
    BUDGET_FROM_HUMAN,
    CROSS_CUTTING_PAGES,
    DEFAULT_CAPACITY_PER_PAGE,
    DISCUSSION_PAGES,
    NAVIGATION_PAGES,
    OPENING_PAGES,
    PAGE_KINDS,
    POINTS_PER_CONTENT_PAGE_RANGE,
    REVIEW_STATES,
    SlidesError,
    budget_origin,
    page_entries,
    read_page_entries,
)
from .render import OUTLINE_VERSION
from .structure import DeckStructure
from .terminal import clip, pad
from .theme import read_theme

CANDIDATES_NAME = "candidates.md"
DECK_SPEC_NAME = "deck.yaml"
OUTLINE_NAME = "outline.yaml"
CHAPTER_COLUMN = 52
# 旧骨架要人跑的那条命令；消息里带上它，人不必去翻文档。
OUTLINE_COMMAND = "python -m primer.slides outline --spec <deck.yaml>"

# frame-granularity：跨页颗粒度。逐帧量**条数、首条长度、总字数**，与全片同维度的中位数比；
# 越出下面这组阈值就报一条 warning。只提醒不挡（`check` 的通过线由 error 决定）——颗粒度
# 是"好不好"的事，不是"能不能生成"的事。阈值取宽一些，宁可漏报，不要逼人为了对齐中位数
# 而删掉本该留下的句子。
GRANULARITY_MIN_PICKS = 3
GRANULARITY_MAX_PICKS = 5
GRANULARITY_LEAD_MAX_CHARS = 48
GRANULARITY_TOTAL_LOW_RATIO = 0.5
GRANULARITY_TOTAL_HIGH_RATIO = 1.6
# ``dict.get(key, _MISSING)`` 的哨兵：区分"键缺失"与"值是 None"。
_MISSING = object()

SEVERITY_ORDER = ("error", "warning", "info")
SEVERITY_LABELS = {"error": "错误", "warning": "警告", "info": "提示"}


@dataclass(frozen=True)
class Finding:
    """一条判据发现。``code`` 是稳定的机器可读标识，``message`` 是中文人读句子。"""

    code: str
    severity: str
    message: str
    location: str = ""

    def __post_init__(self) -> None:
        if self.severity not in SEVERITY_ORDER:
            raise ValueError(f"unknown severity: {self.severity}")

    @property
    def fatal(self) -> bool:
        return self.severity == "error"


# ---------------------------------------------------------------- 读盘（check 用）


def load_outline_document(path: Path) -> Mapping[str, object]:
    """读 ``outline.yaml``；不是 YAML 映射就报错（英文，由命令行打印）。"""
    text = path.read_text(encoding="utf-8")
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise SlidesError(f"outline is not valid YAML: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise SlidesError(f"outline is not a YAML mapping: {path}")
    return raw


def load_candidate_rows(path: Path) -> Mapping[str, cand.CandidateRow]:
    """从 ``candidates.md`` 的候选表里读出整张表（``id -> 行``）。

    解析交给 :func:`primer.slides.candidates.parse_markdown_table`——那份实现同时给出
    句子与页面指针，``build`` 要用它们发射幻灯片，所以整张表只解析一次、只有一处。
    """
    if not path.is_file():
        raise SlidesError(f"candidate table not found: {path} (run outline first)")
    return cand.parse_markdown_table(path.read_text(encoding="utf-8"))


def load_candidates(path: Path) -> Mapping[str, int]:
    """从 ``candidates.md`` 的候选表里读出 ``id -> 字数``。"""
    return {
        identifier: row.length
        for identifier, row in load_candidate_rows(path).items()
    }


def known_section_numbers(structure: Optional[DeckStructure]) -> Tuple[str, ...]:
    """当前源文件里真实存在的节号（``2.1``、``A.3.3.1``……）。结构读不出来时给空表。"""
    if structure is None:
        return ()
    return tuple(
        section.number
        for chapter in structure.chapters
        for section in chapter.sections
    )


def capacity_per_page(outline: Mapping[str, object]) -> int:
    """一页幻灯片的文本容量（字）：``deck.capacity_per_page``，缺省是排版算出的默认值。

    这是**唯一**一处读骨架容量字段的地方：``check`` 的 ``slide-overflow``／
    ``capacity-overflow`` 与 ``select`` 圈要点时的字数预算都调它，两边不会各读一次、
    各算一份。字数与容量都是排版事实，口径由 :mod:`primer.slides.theme` 给出。
    """
    deck = _mapping(outline.get("deck"))
    return _int(deck.get("capacity_per_page"), DEFAULT_CAPACITY_PER_PAGE)


def document_version(outline: Mapping[str, object]) -> int:
    """骨架格式的版本；没有这一键就是改动之前的 ``version: 1``。"""
    return _int(outline.get("version"), 1)


def check_outline(
    outline_path: Path,
    project_root: Path,
    candidates_path: Optional[Path] = None,
    spec_path: Optional[Path] = None,
) -> Tuple[Mapping[str, object], Mapping[str, int], Tuple[Finding, ...]]:
    """把一份 ``outline.yaml`` 从盘上读到判定：返回（骨架、候选表、发现）。不写盘。

    ``deck.yaml`` 与源 markdown 只用来复算节号（``unknown-section``）与比对出生指纹；骨架
    停在旧版（``version: 1``）时不去读它们——那一条错误已经把该做什么说清楚了。
    """
    outline = load_outline_document(outline_path)
    rows = load_candidate_rows(candidates_path or outline_path.parent / CANDIDATES_NAME)
    candidates = {identifier: row.length for identifier, row in rows.items()}
    structure: Optional[DeckStructure] = None
    differences: List[FingerprintDifference] = []
    if document_version(outline) == OUTLINE_VERSION:
        current, problems, structure = current_fingerprint(
            outline, project_root, spec_path
        )
        stored = outline.get(FINGERPRINT_KEY)
        differences = list(problems) + fingerprint_differences(
            stored if isinstance(stored, Mapping) else None, current
        )
    findings = validate_outline(
        outline,
        candidates=candidates,
        candidate_rows=rows,
        known_sections=known_section_numbers(structure),
        fingerprint_differences=differences,
    )
    return outline, candidates, tuple(findings)


# ---------------------------------------------------------------- 判据（纯函数）


def validate_outline(
    outline: Mapping[str, object],
    *,
    candidates: Mapping[str, int],
    known_sections: Sequence[str] = (),
    fingerprint_differences: Sequence[FingerprintDifference] = (),
    candidate_rows: Optional[Mapping[str, cand.CandidateRow]] = None,
) -> List[Finding]:
    """对一份骨架跑全部判据，返回发现（中文）。空表即通过。

    ``candidate_rows`` 给出候选表的整行（``id -> 行``），判引号时按 pick 的三种形状
    取显示文本用；缺省时只有手写句子与提炼句能被判到，候选 id 的句子读不出来。
    ``known_sections`` 是当前源文件里真实存在的节号（``unknown-section`` 用它）。

    骨架还停在 ``version: 1``（读成书 ``.toc``、页数由预期总页数控制的那一版）时**只报一条**
    error 并立即返回：那份骨架该做什么是清楚的（跑一次 ``--merge``），逐章再喷一遍同义错误
    只会把这一条淹掉。
    """
    findings: List[Finding] = []
    rows = candidate_rows or {}
    if document_version(outline) != OUTLINE_VERSION:
        return [
            Finding(
                "outline-version",
                "error",
                "这份骨架是 version 1 的旧写法（结构读自成书的 .toc、页数由预期总页数控制）："
                f"请跑 `{OUTLINE_COMMAND} --merge` 重新生成。工具会保留 budget、"
                "sections[].speak、逐帧 picks／review／note、固定页与 theme，只刷新派生字段。",
            )
        ]

    for difference in fingerprint_differences:
        findings.append(Finding("stale-outline", difference.severity, difference.message))

    deck = _mapping(outline.get("deck"))
    backup = _int(deck.get("backup"), 0)
    capacity = capacity_per_page(outline)
    overhead = (
        OPENING_PAGES + NAVIGATION_PAGES + CROSS_CUTTING_PAGES + DISCUSSION_PAGES + backup
    )
    chapters = [
        chapter for chapter in outline.get("chapters") or [] if isinstance(chapter, Mapping)
    ]
    # 正文页数 = 各章预算之和。它不再是"总页数 − 固定页"，也没有任何总数跟它对账——
    # budget 是唯一的控制项，加出来是多少就是多少。
    body = sum(_int(chapter.get("budget"), 0) for chapter in chapters)

    findings.extend(_page_findings(outline, candidates, backup, rows))
    findings.extend(_theme_findings(outline))

    for chapter in chapters:
        label = _chapter_label(chapter)
        budget = _int(chapter.get("budget"), 0)
        raw = chapter.get("budget")
        if isinstance(raw, bool) or not isinstance(raw, int):
            findings.append(
                Finding(
                    "budget-range",
                    "error",
                    f"章 {label} 的 budget 必须是整数{_shape_suffix(raw)}",
                )
            )
        elif budget < 0:
            findings.append(
                Finding("budget-range", "error", f"章 {label} 预算 {budget} 为负")
            )

    picked_chars = 0
    # 逐帧的颗粒度样本：(人读位置, 条数, 首条字数, 总字数)。只收**挑过句子的**帧——
    # 空帧由 pick-count 负责，拿它算中位数会把"还没挑"误判成"颗粒度不一致"。
    frame_stats: List[Tuple[str, int, int, int]] = []
    # 整份骨架都没有 frames 键 = 改动之前的旧写法。只报**一条**、指同一条命令，不逐章各报
    # 一遍、也不让后面那些判不成帧的判据各喷一条——满屏同义错误反而看不出该做什么。
    legacy_skeleton = bool(chapters) and all("frames" not in chapter for chapter in chapters)
    if legacy_skeleton:
        findings.append(
            Finding(
                "frame-count",
                "error",
                f"这份骨架是旧写法（章一级一串平铺的 picks，没有 frames）："
                f"请跑 `{OUTLINE_COMMAND} --merge` 重新生成，"
                "工具会把各章的 picks 按均衡分法摊到各帧，一条不丢",
            )
        )
    for chapter in chapters:
        label = _chapter_label(chapter)
        budget = _int(chapter.get("budget"), 0)
        raw_frames = chapter.get("frames", _MISSING)
        if raw_frames is _MISSING:
            # 老骨架整份已在上头报过一条；只有"一部分章有 frames、另一部分没有"这种手改
            # 出来的混合情形才在这里逐章点出来。
            if not legacy_skeleton:
                findings.append(
                    Finding(
                        "frame-count",
                        "error",
                        f"章 {label} 还没有 frames（章一级平铺的 picks 不再写进骨架）："
                        f"请跑 `{OUTLINE_COMMAND} --merge` 重新生成，"
                        "工具会把那串句子按均衡分法摊到各帧，一条不丢",
                    )
                )
            continue
        hint = chapter.get("hint", _MISSING)
        if not isinstance(hint, str):
            findings.append(
                Finding("hint-shape", "error", f"章 {label} 的 hint 必须是字符串（可空串）{_shape_suffix(hint)}")
            )
        for section in chapter.get("sections") or []:
            if not isinstance(section, Mapping):
                continue
            value = section.get("hint", _MISSING)
            if not isinstance(value, str):
                findings.append(
                    Finding(
                        "hint-shape",
                        "error",
                        f"章 {label} 节 {section.get('number')!r} 的 hint 必须是字符串"
                        f"（可空串）{_shape_suffix(value)}",
                    )
                )
        if not isinstance(raw_frames, list):
            findings.append(
                Finding(
                    "frame-count",
                    "error",
                    f"章 {label} 的 frames 必须是列表{_shape_suffix(raw_frames)}",
                )
            )
            continue
        if len(raw_frames) != max(budget, 0):
            findings.append(
                Finding(
                    "frame-count",
                    "error",
                    f"章 {label}：frames {len(raw_frames)} 帧 ≠ 预算 {budget} 页"
                    "（一帧一页，长度必须相等）",
                )
            )
        for index, frame in enumerate(raw_frames):
            where = f"帧 {label} 第 {index + 1} 页"
            if not isinstance(frame, Mapping):
                findings.append(
                    Finding(
                        "hint-shape",
                        "error",
                        f"{where} 整条必须是映射表（picks、review、note）{_shape_suffix(frame)}",
                    )
                )
                continue
            note = frame.get("note", _MISSING)
            if not isinstance(note, str):
                findings.append(
                    Finding(
                        "hint-shape",
                        "error",
                        f"{where} 的 note 必须是字符串（可空串）{_shape_suffix(note)}",
                    )
                )
            review = frame.get("review", _MISSING)
            if review not in REVIEW_STATES:
                findings.append(
                    Finding(
                        "review-value",
                        "error",
                        f"{where} 的 review 必须是 {'、'.join(REVIEW_STATES)} 之一，"
                        f"读到的是 {review!r}",
                    )
                )
            raw_picks = frame.get("picks", [])
            if not isinstance(raw_picks, list):
                findings.append(
                    Finding(
                        "pick-shape",
                        "error",
                        f"{where} 的 picks 必须是列表{_shape_suffix(raw_picks)}",
                    )
                )
                raw_picks = []
            picks, pick_findings = _read_picks(list(raw_picks), candidates, where)
            findings.extend(pick_findings)
            for pick in picks:
                if pick is not None and pick.is_candidate and pick.identifier not in candidates:
                    findings.append(
                        Finding(
                            "pick-missing",
                            "error",
                            f"{where} 列出了候选 {pick.identifier!r}，"
                            f"但 {CANDIDATES_NAME} 里没有这个 id",
                        )
                    )
            findings.extend(_quote_findings(picks, rows, where))
            picked_chars += sum(
                cand.pick_length(pick, candidates) for pick in picks if pick is not None
            )
            low, high = POINTS_PER_CONTENT_PAGE_RANGE
            if len(picks) < low:
                findings.append(
                    Finding(
                        "pick-count",
                        "warning",
                        f"{where}：选取 {len(picks)}，预期 {low}–{high}；"
                        "若这一页刻意精简，少一些也可以",
                    )
                )
            elif len(picks) > high:
                findings.append(
                    Finding(
                        "pick-count",
                        "error",
                        f"{where}：选取 {len(picks)}，预期 {low}–{high}；多于预期，放不下",
                    )
                )
            present = [pick for pick in picks if pick is not None]
            if not present:
                continue
            total = sum(cand.pick_length(pick, candidates) for pick in present)
            frame_stats.append(
                (where, len(present), cand.pick_length(present[0], candidates), total)
            )
            if total > capacity:
                parts = " + ".join(
                    f"{_pick_label(pick)}（{cand.pick_length(pick, candidates)}）"
                    for pick in present
                )
                findings.append(
                    Finding(
                        "slide-overflow",
                        "warning",
                        f"{where}：选取 {parts} = {total} 字，"
                        f"超过 capacity_per_page {capacity}；生成器必须压缩或拆分",
                    )
                )

    findings.extend(_granularity_findings(frame_stats))

    capacity_chars = capacity * body
    if picked_chars > capacity_chars:
        findings.append(
            Finding(
                "capacity-overflow",
                "error",
                f"选取共 {picked_chars} 字，超过容量 {capacity_chars} 字"
                f"（{capacity} × {body} 个正片页），超出 {picked_chars - capacity_chars} 字",
            )
        )

    for chapter in chapters:
        label = _chapter_label(chapter)
        for section in chapter.get("sections") or []:
            if not isinstance(section, Mapping):
                continue
            number = section.get("number")
            if known_sections and isinstance(number, str) and number and number not in known_sections:
                findings.append(
                    Finding(
                        "unknown-section",
                        "error",
                        f"章 {label} 列出了节 {number!r}，但当前源文件里没有这个节号"
                        "（deck.yaml 与源 markdown 复算出来的节清单和骨架对不上）",
                    )
                )
    return findings


# ---------------------------------------------------------------- 报告


def severity_lines(findings: Sequence[Finding]) -> List[str]:
    """把发现按严重度分组列成人读行（中文）。

    ``check`` 与 ``build`` 共用这一段：两份报告都要先让眼睛看到"错了几条、警告几条、
    分别是什么"，分层渲染只有一处实现。``info`` 是给"这一帧被折过"一类的提示用的。
    """
    lines: List[str] = []
    for severity in SEVERITY_ORDER:
        group = [finding for finding in findings if finding.severity == severity]
        if not group:
            continue
        lines.extend(["", f"{SEVERITY_LABELS[severity]} ({len(group)})"])
        for finding in group:
            lines.append(f"  [{finding.code}] {finding.message}")
    return lines


def check_report_lines(
    outline: Mapping[str, object],
    findings: Sequence[Finding],
    candidates: Mapping[str, int],
    outline_path: Optional[Path] = None,
    project_root: Optional[Path] = None,
) -> Tuple[str, ...]:
    """人读报告（中文）：先按严重度列发现，再逐章对账，最后给通过／不通过。

    报告正文一律中文，与它同族的 ``candidates.md``／``outline.yaml`` 一致；机器读的只有
    ``[code]`` 里的 finding code 与命令行文本，那些保持英文。``project_root`` 只用来在
    骨架的相对路径一个都读不到时补一行提示（:func:`project_root_hint`），不参与判据。
    """
    lines: List[str] = []
    heading = "primer-slides check"
    if outline_path is not None:
        heading += f": {outline_path.name}"
    lines.append(heading)
    lines.extend(severity_lines(findings))

    lines.extend(["", *_table_lines(outline, candidates)])
    counts = {
        severity: sum(1 for finding in findings if finding.severity == severity)
        for severity in SEVERITY_ORDER
    }
    verdict = "不通过" if counts["error"] else "通过"
    parts = [f"{counts['error']} 个错误", f"{counts['warning']} 个警告"]
    if counts["info"]:
        parts.append(f"{counts['info']} 条提示")
    lines.extend(["", f"{verdict}：" + "，".join(parts)])
    hint = project_root_hint(outline, project_root)
    if hint:
        lines.extend(["", hint])
    return tuple(lines)


def _theme_findings(outline: Mapping[str, object]) -> List[Finding]:
    """主题块的判据：认不出的 token 与读不出的字号都是 error。

    读得出的值一律照用（缺的键取默认值），所以问题清单为空时，``build`` 拿到的就是
    YAML 里写的画布、字号与配色——它一个字也不会改写。
    """
    _, problems = read_theme(outline)
    return [Finding(code, "error", message) for code, message in problems]


def _page_findings(
    outline: Mapping[str, object],
    candidates: Mapping[str, int],
    backup: int,
    rows: Mapping[str, cand.CandidateRow],
) -> List[Finding]:
    """固定页的两条判据：种类与页数对不对得上，以及 picks 指不指得着候选。

    ``picks`` 用的是与逐帧**同一条** ``pick-missing`` 规则——固定页上指的是候选 id 就
    该在 ``candidates.md`` 里，指错了就该在生成之前被挡下；手写的自由文本与提炼句在固定页
    上同样合法。``auto`` 的页（导航、备份）正文由工具算出，人不填 picks，填了也照同一规则
    核。页数由控制模型的常量与 ``--backup`` 决定，与骨架里的条数不符就报错：页数不对，
    整份骨架的页序就全错。
    """
    findings: List[Finding] = []
    entries = read_page_entries(outline)
    expected = page_entries(backup)
    kinds = [entry.kind for entry in entries]
    unknown = sorted({kind for kind in kinds if kind not in PAGE_KINDS})
    for kind in unknown:
        findings.append(
            Finding("pages-kind", "error", f"固定页里出现了未知种类 {kind!r}")
        )
    if not unknown and kinds != [entry.kind for entry in expected]:
        findings.append(
            Finding(
                "pages-count",
                "error",
                "固定页清单与页数模型对不上："
                f"骨架 {_kind_summary(entries)}，模型 {_kind_summary(expected)}"
                f"（共 {len(entries)} vs {len(expected)} 页）",
            )
        )
    for entry in entries:
        picks, pick_findings = _read_picks(
            list(entry.picks), candidates, f"固定页 {entry.label!r}"
        )
        findings.extend(pick_findings)
        for pick in picks:
            if pick is not None and pick.is_candidate and pick.identifier not in candidates:
                findings.append(
                    Finding(
                        "pick-missing",
                        "error",
                        f"固定页 {entry.label!r} 列出了候选 {pick.identifier!r}，"
                        f"但 {CANDIDATES_NAME} 里没有这个 id",
                    )
                )
        findings.extend(_quote_findings(picks, rows, f"固定页 {entry.label!r}"))
    return findings


def _quote_findings(
    picks: Sequence[Optional[cand.Pick]],
    rows: Mapping[str, cand.CandidateRow],
    where: str,
) -> List[Finding]:
    """奇数个 ASCII 直引号的那一句：原样保留，报一条警告。

    配对交给成书同一套 :func:`primer.book.quotes.normalize_double_quotes`（pick 的显示文本
    也由它规整，见 :func:`primer.slides.candidates.pick_display`）。偶数个的已经配成中文
    弯引号，这里读到的直引号因此**只可能来自那一行是奇数个**的情形——原样保留，交给人工
    判断哪一个是收尾，不猜。消息带上人读位置与句子片段。
    """
    findings: List[Finding] = []
    for pick in picks:
        if pick is None:
            continue
        text = cand.pick_display(pick, rows)
        if not text:
            continue
        unpaired = quotes.normalize_double_quotes(text).unpaired
        if not unpaired:
            continue
        findings.append(
            Finding(
                "quotes-unpaired",
                "warning",
                f"{where}：这一句有 {unpaired} 个 ASCII 直引号（奇数），"
                f"按成书语义原样保留待人工确认：{clip(text, 24)}",
            )
        )
    return findings


def _kind_summary(entries: Sequence[object]) -> str:
    counts: List[str] = []
    for entry in entries:
        kind = getattr(entry, "kind", "")
        if counts and counts[-1].startswith(f"{kind} "):
            counts[-1] = f"{kind} {int(counts[-1].split()[1]) + 1}"
        else:
            counts.append(f"{kind} 1")
    return "、".join(counts) if counts else "（空）"


BUDGET_ORIGIN_LABELS = {BUDGET_FROM_HUMAN: "人工", BUDGET_FROM_DEFAULT: "默认"}


def _table_lines(
    outline: Mapping[str, object], candidates: Mapping[str, int]
) -> List[str]:
    """逐章对账表：每章的预算、它的来路（人工／默认建议）、选取条数与字数。

    **没有"预算合计 vs 正文预算"这一行**：budget 是唯一的控制项，数学上不可能对不上。
    报出来的是它自己加出来的结果——正文页数与全片页数。
    """
    deck = _mapping(outline.get("deck"))
    backup = _int(deck.get("backup"), 0)
    capacity = capacity_per_page(outline)
    overhead = (
        OPENING_PAGES + NAVIGATION_PAGES + CROSS_CUTTING_PAGES + DISCUSSION_PAGES + backup
    )
    chapters = [
        chapter for chapter in outline.get("chapters") or [] if isinstance(chapter, Mapping)
    ]

    lines = [
        "逐章对账",
        pad("章", CHAPTER_COLUMN)
        + pad("预算", 7, "right")
        + pad("来源", 8, "right")
        + pad("选取", 14, "right")
        + pad("字数", 8, "right"),
    ]
    budget_total = picks_total = chars_total = hand_total = 0
    low, high = POINTS_PER_CONTENT_PAGE_RANGE
    for chapter in chapters:
        budget = _int(chapter.get("budget"), 0)
        # 骨架里没有 budget_default 这一键 = 旧写法（或手写的骨架）：来路如实记"-"。
        origin = budget_origin(chapter) if "budget_default" in chapter else ""
        hand_total += 1 if origin == BUDGET_FROM_HUMAN else 0
        picks, _ = _read_picks(_chapter_pick_values(chapter), candidates, "")
        chars = sum(
            cand.pick_length(pick, candidates) for pick in picks if pick is not None
        )
        budget_total += budget
        picks_total += len(picks)
        chars_total += chars
        label = clip(
            f"{_chapter_label(chapter)}　{chapter.get('title', '')}".strip(), CHAPTER_COLUMN
        )
        lines.append(
            pad(label, CHAPTER_COLUMN)
            + pad(str(budget), 7, "right")
            + pad(BUDGET_ORIGIN_LABELS.get(origin, "-"), 8, "right")
            + pad(f"{len(picks)}/{budget * low}–{budget * high}", 14, "right")
            + pad(str(chars), 8, "right")
        )
    lines.append(
        pad("合计", CHAPTER_COLUMN)
        + pad(str(budget_total), 7, "right")
        + pad(f"人工 {hand_total}", 8, "right")
        + pad(
            f"{picks_total}/{budget_total * low}–{budget_total * high}",
            14,
            "right",
        )
        + pad(str(chars_total), 8, "right")
    )
    capacity_chars = capacity * budget_total
    chars_delta = chars_total - capacity_chars
    if chars_delta == 0:
        chars_words = "相符"
    elif chars_delta > 0:
        chars_words = f"超出 {chars_delta} 字"
    else:
        chars_words = f"少 {-chars_delta} 字"
    lines.extend(
        [
            "",
            f"  正文 {budget_total} 页 = 各章预算之和（其中 {hand_total} 章人工定过），"
            f"固定页 {overhead} 页 → 全片 {budget_total + overhead} 页",
            f"  选取字数 {chars_total} vs 容量 {capacity_chars}（{chars_words}）",
        ]
    )
    return lines


# ---------------------------------------------------------------- 小工具


def _chapter_label(chapter: Mapping[str, object]) -> str:
    label = chapter.get("label") or chapter.get("chapter")
    return str(label) if label else "(unnamed)"


def _chapter_pick_values(chapter: Mapping[str, object]) -> List[object]:
    """一章的原始 pick 值：逐帧 ``frames`` 摊平；旧骨架只有章一级 ``picks`` 时退回它。

    只给报告表用（:func:`_table_lines`）——它要的是"这一章一共圈了几句、共多少字"，
    不关心分帧。判据本身逐帧走，见 :func:`validate_outline`。
    """
    frames = chapter.get("frames")
    if isinstance(frames, list):
        values: List[object] = []
        for frame in frames:
            if isinstance(frame, Mapping):
                raw = frame.get("picks")
                if isinstance(raw, list):
                    values.extend(raw)
        return values
    return list(chapter.get("picks") or [])


def _shape_suffix(value: object) -> str:
    """形状消息的尾巴：缺失时说"缺失"，其余报读到的是什么值。"""
    if value is _MISSING:
        return "，但这一条缺失"
    return f"，读到的是 {value!r}"


def _read_picks(
    values: Sequence[object], candidates: Mapping[str, int], where: str
) -> Tuple[List[Optional[cand.Pick]], List[Finding]]:
    """一串原始 pick → （逐条解析结果，发现）。

    ``where`` 是人读位置（``章 第一章``／``固定页 '封面'``），接在消息前面。三种形状
    （候选 id／自由文本／``{text, derived_from}`` 提炼句）的判断全交给
    :func:`primer.slides.candidates.parse_pick`；``pick-empty``／``pick-shape`` 与提炼句
    ``derived_from`` 的 ``pick-missing`` 在这里报。读不出来的一条记 ``None``——它照旧计入
    条数，但不参与字数与分页。候选 id 本身在不在候选表里由调用方另报，好让 ``pick-missing``
    的消息保持原样。
    """
    picks: List[Optional[cand.Pick]] = []
    findings: List[Finding] = []
    for value in values:
        try:
            picks.append(cand.parse_pick(value, candidates))
        except cand.PickError as exc:
            picks.append(None)
            findings.append(Finding(exc.code, "error", f"{where} {exc.message}"))
    return picks, findings


def _pick_label(pick: cand.Pick) -> str:
    """报告里的短名：候选 id 用它自己，句子取前 24 列（免得一整句挤进一行警告）。"""
    return pick.identifier if pick.is_candidate else clip(pick.text, 24)


def _median(values: Sequence[int]) -> float:
    """中位数；偶数个样本取中间两个的平均。空样本返回 0。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[middle])
    return (ordered[middle - 1] + ordered[middle]) / 2


def _median_label(value: float) -> str:
    """中位数的可读写法：整数不带小数点，半数保留一位（``4``／``3.5``）。"""
    return str(int(value)) if float(value).is_integer() else f"{value:.1f}"


def _granularity_findings(stats: Sequence[Tuple[str, int, int, int]]) -> List[Finding]:
    """逐帧的颗粒度与全片中位数比，越界就报一条 warning。

    ``stats`` 是 ``(where, 条数, 首条字数, 总字数)``；中位数各维度**分别**取，消息里同时
    给出这一帧的数与中位数，人一眼能看出偏在哪。空样本（一帧都没挑）不报——那是
    ``pick-count`` 的事。总字数一侧只在全片中位数为正时判，免得 0 做分母把比例放大。
    """
    if not stats:
        return []
    count_median = _median([count for _, count, _, _ in stats])
    lead_median = _median([lead for _, _, lead, _ in stats])
    total_median = _median([total for _, _, _, total in stats])
    findings: List[Finding] = []
    for where, count, lead, total in stats:
        reasons: List[str] = []
        if count < GRANULARITY_MIN_PICKS or count > GRANULARITY_MAX_PICKS:
            reasons.append("条数")
        if lead > GRANULARITY_LEAD_MAX_CHARS:
            reasons.append("首条偏长")
        if total_median > 0 and (
            total < GRANULARITY_TOTAL_LOW_RATIO * total_median
            or total > GRANULARITY_TOTAL_HIGH_RATIO * total_median
        ):
            reasons.append("总字数")
        if not reasons:
            continue
        findings.append(
            Finding(
                "frame-granularity",
                "warning",
                f"{where}：条数 {count}（中位 {_median_label(count_median)}）、"
                f"首条 {lead} 字（中位 {_median_label(lead_median)}）、"
                f"总字数 {total}（中位 {_median_label(total_median)}）；"
                f"{'、'.join(reasons)}与全片颗粒度不一致",
            )
        )
    return findings


def project_root_hint(
    outline: Mapping[str, object], project_root: Optional[Path]
) -> Optional[str]:
    """骨架没在 ``--project-root`` 下找到相对路径时的那一行提示；不需要就返回 ``None``。

    骨架里的 ``spec``（deck.yaml 的路径）是**相对工程根**记的（见
    :mod:`primer.slides.fingerprint`）。从别的目录跑 ``check``／``build`` 时它读不到，
    指纹比对会把规格与结构都报成"变了"，满屏假错误。那条路径在当前根下**不存在**
    就说明多半是 ``--project-root`` 指错了地方，补一行话点出来，不动判据等级、不改消息。
    """
    if project_root is None:
        return None
    stored = outline.get(FINGERPRINT_KEY)
    relatives = [
        value
        for value in (stored.get("spec") if isinstance(stored, Mapping) else None,)
        if isinstance(value, str) and value
    ]
    if not relatives or any((Path(project_root) / rel).exists() for rel in relatives):
        return None
    listed = "、".join(relatives)
    return (
        f"提示：骨架里的相对路径（{listed}）是相对 --project-root 解析的，"
        f"当前目录 {Path(project_root)} 下都不存在；请用 --project-root <项目根> 再试"
    )


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _int(value: object, default: int) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default
