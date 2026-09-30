# -*- coding: utf-8 -*-
"""primer-scene：把一份声明式的场景规格变成 Blender 场景，并无声地渲染出来。

**契约**（``_primer/scene/mission_layout.yaml``）是唯一信息源。它写什么，场景就是什么：
天体、显示半径、压缩指数、星带、航迹节点、相机、渲染档位、贴图与署名，全在里面。四个
子命令各管一段，且都不越界：

* ``fetch`` —— 规格声明的贴图备齐（**唯一联网的一步**），逐条记进
  ``assets/credits.yaml``：文件名、URL、sha256、字节数、许可、署名行、下载日期。
* ``build`` —— 发射 ``build/scene.py``（可读、可 diff、逐字节确定：没有时间戳，键排序，
  次序定死），交给无头 Blender 跑，日志一字不改地存进 ``build/``，再立刻过一遍验收。
* ``check`` —— 只读：命名契约、天体自定义属性、航迹自己的四条硬约束（不许穿进天体、顺访
  既不拐弯也不许离太远、借力点的近木点与转角就是声明的那个数）、任务层三条（占位点不许
  落进天体、点位与任务标签要在画面里、任务线必须声明过）、成图的尺寸与亮度、贴图账本、
  指纹。
* ``report`` —— 把布局算出来的数字摊开给人核对。

**尺度不在发射器里**：径向压缩、世界单位换算、每体放大倍数、黄道面落位全部收在
:mod:`primer.scene.layout` 这一个纯函数模块。发射器只消费它的输出，并把放大倍数等四个量
写进每个行星对象的自定义属性——``check`` 拿同一份输出反过来核对，所以"图是按哪套比例画
的"永远查得到。

**产物边界**：所有写入都落在 ``<工程根>/_primer/scene/`` 下（``primer.paths`` 的输出边界
策略），工程目录其余部分一律只读；记进产物的路径都写成相对工程根的形式。

**正式渲染需要人来开门**：``--final`` 不带 ``--allow-final`` 时连 Blender 都不会启动。
预览先给人看，是工作说明里的第一条规矩，这里把它写成了代码。
"""

from __future__ import annotations

__all__ = [
    "COLLECTIONS",
    "AssetEntry",
    "BodyPlacement",
    "BuildError",
    "BuildResult",
    "FetchResult",
    "Finding",
    "PNGStats",
    "SceneBuildPlan",
    "SceneError",
    "SceneSpec",
    "check_report_lines",
    "check_scene",
    "compute_fingerprint",
    "expected_materials",
    "expected_objects",
    "fetch_assets",
    "layout_report_lines",
    "load_spec",
    "parse_spec",
    "plan",
    "read_ledger",
    "read_png",
    "read_report",
    "render_script",
    "run_build",
]

from .assets import AssetEntry, FetchResult, read_ledger
from .assets import fetch as fetch_assets
from .build import BuildError, BuildResult, SceneBuildPlan
from .build import run as run_build
from .checks import Finding, PNGStats, check_report_lines, check_scene, layout_report_lines, read_png, read_report
from .emit import COLLECTIONS, expected_materials, expected_objects, render_script
from .fingerprint import compute_fingerprint
from .layout import BodyPlacement, plan
from .spec import SceneError, SceneSpec, load_spec, parse_spec
