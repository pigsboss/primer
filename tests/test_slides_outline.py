# -*- coding: utf-8 -*-
"""``primer.slides outline``：结构复算、默认预算、容量与取舍、候选抽取与确定性重生成。

夹具是一份合成的"小 deck"（两篇正常材料 + 一篇附录 + 三个源文件 + 一份 ``deck.yaml``），
**没有任何成书产物**——章、节、插图都由源 markdown 复算。它比真 deck 小，但结构齐全：
第二篇的章号从 3 起数、附录整篇一章且节是 ``## ``、源文件里有一行应被丢弃的编辑性尾注。

时间不在模型里，总页数也不在：``chapters[].budget`` 是唯一控制项，默认建议按候选材料
占比给出，分完推出的是容量与取舍，不是通过／不通过。
"""

from __future__ import annotations

import hashlib

import pytest
import yaml

from slides_fixtures import DECK_SPEC, VOLUME_ONE, deck_tree, write_spec, write_sources

from primer.slides import candidates as cand
from primer.slides.__main__ import build_parser, main
from primer.slides.outline import run
from primer.slides.plan import (
    DEFAULT_CAPACITY_PER_PAGE,
    DeckConfig,
    SlidesError,
    allocate,
    capacity_report,
    default_budgets,
    fixed_pages,
    frame_slots,
    page_entries,
)
from primer.slides.spec import DeckSpecError, load_spec, parse_spec
from primer.slides.structure import chinese_numeral, read_structure


@pytest.fixture
def book(tmp_path):
    """搭一份合成的小 deck，返回（工程根，deck.yaml 路径）。"""
    root = tmp_path / "工程"
    root.mkdir()
    spec = deck_tree(root)
    return root, spec


def config(**overrides):
    """控制参数：备份 2 页，每章至少 3 页（小夹具的材料少，默认会落到下限上）。

    ``min_per_chapter`` 在这里兼作"把默认预算抬高到看得见几帧"的旋钮——默认值本来就是按
    材料占比给的，材料少时它自然落到下限。
    """
    values = dict(backup=2, min_per_chapter=3, max_per_chapter=5)
    values.update(overrides)
    return DeckConfig(**values)


def spec_of(path):
    return load_spec(path, path.parents[3])


# ---------------------------------------------------------------- 结构复算


def test_chapters_come_from_the_source_headings(book):
    _, spec_path = book
    structure = read_structure(spec_of(spec_path))

    assert [
        (chapter.number, chapter.label, chapter.title, chapter.volume_id)
        for chapter in structure.chapters
    ] == [
        ("1", "第一章", "第一章标题", "V1"),
        ("2", "第二章", "第二章标题", "V1"),
        ("3", "第三章", "第三章标题", "V2"),
        ("A", "附录 A", "附录标题", "VA"),
    ]
    assert structure.book_line == "合成书（测试）"


def test_section_numbers_follow_the_chapter_and_restart_per_parent(book):
    _, spec_path = book
    structure = read_structure(spec_of(spec_path))

    listed = [
        (chapter.number, [(section.level, section.number) for section in chapter.sections])
        for chapter in structure.chapters
    ]
    assert listed == [
        ("1", [("section", "1.1"), ("section", "1.2")]),
        ("2", [("section", "2.1"), ("section", "2.2")]),
        ("3", [("section", "3.1")]),
        ("A", [("section", "A.1"), ("subsection", "A.1.1"), ("section", "A.2")]),
    ]
    assert structure.chapters[0].sections[0].title == "节一"
    assert structure.chapters[-1].sections[1].title == "附录小节"


def test_chinese_numerals_cover_the_chapter_labels():
    assert [chinese_numeral(number) for number in (1, 5, 10, 11, 14, 20, 21)] == [
        "一", "五", "十", "十一", "十四", "二十", "二十一",
    ]


def test_segments_carry_the_section_pointer_and_the_section_ordinal(book):
    _, spec_path = book
    structure = read_structure(spec_of(spec_path))
    first = structure.chapters[0]

    # 章首（第一节标题之前）的正文归这一章，指针写章标签
    assert first.segments[0].pointer == "第一章"
    assert first.segments[0].section_ordinal == 0
    pointers = [segment.pointer for segment in first.segments]
    assert pointers[1] == "§1.1"
    assert first.segments[-1].pointer == "§1.2"
    assert first.segments[-1].section_ordinal == 2


def test_pointers_none_sends_no_pointer_at_all(tmp_path):
    root = tmp_path / "工程"
    root.mkdir()
    write_sources(root)
    spec_path = write_spec(root, DECK_SPEC.replace("pointers: section", "pointers: none"))
    structure = read_structure(spec_of(spec_path))

    assert structure.pointers == "none"
    assert all(
        segment.pointer == "" for chapter in structure.chapters for segment in chapter.segments
    )


def test_drop_lines_keeps_the_editorial_tail_note_out(tmp_path):
    root = tmp_path / "工程"
    root.mkdir()
    spec_path = deck_tree(root)
    structure = read_structure(spec_of(spec_path))

    # 源文件末尾的 `*（第一篇完）*` 不该出现在任何一段正文里
    body = "\n".join(
        line for chapter in structure.chapters for segment in chapter.segments
        for line in segment.lines
    )
    assert "第一篇完" not in body


def test_material_chars_count_the_prose_only(book):
    _, spec_path = book
    structure = read_structure(spec_of(spec_path))

    assert all(chapter.material_chars > 0 for chapter in structure.chapters)
    assert structure.material_chars == sum(
        chapter.material_chars for chapter in structure.chapters
    )
    # 标题行不算：第一章的正文比它那些标题加起来长得多，也不含标题文本
    assert structure.chapters[0].material_chars < len(VOLUME_ONE)


def test_figures_are_numbered_in_source_order_within_the_chapter(book):
    _, spec_path = book
    structure = read_structure(spec_of(spec_path))

    first = [figure for figure in structure.chapters[0].figures()]
    appendix = [figure for figure in structure.chapters[-1].figures()]
    assert [(figure.number, figure.caption, figure.path) for figure in first] == [
        ("1-1", "示例图", "图/one.png"),
        ("1-2", "第二幅示例图", "图/two.png"),
    ]
    assert [(figure.number, figure.caption) for figure in appendix] == [("A-1", "附录示例图")]
    assert structure.chapters[1].figures() == ()


def test_a_source_without_chapter_headings_is_an_error(tmp_path):
    root = tmp_path / "工程"
    root.mkdir()
    write_sources(root, one="# 第一篇　甲篇\n\n只有一段正文。\n")
    spec_path = write_spec(root)

    with pytest.raises(SlidesError, match="no chapter heading"):
        read_structure(spec_of(spec_path))


# ---------------------------------------------------------------- 规格


def test_spec_rejects_unknown_keys():
    with pytest.raises(DeckSpecError, match="unknown key"):
        parse_spec(
            {
                "book": {"title": "T"},
                "sources": [],
                "book_title": "typo",
            },
            "deck.yaml",
            ".",
        )


def test_spec_requires_a_book_title_and_at_least_one_source():
    with pytest.raises(DeckSpecError, match="book is required"):
        parse_spec({"sources": [{"id": "V1"}]}, "deck.yaml", ".")
    with pytest.raises(DeckSpecError, match="sources must be a non-empty list"):
        parse_spec({"book": {"title": "T"}, "sources": []}, "deck.yaml", ".")


def test_spec_checks_the_pointer_mode_and_the_heading_levels():
    base = {
        "book": {"title": "T"},
        "sources": [{"id": "V1", "title": "篇", "path": "a.md", "chapter_start": 1}],
    }
    with pytest.raises(DeckSpecError, match="pointers must be one of"):
        parse_spec({**base, "pointers": "page"}, "deck.yaml", ".")
    with pytest.raises(DeckSpecError, match="section .* must be deeper"):
        parse_spec({**base, "split": {"chapter": 3, "section": 2}}, "deck.yaml", ".")
    with pytest.raises(DeckSpecError, match="must be a boolean"):
        parse_spec(
            {**base, "sources": [{**base["sources"][0], "appendix": "yes"}]}, "deck.yaml", "."
        )


def test_spec_requires_the_appendix_start_to_be_the_appendix_number():
    base = {
        "book": {"title": "T"},
        "sources": [{"id": "VA", "title": "篇", "path": "a.md", "appendix": True}],
    }
    with pytest.raises(DeckSpecError, match="appendix number"):
        parse_spec({**base, "sources": [{**base["sources"][0], "chapter_start": 3}]}, "deck.yaml", ".")
    with pytest.raises(DeckSpecError, match="chapter_start is required"):
        parse_spec(base, "deck.yaml", ".")


def test_spec_reports_a_missing_source_file(tmp_path):
    write_sources(tmp_path)
    spec_path = write_spec(tmp_path)
    (tmp_path / "成果文件" / "第二篇_乙篇.md").unlink()

    with pytest.raises(DeckSpecError, match="not found"):
        load_spec(spec_path, tmp_path)


# ---------------------------------------------------------------- 默认预算与页数


def test_default_budgets_follow_the_material_share_inside_the_bounds():
    budgets, body = default_budgets([900, 100], 336, 1, 5)

    # 900 + 100 = 1000 字材料 ÷ 一页 336 字 → 3 页；再按占比分：2.7 / 0.3 → 2 / 1
    assert body == 3
    assert budgets == (2, 1)
    assert sum(budgets) == body


def test_default_budgets_never_fall_outside_the_bounds():
    # 材料很少：默认值落到下限；材料极多：被上限收住
    assert default_budgets([1, 1], 336, 2, 5) == ((2, 2), 4)
    assert default_budgets([10**6, 10**6], 336, 1, 5) == ((5, 5), 10)


def test_allocate_sums_to_the_body_budget_exactly():
    assert allocate([8, 4, 4, 4], 12, 1, 5) == (5, 3, 2, 2)


def test_allocate_respects_floor_and_cap():
    assert allocate([10, 10, 10], 10, 1, 5) == (4, 3, 3)
    assert allocate([100, 1], 5, 1, 3) == (3, 2)
    assert allocate([1, 10], 3, 1, 5) == (1, 2)


def test_allocate_is_infeasible_when_the_body_cannot_cover_the_floor():
    with pytest.raises(SlidesError, match="cannot cover"):
        allocate([1, 1, 1], 2, 1, 5)
    with pytest.raises(SlidesError, match="exceeds"):
        allocate([1, 1, 1], 16, 1, 5)


def test_deck_config_rejects_an_impossible_configuration():
    with pytest.raises(SlidesError, match="not be negative"):
        DeckConfig(backup=-1)
    with pytest.raises(SlidesError, match="capacity-per-page"):
        DeckConfig(capacity_per_page=0)
    with pytest.raises(SlidesError, match="max-per-chapter"):
        DeckConfig(min_per_chapter=3, max_per_chapter=2)


def test_fixed_pages_are_opening_contents_cross_cutting_discussion_backup():
    kinds = [(page.kind, page.count) for page in fixed_pages(6)]

    # 结构开销是模块常量，不是选项；备份页按 --backup 给。导航三页合并成一页目录。
    assert kinds == [
        ("opening", 2),
        ("navigation", 1),
        ("cross_cutting", 3),
        ("discussion", 3),
        ("backup", 6),
    ]
    assert [page.label for page in page_entries(0) if page.kind == "navigation"] == ["目录"]


def test_deck_config_has_no_total_page_count_and_no_time_parameters():
    fields = set(DeckConfig.__dataclass_fields__)

    assert fields == {"backup", "min_per_chapter", "max_per_chapter", "capacity_per_page"}


# ---------------------------------------------------------------- 容量与取舍


def test_capacity_report_states_display_chars_and_the_trade_off():
    report = capacity_report(config(), 12, [10, 20, 30, 40])

    assert report.display_chars == DEFAULT_CAPACITY_PER_PAGE * 12
    # 每页 3–5 个要点，默认 4：12 页 → 36–60 个要点，默认 48
    assert report.points_needed_range == (36, 60)
    assert report.points_needed == 48
    assert report.candidates == 4
    assert float(report.candidates_per_point) == pytest.approx(4 / 48)
    assert [float(value) for value in report.candidates_per_point_range] == pytest.approx(
        [4 / 60, 4 / 36]
    )
    # 候选比要点少时没有可丢的
    assert report.discard_share == 0
    assert report.median_chars == 20
    assert report.min_chars == 10 and report.max_chars == 40
    assert report.points_per_page(20) == DEFAULT_CAPACITY_PER_PAGE // 20


def test_capacity_report_reports_what_the_human_must_discard():
    report = capacity_report(config(), 28, [30] * 258)

    # 28 内容页 × 每页 3–5 个要点 = 84–140 个，默认 112 个
    assert report.content_pages == 28
    assert report.points_needed_range == (84, 140)
    assert report.points_needed == 112
    assert report.candidates_per_point == pytest.approx(258 / 112)
    assert [float(value) for value in report.candidates_per_point_range] == pytest.approx(
        [258 / 140, 258 / 84]
    )
    assert float(report.discard_share) == pytest.approx(1 - 112 / 258)
    assert report.points_per_page(report.median_chars) == 11


def test_capacity_report_survives_an_empty_candidate_list():
    report = capacity_report(config(), 12, [])

    assert report.candidates == 0
    assert report.median_chars == 0 and report.max_chars == 0
    assert report.candidates_per_point == 0
    assert report.discard_share == 0
    assert report.points_per_page(0) == 0


def test_capacity_as_dict_is_yaml_ready_and_all_integers():
    payload = capacity_report(config(), 12, [3, 31, 351]).as_dict()

    assert payload["display_chars"] == DEFAULT_CAPACITY_PER_PAGE * 12
    assert payload["points_needed"] == 48
    assert payload["points_needed_min"] == 36 and payload["points_needed_max"] == 60
    assert payload["points_per_content_page"] == 4
    assert payload["median_candidate_chars"] == 31
    assert payload["points_per_page_at_median"] == DEFAULT_CAPACITY_PER_PAGE // 31
    # 351 字一条超过一页容量，放不进任何一页
    assert payload["points_per_page_at_max"] == DEFAULT_CAPACITY_PER_PAGE // 351


def test_frame_slots_balance_the_points_over_the_frames():
    """一章的要点摊到它的帧上要均衡，多出来的先给前面的帧。"""
    assert frame_slots(6, 2) == (3, 3)
    assert frame_slots(7, 2) == (4, 3)
    assert frame_slots(8, 2) == (4, 4)
    assert frame_slots(9, 2) == (5, 4)
    assert frame_slots(10, 2) == (5, 5)
    assert frame_slots(11, 3) == (4, 4, 3)
    # 一条也没圈：每帧照默认 4 个待选位置排
    assert frame_slots(0, 3) == (4, 4, 4)
    # 要点比帧数还少：排不上的帧记 0（check 会报 pick-count warning）
    assert frame_slots(2, 5) == (1, 1, 0, 0, 0)
    assert frame_slots(3, 0) == ()


# ---------------------------------------------------------------- 候选


def test_sentence_split_keeps_the_punctuation():
    assert cand.split_sentences("甲。乙；丙") == ["甲。", "乙；", "丙"]


def test_signals_cover_bold_digit_citation_forecast_and_length():
    short = cand.signals_of("**第一，这是粗体句子 [7]。**", 20)
    with_number = cand.signals_of("它覆盖了 90 个目标。", 20)
    long = cand.signals_of("x" * 200, 200)

    # [7] 是引用，不是数字：数字信号先摘掉引用标记再判
    assert short == ("bold", "cite", "forecast", "short")
    assert with_number == ("digit", "short")
    assert long == ()


def test_length_ignores_emphasis_but_counts_citations():
    assert cand.measure("**粗体**") == 2
    assert cand.measure("结论 [12]") == 7


def test_citation_ranges_count_as_one_marker():
    assert cand.CITATION_RE.findall("见 [96–101] 与 [384][385]。") == ["[96–101]", "[384]", "[385]"]


def test_typing_rules_follow_the_documented_order():
    # 小结最优先；两张词表在"三条"上重叠时落在小结一侧
    assert cand.classify("综上，三条规律成立。", digit=False, cite=False, forecast=True) == "小结"
    assert cand.classify("以下三条是判据。", digit=False, cite=False, forecast=True) == "小结"
    # 框定：预报短语，无引用、无数字
    assert cand.classify("以下是四条判据。", digit=False, cite=False, forecast=True) == "框定"
    # 有引用就是断言，不做框定
    assert cand.classify("以下是四条判据 [1]。", digit=False, cite=True, forecast=True) == "断言"
    # 强断言词
    assert cand.classify("这是首次探测。", digit=False, cite=False, forecast=False) == "断言"
    # 两个以上数字
    assert cand.classify("它覆盖了 90 个目标。", digit=True, cite=False, forecast=False) == "数据"
    # 一条都不命中时按断言计
    assert cand.classify("三十年改变了问题清单。", digit=False, cite=False, forecast=False) == "断言"


def _candidate(identifier, signals, citations, position, length=10, sentence=None):
    return cand.Candidate(
        id=identifier,
        chapter="1",
        chapter_label="第一章",
        type="断言",
        signals=signals,
        citations=citations,
        length=length,
        pointer="§1.1",
        sentence=sentence if sentence is not None else identifier,
        position=position,
    )


def test_ranking_is_signals_then_citations_then_position():
    items = [
        _candidate("a", ("short",), 0, 1),
        _candidate("b", ("bold", "digit", "short"), 0, 2),
        _candidate("c", ("bold", "digit", "short"), 3, 3),
        _candidate("d", ("bold", "digit", "short"), 3, 4),
    ]

    assert [item.id for item in cand.rank(items)] == ["c", "d", "b", "a"]


def test_long_candidates_are_demoted_one_signal_but_not_dropped():
    items = [
        _candidate("long", ("bold", "digit", "cite"), 5, 1, length=213),
        _candidate("short", ("bold", "digit", "cite"), 0, 2, length=68),
    ]

    # 213 字的句子放不满一页：即使信号一样多、引用还更多，也排在后面
    assert [item.id for item in cand.rank(items)] == ["short", "long"]
    assert len(cand.rank(items)) == 2


def test_emphasis_balance_strips_a_marker_stranded_by_sentence_splitting():
    text = "**本篇的中心议题为什么是行星科学。** 浩瀚宇宙。"

    # 切句把收尾的 `**` 留在了下一句的开头，第一句就缺了配对的一侧
    assert cand.split_sentences(text)[0] == "**本篇的中心议题为什么是行星科学。"
    assert cand.balance_emphasis("**本篇的中心议题为什么是行星科学。") == (
        "本篇的中心议题为什么是行星科学。"
    )
    # 配对完整的粗体不动，无论一处还是两处
    assert cand.balance_emphasis("**粗体。**") == "**粗体。**"
    assert cand.balance_emphasis("**甲** 与 **乙**。") == "**甲** 与 **乙**。"


def test_display_normalises_the_stranded_marker_but_keeps_the_sentence(book):
    _, spec_path = book
    structure = read_structure(spec_of(spec_path))
    items = {"{}".format(item.sentence): item for item in cand.extract(structure.chapters[0])}

    # 原文是 `**第一，粗体开头的句子。** 后半句。`，切句后收尾的 `**` 落到了下一句
    raw = "**第一，粗体开头的句子。"
    assert items[raw].sentence == raw
    assert items[raw].display == "第一，粗体开头的句子。"
    # 配对完整的粗体不动
    paired = "**第二段有粗体**，并且提到了 2024 年。"
    assert items[paired].display == paired


def test_every_displayed_sentence_has_balanced_emphasis(book):
    _, spec_path = book
    structure = read_structure(spec_of(spec_path))
    for chapter in structure.chapters:
        for item in cand.extract(chapter):
            assert item.display.count("**") % 2 == 0, item.id
            # 长度只看句子本身，展示形式不改变它是什么
            assert len(item.display.replace("*", "").strip()) == item.length


def test_extraction_attaches_ids_pointers_and_skips_non_paragraphs(book):
    _, spec_path = book
    structure = read_structure(spec_of(spec_path))
    first = structure.chapters[0]
    items = cand.rank(cand.extract(first))
    sentences = [item.sentence for item in items]

    assert "本节第一句。" in sentences
    assert "第一句带一个引用 [1]。" in sentences
    # 题注、表格、引用块、分隔线、标题都不是候选
    assert all("示例图" not in item for item in sentences)
    assert all("引用块" not in item for item in sentences)
    assert all(not item.startswith("|") for item in sentences)
    assert all(not item.startswith("#") for item in sentences)
    # 指针落在所属节的节号上；章首正文写章标签
    pointer = {item.sentence: item.pointer for item in items}
    assert pointer["第一句带一个引用 [1]。"] == "第一章"
    assert pointer["本节第一句。"] == "§1.1"
    assert pointer["**第一，粗体开头的句子。"] == "§1.2"
    # id：章 - 节的章内序号 - 该节第几段；章首正文的节序号是 0
    ids = {item.sentence: item.id for item in items}
    assert ids["第一句带一个引用 [1]。"] == "s1-0p01"
    assert ids["本节第一句。"] == "s1-1p01"
    assert ids["**第一，粗体开头的句子。"] == "s1-2p01"


def test_appendix_paragraphs_point_at_the_finest_section(book):
    _, spec_path = book
    structure = read_structure(spec_of(spec_path))
    appendix = structure.chapters[-1]
    pointers = {item.sentence: item.pointer for item in cand.extract(appendix)}

    assert pointers["附录的第一句 [3]。"] == "§A.1"
    assert pointers["**小节里的粗体句**。"] == "§A.1.1"
    assert pointers["附录第二节的句子。"] == "§A.2"


# ---------------------------------------------------------------- 端到端


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _outline_path(root):
    return root / "_primer" / "slides" / "review-seminar" / "outline.yaml"


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


def test_outline_writes_both_files_and_records_the_control_parameters(book):
    root, spec_path = book
    plan, written = run(root, config(), deck="seminar", spec_path=spec_path)

    candidates_path, outline_path = written
    assert candidates_path.name == "candidates.md"
    assert outline_path.name == "outline.yaml"
    assert candidates_path.parent == root / "_primer" / "slides" / "seminar"
    assert sorted(path.name for path in candidates_path.parent.iterdir()) == [
        "candidates.md",
        "outline.yaml",
    ]

    document = yaml.safe_load(outline_path.read_text(encoding="utf-8"))
    assert document["version"] == 2
    assert document["deck"]["backup"] == 2
    assert document["deck"]["capacity_per_page"] == DEFAULT_CAPACITY_PER_PAGE
    # 没有总页数这个控制项，也没有时间参数
    for gone in ("slides", "minutes", "minutes_per_slide", "wpm"):
        assert gone not in document["deck"]
    assert document["model"]["overhead_pages"] == 2 + 1 + 3 + 3 + 2
    assert document["model"]["body_pages"] == sum(
        entry["budget"] for entry in document["chapters"]
    )
    assert document["model"]["total_pages"] == (
        document["model"]["body_pages"] + document["model"]["overhead_pages"]
    )
    assert "capacity" in document and "checks" not in document
    assert document["capacity"]["content_pages"] == document["model"]["body_pages"]
    # 章一级不再有平铺的 picks；换成一帧一页的 frames，长度等于该章预算，各是空壳。
    assert all("picks" not in entry for entry in document["chapters"])
    assert all(entry["hint"] == "" for entry in document["chapters"])
    assert all("start_page" not in entry for entry in document["chapters"])
    assert all("printed_pages" not in entry for entry in document["chapters"])
    for entry in document["chapters"]:
        assert len(entry["frames"]) == entry["budget"]
        assert entry["budget_default"] == entry["budget"]
        assert entry["material_chars"] > 0
        assert all("page" not in section for section in entry["sections"])
        for frame in entry["frames"]:
            assert frame == {"picks": [], "review": "pending", "note": ""}
    assert document["chapters"][-1]["sections"][1] == {
        "level": "subsection",
        "number": "A.1.1",
        "title": "附录小节",
        "speak": True,
        "hint": "",
    }
    # 骨架里没有页码、没有成书产物的路径
    fingerprint = document["fingerprint"]
    assert fingerprint["spec"] == "_primer/slides/review-seminar/deck.yaml"
    assert set(fingerprint) == {"spec", "spec_sha256", "structure_sha256", "sources"}
    assert "toc" not in document["model"] and "log" not in document["model"]
    assert plan.candidate_total == sum(len(items) for items in plan.by_chapter.values())


def test_outline_header_reserves_a_slot_line_for_the_human(book):
    root, spec_path = book
    _, (_, outline_path) = run(root, config(), spec_path=spec_path)

    header = outline_path.read_text(encoding="utf-8").split("version:", 1)[0]
    assert "# 场合/时长：<自己填，工具不读>" in header
    assert "version: 2" in outline_path.read_text(encoding="utf-8")


def test_outline_emits_the_theme_block_as_plain_data(book):
    """画布、字号与配色是数据：写出来的是值，不是一个预设名。"""
    root, spec_path = book
    _, (_, outline_path) = run(root, config(), spec_path=spec_path)
    theme = yaml.safe_load(outline_path.read_text(encoding="utf-8"))["theme"]

    assert theme["canvas"] == {"width_bp": 960, "height_bp": 540, "margin_ratio": 0.05}
    assert theme["type"]["body"] == {"size_pt": 22, "leading_pt": 30}
    assert theme["type"]["footer"] == {"size_pt": 14, "leading_pt": 17}
    assert theme["palette"]["ink"] == "#1a1a1a"
    assert theme["palette"]["volume"] == {
        "V1": "#1f4e79",
        "V2": "#2f6f9f",
        "V3": "#4a90c2",
        "VA": "#9aa5b1",
    }


def test_outline_emits_the_slides_font_stack_as_data(book):
    """字体栈与画布、字号一样是数据：写出来的是默认无衬线栈，改了就跟着变。"""
    root, spec_path = book
    run(root, config(), spec_path=spec_path)
    path = _outline_path(root)

    assert _load(path)["theme"]["fonts"] == {
        "main": "Helvetica Neue",
        "sans": "Helvetica Neue",
        "mono": "Menlo",
        "cjk_main": "Hiragino Sans GB W3",
        "cjk_sans": "Hiragino Sans GB W6",
        "cjk_mono": "Hiragino Sans GB W3",
    }

    document = _load(path)
    document["theme"]["fonts"]["cjk_sans"] = "PingFang SC"
    _dump(path, document)
    run(root, config(), spec_path=spec_path, merge=True)

    assert _load(path)["theme"]["fonts"]["cjk_sans"] == "PingFang SC"


def test_default_capacity_per_page_follows_the_default_theme():
    from primer.slides.theme import default_capacity_per_page, default_theme, frame_metrics

    metrics = frame_metrics(default_theme())
    assert metrics.text_width_mm == pytest.approx(304.8, abs=0.01)
    assert metrics.chars_per_line == 24
    assert metrics.lines == 14
    assert default_capacity_per_page() == 336 == DEFAULT_CAPACITY_PER_PAGE


def test_a_fresh_outline_takes_its_capacity_from_the_default_theme(book):
    root, spec_path = book
    plan, (_, outline_path) = run(root, config(), spec_path=spec_path)

    assert plan.capacity_source == "theme"
    assert plan.capacity.per_page_chars == DEFAULT_CAPACITY_PER_PAGE == 336
    assert _load(outline_path)["deck"]["capacity_per_page"] == 336


def test_the_written_capacity_follows_the_outline_theme_on_merge(book):
    """容量按**骨架自己的** theme 块算：正文行距 30 pt → 24 字/行 × 14 行 = 336。"""
    root, spec_path = book
    run(root, config(), spec_path=spec_path)
    path = _outline_path(root)
    document = _load(path)
    document["theme"]["type"]["body"]["leading_pt"] = 36
    _dump(path, document)

    run(root, config(), spec_path=spec_path, merge=True)

    merged = _load(path)
    assert merged["deck"]["capacity_per_page"] == 24 * 11 == 264
    assert merged["capacity"]["per_page_chars"] == 264


def test_an_explicit_capacity_overrides_the_theme(book):
    """显式 --capacity-per-page 覆盖主题算出的值，报告里说清它的来路。"""
    root, spec_path = book
    plan, (_, outline_path) = run(
        root, config(capacity_per_page=200), spec_path=spec_path
    )

    assert plan.capacity_source == "explicit"
    assert plan.capacity.per_page_chars == 200
    assert _load(outline_path)["deck"]["capacity_per_page"] == 200


def test_parse_theme_reports_bad_font_keys_and_values():
    from primer.slides.theme import parse_theme

    _, problems = parse_theme({"fonts": {"serif": "X"}})
    assert any(code == "theme-fonts" and "serif" in message for code, message in problems)

    _, problems = parse_theme({"fonts": {"main": "  "}})
    assert any(code == "theme-fonts" and "main" in message for code, message in problems)


def test_the_type_floor_is_the_smallest_step_of_the_theme():
    """字号下限 = 梯级里最小的一档：备份查找表用它，不再往它之下缩。"""
    from primer.slides.theme import default_theme, floor_level_name, parse_theme

    assert floor_level_name(default_theme()) == "footer"
    theme, problems = parse_theme({"type": {"footer": {"size_pt": 30, "leading_pt": 36}}})
    assert not problems
    assert floor_level_name(theme) == "table_body"


def test_candidates_markdown_holds_the_allocation_and_the_trade_off(book):
    root, spec_path = book
    _, (candidates_path, _) = run(root, config(), spec_path=spec_path)

    text = candidates_path.read_text(encoding="utf-8")
    assert "| 序 | 章 | 材料（字） | 预算 | 候选 |" in text
    assert "| 1 | 第一章　第一章标题 |" in text
    assert "## 二、容量与取舍" in text
    assert "**页面容量**" in text and "**取舍比**" in text and "**每页建议条数**" in text
    assert "没有时间参数" in text
    assert "候选总数" in text
    # 指针列写节号，不写页码
    assert "| §1.1 |" in text
    assert "p.5" not in text and "起始页" not in text


def test_candidates_markdown_drops_the_pointer_column_when_asked(tmp_path):
    root = tmp_path / "工程"
    root.mkdir()
    spec_path = write_spec(root, DECK_SPEC.replace("pointers: section", "pointers: none"))
    write_sources(root)
    run(root, config(), spec_path=spec_path)

    text = (root / "_primer" / "slides" / "review-seminar" / "candidates.md").read_text(
        encoding="utf-8"
    )
    assert "页面指针" not in text
    assert "| id | 类型 | 信号 | 引用 | 字数 | 句子 |" in text
    # 没有指针列时照旧读得出候选表（表头驱动，不按位置读）
    rows = cand.parse_markdown_table(text)
    assert rows and all(row.pointer == "" for row in rows.values())


def test_regeneration_is_byte_identical(book):
    root, spec_path = book
    _, first = run(root, config(), spec_path=spec_path)
    before = [_digest(path) for path in first]
    # 第二次重生成要显式 --force：默认拒绝覆盖，见守卫测试。
    _, second = run(root, config(), spec_path=spec_path, force=True)

    assert first == second
    assert before == [_digest(path) for path in second]


# ---------------------------------------------------------------- --merge 迁移与幂等


def _as_old_skeleton(document, chapter_index, picks):
    """把一章改回旧写法：章一级一串平铺的 picks，没有 frames，也没有 hint。"""
    chapter = document["chapters"][chapter_index]
    chapter.pop("frames", None)
    chapter.pop("hint", None)
    chapter["picks"] = list(picks)
    for section in chapter.get("sections") or []:
        section.pop("hint", None)
    return document


def test_merge_migrates_the_flattened_picks_into_frames(book):
    """旧骨架章一级的一串 picks 按均衡分法摊进各帧，一条不丢，旧字段随之消失。"""
    root, spec_path = book
    run(root, config(), spec_path=spec_path)
    path = _outline_path(root)
    ids = [f"s1-0p{index:02d}" for index in range(1, 8)]  # 7 条，第一章预算 3 页
    _dump(path, _as_old_skeleton(_load(path), 0, ids))

    run(root, config(), spec_path=spec_path, merge=True)
    chapter = _load(path)["chapters"][0]

    assert "picks" not in chapter
    assert len(chapter["frames"]) == chapter["budget"] == 3
    counts = frame_slots(len(ids), 3)
    assert [len(frame["picks"]) for frame in chapter["frames"]] == list(counts)
    assert [pick for frame in chapter["frames"] for pick in frame["picks"]] == ids
    assert all(frame["review"] == "pending" for frame in chapter["frames"])
    # 章与节重新带上 hint 空壳
    assert chapter["hint"] == ""
    assert all(section["hint"] == "" for section in chapter["sections"])


def test_merge_twice_is_byte_identical(book):
    """连跑两次 --merge：第二次写出的文件与第一次逐字节相同。"""
    root, spec_path = book
    run(root, config(), spec_path=spec_path)
    path = _outline_path(root)
    document = _as_old_skeleton(_load(path), 0, ["s1-0p01", "s1-0p02"])
    # 人手改过的几处：预算、讲不讲、节级 hint、固定页自由文本
    document["chapters"][0]["budget"] = 4
    document["chapters"][1]["sections"][0]["speak"] = False
    document["chapters"][0]["hint"] = "这一章只立三条规律"
    document["pages"][1]["picks"] = ["科学、工程与战略研判"]
    _dump(path, document)

    run(root, config(), spec_path=spec_path, merge=True)
    first = _digest(path)
    run(root, config(), spec_path=spec_path, merge=True)
    second = _digest(path)

    assert first == second
    merged = _load(path)
    assert merged["chapters"][0]["budget"] == 4
    assert merged["chapters"][0]["budget_default"] == 3
    assert merged["chapters"][0]["hint"] == "这一章只立三条规律"
    assert merged["chapters"][1]["sections"][0]["speak"] is False
    assert merged["pages"][1]["picks"] == ["科学、工程与战略研判"]
    assert len(merged["chapters"][0]["frames"]) == 4


def test_merge_pairs_fixed_pages_by_kind_not_by_position(book):
    """目录从三页并成一页：封面的句子不能挪到主旨页上，旧导航页的标签也不该留下。"""
    root, spec_path = book
    run(root, config(), spec_path=spec_path)
    path = _outline_path(root)
    document = _load(path)
    # 造一份旧骨架的固定页：3 页导航（结构图/阅读路径/章节索引），封面与主旨各带句子
    document["pages"] = [
        {"kind": "opening", "label": "封面", "picks": ["封面副题"],
         "figure": None, "notes": ""},
        {"kind": "opening", "label": "主旨", "picks": ["主旨那一句"],
         "figure": None, "notes": "主旨备注"},
        {"kind": "navigation", "label": "结构图", "auto": True, "picks": [],
         "figure": None, "notes": ""},
        {"kind": "navigation", "label": "阅读路径", "auto": True, "picks": [],
         "figure": None, "notes": ""},
        {"kind": "navigation", "label": "章节索引", "auto": True, "picks": [],
         "figure": None, "notes": ""},
        {"kind": "backup", "label": "备份 1", "auto": True, "picks": [],
         "figure": None, "notes": ""},
        {"kind": "backup", "label": "备份 2", "auto": True, "picks": [],
         "figure": None, "notes": ""},
    ]
    _dump(path, document)

    run(root, config(), spec_path=spec_path, merge=True)
    pages = _load(path)["pages"]

    # 新的固定页清单说了算：2 开场 + 1 目录 + 3 横向 + 3 讨论 + 2 备份
    assert [page["kind"] for page in pages] == (
        ["opening"] * 2 + ["navigation"] + ["cross_cutting"] * 3 + ["discussion"] * 3
        + ["backup"] * 2
    )
    assert pages[0]["picks"] == ["封面副题"]
    assert pages[1]["picks"] == ["主旨那一句"]
    assert pages[1]["notes"] == "主旨备注"
    # 导航页换了套路：标签回到新的默认值，旧的三页标签不再留下来
    assert pages[2]["label"] == "目录"


def test_merge_reports_and_writes_the_human_budgets_everywhere(book):
    """人改过预算之后，骨架的 model／capacity 与候选表的分配表都按那份预算说事。"""
    root, spec = book
    run(root, config(), spec_path=spec)
    path = _outline_path(root)
    document = _load(path)
    document["chapters"][0]["budget"] = 5
    document["chapters"][0]["frames"] = [
        {"picks": [], "review": "pending", "note": ""} for _ in range(5)
    ]
    _dump(path, document)

    plan, (candidates_path, _) = run(root, config(), spec_path=spec, merge=True)
    merged = _load(path)
    text = candidates_path.read_text(encoding="utf-8")

    assert plan.body_pages == sum(entry["budget"] for entry in merged["chapters"]) == 14
    assert merged["model"]["body_pages"] == 14
    assert merged["model"]["total_pages"] == 14 + 11
    assert merged["capacity"]["content_pages"] == 14
    # 候选表里的两处页数也是同一份预算：内容页 14，全片 25
    assert "内容页 14 页" in text
    assert f"全片 {14 + 11} 页" in text
    assert f"| **{14 + 11}** |" in text


def test_merge_keeps_a_fixed_page_pick_that_is_free_text(book):
    """固定页上手写的自由文本（封面副题那类）在 --merge 后仍逐字保留。"""
    root, spec_path = book
    run(root, config(), spec_path=spec_path)
    path = _outline_path(root)
    document = _load(path)
    document["pages"][0]["picks"] = ["科学、工程与战略研判"]
    _dump(path, document)

    run(root, config(), spec_path=spec_path, merge=True)

    assert _load(path)["pages"][0]["picks"] == ["科学、工程与战略研判"]


# ---------------------------------------------------------------- 命令行


def test_cli_reports_the_allocation_and_the_trade_off(book, capsys):
    root, spec_path = book
    code = main(
        [
            "outline",
            "--project-root",
            str(root),
            "--spec",
            str(spec_path),
            "--backup",
            "2",
            "--min-per-chapter",
            "3",
        ]
    )

    captured = capsys.readouterr()
    assert code == 0
    assert "allocation (body 12 pages over 4 chapters," in captured.out
    assert "with a hand-set budget)" in captured.out
    assert "capacity and trade-off" in captured.out
    assert "display capacity" in captured.out and "trade-off" in captured.out
    assert "_primer/slides/review-seminar/candidates.md" in captured.out
    assert str(root) not in captured.out
    assert str(root) not in captured.err


def test_cli_reports_the_budget_origin(book, capsys):
    root, spec_path = book
    main(["outline", "--project-root", str(root), "--spec", str(spec_path),
          "--min-per-chapter", "3"])
    path = _outline_path(root)
    document = _load(path)
    document["chapters"][0]["budget"] = 4
    document["chapters"][0]["frames"].append(
        {"picks": [], "review": "pending", "note": ""}
    )
    _dump(path, document)

    code = main(["check", str(path), "--project-root", str(root)])

    captured = capsys.readouterr()
    assert code == 0
    assert "逐章对账" in captured.out
    assert "人工 1" in captured.out
    assert "正文 13 页 = 各章预算之和（其中 1 章人工定过）" in captured.out


def test_the_outline_report_states_where_the_capacity_came_from(book, capsys):
    root, spec_path = book
    main(["outline", "--project-root", str(root), "--spec", str(spec_path)])
    out = capsys.readouterr().out

    assert "336 chars per page (from the theme)" in out
    assert "3 slides / backup" in out or "slides / backup" in out


def test_outline_help_explains_the_control_model_in_one_sentence():
    parser = build_parser()
    help_text = parser.format_help()
    outline = parser._subparsers._group_actions[0].choices["outline"].format_help()

    assert "outline" in help_text
    assert "a chapter's budget is the only control parameter" in outline
    assert "--min/--max-per-chapter" in outline
    # 总页数与时间都不再是参数
    for gone in ("--slides", "--minutes", "--minutes-per-slide", "--wpm"):
        assert gone not in outline


def test_the_manifest_option_is_deprecated_and_points_at_spec(book, capsys):
    root, spec_path = book
    code = main(
        [
            "outline",
            "--project-root",
            str(root),
            "--manifest",
            str(spec_path),
        ]
    )

    captured = capsys.readouterr()
    assert code == 2
    assert "--manifest 已弃用" in captured.err
    assert "--spec" in captured.err
    # 报错之前一个字节都不写
    assert not _outline_path(root).exists()
