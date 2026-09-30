# -*- coding: utf-8 -*-
"""发射器：规格 + 布局 → 一份可在 Blender 里跑的脚本。

产物是一段**可读、可 diff、逐字节确定**的 Python 脚本，落在
``<工程根>/_primer/scene/build/scene.py``：数据段是布局算好的数（JSON），后面跟着一段
静态的构建代码。把时间戳写进去、让字典按插入序打印、或用进程随机数决定落位，都会让
"这次和上次是不是同一版"无从判断——所以这个文件里没有时间，浮点一律走 JSON 的规范表示，
所有次序都定死（连撒石块的种子都由规格内容摘要推出）。

**命名契约在这里定义一次**（:func:`object_contract` / :func:`material_contract`），
``checks.py`` 导入同一份表去核对。名字写在两处就一定会漂移，所以只写一处。

几处实现选择，都是为"854x480 的预览要一眼看懂"服务的：

* **大气壳默认走 Fresnel 边缘辉光**（``atmosphere.mode: rim``，规格缺省值）。体积散射在
  行星只有几十像素高的时候只剩一层雾，边缘辉光反而能把晨昏线勾出来，而且便宜得多；
  ``volume`` 模式照规格实现，留给大图或近景。
* **标签与轨迹共用 ``MAT_traj``**，不自造 ``MAT_label``：契约里列了几个材质就只建几个，
  何况琥珀色注释本就是这张图的既定语言。
* **暖色主光源留在 ``SUN`` 里**（``SUN_sun_light`` 点光 + ``SUN_fill_light`` 平行补光）。
  太阳本体自发光照到海王星（地球的 5.48 倍显示轨道半径）只剩万分之几，单靠它外行星全黑；
  点光按平方反比照近处、平行补光把外圈托住，这是论证图的做法，也对应工作说明里
  "配 Point Light 作为主光源"。这两个对象一并写进契约，所以不会有"契约外对象"。
* **相机不进任何 collection**（留在场景主 collection 里）：契约给了七个 collection、
  一个不多，所以 :data:`CAMERA_OBJECT` 单列，``checks`` 反过来核对"七个 collection 之外
  只剩相机一个对象"。
* **光照强度、标签尺寸这些常数写在本模块顶部的 UPPER_SNAKE 里**，不进规格：规格是任务的
  信息源，不是渲染调参的地方；改构图应在规格里改，改灯位在这里改。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Mapping, Sequence, Tuple

from .fingerprint import Fingerprint
from .layout import (
    LEGEND_TRAJECTORY_ID,
    BandPlacement,
    BodyPlacement,
    NodePlacement,
    camera_placement,
    label_extent,
    legend_placements,
    mission_placements,
    plan,
    trajectory_polyline,
)
from .spec import Atmosphere, Band, Body, SceneSpec
from ..paths import output_dir_for

__all__ = [
    "ATMOSPHERE_SUFFIX",
    "BACKGROUND_COLLECTION",
    "BAND_PREFIX",
    "CLOUDS_SUFFIX",
    "LABEL_PREFIX",
    "PLANET_PREFIX",
    "SUN_PREFIX",
    "TRAJECTORY_PREFIX",
    "BANDS_COLLECTION",
    "BELT_MATERIAL",
    "CAMERA_OBJECT",
    "COLLECTIONS",
    "LABELS_COLLECTION",
    "MINOR_PREFIX",
    "MISSION_PREFIX",
    "MISSIONS_COLLECTION",
    "PLANETS_COLLECTION",
    "REPORT_NAME",
    "SCRIPT_NAME",
    "STARFIELD_MATERIAL",
    "STARFIELD_OBJECT",
    "SUN_COLLECTION",
    "SUN_MATERIAL",
    "TRAJECTORIES_COLLECTION",
    "TRAJECTORY_MATERIAL",
    "atmosphere_material_name",
    "band_object_name",
    "body_object_name",
    "clouds_material_name",
    "expected_materials",
    "expected_objects",
    "ARROW_SUFFIX",
    "HALO_SUFFIX",
    "LABEL_HALO_MATERIAL",
    "LABEL_MATERIAL",
    "LabelPlacement",
    "arrow_object_name",
    "label_extent",
    "label_halo_name",
    "label_object_name",
    "label_placements",
    "legend_dot_name",
    "legend_label_name",
    "minor_material_name",
    "minor_object_name",
    "mission_data",
    "mission_dot_name",
    "mission_label_name",
    "mission_material_name",
    "material_contract",
    "object_contract",
    "planet_atmosphere_name",
    "planet_clouds_name",
    "planet_material_name",
    "planet_object_name",
    "render_script",
    "scene_data",
    "scene_seed",
    "sun_light_name",
    "sun_object_name",
    "trajectory_object_name",
    "write_script",
]

# ---------------------------------------------------------------- 命名契约

COLLECTIONS = ("SUN", "PLANETS", "BANDS", "TRAJECTORIES", "MISSIONS", "LABELS", "BACKGROUND")
SUN_COLLECTION = "SUN"
PLANETS_COLLECTION = "PLANETS"
BANDS_COLLECTION = "BANDS"
TRAJECTORIES_COLLECTION = "TRAJECTORIES"
MISSIONS_COLLECTION = "MISSIONS"
LABELS_COLLECTION = "LABELS"
BACKGROUND_COLLECTION = "BACKGROUND"

# 对象名的前缀（与下面的名字构造函数同源）：``checks`` 靠它把越界对象归类到
# "天体／标签／星带／航迹／任务点"，所以前缀不能在别处另写一份。
SUN_PREFIX = "SUN_"
PLANET_PREFIX = "PLANET_"
MINOR_PREFIX = "MINOR_"
BAND_PREFIX = "BAND_"
TRAJECTORY_PREFIX = "TRAJ_"
MISSION_PREFIX = "DOT_"
LABEL_PREFIX = "LABEL_"
ATMOSPHERE_SUFFIX = "_ATMO"
CLOUDS_SUFFIX = "_CLOUDS"
ARROW_SUFFIX = "_ARROW"
HALO_SUFFIX = "_HALO"

SUN_MATERIAL = "MAT_sun_emission"
TRAJECTORY_MATERIAL = "MAT_traj"
BELT_MATERIAL = "MAT_belt"
STARFIELD_MATERIAL = "MAT_starfield"
# 标签的白字与其深色描边各用一份材质（参考图的标签就是"白字 + 深描边"）。
LABEL_MATERIAL = "MAT_label"
LABEL_HALO_MATERIAL = "MAT_label_halo"
STARFIELD_OBJECT = "BG_starfield"
# 契约只给七个 collection，相机因此留在场景主 collection 里，单列出来供核对。
CAMERA_OBJECT = "Camera_Main"

SCRIPT_NAME = "scene.py"
REPORT_NAME = "scene_report.json"

# ---------------------------------------------------------------- 调参常数

# 撒石块的球面细分数：1 已是二十面体，石块在 854x480 上只有一两像素。
BELT_SUBDIVISIONS = 1
# 行星球细分：够圆，又不至于让 Cycles 在几百像素高的球上白算。
PLANET_SEGMENTS = 64
PLANET_RINGS = 32
# 星带的形状：竖向厚度占环带半径的比例，以及径向分布的高斯宽度。原来的 0.18 / 均匀分布
# 让 900 块石头摊在一圈很宽的带里，看上去是"满屏彩屑"而不是一条带；收紧到 0.05 并把
# 半径按中间密、两侧疏的高斯撒，才读得出"带"。石头本身由规格的 size_range 决定，这里
# 只改散布形状。
BELT_THICKNESS_RATIO = 0.05
BELT_RADIAL_SIGMA = 0.3
# 大气壳边缘辉光（``rim`` 模式）。可见性取决于 **Fresnel 曲线的宽窄**而不是亮度：原来用
# IOR 1.45 的 Fresnel，辉光只出现在半径最外的一两个百分点里，行星在画面上只有 8 像素时
# 这条环不到 0.1 像素，等于不存在。改用 Layer Weight 的宽过渡 + 幂次收紧，让辉光覆盖
# 外圈一成到两成半径，再靠合成器的辉光把它晕开。
ATMOSPHERE_RIM_OPACITY = 1.0
ATMOSPHERE_RIM_STRENGTH = 8.0
# 亮度 ∝ (1-cosθ)^power（自行点积得到，见脚本里的剖面表）。power 6 时半径八成处 0.4%、
# 九成处 3%、边缘 55%；乘上强度之后只有最外圈越过合成器的 Fog Glow 阈值 1.0。
ATMOSPHERE_RIM_POWER = 6.0
# 轨迹自发光强度：4.0 在 AgX 下把琥珀色冲成奶油白（颜色没了），降到 1.6 才留得住颜色。
TRAJECTORY_STRENGTH = 1.6
# 终点箭头：锥尖指向终点，退让距离**按目标球体半径**算（radius × 这个系数 + 半个箭头
# 长度），所以球大的天体（海王星 4.2）箭头也自动退得更远，不会埋进球里。尺寸按"一眼看得出
# 方向"标定：长度 18 × 航迹宽度、半径 7 × 航迹宽度。
ARROW_STANDOFF_FACTOR = 1.55
# 箭头取"接近段上多远的一点"来定来向：折线上倒退这么多点。
ARROW_APPROACH_POINTS = 10
ARROW_LENGTH_FACTOR = 18.0
ARROW_RADIUS_FACTOR = 7.0
ARROW_SEGMENTS = 24
# 背景天球：半径远大于场景，材质只对相机可见（不参与照明）。强度 8 是量出来的：星图是
# 黑底稀星（线性均值 0.008、p99 0.07），按 1.0 放出去在 AgX 下整块压成黑，8 才看得出星。
STARFIELD_RADIUS = 6000.0
STARFIELD_STRENGTH = 8.0
STARFIELD_SEGMENTS = 64
STARFIELD_RINGS = 32
# 天球贴图是 sRGB 的，乘上增益之后仍要落在 AgX 能看见的区间里；强度、底色与银河带落点
# 都由规格的 ``background`` 块给出（见 spec.py 的 BackgroundSpec）。
WORLD_STRENGTH = 0.35
# 主光（平行光，挂在太阳的显示位置、朝序列照）与补光（平行光，来自侧上方）。
# 照度按"天体表面落在 AgX 的中段"标定：地表 radiance ≈ 反照率 × 照度 / π，1.2 的照度
# 让 0.3–0.5 反照率的地表落在 0.15 上下，颜色与纹理都留得住（早先点光 2600 W 时地表
# radiance 超过 1.5，AgX 把整颗球推成脱色的白盘）。
KEY_LIGHT_ENERGY = 1.6
KEY_LIGHT_ANGLE_DEG = 3.0
FILL_LIGHT_ENERGY = 0.45
FILL_LIGHT_ANGLE_DEG = 25.0
FILL_LIGHT_ELEVATION_DEG = 55.0
FILL_LIGHT_AZIMUTH_DEG = 250.0
# 标签：天体标签在球体上方，节点标签另给一套偏移，两套不叠在一起。
BODY_LABEL_SIZE = 2.0
NODE_LABEL_SIZE = 1.3
BODY_LABEL_CLEARANCE = 2.4
NODE_LABEL_CLEARANCE = 2.4
LABEL_EXTRUDE = 0.02
# 标签用近白自发光（与轨迹的琥珀色分开），并各带一个深色描边层：描边层是同一段文字放大
# 一点点、往相机反方向退一丝放在后面，白字压在上面就得到参考图那种"深色描边"。
# 字**压在合成器辉光阈值（1.0）之下**：2.2 时白字越过阈值，Fog Glow 把每个字晕成一团，
# 描边那一圈被光晕糊住，看上去就是"文字后面一块浅色方块"——比不描边还糟。0.95 仍然白，
# 但不再发光，描边才是描边。
LABEL_STRENGTH = 0.95
LABEL_COLOR = (0.94, 0.96, 1.0)
# 描边要够黑：早先的 0.004 在 AgX 下仍能看出一点灰，压到千分之一以下才是"暗"。
LABEL_HALO_COLOR = (0.0006, 0.0010, 0.0022)
LABEL_HALO_OFFSET_RATIO = 0.16
LABEL_HALO_DEPTH = 0.08
# 太阳：贴图 × 规格的色调，再叠一层临边昏暗（参考图里的太阳不是一块平白）。
SUN_LIMB_EDGE = 0.32
# 小天体（半人马天体）：二十面体细分与顶点推挤幅度，做出一块不规则岩块。
MINOR_SUBDIVISIONS = 3
MINOR_RELIEF = 0.34
ROCK_BUMP_STRENGTH = 0.35
# 合成器辉光（``blender.md`` 第四阶段）：Fog Glow，阈值 1.0，只让太阳与真正亮的元素发光。
GLARE_THRESHOLD = 1.0
GLARE_SIZE = 0.4
GLARE_STRENGTH = 1.0
GLARE_SMOOTHNESS = 0.1
# 每段曲线的求值精度；只是曲线细分，不影响控制点位置。
CURVE_RESOLUTION_U = 12
CURVE_BEVEL_RESOLUTION = 3
# 航天器占位点：一颗低细分的自发光小球。点只有几像素，细分高了是白算；16x8 在近景
# 特写下仍是个圆。
MISSION_DOT_SEGMENTS = 16
MISSION_DOT_RINGS = 8
# 1 天文单位 = 149 597 870.7 km，用来把真实半径折算成 AU 供放大倍数使用。
AU_KM = 149597870.7


def sun_object_name() -> str:
    return f"{SUN_PREFIX}sun"


def sun_light_name() -> str:
    return "SUN_sun_light"


def fill_light_name() -> str:
    return "SUN_fill_light"


def planet_object_name(body_id: str) -> str:
    return f"{PLANET_PREFIX}{body_id}"


def minor_object_name(body_id: str) -> str:
    """小天体（岩质、无贴图，如半人马天体）：与行星分开命名，免得看清单的人误以为它是行星。"""
    return f"{MINOR_PREFIX}{body_id}"


def body_object_name(body: Body) -> str:
    """一个天体的对象名——按种类分派，只此一处。"""
    if body.kind == "star":
        return sun_object_name()
    if body.kind == "minor":
        return minor_object_name(body.id)
    return planet_object_name(body.id)


def planet_atmosphere_name(body_id: str) -> str:
    return f"{planet_object_name(body_id)}{ATMOSPHERE_SUFFIX}"


def planet_clouds_name(body_id: str) -> str:
    return f"{planet_object_name(body_id)}{CLOUDS_SUFFIX}"


def band_object_name(band_id: str) -> str:
    return f"{BAND_PREFIX}{band_id}"


def trajectory_object_name(node_id: str) -> str:
    return f"{TRAJECTORY_PREFIX}{node_id}"


def mission_dot_name(cluster_id: str, dot_id: str) -> str:
    """一个航天器占位点的对象名。``DOT_`` 前缀同时是取景判据的分组依据。"""
    return f"{MISSION_PREFIX}{cluster_id}_{dot_id}"


def legend_dot_name(identifier: str) -> str:
    return f"{MISSION_PREFIX}legend_{identifier}"


def legend_label_name(identifier: str) -> str:
    return label_object_name(f"legend_{identifier}")


def arrow_object_name(node_id: str) -> str:
    """终点箭头：与航迹同属 ``TRAJECTORIES``，名字挂在它指向的那个节点上。"""
    return f"{TRAJECTORY_PREFIX}{node_id}{ARROW_SUFFIX}"


def label_object_name(identifier: str) -> str:
    return f"{LABEL_PREFIX}{identifier}"


def label_halo_name(identifier: str) -> str:
    """标签的描边层：同一段文字放大一丝、退到白字后面。"""
    return f"{LABEL_PREFIX}{identifier}{HALO_SUFFIX}"


def mission_label_name(cluster_id: str) -> str:
    """任务簇的**组标签**（一组点位共用一条，19 个点各配一条无法读）。"""
    return label_object_name(f"mission_{cluster_id}")


def planet_material_name(body_id: str) -> str:
    return f"MAT_planet_{body_id}"


def minor_material_name(body_id: str) -> str:
    return f"MAT_minor_{body_id}"


def atmosphere_material_name(body_id: str) -> str:
    return f"MAT_atmo_{body_id}"


def clouds_material_name(body_id: str) -> str:
    return f"MAT_clouds_{body_id}"


def mission_material_name(line_id: str) -> str:
    """一条任务线的自发光材质：簇里的点位与图例的色点共用它。"""
    return f"MAT_mission_{line_id}"


def object_contract(spec: SceneSpec) -> Dict[str, str]:
    """对象名 → 所属 collection。发射器造的就是这些，checks 核对的也是这些。"""
    contract: Dict[str, str] = {
        sun_object_name(): SUN_COLLECTION,
        sun_light_name(): SUN_COLLECTION,
        fill_light_name(): SUN_COLLECTION,
        STARFIELD_OBJECT: BACKGROUND_COLLECTION,
    }
    for body in spec.bodies:
        if body.kind == "minor":
            contract[minor_object_name(body.id)] = PLANETS_COLLECTION
        elif body.kind != "star":
            contract[planet_object_name(body.id)] = PLANETS_COLLECTION
        if body.atmosphere is not None:
            contract[planet_atmosphere_name(body.id)] = PLANETS_COLLECTION
        if body.clouds is not None:
            contract[planet_clouds_name(body.id)] = PLANETS_COLLECTION
    for band in spec.bands:
        contract[band_object_name(band.id)] = BANDS_COLLECTION
    destination = spec.trajectory.destination.id
    contract[trajectory_object_name(destination)] = TRAJECTORIES_COLLECTION
    contract[arrow_object_name(destination)] = TRAJECTORIES_COLLECTION
    for identifier in [body.id for body in spec.bodies] + [node.id for node in spec.trajectory.nodes]:
        contract[label_object_name(identifier)] = LABELS_COLLECTION
        contract[label_halo_name(identifier)] = LABELS_COLLECTION
    if spec.missions is not None:
        # 点位自成一组（MISSIONS）：它是新加的一层。标签仍然进 LABELS——标签就是标签，
        # 与它说的事情无关，混进 MISSIONS 会让"按名字找对象"这一条失去意义。
        for cluster in spec.missions.clusters:
            for dot in cluster.dots:
                contract[mission_dot_name(cluster.id, dot.id)] = MISSIONS_COLLECTION
            contract[mission_label_name(cluster.id)] = LABELS_COLLECTION
            contract[label_halo_name(f"mission_{cluster.id}")] = LABELS_COLLECTION
        for row in legend_placements(spec):
            contract[legend_dot_name(row.identifier)] = MISSIONS_COLLECTION
            contract[legend_label_name(row.identifier)] = LABELS_COLLECTION
            contract[label_halo_name(f"legend_{row.identifier}")] = LABELS_COLLECTION
    return contract


def material_contract(spec: SceneSpec) -> Tuple[str, ...]:
    """材质的全集：契约里列了几个就建几个，多一个少一个都算漂移。"""
    names: List[str] = []
    for body in spec.bodies:
        if body.kind == "star":
            names.append(SUN_MATERIAL)
        elif body.kind == "minor":
            names.append(minor_material_name(body.id))
        else:
            names.append(planet_material_name(body.id))
        if body.atmosphere is not None:
            names.append(atmosphere_material_name(body.id))
        if body.clouds is not None:
            names.append(clouds_material_name(body.id))
    names.extend([TRAJECTORY_MATERIAL, BELT_MATERIAL, STARFIELD_MATERIAL, LABEL_MATERIAL, LABEL_HALO_MATERIAL])
    if spec.missions is not None:
        # 每条任务线一份自发光材质：图例的色点与簇里的点位共用它，画面上与图例里必然同色。
        names.extend(mission_material_name(line.id) for line in spec.missions.lines)
    return tuple(names)


def expected_objects(spec: SceneSpec) -> Tuple[str, ...]:
    """契约里的对象名（不含相机；相机的名字另有 :data:`CAMERA_OBJECT`）。"""
    return tuple(object_contract(spec))


def expected_materials(spec: SceneSpec) -> Tuple[str, ...]:
    return material_contract(spec)


# ---------------------------------------------------------------- 数据段


def _body_data(
    body: Body, placement: BodyPlacement, textures: Mapping[str, str]
) -> Dict[str, object]:
    """一个天体进场景需要的全部数——全部来自 layout，发射器不再算一次。"""
    if body.texture is None and body.kind != "star":
        object_name = minor_object_name(body.id)
        material_name = minor_material_name(body.id)
    elif body.kind == "star":
        object_name = sun_object_name()
        material_name = SUN_MATERIAL
    else:
        object_name = planet_object_name(body.id)
        material_name = planet_material_name(body.id)
    data: Dict[str, object] = {
        "id": body.id,
        "name_cn": body.name_cn,
        "kind": body.kind,
        "object_name": object_name,
        "material_name": material_name,
        "material_type": body.material.type,
        "strength": body.material.strength,
        "color": list(body.material.color),
        "roughness": body.material.roughness,
        "normal_strength": body.material.normal_strength,
        # 贴图可以没有（小天体）；有就带上绝对路径，没有就给 None。
        "texture_path": textures[body.texture] if body.texture else None,
        "limb_edge": SUN_LIMB_EDGE,
        "position": list(placement.position),
        "radius_units": placement.display_radius,
        "real_orbit_au": placement.real_orbit_au,
        "display_orbit_distance": placement.display_orbit_distance,
        "display_radius": placement.display_radius,
        "magnification": placement.magnification,
        "real_radius_km": placement.real_radius_km,
        "real_radius_au": body.real.radius_km / AU_KM,
    }
    if body.atmosphere is not None:
        data["atmosphere"] = _atmosphere_data(body.id, body.atmosphere)
    if body.clouds is not None and body.clouds.texture:
        data["clouds"] = {
            "object_name": planet_clouds_name(body.id),
            "material_name": clouds_material_name(body.id),
            "texture_path": textures[body.clouds.texture],
            "scale": body.clouds.scale,
            "opacity": body.clouds.opacity,
        }
    return data


def _atmosphere_data(body_id: str, atmosphere: Atmosphere) -> Mapping[str, object]:
    return {
        "object_name": planet_atmosphere_name(body_id),
        "material_name": atmosphere_material_name(body_id),
        "mode": atmosphere.mode,
        "scale": atmosphere.scale,
        "density": atmosphere.density,
        "color": list(atmosphere.color),
    }


def _band_data(band: Band, placement: BandPlacement) -> Mapping[str, object]:
    return {
        "id": band.id,
        "name_cn": band.name_cn,
        "object_name": band_object_name(band.id),
        "count": band.count,
        "size_range": list(band.size_range),
        "color": list(band.color),
        "center": list(placement.center),
        "half_x": placement.half_x,
        "half_y": placement.half_y,
        "half_z": placement.half_z,
    }


def _trajectory_data(spec: SceneSpec, placements: Sequence[NodePlacement]) -> Mapping[str, object]:
    """航迹：节点（带语义 kind）与按语义画出来的实迹折线。"""
    plan_ = trajectory_polyline(spec)
    nodes: List[Mapping[str, object]] = []
    for node, placement in zip(spec.trajectory.nodes, placements):
        nodes.append(
            {
                "id": placement.node_id,
                "kind": node.kind,
                "label": placement.label,
                "body_id": placement.body_id,
                "position": list(placement.position),
                "path_index": plan_.node_points[placement.node_id],
            }
        )
    assert plan_.assist is not None
    assist = plan_.assist
    # 终点箭头的位置：入轨交接点，以及它前一段折线上的点（用来定来向）。路径现在收在一条
    # 闭合极轨上，"终点"就是交接点——箭头因此扎在**接近段**上，指向捕获的那一刻。
    arrival_tip = plan_.capture.join if plan_.capture is not None else plan_.points[-1]
    arrival_index = max(0, len(plan_.points) - 1 - ARROW_APPROACH_POINTS)
    arrival_from = plan_.points[arrival_index]
    return {
        "object_name": trajectory_object_name(spec.trajectory.destination.id),
        "material_name": TRAJECTORY_MATERIAL,
        "color": list(spec.trajectory.style.color),
        "width": spec.trajectory.style.width,
        "strength": TRAJECTORY_STRENGTH,
        "arrow": {
            "object_name": arrow_object_name(spec.trajectory.destination.id),
            "standoff_factor": ARROW_STANDOFF_FACTOR,
            "length_factor": ARROW_LENGTH_FACTOR,
            "radius_factor": ARROW_RADIUS_FACTOR,
            "segments": ARROW_SEGMENTS,
        },
        "nodes": nodes,
        "path": [list(point) for point in plan_.points],
        "assist": {
            "node_id": assist.node_id,
            "body_id": assist.body_id,
            "periapsis": assist.periapsis,
            "periapsis_radii": assist.periapsis_radii,
            "turn_deg": assist.turn_deg,
            "entry": list(assist.entry),
            "exit": list(assist.exit),
        },
        "arrival": {"tip": list(arrival_tip), "from": list(arrival_from)},
        "capture": (
            {
                "node_id": plan_.capture.node_id,
                "body_id": plan_.capture.body_id,
                "semi_major": plan_.capture.semi_major,
                "eccentricity": plan_.capture.eccentricity,
                "polar": plan_.capture.polar,
                "join": list(plan_.capture.join),
                "plane_azimuth_deg": plan_.capture.plane_azimuth_deg,
            }
            if plan_.capture is not None
            else None
        ),
    }


def mission_data(spec: SceneSpec) -> Optional[Mapping[str, object]]:
    """任务层进场景需要的数据：每个簇（含每个点位的对象名与坐标）与图例。

    点位的**对象名**在这里定，check 直接读它——所以判据不认识"布局模块的命名习惯"，
    只认识报告里写下的名字。
    """
    missions = spec.missions
    if missions is None:
        return None
    clusters = [
        {
            "id": cluster.cluster_id,
            "name_cn": cluster.name_cn,
            "line": cluster.line,
            "site": cluster.site,
            "label": cluster.label,
            "label_object_name": mission_label_name(cluster.cluster_id),
            "label_position": list(cluster.label_position),
            "label_size": cluster.label_size,
            "color": list(cluster.color),
            "material_name": mission_material_name(cluster.line),
            "dots": [
                {
                    "id": dot.dot_id,
                    "object_name": mission_dot_name(dot.cluster_id, dot.dot_id),
                    "position": list(dot.position),
                    "radius": dot.radius,
                }
                for dot in cluster.dots
            ],
        }
        for cluster in mission_placements(spec)
    ]
    legend = [
        {
            "identifier": row.identifier,
            "text": row.text,
            "color": list(row.color),
            "dot_object_name": legend_dot_name(row.identifier),
            "label_object_name": legend_label_name(row.identifier),
            "dot_position": list(row.dot_position),
            "label_position": list(row.label_position),
            "dot_radius": row.dot_radius,
            "label_size": row.label_size,
            # 三条线各用自己的自发光材质；飞行轨迹那一行复用轨迹的材质，图例里的颜色
            # 就是画面上那条线的颜色（所以它不在材质契约里另开一份）。
            "material_name": (
                TRAJECTORY_MATERIAL
                if row.identifier == LEGEND_TRAJECTORY_ID
                else mission_material_name(row.identifier)
            ),
        }
        for row in legend_placements(spec)
    ]
    return {
        "lines": [
            {"id": line.id, "name_cn": line.name_cn, "color": list(line.color)}
            for line in missions.lines
        ],
        "sites": [{"id": site.id, "name_cn": site.name_cn} for site in missions.sites],
        "clusters": clusters,
        "legend": legend,
        "dot_radius": missions.dot_radius,
        "dot_strength": missions.dot_strength,
        "segments": MISSION_DOT_SEGMENTS,
        "rings": MISSION_DOT_RINGS,
    }


def _camera_data(spec: SceneSpec) -> Mapping[str, object]:
    """相机落位由 :func:`primer.scene.layout.camera_placement` 给出，发射器不再算一遍。

    位置与朝向都嵌进数据段，Blender 侧直接装 ``matrix_world``——这样"check 预测的取景"
    与"Blender 真正渲出来的取景"用的是同一组数。
    """
    camera = spec.camera
    placement = camera_placement(camera)
    return {
        "object_name": CAMERA_OBJECT,
        "focal_length_mm": camera.focal_length_mm,
        "aperture_f": camera.aperture_f,
        "elevation_deg": camera.elevation_deg,
        "azimuth_deg": camera.azimuth_deg,
        "distance": camera.distance,
        "target": list(camera.target),
        "location": list(placement.location),
        "right": list(placement.right),
        "up": list(placement.up),
        "forward": list(placement.forward),
    }


@dataclass(frozen=True)
class LabelPlacement:
    """一个文字标签的落位：对象名、要显示的中文、世界坐标、字号。

    ``extent`` 是它在画面上大致占的半径（世界单位），取景核对拿它算"标签会不会压线"——
    文字是**贴着画边缘最容易先出画**的东西，只算天体圆心会漏掉它。
    """

    identifier: str
    object_name: str
    text: str
    position: Tuple[float, float, float]
    size: float

    @property
    def halo_name(self) -> str:
        return label_halo_name(self.identifier)

    @property
    def extent(self) -> float:
        return label_extent(self.text, self.size)


def label_placements(spec: SceneSpec) -> Tuple[LabelPlacement, ...]:
    """全部文字标签的落位（天体标签 + 航迹节点标签），次序即建对象的次序。

    正文标签落在天体上方（``BODY_LABEL_CLEARANCE`` 加本体半径），节点标签另给一套偏移；
    钉在天体上的节点标签落到球的下方，正好避开同一天体的正文标签。
    """
    resolved = plan(spec)
    placements = resolved["bodies_by_id"]
    nodes = resolved["nodes"]
    assert isinstance(placements, Mapping) and isinstance(nodes, tuple)

    items: List[LabelPlacement] = []
    for body in spec.bodies:
        placement = placements[body.id]
        items.append(
            LabelPlacement(
                identifier=body.id,
                object_name=label_object_name(body.id),
                text=body.name_cn,
                position=(
                    placement.position[0],
                    placement.position[1],
                    placement.position[2] + BODY_LABEL_CLEARANCE + placement.display_radius,
                ),
                size=BODY_LABEL_SIZE,
            )
        )
    for node in nodes:
        if not node.label:
            continue
        radius = placements[node.body_id].display_radius if node.body_id else 0.0
        height = -(NODE_LABEL_CLEARANCE + radius) if node.body_id else NODE_LABEL_CLEARANCE
        items.append(
            LabelPlacement(
                identifier=node.node_id,
                object_name=label_object_name(node.node_id),
                text=node.label,
                position=(
                    node.position[0],
                    node.position[1],
                    node.position[2] + height,
                ),
                size=NODE_LABEL_SIZE,
            )
        )
    # 任务层：每个簇**一条**组标签，图例每行**一条**文字。两者都进取景核对——角落里的
    # 图例与簇标签同样不许压到画边缘。
    for cluster in mission_placements(spec):
        identifier = f"mission_{cluster.cluster_id}"
        items.append(
            LabelPlacement(
                identifier=identifier,
                object_name=label_object_name(identifier),
                text=cluster.label,
                position=cluster.label_position,
                size=cluster.label_size,
            )
        )
    for row in legend_placements(spec):
        identifier = f"legend_{row.identifier}"
        items.append(
            LabelPlacement(
                identifier=identifier,
                object_name=label_object_name(identifier),
                text=row.text,
                position=row.label_position,
                size=row.label_size,
            )
        )
    return tuple(items)


def background_axis(spec: SceneSpec) -> Tuple[float, float, float]:
    """天球旋转轴：相机右手方向（水平、垂直于视线）。

    绕它转，银河带就在画面里上下移动——正是"把带子摆到画面哪个高度"这一个自由度。
    """
    return camera_placement(spec.camera).right


def background_angle_deg(spec: SceneSpec) -> float:
    """天球旋转角：让银河带落在画面中心偏上 ``band_offset_deg`` 处。

    星图的银河带在等距柱状投影的赤道上，映射到天球就是黄道面；相机从黄道面之上
    ``elevation_deg`` 俯视，视线落点在方位角相反的一侧、地平线以下同样的度数。所以把天球
    绕右手轴转 ``-(elevation_deg + band_offset_deg)``，带子正好过画面中心（偏移为正时偏上）。

    **符号是要紧的**：转成正值等于把天球的**极点**转进画面，等距柱状投影在极点附近把
    星点拉成放射状的条纹——第一版就是这么错的，整幅图看着像一片雪暴。相机一动这个角度
    跟着动，不需要人去追。
    """
    return -(spec.camera.elevation_deg + spec.background.band_offset_deg)


def scene_seed(spec: SceneSpec, fingerprint: Fingerprint) -> int:
    """撒石块的种子：由规格内容摘要推出，"同一份规格 → 同一个星带"。

    不用 ``hash()``：Python 对字符串的哈希每个进程都不同（PYTHONHASHSEED），拿它当种子
    等于每次重建都是另一片星带。
    """
    source = fingerprint.spec_sha256 or "0" * 12
    return int(source, 16) % (2**31 - 1)


def scene_data(
    spec: SceneSpec,
    project_root: Path,
    fingerprint: Fingerprint,
    final: bool = False,
) -> Mapping[str, object]:
    """把规格、布局与指纹压成一份纯数据（JSON 可序列化、键排序后逐字节确定）。"""
    project_root = Path(project_root)
    resolved = plan(spec)
    scene_root = output_dir_for(project_root, "scene")
    textures = {
        entry.texture: str(scene_root / "assets" / "textures" / entry.texture)
        for entry in spec.assets.files
    }
    mode = spec.render.mode(final)
    build_dir = scene_root / "build"
    out_dir = scene_root / (spec.render.output.final_dir if final else spec.render.output.preview_dir)
    stem = f"{spec.meta.name}_{'final' if final else 'preview'}_{mode.width}x{mode.height}"
    font_dir = project_root / "_primer" / "exchange" / "daimon_runtime" / "fonts"

    placements = resolved["bodies_by_id"]
    assert isinstance(placements, Mapping)
    bodies = [
        _body_data(body, placements[body.id], textures)  # type: ignore[index]
        for body in spec.bodies
    ]
    bands = resolved["bands"]
    nodes = resolved["nodes"]
    assert isinstance(bands, tuple) and isinstance(nodes, tuple)

    return {
        "meta": {"name": spec.meta.name, "title": spec.meta.title, "caption": spec.meta.caption},
        "spec_path": str(spec.path),
        "project_root": str(project_root),
        "blend_path": str(scene_root / f"{spec.meta.name}.blend"),
        "report_path": str(build_dir / REPORT_NAME),
        "script_path": str(build_dir / SCRIPT_NAME),
        "fingerprint": fingerprint.as_mapping(),
        "seed": scene_seed(spec, fingerprint),
        "mode": "final" if final else "preview",
        "render": {
            "width": mode.width,
            "height": mode.height,
            "samples": mode.samples,
            "denoise": mode.denoise,
            "exr": mode.exr,
            "view_transform": spec.render.color_management.view_transform,
            "look": spec.render.color_management.look,
            "display_device": spec.render.color_management.display_device,
            "output_dir": str(out_dir),
            "output_stem": stem,
        },
        "collections": list(COLLECTIONS),
        "bodies": bodies,
        "bands": [_band_data(band, placement) for band, placement in zip(spec.bands, bands)],
        "missions": mission_data(spec),
        "trajectory": _trajectory_data(spec, nodes),
        "camera": _camera_data(spec),
        "background": {
            "object_name": STARFIELD_OBJECT,
            "material_name": STARFIELD_MATERIAL,
            "texture_path": textures[spec.background.starfield],
            "radius": STARFIELD_RADIUS,
            "texture_power": spec.background.texture_power,
            "strength": spec.background.strength,
            "field_color": list(spec.background.field_color),
            "rotation_axis": list(background_axis(spec)),
            "rotation_angle_deg": background_angle_deg(spec),
            "world_strength": WORLD_STRENGTH,
            "segments": STARFIELD_SEGMENTS,
            "rings": STARFIELD_RINGS,
        },
        "lights": {
            "key_object": sun_light_name(),
            "key_energy": KEY_LIGHT_ENERGY,
            "key_angle_deg": KEY_LIGHT_ANGLE_DEG,
            "fill_object": fill_light_name(),
            "fill_energy": FILL_LIGHT_ENERGY,
            "fill_angle_deg": FILL_LIGHT_ANGLE_DEG,
            "fill_elevation_deg": FILL_LIGHT_ELEVATION_DEG,
            "fill_azimuth_deg": FILL_LIGHT_AZIMUTH_DEG,
        },
        "labels": {
            "extrude": LABEL_EXTRUDE,
            "material_name": LABEL_MATERIAL,
            "font_regular": str(font_dir / "NotoSansSC-Regular.ttf"),
            "font_bold": str(font_dir / "NotoSansSC-Bold.ttf"),
            # 每个标签落在哪儿由 :func:`label_placements` 一处算好（尺寸也在里面），
            # Blender 侧只负责建对象；取景核对用的是同一份落位，所以"报告说在画内、
            # 图上却出画"不会有第二种解释。
            "strength": LABEL_STRENGTH,
            "color": list(LABEL_COLOR),
            "halo_material_name": LABEL_HALO_MATERIAL,
            "halo_color": list(LABEL_HALO_COLOR),
            "halo_offset_ratio": LABEL_HALO_OFFSET_RATIO,
            "halo_depth": LABEL_HALO_DEPTH,
            "view_axis": list(camera_placement(spec.camera).forward),
            "items": [
                {"object_name": item.object_name, "halo_name": item.halo_name,
                 "text": item.text, "position": list(item.position), "size": item.size}
                for item in label_placements(spec)
            ],
        },
        "grid": {
            "planet_segments": PLANET_SEGMENTS,
            "planet_rings": PLANET_RINGS,
            "minor_subdivisions": MINOR_SUBDIVISIONS,
            "minor_relief": MINOR_RELIEF,
            "rock_bump_strength": ROCK_BUMP_STRENGTH,
            "belt_subdivisions": BELT_SUBDIVISIONS,
            "belt_thickness_ratio": BELT_THICKNESS_RATIO,
            "belt_radial_sigma": BELT_RADIAL_SIGMA,
            "curve_resolution_u": CURVE_RESOLUTION_U,
            "curve_bevel_resolution": CURVE_BEVEL_RESOLUTION,
        },
        "atmosphere_rim": {
            "opacity": ATMOSPHERE_RIM_OPACITY,
            "strength": ATMOSPHERE_RIM_STRENGTH,
            "power": ATMOSPHERE_RIM_POWER,
        },
        "glare": {
            "threshold": GLARE_THRESHOLD,
            "size": GLARE_SIZE,
            "strength": GLARE_STRENGTH,
            "smoothness": GLARE_SMOOTHNESS,
        },
        "trajectory_strength": TRAJECTORY_STRENGTH,
    }


# ---------------------------------------------------------------- 脚本

_HEADER = '''# -*- coding: utf-8 -*-
"""由 ``python -m primer.scene build`` 生成——**不要手改**；改规格，再重新生成。

这份脚本在 Blender 里跑（``blender -b -P scene.py``）：按数据段建场景、设渲染参数、
存 ``.blend``、出图，最后把一份机器可读的现场报告写到 ``build/scene_report.json``。
``primer.scene check`` 读的就是那份报告——核对命名契约、天体自定义属性、轨迹端点是
否真的钉在天体圆心、以及 PNG 的尺寸与亮度。

数据段是 JSON（键已排序、浮点走规范表示、没有时间戳），构建代码是下面这段静态文本。
同一份规格、同一批贴图，两次生成的这个文件逐字节相同。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
import sys

import bmesh
import bpy
from mathutils import Matrix, Vector

'''

_BUILDER = r'''
SUN = "SUN"
PLANETS = "PLANETS"
BANDS = "BANDS"
TRAJECTORIES = "TRAJECTORIES"
MISSIONS = "MISSIONS"
LABELS = "LABELS"
BACKGROUND = "BACKGROUND"
M_BELT = "MAT_belt"
def log(message):
    """诊断一律英文、走 stderr——与 primer 其余功能同一口径。"""
    print("scene: " + message, file=sys.stderr)


def clear_scene():
    """从零开始：不留默认立方体，也不留上一次跑剩的材质与贴图。"""
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    for collection in list(bpy.data.collections):
        bpy.data.collections.remove(collection)
    for block in (
        bpy.data.meshes,
        bpy.data.curves,
        bpy.data.lights,
        bpy.data.cameras,
        bpy.data.materials,
        bpy.data.fonts,
        bpy.data.worlds,
    ):
        for item in list(block):
            try:
                block.remove(item)
            except Exception:
                pass


def link(obj, collection):
    collection.objects.link(obj)
    return obj


def smooth(mesh):
    mesh.polygons.foreach_set("use_smooth", [True] * len(mesh.polygons))
    mesh.update()


def uv_sphere_mesh(name, radius, segments, rings):
    mesh = bpy.data.meshes.new(name)
    build = bmesh.new()
    build.loops.layers.uv.new("UVMap")
    bmesh.ops.create_uvsphere(
        build, u_segments=segments, v_segments=rings, radius=radius, calc_uvs=True
    )
    build.to_mesh(mesh)
    build.free()
    smooth(mesh)
    return mesh


def rock_mesh(name, radius, body_id):
    """小天体：把二十面体的顶点按种子推挤一下，做成不规则的岩块。

    行星用 UV 球（要贴等距柱状投影），小天体没有贴图、也不该是一颗光滑的球——半人马
    天体是不规则形，所以这里用二十面体细分后按顶点索引做确定性位移：同一个 id 每次构出
    同一块石头，形状又不重复。
    """
    mesh = bpy.data.meshes.new(name)
    build = bmesh.new()
    bmesh.ops.create_icosphere(build, subdivisions=SPEC["grid"]["minor_subdivisions"], radius=1.0)
    seed = stable_seed("minor:" + body_id)
    for vertex in build.verts:
        wobble = 1.0 + SPEC["grid"]["minor_relief"] * (
            math.sin(vertex.index * 12.9898 + seed % 4096) * 0.5
            + math.sin(vertex.index * 4.1414 + (seed % 977) * 0.5) * 0.5
        )
        vertex.co = vertex.co.normalized() * radius * wobble
    build.to_mesh(mesh)
    build.free()
    smooth(mesh)
    return mesh


def rock_material(body):
    """岩质材质：只给粗糙度、底色与一点凹凸噪声——程序化的，因为公开贴图里没有这一号。"""
    material, tree = new_material(body["material_name"])
    output = output_node(tree)
    shading = tree.nodes.new("ShaderNodeBsdfPrincipled")
    shading.name = "BSDF_Rock"
    set_color(shading.inputs["Base Color"], body["color"])
    shading.inputs["Roughness"].default_value = body["roughness"]
    noise = tree.nodes.new("ShaderNodeTexNoise")
    noise.name = "Tex_RockGrain"
    noise.inputs["Scale"].default_value = 24.0
    noise.inputs["Detail"].default_value = 6.0
    bump = tree.nodes.new("ShaderNodeBump")
    bump.name = "Bump_RockGrain"
    bump.inputs["Strength"].default_value = SPEC["grid"]["rock_bump_strength"]
    tree.links.new(noise.outputs["Fac"], bump.inputs["Height"])
    tree.links.new(bump.outputs["Normal"], shading.inputs["Normal"])
    tree.links.new(shading.outputs["BSDF"], output.inputs["Surface"])
    return material


def new_material(name):
    material = bpy.data.materials.new(name)
    tree = material.node_tree
    for node in list(tree.nodes):
        tree.nodes.remove(node)
    return material, tree


def load_image(path, colorspace="sRGB"):
    image = bpy.data.images.load(path, check_existing=True)
    image.colorspace_settings.name = colorspace
    return image


def image_node(tree, path, name):
    node = tree.nodes.new("ShaderNodeTexImage")
    node.name = name
    node.label = name
    node.image = load_image(path)
    return node


def output_node(tree, name="Output_Surface"):
    node = tree.nodes.new("ShaderNodeOutputMaterial")
    node.name = name
    node.label = name
    return node


def set_color(socket, color):
    socket.default_value = (color[0], color[1], color[2], 1.0)


def planet_material(body):
    """Principled BSDF + 贴图接 Base Color；法线强度走 Bump 节点，不用外部法线图。"""
    material, tree = new_material(body["material_name"])
    output = output_node(tree)
    shading = tree.nodes.new("ShaderNodeBsdfPrincipled")
    shading.name = "BSDF_Planet"
    shading.inputs["Roughness"].default_value = body["roughness"]
    shading.inputs["Metallic"].default_value = 0.0
    texture = image_node(tree, body["texture_path"], "Tex_BaseColor")
    tree.links.new(texture.outputs["Color"], shading.inputs["Base Color"])
    bump = tree.nodes.new("ShaderNodeBump")
    bump.name = "Bump_Surface"
    bump.inputs["Strength"].default_value = body["normal_strength"]
    bump.inputs["Distance"].default_value = 0.02
    tree.links.new(texture.outputs["Color"], bump.inputs["Height"])
    tree.links.new(bump.outputs["Normal"], shading.inputs["Normal"])
    tree.links.new(shading.outputs["BSDF"], output.inputs["Surface"])
    return material


def sun_material(body):
    """太阳：贴图 × 规格色调 × 临边昏暗，强度仍由规格决定。

    三件事各自一处：``Tex_Photosphere`` 出表面纹理，``Mix_Tint`` 乘上规格给的暖色，
    ``Ramp_LimbDarkening`` 按 Layer Weight 的 Facing 把边缘压暗（真实太阳临边比中心暗
    约四成，参考图里的日面也是中间亮、边缘收）。压暗之后再进 Emission，所以日面是一块
    有明暗的球，而不是一张白纸片；外圈的光晕交给合成器的 Fog Glow。
    """
    material, tree = new_material(body["material_name"])
    output = output_node(tree)
    texture = image_node(tree, body["texture_path"], "Tex_Photosphere")
    tint = tree.nodes.new("ShaderNodeMix")
    tint.name = "Mix_Tint"
    tint.label = "Mix_Tint"
    tint.data_type = "RGBA"
    tint.blend_type = "MULTIPLY"
    tint.inputs["Factor"].default_value = 1.0
    set_color(tint.inputs["A"], body["color"])
    tree.links.new(texture.outputs["Color"], tint.inputs["B"])
    facing = tree.nodes.new("ShaderNodeLayerWeight")
    facing.name = "LayerWeight_Limb"
    facing.label = "LayerWeight_Limb"
    facing.inputs["Blend"].default_value = 0.5
    limb = tree.nodes.new("ShaderNodeValToRGB")
    limb.name = "Ramp_LimbDarkening"
    limb.label = "Ramp_LimbDarkening"
    ramp = limb.color_ramp
    ramp.elements[0].position = 0.0
    ramp.elements[0].color = (body["limb_edge"], body["limb_edge"], body["limb_edge"], 1.0)
    ramp.elements[1].position = 1.0
    ramp.elements[1].color = (1.0, 1.0, 1.0, 1.0)
    tree.links.new(facing.outputs["Facing"], limb.inputs["Fac"])
    shade = tree.nodes.new("ShaderNodeMix")
    shade.name = "Mix_LimbDarkening"
    shade.label = "Mix_LimbDarkening"
    shade.data_type = "RGBA"
    shade.blend_type = "MULTIPLY"
    shade.inputs["Factor"].default_value = 1.0
    tree.links.new(tint.outputs["Result"], shade.inputs["A"])
    tree.links.new(limb.outputs["Color"], shade.inputs["B"])
    emission = tree.nodes.new("ShaderNodeEmission")
    emission.name = "Emission_Sun"
    emission.inputs["Strength"].default_value = body["strength"]
    tree.links.new(shade.outputs["Result"], emission.inputs["Color"])
    tree.links.new(emission.outputs["Emission"], output.inputs["Surface"])
    return material


def volume_node(tree):
    """体积节点：Blender 5 叫 ``ShaderNodeVolumePrincipled``，4.x 叫 ``ShaderNodePrincipledVolume``。

    两个名字都试一遍，取到哪个用哪个——为此让整个 ``volume`` 模式在升级 Blender 后失效，
    才是真的不划算。
    """
    for identifier in ("ShaderNodeVolumePrincipled", "ShaderNodePrincipledVolume"):
        try:
            return tree.nodes.new(identifier)
        except RuntimeError:
            continue
    raise RuntimeError("no Principled Volume node type is available in this Blender")


def atmosphere_material(atmosphere):
    """两种模式：``rim`` 是透明壳 + Fresnel 边缘辉光，``volume`` 是体积散射。"""
    material, tree = new_material(atmosphere["material_name"])
    output = output_node(tree)
    faint = tree.nodes.new("ShaderNodeBsdfTransparent")
    faint.name = "BSDF_Transparent"
    if atmosphere["mode"] == "volume":
        volume = volume_node(tree)
        volume.name = "Volume_Atmosphere"
        set_color(volume.inputs["Color"], atmosphere["color"])
        volume.inputs["Density"].default_value = atmosphere["density"]
        tree.links.new(volume.outputs["Volume"], output.inputs["Volume"])
        tree.links.new(faint.outputs["BSDF"], output.inputs["Surface"])
    else:
        emission = tree.nodes.new("ShaderNodeEmission")
        emission.name = "Emission_Rim"
        set_color(emission.inputs["Color"], atmosphere["color"])
        emission.inputs["Strength"].default_value = SPEC["atmosphere_rim"]["strength"]
        shell = tree.nodes.new("ShaderNodeMixShader")
        shell.name = "Mix_Rim"
        # 壳层是一个球，要的是一条**贴边的环**。这里不用 Layer Weight，而是自己算
        # 掠射程度：``1 - dot(Normal, Incoming)``（盘心 0、轮廓 1，因为 Geometry 的
        # ``Incoming`` 是"从表面指向相机"的视线），再取幂。理由是把
        # 候选曲线在同一颗球上量过径向剖面（0=盘心，1=轮廓）：
        #
        #   Fresnel 节点 IOR=1.45   : 0.204 → 0.255   近乎常数，等于没有环
        #   LayerWeight Fresnel b=1 : 1.000 → 1.000   饱和
        #   LayerWeight Fresnel b=.5: 0.369 → 0.400   仍是常数
        #   LayerWeight Facing  b=.5: 0.035 → 0.647   在变，但半径一半处已到 0.32
        #
        # **最初用的就是第一条**（IOR 1.45），所以大气辉光一直是"配了却看不见"；而
        # Facing 那条太宽，半径一半就开始发白，行星表面又被糊掉。自己算点积则完全可控：
        # 亮度 ∝ (1-cosθ)^power，power 6 时半径八成处只有 0.4%、九成处 3%、边缘 55%——
        # 一眼是一条环。
        geometry = tree.nodes.new("ShaderNodeNewGeometry")
        geometry.name = "Geometry_Rim"
        facing = tree.nodes.new("ShaderNodeVectorMath")
        facing.name = "Vector_DotRim"
        facing.operation = "DOT_PRODUCT"
        tree.links.new(geometry.outputs["Normal"], facing.inputs[0])
        tree.links.new(geometry.outputs["Incoming"], facing.inputs[1])
        edge = tree.nodes.new("ShaderNodeMath")
        edge.name = "Math_RimEdge"
        edge.operation = "SUBTRACT"
        edge.inputs[0].default_value = 1.0
        tree.links.new(facing.outputs["Value"], edge.inputs[1])
        power = tree.nodes.new("ShaderNodeMath")
        power.name = "Math_RimFalloff"
        power.operation = "POWER"
        power.inputs[1].default_value = SPEC["atmosphere_rim"]["power"]
        tree.links.new(edge.outputs["Value"], power.inputs[0])
        opacity = tree.nodes.new("ShaderNodeMath")
        opacity.name = "Math_RimOpacity"
        opacity.operation = "MULTIPLY"
        opacity.inputs[1].default_value = SPEC["atmosphere_rim"]["opacity"]
        tree.links.new(power.outputs["Value"], opacity.inputs[0])
        tree.links.new(opacity.outputs["Value"], shell.inputs["Fac"])
        tree.links.new(faint.outputs["BSDF"], shell.inputs[1])
        tree.links.new(emission.outputs["Emission"], shell.inputs[2])
        tree.links.new(shell.outputs["Shader"], output.inputs["Surface"])
    return material


def clouds_material(clouds):
    """云的透空取贴图本身的明暗：上游给的是黑底白云的 JPEG，没有 alpha 通道。"""
    material, tree = new_material(clouds["material_name"])
    output = output_node(tree)
    faint = tree.nodes.new("ShaderNodeBsdfTransparent")
    faint.name = "BSDF_Transparent"
    shell = tree.nodes.new("ShaderNodeMixShader")
    shell.name = "Mix_Clouds"
    shading = tree.nodes.new("ShaderNodeBsdfPrincipled")
    shading.name = "BSDF_Clouds"
    shading.inputs["Roughness"].default_value = 1.0
    shading.inputs["Base Color"].default_value = (0.92, 0.94, 0.98, 1.0)
    texture = image_node(tree, clouds["texture_path"], "Tex_CloudAlpha")
    opacity = tree.nodes.new("ShaderNodeMath")
    opacity.name = "Math_CloudOpacity"
    opacity.operation = "MULTIPLY"
    opacity.inputs[1].default_value = clouds["opacity"]
    tree.links.new(texture.outputs["Color"], opacity.inputs[0])
    tree.links.new(opacity.outputs["Value"], shell.inputs["Fac"])
    tree.links.new(faint.outputs["BSDF"], shell.inputs[1])
    tree.links.new(shading.outputs["BSDF"], shell.inputs[2])
    tree.links.new(shell.outputs["Shader"], output.inputs["Surface"])
    return material


def flat_material(name, color, roughness):
    material, tree = new_material(name)
    output = output_node(tree)
    shading = tree.nodes.new("ShaderNodeBsdfPrincipled")
    shading.name = "BSDF_Flat"
    set_color(shading.inputs["Base Color"], color)
    shading.inputs["Roughness"].default_value = roughness
    tree.links.new(shading.outputs["BSDF"], output.inputs["Surface"])
    return material


def emission_material(name, color, strength):
    material, tree = new_material(name)
    output = output_node(tree)
    emission = tree.nodes.new("ShaderNodeEmission")
    emission.name = "Emission_Flat"
    set_color(emission.inputs["Color"], color)
    emission.inputs["Strength"].default_value = strength
    tree.links.new(emission.outputs["Emission"], output.inputs["Surface"])
    return material


def starfield_material(background):
    """天球：贴图 × 增益 + 深空底色。

    只放大贴图是没用的：这张星图是黑底稀星（线性均值 0.0046），放大器把几颗亮星推到
    饱和，银河带却仍停在 0.02 上下，整幅图看上去还是黑的。参考图的深空是**深蓝**不是
    纯黑，所以这里在贴图之上再加一层底色——它同时把"背景有多少亮度"这件事与贴图的
    动态范围解耦：带子的对比靠增益，场底的亮度靠底色。
    """
    material, tree = new_material(background["material_name"])
    output = output_node(tree)
    texture = image_node(tree, background["texture_path"], "Tex_Starfield")
    # 幂次曲线先整形：out = in ** power。星图的暗部（银河带）与亮部（星）相差两个数量级，
    # 不先压缩这个范围，增益就只能二选一——要么带子淹没在纯黑里，要么星星爆成一片白斑。
    lift = tree.nodes.new("ShaderNodeGamma")
    lift.name = "Gamma_BandLift"
    lift.label = "Gamma_BandLift"
    lift.inputs["Gamma"].default_value = background["texture_power"]
    tree.links.new(texture.outputs["Color"], lift.inputs["Color"])
    gain = tree.nodes.new("ShaderNodeMix")
    gain.name = "Mix_StarfieldGain"
    gain.label = "Mix_StarfieldGain"
    gain.data_type = "RGBA"
    gain.blend_type = "MULTIPLY"
    gain.inputs["Factor"].default_value = 1.0
    set_color(gain.inputs["A"], (background["strength"],) * 3)
    tree.links.new(lift.outputs["Color"], gain.inputs["B"])
    field = tree.nodes.new("ShaderNodeMix")
    field.name = "Mix_DeepField"
    field.label = "Mix_DeepField"
    field.data_type = "RGBA"
    field.blend_type = "ADD"
    field.inputs["Factor"].default_value = 1.0
    tree.links.new(gain.outputs["Result"], field.inputs["A"])
    set_color(field.inputs["B"], background["field_color"])
    emission = tree.nodes.new("ShaderNodeEmission")
    emission.name = "Emission_Starfield"
    emission.inputs["Strength"].default_value = 1.0
    tree.links.new(field.outputs["Result"], emission.inputs["Color"])
    tree.links.new(emission.outputs["Emission"], output.inputs["Surface"])
    return material


def build_collections():
    collections = {}
    for name in SPEC["collections"]:
        collection = bpy.data.collections.new(name)
        bpy.context.scene.collection.children.link(collection)
        collections[name] = collection
    return collections


def build_minor(body, collections, materials):
    """小天体：不规则岩块 + 程序化岩质材质（没有贴图资产，见 rock_material 的说明）。"""
    mesh = rock_mesh(body["object_name"], body["radius_units"], body["id"])
    obj = link(bpy.data.objects.new(body["object_name"], mesh), collections[PLANETS])
    obj.location = body["position"]
    materials[body["material_name"]] = rock_material(body)
    obj.data.materials.append(materials[body["material_name"]])
    for key in ("real_orbit_au", "display_orbit_distance", "display_radius", "magnification", "real_radius_km"):
        obj[key] = body[key]
    obj["name_cn"] = body["name_cn"]
    return obj


def build_planet(body, collections, materials):
    mesh = uv_sphere_mesh(
        body["object_name"],
        body["radius_units"],
        SPEC["grid"]["planet_segments"],
        SPEC["grid"]["planet_rings"],
    )
    obj = link(bpy.data.objects.new(body["object_name"], mesh), collections[PLANETS])
    obj.location = body["position"]
    materials[body["material_name"]] = planet_material(body)
    obj.data.materials.append(materials[body["material_name"]])
    # 论证图要的是"放大了多少倍"一眼可见：四个量钉在对象上，不在报告里另算。
    obj["real_orbit_au"] = body["real_orbit_au"]
    obj["display_orbit_distance"] = body["display_orbit_distance"]
    obj["display_radius"] = body["display_radius"]
    obj["magnification"] = body["magnification"]
    obj["real_radius_km"] = body["real_radius_km"]
    obj["name_cn"] = body["name_cn"]
    return obj


def build_shell(body, key, collections, materials, builder):
    """大气壳与云层的共同部分：略大一圈的球，圆心与本体相同。"""
    shell = body[key]
    radius = body["radius_units"] * shell["scale"]
    mesh = uv_sphere_mesh(
        shell["object_name"], radius, SPEC["grid"]["planet_segments"], SPEC["grid"]["planet_rings"]
    )
    obj = link(bpy.data.objects.new(shell["object_name"], mesh), collections[PLANETS])
    obj.location = body["position"]
    materials[shell["material_name"]] = builder(shell)
    obj.data.materials.append(materials[shell["material_name"]])
    obj["host_body"] = body["id"]
    # **壳层不投影**。发光的壳是一个"部分不透明的发射体"，Cycles 会按 Mix 因子的比例挡住
    # 阴影射线：行星外圈那一带的因子最大，于是行星自己的边缘被自己的大气壳遮暗——渲染出来
    # 是一圈**暗环**，正好和要的"边缘辉光"相反。关掉投影就只剩发光，环也就亮了。
    obj.visible_shadow = False
    return obj


def build_sun(body, collections, materials):
    mesh = uv_sphere_mesh(
        body["object_name"],
        body["radius_units"],
        SPEC["grid"]["planet_segments"],
        SPEC["grid"]["planet_rings"],
    )
    obj = link(bpy.data.objects.new(body["object_name"], mesh), collections[SUN])
    obj.location = body["position"]
    materials[body["material_name"]] = sun_material(body)
    obj.data.materials.append(materials[body["material_name"]])
    obj["real_orbit_au"] = body["real_orbit_au"]
    obj["display_orbit_distance"] = body["display_orbit_distance"]
    obj["display_radius"] = body["display_radius"]
    obj["magnification"] = body["magnification"]
    obj["real_radius_km"] = body["real_radius_km"]
    obj["name_cn"] = body["name_cn"]
    return obj


def aim_at(obj, source, target):
    """把灯从 ``source`` 指向 ``target``（灯沿本地 -Z 照出去）。"""
    obj.location = source
    obj.rotation_euler = (target - source).to_track_quat("-Z", "Y").to_euler()
    return obj


def build_lights(sun_body, collections):
    """主光与补光：两盏**平行光**，主光从太阳的显示位置射向序列。

    这张图里天体沿 x 一字排开，离太阳的显示位置 20–110 世界单位。点光按平方反比衰减，
    最远端会比近端暗上百倍，外圈直接看不见；更要紧的是"所有晨昏线朝同一边"这件事——
    点光从一点发散，每颗球的受光方向各不相同。平行光两者都解决：照度不随距离变，
    受光方向一致（都从太阳那边来），晨昏线因此齐整。太阳仍是光源：主光就挂在它的显示
    位置上、朝序列照过去；补光很弱，只把暗面从纯黑里提一点起来。
    """
    lights = SPEC["lights"]
    sun_at = Vector(sun_body["position"])
    # 主光指向序列的几何中心：天体沿 x 排开，取 x 的中位数附近即可——用半个 x 跨度当指向，
    # 免得把"照向哪里"也写成需要维护的参数。
    span = [body["position"][0] for body in SPEC["bodies"]]
    target = Vector((sum(span) / len(span), 0.0, 0.0))

    key_data = bpy.data.lights.new(lights["key_object"], type="SUN")
    key_data.energy = lights["key_energy"]
    key_data.color = tuple(sun_body["color"])
    key_data.angle = math.radians(lights["key_angle_deg"])
    key = link(bpy.data.objects.new(lights["key_object"], key_data), collections[SUN])
    aim_at(key, sun_at, target)

    fill_data = bpy.data.lights.new(lights["fill_object"], type="SUN")
    fill_data.energy = lights["fill_energy"]
    fill_data.angle = math.radians(lights["fill_angle_deg"])
    fill = link(bpy.data.objects.new(lights["fill_object"], fill_data), collections[SUN])
    # 补光是方向光，位置不影响结果；仍放在太阳那里，让大纲里"光都从太阳来"一眼可见。
    fill.location = sun_at
    fill.rotation_euler = (
        math.radians(90.0 - lights["fill_elevation_deg"]),
        0.0,
        math.radians(lights["fill_azimuth_deg"]),
    )
    return key, fill


def stable_seed(text):
    """与进程无关的稳定种子：``hash()`` 每个进程都不同，不能用来决定星带。"""
    return int(hashlib.sha256(text.encode("utf-8")).hexdigest()[:8], 16)


def build_missions(materials, collections):
    """任务层：每个点位一颗自发光小球，每个簇（与图例每行）一条标签。

    点位是**刻意抽象**的：一颗球 = 一台设备，颜色 = 它属于哪条任务线。不建任何外形，
    也不连光束线——源文档 A.3 提过"用光束线表现基线编队"，但那是接口关系图的内容，
    blender.md 明确禁止在本场景里画。
    """
    missions = SPEC.get("missions")
    if not missions:
        return []
    created = []
    for line in missions["lines"]:
        name = "MAT_mission_%s" % line["id"]
        materials.setdefault(name, emission_material(name, line["color"], missions["dot_strength"]))
    for cluster in missions["clusters"]:
        for dot in cluster["dots"]:
            mesh = uv_sphere_mesh(
                dot["object_name"],
                dot["radius"],
                missions["segments"],
                missions["rings"],
            )
            obj = link(
                bpy.data.objects.new(dot["object_name"], mesh), collections[MISSIONS]
            )
            obj.location = dot["position"]
            obj.data.materials.append(materials[cluster["material_name"]])
            obj["cluster"] = cluster["id"]
            obj["line"] = cluster["line"]
            created.append(obj)
    for row in missions["legend"]:
        mesh = uv_sphere_mesh(
            row["dot_object_name"], row["dot_radius"], missions["segments"], missions["rings"]
        )
        obj = link(
            bpy.data.objects.new(row["dot_object_name"], mesh), collections[MISSIONS]
        )
        obj.location = row["dot_position"]
        obj.data.materials.append(materials[row["material_name"]])
        obj["legend_row"] = row["identifier"]
        created.append(obj)
    return created


def build_band(band, collections, materials, seed):
    """一个碎屑带：在规格给的长方体里按种子撒石块，最后并成一个对象。

    沿 x 的分布是"中间密、两头疏"的高斯（截在 ``x_half_length`` 内），y 与 z 均匀——一块
    扁平的碎石盘，而不是一圈环：这张图里天体已经一字排开，主带就是横在中间的一段。
    """
    generator = random.Random(seed + stable_seed(band["id"]))
    mesh = bpy.data.meshes.new(band["object_name"])
    build = bmesh.new()
    center = band["center"]
    half_x, half_y, half_z = band["half_x"], band["half_y"], band["half_z"]
    sigma = half_x * SPEC["grid"]["belt_radial_sigma"]
    low, high = band["size_range"]
    for _ in range(band["count"]):
        offset_x = min(max(generator.gauss(0.0, sigma), -half_x), half_x)
        offset_y = generator.uniform(-half_y, half_y)
        offset_z = generator.uniform(-half_z, half_z)
        location = Vector(
            (center[0] + offset_x, center[1] + offset_y, center[2] + offset_z)
        )
        bmesh.ops.create_icosphere(
            build,
            subdivisions=SPEC["grid"]["belt_subdivisions"],
            radius=generator.uniform(low, high),
            matrix=Matrix.Translation(location),
            calc_uvs=False,
        )
    build.to_mesh(mesh)
    build.free()
    obj = link(bpy.data.objects.new(band["object_name"], mesh), collections[BANDS])
    if M_BELT not in materials:
        materials[M_BELT] = flat_material(M_BELT, band["color"], 0.9)
    obj.data.materials.append(materials[M_BELT])
    obj["name_cn"] = band["name_cn"]
    obj["count"] = band["count"]
    obj["center"] = list(center)
    return obj


def build_trajectory(materials, collections):
    """整条实迹折线：几何在 layout.trajectory_polyline 里算好，这里只负责画出来。"""
    trajectory = SPEC["trajectory"]
    points = trajectory["path"]
    node_points = [node["path_index"] for node in trajectory["nodes"]]

    curve = bpy.data.curves.new(trajectory["object_name"], type="CURVE")
    curve.dimensions = "3D"
    curve.resolution_u = SPEC["grid"]["curve_resolution_u"]
    curve.bevel_depth = trajectory["width"]
    curve.bevel_resolution = SPEC["grid"]["curve_bevel_resolution"]
    curve.use_fill_caps = True
    # 折线（POLY）而不是贝塞尔：实迹是解析采样出来的（双曲线弧 + 直线腿 + 闭合极轨），
    # 再套一层贝塞尔只会把形状带偏——贝塞尔画不出双曲线的渐近臂。
    spline = curve.splines.new("POLY")
    spline.points.add(len(points) - 1)
    for spline_point, position in zip(spline.points, points):
        spline_point.co = (position[0], position[1], position[2], 1.0)
    obj = link(bpy.data.objects.new(trajectory["object_name"], curve), collections[TRAJECTORIES])
    materials[trajectory["material_name"]] = emission_material(
        trajectory["material_name"], trajectory["color"], SPEC["trajectory_strength"]
    )
    obj.data.materials.append(materials[trajectory["material_name"]])
    obj["destination"] = trajectory["nodes"][-1]["id"]
    obj["node_count"] = len(trajectory["nodes"])
    obj["assist_periapsis"] = trajectory["assist"]["periapsis"]
    obj["assist_turn_deg"] = trajectory["assist"]["turn_deg"]
    # 折线点是 Blender 侧的真凭实据：报告里记的是从数据块读回来的坐标，不是发射器手里的
    # 那串数——"有没有捅进天体""顺访处有没有拐弯"因此都可以独立核对。
    control_points = [
        [float(point.co[0]), float(point.co[1]), float(point.co[2])] for point in spline.points
    ]
    return obj, control_points, node_points


def load_font(path):
    try:
        return bpy.data.fonts.load(path, check_existing=True)
    except Exception as exc:
        log("cannot load font %s (%s); falling back to the built-in font" % (path, exc))
        return bpy.data.fonts[0] if len(bpy.data.fonts) else None


def build_label(name, text, location, size, font, material, camera, collections, offset=0.0):
    curve = bpy.data.curves.new(name, type="FONT")
    curve.body = text
    curve.size = size
    curve.extrude = SPEC["labels"]["extrude"]
    curve.align_x = "CENTER"
    curve.align_y = "CENTER"
    if offset:
        curve.offset = offset
    if font is not None:
        curve.font = font
    obj = link(bpy.data.objects.new(name, curve), collections[LABELS])
    obj.location = location
    obj.data.materials.append(material)
    track = obj.constraints.new("TRACK_TO")
    track.name = "Billboard_ToCamera"
    track.target = camera
    track.track_axis = "TRACK_Z"
    track.up_axis = "UP_Y"
    return obj


def build_arrow(trajectory, materials, collections, previous_two, tip, standoff):
    """终点箭头：锥尖指向终点，退到目标球体之外。

    方向必须一眼看得出（参考图里每条转移轨道都带箭头），而曲线本身只是粗细均匀的管。
    锥体是独立对象，名字挂在它指向的节点上（``TRAJ_<终点>_ARROW``），因此仍在契约里。
    """
    arrow = trajectory["arrow"]
    approach = Vector(tip) - Vector(previous_two)
    if approach.length <= 1e-9:
        log("trajectory has a zero-length final segment; no arrow")
        return None
    # ``heading`` 指向终点（从上一个控制点看过去的方向）。锥体的 +Z 转到 heading，锥尖因此
    # 朝向终点；锥体整体退到 tip 之前 standoff + 半个锥长的地方。**符号要对**：早先写成
    # "tip 加上反向" ，箭头整个落到了终点的另一侧（放大看才发现）。
    heading = approach.normalized()
    width = trajectory["width"]
    depth = width * arrow["length_factor"]
    radius = width * arrow["radius_factor"]
    mesh = bpy.data.meshes.new(arrow["object_name"])
    build = bmesh.new()
    bmesh.ops.create_cone(
        build,
        cap_ends=True,
        cap_tris=False,
        segments=arrow["segments"],
        radius1=radius,
        radius2=0.0,
        depth=depth,
    )
    build.to_mesh(mesh)
    build.free()
    smooth(mesh)
    obj = link(bpy.data.objects.new(arrow["object_name"], mesh), collections[TRAJECTORIES])
    origin = Vector(tip) - heading * (standoff + depth / 2.0)
    obj.matrix_world = Matrix.Translation(origin) @ heading.to_track_quat("Z", "Y").to_matrix().to_4x4()
    obj.data.materials.append(materials[trajectory["material_name"]])
    obj["points_at"] = trajectory["nodes"][-1]["id"]
    obj["direction"] = [float(value) for value in heading]
    return obj


def build_labels(materials, camera, font, collections):
    """标签只是一串"在哪儿、写什么"的数据：落位由数据段给出，Blender 侧不做排版决定。

    每条标签做两件对象：深色描边层（同一段文字按 ``offset`` 外扩一丝、沿视线往后退
    ``halo_depth``）与压在上面的白字。描边层被白字盖住中间，只露出一圈深色——参考图里
    那圈深色描边。文字都朝相机，所以"往后"用的是数据段给的那条视线方向。
    """
    labels = SPEC["labels"]
    materials.setdefault(
        labels["material_name"],
        emission_material(labels["material_name"], labels["color"], labels["strength"]),
    )
    materials.setdefault(
        labels["halo_material_name"],
        emission_material(labels["halo_material_name"], labels["halo_color"], 1.0),
    )
    material = materials[labels["material_name"]]
    halo = materials[labels["halo_material_name"]]
    # view_axis 是"相机 → 目标"的方向，所以往 **+** 方向退才是退到白字后面（第一版写成
    # 负号，描边层跑到白字前面，标签成了一块黑板——放大 4 倍看才发现的）。
    behind = Vector(labels["view_axis"]) * labels["halo_depth"]
    created = []
    for item in labels["items"]:
        position = tuple(item["position"])
        created.append(
            build_label(
                item["halo_name"],
                item["text"],
                tuple(Vector(position) + behind),
                item["size"],
                font,
                halo,
                camera,
                collections,
                offset=item["size"] * labels["halo_offset_ratio"],
            )
        )
        created.append(
            build_label(
                item["object_name"],
                item["text"],
                position,
                item["size"],
                font,
                material,
                camera,
                collections,
            )
        )
    return created


def build_background(collections, materials):
    background = SPEC["background"]
    mesh = uv_sphere_mesh(
        background["object_name"],
        background["radius"],
        background["segments"],
        background["rings"],
    )
    obj = link(bpy.data.objects.new(background["object_name"], mesh), collections[BACKGROUND])
    materials[background["material_name"]] = starfield_material(background)
    obj.data.materials.append(materials[background["material_name"]])
    # 银河带落在贴图赤道上，也就是黄道面；相机俯视时它整条都在画面上方之外。绕相机的右手
    # 方向转 ``rotation_angle_deg``，带子就摆到画面中心（规格里再给一个偏移量微调高度）。
    obj.rotation_mode = "AXIS_ANGLE"
    obj.rotation_axis_angle = (
        math.radians(background["rotation_angle_deg"]),
        background["rotation_axis"][0],
        background["rotation_axis"][1],
        background["rotation_axis"][2],
    )
    obj["role"] = "starfield"
    obj["band_tilt_deg"] = background["rotation_angle_deg"]
    # 天球只对相机可见：它也参与照明的话，整个太阳系会被压成灰蒙蒙的一片。
    obj.visible_diffuse = False
    obj.visible_glossy = False
    obj.visible_shadow = False
    return obj


def build_world(background):
    world = bpy.data.worlds.new("World_starfield")
    bpy.context.scene.world = world
    tree = world.node_tree
    for node in list(tree.nodes):
        tree.nodes.remove(node)
    output = tree.nodes.new("ShaderNodeOutputWorld")
    output.name = "Output_World"
    surface = tree.nodes.new("ShaderNodeBackground")
    surface.name = "Background_Starfield"
    surface.inputs["Strength"].default_value = background["world_strength"]
    environment = tree.nodes.new("ShaderNodeTexEnvironment")
    environment.name = "Tex_WorldStarfield"
    environment.image = load_image(background["texture_path"])
    tree.links.new(environment.outputs["Color"], surface.inputs["Color"])
    tree.links.new(surface.outputs["Background"], output.inputs["Surface"])
    return world


def build_camera(collections, scene):
    """相机位置与朝向直接来自数据段（由 layout 算好），Blender 侧不再推导一次。"""
    camera = SPEC["camera"]
    location = Vector(camera["location"])
    target = Vector(camera["target"])
    right = Vector(camera["right"])
    up = Vector(camera["up"])
    forward = Vector(camera["forward"])
    data = bpy.data.cameras.new(camera["object_name"])
    data.lens = camera["focal_length_mm"]
    data.dof.use_dof = True
    data.dof.aperture_fstop = camera["aperture_f"]
    data.dof.focus_distance = (location - target).length
    # 远裁剪面必须越过背景天球：默认 100 世界单位，天球在 6000，不改的话背景整块消失。
    data.clip_start = 0.1
    data.clip_end = SPEC["background"]["radius"] * 2.0
    obj = bpy.data.objects.new(camera["object_name"], data)
    scene.collection.objects.link(obj)
    # 相机看向本地 -Z、上为本地 +Y：把 layout 给的基装成 world 矩阵。
    obj.matrix_world = Matrix(
        (
            (right.x, up.x, -forward.x, location.x),
            (right.y, up.y, -forward.y, location.y),
            (right.z, up.z, -forward.z, location.z),
            (0.0, 0.0, 0.0, 1.0),
        )
    )
    scene.camera = obj
    return obj


def configure_render(scene, seed):
    render = SPEC["render"]
    scene.render.engine = "CYCLES"
    scene.cycles.device = "GPU"
    scene.cycles.samples = render["samples"]
    scene.cycles.use_denoising = render["denoise"]
    scene.cycles.seed = seed
    scene.render.resolution_x = render["width"]
    scene.render.resolution_y = render["height"]
    scene.render.resolution_percentage = 100
    scene.render.film_transparent = False
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGB"
    scene.render.image_settings.color_depth = "16"
    scene.view_settings.view_transform = render["view_transform"]
    scene.view_settings.look = render["look"]
    scene.display_settings.display_device = render["display_device"]
    os.makedirs(render["output_dir"], exist_ok=True)
    scene.render.filepath = os.path.join(render["output_dir"], render["output_stem"] + ".png")
    log(
        "render %s %dx%d samples=%d -> %s"
        % (SPEC["mode"], render["width"], render["height"], render["samples"], scene.render.filepath)
    )
    return scene


def stamp_scene(scene):
    """把指纹写进 .blend 自己：文件搬走之后仍然说得出它派生自哪一版。"""
    scene["primer_scene_feature"] = "primer.scene"
    scene["primer_scene_spec"] = SPEC["spec_path"]
    scene["primer_scene_fingerprint"] = SPEC["fingerprint"]["digest"]
    scene["primer_scene_spec_sha256"] = SPEC["fingerprint"]["spec_sha256"]
    scene["primer_scene_mode"] = SPEC["mode"]


def build_compositor(scene):
    """合成器：Render Layers → Glare(Fog Glow) → Composite。

    ``blender.md`` 第四阶段要的就是这一步——太阳是自发光，但"自发光"与"看上去像颗星"
    之间隔着一次散射；Fog Glow 把超过阈值的像素晕开，太阳才有光晕、大气壳的辉光才铺得开。
    阈值取 1.0：只有太阳与真正亮的元素过线，星图与行星表面不会一起糊掉。

    Blender 5 的合成器是一个节点组（``scene.compositing_node_group``），组里要有一个
    组输出节点才算收口；节点按项目习惯命名。
    """
    glare = SPEC["glare"]
    tree = bpy.data.node_groups.new("Compositor_Nodes", "CompositorNodeTree")
    tree.interface.new_socket(name="Image", in_out="OUTPUT", socket_type="NodeSocketColor")
    layers = tree.nodes.new("CompositorNodeRLayers")
    layers.name = "RL_Scene"
    layers.label = "RL_Scene"
    glow = tree.nodes.new("CompositorNodeGlare")
    glow.name = "Glare_FogGlow"
    glow.label = "Glare_FogGlow"
    glow.inputs["Type"].default_value = "Fog Glow"
    glow.inputs["Quality"].default_value = "High"
    glow.inputs["Threshold"].default_value = glare["threshold"]
    glow.inputs["Size"].default_value = glare["size"]
    glow.inputs["Strength"].default_value = glare["strength"]
    glow.inputs["Smoothness"].default_value = glare["smoothness"]
    composite = tree.nodes.new("NodeGroupOutput")
    composite.name = "Composite_Output"
    composite.label = "Composite_Output"
    tree.links.new(layers.outputs["Image"], glow.inputs["Image"])
    tree.links.new(glow.outputs["Image"], composite.inputs[0])
    scene.compositing_node_group = tree
    return tree


def save_blend(scene):
    """存 .blend，并且不留 ``.blend1`` 备份。

    Blender 默认在覆盖已有文件前把上一版存成 ``.blend1``，那是为手工编辑准备的；这里每次
    构建都会重写同一个路径，备份只会在产物目录里堆垃圾，所以先把 ``save_version`` 归零。
    这个偏好在后台会话里设了不写回用户配置，因此不会改动人在 GUI 里的设置。
    """
    try:
        bpy.context.preferences.filepaths.save_version = 0
    except Exception as exc:
        log("cannot disable the .blend1 backup (%s); one will be left behind" % exc)
    bpy.ops.wm.save_as_mainfile(filepath=SPEC["blend_path"])


def render_still(scene):
    bpy.ops.render.render(write_still=True)
    if not SPEC["render"]["exr"]:
        return
    # 正式档另留 16 位 EXR 底片。不再渲一遍：从已成的 Render Result 里存。
    try:
        image = bpy.data.images.get("Render Result")
        if image is None:
            raise RuntimeError("no Render Result to save")
        path = os.path.splitext(scene.render.filepath)[0] + ".exr"
        image.save_render(path, scene=scene)
        log("saved %s" % path)
    except Exception as exc:
        log("EXR pass failed (%s); the PNG is unaffected" % exc)


PROBE_LIMIT = 256


def object_probes(obj, limit=PROBE_LIMIT):
    """对象上的一组世界坐标取样点，供"这块几何在画面上伸到哪儿"的核对使用。

    为什么不是包围盒的 8 个角：斜着拉长的对象的盒子角落在**空处**——一条从地球拉到海王星
    的航迹、一圈散开的星带，它们的盒子角在图上什么也没有，拿它判越界会报出画面里根本不
    存在的东西（第一版就报了，TRAJ 的盒角在 1.20，而曲线最远只到 0.86）。所以取**网格顶点
    本身**：那是几何真正占据的位置。约束（标签的 TRACK_TO）与修改器都走求值后的网格，否则
    朝向相机的文字会被当成没有厚度的一个点。

    点数以"每轴极值点 + 等距抽样"压到 ``limit`` 上下：极值点保证外轮廓不会漏，抽样保证
    环形、管形这种细长几何也有足够的覆盖。
    """
    if obj.type not in {"MESH", "CURVE", "FONT"}:
        return []
    depsgraph = bpy.context.evaluated_depsgraph_get()
    evaluated = obj.evaluated_get(depsgraph)
    try:
        mesh = evaluated.to_mesh()
        if mesh is None:
            return []
        matrix = evaluated.matrix_world
        points = [matrix @ vertex.co for vertex in mesh.vertices]
    except Exception as exc:
        log("cannot probe %s (%s); it will not be checked against the frame" % (obj.name, exc))
        return []
    finally:
        evaluated.to_mesh_clear()
    if not points:
        return []
    chosen = []
    for axis in range(3):
        chosen.append(min(points, key=lambda point: point[axis]))
        chosen.append(max(points, key=lambda point: point[axis]))
    stride = max(1, len(points) // max(limit, 1))
    chosen.extend(points[::stride])
    seen = set()
    probes = []
    for point in chosen:
        key = (round(point.x, 6), round(point.y, 6), round(point.z, 6))
        if key in seen:
            continue
        seen.add(key)
        probes.append([float(point.x), float(point.y), float(point.z)])
    return probes


def write_report(scene, objects, materials, trajectory, control_points, node_points):
    report = {
        "blender": bpy.app.version_string,
        "mode": SPEC["mode"],
        "fingerprint": SPEC["fingerprint"],
        "spec_path": SPEC["spec_path"],
        "blend_path": SPEC["blend_path"],
        "render": {
            "path": scene.render.filepath,
            "width": scene.render.resolution_x,
            "height": scene.render.resolution_y,
            "samples": scene.cycles.samples,
            "engine": scene.render.engine,
            "view_transform": scene.view_settings.view_transform,
            "look": scene.view_settings.look,
        },
        "collections": sorted(collection.name for collection in bpy.data.collections),
        "materials": sorted(materials),
        "objects": [
            {
                "name": obj.name,
                "type": obj.type,
                "collection": obj.users_collection[0].name if obj.users_collection else "",
                "location": [float(value) for value in obj.location],
                "probes": object_probes(obj),
                "custom": {
                    key: obj[key]
                    for key in obj.keys()
                    if not key.startswith("_") and isinstance(obj[key], (int, float, str))
                },
            }
            for obj in objects
        ],
        "trajectory": {
            "name": trajectory.name,
            "material": trajectory.active_material.name if trajectory.active_material else "",
            "points": control_points,
            "nodes": [
                {
                    "id": node["id"],
                    "kind": node["kind"],
                    "body_id": node.get("body_id"),
                    "anchor": list(node["position"]),
                    "point_index": node_points[index],
                }
                for index, node in enumerate(SPEC["trajectory"]["nodes"])
            ],
            "assist": SPEC["trajectory"]["assist"],
            "capture": SPEC["trajectory"]["capture"],
        },
        "missions": SPEC.get("missions"),
    }
    with open(SPEC["report_path"], "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, sort_keys=True, indent=2)
    return report


def main():
    scene = bpy.context.scene
    clear_scene()
    collections = build_collections()
    materials = {}
    objects = []

    camera = build_camera(collections, scene)
    objects.append(camera)
    build_world(SPEC["background"])

    sun_data = None
    for body in SPEC["bodies"]:
        if body["kind"] == "star":
            sun_data = body
            objects.append(build_sun(body, collections, materials))
            continue
        if body["material_type"] == "rock":
            objects.append(build_minor(body, collections, materials))
        else:
            objects.append(build_planet(body, collections, materials))
        if "atmosphere" in body:
            objects.append(build_shell(body, "atmosphere", collections, materials, atmosphere_material))
        if "clouds" in body:
            objects.append(build_shell(body, "clouds", collections, materials, clouds_material))
    if sun_data is None:
        raise RuntimeError("the spec declares no star to light the scene")
    objects.extend(build_lights(sun_data, collections))

    for band in SPEC["bands"]:
        objects.append(build_band(band, collections, materials, SPEC["seed"]))

    trajectory_obj, control_points, node_points = build_trajectory(materials, collections)
    objects.append(trajectory_obj)
    arrival = SPEC["trajectory"]["arrival"]
    arrival_radius = 0.0
    for body in SPEC["bodies"]:
        if body["id"] == SPEC["trajectory"]["nodes"][-1]["body_id"]:
            arrival_radius = body["radius_units"]
    arrow = build_arrow(
        SPEC["trajectory"],
        materials,
        collections,
        arrival["from"],
        arrival["tip"],
        arrival_radius * SPEC["trajectory"]["arrow"]["standoff_factor"],
    )
    if arrow is not None:
        objects.append(arrow)

    font = load_font(SPEC["labels"]["font_regular"])
    objects.extend(build_labels(materials, camera, font, collections))
    objects.extend(build_missions(materials, collections))
    objects.append(build_background(collections, materials))

    configure_render(scene, SPEC["seed"])
    build_compositor(scene)
    stamp_scene(scene)
    save_blend(scene)
    log("saved %s" % SPEC["blend_path"])
    render_still(scene)
    write_report(scene, objects, materials, trajectory_obj, control_points, node_points)
    log("report %s" % SPEC["report_path"])


if __name__ == "__main__":
    main()
'''


def render_script(
    spec: SceneSpec,
    project_root: Path,
    fingerprint: Fingerprint,
    final: bool = False,
) -> str:
    """生成完整脚本（数据段 + 静态构建代码），返回值可逐字节比较。"""
    payload = json.dumps(
        scene_data(spec, project_root, fingerprint, final=final),
        ensure_ascii=True,
        sort_keys=True,
        indent=2,
    )
    literal = json.dumps(payload, ensure_ascii=True)
    return (
        _HEADER
        + f"SCENE_JSON = {literal}\n\n"
        + "SPEC = json.loads(SCENE_JSON)\n"
        + _BUILDER
    )


def write_script(
    spec: SceneSpec,
    project_root: Path,
    fingerprint: Fingerprint,
    path: Path,
    final: bool = False,
) -> Path:
    """把脚本写到磁盘（``build/scene.py``），让人能读、能 diff、能直接拿去跑。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_script(spec, project_root, fingerprint, final=final), encoding="utf-8")
    return path
