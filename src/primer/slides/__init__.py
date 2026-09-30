# -*- coding: utf-8 -*-
"""primer-slides：把一份写完的书稿变成一套可演示的幻灯片。

六个确定性步骤里的四个，加一道可选的模型辅助与一道版面质检：

* ``outline``：读 ``<deck 目录>/deck.yaml`` 与它列的源 markdown（**不读成书产物**），
  复算章、节、插图，产出两件人可编辑的东西——``candidates.md``（逐章主题句候选表）供人
  圈选，``outline.yaml``（幻灯片骨架）供人编辑。已存在的骨架不会被静默覆盖。
* ``check``：拿当前规格与源文件校验一份骨架，只读不写，error 级发现以非零码退出。
* ``build``：把校验过的骨架编成 ``slides.tex``，跑 xelatex 与 xdvipdfmx，再用成书同一
  个日志解析器复核（缺字是硬失败，overfull 报警告并点出页码），最后核对 PDF 页数与帧序。
* ``select``（唯一联网的一步）：让模型为各帧圈要点的一遍初稿，一章一次请求，从该章候选
  池里取原句（模式 3 可提炼新句，数字须逐字见于出处），只填不覆盖。详见
  :mod:`primer.slides.select`。
* ``golden``：从 ``candidates.md`` 里确定性筛"读起来像结论"的句子，落成 ``golden.md``
  递给**人**拍板。不联网、不写骨架。详见 :mod:`primer.slides.golden`。
* ``inspect``：对已生成的 ``slides.pdf`` 做版面质检（确定性几何／字体层 + 可选的逐页视觉层）。

控制参数只有一个——**每章的预算页数**（``chapters[].budget``）——总页数 = 各章预算之和 +
固定页（开场 2、目录 1、横向 3、讨论 3、备份 N），加出来是多少就是多少。``outline`` 只按
候选材料占比给默认建议，``check`` 只报告每章的预算来自人工还是默认。**没有页码，也没有时长**：
指针写节号（``§7.4``），幻灯片不假装知道纸面页数；页数与时长之间不存在经过检验的换算率，
场合与时长只作为注释留给人自己填。详见 :mod:`primer.slides.plan`。

幻灯片上的句子全部来自书稿本身（``candidates.md`` 里人圈出的原句，经成书同一个行内渲染器
``primer.book.tex.render_inline``）。``outline``／``check``／``build`` 只做**选、移、排、查**
四件事，从不自己写正文；``select`` 是唯一会写字的一步，而且写下的每一句都要能回溯到候选表
（模式 3 的提炼句记下 ``derived_from``）。

产物只落在 ``<工程根>/_primer/slides/<deck>/`` 下；输入规格 ``deck.yaml`` 由人写在那里，
工具只读不写。
"""

from __future__ import annotations

__all__ = [
    "BUDGET_FROM_DEFAULT",
    "BUDGET_FROM_HUMAN",
    "DEFAULT_CAPACITY_PER_PAGE",
    "BuildResult",
    "Capacity",
    "DeckConfig",
    "DeckPlan",
    "DeckSpec",
    "DeckStructure",
    "Finding",
    "PageEntry",
    "SlidesError",
    "Theme",
    "build_deck",
    "check_outline",
    "check_report_lines",
    "compose_frames",
    "find_spec",
    "default_theme",
    "load_spec",
    "read_structure",
    "read_theme",
    "resolve_spec",
    "validate_outline",
]

from .build import BuildResult, DeckPlan, compose_frames
from .build import run as build_deck
from .plan import (
    BUDGET_FROM_DEFAULT,
    BUDGET_FROM_HUMAN,
    DEFAULT_CAPACITY_PER_PAGE,
    Capacity,
    DeckConfig,
    PageEntry,
    SlidesError,
)
from .spec import DeckSpec, find_spec, load_spec
from .structure import DeckStructure, read_structure
from .theme import Theme, default_theme, read_theme
from .outline import resolve_spec
from .validate import Finding, check_outline, check_report_lines, validate_outline
