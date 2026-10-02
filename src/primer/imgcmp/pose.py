# -*- coding: utf-8 -*-
"""primer-imgpose —— T2b 姿态反求（剪影模板匹配，粗 5° → 精 1°）。

**用途**：给一张基准图（论文图/CAD 截图）和一台"能把我的场景渲成剪影"的渲染器，反求
基准图对应的相机姿态 `(az, el, roll)`：在 (az, el) 网格上批量渲剪影、用 numpy 扫 roll、
以剪影 IoU 为目标，粗筛后在最优邻域精修；输出三轴角＋残差＋可一键重渲的相机参数文件。

**为什么不 72×37×72 全网格重渲**：滚转在**远场近似**下等价于对剪影做 2D 旋转——
所以 (az, el) 粗网格**一个 Blender 会话里一次渲完**，roll 全程在 numpy 里转、不重渲。
粗网格渲完只对 `--top-k` 个候选做精修重渲。

**二维旋转近似**：正交（或长焦）投影下，绕视轴滚转 θ 与对图像做 2D 旋转 θ 等价到
"物体沿视向的深度差异可忽略"的程度；实测（`_selftest` 与本轮验收记录）在本任务尺度下
同一机位 roll=30° 与"roll=0 渲图做 −30° 2D 旋转"的剪影 IoU 为 0.92 量级，残差来自
盘/筒的前后错位。近似误差随物体"厚度/视距"增大而增大，物体越扁（近棱视）越准。

**约定**：`roll` 是**绕视轴的相机滚转**；2D 旋转用 PIL 的逆时针正角，
`roll = −(2D 旋转角)`——适配器侧按同一约定实现，已用两张实渲图标定（IoU 0.924）。

**渲染器接线**（工具内零任务数值）：`--renderer-cmd "<模板>"`，模板里可用

- `{jobs}`：**批任务 JSON 路径**（推荐）——一次调用渲完一批，渲染器自己写
  `<outdir>/render_manifest.json`（推荐给逐张相机参数）；
- `{outdir}`、`{res}`：批任务输出目录与 `W,H`；
- `{az}`、`{el}`、`{roll}`、`{out}`：单张模式（模板不含 `{jobs}` 时，工具逐张调用）。

现成接法见 :mod:`primer.imgcmp.adapters.observatory_silhouette`（观象台场景 → 双色剪影）。

**产物**（`--out DIR`）：`solve.json`（三轴角/残差/top-k/网格与耗时/记账）、
`camera.json`（一键重渲的相机参数＋可直接粘贴的命令）、`overlay.png`（基准红／最优绿，
重合处黄）、`renders/`（粗网格剪影）、`fine/`（精修剪影）。

语言纪律：stdout 只出中文人读报告；stderr／异常／选项名／JSON 键一律英文。
"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import tempfile
import time

import numpy as np
from PIL import Image

from .common import (
    ImgCmpError,
    KIND_BLACK,
    KIND_WHITE,
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
from .feat import label_runs, otsu_threshold, dilate

TOOL = "primer.imgcmp.pose"

MASK_RES = 256            # 精修/定案用的归一化画布边长
COARSE_RES = 128          # 粗筛画布边长（快 4 倍）
NORM_MARGIN = 0.06        # 归一化时四周留白比例
DEFAULT_COARSE = 5.0
DEFAULT_FINE = 1.0
DEFAULT_RES = 480
DEFAULT_TOP_K = 5
# 姿态歧义分档：top-1 与 top-2 的剪影 IoU 差
AMBIGUITY_STRONG = 0.004     # < 它：两支几乎并列（翻面/掉头歧义实测量级 0.0002–0.004）
AMBIGUITY_WEAK = 0.02        # < 它：存在一条"差得不多的另一支"（正常收敛实测 ≥0.05）
# 有解性门限（**启发式闸门，非判据**）：最优剪影 IoU 低于它 → 该基准图不足以定姿态。
# 取值来源（T2b 实测）：同源可解例 0.968；收窄搜索得到的伪解 0.849；论文 CAD 基准
# Fig.3(b) 0.737。0.80 会把 0.849 的伪解判成可解，故取 0.90。第二道闸是 `ambiguous`
# 分档——过了门限的仍可能是强/弱歧义，两档都要看。
DEFAULT_MIN_IOU = 0.90
DEFAULT_RENDER_TIMEOUT = 900.0

METHOD = {
    "silhouette": "outline_of(mask) = keep the largest 8-connected foreground region of the "
                  "luma Otsu mask (polarity by kind) and fill its holes; applied to the "
                  "baseline AND to every rendered silhouette, so IoU compares outlines rather "
                  "than 'did the renderer draw internal detail'",
    "normalisation": "crop to the mask bbox, scale so max(w,h) = (1 - 2*margin)*canvas, "
                     "centre on a canvas x canvas image. NOTE bbox normalisation is NOT "
                     "rotation invariant, so after each 2D roll rotation the mask is "
                     "re-normalised on its own bbox before the IoU - both sides then live in "
                     "the same 'longest edge fixed, centroid centred' gauge",
    "coarse": "render the (az, el) grid once (roll = 0) in a single renderer call, then sweep "
              "roll in numpy by rotating the rendered silhouette (far-field approximation)",
    "roll_convention": "roll is the camera roll about the view axis; a PIL rotation by angle a "
                       "equals camera roll -a; calibrated with two real renders (IoU 0.924 at "
                       "roll 30 vs a -30 deg 2D rotation)",
    "refine": "render a fine (az, el) neighbourhood (step = --fine) around the --top-k coarse "
              "candidates and sweep roll at full canvas resolution",
    "residual": "iou of the normalised masks; sym_diff_px = symmetric difference area in "
                "render pixels",
    "ambiguity": "ambiguous is graded by the top-1/top-2 silhouette IoU gap: 'strong' when "
                 "the gap < AMBIGUITY_STRONG (%.3f, the measured level of flip/heading "
                 "ambiguity), 'weak' when < AMBIGUITY_WEAK (%.3f), else 'none'. "
                 "ambiguity_gap carries the raw number and ambiguity_note explains the "
                 "suspected kind (el <-> -el, or az flipped by 180)."
                 % (AMBIGUITY_STRONG, AMBIGUITY_WEAK),
    "solvable": "solvable=false when the best silhouette IoU is below --min-iou "
                "(default %.2f, source: a same-source case that is genuinely solvable scores "
                "0.968, a narrowed-search pseudo-solution scores 0.849 and the paper CAD "
                "baseline Fig.3(b) scores 0.737; 0.80 would wrongly pass the 0.849 pseudo-"
                "solution, hence the gate sits at 0.90). --min-iou is only a heuristic gate, "
                "not a criterion - a passing case can still be 'ambiguous', which is the "
                "second gate. An unsolvable baseline cannot pin a pose: do not use its "
                "roll/azimuth for differences." % DEFAULT_MIN_IOU,
}


# ---------------------------------------------------------------- 剪影提取与归一化

def _fill_holes(mask: np.ndarray) -> np.ndarray:
    """把被前景包住、不与图像边框相连的背景连通域填成前景。"""
    background = ~mask
    labels, count = label_runs(background, connectivity=4)
    if count == 0:
        return mask
    border = np.concatenate([labels[0, :], labels[-1, :], labels[:, 0], labels[:, -1]])
    outside = np.unique(border[border > 0])
    filled = mask.copy()
    if outside.size:
        filled[~np.isin(labels, outside) & background] = True
    return filled


def outline_of(mask: np.ndarray) -> np.ndarray:
    """连通域统一口径：只留最大 8 邻接连通域 ＋ 填内部空洞。

    基准图与**所有渲染图**都过这一步——基准多是"填色 CAD 轮廓/带内线的图"，渲染可能是
    桁架那种带洞的双色剪影；不统一口径，IoU 比的就不是轮廓而是"谁有没有画内线"。
    """
    labels, count = label_runs(mask, connectivity=8)
    if count > 1:
        sizes = np.bincount(labels.ravel(), minlength=count + 1)
        sizes[0] = 0
        mask = labels == int(np.argmax(sizes))
    return _fill_holes(mask)


def silhouette_mask(rgb: np.ndarray, threshold: int | None = None):
    """图 → 剪影掩膜（最大连通域 + 填洞）。返回 ``(mask, kind, threshold)``。"""
    gray = to_gray(rgb)
    kind = detect_kind(rgb)
    thr = otsu_threshold(gray) if threshold is None else int(threshold)
    mask = gray <= thr if kind == KIND_WHITE else gray >= thr
    if not mask.any():
        raise ImgCmpError("silhouette mask is empty (kind=%s, threshold=%d)" % (kind, thr))
    return outline_of(mask), kind, thr


def bbox_of(mask: np.ndarray):
    ys, xs = np.nonzero(mask)
    return [int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1]


def normalize_mask(mask: np.ndarray, canvas: int = MASK_RES):
    """裁到 bbox → 按最长边缩放到 ``(1-2·margin)·canvas`` → 居中贴到 ``canvas²`` 画布。"""
    x0, y0, x1, y1 = bbox_of(mask)
    crop = mask[y0:y1, x0:x1]
    h, w = crop.shape
    target = max(1, int(round(canvas * (1.0 - 2.0 * NORM_MARGIN))))
    scale = target / float(max(w, h))
    new_w = max(1, int(round(w * scale)))
    new_h = max(1, int(round(h * scale)))
    img = Image.fromarray(np.where(crop, 255, 0).astype(np.uint8), mode="L")
    img = img.resize((new_w, new_h), Image.BILINEAR)
    out = np.zeros((canvas, canvas), dtype=np.uint8)
    ox = (canvas - new_w) // 2
    oy = (canvas - new_h) // 2
    out[oy:oy + new_h, ox:ox + new_w] = np.asarray(img, dtype=np.uint8)
    return out > 127, scale


def rotate_mask(mask_u8: np.ndarray, angle_deg: float) -> np.ndarray:
    """2D 旋转（PIL，逆时针正角），返回 bool 掩膜。``angle_deg == 0`` 时原样返回。"""
    if abs(angle_deg) < 1e-9:
        return mask_u8 > 0
    img = Image.fromarray((mask_u8 > 0).astype(np.uint8) * 255, mode="L")
    img = img.rotate(float(angle_deg), resample=Image.BILINEAR, fillcolor=0)
    return np.asarray(img, dtype=np.uint8) > 127


def iou(a: np.ndarray, b: np.ndarray) -> float:
    inter = int(np.count_nonzero(a & b))
    union = int(np.count_nonzero(a | b))
    return 0.0 if union == 0 else inter / float(union)


def principal_angle(mask: np.ndarray) -> float:
    """二阶矩主轴方向（度，mod 180）。用于把 roll 搜索窗对准，不用于定案。"""
    ys, xs = np.nonzero(mask)
    if xs.size < 8:
        return 0.0
    x = xs.astype(np.float64) - xs.mean()
    y = ys.astype(np.float64) - ys.mean()
    mu20 = float((x * x).mean())
    mu02 = float((y * y).mean())
    mu11 = float((x * y).mean())
    return math.degrees(0.5 * math.atan2(2.0 * mu11, mu20 - mu02)) % 180.0


# ---------------------------------------------------------------- 渲染器接线

PLACEHOLDERS = ("{az}", "{el}", "{roll}", "{out}", "{jobs}", "{outdir}", "{res}")


class Renderer:
    """按模板驱动外部渲染器；批模式优先（一个会话渲完一批）。"""

    def __init__(self, template: str, out_dir: str, res: int, timeout: float,
                 log=None):
        if not template or not template.strip():
            raise ImgCmpError("--renderer-cmd is required")
        for token in ("{az}", "{el}", "{out}", "{jobs}"):
            if token in template:
                break
        else:
            raise ImgCmpError("--renderer-cmd must contain at least one of "
                              "{jobs} (batch) or {az}/{el}/{out} (single pose)")
        if "{jobs}" not in template and ("{az}" not in template or "{el}" not in template
                                         or "{out}" not in template):
            raise ImgCmpError("single-pose --renderer-cmd needs {az}, {el}, {roll} and {out}")
        self.template = template
        self.out_dir = out_dir
        self.res = int(res)
        self.timeout = float(timeout)
        self.batch = "{jobs}" in template
        self.log = log or []
        self.seconds = 0.0
        self.calls = 0

    def _fill(self, **kw):
        cmd = self.template
        for key, value in kw.items():
            cmd = cmd.replace("{%s}" % key, str(value))
        return cmd

    def _run(self, cmd: str):
        self.calls += 1
        t0 = time.time()
        try:
            proc = subprocess.run(cmd, shell=True, capture_output=True, timeout=self.timeout)
        except subprocess.TimeoutExpired as exc:
            raise ImgCmpError("renderer timed out after %ss: %s" % (self.timeout, cmd)) from exc
        self.seconds += time.time() - t0
        tail = (proc.stdout or b"")[-400:].decode("utf-8", "replace")
        err = (proc.stderr or b"")[-1500:].decode("utf-8", "replace")
        self.log.append({"cmd": cmd, "returncode": proc.returncode,
                         "seconds": round(time.time() - t0, 3), "stdout_tail": tail})
        if proc.returncode != 0:
            raise ImgCmpError("renderer failed (exit %d): %s\n%s"
                              % (proc.returncode, cmd, err))

    def render(self, poses, work_dir: str) -> list:
        """``poses`` = ``[(az, el, roll), ...]`` → 返回 ``[{az, el, roll, out}, ...]``。"""
        ensure_dir(work_dir)
        shots = []
        for i, (az, el, roll) in enumerate(poses):
            shots.append({"az": float(az), "el": float(el), "roll": float(roll),
                          "out": os.path.abspath(
                              os.path.join(work_dir, "shot_%05d_az%.2f_el%.2f_r%.2f.png"
                                           % (i, az, el, roll)))})
        if self.batch:
            job_path = os.path.join(work_dir, "_jobs.json")
            write_json(job_path, {"res": [self.res, self.res], "shots": shots})
            self._run(self._fill(jobs=job_path, outdir=os.path.abspath(work_dir),
                                 res="%d,%d" % (self.res, self.res)))
        else:
            for shot in shots:
                self._run(self._fill(az=shot["az"], el=shot["el"], roll=shot["roll"],
                                     out=shot["out"], res="%d,%d" % (self.res, self.res),
                                     outdir=os.path.abspath(work_dir)))
        missing = [s["out"] for s in shots if not os.path.isfile(s["out"])]
        if missing:
            raise ImgCmpError("renderer produced no image for %d pose(s), first: %s"
                              % (len(missing), missing[0]))
        return shots


def read_rendered_mask(path: str, outline: bool = True) -> np.ndarray:
    """渲出来的剪影读成 bool（严格双色：亮＝对象），并按与基准相同的口径取轮廓。"""
    arr = np.asarray(Image.open(path).convert("L"), dtype=np.uint8)
    mask = arr > 127
    return outline_of(mask) if outline else mask


# ---------------------------------------------------------------- 反求

def _roll_angles(roll_range, step: float):
    """roll 搜索角表（含闭区间端点）。"""
    lo, hi = float(roll_range[0]), float(roll_range[1])
    if hi < lo:
        raise ImgCmpError("--roll-range must be A0,A1 with A1 >= A0, got: %r" % (roll_range,))
    out, a = [], lo
    while a <= hi + 1e-9:
        out.append(round(a, 4))
        a += step
    if not out:
        raise ImgCmpError("--roll-range produced no angle, got: %r" % (roll_range,))
    return out


def pad_for_rotation(mask: np.ndarray) -> np.ndarray:
    """裁到 bbox 再补成正方形（边长＝bbox 对角线）：转起来不切角，又比整幅画布小得多。"""
    x0, y0, x1, y1 = bbox_of(mask)
    crop = mask[y0:y1, x0:x1]
    h, w = crop.shape
    side = int(math.ceil(math.hypot(w, h))) + 2
    out = np.zeros((side, side), dtype=bool)
    oy, ox = (side - h) // 2, (side - w) // 2
    out[oy:oy + h, ox:ox + w] = crop
    return out


def _roll_scan(render_mask: np.ndarray, base_norm: np.ndarray, canvas: int, step: float,
               seeds=None, roll_range=(0.0, 355.0)):
    """对一张剪影扫 roll。

    **关键**：旋转之后要**重新归一化**再比——因为"按 bbox 归一化"本身不是旋转不变的
    （同一个形状转过 30° 后外接框会变大），不重归一化会把滚转搜索带偏。旋转后再按各自
    bbox 归一化，两边就都落在"最长边 = 定值、以质心居中"的同一口径上。
    返回 ``(best_roll, best_iou, n_eval)``。
    """
    padded = pad_for_rotation(render_mask)
    best = (-1e9, 0.0)
    n = 0
    angles = seeds if seeds is not None else _roll_angles(roll_range, step)
    for roll in angles:
        rot = rotate_mask(padded, -float(roll))
        if not rot.any():
            continue
        value = iou(normalize_mask(rot, canvas)[0], base_norm)
        n += 1
        if value > best[0]:
            best = (value, float(roll))
    return best[1], best[0], n


def _grid(az0: float, az1: float, az_step: float, el0: float, el1: float, el_step: float):
    azs, els = [], []
    a = az0
    while a <= az1 + 1e-9:
        azs.append(round(a, 4))
        a += az_step
    e = el0
    while e <= el1 + 1e-9:
        els.append(round(e, 4))
        e += el_step
    return [(az, el) for az in azs for el in els]


def solve(baseline_path: str, renderer: Renderer, out_dir: str, coarse: float = DEFAULT_COARSE,
          fine: float = DEFAULT_FINE, top_k: int = DEFAULT_TOP_K,
          az_range=(0.0, 355.0), el_range=(-60.0, 60.0), roll_range=(0.0, 355.0),
          roll_step=None, res: int = DEFAULT_RES, keep_renders: bool = True,
          min_iou: float = DEFAULT_MIN_IOU) -> dict:
    """反求基准图的姿态。返回结果 dict 并写盘。"""
    if coarse <= 0 or fine <= 0:
        raise ImgCmpError("--coarse and --fine must be > 0")
    if top_k < 1:
        raise ImgCmpError("--top-k must be >= 1")
    t_start = time.time()
    rgb = load_rgb(baseline_path)
    base_mask, kind, thr = silhouette_mask(rgb)
    base_norm = normalize_mask(base_mask, MASK_RES)[0]
    base_coarse = normalize_mask(base_mask, COARSE_RES)[0]
    base_area = int(base_mask.sum())

    coarse_roll_step = float(roll_step if roll_step else max(2.0, coarse))
    work = os.path.join(out_dir, "renders")
    poses = [(az, el, 0.0)
             for az, el in _grid(az_range[0], az_range[1], coarse,
                                 el_range[0], el_range[1], coarse)]

    t0 = time.time()
    shots = renderer.render(poses, work)
    render_s = time.time() - t0

    # 粗筛：一次渲完，roll 全程 numpy
    t0 = time.time()
    scored = []
    n_eval = 0
    for shot in shots:
        mask = read_rendered_mask(shot["out"])
        if not mask.any():
            continue
        roll, value, n = _roll_scan(mask, base_coarse, COARSE_RES, coarse_roll_step,
                                    roll_range=roll_range)
        n_eval += n
        scored.append({"az": shot["az"], "el": shot["el"], "roll": roll, "iou": value})
    scored.sort(key=lambda r: -r["iou"])
    roll_s = time.time() - t0
    if not scored:
        raise ImgCmpError("coarse stage produced no usable silhouette")

    # 精修：在 top-k 邻域重渲小网格（step = fine），roll 再扫一遍（全画布）
    t0 = time.time()
    fine_poses, seen = [], set()
    for cand in scored[:top_k]:
        for az, el in _grid(cand["az"] - coarse, cand["az"] + coarse, fine,
                            cand["el"] - coarse, cand["el"] + coarse, fine):
            key = (round(az, 4), round(el, 4))
            if key in seen:
                continue
            seen.add(key)
            fine_poses.append((key[0], key[1], 0.0))
    fine_work = os.path.join(out_dir, "fine")
    fine_shots = renderer.render(fine_poses, fine_work)
    fine_rows = []
    for shot in fine_shots:
        mask = read_rendered_mask(shot["out"])
        if not mask.any():
            continue
        # 精修的 roll 种子窗必须覆盖"粗筛把 roll 量化到 roll_step"带来的误差：
        # 粗筛只在本网格点上扫 roll，真值可能离最近的粗筛点差 roll_step/2。
        # 窗口开成 max(coarse, roll_step/2)，否则真值会被窗口边缘截住（实测踩过：
        # roll_step=30 时全部滚转误差被截成 ±5°）。
        window = max(coarse, coarse_roll_step / 2.0)
        seeds = [cand["roll"] + d for d in np.arange(-window, window + 1e-9, fine)
                 if roll_range[0] - 1e-9 <= cand["roll"] + d <= roll_range[1] + 1e-9]
        roll, value, _n = _roll_scan(mask, base_norm, MASK_RES, fine, seeds=seeds)
        fine_rows.append({"az": shot["az"], "el": shot["el"], "roll": roll, "iou": value})
    fine_rows.sort(key=lambda r: -r["iou"])
    fine_s = time.time() - t0
    if not fine_rows:
        raise ImgCmpError("fine stage produced no usable silhouette")

    top = fine_rows[:max(top_k, 1)]
    best = top[0]
    gap = round(top[0]["iou"] - top[1]["iou"], 6) if len(top) > 1 else None
    if gap is None:
        ambiguous = "none"
    elif gap < AMBIGUITY_STRONG:
        ambiguous = "strong"
    elif gap < AMBIGUITY_WEAK:
        ambiguous = "weak"
    else:
        ambiguous = "none"
    solvable = bool(best["iou"] >= min_iou)
    best_shot = next(s for s in fine_shots
                     if abs(s["az"] - best["az"]) < 1e-6 and abs(s["el"] - best["el"]) < 1e-6)
    best_mask = read_rendered_mask(best_shot["out"])
    best_norm = normalize_mask(best_mask, MASK_RES)[0]
    best_rot = normalize_mask(rotate_mask(pad_for_rotation(best_mask), -best["roll"]),
                              MASK_RES)[0]
    sym = int(np.count_nonzero(best_rot ^ base_norm))
    sym_px = sym * (best_mask.size / float(MASK_RES * MASK_RES))

    _write_overlay(base_norm, best_rot, os.path.join(out_dir, "overlay.png"))
    result = {
        "tool": TOOL,
        "mode": "solve",
        "baseline": {
            "path": os.path.abspath(baseline_path),
            "image_size": [int(rgb.shape[1]), int(rgb.shape[0])],
            "kind": kind, "threshold": int(thr),
            "silhouette_px": base_area,
            "bbox_xyxy": bbox_of(base_mask),
        },
        "angles": {"az": round(best["az"], 4), "el": round(best["el"], 4),
                   "roll": round(best["roll"], 4)},
        "residual": {"iou": round(best["iou"], 6),
                     "sym_diff_px_norm": sym,
                     "sym_diff_px_render": round(sym_px, 2)},
        "top_k": [{"az": round(r["az"], 4), "el": round(r["el"], 4),
                   "roll": round(r["roll"], 4), "iou": round(r["iou"], 6)} for r in top],
        "ambiguous": ambiguous,
        "ambiguity_gap": gap,
        "ambiguity_note": (_ambiguity_note(top[0], top[1], ambiguous) if ambiguous != "none"
                           else "无歧义：最优与次优剪影 IoU 差 %.4f ≥ weak 阈值 %.3f。"
                           % (gap, AMBIGUITY_WEAK) if gap is not None else
                           "无歧义：精修只产生了一个候选。"),
        "solvable": solvable,
        "min_iou": min_iou,
        "solvable_note": ("基准侧最优剪影 IoU %.4f ≥ 门限 %.2f：该图足以定姿态。"
                          % (best["iou"], min_iou) if solvable else
                          "基准侧最优剪影 IoU %.4f < 门限 %.2f：**该图不足以定姿态**，"
                          "其滚转/方位不要拿去作差——多半是示意画法、比例或材质与渲染器"
                          "不一致，可信度由相似度上限决定。"
                          % (best["iou"], min_iou)),
        "grid": {"coarse": coarse, "fine": fine, "roll_step_coarse": coarse_roll_step,
                 "roll_step_fine": fine, "az_range": list(az_range),
                 "el_range": list(el_range), "roll_range": list(roll_range),
                 "coarse_poses": len(poses),
                 "fine_poses": len(fine_poses), "roll_evaluations": n_eval},
        "renderer": {"command": renderer.template, "mode": "batch" if renderer.batch else "single",
                     "res": res, "calls": renderer.calls,
                     "manifest": os.path.join(os.path.abspath(work), "render_manifest.json")
                     if os.path.isfile(os.path.join(work, "render_manifest.json")) else None},
        "timings_s": {"render_coarse": round(render_s, 3),
                      "roll_scan_coarse": round(roll_s, 3),
                      "render_fine": round(fine_s, 3),
                      "renderer_total": round(renderer.seconds, 3),
                      "total": round(time.time() - t_start, 3)},
        "method": METHOD,
    }
    ensure_dir(out_dir)
    write_json(os.path.join(out_dir, "solve.json"), result)
    write_json(os.path.join(out_dir, "camera.json"),
               camera_record(result, renderer, res, out_dir))
    if not keep_renders:
        for path in (work, fine_work):
            shutil.rmtree(path, ignore_errors=True)
    return result


def _ambiguity_note(first, second, level: str = "weak") -> str:
    """中文一句：说清"为什么可能不是唯一解"、程度以及方向。"""
    d_el = abs(first["el"] + second["el"]) < 15.0 and abs(first["el"] - second["el"]) > 15.0
    d_az = abs(((first["az"] - second["az"] + 180.0) % 360.0) - 180.0) > 120.0
    bits = []
    if d_el:
        bits.append("俯仰反号（el ↔ −el）")
    if d_az:
        bits.append("方位掉头（az 差约 180°）")
    what = "、".join(bits) if bits else "同一支上的两个近邻姿态"
    level_cn = {"strong": "强（两支几乎并列，等于该图定不了姿态）",
                "weak": "弱（存在一条差得不多的另一支，建议复核）"}.get(level, level)
    return ("歧义[%s]：最优 (%.1f, %.1f, %.1f) IoU %.4f 与次优 (%.1f, %.1f, %.1f) IoU %.4f "
            "只差 %.4f，疑似 %s；近轴对称/盘状物体的剪影本来就有两个近乎等高的谷，"
            "该图不足以唯一确定姿态——请收窄 --az-range/--el-range/--roll-range、"
            "提高 --min-iou 判为不可解，或补一张不同机位的图。"
            % (level_cn, first["az"], first["el"], first["roll"], first["iou"],
               second["az"], second["el"], second["roll"], second["iou"],
               first["iou"] - second["iou"], what))


def camera_record(result: dict, renderer: Renderer, res: int, out_dir: str) -> dict:
    """一键重渲的相机参数：角度 + 渲染器命令（占位符已填成真实值）。"""
    angles = result["angles"]
    cmd = renderer.template
    cmd = cmd.replace("{az}", "%.4f" % angles["az"]).replace("{el}", "%.4f" % angles["el"])
    cmd = cmd.replace("{roll}", "%.4f" % angles["roll"]).replace("{res}", "%d,%d" % (res, res))
    cmd = cmd.replace("{outdir}", os.path.abspath(os.path.join(out_dir, "rerender")))
    cmd = cmd.replace("{out}", os.path.abspath(os.path.join(out_dir, "rerender", "pose.png")))
    return {
        "tool": TOOL,
        "kind": "camera_for_rerender",
        "angles": angles,
        "res": [int(res), int(res)],
        "renderer_cmd_resolved": cmd,
        "note": "把 renderer_cmd_resolved 原样执行即可渲出同视角剪影；角度/分辨率已代入。"
                "批模式（模板含 {jobs}）下 {jobs} 已不再可用，请用适配器的单张模式或 "
                "把它换成 --az/--el/--roll/--out。",
        "solve_json": os.path.abspath(os.path.join(out_dir, "solve.json")),
    }


def _write_overlay(base_norm: np.ndarray, best_rot: np.ndarray, path: str) -> None:
    """基准红／最优绿，重合黄——人眼看残差用。"""
    canvas = np.zeros((base_norm.shape[0], base_norm.shape[1], 3), dtype=np.uint8)
    canvas[..., 0] = np.where(base_norm, 255, 0)
    canvas[..., 1] = np.where(best_rot, 255, 0)
    Image.fromarray(canvas, mode="RGB").save(path)


# ---------------------------------------------------------------- 中文报告

def _print_report(result: dict) -> None:
    angles = result["angles"]
    residual = result["residual"]
    grid = result["grid"]
    timing = result["timings_s"]
    rows = [
        ("基准图", "%s（%d x %d，%s，剪影 %d px）"
                   % (result["baseline"]["path"], result["baseline"]["image_size"][0],
                      result["baseline"]["image_size"][1], result["baseline"]["kind"],
                      result["baseline"]["silhouette_px"])),
        ("反求姿态", "方位 %.2f°　俯仰 %.2f°　滚转 %.2f°"
                     % (angles["az"], angles["el"], angles["roll"])),
        ("残差", "剪影 IoU %.4f（门限 %.2f → 该图%s）；对称差 %d px（归一化画布）/ %.0f px（渲染尺度）"
                 % (residual["iou"], result["min_iou"],
                    "足以定姿态" if result["solvable"] else "**不足以定姿态**",
                    residual["sym_diff_px_norm"], residual["sym_diff_px_render"])),
        ("歧义", "%s（top-1/top-2 IoU 差 %s）"
                 % ({"none": "无", "weak": "弱", "strong": "强"}[result["ambiguous"]],
                    "%.4f" % result["ambiguity_gap"]
                    if result["ambiguity_gap"] is not None else "-")),
        ("网格", "粗 %g° × %d 位姿 → 精 %g° × %d 位姿；roll 扫描 %d 次（全在 numpy）"
                 % (grid["coarse"], grid["coarse_poses"], grid["fine"], grid["fine_poses"],
                    grid["roll_evaluations"])),
        ("渲染器", "%s 模式，%d 次调用，%.1fs"
                   % (result["renderer"]["mode"], result["renderer"]["calls"],
                      timing["renderer_total"])),
        ("耗时", "粗渲 %.1fs｜roll 扫描 %.1fs｜精修渲 %.1fs｜合计 %.1fs"
                 % (timing["render_coarse"], timing["roll_scan_coarse"],
                    timing["render_fine"], timing["total"])),
    ]
    print("[imgcmp] T2b 姿态反求：方位 %.1f°／俯仰 %.1f°／滚转 %.1f°（IoU %.4f）"
          % (angles["az"], angles["el"], angles["roll"], residual["iou"]))
    for key, value in rows:
        print("  %-8s  %s" % (key, value))
    print("  %-8s  %s" % ("候选取", "、".join(
        "#%d (%.1f, %.1f, %.1f) IoU %.4f" % (i, r["az"], r["el"], r["roll"], r["iou"])
        for i, r in enumerate(result["top_k"]))))
    print("  · 滚转在远场近似下用 2D 旋转代替重渲；相机参数见 camera.json，可直接一键重渲。")


# ---------------------------------------------------------------- selftest

def _synth_silhouette(width: int = 200, height: int = 200) -> np.ndarray:
    """自造剪影：一个明显取向的"杆＋盘"形状（有长轴，便于验旋转/平移/缩放不变性）。"""
    mask = np.zeros((height, width), dtype=bool)
    mask[40:150, 60:74] = True        # 竖直杆
    mask[140:170, 30:170] = True      # 底部横盘（与竖杆相接）
    mask[30:44, 60:150] = True        # 杆顶向右侧伸出的横臂（与竖杆相接）
    return mask                                  # 单一连通剪影：有明确长轴与取向


def _stub_renderer(base_mask: np.ndarray, canvas: int, transform):
    """给 selftest 用的假渲染器：按 ``transform(az, el, roll)`` 生成剪影。"""
    def render(poses, work_dir):
        ensure_dir(work_dir)
        shots = []
        for i, (az, el, roll) in enumerate(poses):
            mask = transform(base_mask, az, el, roll)
            path = os.path.abspath(os.path.join(work_dir, "stub_%05d.png" % i))
            Image.fromarray(np.where(mask, 255, 0).astype(np.uint8), mode="L").save(path)
            shots.append({"az": float(az), "el": float(el), "roll": float(roll), "out": path})
        return shots
    return render


class _StubRenderer:
    """最小渲染器桩：替代 Renderer 给 selftest 用（只需 .render/.batch/.template）。"""

    def __init__(self, render_fn):
        self.render = render_fn
        self.batch = True
        self.template = "stub"
        self.calls = 0
        self.seconds = 0.0


def _selftest() -> int:
    checks = []
    shape = _synth_silhouette()

    # 1) 归一化对平移/缩放不变
    def shifted(scale, dx, dy):
        img = Image.fromarray(np.where(shape, 255, 0).astype(np.uint8), mode="L")
        img = img.resize((int(shape.shape[1] * scale), int(shape.shape[0] * scale)),
                         Image.BILINEAR)
        arr = np.asarray(img) > 127
        pad = np.zeros((arr.shape[0] + 2 * abs(dy), arr.shape[1] + 2 * abs(dx)), dtype=bool)
        pad[dy:dy + arr.shape[0], dx:dx + arr.shape[1]] = arr
        return pad

    ref = normalize_mask(shape, MASK_RES)[0]
    moved = normalize_mask(shifted(2.3, 17, 11), MASK_RES)[0]
    checks.append(("normalise_invariant", iou(ref, moved) > 0.97,
                   "缩放 2.3× ＋ 平移 (17,11) 后归一化 IoU %.4f（判据 >0.97）"
                   % iou(ref, moved)))

    # 2) 旋转-IoU 单调性：偏离越大 IoU 越小
    base_norm = normalize_mask(shape, MASK_RES)[0]
    seq = []
    for delta in (0, 5, 10, 20, 40, 90):
        rot = rotate_mask(normalize_mask(shifted(1.0, 0, 0), MASK_RES)[0], delta)
        seq.append(iou(rot, base_norm))
    mono = all(seq[i] > seq[i + 1] for i in range(len(seq) - 1))
    checks.append(("rotation_iou_monotone", mono and abs(seq[0] - 1.0) < 1e-9,
                   "旋转 0/5/10/20/40/90° 的 IoU %s"
                   % " ".join("%.3f" % v for v in seq)))

    # 3) 端到端反求：假渲染器按 (el, roll) 造剪影，反求要收敛到真值
    true_el, true_roll = 25.0, 37.0

    def _fake_render(mask, az, el, roll):
        """最简"渲染器"：el → 竖直拉伸（视角形变）；**相机 roll 最后一次作用** → 2D 旋转。

        顺序很关键：相机滚转在图像空间是最后一步，所以只有"先形变、后旋转"才等价于
        "对成图做 2D 旋转"——这也正是 T2b 用 numpy 转剪影代替重渲的前提。
        """
        canvas = np.zeros((320, 320), dtype=bool)
        canvas[60:260, 60:260] = mask
        img = Image.fromarray(np.where(canvas, 255, 0).astype(np.uint8), mode="L")
        w, h = img.size
        img = img.resize((w, max(1, int(round(h * (1.0 + 0.01 * float(el)))))), Image.BILINEAR)
        img = img.rotate(-float(roll), resample=Image.BILINEAR, fillcolor=0)
        return np.asarray(img, dtype=np.uint8) > 127

    stub = _StubRenderer(_stub_renderer(shape, MASK_RES, _fake_render))
    with tempfile.TemporaryDirectory() as tmp:
        target = os.path.join(tmp, "baseline.png")
        base_view = _fake_render(shape, 0.0, true_el, true_roll)
        Image.fromarray(np.where(base_view, 255, 0).astype(np.uint8), mode="L") \
            .convert("RGB").save(target)
        result = solve(target, stub, os.path.join(tmp, "solve"), coarse=10.0, fine=2.0,
                       top_k=2, az_range=(0.0, 0.0), el_range=(-60.0, 60.0), roll_step=10.0,
                       keep_renders=True)
        files_ok = all(os.path.isfile(os.path.join(tmp, "solve", name))
                       for name in ("solve.json", "camera.json", "overlay.png"))
    got = result["angles"]
    roll_err = min(abs(got["roll"] - true_roll), 360 - abs(got["roll"] - true_roll))
    el_err = abs(got["el"] - true_el)
    checks.append(("solve_converges", roll_err <= 3.0 and el_err <= 3.0
                   and result["residual"]["iou"] > 0.90,
                   "真值 (el %.0f°, roll %.0f°) → 反求 (el %.1f°, roll %.1f°)，"
                   "误差 (%.1f°, %.1f°)，IoU %.4f"
                   % (true_el, true_roll, got["el"], got["roll"], el_err, roll_err,
                      result["residual"]["iou"])))
    checks.append(("top_k_reported", len(result["top_k"]) == 2
                   and result["top_k"][0]["iou"] >= result["top_k"][1]["iou"],
                   "top-k %d 条且按 IoU 降序（%.4f ≥ %.4f）"
                   % (len(result["top_k"]), result["top_k"][0]["iou"],
                      result["top_k"][1]["iou"])))
    checks.append(("camera_file", files_ok,
                   "solve.json / camera.json / overlay.png 均已落盘"))

    # 4) 主轴先验与真实 roll 一致（搜索窗对准用）
    a0 = principal_angle(normalize_mask(shape, MASK_RES)[0])
    a1 = principal_angle(rotate_mask(normalize_mask(shape, MASK_RES)[0], true_roll))
    # PIL rotate(+a) 使图像坐标下的主轴角 −a（y 轴向下的约定），故关系取补角
    delta = (a0 - a1) % 180.0
    checks.append(("principal_angle_tracks_rotation",
                   abs(delta - (true_roll % 180.0)) < 3.0,
                   "旋转 %.0f° 后主轴变化 %.1f°（期望 %.1f°）"
                   % (true_roll, delta, true_roll % 180.0)))

    # 5) 剪影提取：白底图里"最大连通域 + 填洞"
    img = np.full((120, 140, 3), 250, dtype=np.uint8)
    img[20:100, 30:110] = 30
    img[40:60, 60:80] = 250          # 内部空洞
    img[10:16, 10:16] = 30           # 另有一小块（应被丢掉）
    mask, kind, _thr = silhouette_mask(img)
    checks.append(("silhouette_from_white",
                   kind == KIND_WHITE and int(mask.sum()) == 80 * 80 and mask[50, 70],
                   "白底图 → %s，剪影 %d px（期望 6400，洞已填），小碎块已丢弃"
                   % (kind, int(mask.sum()))))

    # 6) 渲染器模板校验：坏模板要报错
    bad = 0
    for tmpl in ("", "render --foo", "render {az} {el}"):
        try:
            Renderer(tmpl, ".", 64, 10.0)
        except ImgCmpError:
            bad += 1
    checks.append(("renderer_template_guard", bad == 3,
                   "3 个坏模板全部被拒（%d/3）" % bad))

    return selftest_report(TOOL, checks)


# ---------------------------------------------------------------- CLI

def build_parser():
    parser = make_parser(
        TOOL,
        "T2b pose solving by silhouette template matching: render the (az, el) grid once "
        "through a caller-supplied renderer, sweep roll in numpy by 2D rotation (far-field "
        "approximation), maximise silhouette IoU, then re-render a fine neighbourhood around "
        "the top-k candidates. Emits angles, residuals and a one-click camera file.",
    )
    sub = parser.add_subparsers(dest="command")
    solve_cmd = sub.add_parser("solve", help="solve the camera pose of a baseline image")
    solve_cmd.add_argument("baseline", help="baseline image (figure crop or render)")
    solve_cmd.add_argument("--renderer-cmd", required=True, metavar="TEMPLATE",
                           help="renderer command template; placeholders {jobs} {outdir} {res} "
                                "(batch, preferred) and {az} {el} {roll} {out} (single pose)")
    solve_cmd.add_argument("--out", metavar="DIR", help="output directory (required)")
    solve_cmd.add_argument("--coarse", type=float, default=DEFAULT_COARSE, metavar="DEG",
                           help="coarse grid step in degrees (default 5)")
    solve_cmd.add_argument("--fine", type=float, default=DEFAULT_FINE, metavar="DEG",
                           help="fine refinement step in degrees (default 1)")
    solve_cmd.add_argument("--top-k", type=int, default=DEFAULT_TOP_K, metavar="K",
                           help="candidates carried into refinement (default 5)")
    solve_cmd.add_argument("--res", type=int, default=DEFAULT_RES, metavar="PX",
                           help="render resolution (square, default 480)")
    solve_cmd.add_argument("--az-range", default="0,355", metavar="A0,A1",
                           help="azimuth sweep range in degrees (default 0,355)")
    solve_cmd.add_argument("--el-range", default="-60,60", metavar="E0,E1",
                           help="elevation sweep range in degrees (default -60,60)")
    solve_cmd.add_argument("--roll-range", default="0,355", metavar="R0,R1",
                           help="roll sweep range in degrees (default 0,355); narrow it when "
                                "the roll is known to lie in a band - this also cuts the "
                                "el<->-el / az-flip ambiguity")
    solve_cmd.add_argument("--roll-step", type=float, default=None, metavar="DEG",
                           help="roll sweep step in the coarse stage (default = --coarse)")
    solve_cmd.add_argument("--min-iou", type=float, default=DEFAULT_MIN_IOU, metavar="F",
                           help="heuristic solvability gate (not a criterion): a baseline "
                                "whose best silhouette IoU is below it is reported "
                                "solvable=false (default %.2f; 'ambiguous' is the second gate)"
                                % DEFAULT_MIN_IOU)
    solve_cmd.add_argument("--renderer-timeout", type=float, default=DEFAULT_RENDER_TIMEOUT,
                           metavar="S", help="wall-clock limit per renderer call")
    solve_cmd.add_argument("--drop-renders", action="store_true",
                           help="delete intermediate silhouette renders after solving")
    solve_cmd.add_argument("--selftest", action="store_true", help="run the built-in self-test")
    return parser


def _pair(text, what):
    parts = [p.strip() for p in str(text).split(",")]
    if len(parts) != 2:
        raise ImgCmpError("%s must be 'A,B', got: %r" % (what, text))
    try:
        return float(parts[0]), float(parts[1])
    except ValueError as exc:
        raise ImgCmpError("%s must be numbers, got: %r" % (what, text)) from exc


def main(argv=None) -> int:
    return run_main(TOOL, _run, argv)


def _run(argv) -> int:
    args = list(argv) if argv is not None else None
    if args is not None and args and args[0] == "--selftest":
        return _selftest()
    ns = build_parser().parse_args(args)
    if getattr(ns, "selftest", False):
        return _selftest()
    if not ns.command:
        build_parser().print_usage()
        raise ImgCmpError("a subcommand is required: solve (or --selftest)")
    if not ns.out:
        raise ImgCmpError("--out DIR is required")
    renderer = Renderer(ns.renderer_cmd, ns.out, ns.res, ns.renderer_timeout)
    result = solve(ns.baseline, renderer, ns.out, coarse=ns.coarse, fine=ns.fine,
                   top_k=ns.top_k, az_range=_pair(ns.az_range, "--az-range"),
                   el_range=_pair(ns.el_range, "--el-range"),
                   roll_range=_pair(ns.roll_range, "--roll-range"), roll_step=ns.roll_step,
                   res=ns.res, keep_renders=not ns.drop_renders, min_iou=ns.min_iou)
    _print_report(result)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
