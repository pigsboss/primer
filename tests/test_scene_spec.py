# -*- coding: utf-8 -*-
"""``primer.scene.spec``：规格载入与校验。

夹具是一份小而全的规格（太阳 + 地球（大气 + 云）+ 火星，一条绕木星的航迹那样的小场景）。
每一条拒绝路径都单独测一遍——**未知键、必填键、引用完整性、数值关系**四层各有代表：规格
错一个字段名就被静默忽略，是这类工具最难查的一种坏法，所以载入时必须拒。
"""

from __future__ import annotations

import pytest
import yaml

import scene_fixtures as fixtures
from scene_fixtures import SPEC_YAML, parse_document, spec_document, write_spec

from primer.scene.spec import DEFAULT_ATMOSPHERE_MODE, SceneError, load_spec


def test_a_complete_spec_loads_with_every_field_consumed(tmp_path):
    document = spec_document()
    spec = parse_document(document, tmp_path)

    assert spec.meta.name == "fixture_scene"
    assert spec.meta.title == "夹具场景"
    assert (spec.render.preview.width, spec.render.preview.height) == (854, 480)
    assert spec.render.final.samples == 512
    assert spec.render.final.exr is True
    assert spec.render.aspect_ratio == (16, 9)
    assert spec.render.color_management.view_transform == "AgX"
    assert spec.render.output.preview_dir == "renders/previews"
    assert spec.layout.kind == "monotonic_sequence"
    assert spec.layout.nominal_units_per_au == 12.0
    assert [body.id for body in spec.bodies] == [
        "sun",
        "venus",
        "earth",
        "mainbelt",
        "jupiter",
        "centaur",
        "neptune",
    ]
    assert spec.body("earth").clouds is not None
    assert spec.body("earth").atmosphere.mode == DEFAULT_ATMOSPHERE_MODE
    assert spec.body("venus").atmosphere.mode == "rim"
    assert spec.bands[0].display.x_center == 0.0
    assert spec.bands[0].display.x_half_length == 4.0
    assert spec.bands[0].display.thickness == 1.5
    assert [node.id for node in spec.trajectory.nodes] == [
        "earth_departure",
        "mainbelt_flyby",
        "jupiter_assist",
        "centaur_flyby",
        "neptune_arrival",
    ]
    assert spec.trajectory.destination.id == "neptune_arrival"
    # 顺访天体自己写的是"离路径几个半径"，不是坐标——坐标由路径摆出来。
    assert spec.trajectory.nodes[1].clearance_radii == 2.5
    assert spec.trajectory.nodes[1].body == "mainbelt"
    # 位置全在 display 里写死
    assert spec.body("earth").display.x == -6.0
    assert spec.body("earth").display.y == -3.0
    # 小天体没有贴图：这是合法状态，不是漏写
    assert spec.body("centaur").kind == "minor"
    assert spec.body("centaur").texture is None
    assert spec.body("centaur").material.type == "rock"
    assert spec.camera.focal_length_mm == 40.0
    assert spec.textures == (
        "4k_sun.jpg",
        "4k_venus_atmosphere.jpg",
        "4k_earth_daymap.jpg",
        "4k_earth_clouds.jpg",
        "4k_jupiter.jpg",
        "2k_neptune.jpg",
        "8k_stars_milky_way.jpg",
    )
    assert spec.background.starfield == "8k_stars_milky_way.jpg"
    assert spec.asset("8k_stars_milky_way.jpg").credit_extra == "Milky Way panorama, CC BY 4.0"


def test_load_spec_names_the_file_in_every_error_it_raises(tmp_path):
    """错误信息要同时点名**哪个文件**、**哪条路径**——对着几百行 YAML 找错，缺一个都不行。"""
    path = write_spec(
        tmp_path, SPEC_YAML.replace("  name: fixture_scene", "  name: fixture_scene\n  tpying: yes")
    )

    with pytest.raises(SceneError) as excinfo:
        load_spec(path)

    message = str(excinfo.value)
    assert message.startswith(str(path))
    assert "meta has unknown key(s): tpying" in message


def test_a_malformed_value_that_yaml_reads_as_a_valueless_key_is_rejected(tmp_path, capsys):
    """少写一对引号的流式标量会被读成"无值的键"——那正是缺引号，应当**报错**而不是补回。

    这条曾经被读入端悄悄修好（把无值键并回上一个字符串）。规格改对之后那刀删掉了：
    容错会掩盖规格错误，而规格错误本该是写规格的人看见的。
    """
    document = spec_document()
    files = document["assets"]["files"]
    index = next(i for i, entry in enumerate(files) if entry["texture"] == "8k_stars_milky_way.jpg")
    files[index] = {
        "texture": "8k_stars_milky_way.jpg",
        "used_by": "background.starfield",
        "credit_extra": "Milky Way panorama",
        "CC BY 4.0": None,
    }

    with pytest.raises(SceneError, match=r"assets.files\[6\] has unknown key\(s\): CC BY 4.0"):
        parse_document(document, tmp_path)
    assert capsys.readouterr().err == ""


def test_a_value_of_the_wrong_type_is_rejected_by_name(tmp_path):
    document = spec_document()
    document["assets"]["files"][0]["credit_extra"] = [1, 2]

    with pytest.raises(SceneError, match=r"assets.files\[0\].credit_extra must be a non-empty string"):
        parse_document(document, tmp_path)

    document = spec_document()
    document["bands"][0]["count"] = "900"
    with pytest.raises(SceneError, match=r"bands\[0\].count must be an integer"):
        parse_document(document, tmp_path)


def test_atmosphere_mode_defaults_to_rim_and_can_be_switched_to_volume(tmp_path):
    document = spec_document()
    document["bodies"][2]["atmosphere"]["mode"] = "volume"
    spec = parse_document(document, tmp_path)

    assert spec.body("earth").atmosphere.mode == "volume"

    document["bodies"][1]["atmosphere"]["mode"] = "glow"
    with pytest.raises(SceneError, match="atmosphere.mode must be one of: rim, volume"):
        parse_document(document, tmp_path)


def test_an_unknown_top_level_key_is_rejected_by_name(tmp_path):
    document = spec_document()
    document["renders"] = {}

    with pytest.raises(SceneError, match="spec has unknown key\\(s\\): renders"):
        parse_document(document, tmp_path)


def test_an_unknown_key_inside_a_body_is_rejected(tmp_path):
    document = spec_document()
    document["bodies"][1]["colour"] = [1, 0, 0]

    with pytest.raises(SceneError, match=r"bodies\[1\] has unknown key\(s\): colour"):
        parse_document(document, tmp_path)


def test_a_missing_required_key_names_the_path(tmp_path):
    document = spec_document()
    del document["meta"]["name"]

    with pytest.raises(SceneError, match="meta.name is required"):
        parse_document(document, tmp_path)


def test_a_texture_that_is_not_declared_in_the_asset_table_is_rejected(tmp_path):
    document = spec_document()
    document["bodies"][3]["texture"] = "mars_4k.jpg"

    with pytest.raises(
        SceneError, match=r"bodies\[3\].texture is not declared in assets.files: mars_4k.jpg"
    ):
        parse_document(document, tmp_path)


def test_a_cloud_texture_that_is_not_declared_is_rejected(tmp_path):
    document = spec_document()
    document["bodies"][2]["clouds"]["texture"] = "clouds.jpg"

    with pytest.raises(
        SceneError, match=r"bodies\[2\].clouds.texture is not declared in assets.files: clouds.jpg"
    ):
        parse_document(document, tmp_path)


def test_a_starfield_that_is_not_declared_is_rejected(tmp_path):
    document = spec_document()
    document["background"]["starfield"] = "sky.jpg"

    with pytest.raises(
        SceneError, match="background.starfield is not declared in assets.files: sky.jpg"
    ):
        parse_document(document, tmp_path)


def test_a_trajectory_node_pointing_at_a_body_that_does_not_exist_is_rejected(tmp_path):
    document = spec_document()
    document["trajectory"]["nodes"][2]["body"] = "pluto"

    with pytest.raises(
        SceneError, match=r"trajectory.nodes\[2\].body is not a known body: pluto"
    ):
        parse_document(document, tmp_path)


def test_a_declared_texture_that_nothing_uses_is_rejected(tmp_path):
    document = spec_document()
    document["assets"]["files"].append({"texture": "unused.jpg", "used_by": "nobody"})

    with pytest.raises(SceneError, match="declares texture\\(s\\) that nothing uses: unused.jpg"):
        parse_document(document, tmp_path)


def test_a_band_box_without_a_dimension_is_rejected(tmp_path):
    document = spec_document()
    del document["bands"][0]["display"]["x_half_length"]

    with pytest.raises(SceneError, match=r"bands\[0\].display.x_half_length is required"):
        parse_document(document, tmp_path)

    document = spec_document()
    document["bands"][0]["display"]["thickness"] = 0
    with pytest.raises(SceneError, match=r"bands\[0\].display.thickness must be >= 1e-09"):
        parse_document(document, tmp_path)

    document = spec_document()
    document["bands"][0]["display"]["au_range"] = [2.1, 3.3]
    with pytest.raises(SceneError, match=r"bands\[0\].display has unknown key\(s\): au_range"):
        parse_document(document, tmp_path)


def test_a_body_display_must_give_radius_and_position(tmp_path):
    document = spec_document()
    del document["bodies"][2]["display"]["y"]

    with pytest.raises(SceneError, match=r"bodies\[2\].display.y is required"):
        parse_document(document, tmp_path)

    document = spec_document()
    document["bodies"][2]["display"]["longitude_deg"] = 190.0
    with pytest.raises(SceneError, match=r"bodies\[2\].display has unknown key\(s\): longitude_deg"):
        parse_document(document, tmp_path)


def test_a_body_kind_outside_the_known_set_is_rejected(tmp_path):
    document = spec_document()
    document["bodies"][2]["kind"] = "moon"

    with pytest.raises(SceneError, match=r"bodies\[2\].kind must be one of: star, planet, minor"):
        parse_document(document, tmp_path)


def test_a_size_range_that_descends_is_rejected(tmp_path):
    document = spec_document()
    document["bands"][0]["size_range"] = [0.16, 0.05]

    with pytest.raises(SceneError, match=r"bands\[0\].size_range must ascend"):
        parse_document(document, tmp_path)


def test_a_preview_resolution_that_contradicts_the_aspect_ratio_is_rejected(tmp_path):
    document = spec_document()
    document["render"]["preview"]["width"] = 640
    document["render"]["preview"]["height"] = 480

    with pytest.raises(SceneError, match="render.preview 640x480 does not match aspect_ratio 16:9"):
        parse_document(document, tmp_path)


def test_a_slightly_off_but_declared_aspect_ratio_is_accepted(tmp_path):
    """854x480 相对 16:9 偏 0.08%：这一层拦的是"比例写错了"，不是"不是整比"。"""
    document = spec_document()
    document["render"]["preview"]["width"] = 1280
    document["render"]["preview"]["height"] = 720

    assert parse_document(document, tmp_path).render.preview.width == 1280


def test_an_unknown_layout_kind_is_rejected(tmp_path):
    document = spec_document()
    document["layout"]["kind"] = "radial_compression"

    with pytest.raises(SceneError, match="layout.kind must be one of: monotonic_sequence"):
        parse_document(document, tmp_path)


def test_an_atmosphere_shell_that_does_not_enclose_its_body_is_rejected(tmp_path):
    document = spec_document()
    document["bodies"][1]["atmosphere"]["scale"] = 0.9

    with pytest.raises(SceneError, match=r"bodies\[1\].atmosphere.scale must be > 1"):
        parse_document(document, tmp_path)


def test_a_node_cannot_be_anchored_to_a_point_instead_of_a_body(tmp_path):
    """节点一律钉在天体上：位置要么由出发点推出、要么由路径摆出来，"自由一点"没有意义。"""
    document = spec_document()
    document["trajectory"]["nodes"][0]["display"] = {"x": 1.0, "y": 2.0}

    with pytest.raises(SceneError, match=r"trajectory.nodes\[0\] has unknown key\(s\): display"):
        parse_document(document, tmp_path)


def test_a_node_without_a_body_is_rejected(tmp_path):
    document = spec_document()
    document["trajectory"]["nodes"][1] = {
        "id": "mainbelt_flyby",
        "kind": "incidental",
        "clearance_radii": 2.5,
        "label": "主带飞掠",
    }

    with pytest.raises(SceneError, match=r"trajectory.nodes\[1\].body is required"):
        parse_document(document, tmp_path)


def test_an_incidental_node_without_a_clearance_is_rejected(tmp_path):
    document = spec_document()
    del document["trajectory"]["nodes"][1]["clearance_radii"]

    with pytest.raises(
        SceneError, match=r"trajectory.nodes\[1\].clearance_radii is required"
    ):
        parse_document(document, tmp_path)


def test_a_node_without_a_kind_is_rejected(tmp_path):
    """``kind`` 决定路径怎么走，是节点的必填项——缺了就没法画。"""
    document = spec_document()
    del document["trajectory"]["nodes"][1]["kind"]

    with pytest.raises(SceneError, match=r"trajectory.nodes\[1\].kind is required"):
        parse_document(document, tmp_path)


def test_a_node_kind_outside_the_known_set_is_rejected(tmp_path):
    document = spec_document()
    document["trajectory"]["nodes"][1]["kind"] = "swingby"

    with pytest.raises(
        SceneError,
        match=(
            r"trajectory.nodes\[1\].kind must be one of: "
            r"departure, incidental, gravity_assist, orbit_insertion"
        ),
    ):
        parse_document(document, tmp_path)


def _rekind(node, kind, **extra):
    """把一个夹具节点的 kind 换掉，并清掉属于旧 kind 的参数（它们是"只对本 kind 有效"的）。"""
    for key in (
        "offset_radii",
        "clearance_radii",
        "periapsis_radii",
        "turn_deg",
        "orbit_radii",
        "polar",
    ):
        node.pop(key, None)
    node["kind"] = kind
    node.update(extra)
    return node


def test_a_sequence_that_does_not_start_at_departure_is_rejected(tmp_path):
    document = spec_document()
    _rekind(
        document["trajectory"]["nodes"][0], "incidental", clearance_radii=2.5
    )

    with pytest.raises(SceneError, match=r"trajectory.nodes\[0\].kind must be departure"):
        parse_document(document, tmp_path)


def test_a_sequence_that_does_not_end_at_orbit_insertion_is_rejected(tmp_path):
    document = spec_document()
    _rekind(
        document["trajectory"]["nodes"][-1], "incidental", clearance_radii=2.5
    )

    with pytest.raises(SceneError, match=r"trajectory.nodes\[4\].kind must be orbit_insertion"):
        parse_document(document, tmp_path)


def test_a_sequence_without_exactly_one_gravity_assist_is_rejected(tmp_path):
    document = spec_document()
    _rekind(document["trajectory"]["nodes"][2], "incidental", clearance_radii=2.5)

    with pytest.raises(
        SceneError, match=r"trajectory.nodes must have exactly one gravity_assist node"
    ):
        parse_document(document, tmp_path)


def test_a_gravity_assist_periapsis_inside_the_minimum_is_rejected(tmp_path):
    """近木点太贴就不是"借力"了：半径的 1.5 倍是这张图读得出来的下限。"""
    document = spec_document()
    document["trajectory"]["nodes"][2]["periapsis_radii"] = 1.2

    with pytest.raises(
        SceneError, match=r"trajectory.nodes\[2\].periapsis_radii must be >= 1.5"
    ):
        parse_document(document, tmp_path)


def test_assist_parameters_do_not_apply_to_an_incidental_node(tmp_path):
    document = spec_document()
    document["trajectory"]["nodes"][1]["turn_deg"] = 45.0

    with pytest.raises(
        SceneError, match=r"trajectory.nodes\[1\].turn_deg does not apply to kind incidental"
    ):
        parse_document(document, tmp_path)


def test_duplicate_ids_are_rejected(tmp_path):
    document = spec_document()
    document["bodies"][3]["id"] = "earth"
    with pytest.raises(SceneError, match="duplicate body id: earth"):
        parse_document(document, tmp_path)

    document = spec_document()
    document["trajectory"]["nodes"][3]["id"] = "earth_departure"
    with pytest.raises(SceneError, match="duplicate trajectory node id: earth_departure"):
        parse_document(document, tmp_path)

    document = spec_document()
    document["assets"]["files"][3]["texture"] = "4k_earth_daymap.jpg"
    with pytest.raises(SceneError, match="duplicate asset texture: 4k_earth_daymap.jpg"):
        parse_document(document, tmp_path)


def test_a_single_node_trajectory_is_rejected(tmp_path):
    document = spec_document()
    document["trajectory"]["nodes"] = document["trajectory"]["nodes"][:1]

    with pytest.raises(SceneError, match="trajectory.nodes must list at least two nodes"):
        parse_document(document, tmp_path)


def test_a_missing_or_unparsable_file_is_reported_as_a_scene_error(tmp_path):
    with pytest.raises(SceneError, match="scene spec not found"):
        load_spec(tmp_path / "nope.yaml")

    path = tmp_path / "broken.yaml"
    path.write_text("meta: [1, 2\n", encoding="utf-8")
    with pytest.raises(SceneError, match="is not valid YAML"):
        load_spec(path)


def test_the_repository_spec_key_file_and_loader_agree(tmp_path):
    """写好的夹具规格逐字进 :func:`load_spec` 也必须过——夹具与真规格同一套字段。"""
    path = write_spec(tmp_path)
    assert path.read_text(encoding="utf-8") == SPEC_YAML
    spec = load_spec(path)
    assert spec.meta.name == "fixture_scene"
    assert yaml.safe_load(SPEC_YAML)["meta"]["title"] == spec.meta.title


def test_the_background_defaults_to_a_plain_starfield(tmp_path):
    """三个可选字段都有缺省：规格不写就按缺省画，写了就以规格为准。"""
    document = spec_document()
    document["background"] = {"starfield": "8k_stars_milky_way.jpg"}
    spec = parse_document(document, tmp_path)

    assert spec.background.strength == 8.0
    assert spec.background.texture_power == 0.3
    assert spec.background.field_color == (0.0, 0.0, 0.0)
    assert spec.background.band_offset_deg == 0.0


def test_the_background_fields_are_read_and_validated(tmp_path):
    document = spec_document()
    document["background"].update(
        {
            "strength": 2.0,
            "texture_power": 0.7,
            "field_color": [0.004, 0.008, 0.018],
            "band_offset_deg": 6.0,
        }
    )
    spec = parse_document(document, tmp_path)

    assert spec.background.strength == 2.0
    assert spec.background.texture_power == 0.7
    assert spec.background.field_color == (0.004, 0.008, 0.018)
    assert spec.background.band_offset_deg == 6.0

    document = spec_document()
    document["background"]["texture_power"] = 0
    with pytest.raises(SceneError, match=r"background.texture_power must be >= 1e-06"):
        parse_document(document, tmp_path)

    document = spec_document()
    document["background"]["strength"] = -1
    with pytest.raises(SceneError, match=r"background.strength must be >= 0.0"):
        parse_document(document, tmp_path)

    document = spec_document()
    document["background"]["band_offset_deg"] = "up"
    with pytest.raises(SceneError, match=r"background.band_offset_deg must be a number"):
        parse_document(document, tmp_path)


# ---------------------------------------------------------------- 任务层


def test_the_mission_fixture_covers_the_five_sites_of_the_source(tmp_path):
    """源文档 A.1 列了五个层位；这一层漏一个（比如少摆"外太阳系"）图上就少一整组东西。

    层位声明了就必须有簇——这条在载入时挡（``missions.sites declares site(s) with no
    cluster``），所以这里只需核对"五个都在、每条线都有人用"。
    """
    spec = fixtures.load_mission_spec(tmp_path)
    missions = spec.missions

    assert [site.id for site in missions.sites] == ["l2", "l1", "venus_orbit", "venus", "outer"]
    used_sites = {cluster.site for cluster in missions.clusters}
    assert used_sites == {site.id for site in missions.sites}
    used_lines = {cluster.line for cluster in missions.clusters}
    assert used_lines == {line.id for line in missions.lines}
    # 夹具是缩编版：真规格 19 个占位点（干涉台 1+4、日金 L1 一器、巡天星座 1+6、金星两次
    # 任务 2+1、海王线 3），这里 13 个——每个簇都抽到了，够跑通全部路径。
    assert missions.dot_count == 13


def test_a_mission_cluster_must_name_a_declared_line(tmp_path):
    document = fixtures.mission_spec_document()
    document["missions"]["clusters"][0]["line"] = "mars"

    with pytest.raises(SceneError, match=r"missions.clusters\[0\].line is not a declared line: mars"):
        parse_document(document, tmp_path)


def test_a_mission_cluster_must_name_a_declared_site(tmp_path):
    document = fixtures.mission_spec_document()
    document["missions"]["clusters"][0]["site"] = "kuiper"

    with pytest.raises(SceneError, match=r"missions.clusters\[0\].site is not a declared site: kuiper"):
        parse_document(document, tmp_path)


def test_a_declared_site_with_no_cluster_is_rejected(tmp_path):
    """声明了"外太阳系"却把那一簇删了：层位就空了，图上也少一组东西。"""
    document = fixtures.mission_spec_document()
    document["missions"]["clusters"] = [
        cluster for cluster in document["missions"]["clusters"] if cluster["site"] != "outer"
    ]

    with pytest.raises(SceneError, match=r"missions.sites declares site\(s\) with no cluster: outer"):
        parse_document(document, tmp_path)


def test_a_cluster_needs_exactly_one_of_at_and_anchor(tmp_path):
    document = fixtures.mission_spec_document()
    cluster = document["missions"]["clusters"][0]
    cluster["at"] = [1.0, 2.0]

    with pytest.raises(SceneError, match=r"missions.clusters\[0\] must give either at or anchor"):
        parse_document(document, tmp_path)

    document = fixtures.mission_spec_document()
    cluster = document["missions"]["clusters"][0]
    del cluster["anchor"]
    with pytest.raises(SceneError, match=r"missions.clusters\[0\] must give either at or anchor"):
        parse_document(document, tmp_path)


def test_a_mission_anchor_needs_either_an_offset_or_a_side(tmp_path):
    document = fixtures.mission_spec_document()
    anchor = document["missions"]["clusters"][0]["anchor"]
    del anchor["distance"]

    with pytest.raises(SceneError, match=r"missions.clusters\[0\].anchor must give either offset or"):
        parse_document(document, tmp_path)

    document = fixtures.mission_spec_document()
    document["missions"]["clusters"][0]["anchor"] = {"body": "earth", "side": "west", "distance": 3.0}
    with pytest.raises(SceneError, match=r".anchor.side must be one of: sunward, anti_sun"):
        parse_document(document, tmp_path)


def test_a_cluster_anchored_to_an_unknown_body_is_rejected(tmp_path):
    document = fixtures.mission_spec_document()
    document["missions"]["clusters"][0]["anchor"] = {"body": "planet_x", "offset": [1.0, 2.0]}

    with pytest.raises(SceneError, match=r"missions.clusters\[0\].anchor.body is not a known body"):
        parse_document(document, tmp_path)


def test_a_satellite_body_is_positioned_relative_to_its_parent(tmp_path):
    """卫星只能相对母体摆：母体坐标本身由路径算出来，写死的坐标会悄悄错位。"""
    spec = fixtures.load_mission_spec(tmp_path)

    triton = spec.body("triton")
    assert triton.satellite is True
    assert triton.display.x is None and triton.display.y is None
    assert triton.display.anchor.body == "neptune"
    assert (triton.display.anchor.dx, triton.display.anchor.dy) == (-6.0, -3.0)


def test_a_satellite_anchored_to_a_unknown_body_is_rejected(tmp_path):
    document = fixtures.mission_spec_document()
    for body in document["bodies"]:
        if body["id"] == "triton":
            body["display"]["anchor"]["body"] = "pluto"

    with pytest.raises(SceneError, match=r"bodies\[\d+\].display.anchor.body is not a known body"):
        parse_document(document, tmp_path)
