# -*- coding: utf-8 -*-
"""``primer.imgcmp.tiler``：T1 多尺度多部位切图器。

钉住的都是验收要用的可判定项：原生块与源图**逐像素相同**（含向外重叠后裁到 ROI
边界的情形）、manifest 的 ``src_box`` 能按同一套几何回算、边缘通道两种底别对称、
``--no-edge`` 与多网格并存，以及 CLI 的退出码（正常 0、坏输入 2）。

这些用例都不去看"图好不好看"——只从像素与坐标算，任何一处静默缩放、重采样或把
重叠裁错，都会在这里露出来。
"""

from __future__ import annotations

import json

import numpy as np
import pytest
from PIL import Image

from primer.imgcmp.tiler import build, grid_name, main, tile_name, tiles_of


def _synthetic(width=600, height=420, white=True):
    """白底深墨（或黑底亮墨）：一个套环、一根横杆、一条贴边的斜线。"""
    background, ink = (248, 12) if white else (10, 240)
    canvas = np.full((height, width, 3), background, dtype=np.uint8)
    canvas[80:260, 120:360] = ink
    canvas[160:200, 120:360] = background
    canvas[300:318, 40:560] = ink
    for step in range(80):
        canvas[330 + step, 380 + step] = ink
    return canvas


@pytest.fixture
def picture(tmp_path):
    """一张 600x420 的自造图，以及它的落盘路径。"""
    path = tmp_path / "synth.png"
    Image.fromarray(_synthetic(), mode="RGB").save(path)
    return path


def _manifest(out_dir):
    return json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))


def _read(path):
    with Image.open(path) as image:
        return np.asarray(image.convert("RGB"), dtype=np.uint8)


# ---------------------------------------------------------------- 原生分辨率

def test_every_native_tile_is_pixel_identical_to_the_source_region(picture, tmp_path):
    """重叠外扩、裁到 ROI 边界的块都算数：只要 native=true 就必须逐像素相同。"""
    out_dir = tmp_path / "tiles"
    result = build(str(picture), roi="40,40,500,340", grids=(2, 3), overlap=0.12,
                   out_dir=str(out_dir))
    source = _synthetic()
    manifest = result["manifest"]

    native = [item for item in manifest["items"] if item["native"]]
    assert native, "至少总览（未缩放时）与全部网格块应为原生"
    for item in native:
        x0, y0, x1, y1 = item["src_box"]
        assert np.array_equal(_read(out_dir / item["file"]), source[y0:y1, x0:x1]), item["file"]


def test_a_tile_that_pokes_out_of_the_roi_is_clipped_not_padded(picture, tmp_path):
    """角块向外扩重叠后会压到 ROI 边界，只能裁、不能补边。"""
    out_dir = tmp_path / "tiles"
    result = build(str(picture), roi="40,40,500,340", grids=(3,), overlap=0.4,
                   out_dir=str(out_dir))
    roi = result["manifest"]["roi"]
    corner = next(item for item in result["manifest"]["items"] if item["src_box"][:2] == [
        roi[0], roi[1]])

    assert corner["src_box"] == [roi[0], roi[1], corner["src_box"][2], corner["src_box"][3]]
    tile = _read(out_dir / corner["file"])
    assert tile.shape[:2] == (corner["src_box"][3] - corner["src_box"][1],
                              corner["src_box"][2] - corner["src_box"][0])


def test_adjacent_tiles_agree_on_the_pixels_they_share(picture, tmp_path):
    """重叠不是为了好看：两块交叠的那一段必须来自同一份源像素。"""
    out_dir = tmp_path / "tiles"
    result = build(str(picture), roi="40,40,500,340", grids=(2,), overlap=0.12,
                   out_dir=str(out_dir))
    tiles = {(r, c): box for r, c, box in tiles_of(result["manifest"]["roi"], 2, 0.12)}
    left, right = tiles[(0, 0)], tiles[(0, 1)]
    ix0, ix1 = max(left[0], right[0]), min(left[2], right[2])
    image_left = _read(out_dir / grid_name(2) / tile_name(0, 0))
    image_right = _read(out_dir / grid_name(2) / tile_name(0, 1))

    assert ix1 > ix0, "12% 重叠必须真的产生交叠像素"
    assert np.array_equal(
        image_left[:, ix0 - left[0]:ix1 - left[0]],
        image_right[:, ix0 - right[0]:ix1 - right[0]],
    )


def test_an_overview_over_the_max_edge_is_scaled_and_then_is_not_native(picture, tmp_path):
    """总览可以缩，但缩了就不再是原生块——native 这个标记不能糊。"""
    out_dir = tmp_path / "tiles"
    result = build(str(picture), roi="40,40,500,340", grids=(2,), max_full=200,
                   out_dir=str(out_dir))
    overview = next(item for item in result["manifest"]["items"] if item["kind"] == "overview")

    assert overview["native"] is False
    assert max(Image.open(out_dir / "full_1x.png").size) <= 200


# ---------------------------------------------------------------- ROI

def test_a_roi_out_of_bounds_is_clipped_to_the_image(picture, tmp_path):
    out_dir = tmp_path / "tiles"
    result = build(str(picture), roi="500,300,400,400", grids=(2,), out_dir=str(out_dir))

    assert result["manifest"]["roi"] == [500, 300, 100, 120]
    assert result["manifest"]["image_size"] == [600, 420]


def test_an_empty_roi_is_an_error_not_an_empty_run(picture, tmp_path):
    with pytest.raises(Exception, match="roi is empty"):
        build(str(picture), roi="9000,9000,50,50", out_dir=str(tmp_path / "tiles"))


def test_a_malformed_roi_string_is_refused(picture, tmp_path):
    with pytest.raises(Exception, match="x,y,w,h"):
        build(str(picture), roi="40,40,500", out_dir=str(tmp_path / "tiles"))


# ---------------------------------------------------------------- manifest

def test_the_manifest_src_boxes_round_trip_within_two_pixels(picture, tmp_path):
    out_dir = tmp_path / "tiles"
    result = build(str(picture), roi="40,40,500,340", grids=(2, 3), overlap=0.12,
                   out_dir=str(out_dir))

    for item in result["manifest"]["items"]:
        if item["kind"] not in ("grid", "edge") or not item.get("grid"):
            continue
        size = item["grid"][0]
        recomputed = {
            (r, c): box
            for r, c, box in tiles_of(result["manifest"]["roi"], size, result["manifest"]["overlap"])
        }[(item["row"], item["col"])]
        assert max(abs(a - b) for a, b in zip(recomputed, item["src_box"])) <= 2


def test_the_manifest_records_what_the_acceptance_needs(picture, tmp_path):
    out_dir = tmp_path / "tiles"
    result = build(str(picture), roi="40,40,500,340", grids=(2, 3), overlap=0.12,
                   out_dir=str(out_dir))
    manifest = result["manifest"]

    assert manifest["image"].startswith("/")
    assert manifest["image_size"] == [600, 420]
    assert manifest["roi"] == [40, 40, 500, 340]
    assert manifest["overlap"] == 0.12
    assert manifest["grids"] == [2, 3]
    assert manifest["kind"] == "white"
    assert manifest["reading_note"]
    assert all(len(item["sha256_16"]) == 16 for item in manifest["items"])
    assert {item["kind"] for item in manifest["items"]} == {"overview", "grid", "edge", "contact_sheet"}


def test_the_manifest_checksums_match_the_files_on_disk(picture, tmp_path):
    from primer.imgcmp.common import sha256_16

    out_dir = tmp_path / "tiles"
    result = build(str(picture), roi="40,40,500,340", grids=(2,), out_dir=str(out_dir))

    for item in result["manifest"]["items"]:
        assert item["sha256_16"] == sha256_16(str(out_dir / item["file"]))


# ---------------------------------------------------------------- 网格与边缘

def test_two_grids_produce_two_independent_sets_of_native_tiles(picture, tmp_path):
    out_dir = tmp_path / "tiles"
    result = build(str(picture), roi="40,40,500,340", grids=(2, 3), out_dir=str(out_dir))

    assert (out_dir / "grid2x2" / "tile_r1c1.png").is_file()
    assert (out_dir / "grid3x3" / "tile_r2c2.png").is_file()
    assert sorted(p.name for p in (out_dir / "grid2x2").iterdir()) == sorted(
        tile_name(r, c) for r in range(2) for c in range(2)
    )
    assert sum(1 for item in result["manifest"]["items"] if item["kind"] == "grid") == 4 + 9


def test_no_edge_leaves_no_edge_directory_and_no_edge_items(picture, tmp_path):
    out_dir = tmp_path / "tiles"
    result = build(str(picture), roi="40,40,500,340", grids=(2,), out_dir=str(out_dir),
                   edge=False)

    assert not (out_dir / "edge").exists()
    assert not any(item["kind"] == "edge" for item in result["manifest"]["items"])


def test_the_edge_channel_is_never_native_pixels(picture, tmp_path):
    """边缘是算出来的：坐标可以同源，像素绝不能冒充原生。"""
    out_dir = tmp_path / "tiles"
    result = build(str(picture), roi="40,40,500,340", grids=(2,), out_dir=str(out_dir))
    edge_items = [item for item in result["manifest"]["items"] if item["kind"] == "edge"]
    grid_item = next(item for item in result["manifest"]["items"] if item["kind"] == "grid")
    edge_same_box = next(item for item in edge_items if item["src_box"] == grid_item["src_box"])

    assert edge_items and all(item["native"] is False for item in edge_items)
    assert not np.array_equal(_read(out_dir / edge_same_box["file"]),
                              _read(out_dir / grid_item["file"]))
    assert np.asarray(Image.open(out_dir / edge_same_box["file"])).max() > 0


def test_a_black_background_picture_uses_the_mirrored_brightness_weight(tmp_path):
    """黑底渲染图不能照白底那套口径算，否则亮部全被当成背景抹掉。"""
    path = tmp_path / "dark.png"
    Image.fromarray(_synthetic(white=False), mode="RGB").save(path)
    result = build(str(path), roi="40,40,500,340", grids=(2,), out_dir=str(tmp_path / "dark"))

    assert result["manifest"]["kind"] == "black"
    assert result["manifest"]["edge_channel"]["formula"] == "brightness * gradient_magnitude"
    edge_item = next(item for item in result["manifest"]["items"] if item["kind"] == "edge")
    assert int(np.asarray(Image.open(tmp_path / "dark" / edge_item["file"])).max()) > 0


def test_the_contact_sheet_has_the_overview_size_and_carries_the_finest_grid_labels(picture, tmp_path):
    out_dir = tmp_path / "tiles"
    result = build(str(picture), roi="40,40,500,340", grids=(2, 3), out_dir=str(out_dir))
    sheet = Image.open(out_dir / "contact_sheet.png")
    overview = Image.open(out_dir / "full_1x.png")

    assert sheet.size == overview.size
    assert sheet.tobytes() != overview.tobytes(), "联系表必须叠上网格线与编号"


# ---------------------------------------------------------------- CLI

def test_selftest_exits_zero(capsys):
    assert main(["--selftest"]) == 0
    printed = capsys.readouterr().out

    assert "PASS" in printed and "FAIL" not in printed


def test_the_cli_writes_every_product_in_one_call(picture, tmp_path, capsys):
    out_dir = tmp_path / "cli"
    code = main([str(picture), "--roi", "40,40,500,340", "--grids", "2", "3",
                 "--overlap", "0.15", "--max-full", "300", "--out", str(out_dir)])

    assert code == 0
    for relative in ("full_1x.png", "contact_sheet.png", "manifest.json",
                     "grid2x2/tile_r0c0.png", "grid3x3/tile_r2c2.png",
                     "edge/full_1x.png", "edge/grid2x2/tile_r0c0.png"):
        assert (out_dir / relative).is_file(), relative
    assert _manifest(out_dir)["overlap"] == 0.15
    assert "T1 多尺度多部位切图" in capsys.readouterr().out


def test_a_missing_image_exits_two(tmp_path, capsys):
    assert main([str(tmp_path / "missing.png"), "--out", str(tmp_path / "out")]) == 2
    assert "image not found" in capsys.readouterr().err


def test_an_empty_roi_exits_two(picture, tmp_path, capsys):
    assert main([str(picture), "--roi", "9000,9000,50,50", "--out", str(tmp_path / "out")]) == 2
    assert "roi is empty" in capsys.readouterr().err
