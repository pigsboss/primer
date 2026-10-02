# -*- coding: utf-8 -*-
"""H1 差异分诊与交付门禁（用户真正会敲的命令：``python3 -m primer.imgcmp.triage``）测试。

分两层：

- **自造用例**：合成两张组件特征表（或两张自造图）＋自造规则文件，覆盖三档判定、
  显式构型级优先、默认升级、配对级底色行、选择器（kind／pattern／shape／数值／when／
  top_band_equal）、规则失败即拒、异议单落盘、门禁通过与阻断、模板骨架、CLI 退出码、
  ``--selftest``——不依赖任何任务侧产物。
- **规则示例对照**：若本机能读到 2034 任务侧 T2a 产物，就用仓库示例规则
  ``triage_rules.example.yaml`` 跑真实对，验"允许差挂清单条目号、构型级判升级"；
  读不到则整组 skip（沿用 ``$PRIMER_IMGCMP_T2A`` 覆盖）。
"""

from __future__ import annotations

import json
import os

import pytest
import yaml

from primer.imgcmp import feat, triage as T

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLE_RULES = os.path.join(REPO_ROOT, "src", "primer", "imgcmp", "triage_rules.example.yaml")

T2A_CANDIDATES = (
    os.environ.get("PRIMER_IMGCMP_T2A"),
    ("/Users/huo/Documents/kimi/Workspaces/行星探测工程/_primer/scene/"
     "interferometer/observatory/out/still/imgcmp/t2a"),
    os.path.join(REPO_ROOT, "tests", "fixtures", "imgcmp", "t2a"),
)


def _t2a_root():
    for cand in T2A_CANDIDATES:
        if cand and os.path.isfile(os.path.join(cand, "fig3b_base.feat.json")):
            return cand
    return None


T2A_ROOT = _t2a_root()


# ---------------------------------------------------------------- 夹具

def _comp(bbox, px, shape, height_fraction=0.5, area_fraction=0.2):
    x0, y0, x1, y1 = bbox
    return {"label": "synth", "kind": "component", "bbox_xyxy": list(bbox),
            "bbox_wh": [x1 - x0, y1 - y0], "pixel_count": px,
            "centroid": [(x0 + x1) / 2.0, (y0 + y1) / 2.0],
            "height_fraction": height_fraction, "area_fraction": area_fraction,
            "anchor_ratio": None, "shape_class": shape, "shape_descriptors": {},
            "holes": {"count": 0},
            "openings": {"count": 0, "top_band_count": 0, "items": []},
            "contacts": []}


def _table(image, kind, comps):
    return {"tool": "synth", "image": image, "kind": kind, "image_size": [640, 480],
            "roi": [0, 0, 640, 480], "min_area": 1, "annotation_count": 0,
            "total_payload_height_px": 400, "components": comps}


@pytest.fixture(scope="module")
def pair(tmp_path_factory):
    """基准 white 2 件 / 我方 black 3 件；差集含 added、count、height_fraction、shape_class。"""
    ref = _table("synth_ref.png", "white", [
        _comp((0, 0, 200, 40), 4000, "rect", height_fraction=0.2, area_fraction=0.3),
        _comp((0, 60, 200, 70), 900, "circle", height_fraction=0.2, area_fraction=0.2),
    ])
    ours = _table("synth_ours.png", "black", [
        _comp((0, 0, 500, 40), 9000, "rod", height_fraction=1.0, area_fraction=0.5),
        _comp((0, 60, 200, 70), 900, "circle", height_fraction=0.2, area_fraction=0.2),
        _comp((400, 20, 420, 30), 120, "other", height_fraction=0.05, area_fraction=0.01),
    ])
    return ref, ours


def _rules_path(tmp_path, rules):
    path = tmp_path / "rules.yaml"
    path.write_text(yaml.safe_dump(rules, allow_unicode=True), encoding="utf-8")
    return str(path)


def _run(pair, rules, tmp_path):
    ref, ours = pair
    ruleset = T.load_rules(_rules_path(tmp_path, rules))
    return T.triage_pair(feat.compare_tables(ref, ours), ref, ours, ruleset)


BASE_RULES = {
    "version": 1,
    "allow": [{"list_item_id": "AL-1", "note": "star spec", "kind": "added",
               "ours_max": 200}],
    "free": [{"scope": "shape-cosmetics", "note": "shape wording", "kind": "shape_class"}],
    "escalate": [{"judgement": "CFG-1", "note": "long rod", "advice": "redo",
                  "kind": "height_fraction", "abs_delta_min": 0.5}],
    "escalation": {"default": "escalate",
                   "template": {"title": "异议单",
                                "fields": ["差异描述", "特征证据", "建议方案"]}},
}


def _by_kind(result):
    out = {}
    for row in result["rows"]:
        out.setdefault(row["kind"], []).append(row)
    return out


# ---------------------------------------------------------------- 三档与选择器

def test_three_tiers_and_default(pair, tmp_path):
    result = _run(pair, BASE_RULES, tmp_path)
    kinds = _by_kind(result)
    assert kinds["added"][0]["tier"] == T.TIER_ALLOW
    assert kinds["added"][0]["basis"]["list_item_id"] == "AL-1"
    assert kinds["shape_class"][0]["tier"] == T.TIER_FREE
    assert kinds["shape_class"][0]["basis"]["scope"] == "shape-cosmetics"
    big = [r for r in kinds["height_fraction"] if r["tier"] == T.TIER_ESCALATE]
    assert big and big[0]["basis"]["judgement"] == "CFG-1"
    assert kinds["count"][0]["tier"] == T.TIER_ESCALATE
    assert kinds["count"][0]["hit"] == "default"
    assert kinds["count"][0]["disposition"] == T.DEFAULT_DISPOSITION[T.TIER_ESCALATE]
    assert result["counts"]["escalate"] >= 3
    assert all(r["tier_zh"] == T.TIER_ZH[r["tier"]] for r in result["rows"])


def test_explicit_escalation_beats_allowance(pair, tmp_path):
    """同一条差异同时命中 allow 与 escalate 时，构型级声明胜出（不得自我豁免）。"""
    rules = dict(BASE_RULES)
    rules["allow"] = [{"list_item_id": "AL-9", "note": "would exempt", "kind": "count"}]
    rules["escalate"] = BASE_RULES["escalate"] + [
        {"judgement": "CFG-COUNT", "note": "component count is a construction criterion",
         "kind": "count"}]
    result = _run(pair, rules, tmp_path)
    count = _by_kind(result)["count"][0]
    assert count["tier"] == T.TIER_ESCALATE
    assert count["basis"]["judgement"] == "CFG-COUNT"


def test_pair_level_background_row(pair, tmp_path):
    result = _run(pair, BASE_RULES, tmp_path)
    bg = _by_kind(result)["background_kind"][0]
    assert bg["row_id"] == "R01"
    assert "ref_kind=white" in bg["subject"] and "ours_kind=black" in bg["subject"]
    assert bg["tier"] == T.TIER_ESCALATE                    # 没规则就默认升级


def test_background_allowance_by_kind(pair, tmp_path):
    rules = dict(BASE_RULES)
    rules["allow"] = [{"list_item_id": "AL-BG", "kind": "background_kind",
                       "note": "baseline has no sky"}]
    result = _run(pair, rules, tmp_path)
    bg = _by_kind(result)["background_kind"][0]
    assert bg["tier"] == T.TIER_ALLOW and bg["basis"]["list_item_id"] == "AL-BG"


def test_pattern_selector_matches_subject(pair, tmp_path):
    rules = dict(BASE_RULES)
    rules["allow"] = [{"list_item_id": "AL-P", "pattern": r"kind=added .*shape=-->other"}]
    result = _run(pair, rules, tmp_path)
    added = _by_kind(result)["added"]
    allowed = [r for r in added if r["tier"] == T.TIER_ALLOW]
    assert len(allowed) == 1 and allowed[0]["ours_shapes"] == ["other"]


def test_numeric_selectors_are_fail_closed(pair, tmp_path):
    """数值选择器遇到非数值字段（shape_class 的 ref/ours 是字符串）判不命中。"""
    rules = dict(BASE_RULES)
    rules["free"] = []
    rules["allow"] = [{"list_item_id": "AL-N", "kind": "shape_class", "ref_min": 0.0}]
    result = _run(pair, rules, tmp_path)
    assert _by_kind(result)["shape_class"][0]["tier"] == T.TIER_ESCALATE


def test_when_scoping_limits_a_rule(pair, tmp_path):
    rules = dict(BASE_RULES)
    rules["allow"] = [{"list_item_id": "AL-W", "kind": "added",
                       "when": {"ref_path_contains": "nothing-matches"}}]
    result = _run(pair, rules, tmp_path)
    assert _by_kind(result)["added"][0]["tier"] == T.TIER_ESCALATE
    rules["allow"][0]["when"] = {"ours_path_contains": "synth_ours", "ours_kind": "black"}
    result = _run(pair, rules, tmp_path)
    assert _by_kind(result)["added"][0]["tier"] == T.TIER_ALLOW


def test_top_band_equal_selects_the_cutaway_not_a_tube_opening():
    """开口数差异：顶带内开口（筒顶开口）不命中，筒身剖口（顶带未变）命中。"""
    ref = _table("a.png", "white", [_comp((0, 0, 100, 100), 9000, "other")])
    ours = _table("b.png", "black", [_comp((0, 0, 100, 100), 9000, "other")])
    row_cutaway = {"kind": "hole_count", "ref": 4, "ours": 0, "delta": -4,
                   "ref_shapes": ["other"], "ours_shapes": ["ring"],
                   "top_band_ref": 0, "top_band_ours": 0, "source": "diff",
                   "evidence": "开口数 4→0（顶带内 0→0）"}
    row_tube = dict(row_cutaway, top_band_ref=2, top_band_ours=1,
                    evidence="开口数 2→1（顶带内 2→1）")
    rule = {"tier": T.TIER_ALLOW, "list_item_id": "AL-2", "kind": "hole_count",
            "top_band_equal": True, "_patterns": [], "when": None}
    assert T.decide(row_cutaway, {"escalate": [], "allow": [rule], "free": [],
                                  "escalation": {"default": T.TIER_ESCALATE}},
                    ref, ours)["tier"] == T.TIER_ALLOW
    assert T.decide(row_tube, {"escalate": [], "allow": [rule], "free": [],
                               "escalation": {"default": T.TIER_ESCALATE}},
                    ref, ours)["tier"] == T.TIER_ESCALATE


# ---------------------------------------------------------------- 规则失败即拒

@pytest.mark.parametrize("rules,needle", [
    ({"version": 1, "allow": [{"list_item_id": "X", "note": "wildcard"}]}, "selector"),
    ({"version": 1, "allow": [{"note": "no id", "kind": "added"}]}, "list_item_id"),
    ({"version": 1, "free": [{"note": "no scope", "kind": "added"}]}, "scope"),
    ({"version": 1, "escalate": [{"note": "no judgement", "kind": "added"}]}, "judgement"),
    ({"version": 1, "allow": [{"list_item_id": "X", "kind": "added",
                               "pattern": "([unclosed"}]}, "valid regexp"),
    ({"version": 1, "escalation": {"default": "maybe"}}, "escalation.default"),
    ({"version": 1, "escalation": "nope"}, "escalation must be a mapping"),
    ({"version": 1, "allow": [{"list_item_id": "X", "kind": "added", "ours_min": "x"}]},
     "must be a number"),
    ({"version": 1, "allow": [{"list_item_id": "X", "kind": "added",
                               "when": {"nope": 1}}]}, "unknown key"),
    ({"version": 1, "nonsense": []}, "unknown rules key"),
    (["not", "a", "mapping"], "must be a mapping"),
])
def test_bad_rules_are_rejected(tmp_path, rules, needle):
    path = _rules_path(tmp_path, rules)
    with pytest.raises(T.ImgCmpError) as exc:
        T.load_rules(path)
    assert needle in str(exc.value)


def test_missing_rules_file(tmp_path):
    with pytest.raises(T.ImgCmpError) as exc:
        T.load_rules(str(tmp_path / "nope.yaml"))
    assert "not found" in str(exc.value)


def test_escalation_default_is_read_from_the_file(pair, tmp_path):
    rules = dict(BASE_RULES)
    rules["escalation"] = {"default": "free"}
    result = _run(pair, rules, tmp_path)
    assert _by_kind(result)["count"][0]["tier"] == T.TIER_FREE


# ---------------------------------------------------------------- 产物与异议单

def test_products_and_escalation_notes(pair, tmp_path):
    ref, ours = pair
    ref_path = tmp_path / "ref.json"
    ours_path = tmp_path / "ours.json"
    ref_path.write_text(json.dumps(ref), encoding="utf-8")
    ours_path.write_text(json.dumps(ours), encoding="utf-8")
    rules_path = _rules_path(tmp_path, BASE_RULES)
    out = tmp_path / "out"
    result = T.run_triage(str(ref_path), str(ours_path), rules_path, str(out))

    table_md = out / ("%s.md" % T.SECTION_NAME)
    assert table_md.is_file()
    text = table_md.read_text(encoding="utf-8")
    assert text.startswith("# %s" % T.SECTION_NAME)
    for column in T.SECTION_COLUMNS:
        assert column in text
    assert text.count("\n| ") >= len(result["rows"])

    payload = json.loads((out / "triage.json").read_text(encoding="utf-8"))
    assert payload["mode"] == "triage"
    assert payload["rules"]["sha256_16"]
    assert len(payload["rows"]) == len(result["rows"])
    assert payload["counts"] == result["counts"]

    notes = sorted((out / "escalations").glob("ESC-*.md"))
    assert len(notes) == len(result["escalations"]) >= 1
    body = notes[0].read_text(encoding="utf-8")
    assert body.startswith("# 异议单 ESC-")
    for field in ("差异描述", "特征证据", "建议方案"):
        assert "## %s" % field in body
    esc = payload["escalations"][0]
    assert esc["file"].endswith(".md") and esc["escalation_id"] == "ESC-01"
    assert esc["row_id"] == payload["rows"][0]["row_id"] or esc["escalation_id"]


def test_no_escalation_means_no_escalation_dir(pair, tmp_path):
    rules = {"version": 1, "escalation": {"default": "free"},
             "escalate": [{"judgement": "J", "kind": "no-such-kind", "note": "n"}]}
    ref, ours = pair
    ref_path, ours_path = tmp_path / "r.json", tmp_path / "o.json"
    ref_path.write_text(json.dumps(ref), encoding="utf-8")
    ours_path.write_text(json.dumps(ours), encoding="utf-8")
    out = tmp_path / "out"
    T.run_triage(str(ref_path), str(ours_path), _rules_path(tmp_path, rules), str(out))
    assert not (out / "escalations").exists()


# ---------------------------------------------------------------- 模板与门禁

def test_template_skeleton_has_the_fixed_columns():
    text = T.section_template()
    assert text.startswith("## %s" % T.SECTION_NAME)
    for column in T.SECTION_COLUMNS:
        assert column in text
    assert "禁止自行维持" in text


def _note(path, body):
    path.write_text(body, encoding="utf-8")
    return str(path)


def test_gate_passes_with_template_section(tmp_path):
    path = _note(tmp_path / "ok.md", "# 交付说明\n\n" + T.section_template()
                 + "\n## 下一步\n\n无。\n")
    info = T.check_note(path)
    assert info["section"] == T.SECTION_NAME and info["rows"] >= 1
    assert info["columns"] == list(T.SECTION_COLUMNS)


def test_gate_blocks_when_section_missing(tmp_path):
    path = _note(tmp_path / "bad.md", "# 交付说明\n\n## 其他\n\n没有分诊表。\n")
    with pytest.raises(T.ImgCmpError) as exc:
        T.check_note(path)
    assert "missing the required section" in str(exc.value)


def test_gate_blocks_when_fields_missing(tmp_path):
    path = _note(tmp_path / "bad.md",
                 "# 交付说明\n\n## %s\n\n只有标题没有表。\n" % T.SECTION_NAME)
    with pytest.raises(T.ImgCmpError) as exc:
        T.check_note(path)
    assert "required field" in str(exc.value)


def test_gate_custom_section_name_and_missing_file(tmp_path):
    path = _note(tmp_path / "ok.md", "## 别的章节\n\n| 差异 | 特征证据 | 分诊档位 | 依据 | 处置 |\n"
                 "|---|---|---|---|---|\n| a | b | 升级 | 默认 | 出单 |\n")
    with pytest.raises(T.ImgCmpError) as exc:
        T.check_note(path, "差异分诊表")
    assert "missing the required section" in str(exc.value)
    info = T.check_note(path, "别的章节")
    assert info["section"] == "别的章节" and info["rows"] == 1
    with pytest.raises(T.ImgCmpError) as exc:
        T.check_note(str(tmp_path / "nope.md"))
    assert "not found" in str(exc.value)


def test_gate_only_looks_inside_the_section(tmp_path):
    """章节之外的表格不能顶替——字段必须落在该章节体内。"""
    body = ("# 交付说明\n\n| %s |\n|%s|\n| a |\n\n## %s\n\n（空章节）\n"
            % (" | ".join(T.SECTION_COLUMNS), "---|" * len(T.SECTION_COLUMNS), T.SECTION_NAME))
    path = _note(tmp_path / "bad.md", body)
    with pytest.raises(T.ImgCmpError) as exc:
        T.check_note(path)
    assert "required field" in str(exc.value)


# ---------------------------------------------------------------- CLI

def _cli(argv):
    try:
        return int(T.main(argv))
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2


def test_cli_exit_codes(tmp_path, capsys):
    assert _cli(["--selftest"]) == 0
    capsys.readouterr()
    assert _cli([]) == 2                                     # 缺子命令
    assert _cli(["triage"]) == 2                             # 缺 --ref/--ours/--rules/--out
    assert _cli(["triage", "--ref", "a.json", "--ours", "b.json", "--rules", "r.yaml"]) == 2
    assert _cli(["triage", "--ref", str(tmp_path / "no.json"), "--ours", "b.json",
                 "--rules", "r.yaml", "--out", str(tmp_path)]) == 2
    assert _cli(["gate"]) == 2                               # 缺 --delivery-note
    assert _cli(["gate", "--delivery-note", str(tmp_path / "nope.md")]) == 2
    assert _cli(["nonsense"]) == 2                            # argparse 用法错误


def test_cli_gate_blocks_and_passes(tmp_path, capsys):
    blocked = tmp_path / "blocked.md"
    blocked.write_text("# 交付说明\n\n## 其他\n\n无分诊表。\n", encoding="utf-8")
    assert _cli(["gate", "--delivery-note", str(blocked)]) == 2
    err = capsys.readouterr().err
    assert "missing the required section" in err and "差异分诊表" in err

    good = tmp_path / "good.md"
    good.write_text("# 交付说明\n\n" + T.section_template(), encoding="utf-8")
    assert _cli(["gate", "--delivery-note", str(good)]) == 0
    out = capsys.readouterr().out
    assert "门禁通过" in out


def test_cli_template_on_both_subcommands(capsys):
    assert _cli(["triage", "--template"]) == 0
    first = capsys.readouterr().out
    assert _cli(["gate", "--template"]) == 0
    second = capsys.readouterr().out
    assert first == second and T.SECTION_NAME in first


def test_cli_triage_from_images(tmp_path, capsys):
    """给的是**图**：内部先调 feat.table 建表，再分诊。"""
    from PIL import Image
    ref_png = tmp_path / "ref.png"
    ours_png = tmp_path / "ours.png"
    Image.fromarray(feat._synth(white=True), mode="RGB").save(ref_png)
    Image.fromarray(feat._synth_extra(white=True), mode="RGB").save(ours_png)
    rules = tmp_path / "rules.yaml"
    rules.write_text(yaml.safe_dump(
        {"version": 1, "escalation": {"default": "escalate"}}, allow_unicode=True),
        encoding="utf-8")
    out = tmp_path / "out"
    assert _cli(["triage", "--ref", str(ref_png), "--ours", str(ours_png),
                 "--rules", str(rules), "--out", str(out)]) == 0
    assert (out / ("%s.md" % T.SECTION_NAME)).is_file()
    payload = json.loads((out / "triage.json").read_text(encoding="utf-8"))
    assert payload["ref"]["table"].endswith("ref.png")
    assert payload["counts"]["escalate"] == len(payload["rows"]) >= 1
    assert "差异分诊" in capsys.readouterr().out


def test_cli_triage_json_override_and_bad_rules(tmp_path):
    from PIL import Image
    ref_png, ours_png = tmp_path / "r.png", tmp_path / "o.png"
    Image.fromarray(feat._synth(white=True), mode="RGB").save(ref_png)
    Image.fromarray(feat._synth(white=True), mode="RGB").save(ours_png)
    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.safe_dump({"version": 1, "allow": [{"note": "wild"}]},
                                  allow_unicode=True), encoding="utf-8")
    assert _cli(["triage", "--ref", str(ref_png), "--ours", str(ours_png),
                 "--rules", str(bad), "--out", str(tmp_path / "o")]) == 2
    good = tmp_path / "good.yaml"
    good.write_text("version: 1\nescalation:\n  default: escalate\n", encoding="utf-8")
    target = tmp_path / "custom.json"
    assert _cli(["triage", "--ref", str(ref_png), "--ours", str(ours_png),
                 "--rules", str(good), "--out", str(tmp_path / "o2"),
                 "--json", str(target)]) == 0
    assert target.is_file()
    assert json.loads(target.read_text(encoding="utf-8"))["counts"]["escalate"] == 0
    assert not (tmp_path / "o2" / "escalations").exists()


def test_selftest_passes(capsys):
    assert T.main(["--selftest"]) == 0
    out = capsys.readouterr().out
    assert "PASS" in out and "FAIL" not in out


# ---------------------------------------------------------------- 仓库示例规则

def test_example_rules_file_loads():
    ruleset = T.load_rules(EXAMPLE_RULES)
    ids = [r["list_item_id"] for r in ruleset["allow"]]
    assert "2034-AL-01" in ids and "2034-AL-02" in ids
    judgements = {r["judgement"] for r in ruleset["escalate"]}
    assert {"CFG-01", "CFG-02", "CFG-03"} <= judgements
    assert ruleset["escalation"]["default"] == T.TIER_ESCALATE


@pytest.mark.skipif(T2A_ROOT is None, reason="2034 T2a products not available on this machine")
def test_example_rules_replay_the_2034_first_round(tmp_path, capsys):
    """回放测试：首轮错误模型的分诊表——构型级判升级、两条允许差挂清单条目号。"""
    out = tmp_path / "h1"
    code = _cli(["triage",
                 "--ref", os.path.join(T2A_ROOT, "fig3b_base.feat.json"),
                 "--ours", os.path.join(T2A_ROOT, "fig3b_ours.feat.json"),
                 "--rules", EXAMPLE_RULES, "--out", str(out)])
    assert code == 0
    payload = json.loads((out / "triage.json").read_text(encoding="utf-8"))
    rows = payload["rows"]
    by_kind = {}
    for row in rows:
        by_kind.setdefault(row["kind"], []).append(row)
    assert by_kind["background_kind"][0]["basis"] == {"list_item_id": "2034-AL-01"}
    allowed = [r for r in rows if r["tier"] == T.TIER_ALLOW]
    assert {r["basis"]["list_item_id"] for r in allowed} == {"2034-AL-01", "2034-AL-02"}
    explicit = [r for r in rows if r["hit"] == "explicit"]
    assert {r["basis"]["judgement"] for r in explicit} == {"CFG-01", "CFG-02"}
    notes = sorted((out / "escalations").glob("ESC-*.md"))
    assert len(notes) == len(payload["escalations"]) >= 2
