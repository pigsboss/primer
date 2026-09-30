# -*- coding: utf-8 -*-
"""把分配结果、容量与取舍、候选写成两件人可编辑的产物。

* ``candidates.md``：人读页——先给页数分配表，再给容量与取舍（显示容量、取舍比、每页条数），
  然后逐章列出候选句，供人圈选；
* ``outline.yaml``：可编辑骨架——顶部注释说明**该人改的几处**（预算、讲哪些节、挑哪几个
  候选、放哪幅图、讲者备注、主题块）与"场合/时长自己填"那一行，其余字段由工具算出。

容量那一段只陈述事实，不含通过／不通过：判定"超没超"需要一条页数→时长的换算率，而那条
换算率不存在（见 :mod:`primer.slides.plan`）。

骨架顶部写 ``version: 2``：这一版的结构与指针由 ``deck.yaml`` 与源 markdown 复算（不是读
成书的 ``.toc``），页码字段因此一个都不在。``check`` 遇到 ``version: 1`` 的旧骨架只报一条
错误，指向 ``outline --merge``。

两件产物都不带时间戳、不读环境、不遍历无序容器：同一份输入必然字节相同。
"""

from __future__ import annotations

import re
from fractions import Fraction
from typing import Dict, List, Mapping, Optional, Sequence

import yaml

from . import candidates as cand
from .plan import Capacity, DeckConfig, frame_entry, page_entries
from .structure import Chapter, DeckStructure
from .theme import Theme, default_theme, frame_metrics

OUTLINE_VERSION = 2
ALLOCATION_HEADER = (
    "| 序 | 章 | 材料（字） | 预算 | 候选 |\n"
    "|--:|:--|--:|--:|--:|\n"
)
CANDIDATE_HEADER = (
    "| id | 类型 | 信号 | 引用 | 字数 | {pointer}句子 |\n"
    "|:--|:--|:--|--:|--:|:--|:--|\n"
)

YAML_COMMENT = """\
# primer-slides / outline —— 幻灯片骨架（version 2）
#
# 由 `python -m primer.slides outline --spec <deck.yaml>` 生成，确定性抽取：无模型、无网络、
# 无随机。这个文件是给人改的。可改的有这几处：
#
#   1. chapters[].budget —— 该章分到几页，**唯一的控制参数**。总页数 = 各章预算之和 +
#      固定页（开场 2、目录 1、横向 3、讨论 3、备份 N），加出来是多少就是多少，没有别的
#      数可以跟它对账。budget_default 是工具按材料占比给的默认建议；改了 budget，那个数
#      就是事实（check 会如实说这一章是人工填的还是照默认来的）。
#   2. chapters[].sections[].speak —— 该节要不要讲。默认 true；标 false 表示这一节
#      只在备份页或问答里出现，不占正片页数。
#   3. chapters[].hint / chapters[].sections[].hint —— 方向性启发（章一级、节一级），
#      写给"挑哪几句"这件事看：这一段要立住什么、哪几个节不能漏。人写，可空串；
#      工具不读它，也不会替你写。
#   4. chapters[].frames —— 正文页，**一帧一页**，长度必须等于该章 budget。
#      每帧三样：
#        picks  —— 这一帧要讲的句子。三种形状，与固定页 pages[].picks 同一条规则：
#                  (a) 候选 id（如 s8-6p02，指 candidates.md 的一行）；
#                  (b) 手写自由文本（直接写一句话）；
#                  (c) 机器提炼句 {text: …, derived_from: [候选 id…]}。
#                  每帧 3–5 句，默认 4；空列表表示还没挑，页面上打一个 〔待选句〕 记号。
#        review —— 人审三态：pending（没看）| ok（看过没问题）| redo（要重来）。
#        note   —— 写给这一帧的定向提示（这一页只讲哪一件事），人写，可空串。
#   5. chapters[].figure / pages[].figure —— 放在这一页上的插图编号（如 '2-3'）。
#      章一级留空（null）表示按默认取该章的第一幅图（第 n 页配第 n 幅图）；填 '' 表示
#      不上图。pages[].figure 留空即不上图。
#   6. notes —— 进 \\note{}，只出现在备注页与讲义里；空字符串什么都不发射。
#   7. theme —— 画布（bp）、六档字号（pt）、字体栈与配色 token（#rrggbb）。**这块是数据**：
#      值改了就改，build 跟着变。--merge 像保留 budget／frames 一样原样保留它；整块删掉
#      再 --merge 会重新填入默认值。
#
# 结构与指针（version 2 的变化）：骨架不再读成书产物。章、节与插图都由 deck.yaml 与源
# markdown 复算，节号与成书目录逐字相同，但**不再有页码**——候选表的指针列与帧脚写"§7.4"
# 这样的节号（deck.yaml 的 pointers: none 时连指针列都不发）。于是书重编不会让这份骨架作废；
# fingerprint 里记的是 deck.yaml 与各源文件的内容摘要。
#
# 旧骨架（version 1：读成书 .toc、页数由预期总页数控制）用
# `outline --spec <deck.yaml> --merge` 迁移：budget、sections[].speak、逐帧 picks／review／
# note、固定页的 picks／figure／notes 与整块 theme 照旧逐字保留，派生字段刷新。--merge 可以
# 连跑，第二次与第一次逐字节相同。
#
# 时间不在模型里：页数换算不到时间。场合与时长请写在下面这一行注释里，工具不读、也不会
# 覆盖它；要印在封面上的场合请填 deck.occasion。
# 场合/时长：<自己填，工具不读>
#
# 字段约定：chapter 是短号（'8'、'A'），label 是纸面写法（第八章、附录 A）；sections 只列
# 成书目录给出的层级，附录 A 因此比正文章多出一层 subsection。
"""

# 丢弃比例的口语说法，粗到四分之一；精确值另行给出百分比。
_DISCARD_WORDS = ("约四分之一", "约一半", "约四分之三", "几乎全部")


def escape_cell(text: str) -> str:
    """表格单元格转义：竖线会截断表格，换行会截断行。"""
    return text.replace("|", "\\|").replace("\n", " ")


def _discard_words(share: Fraction) -> str:
    """把丢弃比例说成一句人话。粗到四分之一，因为再细就是假精度。"""
    index = min(3, max(0, int(float(share) * 4 + 0.5) - 1))
    return _DISCARD_WORDS[index]


def _allocation_rows(
    structure: DeckStructure, budgets: Sequence[int], counts: Mapping[str, int]
) -> str:
    rows: List[str] = []
    for order, (chapter, budget) in enumerate(zip(structure.chapters, budgets), 1):
        rows.append(
            f"| {order} | {chapter.label}　{escape_cell(chapter.title)} "
            f"| {chapter.material_chars} | {budget} | {counts.get(chapter.number, 0)} |"
        )
    return "\n".join(rows)


def _capacity_lines(capacity: Capacity, theme: Theme) -> List[str]:
    """容量与取舍：三条陈述，读者是准备圈候选的人。

    一页的字/行 × 行/数由**主题**量出来（:func:`primer.slides.theme.frame_metrics`），
    不写成死数字：换画布或正文字号时这一段跟着变。
    """
    metrics = frame_metrics(theme)
    body = theme.level("body")
    median_fit = capacity.points_per_page(capacity.median_chars)
    low, high = capacity.points_per_content_page_range
    ratio_low, ratio_high = capacity.candidates_per_point_range
    longest = (
        f"最长的一条 {capacity.max_chars} 字超过一页容量（{capacity.per_page_chars} 字），"
        "放不进任何一页，只能拆开或压成两条。"
        if capacity.max_chars > capacity.per_page_chars
        else ""
    )
    return [
        "## 二、容量与取舍",
        "",
        f"- **页面容量**：一页 {capacity.per_page_chars} 字"
        f"（{theme.width_bp:g} × {theme.height_bp:g} bp 画布、{body.size:g} pt 中文正文，"
        f"{metrics.chars_per_line} 字/行 × {metrics.lines} 行；见 outline.yaml 的 theme 块）"
        f" × 内容页 {capacity.content_pages} 页 = **{capacity.display_chars} 字**。"
        "这是这份骨架能显示的字数上限，是事实。",
        f"- **取舍比**：内容页 {capacity.content_pages} 页 × 每页 "
        f"{low}–{high} 个要点（默认 {capacity.points_per_content_page}）"
        f" = **{capacity.points_needed_range[0]}–{capacity.points_needed_range[1]} 个要点**；"
        f"候选表给出 **{capacity.candidates}** 条 → **"
        f"{float(ratio_low):.1f} : 1 … {float(ratio_high):.1f} : 1**"
        f"（按默认每页 {capacity.points_per_content_page} 个要点是 "
        f"{float(capacity.candidates_per_point):.1f} : 1）。**人必须丢掉"
        f"{_discard_words(capacity.discard_share)}"
        f"（{float(capacity.discard_share) * 100:.0f}%，按默认要点数算）**——候选多是好事，"
        "选才是本工具接下来要你做的事。",
        f"- **每页建议条数**：候选 {capacity.candidates} 条，长度中位 "
        f"{capacity.median_chars} 字、最短 {capacity.min_chars} 字、最长 "
        f"{capacity.max_chars} 字；一页 {capacity.per_page_chars} 字，按中位长度放得下 "
        f"{median_fit} 条，若一页排 {capacity.points_per_content_page} 条则每条可写到 "
        f"{capacity.per_page_chars // capacity.points_per_content_page} 字。"
        + longest
        + "只作描述，不设通过线。",
        "",
    ]


def _chapter_candidates(
    chapter: Chapter, budget: int, items: Sequence[cand.Candidate], pointers_on: bool = True
) -> str:
    lines = [
        f"### {chapter.label}　{chapter.title}",
        "",
        f"材料 {chapter.material_chars} 字，预算 {budget} 页；候选 {len(items)} 条。",
        "",
    ]
    if not items:
        lines.extend(["（本章无候选。）", ""])
        return "\n".join(lines)
    lines.append(
        CANDIDATE_HEADER.rstrip("\n").format(pointer="页面指针 | " if pointers_on else "")
    )
    for item in items:
        lines.append(
            f"| {item.id} | {item.type} | {item.signal_codes} | {item.citations} | "
            f"{item.length} | {item.pointer} | {escape_cell(item.display)} |"
        )
    lines.append("")
    return "\n".join(lines)


def build_candidates_md(
    structure: DeckStructure,
    config: DeckConfig,
    budgets: Sequence[int],
    capacity: Capacity,
    by_chapter: Mapping[str, Sequence[cand.Candidate]],
    theme: Optional[Theme] = None,
) -> str:
    """渲染 ``candidates.md``。容量那一段的"字/行 × 行"由 ``theme`` 量出来。

    ``deck.yaml`` 的 ``pointers: none`` 时（``structure.pointers``）候选表不发指针列，
    前面那段说明也换一句——指针是"这一句出自哪一节"，不发就不提。
    """
    theme = theme or default_theme()
    pointers_on = structure.pointers != "none"
    counts = {number: len(items) for number, items in by_chapter.items()}
    total = sum(counts.values())
    total_material = structure.material_chars
    types: Dict[str, int] = {}
    for items in by_chapter.values():
        for name, value in cand.count_by_type(items).items():
            types[name] = types.get(name, 0) + value
    pointer_note = (
        "指针是该句所属**节的节号**（``§2.1``；章首、第一节之前的句子写章标签）——只到节，"
        "不假装到页，也不随书重编而失效。"
        if pointers_on
        else "这份 deck 的 ``deck.yaml`` 写了 ``pointers: none``：**不发指针列**，"
        "也不在帧脚上印出处。"
    )
    out: List[str] = [
        f"# {structure.book_line} · 幻灯片主题句候选表",
        "",
        "本表由 `python -m primer.slides outline` 生成，全程确定性：无模型、无网络。"
        "同一份输入必定产出同一份本文件。",
        "",
        "段首句是候选——中文技术写作把主题句放在段首，本书对这一体例执行得格外整齐。"
        + pointer_note
        + "引用数用正则数 `[n]` 标记得出，不读 `claims.json`"
        "（那份产物存的是段落的论断—引用对，一句话可以散成多对，反推不出“这句有几个"
        "标记”）。",
        "",
        "## 一、页数分配",
        "",
        f"控制参数：各章预算之和 = 内容页 {capacity.content_pages} 页；固定页 "
        f"{config.overhead} 页（" + "、".join(f"{page.label} {page.count}" for page in config.pages)
        + f"），全片 {capacity.content_pages + config.overhead} 页。"
        "每章预算的默认建议按**候选材料占比**给出，改了 outline.yaml 里的 budget 即成事实；"
        "没有时间参数：场合与时长写在 `outline.yaml` 的注释里由人自己填，工具不读。",
        "",
        ALLOCATION_HEADER.rstrip("\n"),
        _allocation_rows(structure, budgets, counts),
        f"| | **固定页**（开场/目录/横向/讨论/备份） | | **{config.overhead}** | |",
        f"| | **合计** | {total_material} | **{capacity.content_pages + config.overhead}** "
        f"| **{total}** |",
        "",
    ]
    out.extend(_capacity_lines(capacity, theme))
    out.extend(
        [
            "## 三、计数",
            "",
            f"- 候选总数：**{total}**（按类型："
            + "、".join(f"{name} {value}" for name, value in types.items())
            + "）",
            f"- 材料（正文字数）：{total_material}；逐章候选："
            + "、".join(
                f"{chapter.label} {counts.get(chapter.number, 0)}" for chapter in structure.chapters
            ),
            "",
            "## 四、逐章候选",
            "",
            "排序：长句降一档后的信号数降序 → 引用数降序 → 章内位置升序；130 字以上的句子"
            "按少一个信号计，免得放不满一页的长句被摆成首选。信号码：B 粗体、D 数字、C 引用"
            "标记、F 预报短语（以下/如下/三条/两个/判据/清单/小结/综上/至此/本节/本章/第一/"
            "第二/第三）、S 短句（≤120 字）。类型：小结 → 框定 → 断言 → 数据，一条都不命中"
            "的段首句按断言计。句子列清掉了跨句残留的粗体标记，原文原样留在抽取结果里。",
            "",
        ]
    )
    for chapter, budget in zip(structure.chapters, budgets):
        out.append(
            _chapter_candidates(
                chapter, budget, by_chapter.get(chapter.number, ()), pointers_on
            )
        )
    return "\n".join(out).rstrip("\n") + "\n"


def build_outline_document(
    structure: DeckStructure,
    config: DeckConfig,
    deck: str,
    budgets: Sequence[int],
    default_budgets: Sequence[int],
    capacity: Capacity,
    by_chapter: Mapping[str, Sequence[cand.Candidate]],
    fingerprint: Optional[Mapping[str, object]] = None,
    theme: Optional[Theme] = None,
) -> Mapping[str, object]:
    """组装 ``outline.yaml`` 的数据结构。

    ``budgets`` 是生效的逐章页数，``default_budgets`` 是本次算出的默认建议——两者都写进
    骨架，``check`` 据此如实报告"这一章的预算来自人工还是默认"。
    """
    chapters: List[Mapping[str, object]] = []
    for index, (chapter, budget) in enumerate(zip(structure.chapters, budgets)):
        chapters.append(
            {
                "chapter": chapter.number,
                "label": chapter.label,
                "title": chapter.title,
                "volume": chapter.volume_id,
                "source": chapter.source,
                "material_chars": chapter.material_chars,
                "budget": budget,
                "budget_default": (
                    default_budgets[index] if index < len(default_budgets) else budget
                ),
                "candidates": len(by_chapter.get(chapter.number, ())),
                # 章级启发：方向性提示，供人选点；人写，可空串。
                "hint": "",
                "sections": [
                    {
                        "level": section.level,
                        "number": section.number,
                        "title": section.title,
                        "speak": True,
                        # 节级启发，与章级同义。
                        "hint": "",
                    }
                    for section in chapter.sections
                ],
                # 一帧一页、长度等于 budget。每帧自带 picks（三种形状，规则同章一级）、
                # 人审三态 review 与定向提示 note；生成时是空壳，由人自己填或 --merge 迁移。
                "frames": [dict(frame_entry()) for _ in range(max(budget, 0))],
                # 留空表示按默认：这一章的第 n 页配它的第 n 幅图（附录 A 有 5 幅）。
                # 填一个编号（如 '2-3'）就整章都用那一幅；填空串 '' 表示这一章不上图。
                "figure": None,
                "notes": "",
            }
        )
    content_pages = sum(budgets)
    document: Dict[str, object] = {
        "version": OUTLINE_VERSION,
        "deck": {
            "title": structure.book_line,
            "deck": deck,
            # 报告人与场合只印在封面上；留空则不印。工具读、不改。
            "presenter": "",
            "occasion": "",
            "backup": config.backup,
            "capacity_per_page": config.capacity_per_page,
            "min_per_chapter": config.min_per_chapter,
            "max_per_chapter": config.max_per_chapter,
        },
        # 画布、字号与配色：数据，不是代码（见 primer.slides.theme）。
        "theme": (theme or default_theme()).as_dict(),
        "model": {
            "overhead_pages": config.overhead,
            "body_pages": content_pages,
            "total_pages": content_pages + config.overhead,
            "material_chars": structure.material_chars,
            "candidate_chars": sum(item.length for items in by_chapter.values() for item in items),
        },
        "pages": [entry.as_dict() for entry in page_entries(config.backup)],
        "capacity": capacity.as_dict(),
        "chapters": chapters,
    }
    if fingerprint is not None:
        document["fingerprint"] = dict(fingerprint)
    return document


BUDGET_LINE_RE = re.compile(r"^(?P<indent>\s*)budget: (?P<value>-?\d+)\s*$")


def budget_comment(default_budget: int, candidate_chars: int, total_chars: int) -> str:
    """一行预算的来路：工具按候选材料占比算出的默认值。

    写成 YAML 行尾注释而不是一个新字段：人改了 ``budget`` 之后，值会变、注释不动，
    "哪几个数字是工具算的、哪几个是人改的"一眼可分。
    """
    share = (candidate_chars / total_chars) if total_chars else 0.0
    return (
        f"# 默认 {default_budget}：候选 {candidate_chars} 字 / {total_chars} 字"
        f"（占 {share:.0%}）"
    )


def _annotate_budgets(text: str, notes: Sequence[str]) -> str:
    """给每章 ``budget`` 那行加尾注释，说明它的默认值是怎么来的。"""
    lines: List[str] = []
    cursor = 0
    for line in text.splitlines():
        if cursor < len(notes) and BUDGET_LINE_RE.match(line):
            line = f"{line}   {notes[cursor]}"
            cursor += 1
        lines.append(line)
    return "\n".join(lines) + "\n"


def dump_outline(
    document: Mapping[str, object],
    default_budgets: Optional[Sequence[int]] = None,
    candidate_chars: Optional[Sequence[int]] = None,
) -> str:
    """把 ``outline.yaml`` 的数据结构落成文本（注释在前、每行预算带尾注释）。"""
    body = yaml.safe_dump(
        document,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        width=10**6,
    )
    if default_budgets is not None:
        totals = list(candidate_chars or [])
        total = sum(totals)
        notes = [
            budget_comment(
                default_budgets[index] if index < len(default_budgets) else 0,
                totals[index] if index < len(totals) else 0,
                total,
            )
            for index in range(len(default_budgets))
        ]
        body = _annotate_budgets(body, notes)
    return YAML_COMMENT + body
