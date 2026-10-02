# -*- coding: utf-8 -*-
"""imgcmp 公共层：图像 I/O、ROI 几何、底别判定、报告与 JSON 约定。

**全工具共享约定**（改这里等于改全部工具的接口，务必谨慎）：

- **底别**：``white`` ＝白底图（论文/CAD 截图，背景近白、图元为深色墨）；
  ``black`` ＝黑底图（空间渲染，背景近黑、对象为亮部）。由 :func:`detect_kind` 判定。
- **ROI**：一律原图像素坐标 ``(x, y, w, h)``，左闭右开；CLI 用 ``"x,y,w,h"`` 字符串。
- **框**：内部统一用 ``(x0, y0, x1, y1)``（:func:`box_xyxy`），与 PIL 的 crop 一致。
- **尺度**：一切"原生分辨率"块必须与源图**逐像素相同**，不得经过缩放或重采样；
  这条是 T1 验收"某单块内原生可读"可客观判定的基础（逐像素比对即可证）。
- **语言**：stdout 只出中文人读报告；stderr／异常／选项名／JSON 键一律英文。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys

import numpy as np
from PIL import Image

KIND_WHITE = "white"
KIND_BLACK = "black"


class ImgCmpError(Exception):
    """User-facing failure. Message is English; CLI turns it into exit code 2."""


# ---------------------------------------------------------------- 图像 I/O

def ensure_dir(path: str) -> str:
    if path:
        os.makedirs(path, exist_ok=True)
    return path


def load_rgb(path: str) -> np.ndarray:
    """读为 RGB uint8 数组 (H, W, 3)。读失败抛 :class:`ImgCmpError`（禁止静默通过）。"""
    if not os.path.isfile(path):
        raise ImgCmpError("image not found: %s" % path)
    try:
        with Image.open(path) as im:
            return np.asarray(im.convert("RGB"), dtype=np.uint8)
    except Exception as exc:  # noqa: BLE001
        raise ImgCmpError("cannot read image %s: %s" % (path, exc)) from exc


def load_gray(path: str) -> np.ndarray:
    return to_gray(load_rgb(path))


def save_rgb(arr: np.ndarray, path: str) -> str:
    ensure_dir(os.path.dirname(path))
    Image.fromarray(np.asarray(arr, dtype=np.uint8), mode="RGB").save(path)
    return path


def save_gray(arr: np.ndarray, path: str) -> str:
    ensure_dir(os.path.dirname(path))
    Image.fromarray(np.asarray(arr, dtype=np.uint8), mode="L").save(path)
    return path


def to_gray(rgb: np.ndarray) -> np.ndarray:
    """Rec.601 亮度，uint8。"""
    a = np.asarray(rgb, dtype=np.float32)
    if a.ndim == 2:
        return np.clip(a, 0, 255).astype(np.uint8)
    g = 0.299 * a[..., 0] + 0.587 * a[..., 1] + 0.114 * a[..., 2]
    return np.clip(g, 0, 255).astype(np.uint8)


def resize_max_edge(arr: np.ndarray, max_edge: int) -> tuple[np.ndarray, float]:
    """按最长边限制缩放（仅用于总览图；原生块**不得**走这里）。返回 (新数组, 缩放比)。"""
    h, w = arr.shape[:2]
    longest = max(w, h)
    if longest <= max_edge or longest == 0:
        return arr, 1.0
    scale = max_edge / float(longest)
    im = Image.fromarray(np.asarray(arr, dtype=np.uint8), mode="RGB")
    im = im.resize((max(1, int(round(w * scale))), max(1, int(round(h * scale)))), Image.LANCZOS)
    return np.asarray(im, dtype=np.uint8), scale


# ---------------------------------------------------------------- ROI 与框

def parse_roi(text, size: tuple[int, int] | None = None) -> tuple[int, int, int, int]:
    """``"x,y,w,h"`` → 元组；给 ``size`` 时裁到图内并校验非空。"""
    if text is None:
        if size is None:
            raise ImgCmpError("roi is required when image size is unknown")
        return (0, 0, int(size[0]), int(size[1]))
    parts = [p.strip() for p in str(text).split(",")]
    if len(parts) != 4:
        raise ImgCmpError("roi must be 'x,y,w,h', got: %r" % text)
    try:
        box = tuple(int(p) for p in parts)
    except ValueError as exc:
        raise ImgCmpError("roi must contain integers: %r" % text) from exc
    if size is not None:
        box = clamp_box(box, size)
    if box[2] <= 0 or box[3] <= 0:
        raise ImgCmpError("roi is empty after clipping: %r" % (box,))
    return box


def clamp_box(box, size: tuple[int, int]) -> tuple[int, int, int, int]:
    """``(x, y, w, h)`` 裁到 ``size=(W, H)`` 内；宽高允许为 0（由调用方判空）。"""
    x, y, w, h = (int(v) for v in box)
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(int(size[0]), x + w), min(int(size[1]), y + h)
    return (x0, y0, max(0, x1 - x0), max(0, y1 - y0))


def box_xyxy(box) -> tuple[int, int, int, int]:
    """``(x, y, w, h)`` → ``(x0, y0, x1, y1)``（左闭右开）。"""
    x, y, w, h = (int(v) for v in box)
    return (x, y, x + w, y + h)


def box_wh(xyxy) -> tuple[int, int, int, int]:
    x0, y0, x1, y1 = (int(v) for v in xyxy)
    return (x0, y0, x1 - x0, y1 - y0)


def box_contains(outer, inner, tol: int = 0) -> bool:
    """``inner`` 是否完全落在 ``outer`` 内（两框均为 ``(x0, y0, x1, y1)``，各边允许 ``tol`` 容差）。"""
    ox0, oy0, ox1, oy1 = (int(v) for v in outer)
    ix0, iy0, ix1, iy1 = (int(v) for v in inner)
    return (ix0 >= ox0 - tol) and (iy0 >= oy0 - tol) and (ix1 <= ox1 + tol) and (iy1 <= oy1 + tol)


def box_iou(a, b) -> float:
    """两个 ``(x0, y0, x1, y1)`` 的 IoU。"""
    ix0, iy0 = max(a[0], b[0]), max(a[1], b[1])
    ix1, iy1 = min(a[2], b[2]), min(a[3], b[3])
    iw, ih = max(0, ix1 - ix0), max(0, iy1 - iy0)
    inter = float(iw * ih)
    ua = float((a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1])) - inter
    return 0.0 if ua <= 0 else inter / ua


def box_gap(a, b) -> float:
    """两框的间隙（相交为负值，单位像素）；用于"接触/间隙关系"。"""
    gx = max(a[0] - b[2], b[0] - a[2])
    gy = max(a[1] - b[3], b[1] - a[3])
    if gx <= 0 and gy <= 0:
        ix0, iy0 = max(a[0], b[0]), max(a[1], b[1])
        ix1, iy1 = min(a[2], b[2]), min(a[3], b[3])
        return -float(min(ix1 - ix0, iy1 - iy0)) if (ix1 > ix0 and iy1 > iy0) else 0.0
    return float(max(gx, gy))


# ---------------------------------------------------------------- 底别

def border_luma(gray: np.ndarray, pad: int = 4) -> float:
    """边框带的中位亮度（0–255）。"""
    if gray.ndim != 2:
        gray = to_gray(gray)
    h, w = gray.shape
    pad = max(1, min(pad, max(1, min(h, w) // 4)))
    band = np.concatenate([
        gray[:pad, :].ravel(), gray[-pad:, :].ravel(),
        gray[:, :pad].ravel(), gray[:, -pad:].ravel(),
    ])
    return float(np.median(band))


def detect_kind(arr: np.ndarray, threshold: float = 128.0) -> str:
    """按边框亮度判底别：≥ threshold 为白底，否则黑底。"""
    return KIND_WHITE if border_luma(to_gray(arr)) >= threshold else KIND_BLACK


# ---------------------------------------------------------------- 记账

def sha256_16(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def write_json(path: str, obj) -> str:
    ensure_dir(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1, sort_keys=False)
    return path


def read_json(path: str):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------- 报告（中文）

def _dwidth(text: str) -> int:
    """终端显示宽度：CJK/全角区段按 2 列计。"""
    w = 0
    for ch in str(text):
        o = ord(ch)
        w += 2 if (0x1100 <= o <= 0x115F or 0x2E80 <= o <= 0xA4CF or 0xAC00 <= o <= 0xD7A3
                   or 0xF900 <= o <= 0xFAFF or 0xFE30 <= o <= 0xFE6F or 0xFF00 <= o <= 0xFF60
                   or 0xFFE0 <= o <= 0xFFE6 or 0x3000 <= o <= 0x303F) else 1
    return w


def _pad(text, width: int) -> str:
    return str(text) + " " * max(0, width - _dwidth(text))


def report(title: str, rows=(), notes=()) -> None:
    """人读报告：中文标题＋ ``(标签, 值)`` 行＋注记。stdout only。"""
    print("[imgcmp] %s" % title)
    width = max([_dwidth(k) for k, _ in rows], default=0)
    for key, val in rows:
        print("  %s  %s" % (_pad(key, width), val))
    for note in notes:
        print("  · %s" % note)


def selftest_report(tool: str, checks) -> int:
    """``checks`` ＝ ``[(name, ok, detail), ...]``；全过返回 0，否则 1。"""
    bad = [(n, d) for n, ok, d in checks if not ok]
    print("[imgcmp] %s --selftest：%d/%d PASS" % (tool, len(checks) - len(bad), len(checks)))
    width = max([_dwidth(n) for n, _, _ in checks], default=0)
    for name, ok, detail in checks:
        print("  %s  %s%s" % (_pad(name, width), "PASS" if ok else "FAIL",
                              ("  %s" % detail) if detail else ""))
    return 0 if not bad else 1


def die(msg: str, code: int = 2) -> int:
    """英文诊断 → stderr，返回退出码（调用方 ``return die(...)``）。"""
    sys.stderr.write("[imgcmp] error: %s\n" % msg)
    return code


# ---------------------------------------------------------------- CLI

def make_parser(prog: str, description: str) -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog=prog, description=description)
    ap.add_argument("--selftest", action="store_true", help="run built-in self-test and exit")
    return ap


def run_main(tool: str, fn, argv=None) -> int:
    """统一异常→退出码：``ImgCmpError`` → 2，``--selftest`` 由 ``fn`` 自理。"""
    try:
        return int(fn(argv))
    except ImgCmpError as exc:
        return die(str(exc))
    except KeyboardInterrupt:  # pragma: no cover
        return die("interrupted", 130)
    except Exception as exc:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        return die("unexpected failure in %s: %s" % (tool, exc), 3)
