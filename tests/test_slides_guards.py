# -*- coding: utf-8 -*-
"""``primer-slides`` 的两道关卡：不静默覆盖人改过的骨架，以及生成前的校验。

* 覆盖守卫：``outline`` 第二次运行拒绝写，``--force`` 丢弃人改、``--merge`` 只刷新派生量；
* ``check``：读回骨架，比对出生指纹、对账预算与候选、逐节核对目录，error 级发现以非零码
  退出，warning 级不挡——这份报告是人在生成之前要读的那份。

夹具沿用 ``test_slides_outline`` 里那本合成的"小书"（三个篇、四个章、一份目录与日志）。
"""

from __future__ import annotations

import hashlib
import os

import pytest
import yaml

from test_slides_outline import book, config  # noqa: F401  夹具与参数工厂

from primer.slides.__main__ import build_parser, main
from primer.slides import candidates as cand
from primer.slides.fingerprint import fingerprint_differences
from primer.slides.outline import run
from primer.slides.plan import DEFAULT_CAPACITY_PER_PAGE, SlidesError
from primer.slides.validate import (
    check_outline,
    check_report_lines,
    load_candidates,
    validate_outline,
)

OUTLINE_REL = ("_primer", "slides", "review-seminar", "outline.yaml")
CANDIDATES_REL = ("_primer", "slides", "review-seminar", "candidates.md")


def _outline_path(root):
    return root.joinpath(*OUTLINE_REL)


def _candidates_path(root):
    return root.joinpath(*CANDIDATES_REL)


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _dump(path, document):
    path.write_text(
        yaml.safe_dump(
            document,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
            width=10**6,
        ),
        encoding="utf-8",
    )


def _codes(findings, severity=None):
    return {
        finding.code
        for finding in findings
        if severity is None or finding.severity == severity
    }


# ---------------------------------------------------------------- 覆盖守卫


def test_outline_refuses_to_overwrite_an_existing_outline(book):
    root, spec = book
    _, first = run(root, config(), spec_path=spec)
    before = [_digest(path) for path in first]

    with pytest.raises(SlidesError) as excinfo:
        run(root, config(), spec_path=spec)

    message = str(excinfo.value)
    assert "已存在；拒绝覆盖" in message
    assert "--force" in message and "--merge" in message
    # 报的是产物边界内的相对路径，不落绝对路径
    assert "review-seminar/outline.yaml" in message
    assert str(root) not in message
    # 拒绝之后两个文件一个字节都没动
    assert before == [_digest(path) for path in first]


def test_cli_refuses_the_second_run_with_a_nonzero_exit(book, capsys):
    root, spec = book
    assert main(["outline", "--project-root", str(root), "--spec", str(spec)]) == 0
    capsys.readouterr()

    code = main(["outline", "--project-root", str(root), "--spec", str(spec)])

    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""
    assert "已存在；拒绝覆盖" in captured.err
    assert "--force" in captured.err and "--merge" in captured.err


def test_force_overwrites_and_regeneration_stays_byte_identical(book):
    root, spec = book
    _, first = run(root, config(), spec_path=spec)
    before = [_digest(path) for path in first]

    _, second = run(root, config(), spec_path=spec, force=True)

    assert first == second
    assert before == [_digest(path) for path in second]


def test_force_and_merge_are_mutually_exclusive():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["outline", "--force", "--merge"])


def test_outline_help_states_what_force_and_merge_do():
    parser = build_parser()
    outline = parser._subparsers._group_actions[0].choices["outline"].format_help()
    flat = " ".join(outline.split())

    assert "discarding your edits to budget, sections[].speak and picks" in flat
    assert "keeping budget, sections[].speak and picks" in flat


# ---------------------------------------------------------------- merge


def test_merge_keeps_human_edits_and_refreshes_the_derived_fields(book):
    root, spec = book
    run(root, config(), spec_path=spec)
    outline_path = _outline_path(root)
    document = _load(outline_path)
    # 人改几处：预算、逐帧的 picks／review／note、讲不讲；再把派生字段改乱，好看清 merge 刷了哪些。
    document["chapters"][0]["budget"] = 5
    document["chapters"][0]["frames"][0]["picks"] = ["s1-0p01"]
    document["chapters"][0]["frames"][0]["review"] = "ok"
    document["chapters"][0]["frames"][0]["note"] = "只讲这一句"
    document["chapters"][0]["candidates"] = 999
    document["chapters"][0]["material_chars"] = 999
    document["chapters"][1]["sections"][0]["speak"] = False
    document["chapters"][1]["budget_default"] = 999
    document["fingerprint"]["structure_sha256"] = "deadbeefdead"
    _dump(outline_path, document)

    plan, _ = run(root, config(), spec_path=spec, merge=True)
    merged = _load(outline_path)

    assert merged["chapters"][0]["budget"] == 5
    # 预算从 3 页改成 5 页：frames 跟着补到 5 帧，人填的第一帧逐字保留
    assert len(merged["chapters"][0]["frames"]) == 5
    assert merged["chapters"][0]["frames"][0] == {
        "picks": ["s1-0p01"],
        "review": "ok",
        "note": "只讲这一句",
    }
    assert merged["chapters"][1]["sections"][0]["speak"] is False
    # 派生量以本次算出为准
    assert merged["chapters"][0]["candidates"] != 999
    assert merged["chapters"][0]["material_chars"] != 999
    assert merged["chapters"][1]["budget_default"] != 999
    assert merged["fingerprint"]["structure_sha256"] != "deadbeefdead"
    assert any("kept budget" in note for note in plan.merge_notes)


def test_merge_reports_chapters_that_left_and_join_the_book(book):
    root, spec = book
    run(root, config(), spec_path=spec)
    outline_path = _outline_path(root)
    document = _load(outline_path)
    # 旧骨架里删掉第二章（书变了，新书里它重新出现），再塞一个书里没有的章。
    document["chapters"].pop(1)
    document["chapters"].append(
        {"chapter": "99", "label": "第九九章", "budget": 3, "sections": [], "picks": []}
    )
    _dump(outline_path, document)

    plan, _ = run(root, config(), spec_path=spec, merge=True)
    notes = "\n".join(plan.merge_notes)
    merged = _load(outline_path)

    assert "第二章" in notes and "new in the book" in notes
    assert "第九九章" in notes and "dropped" in notes
    assert "99" not in {chapter["chapter"] for chapter in merged["chapters"]}
    assert "第二章" in [chapter["label"] for chapter in merged["chapters"]]


# ---------------------------------------------------------------- 默认预算尾注释


def test_each_default_budget_carries_its_derivation_as_a_trailing_comment(book):
    root, spec = book
    plan, (_, outline_path) = run(root, config(), spec_path=spec)
    text = outline_path.read_text(encoding="utf-8")
    materials = plan.candidate_chars
    total = sum(materials)

    for chapter, budget, chars in zip(plan.structure.chapters, plan.budgets, materials):
        assert (
            f"  budget: {budget}   # 默认 {budget}：候选 {chars} 字 / {total} 字"
            in text
        )
    # 是一条注释，不是新字段：解析回来不该多出任何预算注释键
    assert "budget_comment" not in _load(outline_path)


# ---------------------------------------------------------------- 出生指纹


def test_outline_records_a_fingerprint_of_the_spec_and_the_sources(book):
    root, spec = book
    plan, (_, outline_path) = run(root, config(), spec_path=spec)
    fingerprint = _load(outline_path)["fingerprint"]

    assert set(fingerprint) == {"spec", "spec_sha256", "structure_sha256", "sources"}
    assert fingerprint["spec"] == "_primer/slides/review-seminar/deck.yaml"
    assert len(fingerprint["spec_sha256"]) == 12
    assert len(fingerprint["structure_sha256"]) == 12
    paths = [entry["path"] for entry in fingerprint["sources"]]
    expected: list = []
    for chapter in plan.structure.chapters:
        if chapter.source not in expected:
            expected.append(chapter.source)
    assert paths == expected
    # 源文件记的是内容哈希，不是 mtime——mtime 会被一次 touch 弄成假差异
    assert all(len(entry["sha256"]) == 12 for entry in fingerprint["sources"])


# ---------------------------------------------------------------- check：通过


def test_fingerprint_differences_split_structure_drift_from_pool_drift():
    """纯比对：内容一致则无差异；规格与结构进 error，候选池字段进 warning。"""
    stored = {
        "spec": "_primer/slides/d/deck.yaml",
        "spec_sha256": "aaaa",
        "structure_sha256": "bbbb",
        "sources": [{"path": "成果文件/甲.md", "sha256": "cccc"}],
    }
    identical = {
        "spec": "_primer/slides/d/deck.yaml",
        "spec_sha256": "aaaa",
        "structure_sha256": "bbbb",
        "sources": [{"path": "成果文件/甲.md", "sha256": "cccc"}],
    }
    assert fingerprint_differences(stored, identical) == []

    spec_changed = dict(identical, spec_sha256="dddd")
    structure_changed = dict(identical, structure_sha256="eeee")
    source_changed = dict(identical, sources=[{"path": "成果文件/甲.md", "sha256": "ffff"}])

    assert all(d.severity == "error" for d in fingerprint_differences(stored, spec_changed))
    assert all(d.severity == "error" for d in fingerprint_differences(stored, structure_changed))
    assert all(d.severity == "warning" for d in fingerprint_differences(stored, source_changed))
    assert [d.code for d in fingerprint_differences(stored, source_changed)] == ["source_sha256"]


def test_fingerprint_differences_report_a_skeleton_without_one():
    differences = fingerprint_differences(None, {"spec": "x"})

    assert [item.code for item in differences] == ["fingerprint"]
    assert differences[0].severity == "error"


def test_check_passes_a_fresh_outline_with_only_pick_count_warnings(book, capsys):
    root, spec = book
    run(root, config(), spec_path=spec)

    code = main(
        ["check", str(_outline_path(root)), "--project-root", str(root)]
    )
    captured = capsys.readouterr()

    assert code == 0
    assert "[pick-count]" in captured.out
    # 一帧一条警告：预算 3+2+2+5 = 12 帧
    assert "警告 (12)" in captured.out
    assert "帧 第一章 第 1 页：选取 0，预期 3–5" in captured.out
    for other in (
        "stale-outline",
        "budget-sum",
        "budget-range",
        "frame-count",
        "pick-missing",
        "slide-overflow",
        "capacity-overflow",
        "review-value",
        "hint-shape",
        "unknown-section",
    ):
        assert f"[{other}]" not in captured.out
    assert "通过：0 个错误，12 个警告" in captured.out


def test_check_blocks_on_an_unparsable_theme_block(book, capsys):
    """主题块读不出来（字号不是数字、token 认不出）是 error。"""
    root, spec = book
    run(root, config(), spec_path=spec)
    outline_path = _outline_path(root)
    document = _load(outline_path)
    document["theme"]["type"]["body"]["size_pt"] = "22pt"
    document["theme"]["palette"]["ink"] = "1a1a1a"
    document["theme"]["palette"]["highlight"] = "#ff0000"
    _dump(outline_path, document)

    code = main(["check", str(outline_path), "--project-root", str(root)])
    out = capsys.readouterr().out

    assert code == 2
    assert "[theme-type]" in out and "size_pt" in out
    assert "[theme-token]" in out and "token 'highlight'" in out
    assert "1a1a1a" in out


def test_check_accepts_a_theme_block_that_only_changes_one_value(book, capsys):
    """缺的键取默认值：只手写一段 palette 也是合法的。"""
    root, spec = book
    run(root, config(), spec_path=spec)
    outline_path = _outline_path(root)
    document = _load(outline_path)
    document["theme"] = {"palette": {"accent": "#b03030"}}
    _dump(outline_path, document)

    code = main(["check", str(outline_path), "--project-root", str(root)])
    out = capsys.readouterr().out

    assert code == 0
    assert "[theme-" not in out
    _, _, findings = check_outline(outline_path, root)
    assert "theme-token" not in _codes(findings)


def test_merge_keeps_a_hand_edited_theme_block(book):
    root, spec = book
    run(root, config(), spec_path=spec)
    outline_path = _outline_path(root)
    document = _load(outline_path)
    document["theme"]["palette"]["accent"] = "#b03030"
    document["theme"]["type"]["title"]["size_pt"] = 32
    document["theme"]["canvas"]["width_bp"] = 1024
    _dump(outline_path, document)

    plan, _ = run(root, config(), spec_path=spec, merge=True)
    merged = _load(outline_path)

    assert merged["theme"]["palette"]["accent"] == "#b03030"
    assert merged["theme"]["type"]["title"]["size_pt"] == 32
    assert merged["theme"]["canvas"]["width_bp"] == 1024
    assert any("kept theme" in note for note in plan.merge_notes)


def test_merge_fills_the_default_theme_when_the_old_outline_has_none(book):
    root, spec = book
    run(root, config(), spec_path=spec)
    outline_path = _outline_path(root)
    document = _load(outline_path)
    document.pop("theme")
    _dump(outline_path, document)

    plan, _ = run(root, config(), spec_path=spec, merge=True)
    merged = _load(outline_path)

    assert merged["theme"]["canvas"]["width_bp"] == 960
    assert merged["theme"]["type"]["body"]["size_pt"] == 22
    assert any("no theme block" in note for note in plan.merge_notes)


def test_check_writes_nothing(book):
    root, spec = book
    run(root, config(), spec_path=spec)
    outline_before = _digest(_outline_path(root))
    candidates_before = _digest(_candidates_path(root))

    check_outline(_outline_path(root), root)

    assert _digest(_outline_path(root)) == outline_before
    assert _digest(_candidates_path(root)) == candidates_before


def test_touching_the_spec_without_changing_it_does_not_stale_the_outline(book):
    root, spec = book
    run(root, config(), spec_path=spec)
    stamp = spec.stat().st_mtime
    os.utime(spec, (stamp + 3600, stamp + 3600))

    _, _, findings = check_outline(_outline_path(root), root)

    # 内容哈希看不见 touch
    assert "stale-outline" not in _codes(findings)


def test_touching_a_source_without_changing_it_does_not_stale_the_outline(book):
    root, spec = book
    run(root, config(), spec_path=spec)
    source = root / "成果文件" / "第一篇_甲篇.md"
    stamp = source.stat().st_mtime
    os.utime(source, (stamp + 120, stamp + 120))

    _, _, findings = check_outline(_outline_path(root), root)

    assert "stale-outline" not in _codes(findings)


def test_check_errors_when_the_deck_spec_changed(book):
    root, spec = book
    run(root, config(), spec_path=spec)
    spec.write_text(
        spec.read_text(encoding="utf-8").replace("appendix_label: 附录 {number}",
                                                "appendix_label: 附录 {number}　"),
        encoding="utf-8",
    )

    _, _, findings = check_outline(_outline_path(root), root)

    stale = [finding for finding in findings if finding.code == "stale-outline"]
    assert stale and all(finding.severity == "error" for finding in stale)
    assert any("deck.yaml 已变" in finding.message for finding in stale)


def test_check_errors_when_a_source_changes_the_structure(book):
    """加一节 = 结构变了：error，挡住生成（picks 的归属可能已经对不上）。"""
    root, spec = book
    run(root, config(), spec_path=spec)
    source = root / "成果文件" / "第一篇_甲篇.md"
    source.write_text(
        source.read_text(encoding="utf-8").replace(
            "### 1.1.2 节二", "### 1.1.2 新插进来的一节\n\n插进来的第一句。\n\n### 1.1.3 节二"
        ),
        encoding="utf-8",
    )

    _, _, findings = check_outline(_outline_path(root), root)

    stale = [finding for finding in findings if finding.code == "stale-outline"]
    assert any(finding.severity == "error" and "结构已变" in finding.message for finding in stale)


def test_check_warns_when_a_candidate_source_content_changed(book):
    root, spec = book
    run(root, config(), spec_path=spec)
    source = root / "成果文件" / "第一篇_甲篇.md"
    source.write_text(
        source.read_text(encoding="utf-8") + "\n另起一段补充。\n", encoding="utf-8"
    )

    _, _, findings = check_outline(_outline_path(root), root)

    stale = [finding for finding in findings if finding.code == "stale-outline"]
    # 候选池变了、页号没变：只提醒，不挡
    assert stale and all(finding.severity == "warning" for finding in stale)
    assert any("第一篇_甲篇.md" in finding.message for finding in stale)


def test_check_flags_an_outline_without_a_fingerprint(book):
    root, spec = book
    run(root, config(), spec_path=spec)
    outline_path = _outline_path(root)
    document = _load(outline_path)
    document.pop("fingerprint")
    _dump(outline_path, document)

    _, _, findings = check_outline(outline_path, root)

    assert "stale-outline" in _codes(findings, "error")


# ---------------------------------------------------------------- check：拦下


def test_check_blocks_on_frame_count_and_section_problems(book, capsys):
    root, spec = book
    run(root, config(), spec_path=spec)
    outline_path = _outline_path(root)
    document = _load(outline_path)
    # 预算加 2 页却不补帧：一帧一页，长度必须相等
    document["chapters"][0]["budget"] += 2
    document["chapters"][0]["frames"][0]["picks"] = ["s1-99p01"]
    document["chapters"][0]["sections"].append(
        {"level": "section", "number": "1.99", "title": "不存在的节", "speak": True, "hint": ""}
    )
    _dump(outline_path, document)

    code = main(["check", str(outline_path), "--project-root", str(root)])
    captured = capsys.readouterr()

    assert code == 2
    assert "不通过：" in captured.out
    assert "[frame-count]" in captured.out
    assert "[pick-missing]" in captured.out
    assert "[unknown-section]" in captured.out
    # 报出的是人认得的名目，不是裸数字
    assert "第一章" in captured.out and "s1-99p01" in captured.out


# ---------------------------------------------------------------- 判据（纯函数）


def _frame(picks=(), review="pending", note=""):
    return {"picks": list(picks), "review": review, "note": note}


def _base_outline(**deck):
    merged = {"backup": 2, "capacity_per_page": DEFAULT_CAPACITY_PER_PAGE,
              "min_per_chapter": 1, "max_per_chapter": 5}
    merged.update(deck)
    budget = 12
    return {
        "version": 2,
        "deck": merged,
        "chapters": [
            {
                "chapter": "1",
                "label": "第一章",
                "budget": budget,
                "hint": "",
                "frames": [_frame() for _ in range(budget)],
                "sections": [{"number": "1.1", "hint": ""}],
            }
        ],
    }


def _frames(outline, picks=(), review="pending", note="", budget=1):
    """把基础骨架改成"预算 budget 页、第一帧放 picks"的形状，供逐帧判据测试用。"""
    chapter = outline["chapters"][0]
    chapter["budget"] = budget
    chapter["frames"] = [_frame(picks, review, note)] + [
        _frame() for _ in range(max(budget - 1, 0))
    ]
    return outline


def test_validate_never_reconciles_the_budget_against_a_total():
    """budget 是唯一的控制项：加两页不会报 budget-sum，报告里只报加出来的总数。"""
    outline = _base_outline()
    outline["chapters"][0]["budget"] = 14
    outline["chapters"][0]["frames"] = [_frame() for _ in range(14)]

    findings = validate_outline(outline, candidates={}, known_sections={"1.1": 5})

    assert "budget-sum" not in _codes(findings)
    lines = "\n".join(check_report_lines(outline, findings, {}))
    assert "正文 14 页 = 各章预算之和" in lines
    assert "固定页 11 页 → 全片 25 页" in lines


def test_validate_flags_a_negative_or_a_non_integer_budget():
    outline = _base_outline()
    outline["chapters"][0]["budget"] = -1
    assert "budget-range" in _codes(
        validate_outline(outline, candidates={}, known_sections={"1.1": 5}), "error"
    )

    # 超出默认建议的上下限不是错误：那是人定的数，工具不替他对账
    outline["chapters"][0]["budget"] = 9
    outline["chapters"][0]["frames"] = [_frame() for _ in range(9)]
    findings = validate_outline(outline, candidates={}, known_sections={"1.1": 5})
    assert "budget-range" not in _codes(findings)

    outline["chapters"][0]["budget"] = "五页"
    assert "budget-range" in _codes(
        validate_outline(outline, candidates={}, known_sections={"1.1": 5}), "error"
    )


def test_validate_names_a_missing_pick_and_its_chapter():
    outline = _frames(_base_outline(), ["s1-0p01"])

    finding = next(
        f for f in validate_outline(outline, candidates={}, known_sections={"1.1": 5})
        if f.code == "pick-missing"
    )
    assert finding.severity == "error"
    assert "第一章" in finding.message and "s1-0p01" in finding.message


def test_validate_pick_count_warns_when_thin_and_errors_when_too_many():
    """条数改成逐帧报：消息点得出是"哪一章的第几页"。"""
    thin = _frames(_base_outline(), [])
    warning = next(
        f for f in validate_outline(thin, candidates={}, known_sections={"1.1": 5})
        if f.code == "pick-count"
    )
    assert warning.severity == "warning"
    # 一帧 3–5 句：这一帧 0 句
    assert warning.message == "帧 第一章 第 1 页：选取 0，预期 3–5；若这一页刻意精简，少一些也可以"

    fat = _frames(_base_outline(), ["a", "b", "c", "d", "e", "f"])
    heavy = next(
        f for f in validate_outline(fat, candidates={}, known_sections={"1.1": 5})
        if f.code == "pick-count"
    )
    assert heavy.severity == "error"
    assert heavy.message == "帧 第一章 第 1 页：选取 6，预期 3–5；多于预期，放不下"

    # 上限之内不报：一帧选 5 个正好是上限
    ok = _frames(_base_outline(), ["a", "b", "c", "d", "e"])
    assert "pick-count" not in _codes(
        validate_outline(ok, candidates={}, known_sections={"1.1": 5})
    )


def test_validate_reports_a_frame_count_that_does_not_match_the_budget():
    outline = _base_outline()
    outline["chapters"][0]["frames"] = [_frame(), _frame()]  # 预算 12 页，却只有 2 帧

    finding = next(
        f for f in validate_outline(outline, candidates={}, known_sections={"1.1": 5})
        if f.code == "frame-count"
    )
    assert finding.severity == "error"
    assert "第一章" in finding.message
    assert "frames 2 帧" in finding.message and "预算 12 页" in finding.message


def test_validate_reports_the_old_skeleton_once_and_actionably():
    """整份旧的、没有 frames 的骨架：只报一条 frame-count，且指明 --merge。"""
    outline = _base_outline()
    chapter = outline["chapters"][0]
    chapter.pop("frames")
    chapter.pop("hint")
    chapter["picks"] = ["s1-0p01"]

    findings = validate_outline(
        outline, candidates={"s1-0p01": 5}, known_sections={"1.1": 5}
    )
    frame_count = [f for f in findings if f.code == "frame-count"]

    assert len(frame_count) == 1 and frame_count[0].severity == "error"
    assert "--merge" in frame_count[0].message
    # 不连带喷一屏别的错：pick／hint 都不再逐帧判
    assert "hint-shape" not in _codes(findings)
    assert "pick-count" not in _codes(findings)
    assert "pick-missing" not in _codes(findings)


def test_validate_reports_a_review_value_outside_the_three_states():
    outline = _frames(_base_outline(), [], review="done")

    finding = next(
        f for f in validate_outline(outline, candidates={}, known_sections={"1.1": 5})
        if f.code == "review-value"
    )
    assert finding.severity == "error"
    assert "帧 第一章 第 1 页" in finding.message
    assert "'done'" in finding.message
    assert "pending、ok、redo" in finding.message


def test_validate_reports_a_hint_or_note_that_is_not_a_string():
    # 章级 hint 缺失
    missing_hint = _base_outline()
    missing_hint["chapters"][0].pop("hint")
    finding = next(
        f for f in validate_outline(missing_hint, candidates={}, known_sections={"1.1": 5})
        if f.code == "hint-shape"
    )
    assert finding.severity == "error" and "缺" in finding.message

    # 帧的 note 不是字符串
    bad_note = _base_outline()
    bad_note["chapters"][0]["frames"][0]["note"] = 7
    assert any(
        f.code == "hint-shape" and "note" in f.message
        for f in validate_outline(bad_note, candidates={}, known_sections={"1.1": 5})
    )

    # 节级 hint 不是字符串
    bad_section = _base_outline()
    bad_section["chapters"][0]["sections"][0]["hint"] = 3
    assert any(
        f.code == "hint-shape" and "节" in f.message
        for f in validate_outline(bad_section, candidates={}, known_sections={"1.1": 5})
    )


def test_validate_warns_when_one_frame_holds_too_many_characters():
    """一帧的几句加起来超过一页容量：逐帧判定。"""
    outline = _frames(
        _base_outline(), ["s1-0p01", "s1-0p02", "s1-0p03", "s1-0p04", "s1-0p05"]
    )

    findings = validate_outline(
        outline,
        candidates={f"s1-0p0{n}": 100 for n in range(1, 6)},
        known_sections={"1.1": 5},
    )

    overflow = next(f for f in findings if f.code == "slide-overflow")
    assert overflow.severity == "warning"
    assert overflow.message.startswith("帧 第一章 第 1 页：选取 ")
    assert (
        "s1-0p01（100） + s1-0p02（100） + s1-0p03（100） + s1-0p04（100） + s1-0p05（100） = 500"
        in overflow.message
    )
    assert f"超过 capacity_per_page {DEFAULT_CAPACITY_PER_PAGE}" in overflow.message


def test_validate_errors_when_the_picked_chars_exceed_the_capacity():
    outline = _frames(_base_outline(), ["s1-0p01"], budget=12)

    findings = validate_outline(
        outline, candidates={"s1-0p01": 9999}, known_sections={"1.1": 5}
    )

    capacity = next(f for f in findings if f.code == "capacity-overflow")
    assert capacity.severity == "error"
    assert "9999" in capacity.message
    assert str(DEFAULT_CAPACITY_PER_PAGE * 12) in capacity.message


def test_validate_flags_a_section_that_is_not_in_the_toc():
    outline = _base_outline()
    outline["chapters"][0]["sections"] = [{"number": "9.9", "hint": ""}]

    finding = next(
        f for f in validate_outline(outline, candidates={}, known_sections={"1.1": 5})
        if f.code == "unknown-section"
    )
    assert finding.severity == "error" and "9.9" in finding.message


def test_validate_ignores_a_section_with_an_empty_number():
    outline = _base_outline()
    outline["chapters"][0]["sections"] = [{"number": "", "hint": ""}]

    assert "unknown-section" not in _codes(
        validate_outline(outline, candidates={}, known_sections={"1.1": 5})
    )


def _granularity_outline(frames_per_frame):
    """把基础骨架改成"若干帧，每帧给定的 picks"，供颗粒度判据测试用。"""
    outline = _base_outline()
    chapter = outline["chapters"][0]
    chapter["budget"] = len(frames_per_frame)
    chapter["frames"] = [_frame(picks) for picks in frames_per_frame]
    return outline


def test_validate_warns_when_a_frame_is_far_off_the_median_granularity():
    """一帧比别的帧少得多：报一张"这一帧的数与全片中位数"。"""
    steady = ["甲乙丙丁戊己庚辛壬癸"] * 4  # 4 条 × 10 字 = 40 字
    outline = _granularity_outline([steady, steady, steady, ["甲乙丙丁戊"]])

    findings = validate_outline(outline, candidates={}, known_sections={"1.1": 5})

    granularity = [f for f in findings if f.code == "frame-granularity"]
    assert len(granularity) == 1
    finding = granularity[0]
    assert finding.severity == "warning"
    assert "帧 第一章 第 4 页" in finding.message
    # 这一帧的数与中位数都写在报里
    assert "条数 1（中位 4）" in finding.message
    assert "首条 5 字（中位 10）" in finding.message
    assert "总字数 5（中位 40）" in finding.message
    assert "条数、总字数与全片颗粒度不一致" in finding.message


def test_validate_warns_when_a_frames_first_pick_is_too_long():
    long_lead = "甲乙丙丁戊己庚辛壬癸" * 6  # 60 字，超过阈值 48
    steady = ["甲乙丙丁戊己庚辛壬癸"] * 4
    outline = _granularity_outline([[long_lead, *steady[:3]], steady, steady])

    findings = validate_outline(outline, candidates={}, known_sections={"1.1": 5})

    finding = next(f for f in findings if f.code == "frame-granularity")
    assert finding.severity == "warning"
    assert "首条 60 字" in finding.message
    assert "首条偏长" in finding.message


def test_validate_stays_quiet_when_the_frames_are_of_one_grain():
    steady = ["甲乙丙丁戊己庚辛壬癸"] * 4
    outline = _granularity_outline([steady, steady, steady])

    assert "frame-granularity" not in _codes(
        validate_outline(outline, candidates={}, known_sections={"1.1": 5})
    )


def test_validate_does_not_judge_granularity_on_frames_with_no_picks():
    """还没挑的帧由 pick-count 负责，不拿空帧算中位数、也不报颗粒度。"""
    assert "frame-granularity" not in _codes(
        validate_outline(_base_outline(), candidates={}, known_sections={"1.1": 5})
    )


def test_load_candidates_reads_ids_and_lengths_from_the_table(book):
    root, spec = book
    run(root, config(), spec_path=spec)

    candidates = load_candidates(_candidates_path(root))

    assert candidates["s1-0p01"] > 0
    assert all(isinstance(value, int) for value in candidates.values())
    assert "id" not in candidates and "类型" not in candidates


# ---------------------------------------------------------------- pick 的三种形状


def test_parse_pick_reads_the_three_shapes():
    candidates = {"s2-4p01": 30, "sA-3p02": 40}

    identifier = cand.parse_pick("s2-4p01", candidates)
    assert identifier.is_candidate and identifier.identifier == "s2-4p01"
    assert identifier.refs() == ("s2-4p01",)

    free = cand.parse_pick("手写的一句。", candidates)
    assert free.kind == cand.PICK_TEXT and free.text == "手写的一句。"
    assert not free.derived and free.refs() == ()

    derived = cand.parse_pick(
        {"text": "提炼出的一句。", "derived_from": ["s2-4p01", "sA-3p02"], "verbatim": True},
        candidates,
    )
    # 映射里多出来的键（verbatim）为下一轮留位置，一律忽略
    assert derived.derived and derived.derived_from == ("s2-4p01", "sA-3p02")
    assert derived.refs() == ("s2-4p01", "sA-3p02")


def test_parse_pick_treats_an_empty_derived_from_as_free_text():
    assert cand.parse_pick({"text": "还是手写的。", "derived_from": []}, {}).derived is False
    assert cand.parse_pick({"text": "还是手写的。"}, {}).derived is False


def test_parse_pick_rejects_empty_and_wrong_shaped_entries():
    for blank in ("", "   ", {"text": ""}, {"text": "  "}):
        with pytest.raises(cand.PickError) as empty:
            cand.parse_pick(blank, {})
        assert empty.value.code == "pick-empty"

    for bad in (3, None, ["s2-4p01"], True):
        with pytest.raises(cand.PickError) as shape:
            cand.parse_pick(bad, {})
        assert shape.value.code == "pick-shape"


def test_parse_pick_reports_a_derived_from_id_that_is_not_a_candidate():
    with pytest.raises(cand.PickError) as missing:
        cand.parse_pick({"text": "提炼。", "derived_from": ["s2-99p01"]}, {"s2-4p01": 30})

    assert missing.value.code == "pick-missing" and "s2-99p01" in missing.value.message


def test_pick_display_and_length_cover_all_three_shapes():
    rows = {
        "s2-4p01": cand.CandidateRow(
            id="s2-4p01",
            type="断言",
            signal_codes="B",
            citations=0,
            length=30,
            pointer="p.7（§2.4 起始页）",
            display="**候选句。**",
        )
    }
    lengths = {"s2-4p01": 30}

    candidate = cand.parse_pick("s2-4p01", rows)
    assert cand.pick_display(candidate, rows) == "**候选句。**"
    assert cand.pick_length(candidate, lengths) == 30

    free = cand.parse_pick("手写的一句。", rows)
    assert cand.pick_display(free, rows) == "手写的一句。"
    assert cand.pick_length(free, lengths) == 6

    derived = cand.parse_pick({"text": "提炼。", "derived_from": ["s2-4p01"]}, rows)
    assert cand.pick_display(derived, rows) == "提炼。"
    assert cand.pick_length(derived, lengths) == 3


def test_pick_display_normalizes_straight_quotes_with_the_book_rule():
    """三种形状的显示文本都接成书同一套引号规整；奇数个的那一行原样保留。"""
    rows = {
        "s2-4p01": cand.CandidateRow(
            id="s2-4p01",
            type="断言",
            signal_codes="B",
            citations=0,
            length=30,
            pointer="p.7（§2.4 起始页）",
            display='从"条件判据"到"演化路径"。',
        )
    }

    candidate = cand.parse_pick("s2-4p01", rows)
    assert cand.pick_display(candidate, rows) == "从“条件判据”到“演化路径”。"

    free = cand.parse_pick('他只说了"开始。', rows)
    assert cand.pick_display(free, rows) == '他只说了"开始。'


def test_validate_flags_an_empty_pick():
    outline = _frames(_base_outline(), ["   "])

    finding = next(
        f
        for f in validate_outline(outline, candidates={}, known_sections={"1.1": 5})
        if f.code == "pick-empty"
    )
    assert finding.severity == "error" and "帧 第一章 第 1 页" in finding.message


def test_validate_flags_a_wrong_shaped_pick():
    for bad in (7, None, ["s1-0p01"]):
        outline = _frames(_base_outline(), [bad])
        finding = next(
            f
            for f in validate_outline(outline, candidates={}, known_sections={"1.1": 5})
            if f.code == "pick-shape"
        )
        assert finding.severity == "error"
        assert "既不是候选 id，也不是句子" in finding.message


def test_validate_keeps_pick_missing_for_a_mistyped_candidate_id():
    """形状像候选 id、表里却没有：照旧 pick-missing，消息指出章与帧。"""
    outline = _frames(_base_outline(), ["s1-0p01"])

    finding = next(
        f
        for f in validate_outline(outline, candidates={}, known_sections={"1.1": 5})
        if f.code == "pick-missing"
    )
    assert finding.message == (
        "帧 第一章 第 1 页 列出了候选 's1-0p01'，但 candidates.md 里没有这个 id"
    )


def test_validate_accepts_free_text_and_derived_sentences():
    outline = _frames(
        _base_outline(),
        ["手写的一句。", {"text": "提炼出来的一句。", "derived_from": ["s1-0p01"]}],
    )

    codes = _codes(
        validate_outline(outline, candidates={"s1-0p01": 30}, known_sections={"1.1": 5})
    )

    assert "pick-empty" not in codes
    assert "pick-shape" not in codes
    assert "pick-missing" not in codes


def test_free_text_counts_toward_the_picked_chars():
    """自由文本按 len(text) 计入选取字数，与候选表的口径一致。"""
    outline = _frames(_base_outline(capacity_per_page=10), ["字" * 121], budget=12)

    findings = validate_outline(outline, candidates={}, known_sections={"1.1": 5})

    capacity = next(f for f in findings if f.code == "capacity-overflow")
    assert "选取共 121 字" in capacity.message and "超过容量 120 字" in capacity.message


def test_check_accepts_free_text_on_a_fixed_page(book, capsys):
    """固定页上手写的句子不再当成"打错的候选 id"：走自由文本，不报 pick-missing。"""
    root, spec = book
    run(root, config(), spec_path=spec)
    document = _load(_outline_path(root))
    document["pages"][1]["picks"] = ["科学、工程与战略研判"]
    _dump(_outline_path(root), document)

    code = main(["check", str(_outline_path(root)), "--project-root", str(root)])
    out = capsys.readouterr().out

    assert code == 0
    assert "pick-missing" not in out


# ---------------------------------------------------------------- 指错工程根时的提示


def test_check_hints_when_the_project_root_is_wrong(book, capsys, tmp_path):
    root, spec = book
    run(root, config(), spec_path=spec)
    wrong = tmp_path / "not-the-root"
    wrong.mkdir()

    code = main(["check", str(_outline_path(root)), "--project-root", str(wrong)])
    out = capsys.readouterr().out

    assert code == 2
    assert "请用 --project-root" in out
    assert "都不存在" in out


def test_check_omits_the_project_root_hint_when_the_paths_resolve(book, capsys):
    root, spec = book
    run(root, config(), spec_path=spec)

    code = main(["check", str(_outline_path(root)), "--project-root", str(root)])
    out = capsys.readouterr().out

    assert code == 0
    assert "请用 --project-root" not in out
