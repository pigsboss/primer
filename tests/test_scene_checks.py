# -*- coding: utf-8 -*-
"""``primer.scene.checks``：构建后的验收关卡。

关卡读的是**现场报告**（Blender 侧写下的那份），所以这里不需要真的开 Blender：把报告摆
成各种坏掉的样子，逐条确认它会被抓出来。航迹那三条尤其要紧——"不要插进行星里""顺访不用
绕弯""木星借力"都是**量出来的**：穿没穿进圆面、顺访处切线拐了几度、近木点与转角对不对得
上声明，全部从折线与实测记录里算，不看任何"意图"。

另外单独钉住 PNG 的读法：Blender 出的是 16 位 RGB PNG，而"全黑"是本项目最容易悄悄发生的
失败（灯没打上、相机朝向反了、贴图没下来都会全黑），所以这一层必须能自己把像素读出来。
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import pytest

import scene_fixtures as fixtures

from primer.scene import emit
from primer.scene.checks import (
    ERROR,
    WARNING,
    check_report_lines,
    check_scene,
    layout_report_lines,
    read_png,
    read_report,
)
from primer.scene.layout import INCIDENTAL_FAR_FACTOR, plan
from primer.scene.spec import load_spec


def _codes(findings, severity=None):
    return {
        finding.code
        for finding in findings
        if severity is None or finding.severity == severity
    }


def _coarse_codes(findings):
    return {finding.code for finding in findings}


def _snapshot(root: Path):
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


# ---------------------------------------------------------------- PNG


def test_read_png_reads_the_dimensions_and_brightness_of_eight_and_sixteen_bit_files(tmp_path):
    for depth in (8, 16):
        path = fixtures.write_png(tmp_path / f"{depth}.png", 40, 24, luminance=0.5, bit_depth=depth)
        stats = read_png(path)

        assert (stats.width, stats.height, stats.bit_depth) == (40, 24, depth)
        assert stats.mean_luminance == pytest.approx(0.5, abs=0.01)
        assert stats.max_luminance == pytest.approx(0.5, abs=0.01)
        assert stats.blank is False


def test_read_png_flags_a_black_frame(tmp_path):
    stats = read_png(fixtures.write_png(tmp_path / "black.png", 20, 10, luminance=0.0))

    assert stats.blank is True
    assert stats.max_luminance == 0.0


def test_read_png_refuses_something_that_is_not_a_png(tmp_path):
    path = tmp_path / "nope.png"
    path.write_bytes(b"not a png at all")

    with pytest.raises(ValueError, match="not a PNG file"):
        read_png(path)


def test_read_png_decodes_sub_and_up_filtered_rows_correctly(tmp_path):
    """回归：滤波按整像素（3 字节）还原，不是按单个采样。

    一度写成"往前一个采样"，于是 16 位 RGB 的行被解错，成图亮度凭空多出十几倍——纯色图
    配零号滤波器测不出来，只有逐像素变化配上非零滤波器才会暴露。真机上 Blender 输出的
    正是自适应滤波的 16 位 PNG，所以这条必须钉住。
    """
    width, height = 32, 8
    expected = sum(
        ((column * 7 + row * 13) % 200 + 20) for row in range(height) for column in range(width)
    ) / (width * height) / 255.0
    for filter_type in (0, 1, 2):
        path = fixtures.write_png(
            tmp_path / f"f{filter_type}.png",
            width,
            height,
            filter_type=filter_type,
            gradient=True,
        )

        stats = read_png(path)

        assert stats.mean_luminance == pytest.approx(expected, abs=0.01), filter_type
        assert stats.width == width and stats.height == height
        assert stats.blank is False


# ---------------------------------------------------------------- 关卡


def test_a_complete_staged_scene_passes_every_check(tmp_path):
    spec, _ = fixtures.staged_scene(tmp_path)

    findings = check_scene(spec, tmp_path)

    assert findings == []
    lines = check_report_lines(spec, findings, tmp_path)
    assert lines[0].startswith("场景验收：")
    assert "结论：全部通过" in lines[-1]


def test_check_scene_reads_only(tmp_path):
    spec, _ = fixtures.staged_scene(tmp_path)
    before = _snapshot(tmp_path)

    check_scene(spec, tmp_path)

    assert _snapshot(tmp_path) == before


# ---------------------------------------------------------------- 取景余量


def test_the_staged_scene_sits_inside_the_frame_margin(tmp_path):
    """干净场景里没有任何东西压到边缘：这一条同时守住"余量判据不会误报"。"""
    spec, report = fixtures.staged_scene(tmp_path)

    findings = [finding for finding in check_scene(spec, tmp_path) if "out_of_frame" in finding.code]

    assert findings == []
    # 现场报告里确实带了几何取样点——否则这条测试只是在测"没有数据"。
    assert any(entry["probes"] for entry in report["objects"])


def test_the_background_starfield_is_never_reported_as_out_of_frame(tmp_path):
    """天球包住整个场景，它"出画"是必然的；取景核对必须把它排除，否则天天误报。"""
    spec, report = fixtures.staged_scene(tmp_path)
    starfield = next(entry for entry in report["objects"] if entry["name"] == emit.STARFIELD_OBJECT)

    assert starfield["probes"], "the fixture should model a starfield far larger than the frame"
    assert check_scene(spec, tmp_path) == []


def test_geometry_that_reaches_the_frame_edge_is_reported(tmp_path):
    """圆心还在画面里、几何却已经压线——只有看几何本身才看得见的一类。"""
    spec, _ = fixtures.staged_scene(tmp_path, probe_scale=6.0)

    findings = check_scene(spec, tmp_path)

    codes = _codes(findings)
    assert codes, "scaling the geometry up by 6x has to show up somewhere"
    assert codes <= {
        "body_out_of_frame",
        "band_out_of_frame",
        "label_out_of_frame",
        "node_out_of_frame",
    }
    assert all(finding.severity == WARNING for finding in findings)
    assert any("0.90" in finding.message for finding in findings)


def test_the_band_and_the_curve_are_checked_too(tmp_path):
    """星带与航迹管不是"点"：把星带放大到画外，报的必须是星带。"""
    spec, report = fixtures.staged_scene(tmp_path)
    for entry in report["objects"]:
        if entry["name"].startswith(emit.BAND_PREFIX):
            entry["probes"] = [
                [point[0] * 40.0, point[1] * 40.0, point[2] * 40.0] for point in entry["probes"]
            ]
    fixtures.stage_report(tmp_path, report)

    findings = check_scene(spec, tmp_path)

    assert _codes(findings, WARNING) == {"band_out_of_frame"}


def test_a_label_that_reaches_the_edge_is_reported_under_its_own_code(tmp_path):
    spec, report = fixtures.staged_scene(tmp_path)
    target = "LABEL_neptune_arrival"
    entry = next(item for item in report["objects"] if item["name"] == target)
    anchor = next(
        node["anchor"] for node in report["trajectory"]["nodes"] if node["id"] == "neptune_arrival"
    )
    # 以标签自己的锚点为心放大 12 倍：文字块伸到画外，锚点本身仍留在画面里。
    entry["probes"] = [
        [anchor[axis] + (corner[axis] - anchor[axis]) * 12.0 for axis in range(3)]
        for corner in entry["probes"]
    ]
    fixtures.stage_report(tmp_path, report)

    findings = [finding for finding in check_scene(spec, tmp_path) if "out_of_frame" in finding.code]

    assert _codes(findings) == {"label_out_of_frame"}
    assert target in findings[0].message


def test_the_framing_forecast_needs_no_render(tmp_path):
    """``report`` 的取景预算只看规格：还没 build 也能说出"装不装得下"。"""
    spec = fixtures.load_fixture_spec(tmp_path)

    text = "\n".join(layout_report_lines(spec))

    assert "取景（按规格预算" in text
    assert "余量上限 0.90" in text
    assert "最外一环" in text and "在余量内" in text
    assert "LABEL_neptune_arrival" in text


def test_a_camera_too_close_is_visible_in_the_forecast_and_in_the_check(tmp_path):
    """把夹具相机拉近，预算与验收都要变脸——两边用的是同一套投影。"""
    spec, _ = fixtures.staged_scene(
        tmp_path, spec_text=fixtures.SPEC_YAML.replace("distance: 100.0", "distance: 30.0")
    )

    assert "**已超出余量**" in "\n".join(layout_report_lines(spec))
    codes = {
        code for code in _codes(check_scene(spec, tmp_path), WARNING) if code.endswith("out_of_frame")
    }
    assert codes, "a 30-unit camera cannot possibly frame a 27-unit orbit"


def test_a_missing_report_is_reported_with_the_path_to_build(tmp_path):
    spec = fixtures.load_fixture_spec(tmp_path)

    findings = check_scene(spec, tmp_path)

    assert _codes(findings) == {"report_missing"}
    assert findings[0].fatal is True
    assert "build" in findings[0].message


def test_a_missing_object_is_reported_by_name(tmp_path):
    spec, _ = fixtures.staged_scene(tmp_path, missing_objects=("PLANET_jupiter",))

    findings = check_scene(spec, tmp_path)

    # 少了行星对象，钉在它上面的航迹节点自然也找不着——两处会一起报，这是对的。
    assert "object_missing" in _coarse_codes(findings)
    assert any("PLANET_jupiter" in finding.message for finding in findings)


def test_an_object_outside_the_contract_is_reported(tmp_path):
    spec, _ = fixtures.staged_scene(
        tmp_path,
        extra_objects=(
            {
                "name": "PLANET_pluto",
                "type": "MESH",
                "collection": "PLANETS",
                "location": [0, 0, 0],
                "custom": {},
            },
        ),
    )

    findings = check_scene(spec, tmp_path)

    assert _coarse_codes(findings) == {"object_unexpected"}
    assert "PLANET_pluto" in findings[0].message


def test_the_camera_is_allowed_outside_the_collections_but_others_are_not(tmp_path):
    """契约只给六个 collection，相机因此单列；别的对象没有这个待遇。"""
    spec, report = fixtures.staged_scene(tmp_path)

    assert any(
        entry["name"] == emit.CAMERA_OBJECT and entry["collection"] == ""
        for entry in report["objects"]
    )
    assert check_scene(spec, tmp_path) == []


def test_an_object_in_the_wrong_collection_is_reported(tmp_path):
    spec, report = fixtures.staged_scene(tmp_path)
    for entry in report["objects"]:
        if entry["name"] == "PLANET_jupiter":
            entry["collection"] = "SUN"
    fixtures.stage_report(tmp_path, report)

    findings = check_scene(spec, tmp_path)

    assert _coarse_codes(findings) == {"object_collection"}
    assert "PLANET_jupiter" in findings[0].message


def test_a_missing_or_extra_collection_is_reported(tmp_path):
    spec, report = fixtures.staged_scene(tmp_path)
    report["collections"] = sorted(set(report["collections"]) - {"LABELS"} | {"EXTRAS"})
    fixtures.stage_report(tmp_path, report)

    codes = _codes(check_scene(spec, tmp_path))

    assert codes == {"collection_missing", "collection_unexpected"}


def test_a_missing_or_extra_material_is_reported(tmp_path):
    spec, report = fixtures.staged_scene(tmp_path)
    report["materials"] = sorted(set(report["materials"]) - {"MAT_belt"} | {"MAT_extra"})
    fixtures.stage_report(tmp_path, report)

    assert _codes(check_scene(spec, tmp_path)) == {"material_missing", "material_unexpected"}


def test_the_body_custom_properties_must_match_the_layout_module(tmp_path):
    """自定义属性是"发射器真的用了 layout"的判据，不是装饰。"""
    spec, _ = fixtures.staged_scene(
        tmp_path, body_property_drift=("earth", "magnification", 1.0)
    )

    findings = check_scene(spec, tmp_path)

    assert _coarse_codes(findings) == {"body_property"}
    assert "magnification" in findings[0].message


def test_a_missing_custom_property_is_reported(tmp_path):
    spec, report = fixtures.staged_scene(tmp_path)
    for entry in report["objects"]:
        if entry["name"] == "PLANET_earth":
            del entry["custom"]["display_orbit_distance"]
    fixtures.stage_report(tmp_path, report)

    findings = check_scene(spec, tmp_path)

    assert _coarse_codes(findings) == {"body_property"}
    assert "display_orbit_distance" in findings[0].message


def test_a_trajectory_that_cuts_through_a_planet_is_reported(tmp_path):
    """人的那句"不要插进行星里，从附近走"的下界：沿线任何一点落进圆面就要报。"""
    spec, report = fixtures.staged_scene(tmp_path)
    centre = next(
        entry["location"] for entry in report["objects"] if entry["name"] == "PLANET_jupiter"
    )
    report["trajectory"]["points"][12] = list(centre)
    fixtures.stage_report(tmp_path, report)

    findings = check_scene(spec, tmp_path)

    assert _coarse_codes(findings) == {"trajectory_penetrates_body"}
    assert "jupiter" in findings[0].message


def test_an_incidental_flyby_that_passes_too_far_is_reported(tmp_path):
    """同一句话的上界：路径离顺访天体超过 ``clearance_radii`` 的若干倍就不叫"附近"了。

    造法是把整条折线上移——顺访天体留在原处，于是它们被甩在下面。这是该判据真正要抓的
    情形：**现场的折线**离天体太远（摆位算得再对，画出来的不是那条线也没用）。
    """
    spec, report = fixtures.staged_scene(tmp_path)
    for point in report["trajectory"]["points"]:
        point[1] += 4.0
    fixtures.stage_report(tmp_path, report)

    findings = check_scene(spec, tmp_path)
    too_far = [finding for finding in findings if finding.code == "incidental_flyby_too_far"]

    assert len(too_far) == 2  # 入腿、出腿各一个顺访天体
    assert "mainbelt" in " ".join(finding.message for finding in too_far)
    assert "半径" in too_far[0].message


def test_the_fixtures_incidental_clearances_sit_inside_the_band(tmp_path):
    """"从附近走"是一条带：下界是圆面、上界是 clearance_radii 的若干倍，夹具落在带里。"""
    spec, report = fixtures.staged_scene(tmp_path)

    assert check_scene(spec, tmp_path) == []
    points = report["trajectory"]["points"]
    for node in report["trajectory"]["nodes"]:
        if node["kind"] != "incidental":
            continue
        declared = next(item for item in spec.trajectory.nodes if item.id == node["id"])
        body = spec.body(declared.body)
        measured = min(math.dist(point, node["anchor"]) for point in points)

        assert body.display.radius < measured
        assert measured <= declared.clearance_radii * body.display.radius * INCIDENTAL_FAR_FACTOR


def test_a_bend_at_an_incidental_flyby_is_reported(tmp_path):
    """顺访是沿途顺访，不该机动：路径在那儿拐了弯就要报。

    造法就是"拐一下"本身——把折线的**前半段**绕顺访那一点转过一个角，后半段原样不动，
    于是那一点两侧的切线差出了一个角，其余形状（含入轨闭环）一点没变。
    """
    spec, report = fixtures.staged_scene(tmp_path)
    trajectory = report["trajectory"]
    node = next(entry for entry in trajectory["nodes"] if entry["kind"] == "incidental")
    index = node["point_index"]
    points = trajectory["points"]
    pivot = points[index]
    angle = math.radians(25.0)
    cosine, sine = math.cos(angle), math.sin(angle)
    for position in range(index + 1):
        dx, dy = points[position][0] - pivot[0], points[position][1] - pivot[1]
        points[position][0] = pivot[0] + dx * cosine - dy * sine
        points[position][1] = pivot[1] + dx * sine + dy * cosine
    fixtures.stage_report(tmp_path, report)

    findings = check_scene(spec, tmp_path)

    assert "incidental_flyby_bends" in _coarse_codes(findings)
    assert any("拐了" in finding.message for finding in findings)


def test_an_assist_that_passes_too_close_to_the_planet_is_reported(tmp_path):
    spec, report = fixtures.staged_scene(tmp_path)
    report["trajectory"]["assist"]["periapsis"] = 2.5
    fixtures.stage_report(tmp_path, report)

    findings = check_scene(spec, tmp_path)

    assert _coarse_codes(findings) == {"assist_periapsis_too_tight"}
    assert "半径" in findings[0].message


def test_an_assist_turn_that_does_not_match_the_declaration_is_reported(tmp_path):
    """转角是这张图的信息量：画出来的那个角必须就是规格声明的数。"""
    spec, report = fixtures.staged_scene(tmp_path)
    report["trajectory"]["assist"]["turn_deg"] = 12.0
    fixtures.stage_report(tmp_path, report)

    findings = check_scene(spec, tmp_path)

    assert _coarse_codes(findings) == {"assist_turn_mismatch"}
    assert "45" in findings[0].message


def test_the_two_legs_the_fixture_draws_differ_by_the_declared_turn(tmp_path):
    """路径是主：两条腿的走向由出发点与木星那一下定，差必须就是声明的转角。

    顺带钉住"天体在弯的内圈"——这一条是"木星借力看得见"的几何前提：右转的弧要真的绕着
    木星转，天体就必须落在入射腿的右侧。
    """
    spec, report = fixtures.staged_scene(tmp_path)
    trajectory = report["trajectory"]
    start = trajectory["points"][0]
    centre = next(
        entry["anchor"] for entry in trajectory["nodes"] if entry["kind"] == "gravity_assist"
    )

    def bearing(origin, target):
        return math.degrees(math.atan2(target[1] - origin[1], target[0] - origin[0]))

    incoming = bearing(start, trajectory["assist"]["entry"])
    outgoing = bearing(trajectory["assist"]["exit"], trajectory["capture"]["join"])
    declared = next(
        node.turn_deg for node in spec.trajectory.nodes if node.kind == "gravity_assist"
    )
    turned = (outgoing - incoming + 180.0) % 360.0 - 180.0

    assert abs(turned) == pytest.approx(declared, abs=2.0)
    # 负号 = 顺时针右转；天体在入射腿的右侧 = 弯的内圈。
    assert turned < 0.0
    axis = (math.cos(math.radians(incoming)), math.sin(math.radians(incoming)))
    offset = (centre[0] - start[0], centre[1] - start[1])
    assert axis[0] * offset[1] - axis[1] * offset[0] < 0.0


def test_a_preview_that_is_not_where_the_report_says_is_reported(tmp_path):
    spec, report = fixtures.staged_scene(tmp_path)
    report["render"]["path"] = str(tmp_path / "_primer" / "scene" / "renders" / "gone.png")
    fixtures.stage_report(tmp_path, report)

    assert _coarse_codes(check_scene(spec, tmp_path)) == {"preview_missing"}


def test_a_black_preview_is_a_failure_not_a_look(tmp_path):
    spec, _ = fixtures.staged_scene(tmp_path, luminance=0.0)

    findings = check_scene(spec, tmp_path)

    assert _coarse_codes(findings) == {"preview_blank"}
    assert findings[0].severity == ERROR


def test_a_preview_at_the_wrong_resolution_is_reported(tmp_path):
    spec, report = fixtures.staged_scene(tmp_path)
    wrong = tmp_path / "_primer" / "scene" / "renders" / "previews" / "wrong.png"
    fixtures.write_png(wrong, 640, 480, luminance=0.5)
    report["render"]["path"] = str(wrong)
    fixtures.stage_report(tmp_path, report)

    findings = check_scene(spec, tmp_path)

    assert _coarse_codes(findings) == {"preview_dimensions"}
    assert "640x480" in findings[0].message


def test_a_render_section_that_contradicts_the_spec_is_reported(tmp_path):
    spec, report = fixtures.staged_scene(tmp_path)
    report["render"]["width"] = 1920
    fixtures.stage_report(tmp_path, report)

    codes = _coarse_codes(check_scene(spec, tmp_path))

    assert "render_dimensions" in codes


def test_an_incomplete_credits_ledger_blocks_the_delivery(tmp_path):
    spec, _ = fixtures.staged_scene(tmp_path)
    fixtures.texture_path(tmp_path, "4k_jupiter.jpg").unlink()

    findings = check_scene(spec, tmp_path)

    assert "credits_incomplete" in _coarse_codes(findings)
    assert any(
        finding.code == "credits_incomplete" and finding.severity == ERROR for finding in findings
    )


def test_a_texture_taken_at_another_resolution_is_only_a_warning(tmp_path):
    spec, _ = fixtures.staged_scene(tmp_path)
    ledger = fixtures.ledger_path(tmp_path)
    import yaml

    document = yaml.safe_load(ledger.read_text(encoding="utf-8"))
    for entry in document["files"]:
        if entry["texture"] == "4k_sun.jpg":
            entry["source_file"] = "2k_sun.jpg"
            entry["url"] = "https://example.invalid/textures/download/2k_sun.jpg"
    ledger.write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8")

    findings = check_scene(spec, tmp_path)

    assert _coarse_codes(findings) == {"asset_resolution_fallback"}
    assert findings[0].severity == WARNING
    assert "2k_sun.jpg" in findings[0].message


def test_a_spec_that_changed_since_the_build_makes_the_artifacts_stale(tmp_path):
    spec, _ = fixtures.staged_scene(tmp_path)
    fixtures.write_spec(tmp_path, fixtures.SPEC_YAML.replace("夹具场景", "改过的标题"))
    reloaded = load_spec(spec.path)

    findings = check_scene(reloaded, tmp_path)

    assert any(
        finding.code == "fingerprint_stale" and finding.severity == ERROR for finding in findings
    )
    assert any("规格内容已变" in finding.message for finding in findings)


def test_a_recorded_fingerprint_that_is_absent_is_reported(tmp_path):
    spec, report = fixtures.staged_scene(tmp_path)
    report["fingerprint"] = {}
    fixtures.stage_report(tmp_path, report)

    assert _coarse_codes(check_scene(spec, tmp_path)) == {"fingerprint_missing"}


def test_a_missing_source_file_is_reported(tmp_path):
    spec, _ = fixtures.staged_scene(tmp_path)
    (tmp_path / "_primer" / "scene" / "fixture_scene.blend").unlink()

    assert _coarse_codes(check_scene(spec, tmp_path)) == {"blend_missing"}


def test_the_report_lines_carry_the_codes_and_the_verdict(tmp_path):
    spec, _ = fixtures.staged_scene(tmp_path, luminance=0.0)

    lines = check_report_lines(spec, check_scene(spec, tmp_path), tmp_path)

    assert any("preview_blank" in line for line in lines)
    assert lines[-1].startswith("  结论：1 项 error")


def test_read_report_returns_none_when_the_build_never_ran(tmp_path):
    assert read_report(tmp_path) is None


def test_the_layout_report_prints_the_numbers_the_pipeline_used(tmp_path):
    spec = fixtures.load_fixture_spec(tmp_path)

    lines = layout_report_lines(spec)
    text = "\n".join(lines)

    assert "布局规则：monotonic_sequence" in text
    assert "earth" in text and "mainbelt" in text and "centaur" in text
    assert "航迹节点" in text
    assert "earth_departure" in text and "departure" in text
    assert "gravity_assist" in text and "orbit_insertion" in text
    assert "854x480" in text and "3840x2160" in text


def test_the_report_is_written_by_blender_not_by_the_checker(tmp_path):
    """关卡只读：报告由 emitter 生成的脚本写，关卡读不到就报 report_missing。"""
    spec, report = fixtures.staged_scene(tmp_path)
    fixtures.stage_report(tmp_path, report)

    assert read_report(tmp_path) == json.loads(
        (tmp_path / "_primer" / "scene" / "build" / "scene_report.json").read_text(encoding="utf-8")
    )


def test_an_orphaned_texture_is_reported_as_a_warning(tmp_path):
    """贴图目录里躺着规格不认的文件：不是错误，但目录就不等于规格了。"""
    from primer.scene.assets import texture_dir

    spec, _ = fixtures.staged_scene(tmp_path)
    stale = texture_dir(tmp_path) / "2k_sun.jpg"
    stale.write_bytes(b"the previous generation\n")

    findings = check_scene(spec, tmp_path)

    assert _coarse_codes(findings) == {"orphan_texture"}
    assert findings[0].severity == WARNING
    assert "2k_sun.jpg" in findings[0].message and "fetch" in findings[0].message


def test_geometry_entirely_behind_the_camera_is_reported(tmp_path):
    """投不出来的东西不等于合格：投影返回 ``None`` 时**不能**当成"没越界"放过去。"""
    from primer.scene.layout import camera_placement

    spec, report = fixtures.staged_scene(tmp_path)
    # 把主带搬到相机身后：沿相机→目标的射线再往前一个相机距离。
    location = camera_placement(spec.camera).location
    beyond = [value * 2.0 for value in location]
    for entry in report["objects"]:
        if entry["name"].startswith(emit.BAND_PREFIX):
            entry["probes"] = [
                [beyond[axis] + (1.0 if sign < 4 else -1.0) for axis in range(3)]
                for sign in range(8)
            ]
    fixtures.stage_report(tmp_path, report)

    findings = check_scene(spec, tmp_path)

    assert _codes(findings) == {"band_out_of_frame"}
    assert "相机背后" in findings[0].message


def test_a_background_that_falls_back_to_a_void_is_reported(tmp_path):
    """深空亮度是量得出来的：成图四周边带的中位亮度低于下限就提醒。

    这一条挡的是"背景又被改回纯黑"——它不是渲染失败（成图仍有内容），所以只算警告，
    但必须有人看见。
    """
    spec, _ = fixtures.staged_scene(tmp_path, luminance=0.025)

    findings = check_scene(spec, tmp_path)

    assert _coarse_codes(findings) == {"background_too_dark"}
    assert findings[0].severity == WARNING
    assert "深空" in findings[0].message


def test_the_border_median_is_reported_separately_from_the_whole_frame(tmp_path):
    stats = read_png(
        fixtures.write_png(tmp_path / "uniform.png", 100, 60, luminance=0.42)
    )

    assert stats.border_median == pytest.approx(0.42, abs=0.01)
    assert stats.mean_luminance == pytest.approx(0.42, abs=0.01)


# ---------------------------------------------------------------- 单调性（新规则）

def test_the_staged_scene_is_monotonic_in_both_senses(tmp_path):
    """干净场景必须两条都过：显示的远近次序、显示的大小次序，都与真实一致。"""
    spec, _ = fixtures.staged_scene(tmp_path)

    findings = check_scene(spec, tmp_path)

    assert findings == []
    assert "order_not_monotonic" not in _coarse_codes(findings)
    assert "size_not_monotonic" not in _coarse_codes(findings)


def test_a_display_order_that_contradicts_the_real_orbit_is_rejected(tmp_path):
    """把金星摆到比地球更远的地方：显示的远近次序与真实轨道次序对不上了。

    这是本图唯一的语义承诺，所以是 error，并且要把**两个次序**都报出来——只报一句
    "不单调" 人还得自己去排。
    """
    spec, _ = fixtures.staged_scene(tmp_path)
    swapped = fixtures.swapped_placement(
        spec, tmp_path, first="venus", second="earth"
    )

    findings = check_scene(swapped, tmp_path)
    orders = [finding for finding in findings if finding.code == "order_not_monotonic"]

    assert len(orders) == 1
    assert orders[0].severity == ERROR
    message = orders[0].message
    # 真实次序：金星 < 地球；显示次序：地球 < 金星
    assert "venus < earth" in message
    assert "earth < venus" in message


def test_a_display_size_that_contradicts_the_real_radius_is_rejected(tmp_path):
    """把主带小行星画得比木星还大：大小的次序反了。"""
    spec, _ = fixtures.staged_scene(tmp_path)
    inflated = fixtures.resized_body(spec, tmp_path, body_id="mainbelt", radius=9.0)

    findings = check_scene(inflated, tmp_path)
    sizes = [finding for finding in findings if finding.code == "size_not_monotonic"]

    assert len(sizes) == 1
    assert sizes[0].severity == ERROR
    assert "mainbelt" in sizes[0].message and "jupiter" in sizes[0].message


def test_the_monotonic_checks_are_only_about_order_not_proportion(tmp_path):
    """次序对但完全不成比例，必须放行：这张图本来就不要求成比例。"""
    spec, _ = fixtures.staged_scene(tmp_path)
    squashed = fixtures.moved_body(spec, tmp_path, body_id="venus", x=-23.0, y=2.0)

    findings = [
        finding
        for finding in check_scene(squashed, tmp_path)
        if finding.code in {"order_not_monotonic", "size_not_monotonic"}
    ]

    # 金星真实到 0.723 AU，这里几乎贴着太阳画（显示距离 2.2，地球是 18.2）——次序没坏，就该过。
    assert findings == []


# ---------------------------------------------------------------- 任务层


def test_a_staged_scene_with_the_mission_layer_passes_every_check(tmp_path):
    spec, _ = fixtures.staged_mission_scene(tmp_path)

    findings = check_scene(spec, tmp_path)

    assert findings == []


def test_a_dot_that_falls_inside_a_planet_is_reported(tmp_path):
    """占位点是航天器：可以贴着行星飞，但落进圆面里就要报。"""
    spec, report = fixtures.staged_mission_scene(tmp_path)
    placements = plan(spec)["bodies_by_id"]
    venus = placements["venus"].position
    report["missions"]["clusters"][0]["dots"][0]["position"] = list(venus)
    fixtures.stage_report(tmp_path, report)

    findings = check_scene(spec, tmp_path)

    assert _coarse_codes(findings) == {"spacecraft_overlaps_body"}
    assert "venus" in findings[0].message


def test_a_mission_dot_that_reaches_the_frame_edge_is_reported(tmp_path):
    """角落里的图例与簇里的点同样不许压到画边缘。"""
    spec, report = fixtures.staged_mission_scene(tmp_path)
    name = "DOT_survey_mother"
    entry = next(item for item in report["objects"] if item["name"] == name)
    entry["probes"] = [[value * 40.0 for value in probe] for probe in entry["probes"]]
    fixtures.stage_report(tmp_path, report)

    findings = check_scene(spec, tmp_path)
    offenders = [item for item in findings if item.code == "mission_dot_out_of_frame"]

    assert offenders, _coarse_codes(findings)
    assert name in offenders[0].message


def test_a_mission_line_that_was_never_declared_is_reported(tmp_path):
    """图例只有三种颜色：报告里冒出第四种（或者某条线的名字打错），必须挡住。"""
    spec, report = fixtures.staged_mission_scene(tmp_path)
    report["missions"]["clusters"][0]["line"] = "interstellar"
    fixtures.stage_report(tmp_path, report)

    findings = check_scene(spec, tmp_path)

    assert _coarse_codes(findings) == {"mission_line_unknown"}
    assert "interstellar" in findings[0].message


def test_a_declared_line_with_no_mission_is_reported(tmp_path):
    """声明了三条线却一条都没有：图例会多出一行无主的颜色。"""
    spec, report = fixtures.staged_mission_scene(tmp_path)
    report["missions"]["clusters"] = [
        cluster
        for cluster in report["missions"]["clusters"]
        if cluster["line"] != "neptune"
    ]
    fixtures.stage_report(tmp_path, report)

    findings = check_scene(spec, tmp_path)

    assert _coarse_codes(findings) == {"mission_line_unknown"}
    assert "neptune" in findings[0].message
