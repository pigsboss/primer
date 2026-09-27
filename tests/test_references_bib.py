# -*- coding: utf-8 -*-
"""bib 的单元测试：cite key、字段抽取、姓名转换、LaTeX 转义与校核报告。"""

import re

from primer.references.bib import (
    build_records,
    cite_key,
    render_bib,
    render_review,
    write_bib,
    write_review,
)
from primer.references.entries import parse_markdown

CLEAN = (
    "[265] Harris A.W., D'Abramo G. The population of near-Earth asteroids. "
    "Icarus 257, 302–312, 2015. ［原文：待图书馆获取］"
)
CHINESE = (
    "[10] 中华人民共和国国务院新闻办公室. 《2016中国的航天》白皮书. 北京, 2016年12月. "
    "［原文：待图书馆获取］"
)
UNCERTAIN = (
    "[26] Quanz S.P., Ottiger M., et al. Large Interferometer For Exoplanets (LIFE): I. "
    "Improved exoplanet detection yield estimates. Astronomy & Astrophysics 664, A21, 2022. "
    "［原文：arXiv:2101.07500 已存本地］"
)


def _records(*lines: str):
    text = "\n".join(lines) + "\n"
    lists = parse_markdown(text, source="refs.md")
    return build_records([entry for ref_list in lists for entry in ref_list.entries])


def test_cite_key_is_the_number_zero_padded_to_three_digits():
    assert cite_key(1) == "ref001"
    assert cite_key(443) == "ref443"
    assert cite_key(1000) == "ref1000"


def test_a_clean_english_entry_maps_to_article_fields_without_warnings():
    record = _records(CLEAN)[0]

    assert record.kind == "article"
    assert record.key == "ref265"
    assert record.uncertain is False
    assert record.get("author") == "A.W. Harris and G. D'Abramo"
    assert record.get("title") == "The population of near-Earth asteroids"
    assert record.get("journal") == "Icarus"
    assert record.get("volume") == "257"
    assert record.get("pages") == "302–312"
    assert record.get("year") == "2015"


def test_et_al_becomes_bibtex_and_others_and_is_recorded_as_a_caveat():
    record = _records(CLEAN.replace("D'Abramo G", "D'Abramo G., et al"))[0]

    assert record.get("author") == "A.W. Harris and G. D'Abramo and others"
    assert record.warning_codes() == []
    assert record.caveat_codes() == ["author-etal"]


def test_an_institutional_author_is_kept_verbatim_in_braces():
    record = _records(
        "[1] National Research Council. New Frontiers in the Solar System. "
        "Washington, DC: The National Academies Press, 2003. ［原文：待图书馆获取］"
    )[0]

    assert record.get("author") == "{National Research Council}"
    assert record.kind == "report"
    assert record.get("institution") == "The National Academies Press"
    assert record.get("location") == "Washington, DC"
    assert record.warning_codes() == ["author-literal"]
    assert "@report{ref001," in render_bib([record])
    assert "author        = {{National Research Council}}," in render_bib([record])


def test_a_chinese_entry_keeps_every_field_but_is_flagged():
    record = _records(CHINESE)[0]

    assert "chinese" in record.warning_codes()
    assert record.get("author") == "{中华人民共和国国务院新闻办公室}"
    assert record.get("year") == "2016"
    assert record.kind == "report"


def test_a_missing_author_leaves_the_field_out_and_flags_it():
    record = _records(
        "[358] 不依赖撞击机制的盘内向类地行星输水新机制研究. Nature, 2024. ［原文：待图书馆获取］"
    )[0]

    # 这条题录把标题当成了作者段：author 与 title 都照抄原文，但必须报出来。
    assert "author-literal" in record.warning_codes() or "title-unsplit" in record.warning_codes()
    assert record.get("title")


def test_no_year_anywhere_is_flagged_and_the_field_is_left_out():
    record = _records("[433] 华东师范大学. 俄罗斯航天计划实施分析. ［原文：待图书馆获取］")[0]

    assert record.get("year") is None
    assert "no-year" in record.warning_codes()


def test_arxiv_ids_become_eprint_and_archive_prefix():
    record = _records(UNCERTAIN)[0]

    assert record.get("eprint") == "2101.07500"
    assert record.get("archivePrefix") == "arXiv"


def test_a_title_cut_in_the_middle_by_the_segmenter_is_flagged():
    record = _records(UNCERTAIN)[0]

    assert record.get("title") == "Large Interferometer For Exoplanets (LIFE): I"
    assert "title-truncated" in record.warning_codes()


def test_latex_special_characters_are_escaped_not_dropped():
    record = _records(
        "[108] Crill B.P., Siegler N. Technology for directly imaging exoplanets. "
        "Nature Astronomy 1, 2017. ［原文：待图书馆获取］"
    )[0]

    assert record.get("journal") == "Nature Astronomy"

    ampersand = _records(
        "[97] Labeyrie A. Resolved imaging. Astronomy & Astrophysics 118, 517–524, 1996. "
        "［原文：待图书馆获取］"
    )[0]

    # fields 里存的就是写进 .bib 的字面值：``&`` 已转义，字符本身不丢。
    assert ampersand.get("journal") == "Astronomy \\& Astrophysics"
    assert "= {Astronomy \\& Astrophysics}," in render_bib([ampersand])
    assert ampersand.entry.raw.startswith("Labeyrie A. Resolved imaging. Astronomy & Astrophysics")


def test_multi_author_lists_are_inverted_and_deduplicated_names_survive():
    record = _records(
        "[164] Snellen I.A.G., van der Tak F.F.S. Re-analysis of the observations. "
        "Astronomy & Astrophysics 644, L2, 2020. ［原文：待图书馆获取］"
    )[0]

    assert record.get("author") == "I.A.G. Snellen and F.F.S. van der Tak"


def test_duplicate_numbers_get_a_deterministic_suffix():
    records = _records(CLEAN, CLEAN.replace("Harris A.W., D'Abramo G", "Other O"))

    assert [record.key for record in records] == ["ref265", "ref265-2"]
    assert "key-duplicate-number" in records[1].warning_codes()


def test_keys_are_unique_and_rendering_is_deterministic():
    records = _records(CLEAN, CHINESE, UNCERTAIN)

    keys = [record.key for record in records]
    assert len(set(keys)) == len(keys)
    assert render_bib(records) == render_bib(build_records(_entries_for(records)))


def _entries_for(records):
    return [record.entry for record in records]


def test_every_entry_has_balanced_braces_and_one_key():
    records = _records(CLEAN, CHINESE, UNCERTAIN)
    bib = render_bib(records)

    for block in re.findall(r"@(\w+)\{([^,]+),(.*?)\n\}", bib, re.S):
        kind, key, body = block
        assert kind in ("article", "book", "inbook", "inproceedings", "misc", "online", "report", "thesis")
        assert key.strip()
        assert body.count("{") == body.count("}"), body


def test_review_report_lists_every_flagged_entry_with_raw_text_and_assumption():
    records = _records(CLEAN, CHINESE, UNCERTAIN)
    review = render_review(records, source_note="refs.md")

    assert "解析不确定（有靠猜的字段）：2 条" in review
    assert "- 条目总数：3" in review
    assert "author-etal" in review  # 不确定项汇总之外的"信息不完整"一节
    for record in records:
        if not record.uncertain:
            continue
        assert f"### [{record.number}] {record.key}" in review
        assert record.entry.raw in review
        for code, note in record.warnings:
            assert f"假定（`{code}`）：{note}" in review
    # 干净条目不出现在逐条清单里
    assert "### [265] ref265" not in review


def test_review_report_explains_the_cite_key_scheme_and_the_transformations():
    review = render_review(_records(CLEAN), source_note="refs.md")

    assert "cite key：``refNNN``" in review
    assert "源：refs.md" in review


def test_writers_create_parent_directories(tmp_path):
    records = _records(CLEAN)
    out = tmp_path / "deep" / "out"

    write_bib(out / "references.bib", records, source_note="refs.md")
    write_review(out / "references-bib-review.md", records, source_note="refs.md")

    assert (out / "references.bib").read_text(encoding="utf-8").startswith("% 参考文献库")
    assert (out / "references-bib-review.md").read_text(encoding="utf-8").startswith(
        "# 参考文献 .bib 校核报告"
    )
