# -*- coding: utf-8 -*-
"""``primer.scene.assets``：贴图声明、下载、账本与校验。

测试**绝不联网**：下载入口 :func:`primer.scene.assets.fetch` 的 ``opener`` 是注入点，这
里全部换成替身。要测的三件事——幂等（跑第二遍不改一个字节）、换分辨率如实记账、网络失败
报得出 URL——都是"以后会咬人"的那类问题，所以单独钉住。
"""

from __future__ import annotations

import hashlib
import urllib.error

import pytest
import yaml

from scene_fixtures import fake_payload, load_fixture_spec, stage_assets

from primer.scene.assets import (
    AssetDecl,
    asset_url,
    candidate_urls,
    declared_assets,
    fetch,
    ledger_path,
    orphan_textures,
    prune_orphaned_textures,
    read_ledger,
    sha256_file,
    texture_dir,
    texture_path,
    verify_ledger,
)
from primer.scene.spec import SceneError

BASE_URL = "https://example.invalid/textures/download"


@pytest.fixture
def spec(tmp_path):
    return load_fixture_spec(tmp_path)


class _Opener:
    """替身下载器：按 URL 回内容，或按 URL 抛指定的异常。"""

    def __init__(self, payloads=None, missing=(), broken=()):
        self.payloads = dict(payloads or {})
        self.missing = set(missing)
        self.broken = set(broken)
        self.calls = []

    def __call__(self, url, timeout):
        self.calls.append(url)
        if url in self.broken:
            raise urllib.error.URLError("connection refused")
        if url in self.missing:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        if url in self.payloads:
            return self.payloads[url]
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)


def _payloads_for(spec):
    return {asset_url(spec.assets.base_url, name): fake_payload(name) for name in spec.textures}


# ---------------------------------------------------------------- 声明


def test_the_asset_declaration_carries_the_url_license_and_credit(spec):
    declarations = declared_assets(spec)
    by_texture = {declaration.texture: declaration for declaration in declarations}

    assert set(by_texture) == set(spec.textures)
    assert by_texture["4k_earth_daymap.jpg"].url == f"{BASE_URL}/4k_earth_daymap.jpg"
    assert by_texture["4k_earth_daymap.jpg"].license == "CC BY 4.0"
    assert by_texture["4k_earth_daymap.jpg"].credit == spec.assets.credit
    assert by_texture["4k_earth_daymap.jpg"].used_by == "earth"
    # 额外出处拼在总署名之后，而不是取而代之。
    assert by_texture["8k_stars_milky_way.jpg"].credit == f"{spec.assets.credit}; Milky Way panorama, CC BY 4.0"


def test_the_expected_fingerprint_comes_from_the_ledger(tmp_path, spec):
    stage_assets(tmp_path, spec)
    ledger = read_ledger(ledger_path(tmp_path))
    declarations = {item.texture: item for item in declared_assets(spec, ledger)}

    assert declarations["4k_jupiter.jpg"].expected_sha256 == ledger["4k_jupiter.jpg"].sha256
    assert declarations["4k_jupiter.jpg"].expected_bytes == ledger["4k_jupiter.jpg"].bytes


def test_candidate_urls_try_the_declared_name_first_then_the_size_variants(spec):
    declaration = AssetDecl(texture="4k_jupiter.jpg", url="", license="", credit="", used_by="jupiter")
    urls = candidate_urls(declaration, BASE_URL)

    assert urls[0] == f"{BASE_URL}/4k_jupiter.jpg"
    assert set(urls) == {
        f"{BASE_URL}/4k_jupiter.jpg",
        f"{BASE_URL}/2k_jupiter.jpg",
        f"{BASE_URL}/8k_jupiter.jpg",
    }
    # 2k 先于 8k：本图的半径都是示意比例，几千像素的贴图已经绰绰有余。
    assert urls[1] == f"{BASE_URL}/2k_jupiter.jpg"


def test_a_name_without_a_size_prefix_has_only_one_candidate():
    declaration = AssetDecl(texture="stars.jpg", url="", license="", credit="", used_by="sky")

    assert candidate_urls(declaration, BASE_URL) == (f"{BASE_URL}/stars.jpg",)


def test_textures_and_the_ledger_live_inside_the_output_boundary(tmp_path, spec):
    assert texture_dir(tmp_path) == tmp_path / "_primer" / "scene" / "assets" / "textures"
    assert ledger_path(tmp_path) == tmp_path / "_primer" / "scene" / "assets" / "credits.yaml"
    assert texture_path(tmp_path, "4k_earth_daymap.jpg") == texture_dir(tmp_path) / "4k_earth_daymap.jpg"


# ---------------------------------------------------------------- 下载


def test_fetch_downloads_every_declared_texture_and_records_provenance(tmp_path, spec):
    opener = _Opener(_payloads_for(spec))

    result = fetch(spec, tmp_path, opener=opener, today="2026-05-05")

    assert result.downloaded == spec.textures
    assert result.skipped == ()
    for texture in spec.textures:
        path = texture_path(tmp_path, texture)
        assert path.is_file()
        assert path.read_bytes() == fake_payload(texture)
    ledger = read_ledger(ledger_path(tmp_path))
    entry = ledger["4k_earth_daymap.jpg"]
    assert entry.url == f"{BASE_URL}/4k_earth_daymap.jpg"
    assert entry.source_file == "4k_earth_daymap.jpg"
    assert entry.path == "_primer/scene/assets/textures/4k_earth_daymap.jpg"
    assert entry.sha256 == sha256_file(texture_path(tmp_path, "4k_earth_daymap.jpg"))
    assert entry.bytes == len(fake_payload("4k_earth_daymap.jpg"))
    assert entry.license == "CC BY 4.0"
    assert entry.credit == spec.assets.credit
    assert entry.downloaded == "2026-05-05"


def test_a_second_fetch_changes_nothing(tmp_path, spec):
    fetch(spec, tmp_path, opener=_Opener(_payloads_for(spec)), today="2026-05-05")
    before = ledger_path(tmp_path).read_bytes()
    again = _Opener(_payloads_for(spec))

    result = fetch(spec, tmp_path, opener=again, today="2026-06-06")

    assert result.downloaded == ()
    assert result.skipped == spec.textures
    assert again.calls == []
    # 幂等包括下载日期：沿用账本里那一个，否则"有没有变"就看不出来。
    assert ledger_path(tmp_path).read_bytes() == before


def test_force_downloads_again(tmp_path, spec):
    fetch(spec, tmp_path, opener=_Opener(_payloads_for(spec)), today="2026-05-05")
    again = _Opener(_payloads_for(spec))

    result = fetch(spec, tmp_path, force=True, opener=again, today="2026-06-06")

    assert result.downloaded == spec.textures
    assert read_ledger(ledger_path(tmp_path))["4k_sun.jpg"].downloaded == "2026-06-06"


def test_a_file_that_no_longer_matches_the_ledger_is_downloaded_again(tmp_path, spec, capsys):
    fetch(spec, tmp_path, opener=_Opener(_payloads_for(spec)), today="2026-05-05")
    texture_path(tmp_path, "4k_earth_daymap.jpg").write_bytes(b"tampered\n")
    again = _Opener(_payloads_for(spec))

    result = fetch(spec, tmp_path, opener=again, today="2026-05-05")

    assert result.downloaded == ("4k_earth_daymap.jpg",)
    assert set(result.skipped) == set(spec.textures) - {"4k_earth_daymap.jpg"}
    assert texture_path(tmp_path, "4k_earth_daymap.jpg").read_bytes() == fake_payload("4k_earth_daymap.jpg")
    assert "does not match the ledger" in capsys.readouterr().err


def test_missing_four_k_names_fall_back_and_the_ledger_says_so(tmp_path, spec, capsys):
    """上游没有的部分 4k 名字：退到 2k，账本记的是**真正用了哪个 URL**。"""
    payloads = _payloads_for(spec)
    payloads[f"{BASE_URL}/2k_earth_daymap.jpg"] = fake_payload("4k_earth_daymap.jpg")
    opener = _Opener(payloads, missing=[f"{BASE_URL}/4k_earth_daymap.jpg"])

    fetch(spec, tmp_path, opener=opener, today="2026-05-05")

    entry = read_ledger(ledger_path(tmp_path))["4k_earth_daymap.jpg"]
    assert entry.url == f"{BASE_URL}/2k_earth_daymap.jpg"
    assert entry.source_file == "2k_earth_daymap.jpg"
    # 落盘文件名仍是规格里的名字：发射器只认这个名字。
    assert texture_path(tmp_path, "4k_earth_daymap.jpg").read_bytes() == fake_payload("4k_earth_daymap.jpg")
    assert "returned HTTP 404" in capsys.readouterr().err
    assert entry.sha256 == hashlib.sha256(fake_payload("4k_earth_daymap.jpg")).hexdigest()


def test_a_network_failure_raises_a_scene_error_naming_the_url(tmp_path, spec):
    opener = _Opener(broken=[asset_url(BASE_URL, name) for name in spec.textures])

    with pytest.raises(SceneError) as excinfo:
        fetch(spec, tmp_path, opener=opener)

    message = str(excinfo.value)
    assert "failed to download 4k_sun.jpg" in message
    assert f"{BASE_URL}/4k_sun.jpg" in message
    assert "connection refused" in message


def test_a_texture_that_is_missing_upstream_names_every_url_it_tried(tmp_path, spec):
    with pytest.raises(SceneError) as excinfo:
        fetch(spec, tmp_path, opener=_Opener())

    message = str(excinfo.value)
    assert "failed to download 4k_sun.jpg" in message
    assert "HTTP 404" in message
    # 规模型候选都试过了，报错时逐条点名。
    assert f"{BASE_URL}/4k_sun.jpg" in message
    assert f"{BASE_URL}/2k_sun.jpg" in message
    assert f"{BASE_URL}/8k_sun.jpg" in message


# ---------------------------------------------------------------- 校验


def test_verify_ledger_is_silent_when_everything_lines_up(tmp_path, spec):
    stage_assets(tmp_path, spec)

    assert verify_ledger(spec, tmp_path) == ()


def test_verify_ledger_reports_a_missing_ledger_entry(tmp_path, spec):
    stage_assets(tmp_path, spec)
    document = yaml.safe_load(ledger_path(tmp_path).read_text(encoding="utf-8"))
    document["files"] = [item for item in document["files"] if item["texture"] != "4k_jupiter.jpg"]
    ledger_path(tmp_path).write_text(yaml.safe_dump(document), encoding="utf-8")

    problems = verify_ledger(spec, tmp_path)

    assert problems == ("4k_jupiter.jpg is missing from the credits ledger",)


def test_verify_ledger_reports_a_missing_file_and_a_changed_hash(tmp_path, spec):
    stage_assets(tmp_path, spec)
    texture_path(tmp_path, "4k_jupiter.jpg").unlink()
    texture_path(tmp_path, "4k_earth_daymap.jpg").write_bytes(b"tampered\n")

    problems = verify_ledger(spec, tmp_path)

    assert "4k_jupiter.jpg is recorded in the ledger but missing on disk" in problems
    assert any(problem.startswith("4k_earth_daymap.jpg sha256 does not match the ledger") for problem in problems)


def test_verify_ledger_reports_a_blank_credit_line(tmp_path, spec):
    stage_assets(tmp_path, spec)
    document = yaml.safe_load(ledger_path(tmp_path).read_text(encoding="utf-8"))
    for item in document["files"]:
        if item["texture"] == "4k_sun.jpg":
            item["credit"] = "  "
    ledger_path(tmp_path).write_text(yaml.safe_dump(document), encoding="utf-8")

    assert "4k_sun.jpg has no credit line in the ledger" in verify_ledger(spec, tmp_path)


def test_a_corrupt_ledger_is_reported_rather_than_ignored(tmp_path, spec):
    ledger_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    ledger_path(tmp_path).write_text("files: [1, 2\n", encoding="utf-8")

    with pytest.raises(SceneError, match="credits ledger is not valid YAML"):
        read_ledger(ledger_path(tmp_path))


# ---------------------------------------------------------------- 孤儿文件


def test_a_leftover_from_a_renamed_texture_is_an_orphan(tmp_path, spec):
    """规格改了文件名（上游 4k 换成 8k），旧文件就留在目录里什么也不干。"""
    stage_assets(tmp_path, spec)
    stale = texture_dir(tmp_path) / "2k_sun.jpg"
    stale.write_bytes(b"the previous generation\n")

    assert orphan_textures(spec, tmp_path) == (stale,)


def test_orphans_are_recognised_by_name_not_by_content(tmp_path, spec):
    """名字一样就是自己人：内容对不对是账本的事，不是"孤儿"的事。"""
    stage_assets(tmp_path, spec)
    texture_path(tmp_path, "4k_jupiter.jpg").write_bytes(b"whatever the fetch left here\n")

    assert orphan_textures(spec, tmp_path) == ()


def test_hidden_files_are_not_orphans(tmp_path, spec):
    """``.DS_Store`` 是 macOS 自己放的，删了还会回来；报出来只是噪声。"""
    stage_assets(tmp_path, spec)
    (texture_dir(tmp_path) / ".DS_Store").write_bytes(b"\x00\x01")

    assert orphan_textures(spec, tmp_path) == ()


def test_a_missing_texture_directory_has_no_orphans(tmp_path, spec):
    assert orphan_textures(spec, tmp_path) == ()


def test_pruning_removes_orphans_and_half_finished_downloads_but_keeps_the_declared(
    tmp_path, spec, capsys
):
    stage_assets(tmp_path, spec)
    orphan = texture_dir(tmp_path) / "4k_saturn.jpg"
    orphan.write_bytes(b"stale\n")
    partial = texture_dir(tmp_path) / "4k_jupiter.jpg.part"
    partial.write_bytes(b"half a download\n")

    removed = prune_orphaned_textures(spec, tmp_path)

    # 名字判定一视同仁：下载中断留下的半个文件同样是"规格里没有的东西"。
    assert removed == ("4k_jupiter.jpg.part", "4k_saturn.jpg")
    assert not orphan.exists() and not partial.exists()
    for texture in spec.textures:
        assert texture_path(tmp_path, texture).is_file()
    assert "removing orphaned texture 4k_saturn.jpg" in capsys.readouterr().err
    # 再跑一遍什么都不删：幂等。
    assert prune_orphaned_textures(spec, tmp_path) == ()


def test_fetch_prunes_the_leftovers_of_the_previous_names(tmp_path, spec):
    """一条命令把目录收干净：账本按规格重写，不认的文件删掉。"""
    stage_assets(tmp_path, spec)
    for name in ("2k_sun.jpg", "8k_jupiter.jpg"):
        (texture_dir(tmp_path) / name).write_bytes(b"the previous generation\n")

    result = fetch(spec, tmp_path, opener=_Opener(_payloads_for(spec)), today="2026-05-05")

    assert result.pruned == ("2k_sun.jpg", "8k_jupiter.jpg")
    assert sorted(path.name for path in texture_dir(tmp_path).iterdir()) == sorted(spec.textures)
