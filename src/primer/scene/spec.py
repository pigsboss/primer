# -*- coding: utf-8 -*-
"""场景规格（scene spec）：``mission_layout.yaml`` 的数据结构、载入与校验。

规格文件是**唯一信息源**：它写什么，场景就是什么。发射器（:mod:`primer.scene.emit`）只
消费这里解析出的冻结值对象，不在别处重算字段，也不从别处补默认值——所以"场景和规格
不一致"这一类问题只可能有两个来源：规格本身非法，或者发射器没照规格做。

校验分四层，任何一层不过就抛 :class:`SceneError`：

* **未知键**：每一层都列出允许键，多一个字就报错。拼错的字段名若被静默忽略，规格看起来
  生效了，实际渲染用的是默认值——这是最难查的一类错，所以在载入时直接拒绝。
* **必填键**：缺失时点名 ``<路径>.<键>``，而不是笼统地说"规格非法"。
* **引用完整性**：``bodies[].texture`` / ``clouds.texture`` / ``background.starfield`` 必须
  在 ``assets.files`` 里出现过，否则贴图找不到下载来源；``trajectory.nodes[].body`` 必须
  指向真实存在的 ``bodies[].id``，否则轨迹末端会悬空。
* **数值关系**：``bands[].size_range`` 递增、``aspect_ratio`` 与 ``preview``／``final``
  的分辨率一致（容差 1%，因为 854x480 这种"接近 16:9"的尺寸本来就不完全等于 16:9）。

布局是**单调序列**（``layout.kind: monotonic_sequence``）：每个天体在 ``display`` 里写明
半径与黄道面内的 ``(x, y)``，位置不从轨道半径换算。"显示的次序与真实的次序一致"这一条不在
这里管——它是**语义**约定，由 :mod:`primer.scene.checks` 的 ``order_not_monotonic`` 与
``size_not_monotonic`` 硬核对，因为规格本身写得出一个次序不对的布局。

``atmosphere.mode`` 是本包新增的字段，**缺省 ``rim``**：小尺寸预览下，"略大一圈的透明壳
+ Fresnel 驱动的边缘辉光"比体积散射便宜得多也好看得多；``volume`` 走 Principled Volume，
留给后续大图或近景。选择写在规格里而不是代码里，是因为这是**构图决定**，不是实现细节。

本模块只读规格、不改规格：值少了、值写错了、键不认识，一律抛 :class:`SceneError` 并点名
**出错的那条路径**（``assets.files[2].texture`` 这样），由人去规格里改。曾经为了迁就一处
少写引号的标量而在这里补过一刀（把"无值的键"并回上一个字符串），规格改对之后那一刀就删掉
了——读入端的容错会掩盖规格错误，而规格错误本该是**写规格的人**看见的。

异常消息与 ``code`` 一律英文，人读报告是中文（见 ``docs/guides/CODING_STANDARDS.md``）。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, List, Mapping, Optional, Sequence, Tuple

import yaml

__all__ = [
    "ASPECT_TOLERANCE",
    "ATMOSPHERE_MODES",
    "BODY_KINDS",
    "DEFAULT_ATMOSPHERE_MODE",
    "LAYOUT_KINDS",
    "MATERIAL_TYPES",
    "MIN_PERIAPSIS_RADII",
    "NODE_KINDS",
    "AssetsSpec",
    "AssetFile",
    "Atmosphere",
    "BackgroundSpec",
    "Band",
    "BandDisplay",
    "Body",
    "BodyAnchor",
    "BodyDisplay",
    "BodyMaterial",
    "BodyReal",
    "CameraSpec",
    "Clouds",
    "ColorManagement",
    "LayoutSpec",
    "MetaSpec",
    "MISSION_SIDES",
    "MissionAnchor",
    "MissionCluster",
    "MissionDot",
    "MissionLegend",
    "MissionLine",
    "Missions",
    "MissionSite",
    "RenderMode",
    "RenderOutput",
    "RenderSpec",
    "SceneError",
    "SceneSpec",
    "Trajectory",
    "TrajectoryNode",
    "TrajectoryStyle",
    "load_spec",
    "parse_spec",
]

# 大气壳的两种实现：``rim`` 是缺省（壳 + Fresnel 边缘辉光），``volume`` 是体积散射。
DEFAULT_ATMOSPHERE_MODE = "rim"
ATMOSPHERE_MODES = ("rim", "volume")
# 布局规则只有一种：单调序列。天体是背景，只要"显示的远近次序"与"真实的远近次序"一致、
# "显示的大小次序"与"真实的大小次序"一致即可，不要求成比例——这两条由 checks 硬核对。
LAYOUT_KINDS = ("monotonic_sequence",)
MATERIAL_TYPES = ("emission", "principled", "rock")
# 天体种类：恒星、行星、小天体（无贴图的岩质体，如半人马天体）。
BODY_KINDS = ("star", "planet", "minor")
# 航迹节点的四种语义。这是这张图最要紧的一组取值：它决定路径**怎么画**，而不只是标签。
# ``departure`` 从地球附近出发、``incidental`` 顺访不机动、``gravity_assist`` 双曲线借力、
# ``orbit_insertion`` 收进一条闭合轨道。见 layout.trajectory_polyline。
NODE_KINDS = ("departure", "incidental", "gravity_assist", "orbit_insertion")
# 借力点近木点相对天体半径的下限：1.5 倍半径既有"贴着走"的观感，又稳稳落在圆面之外。
MIN_PERIAPSIS_RADII = 1.5
# 854x480 相对 16:9 偏 0.08%，所以容差取 1%：这一层检的是"写错了比例"，
# 不是"分辨率不是整比"。
ASPECT_TOLERANCE = 0.01


class SceneError(Exception):
    """场景规格缺失或非法，或构建产物不符合契约。"""


# ---------------------------------------------------------------- 字段解析


def _mapping(value: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise SceneError(f"{where} must be a mapping")
    return value


def _reject_unknown(value: Mapping[str, Any], allowed: Sequence[str], where: str) -> None:
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        raise SceneError(f"{where} has unknown key(s): {', '.join(unknown)}")


def _required(value: Mapping[str, Any], key: str, where: str) -> Any:
    if value.get(key) is None:
        raise SceneError(f"{where}.{key} is required")
    return value[key]


def _text(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SceneError(f"{where} must be a non-empty string")
    return value


def _optional_text(value: Any, where: str, default: str = "") -> str:
    if value is None:
        return default
    return _text(value, where)


def _number(value: Any, where: str, minimum: Optional[float] = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SceneError(f"{where} must be a number")
    number = float(value)
    if minimum is not None and number < minimum:
        raise SceneError(f"{where} must be >= {minimum}")
    return number


def _integer(value: Any, where: str, minimum: Optional[int] = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SceneError(f"{where} must be an integer")
    if minimum is not None and value < minimum:
        raise SceneError(f"{where} must be >= {minimum}")
    return value


def _flag(value: Any, where: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise SceneError(f"{where} must be a boolean")
    return value


def _vector(value: Any, where: str, length: int, minimum: Optional[float] = None) -> Tuple[float, ...]:
    if not isinstance(value, list) or len(value) != length:
        raise SceneError(f"{where} must be a list of {length} numbers")
    return tuple(_number(item, f"{where}[{index}]", minimum) for index, item in enumerate(value))


def _vector3(value: Any, where: str) -> Tuple[float, float, float]:
    items = _vector(value, where, 3)
    return (items[0], items[1], items[2])


def _vector2(value: Any, where: str, minimum: Optional[float] = None) -> Tuple[float, float]:
    items = _vector(value, where, 2, minimum)
    return (items[0], items[1])


def _sequence(value: Any, where: str) -> List[Any]:
    if not isinstance(value, list):
        raise SceneError(f"{where} must be a list")
    return value


# ---------------------------------------------------------------- 值对象


@dataclass(frozen=True)
class MetaSpec:
    """场景的标题信息；``name`` 用作产物文件名，``title``／``caption`` 只进报告。"""

    name: str
    title: str
    source_document: str = ""
    caption: str = ""


@dataclass(frozen=True)
class RenderMode:
    """一种渲染档位：预览 64 采样 854x480，正式 512 采样 4K，同一条代码路径。"""

    width: int
    height: int
    samples: int
    denoise: bool
    exr: bool = False


@dataclass(frozen=True)
class ColorManagement:
    view_transform: str
    look: str
    display_device: str


@dataclass(frozen=True)
class RenderOutput:
    """``renders/previews`` 与 ``renders/final``，相对 ``_primer/scene/``。"""

    preview_dir: str
    final_dir: str


@dataclass(frozen=True)
class RenderSpec:
    aspect_ratio: Tuple[int, int]
    preview: RenderMode
    final: RenderMode
    color_management: ColorManagement
    output: RenderOutput

    def mode(self, final: bool) -> RenderMode:
        return self.final if final else self.preview


@dataclass(frozen=True)
class LayoutSpec:
    """布局规则与单位基准。

    ``nominal_units_per_au`` **不参与任何位置换算**——位置全部由规格写死；它只用来把
    "放大倍数"这个自定义属性折算出来（显示半径 / (真实半径 × 基准)），好让人一眼看出
    每颗球被放大了多少。这一点必须在文档里说清，否则下一个人会以为它在算布局。
    """

    kind: str
    nominal_units_per_au: float


@dataclass(frozen=True)
class BodyReal:
    radius_km: float
    orbit_au: float


@dataclass(frozen=True)
class BodyAnchor:
    """卫星相对母体的位置：``body`` 加上黄道面内的偏移（世界单位）。

    卫星的"显示位置"不能写死：母体（这里是海王星）的坐标本身是由航迹摆出来的，所以卫星
    只能用相对位置，由 :mod:`primer.scene.layout` 在母体落位之后再解出来。
    """

    body: str
    dx: float
    dy: float


@dataclass(frozen=True)
class BodyDisplay:
    """显示量：世界单位下的半径与黄道面内的位置（``z`` 恒为 0）。

    位置是**写死**的，不是从轨道半径算出来的：这张图是任务序列，不是比例模型，天体
    按"地球出发、木星借力、顺访半人马、绕海王"的次序一字排开，远近只保证次序正确。
    卫星例外：它给的是相对母体的 :class:`BodyAnchor`，``x``／``y`` 因此为空。
    """

    radius: float
    x: Optional[float] = None
    y: Optional[float] = None
    anchor: Optional[BodyAnchor] = None


@dataclass(frozen=True)
class BodyMaterial:
    """``emission`` 用 strength/color，``principled`` 用 roughness/normal_strength。"""

    type: str
    strength: float = 0.0
    color: Tuple[float, float, float] = (1.0, 1.0, 1.0)
    roughness: float = 0.8
    normal_strength: float = 0.0


@dataclass(frozen=True)
class Atmosphere:
    """略大一圈的壳。``rim`` 用 Fresnel 边缘辉光，``volume`` 用 Principled Volume。"""

    scale: float
    density: float
    color: Tuple[float, float, float]
    mode: str = DEFAULT_ATMOSPHERE_MODE


@dataclass(frozen=True)
class Clouds:
    texture: str
    scale: float
    opacity: float


@dataclass(frozen=True)
class Body:
    id: str
    name_cn: str
    kind: str
    real: BodyReal
    display: BodyDisplay
    material: BodyMaterial
    texture: Optional[str] = None
    atmosphere: Optional[Atmosphere] = None
    clouds: Optional[Clouds] = None
    # 卫星：绕着另一个天体转，不在"到太阳的显示距离"这条序列上。日心次序核对会把它们
    # 排除掉——卫星的日心距离与母体相同，并进序列必然报一次假失败。
    satellite: bool = False


@dataclass(frozen=True)
class BandDisplay:
    """碎屑带的显示范围：一块以 ``(x_center, y)`` 为中心的长方体（世界单位）。

    ``x_half_length`` / ``y_half_width`` 是半长，``thickness`` 是 z 方向的总厚度。
    """

    x_center: float
    y: float
    x_half_length: float
    y_half_width: float
    thickness: float


@dataclass(frozen=True)
class Band:
    """碎屑带：在 ``display`` 给的盒子里撒 ``count`` 颗小石块，尺寸按种子随机。"""

    id: str
    name_cn: str
    display: BandDisplay
    count: int
    size_range: Tuple[float, float]
    color: Tuple[float, float, float]


@dataclass(frozen=True)
class TrajectoryNode:
    """一个航迹节点：``kind`` 决定路径怎么走，``body`` 说它属于哪颗天体。

    ``kind`` 的四种取值各带自己的参数（见 :data:`NODE_KINDS`）：

    * ``departure``：``offset_radii`` —— 路径的**起点**落在天体中心多远（半径的倍数），
      方向背离太阳；
    * ``incidental``：``clearance_radii`` —— **天体**摆在离路径多远（半径的倍数）。路径先
      定、天体后摆，所以这个数是"摆位"与"查验"共用的同一个数；
    * ``gravity_assist``：``periapsis_radii``（≥ 1.5）与 ``turn_deg`` —— 双曲线借力的近木点
      与转角；
    * ``orbit_insertion``：``orbit_radii``、``polar`` 与 ``clearance_radii`` —— 收进多大的
      闭合轨道、是否极轨，以及捕获点离天体中心多远（= 闭合轨道的近点）。

    每个节点都必须钉在天体上：路径的位置与沿途天体的位置都由这一串天体推出来，"自由一点"
    没有意义，所以 ``display`` 不是节点的字段。
    """

    id: str
    label: str
    kind: str
    body: str
    offset_radii: Optional[float] = None
    clearance_radii: Optional[float] = None
    periapsis_radii: Optional[float] = None
    turn_deg: Optional[float] = None
    orbit_radii: Optional[float] = None
    polar: bool = False


@dataclass(frozen=True)
class TrajectoryStyle:
    color: Tuple[float, float, float]
    width: float


@dataclass(frozen=True)
class Trajectory:
    nodes: Tuple[TrajectoryNode, ...]
    style: TrajectoryStyle

    @property
    def destination(self) -> TrajectoryNode:
        """终点节点——整条曲线以它命名（``TRAJ_<destination-id>``）。"""
        return self.nodes[-1]


@dataclass(frozen=True)
class CameraSpec:
    focal_length_mm: float
    aperture_f: float
    elevation_deg: float
    azimuth_deg: float
    distance: float
    target: Tuple[float, float, float]


@dataclass(frozen=True)
class AssetFile:
    """一条贴图声明：规格里的文件名，加使用方与额外出处（如银河全景另有作者）。"""

    texture: str
    used_by: str
    credit_extra: str = ""


@dataclass(frozen=True)
class AssetsSpec:
    source: str
    license: str
    credit: str
    base_url: str
    files: Tuple[AssetFile, ...]


@dataclass(frozen=True)
class BackgroundSpec:
    """背景天球：贴图、自发光增益、深空底色，以及银河带在画面里的落点。

    三个可选字段都有缺省值，写法与 ``atmosphere.mode`` 同一路数——规格不写就按缺省的
    画法来，写了就以规格为准：

    * ``texture_power``（缺省 0.3）：作用在贴图上的幂次曲线（``out = in ** power``）。
      这一项是必需的，不是调味：星图的线性动态范围极大（银河带约 ``3e-4``，亮星约
      ``2.4e-2``，差 80 倍），单靠增益要么带子看不见、要么星星全爆掉；取 ``power < 1``
      先提起暗部（带子）再压亮部（星），两者才同时看得见。
    * ``strength``（缺省 8.0）：整型之后的增益。
    * ``field_color``（缺省纯黑）：底色，加在贴图之上。参考图的深空是深蓝而不是纯黑，
      正是这一项。
    * ``band_offset_deg``（缺省 0.0）：银河带相对**画面中心**的高度（度）。星图的银河带
      落在等距柱状投影的赤道上，也就是黄道面；相机从黄道面之上俯视，视线落点在地平线
      以下的 38°，所以带子默认根本不在画面里。这个数由发射器换算成天球的旋转。
    """

    starfield: str
    texture_power: float = 0.3
    strength: float = 8.0
    field_color: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    band_offset_deg: float = 0.0


@dataclass(frozen=True)
class MissionLine:
    """一条任务线：它的名字与颜色。图例的一行就是它。"""

    id: str
    name_cn: str
    color: Tuple[float, float, float]


@dataclass(frozen=True)
class MissionSite:
    """一个空间层位（源文档图 5-1 的"层位"）：日地 L2 / 日金 L1 / 类金星轨道 / 金星 /
    外太阳系。层位本身不画东西，它把任务簇分门别类，也是"五个层位一个都没落"的核对依据。
    """

    id: str
    name_cn: str


@dataclass(frozen=True)
class MissionDot:
    """一个航天器占位点：相对所在簇的锚点的偏移（世界单位，黄道面内）。

    点位是**刻意抽象**的：源文档里这些都是探测器与航天器，本图不建任何外形，只画一颗
    自发光的小球表示"这里有一台"。
    """

    id: str
    dx: float
    dy: float


@dataclass(frozen=True)
class MissionAnchor:
    """任务簇的位置基准：锚在一个天体上，两种说法二选一。

    * ``offset``：直接给黄道面内的偏移；
    * ``side`` + ``distance``：沿"太阳—天体"连线取向（``sunward`` 靠太阳、``anti_sun``
      背太阳）走多远——日金 L1 与日地 L2 两个平动点就是这两种。
    """

    body: str
    dx: Optional[float] = None
    dy: Optional[float] = None
    side: Optional[str] = None
    distance: Optional[float] = None


@dataclass(frozen=True)
class MissionCluster:
    """一组同属一个任务单元的点位，外加**一条**组标签（19 个点各配一条标签读不了）。"""

    id: str
    name_cn: str
    line: str
    site: str
    label: str
    radius: float
    dots: Tuple[MissionDot, ...]
    x: Optional[float] = None
    y: Optional[float] = None
    anchor: Optional[MissionAnchor] = None
    label_dx: Optional[float] = None
    label_dy: Optional[float] = None


@dataclass(frozen=True)
class MissionLegend:
    """图例：屏幕角落里的几行"色点 + 文字"，**不画框**（见 blender.md 的禁止事项）。

    三条任务线各一行（颜色与名字取自 ``lines``），最后一行是飞行轨迹——它也染了色，
    所以也得进图例，否则那条琥珀色的线就成了没有说法的东西。
    """

    x: float
    y: float
    row_spacing: float
    dot_radius: float
    text_size: float
    text_gap: float
    trajectory_label: str


@dataclass(frozen=True)
class Missions:
    """任务层：三条染色线、五个层位、若干任务簇，外加角落里的图例。"""

    lines: Tuple[MissionLine, ...]
    sites: Tuple[MissionSite, ...]
    clusters: Tuple[MissionCluster, ...]
    legend: MissionLegend
    dot_radius: float = 0.45
    dot_strength: float = 1.5

    def line(self, line_id: str) -> MissionLine:
        for item in self.lines:
            if item.id == line_id:
                return item
        raise SceneError(f"unknown mission line: {line_id}")

    @property
    def dot_count(self) -> int:
        return sum(len(cluster.dots) for cluster in self.clusters)


MISSION_SIDES = ("sunward", "anti_sun")


@dataclass(frozen=True)
class SceneSpec:
    """一份校验过的场景规格；``path`` 是它来自哪个文件（不进 YAML）。"""

    meta: MetaSpec
    render: RenderSpec
    layout: LayoutSpec
    bodies: Tuple[Body, ...]
    bands: Tuple[Band, ...]
    trajectory: Trajectory
    camera: CameraSpec
    assets: AssetsSpec
    background: BackgroundSpec
    path: Path
    # 任务层是**可选**的：背景层单独照样成图（早先的图就没有这一层）。没有 `missions:`
    # 时，发射器不建任何点位，三个 mission_* 判据也一并跳过。
    missions: Optional[Missions] = None

    def body(self, body_id: str) -> Body:
        for body in self.bodies:
            if body.id == body_id:
                return body
        raise SceneError(f"unknown body: {body_id}")

    def asset(self, texture: str) -> AssetFile:
        for entry in self.assets.files:
            if entry.texture == texture:
                return entry
        raise SceneError(f"texture is not declared in assets.files: {texture}")

    @property
    def textures(self) -> Tuple[str, ...]:
        """规格里出现过的全部贴图文件名，按 ``assets.files`` 的次序。"""
        return tuple(entry.texture for entry in self.assets.files)


# ---------------------------------------------------------------- 各段落


def _parse_meta(value: Any) -> MetaSpec:
    where = "meta"
    mapping = _mapping(value, where)
    _reject_unknown(mapping, ("name", "title", "source_document", "caption"), where)
    return MetaSpec(
        name=_text(_required(mapping, "name", where), f"{where}.name"),
        title=_text(_required(mapping, "title", where), f"{where}.title"),
        source_document=_optional_text(mapping.get("source_document"), f"{where}.source_document"),
        caption=_optional_text(mapping.get("caption"), f"{where}.caption"),
    )


def _parse_mode(value: Any, where: str) -> RenderMode:
    mapping = _mapping(value, where)
    _reject_unknown(mapping, ("width", "height", "samples", "denoise", "exr"), where)
    return RenderMode(
        width=_integer(_required(mapping, "width", where), f"{where}.width", 1),
        height=_integer(_required(mapping, "height", where), f"{where}.height", 1),
        samples=_integer(_required(mapping, "samples", where), f"{where}.samples", 1),
        denoise=_flag(mapping.get("denoise"), f"{where}.denoise", True),
        exr=_flag(mapping.get("exr"), f"{where}.exr", False),
    )


def _parse_render(value: Any) -> RenderSpec:
    where = "render"
    mapping = _mapping(value, where)
    _reject_unknown(mapping, ("aspect_ratio", "preview", "final", "color_management", "output"), where)

    raw_ratio = mapping.get("aspect_ratio")
    if not isinstance(raw_ratio, list) or len(raw_ratio) != 2:
        raise SceneError(f"{where}.aspect_ratio must be a list of 2 integers")
    aspect = (
        _integer(raw_ratio[0], f"{where}.aspect_ratio[0]", 1),
        _integer(raw_ratio[1], f"{where}.aspect_ratio[1]", 1),
    )

    color_where = f"{where}.color_management"
    color = _mapping(_required(mapping, "color_management", where), color_where)
    _reject_unknown(color, ("view_transform", "look", "display_device"), color_where)

    output_where = f"{where}.output"
    output = _mapping(_required(mapping, "output", where), output_where)
    _reject_unknown(output, ("preview_dir", "final_dir"), output_where)

    spec = RenderSpec(
        aspect_ratio=aspect,
        preview=_parse_mode(_required(mapping, "preview", where), f"{where}.preview"),
        final=_parse_mode(_required(mapping, "final", where), f"{where}.final"),
        color_management=ColorManagement(
            view_transform=_text(_required(color, "view_transform", color_where), f"{color_where}.view_transform"),
            look=_optional_text(color.get("look"), f"{color_where}.look", "None"),
            display_device=_optional_text(color.get("display_device"), f"{color_where}.display_device", "sRGB"),
        ),
        output=RenderOutput(
            preview_dir=_text(_required(output, "preview_dir", output_where), f"{output_where}.preview_dir"),
            final_dir=_text(_required(output, "final_dir", output_where), f"{output_where}.final_dir"),
        ),
    )
    _check_aspect(spec)
    return spec


def _check_aspect(render: RenderSpec) -> None:
    """比例写错时点名是哪一档：854x480 这种"接近 16:9"的尺寸在容差内，4:3 会被挡下。"""
    numerator, denominator = render.aspect_ratio
    declared = numerator / denominator
    for label, mode in (("preview", render.preview), ("final", render.final)):
        actual = mode.width / mode.height
        if abs(actual - declared) / declared > ASPECT_TOLERANCE:
            raise SceneError(
                f"render.{label} {mode.width}x{mode.height} does not match "
                f"aspect_ratio {numerator}:{denominator}"
            )


def _parse_layout(value: Any) -> LayoutSpec:
    where = "layout"
    mapping = _mapping(value, where)
    _reject_unknown(mapping, ("kind", "nominal_units_per_au"), where)

    kind = _text(_required(mapping, "kind", where), f"{where}.kind")
    if kind not in LAYOUT_KINDS:
        raise SceneError(f"{where}.kind must be one of: {', '.join(LAYOUT_KINDS)}")
    return LayoutSpec(
        kind=kind,
        nominal_units_per_au=_number(
            _required(mapping, "nominal_units_per_au", where),
            f"{where}.nominal_units_per_au",
            1e-9,
        ),
    )


def _parse_body(value: Any, index: int, kind_of: Mapping[str, str]) -> Body:
    where = f"bodies[{index}]"
    mapping = _mapping(value, where)
    _reject_unknown(
        mapping,
        ("id", "name_cn", "kind", "real", "display", "material", "texture", "atmosphere",
         "clouds", "satellite"),
        where,
    )
    body_id = _text(_required(mapping, "id", where), f"{where}.id")
    if body_id in kind_of:
        raise SceneError(f"duplicate body id: {body_id}")
    kind = _text(_required(mapping, "kind", where), f"{where}.kind")
    if kind not in BODY_KINDS:
        raise SceneError(f"{where}.kind must be one of: {', '.join(BODY_KINDS)}")

    real_where = f"{where}.real"
    real = _mapping(_required(mapping, "real", where), real_where)
    _reject_unknown(real, ("radius_km", "orbit_au"), real_where)

    display_where = f"{where}.display"
    display = _parse_body_display(_required(mapping, "display", where), display_where)

    material_where = f"{where}.material"
    material = _mapping(_required(mapping, "material", where), material_where)
    _reject_unknown(material, ("type", "strength", "color", "roughness", "normal_strength"), material_where)

    return Body(
        id=body_id,
        name_cn=_text(_required(mapping, "name_cn", where), f"{where}.name_cn"),
        kind=kind,
        real=BodyReal(
            radius_km=_number(_required(real, "radius_km", real_where), f"{real_where}.radius_km", 1e-9),
            orbit_au=_number(_required(real, "orbit_au", real_where), f"{real_where}.orbit_au", 0.0),
        ),
        display=display,
        material=_parse_material(material, material_where, kind),
        # 贴图可以不给：小天体（如半人马天体）没有公开贴图，用的是程序化的岩质材质。
        texture=_optional_text(mapping.get("texture"), f"{where}.texture") or None,
        atmosphere=_parse_atmosphere(mapping.get("atmosphere"), f"{where}.atmosphere"),
        clouds=_parse_clouds(mapping.get("clouds"), f"{where}.clouds"),
        satellite=_flag(mapping.get("satellite"), f"{where}.satellite", False),
    )


def _parse_body_display(value: Any, where: str) -> BodyDisplay:
    """天体位置：写死的 ``x``/``y``，或相对母体的 ``anchor``（卫星用后者）。

    两者只能给一个。卫星的位置必须相对母体给——母体（海王星）的坐标本身由航迹摆出来，
    写死的坐标会在母体一动之后悄悄错位。
    """
    mapping = _mapping(value, where)
    _reject_unknown(mapping, ("radius", "x", "y", "anchor"), where)
    radius = _number(_required(mapping, "radius", where), f"{where}.radius", 1e-9)
    anchor = mapping.get("anchor")
    if anchor is not None:
        if mapping.get("x") is not None or mapping.get("y") is not None:
            raise SceneError(f"{where} must give either x/y or anchor, not both")
        anchor_where = f"{where}.anchor"
        point = _mapping(anchor, anchor_where)
        _reject_unknown(point, ("body", "offset"), anchor_where)
        offset = _vector2(_required(point, "offset", anchor_where), f"{anchor_where}.offset")
        return BodyDisplay(
            radius=radius,
            anchor=BodyAnchor(
                body=_text(_required(point, "body", anchor_where), f"{anchor_where}.body"),
                dx=offset[0],
                dy=offset[1],
            ),
        )
    return BodyDisplay(
        radius=radius,
        x=_number(_required(mapping, "x", where), f"{where}.x"),
        y=_number(_required(mapping, "y", where), f"{where}.y"),
    )


def _parse_material(value: Mapping[str, Any], where: str, kind: str) -> BodyMaterial:
    default_type = "emission" if kind == "star" else ("rock" if kind == "minor" else "principled")
    material_type = _optional_text(value.get("type"), f"{where}.type", default_type)
    if material_type not in MATERIAL_TYPES:
        raise SceneError(f"{where}.type must be one of: {', '.join(MATERIAL_TYPES)}")
    color = value.get("color")
    return BodyMaterial(
        type=material_type,
        strength=_number(value.get("strength", 0.0), f"{where}.strength", 0.0),
        color=_vector3(color, f"{where}.color") if color is not None else (1.0, 1.0, 1.0),
        roughness=_number(value.get("roughness", 0.8), f"{where}.roughness", 0.0),
        normal_strength=_number(value.get("normal_strength", 0.0), f"{where}.normal_strength", 0.0),
    )


def _parse_atmosphere(value: Any, where: str) -> Optional[Atmosphere]:
    if value is None:
        return None
    mapping = _mapping(value, where)
    _reject_unknown(mapping, ("scale", "density", "color", "mode"), where)
    mode = _optional_text(mapping.get("mode"), f"{where}.mode", DEFAULT_ATMOSPHERE_MODE)
    if mode not in ATMOSPHERE_MODES:
        raise SceneError(f"{where}.mode must be one of: {', '.join(ATMOSPHERE_MODES)}")
    scale = _number(_required(mapping, "scale", where), f"{where}.scale", 1e-9)
    if scale <= 1.0:
        raise SceneError(f"{where}.scale must be > 1 (the shell has to enclose the body)")
    return Atmosphere(
        scale=scale,
        density=_number(mapping.get("density", 0.0), f"{where}.density", 0.0),
        color=_vector3(_required(mapping, "color", where), f"{where}.color"),
        mode=mode,
    )


def _parse_clouds(value: Any, where: str) -> Optional[Clouds]:
    if value is None:
        return None
    mapping = _mapping(value, where)
    _reject_unknown(mapping, ("texture", "scale", "opacity"), where)
    scale = _number(_required(mapping, "scale", where), f"{where}.scale", 1e-9)
    if scale <= 1.0:
        raise SceneError(f"{where}.scale must be > 1 (the cloud deck has to enclose the body)")
    return Clouds(
        texture=_text(_required(mapping, "texture", where), f"{where}.texture"),
        scale=scale,
        opacity=_number(_required(mapping, "opacity", where), f"{where}.opacity", 0.0),
    )


def _parse_band(value: Any, index: int, seen: List[str]) -> Band:
    where = f"bands[{index}]"
    mapping = _mapping(value, where)
    _reject_unknown(mapping, ("id", "name_cn", "display", "count", "size_range", "color"), where)
    band_id = _text(_required(mapping, "id", where), f"{where}.id")
    if band_id in seen:
        raise SceneError(f"duplicate band id: {band_id}")
    display_where = f"{where}.display"
    display = _mapping(_required(mapping, "display", where), display_where)
    _reject_unknown(
        display, ("x_center", "y", "x_half_length", "y_half_width", "thickness"), display_where
    )
    size_range = _vector2(_required(mapping, "size_range", where), f"{where}.size_range", 1e-9)
    if size_range[1] < size_range[0]:
        raise SceneError(f"{where}.size_range must ascend: [{size_range[0]}, {size_range[1]}]")
    return Band(
        id=band_id,
        name_cn=_text(_required(mapping, "name_cn", where), f"{where}.name_cn"),
        display=BandDisplay(
            x_center=_number(_required(display, "x_center", display_where), f"{display_where}.x_center"),
            y=_number(_required(display, "y", display_where), f"{display_where}.y"),
            x_half_length=_number(
                _required(display, "x_half_length", display_where), f"{display_where}.x_half_length", 1e-9
            ),
            y_half_width=_number(
                _required(display, "y_half_width", display_where), f"{display_where}.y_half_width", 1e-9
            ),
            thickness=_number(
                _required(display, "thickness", display_where), f"{display_where}.thickness", 1e-9
            ),
        ),
        count=_integer(_required(mapping, "count", where), f"{where}.count", 1),
        size_range=size_range,
        color=_vector3(_required(mapping, "color", where), f"{where}.color"),
    )


def _parse_node(value: Any, index: int, seen: List[str]) -> TrajectoryNode:
    where = f"trajectory.nodes[{index}]"
    mapping = _mapping(value, where)
    _reject_unknown(
        mapping,
        ("id", "kind", "body", "label", "offset_radii", "clearance_radii",
         "periapsis_radii", "turn_deg", "orbit_radii", "polar"),
        where,
    )
    node_id = _text(_required(mapping, "id", where), f"{where}.id")
    if node_id in seen:
        raise SceneError(f"duplicate trajectory node id: {node_id}")
    kind = _text(_required(mapping, "kind", where), f"{where}.kind")
    if kind not in NODE_KINDS:
        raise SceneError(
            f"{where}.kind must be one of: {', '.join(NODE_KINDS)} (node {node_id})"
        )
    # 节点一律钉在天体上：位置要么是出发点（天体坐标 + offset_radii），要么由路径摆出来。
    body = _text(_required(mapping, "body", where), f"{where}.body")

    node = TrajectoryNode(
        id=node_id,
        label=_optional_text(mapping.get("label"), f"{where}.label"),
        kind=kind,
        body=body,
    )
    if kind == "departure":
        radii = _number(mapping.get("offset_radii", 2.0), f"{where}.offset_radii", 1e-9)
        if radii < 1.0:
            raise SceneError(f"{where}.offset_radii must be >= 1 (node {node_id}, the path starts outside the body)")
        return replace(node, offset_radii=radii)
    if kind == "gravity_assist":
        radii = _number(mapping.get("periapsis_radii", 0.0), f"{where}.periapsis_radii", 0.0)
        if radii < MIN_PERIAPSIS_RADII:
            raise SceneError(
                f"{where}.periapsis_radii must be >= {MIN_PERIAPSIS_RADII} (node {node_id}, "
                "a tighter pass is not legible)"
            )
        turn = _number(mapping.get("turn_deg", 0.0), f"{where}.turn_deg", 0.0)
        if not 0.0 < turn < 180.0:
            raise SceneError(f"{where}.turn_deg must be in (0, 180) (node {node_id})")
        return replace(node, periapsis_radii=radii, turn_deg=turn)
    if kind == "orbit_insertion":
        radii = _number(mapping.get("orbit_radii", 0.0), f"{where}.orbit_radii", 0.0)
        if radii <= 1.0:
            raise SceneError(
                f"{where}.orbit_radii must be > 1 (node {node_id}, the orbit has to enclose the body)"
            )
        clearance = _number(
            _required(mapping, "clearance_radii", where), f"{where}.clearance_radii", 0.0
        )
        if clearance < 1.0:
            raise SceneError(
                f"{where}.clearance_radii must be >= 1 (node {node_id}, the capture point has to "
                "stay outside the disc)"
            )
        return replace(
            node,
            orbit_radii=radii,
            clearance_radii=clearance,
            polar=_flag(mapping.get("polar"), f"{where}.polar", True),
        )
    if kind == "incidental":
        if node.body is None:
            raise SceneError(
                f"{where}.body is required for kind incidental (node {node_id}, the body is placed "
                "clearance_radii away from the path, so it has to be a body)"
            )
        clearance = _number(
            _required(mapping, "clearance_radii", where), f"{where}.clearance_radii", 0.0
        )
        for extra in ("offset_radii", "periapsis_radii", "turn_deg", "orbit_radii", "polar"):
            if mapping.get(extra) is not None:
                raise SceneError(
                    f"{where}.{extra} does not apply to kind incidental (node {node_id})"
                )
        return replace(node, clearance_radii=clearance)
    raise SceneError(f"{where}.kind is not implemented: {kind}")


def _parse_missions(value: Any) -> Missions:
    where = "missions"
    mapping = _mapping(value, where)
    _reject_unknown(mapping, ("lines", "sites", "clusters", "legend", "dot_radius", "dot_strength"), where)

    lines: List[MissionLine] = []
    for index, item in enumerate(_sequence(_required(mapping, "lines", where), f"{where}.lines")):
        line_where = f"{where}.lines[{index}]"
        entry = _mapping(item, line_where)
        _reject_unknown(entry, ("id", "name_cn", "color"), line_where)
        line_id = _text(_required(entry, "id", line_where), f"{line_where}.id")
        if any(line.id == line_id for line in lines):
            raise SceneError(f"duplicate mission line id: {line_id}")
        lines.append(
            MissionLine(
                id=line_id,
                name_cn=_text(_required(entry, "name_cn", line_where), f"{line_where}.name_cn"),
                color=_vector3(_required(entry, "color", line_where), f"{line_where}.color"),
            )
        )
    if not lines:
        raise SceneError(f"{where}.lines must not be empty")

    sites: List[MissionSite] = []
    for index, item in enumerate(_sequence(_required(mapping, "sites", where), f"{where}.sites")):
        site_where = f"{where}.sites[{index}]"
        entry = _mapping(item, site_where)
        _reject_unknown(entry, ("id", "name_cn"), site_where)
        site_id = _text(_required(entry, "id", site_where), f"{site_where}.id")
        if any(site.id == site_id for site in sites):
            raise SceneError(f"duplicate mission site id: {site_id}")
        sites.append(
            MissionSite(
                id=site_id,
                name_cn=_text(_required(entry, "name_cn", site_where), f"{site_where}.name_cn"),
            )
        )
    if not sites:
        raise SceneError(f"{where}.sites must not be empty")

    clusters: List[MissionCluster] = []
    seen: List[str] = []
    for index, item in enumerate(
        _sequence(_required(mapping, "clusters", where), f"{where}.clusters")
    ):
        cluster = _parse_mission_cluster(item, index, seen)
        seen.append(cluster.id)
        clusters.append(cluster)
    if not clusters:
        raise SceneError(f"{where}.clusters must not be empty")

    return Missions(
        lines=tuple(lines),
        sites=tuple(sites),
        clusters=tuple(clusters),
        legend=_parse_legend(_required(mapping, "legend", where)),
        dot_radius=_number(mapping.get("dot_radius", 0.45), f"{where}.dot_radius", 1e-9),
        dot_strength=_number(mapping.get("dot_strength", 1.5), f"{where}.dot_strength", 0.0),
    )


def _parse_legend(value: Any) -> MissionLegend:
    where = "missions.legend"
    mapping = _mapping(value, where)
    _reject_unknown(
        mapping,
        ("at", "row_spacing", "dot_radius", "text_size", "text_gap", "trajectory_label"),
        where,
    )
    at = _vector2(_required(mapping, "at", where), f"{where}.at")
    return MissionLegend(
        x=at[0],
        y=at[1],
        row_spacing=_number(_required(mapping, "row_spacing", where), f"{where}.row_spacing", 1e-9),
        dot_radius=_number(_required(mapping, "dot_radius", where), f"{where}.dot_radius", 1e-9),
        text_size=_number(_required(mapping, "text_size", where), f"{where}.text_size", 1e-9),
        text_gap=_number(_required(mapping, "text_gap", where), f"{where}.text_gap", 0.0),
        trajectory_label=_text(
            _required(mapping, "trajectory_label", where), f"{where}.trajectory_label"
        ),
    )


def _parse_mission_cluster(value: Any, index: int, seen: Sequence[str]) -> MissionCluster:
    where = f"missions.clusters[{index}]"
    mapping = _mapping(value, where)
    _reject_unknown(
        mapping,
        ("id", "name_cn", "line", "site", "label", "radius", "dots", "at", "anchor",
         "label_offset"),
        where,
    )
    cluster_id = _text(_required(mapping, "id", where), f"{where}.id")
    if cluster_id in seen:
        raise SceneError(f"duplicate mission cluster id: {cluster_id}")

    dots: List[MissionDot] = []
    dot_ids: List[str] = []
    for dot_index, item in enumerate(_sequence(_required(mapping, "dots", where), f"{where}.dots")):
        dot_where = f"{where}.dots[{dot_index}]"
        entry = _mapping(item, dot_where)
        _reject_unknown(entry, ("id", "offset"), dot_where)
        dot_id = _text(_required(entry, "id", dot_where), f"{dot_where}.id")
        if dot_id in dot_ids:
            raise SceneError(f"duplicate mission dot id in {cluster_id}: {dot_id}")
        dot_ids.append(dot_id)
        offset = _vector2(_required(entry, "offset", dot_where), f"{dot_where}.offset")
        dots.append(MissionDot(id=dot_id, dx=offset[0], dy=offset[1]))
    if not dots:
        raise SceneError(f"{where}.dots must not be empty")

    at = mapping.get("at")
    anchor = mapping.get("anchor")
    if at is not None and anchor is not None:
        raise SceneError(f"{where} must give either at or anchor, not both")
    if at is None and anchor is None:
        raise SceneError(f"{where} must give either at or anchor")
    x = y = None
    parsed_anchor: Optional[MissionAnchor] = None
    if at is not None:
        x, y = _vector2(at, f"{where}.at")
    else:
        parsed_anchor = _parse_mission_anchor(anchor, f"{where}.anchor")

    label_offset = mapping.get("label_offset")
    label_dx = label_dy = None
    if label_offset is not None:
        offset = _vector2(label_offset, f"{where}.label_offset")
        label_dx, label_dy = offset

    return MissionCluster(
        id=cluster_id,
        name_cn=_text(_required(mapping, "name_cn", where), f"{where}.name_cn"),
        line=_text(_required(mapping, "line", where), f"{where}.line"),
        site=_text(_required(mapping, "site", where), f"{where}.site"),
        label=_text(_required(mapping, "label", where), f"{where}.label"),
        radius=_number(mapping.get("radius", 0.0), f"{where}.radius", 0.0),
        dots=tuple(dots),
        x=x,
        y=y,
        anchor=parsed_anchor,
        label_dx=label_dx,
        label_dy=label_dy,
    )


def _parse_mission_anchor(value: Any, where: str) -> MissionAnchor:
    mapping = _mapping(value, where)
    _reject_unknown(mapping, ("body", "offset", "side", "distance"), where)
    body = _text(_required(mapping, "body", where), f"{where}.body")
    offset = mapping.get("offset")
    side = mapping.get("side")
    distance = mapping.get("distance")
    if offset is not None:
        if side is not None or distance is not None:
            raise SceneError(f"{where} must give either offset or side+distance, not both")
        point = _vector2(offset, f"{where}.offset")
        return MissionAnchor(body=body, dx=point[0], dy=point[1])
    if side is None or distance is None:
        raise SceneError(f"{where} must give either offset or side+distance")
    side_name = _text(side, f"{where}.side")
    if side_name not in MISSION_SIDES:
        raise SceneError(f"{where}.side must be one of: {', '.join(MISSION_SIDES)}")
    return MissionAnchor(
        body=body,
        side=side_name,
        distance=_number(distance, f"{where}.distance", 0.0),
    )


def _parse_trajectory(value: Any) -> Trajectory:
    where = "trajectory"
    mapping = _mapping(value, where)
    _reject_unknown(mapping, ("nodes", "style"), where)

    seen: List[str] = []
    parsed: List[TrajectoryNode] = []
    for index, item in enumerate(_sequence(_required(mapping, "nodes", where), f"{where}.nodes")):
        node = _parse_node(item, index, seen)
        seen.append(node.id)
        parsed.append(node)
    nodes = tuple(parsed)
    if len(nodes) < 2:
        raise SceneError(f"{where}.nodes must list at least two nodes")
    _check_node_sequence(nodes, where)

    style_where = f"{where}.style"
    style = _mapping(_required(mapping, "style", where), style_where)
    _reject_unknown(style, ("color", "width"), style_where)
    return Trajectory(
        nodes=nodes,
        style=TrajectoryStyle(
            color=_vector3(_required(style, "color", style_where), f"{style_where}.color"),
            width=_number(_required(style, "width", style_where), f"{style_where}.width", 1e-9),
        ),
    )


def _check_node_sequence(nodes: Sequence[TrajectoryNode], where: str) -> None:
    """节点的种类要能拼成一条路：起点出发、终点入轨、借力点只有一个。

    这不是形式主义——:func:`primer.scene.layout.trajectory_polyline` 只实现了"一个借力点、
    两端各一条腿"的构造，规格写出别的形状时应当在这里挡住，而不是在几何里算出怪东西。
    """
    if nodes[0].kind != "departure":
        raise SceneError(
            f"{where}.nodes[0].kind must be departure (got {nodes[0].kind}, node {nodes[0].id})"
        )
    if nodes[-1].kind != "orbit_insertion":
        raise SceneError(
            f"{where}.nodes[{len(nodes) - 1}].kind must be orbit_insertion "
            f"(got {nodes[-1].kind}, node {nodes[-1].id})"
        )
    assists = [index for index, node in enumerate(nodes) if node.kind == "gravity_assist"]
    if len(assists) != 1:
        found = ", ".join(nodes[index].id for index in assists) or "none"
        raise SceneError(
            f"{where}.nodes must have exactly one gravity_assist node "
            f"(the path is built as one assist between two straight legs); found: {found}"
        )
    index = assists[0]
    if index in (0, len(nodes) - 1):
        raise SceneError(
            f"{where}.nodes[{index}] is a gravity_assist at the end of the sequence "
            "(it needs an incoming and an outgoing leg)"
        )


def _parse_camera(value: Any) -> CameraSpec:
    where = "camera"
    mapping = _mapping(value, where)
    _reject_unknown(
        mapping,
        ("focal_length_mm", "aperture_f", "elevation_deg", "azimuth_deg", "distance", "target"),
        where,
    )
    return CameraSpec(
        focal_length_mm=_number(
            _required(mapping, "focal_length_mm", where), f"{where}.focal_length_mm", 1e-9
        ),
        aperture_f=_number(_required(mapping, "aperture_f", where), f"{where}.aperture_f", 1e-9),
        elevation_deg=_number(_required(mapping, "elevation_deg", where), f"{where}.elevation_deg"),
        azimuth_deg=_number(_required(mapping, "azimuth_deg", where), f"{where}.azimuth_deg"),
        distance=_number(_required(mapping, "distance", where), f"{where}.distance", 1e-9),
        target=_vector3(_required(mapping, "target", where), f"{where}.target"),
    )


def _parse_assets(value: Any) -> AssetsSpec:
    where = "assets"
    mapping = _mapping(value, where)
    _reject_unknown(mapping, ("source", "license", "credit", "base_url", "files"), where)

    files: List[AssetFile] = []
    seen: List[str] = []
    for index, item in enumerate(_sequence(_required(mapping, "files", where), f"{where}.files")):
        file_where = f"{where}.files[{index}]"
        entry = _mapping(item, file_where)
        _reject_unknown(entry, ("texture", "used_by", "credit_extra"), file_where)
        texture = _text(_required(entry, "texture", file_where), f"{file_where}.texture")
        if texture in seen:
            raise SceneError(f"duplicate asset texture: {texture}")
        seen.append(texture)
        files.append(
            AssetFile(
                texture=texture,
                used_by=_text(_required(entry, "used_by", file_where), f"{file_where}.used_by"),
                credit_extra=_optional_text(entry.get("credit_extra"), f"{file_where}.credit_extra"),
            )
        )
    if not files:
        raise SceneError(f"{where}.files must not be empty")
    return AssetsSpec(
        source=_text(_required(mapping, "source", where), f"{where}.source"),
        license=_text(_required(mapping, "license", where), f"{where}.license"),
        credit=_text(_required(mapping, "credit", where), f"{where}.credit"),
        base_url=_text(_required(mapping, "base_url", where), f"{where}.base_url").rstrip("/"),
        files=tuple(files),
    )


def _parse_background(value: Any) -> BackgroundSpec:
    where = "background"
    mapping = _mapping(value, where)
    _reject_unknown(
        mapping,
        ("starfield", "texture_power", "strength", "field_color", "band_offset_deg"),
        where,
    )
    color = mapping.get("field_color")
    return BackgroundSpec(
        starfield=_text(_required(mapping, "starfield", where), f"{where}.starfield"),
        texture_power=_number(mapping.get("texture_power", 0.3), f"{where}.texture_power", 1e-6),
        strength=_number(mapping.get("strength", 8.0), f"{where}.strength", 0.0),
        field_color=_vector3(color, f"{where}.field_color") if color is not None else (0.0, 0.0, 0.0),
        band_offset_deg=_number(mapping.get("band_offset_deg", 0.0), f"{where}.band_offset_deg"),
    )


# ---------------------------------------------------------------- 交叉引用


def _check_references(spec: SceneSpec) -> None:
    """引用完整性：贴图必须在账本里，轨迹节点必须指着真实天体。

    这两类错误都不会让校验器"看起来失败"——贴图找不到只是渲染成灰球，节点指空只是
    曲线末端浮在空处——所以只能在这里挡下来。
    """
    declared = {entry.texture for entry in spec.assets.files}
    for index, body in enumerate(spec.bodies):
        if body.texture is None:
            continue  # 小天体没有贴图，用的是岩质材质（见 Body.texture 的说明）
        if body.texture not in declared:
            raise SceneError(f"bodies[{index}].texture is not declared in assets.files: {body.texture}")
        if body.clouds is not None and body.clouds.texture not in declared:
            raise SceneError(
                f"bodies[{index}].clouds.texture is not declared in assets.files: {body.clouds.texture}"
            )
    if spec.background.starfield not in declared:
        raise SceneError(
            f"background.starfield is not declared in assets.files: {spec.background.starfield}"
        )

    body_ids = {body.id for body in spec.bodies}
    for index, node in enumerate(spec.trajectory.nodes):
        if node.body is not None and node.body not in body_ids:
            raise SceneError(f"trajectory.nodes[{index}].body is not a known body: {node.body}")
    for index, body in enumerate(spec.bodies):
        anchor = body.display.anchor
        if anchor is None:
            continue
        if anchor.body not in body_ids:
            raise SceneError(
                f"bodies[{index}].display.anchor.body is not a known body: {anchor.body}"
            )
        if anchor.body == body.id:
            raise SceneError(f"bodies[{index}].display.anchor cannot point at the body itself")

    if spec.missions is not None:
        line_ids = {line.id for line in spec.missions.lines}
        site_ids = {site.id for site in spec.missions.sites}
        for index, cluster in enumerate(spec.missions.clusters):
            if cluster.anchor is not None and cluster.anchor.body not in body_ids:
                raise SceneError(
                    f"missions.clusters[{index}].anchor.body is not a known body: "
                    f"{cluster.anchor.body}"
                )
            if cluster.line not in line_ids:
                raise SceneError(
                    f"missions.clusters[{index}].line is not a declared line: {cluster.line}"
                )
            if cluster.site not in site_ids:
                raise SceneError(
                    f"missions.clusters[{index}].site is not a declared site: {cluster.site}"
                )
        _check_mission_coverage(spec.missions)

    unused = sorted(
        entry.texture for entry in spec.assets.files if not _is_used(spec, entry.texture)
    )
    if unused:
        raise SceneError(
            "assets.files declares texture(s) that nothing uses: " + ", ".join(unused)
        )


def _check_mission_coverage(missions: Missions) -> None:
    """声明的层位必须都有任务簇。

    源文档图 5-1 有五个层位，这张图的使命就是把它们摆出来；层位声明在那里却一个簇都没有，
    多半是漏了一层。这条在**载入时**挡，而不是等 check 去报告：漏层是规格错误。
    """
    used = {cluster.site for cluster in missions.clusters}
    missing = [site.id for site in missions.sites if site.id not in used]
    if missing:
        raise SceneError(
            "missions.sites declares site(s) with no cluster: " + ", ".join(missing)
        )


def _is_used(spec: SceneSpec, texture: str) -> bool:
    if texture == spec.background.starfield:
        return True
    for body in spec.bodies:
        if body.texture is not None and body.texture == texture:
            return True
        if body.clouds is not None and body.clouds.texture == texture:
            return True
    return False


# ---------------------------------------------------------------- 入口


def parse_spec(document: Any, path: Path) -> SceneSpec:
    """把已载入的 YAML 文档解析成 :class:`SceneSpec`，任何一处不对就抛错。"""
    where = "spec"
    mapping = _mapping(document, where)
    _reject_unknown(
        mapping,
        ("meta", "render", "layout", "bodies", "bands", "trajectory", "camera", "assets",
         "background", "missions"),
        where,
    )

    kind_of: dict = {}
    bodies: List[Body] = []
    body_items = _sequence(_required(mapping, "bodies", where), f"{where}.bodies")
    for index, item in enumerate(body_items):
        body = _parse_body(item, index, kind_of)
        kind_of[body.id] = body.kind
        bodies.append(body)
    if not bodies:
        raise SceneError(f"{where}.bodies must not be empty")

    band_ids: List[str] = []
    parsed_bands: List[Band] = []
    for index, item in enumerate(_sequence(mapping.get("bands") or [], f"{where}.bands")):
        band = _parse_band(item, index, band_ids)
        band_ids.append(band.id)
        parsed_bands.append(band)

    spec = SceneSpec(
        meta=_parse_meta(_required(mapping, "meta", where)),
        render=_parse_render(_required(mapping, "render", where)),
        layout=_parse_layout(_required(mapping, "layout", where)),
        bodies=tuple(bodies),
        bands=tuple(parsed_bands),
        trajectory=_parse_trajectory(_required(mapping, "trajectory", where)),
        camera=_parse_camera(_required(mapping, "camera", where)),
        assets=_parse_assets(_required(mapping, "assets", where)),
        background=_parse_background(_required(mapping, "background", where)),
        path=path,
        missions=(
            _parse_missions(mapping["missions"]) if mapping.get("missions") is not None else None
        ),
    )
    _check_references(spec)
    return spec


def load_spec(path: Path) -> SceneSpec:
    """读取并校验一份 ``mission_layout.yaml``。

    出错时报的是 **``<文件>: <路径>``**：规格动辄几百行，"字段名拼错了"这类错误只给一个
    路径，人还得自己找是哪个文件；只给文件名又定位不到行里的哪一条。两个都要。
    """
    if not Path(path).is_file():
        raise SceneError(f"scene spec not found: {path}")
    try:
        document = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise SceneError(f"scene spec is not valid YAML: {path}: {exc}") from exc
    try:
        return parse_spec(document, Path(path))
    except SceneError as exc:
        raise SceneError(f"{path}: {exc}") from exc
