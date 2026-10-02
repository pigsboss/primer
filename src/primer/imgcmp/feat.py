# -*- coding: utf-8 -*-
"""primer-imgfeat —— T2a 组件特征表（imgcmp 工具链第三件）。

**用途**：输入单张图（白底 CAD 截图或黑底渲染图）＋可选的锚件定义，输出**组件特征表**
（外接框、面积占比、相对锚件尺寸比、高度占载荷总高分数、形状类、相互接触/间隙关系）；
再把参考表与我方表**逐件对照**，输出差异对照表，供 H1 差异分诊直接消化。

全部处理都是**确定性图像处理**：阈值分割（Otsu）→ 连通域（行程并查集）→ 轮廓描述子
（矩形度/圆度/环带性/长宽比）→ 锚件比率。**不含任何需要训练或外部服务的模型**，
只依赖 numpy 与 Pillow（仓库既有依赖，不引新依赖）。

**口径**（判据与阈值，机器可读版见 JSON 的 ``method`` 键）：

- **底别**：``white`` 白底（图元为深色墨）／``black`` 黑底（对象为亮部），由边框带中位
  亮度判定，可用 ``--kind`` 强制。前景掩膜 = 亮度 Otsu 阈值 + 底别极性
  （白底取 ``luma <= t``、黑底取 ``luma >= t``）。
- **组件**：前景掩膜的 **8 邻接连通域**（行程并查集），面积 ≥ ``--min-area`` 保留。
  ``--min-area`` 缺省 ``auto`` = ``max(64, 0.0005 · W · H)``——该缺省值是对
  refgen 合成真值组（``out/still/imgcmp/refgen/`` 各机位：白底 19 ＋ 黑底 19 = 38 例）
  做过枚举对比后取的"组件数与真值完全一致"率最高的口径（17/38），不是拍脑袋。
- **载荷总高**：``total_payload_height_px`` = 保留组件像素并集在 y 方向的总跨度（顶到底）。
- **高度分数** = 该件 bbox 竖直跨度 ÷ 载荷总高；**面积分数** = 该件像素 ÷ 保留组件像素总数。
- **形状类**（``ring|polygon|circle|rod|rect|other``，按轮廓描述子判定，依次短路）：
  1. ``ring``：该件自身含孔洞，且最大孔洞面积 ≥ 0.15 × 该件面积（环带性）；
  2. ``polygon``：凸包抽稀后残留 5–10 条直边、残差 ≤ 0.035（面积归一）、凸包充实度
     ≥ 0.97——正多边形板（六边形主镜这类）。``polygon_name`` 另给
     ``pentagon/hexagon/heptagon/octagon/nonagon``；
  3. ``circle``：``fill = 4A/(π·w·h) ∈ [0.88, 1.12]`` 且长宽比 ``≤ 1.25``（圆度；
     实心圆的 ``fill = 1``，实心矩形 ``fill = 4/π ≈ 1.27``，两者因此可分。**正六边形
     与圆单纯看圆度＋长宽比不可分**——见 ``polygon`` 判据）；
  4. ``rod``：长宽比 ``≥ 3.5``（细长且非规整矩形：斜置杆、圆柱/锥形投影）；
  5. ``rect``：矩形度 ``A/(w·h) ≥ 0.90``（规整矩形——**含规则矩形长条**）；
  6. ``other``：其余。
- **孔洞/开口**：背景的 4 邻接连通域中**不接触图像边框**者即孔洞，按 4 邻接归属到包围它的
  组件。每个孔洞另给描述子（矩形度、``4A/(πwh)``），用于判"开口"的截面形状类。
- **接触**：两件 bbox 间隙 ≤ ``--contact-tol``（缺省 2 px）即记为相接；``gap_px`` 为
  ``common.box_gap``（相交为负），另给 ``bbox_overlap`` 布尔。
- **锚件**：只走配置文件（工具内零任务数值）：``anchors: [{name, ref_m, selector}]``，
  ``selector.rule ∈ {largest, bbox_longest, position}``。``anchor_ratio`` = 该件最大跨度 ÷
  锚件最大跨度（与 refgen 真值同口径）。示例见
  ``tests/fixtures/imgcmp/anchors/observatory_2034.yaml``。
- **color_groups**：图像为"平色块"时按主色归并的件数（色族数），非平色图给 ``null``。
  平色块判据：前景像素中，少数几个**精确 RGB 值**（每种占比 ≥ 2%）合计覆盖
  ≥ 85% 前景——CAD 平色截图满足，连续调渲染图不满足。

**产物**（``--out`` 目录）：组件特征表 JSON、差异对照表 JSON，以及中文人读报告（stdout）。

语言纪律（docs/guides/CODING_STANDARDS.md v1.1）：stdout 只出中文人读报告；
stderr／异常／选项名／JSON 键一律英文。
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import math
import os
import tempfile

import numpy as np
import yaml
from PIL import Image, ImageDraw

from .common import (
    ImgCmpError,
    KIND_BLACK,
    KIND_WHITE,
    box_gap,
    box_iou,
    detect_kind,
    ensure_dir,
    load_rgb,
    make_parser,
    report,
    run_main,
    selftest_report,
    to_gray,
    write_json,
)

TOOL = "primer.imgcmp.feat"

KIND_LABEL = {KIND_WHITE: "白底（CAD/论文截图）", KIND_BLACK: "黑底（空间渲染）"}
SHAPE_ORDER = ("ring", "polygon", "circle", "rod", "rect", "other")
SHAPE_CN = {"rect": "矩形", "circle": "圆", "ring": "环", "rod": "杆", "other": "其他"}

# ---------------------------------------------------------------- 口径常量（写进 JSON.method）

# 开口（筒口/结构孔）判据：孔洞面积 ≥ max(16 px, OPENING_MIN_FRAC × 该件面积) 才算"开口"；
# 再按 bbox 中心是否落在该件顶部 OPENING_TOP_FRAC 的带内，分出"顶带开口"。
OPENING_MIN_FRAC = 0.005
OPENING_TOP_FRAC = 0.35

# 分割：背景带统计取自边框带（common.border_luma 同款取法），阈值 = median ∓ max(floor, k·MAD)。
# MAD 是抗离群的离散度：白底纸面有噪点时自动放宽，纯色底（MAD=0）退回 floor。
BG_MARGIN_K = 6.0
BG_MARGIN_FLOOR = 8.0
BG_BAND_PAD = 4
# 背景相对阈值若把整幅图都吃成前景（> 该比例）或吃空，视为退化 → 回退 Otsu。
MAX_FG_FRAC = 0.5

# 图注（"(a)" 这类文字）判据：小面积 ＋ 高填充 ＋ 落在图幅边缘带或与任何"大件"不相接。
ANNOT_MAX_FRAC = 0.02
ANNOT_MIN_EXTENT = 0.30
ANNOT_BAND_Y = 0.20
ANNOT_BAND_X = 0.15

# 细长件判据（开口归属用）：与 shape_rules 里的 rod 同一切口。
SLENDER_ELONGATION = 3.5

# 多边形（正多边形板：六边形主镜这类）判据。**为什么需要它**：正六边形的
# `circle_fill = 4A/(πwh)` = 1.103 落在 circle 的 [0.88,1.12] 带内，而六重对称让 PCA
# 涨宽比 = 1.00、圆盘也一样——只用"圆度 + 长宽比"两个量，正六边形与圆**数学上不可分**。
# 补的量是**轮廓的直线段结构**：对轮廓凸包做顶点抽稀，看"几条直边 + 拟合残差"。
#   · 正六边形：抽稀出 6 个顶点、残差 ~0.01；
#   · 圆盘：同一容差下要把弧拆成 16–19 段才达标（弦高 sagitta ≈ rθ²/8 ≤ 3.5%·r ⇒ θ ≈ 30°），
#     所以"边数上限 10"这一条就能把圆排除，圆仍判 circle。
POLY_MIN_VERTICES = 5          # 少于 5 边不进多边形（四边形归 rect，三角形归 other）
POLY_MAX_VERTICES = 10         # 多于 10 边视为"圆周采样"而非多边形（圆 16–19 段）
POLY_SAGITTA_TOL = 0.035       # 抽稀容差：弦高 ≤ 3.5% × 等效半径（像素阶梯噪声所需）
POLY_MAX_RESIDUAL = 0.035      # 拟合残差（面积归一）≤ 3.5%（构造上 ≤ 抽稀容差）
POLY_MIN_SOLIDITY = 0.97       # 凸包充实度：非凸件（星形/桁架）不冒充多边形
POLY_NAMES = {5: "pentagon", 6: "hexagon", 7: "heptagon", 8: "octagon", 9: "nonagon"}

# 边缘切分：相邻流域的对比度 `min(峰值) - 谷脊` 浅于它（亮度单位）就并回去——同一实体
# 表面的纹理不该被当成两件；真正分开的两件之间必有一条明显的暗缝/明暗分界。
MERGE_CONTRAST = 16.0

# `--merge-large-blobs`（缺省关）用的两条附加判据：**大块表面的明暗起伏**（小行星、陨石这类
# 有纹理的整器）会被分水岭切成几块，但它们之间的谷脊很浅且面积悬殊——小块贴着大块。
# 同时满足"谷脊 < MERGE_LARGE_REL × 较低峰值"与"面积比 ≤ MERGE_LARGE_AREA_RATIO"才并。
MERGE_LARGE_REL = 0.6
MERGE_LARGE_AREA_RATIO = 0.5

METHOD = {
    "segmentation": "two-stage split (default strategy 'inclusive+edge_split'): "
                    "(1) SUPPORT = background-relative threshold t = median(border band) +/- "
                    "max(BG_MARGIN_FLOOR, BG_MARGIN_K*MAD) - it only answers 'unlike the "
                    "background', so the payload's dim/bright parts are never dropped; "
                    "(2) SEEDS = Otsu threshold INTERSECTED with the support, keeping only "
                    "seed cores of area >= min_area; "
                    "(3) SPLIT: every support component is watershed-flooded from its seeds "
                    "in descending |luma - background| order, so the watershed line falls on "
                    "the valley between two cores (the dark seam / shading break between "
                    "parts); support components with 0 or 1 seed stay whole, which is the "
                    "'dim part is never lost' fallback; "
                    "(4) MERGE: adjacent basins whose contrast min(peak) - saddle is below "
                    "MERGE_CONTRAST are merged back, so surface texture of one part is not "
                    "mistaken for two parts. Single-threshold masks are used only for "
                    "--threshold (strategy 'explicit'). A single global threshold cannot do "
                    "both jobs: it either drops a part at 28 luma against a 0 background or "
                    "lets the glow bridge separate struts into one blob.",
    "mask_strategy_values": ["inclusive+edge_split", "otsu", "explicit"],
    "merge_large_blobs": "optional switch (default off): besides the absolute ridge "
                         "contrast, also merge two adjacent basins when the ridge is shallower "
                         "than MERGE_LARGE_REL (%.2f) times the lower peak AND their area ratio "
                         "is <= MERGE_LARGE_AREA_RATIO (%.2f) - i.e. a small facet sitting on a "
                         "big one. It rescues over-splitting of a textured single body (an "
                         "asteroid) and can only ever MERGE, never split."
                         % (MERGE_LARGE_REL, MERGE_LARGE_AREA_RATIO),
    "segmentation_constants": {"BG_MARGIN_K": BG_MARGIN_K,
                               "BG_MARGIN_FLOOR": BG_MARGIN_FLOOR,
                               "BG_BAND_PAD": BG_BAND_PAD,
                               "MAX_FG_FRAC": MAX_FG_FRAC},
    "components": "8-connected components of the foreground mask, area >= min_area; "
                  "each kept region is classed as 'component' or 'annotation'",
    "min_area_default": "auto = max(32, 0.0002 * W * H)",
    "annotation": "a kept region is an 'annotation' (caption text such as '(a)') when all of: "
                  "area <= ANNOT_MAX_FRAC * foreground pixels; bbox extent >= ANNOT_MIN_EXTENT; "
                  "bbox lies entirely in the outer band (top/bottom ANNOT_BAND_Y of H, "
                  "left/right ANNOT_BAND_X of W); and it does not touch any region at least "
                  "10x its own area. Annotations are counted separately from components.",
    "annotation_constants": {"ANNOT_MAX_FRAC": ANNOT_MAX_FRAC,
                             "ANNOT_MIN_EXTENT": ANNOT_MIN_EXTENT,
                             "ANNOT_BAND_Y": ANNOT_BAND_Y,
                             "ANNOT_BAND_X": ANNOT_BAND_X},
    "total_payload_height": "y-span of the union of kept component pixels (top to bottom)",
    "height_fraction": "component bbox y-span / total_payload_height_px",
    "area_fraction": "component pixel count / kept component pixel count",
    "shape_rules": [
        "ring: max hole area >= 0.15 * component area",
        "polygon: convex-hull decimation gives 5-10 straight edges, residual <= 0.035 "
        "(area-normalised) and solidity >= 0.97; a circle needs 16-19 segments at the same "
        "sagitta tolerance (3.5% x equivalent radius), so it never reaches <= 10 edges and "
        "stays 'circle'. polygon_name carries pentagon/hexagon/heptagon/octagon/nonagon",
        "circle: 4A/(pi*w*h) in [0.88, 1.12] and orientation-free aspect <= 1.25",
        "rod: orientation-free aspect (PCA principal extents ratio) >= 3.5",
        "rect: A/(w*h) >= 0.90",
        "other: otherwise",
    ],
    "pca_aspect": "elongation = sqrt(lambda1/lambda2) of the pixel coordinate covariance; "
                  "orientation-free (equals w/h for a filled rectangle, 1.0 for a disc). "
                  "3.5 is the 'slender' cut; it captures the refgen pair's declared rod "
                  "(elongation 3.96) while the wing panels (3.3-3.4) stay rect.",
    "hole": "4-connected background components not touching the image border; "
            "assigned to the component 4-adjacent to them",
    "opening": "a hole counted as an 'opening' when its area >= max(16 px, %.3f * component "
               "area); top_band openings additionally have their bbox centre in the top %.0f%% "
               "of the component bbox (the tube-mouth region of a payload)"
               % (OPENING_MIN_FRAC, OPENING_TOP_FRAC * 100),
    "opening_host": "each opening is attributed to the slender component (orientation-free "
                    "aspect >= SLENDER_ELONGATION) whose bbox contains its centre, else to the "
                    "component it is enclosed by; the top-level openings[] list carries "
                    "host_component so 'openings per tube' can be counted",
    "slender_elongation": SLENDER_ELONGATION,
    "contact": "bbox gap <= contact_tol_px (common.box_gap; negative means bbox overlap)",
    "anchor_ratio": "component max bbox extent / anchor max bbox extent",
    "flat_color": "exact RGB values each covering >= 2% of foreground pixels cover >= 85% "
                  "of the foreground -> flat-color CAD; color_groups is the number of "
                  "colour families otherwise null",
    "no_model": "deterministic image processing only; no trained model, no external service",
}




# ---------------------------------------------------------------- 基础图像运算

def otsu_threshold(gray: np.ndarray) -> int:
    """Otsu 阈值（0–255）。常量图退化为 128（不抛错，交由上层按空前景处理）。"""
    hist, _ = np.histogram(np.asarray(gray, dtype=np.uint8), bins=256, range=(0, 256))
    total = float(hist.sum())
    if total <= 0.0:
        return 128
    w0 = np.cumsum(hist).astype(np.float64)
    m0 = np.cumsum(hist * np.arange(256)).astype(np.float64)
    w1 = total - w0
    with np.errstate(invalid="ignore", divide="ignore"):
        between = (m0[-1] * w0 / total - m0) ** 2 / (w0 * w1)
    between = np.nan_to_num(between, nan=-1.0, posinf=-1.0, neginf=-1.0)
    best = float(between.max())
    if best <= 0.0:
        return 128
    # 双峰平直分布（如纯背景＋纯前景两色）会让多档阈值同分；取同分平台的中位，避免落到
    # 平台边沿（平台左端会把背景整片吃成前景）。
    plateau = np.nonzero(between >= best - max(1e-9, best * 1e-6))[0]
    return int(np.median(plateau))


def background_stats(gray: np.ndarray, pad: int = BG_BAND_PAD):
    """边框带的中位亮度与 MAD（抗离群离散度）。取法与 ``common.border_luma`` 一致。"""
    if gray.ndim != 2:
        gray = to_gray(gray)
    height, width = gray.shape
    pad = max(1, min(int(pad), max(1, min(height, width) // 4)))
    band = np.concatenate([
        gray[:pad, :].ravel(), gray[-pad:, :].ravel(),
        gray[:, :pad].ravel(), gray[:, -pad:].ravel(),
    ]).astype(np.float64)
    median = float(np.median(band))
    mad = float(np.median(np.abs(band - median)))
    return median, mad


def background_threshold(gray: np.ndarray, kind: str) -> int:
    """背景相对阈值：白底 ``median - margin``、黑底 ``median + margin``。

    ``margin = max(BG_MARGIN_FLOOR, BG_MARGIN_K · MAD)``——纯色底（MAD=0）退回 floor，
    有噪点的扫描底自动放宽。它只回答"和背景不一样"，所以对象自身偏暗/偏亮的部分也在内。
    """
    median, mad = background_stats(gray)
    margin = max(BG_MARGIN_FLOOR, BG_MARGIN_K * mad)
    if kind == KIND_WHITE:
        return int(np.clip(median - margin, 0, 255))
    if kind == KIND_BLACK:
        return int(np.clip(median + margin, 0, 255))
    raise ImgCmpError("kind must be 'white' or 'black', got: %r" % (kind,))


def _mask_for(gray: np.ndarray, kind: str, thr: int) -> np.ndarray:
    if kind == KIND_WHITE:
        return gray <= thr
    if kind == KIND_BLACK:
        return gray >= thr
    raise ImgCmpError("kind must be 'white' or 'black', got: %r" % (kind,))


def foreground_mask(rgb: np.ndarray, kind: str, threshold: int | None = None,
                    bg_source: np.ndarray | None = None):
    """返回 ``(mask, threshold, source)``；``source`` ∈ ``explicit``/``background``/``otsu``。

    **单阈值掩膜**（`--threshold` 与退化回退走这条）。默认的两段式分件见 :func:`segment`。
    ``bg_source`` 给"背景统计该看哪张图"——限 ROI 分析时看整幅图（ROI 可能全落在对象内）。
    """
    gray = to_gray(rgb)
    if threshold is not None:
        thr = int(threshold)
        if not 0 <= thr <= 255:
            raise ImgCmpError("threshold must be in 0..255, got: %r" % (threshold,))
        mask = _mask_for(gray, kind, thr)
        if not mask.any():
            raise ImgCmpError("foreground mask is empty at --threshold %d" % thr)
        return mask, thr, "explicit"
    bg_gray = gray if bg_source is None else to_gray(bg_source)
    thr = background_threshold(bg_gray, kind)
    mask = _mask_for(gray, kind, thr)
    fraction = float(mask.mean())
    if fraction > 0.0 and fraction <= MAX_FG_FRAC:
        return mask, thr, "background"
    otsu = otsu_threshold(gray)
    fallback = _mask_for(gray, kind, otsu)
    return fallback, otsu, "otsu"


def watershed_from_seeds(score: np.ndarray, region: np.ndarray, seeds: np.ndarray):
    """从种子做**降序优先泛滥**（bucket-free，用 ``heapq``）：高亮（或高对比）像素先淹。

    结果把 ``region`` 的每个像素判给"沿最高路径最近的那个种子"；两种子相遇处即分水岭线，
    落在两者之间的**谷脊**上——这正是"按零件间暗缝切分"的实现。
    """
    import heapq

    height, width = score.shape
    labels = seeds.astype(np.int32, copy=True)
    ys, xs = np.nonzero(seeds)
    heap = [(-float(score[y, x]), int(y) * width + int(x)) for y, x in zip(ys, xs)]
    heapq.heapify(heap)
    while heap:
        _neg, flat = heapq.heappop(heap)
        y, x = divmod(flat, width)
        label = labels[y, x]
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                ny, nx = y + dy, x + dx
                if 0 <= ny < height and 0 <= nx < width and region[ny, nx] \
                        and labels[ny, nx] == 0:
                    labels[ny, nx] = label
                    heapq.heappush(heap, (-float(score[ny, nx]), ny * width + nx))
    return labels


def merge_flat_basins(labels: np.ndarray, score: np.ndarray, contrast: float,
                      large_rel: float = 0.0, area_ratio: float = 0.0) -> np.ndarray:
    """把"山谷很浅"的相邻流域并回去。

    分水岭只会按核切，**同一实体表面上的纹理/明暗起伏**也会被切成几块（过切）。
    判据：相邻流域 A、B 的 `contrast = min(peak_A, peak_B) - saddle(A,B)`，
    `saddle` 取两流域交界处的最高分。浅于 ``contrast`` 的一律合并——真正分开的两件之间
    必有一条明显的暗缝/明暗分界，山谷不会浅。
    """
    n = int(labels.max())
    if n <= 1 or contrast <= 0:
        return labels
    flat = labels.ravel()
    peaks = np.zeros(n + 1, dtype=np.float64)
    np.maximum.at(peaks, flat, score.ravel())
    areas = np.bincount(flat, minlength=n + 1).astype(np.float64)
    areas[0] = 0.0
    pairs = {}

    def collect(shift_y, shift_x):
        a = labels[max(0, shift_y):labels.shape[0] + min(0, shift_y),
                   max(0, shift_x):labels.shape[1] + min(0, shift_x)]
        b = labels[max(0, -shift_y):labels.shape[0] + min(0, -shift_y),
                   max(0, -shift_x):labels.shape[1] + min(0, -shift_x)]
        s = score[max(0, shift_y):labels.shape[0] + min(0, shift_y),
                  max(0, shift_x):labels.shape[1] + min(0, shift_x)]
        sel = (a > 0) & (b > 0) & (a != b)
        if not sel.any():
            return
        for u, v, val in zip(a[sel].tolist(), b[sel].tolist(), s[sel].tolist()):
            key = (u, v) if u < v else (v, u)
            if val > pairs.get(key, -1.0):
                pairs[key] = val

    for sy, sx in ((0, 1), (1, 0), (1, 1), (1, -1)):
        collect(sy, sx)

    parent = list(range(n + 1))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return int(a)

    for (u, v), saddle in sorted(pairs.items(), key=lambda kv: kv[1]):
        ru, rv = find(u), find(v)
        if ru == rv:
            continue
        floor = min(peaks[ru], peaks[rv])
        small, big = sorted((areas[ru], areas[rv]))
        shallow = floor - saddle < contrast
        if not shallow and large_rel > 0.0:
            shallow = (big > 0 and small / big <= area_ratio
                       and floor - saddle < large_rel * floor)
        if shallow:
            keep, drop = min(ru, rv), max(ru, rv)
            parent[drop] = keep
            peaks[keep] = max(peaks[ru], peaks[rv])
            areas[keep] = areas[ru] + areas[rv]
    roots = np.array([find(i) for i in range(n + 1)], dtype=np.int32)
    return roots[labels]


def segment(rgb: np.ndarray, kind: str, threshold: int | None = None,
            bg_source: np.ndarray | None = None, min_area: int = 1,
            merge_large: bool = False) -> dict:
    """两段式分件：**包含性支撑掩膜** + **边缘（分水岭）切分**。

    1. **支撑掩膜**（inclusive）：背景相对阈值 ``median ∓ max(floor, k·MAD)``——只回答
       "和背景不一样"，所以对象自身很暗/很亮的部分都在内，不会像全局 Otsu 那样把暗件丢掉。
    2. **种子核**：Otsu 阈值 ∩ 支撑掩膜。核回答的是"哪里是典型零件实体"，噪声/辉光不在内。
    3. **切分**：对含 ≥2 个核的支撑连通域做降序分水岭（:func:`watershed_from_seeds`），
       分水岭线落在两核之间的谷脊上；只含 1 个核的连通域整体成一件；**一个核都没有的
       支撑连通域整块保留为一件**（这就是"暗件不被丢"的兜底）。

    ``--threshold`` 显式给定时走**单阈值**（不做切分），保持它作为逃生口的语义。
    返回 ``dict(mask, labels, n_labels, strategy, threshold, otsu, support_threshold, seeds)``。
    """
    gray = to_gray(rgb)
    bg_gray = gray if bg_source is None else to_gray(bg_source)
    otsu = otsu_threshold(gray)

    if threshold is not None:
        thr = int(threshold)
        if not 0 <= thr <= 255:
            raise ImgCmpError("threshold must be in 0..255, got: %r" % (threshold,))
        mask = _mask_for(gray, kind, thr)
        if not mask.any():
            raise ImgCmpError("foreground mask is empty at --threshold %d" % thr)
        labels, n = label_runs(mask, 8)
        return {"mask": mask, "labels": labels, "n_labels": n, "strategy": "explicit",
                "threshold": thr, "otsu": otsu, "support_threshold": thr, "seeds": None,
                "background_luma": background_stats(bg_gray)}

    support_thr = background_threshold(bg_gray, kind)
    support = _mask_for(gray, kind, support_thr)
    fraction = float(support.mean())
    if fraction <= 0.0 or fraction > MAX_FG_FRAC:
        mask = _mask_for(gray, kind, otsu)
        labels, n = label_runs(mask, 8)
        return {"mask": mask, "labels": labels, "n_labels": n, "strategy": "otsu",
                "threshold": otsu, "otsu": otsu, "support_threshold": support_thr, "seeds": 0,
                "background_luma": background_stats(bg_gray)}

    seed_mask = support & _mask_for(gray, kind, otsu)
    seed_labels, _n_raw_seeds = label_runs(seed_mask, 8)
    # 种子核面积下限 = `--min-area`：CAD 线稿会在实体内部造出一堆几像素的"核"，
    # 若全部当种子，分水岭会把一件碎成几十件（过切）。小于该门限的核不参与定件，
    # 它们的像素照样在支撑掩膜内，被相邻流域吸收。
    if min_area > 1:
        seed_sizes = np.bincount(seed_labels.ravel(), minlength=1)
        small = np.nonzero((seed_sizes < min_area) & (seed_sizes > 0))[0]
        if small.size:
            seed_labels[np.isin(seed_labels, small)] = 0
    kept_seeds = np.unique(seed_labels[seed_labels > 0])
    remap_seed = np.zeros(int(seed_labels.max()) + 1, dtype=np.int32)
    remap_seed[kept_seeds] = np.arange(1, kept_seeds.size + 1, dtype=np.int32)
    seed_labels = remap_seed[seed_labels]
    n_seeds = int(kept_seeds.size)
    support_labels, n_support = label_runs(support, 8)

    score = np.abs(gray.astype(np.float32) - float(background_stats(bg_gray)[0]))
    labels = np.zeros(support.shape, dtype=np.int32)
    next_label = 0
    for region_id in range(1, n_support + 1):
        region = support_labels == region_id
        inside = seed_labels[region]
        ids = np.unique(inside)
        ids = ids[ids > 0]
        if ids.size == 0:
            next_label += 1
            labels[region] = next_label
            continue
        if ids.size == 1:
            next_label += 1
            labels[region] = next_label
            continue
        local_seeds = np.where(region, seed_labels, 0)
        territories = watershed_from_seeds(score, region, local_seeds)
        remap = np.zeros(int(territories.max()) + 1, dtype=np.int32)
        for seed_id in ids:
            next_label += 1
            remap[int(seed_id)] = next_label
        filled = territories > 0
        labels[filled] = remap[territories[filled]]
    labels = merge_flat_basins(labels, score, MERGE_CONTRAST,
                               large_rel=MERGE_LARGE_REL if merge_large else 0.0,
                               area_ratio=MERGE_LARGE_AREA_RATIO)
    present = np.unique(labels[labels > 0])
    compact = np.zeros(int(labels.max()) + 1, dtype=np.int32)
    compact[present] = np.arange(1, present.size + 1, dtype=np.int32)
    labels = compact[labels]
    next_label = int(present.size)
    return {"mask": labels > 0, "labels": labels, "n_labels": next_label,
            "strategy": "inclusive+edge_split", "threshold": support_thr, "otsu": otsu,
            "support_threshold": support_thr, "seeds": int(n_seeds),
            "background_luma": background_stats(bg_gray)}


def _row_runs(mask: np.ndarray):
    """逐行前景游程：返回 ``(y, x0, x1)``，x0 闭、x1 开，按 (y, x) 升序。"""
    height, width = mask.shape
    if height == 0 or width == 0:
        empty = np.zeros(0, dtype=np.int64)
        return empty, empty.copy(), empty.copy()
    pad = np.zeros((height, width + 2), dtype=np.int8)
    pad[:, 1:width + 1] = np.asarray(mask, dtype=np.int8)
    diff = np.diff(pad, axis=1)
    sy, sx = np.nonzero(diff == 1)
    ey, ex = np.nonzero(diff == -1)
    if sy.size != ey.size or not np.array_equal(sy, ey):
        raise ImgCmpError("internal error: run pairing mismatch")
    # pad[:, k] == mask[:, k-1]，故 pad 差分在 k 处 +1 ⇒ 游程起于原图第 k 列、止于第 k 列（开）。
    return sy.astype(np.int64), sx.astype(np.int64), ex.astype(np.int64)


def label_runs(mask: np.ndarray, connectivity: int = 8):
    """行程并查集连通域标注。返回 ``(labels>0 的 int32 数组, 域数)``。"""
    if connectivity not in (4, 8):
        raise ImgCmpError("connectivity must be 4 or 8, got: %r" % (connectivity,))
    height = mask.shape[0]
    y, x0, x1 = _row_runs(mask)
    labels = np.zeros(mask.shape, dtype=np.int32)
    count = int(y.size)
    if count == 0:
        return labels, 0
    parent = np.arange(count, dtype=np.int64)

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return int(a)

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra == rb:
            return
        if ra < rb:
            parent[rb] = ra
        else:
            parent[ra] = rb

    edge = 0 if connectivity == 8 else 1
    row_start = np.searchsorted(y, np.arange(height + 1))
    for r in range(height - 1):
        ia, ia_end = int(row_start[r]), int(row_start[r + 1])
        ib, ib_end = int(row_start[r + 1]), int(row_start[r + 2])
        while ia < ia_end and ib < ib_end:
            if x0[ib] > x1[ia] - edge:
                ia += 1
            elif x0[ia] > x1[ib] - edge:
                ib += 1
            else:
                union(ia, ib)
                if x1[ia] <= x1[ib]:
                    ia += 1
                else:
                    ib += 1
    roots = np.fromiter((find(i) for i in range(count)), dtype=np.int64, count=count)
    _uniq, inverse = np.unique(roots, return_inverse=True)
    inverse = inverse.astype(np.int32)
    for i in range(count):
        labels[y[i], x0[i]:x1[i]] = inverse[i] + 1
    return labels, int(inverse.max()) + 1


def hole_masks(mask: np.ndarray):
    """孔洞 = 不接触图像边框的背景 4 邻接连通域。返回 ``(background_flags, hole_labels, n)``。"""
    background = ~np.asarray(mask, dtype=bool)
    labels, count = label_runs(background, connectivity=4)
    if count == 0:
        return background, labels, 0
    border = np.concatenate([labels[0, :], labels[-1, :], labels[:, 0], labels[:, -1]])
    outside = np.unique(border[border > 0])
    if outside.size:
        labels[np.isin(labels, outside)] = 0
    kept = np.unique(labels[labels > 0])
    remap = np.zeros(int(labels.max()) + 1, dtype=np.int32)
    remap[kept] = np.arange(1, kept.size + 1, dtype=np.int32)
    return background, remap[labels], int(kept.size)


def _bbox_of(mask: np.ndarray):
    ys, xs = np.nonzero(mask)
    return [int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1]


# ---------------------------------------------------------------- 形状描述子

def classify_shape(descriptors: dict) -> str:
    """按轮廓描述子短路判定形状类（判据见模块 docstring / JSON.method）。"""
    if descriptors["hole_area_fraction"] >= 0.15:
        return "ring"
    if is_polygon(descriptors):
        return "polygon"
    if 0.88 <= descriptors["circle_fill"] <= 1.12 and descriptors["elongation"] <= 1.25:
        return "circle"
    if descriptors["elongation"] >= 3.5:
        return "rod"
    if descriptors["extent"] >= 0.90:
        return "rect"
    return "other"


def is_polygon(descriptors: dict) -> bool:
    """正多边形判据：凸包抽稀后 5–10 条直边、残差 ≤3.5%、凸包充实度 ≥0.97。"""
    vertices = descriptors.get("polygon_vertices")
    if vertices is None:
        return False
    return (POLY_MIN_VERTICES <= vertices <= POLY_MAX_VERTICES
            and descriptors.get("polygon_residual", 1.0) <= POLY_MAX_RESIDUAL
            and descriptors.get("solidity", 0.0) >= POLY_MIN_SOLIDITY)


def convex_hull(xs: np.ndarray, ys: np.ndarray):
    """Andrew 单调链凸包；返回逆时针顶点数组 ``(n, 2)``（自实现，不引 scipy）。"""
    # 凸包顶点只可能是"每一列 x 上的最高/最低像素"——先这样把候选压到 ~2W 个，
    # 再跑单调链（对整幅像素跑单调链在 Python 里太慢）。
    xs = xs.astype(np.int64)
    order = np.lexsort((ys, xs))
    xs, ys = xs[order], ys[order].astype(np.float64)
    if xs.size < 3:
        return np.stack([xs, ys], axis=1)
    uniq_x, first = np.unique(xs, return_index=True)
    last = np.append(first[1:] - 1, xs.size - 1)
    cand = np.concatenate([np.stack([xs[first], ys[first]], axis=1),
                           np.stack([xs[last], ys[last]], axis=1)])
    pts = np.unique(cand, axis=0)
    if pts.shape[0] < 3:
        return pts.astype(np.float64)

    def half(sorted_pts):
        out = []
        for point in sorted_pts:
            while len(out) >= 2:
                (x1, y1), (x2, y2) = out[-2], out[-1]
                if (x2 - x1) * (point[1] - y1) - (y2 - y1) * (point[0] - x1) <= 0:
                    out.pop()
                else:
                    break
            out.append(tuple(point))
        return out

    order = sorted((float(a), float(b)) for a, b in pts)
    lower, upper = half(order), half(order[::-1])
    return np.asarray(lower[:-1] + upper[:-1], dtype=np.float64)


def polygon_area(points: np.ndarray) -> float:
    if points.shape[0] < 3:
        return 0.0
    x, y = points[:, 0], points[:, 1]
    return float(abs(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))) / 2.0)


def decimate_closed(points: np.ndarray, tol: float, min_vertices: int = 3):
    """闭多边形抽稀（自实现的**贪心顶点抽稀**）：反复去掉"去掉代价最小且 ≤ tol"的顶点。

    代价＝该顶点到"前后邻点连线"的垂距。凸多边形上这等价于"找出真正的直边角点"：
    正六边形留下 6 个角点、圆盘要十几段才达标。每轮批量去掉一批低代价顶点，
    **同一轮内不允许去掉相邻两点**（含环首尾相邻）——栅格化会在一个尖角处留下两三个
    几乎共线的像素（如六边形左尖角 x=0 处的两个点），若不设这条限制，它们会因"彼此共线"
    被同时判为低代价而一起拿掉，等于把整个尖角用一条弦切平，正六边形因此被降成圆。
    十几轮收敛，纯 numpy，不引 scipy。
    """
    keep = np.ones(points.shape[0], dtype=bool)
    while int(keep.sum()) > min_vertices:
        idx = np.nonzero(keep)[0]
        n = idx.size
        prev, nxt = np.roll(idx, 1), np.roll(idx, -1)
        a, b, p = points[prev], points[nxt], points[idx]
        ab = b - a
        norm = np.hypot(ab[:, 0], ab[:, 1])
        cross = np.abs(ab[:, 0] * (p[:, 1] - a[:, 1]) - ab[:, 1] * (p[:, 0] - a[:, 0]))
        cost = np.where(norm < 1e-9, np.inf, cross / np.maximum(norm, 1e-9))
        if float(cost.min()) > tol:
            break
        room = n - min_vertices                 # 本轮最多能去几个
        drop = np.zeros(n, dtype=bool)
        picked = 0
        for j in np.argsort(cost):
            if picked >= room or cost[j] > tol:
                break
            if drop[(j - 1) % n] or drop[(j + 1) % n]:
                continue
            drop[j] = True
            picked += 1
        if picked == 0:
            break
        keep[idx[drop]] = False
    return points[keep]


def polygon_descriptor(mask: np.ndarray):
    """轮廓的多边形描述子：``(顶点数, 面积归一残差, 凸包充实度)``。

    轮廓用**像素集凸包**表示（判据只针对凸多边形件）；残差取"抽稀多边形与凸包的相对面积差"。
    """
    ys, xs = np.nonzero(mask)
    if xs.size < 8:
        return 0, 1.0, 0.0
    hull = convex_hull(xs.astype(np.float64), ys.astype(np.float64))
    if hull.shape[0] < 3:
        return int(hull.shape[0]), 1.0, 0.0
    hull_area = polygon_area(hull)
    if hull_area <= 1.0:
        return int(hull.shape[0]), 1.0, 0.0
    radius = math.sqrt(hull_area / math.pi)
    # 充实度＝件像素数 ÷ 凸包面积。凸件 ≈ 1；星形/桁架这类凹件明显 < 1，
    # 于是不会"冒充"多边形。（凸包面积用鞋带公式解析算，不做栅格化——快两个量级。）
    solidity = float(np.count_nonzero(mask) / max(1.0, hull_area))
    simple = decimate_closed(hull, POLY_SAGITTA_TOL * radius, 3)
    residual = abs(polygon_area(simple) - hull_area) / hull_area
    return int(simple.shape[0]), float(residual), solidity


def _elongation(xs: np.ndarray, ys: np.ndarray) -> float:
    """朝向无关的长宽比：像素坐标协方差主轴跨度比 ``sqrt(l1/l2)``。

    实心矩形退化为 ``w/h``；圆盘为 1；斜置杆件不受朝向影响（轴对齐外接框做不到这点）。
    """
    if xs.size < 2:
        return 1.0
    mx, my = float(xs.mean()), float(ys.mean())
    cxx = float(((xs - mx) ** 2).mean())
    cyy = float(((ys - my) ** 2).mean())
    cxy = float(((xs - mx) * (ys - my)).mean())
    trace = cxx + cyy
    det = cxx * cyy - cxy * cxy
    root = math.sqrt(max(0.0, trace * trace / 4.0 - det))
    lam1, lam2 = trace / 2.0 + root, trace / 2.0 - root
    if lam2 <= 1e-12:
        return 99.0
    return math.sqrt(max(1.0, lam1 / lam2))


def describe(pixels: np.ndarray, bbox) -> dict:
    """单个区域的轮廓描述子。``pixels`` 为该区域的 bool 掩膜（已裁到 bbox）。"""
    w = max(1, bbox[2] - bbox[0])
    h = max(1, bbox[3] - bbox[1])
    area = float(pixels.sum())
    extent = area / float(w * h)
    circle_fill = 4.0 * area / (math.pi * float(w) * float(h))
    ys, xs = np.nonzero(pixels)
    elongation = _elongation(xs.astype(np.float64), ys.astype(np.float64))
    centroid = [float(xs.mean()) + bbox[0], float(ys.mean()) + bbox[1]]
    vertices, residual, solidity = polygon_descriptor(pixels)
    return {
        "extent": round(extent, 6),
        "elongation": round(elongation, 6),
        "circle_fill": round(circle_fill, 6),
        "hole_area_fraction": 0.0,
        "centroid": centroid,
        "polygon_vertices": int(vertices),
        "polygon_residual": round(float(residual), 6),
        "solidity": round(float(solidity), 6),
    }


def erode(mask: np.ndarray, radius: int) -> np.ndarray:
    """4 邻接腐蚀 radius 次（:func:`dilate` 的对偶）。"""
    out = mask
    for _ in range(int(radius)):
        shrunk = out.copy()
        shrunk[1:, :] &= out[:-1, :]
        shrunk[:-1, :] &= out[1:, :]
        shrunk[:, 1:] &= out[:, :-1]
        shrunk[:, :-1] &= out[:, 1:]
        out = shrunk
    return out


def dilate(mask: np.ndarray, radius: int) -> np.ndarray:
    """4 邻接膨胀 radius 次（自己实现，不引 scipy）。"""
    out = mask
    for _ in range(int(radius)):
        grown = out.copy()
        grown[1:, :] |= out[:-1, :]
        grown[:-1, :] |= out[1:, :]
        grown[:, 1:] |= out[:, :-1]
        grown[:, :-1] |= out[:, 1:]
        out = grown
    return out


def classify_regions(components, shape, total_px: int, contact_tol: int):
    """把保留下来的区域分成 ``component`` / ``annotation``（判据见 JSON.method.annotation）。

    "孤立"用**像素距离**判断，不用 bbox 间隙：图注的 bbox 往往整个落在主体 bbox 内部
    （如论文图里的 "(a)" 落在卫星外接框内），按 bbox 判会把图注误当主体的一部分。
    """
    height, width = shape
    radius = max(1, int(contact_tol))
    out = []
    for comp in components:
        x0, y0, x1, y1 = comp["bbox_xyxy"]
        small = comp["pixel_count"] <= ANNOT_MAX_FRAC * max(1, total_px)
        compact = comp["shape_descriptors"]["extent"] >= ANNOT_MIN_EXTENT
        banded = (y1 <= ANNOT_BAND_Y * height or y0 >= (1.0 - ANNOT_BAND_Y) * height
                  or x1 <= ANNOT_BAND_X * width or x0 >= (1.0 - ANNOT_BAND_X) * width)
        isolated = True
        if small and compact and banded:
            grown = dilate(comp["_mask"], radius)
            for other in components:
                if other["index"] == comp["index"]:
                    continue
                if other["pixel_count"] >= 10 * comp["pixel_count"] \
                        and (grown & other["_mask"]).any():
                    isolated = False
                    break
        out.append("annotation" if (small and compact and banded and isolated) else "component")
    return out


def attribute_openings(components, offset=(0, 0)):
    """把每个开口归属到一个件：优先落在**细长件**（筒）的 bbox 内，否则归包围它的件。

    返回顶层 ``openings[]``，每条带 ``host_component``——"每筒开口数"就靠它数出来。
    """
    slender = [c for c in components if c.get("slender")]
    items = []
    for comp in components:
        for item in comp["openings"].get("items", []):
            bx0, by0, bx1, by1 = item["bbox_xyxy"]
            cx, cy = (bx0 + bx1) / 2.0, (by0 + by1) / 2.0
            host = None
            for cand in slender:
                hx0, hy0, hx1, hy1 = cand["bbox_xyxy"]
                if hx0 <= cx <= hx1 and hy0 <= cy <= hy1:
                    host = cand["index"]
                    break
            items.append({
                "host_component": comp["index"] if host is None else host,
                "owner_component": comp["index"],
                "bbox_xyxy": [bx0 + offset[0], by0 + offset[1], bx1 + offset[0], by1 + offset[1]],
                "area_px": item["area_px"],
                "extent": item["extent"],
                "circle_fill": item["circle_fill"],
                "elongation": item["elongation"],
                "in_top_band": item["in_top_band"],
                "host_is_slender": host is not None,
            })
    items.sort(key=lambda o: (o["host_component"], o["bbox_xyxy"][1], o["bbox_xyxy"][0]))
    return items


def extract_components(rgb: np.ndarray, mask: np.ndarray, min_area: int, contact_tol: int,
                       offset=(0, 0), labels=None, n_labels=None):
    """前景 → 组件列表（含描述子、孔洞、开口、接触、图注分类）。

    ``offset`` 为 ROI 原点：掩膜按 ROI 内坐标算，输出 bbox 一律换算回**源图坐标**。
    ``labels`` 给定时用它作为件划分（两段式分件的结果），否则按掩膜 8 邻接连通域。
    """
    if labels is None:
        labels, n_raw = label_runs(mask, connectivity=8)
    else:
        n_raw = int(n_labels if n_labels is not None else labels.max())
    components = []
    for i in range(1, n_raw + 1):
        sel = labels == i
        size = int(sel.sum())
        if size < min_area:
            continue
        bbox = _bbox_of(sel)
        sub = sel[bbox[1]:bbox[3], bbox[0]:bbox[2]]
        desc = describe(sub, bbox)
        components.append({
            "index": len(components),
            "bbox_xyxy": bbox,
            "pixel_count": size,
            "shape_descriptors": desc,
            "median_rgb": [int(v) for v in np.median(rgb[sel], axis=0)],
            "_mask": sel,
            "_label": i,
        })
    if components:
        order = sorted(range(len(components)),
                       key=lambda k: (components[k]["bbox_xyxy"][1],
                                      components[k]["bbox_xyxy"][0]))
        components = [components[k] for k in order]
        for idx, comp in enumerate(components):
            comp["index"] = idx

    _background, holes, n_holes = hole_masks(mask)
    for comp in components:
        comp["holes"] = []
    for hi in range(1, n_holes + 1):
        hole = holes == hi
        if not hole.any():
            continue
        ys, xs = np.nonzero(hole)
        touched = np.zeros(0, dtype=np.int32)
        for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            ny, nx = ys + dy, xs + dx
            ok = (ny >= 0) & (ny < mask.shape[0]) & (nx >= 0) & (nx < mask.shape[1])
            vals = labels[ny[ok], nx[ok]]
            touched = np.concatenate([touched, vals[vals > 0]])
        owner_label = int(np.bincount(touched).argmax()) if touched.size else 0
        hbox = [int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1]
        hsub = hole[hbox[1]:hbox[3], hbox[0]:hbox[2]]
        hdesc = describe(hsub, hbox)
        item = {
            "bbox_xyxy": hbox,
            "area_px": int(hole.sum()),
            "extent": hdesc["extent"],
            "circle_fill": hdesc["circle_fill"],
            "elongation": hdesc["elongation"],
            "owner_label": owner_label,
        }
        for comp in components:
            if comp["_label"] == owner_label:
                comp["holes"].append(item)
                break

    for comp in components:
        holes = comp["holes"]
        comp["holes"] = {
            "count": len(holes),
            "total_area_px": int(sum(h_["area_px"] for h_ in holes)),
            "items": holes,
        }
        area = float(comp["pixel_count"])
        frac = (holes and max(h_["area_px"] for h_ in holes) / area) or 0.0
        comp["shape_descriptors"]["hole_area_fraction"] = round(float(frac), 6)
        comp["shape_class"] = classify_shape(comp["shape_descriptors"])
        if comp["shape_class"] == "polygon":
            name = POLY_NAMES.get(comp["shape_descriptors"]["polygon_vertices"])
            if name:
                comp["shape_descriptors"]["polygon_name"] = name
        comp["islands"] = 1

        x0, y0, _x1, y1 = comp["bbox_xyxy"]
        band = y0 + OPENING_TOP_FRAC * (y1 - y0)
        floor = max(16, OPENING_MIN_FRAC * area)
        openings = [h_ for h_ in holes if h_["area_px"] >= floor]
        for item in openings:
            item["in_top_band"] = bool(
                (item["bbox_xyxy"][1] + item["bbox_xyxy"][3]) / 2.0 <= band)
        comp["openings"] = {
            "count": len(openings),
            "top_band_count": sum(1 for h_ in openings if h_["in_top_band"]),
            "min_area_px": round(float(floor), 3),
            "top_band_until_y": round(float(band), 3),
            "items": openings,
        }

    for comp in components:
        contacts = []
        for other in components:
            if other["index"] == comp["index"]:
                continue
            gap = box_gap(comp["bbox_xyxy"], other["bbox_xyxy"])
            if gap <= contact_tol:
                contacts.append({
                    "part": other["index"],
                    "gap_px": round(float(gap), 3),
                    "bbox_overlap": bool(box_iou(comp["bbox_xyxy"], other["bbox_xyxy"]) > 0
                                         or gap < 0),
                })
        comp["contacts"] = contacts
        comp["slender"] = bool(
            comp["shape_descriptors"]["elongation"] >= SLENDER_ELONGATION)

    total_px = sum(c["pixel_count"] for c in components)
    _classes = classify_regions(components, mask.shape, total_px, contact_tol)
    for comp, cls in zip(components, _classes):
        comp["kind"] = cls

    openings = attribute_openings(components, offset)
    for comp in components:
        host = [o for o in openings if o["host_component"] == comp["index"]]
        comp["openings"] = {
            "count": len(host),
            "top_band_count": sum(1 for o in host if o["in_top_band"]),
            "min_area_px": comp["openings"]["min_area_px"],
            "top_band_until_y": comp["openings"]["top_band_until_y"],
            "items": host,
        }
    for comp in components:
        x0, y0, x1, y1 = comp["bbox_xyxy"]
        comp["bbox_xyxy"] = [x0 + offset[0], y0 + offset[1], x1 + offset[0], y1 + offset[1]]
        for item in comp["holes"]["items"]:
            bx0, by0, bx1, by1 = item["bbox_xyxy"]
            item["bbox_xyxy"] = [bx0 + offset[0], by0 + offset[1],
                                 bx1 + offset[0], by1 + offset[1]]
        desc = comp["shape_descriptors"]
        if "centroid" in desc:
            desc["centroid"] = [desc["centroid"][0] + offset[0], desc["centroid"][1] + offset[1]]
    return components, int(n_raw), openings


def _color_family(rgb: np.ndarray, masks) -> list:
    """每个组件的色族标签：``achromatic`` 或 ``hue<桶>``（桶宽 60°）。"""
    fams = []
    for mask in masks:
        px = rgb[mask].astype(np.int32)
        if px.size == 0:
            fams.append("empty")
            continue
        med = np.median(px, axis=0)
        chroma = float(med.max() - med.min())
        if chroma < 40.0:
            fams.append("achromatic")
            continue
        r, g, b = float(med[0]), float(med[1]), float(med[2])
        mx, mn = float(med.max()), float(med.min())
        d = (mx - mn) or 1.0
        if mx == r:
            hue = ((g - b) / d) % 6
        elif mx == g:
            hue = (b - r) / d + 2
        else:
            hue = (r - g) / d + 4
        fams.append("hue%d" % int((hue * 60.0) // 60))
    return fams


def flat_color_stats(rgb: np.ndarray, mask: np.ndarray, plateau_share: float = 0.02,
                     cover_threshold: float = 0.85):
    """平色块判据：占比 ≥ ``plateau_share`` 的精确 RGB 值合计覆盖 ≥ ``cover_threshold``。"""
    px = rgb[mask].astype(np.int64)
    total = int(px.shape[0])
    if total == 0:
        return False, 0.0, 0
    key = px[:, 0] * 65536 + px[:, 1] * 256 + px[:, 2]
    counts = np.bincount(key, minlength=1 << 24)
    nz = counts[counts > 0]
    nz.sort()
    plateau = nz[nz >= plateau_share * total]
    cover = float(plateau.sum() / total)
    return bool(cover >= cover_threshold), round(cover, 6), int(plateau.size)


# ---------------------------------------------------------------- 锚件

def load_anchors(path: str | None):
    """读锚件配置；``None`` 返回空列表。坏文件抛 :class:`ImgCmpError`。"""
    if not path:
        return []
    if not os.path.isfile(path):
        raise ImgCmpError("anchors file not found: %s" % path)
    try:
        with open(path, encoding="utf-8") as handle:
            doc = yaml.safe_load(handle) or {}
    except Exception as exc:  # noqa: BLE001
        raise ImgCmpError("cannot parse anchors yaml %s: %s" % (path, exc)) from exc
    items = doc.get("anchors") if isinstance(doc, dict) else None
    if items is None:
        raise ImgCmpError("anchors yaml must contain an 'anchors' list: %s" % path)
    if not isinstance(items, list):
        raise ImgCmpError("'anchors' must be a list: %s" % path)
    out = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise ImgCmpError("anchors[%d] must be a mapping" % i)
        name = item.get("name")
        if not name:
            raise ImgCmpError("anchors[%d] needs a 'name'" % i)
        ref_m = item.get("ref_m")
        if ref_m is None:
            raise ImgCmpError("anchors[%d] (%s) needs 'ref_m'" % (i, name))
        try:
            ref_m = float(ref_m)
        except (TypeError, ValueError) as exc:
            raise ImgCmpError("anchors[%d] ref_m must be a number" % i) from exc
        selector = item.get("selector") or {"rule": "largest"}
        if not isinstance(selector, dict) or selector.get("rule") not in (
                "largest", "bbox_longest", "position"):
            raise ImgCmpError("anchors[%d] (%s) selector.rule must be one of "
                              "largest|bbox_longest|position" % (i, name))
        out.append({"name": str(name), "ref_m": ref_m, "selector": selector})
    return out


def _span(comp, axis=None) -> float:
    x0, y0, x1, y1 = comp["bbox_xyxy"]
    wx, wy = x1 - x0, y1 - y0
    if axis == "x":
        return float(wx)
    if axis == "y":
        return float(wy)
    return float(max(wx, wy))


def pick_anchor_component(components, selector):
    """按 selector 在组件里挑锚件，返回索引；挑不到返回 ``None``。"""
    if not components:
        return None
    rule = selector.get("rule", "largest")
    if rule == "largest":
        return max(range(len(components)), key=lambda k: components[k]["pixel_count"])
    if rule == "bbox_longest":
        axis = selector.get("axis")
        return max(range(len(components)), key=lambda k: _span(components[k], axis))
    side = selector.get("side")
    if side not in ("top", "bottom", "left", "right"):
        raise ImgCmpError("position selector needs side in top|bottom|left|right")
    ys = [c["bbox_xyxy"][1] for c in components] + [c["bbox_xyxy"][3] for c in components]
    xs = [c["bbox_xyxy"][0] for c in components] + [c["bbox_xyxy"][2] for c in components]
    y0, y1, x0, x1 = min(ys), max(ys), min(xs), max(xs)
    frac = float(selector.get("within_frac", 0.35))
    cands = []
    for k, comp in enumerate(components):
        bx0, by0, bx1, by1 = comp["bbox_xyxy"]
        cx, cy = (bx0 + bx1) / 2.0, (by0 + by1) / 2.0
        if side == "top" and by1 <= y0 + frac * (y1 - y0):
            cands.append(k)
        elif side == "bottom" and by0 >= y1 - frac * (y1 - y0):
            cands.append(k)
        elif side == "left" and bx1 <= x0 + frac * (x1 - x0):
            cands.append(k)
        elif side == "right" and bx0 >= x1 - frac * (x1 - x0):
            cands.append(k)
    if not cands:
        return None
    return max(cands, key=lambda k: components[k]["pixel_count"])


# ---------------------------------------------------------------- 组件特征表

def build_table(image_path: str, anchors_path: str | None = None, kind: str = "auto",
                min_area: int | None = None, contact_tol: int = 2,
                threshold: int | None = None, roi=None, merge_large_blobs: bool = False) -> dict:
    """单图（可限 ROI）→ 组件特征表（dict）。

    ``threshold=None`` 用背景相对阈值（退化时回退 Otsu）；``roi`` 用 ``common.parse_roi``
    的口径（源图像素 ``x,y,w,h``，左闭右开），是"筒与罩盘在剪影上连通、切不开"时调用方
    可控的正经出口。组件 bbox 一律换算回**源图坐标**，ROI 只决定分析范围。
    """
    rgb = load_rgb(image_path)
    height, width = rgb.shape[0], rgb.shape[1]
    if kind not in ("auto", KIND_WHITE, KIND_BLACK):
        raise ImgCmpError("--kind must be white|black|auto, got: %r" % (kind,))
    resolved = detect_kind(rgb) if kind == "auto" else kind
    if contact_tol < 0:
        raise ImgCmpError("--contact-tol must be >= 0, got: %r" % (contact_tol,))

    from .common import parse_roi  # 与 T1 同一套 ROI 口径

    box = parse_roi(roi, (width, height))          # (x, y, w, h)，已裁到图内
    rx, ry, rw, rh = box
    if rw <= 0 or rh <= 0:
        raise ImgCmpError("roi is empty: %r" % (box,))
    crop = rgb[ry:ry + rh, rx:rx + rw]
    analysed_w, analysed_h = rw, rh

    auto_min = max(32, int(round(0.0002 * analysed_w * analysed_h)))
    if min_area is None:
        min_area_used, min_area_option = auto_min, "auto"
    else:
        min_area_used = int(min_area)
        if min_area_used < 1:
            raise ImgCmpError("--min-area must be >= 1, got: %r" % (min_area,))
        min_area_option = "explicit"

    # 背景统计取自**整幅图**的边框带：ROI 可能整个落在对象内部，此时 ROI 自己的边框不是背景，
    # 拿它当背景会把阈值抬到对象内部去。ROI 只决定分析范围，不改变"背景是什么"。
    seg = segment(crop, resolved, threshold, bg_source=rgb, min_area=min_area_used,
                  merge_large=merge_large_blobs)
    mask = seg["mask"]
    thr, thr_source = seg["threshold"], seg["strategy"]
    if not mask.any():
        raise ImgCmpError("foreground mask is empty for %s (kind=%s, threshold=%d)"
                          % (image_path, resolved, thr))
    components, islands, opening_items = extract_components(
        crop, mask, min_area_used, contact_tol, offset=(rx, ry),
        labels=seg["labels"], n_labels=seg["n_labels"])

    kept = np.zeros_like(mask)
    for comp in components:
        kept |= comp["_mask"]
    total_pixels = int(kept.sum())
    total_height = 0
    payload_box = None
    if total_pixels:
        ys, xs = np.nonzero(kept)
        total_height = int(ys.max() - ys.min() + 1)
        payload_box = [int(xs.min()) + rx, int(ys.min()) + ry,
                       int(xs.max()) + 1 + rx, int(ys.max()) + 1 + ry]

    fams = _color_family(crop, [c["_mask"] for c in components])
    for comp, fam in zip(components, fams):
        comp["color_family"] = fam

    flat, flat_cover, plateaus = flat_color_stats(crop, mask)
    color_groups = len(set(fams)) if (flat and fams) else None

    anchors = load_anchors(anchors_path)
    anchor_info = []
    for spec in anchors:
        candidates = [c for c in components if c["kind"] == "component"] or components
        idx = pick_anchor_component(candidates, spec["selector"])
        idx = None if idx is None else candidates[idx]["index"]
        if idx is None:
            anchor_info.append({"name": spec["name"], "ref_m": spec["ref_m"],
                                "rule": spec["selector"].get("rule"), "component_index": None,
                                "span_px": None, "note": "no component matched the selector"})
            continue
        span = _span(components[idx], spec["selector"].get("axis"))
        anchor_info.append({
            "name": spec["name"],
            "ref_m": spec["ref_m"],
            "rule": spec["selector"].get("rule"),
            "selector": {k: v for k, v in spec["selector"].items() if k != "rule"},
            "component_index": idx,
            "span_px": round(span, 3),
        })
    primary = next((a for a in anchor_info if a.get("component_index") is not None), None)

    for comp in components:
        x0, y0, x1, y1 = comp["bbox_xyxy"]
        comp["bbox_xyxy"] = [int(v) for v in comp["bbox_xyxy"]]
        comp["bbox_whxy"] = [int(x0), int(y0), int(x1 - x0), int(y1 - y0)]
        comp["centroid"] = [round(v, 3) for v in comp["shape_descriptors"].pop("centroid")]
        comp["area_fraction"] = round(comp["pixel_count"] / total_pixels, 6) if total_pixels else 0.0
        comp["height_fraction"] = round((y1 - y0) / total_height, 6) if total_height else 0.0
        comp["span_px"] = round(_span(comp), 3)
        comp["anchor_ratio"] = None
        comp["size_m"] = None
        if primary is not None and primary["span_px"]:
            ratio = _span(comp) / primary["span_px"]
            comp["anchor_ratio"] = round(float(ratio), 6)
            comp["size_m"] = round(float(ratio) * primary["ref_m"], 6)
        comp.pop("_mask", None)
        comp.pop("_label", None)

    annotations = [c for c in components if c["kind"] == "annotation"]
    payload = [c for c in components if c["kind"] == "component"]
    table = {
        "tool": TOOL,
        "mode": "table",
        "image": os.path.abspath(image_path),
        "image_size": [int(width), int(height)],
        "roi": [int(v) for v in box],
        "roi_box": [int(rx), int(ry), int(rx + rw), int(ry + rh)],
        "analysed_size": [int(analysed_w), int(analysed_h)],
        "kind": resolved,
        "kind_requested": kind,
        "threshold": int(thr),
        "threshold_source": thr_source,
        "mask_strategy": thr_source,
        "merge_large_blobs": bool(merge_large_blobs),
        "otsu_threshold": int(seg["otsu"]),
        "support_threshold": int(seg["support_threshold"]),
        "seed_count": seg["seeds"],
        "background_luma": [round(float(background_stats(rgb)[0]), 3),
                            round(float(background_stats(rgb)[1]), 3)],
        "min_area": int(min_area_used),
        "min_area_option": min_area_option,
        "contact_tol_px": int(contact_tol),
        "foreground_pixels": int(mask.sum()),
        "component_pixels": total_pixels,
        "region_count": len(components),
        "component_count": len(payload),
        "annotation_count": len(annotations),
        "annotation_bboxes": [c["bbox_xyxy"] for c in annotations],
        "islands": int(islands),
        "total_payload_height_px": total_height,
        "payload_box_xyxy": payload_box,
        "flat_color": flat,
        "flat_color_cover": flat_cover,
        "flat_color_plateaus": plateaus,
        "color_groups": color_groups,
        "anchors": anchor_info,
        "openings": opening_items,
        "components": components,
        "method": METHOD,
    }
    return table


# ---------------------------------------------------------------- 差异对照

DEFAULT_TOL = {
    "height_fraction": 0.05,
    "area_fraction": 0.02,
    "anchor_ratio": 0.10,
    "contact_px": 2.0,
    "match_score": 0.15,
}


def _centroid(comp):
    return comp.get("centroid") or [0.0, 0.0]


def _pair_score(ref, ours):
    iou = box_iou(ref["bbox_xyxy"], ours["bbox_xyxy"])
    rx, ry = _centroid(ref)
    ox, oy = _centroid(ours)
    dist = math.hypot(rx - ox, ry - oy)
    norm = 0.5 * (max(_span(ref), 1.0) + max(_span(ours), 1.0))
    return 0.6 * iou + 0.4 * math.exp(-dist / norm)


def _payload_scale(table):
    box = table.get("payload_box_xyxy")
    if not box:
        return 1.0
    return float(max(box[2] - box[0], box[3] - box[1], 1))


def compare_tables(ref: dict, ours: dict, tol: dict | None = None) -> dict:
    """两张组件特征表 → 差异对照表（dict）。"""
    margins = dict(DEFAULT_TOL)
    margins.update(tol or {})
    # 图注（"(a)" 这类文字）不是载荷：计数与配对都只看 component 类，annotation 只报数量。
    refs = [c for c in (ref.get("components") or []) if c.get("kind", "component") == "component"]
    ourss = [c for c in (ours.get("components") or []) if c.get("kind", "component") == "component"]
    if not refs and not ourss:
        raise ImgCmpError("both component tables are empty; nothing to compare")
    ref_ann = int(ref.get("annotation_count", len(ref.get("components") or []) - len(refs)))
    our_ann = int(ours.get("annotation_count", len(ours.get("components") or []) - len(ourss)))

    pairs = []
    used_r, used_o = set(), set()
    scored = []
    for i, r in enumerate(refs):
        for j, o in enumerate(ourss):
            scored.append((_pair_score(r, o), i, j))
    scored.sort(key=lambda t: (-t[0], t[1], t[2]))
    for score, i, j in scored:
        if i in used_r or j in used_o or score < margins["match_score"]:
            continue
        used_r.add(i)
        used_o.add(j)
        pairs.append({"ref": i, "ours": j, "score": round(float(score), 6),
                      "ref_bbox": refs[i]["bbox_xyxy"], "ours_bbox": ourss[j]["bbox_xyxy"]})

    diffs = []
    if len(refs) != len(ourss):
        diffs.append({
            "kind": "count",
            "ref": len(refs),
            "ours": len(ourss),
            "delta": len(ourss) - len(refs),
            "rank": "major",
            "evidence": "组件数 基准 %d（图 %s）／我方 %d（图 %s）"
                        % (len(refs), os.path.basename(ref.get("image", "?")),
                           len(ourss), os.path.basename(ours.get("image", "?"))),
        })

    def _evidence(r_idx, o_idx, text):
        parts = []
        if r_idx is not None:
            parts.append("ref#%d bbox=%s px=%d" % (r_idx, refs[r_idx]["bbox_xyxy"],
                                                   refs[r_idx]["pixel_count"]))
        if o_idx is not None:
            parts.append("ours#%d bbox=%s px=%d" % (o_idx, ourss[o_idx]["bbox_xyxy"],
                                                    ourss[o_idx]["pixel_count"]))
        return "; ".join(parts) + ("; " + text if text else "")

    for pair in pairs:
        i, j = pair["ref"], pair["ours"]
        r, o = refs[i], ourss[j]
        for key, kind_name in (("height_fraction", "height_fraction"),
                               ("area_fraction", "area_fraction"),
                               ("anchor_ratio", "anchor_ratio")):
            rv, ov = r.get(key), o.get(key)
            if rv is None or ov is None:
                continue
            delta = float(ov) - float(rv)
            keep = abs(delta) > margins[key]
            if not keep:
                continue
            diffs.append({
                "kind": kind_name,
                "ref": rv, "ours": ov, "delta": round(delta, 6),
                "rank": "major" if abs(delta) > 2.0 * margins[key] else "minor",
                "evidence": _evidence(i, j, "%s %.4f -> %.4f" % (key, rv, ov)),
            })
        if r.get("shape_class") != o.get("shape_class"):
            diffs.append({
                "kind": "shape_class",
                "ref": r.get("shape_class"), "ours": o.get("shape_class"), "delta": None,
                "rank": "major",
                "evidence": _evidence(i, j, "形状类 %s -> %s（描述子 ref %s / ours %s）"
                                      % (r.get("shape_class"), o.get("shape_class"),
                                         r.get("shape_descriptors"), o.get("shape_descriptors"))),
            })
        ro = r.get("openings") or {"count": 0, "top_band_count": 0, "items": []}
        oo = o.get("openings") or {"count": 0, "top_band_count": 0, "items": []}
        if (ro["count"], ro["top_band_count"]) != (oo["count"], oo["top_band_count"]):
            diffs.append({
                "kind": "hole_count",
                "ref": ro["count"], "ours": oo["count"],
                "delta": oo["count"] - ro["count"],
                "rank": "major",
                "evidence": _evidence(i, j, "开口数 %d→%d（顶带内 %d→%d；原始孔洞 %d/%d；"
                                      "开口 bbox ref %s / ours %s）"
                                      % (ro["count"], oo["count"],
                                         ro["top_band_count"], oo["top_band_count"],
                                         (r.get("holes") or {}).get("count", 0),
                                         (o.get("holes") or {}).get("count", 0),
                                         [h["bbox_xyxy"] for h in ro["items"]],
                                         [h["bbox_xyxy"] for h in oo["items"]])),
            })
        rc = {c["part"]: c for c in (r.get("contacts") or [])}
        oc = {c["part"]: c for c in (o.get("contacts") or [])}
        if len(rc) != len(oc):
            diffs.append({
                "kind": "contact",
                "ref": len(rc), "ours": len(oc), "delta": len(oc) - len(rc),
                "rank": "major",
                "evidence": _evidence(i, j, "相接件数 %d -> %d（ref %s / ours %s）"
                                      % (len(rc), len(oc),
                                         [c["part"] for c in (r.get("contacts") or [])],
                                         [c["part"] for c in (o.get("contacts") or [])])),
            })

    # 「一件对多件」先判断，再看剩余未配对件是 added 还是 removed——否则配对成功的那个
    # 会把合并/拆分关系吃掉，只剩一条含糊的 added。
    ref_to_ours = {}
    for i, r in enumerate(refs):
        ref_to_ours[i] = [j for j, o in enumerate(ourss)
                          if box_iou(r["bbox_xyxy"], o["bbox_xyxy"]) > 0.05
                          or _inside(_centroid(o), r["bbox_xyxy"])]
    ours_to_ref = {}
    for j, o in enumerate(ourss):
        ours_to_ref[j] = [i for i, r in enumerate(refs)
                          if box_iou(r["bbox_xyxy"], o["bbox_xyxy"]) > 0.05
                          or _inside(_centroid(r), o["bbox_xyxy"])]

    merged_refs = sorted(i for i, js in ref_to_ours.items() if len(js) >= 2)
    split_ours = sorted(j for j, iis in ours_to_ref.items() if len(iis) >= 2)
    for i in merged_refs:
        diffs.append({
            "kind": "merged", "ref": 1, "ours": len(ref_to_ours[i]),
            "delta": len(ref_to_ours[i]) - 1, "rank": "major",
            "evidence": _evidence(i, None, "基准 1 件 ← 我方 %d 件（#%s）"
                                  % (len(ref_to_ours[i]), ref_to_ours[i])),
        })
    for j in split_ours:
        diffs.append({
            "kind": "split", "ref": len(ours_to_ref[j]), "ours": 1,
            "delta": 1 - len(ours_to_ref[j]), "rank": "major",
            "evidence": _evidence(None, j, "基准 %d 件 → 我方 1 件（#%s）"
                                  % (len(ours_to_ref[j]), ours_to_ref[j])),
        })
    absorbed_ours = {j for i in merged_refs for j in ref_to_ours[i]}
    absorbed_refs = {i for j in split_ours for i in ours_to_ref[j]}

    for i, r in enumerate(refs):
        if i in used_r or i in absorbed_refs:
            continue
        diffs.append({
            "kind": "removed", "ref": r["pixel_count"], "ours": 0,
            "delta": -int(r["pixel_count"]), "rank": "major",
            "evidence": _evidence(i, None, "基准有、我方无"),
        })
    for j, o in enumerate(ourss):
        if j in used_o or j in absorbed_ours:
            continue
        diffs.append({
            "kind": "added", "ref": 0, "ours": o["pixel_count"],
            "delta": int(o["pixel_count"]), "rank": "major",
            "evidence": _evidence(None, j, "我方有、基准无"),
        })

    def _rank_key(diff):
        return 0 if diff["rank"] == "major" else 1

    diffs.sort(key=lambda d: (_rank_key(d), d["kind"]))
    summary = {"major": sum(1 for d in diffs if d["rank"] == "major"),
               "minor": sum(1 for d in diffs if d["rank"] == "minor")}
    return {
        "tool": TOOL,
        "mode": "compare",
        "ref": {"table": ref.get("image"), "component_count": len(refs),
                "total_payload_height_px": ref.get("total_payload_height_px"),
                "kind": ref.get("kind"), "min_area": ref.get("min_area")},
        "ours": {"table": ours.get("image"), "component_count": len(ourss),
                 "total_payload_height_px": ours.get("total_payload_height_px"),
                 "kind": ours.get("kind"), "min_area": ours.get("min_area")},
        "tolerances": margins,
        "counts": {"ref": len(refs), "ours": len(ourss), "delta": len(ourss) - len(refs),
                   "ref_annotation": ref_ann, "ours_annotation": our_ann},
        "pairs": pairs,
        "diffs": diffs,
        "summary": summary,
        "method": {"pairing": "greedy by score = 0.6*bbox_IoU + 0.4*exp(-centroid_distance/mean_span), "
                              "score >= tolerances.match_score",
                   "rank": "structural kinds (count/added/removed/merged/split/shape_class/"
                           "hole_count/contact) are major; numeric kinds are major when "
                           "|delta| > 2*tolerance, else minor"},
    }


def _inside(point, box) -> bool:
    return bool(box[0] <= point[0] < box[2] and box[1] <= point[1] < box[3])


# ---------------------------------------------------------------- 中文报告

def _print_table_report(table: dict, out_path: str | None = None) -> None:
    thr_source_cn = {"background": "背景相对", "otsu": "Otsu 回退", "explicit": "--threshold"}
    rows = [
        ("输入图", table["image"]),
        ("图像尺寸", "%d x %d" % tuple(table["image_size"])),
        ("分析范围", "ROI x=%d y=%d w=%d h=%d" % tuple(table["roi"])
                     if list(table["roi"]) != [0, 0, table["image_size"][0], table["image_size"][1]]
                     else "整图"),
        ("底别", "%s%s" % (KIND_LABEL.get(table["kind"], table["kind"]),
                           "" if table["kind_requested"] == "auto" else "（--kind 强制）")),
        ("分割阈值", "%d（%s；边框带中位 %.0f／MAD %.0f）"
                     % (table["threshold"], thr_source_cn.get(table["threshold_source"],
                                                              table["threshold_source"]),
                        table["background_luma"][0], table["background_luma"][1])),
        ("最小面积", "%d px（%s）" % (table["min_area"], table["min_area_option"])),
        ("前景像素", "%d（连通域 %d 个，保留区域 %d：组件 %d ＋ 图注 %d）"
                     % (table["foreground_pixels"], table["islands"], table["region_count"],
                        table["component_count"], table["annotation_count"])),
        ("载荷总高", "%d px" % table["total_payload_height_px"]),
        ("平色块", "是（主色覆盖 %.1f%%，%d 个主色）→ color_groups=%s"
                   % (table["flat_color_cover"] * 100, table["flat_color_plateaus"],
                      table["color_groups"]) if table["flat_color"] else "否 → color_groups=null"),
    ]
    for anchor in table["anchors"]:
        if anchor.get("component_index") is None:
            rows.append(("锚件 %s" % anchor["name"],
                         "%s = %s m｜未匹配到组件" % (anchor["rule"], anchor["ref_m"])))
        else:
            rows.append(("锚件 %s" % anchor["name"],
                         "%s｜组件 #%d｜跨度 %.1f px = %s m"
                         % (anchor["rule"], anchor["component_index"], anchor["span_px"],
                            anchor["ref_m"])))
    if out_path:
        rows.append(("特征表", os.path.abspath(out_path)))

    width = 0
    for comp in table["components"]:
        width = max(width, len("#%d %s" % (comp["index"], comp["shape_class"])))
    print("[imgcmp] T2a 组件特征表：组件 %d ＋ 图注 %d（保留区域 %d）"
          % (table["component_count"], table["annotation_count"], table["region_count"]))
    for key, val in rows:
        print("  %-10s  %s" % (key, val))
    print("  %-10s  %s" % ("组件明细", "序 类别       形类      bbox(x0,y0,x1,y1)        像素  "
                                      "  面积占  高度分  锚比   开口(全部/顶带) 相接"))
    for comp in table["components"]:
        print("    %-*s  %-8s %-22s %6d  %6.3f  %6.3f  %-5s %-9s %s"
              % (width, "#%d %s" % (comp["index"], comp["shape_class"]), comp["kind"],
                 str(comp["bbox_xyxy"]), comp["pixel_count"], comp["area_fraction"],
                 comp["height_fraction"],
                 "%.3f" % comp["anchor_ratio"] if comp["anchor_ratio"] is not None else "-",
                 "%d/%d" % (comp["openings"]["count"], comp["openings"]["top_band_count"]),
                 ",".join(str(c["part"]) for c in comp["contacts"]) or "-"))
    if table["openings"]:
        print("  %-10s  %s" % ("开口归属", "开口 bbox(x0,y0,x1,y1)｜面积｜顶带｜归属件(是否细长)"))
        for item in table["openings"]:
            print("    %-22s %5d   %-4s  #%d%s"
                  % (str(item["bbox_xyxy"]), item["area_px"], "是" if item["in_top_band"] else "否",
                     item["host_component"], "（细长件）" if item["host_is_slender"] else ""))
    print("  · 形状类/开口判据与全部阈值见 JSON 的 method 键；锚件只走 --anchors 配置文件。")


def _print_compare_report(diff: dict, out_path: str | None = None) -> None:
    rows = [
        ("基准表", "%s（组件 %d，总高 %s px）"
                   % (diff["ref"]["table"], diff["ref"]["component_count"],
                      diff["ref"]["total_payload_height_px"])),
        ("我方表", "%s（组件 %d，总高 %s px）"
                   % (diff["ours"]["table"], diff["ours"]["component_count"],
                      diff["ours"]["total_payload_height_px"])),
        ("组件数", "基准 %d → 我方 %d（Δ%+d）｜图注 基准 %s → 我方 %s"
                   % (diff["counts"]["ref"], diff["counts"]["ours"], diff["counts"]["delta"],
                      diff["counts"].get("ref_annotation", "-"),
                      diff["counts"].get("ours_annotation", "-"))),
        ("配对", "%d 对（评分阈值 %.2f）"
                 % (len(diff["pairs"]), diff["tolerances"]["match_score"])),
        ("差异", "major %d ｜ minor %d" % (diff["summary"]["major"], diff["summary"]["minor"])),
    ]
    if out_path:
        rows.append(("对照表", os.path.abspath(out_path)))
    print("[imgcmp] T2a 差异对照表：%d 条差异（major %d）"
          % (len(diff["diffs"]), diff["summary"]["major"]))
    for key, val in rows:
        print("  %-8s  %s" % (key, val))
    print("  %-8s  %s" % ("差异清单", "档位  类别             基准→我方                       证据"))
    for item in diff["diffs"]:
        print("    %-6s %-16s %-30s %s"
              % (item["rank"], item["kind"],
                 "%s → %s" % (_short(item["ref"]), _short(item["ours"])),
                 item["evidence"]))
    print("  · 差异对照表是 H1 差异分诊的输入：major 项须逐条给处置（升级/允许差/自由度内）。")


def _short(value) -> str:
    if isinstance(value, float):
        return "%.4f" % value
    if value is None:
        return "-"
    return str(value)


# ---------------------------------------------------------------- selftest

def _synth(white: bool = True, width: int = 640, height: int = 480) -> np.ndarray:
    """自造图：rect ＋ circle ＋ ring ＋ rod（rod 与 rect 相接），circle/ring 与其余留间隙。"""
    bg, ink = ((250, 250, 250), (20, 20, 20)) if white else ((8, 8, 8), (235, 235, 235))
    arr = np.full((height, width, 3), bg, dtype=np.uint8)
    arr[300:420, 40:200] = ink                      # rect 160x120
    arr[354:368, 202:400] = ink                     # rod 198x14，与 rect 留 2 px 间隙（判为相接）
    yy, xx = np.mgrid[0:height, 0:width]
    circle = (yy - 120) ** 2 + (xx - 300) ** 2 <= 60 ** 2
    arr[circle] = ink                               # 实心圆 r=60
    ring = ((yy - 120) ** 2 + (xx - 480) ** 2 <= 60 ** 2) & \
           ((yy - 120) ** 2 + (xx - 480) ** 2 >= 32 ** 2)
    arr[ring] = ink                                 # 环：外 r60 / 内 r32
    return arr


EXPECT_BOX = {
    "rect": [40, 300, 200, 420],
    "rod": [202, 354, 400, 368],
    "circle": [240, 60, 360, 180],
    "ring": [420, 60, 540, 180],
}


def _synth_extra(white: bool = True, width: int = 720, height: int = 560) -> np.ndarray:
    """自造图（ROI／图注／开口归属用）：一只空心筒 ＋ 左上角一枚"图注"字样块。

    空心筒：外框 400x60、壁厚 8 → 长宽比 6.7（细长）且自带大孔洞（开口归属到它）。
    图注块：8x16、实心、落在图幅左上边缘带、与任何大件都不接 → 应判 ``annotation``。
    """
    bg, ink = ((250, 250, 250), (20, 20, 20)) if white else ((8, 8, 8), (235, 235, 235))
    arr = np.full((height, width, 3), bg, dtype=np.uint8)
    arr[300:360, 40:440] = ink                      # 筒外框
    arr[308:352, 48:432] = bg                       # 挖空 → 壁厚 8，孔洞 384x44
    arr[40:56, 40:48] = ink                         # "图注"字样块 8x16
    return arr


EXTRA_EXPECT = {
    "tube_bbox": [40, 300, 440, 360],
    "annotation_bbox": [40, 40, 48, 56],
}


def _synth_seam(width: int = 800, height: int = 300) -> np.ndarray:
    """两块亮件相邻，中间只有一条**暗缝**（luma 40，两侧 200，底 6）。

    单阈值（包含性掩膜）会把它们连成一件；边缘分割按谷脊切开 → 2 件。
    """
    arr = np.full((height, width, 3), 6, dtype=np.uint8)
    arr[40:200, 60:280] = 200
    arr[40:200, 314:534] = 200
    arr[40:200, 280:314] = 40          # 34 px 宽的暗缝
    return arr


def _synth_neck(dim: bool, width: int = 800, height: int = 300) -> np.ndarray:
    """两件由一条**细颈**相连：``dim=True`` 时细颈是暗的（可切），否则与两件同亮（切不开）。"""
    arr = np.full((height, width, 3), 6, dtype=np.uint8)
    arr[40:200, 60:300] = 200
    arr[40:200, 340:580] = 200
    arr[115:125, 300:340] = 40 if dim else 200
    return arr


def _synth_dark_object(width: int = 640, height: int = 360) -> np.ndarray:
    """自造黑底图：一件亮件（luma 220）＋ 一件暗件（luma 34），背景 luma 6、零噪声。

    全局 Otsu 会把阈值抬到两件之间以上，暗件被漏掉；背景相对阈值（6+8=14）两件都要。
    """
    arr = np.full((height, width, 3), 6, dtype=np.uint8)
    arr[60:300, 60:260] = 220                       # 亮件
    arr[120:240, 340:560] = 34                      # 暗件（与背景只差 28）
    return arr


def _cli_code(argv) -> int:
    """跑一次 CLI 拿退出码；argparse 的 SystemExit 也折算成退出码。"""
    try:
        return int(main(argv))
    except SystemExit as exc:  # argparse 用法错误走 SystemExit(2)
        code = exc.code
        return int(code) if isinstance(code, int) else 2


def _selftest() -> int:
    checks = []
    with tempfile.TemporaryDirectory() as tmp:
        for white in (True, False):
            tag = "white" if white else "black"
            path = os.path.join(tmp, "synth_%s.png" % tag)
            Image.fromarray(_synth(white=white), mode="RGB").save(path)
            table = build_table(path, kind="auto")
            comps = table["components"]
            checks.append(("kind_%s" % tag, table["kind"] == (KIND_WHITE if white else KIND_BLACK),
                           "底别判定 %s" % table["kind"]))
            checks.append(("count_%s" % tag, table["component_count"] == 4,
                           "组件数 %d（期望 4）" % table["component_count"]))
            if table["component_count"] != 4:
                continue

            # bbox：按形状类逐个核对（rect / circle / ring / rod 各一处）
            box_err = 0
            for comp in comps:
                want = EXPECT_BOX.get(comp["shape_class"])
                if want:
                    box_err = max(box_err, max(abs(a - b) for a, b in zip(comp["bbox_xyxy"], want)))
            checks.append(("bbox_%s" % tag, box_err <= 2,
                           "与期望框最大偏差 %d px（判据 ≤2）" % box_err))

            shapes = sorted(c["shape_class"] for c in comps)
            checks.append(("shapes_%s" % tag, shapes == ["circle", "rect", "ring", "rod"],
                           "形状类 %s（期望 circle/rect/ring/rod 各一）" % shapes))

            # 面积：矩形与杆是规整形状，面积误差必须为 0
            area_ok, detail = True, []
            for comp in comps:
                if comp["shape_class"] == "rect":
                    detail.append("rect %d/%d" % (comp["pixel_count"], 160 * 120))
                    area_ok &= abs(comp["pixel_count"] - 160 * 120) <= 2
                elif comp["shape_class"] == "rod":
                    detail.append("rod %d/%d" % (comp["pixel_count"], 198 * 14))
                    area_ok &= abs(comp["pixel_count"] - 198 * 14) <= 2
                elif comp["shape_class"] == "circle":
                    area_ok &= abs(comp["pixel_count"] - math.pi * 60 * 60) / (math.pi * 3600) < 0.03
                    detail.append("circle %d/%.0f" % (comp["pixel_count"], math.pi * 3600))
                elif comp["shape_class"] == "ring":
                    want = math.pi * (60 ** 2 - 32 ** 2)
                    area_ok &= abs(comp["pixel_count"] - want) / want < 0.05
                    detail.append("ring %d/%.0f" % (comp["pixel_count"], want))
            checks.append(("areas_%s" % tag, bool(area_ok), "；".join(detail)))

            # 高度分数：总高 = 60..420 = 360；rect 120/360、rod 14/360
            hf = {c["shape_class"]: c["height_fraction"] for c in comps}
            hf_ok = (abs(hf.get("rect", 0) - 120 / 360.0) < 0.01
                     and abs(hf.get("rod", 0) - 14 / 360.0) < 0.01
                     and table["total_payload_height_px"] == 360)
            checks.append(("height_fraction_%s" % tag, hf_ok,
                           "总高 %d px；rect %.3f、rod %.3f（期望 360 / 0.333 / 0.039）"
                           % (table["total_payload_height_px"], hf.get("rect", -1), hf.get("rod", -1))))

            # 接触：rect↔rod 相接；circle 与 ring 之间是间隙，不得相接
            by_shape = {c["shape_class"]: c for c in comps}
            contact_ok = True
            detail = []
            if "rect" in by_shape and "rod" in by_shape:
                gap = box_gap(by_shape["rect"]["bbox_xyxy"], by_shape["rod"]["bbox_xyxy"])
                contact_ok &= 0 < gap <= table["contact_tol_px"]
                detail.append("rect-rod gap=%d（判相接，≤%d）" % (gap, table["contact_tol_px"]))
            if "circle" in by_shape and "ring" in by_shape:
                gap = box_gap(by_shape["circle"]["bbox_xyxy"], by_shape["ring"]["bbox_xyxy"])
                contact_ok &= gap >= 50
                detail.append("circle-ring gap=%d（期望 ≥50）" % gap)
            if "rod" in by_shape and "circle" in by_shape:
                gap = box_gap(by_shape["rod"]["bbox_xyxy"], by_shape["circle"]["bbox_xyxy"])
                contact_ok &= gap > table["contact_tol_px"]
                detail.append("rod-circle gap=%d" % gap)
            checks.append(("contacts_%s" % tag, bool(contact_ok), "；".join(detail)))

            # 环带性：ring 必须真的带孔洞，且孔洞是圆
            ring = by_shape.get("ring")
            hole_ok = bool(ring) and ring["holes"]["count"] == 1
            if hole_ok:
                hole = ring["holes"]["items"][0]
                hole_ok = (abs(hole["circle_fill"] - 1.0) < 0.12
                           and 0.9 <= hole["elongation"] <= 1.1)
            checks.append(("ring_hole_%s" % tag, bool(hole_ok),
                           "环孔数 %s，孔洞描述子 %s"
                           % (ring["holes"]["count"] if ring else "-",
                              ring["holes"]["items"] if ring and ring["holes"]["items"] else "-")))

            # 锚件：自造临时 anchors.yaml，用 bbox_longest 取 rod 为锚，ref_m=1.0
            anchor_path = os.path.join(tmp, "anchors_%s.yaml" % tag)
            with open(anchor_path, "w", encoding="utf-8") as handle:
                handle.write("anchors:\n"
                             "  - name: rod\n"
                             "    ref_m: 1.0\n"
                             "    selector: {rule: bbox_longest, axis: x}\n")
            with_anchor = build_table(path, anchors_path=anchor_path, kind="auto")
            rod = next(c for c in with_anchor["components"] if c["shape_class"] == "rod")
            circle = next(c for c in with_anchor["components"] if c["shape_class"] == "circle")
            anchor_ok = (rod["anchor_ratio"] == 1.0
                         and abs(circle["anchor_ratio"] - 120 / 198.0) < 0.02
                         and abs(rod["size_m"] - 1.0) < 1e-6)
            checks.append(("anchor_ratio_%s" % tag, anchor_ok,
                           "rod 锚比 %.3f、圆锚比 %.3f（期望 1.0 / 0.6）"
                           % (rod["anchor_ratio"], circle["anchor_ratio"])))

        # 背景相对阈值：黑底图里"对象偏暗"与"对象偏亮"都必须纳入（Otsu 会漏掉偏暗的那件）
        dark_path = os.path.join(tmp, "synth_darkobject.png")
        Image.fromarray(_synth_dark_object(), mode="RGB").save(dark_path)
        dark = build_table(dark_path, kind="black")
        otsu_only = build_table(dark_path, kind="black",
                                threshold=otsu_threshold(to_gray(_synth_dark_object())))
        bg_ok = (dark["mask_strategy"] == "inclusive+edge_split"
                 and dark["component_count"] == 2
                 and otsu_only["component_count"] != 2)
        checks.append(("background_threshold", bg_ok,
                       "背景相对：阈值 %d（%s）→ %d 件；同图 Otsu=%d → %d 件（期望 2 与 ≠2）"
                       % (dark["threshold"], dark["mask_strategy"], dark["component_count"],
                          otsu_only["threshold"], otsu_only["component_count"])))

        # 边缘分割：暗缝相连的两件必须切开（单阈值会把它们连成一件）
        seam_path = os.path.join(tmp, "synth_seam.png")
        Image.fromarray(_synth_seam(), mode="RGB").save(seam_path)
        seam = build_table(seam_path, kind="black")
        seam_single = build_table(seam_path, kind="black",
                                  threshold=background_threshold(
                                      to_gray(_synth_seam()), KIND_BLACK))
        checks.append(("edge_split_seam",
                       seam["mask_strategy"] == "inclusive+edge_split"
                       and seam["component_count"] == 2
                       and seam_single["component_count"] == 1,
                       "边缘分割 %d 件（策略 %s）；同一图单阈值 %d 件（阈值 %d）"
                       % (seam["component_count"], seam["mask_strategy"],
                          seam_single["component_count"], seam_single["threshold"])))

        # 细颈：暗颈能切（2 件）；**同亮的细颈切不开**（1 件）——这是判据的已知失败条件
        dim_neck_path = os.path.join(tmp, "synth_neck_dim.png")
        Image.fromarray(_synth_neck(dim=True), mode="RGB").save(dim_neck_path)
        bright_neck_path = os.path.join(tmp, "synth_neck_bright.png")
        Image.fromarray(_synth_neck(dim=False), mode="RGB").save(bright_neck_path)
        dim_neck = build_table(dim_neck_path, kind="black")
        bright_neck = build_table(bright_neck_path, kind="black")
        checks.append(("edge_split_neck",
                       dim_neck["component_count"] == 2
                       and bright_neck["component_count"] == 1,
                       "暗细颈 %d 件（期望 2）；同亮细颈 %d 件（期望 1，判据的已知失败条件）"
                       % (dim_neck["component_count"], bright_neck["component_count"])))

        # ROI：只在给定 ROI 内建表，bbox 仍换算回源图坐标
        extra_path = os.path.join(tmp, "synth_extra.png")
        extra_src = _synth_extra(white=True)
        Image.fromarray(extra_src, mode="RGB").save(extra_path)
        full = build_table(extra_path, kind="auto")
        crop = build_table(extra_path, kind="auto", roi="40,290,400,80")
        roi_ok = (list(crop["roi"]) == [40, 290, 400, 80]
                  and crop["component_count"] == 1
                  and crop["annotation_count"] == 0
                  and crop["components"][0]["bbox_xyxy"] == EXTRA_EXPECT["tube_bbox"]
                  and full["component_count"] == 1
                  and full["annotation_count"] == 1
                  and full["annotation_bboxes"] == [EXTRA_EXPECT["annotation_bbox"]]
                  and crop["payload_box_xyxy"] == [40, 300, 440, 360])
        checks.append(("roi_and_annotation", roi_ok,
                       "整图：组件 %d ＋ 图注 %d（图注框 %s）；ROI %s → 组件 %d，首件 bbox %s"
                       % (full["component_count"], full["annotation_count"],
                          full["annotation_bboxes"], crop["roi"], crop["component_count"],
                          crop["components"][0]["bbox_xyxy"] if crop["components"] else "-")))

        # 开口归属：空心筒的孔洞必须成为"开口"，且归属到这只细长筒
        tube = next(c for c in full["components"] if c["kind"] == "component")
        openings = full["openings"]
        host_ok = (tube["slender"] and tube["shape_class"] == "ring"
                   and tube["openings"]["count"] == 1
                   and len(openings) == 1
                   and openings[0]["host_component"] == tube["index"]
                   and openings[0]["host_is_slender"]
                   and openings[0]["area_px"] == 384 * 44)
        checks.append(("opening_host", host_ok,
                       "筒 slender=%s 形类=%s 开口=%d；顶层开口 %d 条，归属件 #%s（细长=%s），面积 %s"
                       % (tube["slender"], tube["shape_class"], tube["openings"]["count"],
                          len(openings), openings[0]["host_component"] if openings else "-",
                          openings[0]["host_is_slender"] if openings else "-",
                          openings[0]["area_px"] if openings else "-")))

        # compare：图注不进配对/计数
        ann_only = compare_tables(full, crop)
        checks.append(("compare_ignores_annotation",
                       ann_only["counts"]["ref"] == 1 and ann_only["counts"]["ours"] == 1
                       and ann_only["counts"]["ref_annotation"] == 1
                       and ann_only["counts"]["ours_annotation"] == 0,
                       "整图 vs ROI：组件 %d/%d、图注 %d/%d、差异 %d 条"
                       % (ann_only["counts"]["ref"], ann_only["counts"]["ours"],
                          ann_only["counts"]["ref_annotation"],
                          ann_only["counts"]["ours_annotation"], len(ann_only["diffs"]))))

        # compare：同一张图自比必须零差异；白底 vs 黑底自比也应零差异（分割对称）
        white_path = os.path.join(tmp, "synth_white.png")
        table_a = build_table(white_path, kind="auto")
        same = compare_tables(table_a, table_a)
        checks.append(("compare_self_zero", not same["diffs"],
                       "自比差异 %d 条（期望 0）" % len(same["diffs"])))

        # compare：人为去掉 rod（抹白）→ 必须报 removed 与 count
        arr = _synth(white=True)
        arr[354:368, 202:400] = (250, 250, 250)
        cut_path = os.path.join(tmp, "synth_cut.png")
        Image.fromarray(arr, mode="RGB").save(cut_path)
        table_b = build_table(cut_path, kind="auto")
        diff = compare_tables(table_a, table_b)
        kinds = {d["kind"] for d in diff["diffs"]}
        checks.append(("compare_removed", "removed" in kinds and "count" in kinds,
                       "差异类别 %s" % sorted(kinds)))

        # compare：把矩形整体挪走（尺寸不变）→ 必须报出 removed/added 这类差异
        arr2 = np.full((480, 640, 3), (250, 250, 250), dtype=np.uint8)
        arr2[60:180, 40:200] = (20, 20, 20)                     # 同尺寸矩形换到顶部
        arr2[354:368, 202:400] = (20, 20, 20)                   # 杆件原位不动
        yy, xx = np.mgrid[0:480, 0:640]
        arr2[(yy - 120) ** 2 + (xx - 300) ** 2 <= 60 ** 2] = (20, 20, 20)
        arr2[((yy - 120) ** 2 + (xx - 480) ** 2 <= 60 ** 2)
             & ((yy - 120) ** 2 + (xx - 480) ** 2 >= 32 ** 2)] = (20, 20, 20)
        moved_path = os.path.join(tmp, "synth_moved.png")
        Image.fromarray(arr2, mode="RGB").save(moved_path)
        table_c = build_table(moved_path, kind="auto")
        diff_moved = compare_tables(table_a, table_c)
        moved_kinds = sorted({d["kind"] for d in diff_moved["diffs"]})
        checks.append(("compare_geom", len(diff_moved["diffs"]) >= 1
                       and ("removed" in moved_kinds or "added" in moved_kinds),
                       "矩形挪位后差异 %d 条：%s" % (len(diff_moved["diffs"]), moved_kinds)))

        # compare：把圆换成同位置的方块（形状类必须报差异，且 height/area 保持）
        arr3 = _synth(white=True)
        arr3[(np.mgrid[0:480, 0:640][0] - 120) ** 2
             + (np.mgrid[0:480, 0:640][1] - 300) ** 2 <= 60 ** 2] = (250, 250, 250)
        arr3[60:180, 240:360] = (20, 20, 20)                    # 同一 bbox 改成方块
        square_path = os.path.join(tmp, "synth_square.png")
        Image.fromarray(arr3, mode="RGB").save(square_path)
        diff_shape = compare_tables(table_a, build_table(square_path, kind="auto"))
        shape_kinds = sorted({d["kind"] for d in diff_shape["diffs"]})
        checks.append(("compare_shape_class",
                       "shape_class" in shape_kinds
                       and any(d["ref"] == "circle" and d["ours"] == "rect"
                               for d in diff_shape["diffs"] if d["kind"] == "shape_class"),
                       "圆→方块差异：%s" % shape_kinds))

        # compare 顶层字段必须稳定（H1 分诊直接吃它）
        checks.append(("compare_fields",
                       {"diffs", "counts", "pairs", "tolerances", "summary"} <= set(diff.keys())
                       and all({"kind", "ref", "ours", "delta", "rank", "evidence"}
                               <= set(item) for item in diff["diffs"]),
                       "顶层键 %s；diff 项键 %s"
                       % (sorted(diff.keys()),
                          sorted(diff["diffs"][0].keys()) if diff["diffs"] else "-")))

        # CLI 退出码：正常 0；用法/输入错误 2
        quiet = contextlib.redirect_stdout(io.StringIO())
        with quiet, contextlib.redirect_stderr(io.StringIO()):
            ok_code = _cli_code(["table", white_path, "--out", os.path.join(tmp, "cli_out")])
            tab_json = os.path.join(tmp, "cli_out", "synth_white.feat.json")
            cmp_code = _cli_code(["compare", tab_json, tab_json, "--out",
                                  os.path.join(tmp, "cli_out")])
            bad_kind = _cli_code(["table", white_path, "--kind", "grey"])
            bad_anchor = _cli_code(["table", white_path, "--anchors",
                                    os.path.join(tmp, "nope.yaml")])
            missing = _cli_code(["table", os.path.join(tmp, "missing.png")])
            bad_json = _cli_code(["compare", os.path.join(tmp, "nope.json"), tab_json])
            no_cmd = _cli_code([])
        codes = (ok_code, cmp_code, bad_kind, bad_anchor, missing, bad_json, no_cmd)
        checks.append(("cli_exit_codes", codes == (0, 0, 2, 2, 2, 2, 2)
                       and os.path.isfile(tab_json),
                       "table=%d compare=%d kind=%d anchors=%d missing=%d badjson=%d nouse=%d"
                       "（期望 0/0/2/2/2/2/2）" % codes))

    return selftest_report(TOOL, checks)


# ---------------------------------------------------------------- CLI

def build_parser():
    parser = make_parser(
        TOOL,
        "T2a component feature table: turn one image (white-background CAD or black-background "
        "render) into a component table (bbox, area fraction, anchor ratio, height fraction, "
        "shape class, contacts) and diff two tables. Deterministic image processing only.",
    )
    sub = parser.add_subparsers(dest="command")

    table = sub.add_parser("table", help="build a component feature table from one image")
    table.add_argument("image", help="input image (PNG/JPG)")
    table.add_argument("--anchors", metavar="YAML",
                       help="anchor declaration file (name/ref_m/selector); no task numbers "
                            "are baked into this tool")
    table.add_argument("--kind", choices=["auto", "white", "black"], default="auto",
                       help="image polarity; default auto (border luma)")
    table.add_argument("--roi", metavar="X,Y,W,H",
                       help="analyse only this region (source pixels, left-closed right-open, "
                            "same convention as primer-imgtile); component bboxes are still "
                            "reported in source coordinates")
    table.add_argument("--threshold", type=int, default=None, metavar="PX",
                       help="luma threshold override (0..255). Default is the background-"
                            "relative threshold t = median(border band) +/- max(8, 6*MAD), "
                            "falling back to Otsu when that mask is degenerate")
    table.add_argument("--min-area", type=int, default=None, metavar="PX",
                       help="minimum region area in pixels; default auto = max(32, 0.0002*W*H)")
    table.add_argument("--merge-large-blobs", action="store_true",
                       help="merge shallow-ridge adjacent basins whose areas are very "
                            "different (rescues over-splitting of a textured single body); "
                            "default off")
    table.add_argument("--contact-tol", type=int, default=2, metavar="PX",
                       help="bbox gap (px) below which two parts count as touching (default 2)")
    table.add_argument("--out", metavar="DIR", help="output directory for the JSON table")
    table.add_argument("--json", metavar="OUT.json", help="explicit JSON path for the table")
    table.add_argument("--selftest", action="store_true", help="run the built-in self-test")

    compare = sub.add_parser("compare", help="diff a reference table against our table")
    compare.add_argument("ref", metavar="ref.json", help="reference component table JSON")
    compare.add_argument("ours", metavar="ours.json", help="our component table JSON")
    compare.add_argument("--out", metavar="DIR", help="output directory for the diff JSON")
    compare.add_argument("--json", metavar="OUT.json", help="explicit JSON path for the diff")
    compare.add_argument("--tol", action="append", default=[], metavar="KEY=VALUE",
                         help="tolerance override, repeatable; keys: height_fraction, "
                              "area_fraction, anchor_ratio, contact_px, match_score")
    compare.add_argument("--selftest", action="store_true", help="run the built-in self-test")
    return parser


def main(argv=None) -> int:
    return run_main(TOOL, _run, argv)


def _parse_tols(items):
    tol = {}
    for item in items or []:
        if "=" not in item:
            raise ImgCmpError("--tol must be KEY=VALUE, got: %r" % (item,))
        key, _, raw = item.partition("=")
        key = key.strip()
        if key not in DEFAULT_TOL:
            raise ImgCmpError("unknown --tol key %r (known: %s)"
                              % (key, ", ".join(sorted(DEFAULT_TOL))))
        try:
            tol[key] = float(raw)
        except ValueError as exc:
            raise ImgCmpError("--tol %s value must be a number, got: %r" % (key, raw)) from exc
    return tol


def _run(argv) -> int:
    args = list(argv) if argv is not None else None
    if args is not None and args and args[0] == "--selftest":
        return _selftest()
    parser = build_parser()
    ns = parser.parse_args(args)
    if getattr(ns, "selftest", False):
        return _selftest()
    if not ns.command:
        parser.print_usage()
        raise ImgCmpError("a subcommand is required: table | compare (or --selftest)")

    if ns.command == "table":
        table = build_table(ns.image, anchors_path=ns.anchors, kind=ns.kind,
                            min_area=ns.min_area, contact_tol=ns.contact_tol,
                            threshold=ns.threshold, roi=ns.roi,
                            merge_large_blobs=ns.merge_large_blobs)
        path = None
        if ns.json:
            path = ns.json
        elif ns.out:
            stem = os.path.splitext(os.path.basename(ns.image))[0]
            path = _default_out_path(ns.out, stem, ".feat.json",
                                     os.path.abspath(ns.image), lambda d: d.get("image"))
        if path:
            ensure_dir(os.path.dirname(os.path.abspath(path)))
            write_json(path, table)
        _print_table_report(table, path)
        return 0

    for label, path in (("ref", ns.ref), ("ours", ns.ours)):
        if not os.path.isfile(path):
            raise ImgCmpError("%s table not found: %s" % (label, path))
    try:
        ref = _read_json_obj(ns.ref)
        ours = _read_json_obj(ns.ours)
    except ValueError as exc:
        raise ImgCmpError("cannot parse table json: %s" % exc) from exc
    for label, table in (("ref", ref), ("ours", ours)):
        if not isinstance(table, dict) or not isinstance(table.get("components"), list):
            raise ImgCmpError("%s table must be a feature-table JSON with a 'components' list "
                              "(got %s)" % (label, type(table).__name__))
    diff = compare_tables(ref, ours, _parse_tols(ns.tol))
    path = None
    if ns.json:
        path = ns.json
    elif ns.out:
        stem = "%s_vs_%s" % (os.path.splitext(os.path.basename(ns.ref))[0],
                             os.path.splitext(os.path.basename(ns.ours))[0])
        pair = "%s|%s" % (os.path.abspath(ns.ref), os.path.abspath(ns.ours))
        path = _default_out_path(
            ns.out, stem, ".diff.json", pair,
            lambda d: "%s|%s" % (d.get("ref", {}).get("table"), d.get("ours", {}).get("table")))
    if path:
        ensure_dir(os.path.dirname(os.path.abspath(path)))
        write_json(path, diff)
    _print_compare_report(diff, path)
    return 0


def _read_json_obj(path: str):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _default_out_path(out_dir: str, stem: str, suffix: str, source_value: str, extract):
    """``--out`` 下的缺省 JSON 名；同名但来源不同时补一个短摘要，绝不静默覆盖别人的表。

    同名不同源是多图同元数据（如各机位都叫 `white_cad.png`）时的常见事故：两个不同来源
    的表会互相覆盖。这里先看一眼既有文件记录的数据来源，不一致就改用
    ``<stem>.<digest8><suffix>``。
    """
    path = os.path.join(out_dir, "%s%s" % (stem, suffix))
    if not os.path.isfile(path):
        return path
    try:
        previous = extract(_read_json_obj(path))
    except (OSError, ValueError, KeyError, TypeError):
        previous = None
    if previous == source_value:
        return path
    digest = hashlib.sha1(source_value.encode("utf-8")).hexdigest()[:8]
    return os.path.join(out_dir, "%s.%s%s" % (stem, digest, suffix))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
