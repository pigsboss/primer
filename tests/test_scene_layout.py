# -*- coding: utf-8 -*-
"""``primer.scene.layout``：单调序列布局的落位、显示远近与放大倍数。

布局规则换了之后，这个模块在位置上几乎什么都不做——位置是规格写死的。它做的是三件事：
把 ``(x, y)`` 落成 ``(x, y, 0)``、算出"显示远近"（到恒星显示位置的距离，供单调性核对）、
以及把放大倍数折出来（用 ``nominal_units_per_au`` 这个**不参与位置换算**的基准尺）。
相机那一半（位置、朝向基、投影）也在这里，纯函数，发射器与核对共用同一套。
"""

from __future__ import annotations

import math

import pytest

import scene_fixtures as fixtures
from scene_fixtures import load_fixture_spec

from primer.scene.layout import (
    AU_KM,
    body_display_distance,
    camera_placement,
    frame_half_height,
    frame_half_width,
    label_extent,
    legend_placements,
    magnification,
    mission_placements,
    plan,
    project_extent,
    project_point,
    project_sphere,
    resolve_band,
    resolve_body,
    resolve_node,
    trajectory_polyline,
)
from primer.scene.spec import SceneError


@pytest.fixture
def spec(tmp_path):
    return load_fixture_spec(tmp_path)


def test_a_body_sits_where_the_spec_says_in_the_ecliptic_plane(spec):
    placement = resolve_body(spec.body("earth"), spec.layout)

    assert placement.position == (-6.0, -3.0, 0.0)
    assert placement.body_id == "earth"
    assert placement.kind == "planet"
    assert placement.display_radius == 1.3
    assert placement.real_orbit_au == 1.0
    assert placement.real_radius_km == 6371.0


def test_the_display_distance_is_measured_from_the_stars_displayed_position(spec):
    origin = (-24.0, 0.0, 0.0)
    placement = resolve_body(spec.body("earth"), spec.layout, origin)

    assert placement.display_orbit_distance == pytest.approx(math.hypot(18.0, 3.0))
    assert body_display_distance(spec.body("earth"), origin) == placement.display_orbit_distance
    # 显示距离与真实轨道半径是两套量：这里只是"看上去多远"，不成比例。
    assert placement.display_orbit_distance / placement.real_orbit_au != pytest.approx(
        resolve_body(spec.body("jupiter"), spec.layout, origin).display_orbit_distance / 5.203
    )


def test_magnification_is_measured_against_the_nominal_scale(spec):
    earth = spec.body("earth")
    expected = earth.display.radius / (earth.real.radius_km / AU_KM * spec.layout.nominal_units_per_au)

    assert magnification(earth, spec.layout) == pytest.approx(expected)
    assert magnification(earth, spec.layout) == pytest.approx(2543.8, abs=0.5)
    # 小天体真实半径小得多，放大倍数因此大得多——这正是要写进自定义属性给人看的量。
    assert magnification(spec.body("centaur"), spec.layout) > magnification(earth, spec.layout)


def test_nominal_units_per_au_does_not_move_anything(spec):
    """基准尺只影响放大倍数：把它改掉，位置与显示半径一个都不许动。"""
    from dataclasses import replace

    doubled = replace(spec.layout, nominal_units_per_au=spec.layout.nominal_units_per_au * 2.0)
    before = resolve_body(spec.body("jupiter"), spec.layout)
    after = resolve_body(spec.body("jupiter"), doubled)

    assert before.position == after.position
    assert before.display_radius == after.display_radius
    assert before.display_orbit_distance == after.display_orbit_distance
    assert after.magnification == pytest.approx(before.magnification / 2.0)


def test_the_band_is_a_box_in_the_ecliptic_plane(spec):
    placement = resolve_band(spec.bands[0], spec.layout)

    assert placement.center == (0.0, 0.0, 0.0)
    assert (placement.half_x, placement.half_y, placement.half_z) == (4.0, 3.0, 0.75)


def test_a_node_pinned_to_a_body_uses_the_very_same_coordinates(spec):
    bodies = {body.id: body for body in spec.bodies}
    placement = resolve_node(spec.trajectory.nodes[0], bodies, spec.layout)

    assert placement.body_id == "earth"
    assert placement.position == resolve_body(spec.body("earth"), spec.layout).position
    assert placement.position[2] == 0.0


def test_a_body_with_a_trajectory_node_sits_at_the_clearance_the_spec_declares(spec):
    """顺访天体的位置是**路径摆出来的**：到路径的最近距离 = clearance_radii × 显示半径。

    这一条把"路径是主、天体是从"钉住：坐标不是规格写死的 y，而是路径与那个倍数算出来的。
    """
    placements = plan(spec)["bodies_by_id"]
    plan_ = trajectory_polyline(spec)
    for node in spec.trajectory.nodes:
        if node.kind != "incidental":
            continue
        body = spec.body(node.body)
        centre = placements[body.id].position
        measured = min(
            math.sqrt(sum((point[axis] - centre[axis]) ** 2 for axis in range(3)))
            for point in plan_.points
        )
        assert measured == pytest.approx(
            node.clearance_radii * body.display.radius, abs=0.05
        )
        # 垂直方向挪过，左右次序不动。
        assert placements[body.id].position[0] == pytest.approx(body.display.x)


def test_the_path_begins_at_the_offset_the_departure_node_declares(spec):
    """出发点：路径的第一点离出发天体中心正好 ``offset_radii`` 个半径，且在背离太阳的一侧。

    这就是"线从地球身边出去、不悬空"的数值版本——不是"腿恰巧路过"，是起点本身钉在那里。
    """
    departure = next(node for node in spec.trajectory.nodes if node.kind == "departure")
    body = spec.body(departure.body)
    centre = plan(spec)["bodies_by_id"][body.id].position
    start = trajectory_polyline(spec).points[0]

    assert math.dist(centre[:2], start[:2]) == pytest.approx(
        departure.offset_radii * body.display.radius, abs=1e-6
    )
    sun = spec.body("sun").display
    assert math.hypot(start[0] - sun.x, start[1] - sun.y) > math.hypot(
        centre[0] - sun.x, centre[1] - sun.y
    )


def test_a_node_referring_to_an_unknown_body_is_rejected_by_the_layout(spec):
    bodies = {body.id: body for body in spec.bodies}
    missing = spec.trajectory.nodes[0].__class__(id="x", label="", kind="incidental", body="pluto")

    with pytest.raises(SceneError, match="refers to an unknown body: pluto"):
        resolve_node(missing, bodies, spec.layout)


def test_plan_returns_one_consistent_set_of_numbers(spec):
    resolved = plan(spec)

    assert set(resolved) == {"bodies", "bodies_by_id", "bands", "nodes", "origin"}
    assert set(resolved["bodies_by_id"]) == {
        "sun",
        "venus",
        "earth",
        "mainbelt",
        "jupiter",
        "centaur",
        "neptune",
    }
    assert resolved["origin"] == (-24.0, 0.0, 0.0)
    assert len(resolved["nodes"]) == len(spec.trajectory.nodes)
    for placement in resolved["bodies"]:
        assert resolved["bodies_by_id"][placement.body_id] is placement


# ---------------------------------------------------------------- 相机


def test_the_camera_sits_at_the_declared_azimuth_elevation_and_distance(spec):
    placement = camera_placement(spec.camera)
    azimuth = math.radians(spec.camera.azimuth_deg)
    elevation = math.radians(spec.camera.elevation_deg)

    assert placement.location == pytest.approx(
        (
            spec.camera.distance * math.cos(elevation) * math.cos(azimuth),
            spec.camera.distance * math.cos(elevation) * math.sin(azimuth),
            spec.camera.distance * math.sin(elevation),
        )
    )
    assert sum(a * b for a, b in zip(placement.forward, placement.right)) == pytest.approx(0.0)
    assert placement.up[2] > 0.0
    for vector in (placement.forward, placement.right, placement.up):
        assert math.sqrt(sum(component * component for component in vector)) == pytest.approx(1.0)


def test_azimuth_minus_ninety_puts_world_x_on_the_screens_horizontal(spec):
    """序列沿 x 排开，所以屏幕的水平轴必须就是世界 +x——否则一行天体叠成一串。"""
    placement = camera_placement(spec.camera)

    assert spec.camera.azimuth_deg == -90.0
    assert placement.right == pytest.approx((1.0, 0.0, 0.0), abs=1e-9)
    assert placement.location[0] == pytest.approx(0.0)


def test_the_target_projects_to_the_centre_of_the_frame(spec):
    assert project_point(spec.camera.target, spec.camera, 854, 480) == pytest.approx((0.0, 0.0))


def test_a_body_to_the_right_of_the_sun_lands_on_the_right_of_the_frame(spec):
    """单调序列的读法：x 大的天体必须落在画面右边。"""
    placements = plan(spec)["bodies_by_id"]
    sun = project_point(placements["sun"].position, spec.camera, 854, 480)
    centaur = project_point(placements["centaur"].position, spec.camera, 854, 480)

    assert sun[0] < centaur[0]
    assert all(
        project_point(placements[left].position, spec.camera, 854, 480)[0]
        < project_point(placements[right].position, spec.camera, 854, 480)[0]
        for left, right in (("sun", "venus"), ("venus", "earth"), ("earth", "jupiter"),
                            ("jupiter", "centaur"))
    )


def test_the_frame_half_width_is_where_the_frame_edge_actually_is(spec):
    half = frame_half_width(spec.camera, 854, 480)
    placement = camera_placement(spec.camera)
    edge = tuple(spec.camera.target[axis] + placement.right[axis] * half for axis in range(3))

    projected = project_point(edge, spec.camera, 854, 480)

    assert projected is not None
    assert projected[0] == pytest.approx(1.0)
    assert projected[1] == pytest.approx(0.0)
    assert frame_half_height(spec.camera, 854, 480) == pytest.approx(half * 480 / 854)


def test_a_point_behind_the_camera_has_no_projection(spec):
    placement = camera_placement(spec.camera)
    behind = tuple(placement.location[axis] - placement.forward[axis] * 10.0 for axis in range(3))

    assert project_point(behind, spec.camera, 854, 480) is None


def test_a_sphere_gets_a_silhouette_and_a_text_block_gets_two_extents(spec):
    """球用角半径（近侧会更大），文字块两轴分开算——两者的半宽都不该是零。"""
    sphere = project_sphere((0.0, 0.0, 0.0), 3.0, spec.camera, 854, 480)
    block = project_extent((0.0, 0.0, 0.0), 4.0, 0.6, spec.camera, 854, 480)

    assert sphere is not None and block is not None
    assert sphere.radius[0] > 0 and sphere.radius[1] > 0
    assert block.radius[0] > block.radius[1]
    # 同样的世界尺度，横向半视角大、竖直小，所以横向的归一化半宽更小。
    assert sphere.radius[1] > sphere.radius[0]


# ---------------------------------------------------------------- 任务层


@pytest.fixture
def mission_spec(tmp_path):
    return fixtures.load_mission_spec(tmp_path)


def _expected_off_sun(body, layout_placements, sun_id, side, distance):
    centre = layout_placements[body].position
    sun = layout_placements[sun_id].position
    ux, uy = centre[0] - sun[0], centre[1] - sun[1]
    length = math.hypot(ux, uy)
    sign = 1.0 if side == "anti_sun" else -1.0
    return (centre[0] + sign * ux / length * distance, centre[1] + sign * uy / length * distance)


def test_a_cluster_anchored_off_the_sun_line_lands_where_the_spec_says(mission_spec):
    """两个平动点写在"太阳—天体"连线上：``anti_sun`` 背太阳、``sunward`` 朝太阳。"""
    placements = plan(mission_spec)["bodies_by_id"]
    clusters = {item.cluster_id: item for item in mission_placements(mission_spec)}

    l2 = clusters["interferometer"]
    expected = _expected_off_sun("earth", placements, "sun", "anti_sun", 6.0)
    assert (l2.dots[0].position[0], l2.dots[0].position[1]) == pytest.approx(expected, abs=1e-6)

    l1 = clusters["venus_l1"]
    expected = _expected_off_sun("venus", placements, "sun", "sunward", 4.5)
    assert (l1.dots[0].position[0], l1.dots[0].position[1]) == pytest.approx(expected, abs=1e-6)


def test_a_body_anchored_cluster_is_the_body_position_plus_its_offset(mission_spec):
    placements = plan(mission_spec)["bodies_by_id"]
    clusters = {item.cluster_id: item for item in mission_placements(mission_spec)}
    venus = placements["venus"].position

    first = clusters["venus_first"]
    assert first.dots[0].position == pytest.approx(
        (venus[0] - 2.0 - 1.6, venus[1] - 3.6 + 0.0, 0.0), abs=1e-6
    )


def test_a_satellite_body_sits_at_its_anchor_offset_from_its_parent(mission_spec):
    """卫星的位置是"母体的位置 + 偏移"——母体动，它跟着动。"""
    placements = plan(mission_spec)["bodies_by_id"]
    neptune = placements["neptune"].position
    triton = placements["triton"].position

    assert triton[:2] == pytest.approx((neptune[0] - 6.0, neptune[1] - 3.0), abs=1e-6)
    # 它仍然是一条正经的落位：半径、放大倍数、自定义属性都在。
    assert placements["triton"].display_radius == mission_spec.body("triton").display.radius


def test_the_legend_runs_down_the_corner_with_its_text_to_the_right_of_the_dots(mission_spec):
    rows = legend_placements(mission_spec)

    assert [row.identifier for row in rows] == ["exo", "venus", "neptune", "traj"]
    assert [row.text for row in rows][-1] == "海王星飞行轨迹"
    for row in rows:
        # 文字是居中摆的，锚点必须退到色点之外，否则头一个字压在色点上。
        assert row.label_position[0] - label_extent(row.text, row.label_size) > row.dot_position[0] + row.dot_radius
    # 自上而下，行距就是规格给的那个数。
    ys = [row.dot_position[1] for row in rows]
    assert ys == pytest.approx([ys[0] - index * 2.6 for index in range(len(ys))])


def test_the_trajectory_gets_its_own_legend_row_in_the_trajectory_colour(mission_spec):
    from primer.scene.layout import LEGEND_TRAJECTORY_ID

    rows = {row.identifier: row for row in legend_placements(mission_spec)}

    assert LEGEND_TRAJECTORY_ID in rows
    assert rows[LEGEND_TRAJECTORY_ID].color == mission_spec.trajectory.style.color
