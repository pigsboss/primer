# -*- coding: utf-8 -*-
"""构建后的验收：拿现场报告核对命名契约、锚点、贴图账本与成图本身。

这一段是**关卡**，不是"看看有没有报错"。Blender 退出码为零只说明脚本跑完了；跑完的
场景完全可能是另一副样子——名字对不上、行星少了自定义属性、轨迹浮在天体旁边。所以
发射器在场景建完之后写一份机器可读的现场报告（``build/scene_report.json``），这里逐项
核对，每一项给出稳定的英文 ``code`` 与一句中文人读说明（严重度 ``error`` 挡交付，
``warning`` 只提示）。

核对的十件事：

* **命名契约**：七个 collection 一个不多一个不少（``MISSIONS`` 是任务层那一组）；对象名与
  所属 collection 与 :func:`primer.scene.emit.object_contract` 逐项相符（两边同一份表，
  所以不存在"哪边过时了"）；材质集合相同。契约之外只允许相机一个对象。
* **单调次序**（``order_not_monotonic``）：非恒星天体按"到太阳显示位置的距离"排序，必须
  与按 ``real.orbit_au`` 排序得到同一个序列。这张图是任务序列不是比例模型，**次序**是它
  唯一的语义承诺，所以这一条在这里硬核对：规格写得出一份次序颠倒的布局，只有核对能挡。
* **单调尺寸**（``size_not_monotonic``）：按 ``real.radius_km`` 排序与按 ``display.radius``
  排序必须是同一个序列。
* **天体自定义属性**：每个天体对象上的 ``real_orbit_au`` / ``display_orbit_distance`` /
  ``display_radius`` / ``magnification`` / ``real_radius_km`` 必须等于
  :mod:`primer.scene.layout` 算出来的数——这是"发射器有没有真的消费布局模块"的判据，比
  读代码可靠。
* **航迹自己的四条硬约束**（工作说明里"不要插进行星里，从附近走""顺访不用绕弯""木星借力"
  那几句人话）：折线不许穿进任何天体的圆面（``trajectory_penetrates_body``）；顺访节点处
  切线不许拐弯（``incidental_flyby_bends``），也不许离那天体太远
  （``incidental_flyby_too_far``，上界由摆位用的同一个 ``clearance_radii`` 撑出来）；借力
  点的近木点与转角必须就是规格声明的数（``assist_periapsis_too_tight`` /
  ``assist_turn_mismatch``）。四条都量**现场报告里的折线**，不看发射器的意图。
* **任务层**（``spacecraft_overlaps_body`` / ``mission_dot_out_of_frame`` /
  ``mission_line_unknown``）：占位点不许落进任何天体的圆面（留一点净量）；每个点与每条
  任务标签都要在画面里；每条线的名字必须是规格声明过的，声明过的线也不能一个簇都没有。
  层位的覆盖在**载入时**核对——漏一层是规格错误，不是产物问题。
* **成图**：预览 PNG 在、横向纵向尺寸与规格相符、并且**不是全黑**。全黑是一种很容易
  悄悄发生的失败（灯没打上、相机朝向反了、贴图没下来），所以这里解一遍 PNG 像素来判。
* **取景**：相机装不下的天体与航迹节点报出来（``warning``）。用 :mod:`primer.scene.layout`
  的投影算，跟发射器给 Blender 的那组基同源——本图的相机就只能装下 43.2 世界单位，而
  海王星在 65.8，所以它必然出画；这是规格里的构图决定，代码不该替人做主，但也不该让人
  自己发现。
* **贴图账本**：规格声明的每张贴图都在账本里、文件都在、sha256 相符、署名行非空。
* **指纹**：报告里的指纹与当前规格、当前贴图比对——对不上说明产物已经过期。

PNG 解析用标准库（``struct`` + ``zlib``）手写。为这张图引入 Pillow 会破坏"运行期只依赖
PyYAML"这条线，而这里只需要尺寸与亮度两个统计量。
"""

from __future__ import annotations

import json
import math
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence

from ..paths import output_dir_for, relative_to_root
from . import assets as assets_module
from . import emit as emit_module
from .fingerprint import (
    AssetFingerprint,
    Fingerprint,
    compute_fingerprint,
    differences as fingerprint_differences,
)
from .layout import (
    INCIDENTAL_FAR_FACTOR,
    frame_half_height,
    frame_half_width,
    legend_placements,
    mission_placements,
    plan,
    project_extent,
    project_point,
    project_sphere,
)
from .spec import MIN_PERIAPSIS_RADII, SceneSpec

__all__ = [
    "BACKGROUND_MIN_LUMINANCE",
    "BLANK_LIT_FRACTION",
    "ASSIST_TURN_TOLERANCE_DEG",
    "BLANK_MAX_LUMINANCE",
    "BORDER_FRACTION",
    "ERROR",
    "FRAME_LIMIT",
    "INCIDENTAL_BEND_LIMIT_DEG",
    "WARNING",
    "Finding",
    "PNGStats",
    "check_report_lines",
    "check_scene",
    "layout_report_lines",
    "read_png",
    "read_report",
]

ERROR = "error"
WARNING = "warning"
# 判定"全黑"的两条线：最亮像素低于 2%，或亮点占比不足千分之五。
BLANK_MAX_LUMINANCE = 0.02
BLANK_LIT_FRACTION = 0.005
# 取景余量：屏幕上最外的一圈必须离画边缘至少一成（|ndc| <= 0.9）。留余量不是洁癖——
# 图最终要贴进 PPT，紧贴边缘的元素在版式里等于已经被裁掉。
FRAME_LIMIT = 0.90
# 顺访处允许的切线变化：物理上"顺访不机动"就是 0，留 8° 是给折线离散化的余量。
INCIDENTAL_BEND_LIMIT_DEG = 8.0
# 量顺访处的走向差时，节点前后各取几个折线点量切线（窗口不跨过节点，见 _leg_bend）。
INCIDENTAL_BEND_STRIDE = 2
# 借力转角允许的偏差：实测与声明差多少度就算画错了。
ASSIST_TURN_TOLERANCE_DEG = 3.0
# 边带宽度占画面尺寸的比例：8% 的一条框，够宽到不被内容碰到。
BORDER_FRACTION = 0.08
# 背景的下限：深空要看得见（参考图为深蓝星野，不是纯黑）。低于这条线多半是天球增益、
# 幂次整形或深空底色被改回默认，报出来提醒。
BACKGROUND_MIN_LUMINANCE = 0.03
# 亮点阈值（世界单位）。
LIT_LUMINANCE = 0.02
# 数值字段的比对容差：自定义属性在 IDProperty 里以单精度或双精度往返，留一点余量。
PROPERTY_TOLERANCE = 1e-6
# 航天器占位点到天体圆面必须留的净空（世界单位）：一个点可以贴着行星飞，但不能落进去。
# 0.5 世界单位约合一个占位点的直径，读起来仍是"在旁边"而不是"擦上了"。
MISSION_DOT_MARGIN = 0.5

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
_SUPPORTED_DEPTHS = (8, 16)


@dataclass(frozen=True)
class Finding:
    """一条发现：``code`` 英文稳定，``severity`` 决定挡不挡，``message`` 中文人读。"""

    code: str
    severity: str
    message: str

    @property
    def fatal(self) -> bool:
        return self.severity == ERROR


@dataclass(frozen=True)
class PNGStats:
    """一张 PNG 的尺寸与亮度统计（全部像素或抽样，见 :func:`read_png`）。"""

    width: int
    height: int
    bit_depth: int
    color_type: int
    mean_luminance: float
    max_luminance: float
    lit_fraction: float
    sampled: int
    # 画面四周那条边带的中位亮度：构图把内容放在中间，边带基本是背景，用它量"深空有多亮"。
    border_median: float = 0.0

    @property
    def blank(self) -> bool:
        return self.max_luminance < BLANK_MAX_LUMINANCE or self.lit_fraction < BLANK_LIT_FRACTION


# ---------------------------------------------------------------- PNG


def _paeth(left: int, above: int, corner: int) -> int:
    estimate = left + above - corner
    distance_left = abs(estimate - left)
    distance_above = abs(estimate - above)
    distance_corner = abs(estimate - corner)
    if distance_left <= distance_above and distance_left <= distance_corner:
        return left
    if distance_above <= distance_corner:
        return above
    return corner


def _unfilter(raw: bytes, width: int, height: int, channels: int, sample_bytes: int) -> bytes:
    """还原 PNG 的行滤波。每一行的滤波器由该行首字节给出。

    滤波按**整像素**做：左邻是"往前一个像素"，而一个像素是 ``channels * sample_bytes``
    字节。这里一度写成"往前一个采样"，于是 16 位 RGB 的行被解错、成图亮度凭空多出十几倍
    ——滤波器是自适应的，只有存在非零滤波器的 PNG 才会暴露这个错，自造的测试图恰好全是
    零号滤波器。现在按整像素算，并有一条用非零滤波器写的测试图钉住它。
    """
    pixel_bytes = channels * sample_bytes
    stride = width * pixel_bytes
    output = bytearray(stride * height)
    previous = bytearray(stride)
    offset = 0
    for row in range(height):
        filter_type = raw[offset]
        offset += 1
        line = bytearray(raw[offset : offset + stride])
        offset += stride
        if filter_type == 1:
            for index in range(pixel_bytes, stride):
                line[index] = (line[index] + line[index - pixel_bytes]) & 0xFF
        elif filter_type == 2:
            for index in range(stride):
                line[index] = (line[index] + previous[index]) & 0xFF
        elif filter_type == 3:
            for index in range(stride):
                left = line[index - pixel_bytes] if index >= pixel_bytes else 0
                line[index] = (line[index] + ((left + previous[index]) >> 1)) & 0xFF
        elif filter_type == 4:
            for index in range(stride):
                left = line[index - pixel_bytes] if index >= pixel_bytes else 0
                corner = previous[index - pixel_bytes] if index >= pixel_bytes else 0
                line[index] = (line[index] + _paeth(left, previous[index], corner)) & 0xFF
        elif filter_type != 0:
            raise ValueError(f"unsupported PNG filter type: {filter_type}")
        output[row * stride : (row + 1) * stride] = line
        previous = line
    return bytes(output)


def read_png(path: Path, sample_budget: int = 40000) -> PNGStats:
    """读 PNG 的尺寸与亮度统计。

    只做本包需要的一件事：判断"这张图有没有内容"。为此支持 8/16 位、灰度/RGB/RGBA、
    非隔行；调色板与隔行 PNG 直接报错（Blender 不会产出这两种）。
    """
    data = Path(path).read_bytes()
    if not data.startswith(_PNG_SIGNATURE):
        raise ValueError(f"not a PNG file: {path}")
    offset = len(_PNG_SIGNATURE)
    width = height = bit_depth = color_type = interlace = 0
    payload = bytearray()
    while offset + 12 <= len(data):
        (length,) = struct.unpack(">I", data[offset : offset + 4])
        chunk_type = data[offset + 4 : offset + 8]
        body = data[offset + 8 : offset + 8 + length]
        offset += 12 + length
        if chunk_type == b"IHDR":
            width, height, bit_depth, color_type, _compression, _filter, interlace = struct.unpack(
                ">IIBBBBB", body
            )
        elif chunk_type == b"IDAT":
            payload += body
        elif chunk_type == b"IEND":
            break
    if width <= 0 or height <= 0:
        raise ValueError(f"PNG has no usable header: {path}")
    if interlace != 0:
        raise ValueError(f"interlaced PNG is not supported: {path}")
    if bit_depth not in _SUPPORTED_DEPTHS:
        raise ValueError(f"unsupported PNG bit depth: {bit_depth}")
    if color_type not in _CHANNELS or color_type == 3:
        raise ValueError(f"unsupported PNG color type: {color_type}")

    channels = _CHANNELS[color_type]
    sample_bytes = bit_depth // 8
    raw = zlib.decompress(bytes(payload))
    pixels = _unfilter(raw, width, height, channels, sample_bytes)

    stride = width * channels * sample_bytes
    total = width * height
    step = max(1, total // sample_budget)
    highest = 0.0
    total_luminance = 0.0
    lit = 0
    sampled = 0
    border: List[float] = []
    border_x = max(1, int(width * BORDER_FRACTION))
    border_y = max(1, int(height * BORDER_FRACTION))
    for index in range(0, total, step):
        row, column = divmod(index, width)
        base = row * stride + column * channels * sample_bytes
        if sample_bytes == 2:
            values = [
                struct.unpack_from(">H", pixels, base + channel * 2)[0] / 65535.0
                for channel in range(channels)
            ]
        else:
            values = [pixels[base + channel] / 255.0 for channel in range(channels)]
        if color_type == 6 or color_type == 4:
            values = values[:-1]
        luminance = sum(values) / len(values)
        total_luminance += luminance
        highest = max(highest, luminance)
        if luminance > LIT_LUMINANCE:
            lit += 1
        if column < border_x or column >= width - border_x or row < border_y or row >= height - border_y:
            border.append(luminance)
        sampled += 1
    return PNGStats(
        width=width,
        height=height,
        bit_depth=bit_depth,
        color_type=color_type,
        mean_luminance=total_luminance / max(sampled, 1),
        max_luminance=highest,
        lit_fraction=lit / max(sampled, 1),
        sampled=sampled,
        border_median=sorted(border)[len(border) // 2] if border else 0.0,
    )


# ---------------------------------------------------------------- 报告读取


def build_dir(project_root: Path) -> Path:
    return output_dir_for(project_root, "scene") / "build"


def report_path(project_root: Path) -> Path:
    return build_dir(project_root) / emit_module.REPORT_NAME


def script_path(project_root: Path) -> Path:
    return build_dir(project_root) / emit_module.SCRIPT_NAME


def fingerprint_path(project_root: Path) -> Path:
    return build_dir(project_root) / "fingerprint.json"


def read_report(project_root: Path) -> Optional[Mapping[str, object]]:
    """读现场报告；没有就是 ``None``（``check`` 据此报 ``report_missing``）。"""
    path = report_path(project_root)
    if not path.is_file():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return document if isinstance(document, dict) else None


# ---------------------------------------------------------------- 逐项核对


def _objects_by_name(report: Mapping[str, object]) -> Dict[str, Mapping[str, object]]:
    result: Dict[str, Mapping[str, object]] = {}
    for entry in report.get("objects") or []:  # type: ignore[union-attr]
        if isinstance(entry, Mapping) and entry.get("name"):
            result[str(entry["name"])] = entry
    return result


def _near(left: float, right: float, tolerance: float) -> bool:
    return abs(left - right) <= tolerance


def _check_contract(spec: SceneSpec, report: Mapping[str, object]) -> List[Finding]:
    findings: List[Finding] = []
    expected = emit_module.object_contract(spec)
    collected = report.get("collections") or []
    if not isinstance(collected, list):
        collected = []
    for name in sorted(set(emit_module.COLLECTIONS) - set(collected)):
        findings.append(Finding("collection_missing", ERROR, f"缺少 collection：{name}"))
    for name in sorted(set(collected) - set(emit_module.COLLECTIONS)):
        findings.append(Finding("collection_unexpected", ERROR, f"契约之外的 collection：{name}"))

    observed = _objects_by_name(report)
    for name in sorted(set(expected) - set(observed)):
        findings.append(
            Finding("object_missing", ERROR, f"缺少对象：{name}（应在 {expected[name]} 里）")
        )
    for name in sorted(set(observed) - set(expected)):
        if name == emit_module.CAMERA_OBJECT:
            continue
        findings.append(Finding("object_unexpected", ERROR, f"契约之外的对象：{name}"))
    for name in sorted(set(expected) & set(observed)):
        actual = observed[name].get("collection")
        if actual != expected[name]:
            findings.append(
                Finding(
                    "object_collection",
                    ERROR,
                    f"对象 {name} 落在 {actual}，应在 {expected[name]}",
                )
            )

    materials = set(report.get("materials") or [])
    wanted = set(emit_module.material_contract(spec))
    for name in sorted(wanted - materials):
        findings.append(Finding("material_missing", ERROR, f"缺少材质：{name}"))
    for name in sorted(materials - wanted):
        findings.append(Finding("material_unexpected", ERROR, f"契约之外的材质：{name}"))
    return findings


def _check_body_properties(
    spec: SceneSpec, report: Mapping[str, object], placements: Mapping[str, object]
) -> List[Finding]:
    """天体自定义属性必须等于布局算出来的数——这是发射器真的用了 layout 的证据。"""
    findings: List[Finding] = []
    observed = _objects_by_name(report)
    for body in spec.bodies:
        name = emit_module.body_object_name(body)
        entry = observed.get(name)
        if entry is None:
            continue
        custom = entry.get("custom")
        custom = custom if isinstance(custom, Mapping) else {}
        placement = placements.get(body.id)
        expected = {
            "real_orbit_au": getattr(placement, "real_orbit_au", None),
            "display_orbit_distance": getattr(placement, "display_orbit_distance", None),
            "display_radius": getattr(placement, "display_radius", None),
            "magnification": getattr(placement, "magnification", None),
            "real_radius_km": body.real.radius_km,
        }
        for key, wanted in expected.items():
            if key not in custom:
                findings.append(
                    Finding("body_property", ERROR, f"对象 {name} 缺少自定义属性 {key}")
                )
                continue
            actual = custom[key]
            if not isinstance(actual, (int, float)) or not _near(
                float(actual), float(wanted), PROPERTY_TOLERANCE
            ):
                findings.append(
                    Finding(
                        "body_property",
                        ERROR,
                        f"对象 {name} 的 {key} 是 {actual}，布局算出的是 {wanted}",
                    )
                )
    return findings


def _polyline_tangent(
    points: Sequence[Sequence[float]], index: int, stride: int
) -> Optional[float]:
    """折线在 ``index`` 处的切线方位（度）；两头取不到窗口就返回 ``None``。"""
    low, high = index - stride, index + stride
    if low < 0 or high >= len(points):
        return None
    start, finish = points[low], points[high]
    return math.degrees(math.atan2(finish[1] - start[1], finish[0] - start[0]))


def _leg_bend(points: Sequence[Sequence[float]], index: int, stride: int) -> Optional[float]:
    """节点两侧的走向差（度）：节点**前后各取一个窗口**量切线，窗口不跨过节点。

    跨过节点的写法（``(i-s, i+s)`` 与 ``(i+1-s, i+1+s)``）会把拐角本身平均掉：25° 的角
    读出来只剩 6°。取不到窗口（节点太靠折线两端）时把 stride 收到一半，再不行返回 ``None``。
    """
    while stride >= 1:
        if index - 2 * stride >= 0 and index + 2 * stride < len(points):
            incoming = _polyline_tangent(points, index - stride, stride)
            outgoing = _polyline_tangent(points, index + stride, stride)
            if incoming is not None and outgoing is not None:
                return abs((outgoing - incoming + 180.0) % 360.0 - 180.0)
        stride -= 1
    return None


def _check_trajectory(
    spec: SceneSpec, report: Mapping[str, object], placements: Mapping[str, object]
) -> List[Finding]:
    """实迹的四条硬约束：不许穿进天体、顺访处不拐弯也不许太远、借力点要对得上声明。

    这四条正是工作说明里那几句人话的机器版本——"不要插进行星里，从附近走"、"顺访不用绕弯"、
    "木星借力"——所以它们都靠**折线的实测值**判定，不看发射器的意图。"从附近走"是上下两条
    线夹出来的带：下界是 `trajectory_penetrates_body`，上界是 `incidental_flyby_too_far`，
    而后者用的 `clearance_radii` 就是摆位用的那一个。
    """
    findings: List[Finding] = []
    trajectory = report.get("trajectory")
    if not isinstance(trajectory, Mapping):
        findings.append(Finding("trajectory_missing", ERROR, "现场报告里没有轨迹段"))
        return findings
    points = [point for point in (trajectory.get("points") or []) if isinstance(point, list)]
    if len(points) < 2:
        findings.append(Finding("trajectory_missing", ERROR, "轨迹折线少于两个点"))
        return findings

    # 1) 不许穿进任何天体的圆面
    for body in spec.bodies:
        placement = placements[body.id]
        centre = getattr(placement, "position")
        clearance = min(
            math.sqrt(sum((float(point[axis]) - centre[axis]) ** 2 for axis in range(3)))
            for point in points
        )
        if clearance < body.display.radius:
            findings.append(
                Finding(
                    "trajectory_penetrates_body",
                    ERROR,
                    f"航迹穿进了 {body.id} 的圆面：轨迹到圆心的最近距离 {clearance:.2f}，"
                    f"而显示半径是 {body.display.radius:.2f}"
                    f"（净空 {clearance - body.display.radius:+.2f}）",
                )
            )

    # 2) 顺访处不许拐弯，也不许离得太远——两条一起把"从附近走"夹成一条带
    nodes = trajectory.get("nodes") or []
    for node in nodes:
        if not isinstance(node, Mapping) or node.get("kind") != "incidental":
            continue
        declared = next(
            (item for item in spec.trajectory.nodes if item.id == node.get("id")), None
        )
        body = spec.body(declared.body) if declared is not None else None
        if body is not None:
            centre = getattr(placements[body.id], "position")
            clearance = min(
                math.sqrt(sum((float(point[axis]) - centre[axis]) ** 2 for axis in range(3)))
                for point in points
            )
            # 上限由**摆位用过的同一个 clearance_radii** 撑出来：位置与判据因此不会各说各话。
            limit = (declared.clearance_radii or 0.0) * body.display.radius * INCIDENTAL_FAR_FACTOR
            if clearance > limit:
                findings.append(
                    Finding(
                        "incidental_flyby_too_far",
                        ERROR,
                        f"顺访节点 {node.get('id')} 处航迹离 {body.id} 中心 {clearance:.2f}"
                        f"（{clearance / body.display.radius:.2f} 个半径）"
                        f"（上限 {limit:.2f} = {declared.clearance_radii:.2f} × "
                        f"{body.display.radius:.2f} × {INCIDENTAL_FAR_FACTOR:.1f}）："
                        "顺访要从附近走，不是远远掠过",
                    )
                )
        index = node.get("point_index")
        if not isinstance(index, int):
            continue
        bend = _leg_bend(points, index, INCIDENTAL_BEND_STRIDE)
        if bend is None:
            continue
        if bend > INCIDENTAL_BEND_LIMIT_DEG:
            findings.append(
                Finding(
                    "incidental_flyby_bends",
                    ERROR,
                    f"顺访节点 {node.get('id')} 处航迹拐了 {bend:.1f}°"
                    f"（上限 {INCIDENTAL_BEND_LIMIT_DEG:.1f}°）：顺访是沿途顺访，不该机动",
                )
            )

    # 3) 借力点：近木点与转角都要与声明相符
    assist = trajectory.get("assist")
    declared = next(
        (node for node in spec.trajectory.nodes if node.kind == "gravity_assist"), None
    )
    if isinstance(assist, Mapping) and declared is not None:
        measured = float(assist.get("periapsis", 0.0))
        body = spec.body(str(assist.get("body_id", declared.body)))
        wanted = (declared.periapsis_radii or 0.0) * body.display.radius
        # 相对容差：采样出来的近木点与解析值只差浮点末位，别让它变成"差 1e-15 就报错"。
        tolerance = max(wanted, body.display.radius * MIN_PERIAPSIS_RADII) * 1e-4
        if measured < wanted - tolerance or measured < body.display.radius * MIN_PERIAPSIS_RADII - tolerance:
            findings.append(
                Finding(
                    "assist_periapsis_too_tight",
                    ERROR,
                    f"借力节点 {assist.get('node_id')} 的近木点只有 {measured:.2f}"
                    f"（{measured / body.display.radius:.2f} 个半径）：规格声明的是 "
                    f"{declared.periapsis_radii:.2f} 个半径（{wanted:.2f}），下限 "
                    f"{MIN_PERIAPSIS_RADII:.1f}",
                )
            )
        turn = float(assist.get("turn_deg", 0.0))
        if abs(turn - (declared.turn_deg or 0.0)) > ASSIST_TURN_TOLERANCE_DEG:
            findings.append(
                Finding(
                    "assist_turn_mismatch",
                    ERROR,
                    f"借力节点 {assist.get('node_id')} 实测转角 {turn:.1f}°，"
                    f"规格声明 {declared.turn_deg:.1f}°"
                    f"（容差 {ASSIST_TURN_TOLERANCE_DEG:.1f}°）；转角是这张图的信息量，"
                    "画出来必须就是声明的那个数",
                )
            )
    return findings


def _check_missions(
    spec: SceneSpec, report: Mapping[str, object], placements: Mapping[str, object]
) -> List[Finding]:
    """任务层的两条硬约束：点位不许落进天体的圆面；每条线的名字必须是声明过的。

    "从附近走"的反面在这里：占位点是航天器，它当然可以贴着行星飞，但不能**落进**行星里；
    线名则是图例的凭据——图上画了三种颜色，报告里就必须说得出每种颜色是哪条线，而且
    声明过的线不能一个簇都没有（那样图例就多出一行无主的颜色）。层位的覆盖在**载入时**
    核对（``_check_mission_coverage``），因为漏层是规格错误，不是产物问题。
    """
    findings: List[Finding] = []
    missions = report.get("missions")
    if not isinstance(missions, Mapping):
        return findings
    clusters = [item for item in (missions.get("clusters") or []) if isinstance(item, Mapping)]

    for cluster in clusters:
        for dot in cluster.get("dots") or []:
            if not isinstance(dot, Mapping):
                continue
            position = dot.get("position")
            if not isinstance(position, list) or len(position) != 3:
                continue
            for body in spec.bodies:
                centre = getattr(placements[body.id], "position")
                distance = math.sqrt(
                    sum((float(position[axis]) - centre[axis]) ** 2 for axis in range(3))
                )
                if distance < body.display.radius + MISSION_DOT_MARGIN:
                    findings.append(
                        Finding(
                            "spacecraft_overlaps_body",
                            ERROR,
                            f"占位点 {dot.get('object_name')} 落在 {body.id} 的圆面里："
                            f"到圆心 {distance:.2f}，而显示半径 {body.display.radius:.2f}"
                            f"（要留 {MISSION_DOT_MARGIN:.2f} 的余量）",
                        )
                    )

    declared = {line.id for line in (spec.missions.lines if spec.missions else ())}
    used = {str(cluster.get("line")) for cluster in clusters}
    unknown = sorted(used - declared)
    if unknown:
        findings.append(
            Finding(
                "mission_line_unknown",
                ERROR,
                "任务线没在规格里声明过：" + ", ".join(unknown)
                + f"（声明的是 {', '.join(sorted(declared)) or '(无)'}）",
            )
        )
    unused = sorted(declared - used)
    if unused:
        findings.append(
            Finding(
                "mission_line_unknown",
                ERROR,
                "规格声明的任务线一个簇都没有：" + ", ".join(unused)
                + "（图例会出现一行无主的颜色）",
            )
        )
    return findings


def _check_preview(spec: SceneSpec, report: Mapping[str, object], project_root: Path) -> List[Finding]:
    findings: List[Finding] = []
    render = report.get("render")
    render = render if isinstance(render, Mapping) else {}
    mode = spec.render.mode(report.get("mode") == "final")
    for key, wanted in (("width", mode.width), ("height", mode.height)):
        if render.get(key) != wanted:
            findings.append(
                Finding(
                    "render_dimensions",
                    ERROR,
                    f"报告里的渲染 {key} 是 {render.get(key)}，规格要求 {wanted}",
                )
            )
    path = Path(str(render.get("path", "")))
    if not path.is_file():
        findings.append(Finding("preview_missing", ERROR, f"成图不存在：{path}"))
        return findings
    try:
        stats = read_png(path)
    except (OSError, ValueError, zlib.error) as exc:
        findings.append(Finding("preview_unreadable", ERROR, f"成图读不出来：{path}: {exc}"))
        return findings
    if stats.width != mode.width or stats.height != mode.height:
        findings.append(
            Finding(
                "preview_dimensions",
                ERROR,
                f"成图是 {stats.width}x{stats.height}，规格要求 {mode.width}x{mode.height}",
            )
        )
    if stats.blank:
        findings.append(
            Finding(
                "preview_blank",
                ERROR,
                f"成图近乎全黑（最亮 {stats.max_luminance:.3f}，亮点占比 "
                f"{stats.lit_fraction:.4f}）：灯光、相机或贴图有一处没生效",
            )
        )
    elif stats.border_median < BACKGROUND_MIN_LUMINANCE:
        findings.append(
            Finding(
                "background_too_dark",
                WARNING,
                f"画面四周边带的中位亮度只有 {stats.border_median:.4f}"
                f"（深空应不低于 {BACKGROUND_MIN_LUMINANCE:.2f}）：天球的增益、幂次整形"
                "或深空底色被改回默认了？参考图里的深空是深蓝星野，不是纯黑",
            )
        )
    return findings


def _framing_overflow(
    point: Tuple[float, float, float], camera, width: int, height: int
) -> Optional[float]:
    """一个点到画面中心的最大归一化偏移（``1.0`` 就是压到画边缘）；在相机背后返回 ``None``。"""
    projected = project_point(point, camera, width, height)
    if projected is None:
        return None
    return max(abs(projected[0]), abs(projected[1]))


def _framing_offsets(
    spec: SceneSpec, report: Mapping[str, object], width: int, height: int
) -> List[Tuple[str, float]]:
    """每个对象离画面中心的最大归一化偏移，按大到小排；``inf`` 表示整个在相机背后。

    用**实测几何**而不是球心、也不用包围盒角：标签是朝向相机的文字（约束生效后才有
    厚度），航迹是有粗细的管、星带是散开的碎石——这些都不是一个点；而它们的包围盒角又
    落在空处（斜拉一条线的盒子角，图上什么也没有），拿盒子判会报出画面里不存在的东西。
    报告里的 ``probes`` 是网格顶点（每轴极值 + 等距抽样），所以量的是几何真正占的位置。
    """
    offsets: List[Tuple[str, float]] = []
    for entry in report.get("objects") or []:
        if not isinstance(entry, Mapping):
            continue
        name = str(entry.get("name", ""))
        collection = str(entry.get("collection", ""))
        if collection == emit_module.BACKGROUND_COLLECTION or name == emit_module.STARFIELD_OBJECT:
            continue  # 天球包住整个场景，它在画面里是必然的，不该报
        probes = entry.get("probes")
        if not isinstance(probes, list) or not probes:
            continue
        if _mean_behind_camera(spec, probes, width, height):
            # 整个对象都在相机背后：一个点都投不出来，绝不能因为"投不出"就当它合格。
            offsets.append((name, float("inf")))
            continue
        worst = 0.0
        for probe in probes:
            if not isinstance(probe, list) or len(probe) != 3:
                continue
            offset = _framing_overflow(
                (float(probe[0]), float(probe[1]), float(probe[2])), spec.camera, width, height
            )
            if offset is not None:
                worst = max(worst, offset)
        offsets.append((name, worst))
    return sorted(offsets, key=lambda item: (-item[1], item[0]))


def _mean_behind_camera(
    spec: SceneSpec, probes: Sequence[object], width: int, height: int
) -> bool:
    """取样点的平均位置是否落在相机背后——整个对象都看不见的那种情形。"""
    points = [probe for probe in probes if isinstance(probe, list) and len(probe) == 3]
    if not points:
        return False
    centre = tuple(
        sum(float(point[axis]) for point in points) / len(points) for axis in range(3)  # type: ignore[index]
    )
    return project_point(centre, spec.camera, width, height) is None


def _framing_code(name: str) -> str:
    if name.startswith("BAND_"):
        return "band_out_of_frame"
    if name.startswith(emit_module.TRAJECTORY_PREFIX):
        return "node_out_of_frame"
    if (
        name.startswith(emit_module.MISSION_PREFIX)
        or name.startswith("LABEL_mission_")
        or name.startswith("LABEL_legend_")
    ):
        # 任务层自成一组：点位、簇标签、图例都归这一码——它们是一起进场的，一起报更好定位。
        return "mission_dot_out_of_frame"
    if name.startswith("LABEL_"):
        return "label_out_of_frame"
    return "body_out_of_frame"


def _check_framing(
    spec: SceneSpec, report: Mapping[str, object], mode: object
) -> List[Finding]:
    """画面装不下的东西报出来——"天体或标签出画"是构图问题，但不该由人肉眼发现。

    判据基于 :mod:`primer.scene.layout` 的投影（与发射器给 Blender 的那组相机基同源），
    输入是现场报告里**求值后**的几何取样点，所以量的是"几何真的伸到哪儿"，不是估算。余量按
    :data:`FRAME_LIMIT` 取：屏幕上最外的那一圈必须离边缘至少有一成。这是警告，不挡交付：
    构图是人的决定（改焦距、改距离、改压缩指数都在规格里）。
    """
    width = getattr(mode, "width", spec.render.preview.width)
    height = getattr(mode, "height", spec.render.preview.height)
    findings: List[Finding] = []
    offsets = _framing_offsets(spec, report, width, height)
    offenders = [entry for entry in offsets if entry[1] > FRAME_LIMIT]
    for name, worst in offenders:
        if worst == float("inf"):
            findings.append(
                Finding(
                    _framing_code(name),
                    WARNING,
                    f"{name} 整个落在相机背后（画面坐标无从计算）：相机的方位角／仰角"
                    "需要重新对一遍",
                )
            )
            continue
        findings.append(
            Finding(
                _framing_code(name),
                WARNING,
                f"{name} 伸到了画面的 {worst:.2f}（余量要求不超过 {FRAME_LIMIT:.2f}，"
                f"否则离画边缘不足 {int(round((1.0 - FRAME_LIMIT) * 100))}%）",
            )
        )

    # 节点坐标本身也报一次：曲线包住节点，但"哪个节点跑到画外"比"这条曲线出画"更好定位。
    # 曲线已经报过就不再逐点重复。
    curve_reported = any(name.startswith(emit_module.TRAJECTORY_PREFIX) for name, _ in offenders)
    if not curve_reported:
        for node in plan(spec)["nodes"]:  # type: ignore[union-attr]
            offset = _framing_overflow(node.position, spec.camera, width, height)
            if offset is None or offset <= FRAME_LIMIT:
                continue
            findings.append(
                Finding(
                    "node_out_of_frame",
                    WARNING,
                    f"航迹节点 {node.node_id} 落在画面的 {offset:.2f}（上限 {FRAME_LIMIT:.2f}）",
                )
            )
    return findings


def _order_by(values: Sequence[Tuple[str, float]]) -> Tuple[str, ...]:
    """按值排序后的 id 序列（同值的按 id 兜底，保证结果稳定可复现）。"""
    return tuple(identifier for identifier, _ in sorted(values, key=lambda item: (item[1], item[0])))


def _describe(values: Sequence[Tuple[str, float]]) -> str:
    return " < ".join(identifier for identifier, _ in sorted(values, key=lambda item: (item[1], item[0])))


def _check_monotonic_order(spec: SceneSpec, placements: Mapping[str, object]) -> List[Finding]:
    """显示的远近次序必须与真实的远近次序一致——这张图唯一的语义承诺。

    卫星不参加：海卫一的日心距离与海王星相同，并进这条序列必然并列或颠倒，那是**定义**
    上的平局，不是构图错误。
    """
    displayed: List[Tuple[str, float]] = []
    real: List[Tuple[str, float]] = []
    for body in spec.bodies:
        if body.kind == "star":
            continue  # 恒星是"显示远近"的原点，不参与排序
        if body.satellite:
            continue  # 卫星绕母体转，不在"到太阳的距离"这条线上
        displayed.append((body.id, float(getattr(placements[body.id], "display_orbit_distance"))))
        real.append((body.id, body.real.orbit_au))
    if _order_by(displayed) == _order_by(real):
        return []
    return [
        Finding(
            "order_not_monotonic",
            ERROR,
            "天体的显示远近次序与真实轨道次序不一致："
            f"按真实轨道半径排 → {_describe(real)}；"
            f"按显示距离排 → {_describe(displayed)}。"
            "这张图是任务序列不是比例模型，距离不必成比例，但次序必须守序",
        )
    ]


def _check_monotonic_size(spec: SceneSpec) -> List[Finding]:
    """显示的大小次序必须与真实半径次序一致（恒星一并参与，卫星除外）。

    卫星的显示半径是"看得见"决定的（海卫一比半人马天体大得多，但在这里不能画得比它大，
    否则贴着海王星的那一小块会喧宾夺主），所以它不进这条序列。
    """
    displayed = [(body.id, body.display.radius) for body in spec.bodies if not body.satellite]
    real = [(body.id, body.real.radius_km) for body in spec.bodies if not body.satellite]
    if _order_by(displayed) == _order_by(real):
        return []
    return [
        Finding(
            "size_not_monotonic",
            ERROR,
            "天体的显示大小次序与真实半径次序不一致："
            f"按真实半径排 → {_describe(real) or '(无)'}；"
            f"按显示半径排 → {_describe(displayed) or '(无)'}",
        )
    ]


def _check_assets(spec: SceneSpec, project_root: Path) -> List[Finding]:
    findings: List[Finding] = []
    for problem in assets_module.verify_ledger(spec, project_root):
        findings.append(Finding("credits_incomplete", ERROR, f"贴图出处不齐：{problem}"))
    ledger = assets_module.read_ledger(assets_module.ledger_path(project_root))
    for entry in spec.assets.files:
        recorded = ledger.get(entry.texture)
        if recorded is not None and recorded.source_file and recorded.source_file != entry.texture:
            findings.append(
                Finding(
                    "asset_resolution_fallback",
                    WARNING,
                    f"{entry.texture} 在上游不存在，实际取的是 {recorded.source_file}"
                    f"（{recorded.url}），已在账本里记下",
                )
            )
    for path in assets_module.orphan_textures(spec, project_root):
        findings.append(
            Finding(
                "orphan_texture",
                WARNING,
                f"贴图目录里有规格未声明的文件：{path.name}"
                f"（{path.stat().st_size} 字节）；运行 fetch 会把它清掉",
            )
        )
    return findings


def _check_fingerprint(
    spec: SceneSpec, report: Mapping[str, object], project_root: Path
) -> List[Finding]:
    """报告里的指纹 vs 当前磁盘状态：规格变了是 error，只有贴图变了是 warning。"""
    findings: List[Finding] = []
    stored = report.get("fingerprint")
    stored = stored if isinstance(stored, Mapping) else {}
    recorded = Fingerprint(
        spec=str(stored.get("spec", "")),
        spec_sha256=str(stored.get("spec_sha256", "")),
        assets=tuple(
            AssetFingerprint(
                texture=str(item.get("texture", "")),
                path=str(item.get("path", "")),
                sha256=str(item.get("sha256", "")),
            )
            for item in (stored.get("assets") or [])
            if isinstance(item, Mapping)
        ),
        digest=str(stored.get("digest", "")),
    )
    if not recorded.digest:
        findings.append(
            Finding("fingerprint_missing", ERROR, "现场报告里没有指纹，无法判断产物是否过期")
        )
        return findings
    current = compute_fingerprint(spec, project_root)
    for difference in fingerprint_differences(recorded, current):
        findings.append(
            Finding(
                "fingerprint_stale",
                ERROR if difference.severity == ERROR else WARNING,
                difference.message,
            )
        )
    return findings


def check_scene(spec: SceneSpec, project_root: Path) -> List[Finding]:
    """核对一份已构建的场景。只读，不写任何东西。"""
    project_root = Path(project_root)
    report = read_report(project_root)
    findings: List[Finding] = []
    if report is None:
        findings.append(
            Finding(
                "report_missing",
                ERROR,
                f"没有现场报告 {relative_to_root(report_path(project_root), project_root)}；"
                "先运行 build",
            )
        )
        return findings

    resolved = plan(spec)
    placements = resolved["bodies_by_id"]
    assert isinstance(placements, Mapping)
    findings.extend(_check_monotonic_order(spec, placements))
    findings.extend(_check_monotonic_size(spec))
    findings.extend(_check_contract(spec, report))
    findings.extend(_check_body_properties(spec, report, placements))
    findings.extend(_check_trajectory(spec, report, placements))
    findings.extend(_check_missions(spec, report, placements))
    findings.extend(_check_preview(spec, report, project_root))
    findings.extend(_check_framing(spec, report, spec.render.mode(report.get("mode") == "final")))
    findings.extend(_check_assets(spec, project_root))
    findings.extend(_check_fingerprint(spec, report, project_root))

    blend = Path(str(report.get("blend_path", "")))
    if not blend.is_file():
        findings.append(
            Finding("blend_missing", ERROR, f"来源文件不存在：{blend}")
        )
    return findings


# ---------------------------------------------------------------- 报告文本


def check_report_lines(
    spec: SceneSpec, findings: Sequence[Finding], project_root: Path
) -> List[str]:
    """``check`` 的人读报告（中文）。"""
    lines = [f"场景验收：{spec.meta.title}（{spec.meta.name}）"]
    lines.append(f"  工程根：{Path(project_root)}")
    lines.append(f"  规格：{relative_to_root(spec.path, project_root)}")
    lines.append(
        f"  对象 {len(emit_module.object_contract(spec))} 个 / 材质 "
        f"{len(emit_module.material_contract(spec))} 个 / 贴图 {len(spec.assets.files)} 张"
    )
    report = read_report(project_root)
    if report is not None:
        render = report.get("render")
        render = render if isinstance(render, Mapping) else {}
        path = Path(str(render.get("path", "")))
        if path.is_file():
            lines.append(
                f"  成图：{relative_to_root(path, project_root)}"
                f"（{path.stat().st_size} 字节，{render.get('width')}x{render.get('height')}，"
                f"{render.get('samples')} 采样）"
            )
        lines.append(f"  Blender：{report.get('blender')}，指纹 {report.get('fingerprint', {}).get('digest', '(无)')}")
        mode = spec.render.mode(report.get("mode") == "final")
        offsets = _framing_offsets(spec, report, mode.width, mode.height)
        lines.append(
            f"  取景：画面半宽 {frame_half_width(spec.camera, mode.width, mode.height):.1f}"
            f" × 半高 {frame_half_height(spec.camera, mode.width, mode.height):.1f} 世界单位；"
            f"余量上限 {FRAME_LIMIT:.2f}"
        )
        if offsets:
            name, worst = offsets[0]
            shown = "在相机背后" if worst == float("inf") else f"{worst:.2f}"
            lines.append(f"        最外一环 {shown}（{name}）")
    errors = [finding for finding in findings if finding.fatal]
    warnings = [finding for finding in findings if not finding.fatal]
    if not errors and not warnings:
        lines.append("  结论：全部通过")
        return lines
    for finding in errors:
        lines.append(f"  [error] {finding.code}: {finding.message}")
    for finding in warnings:
        lines.append(f"  [warning] {finding.code}: {finding.message}")
    lines.append(f"  结论：{len(errors)} 项 error，{len(warnings)} 项 warning")
    return lines


def layout_report_lines(spec: SceneSpec) -> List[str]:
    """``report`` 的人读输出：把算好的布局数字摊开，供人核对图的尺度语言。"""
    resolved = plan(spec)
    lines = [f"布局参数：{spec.meta.title}（{spec.meta.name}）"]
    lines.append(
        f"  布局规则：{spec.layout.kind}（天体按显示位置排开，距离只保证次序；"
        f"放大倍数以 {spec.layout.nominal_units_per_au} 世界单位/AU 为基准折算）"
    )
    lines.append("  天体：")
    lines.append("    id        x        y      半径(世界)  放大倍数   真实轨道(AU)  显示距离")
    for placement in resolved["bodies"]:  # type: ignore[union-attr]
        lines.append(
            f"    {placement.body_id:<9} {placement.position[0]:>7.2f} {placement.position[1]:>7.2f} "
            f"{placement.display_radius:>10.2f} {placement.magnification:>10.1f} "
            f"{placement.real_orbit_au:>12.3f} {placement.display_orbit_distance:>9.2f}"
        )
    for band, placement in zip(spec.bands, resolved["bands"]):  # type: ignore[arg-type]
        lines.append(
            f"    {band.id:<9} 盒子中心 ({placement.center[0]:.2f}, {placement.center[1]:.2f})"
            f" 半长 {placement.half_x:.2f} x {placement.half_y:.2f} x {placement.half_z:.2f}"
            f"（{band.count} 块）"
        )
    lines.append("  航迹节点：")
    for node, spec_node in zip(resolved["nodes"], spec.trajectory.nodes):  # type: ignore[arg-type]
        lines.append(
            f"    {node.node_id:<18} ({spec_node.kind:<15}) "
            f"x={node.position[0]:>8.3f} y={node.position[1]:>8.3f} z={node.position[2]:>6.3f}"
        )
    mode = spec.render.mode(False)
    mission_items = mission_placements(spec)
    if mission_items:
        missions = spec.missions
        assert missions is not None
        lines.append(
            f"  任务层：{len(missions.sites)} 个层位，{len(missions.clusters)} 簇，"
            f"{missions.dot_count} 个占位点（图例 {len(legend_placements(spec))} 行）"
        )
        for line in missions.lines:
            mine = [item for item in mission_items if item.line == line.id]
            dots = sum(len(item.dots) for item in mine)
            lines.append(
                f"    {line.name_cn:<8} {len(mine)} 簇 / {dots} 点："
                + "、".join(f"{item.name_cn}({len(item.dots)})" for item in mine)
            )
    lines.append(
        f"  预览：{mode.width}x{mode.height} {mode.samples} 采样；"
        f"正式：{spec.render.final.width}x{spec.render.final.height} {spec.render.final.samples} 采样"
    )
    lines.append(f"  相机：{spec.camera.focal_length_mm}mm f/{spec.camera.aperture_f}，"
                 f"仰角 {spec.camera.elevation_deg}°，方位 {spec.camera.azimuth_deg}°，"
                 f"距离 {spec.camera.distance}")
    lines.append(f"  取景（按规格预算，不依赖任何渲染产物；余量上限 {FRAME_LIMIT:.2f}）：")
    lines.append(
        f"    画面在目标平面上的半宽 {frame_half_width(spec.camera, mode.width, mode.height):.1f}"
        f" × 半高 {frame_half_height(spec.camera, mode.width, mode.height):.1f} 世界单位"
    )
    worst = (0.0, "")
    for placement in resolved["bodies"]:  # type: ignore[union-attr]
        body = spec.body(placement.body_id)
        radius = placement.display_radius
        for shell in (body.atmosphere, body.clouds):
            if shell is not None:
                radius = max(radius, placement.display_radius * shell.scale)
        projection = project_sphere(
            placement.position, radius, spec.camera, mode.width, mode.height
        )
        if projection is None:
            lines.append(f"    {placement.body_id:<9} 在相机背后")
            continue
        value = max(
            abs(projection.ndc[0]) + projection.radius[0],
            abs(projection.ndc[1]) + projection.radius[1],
        )
        worst = max(worst, (value, placement.body_id))
        lines.append(
            f"    {placement.body_id:<9} 圆心 ({projection.ndc[0]:+.3f}, {projection.ndc[1]:+.3f})"
            f"  含半径最外 {value:.3f}"
        )
    for item in emit_module.label_placements(spec):
        projection = project_extent(
            item.position,
            item.extent,
            0.6 * item.size,
            spec.camera,
            mode.width,
            mode.height,
        )
        if projection is None:
            continue
        value = max(
            abs(projection.ndc[0]) + projection.radius[0],
            abs(projection.ndc[1]) + projection.radius[1],
        )
        worst = max(worst, (value, item.object_name))
        lines.append(
            f"    标签 {item.object_name:<24} 锚点 ({projection.ndc[0]:+.3f}, {projection.ndc[1]:+.3f})"
            f"  含文字最外 {value:.3f}（半宽 {item.extent:.1f} 世界单位）"
        )
    verdict = "在余量内" if worst[0] <= FRAME_LIMIT else "**已超出余量**"
    lines.append(
        f"    最外一环 {worst[0]:.3f}（{worst[1]}）—— {verdict}；"
        "曲线管与文字块的精确判据由 check 用现场报告里的取样点给出（build 之后）"
    )
    return lines
