# -*- coding: utf-8 -*-
"""T2a 组件特征表（用户真正会敲的命令：``python3 -m primer.imgcmp.feat``）测试。

分两层：

- **自造图**：合成一张含 rect/circle/ring/rod、含相接与间隙的白底图与黑底图，验分割、
  形状类、面积/高度分数、接触关系、锚件比率与 CLI 退出码——不依赖任何外部产物。
- **refgen 真值对照**：若本机能读到 refgen 产物（``out/still/imgcmp/refgen/``），
  就逐机位把本工具的表与 ``truth.json`` 对照并给出误差；读不到则整组 skip。
  搜索顺序：``$PRIMER_IMGCMP_REFGEN`` → 仓库内 ``tests/fixtures/imgcmp/refgen``。
"""

from __future__ import annotations

import glob
import json
import os

import numpy as np
import pytest
import yaml

from primer.imgcmp import feat
from primer.imgcmp.common import box_gap, box_iou

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 2034 任务侧 refgen 产物（T2a 的客观评分源）。本机没有就整组 skip；
# 换机器/换目录用 $PRIMER_IMGCMP_REFGEN 覆盖即可，测试不依赖这个默认值。
TASK_REFGEN = ("/Users/huo/Documents/kimi/Workspaces/行星探测工程/_primer/scene/"
               "interferometer/observatory/out/still/imgcmp/refgen")
REFGEN_CANDIDATES = (
    os.environ.get("PRIMER_IMGCMP_REFGEN"),
    TASK_REFGEN,
    os.path.join(REPO_ROOT, "tests", "fixtures", "imgcmp", "refgen"),
)
ANCHOR_FIXTURE = os.path.join(REPO_ROOT, "tests", "fixtures", "imgcmp", "anchors",
                              "observatory_2034.yaml")


def _refgen_root():
    for cand in REFGEN_CANDIDATES:
        if cand and os.path.isdir(cand) and glob.glob(os.path.join(cand, "**", "truth.json"),
                                                      recursive=True):
            return cand
    return None


REFGEN_ROOT = _refgen_root()


# ---------------------------------------------------------------- 自造图夹具

@pytest.fixture(scope="module")
def white_table(tmp_path_factory):
    path = tmp_path_factory.mktemp("feat") / "synth_white.png"
    feat._synth(white=True).tofile  # noqa: B018  (keep the helper imported for clarity)
    from PIL import Image
    Image.fromarray(feat._synth(white=True), mode="RGB").save(path)
    return feat.build_table(str(path), kind="auto")


@pytest.fixture(scope="module")
def black_table(tmp_path_factory):
    path = tmp_path_factory.mktemp("feat") / "synth_black.png"
    from PIL import Image
    Image.fromarray(feat._synth(white=False), mode="RGB").save(path)
    return feat.build_table(str(path), kind="auto")


@pytest.mark.parametrize("fixture_name", ["white_table", "black_table"])
def test_segmentation_counts_and_boxes(request, fixture_name):
    table = request.getfixturevalue(fixture_name)
    assert table["component_count"] == 4
    assert table["total_payload_height_px"] == 360
    seen = {}
    for comp in table["components"]:
        seen[comp["shape_class"]] = comp
    assert sorted(seen) == ["circle", "rect", "ring", "rod"]
    for shape, want in feat.EXPECT_BOX.items():
        got = seen[shape]["bbox_xyxy"]
        assert max(abs(a - b) for a, b in zip(got, want)) <= 2, (shape, got, want)


@pytest.mark.parametrize("fixture_name", ["white_table", "black_table"])
def test_areas_and_height_fractions(request, fixture_name):
    table = request.getfixturevalue(fixture_name)
    by_shape = {c["shape_class"]: c for c in table["components"]}
    assert by_shape["rect"]["pixel_count"] == 160 * 120
    assert by_shape["rod"]["pixel_count"] == 198 * 14
    assert abs(by_shape["circle"]["pixel_count"] - np.pi * 3600) / (np.pi * 3600) < 0.03
    assert by_shape["rect"]["height_fraction"] == pytest.approx(120 / 360.0, abs=0.01)
    assert by_shape["rod"]["height_fraction"] == pytest.approx(14 / 360.0, abs=0.01)
    total = sum(c["pixel_count"] for c in table["components"])
    assert by_shape["rect"]["area_fraction"] == pytest.approx(160 * 120 / total, abs=1e-6)


@pytest.mark.parametrize("fixture_name", ["white_table", "black_table"])
def test_contacts_and_gaps(request, fixture_name):
    table = request.getfixturevalue(fixture_name)
    by_shape = {c["shape_class"]: c for c in table["components"]}
    rect, rod = by_shape["rect"], by_shape["rod"]
    assert 0 < box_gap(rect["bbox_xyxy"], rod["bbox_xyxy"]) <= table["contact_tol_px"]
    assert rod["index"] in [c["part"] for c in rect["contacts"]]
    circle, ring = by_shape["circle"], by_shape["ring"]
    assert box_gap(circle["bbox_xyxy"], ring["bbox_xyxy"]) > table["contact_tol_px"]
    assert ring["index"] not in [c["part"] for c in circle["contacts"]]


def test_ring_has_a_round_hole(white_table):
    ring = next(c for c in white_table["components"] if c["shape_class"] == "ring")
    assert ring["holes"]["count"] == 1
    hole = ring["holes"]["items"][0]
    assert hole["circle_fill"] == pytest.approx(1.0, abs=0.12)
    assert hole["elongation"] == pytest.approx(1.0, abs=0.1)
    assert ring["openings"]["count"] == 1


def test_anchor_file_ratio(tmp_path):
    from PIL import Image
    path = tmp_path / "synth.png"
    Image.fromarray(feat._synth(white=True), mode="RGB").save(path)
    anchors = tmp_path / "anchors.yaml"
    anchors.write_text("anchors:\n"
                       "  - name: rod\n"
                       "    ref_m: 2.0\n"
                       "    selector: {rule: bbox_longest, axis: x}\n", encoding="utf-8")
    table = feat.build_table(str(path), anchors_path=str(anchors))
    by_shape = {c["shape_class"]: c for c in table["components"]}
    assert by_shape["rod"]["anchor_ratio"] == 1.0
    assert by_shape["rod"]["size_m"] == pytest.approx(2.0)
    assert by_shape["circle"]["anchor_ratio"] == pytest.approx(120 / 198.0, abs=0.02)
    assert table["anchors"][0]["component_index"] == by_shape["rod"]["index"]


def test_repo_example_anchor_file_parses():
    specs = feat.load_anchors(ANCHOR_FIXTURE)
    assert [s["name"] for s in specs] == ["shield", "primary_mirror", "tube"]
    assert specs[0]["ref_m"] == 10.5
    assert specs[2]["selector"]["rule"] == "position"


def test_bad_anchor_file(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("anchors:\n  - name: x\n", encoding="utf-8")
    with pytest.raises(feat.ImgCmpError):
        feat.load_anchors(str(bad))
    with pytest.raises(feat.ImgCmpError):
        feat.load_anchors(str(tmp_path / "missing.yaml"))


def test_kind_forced_and_invalid(tmp_path):
    from PIL import Image
    path = tmp_path / "synth.png"
    Image.fromarray(feat._synth(white=True), mode="RGB").save(path)
    forced = feat.build_table(str(path), kind="white")
    assert forced["kind"] == "white" and forced["kind_requested"] == "white"
    with pytest.raises(feat.ImgCmpError):
        feat.build_table(str(path), kind="grey")


# ---------------------------------------------------------------- compare 各类差异

def _component(index, bbox, pixels, shape, height_fraction, area_fraction,
               anchor_ratio=None, holes=0, openings=0, contacts=()):
    span = max(bbox[2] - bbox[0], bbox[3] - bbox[1])
    return {
        "index": index, "bbox_xyxy": list(bbox), "pixel_count": pixels,
        "shape_class": shape, "height_fraction": height_fraction,
        "area_fraction": area_fraction, "anchor_ratio": anchor_ratio,
        "span_px": span, "centroid": [(bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0],
        "shape_descriptors": {}, "holes": {"count": holes, "items": []},
        "openings": {"count": openings, "top_band_count": 0, "items": []},
        "contacts": [{"part": p, "gap_px": 0.0, "bbox_overlap": False} for p in contacts],
    }


def _table(components, image="x.png"):
    return {"image": image, "components": components, "total_payload_height_px": 100,
            "payload_box_xyxy": [0, 0, 400, 100], "kind": "white", "min_area": 64}


def test_compare_kinds_cover_every_declared_kind():
    ref = _table([
        _component(0, (0, 0, 100, 60), 6000, "rect", 0.60, 0.60, 1.0, contacts=(1,)),
        _component(1, (100, 0, 200, 40), 4000, "circle", 0.40, 0.40, 0.40),
        _component(2, (250, 0, 300, 30), 1000, "rod", 0.30, 0.10, 0.30),
        _component(3, (320, 0, 340, 20), 500, "other", 0.20, 0.05),
    ])
    ours = _table([
        _component(0, (0, 0, 100, 20), 3000, "rect", 0.20, 0.30, 1.0, contacts=(1, 2)),
        _component(1, (60, 0, 160, 40), 5000, "rect", 0.40, 0.50, 0.60),
        _component(2, (250, 0, 310, 30), 1500, "rod", 0.30, 0.15, 0.30),
        _component(4, (400, 0, 430, 20), 400, "other", 0.10, 0.05),
        _component(5, (500, 0, 520, 20), 300, "other", 0.10, 0.03),
    ])
    diff = feat.compare_tables(ref, ours)
    kinds = {d["kind"] for d in diff["diffs"]}
    assert "count" in kinds
    assert "height_fraction" in kinds
    assert "shape_class" in kinds       # ref#1 circle -> ours#1 rect
    assert "anchor_ratio" in kinds      # ref#1 0.40 -> ours#1 0.60
    assert "contact" in kinds           # ours#0 相接件数 1 -> 2
    assert "removed" in kinds           # ref#3 无对应
    assert "added" in kinds             # ours#4 无对应
    for item in diff["diffs"]:
        assert {"kind", "ref", "ours", "delta", "rank", "evidence"} <= set(item)
        assert item["rank"] in ("major", "minor")
    assert diff["counts"]["ref"] == 4 and diff["counts"]["ours"] == 5
    assert diff["counts"]["delta"] == 1
    assert "merged" in kinds            # ref#0 ~ ours#0＋ours#1（bbox 相交多于一件）
    heights = [d for d in diff["diffs"] if d["kind"] == "height_fraction"]
    assert any(d["rank"] == "major" for d in heights)   # ref#0 0.60 -> ours#0 0.20


def test_compare_reports_merged_and_split():
    ref = _table([_component(0, (0, 0, 200, 100), 20000, "rect", 1.0, 1.0)])
    ours = _table([
        _component(0, (0, 0, 100, 100), 10000, "rect", 1.0, 0.5),
        _component(1, (100, 0, 200, 100), 10000, "rect", 1.0, 0.5),
    ])
    diff = feat.compare_tables(ref, ours)
    assert "merged" in {d["kind"] for d in diff["diffs"]}

    back = feat.compare_tables(ours, ref)
    assert "split" in {d["kind"] for d in back["diffs"]}


def test_compare_hole_count_and_tolerance_override():
    ref = _table([_component(0, (0, 0, 100, 100), 10000, "rect", 1.0, 1.0, openings=2)])
    ours = _table([_component(0, (0, 0, 100, 100), 10000, "rect", 1.0, 1.0, openings=1)])
    diff = feat.compare_tables(ref, ours)
    hole = next(d for d in diff["diffs"] if d["kind"] == "hole_count")
    assert (hole["ref"], hole["ours"], hole["delta"]) == (2, 1, -1)

    moved = _table([_component(0, (0, 0, 100, 100), 10000, "rect", 1.0, 1.0, openings=2)])
    moved["components"][0]["height_fraction"] = 0.9
    loose = feat.compare_tables(ref, moved, {"height_fraction": 0.2})
    assert not [d for d in loose["diffs"] if d["kind"] == "height_fraction"]
    tight = feat.compare_tables(ref, moved, {"height_fraction": 0.05})
    assert [d for d in tight["diffs"] if d["kind"] == "height_fraction"]
    assert feat.compare_tables(ref, moved, {"match_score": 2.0})["pairs"] == []


def test_compare_rejects_empty_and_bad_shapes():
    with pytest.raises(feat.ImgCmpError):
        feat.compare_tables(_table([]), _table([]))


def test_pairing_prefers_the_overlapping_component():
    ref = _table([
        _component(0, (0, 0, 50, 50), 2500, "rect", 0.5, 0.5),
        _component(1, (200, 0, 260, 50), 3000, "rect", 0.5, 0.5),
    ])
    ours = _table([
        _component(0, (200, 0, 260, 50), 3000, "rect", 0.5, 0.5),
        _component(1, (0, 0, 50, 50), 2500, "rect", 0.5, 0.5),
    ])
    diff = feat.compare_tables(ref, ours)
    assert {(p["ref"], p["ours"]) for p in diff["pairs"]} == {(0, 1), (1, 0)}
    assert not [d for d in diff["diffs"] if d["kind"] in ("added", "removed")]


# ---------------------------------------------------------------- 背景相对阈值 / ROI / 图注 / 开口归属

def test_background_threshold_keeps_dim_and_bright_parts(tmp_path):
    """黑底图里"对象偏暗"的那件：包含性掩膜要纳入，全局 Otsu 会漏。"""
    from PIL import Image
    path = tmp_path / "dark.png"
    Image.fromarray(feat._synth_dark_object(), mode="RGB").save(path)
    table = feat.build_table(path, kind="black")
    assert table["mask_strategy"] == "inclusive+edge_split"
    assert table["threshold"] == 6 + feat.BG_MARGIN_FLOOR     # 包含性支撑阈值
    assert table["component_count"] == 2
    forced = feat.build_table(path, kind="black",
                              threshold=feat.otsu_threshold(
                                  feat.to_gray(feat._synth_dark_object())))
    assert forced["component_count"] == 1          # Otsu 漏掉暗件
    assert forced["mask_strategy"] == "explicit"


def test_edge_split_separates_parts_joined_by_a_dark_seam(tmp_path):
    """两块亮件之间只有一条暗缝：边缘分割切开，单阈值连成一件。"""
    from PIL import Image
    path = tmp_path / "seam.png"
    Image.fromarray(feat._synth_seam(), mode="RGB").save(path)
    table = feat.build_table(path, kind="black")
    assert table["mask_strategy"] == "inclusive+edge_split"
    assert table["component_count"] == 2
    single = feat.build_table(path, kind="black",
                              threshold=feat.background_threshold(
                                  feat.to_gray(feat._synth_seam()), feat.KIND_BLACK))
    assert single["mask_strategy"] == "explicit"
    assert single["component_count"] == 1


def test_edge_split_neck_cases(tmp_path):
    """细颈：暗颈能切；**同亮细颈切不开**（判据的已知失败条件）。"""
    from PIL import Image
    dim = tmp_path / "neck_dim.png"
    bright = tmp_path / "neck_bright.png"
    Image.fromarray(feat._synth_neck(dim=True), mode="RGB").save(dim)
    Image.fromarray(feat._synth_neck(dim=False), mode="RGB").save(bright)
    assert feat.build_table(dim, kind="black")["component_count"] == 2
    assert feat.build_table(bright, kind="black")["component_count"] == 1


def test_background_stats_are_robust_to_noise():
    clean = np.full((80, 80, 3), 250, dtype=np.uint8)
    noisy = clean.copy()
    rng = np.random.default_rng(0)
    noisy[:4, :] = np.clip(250 + rng.integers(-6, 7, (4, 80, 1)), 0, 255).astype(np.uint8)
    assert feat.background_threshold(feat.to_gray(clean), "white") == 250 - feat.BG_MARGIN_FLOOR
    noisy_thr = feat.background_threshold(feat.to_gray(noisy), "white")
    assert noisy_thr <= 250 - feat.BG_MARGIN_FLOOR


def test_roi_limits_analysis_and_keeps_source_coordinates(tmp_path):
    from PIL import Image
    path = tmp_path / "extra.png"
    Image.fromarray(feat._synth_extra(white=True), mode="RGB").save(path)
    full = feat.build_table(path)
    assert full["component_count"] == 1 and full["annotation_count"] == 1
    assert full["annotation_bboxes"] == [feat.EXTRA_EXPECT["annotation_bbox"]]

    crop = feat.build_table(path, roi="40,290,400,80")
    assert list(crop["roi"]) == [40, 290, 400, 80]
    assert crop["component_count"] == 1 and crop["annotation_count"] == 0
    assert crop["components"][0]["bbox_xyxy"] == feat.EXTRA_EXPECT["tube_bbox"]
    assert crop["payload_box_xyxy"] == [40, 300, 440, 360]
    assert crop["analysed_size"] == [400, 80]

    # ROI 内落在对象内部时，背景统计仍取整幅图的边框带（不是 ROI 自己的边框）
    assert crop["threshold"] == full["threshold"]
    assert list(full["roi"]) == [0, 0, 720, 560]


def test_roi_rejects_bad_input(tmp_path):
    from PIL import Image
    path = tmp_path / "extra.png"
    Image.fromarray(feat._synth_extra(white=True), mode="RGB").save(path)
    with pytest.raises(feat.ImgCmpError):
        feat.build_table(path, roi="40,290,400")
    with pytest.raises(feat.ImgCmpError):
        feat.build_table(path, roi="9000,9000,10,10")


def test_annotation_is_counted_separately_from_components(tmp_path):
    from PIL import Image
    path = tmp_path / "extra.png"
    Image.fromarray(feat._synth_extra(white=True), mode="RGB").save(path)
    table = feat.build_table(path)
    kinds = {c["kind"] for c in table["components"]}
    assert kinds == {"component", "annotation"}
    assert table["region_count"] == table["component_count"] + table["annotation_count"]
    ann = next(c for c in table["components"] if c["kind"] == "annotation")
    assert ann["bbox_xyxy"] == feat.EXTRA_EXPECT["annotation_bbox"]


def test_openings_are_attributed_to_the_slender_host(tmp_path):
    from PIL import Image
    path = tmp_path / "extra.png"
    Image.fromarray(feat._synth_extra(white=True), mode="RGB").save(path)
    table = feat.build_table(path)
    tube = next(c for c in table["components"] if c["kind"] == "component")
    assert tube["slender"] is True
    assert tube["shape_class"] == "ring"
    assert tube["openings"]["count"] == 1
    assert len(table["openings"]) == 1
    opening = table["openings"][0]
    assert opening["host_component"] == tube["index"]
    assert opening["host_is_slender"] is True
    assert opening["area_px"] == 384 * 44


def test_merge_large_blobs_switch():
    """`--merge-large-blobs`：只剩"小块贴大块 + 浅谷脊"时并，其它一律不动。"""
    labels = np.zeros((3, 24), dtype=np.int32)
    score = np.zeros((3, 24), dtype=np.float64)
    labels[:, 1:10] = 1
    score[:, 1:10] = 200.0
    score[:, 9] = 110.0             # 谷脊：边界像素掉到 110
    labels[:, 10:13] = 2            # 小块：面积比 9/27 = 0.33
    score[:, 10:13] = 180.0
    score[:, 10] = 110.0
    labels[:, 16:23] = 3            # 另一块（与谁都不相邻）
    score[:, 16:23] = 200.0
    off = feat.merge_flat_basins(labels, score, feat.MERGE_CONTRAST)
    on = feat.merge_flat_basins(labels, score, feat.MERGE_CONTRAST,
                                large_rel=feat.MERGE_LARGE_REL,
                                area_ratio=feat.MERGE_LARGE_AREA_RATIO)
    assert len(off[off > 0].tolist()) and len(set(off[off > 0].tolist())) == 3   # 默认关：不动
    assert len(set(on[on > 0].tolist())) == 2          # 开：9 px 那块并进 27 px 那块
    # 面积相当的相邻块即使开了开关也不并（判据要的是"小块贴大块"）
    labels2 = np.zeros((3, 20), dtype=np.int32)
    s2 = np.zeros((3, 20), dtype=np.float64)
    labels2[:, 1:10] = 1
    labels2[:, 10:19] = 2
    s2[:, 1:19] = 200.0
    s2[:, 9] = 110.0
    s2[:, 10] = 110.0
    on2 = feat.merge_flat_basins(labels2, s2, feat.MERGE_CONTRAST,
                                 large_rel=feat.MERGE_LARGE_REL,
                                 area_ratio=feat.MERGE_LARGE_AREA_RATIO)
    assert len(set(on2[on2 > 0].tolist())) == 2


def test_merge_large_blobs_keeps_the_default_off(tmp_path):
    from PIL import Image
    path = tmp_path / "extra.png"
    Image.fromarray(feat._synth_extra(white=True), mode="RGB").save(path)
    default = feat.build_table(path)
    on = feat.build_table(path, merge_large_blobs=True)
    assert default["merge_large_blobs"] is False
    assert on["merge_large_blobs"] is True
    assert feat._cli_code(["table", str(path), "--merge-large-blobs",
                           "--json", str(tmp_path / "o.json")]) == 0
    assert json.loads((tmp_path / "o.json").read_text(encoding="utf-8"))["merge_large_blobs"]


def test_compare_ignores_annotations(tmp_path):
    from PIL import Image
    path = tmp_path / "extra.png"
    Image.fromarray(feat._synth_extra(white=True), mode="RGB").save(path)
    full = feat.build_table(path)
    crop = feat.build_table(path, roi="40,290,400,80")
    diff = feat.compare_tables(full, crop)
    assert diff["counts"]["ref"] == diff["counts"]["ours"] == 1
    assert diff["counts"]["ref_annotation"] == 1
    assert diff["counts"]["ours_annotation"] == 0


# ---------------------------------------------------------------- CLI

def test_cli_table_and_compare_roundtrip(tmp_path):
    from PIL import Image
    path = tmp_path / "synth.png"
    Image.fromarray(feat._synth(white=True), mode="RGB").save(path)
    out = tmp_path / "out"
    assert feat._cli_code(["table", str(path), "--out", str(out)]) == 0
    table_json = out / "synth.feat.json"
    assert table_json.is_file()
    assert feat._cli_code(["compare", str(table_json), str(table_json), "--out", str(out)]) == 0
    diff_json = next(out.glob("*.diff.json"))
    diff = json.loads(diff_json.read_text(encoding="utf-8"))
    assert diff["diffs"] == []
    assert "compare" == diff["mode"]


def test_cli_exit_codes(tmp_path):
    from PIL import Image
    path = tmp_path / "synth.png"
    Image.fromarray(feat._synth(white=True), mode="RGB").save(path)
    table_json = tmp_path / "t.json"
    table_json.write_text(json.dumps(_table([])), encoding="utf-8")
    assert feat._cli_code(["table", str(tmp_path / "missing.png")]) == 2
    assert feat._cli_code(["table", str(path), "--kind", "grey"]) == 2
    assert feat._cli_code(["table", str(path), "--min-area", "0"]) == 2
    assert feat._cli_code(["table", str(path), "--selftest"]) == 0
    assert feat._cli_code([]) == 2
    assert feat._cli_code(["compare", str(tmp_path / "nope.json"), str(table_json)]) == 2
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"components": []}), encoding="utf-8")
    assert feat._cli_code(["compare", str(empty), str(empty)]) == 2
    assert feat._cli_code(["compare", str(table_json), str(table_json),
                           "--tol", "nope=1"]) == 2
    assert feat._cli_code(["compare", str(table_json), str(table_json),
                           "--tol", "height_fraction"]) == 2


def test_selftest_passes(capsys):
    assert feat.main(["--selftest"]) == 0
    out = capsys.readouterr().out
    assert "PASS" in out and "FAIL" not in out


# ---------------------------------------------------------------- refgen 真值对照

pytestmark_refgen = pytest.mark.skipif(REFGEN_ROOT is None,
                                       reason="refgen products not available on this machine")


def _refgen_views():
    """每个机位一条：``("grid_mini/views/az0_el0_roll0", <truth.json>)``。

    piece 必须带机位目录——只取到产品名会把同一产品的各机位折叠成同一键。
    """
    if REFGEN_ROOT is None:
        return []
    views = []
    for truth_path in sorted(glob.glob(os.path.join(REFGEN_ROOT, "**", "truth.json"),
                                       recursive=True)):
        rel = os.path.relpath(os.path.dirname(truth_path), REFGEN_ROOT)
        views.append((rel.replace(os.sep, "/"), truth_path))
    return views


@pytest.fixture(scope="module")
def refgen_tables():
    """把所有 refgen 机位跑一遍并缓存（每机位白底/黑底各一张表）。"""
    if REFGEN_ROOT is None:
        return {}
    out = {}
    for piece, truth_path in _refgen_views():
        truth = json.loads(open(truth_path, encoding="utf-8").read())
        for image_name in ("white_cad.png", "black_render.png"):
            image = os.path.join(os.path.dirname(truth_path), image_name)
            if os.path.isfile(image):
                out[(piece, image_name)] = (feat.build_table(image), truth)
    return out


@pytest.mark.skipif(REFGEN_ROOT is None, reason="refgen products not available")
@pytest.mark.parametrize("piece,truth_path", _refgen_views())
def test_refgen_tables_are_self_consistent(piece, truth_path, refgen_tables):
    """逐机位结构自洽：非空、框在界内、分数在 (0,1]、面积分数合计为 1。"""
    truth = json.loads(open(truth_path, encoding="utf-8").read())
    for image_name in ("white_cad.png", "black_render.png"):
        entry = refgen_tables.get((piece, image_name))
        if entry is None:
            continue
        table, _ = entry
        width, height = table["image_size"]
        assert table["component_count"] >= 1
        assert table["total_payload_height_px"] >= 1
        for comp in table["components"]:
            x0, y0, x1, y1 = comp["bbox_xyxy"]
            assert 0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height
            assert 0.0 < comp["height_fraction"] <= 1.0
            assert comp["shape_class"] in feat.SHAPE_ORDER
        total = sum(c["area_fraction"] for c in table["components"])
        assert total == pytest.approx(1.0, abs=1e-3)


def _truth_islands(truth) -> int:
    """真值的 8 邻接孤岛总数：$(按件) sum(islands)——与"可见材料连通区"同口径。"""
    return sum(int(c.get("islands", 0)) for c in truth["components"])


@pytest.mark.skipif(REFGEN_ROOT is None, reason="refgen products not available")
def test_refgen_object_height_accuracy_mostly_holds(refgen_tables):
    """载荷总高（高度分数的分母）是 T2a 最该稳的量。

    判据取"多数成立"而不是"逐例成立"：实测白底与黑底差别很大，故**分开设线**，
    线设在实测值下方一点，只在明显退化时报警。
    """
    stats = {}
    worst = []
    for (piece, image_name), (table, truth) in refgen_tables.items():
        want = truth["total_payload_height_px"]
        got = table["total_payload_height_px"]
        slot = stats.setdefault(image_name, [0, 0])
        slot[1] += 1
        if abs(got - want) <= max(5, 0.08 * want):
            slot[0] += 1
        else:
            worst.append((piece, image_name, got, want))
    assert stats["white_cad.png"][1] >= 5 and stats["black_render.png"][1] >= 5
    assert stats["white_cad.png"][0] / stats["white_cad.png"][1] >= 0.85, worst
    assert stats["black_render.png"][0] / stats["black_render.png"][1] >= 0.60, worst


@pytest.mark.skipif(REFGEN_ROOT is None, reason="refgen products not available")
def test_refgen_component_count_agreement_both_bases(refgen_tables):
    """件数一致率**两个口径都钉住**：

    - 与 ``truth.component_count``（spec 声明的语义件）——仅供语义评分，必然偏低；
    - 与 ``truth.islands`` 总数（8 邻接孤岛）——这才是"可见材料连通区"的对位口径。

    第 3 轮改成边缘分割（包含性支撑 + Otsu 核 + 分水岭）后实测 9/38 = 24%、soft 30%，
    与第 2 轮单阈值口径（10/38 = 26%、soft 33%）基本持平：边缘分割换来的是"暗件不丢 +
    辉光桥不糊"（`_ours_fig3b` 由 1 件到 9 件），代价是表面有纹理的整器（如 ast_bennu
    黑底）会被切成几块。

    真值里 ``dsn70m``（649–732 孤岛）与 ``jwst``（585）的孤岛数来自桁架/镜片的碎块，
    像素上根本切不出来；一并纳入统计会把这些不可达案例算进去，故这里同时钉住
    "全体"与"排除孤岛 >100 的模型"两条线。
    """
    hit_c = hit_i = 0
    soft_i = soft_n = 0
    for (_piece, image_name), (table, truth) in refgen_tables.items():
        ours = table["component_count"]
        hit_c += ours == truth["component_count"]
        hit_i += ours == _truth_islands(truth)
        if _truth_islands(truth) <= 100:
            soft_n += 1
            soft_i += ours == _truth_islands(truth)
    total = len(refgen_tables)
    assert total >= 20
    assert hit_i / total >= 0.20                       # 实测 9/38 = 24%
    assert hit_c / total >= 0.18                       # 实测 8/38 = 21%
    assert soft_i / soft_n >= 0.25                     # 排除碎块模型后 = 30%


PAIR_VIEWS = ("pair/module_split", "pair/module_long")


@pytest.mark.skipif(REFGEN_ROOT is None, reason="refgen products not available")
@pytest.mark.parametrize("piece", PAIR_VIEWS)
def test_refgen_pair_truth_score(piece):
    """受控对的逐件对照：组件数、bearing 件的 bbox/面积/高度分数误差。"""
    report = _score_piece(piece)
    if piece == "pair/module_long":
        # 该件的可见材料区恰好等于 spec 声明的件数，两个口径同时对上
        assert report["component_count_ours"] == report["component_count_truth"] == 3
        assert abs(report["component_count_ours"] - report["islands_truth"]) <= 1
    else:
        # 该件的可见材料区比 refgen 声明的部件多（真值是 spec 声明的件，不是像素区），
        # 因此对 **islands** 口径设线；对 components 口径只作记录。
        assert abs(report["component_count_ours"] - report["islands_truth"]) <= 2
    # 白底 CAD：总高与真值一致（≤2 px）；黑底渲染：对象暗部会掉出亮度阈值，
    # 这里只断言"没跑飞"（≤ 真值的 40%），实测值与根因写在交付说明里。
    assert report["height_error_px"]["white"] <= 2
    assert report["height_error_px"]["black"] <= 0.40 * 208
    for entry in report["cores"]:
        assert entry["centre_inside"], entry
        if entry["tag"] == "white":
            assert entry["height_fraction_error"] <= 0.06, entry
            assert entry["area_fraction_error"] <= 0.40, entry


@pytest.mark.skipif(REFGEN_ROOT is None, reason="refgen products not available")
def test_refgen_pair_diff_height_fraction():
    """module_split vs module_long：compare 必须报出「局部高件 ↔ 整器高件」的差异。"""
    left = os.path.join(REFGEN_ROOT, "pair", "module_split", "views", "az0_el0_roll0",
                        "white_cad.png")
    right = os.path.join(REFGEN_ROOT, "pair", "module_long", "views", "az0_el0_roll0",
                         "white_cad.png")
    if not (os.path.isfile(left) and os.path.isfile(right)):
        pytest.skip("pair views not rendered")
    diff = feat.compare_tables(feat.build_table(left), feat.build_table(right))
    kinds = {d["kind"] for d in diff["diffs"]}
    assert "count" in kinds
    height = [d for d in diff["diffs"] if d["kind"] == "height_fraction"]
    assert height, kinds
    assert max(d["delta"] for d in height) >= 0.5
    assert max(d["delta"] for d in height) == pytest.approx(1.0 - 0.295, abs=0.05)


# 受控缺陷对里的镜件：`mirror_hex_regular` 是正六边形（polygon/hexagon），
# `mirror_hex` 是 1.35 拉长六边形（附加用例，同判 polygon/hexagon），
# `mirror_round` 是同构的圆板（必须仍判 circle）。三个 spec 的其余四件逐值相同。
MIRROR_VIEW = "views/az0_el0_roll0"
MIRROR_SHAPE = {
    "pair/mirror_hex_regular/" + MIRROR_VIEW: ("polygon", "hexagon", 6),
    "pair/mirror_hex/" + MIRROR_VIEW: ("polygon", "hexagon", 6),
    "pair/mirror_round/" + MIRROR_VIEW: ("circle", None, None),
}


def _centre_piece(table):
    """受控对的镜件＝居中、面积最大的那件（两侧遮阳屏盘与上下桁架杆都不居中/更小）。"""
    width = table["image_size"][0]
    centre = [c for c in table["components"]
              if abs(c["centroid"][0] - width / 2.0) <= 0.06 * width]
    assert centre, "no centred component in %s" % table["image"]
    return max(centre, key=lambda c: c["pixel_count"])


@pytest.mark.skipif(REFGEN_ROOT is None, reason="refgen products not available")
@pytest.mark.parametrize("piece", sorted(MIRROR_SHAPE))
@pytest.mark.parametrize("image_name", ["white_cad.png", "black_render.png"])
def test_refgen_hexagon_is_polygon_and_circle_stays_circle(piece, image_name, refgen_tables):
    """正六边形判 polygon/hexagon、同构圆板判 circle——两者必须分开，圆不被误判。

    这是第 5 轮补的 polygon 判据（凸包抽稀 5–10 条直边 + 残差 + 凸包充实度）的验收：
    正六边形与圆的 `circle_fill`/`elongation` 在数值上重合，旧判据无法分开；新判据靠
    "轮廓直边数"把它们区分开，且圆盘抽稀出 16–19 段、落在上限之外仍判 circle。
    """
    entry = refgen_tables.get((piece, image_name))
    if entry is None:
        pytest.skip("%s/%s not rendered" % (piece, image_name))
    table, _ = entry
    want_class, want_name, want_vertices = MIRROR_SHAPE[piece]
    mirror = _centre_piece(table)
    desc = mirror["shape_descriptors"]
    assert mirror["shape_class"] == want_class, (piece, image_name, mirror["shape_class"])
    if want_name is not None:
        assert desc["polygon_name"] == want_name
        assert desc["polygon_vertices"] == want_vertices
        assert desc["polygon_residual"] <= feat.POLY_MAX_RESIDUAL
        assert 5 <= desc["polygon_vertices"] <= feat.POLY_MAX_VERTICES
    else:
        # 圆盘：抽稀段数必须多于 polygon 上限，且圆度落在 circle 带内
        assert desc["polygon_vertices"] > feat.POLY_MAX_VERTICES
        assert 0.88 <= desc["circle_fill"] <= 1.12
        assert desc["elongation"] <= 1.25


@pytest.mark.skipif(REFGEN_ROOT is None, reason="refgen products not available")
@pytest.mark.parametrize("image_name", ["white_cad.png", "black_render.png"])
def test_refgen_circle_pair_has_no_spurious_polygon(image_name, refgen_tables):
    """圆板那一侧整表不得出现 polygon 件（polygon 判据不许误伤圆/杆/环）。"""
    entry = refgen_tables.get(("pair/mirror_round/" + MIRROR_VIEW, image_name))
    if entry is None:
        pytest.skip("pair/mirror_round/%s not rendered" % image_name)
    table, _ = entry
    assert not [c for c in table["components"] if c["shape_class"] == "polygon"]
    assert [c for c in table["components"] if c["shape_class"] == "circle"]


def test_repo_example_anchors_run_on_a_pair_image():
    if REFGEN_ROOT is None:
        pytest.skip("refgen products not available")
    image = os.path.join(REFGEN_ROOT, "pair", "module_split", "views", "az0_el0_roll0",
                         "white_cad.png")
    if not os.path.isfile(image):
        pytest.skip("pair views not rendered")
    table = feat.build_table(image, anchors_path=ANCHOR_FIXTURE)
    assert len(table["anchors"]) == 3
    shield = table["anchors"][0]
    assert shield["rule"] == "largest" and shield["ref_m"] == 10.5
    assert shield["component_index"] is not None
    largest = max(table["components"], key=lambda c: c["pixel_count"])
    assert largest["anchor_ratio"] == 1.0
    assert largest["size_m"] == pytest.approx(10.5)


def _score_piece(piece):
    """逐件评分：把本工具的组件与 refgen 真值组件按 bbox IoU 配对，给出误差。

    真值是 **spec 声明的部件**（遮挡感知），本工具给的是 **可见材料区**——件与件不必一一
    对应，所以判据落在"配对件的高度分数/面积分数误差"与"配对件中心仍落在真值框内"
    这两条可比量上，而不是外接框逐坐标相等。
    """
    report = {"piece": piece, "cores": []}
    for image_name, tag in (("white_cad.png", "white"), ("black_render.png", "black")):
        truth_path = os.path.join(REFGEN_ROOT, piece, "views", "az0_el0_roll0", "truth.json")
        truth = json.loads(open(truth_path, encoding="utf-8").read())
        image = os.path.join(os.path.dirname(truth_path), image_name)
        if not os.path.isfile(image):
            continue
        table = feat.build_table(image)
        ours = table["components"]
        xs = [c["bbox_xyxy"][k] for c in truth["components"] for k in (0, 2)]
        ys = [c["bbox_xyxy"][k] for c in truth["components"] for k in (1, 3)]
        slack = 0.15 * max(max(xs) - min(xs), max(ys) - min(ys))
        report["component_count_ours"] = len(ours)
        report["component_count_truth"] = truth["component_count"]
        report["islands_truth"] = _truth_islands(truth)
        report.setdefault("height_error_px", {})[tag] = abs(
            table["total_payload_height_px"] - truth["total_payload_height_px"])
        report["%s_largest_bbox" % tag] = max(ours, key=lambda c: c["pixel_count"])["bbox_xyxy"]
        for truth_comp in truth["components"]:
            if truth_comp["area_fraction"] < 0.2:
                continue
            best = max(ours, key=lambda c: box_iou(truth_comp["bbox_xyxy"], c["bbox_xyxy"]))
            bx0, by0, bx1, by1 = truth_comp["bbox_xyxy"]
            cx, cy = best["centroid"]
            report["cores"].append({
                "tag": tag,
                "truth": truth_comp["name"],
                "truth_bbox": truth_comp["bbox_xyxy"],
                "ours_bbox": best["bbox_xyxy"],
                "iou": round(box_iou(truth_comp["bbox_xyxy"], best["bbox_xyxy"]), 4),
                "centre_inside": bool(bx0 - slack <= cx <= bx1 + slack
                                      and by0 - slack <= cy <= by1 + slack),
                "height_fraction_error": round(abs(truth_comp["height_fraction"]
                                                   - best["height_fraction"]), 4),
                "area_fraction_error": round(abs(truth_comp["area_fraction"]
                                                 - best["area_fraction"]), 4),
            })
    return report


def test_repo_anchor_fixture_is_valid_yaml():
    with open(ANCHOR_FIXTURE, encoding="utf-8") as handle:
        doc = yaml.safe_load(handle)
    assert doc["note"] == "示例，真值由总体提供"
