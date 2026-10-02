# -*- coding: utf-8 -*-
"""primer-imgtile —— T1 多尺度多部位切图器（imgcmp 工具链第一件）。

**用途**：把一张大尺寸参考图（论文页／PPT 渲染页／CAD 截图，可带 ROI）切成一组
"多尺度＋多部位"的上下文图像块，供多模态 LLM 按序阅读——先看总览建立全局构型，
再按网格由粗到细逐块细读，需要核对轮廓与连接关系时对照同坐标的边缘强度通道。

**产物**（``--out`` 目录）::

    full_1x.png                     ROI 总览，最长边 ≤ --max-full
    grid{N}x{N}/tile_r{r}c{c}.png   n×n 原生分辨率块（含 --overlap 重叠、裁到 ROI 边界）
    edge/full_1x.png                总览的同尺度边缘强度通道
    edge/grid{N}x{N}/tile_r{r}c{c}.png  各原生块的同坐标边缘强度通道
    contact_sheet.png               总览＋最细网格线与块编号（定位引用用）
    manifest.json                   每块源坐标、尺度、摘要与建议阅读顺序

**原生分辨率纪律**：``native=true`` 的块一律由源图 ROI 直接切片，不经任何缩放或
重采样，因而与源图对应区域**逐像素相同**——这条是 T1 验收"某单块内原生可读"可
客观判定的基础（逐像素比对即可证）。

**边缘通道口径**（对称双口径，写进 manifest 的 ``edge_channel`` 键）：

- 白底图（``kind=white``，论文/CAD 截图）：``e = 暗部 × 梯度幅值``，
  其中 ``暗部 = 1 − luma/255``，``梯度幅值 = |∇luma|``（Rec.601 亮度上的中心差分）；
- 黑底图（``kind=black``，空间渲染）：``e = 亮部 × 梯度幅值``，``亮部 = luma/255``。

两种口径都用**同一个因子放大**：``luma/255`` 与 ``1 − luma/255`` 是关于 127.5 的
镜像，因此白底看墨线、黑底看亮部，行为对称。归一化除数取**整个 ROI** 的最大场值
（不是逐块），所以各块的灰度值可跨块比较，阈值也就能共用一套。

语言纪律（docs/guides/CODING_STANDARDS.md v1.1）：stdout 只出中文人读报告；
stderr／异常／选项名／JSON 键一律英文。
"""

from __future__ import annotations

import contextlib
import io
import math
import os
import tempfile

import numpy as np
from PIL import Image, ImageDraw

from .common import (
    ImgCmpError,
    KIND_BLACK,
    KIND_WHITE,
    box_xyxy,
    detect_kind,
    ensure_dir,
    load_rgb,
    make_parser,
    parse_roi,
    report,
    resize_max_edge,
    run_main,
    save_gray,
    save_rgb,
    selftest_report,
    sha256_16,
    to_gray,
    write_json,
)

TOOL = "primer.imgcmp.tiler"

KIND_LABEL = {KIND_WHITE: "白底（论文/CAD 截图）", KIND_BLACK: "黑底（空间渲染）"}

EDGE_WEIGHT = {
    KIND_WHITE: ("darkness", "1 - luma/255"),
    KIND_BLACK: ("brightness", "luma/255"),
}
EDGE_WEIGHT_CN = {KIND_WHITE: "暗部", KIND_BLACK: "亮部"}


# ---------------------------------------------------------------- 几何

def tiles_of(box, n: int, overlap: float):
    """把 ROI ``(x, y, w, h)`` 切成 n×n 块，各块向外扩 ``overlap`` 比例后裁到 ROI。

    返回 ``[(r, c, (x0, y0, x1, y1)), ...]``，原图像素、左闭右开。下界取 floor、
    上界取 ceil，保证 n² 块的并集恰好覆盖 ROI，且任意相邻块的重叠不小于 ``overlap``。
    """
    if n < 1:
        raise ImgCmpError("grid size must be >= 1, got: %r" % (n,))
    if not (0.0 <= float(overlap) < 1.0):
        raise ImgCmpError("overlap must be in [0, 1), got: %r" % (overlap,))
    x, y, w, h = (int(v) for v in box)
    bw, bh = w / float(n), h / float(n)
    px, py = bw * float(overlap), bh * float(overlap)
    out = []
    for r in range(n):
        for c in range(n):
            x0 = max(x, int(math.floor(x + c * bw - px)))
            y0 = max(y, int(math.floor(y + r * bh - py)))
            x1 = min(x + w, int(math.ceil(x + (c + 1) * bw + px)))
            y1 = min(y + h, int(math.ceil(y + (r + 1) * bh + py)))
            out.append((r, c, (x0, y0, x1, y1)))
    return out


def grid_name(n: int) -> str:
    return "grid%dx%d" % (n, n)


def tile_name(r: int, c: int) -> str:
    return "tile_r%dc%d.png" % (r, c)


# ---------------------------------------------------------------- 边缘通道

def edge_field(gray: np.ndarray, kind: str) -> np.ndarray:
    """未归一化的边缘强度场 ``weight × |∇luma|``（float32，与原图同尺寸）。"""
    g = np.asarray(gray, dtype=np.float32) / 255.0
    if g.ndim != 2 or min(g.shape) < 2:
        return np.zeros(g.shape, dtype=np.float32)
    dy, dx = np.gradient(g)
    mag = np.hypot(dx, dy)
    weight = (1.0 - g) if kind == KIND_WHITE else g
    return (weight * mag).astype(np.float32)


def edge_u8(field: np.ndarray, denom: float) -> np.ndarray:
    """场值 → uint8。``denom`` 用 ROI 全域的场极大值，跨块一致。"""
    if denom <= 0.0:
        return np.zeros(np.asarray(field).shape, dtype=np.uint8)
    return np.clip(np.asarray(field, dtype=np.float32) / denom * 255.0, 0, 255).astype(np.uint8)


# ---------------------------------------------------------------- 主流程

def build(image_path: str, roi=None, grids=(2, 3), overlap: float = 0.12,
          max_full: int = 1024, out_dir: str | None = None, edge: bool = True) -> dict:
    """切图并写盘。返回 ``{"out", "manifest", "path"}``。"""
    if max_full < 1:
        raise ImgCmpError("--max-full must be >= 1, got: %r" % (max_full,))
    grid_list = []
    for n in grids:
        n = int(n)
        if n < 1:
            raise ImgCmpError("grid size must be >= 1, got: %r" % (n,))
        if n not in grid_list:
            grid_list.append(n)
    if not grid_list:
        raise ImgCmpError("--grids needs at least one size")
    if not (0.0 <= float(overlap) < 1.0):
        raise ImgCmpError("overlap must be in [0, 1), got: %r" % (overlap,))

    src = load_rgb(image_path)
    height, width = src.shape[0], src.shape[1]
    box = parse_roi(roi, (width, height))          # (x, y, w, h)，已裁到图内
    x, y, w, h = box
    if w <= 0 or h <= 0:
        raise ImgCmpError("roi is empty: %r" % (box,))
    roi_rgb = src[y:y + h, x:x + w]
    kind = detect_kind(roi_rgb)

    if out_dir is None:
        stem = os.path.splitext(os.path.basename(image_path))[0]
        out_dir = os.path.join(os.path.dirname(os.path.abspath(image_path)), "%s_tiles" % stem)
    ensure_dir(out_dir)

    overview, scale = resize_max_edge(roi_rgb, max_full)
    ov_h, ov_w = overview.shape[0], overview.shape[1]

    field = edge_field(to_gray(roi_rgb), kind) if edge else None
    denom = float(field.max()) if field is not None and field.size else 0.0

    items = []

    def add(kind_tag, rel, src_box, native, note, **extra):
        items.append({
            "order": len(items),
            "kind": kind_tag,
            "file": rel,
            "src_box": [int(v) for v in src_box],
            "native": bool(native),
            "sha256_16": sha256_16(os.path.join(out_dir, rel)),
            "note": note,
            **extra,
        })

    # 1) 总览（限长边；scale == 1 时即原生，未重采样）
    save_rgb(overview, os.path.join(out_dir, "full_1x.png"))
    add("overview", "full_1x.png", box_xyxy(box), scale == 1.0,
        "ROI 总览，最长边 %d，缩放比 %.4f——先看它建立全局构型" % (max_full, scale),
        scale=round(float(scale), 6))

    # 2) 原生网格块（源图 ROI 直接切片，禁止缩放/重采样）
    for n in grid_list:
        gdir = grid_name(n)
        ensure_dir(os.path.join(out_dir, gdir))
        for r, c, tb in tiles_of(box, n, overlap):
            x0, y0, x1, y1 = tb
            rel = "%s/%s" % (gdir, tile_name(r, c))
            save_rgb(src[y0:y1, x0:x1], os.path.join(out_dir, rel))
            add("grid", rel, tb, True,
                "原生块 r%dc%d（%s，含 %.0f%% 重叠）——与源图逐像素相同" % (r, c, gdir, overlap * 100),
                grid=[n, n], row=r, col=c)

    # 3) 边缘强度通道（同坐标；非原生像素，故 native=false）
    if edge:
        weight_name = EDGE_WEIGHT[kind][0]
        full_edge = edge_u8(field, denom)
        if scale != 1.0:
            full_edge = np.asarray(Image.fromarray(full_edge, mode="L").resize(
                (ov_w, ov_h), Image.LANCZOS), dtype=np.uint8)
        save_gray(full_edge, os.path.join(out_dir, "edge", "full_1x.png"))
        add("edge", "edge/full_1x.png", box_xyxy(box), False,
            "总览同尺寸边缘通道（%s×梯度幅值），只作轮廓/连接关系核对" % weight_name,
            scale=round(float(scale), 6))
        for n in grid_list:
            gdir = os.path.join("edge", grid_name(n))
            ensure_dir(os.path.join(out_dir, gdir))
            for r, c, tb in tiles_of(box, n, overlap):
                x0, y0, x1, y1 = tb
                tile_field = field[y0 - y:y1 - y, x0 - x:x1 - x]
                rel = "%s/%s" % (gdir, tile_name(r, c))
                save_gray(edge_u8(tile_field, denom), os.path.join(out_dir, rel))
                add("edge", rel, tb, False,
                    "原生块 r%dc%d 的同坐标边缘通道（%s），非原生像素" % (r, c, weight_name),
                    grid=[n, n], row=r, col=c)

    # 4) 联系表：总览＋最细网格线＋块编号
    finest = max(grid_list)
    sheet = _contact_sheet(overview, scale, box, finest, overlap)
    save_rgb(np.asarray(sheet, dtype=np.uint8), os.path.join(out_dir, "contact_sheet.png"))
    add("contact_sheet", "contact_sheet.png", box_xyxy(box), False,
        "总览＋最细网格（%s）线与块编号，用于把某一发现指回具体块" % grid_name(finest),
        scale=round(float(scale), 6))

    edge_channel = {
        "background": kind,
        "formula": "%s * gradient_magnitude" % EDGE_WEIGHT[kind][0],
        "weight": EDGE_WEIGHT[kind][1],
        "gradient": "abs(gradient of Rec.601 luma), central difference",
        "normalize": "divided by the maximum of the whole ROI, so tiles are comparable",
        "no_scale": scale == 1.0,
    }
    manifest = {
        "tool": TOOL,
        "image": os.path.abspath(image_path),
        "image_size": [int(width), int(height)],
        "roi": [int(v) for v in box],
        "roi_box": [int(v) for v in box_xyxy(box)],
        "overlap": float(overlap),
        "grids": [int(n) for n in grid_list],
        "max_full": int(max_full),
        "kind": kind,
        "scale": round(float(scale), 6),
        "overview_size": [int(ov_w), int(ov_h)],
        "edge_channel": edge_channel,
        "items": items,
        "contact_sheet": "contact_sheet.png",
        "reading_note": (
            "先看 full_1x.png 建立全局构型；再按 order 逐块细读（粗网格在前、细网格在后）；"
            "每块均为原生分辨率，可与源图对应区域逐像素对照。需要核对轮廓是否闭合、杆件是否"
            "连续时，看 edge/ 下同坐标的边缘通道（%s×梯度幅值口径，全 ROI 统一归一化）。"
            "要引用某个发现，用 contact_sheet.png 上的 %s 块编号 r{行}c{列}。"
            % (EDGE_WEIGHT_CN[kind], grid_name(finest))
        ),
    }
    path = os.path.join(out_dir, "manifest.json")
    write_json(path, manifest)
    return {"out": out_dir, "manifest": manifest, "path": path}


def _contact_sheet(overview: np.ndarray, scale: float, box, n: int, overlap: float):
    """总览图上叠最细网格的块框与编号（框用与产物同一套 ``tiles_of``）。"""
    image = Image.fromarray(np.asarray(overview, dtype=np.uint8), mode="RGB").copy()
    draw = ImageDraw.Draw(image)
    ox, oy = int(box[0]), int(box[1])
    for r, c, tb in tiles_of(box, n, overlap):
        x0, y0, x1, y1 = tb
        px = [int(round((x0 - ox) * scale)), int(round((y0 - oy) * scale)),
              int(round((x1 - ox) * scale)), int(round((y1 - oy) * scale))]
        draw.rectangle(px, outline=(255, 64, 64), width=2)
        draw.text((px[0] + 4, px[1] + 4), "r%dc%d" % (r, c), fill=(255, 64, 64))
    return image


# ---------------------------------------------------------------- 报告

def _print_report(result: dict) -> None:
    manifest = result["manifest"]
    items = manifest["items"]
    counts = {}
    for item in items:
        counts[item["kind"]] = counts.get(item["kind"], 0) + 1
    rows = [
        ("输入图", manifest["image"]),
        ("图像尺寸", "%d x %d" % tuple(manifest["image_size"])),
        ("ROI", "x=%d y=%d w=%d h=%d" % tuple(manifest["roi"])),
        ("底别", KIND_LABEL.get(manifest["kind"], manifest["kind"])),
        ("网格", "、".join(grid_name(n) for n in manifest["grids"])
                 + "（重叠 %.0f%%）" % (manifest["overlap"] * 100)),
        ("总览", "最长边 %d，缩放比 %.4f" % (manifest["max_full"], manifest["scale"])),
        ("块数", "总览 %d｜原生块 %d｜边缘块 %d｜联系表 %d"
                 % (counts.get("overview", 0), counts.get("grid", 0),
                    counts.get("edge", 0), counts.get("contact_sheet", 0))),
        ("产物目录", os.path.abspath(result["out"])),
        ("清单", os.path.abspath(result["path"])),
    ]
    notes = [
        "原生块（native=true）与源图对应区域逐像素相同，未经缩放或重采样。",
        "边缘通道口径：%s×梯度幅值（%s），全 ROI 统一归一化（manifest.edge_channel）。"
        % (EDGE_WEIGHT_CN[manifest["kind"]], manifest["edge_channel"]["formula"]),
        manifest["reading_note"],
    ]
    report("T1 多尺度多部位切图：%d 块" % len(items), rows, notes)


# ---------------------------------------------------------------- selftest

def _synth(white: bool = True, width: int = 600, height: int = 420) -> np.ndarray:
    """自造图：白底深墨（或黑底亮墨）的环＋杆，边界处还有一条斜线。"""
    bg, ink = (248, 12) if white else (10, 240)
    arr = np.full((height, width, 3), bg, dtype=np.uint8)
    arr[80:260, 120:360] = ink
    arr[160:200, 120:360] = bg
    arr[300:318, 40:560] = ink
    for i in range(80):
        arr[330 + i, 380 + i] = ink
    arr[60:90, 430:520] = ink
    return arr


def _selftest() -> int:
    checks = []
    with tempfile.TemporaryDirectory() as tmp:
        src_path = os.path.join(tmp, "synth_white.png")
        source = _synth(white=True)
        Image.fromarray(source, mode="RGB").save(src_path)
        out_dir = os.path.join(tmp, "tiles")
        result = build(src_path, roi="40,40,500,340", grids=(2, 3), overlap=0.12,
                       max_full=256, out_dir=out_dir, edge=True)
        manifest = result["manifest"]

        # 1) 原生逐像素一致（含重叠块与裁到 ROI 边界的块）
        bad = []
        native_items = [i for i in manifest["items"] if i["native"]]
        for item in native_items:
            got = load_rgb(os.path.join(out_dir, item["file"]))
            x0, y0, x1, y1 = item["src_box"]
            want = source[y0:y1, x0:x1]
            if got.shape != want.shape or not np.array_equal(got, want):
                bad.append(item["file"])
        checks.append(("native_pixels", bool(native_items) and not bad,
                       "%d/%d 块与源图逐像素相同%s"
                       % (len(native_items) - len(bad), len(native_items),
                          ("；不符：" + ",".join(bad)) if bad else "")))

        # 2) 重叠生效：相邻块的 src_box 相交，且交集像素两两相同
        overlaps, mismatched = 0, []
        for n in manifest["grids"]:
            tiles = {(r, c): tb for r, c, tb in tiles_of(manifest["roi"], n, manifest["overlap"])}
            for (r, c), box in tiles.items():
                for nb in ((r, c + 1), (r + 1, c)):
                    if nb not in tiles:
                        continue
                    a, b = box, tiles[nb]
                    ix0, iy0 = max(a[0], b[0]), max(a[1], b[1])
                    ix1, iy1 = min(a[2], b[2]), min(a[3], b[3])
                    if ix1 <= ix0 or iy1 <= iy0:
                        continue
                    overlaps += 1
                    pa = load_rgb(os.path.join(out_dir, grid_name(n), tile_name(r, c)))
                    pb = load_rgb(os.path.join(out_dir, grid_name(n), tile_name(*nb)))
                    sub_a = pa[iy0 - a[1]:iy1 - a[1], ix0 - a[0]:ix1 - a[0]]
                    sub_b = pb[iy0 - b[1]:iy1 - b[1], ix0 - b[0]:ix1 - b[0]]
                    if not np.array_equal(sub_a, sub_b):
                        mismatched.append("%s r%dc%d/%s" % (grid_name(n), r, c, nb))
        checks.append(("overlap_effective", overlaps > 0 and not mismatched,
                       "%d 对相邻块重叠，交集逐像素一致%s"
                       % (overlaps, ("；不符：" + ",".join(mismatched)) if mismatched else "")))

        # 3) manifest 回算一致：用同一套几何重算，与 src_box 误差必须为 0
        worst = 0
        for item in manifest["items"]:
            if item["kind"] not in ("grid", "edge") or not item.get("grid"):
                continue
            n = item["grid"][0]
            want = dict(((r, c), tb) for r, c, tb in tiles_of(manifest["roi"], n, manifest["overlap"]))
            got = want[(item["row"], item["col"])]
            worst = max(worst, max(abs(g - s) for g, s in zip(got, item["src_box"])))
        checks.append(("manifest_src_box", worst <= 2,
                       "重算与 src_box 最大偏差 %d px（判据 ≤2）" % worst))

        # 4) 边缘通道非空，且白底口径确为"暗部权重"
        edge_items = [i for i in manifest["items"] if i["kind"] == "edge"]
        edge_max = 0
        for item in edge_items:
            edge_max = max(edge_max, int(np.asarray(Image.open(os.path.join(out_dir, item["file"]))).max()))
        checks.append(("edge_nonempty", bool(edge_items) and edge_max > 0,
                       "%d 个通道，最大强度 %d/255" % (len(edge_items), edge_max)))
        checks.append(("edge_white_rule", manifest["edge_channel"]["formula"] ==
                       "darkness * gradient_magnitude",
                       "白底口径 %s" % manifest["edge_channel"]["formula"]))

        # 5) 联系表落盘且尺寸等于总览；总览边缘通道与总览同尺寸
        sheet = Image.open(os.path.join(out_dir, "contact_sheet.png"))
        ov_size = tuple(manifest["overview_size"])
        edge_full = next(i for i in edge_items if not i.get("grid"))
        edge_size = Image.open(os.path.join(out_dir, edge_full["file"])).size
        checks.append(("contact_sheet", sheet.size == ov_size,
                       "contact_sheet.png %dx%d，与总览同尺寸" % sheet.size))
        checks.append(("edge_overview_size", edge_size == ov_size,
                       "edge/full_1x.png %dx%d，与总览同尺寸" % edge_size))

        # 6) 黑底：对称口径（亮部×梯度幅值）
        dark_path = os.path.join(tmp, "synth_black.png")
        Image.fromarray(_synth(white=False), mode="RGB").save(dark_path)
        dark = build(dark_path, roi="40,40,500,340", grids=(2,), overlap=0.12,
                     out_dir=os.path.join(tmp, "dark_tiles"), edge=True)
        dark_manifest = dark["manifest"]
        dark_edge = next(i for i in dark_manifest["items"] if i["kind"] == "edge" and i.get("grid"))
        lev = int(np.asarray(Image.open(os.path.join(dark["out"], dark_edge["file"]))).max())
        checks.append(("edge_black_rule",
                       dark_manifest["kind"] == KIND_BLACK
                       and dark_manifest["edge_channel"]["formula"] == "brightness * gradient_magnitude"
                       and lev > 0,
                       "底别 %s，口径 %s，最大强度 %d/255"
                       % (dark_manifest["kind"], dark_manifest["edge_channel"]["formula"], lev)))

        # 7) CLI 退出码：正常 0；坏输入 2；空 ROI 2（把自检自身的报告与诊断静音）
        quiet = contextlib.redirect_stdout(io.StringIO())
        with quiet, contextlib.redirect_stderr(io.StringIO()):
            ok_code = main([src_path, "--roi", "40,40,500,340", "--grids", "2", "--no-edge",
                            "--out", os.path.join(tmp, "cli_tiles")])
            bad_code = main([os.path.join(tmp, "missing.png"), "--out", os.path.join(tmp, "x")])
            empty_code = main([src_path, "--roi", "9000,9000,50,50",
                               "--out", os.path.join(tmp, "y")])
        checks.append(("cli_exit_codes", (ok_code, bad_code, empty_code) == (0, 2, 2),
                       "正常=%d 坏输入=%d 空 ROI=%d（期望 0/2/2）" % (ok_code, bad_code, empty_code)))

        # 8) --no-edge 不产 edge 目录
        no_edge = build(src_path, roi="40,40,500,340", grids=(2,),
                        out_dir=os.path.join(tmp, "noedge_tiles"), edge=False)
        checks.append(("no_edge_flag",
                       not os.path.exists(os.path.join(no_edge["out"], "edge"))
                       and not any(i["kind"] == "edge" for i in no_edge["manifest"]["items"]),
                       "--no-edge 后无 edge/ 目录、无 edge 条目"))

    return selftest_report(TOOL, checks)


# ---------------------------------------------------------------- CLI

def build_parser():
    parser = make_parser(
        TOOL,
        "Tile a reference image into multi-scale, multi-part context blocks for sequential "
        "reading by a multimodal model: an overview, NxN native-resolution tiles with overlap, "
        "an edge-strength channel, a contact sheet and a manifest. Native tiles are cut "
        "straight from the source, so they are pixel-identical to it.",
    )
    parser.add_argument("image", nargs="?", help="reference image to tile (PNG/JPG)")
    parser.add_argument("--roi", metavar="X,Y,W,H",
                        help="region of interest in source pixels; default is the whole image")
    parser.add_argument("--grids", type=int, nargs="+", default=[2, 3], metavar="N",
                        help="grid divisions, e.g. --grids 2 3 (default: 2 3)")
    parser.add_argument("--overlap", type=float, default=0.12, metavar="F",
                        help="fraction of a tile added on each side, in [0, 1) (default: 0.12)")
    parser.add_argument("--max-full", type=int, default=1024, metavar="PX",
                        help="longest edge of the overview (default: 1024)")
    parser.add_argument("--out", metavar="DIR",
                        help="output directory; default: <image>_tiles next to the image")
    parser.add_argument("--no-edge", action="store_true",
                        help="skip the edge-strength channel")
    return parser


def main(argv=None) -> int:
    return run_main(TOOL, _run, argv)


def _run(argv) -> int:
    args = build_parser().parse_args(argv)
    if args.selftest:
        return _selftest()
    if not args.image:
        raise ImgCmpError("image is required (or use --selftest)")
    result = build(args.image, roi=args.roi, grids=args.grids, overlap=args.overlap,
                   max_full=args.max_full, out_dir=args.out, edge=not args.no_edge)
    _print_report(result)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
