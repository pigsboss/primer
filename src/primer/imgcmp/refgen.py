# -*- coding: utf-8 -*-
"""primer-imgrefgen —— 参考生成器（imgcmp 工具链的"真值源"）。

**定位**：refgen 是**离 Blender 的编排器**——本体在普通 python 下跑，负责参数解析、
构型声明、真值提取与报告；它把渲染任务写成一个 job JSON，生成一个 Blender 侧脚本，
再用 ``blender --background --python <tmp>`` 执行渲染（四类图），最后**在 refgen 侧**
用 numpy/PIL 从自己的 ``id.png`` 里算几何真值。Blender 只出图，不算真值。

**产物**（``--out`` 目录）::

    views/az{A}_el{E}_roll{R}/
        id.png           每件一个唯一纯色 → 逐像素部件标签（关抗锯齿、关阴影、无泛光）
        id_legend.json   rgb -> 部件名/序号（英文键）
        silhouette.png   白对象/黑底、关抗锯齿的掩膜
        white_cad.png    白底 CAD 观感（背景纯白、环境/平光着色、无阴影）
        black_render.png 黑底渲染（背景纯黑、单一主光＋fill、不加星空）
        truth.json       该机位的真值（见下）
    truth_summary.json   本次运行的机位清单 ＋ 各机位 truth 的路径与 sha256

**真值口径（关键，T2a 与 refgen 不同源）**：

- **几何量**（``bbox_xyxy``／``pixel_count``／``area_fraction``／``height_fraction``／
  ``total_payload_height_px``／``contacts``／``islands``）＝ **由 ``id.png`` 逐像素算得**：
  先按 ``id_legend.json`` 把像素映到部件（近邻匹配，容差内），再做**连通域 ＋ 包围盒**。
  refgen **不做形状描述子、不做分割阈值、不做边缘检测**——那是 T2a 的事，两者不同源，
  才不会测试循环自证。
- **语义量**（``shape_class`` ∈ rect/circle/ring/rod/other、以及"哪件是锚件"）＝
  **手工声明的 YAML**（``--truth`` 指向的文件，或 ``--assemble`` spec 内的 ``truth:`` 块）。
  refgen 从不用形状描述子自动分类。
- ``components[].source``：该件在 YAML 里被点名声明＝``semantic``；只是被 ID 图发现、
  YAML 未点名（形类按缺省填 ``other``）＝``derived``。

**``--assemble spec.yaml``（拼装模式）**：按 YAML 把多个部件文件拼成**已知构型**并放置
（每项 ``part``／``offset_m``／``rot_deg``／``scale``），用于造**受控缺陷对**——组件数与
高度分数的差异由 spec 指定，可直接给 A-T2a-1 当数据源。spec 可带 ``expect:`` 声明期望
差异，refgen 打印"期望 vs 实测"对照表。

**颜色分配**：部件序号 ``i`` → ``palette_color(i)``，三通道各 31 级、步距 8 起于 8
（最多 29791 件）。Workbench ``OBJECT`` 颜色模式直接取 ``obj.color``，我们把 sRGB 目标值
先转线性写入；``view_transform=Standard`` ＋ ``dither_intensity=0`` ＋ ``render_aa=OFF``
使像素**逐字节等于目标色**（实测往返精确）。refgen 仍按近邻容差匹配，故 1 LSB 抖动也安全。

语言纪律（docs/guides/CODING_STANDARDS.md v1.1）：stdout 只出中文人读报告；
stderr／异常／选项名／JSON 键一律英文。
"""

from __future__ import annotations

import math
import os
import shutil
import subprocess
import sys
import tempfile
import time

import numpy as np
import yaml

from .common import (
    ImgCmpError,
    ensure_dir,
    make_parser,
    read_json,
    report,
    run_main,
    save_rgb,
    selftest_report,
    sha256_16,
    write_json,
)

TOOL = "primer.imgcmp.refgen"

VIEW_KINDS = ("white", "black", "id", "silhouette")
VIEW_FILES = {
    "id": "id.png",
    "silhouette": "silhouette.png",
    "white": "white_cad.png",
    "black": "black_render.png",
}
VIEW_LABEL = {
    "id": "ID 图（逐像素部件标签）",
    "silhouette": "剪影（白对象/黑底）",
    "white": "白底 CAD 观感",
    "black": "黑底渲染",
}
SHAPE_CLASSES = ("rect", "circle", "ring", "rod", "other")

PALETTE_BASE = 8
PALETTE_STEP = 8
PALETTE_LEVELS = 31
PALETTE_CAPACITY = PALETTE_LEVELS ** 3
BACKGROUND_RGB = (0, 0, 0)

CONTACT_TOL_PX = 2.0
CONTACT_MAX = 64

DEFAULT_FOCAL_MM = 50.0
DEFAULT_SENSOR_MM = 36.0

MATCH_COLOR_TOL = 4.0
"""像素色与调色板色的最大曼哈顿距离；超出即判为未匹配（背景或残留抗锯齿边）。"""


# ---------------------------------------------------------------- 调色板

def palette_color(index: int) -> tuple[int, int, int]:
    """部件序号 → 唯一纯色。三通道各 31 级、步距 8、起于 8（避开纯黑背景）。"""
    if index < 0:
        raise ImgCmpError("component index must be >= 0, got: %r" % (index,))
    return (
        PALETTE_BASE + PALETTE_STEP * (index % PALETTE_LEVELS),
        PALETTE_BASE + PALETTE_STEP * ((index // PALETTE_LEVELS) % PALETTE_LEVELS),
        PALETTE_BASE + PALETTE_STEP * ((index // (PALETTE_LEVELS ** 2)) % PALETTE_LEVELS),
    )


def srgb_to_linear(u8: int) -> float:
    """8 bit sRGB → 线性光（Blender ``obj.color`` 是线性的）。"""
    c = float(u8) / 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


# ---------------------------------------------------------------- 参数解析

def _num(value: float) -> str:
    return ("%g" % float(value))


def view_name(az: float, el: float, roll: float) -> str:
    return "az%s_el%s_roll%s" % (_num(az), _num(el), _num(roll))


def parse_res(text) -> tuple[int, int]:
    parts = [p.strip() for p in str(text).split(",")]
    if len(parts) != 2:
        raise ImgCmpError("--res must be 'W,H', got: %r" % (text,))
    try:
        w, h = (int(p) for p in parts)
    except ValueError as exc:
        raise ImgCmpError("--res must contain integers: %r" % (text,)) from exc
    if w < 32 or h < 32:
        raise ImgCmpError("--res must be at least 32x32, got: %r" % (text,))
    return (w, h)


def _floats(text, count, what):
    parts = [p.strip() for p in str(text).split(",")]
    if len(parts) != count:
        raise ImgCmpError("%s must be %d comma-separated numbers, got: %r" % (what, count, text))
    try:
        return tuple(float(p) for p in parts)
    except ValueError as exc:
        raise ImgCmpError("%s must be numbers, got: %r" % (what, text)) from exc


def parse_angles(text) -> list[tuple[float, float, float]]:
    """``"az,el,roll;az,el,roll"`` → 位姿表；``roll`` 可省（默认 0）。"""
    if text is None or str(text).strip() == "":
        return [(0.0, 0.0, 0.0)]
    out = []
    for chunk in str(text).split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = [p.strip() for p in chunk.split(",")]
        if len(parts) == 2:
            parts.append("0")
        if len(parts) != 3:
            raise ImgCmpError("--angles entries must be 'az,el[,roll]', got: %r" % (chunk,))
        try:
            out.append(tuple(float(p) for p in parts))
        except ValueError as exc:
            raise ImgCmpError("--angles entries must be numbers, got: %r" % (chunk,)) from exc
    if not out:
        raise ImgCmpError("--angles produced no poses: %r" % (text,))
    return out


def parse_grid(text) -> list[tuple[float, float, float]]:
    """``--az-el-grid`` → 位姿表。

    两值形式 ``AZ_STEP,EL_STEP``：以 0 为中心的 3×3 网格
    （``az ∈ {-AZ_STEP, 0, +AZ_STEP}``、``el ∈ {-EL_STEP, 0, +EL_STEP}``），共 9 个位姿；
    六值形式 ``AZ0,AZ1,AZ_STEP,EL0,EL1,EL_STEP``：显式给区间与步长，两端点均含。
    """
    parts = [p.strip() for p in str(text).split(",")]
    try:
        vals = [float(p) for p in parts]
    except ValueError as exc:
        raise ImgCmpError("--az-el-grid must contain numbers, got: %r" % (text,)) from exc
    if len(vals) == 2:
        az_s, el_s = vals
        azs = [-abs(az_s), 0.0, abs(az_s)]
        els = [-abs(el_s), 0.0, abs(el_s)]
    elif len(vals) == 6:
        az0, az1, az_s, el0, el1, el_s = vals
        if az_s == 0 or el_s == 0:
            raise ImgCmpError("--az-el-grid steps must be non-zero, got: %r" % (text,))
        azs = _linspace(az0, az1, az_s)
        els = _linspace(el0, el1, el_s)
    else:
        raise ImgCmpError("--az-el-grid takes 2 or 6 numbers, got: %r" % (text,))
    if not azs or not els:
        raise ImgCmpError("--az-el-grid produced an empty grid: %r" % (text,))
    return [(az, el, 0.0) for el in els for az in azs]


def _linspace(a: float, b: float, step: float) -> list[float]:
    span = b - a
    n = int(math.floor(abs(span) / abs(step) + 1e-9))
    sign = 1.0 if step > 0 else -1.0
    if (span > 0 and step < 0) or (span < 0 and step > 0):
        sign = -sign if span != 0 else sign
    return [a + sign * abs(step) * k for k in range(n + 1)]


def parse_views(text) -> list[str]:
    if text is None:
        return list(VIEW_KINDS)
    items = [p.strip() for p in str(text).split(",") if p.strip()]
    if not items:
        raise ImgCmpError("--views produced an empty list: %r" % (text,))
    unknown = [v for v in items if v not in VIEW_KINDS]
    if unknown:
        raise ImgCmpError("unknown view(s) %s; known: %s"
                          % (", ".join(unknown), ", ".join(VIEW_KINDS)))
    if "id" not in items:
        raise ImgCmpError("--views must include 'id' (truth is derived from the id image)")
    out = []
    for v in VIEW_KINDS:
        if v in items:
            out.append(v)
    return out


# ---------------------------------------------------------------- YAML 读取

def _load_yaml(path: str, what: str) -> dict:
    if not os.path.isfile(path):
        raise ImgCmpError("%s not found: %s" % (what, path))
    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except Exception as exc:  # noqa: BLE001
        raise ImgCmpError("cannot parse %s %s: %s" % (what, path, exc)) from exc
    if data is None:
        raise ImgCmpError("%s is empty: %s" % (what, path))
    if not isinstance(data, dict):
        raise ImgCmpError("%s must be a mapping at top level: %s" % (what, path))
    return data


def _vec3(value, what, default=(0.0, 0.0, 0.0)):
    if value is None:
        return tuple(float(v) for v in default)
    if isinstance(value, (int, float)):
        return (float(value),) * 3
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ImgCmpError("%s must be a number or a list of 3 numbers, got: %r" % (what, value))
    try:
        return tuple(float(v) for v in value)
    except (TypeError, ValueError) as exc:
        raise ImgCmpError("%s must contain numbers, got: %r" % (what, value)) from exc


def parse_assemble_spec(path: str) -> dict:
    """读 ``--assemble`` spec 并校验。返回规范化后的 dict（路径已解析为绝对路径）。"""
    data = _load_yaml(path, "assemble spec")
    base = os.path.dirname(os.path.abspath(path))
    root = data.get("models_root")
    models_root = os.path.normpath(os.path.join(base, str(root))) if root else base
    raw_parts = data.get("parts")
    if not isinstance(raw_parts, list) or not raw_parts:
        raise ImgCmpError("assemble spec needs a non-empty 'parts' list: %s" % path)
    parts = []
    seen = set()
    for idx, item in enumerate(raw_parts):
        if not isinstance(item, dict):
            raise ImgCmpError("parts[%d] must be a mapping: %s" % (idx, path))
        rel = item.get("part")
        if not rel:
            raise ImgCmpError("parts[%d] misses 'part': %s" % (idx, path))
        resolved = str(rel) if os.path.isabs(str(rel)) else os.path.normpath(
            os.path.join(models_root, str(rel)))
        if not os.path.isfile(resolved):
            raise ImgCmpError("part file not found: %s (from parts[%d] %r)"
                              % (resolved, idx, rel))
        name = str(item.get("name") or os.path.splitext(os.path.basename(resolved))[0])
        if name in seen:
            raise ImgCmpError("duplicate part name %r in assemble spec: %s" % (name, path))
        seen.add(name)
        scale = _vec3(item.get("scale", 1.0), "parts[%d].scale" % idx, default=(1.0, 1.0, 1.0))
        if any(s <= 0.0 for s in scale):
            raise ImgCmpError("parts[%d].scale must be positive, got: %r" % (idx, scale))
        parts.append({
            "name": name,
            "path": resolved,
            "offset_m": _vec3(item.get("offset_m"), "parts[%d].offset_m" % idx),
            "rot_deg": _vec3(item.get("rot_deg"), "parts[%d].rot_deg" % idx),
            "scale": scale,
        })
    truth = data.get("truth") or {}
    if not isinstance(truth, dict):
        raise ImgCmpError("assemble spec 'truth' must be a mapping: %s" % path)
    expect = data.get("expect") or {}
    if not isinstance(expect, dict):
        raise ImgCmpError("assemble spec 'expect' must be a mapping: %s" % path)
    return {
        "path": os.path.abspath(path),
        "name": str(data.get("name") or os.path.splitext(os.path.basename(path))[0]),
        "models_root": models_root,
        "scale_to": data.get("scale_to"),
        "parts": parts,
        "truth": truth,
        "expect": expect,
    }


def _glob_match(pattern: str, name: str) -> bool:
    """带通配符的按 glob 匹配，否则要求**完全相等**（不做隐式前后缀匹配）。"""
    import fnmatch

    if any(ch in pattern for ch in "*?["):
        return fnmatch.fnmatchcase(name, pattern)
    return name == pattern


def resolve_semantics(truth_doc: dict | None, names) -> dict:
    """把手工声明的语义（形类/锚件）落到具体部件名上。

    返回 ``{"shape_class": {name: cls}, "source": {name: "semantic"|"derived"},
    "anchor": name|None, "anchor_size_m": float|None, "declared": [...]}``。
    ``truth_doc`` 为 ``None`` 时全部件都是 ``derived``／形类 ``other``。
    """
    doc = truth_doc or {}
    patterns = doc.get("parts") or {}
    if not isinstance(patterns, dict):
        raise ImgCmpError("truth 'parts' must be a mapping of name-pattern -> shape_class")
    declared_default = doc.get("default_shape_class")
    if declared_default is not None and declared_default not in SHAPE_CLASSES:
        raise ImgCmpError("unknown default_shape_class %r; known: %s"
                          % (declared_default, ", ".join(SHAPE_CLASSES)))
    shape, source, declared = {}, {}, []
    for nm in names:
        cls = None
        for pattern, value in patterns.items():
            if _glob_match(str(pattern), str(nm)):
                cls = value
                break
        if cls is None:
            shape[nm] = declared_default or "other"
            source[nm] = "derived"
        else:
            if cls not in SHAPE_CLASSES:
                raise ImgCmpError("unknown shape_class %r for %r; known: %s"
                                  % (cls, nm, ", ".join(SHAPE_CLASSES)))
            shape[nm] = cls
            source[nm] = "semantic"
            declared.append(nm)
    anchor_block = doc.get("anchor")
    anchor, anchor_size = None, None
    if isinstance(anchor_block, dict):
        anchor = anchor_block.get("name")
        anchor_size = anchor_block.get("size_m")
    elif anchor_block:
        anchor = anchor_block
    if anchor is not None and anchor not in names:
        raise ImgCmpError("declared anchor %r is not one of the components: %s"
                          % (anchor, ", ".join(sorted(str(n) for n in names))))
    return {
        "shape_class": shape,
        "source": source,
        "anchor": anchor,
        "anchor_size_m": float(anchor_size) if anchor_size is not None else None,
        "declared": declared,
    }


# ---------------------------------------------------------------- 真值提取（几何）

def _color_keys(rgb: np.ndarray) -> np.ndarray:
    a = np.asarray(rgb, dtype=np.int32)
    return (a[..., 0] << 16) | (a[..., 1] << 8) | a[..., 2]


def _match_labels(id_rgb: np.ndarray, legend) -> tuple[np.ndarray, dict]:
    """像素 → 部件序号（-1 ＝ 背景/未匹配）。返回 ``(labels, stats)``。

    先走"精确命中调色板"的快路径（渲染往返精确时全部走这条）；余下的杂色
    （背景、抗锯齿残留）按曼哈顿距离近邻匹配，超出容差的记为未匹配。
    """
    h, w = id_rgb.shape[:2]
    keys = _color_keys(id_rgb).reshape(-1)
    uniq, inv = np.unique(keys, return_inverse=True)
    key_counts = np.bincount(inv, minlength=uniq.shape[0])

    table = np.full(uniq.shape[0], -1, dtype=np.int32)
    bg_key = _color_keys(np.array(BACKGROUND_RGB, dtype=np.int32))
    pal_keys = _color_keys(np.array(legend, dtype=np.int32)) if len(legend) else \
        np.zeros(0, dtype=np.int32)
    pal_rgb = np.array(legend, dtype=np.int32) if len(legend) else np.zeros((0, 3), dtype=np.int32)

    exact = np.zeros(uniq.shape[0], dtype=bool)
    if pal_keys.size:
        order = np.argsort(pal_keys, kind="stable")
        sorted_keys = pal_keys[order]
        pos = np.clip(np.searchsorted(sorted_keys, uniq), 0, sorted_keys.shape[0] - 1)
        exact = sorted_keys[pos] == uniq
        table[exact] = order[pos[exact]]

    ur, ug, ub = (uniq >> 16) & 0xFF, (uniq >> 8) & 0xFF, uniq & 0xFF
    unmatched = 0
    for k in range(uniq.shape[0]):
        if exact[k] or uniq[k] == bg_key:
            continue
        if pal_keys.size:
            dist = (np.abs(pal_rgb[:, 0] - ur[k]) + np.abs(pal_rgb[:, 1] - ug[k])
                    + np.abs(pal_rgb[:, 2] - ub[k]))
            best = int(np.argmin(dist))
            if dist[best] <= MATCH_COLOR_TOL * 3:
                table[k] = best
                continue
        unmatched += int(key_counts[k])

    labels = table[inv].reshape(h, w)
    stats = {
        "unique_colors": int(uniq.shape[0]),
        "exact_matched_colors": int(np.count_nonzero(exact)),
        "unmatched_pixels": int(unmatched),
    }
    return labels, stats


def _islands_of(labels: np.ndarray, count: int) -> np.ndarray:
    """按 8 邻接数各部件（label）的连通域个数。返回长度 ``count`` 的数组。

    逐行取"游程"，同一部件且列区间相交的相邻两行游程才 union——这样一次扫描即得
    全部部件的连通域数，不必逐部件单独做一次洪水填充。
    """
    h, _w = labels.shape
    parent: dict[int, int] = {}
    root_label: dict[int, int] = {}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    prev = []  # (start, end, root, label)
    for y in range(h):
        row = labels[y]
        concat = np.concatenate((np.array([-2], dtype=np.int64), row.astype(np.int64),
                                 np.array([-2], dtype=np.int64)))
        edges = np.flatnonzero(np.diff(concat) != 0)
        runs = []
        for k in range(edges.shape[0] - 1):
            s, e = int(edges[k]), int(edges[k + 1]) - 1
            lab = int(row[s])
            if lab < 0:
                continue
            root = None
            for (ps, pe, pr, plab) in prev:
                if plab == lab and ps <= e + 1 and s <= pe + 1:
                    if root is None:
                        root = pr
                    else:
                        union(root, pr)
            if root is None:
                root = len(parent) + 1
                parent[root] = root
                root_label[root] = lab
            runs.append((s, e, root, lab))
        prev = runs

    counts = np.zeros(count, dtype=np.int64)
    for node in parent:
        if find(node) == node:
            counts[root_label[node]] += 1
    return counts


def extract_truth(id_rgb: np.ndarray, legend, camera: dict, model: dict,
                  shape_classes=None, sources=None, anchor=None, anchor_size_m=None,
                  contact_tol: float = CONTACT_TOL_PX) -> dict:
    """从 ID 图算几何真值。``legend`` ＝ ``[(r, g, b), ...]``，序号即部件序号。"""
    labels, match_stats = _match_labels(id_rgb, legend)
    h, w = labels.shape
    names = list(model.get("component_names") or [])
    n = len(legend)
    weights = list(model.get("component_weights") or [])
    shape_classes = shape_classes or {}
    sources = sources or {}

    flat = labels.ravel()
    idx = np.flatnonzero(flat >= 0)
    ys, xs = idx // w, idx % w
    lab = flat[idx]
    counts = np.bincount(lab, minlength=n).astype(np.int64)
    total_px = int(counts.sum())
    islands = _islands_of(labels, n)

    order = np.argsort(lab, kind="stable")
    lab_s, xs_s, ys_s = lab[order], xs[order], ys[order]
    starts = np.searchsorted(lab_s, np.arange(n), side="left")
    ends = np.searchsorted(lab_s, np.arange(n), side="right")

    geom = {}
    for i in range(n):
        lo, hi = int(starts[i]), int(ends[i])
        if hi <= lo:
            geom[i] = None
            continue
        x = xs_s[lo:hi]
        y = ys_s[lo:hi]
        geom[i] = {
            "bbox_xyxy": [int(x.min()), int(y.min()), int(x.max()) + 1, int(y.max()) + 1],
            "centroid": [round(float(x.mean()), 3), round(float(y.mean()), 3)],
            "pixel_count": int(hi - lo),
        }

    visible = [i for i in range(n) if geom[i] is not None]
    if visible:
        top = min(geom[i]["bbox_xyxy"][1] for i in visible)
        bottom = max(geom[i]["bbox_xyxy"][3] for i in visible)
        total_height = max(1, bottom - top)
    else:
        total_height = 1
    anchor_span = None
    if anchor is not None:
        for i, nm in enumerate(names):
            if nm == anchor and geom[i] is not None:
                b = geom[i]["bbox_xyxy"]
                anchor_span = max(b[2] - b[0], b[3] - b[1]) or 1
                break

    components = []
    for i in range(n):
        nm = names[i] if i < len(names) else "component_%d" % i
        g = geom[i]
        entry = {
            "index": i,
            "name": nm,
            "id_rgb": [int(c) for c in legend[i]],
            "visible": g is not None,
            "pixel_count": int(g["pixel_count"]) if g else 0,
            "area_fraction": round(g["pixel_count"] / total_px, 6) if (g and total_px) else 0.0,
            "bbox_xyxy": g["bbox_xyxy"] if g else None,
            "centroid": g["centroid"] if g else None,
            "shape_class": shape_classes.get(nm, "other"),
            "height_fraction": (round((g["bbox_xyxy"][3] - g["bbox_xyxy"][1]) / total_height, 6)
                                if g else 0.0),
            "anchor_ratio": (round(max(g["bbox_xyxy"][2] - g["bbox_xyxy"][0],
                                       g["bbox_xyxy"][3] - g["bbox_xyxy"][1]) / anchor_span, 6)
                             if (g and anchor_span) else None),
            "islands": int(islands[i]),
            "contacts": [],
            "source": sources.get(nm, "derived"),
        }
        if i < len(weights):
            entry["weight"] = weights[i]
        components.append(entry)

    _attach_contacts(components, contact_tol)

    components.sort(key=lambda c: (-c["pixel_count"], c["name"]))
    by_name = {c["name"]: c for c in components}

    return {
        "tool": TOOL,
        "view": camera.get("view"),
        "camera": camera,
        "model": model,
        "truth_schema": {
            "geometry_source": "id_image (connected components + bounding boxes only)",
            "semantic_source": "declared yaml (shape_class, anchor)",
            "background_rgb": list(BACKGROUND_RGB),
            "color_match_tol_manhattan": MATCH_COLOR_TOL * 3,
            "contact_tol_px": float(contact_tol),
            "contact_max_per_component": CONTACT_MAX,
            "height_axis": "y (image rows, top-down)",
            "total_height_definition":
                "span from the topmost to the bottommost object pixel over all components",
        },
        "total_payload_height_px": int(total_height),
        "object_pixel_count": total_px,
        "component_count": len(visible),
        "component_count_all": n,
        "anchor": {"name": anchor, "size_m": anchor_size_m} if anchor else None,
        "components": components,
        "components_by_name": by_name,
        "id_match": match_stats,
    }


def _attach_contacts(components, tol: float) -> None:
    """按包围盒间隙记接触/相邻关系（含间隙 px）。每个部件最多留 ``CONTACT_MAX`` 条。"""
    boxes, names, present = [], [], []
    for c in components:
        if c["bbox_xyxy"] is None:
            continue
        present.append(c)
        boxes.append(c["bbox_xyxy"])
        names.append(c["name"])
    m = len(boxes)
    if m < 2 or tol < 0:
        return
    arr = np.asarray(boxes, dtype=np.float64)
    chunk = max(1, min(m, int(4_000_000 / max(1, m))))
    pairs: dict[int, list] = {i: [] for i in range(m)}
    for start in range(0, m, chunk):
        stop = min(m, start + chunk)
        a = arr[start:stop]
        gx = np.maximum(a[:, None, 0] - arr[None, :, 2], arr[None, :, 0] - a[:, None, 2])
        gy = np.maximum(a[:, None, 1] - arr[None, :, 3], arr[None, :, 1] - a[:, None, 3])
        gap = np.maximum(gx, gy)
        ix = np.minimum(a[:, None, 2], arr[None, :, 2]) - np.maximum(a[:, None, 0], arr[None, :, 0])
        iy = np.minimum(a[:, None, 3], arr[None, :, 3]) - np.maximum(a[:, None, 1], arr[None, :, 1])
        overlap = (ix > 0) & (iy > 0)
        hit = (gap <= tol) & ~np.eye(m, dtype=bool)[start:stop]
        for r, cidx in np.argwhere(hit):
            i, j = start + int(r), int(cidx)
            if i >= j:
                continue
            pairs[i].append((float(gap[r, cidx]), bool(overlap[r, cidx]), j))
    for i in range(m):
        rows = sorted(pairs[i])[:CONTACT_MAX]
        out = []
        for gap, ov, j in rows:
            out.append({"part": names[j], "gap_px": round(gap, 3), "bbox_overlap": ov})
        present[i]["contacts"] = out
        present[i]["contacts_truncated"] = len(pairs[i]) > CONTACT_MAX


# ---------------------------------------------------------------- Blender 侧脚本

_BLENDER_SCRIPT = r'''
# -*- coding: utf-8 -*-
"""Blender-side renderer driven by primer-imgrefgen (job JSON on the command line)."""
import json
import math
import os
import sys
import time

import bpy
import mathutils


def srgb_to_linear(u8):
    c = float(u8) / 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def palette_color(i):
    return (8 + 8 * (i % 31), 8 + 8 * ((i // 31) % 31), 8 + 8 * ((i // 961) % 31))


def all_meshes():
    return [o for o in bpy.data.objects if o.type == 'MESH']


def world_bbox(objs):
    mn = [1e30] * 3
    mx = [-1e30] * 3
    for ob in objs:
        for corner in ob.bound_box:
            p = ob.matrix_world @ mathutils.Vector(corner)
            for k in range(3):
                mn[k] = min(mn[k], p[k])
                mx[k] = max(mx[k], p[k])
    return mn, mx


def flatten(objs):
    """Detach from parents while keeping the world transform; drop empties."""
    for ob in objs:
        if ob.type != 'MESH':
            continue
        mw = ob.matrix_world.copy()
        ob.parent = None
        ob.matrix_world = mw


def import_file(path):
    before = set(bpy.data.objects)
    if path.endswith(('.glb', '.gltf')):
        bpy.ops.import_scene.gltf(filepath=path)
    elif path.endswith('.stl'):
        try:
            bpy.ops.wm.stl_import(filepath=path)
        except AttributeError:
            bpy.ops.import_mesh.stl(filepath=path)
    else:
        raise RuntimeError("unsupported model format: %s" % path)
    fresh = [o for o in bpy.data.objects if o not in before]
    flatten(fresh)
    meshes = [o for o in fresh if o.type == 'MESH']
    for ob in [o for o in fresh if o.type != 'MESH']:
        bpy.data.objects.remove(ob, do_unlink=True)
    return meshes


def apply_matrix(objs, mat):
    for ob in objs:
        ob.matrix_world = mat @ ob.matrix_world


def load_world(scene, rgb=(0.0, 0.0, 0.0)):
    world = scene.world
    if world is None:
        world = bpy.data.worlds.new("refgen_world")
        scene.world = world
    tree = world.node_tree
    if tree is None:
        world.use_nodes = True
        tree = world.node_tree
    bg = tree.nodes.get("Background")
    if bg is not None:
        bg.inputs[0].default_value = (rgb[0], rgb[1], rgb[2], 1.0)
        bg.inputs[1].default_value = 1.0
    return world


def set_workbench(scene, color_type, background, aa, light='FLAT', single=(1, 1, 1),
                  single_color=(0.75, 0.75, 0.75)):
    scene.render.engine = 'BLENDER_WORKBENCH'
    scene.display.render_aa = aa
    sh = scene.display.shading
    sh.light = light
    sh.color_type = color_type
    sh.show_shadows = False
    sh.show_cavity = False
    sh.show_object_outline = False
    sh.show_specular_highlight = False
    if scene.world is not None:
        # Workbench reads the flat world.color, not the shader-node background.
        scene.world.color = background
        sh.background_type = 'WORLD'
    else:
        sh.background_type = 'VIEWPORT'
    sh.background_color = background
    if color_type == 'SINGLE':
        sh.single_color = single_color
    scene.view_settings.view_transform = 'Standard'
    scene.view_settings.look = 'None'
    scene.render.dither_intensity = 0.0


def set_eevee(scene, samples):
    for eng in ('BLENDER_EEVEE', 'BLENDER_EEVEE_NEXT'):
        try:
            scene.render.engine = eng
            break
        except TypeError:
            continue
    try:
        scene.eevee.taa_render_samples = samples
    except AttributeError:
        pass
    scene.view_settings.view_transform = 'Standard'
    scene.view_settings.look = 'None'
    scene.render.dither_intensity = 0.0


def make_lights(scene):
    key = bpy.data.objects.new("key", bpy.data.lights.new("key", type='SUN'))
    key.data.energy = 4.0
    key.data.angle = math.radians(2.0)
    fill = bpy.data.objects.new("fill", bpy.data.lights.new("fill", type='SUN'))
    fill.data.energy = 1.4
    fill.data.angle = math.radians(20.0)
    scene.collection.objects.link(key)
    scene.collection.objects.link(fill)
    return key, fill


def aim(obj, direction):
    obj.rotation_mode = 'QUATERNION'
    obj.rotation_quaternion = mathutils.Vector(direction).normalized().to_track_quat('-Z', 'Y')


def frame_camera(cam, corners, az, el, roll, ortho, focal, res, margin=1.08):
    w, h = res
    azr, elr = math.radians(az), math.radians(el)
    u = mathutils.Vector((math.sin(azr) * math.cos(elr),
                          -math.cos(azr) * math.cos(elr),
                          math.sin(elr)))
    center = mathutils.Vector((sum(c[0] for c in corners) / len(corners),
                               sum(c[1] for c in corners) / len(corners),
                               sum(c[2] for c in corners) / len(corners)))
    quat = (-u).to_track_quat('-Z', 'Y')
    quat = quat @ mathutils.Quaternion((0.0, 0.0, 1.0), math.radians(roll))
    cam.rotation_mode = 'QUATERNION'
    cam.rotation_quaternion = quat
    inv = quat.inverted()
    xs, ys, zs = [], [], []
    for c in corners:
        v = inv @ (mathutils.Vector(c) - center)
        xs.append(v.x)
        ys.append(v.y)
        zs.append(v.z)
    ex = (max(xs) - min(xs)) / 2.0 * margin
    ey = (max(ys) - min(ys)) / 2.0 * margin
    depth = max(zs) - min(zs)
    aspect = w / float(h)
    data = cam.data
    data.sensor_fit = 'HORIZONTAL'
    if ortho:
        data.type = 'ORTHO'
        data.ortho_scale = max(2.0 * ex, 2.0 * ey * aspect)
        dist = max(4.0 * (ex + ey) + depth, 1e-6) + 10.0
        scale = data.ortho_scale
    else:
        data.type = 'PERSP'
        data.lens = focal
        data.sensor_width = 36.0
        tan_hx = (data.sensor_width / 2.0) / focal
        dist = max(ex / tan_hx, ey * aspect / tan_hx) + depth
        scale = None
    cam.location = center + u * dist
    return {"center": [center.x, center.y, center.z], "distance": dist,
            "ortho_scale": scale, "extent_x": ex, "extent_y": ey}


def render_to(scene, path):
    scene.render.filepath = path
    bpy.ops.render.render(write_still=True)


def main():
    job_path = sys.argv[sys.argv.index("--") + 1]
    with open(job_path, encoding="utf-8") as fh:
        job = json.load(fh)

    out = job["out"]
    views = job["views"]
    poses = job["poses"]
    res = job["res"]
    ortho = bool(job["ortho"])
    focal = float(job["focal_mm"])
    scale_to = job.get("scale_to")

    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.render.resolution_x = int(res[0])
    scene.render.resolution_y = int(res[1])
    scene.render.resolution_percentage = 100
    scene.render.film_transparent = False
    scene.render.image_settings.file_format = 'PNG'
    scene.render.image_settings.color_mode = 'RGB'
    scene.render.image_settings.compression = 15
    load_world(scene, (0.0, 0.0, 0.0))

    t0 = time.time()
    components = []
    if job["mode"] == "assemble":
        for idx, part in enumerate(job["parts"]):
            objs = import_file(part["path"])
            if not objs:
                raise RuntimeError("no mesh in part %s" % part["path"])
            mn, mx = world_bbox(objs)
            center = mathutils.Vector(((mn[0] + mx[0]) / 2, (mn[1] + mx[1]) / 2,
                                       (mn[2] + mx[2]) / 2))
            rot = mathutils.Euler([math.radians(a) for a in part["rot_deg"]], 'XYZ').to_matrix().to_4x4()
            sca = mathutils.Matrix.Diagonal(
                mathutils.Vector(part["scale"]).to_4d()).to_4x4()
            mat = (mathutils.Matrix.Translation(mathutils.Vector(part["offset_m"]))
                   @ rot @ sca @ mathutils.Matrix.Translation(-center))
            apply_matrix(objs, mat)
            components.append({"name": part["name"], "objects": sorted(o.name for o in objs),
                               "object_refs": objs})
    else:
        objs = import_file(job["model"])
        if not objs:
            raise RuntimeError("no mesh in model %s" % job["model"])
        for ob in sorted(objs, key=lambda o: o.name):
            components.append({"name": ob.name, "objects": [ob.name], "object_refs": [ob]})
    import_s = time.time() - t0

    meshes = all_meshes()
    mn, mx = world_bbox(meshes)
    center = mathutils.Vector(((mn[0] + mx[0]) / 2, (mn[1] + mx[1]) / 2, (mn[2] + mx[2]) / 2))
    dims = [mx[k] - mn[k] for k in range(3)]
    s = 1.0
    if scale_to:
        longest = max(dims) or 1.0
        s = float(scale_to) / longest
    norm = (mathutils.Matrix.Translation(-s * center)
            @ mathutils.Matrix.Scale(s, 4))
    apply_matrix(meshes, norm)
    bpy.context.view_layer.update()
    mn2, mx2 = world_bbox(meshes)
    norm_dim = [mx2[k] - mn2[k] for k in range(3)]

    for idx, comp in enumerate(components):
        rgb = palette_color(idx)
        col = (srgb_to_linear(rgb[0]), srgb_to_linear(rgb[1]), srgb_to_linear(rgb[2]), 1.0)
        for ob in comp["object_refs"]:
            ob.color = col
            ob.pass_index = idx + 1

    pts = []
    for ob in meshes:
        for corner in ob.bound_box:
            p = ob.matrix_world @ mathutils.Vector(corner)
            pts.append((p.x, p.y, p.z))

    cam_data = bpy.data.cameras.new("refgen_cam")
    cam = bpy.data.objects.new("refgen_cam", cam_data)
    scene.collection.objects.link(cam)
    scene.camera = cam

    key, fill = make_lights(scene)

    verts = sum(len(o.data.vertices) for o in meshes)
    polys = sum(len(o.data.polygons) for o in meshes)
    meta = {
        "tool": "primer.imgcmp.refgen-blender",
        "mode": job["mode"],
        "components": [{"index": i, "name": c["name"], "rgb": list(palette_color(i)),
                        "objects": c["objects"]} for i, c in enumerate(components)],
        "model_normalized_dim_m": [round(v, 6) for v in norm_dim],
        "model_scale_applied": s,
        "mesh_count": len(meshes),
        "vertex_count": int(verts),
        "polygon_count": int(polys),
        "import_seconds": round(import_s, 3),
        "views": [],
    }

    ev_sub = ('#', '#')
    for pose in poses:
        vname = pose["name"]
        vdir = os.path.join(out, "views", vname)
        if not os.path.isdir(vdir):
            os.makedirs(vdir)
        cam_info = frame_camera(cam, pts, pose["az"], pose["el"], pose["roll"], ortho,
                               focal, res)
        cam_u = mathutils.Vector((math.sin(math.radians(pose["az"])) * math.cos(math.radians(pose["el"])),
                                  -math.cos(math.radians(pose["az"])) * math.cos(math.radians(pose["el"])),
                                  math.sin(math.radians(pose["el"]))))
        quat = cam.rotation_quaternion
        up = quat @ mathutils.Vector((0.0, 1.0, 0.0))
        right = quat @ mathutils.Vector((1.0, 0.0, 0.0))
        aim(key, -(cam_u + 0.75 * up - 0.60 * right))
        aim(fill, -(-cam_u + 0.45 * up + 0.85 * right))
        timings = {}
        for kind in views:
            path = os.path.join(vdir, {"id": "id.png", "silhouette": "silhouette.png",
                                       "white": "white_cad.png", "black": "black_render.png"}[kind])
            t = time.time()
            if kind == "id":
                set_workbench(scene, 'OBJECT', (0.0, 0.0, 0.0), 'OFF')
            elif kind == "silhouette":
                set_workbench(scene, 'SINGLE', (0.0, 0.0, 0.0), 'OFF',
                              single_color=(1.0, 1.0, 1.0))
            elif kind == "white":
                set_workbench(scene, 'MATERIAL', (1.0, 1.0, 1.0), '8', light='STUDIO')
            else:
                set_eevee(scene, 32)
            render_to(scene, path)
            timings[kind] = round(time.time() - t, 3)
        meta["views"].append({"name": vname, "az": pose["az"], "el": pose["el"],
                              "roll": pose["roll"], "camera": cam_info,
                              "timings_s": timings})
        print("%s rendered view %s (%.2fs)" % (ev_sub[0], vname, sum(timings.values())))

    meta["render_seconds"] = round(time.time() - t0, 3)
    with open(os.path.join(out, "render_meta.json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=1)
    print("# done: %s" % out)


main()
'''


def find_blender() -> str:
    """Blender 可执行文件：``$PRIMER_BLENDER`` → PATH 上的 ``blender`` → macOS 应用路径。"""
    env = os.environ.get("PRIMER_BLENDER")
    if env:
        if not os.path.isfile(env):
            raise ImgCmpError("PRIMER_BLENDER is not a file: %s" % env)
        return env
    found = shutil.which("blender")
    if found:
        return found
    app = "/Applications/Blender.app/Contents/MacOS/Blender"
    if os.path.isfile(app):
        return app
    raise ImgCmpError("Blender not found; set PRIMER_BLENDER or put 'blender' on PATH")


# ---------------------------------------------------------------- 主流程

def build_views(args) -> dict:
    """按 CLI 参数渲染并提取真值，返回本次运行的结果 dict（供报告与测试用）。"""
    if args.assemble and args.model:
        raise ImgCmpError("--model and --assemble are mutually exclusive")
    if not args.assemble and not args.model:
        raise ImgCmpError("either --model or --assemble is required (or use --selftest)")
    if not args.out:
        raise ImgCmpError("--out DIR is required")

    poses = parse_angles(args.angles)
    if args.az_el_grid:
        grid = parse_grid(args.az_el_grid)
        poses = poses + grid if args.angles else grid
        seen, uniq = set(), []
        for p in poses:
            key = view_name(*p)
            if key not in seen:
                seen.add(key)
                uniq.append(p)
        poses = uniq
    res = parse_res(args.res)
    views = parse_views(args.views)
    ortho = bool(args.ortho)
    scale_to = float(args.scale_to) if args.scale_to else None
    if scale_to is not None and scale_to <= 0:
        raise ImgCmpError("--scale-to must be positive, got: %r" % (args.scale_to,))
    contact_tol = float(args.contact_tol)

    spec = parse_assemble_spec(args.assemble) if args.assemble else None
    truth_doc = None
    if args.truth:
        truth_doc = _load_yaml(args.truth, "truth yaml")

    out_dir = os.path.abspath(args.out)
    ensure_dir(out_dir)
    ensure_dir(os.path.join(out_dir, "views"))

    job = {
        "out": out_dir,
        "views": views,
        "poses": [{"name": view_name(*p), "az": p[0], "el": p[1], "roll": p[2]} for p in poses],
        "res": list(res),
        "ortho": ortho,
        "focal_mm": float(args.focal),
        "scale_to": scale_to if scale_to is not None else (spec or {}).get("scale_to"),
        "contact_tol": contact_tol,
    }
    if spec:
        job["mode"] = "assemble"
        job["parts"] = spec["parts"]
        job["spec"] = spec["path"]
    else:
        model_path = os.path.abspath(args.model)
        if not os.path.isfile(model_path):
            raise ImgCmpError("model not found: %s" % model_path)
        if not model_path.lower().endswith((".glb", ".gltf", ".stl")):
            raise ImgCmpError("unsupported model format (expect .glb/.gltf/.stl): %s" % model_path)
        job["mode"] = "single"
        job["model"] = model_path

    blender = find_blender()
    tmp = tempfile.mkdtemp(prefix="refgen_")
    try:
        script_path = os.path.join(tmp, "refgen_blender.py")
        job_path = os.path.join(tmp, "job.json")
        with open(script_path, "w", encoding="utf-8") as fh:
            fh.write(_BLENDER_SCRIPT)
        write_json(job_path, job)
        t0 = time.time()
        try:
            proc = subprocess.run([blender, "--background", "--factory-startup",
                                   "--python", script_path, "--", job_path],
                                  capture_output=True, text=True, timeout=args.timeout)
        except subprocess.TimeoutExpired as exc:
            raise ImgCmpError("blender exceeded --timeout %.0fs" % args.timeout) from exc
        wall = time.time() - t0
        log_path = os.path.join(out_dir, "blender.log")
        with open(log_path, "w", encoding="utf-8") as fh:
            fh.write(proc.stdout)
            fh.write("\n--- stderr ---\n")
            fh.write(proc.stderr)
        if proc.returncode != 0:
            tail = "\n".join((proc.stderr or proc.stdout).strip().splitlines()[-12:])
            raise ImgCmpError("blender failed (exit %d):\n%s" % (proc.returncode, tail))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    meta = read_json(os.path.join(out_dir, "render_meta.json"))
    result = _extract_all(out_dir, meta, job, truth_doc, spec, contact_tol)
    result.update({
        "out": out_dir,
        "wall_seconds": round(wall, 3),
        "blender": blender,
        "log": log_path,
        "runtime_peak_rss_mb": _peak_rss_mb(),
        "job": job,
        "spec": spec,
    })
    _write_summary(result)
    return result


def _peak_rss_mb() -> float | None:
    try:
        import resource

        rss = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
        if sys.platform == "darwin":
            return round(rss / (1024.0 * 1024.0), 1)
        return round(rss / 1024.0, 1)
    except Exception:  # noqa: BLE001
        return None


def _extract_all(out_dir, meta, job, truth_doc, spec, contact_tol) -> dict:
    from PIL import Image

    model = {
        "source": "assemble" if spec else "single",
        "path": spec["path"] if spec else job["model"],
        "sha256_16": sha256_16(spec["path"] if spec else job["model"]),
        "normalized_dim_m": meta["model_normalized_dim_m"],
        "mesh_count": meta["mesh_count"],
        "vertex_count": meta["vertex_count"],
        "polygon_count": meta["polygon_count"],
    }
    if spec:
        model["parts"] = [{"name": p["name"], "path": p["path"],
                           "sha256_16": sha256_16(p["path"]),
                           "offset_m": list(p["offset_m"]), "rot_deg": list(p["rot_deg"]),
                           "scale": list(p["scale"])} for p in spec["parts"]]

    semantic_doc = truth_doc if truth_doc is not None else (spec or {}).get("truth") or None
    by_view = {v["name"]: v for v in meta["views"]}
    truths = []
    all_names = [c["name"] for c in meta["components"]]
    sem = resolve_semantics(semantic_doc, all_names)

    for pose in job["poses"]:
        vdir = os.path.join(out_dir, "views", pose["name"])
        id_path = os.path.join(vdir, "id.png")
        with Image.open(id_path) as im:
            id_rgb = np.asarray(im.convert("RGB"), dtype=np.uint8)
        legend = [[int(c) for c in comp["rgb"]] for comp in meta["components"]]
        write_json(os.path.join(vdir, "id_legend.json"), {
            "tool": TOOL,
            "view": pose["name"],
            "res": [int(id_rgb.shape[1]), int(id_rgb.shape[0])],
            "background_rgb": list(BACKGROUND_RGB),
            "legend": [{"index": comp["index"], "rgb": list(comp["rgb"]),
                        "name": comp["name"], "objects": comp["objects"],
                        "source": sem["source"].get(comp["name"], "derived")}
                       for comp in meta["components"]],
        })
        cam_meta = by_view[pose["name"]]
        camera = {
            "view": pose["name"],
            "az_deg": pose["az"],
            "el_deg": pose["el"],
            "roll_deg": pose["roll"],
            "az_convention": "horizon direction (sin(az), -cos(az), 0); az=0 is a front view "
                             "looking along +Y with +X to the right and +Z up",
            "ortho": bool(job["ortho"]),
            "focal_mm": None if job["ortho"] else float(job["focal_mm"]),
            "sensor_mm": DEFAULT_SENSOR_MM,
            "sensor_fit": "HORIZONTAL",
            "ortho_scale": cam_meta["camera"]["ortho_scale"],
            "distance": round(cam_meta["camera"]["distance"], 6),
            "target_center": [round(v, 6) for v in cam_meta["camera"]["center"]],
            "res": [int(id_rgb.shape[1]), int(id_rgb.shape[0])],
            "render_seconds": cam_meta["timings_s"],
        }
        model_view = dict(model)
        model_view["component_names"] = all_names
        truth = extract_truth(id_rgb, legend, camera, model_view,
                              shape_classes=sem["shape_class"], sources=sem["source"],
                              anchor=sem["anchor"], anchor_size_m=sem["anchor_size_m"],
                              contact_tol=contact_tol)
        tpath = os.path.join(vdir, "truth.json")
        write_json(tpath, {k: v for k, v in truth.items() if k != "components_by_name"})
        truths.append({
            "view": pose["name"],
            "truth": os.path.relpath(tpath, out_dir),
            "sha256_16": sha256_16(tpath),
            "component_count": truth["component_count"],
            "total_payload_height_px": truth["total_payload_height_px"],
        })

    expect = (spec or {}).get("expect") or {}
    expect_pose, expect_rows = _check_expect(expect, truths, out_dir) if expect else (None, [])
    return {
        "out": out_dir,
        "views": list(job["views"]),
        "poses": [p["name"] for p in job["poses"]],
        "meta": meta,
        "semantics": sem,
        "truths": truths,
        "expect": expect,
        "expect_pose": expect_pose,
        "expect_check": expect_rows,
    }


def _check_expect(expect: dict, truths, out_dir) -> tuple:
    """把 spec 里声明的期望差异与实测的 truth 对照。

    期望是给**基准机位**写的（高度分数随视角而变），因此固定对照
    ``expect.pose``（缺省 ``az0_el0_roll0``）；该机位不在本次运行里就当次不发生。
    """
    want_pose = str(expect.get("pose") or "az0_el0_roll0")
    names = [t["view"] for t in truths]
    if want_pose not in names:
        return (None, [])
    entry = truths[names.index(want_pose)]
    truth = read_json(os.path.join(out_dir, entry["truth"]))
    by_name = {c["name"]: c for c in truth["components"]}
    rows = []
    if "component_count" in expect:
        rows.append(("component_count", expect["component_count"], truth["component_count"],
                     _close(expect["component_count"], truth["component_count"])))
    if "total_payload_height_px" in expect:
        rows.append(("total_payload_height_px", expect["total_payload_height_px"],
                     truth["total_payload_height_px"],
                     _close(expect["total_payload_height_px"], truth["total_payload_height_px"])))
    for key in ("height_fraction", "area_fraction"):
        for name, want in (expect.get(key) or {}).items():
            comp = by_name.get(name)
            got = None if comp is None else comp[key]
            rows.append(("%s[%s]" % (key, name), want, got,
                         got is not None and _close(want, got)))
    return (want_pose, rows)


def _close(want, got, tol: float = 0.02) -> bool:
    try:
        return abs(float(want) - float(got)) <= tol
    except (TypeError, ValueError):
        return False


def _write_summary(result: dict) -> None:
    summary = {
        "tool": TOOL,
        "out": result["out"],
        "mode": result["job"]["mode"],
        "model": result["job"].get("model") or result["job"].get("spec"),
        "views": result["views"],
        "res": result["job"]["res"],
        "ortho": result["job"]["ortho"],
        "poses": result["truths"],
        "blender": result["blender"],
        "wall_seconds": result["wall_seconds"],
        "runtime_peak_rss_mb": result["runtime_peak_rss_mb"],
    }
    if result["expect_check"]:
        summary["expect"] = {
            "pose": result["expect_pose"],
            "checks": [{"check": name, "expected": want, "measured": got, "ok": ok}
                       for name, want, got, ok in result["expect_check"]],
        }
    write_json(os.path.join(result["out"], "truth_summary.json"), summary)


# ---------------------------------------------------------------- 报告

def _print_report(result: dict) -> None:
    meta = result["meta"]
    out = result["out"]
    rows = [
        ("产物目录", out),
        ("模式", "拼装（--assemble）" if result["job"]["mode"] == "assemble"
                 else "单件（--model）"),
        ("模型", result["job"].get("model") or result["job"].get("spec")),
        ("网格/顶点", "%d mesh｜%d 顶点｜%d 面"
                       % (meta["mesh_count"], meta["vertex_count"], meta["polygon_count"])),
        ("归一化尺寸", " × ".join("%.3f" % v for v in meta["model_normalized_dim_m"]) + " m"),
        ("机位", "%d 个：%s" % (len(result["poses"]), "、".join(result["poses"]))),
        ("图像", " ".join(VIEW_LABEL[v] for v in result["views"])),
        ("分辨率", "%d x %d%s" % (result["job"]["res"][0], result["job"]["res"][1],
                                  "（正交）" if result["job"]["ortho"] else "")),
        ("Blender", "%s｜导入 %.1fs｜本次总耗时 %.1fs"
                    % (result["blender"], meta["import_seconds"], result["wall_seconds"])),
    ]
    if result["runtime_peak_rss_mb"]:
        rows.append(("子进程峰值内存", "%.0f MB" % result["runtime_peak_rss_mb"]))
    sem = result["semantics"]
    rows.append(("手工声明件", "%d 件（形类语义源＝YAML）" % len(sem["declared"])))
    if sem["anchor"]:
        rows.append(("锚件", "%s%s" % (sem["anchor"],
                                       ("（%g m）" % sem["anchor_size_m"])
                                       if sem["anchor_size_m"] else "")))
    notes = [
        "几何真值由 id.png 逐像素算得：连通域＋包围盒，不含形状描述子、不含分割阈值"
        "（与 T2a 不同源）。",
        "shape_class 与锚件来自手工声明 YAML，refgen 不做自动形状分类。",
        "ID 色分配：序号 i → palette_color(i)（31³ 级、步距 8、起于 8）；Workbench OBJECT 色"
        "＋Standard 视图变换＋dither=0＋关抗锯齿，像素逐字节等于目标色。",
    ]
    report("参考生成：%d 机位 × %d 类图" % (len(result["poses"]), len(result["views"])),
           rows, notes)

    for entry in result["truths"]:
        truth = read_json(os.path.join(out, entry["truth"]))
        comps = truth["components"]
        top = [c for c in comps if c["pixel_count"] > 0][:3]
        sub = [
            ("机位", entry["view"]),
            ("组件数", "%d（可见）；声明共 %d 件" % (truth["component_count"],
                                                     truth["component_count_all"])),
            ("整器高度", "%d px" % truth["total_payload_height_px"]),
            ("面积前三", "｜".join("%s %.3f" % (c["name"], c["area_fraction"]) for c in top)
                         or "—"),
            ("高度分数前三", "｜".join("%s %.3f" % (c["name"], c["height_fraction"])
                                       for c in sorted(comps, key=lambda c: -c["height_fraction"])[:3])),
            ("未匹配像素", "%d（共 %d 种色）" % (truth["id_match"]["unmatched_pixels"],
                                                 truth["id_match"]["unique_colors"])),
            ("真值", entry["truth"] + "  sha256:" + entry["sha256_16"]),
        ]
        report("真值摘要", sub)

    if result["expect_check"]:
        bad = [r for r in result["expect_check"] if not r[3]]
        rows = [(name, "期望 %s｜实测 %s｜%s" % (want, got, "一致" if ok else "不符"))
                for name, want, got, ok in result["expect_check"]]
        report("受控缺陷对：期望差异表（对照机位 %s，%d 项，%d 项不符）"
               % (result["expect_pose"], len(result["expect_check"]), len(bad)), rows)


# ---------------------------------------------------------------- selftest

def _synth_id() -> tuple[np.ndarray, list, dict]:
    """自造 ID 图：rect／circle／ring／rod／双孤岛＋一处未声明色，附真值参照。

    刻意覆盖验收要用的量：包围盒、面积分数、高度分数、bbox 接触（box 与 rod 隔 1 px）、
    连通域个数（pair 分成两块＝2 个孤岛；ring 带孔但仍是 1 个孤岛）、未声明色的
    背景归并。
    """
    from PIL import Image, ImageDraw

    w, h = 480, 360
    img = Image.new("RGB", (w, h), BACKGROUND_RGB)
    draw = ImageDraw.Draw(img)
    legend = [palette_color(i) for i in range(5)]
    names = ["box", "disc", "ring", "rod", "pair"]
    draw.rectangle([40, 60, 160, 240], fill=tuple(legend[0]))          # rect
    draw.ellipse([200, 60, 320, 180], fill=tuple(legend[1]))           # circle
    draw.ellipse([360, 60, 460, 160], fill=tuple(legend[2]))
    draw.ellipse([382, 82, 438, 138], fill=BACKGROUND_RGB)             # ring（带孔）
    draw.rectangle([40, 242, 440, 260], fill=tuple(legend[3]))         # rod：距 box 下沿 1 px
    draw.rectangle([40, 300, 100, 330], fill=tuple(legend[4]))         # pair：两块同色
    draw.rectangle([140, 300, 200, 330], fill=tuple(legend[4]))
    draw.rectangle([240, 300, 300, 330], fill=(255, 0, 128))           # 未声明色
    arr = np.asarray(img, dtype=np.uint8)
    expect = {
        "pixels": {"box": 121 * 181, "rod": 401 * 19, "pair": 2 * (61 * 31),
                   "undeclared": 61 * 31},
        "bbox": {
            "box": [40, 60, 161, 241],
            "disc": [200, 60, 321, 181],
            "ring": [360, 60, 461, 161],
            "rod": [40, 242, 441, 261],
            "pair": [40, 300, 201, 331],
        },
        "touching": ("box", "rod"),
        "islands": {"ring": 1, "pair": 2, "box": 1},
        "total_height": 331 - 60,
    }
    return arr, legend, {"names": names, "expect": expect}


def _selftest() -> int:
    checks = []
    arr, legend, spec = _synth_id()
    names, expect = spec["names"], spec["expect"]
    camera = {"view": "selftest", "az_deg": 0.0, "el_deg": 0.0, "roll_deg": 0.0,
              "ortho": True, "res": [arr.shape[1], arr.shape[0]]}
    model = {"source": "selftest", "path": "synthetic", "sha256_16": "0" * 16,
             "normalized_dim_m": [1.0, 1.0, 1.0], "component_names": names}
    truth = extract_truth(arr, legend, camera, model,
                          shape_classes={n: c for n, c in zip(names,
                                                              ("rect", "circle", "ring",
                                                               "rod", "rect"))},
                          sources={n: "semantic" for n in names},
                          anchor="disc", anchor_size_m=10.5, contact_tol=2.0)
    by = truth["components_by_name"]

    checks.append(("连通域计数", truth["component_count"] == 5,
                   "可见 %d 件" % truth["component_count"]))
    checks.append(("未声明色→背景", truth["id_match"]["unmatched_pixels"] == expect["pixels"]["undeclared"],
                   "未匹配 %d px（期望 %d）" % (truth["id_match"]["unmatched_pixels"],
                                                expect["pixels"]["undeclared"])))
    ok_bbox = all(by[n]["bbox_xyxy"] == box for n, box in expect["bbox"].items())
    checks.append(("包围盒", ok_bbox,
                   "；".join("%s=%s" % (n, by[n]["bbox_xyxy"]) for n in names)))
    ok_px = all(by[n]["pixel_count"] == p for n, p in expect["pixels"].items()
                if n in by and n != "undeclared")
    checks.append(("像素计数", ok_px,
                   "；".join("%s=%d" % (n, by[n]["pixel_count"]) for n in names)))
    total = sum(c["pixel_count"] for c in truth["components"])
    area_sum = sum(c["area_fraction"] for c in truth["components"])
    checks.append(("面积分数归一", abs(area_sum - 1.0) < 1e-5
                   and abs(by["box"]["area_fraction"] - by["box"]["pixel_count"] / total) < 1e-6,
                   "Σarea_fraction=%.6f" % area_sum))
    hf_want = 181.0 / expect["total_height"]
    checks.append(("高度分数",
                   abs(by["box"]["height_fraction"] - hf_want) < 1e-6
                   and truth["total_payload_height_px"] == expect["total_height"],
                   "整器高 %d px（期望 %d）｜box=%.4f"
                   % (truth["total_payload_height_px"], expect["total_height"],
                      by["box"]["height_fraction"])))
    a, b = expect["touching"]
    contact_names = {c["part"] for c in by[a]["contacts"]}
    gap = next((c["gap_px"] for c in by[a]["contacts"] if c["part"] == b), None)
    checks.append(("接触关系", b in contact_names and gap is not None and gap <= 2.0,
                   "%s-%s gap=%s" % (a, b, gap)))
    checks.append(("孤岛数（环带孔仍 1；两块同色＝2）",
                   all(by[n]["islands"] == v for n, v in expect["islands"].items()),
                   "；".join("%s=%d" % (n, by[n]["islands"]) for n in names)))
    anchor_ok = by["disc"]["anchor_ratio"] is not None and \
        abs(by["disc"]["anchor_ratio"] - 1.0) < 1e-6 and truth["anchor"]["size_m"] == 10.5
    checks.append(("锚件比率", anchor_ok,
                   "disc anchor_ratio=%s" % by["disc"]["anchor_ratio"]))
    src_ok = all(c["source"] == "semantic" for c in truth["components"])
    checks.append(("语义来源标记", src_ok, "全件 semantic"))

    pal_ok = all(palette_color(i) == tuple(legend[i]) for i in range(len(legend)))
    checks.append(("调色板可复算", pal_ok, "%d 色一致" % len(legend)))

    with tempfile.TemporaryDirectory() as tmp:
        legend_path = os.path.join(tmp, "id_legend.json")
        write_json(legend_path, {"legend": [{"index": i, "rgb": list(legend[i]), "name": names[i]}
                                            for i in range(len(names))]})
        back = read_json(legend_path)["legend"]
        rt_ok = (all(tuple(e["rgb"]) == palette_color(e["index"]) for e in back)
                 and [e["name"] for e in back] == names)
        checks.append(("id_legend 往返", rt_ok, "写盘再读回一致"))

        from PIL import Image

        png = os.path.join(tmp, "id.png")
        save_rgb(arr, png)
        with Image.open(png) as im:
            back_arr = np.asarray(im.convert("RGB"), dtype=np.uint8)
        checks.append(("ID 图落盘往返", np.array_equal(back_arr, arr), os.path.basename(png)))

        spec_path = os.path.join(tmp, "bad.yaml")
        with open(spec_path, "w", encoding="utf-8") as fh:
            fh.write("parts: []\n")
        try:
            parse_assemble_spec(spec_path)
            checks.append(("空 parts 报错", False, "未报错"))
        except ImgCmpError as exc:
            checks.append(("空 parts 报错", "non-empty 'parts'" in str(exc), str(exc)))

    return selftest_report(TOOL, checks)


# ---------------------------------------------------------------- CLI

def build_parser():
    parser = make_parser(
        TOOL,
        "Reference generator: render a 3D asset (or an assembled configuration) into a "
        "scored synthetic benchmark - per-part ID map, silhouette, white CAD and black "
        "render images, plus a truth JSON whose geometry comes from the ID image alone. "
        "Blender runs headless; truth extraction stays in plain python.",
    )
    parser.add_argument("--model", metavar="FILE",
                        help="a single .glb/.gltf/.stl to render (mutually exclusive with --assemble)")
    parser.add_argument("--assemble", metavar="SPEC.YAML",
                        help="assemble multiple part files into a known configuration")
    parser.add_argument("--out", metavar="DIR", help="output directory")
    parser.add_argument("--views", metavar="LIST",
                        help="comma list drawn from white,black,id,silhouette (default: all; "
                             "'id' is mandatory because truth is derived from it)")
    parser.add_argument("--angles", metavar="A", default=None,
                        help="semicolon list of 'az,el[,roll]' poses (default: 0,0,0)")
    parser.add_argument("--az-el-grid", metavar="G", default=None,
                        help="pose grid: 'AZ_STEP,EL_STEP' for a 3x3 grid around zero, or "
                             "'AZ0,AZ1,AZ_STEP,EL0,EL1,EL_STEP' (replaces the default pose)")
    parser.add_argument("--res", metavar="W,H", default="1200,900",
                        help="render resolution (default: 1200,900)")
    parser.add_argument("--ortho", action="store_true", help="orthographic camera")
    parser.add_argument("--focal", type=float, default=DEFAULT_FOCAL_MM, metavar="MM",
                        help="perspective focal length in mm (default: 50)")
    parser.add_argument("--scale-to", type=float, default=None, metavar="M",
                        help="uniformly scale the model so its longest dimension is M metres")
    parser.add_argument("--truth", "--truth-truth", dest="truth", metavar="SPEC.YAML",
                        help="semantic declarations (shape_class per part name pattern, anchor)")
    parser.add_argument("--contact-tol", type=float, default=CONTACT_TOL_PX, metavar="PX",
                        help="bounding-box gap in px still counted as touching (default: 2)")
    parser.add_argument("--timeout", type=float, default=3600.0, metavar="S",
                        help="Blender wall-clock limit in seconds (default: 3600)")
    return parser


def main(argv=None) -> int:
    return run_main(TOOL, _run, argv)


def _run(argv) -> int:
    args = build_parser().parse_args(argv)
    if args.selftest:
        return _selftest()
    result = build_views(args)
    _print_report(result)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
