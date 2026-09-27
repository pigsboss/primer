# -*- coding: utf-8 -*-
"""entries 的单元测试：条目切分、尾标分类、字段抽取与清单自洽性校验。"""

import pytest

from primer.references.entries import (
    TAG_AWAITING,
    TAG_LOCAL,
    TAG_NONE,
    TAG_PUBLIC_WEB,
    all_entries,
    classify_tag,
    discover_lists,
    parse_markdown,
    split_citation,
    validate,
)

REF_MD = """# 参考文献库

> 版本：v1 · 共 5 条
> 结构：A 战略报告 [1–2] / B 系外行星 [3–5]

---

## A 战略报告

[1] Mayor M., Queloz D. A Jupiter-mass companion to a solar-type star. Nature 378, 355–359, 1995. ［原文：待图书馆获取］
[2] NASA. Enduring Quests, Daring Visions. 2013. ［原文：arXiv:1401.3741 已存本地］

## B 系外行星

[3] Gaudi B.S. Microlensing surveys for exoplanets. Annual Review 50, 411–453, 2012. ［原文：待图书馆获取］
[4] Smith J. DOI: 10.1234/abcd.2020.01. Title of record. Journal 1, 1–2, 2020. ［原文：待图书馆获取］
[5] 中华人民共和国国务院新闻办公室. 《2021中国的航天》白皮书. 北京, 2022年1月. ［原文：NASA 官网公开］
"""


def test_parse_entry_core_fields():
    ref_list = parse_markdown(REF_MD, source="refs.md")[0]

    assert [entry.number for entry in ref_list.entries] == [1, 2, 3, 4, 5]
    assert ref_list.declared_total == 5
    assert ref_list.declared_sections == ["A", "B"]

    first = ref_list.entries[0]
    assert first.tag == "待图书馆获取"
    assert first.tag_kind == TAG_AWAITING
    assert first.authors == "Mayor M., Queloz D"
    assert first.title == "A Jupiter-mass companion to a solar-type star"
    assert first.venue_year == "Nature 378, 355–359, 1995"
    assert first.raw.endswith("1995.")
    assert "［原文" not in first.raw
    assert first.class_letter == "A"
    assert first.class_title == "战略报告"
    assert first.source == "refs.md"
    assert first.line == 10


def test_arxiv_from_tag_and_from_prose_and_doi():
    ref_list = parse_markdown(REF_MD, source="refs.md")[0]
    entries = {entry.number: entry for entry in ref_list.entries}

    assert entries[2].tag_kind == TAG_LOCAL
    assert entries[2].tag_arxiv == "1401.3741"
    assert entries[2].prose_arxiv is None
    assert entries[2].arxiv == "1401.3741"
    assert entries[4].doi == "10.1234/abcd.2020.01"
    assert entries[4].arxiv is None
    assert entries[1].tag_arxiv is None and entries[1].arxiv is None


def test_arxiv_in_prose_is_used_when_the_tag_has_none():
    text = "[7] Doe J. A paper. arXiv:2202.0001, 2022. ［原文：待图书馆获取］\n"

    entry = parse_markdown(text)[0].entries[0]

    assert entry.tag_arxiv is None
    assert entry.prose_arxiv == "2202.0001"
    assert entry.arxiv == "2202.0001"


def test_continuation_lines_are_appended_to_the_open_entry():
    text = (
        "## A\n"
        "[1] Author A. Title one. Journal 1, 2001.\n"
        "    and a continued clause. ［原文：待图书馆获取］\n"
        "[2] Author B. Title two. Journal 2, 2002. ［原文：待图书馆获取］\n"
    )

    entries = parse_markdown(text)[0].entries

    assert len(entries) == 2
    assert "continued clause" in entries[0].raw
    assert entries[0].tag == "待图书馆获取"
    assert "continued clause" not in entries[1].raw


def test_thematic_break_closes_the_entry_and_is_not_a_continuation():
    text = (
        "## A\n"
        "[1] Author A. Title one. Journal 1, 2001. ［原文：待图书馆获取］\n"
        "\n"
        "---\n"
        "\n"
        "*（续：说明行）*\n"
        "[2] Author B. Title two. Journal 2, 2002. ［原文：待图书馆获取］\n"
    )

    entries = parse_markdown(text)[0].entries

    assert len(entries) == 2
    assert "说明行" not in entries[0].raw
    assert entries[0].raw == "Author A. Title one. Journal 1, 2001."


@pytest.mark.parametrize(
    "heading, letter",
    [("## A 战略", "A"), ("### M 增补", "M"), ("### I+ 问题", "I+"), ("#### G8 工程", "G")],
)
def test_class_letter_comes_from_any_heading_level(heading, letter):
    text = f"{heading}\n\n[1] A. T. J 1, 2001. ［原文：待图书馆获取］\n"

    entry = parse_markdown(text)[0].entries[0]

    assert entry.class_letter == letter


def test_multiple_lists_are_discovered_across_files(tmp_path):
    first = tmp_path / "one.md"
    first.write_text(
        "## A\n[1] A. T. J 1, 2001. ［原文：待图书馆获取］\n"
        "# Second list\n## B\n[2] B. T. J 2, 2002. ［原文：待图书馆获取］\n",
        encoding="utf-8",
    )
    second = tmp_path / "two.md"
    second.write_text("## C\n[3] C. T. J 3, 2003. ［原文：待图书馆获取］\n", encoding="utf-8")

    lists = discover_lists([first, second])

    assert [len(item.entries) for item in lists] == [1, 1, 1]
    assert [entry.number for entry in all_entries(lists)] == [1, 2, 3]
    assert lists[0].entries[0].class_letter == "A"
    assert lists[1].entries[0].class_letter == "B"


def test_validate_reports_count_delta_and_section_letters():
    text = (
        "# t\n> 共 4 条\n> 结构：A Alpha [1–2] / B Beta [3–4] / Z Zed\n"
        "## A Alpha\n[1] A. T. J 1, 2001. ［原文：待图书馆获取］\n"
        "### M 增补\n[5] E. T. J 5, 2005. ［原文：待图书馆获取］\n"
    )

    report = validate(parse_markdown(text)[0])

    assert report.declared_total == 4
    assert report.actual_count == 2
    assert report.total_delta == -2
    assert report.missing_letters == ["B", "Z"]
    assert report.undeclared_letters == ["M"]


def test_validate_reports_duplicates_and_gaps():
    text = (
        "## A\n"
        "[1] A. T. J 1, 2001. ［原文：待图书馆获取］\n"
        "[3] B. T. J 3, 2003. ［原文：待图书馆获取］\n"
        "[3] C. T. J 3, 2003. ［原文：待图书馆获取］\n"
        "[7] D. T. J 7, 2007. ［原文：待图书馆获取］\n"
    )

    report = validate(parse_markdown(text)[0])

    assert report.duplicates == [3]
    assert report.gaps == [2, 4, 5, 6]


def test_validate_clean_list_has_no_duplicates_or_gaps():
    report = validate(parse_markdown(REF_MD)[0])

    assert report.duplicates == []
    assert report.gaps == []
    assert report.missing_letters == []
    assert report.undeclared_letters == []


@pytest.mark.parametrize(
    "tag, kind",
    [
        (None, TAG_NONE),
        ("待图书馆获取", TAG_AWAITING),
        ("arXiv:1401.3741 已存本地", TAG_LOCAL),
        ("NASA 官网公开", TAG_PUBLIC_WEB),
        ("NASA NTRS 公开", TAG_PUBLIC_WEB),
        ("白宫官网公开", TAG_PUBLIC_WEB),
        ("CBO 官网公开", TAG_PUBLIC_WEB),
        ("看不懂的说明", TAG_NONE),
    ],
)
def test_classify_tag(tag, kind):
    assert classify_tag(tag) == kind


def test_split_citation_skips_author_tail_and_falls_back():
    assert split_citation("Knutson H.A., et al. A map. Nature 447, 2007.") == (
        "Knutson H.A., et al",
        "A map",
        "Nature 447, 2007",
    )
    assert split_citation("Wessen R.R. et al. JPL Concept Maturity Level. 2013.") == (
        "Wessen R.R",
        "JPL Concept Maturity Level",
        "2013",
    )
    assert split_citation("singleblock") == ("singleblock", "singleblock", "")
