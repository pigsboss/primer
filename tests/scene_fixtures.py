# -*- coding: utf-8 -*-
"""``primer-scene`` 的共享夹具：一份小而全的场景规格，加一套"假现场"的搭台工具。

夹具故意小到能一眼读完——太阳 + 地球（大气 + 云）+ 火星，一条从地球到火星、中间借力木星
的航迹——但**结构齐全**：两种渲染档位、色管、贴图账本、背景天球、碎屑带一样不少。真规格
（``mission_layout.yaml``）该走的分支，这里都有便宜的替身。

三件搭台工具：

* :func:`write_spec` —— 把 :data:`SPEC_YAML` 落到 ``_primer/scene/`` 下；
* :func:`fake_report` —— 造一份 **emitter 面（现场报告）** 的样子，用来把验收关卡单向地
  测出来（关卡读报告、不看场景，所以不需要真的开 Blender）；
* :func:`write_png` —— 手写一张 PNG（标准库 ``zlib`` + ``struct``），用来测成图的尺寸与
  亮度判据；全黑与有内容两种都能出。
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import struct
import zlib
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

import yaml

from primer.scene import emit
from primer.scene.assets import (
    AssetEntry,
    ledger_path,
    read_ledger,
    texture_path,
)
from primer.scene.layout import legend_placements, mission_placements, plan, trajectory_polyline
from primer.scene.spec import SceneSpec, load_spec, parse_spec

SPEC_NAME = "mission_layout.yaml"

SPEC_YAML = """\
meta:
  name: fixture_scene
  title: 夹具场景
  caption: 仅用于测试，不是真实任务布局；天体按单调序列排开

render:
  aspect_ratio: [16, 9]
  preview:
    width: 854
    height: 480
    samples: 8
    denoise: true
  final:
    width: 3840
    height: 2160
    samples: 512
    denoise: true
    exr: true
  color_management:
    view_transform: AgX
    look: AgX - Medium High Contrast
    display_device: sRGB
  output:
    preview_dir: renders/previews
    final_dir: renders/final

layout:
  kind: monotonic_sequence
  nominal_units_per_au: 12.0

bodies:
  - id: sun
    name_cn: 太阳
    kind: star
    real: {radius_km: 696000, orbit_au: 0.0}
    display: {radius: 3.0, x: -24.0, y: 0.0}
    material:
      type: emission
      strength: 40.0
      color: [1.0, 0.97, 0.93]
    texture: 4k_sun.jpg

  - id: venus
    name_cn: 金星
    kind: planet
    real: {radius_km: 6052, orbit_au: 0.723}
    display: {radius: 1.2, x: -16.0, y: 2.0}
    material: {roughness: 0.85, normal_strength: 0.15}
    atmosphere: {mode: rim, scale: 1.04, density: 0.060, color: [1.0, 0.85, 0.60]}
    texture: 4k_venus_atmosphere.jpg

  - id: earth
    name_cn: 地球
    kind: planet
    real: {radius_km: 6371, orbit_au: 1.000}
    display: {radius: 1.3, x: -6.0, y: -3.0}
    material: {roughness: 0.75, normal_strength: 0.20}
    atmosphere: {mode: rim, scale: 1.03, density: 0.020, color: [0.35, 0.55, 1.0]}
    texture: 4k_earth_daymap.jpg
    clouds:
      texture: 4k_earth_clouds.jpg
      scale: 1.012
      opacity: 0.85

  - id: mainbelt
    name_cn: 主带小行星
    kind: minor
    real: {radius_km: 60, orbit_au: 2.700}
    # y 只是标称值：真正的位置由路径按节点的 clearance_radii 摆出来（见 trajectory 段）。
    display: {radius: 0.35, x: 2.0, y: -2.0}
    material:
      type: rock
      roughness: 0.95
      color: [0.38, 0.35, 0.32]

  - id: jupiter
    name_cn: 木星
    kind: planet
    real: {radius_km: 69911, orbit_au: 5.203}
    display: {radius: 2.2, x: 10.0, y: 5.0}
    material: {roughness: 0.80, normal_strength: 0.10}
    texture: 4k_jupiter.jpg

  - id: centaur
    name_cn: 半人马天体
    kind: minor
    real: {radius_km: 300, orbit_au: 14.0}
    display: {radius: 0.5, x: 20.0, y: -3.0}
    material:
      type: rock
      roughness: 0.95
      color: [0.42, 0.38, 0.34]

  - id: neptune
    name_cn: 海王星
    kind: planet
    real: {radius_km: 24622, orbit_au: 30.070}
    display: {radius: 1.6, x: 30.0, y: -6.0}
    material: {roughness: 0.80, normal_strength: 0.12}
    texture: 2k_neptune.jpg

bands:
  - id: main_belt
    name_cn: 主带
    display: {x_center: 0.0, y: 0.0, x_half_length: 4.0, y_half_width: 3.0, thickness: 1.5}
    count: 40
    size_range: [0.05, 0.16]
    color: [0.62, 0.58, 0.50]

trajectory:
  # 与真规格同构：出发 → 顺访（入腿）→ 借力 → 顺访（出腿）→ 捕获。顺访天体与捕获天体的
  # 位置由路径按 clearance_radii 摆出来，所以那三条腿的形状就是这张夹具要练的东西。
  nodes:
    - {id: earth_departure, kind: departure, body: earth, offset_radii: 1.8, label: 自地球出发}
    - {id: mainbelt_flyby, kind: incidental, body: mainbelt, clearance_radii: 2.5, label: 主带飞掠}
    - {id: jupiter_assist, kind: gravity_assist, body: jupiter, periapsis_radii: 1.8, turn_deg: 45.0, label: 木星借力}
    - {id: centaur_flyby, kind: incidental, body: centaur, clearance_radii: 2.5, label: 半人马天体飞掠}
    - {id: neptune_arrival, kind: orbit_insertion, body: neptune, orbit_radii: 3.2, clearance_radii: 2.8, polar: true, label: 抵达海王星}
  style:
    color: [1.0, 0.62, 0.20]
    width: 0.10

camera:
  focal_length_mm: 40
  aperture_f: 2.8
  elevation_deg: 38.0
  azimuth_deg: -90.0
  distance: 100.0
  target: [0.0, 0.0, 0.0]

assets:
  source: Fixture Studio
  license: CC BY 4.0
  credit: Textures by Fixture Studio, CC BY 4.0
  base_url: https://example.invalid/textures/download
  files:
    - {texture: 4k_sun.jpg, used_by: sun}
    - {texture: 4k_venus_atmosphere.jpg, used_by: venus}
    - {texture: 4k_earth_daymap.jpg, used_by: earth}
    - {texture: 4k_earth_clouds.jpg, used_by: earth.clouds}
    - {texture: 4k_jupiter.jpg, used_by: jupiter}
    - {texture: 2k_neptune.jpg, used_by: neptune}
    - {texture: 8k_stars_milky_way.jpg, used_by: background.starfield, credit_extra: "Milky Way panorama, CC BY 4.0"}

background:
  starfield: 8k_stars_milky_way.jpg
"""

# 任务层的夹具：在主规格之上补一颗卫星（海卫一）与一整块 ``missions:``。用字符串拼而不是
# 复制一份完整 YAML，是为了让两份规格永远同步——背景层只有一处定义。
TRITON_BODY = """  - id: triton
    name_cn: 海卫一
    kind: minor
    satellite: true
    real: {radius_km: 1353, orbit_au: 30.070}
    display: {radius: 0.35, anchor: {body: neptune, offset: [-6.0, -3.0]}}
    material:
      type: rock
      roughness: 0.95
      color: [0.44, 0.42, 0.40]

"""

MISSION_BLOCK = """
missions:
  dot_radius: 0.45
  dot_strength: 1.5
  lines:
    - {id: exo, name_cn: 系外线, color: [0.105, 0.070, 0.342]}
    - {id: venus, name_cn: 金星线, color: [0.694, 0.202, 0.024]}
    - {id: neptune, name_cn: 海王星线, color: [0.014, 0.195, 0.262]}
  sites:
    - {id: l2, name_cn: 日地 L2}
    - {id: l1, name_cn: 日金 L1}
    - {id: venus_orbit, name_cn: 类金星轨道}
    - {id: venus, name_cn: 金星}
    - {id: outer, name_cn: 外太阳系}
  legend:
    at: [-30.0, -16.0]
    row_spacing: 2.6
    dot_radius: 0.6
    text_size: 1.6
    text_gap: 1.0
    trajectory_label: 海王星飞行轨迹
  clusters:
    - id: interferometer
      name_cn: 干涉天文台
      site: l2
      line: exo
      radius: 0.5
      anchor: {body: earth, side: anti_sun, distance: 6.0}
      label: 日地 L2：干涉天文台（1+2→1+4）
      label_offset: [0.0, -3.6]
      dots:
        - {id: combiner, offset: [0.0, 0.0]}
        - {id: collector_a, offset: [-2.0, 1.4]}
        - {id: collector_b, offset: [2.0, 1.4]}
    - id: venus_l1
      name_cn: 日金 L1 多信使观测设施
      site: l1
      line: venus
      radius: 0.5
      anchor: {body: venus, side: sunward, distance: 4.5}
      label: 日金 L1：多信使观测设施
      dots:
        - {id: facility, offset: [0.0, 0.0]}
    - id: survey
      name_cn: 巡天星座
      site: venus_orbit
      line: venus
      radius: 0.45
      at: [-13.0, 8.0]
      label: 巡天星座：母星＋子星
      dots:
        - {id: mother, offset: [0.0, 0.0]}
        - {id: child_a, offset: [-2.6, -0.8]}
        - {id: child_b, offset: [2.6, 0.8]}
    - id: venus_first
      name_cn: 第一次·大气进入探测
      site: venus
      line: venus
      radius: 0.5
      anchor: {body: venus, offset: [-2.0, -3.6]}
      label: 第一次·大气进入探测：环绕器＋进入探头
      label_offset: [0.0, -3.0]
      dots:
        - {id: orbiter, offset: [-1.6, 0.0]}
        - {id: probe, offset: [1.4, 1.0]}
    - id: venus_second
      name_cn: 第二次·大气驻留探测
      site: venus
      line: venus
      radius: 0.5
      anchor: {body: venus, offset: [3.4, -2.6]}
      label: 第二次·大气驻留探测：大气巡飞器
      label_offset: [0.0, 3.2]
      dots:
        - {id: cruiser, offset: [0.0, 0.0]}
    - id: neptune_orbiter
      name_cn: 核动力轨道器
      site: outer
      line: neptune
      radius: 0.5
      anchor: {body: neptune, offset: [-4.2, -6.0]}
      label: 核动力轨道器（极轨环绕）
      dots:
        - {id: orbiter, offset: [0.0, 0.0]}
    - id: neptune_probe
      name_cn: 大气探测器
      site: outer
      line: neptune
      radius: 0.5
      anchor: {body: neptune, offset: [-3.2, 1.0]}
      label: 大气探测器
      dots:
        - {id: probe, offset: [0.0, 0.0]}
    - id: triton_penetrator
      name_cn: 海卫一穿透器
      site: outer
      line: neptune
      radius: 0.45
      anchor: {body: triton, offset: [-0.8, 0.6]}
      label: 海卫一穿透器
      label_offset: [0.0, -2.2]
      dots:
        - {id: penetrator, offset: [0.0, 0.0]}
"""

MISSION_SPEC_YAML = SPEC_YAML.replace("bands:\n", TRITON_BODY + "bands:\n") + MISSION_BLOCK


def mission_spec_document() -> Dict[str, object]:
    """带任务层的那份规格文档（可任意改坏，每次重新解析）。"""
    return copy.deepcopy(yaml.safe_load(MISSION_SPEC_YAML))


def load_mission_spec(root: Path) -> SceneSpec:
    return load_spec(write_spec(root, MISSION_SPEC_YAML))


def spec_document() -> Dict[str, object]:
    """一份可任意改坏的规格文档（每次都重新解析，改一处不影响别的用例）。"""
    return copy.deepcopy(yaml.safe_load(SPEC_YAML))


def write_spec(root: Path, text: str = SPEC_YAML, name: str = SPEC_NAME) -> Path:
    """把规格落到 ``<root>/_primer/scene/<name>``。"""
    path = Path(root) / "_primer" / "scene" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def load_fixture_spec(root: Path) -> SceneSpec:
    return load_spec(write_spec(root))


def parse_document(document: Mapping[str, object], root: Path) -> SceneSpec:
    """解析一份（多半被改坏的）文档，路径字段指向夹具目录。"""
    return parse_spec(document, Path(root) / "_primer" / "scene" / SPEC_NAME)


def fake_payload(texture: str) -> bytes:
    """贴图替身：内容由文件名决定，所以 sha256 稳定、可复现。"""
    return ("FAKE-" + texture + "\n").encode("utf-8") * 8


def stage_assets(root: Path, spec: SceneSpec) -> Tuple[Path, ...]:
    """把规格声明的每张贴图落到 ``assets/textures/`` 并写好账本（不联网）。"""
    entries: List[AssetEntry] = []
    written: List[Path] = []
    for entry in spec.assets.files:
        payload = fake_payload(entry.texture)
        path = texture_path(root, entry.texture)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        written.append(path)
        credit = spec.assets.credit
        if entry.credit_extra:
            credit = f"{credit}; {entry.credit_extra}"
        entries.append(
            AssetEntry(
                texture=entry.texture,
                url=f"{spec.assets.base_url}/{entry.texture}",
                source_file=entry.texture,
                path=path.relative_to(root).as_posix(),
                sha256=hashlib.sha256(payload).hexdigest(),
                bytes=len(payload),
                license=spec.assets.license,
                credit=credit,
                downloaded="2026-01-01",
            )
        )
    ledger = ledger_path(root)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(
        yaml.safe_dump(
            {
                "source": spec.assets.source,
                "license": spec.assets.license,
                "credit": spec.assets.credit,
                "base_url": spec.assets.base_url,
                "fetched": "2026-01-01",
                "files": [entry.__dict__ for entry in entries],
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return tuple(written)


# ---------------------------------------------------------------- 画面


def _filter_row(raw: bytes, previous: bytes, filter_type: int, bpp: int) -> bytes:
    """按 PNG 的滤波规则写出该行的字节（支持 0 无滤波 / 1 Sub / 2 Up）。"""
    if filter_type == 0:
        return raw
    if filter_type == 1:
        return bytes(
            (raw[index] - (raw[index - bpp] if index >= bpp else 0)) & 0xFF for index in range(len(raw))
        )
    if filter_type == 2:
        return bytes((raw[index] - previous[index]) & 0xFF for index in range(len(raw)))
    raise ValueError(f"unsupported fixture filter type: {filter_type}")


def write_png(
    path: Path,
    width: int,
    height: int,
    luminance: float = 0.5,
    bit_depth: int = 8,
    filter_type: int = 0,
    gradient: bool = False,
) -> Path:
    """手写一张 PNG（标准库实现，避免为测试引入图像库）。

    ``filter_type`` 与 ``gradient`` 是给"滤波器还原"那条回归测试用的：一行纯色图用零号
    滤波器也能读对，只有把逐像素变化的图配上非零滤波器，才有可能暴露"左邻少算了一个像素"
    这类错误——真机上的 Blender 输出正是自适应滤波的。
    """

    def chunk(kind: bytes, body: bytes) -> bytes:
        return (
            struct.pack(">I", len(body))
            + kind
            + body
            + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)
        )

    sample_bytes = bit_depth // 8
    bpp = 3 * sample_bytes
    if bit_depth == 16:
        value = struct.pack(">H", int(luminance * 65535))
        flat = value * 3 * width
    else:
        flat = bytes([int(luminance * 255)] * 3 * width)
    rows = []
    previous = bytes(len(flat))
    for row_index in range(height):
        if gradient:
            raw = bytes(
                ((column * 7 + row_index * 13) % 200 + 20) for column in range(width) for _ in range(3)
            )
        else:
            raw = flat
        rows.append(bytes([filter_type]) + _filter_row(raw, previous, filter_type, bpp))
        previous = raw
    body = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, bit_depth, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"".join(rows), 6))
        + chunk(b"IEND", b"")
    )
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


# ---------------------------------------------------------------- 假现场


def _trajectory_points(spec: SceneSpec) -> Tuple[List[List[float]], List[int]]:
    """折线与每个节点在折线上的位置——直接问几何模块要，夹具不再自己模拟一套。"""
    plan_ = trajectory_polyline(spec)
    points = [[float(value) for value in point] for point in plan_.points]
    indices = [plan_.node_points[node.id] for node in spec.trajectory.nodes]
    return points, indices


def _box(center: Sequence[float], half: Sequence[float]) -> List[List[float]]:
    """一个轴对齐盒子的 8 个角——标签那种"一块矩形板"的取样点。"""
    return [
        [float(center[0] + sx * half[0]), float(center[1] + sy * half[1]), float(center[2] + sz * half[2])]
        for sx, sy, sz in (
            (-1, -1, -1), (-1, -1, 1), (-1, 1, -1), (-1, 1, 1),
            (1, -1, -1), (1, -1, 1), (1, 1, -1), (1, 1, 1),
        )
    ]


def _sphere_probes(center: Sequence[float], radius: float, rings: int = 5, spokes: int = 12):
    """球面上的一组取样点——真场景里报告的就是网格顶点，这里是同一件事的便宜替身。"""
    points = [[float(center[axis]) for axis in range(3)]]
    for ring in range(1, rings + 1):
        phi = math.pi * ring / (rings + 1)
        for spoke in range(spokes):
            theta = 2.0 * math.pi * spoke / spokes
            points.append([
                center[0] + radius * math.sin(phi) * math.cos(theta),
                center[1] + radius * math.sin(phi) * math.sin(theta),
                center[2] + radius * math.cos(phi),
            ])
    return points


def fake_probes(spec: SceneSpec) -> Mapping[str, List[List[float]]]:
    """照 Blender 的口径给每个契约对象编一组取样点（取景判据吃的是这个）。

    形状与真场景一致：天体是球面上的点，大气壳与云层各自大一圈，标签是"整幅字宽 ×
    一个字高"的薄板四角，航迹取控制点（都在曲线上），星带取环上的散点，天球远得离谱
    ——正因为离谱，取景核对必须把它排除在外（它本来就该铺满画面）。
    """
    resolved = plan(spec)
    placements = resolved["bodies_by_id"]
    boxes: Dict[str, List[List[float]]] = {}
    for body in spec.bodies:
        placement = placements[body.id]
        name = emit.body_object_name(body)
        boxes[name] = _sphere_probes(placement.position, placement.display_radius)
        for shell, attribute in (
            (emit.planet_atmosphere_name(body.id), body.atmosphere),
            (emit.planet_clouds_name(body.id), body.clouds),
        ):
            if attribute is not None:
                radius = placement.display_radius * attribute.scale
                boxes[shell] = _sphere_probes(placement.position, radius)
    for item in emit.label_placements(spec):
        boxes[item.object_name] = _box(item.position, (item.extent, 0.6 * item.size, 0.1))
    points, _ = _trajectory_points(spec)
    boxes[emit.trajectory_object_name(spec.trajectory.destination.id)] = [
        [float(value) for value in point] for point in points
    ]
    for band, placement in zip(spec.bands, resolved["bands"]):
        boxes[emit.band_object_name(band.id)] = _sphere_probes(
            placement.center, max(placement.half_x, placement.half_y), rings=3, spokes=16
        )
    # 任务层的占位点：球面上的取样点，与真场景里那颗自发光小球同一口径。标签走上面那条
    # 通用循环（`label_placements` 已经把簇标签与图例文字一并列出）。
    for cluster in mission_placements(spec):
        for dot in cluster.dots:
            boxes[emit.mission_dot_name(dot.cluster_id, dot.dot_id)] = _sphere_probes(
                dot.position, dot.radius, rings=2, spokes=8
            )
    for row in legend_placements(spec):
        boxes[emit.legend_dot_name(row.identifier)] = _sphere_probes(
            row.dot_position, row.dot_radius, rings=2, spokes=8
        )
    boxes[emit.STARFIELD_OBJECT] = _sphere_probes((0.0, 0.0, 0.0), 6000.0, rings=2, spokes=4)
    return boxes


def fake_report(
    spec: SceneSpec,
    project_root: Path,
    render_path: Path,
    mode: str = "preview",
    missing_objects: Sequence[str] = (),
    extra_objects: Sequence[Mapping[str, object]] = (),
    anchor_drift: float = 0.0,
    body_property_drift: Optional[Tuple[str, str, float]] = None,
    probe_scale: float = 1.0,
) -> Mapping[str, object]:
    """造一份现场报告（emitter 写在 ``build/scene_report.json`` 的那个形状）。

    允许注入几处"坏掉的地方"：少一个对象、多一个对象、锚点漂移、自定义属性不对、几何被
    放大 —— 验收关卡的每一条判据都能因此被单向地测到。``probe_scale`` 只放大取样点，
    用来制造"几何伸到画外"而坐标（圆心、锚点）仍然合规的情形。
    """
    root = Path(project_root)
    resolved = plan(spec)
    placements = resolved["bodies_by_id"]
    contract = emit.object_contract(spec)
    boxes = fake_probes(spec)

    objects: List[Dict[str, object]] = []
    for name, collection in contract.items():
        if name in missing_objects:
            continue
        probes = boxes.get(name)
        if probes and probe_scale != 1.0:
            centre = [sum(point[axis] for point in probes) / len(probes) for axis in range(3)]
            probes = [
                [centre[axis] + (point[axis] - centre[axis]) * probe_scale for axis in range(3)]
                for point in probes
            ]
        objects.append(
            {
                "name": name,
                "type": "MESH",
                "collection": collection,
                "location": [0.0, 0.0, 0.0],
                "probes": probes or [],
                "custom": {},
            }
        )
    by_name = {str(entry["name"]): entry for entry in objects}
    for body in spec.bodies:
        entry = by_name.get(emit.body_object_name(body))
        if entry is None:
            continue
        placement = placements[body.id]
        entry["location"] = [float(value) for value in placement.position]
        custom = {
            "real_orbit_au": placement.real_orbit_au,
            "display_orbit_distance": placement.display_orbit_distance,
            "display_radius": placement.display_radius,
            "magnification": placement.magnification,
            "real_radius_km": body.real.radius_km,
        }
        if body_property_drift is not None and body_property_drift[0] == body.id:
            custom[body_property_drift[1]] = body_property_drift[2]
        entry["custom"] = custom
    objects.extend(dict(entry) for entry in extra_objects)
    objects.append(
        {
            "name": emit.CAMERA_OBJECT,
            "type": "CAMERA",
            "collection": "",
            "location": [0.0, 0.0, 0.0],
            "probes": [],
            "custom": {},
        }
    )

    points, node_points = _trajectory_points(spec)
    if anchor_drift:
        points[node_points[-1]] = [value + anchor_drift for value in points[node_points[-1]]]

    nodes = list(resolved["nodes"])
    fingerprint_path = root / "_primer" / "scene" / "build" / "fingerprint.json"
    fingerprint = json.loads(fingerprint_path.read_text(encoding="utf-8"))
    render_mode = spec.render.mode(mode == "final")
    return {
        "blender": "5.1.1",
        "mode": mode,
        "fingerprint": fingerprint,
        "spec_path": str(spec.path),
        "blend_path": str(root / "_primer" / "scene" / f"{spec.meta.name}.blend"),
        "render": {
            "path": str(render_path),
            "width": render_mode.width,
            "height": render_mode.height,
            "samples": render_mode.samples,
            "engine": "CYCLES",
            "view_transform": spec.render.color_management.view_transform,
            "look": spec.render.color_management.look,
        },
        "collections": sorted(emit.COLLECTIONS),
        "materials": sorted(emit.material_contract(spec)),
        "objects": objects,
        "missions": emit.mission_data(spec),
        "trajectory": {
            "name": emit.trajectory_object_name(spec.trajectory.destination.id),
            "material": emit.TRAJECTORY_MATERIAL,
            "points": points,
            "nodes": [
                {
                    "id": node.node_id,
                    "kind": spec_node.kind,
                    "body_id": node.body_id,
                    "anchor": [float(value) for value in node.position],
                    "point_index": node_points[index],
                }
                for index, (node, spec_node) in enumerate(zip(nodes, spec.trajectory.nodes))
            ],
            "assist": assist_block(spec),
            "capture": capture_block(spec),
        },
    }


def assist_block(spec: SceneSpec) -> Mapping[str, object]:
    """借力段的实测记录（夹具用几何模块的真值，不另编）。"""
    plan_ = trajectory_polyline(spec)
    assert plan_.assist is not None
    return {
        "node_id": plan_.assist.node_id,
        "body_id": plan_.assist.body_id,
        "periapsis": plan_.assist.periapsis,
        "periapsis_radii": plan_.assist.periapsis_radii,
        "turn_deg": plan_.assist.turn_deg,
        "entry": list(plan_.assist.entry),
        "exit": list(plan_.assist.exit),
    }


def capture_block(spec: SceneSpec) -> Mapping[str, object]:
    plan_ = trajectory_polyline(spec)
    assert plan_.capture is not None
    return {
        "node_id": plan_.capture.node_id,
        "body_id": plan_.capture.body_id,
        "semi_major": plan_.capture.semi_major,
        "eccentricity": plan_.capture.eccentricity,
        "polar": plan_.capture.polar,
        "join": list(plan_.capture.join),
        "plane_azimuth_deg": plan_.capture.plane_azimuth_deg,
    }


def stage_report(root: Path, report: Mapping[str, object]) -> Path:
    """把假现场报告写到 ``build/scene_report.json``。"""
    path = Path(root) / "_primer" / "scene" / "build" / "scene_report.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8"
    )
    return path


def stage_fingerprint(root: Path, spec: SceneSpec) -> Path:
    """把当前指纹落到 ``build/fingerprint.json``（``fake_report`` 会读它）。"""
    from primer.scene.fingerprint import compute_fingerprint

    path = Path(root) / "_primer" / "scene" / "build" / "fingerprint.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            compute_fingerprint(spec, root).as_mapping(), ensure_ascii=False, sort_keys=True, indent=2
        ),
        encoding="utf-8",
    )
    return path


def staged_scene(
    root: Path,
    luminance: float = 0.5,
    bit_depth: int = 8,
    spec_text: str = SPEC_YAML,
    **report_kwargs,
) -> Tuple[SceneSpec, Mapping[str, object]]:
    """搭一个"看起来已经构建过"的工程目录：规格 + 贴图 + 账本 + 成图 + 现场报告。

    ``spec_text`` 让人用改过一两处的规格搭同一套产物——改一处再看关卡反应，比在报告上
    动手脚更接近真实情形。
    """
    spec = load_spec(write_spec(root, spec_text))
    stage_assets(root, spec)
    (root / "_primer" / "scene" / "build").mkdir(parents=True, exist_ok=True)
    stage_fingerprint(root, spec)
    mode = spec.render.preview
    render_path = (
        root
        / "_primer"
        / "scene"
        / spec.render.output.preview_dir
        / f"{spec.meta.name}_preview_{mode.width}x{mode.height}.png"
    )
    write_png(render_path, mode.width, mode.height, luminance=luminance, bit_depth=bit_depth)
    (root / "_primer" / "scene" / f"{spec.meta.name}.blend").write_bytes(b"fake blend\n")
    report = fake_report(spec, root, render_path, **report_kwargs)
    stage_report(root, report)
    return spec, report


def staged_mission_scene(root: Path, **kwargs) -> Tuple[SceneSpec, Mapping[str, object]]:
    """带任务层的假现场：与 :func:`staged_scene` 同一套产物，只是规格里多了 ``missions:``。"""
    kwargs.setdefault("spec_text", MISSION_SPEC_YAML)
    return staged_scene(root, **kwargs)


def ledger_entries(root: Path) -> Mapping[str, AssetEntry]:
    return read_ledger(ledger_path(root))


def swapped_placement(spec: SceneSpec, root: Path, first: str, second: str) -> SceneSpec:
    """把两个天体的显示位置对调，其余原样——用来制造次序颠倒的布局。"""
    document = yaml.safe_load(spec.path.read_text(encoding="utf-8"))
    bodies = {body["id"]: body for body in document["bodies"]}
    a, b = bodies[first]["display"], bodies[second]["display"]
    a["x"], a["y"], b["x"], b["y"] = b["x"], b["y"], a["x"], a["y"]
    return load_spec(write_spec(root, yaml.safe_dump(document, allow_unicode=True, sort_keys=False)))


def moved_body(spec: SceneSpec, root: Path, body_id: str, x: float, y: float) -> SceneSpec:
    """把一个天体挪到指定的显示位置。"""
    document = yaml.safe_load(spec.path.read_text(encoding="utf-8"))
    for body in document["bodies"]:
        if body["id"] == body_id:
            body["display"]["x"], body["display"]["y"] = x, y
    return load_spec(write_spec(root, yaml.safe_dump(document, allow_unicode=True, sort_keys=False)))


def resized_body(spec: SceneSpec, root: Path, body_id: str, radius: float) -> SceneSpec:
    """改一个天体的显示半径，其余原样。"""
    document = yaml.safe_load(spec.path.read_text(encoding="utf-8"))
    for body in document["bodies"]:
        if body["id"] == body_id:
            body["display"]["radius"] = radius
    return load_spec(write_spec(root, yaml.safe_dump(document, allow_unicode=True, sort_keys=False)))
