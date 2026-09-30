# -*- coding: utf-8 -*-
"""``primer.scene.emit``：发射出来的脚本必须是确定的，命名契约必须只有一份。

两件事非测不可：

* **逐字节确定**。同一份规格、同一批贴图，两次发射必须一模一样（没有时间戳、键排序、
  次序定死），并且**与进程无关**——Python 对字符串的哈希每个进程都不同，用它当撒石块的
  种子会让每次重建都是另一片星带；那一条用一个换 ``PYTHONHASHSEED`` 的子进程来钉。
* **命名契约只有一份**。名字由 :func:`object_contract` / :func:`material_contract` 给出，
  ``checks`` 导入的是同一份；这里把契约摊开写死一遍，改动契约就会在这里被看见。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

import scene_fixtures as fixtures
from scene_fixtures import load_fixture_spec, spec_document, stage_assets, write_spec

from primer.scene import emit
from primer.scene.fingerprint import compute_fingerprint
from primer.scene.layout import plan
from primer.scene.spec import load_spec

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def spec(tmp_path):
    return load_fixture_spec(tmp_path)


def _fingerprint(tmp_path, spec):
    return compute_fingerprint(spec, tmp_path)


def test_the_naming_contract_is_exactly_the_documented_table(spec):
    contract = emit.object_contract(spec)

    assert contract == {
        "SUN_sun": "SUN",
        "SUN_sun_light": "SUN",
        "SUN_fill_light": "SUN",
        "BG_starfield": "BACKGROUND",
        "PLANET_venus": "PLANETS",
        "PLANET_venus_ATMO": "PLANETS",
        "PLANET_earth": "PLANETS",
        "PLANET_earth_ATMO": "PLANETS",
        "PLANET_earth_CLOUDS": "PLANETS",
        "PLANET_jupiter": "PLANETS",
        "PLANET_neptune": "PLANETS",
        # 小天体单独一套名字：它不是行星，混在 PLANET_ 里会让看清单的人认错。
        "MINOR_mainbelt": "PLANETS",
        "MINOR_centaur": "PLANETS",
        "BAND_main_belt": "BANDS",
        "TRAJ_neptune_arrival": "TRAJECTORIES",
        "TRAJ_neptune_arrival_ARROW": "TRAJECTORIES",
        "LABEL_sun": "LABELS",
        "LABEL_sun_HALO": "LABELS",
        "LABEL_venus": "LABELS",
        "LABEL_venus_HALO": "LABELS",
        "LABEL_earth": "LABELS",
        "LABEL_earth_HALO": "LABELS",
        "LABEL_mainbelt": "LABELS",
        "LABEL_mainbelt_HALO": "LABELS",
        "LABEL_jupiter": "LABELS",
        "LABEL_jupiter_HALO": "LABELS",
        "LABEL_centaur": "LABELS",
        "LABEL_centaur_HALO": "LABELS",
        "LABEL_neptune": "LABELS",
        "LABEL_neptune_HALO": "LABELS",
        "LABEL_earth_departure": "LABELS",
        "LABEL_earth_departure_HALO": "LABELS",
        "LABEL_mainbelt_flyby": "LABELS",
        "LABEL_mainbelt_flyby_HALO": "LABELS",
        "LABEL_jupiter_assist": "LABELS",
        "LABEL_jupiter_assist_HALO": "LABELS",
        "LABEL_centaur_flyby": "LABELS",
        "LABEL_centaur_flyby_HALO": "LABELS",
        "LABEL_neptune_arrival": "LABELS",
        "LABEL_neptune_arrival_HALO": "LABELS",
    }
    assert emit.COLLECTIONS == (
        "SUN",
        "PLANETS",
        "BANDS",
        "TRAJECTORIES",
        "MISSIONS",
        "LABELS",
        "BACKGROUND",
    )
    assert emit.material_contract(spec) == (
        "MAT_sun_emission",
        "MAT_planet_venus",
        "MAT_atmo_venus",
        "MAT_planet_earth",
        "MAT_atmo_earth",
        "MAT_clouds_earth",
        "MAT_minor_mainbelt",
        "MAT_planet_jupiter",
        "MAT_minor_centaur",
        "MAT_planet_neptune",
        "MAT_traj",
        "MAT_belt",
        "MAT_starfield",
        "MAT_label",
        "MAT_label_halo",
    )
    assert emit.expected_objects(spec) == tuple(contract)
    # 相机不进任何 collection（契约只给六个），所以它是单独一个名字。
    assert emit.CAMERA_OBJECT == "Camera_Main"
    assert emit.CAMERA_OBJECT not in contract


def test_the_contract_is_built_from_the_specs_own_ids(spec):
    """``PLANET_<id>``／``TRAJ_<终点 id>`` 这些名字都从规格的 id 推出来，不是写死的。"""
    names = emit.expected_objects(spec)

    assert "PLANET_sun" not in names  # 恒星走 SUN_sun，不是 PLANET_sun
    assert "PLANET_centaur" not in names  # 小天体走 MINOR_centaur
    assert "MINOR_mainbelt" in names
    assert "MINOR_centaur" in names
    assert "TRAJ_neptune_arrival" in names
    assert "TRAJ_mainbelt_flyby" not in names  # 整条航迹只以终点命名


def test_emitting_twice_yields_a_byte_identical_script(tmp_path, spec):
    fingerprint = _fingerprint(tmp_path, spec)

    first = emit.render_script(spec, tmp_path, fingerprint)
    second = emit.render_script(spec, tmp_path, fingerprint)

    assert first == second
    assert "20" + "26-" not in first.split("SCENE_JSON")[0]
    # 数据段是纯 ASCII 的 JSON，构建代码是静态文本：整份脚本可 diff。
    assert "SCENE_JSON" in first
    assert "def main():" in first


def test_the_emitted_script_embeds_the_numbers_the_layout_module_resolved(tmp_path, spec):
    fingerprint = _fingerprint(tmp_path, spec)

    script = emit.render_script(spec, tmp_path, fingerprint)
    payload = json.loads(json.loads(script.split("SCENE_JSON = ", 1)[1].split("\n", 1)[0]))
    bodies = {body["id"]: body for body in payload["bodies"]}
    placements = plan(spec)["bodies_by_id"]

    assert bodies["earth"]["position"] == list(placements["earth"].position)
    assert bodies["earth"]["magnification"] == pytest.approx(placements["earth"].magnification)
    assert bodies["earth"]["display_orbit_distance"] == pytest.approx(
        placements["earth"].display_orbit_distance
    )
    assert bodies["earth"]["real_radius_km"] == 6371.0
    # 小天体没有贴图，数据段里就是 null——发射器不替它编一个。
    assert bodies["centaur"]["texture_path"] is None
    assert bodies["centaur"]["material_type"] == "rock"
    assert bodies["earth"]["atmosphere"]["mode"] == "rim"
    assert bodies["earth"]["clouds"]["opacity"] == 0.85
    assert bodies["sun"]["material_type"] == "emission"
    assert bodies["sun"]["strength"] == 40.0
    assert bodies["neptune"]["texture_path"].endswith("/assets/textures/2k_neptune.jpg")
    # 节点自报语义 kind：出发、顺访、借力、顺访、捕获。
    assert [node["kind"] for node in payload["trajectory"]["nodes"]] == [
        "departure",
        "incidental",
        "gravity_assist",
        "incidental",
        "orbit_insertion",
    ]
    # 借力那一下的数是从几何解出来的（不是把规格里的 45° 抄一遍）：近木点落在半径倍数上。
    assist = payload["trajectory"]["assist"]
    assert assist["body_id"] == "jupiter"
    assert assist["periapsis_radii"] == pytest.approx(1.8, rel=1e-6)
    assert assist["turn_deg"] == pytest.approx(45.0, abs=0.5)
    # 终点收进一条闭合轨道，而且是极轨。
    assert payload["trajectory"]["capture"]["body_id"] == "neptune"
    assert payload["trajectory"]["capture"]["polar"] is True
    # 路径是解析采样出来的折线；每个节点都落在折线上的某一点。
    path = payload["trajectory"]["path"]
    assert len(path) > 100
    assert all(
        0 <= node["path_index"] < len(path) for node in payload["trajectory"]["nodes"]
    )
    assert payload["background"]["object_name"] == emit.STARFIELD_OBJECT


def test_changing_the_layout_changes_the_emitted_numbers(tmp_path, spec):
    """发射器不许自己重算布局：规格改一个位置，数据段必须跟着变。"""
    document = spec_document()
    document["bodies"][4]["display"]["x"] = 30.0
    document["bodies"][4]["display"]["y"] = -12.0
    path = write_spec(
        tmp_path, yaml.safe_dump(document, allow_unicode=True, sort_keys=False), name="moved.yaml"
    )
    moved = load_spec(path)

    def position_of(candidate, body_id):
        script = emit.render_script(candidate, tmp_path, _fingerprint(tmp_path, candidate))
        payload = json.loads(json.loads(script.split("SCENE_JSON = ", 1)[1].split("\n", 1)[0]))
        body = next(item for item in payload["bodies"] if item["id"] == body_id)
        return body["position"]

    assert position_of(spec, "jupiter") == [10.0, 5.0, 0.0]
    assert position_of(moved, "jupiter") == [30.0, -12.0, 0.0]


def test_the_final_mode_writes_to_the_final_directory_at_final_resolution(tmp_path, spec):
    fingerprint = _fingerprint(tmp_path, spec)

    preview = emit.scene_data(spec, tmp_path, fingerprint, final=False)
    final = emit.scene_data(spec, tmp_path, fingerprint, final=True)

    assert preview["render"]["width"] == 854 and preview["render"]["height"] == 480
    assert preview["render"]["samples"] == 8
    assert preview["render"]["output_dir"].endswith("/_primer/scene/renders/previews")
    assert preview["render"]["output_stem"] == "fixture_scene_preview_854x480"
    assert final["render"]["width"] == 3840 and final["render"]["height"] == 2160
    assert final["render"]["samples"] == 512
    assert final["render"]["exr"] is True
    assert final["render"]["output_dir"].endswith("/_primer/scene/renders/final")
    assert final["mode"] == "final"


def test_the_seed_is_derived_from_the_spec_content_not_from_the_process(tmp_path, spec):
    fingerprint = _fingerprint(tmp_path, spec)

    assert emit.scene_seed(spec, fingerprint) == int(fingerprint.spec_sha256, 16) % (2**31 - 1)

    document = spec_document()
    document["meta"]["title"] = "改过的标题"
    path = write_spec(
        tmp_path, yaml.safe_dump(document, allow_unicode=True, sort_keys=False), name="other.yaml"
    )
    changed = load_spec(path)

    assert emit.scene_seed(changed, compute_fingerprint(changed, tmp_path)) != emit.scene_seed(
        spec, fingerprint
    )


def test_the_script_is_independent_of_python_hash_randomisation(tmp_path, spec):
    """换 ``PYTHONHASHSEED`` 重发一次，必须逐字节相同。"""
    script = (
        "import sys; sys.path.insert(0, %r)\n"
        "from pathlib import Path\n"
        "from primer.scene.spec import load_spec\n"
        "from primer.scene.fingerprint import compute_fingerprint\n"
        "from primer.scene.emit import render_script\n"
        "root = Path(%r)\n"
        "spec = load_spec(Path(%r))\n"
        "sys.stdout.write(render_script(spec, root, compute_fingerprint(spec, root)))\n"
    ) % (str(REPO_ROOT / "src"), str(tmp_path), str(spec.path))
    outputs = []
    for seed in ("random", "0", "1"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        result = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, env=env, cwd=str(REPO_ROOT)
        )
        assert result.returncode == 0, result.stderr
        outputs.append(result.stdout)
    assert outputs[0] == outputs[1] == outputs[2]


def test_write_script_puts_the_script_where_the_build_expects_it(tmp_path, spec):
    target = tmp_path / "_primer" / "scene" / "build" / emit.SCRIPT_NAME

    written = emit.write_script(spec, tmp_path, _fingerprint(tmp_path, spec), target)

    assert written == target
    assert target.read_text(encoding="utf-8") == emit.render_script(
        spec, tmp_path, _fingerprint(tmp_path, spec)
    )


def test_the_asset_staging_does_not_influence_the_contract(tmp_path, spec):
    """契约只由规格决定：贴图到位与否都不改变对象名。"""
    before = emit.object_contract(spec)
    stage_assets(tmp_path, spec)

    assert emit.object_contract(spec) == before


def test_the_background_rotation_brings_the_band_in_not_the_pole(tmp_path, spec):
    """回归：天球要转的是**银河带**（赤道），不是极点。

    转成正值等于把星图的极点转进画面——等距柱状投影在极点附近把星点拉成放射状条纹，
    整幅图看着像雪暴。所以角度必须是 ``-(elevation + band_offset)``。
    """
    assert emit.background_angle_deg(spec) == -(spec.camera.elevation_deg + 0.0)
    assert emit.background_angle_deg(spec) < 0.0

    axis = emit.background_axis(spec)
    assert axis[2] == 0.0  # 水平轴，绕它转才是在画面里上下移动
    assert abs(sum(component * component for component in axis) - 1.0) < 1e-9

    # 转过去之后，天球上"原来在黄道面、正对相机另一侧"的那一点要落在视线上。
    import math

    angle = math.radians(emit.background_angle_deg(spec))
    azimuth = math.radians(spec.camera.azimuth_deg + 180.0)
    equator_point = (math.cos(azimuth), math.sin(azimuth), 0.0)
    cosine, sine = math.cos(angle), math.sin(angle)
    ax, ay = axis[0], axis[1]
    cross = (ay * equator_point[2] - 0.0 * equator_point[1], 0.0 * equator_point[0] - ax * equator_point[2], ax * equator_point[1] - ay * equator_point[0])
    dot = ax * equator_point[0] + ay * equator_point[1]
    rotated = tuple(
        equator_point[i] * cosine + cross[i] * sine + axis[i] * dot * (1.0 - cosine)
        for i in range(3)
    )
    elevation = math.radians(-spec.camera.elevation_deg)
    expected = (
        math.cos(elevation) * math.cos(azimuth),
        math.cos(elevation) * math.sin(azimuth),
        math.sin(elevation),
    )
    assert rotated == pytest.approx(expected, abs=1e-9)


def test_the_emitted_data_carries_the_look_settings(tmp_path, spec):
    fingerprint = _fingerprint(tmp_path, spec)
    script = emit.render_script(spec, tmp_path, fingerprint)
    payload = json.loads(json.loads(script.split("SCENE_JSON = ", 1)[1].split("\n", 1)[0]))

    background = payload["background"]
    assert background["texture_power"] == spec.background.texture_power
    assert background["strength"] == spec.background.strength
    assert background["field_color"] == list(spec.background.field_color)
    assert background["rotation_angle_deg"] == emit.background_angle_deg(spec)
    assert payload["glare"]["threshold"] == emit.GLARE_THRESHOLD
    assert payload["labels"]["halo_material_name"] == emit.LABEL_HALO_MATERIAL
    assert payload["labels"]["view_axis"] == list(
        __import__("primer.scene.layout", fromlist=["camera_placement"]).camera_placement(spec.camera).forward
    )
    # 夹具里天体的次序是 sun / venus / earth / ...，所以头三个标签是太阳、金星与地球。
    assert [item["halo_name"] for item in payload["labels"]["items"]][:3] == [
        "LABEL_sun_HALO",
        "LABEL_venus_HALO",
        "LABEL_earth_HALO",
    ]
    assert [item["object_name"] for item in payload["labels"]["items"]][:3] == [
        "LABEL_sun",
        "LABEL_venus",
        "LABEL_earth",
    ]
    assert payload["trajectory"]["arrow"]["object_name"] == emit.arrow_object_name(
        spec.trajectory.destination.id
    )
    assert payload["bodies"][0]["limb_edge"] == emit.SUN_LIMB_EDGE


def test_the_emitted_script_builds_the_glare_the_arrow_and_the_text_outline(tmp_path, spec):
    """脚本正文里那一串节点名就是这个功能的实现清单：合成器、箭头、描边、贴图日面。"""
    script = emit.render_script(spec, tmp_path, _fingerprint(tmp_path, spec))

    for marker in (
        "Glare_FogGlow",
        "Compositor_Nodes",
        "RL_Scene",
        "Composite_Output",
        "Fog Glow",
        "Tex_Photosphere",
        "Ramp_LimbDarkening",
        "def build_arrow",
        "Gamma_BandLift",
        "Mix_DeepField",
        'rotation_mode = "AXIS_ANGLE"',
        "curve.offset = offset",
        "Vector_DotRim",
        "Math_RimFalloff",
        "save_version = 0",
    ):
        assert marker in script, marker


# ---------------------------------------------------------------- 任务层


def test_the_mission_layer_enters_the_contract_and_the_report(tmp_path):
    """19 个占位点、8 条组标签、图例 4 行：对象名、材质、数据段一处都不能少。"""
    spec = fixtures.load_mission_spec(tmp_path)
    fingerprint = _fingerprint(tmp_path, spec)
    contract = emit.object_contract(spec)

    dots = [name for name in contract if name.startswith(emit.MISSION_PREFIX)]
    assert len(dots) == 13 + 4  # 13 个簇内点位 + 图例 4 个色点
    assert all(contract[name] == emit.MISSIONS_COLLECTION for name in dots)
    assert "LABEL_mission_interferometer" in contract
    assert contract["LABEL_mission_interferometer"] == emit.LABELS_COLLECTION
    assert "LABEL_legend_traj" in contract
    assert emit.MISSIONS_COLLECTION in emit.COLLECTIONS
    for material in ("MAT_mission_exo", "MAT_mission_venus", "MAT_mission_neptune"):
        assert material in emit.material_contract(spec)

    payload = json.loads(
        json.loads(
            emit.render_script(spec, tmp_path, fingerprint).split("SCENE_JSON = ", 1)[1].split(
                "\n", 1
            )[0]
        )
    )
    missions = payload["missions"]
    assert [line["id"] for line in missions["lines"]] == ["exo", "venus", "neptune"]
    assert [site["id"] for site in missions["sites"]] == ["l2", "l1", "venus_orbit", "venus", "outer"]
    assert [cluster["id"] for cluster in missions["clusters"]][0] == "interferometer"
    assert len(missions["legend"]) == 4
    # 点位带自己的对象名与坐标：check 读的就是这一份，不猜命名习惯。
    first = missions["clusters"][0]["dots"][0]
    assert first["object_name"] == "DOT_interferometer_combiner"
    assert len(first["position"]) == 3


def test_a_spec_without_missions_still_builds(tmp_path):
    """任务层是可选的：没有 `missions:` 就不建点位，也不给它们开材质。"""
    spec = fixtures.load_fixture_spec(tmp_path)
    script = emit.render_script(spec, tmp_path, _fingerprint(tmp_path, spec))

    payload = json.loads(json.loads(script.split("SCENE_JSON = ", 1)[1].split("\n", 1)[0]))
    assert payload["missions"] is None
    assert not [name for name in emit.object_contract(spec) if name.startswith("DOT_")]
