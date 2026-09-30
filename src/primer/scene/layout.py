# -*- coding: utf-8 -*-
"""布局：把规格写死的显示位置整理成场景坐标，并把"放大倍数"折算出来。

这张图按**单调序列**布局：天体是背景，规格直接给出每个天体的显示半径与黄道面内的
``(x, y)``，不必（也不该）从轨道半径换算——"显示次序与真实次序一致"由 checks 硬核对，
算在这里是算不出来的。所以本模块在位置上几乎什么都不做，它做的是三件必要的事：

1. **落位**：``(x, y, 0)``——黄道面内，z 恒为 0。**例外**是航迹沿途的天体
   （顺访天体与捕获天体）：它们的坐标由路径按节点声明的 ``clearance_radii`` 摆出来
   （见 :func:`trajectory_polyline` 与 :attr:`TrajectoryPlan.derived`），规格里那个 ``y``
   只是标称值，也是"摆在哪一侧"的依据。
2. **显示远近**：``display_orbit_distance`` = 到恒星显示位置的欧氏距离。这是
   ``order_not_monotonic`` 的判据，也是写进天体自定义属性的量。
3. **放大倍数**：``magnification = display_radius / (real_radius_au *
   nominal_units_per_au)``。``nominal_units_per_au`` **不参与位置换算**，它只是一个基准
   尺，用来把"这颗球被放大了多少倍"折成一个可比的数——图例语言的一部分，不是中间量。

路径先算、天体后摆，所以 :func:`plan` 要先问 :func:`trajectory_polyline` 拿到那份派生位置
再落位；同一份落位数既喂发射器也喂报告，所以"报告里的数"与"场景里的数"必然一致。

相机也在这里落地（:func:`camera_placement` / :func:`project_point`）：位置、朝向基与投影
都是纯数学，放在同一个模块里，发射器把算好的基直接装进 Blender，``check`` 又用同一个投影
判断"哪个天体出画"。两处各写一份取景公式，就会出现"报告说在画内、图上却看不见"。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import List, Mapping, Optional, Sequence, Tuple

from .spec import Band, Body, CameraSpec, LayoutSpec, SceneError, SceneSpec, TrajectoryNode

__all__ = [
    "AU_KM",
    "SENSOR_WIDTH_MM",
    "BandPlacement",
    "BodyPlacement",
    "CameraPlacement",
    "NodePlacement",
    "Projection",
    "ASSIST_TRUNCATION_MARGIN_DEG",
    "CAPTURE_POLAR_TILT_DEG",
    "INCIDENTAL_FAR_FACTOR",
    "MISSION_LABEL_CLEARANCE",
    "MISSION_LABEL_SIZE",
    "AssistRecord",
    "CaptureRecord",
    "ClusterPlacement",
    "DotPlacement",
    "LegendRow",
    "TrajectoryPlan",
    "body_display_distance",
    "camera_placement",
    "frame_half_height",
    "frame_half_width",
    "label_extent",
    "legend_placements",
    "magnification",
    "mission_placements",
    "plan",
    "project_extent",
    "project_point",
    "project_sphere",
    "resolve_band",
    "resolve_body",
    "resolve_node",
    "solve_assist_eccentricity",
    "trajectory_polyline",
]

# 1 天文单位 = 149 597 870.7 km（IAU 2012 定义值）。放大倍数靠它把半径换算成 AU。
AU_KM = 149597870.7
# Blender 相机的默认片幅宽度（水平方向按它取景），单位毫米。
SENSOR_WIDTH_MM = 36.0
# 恒星只有一个（本图是太阳），它在布局里就是"显示远近"的原点；找不到时的退路。
ORIGIN = (0.0, 0.0, 0.0)


@dataclass(frozen=True)
class BodyPlacement:
    """一个天体在场景里的全部落位数，含写进自定义属性的那几个量。

    ``display_orbit_distance`` 是到恒星显示位置的欧氏距离（世界单位）——本图里它是
    "看上去多远"，由规格写死；``real_orbit_au`` 是真实远近。两者的**次序**必须一致，
    那一条由 checks 核对。
    """

    body_id: str
    kind: str
    real_orbit_au: float
    display_orbit_distance: float
    display_radius: float
    magnification: float
    real_radius_km: float
    position: Tuple[float, float, float]


@dataclass(frozen=True)
class BandPlacement:
    """碎屑带的显示范围（世界单位）：长方体中心与三个半长。"""

    band_id: str
    center: Tuple[float, float, float]
    half_x: float
    half_y: float
    half_z: float


@dataclass(frozen=True)
class NodePlacement:
    """一个航迹节点的落位；``body_id`` 非空表示它钉在那个天体的圆心。"""

    node_id: str
    label: str
    position: Tuple[float, float, float]
    body_id: Optional[str]


def magnification(body: Body, layout: LayoutSpec) -> float:
    """显示半径相对真实半径的放大倍数。

    分母用 ``nominal_units_per_au``：它只提供一个"1 AU 相当于多少世界单位"的基准，好让
    各体的放大倍数可比（太阳约 54 倍、地球约 580 倍、木星约 142 倍）。**它不是布局参数**：
    位置早就写在规格里了，这一项只进自定义属性。
    """
    real_radius_au = body.real.radius_km / AU_KM
    denominator = real_radius_au * layout.nominal_units_per_au
    if denominator <= 0.0:
        raise SceneError(f"body {body.id} has a non-positive real radius")
    return body.display.radius / denominator


def body_display_distance(body: Body, origin: Tuple[float, float, float]) -> float:
    """天体到 ``origin``（恒星的显示位置）的显示距离，世界单位。"""
    return math.hypot(body.display.x - origin[0], body.display.y - origin[1])


def resolve_body(
    body: Body, layout: LayoutSpec, origin: Tuple[float, float, float] = ORIGIN
) -> BodyPlacement:
    """一个天体的全部落位数：位置就是规格写的 ``(x, y, 0)``。

    卫星的 ``display`` 里没有写死的坐标（它相对母体给，见 :func:`_resolve_body_positions`），
    所以到这里必须已经被换成具体坐标——直接拿规格里的卫星来这儿会明确报错，而不是算出一个
    ``None`` 位置。
    """
    if body.display.x is None or body.display.y is None:
        raise SceneError(
            f"body {body.id} is anchored to another body; resolve it through plan() first"
        )
    return BodyPlacement(
        body_id=body.id,
        kind=body.kind,
        real_orbit_au=body.real.orbit_au,
        display_orbit_distance=body_display_distance(body, origin),
        display_radius=body.display.radius,
        magnification=magnification(body, layout),
        real_radius_km=body.real.radius_km,
        position=(body.display.x, body.display.y, 0.0),
    )


def resolve_band(band: Band, layout: LayoutSpec) -> BandPlacement:
    """碎屑带的显示范围（世界单位）。"""
    del layout  # 盒子的尺寸全在规格里；签名保持一致，将来加"按基准缩放"时不必改调用方
    display = band.display
    return BandPlacement(
        band_id=band.id,
        center=(display.x_center, display.y, 0.0),
        half_x=display.x_half_length,
        half_y=display.y_half_width,
        half_z=display.thickness * 0.5,
    )


def resolve_node(
    node: TrajectoryNode, bodies: Mapping[str, Body], layout: LayoutSpec
) -> NodePlacement:
    """一个航迹节点的落位：取它所钉天体的显示圆心。

    节点**取的是同一个 :func:`resolve_body` 的值**，所以"每个节点都在它那颗天体上"不是靠
    发射器对齐，而是这里根本只有一份坐标。顺访天体与捕获天体的坐标本身又是路径摆出来的
    （见 :func:`trajectory_polyline`），所以调用方要把派生后的天体表传进来。
    """
    body = bodies.get(node.body)
    if body is None:
        raise SceneError(f"trajectory node {node.id} refers to an unknown body: {node.body}")
    placement = resolve_body(body, layout)
    return NodePlacement(
        node_id=node.id, label=node.label, position=placement.position, body_id=node.body
    )


def _bodies_with_positions(
    spec: SceneSpec, overrides: Mapping[str, Tuple[float, float]]
):
    """把"由别处摆出来"的天体位置替换进规格里的天体表（其余原样）。

    两类天体走这条路：

    * **航迹沿途的天体**（顺访、捕获）：规格里的 ``display.y`` 只是**标称**位置（也是
      "摆哪一侧"的依据），真正坐标由路径给出；
    * **卫星**：规格给的是相对母体的 :class:`BodyAnchor`，母体坐标本身可能是路径摆出来
      的，所以只能在母体落位之后解。

    因此报告、发射器与 check 都必须从这一处取天体，不能有人偷偷读规格的原值。
    """
    anchors = {body.id: body.display.anchor for body in spec.bodies}
    positions: dict = {body.id: (body.display.x, body.display.y) for body in spec.bodies}
    positions.update({key: tuple(value) for key, value in overrides.items()})
    # 卫星可能挂在卫星上，逐轮解到不动点；每轮至少解出一个，环状锚定则报错。
    pending = {body.id for body in spec.bodies if anchors[body.id] is not None}
    for _ in range(len(pending) + 1):
        if not pending:
            break
        stuck = set(pending)
        for body_id in sorted(pending):
            anchor = anchors[body_id]
            parent = positions.get(anchor.body)
            if parent is None or parent[0] is None or parent[1] is None:
                continue
            positions[body_id] = (parent[0] + anchor.dx, parent[1] + anchor.dy)
            pending.discard(body_id)
        if pending == stuck:
            raise SceneError(
                "bodies anchor to each other in a cycle: " + ", ".join(sorted(pending))
            )
    out = []
    for body in spec.bodies:
        position = positions[body.id]
        out.append(
            replace(body, display=replace(body.display, x=position[0], y=position[1]))
        )
    return tuple(out)


def _resolve_placements(spec: SceneSpec, overrides: Mapping[str, Tuple[float, float]]):
    bodies = _bodies_with_positions(spec, overrides)
    # 显示远近以恒星的显示位置为原点；没有恒星（或它不在原点）也照样算得出来。
    origin = ORIGIN
    for body in bodies:
        if body.kind == "star":
            origin = (body.display.x, body.display.y, 0.0)
            break
    resolved = tuple(resolve_body(body, spec.layout, origin) for body in bodies)
    return bodies, resolved, origin


# ---------------------------------------------------------------- 任务层
#
# 第三层信息（在太阳系背景与飞行轨迹之上）：**任务要素**。源文档图 5-1 的五个层位上摆着
# 18 台探测器与航天器，本图把它们画成**自发光的小球**——刻意抽象，不建任何外形（见
# blender.md 的"不画框线图、不建外形"一带）。点位是占位的：一个点 = 一台设备，颜色 = 它
# 属于哪条任务线，标签按**簇**给（19 个点各配一条标签读不了）。
#
# 位置要么锚在天体上（相对母体的偏移，或沿"太阳—天体"连线取向的平动点），要么在规格里
# 直接给黄道面内的 (x, y)。

# 组标签的字号与摆放：字号与航迹节点标签一致（图上最小的正文一级）。
MISSION_LABEL_SIZE = 1.3
MISSION_LABEL_CLEARANCE = 2.6
# 图例里"飞行轨迹"那一行的 id：它不是任务线，颜色取自轨迹自己的样式。
LEGEND_TRAJECTORY_ID = "traj"


def label_extent(text: str, size: float) -> float:
    """文字的半宽估计（世界单位），取自"每个字都按全角算"这一最坏情形。

    西方文字的真实宽度一定小于这个值（拉丁字母与数字约 0.55 em），所以这是**保守**估计：
    它可能把本来还塞得下的标签判成压线，但不会反过来放过一个真的伸到画外的标签。文字对象
    是**居中对齐**的，所以这个半宽也正是"从锚点到文字边缘"的距离——图例把文字摆在色点右侧
    时靠的就是它。
    """
    return 0.5 * max(len(text), 1) * size


@dataclass(frozen=True)
class DotPlacement:
    """一个航天器占位点：属于哪个簇、哪个点、在哪、多大。"""

    cluster_id: str
    dot_id: str
    position: Tuple[float, float, float]
    radius: float


@dataclass(frozen=True)
class ClusterPlacement:
    """一个任务簇的落位：它属哪条线／哪个层位，点位在哪，组标签写在哪儿。"""

    cluster_id: str
    name_cn: str
    line: str
    site: str
    label: str
    color: Tuple[float, float, float]
    dots: Tuple[DotPlacement, ...]
    label_position: Tuple[float, float, float]
    label_size: float


@dataclass(frozen=True)
class LegendRow:
    """图例的一行：色点 + 文字。没有框（blender.md 禁止框线图）。

    行里不带对象名——那是发射器的活（``DOT_``／``LABEL_`` 前缀在 emit 里只有一处），
    布局只管几何。
    """

    identifier: str
    text: str
    color: Tuple[float, float, float]
    dot_position: Tuple[float, float, float]
    label_position: Tuple[float, float, float]
    dot_radius: float
    label_size: float


def _cluster_origin(
    cluster, placements: Mapping[str, BodyPlacement], origin: Tuple[float, float, float]
) -> Tuple[float, float]:
    """一个任务簇的锚点：天体 + 偏移。

    ``side``/``distance`` 的形式沿"太阳—天体"连线取向——两个平动点（日地 L2、日金 L1）
    本来就在这条线上，所以规格里写的是"背太阳 9 个单位"而不是一对坐标：母体一动，点位
    跟着动，不必人去追。
    """
    if cluster.anchor is None:
        return (cluster.x, cluster.y)
    anchor = cluster.anchor
    body = placements[anchor.body].position
    if anchor.side is None:
        return (body[0] + anchor.dx, body[1] + anchor.dy)
    ux, uy = _unit((body[0] - origin[0], body[1] - origin[1]))
    sign = 1.0 if anchor.side == "anti_sun" else -1.0
    return (body[0] + sign * ux * anchor.distance, body[1] + sign * uy * anchor.distance)


def mission_placements(spec: SceneSpec) -> Tuple[ClusterPlacement, ...]:
    """把任务层的每个簇解成具体坐标（点位 + 组标签）。"""
    missions = spec.missions
    if missions is None:
        return ()
    resolved = plan(spec)
    placements = resolved["bodies_by_id"]
    origin = resolved["origin"]
    assert isinstance(placements, Mapping) and isinstance(origin, tuple)

    items: List[ClusterPlacement] = []
    for cluster in missions.clusters:
        base = _cluster_origin(cluster, placements, origin)  # type: ignore[arg-type]
        radius = cluster.radius or missions.dot_radius
        dots = tuple(
            DotPlacement(
                cluster_id=cluster.id,
                dot_id=dot.id,
                position=(base[0] + dot.dx, base[1] + dot.dy, 0.0),
                radius=radius,
            )
            for dot in cluster.dots
        )
        if cluster.label_dx is not None and cluster.label_dy is not None:
            label_position = (base[0] + cluster.label_dx, base[1] + cluster.label_dy, 0.0)
        else:
            # 不指定就落在**点位**的上方：簇是一个整体，标签属于簇。
            left = min(dot.position[0] for dot in dots)
            right = max(dot.position[0] for dot in dots)
            top = max(dot.position[1] for dot in dots)
            label_position = (
                (left + right) * 0.5,
                top + MISSION_LABEL_CLEARANCE,
                0.0,
            )
        items.append(
            ClusterPlacement(
                cluster_id=cluster.id,
                name_cn=cluster.name_cn,
                line=cluster.line,
                site=cluster.site,
                label=cluster.label,
                color=missions.line(cluster.line).color,
                dots=dots,
                label_position=label_position,
                label_size=MISSION_LABEL_SIZE,
            )
        )
    return tuple(items)


def legend_placements(spec: SceneSpec) -> Tuple[LegendRow, ...]:
    """角落里的图例：三条染色线各一行，飞行轨迹一行，自上而下。

    行距与字号都来自规格（``missions.legend``）：它是构图决定，不是实现细节。图例本身
    也走取景核对——角落里的东西同样不许压到画边缘。
    """
    missions = spec.missions
    if missions is None:
        return ()
    legend = missions.legend
    rows: List[Tuple[str, str, Tuple[float, float, float]]] = [
        (line.id, line.name_cn, line.color) for line in missions.lines
    ]
    rows.append((LEGEND_TRAJECTORY_ID, legend.trajectory_label, spec.trajectory.style.color))
    items: List[LegendRow] = []
    for index, (identifier, text, color) in enumerate(rows):
        y = legend.y - index * legend.row_spacing
        # 文字是**居中**摆的，所以锚点要退到"色点半径 + 间隔 + 半个字宽"之外，否则头一个字
        # 正好压在色点上（第一版就是这样，图上第一个字看不见）。
        anchor_x = (
            legend.x + legend.dot_radius + legend.text_gap + label_extent(text, legend.text_size)
        )
        items.append(
            LegendRow(
                identifier=identifier,
                text=text,
                color=color,
                dot_position=(legend.x, y, 0.0),
                label_position=(anchor_x, y, 0.0),
                dot_radius=legend.dot_radius,
                label_size=legend.text_size,
            )
        )
    return tuple(items)


def plan(spec: SceneSpec) -> Mapping[str, object]:
    """把整份规格一次性换算成落位数：``bodies`` / ``bands`` / ``nodes`` / ``origin``。

    发射器与 ``report`` 都吃这一份，所以"报告里的数"与"场景里的数"必然一致。路径先算
    （:func:`trajectory_polyline`），顺访天体与捕获天体的位置由它给出，所以这里要先把那份
    派生位置拿到手，再落位。
    """
    derived = trajectory_polyline(spec).derived
    bodies, resolved, origin = _resolve_placements(spec, derived)
    by_id = {body.id: body for body in bodies}
    return {
        "bodies": resolved,
        "bodies_by_id": {placement.body_id: placement for placement in resolved},
        "bands": tuple(resolve_band(band, spec.layout) for band in spec.bands),
        "nodes": tuple(resolve_node(node, by_id, spec.layout) for node in spec.trajectory.nodes),
        "origin": origin,
    }


# ---------------------------------------------------------------- 相机取景


@dataclass(frozen=True)
class CameraPlacement:
    """相机的位置与三个正交基，供"某个天体是否落在画面里"这类判断使用。"""

    location: Tuple[float, float, float]
    forward: Tuple[float, float, float]
    right: Tuple[float, float, float]
    up: Tuple[float, float, float]


def camera_placement(camera: CameraSpec) -> CameraPlacement:
    """按方位角／仰角／距离把相机摆到目标点周围，并算出朝向基。

    约定与 Blender 一致：相机看向目标点，方位角绕 +Z 从 +X 起逆时针，仰角从黄道面起算。
    """
    azimuth = math.radians(camera.azimuth_deg)
    elevation = math.radians(camera.elevation_deg)
    distance = camera.distance
    location = (
        camera.target[0] + distance * math.cos(elevation) * math.cos(azimuth),
        camera.target[1] + distance * math.cos(elevation) * math.sin(azimuth),
        camera.target[2] + distance * math.sin(elevation),
    )
    forward = _normalize(tuple(camera.target[axis] - location[axis] for axis in range(3)))
    world_up = (0.0, 0.0, 1.0)
    right = _normalize(_cross(forward, world_up))
    up = _cross(right, forward)
    return CameraPlacement(location=location, forward=forward, right=right, up=up)


def frame_half_width(camera: CameraSpec, width: int, height: int) -> float:
    """画面在**目标平面**上的半宽（世界单位）。

    这是"这个相机装得下多大的场景"那句话的数：``distance * tan(水平半视角)``。本图里
    天体的显示轨道半径最大到 65 世界单位，而距离 96、40mm 的相机只能给出 43 单位——差值
    就是"外圈天体出画"的原因，所以它要算得出来、写进报告。
    """
    del height  # 半宽只与焦距、片幅和距离有关；纵横比决定的是半高。
    half_angle = math.atan(SENSOR_WIDTH_MM / 2.0 / camera.focal_length_mm)
    return camera.distance * math.tan(half_angle)


def project_point(
    point: Tuple[float, float, float], camera: CameraSpec, width: int, height: int
) -> Optional[Tuple[float, float]]:
    """把世界点投成归一化画面坐标：``(0, 0)`` 是画面中心，``±1`` 是画面边缘。

    点落在相机背后时返回 ``None``（投影无意义，不该被当成"在画面外"报出来）。
    """
    projection = project_sphere(point, 0.0, camera, width, height)
    return None if projection is None else projection.ndc


@dataclass(frozen=True)
class Projection:
    """一个球在画面上的落点与半宽：``ndc`` 是中心，``radius`` 是它在两个方向上的半宽。

    半径也用归一化坐标表示，所以"有没有出画"是一句话：``|ndc_x| + radius_x <= 1``。
    用球心角半径（``asin(r / d)``）而不是简单地把半径除以深度，是因为透视下**近侧**的
    球看起来更大——离相机近的那半个球面才是轮廓的边界，漏掉它就会在画面边缘放行一个
    实际已经压线甚至出画的球。
    """

    ndc: Tuple[float, float]
    radius: Tuple[float, float]


def project_sphere(
    point: Tuple[float, float, float],
    radius: float,
    camera: CameraSpec,
    width: int,
    height: int,
) -> Optional[Projection]:
    """把"球心 + 半径"投成画面上的中心与半宽；球心在相机背后返回 ``None``。"""
    placement = camera_placement(camera)
    delta = tuple(point[axis] - placement.location[axis] for axis in range(3))
    depth = _dot(delta, placement.forward)
    if depth <= 1e-9:
        return None
    half_width = SENSOR_WIDTH_MM / 2.0 / camera.focal_length_mm
    half_height = half_width * height / width
    extent = max(float(radius), 0.0)
    if extent >= depth:
        # 相机落在球体内：视野全被它占满。
        tangent = float("inf")
    else:
        tangent = extent / math.sqrt(depth * depth - extent * extent)
    return Projection(
        ndc=(
            _dot(delta, placement.right) / depth / half_width,
            _dot(delta, placement.up) / depth / half_height,
        ),
        radius=(tangent / half_width, tangent / half_height),
    )


def project_extent(
    point: Tuple[float, float, float],
    half_width: float,
    half_height: float,
    camera: CameraSpec,
    width: int,
    height: int,
) -> Optional[Projection]:
    """把"朝向相机的矩形"（文字标签）投成画面上的中心与半宽。

    广告牌文字的两轴尺寸差得很远（整幅字宽 vs 一个字高），所以两个方向分开算；用球半径
    一刀切会把竖排余量白白放大，逼出不必要的远机位。半宽半高按"同一深度上的相似三角形"
    折算——这正是朝相机的平面在针孔相机里的成像关系。
    """
    placement = camera_placement(camera)
    delta = tuple(point[axis] - placement.location[axis] for axis in range(3))
    depth = _dot(delta, placement.forward)
    if depth <= 1e-9:
        return None
    half_width_tan = SENSOR_WIDTH_MM / 2.0 / camera.focal_length_mm
    half_height_tan = half_width_tan * height / width
    return Projection(
        ndc=(
            _dot(delta, placement.right) / depth / half_width_tan,
            _dot(delta, placement.up) / depth / half_height_tan,
        ),
        radius=(
            max(float(half_width), 0.0) / depth / half_width_tan,
            max(float(half_height), 0.0) / depth / half_height_tan,
        ),
    )


def frame_half_height(camera: CameraSpec, width: int, height: int) -> float:
    """画面在目标平面上的半高（世界单位）。"""
    half_angle = math.atan(SENSOR_WIDTH_MM / 2.0 / camera.focal_length_mm)
    return camera.distance * math.tan(half_angle) * height / width


def _dot(left, right) -> float:
    return sum(left[axis] * right[axis] for axis in range(3))


def _cross(left, right) -> Tuple[float, float, float]:
    return (
        left[1] * right[2] - left[2] * right[1],
        left[2] * right[0] - left[0] * right[2],
        left[0] * right[1] - left[1] * right[0],
    )


def _normalize(vector) -> Tuple[float, float, float]:
    length = math.sqrt(sum(component * component for component in vector))
    if length <= 1e-12:
        raise SceneError("degenerate camera basis: the view direction has zero length")
    return (vector[0] / length, vector[1] / length, vector[2] / length)


# ---------------------------------------------------------------- 航迹几何
#
# 这张图最要紧的一段几何：路径不是"连起节点"的折线，而是按**语义**画出来的实迹——
#   departure        从地球附近出发（不在圆心，也不悬在空处）
#   incidental       顺访，从旁边掠过，不机动（切线不变）
#   gravity_assist   双曲线借力：入射腿 → 近木点 → 出射腿，转角就是信息
#   orbit_insertion  收进一条闭合的极轨
#
# **路径是主、天体是从**：双曲线的两条腿是它的切线，切线到焦点的垂距恒 ≥ 近木点，所以
# 任何"摆在天体连线上"的顺访天体都会被甩开十来单位（这正是上一版的问题）。现在反过来：
# 先定路径（出发点 + 木星近木点与转角 + 海王星捕获），再把 earth 之外的天体按
# ``clearance_radii``（路径到天体中心的最近距离 = 半径 × 这个数）摆到路径旁边——放与查
# 用的是同一个数，不会漂。earth 是出发点：规格给它的显示位置，路径**起点本身**落在离它
# ``offset_radii`` 个半径处、朝背离太阳的方向。
#
# 四条硬约束把几何钉死：**不许穿进任何天体的圆面**、**顺访处不许拐弯**、**顺访要"从附近
# 走"**、**借力点的近木点与转角就是规格写的那个数**。四条都由 checks 复核
# （trajectory_penetrates_body / incidental_flyby_bends / incidental_flyby_too_far /
# assist_periapsis_too_tight + assist_turn_mismatch）。

# 双曲线在真近点角 ±截断角处截断，截断角 = 声明转角 + 这个余量。余量必须为正：e→1 时
# 画出来的转角上限就是截断角本身（切线还没转到渐近线方向），所以"转角 70°"要求截断角
# 至少 70° 多一点，否则解不出来。
ASSIST_TRUNCATION_MARGIN_DEG = 5.0
# 弧上的采样点数：折线够密，切线的差分才准。
ASSIST_SAMPLES = 96
# 顺访"离得太远"的上限倍数（check 用）：实测净空超过 ``clearance_radii × 这个数`` 个半径
# 就报出来。放置用 2.5、上限用它撑到 4，于是"不许穿进去"与"不许离太远"把净空夹成一条带。
INCIDENTAL_FAR_FACTOR = 1.6
# 极轨面相对"垂直于黄道面"的倾角（度）。0 就是严格极轨，但在本图的机位下平面几乎正对
# 镜头侧边、闭环压成一条线；倾一点才看得出是个圈，黄道穿越依然陡。
CAPTURE_POLAR_TILT_DEG = 38.0
CAPTURE_LOOP_SAMPLES = 120


@dataclass(frozen=True)
class AssistRecord:
    """借力点的实测结果：近木点、转角，以及弧段两端的坐标。

    报告里记的是**量出来的**值，check 再拿它跟规格声明的 ``periapsis_radii`` / ``turn_deg``
    对账——声明与实迹不符时，是这里先露馅。
    """

    node_id: str
    body_id: str
    periapsis: float
    periapsis_radii: float
    turn_deg: float
    entry: Tuple[float, float, float]
    exit: Tuple[float, float, float]


@dataclass(frozen=True)
class CaptureRecord:
    """入轨段的实测结果：闭环的尺寸、平面方位与交接点。"""

    node_id: str
    body_id: str
    semi_major: float
    eccentricity: float
    polar: bool
    join: Tuple[float, float, float]
    plane_azimuth_deg: float


@dataclass(frozen=True)
class TrajectoryPlan:
    """整条航迹：折线、每个节点在折线上的最近点、借力与入轨的实测记录。

    ``derived`` 是"由路径摆出来"的天体位置（``body_id -> (x, y)``）：顺访天体与捕获天体
    按各自的 ``clearance_radii`` 落在路径旁边，所以它们的坐标是这条折线的产物，不是规格
    写死的。:func:`plan` 拿它去替换规格里的标称位置，于是报告、发射器、check 三处看到的是
    同一组坐标。
    """

    points: Tuple[Tuple[float, float, float], ...]
    node_points: Mapping[str, int]
    assist: Optional[AssistRecord]
    capture: Optional[CaptureRecord]
    derived: Mapping[str, Tuple[float, float]] = field(default_factory=dict)


def _flight_path_angle(true_anomaly: float, eccentricity: float) -> float:
    """双曲线的飞行路径角 γ：``tan γ = e sinν / (1 + e cosν)``。"""
    return math.atan2(
        eccentricity * math.sin(true_anomaly), 1.0 + eccentricity * math.cos(true_anomaly)
    )


def _tangent_bearing(true_anomaly: float, eccentricity: float) -> float:
    """真近点角处切线的极角 ``ν + 90° - γ(ν)``（双曲线自身坐标系，单位度）。"""
    return math.degrees(true_anomaly + math.pi / 2.0 - _flight_path_angle(true_anomaly, eccentricity))


def solve_assist_eccentricity(truncation_deg: float, turn_deg: float) -> float:
    """解出"在 ±截断角处切线正好转过 ``turn_deg``"所需的离心率。

    **为什么不用教科书上的 ``e = 1/sin(δ/2)``**：那个式子说的是两条**渐近线**的夹角。截断在
    ±50° 时曲线离渐近线还远，切线只转过 26°（δ=45°、e=2.61 时）——画出来既不是 45° 的弯，
    两条腿还得各折 9° 才能接上；要让 ±50° 处真看到 45°，那种双曲线得伸到 337 单位以外。
    所以反过来解：以"画出来的弧在截断处正好转过声明的转角"为准，得到 e ≈ 1.2（一条很开的
    双曲线）。这张图要传达的是转角与贴近程度，不是偏心率本身。
    """
    truncation = math.radians(truncation_deg)
    low, high = 1.0000001, 100.0
    for _ in range(200):
        middle = 0.5 * (low + high)
        drawn = math.degrees(2.0 * (truncation - _flight_path_angle(truncation, middle)))
        if drawn > turn_deg:
            low = middle
        else:
            high = middle
    return 0.5 * (low + high)


def _anomalies(half_angle: float, clockwise: bool, samples: int) -> List[float]:
    """沿**行进方向**排好的真近点角序列。

    双曲线的切线极角随 ν 单调增，所以从 -ν_max 走到 +ν_max 是左转；要右转就把序列倒过来。
    方向搞反了，出射腿会朝反方向伸出去——第一版就是这样，出口跑到天体上方去了。
    """
    ordered = [-half_angle + 2.0 * half_angle * index / samples for index in range(samples + 1)]
    return list(reversed(ordered)) if clockwise else ordered


def _assist_points(
    centre: Tuple[float, float],
    eccentricity: float,
    periapsis: float,
    anomalies: Sequence[float],
    orientation: float,
) -> Tuple[Tuple[float, float, float], ...]:
    """按真近点角序列采样双曲线弧段（已绕天体旋转 ``orientation``）。"""
    points = []
    for anomaly in anomalies:
        radius = periapsis * (1.0 + eccentricity) / (1.0 + eccentricity * math.cos(anomaly))
        angle = anomaly + orientation
        points.append(
            (centre[0] + radius * math.cos(angle), centre[1] + radius * math.sin(angle), 0.0)
        )
    return tuple(points)


def _mirror_points(
    points: Sequence[Tuple[float, float, float]],
    centre: Tuple[float, float],
    axis_deg: float,
) -> Tuple[Tuple[float, float, float], ...]:
    """把整条弧关于"过天体、与给定方位平行"的直线镜像。

    双曲线的**弯曲方向是固定的**：沿真近点角从小到大走，切线极角单调增，也就是永远朝一个
    方向绕——从 +16° 转到 −29° 这种"右转"用它是画不出来的（只能画出左转）。镜像一下方向就
    翻过来，而入射切线因为与镜轴平行，镜像后保持不变。
    """
    ux, uy = math.cos(math.radians(axis_deg)), math.sin(math.radians(axis_deg))
    mirrored = []
    for point in points:
        dx, dy = point[0] - centre[0], point[1] - centre[1]
        projection = dx * ux + dy * uy
        mirrored.append(
            (
                centre[0] + 2.0 * projection * ux - dx,
                centre[1] + 2.0 * projection * uy - dy,
                point[2],
            )
        )
    return tuple(mirrored)


def _unit(vector: Tuple[float, float]) -> Tuple[float, float]:
    length = math.hypot(*vector)
    if length < 1e-12:
        raise SceneError("a direction that should point somewhere has zero length")
    return (vector[0] / length, vector[1] / length)


def _line_distance(point: Sequence[float], direction_deg: float, target: Sequence[float]) -> float:
    """``target`` 到"过 ``point``、方位 ``direction_deg``"那条直线的垂距。"""
    angle = math.radians(direction_deg)
    dx, dy = math.cos(angle), math.sin(angle)
    vx, vy = target[0] - point[0], target[1] - point[1]
    return abs(dx * vy - dy * vx)


def _signed_line_distance(
    point: Sequence[float], direction_deg: float, target: Sequence[float]
) -> float:
    """``target`` 到"过 ``point``、方位 ``direction_deg``"那条直线的**有向**垂距。

    正负号说明它在行进方向的哪一侧：正 = 左侧。判断"天体是不是在弯的内圈"用它。
    """
    angle = math.radians(direction_deg)
    dx, dy = math.cos(angle), math.sin(angle)
    vx, vy = target[0] - point[0], target[1] - point[1]
    return dx * vy - dy * vx


def _line_point_at_x(
    origin: Sequence[float], direction_deg: float, x_target: float
) -> Tuple[float, float]:
    """过 ``origin``、方位 ``direction_deg`` 的直线上 ``x = x_target`` 的那一点。"""
    angle = math.radians(direction_deg)
    ux, uy = math.cos(angle), math.sin(angle)
    if abs(ux) < 1e-9:
        raise SceneError("a trajectory leg is vertical; a body cannot be placed by its x")
    return (x_target, origin[1] + (x_target - origin[0]) / ux * uy)


def _place_at_clearance(
    origin: Sequence[float],
    direction_deg: float,
    x_target: float,
    clearance: float,
    side: float,
) -> Tuple[float, float]:
    """把天体摆到"到这条腿的垂距正好是 ``clearance``"处，x 坐标保持 ``x_target``。

    ``side`` 取 +1（腿的上方）或 −1（下方）。位置由路径决定、只有垂直方向挪——左右次序
    因此原样保留。
    """
    angle = math.radians(direction_deg)
    ux, uy = math.cos(angle), math.sin(angle)
    nx, ny = -uy, ux
    offset = side * clearance
    t = (x_target - origin[0] - offset * nx) / ux
    return (x_target, origin[1] + t * uy + offset * ny)


def _assist_arc(
    centre: Tuple[float, float],
    eccentricity: float,
    periapsis: float,
    anomalies: Sequence[float],
    bearing_in: float,
    mirrored: bool,
) -> Tuple[Tuple[Tuple[float, float, float], ...], float, float]:
    """按给定入射方位摆好双曲线弧，返回（弧上的点、入射方位、出射方位）。

    ``mirrored`` 决定转弯方向：沿真近点角增大的方向走，切线极角单调增（左转）；要右转必须
    把弧镜像过来（见 :func:`_mirror_points`）——镜像轴与入射切线平行，所以入射方位不变。
    """
    tangent_in_natural = _tangent_bearing(anomalies[0], eccentricity)
    orientation = math.radians(bearing_in) - math.radians(tangent_in_natural)
    arc = _assist_points(centre, eccentricity, periapsis, anomalies, orientation)
    tangent_in = tangent_in_natural + math.degrees(orientation)
    tangent_out = _tangent_bearing(anomalies[-1], eccentricity) + math.degrees(orientation)
    if mirrored:
        arc = _mirror_points(arc, centre, tangent_in)
        tangent_out = 2.0 * tangent_in - tangent_out
    return arc, tangent_in, tangent_out


def _choose_assist_arc(
    centre: Tuple[float, float],
    eccentricity: float,
    periapsis: float,
    anomalies: Sequence[float],
    start: Sequence[float],
) -> Tuple[Tuple[Tuple[float, float, float], ...], float, float]:
    """定弧的取向与转向：**入射腿必须从出发点起**，天体落在弯的内圈。

    切线到焦点的垂距 d 与取向无关，所以"过出发点、方位 θ"的直线要当入射腿，θ 只有两个解
    （该直线到焦点的垂距必须正好是 d），且只有"天体在行进方向右侧"的那一个解——那正是右转
    的内圈。转向再二选一（原弧／镜像），其中把入射切线换到焦点另一侧的那一个才真的过出发点。
    """
    natural = _assist_points(centre, eccentricity, periapsis, anomalies, 0.0)
    tangent_in_natural = _tangent_bearing(anomalies[0], eccentricity)
    impact = _line_distance(natural[0], tangent_in_natural, centre)
    delta = (centre[0] - start[0], centre[1] - start[1])
    span = math.hypot(*delta)
    if span < impact:
        raise SceneError(
            "the departure point is too close to the assist body for any tangent leg to reach it"
        )
    psi = math.degrees(math.atan2(delta[1], delta[0]))
    beta = math.degrees(math.asin(impact / span))
    for bearing_in in (psi + beta, psi - beta):
        if _signed_line_distance(start, bearing_in, centre) >= 0.0:
            continue
        for mirrored in (False, True):
            arc, tangent_in, tangent_out = _assist_arc(
                centre, eccentricity, periapsis, anomalies, bearing_in, mirrored
            )
            # 右转：出射方位必须小于入射方位（方位角顺时针减小）。
            if _angle_between(tangent_in, tangent_out) >= 0.0:
                continue
            # 出发点必须正好落在入射腿上：镜像把切线换到焦点的另一侧，只有一侧是对的。
            if _line_distance(start, tangent_in, arc[0]) > 1e-6:
                continue
            return arc, tangent_in, tangent_out
    raise SceneError("no assist orientation puts the departure point on the incoming leg")


def _angle_between(first: float, second: float) -> float:
    """两个方位之差，折进 (−180, 180]。"""
    return (second - first + 180.0) % 360.0 - 180.0


def _nearest_index(points: Sequence[Tuple[float, float, float]], target: Sequence[float]) -> int:
    best = (float("inf"), 0)
    for index, point in enumerate(points):
        distance = math.hypot(point[0] - target[0], point[1] - target[1])
        if distance < best[0]:
            best = (distance, index)
    return best[1]


def _capture_loop(
    centre: Tuple[float, float],
    semi_major: float,
    eccentricity: float,
    join: Tuple[float, float],
    samples: int,
) -> Tuple[Tuple[float, float, float], ...]:
    """入轨后的闭合极轨：焦点在天体中心、近点在交接点、轨道面垂直于黄道面。

    近点在交接点意味着长轴指过去（``r(0) = a(1-e) = |join - centre|``），轨道面由长轴与
    +Z 张成——这就是"极轨"：它从黄道面上穿过时速度是**竖直**的，所以水平的入射腿交接进来
    一定有一次机动。这不是瑕疵，正是"电推捕获入极轨"要表达的那一下。
    """
    axis = (join[0] - centre[0], join[1] - centre[1])
    length = math.hypot(*axis)
    mx, my = (axis[0] / length, axis[1] / length) if length > 1e-9 else (1.0, 0.0)
    # 轨道面由"长轴方向"与"自转轴"张成。``polar`` 时自转轴就是 +Z，即轨道面垂直于黄道面——
    # 但在这个机位下，垂直于黄道面的平面**几乎正对镜头侧边**，整条环压成一条线。所以从极轨
    # 开始、向黄道面法线方向倾斜 :data:`CAPTURE_POLAR_TILT_DEG`：黄道穿越仍然陡（读起来还是
    # 极轨），环面却转开了一点，能看出它是个闭合的圈。见模块说明里的取舍。
    tilt = math.radians(CAPTURE_POLAR_TILT_DEG)
    wx, wy, wz = math.sin(tilt) * my, -math.sin(tilt) * mx, math.cos(tilt)
    points = []
    for index in range(samples + 1):
        anomaly = 2.0 * math.pi * index / samples
        radius = semi_major * (1.0 - eccentricity * eccentricity) / (
            1.0 + eccentricity * math.cos(anomaly)
        )
        points.append(
            (
                centre[0] + radius * math.cos(anomaly) * mx,
                centre[1] + radius * math.cos(anomaly) * my,
                radius * math.sin(anomaly) * wz,
            )
        )
    return tuple(points)


def _straight_leg(
    start: Tuple[float, float, float], end: Tuple[float, float, float], spacing: float = 0.3
) -> Tuple[Tuple[float, float, float], ...]:
    """直腿上的采样点：按固定间距铺，密度与双曲线弧一致。

    间距不能大：check 量的是"天体的中心到折线**顶点**的最近距离"，腿上的顶点稀疏时这一点
    会系统性偏大（8 段铺一条 28 单位的腿，量出来能差 0.6 个单位）。
    """
    length = math.dist(start, end)
    samples = max(2, int(math.ceil(length / spacing)) if spacing > 0.0 else 2)
    return tuple(
        (
            start[0] + (end[0] - start[0]) * index / samples,
            start[1] + (end[1] - start[1]) * index / samples,
            start[2] + (end[2] - start[2]) * index / samples,
        )
        for index in range(samples + 1)
    )


def trajectory_polyline(spec: SceneSpec) -> TrajectoryPlan:
    """把规格里的节点翻译成一条**实迹折线**，并给出由它摆出来的天体位置。

    四种节点各画各的：出发段从地球附近起始、顺访段直着掠过、借力段走双曲线、末端收进闭合
    极轨。除了出发段与入轨段，途中不再有任何拐点——"顺访不机动"这条要求因此是几何本身的
    性质，不需要人眼检查，也不靠"节点连直线"碰巧成立。

    **路径是主、天体是从**：出发点由规格给的地球位置与 ``offset_radii`` 定，弧由木星的
    近木点与转角定；顺访天体与捕获天体随后按各自的 ``clearance_radii`` 摆到路径旁边
    （返回在 :attr:`TrajectoryPlan.derived` 里）。
    """
    nodes = spec.trajectory.nodes
    base_bodies = {body.id: body for body in spec.bodies}

    assists = [index for index, node in enumerate(nodes) if node.kind == "gravity_assist"]
    if not assists:
        anchors = [resolve_node(node, base_bodies, spec.layout).position for node in nodes]
        return TrajectoryPlan(
            tuple(anchors), {node.id: index for index, node in enumerate(nodes)}, None, None
        )

    assist_index = assists[0]
    assist_node = nodes[assist_index]
    assist_body = base_bodies[assist_node.body] if assist_node.body else None
    if assist_body is None:
        raise SceneError(f"trajectory node {assist_node.id} is a gravity_assist without a body")
    centre = (assist_body.display.x, assist_body.display.y)
    radius = assist_body.display.radius
    turn = assist_node.turn_deg or 0.0
    truncation = turn + ASSIST_TRUNCATION_MARGIN_DEG
    half_angle = math.radians(truncation)
    eccentricity = solve_assist_eccentricity(truncation, turn)
    periapsis = (assist_node.periapsis_radii or 0.0) * radius
    anomalies = _anomalies(half_angle, False, ASSIST_SAMPLES)

    # 出发点：地球的显示位置上、朝背离太阳的方向偏 offset_radii 个半径。路径从这里起笔，
    # 入射腿正好压在这条线上（下面的取向求解保证），所以线是"从地球身边出去"的，不悬空。
    departure_node = next((node for node in nodes if node.kind == "departure"), None)
    if departure_node is None or departure_node.body is None:
        raise SceneError("trajectory has no departure node to start the path from")
    departure_body = base_bodies[departure_node.body]
    departure_centre = (departure_body.display.x, departure_body.display.y)
    star = next((body for body in spec.bodies if body.kind == "star"), None)
    outward = (
        _unit((departure_centre[0] - star.display.x, departure_centre[1] - star.display.y))
        if star is not None
        else (1.0, 0.0)
    )
    reach = (departure_node.offset_radii or 2.0) * departure_body.display.radius
    start = (
        departure_centre[0] + outward[0] * reach,
        departure_centre[1] + outward[1] * reach,
    )

    arc, tangent_in, tangent_out = _choose_assist_arc(
        centre, eccentricity, periapsis, anomalies, start
    )

    # 顺访与捕获天体按 clearance_radii 摆到腿旁边；x 保持规格里的值，只挪垂直方向，于是
    # 左右次序与构图不变。摆哪一侧由规格里的标称 y 决定（原来在上面的还在上面）。
    derived: dict = {}
    incoming_line = (arc[0], tangent_in)
    outgoing_line = (arc[-1], tangent_out)
    for index, node in enumerate(nodes):
        if node.kind not in ("incidental", "orbit_insertion"):
            continue
        body = base_bodies[node.body] if node.body else None
        if body is None:
            continue
        line = incoming_line if index < assist_index else outgoing_line
        clearance = (node.clearance_radii or 0.0) * body.display.radius
        side = 1.0 if body.display.y > _line_point_at_x(line[0], line[1], body.display.x)[1] else -1.0
        derived[body.id] = _place_at_clearance(
            line[0], line[1], body.display.x, clearance, side
        )

    _, placements, _ = _resolve_placements(spec, derived)
    by_id = {placement.body_id: placement for placement in placements}
    bodies = _bodies_with_positions(spec, derived)
    by_id_bodies = {body.id: body for body in bodies}
    anchors = [
        resolve_node(node, by_id_bodies, spec.layout).position for node in nodes
    ]

    points: List[Tuple[float, float, float]] = []

    # 1) 出发段：从出发点起，沿入射腿直着走到弧的入口。出发点本来就在这条腿上。
    entry = arc[0]
    points.extend(_straight_leg((start[0], start[1], 0.0), entry)[:-1])

    # 2) 借力弧
    points.extend(arc)

    # 3) 出射段 → 入轨交接点 → 闭合极轨
    capture = next((node for node in nodes if node.kind == "orbit_insertion"), None)
    capture_record: Optional[CaptureRecord] = None
    if capture is not None and capture.body:
        body = by_id_bodies[capture.body]
        body_centre = by_id[capture.body].position
        semi_major = (capture.orbit_radii or 2.0) * body.display.radius
        # 捕获天体是按"到出射腿的垂距 = clearance_radii 个半径"摆的，所以那个垂足就是闭环
        # 的近点：腿与环在这一点相切接上，不留缝，也不用再猜离心率。
        loop_periapsis = (capture.clearance_radii or 0.0) * body.display.radius
        loop_eccentricity = max(0.0, min(0.6, 1.0 - loop_periapsis / semi_major))
        angle_rad = math.radians(tangent_out)
        direction = (math.cos(angle_rad), math.sin(angle_rad))
        ox, oy = arc[-1][0] - body_centre[0], arc[-1][1] - body_centre[1]
        t = -(ox * direction[0] + oy * direction[1])
        join = (arc[-1][0] + direction[0] * t, arc[-1][1] + direction[1] * t, 0.0)
        points.extend(_straight_leg(arc[-1], join)[1:])
        loop = _capture_loop(
            body_centre[:2], semi_major, loop_eccentricity, join[:2], CAPTURE_LOOP_SAMPLES
        )
        points.extend(loop[1:])
        capture_record = CaptureRecord(
            node_id=capture.id,
            body_id=capture.body,
            semi_major=semi_major,
            eccentricity=loop_eccentricity,
            polar=bool(capture.polar),
            join=join,
            plane_azimuth_deg=math.degrees(
                math.atan2(join[1] - body_centre[1], join[0] - body_centre[0])
            ),
        )

    node_points = {
        node.id: _nearest_index(points, anchors[index]) for index, node in enumerate(nodes)
    }
    arc_distances = [math.hypot(point[0] - centre[0], point[1] - centre[1]) for point in arc]
    measured_turn = abs(
        _angle_between(
            math.degrees(math.atan2(arc[1][1] - arc[0][1], arc[1][0] - arc[0][0])),
            math.degrees(math.atan2(arc[-1][1] - arc[-2][1], arc[-1][0] - arc[-2][0])),
        )
    )
    assist_record = AssistRecord(
        node_id=assist_node.id,
        body_id=assist_node.body or "",
        periapsis=min(arc_distances),
        periapsis_radii=min(arc_distances) / radius,
        turn_deg=measured_turn,
        entry=arc[0],
        exit=arc[-1],
    )
    return TrajectoryPlan(tuple(points), node_points, assist_record, capture_record, derived)
