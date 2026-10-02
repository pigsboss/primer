# -*- coding: utf-8 -*-
"""``primer.imgcmp.refgen``：参考生成器（真值源）。

钉住的都是验收要用的可判定项：几何真值只由 ID 图算得（连通域＋包围盒）、
``id_legend`` 能往返、``--assemble`` spec 的坏输入一律报错退出 2、``--selftest`` 退出码，
以及语义量确实来自手工 YAML（未点名者标 ``derived``）。

除最后一节外**都不依赖 Blender**：真值提取用的是随仓库提交的合成 ID 图
（``fixtures/imgcmp/id_sample/``）与自造图；Blender 路径的用例在没装时跳过。
"""

from __future__ import annotations

import json
import os
import shutil

import numpy as np
import pytest
from PIL import Image

from primer.imgcmp.refgen import (
    BACKGROUND_RGB,
    ImgCmpError,
    extract_truth,
    main,
    palette_color,
    parse_angles,
    parse_assemble_spec,
    parse_grid,
    parse_res,
    parse_views,
    resolve_semantics,
    view_name,
)

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "imgcmp")
ASSEMBLIES = os.path.join(FIXTURES, "assemblies")
MODEL_TRUTH = os.path.join(FIXTURES, "model_truth")
ID_SAMPLE = os.path.join(FIXTURES, "id_sample")


def _blender() -> str | None:
    env = os.environ.get("PRIMER_BLENDER")
    if env and os.path.isfile(env):
        return env
    found = shutil.which("blender")
    if found:
        return found
    app = "/Applications/Blender.app/Contents/MacOS/Blender"
    return app if os.path.isfile(app) else None


needs_blender = pytest.mark.skipif(_blender() is None,
                                   reason="Blender not installed (set PRIMER_BLENDER)")


# ---------------------------------------------------------------- 自造 ID 图

def _synthetic_id(legend, spec):
    """按 ``spec`` 画一张 ID 图：``[(rgb, kind, box), ...]``，kind ∈ rect/circle/ring。"""
    from PIL import ImageDraw

    img = Image.new("RGB", (480, 360), BACKGROUND_RGB)
    draw = ImageDraw.Draw(img)
    for rgb, kind, box in spec:
        if kind == "rect":
            draw.rectangle(box, fill=tuple(rgb))
        elif kind == "circle":
            draw.ellipse(box, fill=tuple(rgb))
        elif kind == "ring":
            draw.ellipse(box, fill=tuple(rgb))
            draw.ellipse([box[0] + 20, box[1] + 20, box[2] - 20, box[3] - 20],
                         fill=BACKGROUND_RGB)
    return np.asarray(img, dtype=np.uint8)


def _truth_for(arr, legend, names, **kw):
    camera = {"view": "unit", "az_deg": 0.0, "el_deg": 0.0, "roll_deg": 0.0,
              "ortho": True, "res": [arr.shape[1], arr.shape[0]]}
    model = {"source": "unit", "path": "synthetic", "sha256_16": "0" * 16,
             "normalized_dim_m": [1.0, 1.0, 1.0], "component_names": list(names)}
    return extract_truth(arr, legend, camera, model, **kw)


# ---------------------------------------------------------------- 参数解析

def test_grid_two_numbers_is_a_three_by_three_around_zero():
    poses = parse_grid("30,15")

    assert len(poses) == 9
    assert {p[0] for p in poses} == {-30.0, 0.0, 30.0}
    assert {p[1] for p in poses} == {-15.0, 0.0, 15.0}
    assert all(p[2] == 0.0 for p in poses)


def test_grid_six_numbers_walk_the_explicit_range():
    poses = parse_grid("0,60,30,-15,15,15")

    assert len(poses) == 9
    assert sorted({p[0] for p in poses}) == [0.0, 30.0, 60.0]
    assert sorted({p[1] for p in poses}) == [-15.0, 0.0, 15.0]


def test_grid_rejects_a_bad_arity_and_a_zero_step():
    with pytest.raises(ImgCmpError, match="2 or 6"):
        parse_grid("1,2,3")
    with pytest.raises(ImgCmpError, match="non-zero"):
        parse_grid("0,60,0,-15,15,15")


def test_angles_default_to_a_single_front_pose_and_accept_two_or_three_numbers():
    assert parse_angles(None) == [(0.0, 0.0, 0.0)]
    assert parse_angles("30,15") == [(30.0, 15.0, 0.0)]
    assert parse_angles("0,0,0;45,-10,90") == [(0.0, 0.0, 0.0), (45.0, -10.0, 90.0)]
    with pytest.raises(ImgCmpError, match="az,el"):
        parse_angles("1,2,3,4")


def test_view_names_are_stable_and_readable():
    assert view_name(0, 0, 0) == "az0_el0_roll0"
    assert view_name(-30, 22.5, 180) == "az-30_el22.5_roll180"


def test_views_must_include_id_because_truth_comes_from_it():
    assert parse_views(None) == ["white", "black", "id", "silhouette"]
    with pytest.raises(ImgCmpError, match="must include 'id'"):
        parse_views("white,black")
    with pytest.raises(ImgCmpError, match="unknown view"):
        parse_views("id,heatmap")


def test_res_must_be_two_positive_integers():
    assert parse_res("1200,900") == (1200, 900)
    with pytest.raises(ImgCmpError, match="W,H"):
        parse_res("1200")
    with pytest.raises(ImgCmpError, match="at least 32x32"):
        parse_res("8,8")


# ---------------------------------------------------------------- 调色板

def test_palette_colours_are_unique_and_never_pure_black():
    colors = [palette_color(i) for i in range(4000)]

    assert len(set(colors)) == len(colors)
    assert all(min(c) >= 8 for c in colors)
    assert all(max(c) <= 248 for c in colors)
    assert tuple(BACKGROUND_RGB) not in colors


def test_palette_steps_are_wide_enough_to_absorb_one_lsb_of_noise():
    assert palette_color(1)[0] - palette_color(0)[0] == 8


# ---------------------------------------------------------------- assemble spec

def test_a_spec_resolves_part_paths_against_models_root():
    spec = parse_assemble_spec(os.path.join(ASSEMBLIES, "mini_split.yaml"))

    assert spec["name"] == "mini_split"
    assert [p["name"] for p in spec["parts"]] == ["hull", "wing", "dish"]
    assert os.path.isabs(spec["parts"][0]["path"])
    assert os.path.isfile(spec["parts"][0]["path"])
    assert spec["parts"][1]["rot_deg"] == (0.0, 0.0, 90.0)
    assert spec["parts"][0]["scale"] == (1.0, 1.0, 1.0)


def test_a_spec_pointing_at_a_missing_part_file_is_refused():
    with pytest.raises(ImgCmpError, match="part file not found"):
        parse_assemble_spec(os.path.join(ASSEMBLIES, "mini_broken.yaml"))


def test_an_empty_parts_list_is_refused(tmp_path):
    path = tmp_path / "empty.yaml"
    path.write_text("parts: []\n", encoding="utf-8")

    with pytest.raises(ImgCmpError, match="non-empty 'parts'"):
        parse_assemble_spec(str(path))


def test_a_part_without_a_file_key_is_refused(tmp_path):
    path = tmp_path / "nofile.yaml"
    path.write_text("parts:\n  - name: hull\n", encoding="utf-8")

    with pytest.raises(ImgCmpError, match="misses 'part'"):
        parse_assemble_spec(str(path))


def test_duplicate_part_names_are_refused(tmp_path):
    path = tmp_path / "dup.yaml"
    path.write_text(
        "models_root: " + os.path.join(FIXTURES, "parts") + "\n"
        "parts:\n  - {name: a, part: box.glb}\n  - {name: a, part: bar.glb}\n",
        encoding="utf-8")

    with pytest.raises(ImgCmpError, match="duplicate part name"):
        parse_assemble_spec(str(path))


def test_a_non_positive_scale_is_refused(tmp_path):
    path = tmp_path / "scale.yaml"
    path.write_text(
        "models_root: " + os.path.join(FIXTURES, "parts") + "\n"
        "parts:\n  - {name: a, part: box.glb, scale: [1, 1, 0]}\n",
        encoding="utf-8")

    with pytest.raises(ImgCmpError, match="scale must be positive"):
        parse_assemble_spec(str(path))


# ---------------------------------------------------------------- 语义来源

def test_semantics_come_from_the_declared_yaml_and_are_marked_as_such():
    doc = {"default_shape_class": "other",
           "anchor": {"name": "dish", "size_m": 70.0},
           "parts": {"dish": "circle", "truss*": "rod"}}
    sem = resolve_semantics(doc, ["dish", "truss_dish", "base"])

    assert sem["shape_class"] == {"dish": "circle", "truss_dish": "rod", "base": "other"}
    assert sem["source"] == {"dish": "semantic", "truss_dish": "semantic", "base": "derived"}
    assert sem["declared"] == ["dish", "truss_dish"]
    assert sem["anchor"] == "dish" and sem["anchor_size_m"] == 70.0


def test_an_undeclared_run_marks_every_component_as_derived():
    sem = resolve_semantics(None, ["a", "b"])

    assert sem["source"] == {"a": "derived", "b": "derived"}
    assert sem["shape_class"] == {"a": "other", "b": "other"}


def test_an_unknown_shape_class_or_anchor_is_refused():
    with pytest.raises(ImgCmpError, match="unknown shape_class"):
        resolve_semantics({"parts": {"a": "ellipse"}}, ["a"])
    with pytest.raises(ImgCmpError, match="declared anchor"):
        resolve_semantics({"anchor": "nope"}, ["a"])


# ---------------------------------------------------------------- 真值提取

def test_geometry_is_computed_from_the_id_image_only():
    legend = [palette_color(i) for i in range(4)]
    arr = _synthetic_id(legend, [
        (legend[0], "rect", [40, 60, 160, 240]),
        (legend[1], "circle", [200, 60, 320, 180]),
        (legend[2], "ring", [360, 60, 460, 160]),
        (legend[3], "rect", [40, 242, 440, 260]),
    ])
    truth = _truth_for(arr, legend, ["box", "disc", "ring", "rod"], contact_tol=2.0)
    by = truth["components_by_name"]

    assert truth["component_count"] == 4
    assert by["box"]["bbox_xyxy"] == [40, 60, 161, 241]
    assert by["rod"]["bbox_xyxy"] == [40, 242, 441, 261]
    assert by["box"]["pixel_count"] == 121 * 181
    assert by["ring"]["pixel_count"] > 0 and by["ring"]["islands"] == 1
    assert abs(sum(c["area_fraction"] for c in truth["components"]) - 1.0) < 1e-5
    assert truth["total_payload_height_px"] == 261 - 60
    assert truth["id_match"]["unmatched_pixels"] == 0


def test_contacts_report_the_bounding_box_gap_in_pixels():
    legend = [palette_color(i) for i in range(2)]
    arr = _synthetic_id(legend, [
        (legend[0], "rect", [40, 60, 160, 240]),
        (legend[1], "rect", [40, 242, 440, 260]),
    ])
    truth = _truth_for(arr, legend, ["box", "rod"], contact_tol=2.0)
    by = truth["components_by_name"]

    assert [c["part"] for c in by["box"]["contacts"]] == ["rod"]
    assert by["box"]["contacts"][0]["gap_px"] == 1.0
    assert by["box"]["contacts"][0]["bbox_overlap"] is False


def test_a_gap_wider_than_the_tolerance_is_not_a_contact():
    legend = [palette_color(i) for i in range(2)]
    arr = _synthetic_id(legend, [
        (legend[0], "rect", [40, 60, 160, 240]),
        (legend[1], "rect", [40, 300, 440, 318]),
    ])
    truth = _truth_for(arr, legend, ["a", "b"], contact_tol=2.0)

    assert truth["components_by_name"]["a"]["contacts"] == []
    assert truth["components_by_name"]["b"]["contacts"] == []


def test_two_blobs_of_the_same_colour_are_one_component_with_two_islands():
    legend = [palette_color(0)]
    arr = _synthetic_id(legend, [
        (legend[0], "rect", [40, 60, 100, 120]),
        (legend[0], "rect", [200, 200, 260, 260]),
    ])
    truth = _truth_for(arr, legend, ["pair"])

    assert truth["component_count"] == 1
    assert truth["components_by_name"]["pair"]["islands"] == 2


def test_an_undeclared_colour_is_folded_into_the_background():
    legend = [palette_color(0)]
    arr = _synthetic_id(legend, [
        (legend[0], "rect", [40, 60, 100, 120]),
        ((255, 0, 128), "rect", [200, 200, 260, 260]),
    ])
    truth = _truth_for(arr, legend, ["a"])

    assert truth["component_count"] == 1
    assert truth["id_match"]["unmatched_pixels"] == 61 * 61


def test_geometry_and_semantics_are_labelled_by_source():
    legend = [palette_color(0), palette_color(1)]
    arr = _synthetic_id(legend, [
        (legend[0], "rect", [40, 60, 160, 240]),
        (legend[1], "rect", [40, 242, 440, 260]),
    ])
    truth = _truth_for(arr, legend, ["box", "rod"],
                       shape_classes={"box": "rect", "rod": "rod"},
                       sources={"box": "semantic", "rod": "derived"})

    assert truth["truth_schema"]["geometry_source"].startswith("id_image")
    assert {c["name"]: c["source"] for c in truth["components"]} == {
        "box": "semantic", "rod": "derived"}
    assert {c["name"]: c["shape_class"] for c in truth["components"]} == {
        "box": "rect", "rod": "rod"}


def test_an_invisible_component_is_listed_but_not_counted():
    legend = [palette_color(i) for i in range(3)]
    arr = _synthetic_id(legend, [(legend[0], "rect", [40, 60, 160, 240])])
    truth = _truth_for(arr, legend, ["visible", "hidden_a", "hidden_b"])

    assert truth["component_count"] == 1
    assert truth["component_count_all"] == 3
    assert truth["components_by_name"]["hidden_a"]["visible"] is False
    assert truth["components_by_name"]["hidden_a"]["pixel_count"] == 0


def test_anchor_ratio_is_measured_against_the_declared_anchor():
    legend = [palette_color(i) for i in range(2)]
    arr = _synthetic_id(legend, [
        (legend[0], "rect", [40, 60, 160, 240]),
        (legend[1], "rect", [40, 242, 440, 260]),
    ])
    truth = _truth_for(arr, legend, ["box", "rod"], anchor="box", anchor_size_m=10.5)
    by = truth["components_by_name"]

    assert by["box"]["anchor_ratio"] == 1.0
    assert by["rod"]["anchor_ratio"] == pytest.approx(401.0 / 181.0, abs=1e-5)
    assert truth["anchor"] == {"name": "box", "size_m": 10.5}


# ---------------------------------------------------------------- 期望差异表

def _write_truth(out_dir, view, component_count, heights):
    path = out_dir / ("truth_%s.json" % view)
    path.write_text(json.dumps({
        "component_count": component_count,
        "total_payload_height_px": 100,
        "components": [{"name": n, "height_fraction": h, "area_fraction": 0.5}
                       for n, h in heights.items()],
    }), encoding="utf-8")
    return {"view": view, "truth": str(path)}


def test_the_expect_table_is_checked_against_the_declared_reference_pose(tmp_path):
    from primer.imgcmp.refgen import _check_expect

    truths = [_write_truth(tmp_path, "az30_el15_roll0", 2, {"body": 0.4}),
              _write_truth(tmp_path, "az0_el0_roll0", 4, {"body": 0.263})]
    pose, rows = _check_expect({"component_count": 4, "height_fraction": {"body": 0.263}},
                               truths, str(tmp_path))

    assert pose == "az0_el0_roll0"
    assert all(ok for _, _, _, ok in rows)


def test_the_expect_table_is_skipped_when_the_reference_pose_did_not_run(tmp_path):
    from primer.imgcmp.refgen import _check_expect

    truths = [_write_truth(tmp_path, "az30_el15_roll0", 2, {"body": 0.4})]
    pose, rows = _check_expect({"component_count": 4}, truths, str(tmp_path))

    assert pose is None and rows == []


def test_a_declared_difference_that_does_not_hold_is_reported_as_a_mismatch(tmp_path):
    from primer.imgcmp.refgen import _check_expect

    truths = [_write_truth(tmp_path, "az0_el0_roll0", 2, {"body": 0.4})]
    _, rows = _check_expect({"component_count": 4, "height_fraction": {"body": 0.263}},
                            truths, str(tmp_path))

    assert [ok for _, _, _, ok in rows] == [False, False]


# ---------------------------------------------------------------- id_legend 往返

def test_id_legend_round_trips_through_disk(tmp_path):
    legend = [palette_color(i) for i in range(3)]
    names = ["hull", "wing", "dish"]
    path = tmp_path / "id_legend.json"
    path.write_text(json.dumps({
        "legend": [{"index": i, "rgb": list(legend[i]), "name": names[i]} for i in range(3)]
    }), encoding="utf-8")

    back = json.loads(path.read_text(encoding="utf-8"))["legend"]
    assert [tuple(e["rgb"]) for e in back] == [palette_color(e["index"]) for e in back]
    assert [e["name"] for e in back] == names


def test_the_committed_id_sample_reproduces_its_committed_truth():
    """随仓库提交的合成样本：把 id.png ＋ id_legend.json 再算一遍，须与 truth.json 一致。"""
    with Image.open(os.path.join(ID_SAMPLE, "id.png")) as im:
        arr = np.asarray(im.convert("RGB"), dtype=np.uint8)
    legend_doc = json.loads(open(os.path.join(ID_SAMPLE, "id_legend.json"),
                                 encoding="utf-8").read())
    stored = json.loads(open(os.path.join(ID_SAMPLE, "truth.json"), encoding="utf-8").read())
    legend = [e["rgb"] for e in legend_doc["legend"]]
    names = [e["name"] for e in legend_doc["legend"]]
    shape = {c["name"]: c["shape_class"] for c in stored["components"]}

    truth = _truth_for(arr, legend, names, shape_classes=shape, contact_tol=2.0)

    assert truth["component_count"] == stored["component_count"] == 3
    assert truth["total_payload_height_px"] == stored["total_payload_height_px"]
    assert [c["name"] for c in truth["components"]] == [c["name"] for c in stored["components"]]
    for got, want in zip(truth["components"], stored["components"]):
        assert got["bbox_xyxy"] == want["bbox_xyxy"]
        assert got["pixel_count"] == want["pixel_count"]
        assert got["islands"] == want["islands"]
    assert truth["id_match"]["unmatched_pixels"] == 0


# ---------------------------------------------------------------- CLI 退出码

def test_selftest_exits_zero(capsys):
    assert main(["--selftest"]) == 0
    printed = capsys.readouterr().out

    assert "PASS" in printed and "FAIL" not in printed


def test_a_missing_model_exits_two(tmp_path, capsys):
    code = main(["--model", str(tmp_path / "nope.glb"), "--out", str(tmp_path / "out")])

    assert code == 2
    assert "model not found" in capsys.readouterr().err


def test_an_unsupported_model_format_exits_two(tmp_path, capsys):
    bad = tmp_path / "model.obj"
    bad.write_text("x", encoding="utf-8")

    assert main(["--model", str(bad), "--out", str(tmp_path / "out")]) == 2
    assert "unsupported model format" in capsys.readouterr().err


def test_out_is_required(tmp_path, capsys):
    assert main(["--model", str(tmp_path / "x.glb")]) == 2
    assert "--out DIR is required" in capsys.readouterr().err


def test_model_and_assemble_are_mutually_exclusive(tmp_path, capsys):
    code = main(["--model", "a.glb", "--assemble", os.path.join(ASSEMBLIES, "mini_split.yaml"),
                 "--out", str(tmp_path / "out")])

    assert code == 2
    assert "mutually exclusive" in capsys.readouterr().err


def test_a_broken_assemble_spec_exits_two(tmp_path, capsys):
    code = main(["--assemble", os.path.join(ASSEMBLIES, "mini_broken.yaml"),
                 "--out", str(tmp_path / "out")])

    assert code == 2
    assert "part file not found" in capsys.readouterr().err


def test_views_without_id_exits_two(tmp_path, capsys):
    code = main(["--assemble", os.path.join(ASSEMBLIES, "mini_split.yaml"),
                 "--views", "white", "--out", str(tmp_path / "out")])

    assert code == 2
    assert "must include 'id'" in capsys.readouterr().err


def test_a_missing_truth_yaml_exits_two(tmp_path, capsys):
    code = main(["--assemble", os.path.join(ASSEMBLIES, "mini_split.yaml"),
                 "--truth", str(tmp_path / "nope.yaml"), "--out", str(tmp_path / "out")])

    assert code == 2
    assert "truth yaml not found" in capsys.readouterr().err


def test_the_truth_truth_alias_is_accepted(tmp_path):
    from primer.imgcmp.refgen import build_parser

    parsed = build_parser().parse_args(["--assemble", "s.yaml", "--out", "o",
                                        "--truth-truth", "t.yaml"])

    assert parsed.truth == "t.yaml"


# ---------------------------------------------------------------- Blender 路径

@needs_blender
def test_end_to_end_render_of_the_mini_assembly(tmp_path, capsys):
    out = tmp_path / "mini"
    code = main(["--assemble", os.path.join(ASSEMBLIES, "mini_split.yaml"), "--out", str(out),
                 "--angles", "0,0,0;30,15,45", "--ortho", "--res", "320,240"])
    printed = capsys.readouterr().out

    assert code == 0, printed
    for view in ("az0_el0_roll0", "az30_el15_roll45"):
        vdir = out / "views" / view
        for name in ("id.png", "id_legend.json", "silhouette.png", "white_cad.png",
                     "black_render.png", "truth.json"):
            assert (vdir / name).is_file(), "%s/%s" % (view, name)
    summary = json.loads((out / "truth_summary.json").read_text(encoding="utf-8"))

    assert [p["view"] for p in summary["poses"]] == ["az0_el0_roll0", "az30_el15_roll45"]
    assert all(len(p["sha256_16"]) == 16 for p in summary["poses"])
    truth = json.loads((out / "views" / "az0_el0_roll0" / "truth.json").read_text(encoding="utf-8"))

    assert truth["component_count"] == 3
    assert truth["id_match"]["unmatched_pixels"] == 0
    legend = json.loads((out / "views" / "az0_el0_roll0" / "id_legend.json")
                        .read_text(encoding="utf-8"))

    assert [e["rgb"] for e in legend["legend"]] == [list(palette_color(i)) for i in range(3)]


@needs_blender
def test_the_id_image_uses_exactly_the_palette_colours(tmp_path):
    out = tmp_path / "mini"
    assert main(["--assemble", os.path.join(ASSEMBLIES, "mini_split.yaml"), "--out", str(out),
                 "--angles", "0,0,0", "--res", "320,240"]) == 0
    with Image.open(out / "views" / "az0_el0_roll0" / "id.png") as im:
        arr = np.asarray(im.convert("RGB"), dtype=np.uint8)
    colors = {tuple(int(v) for v in c) for c in np.unique(arr.reshape(-1, 3), axis=0)}

    assert colors <= {tuple(BACKGROUND_RGB), *(palette_color(i) for i in range(3))}


@needs_blender
def test_the_grid_run_writes_one_directory_per_pose(tmp_path):
    out = tmp_path / "grid"
    assert main(["--assemble", os.path.join(ASSEMBLIES, "mini_split.yaml"), "--out", str(out),
                 "--az-el-grid", "30,15", "--res", "200,160"]) == 0
    poses = sorted(p.name for p in (out / "views").iterdir())

    assert len(poses) == 9
    assert all((out / "views" / p / "truth.json").is_file() for p in poses)
