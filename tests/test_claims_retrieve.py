# -*- coding: utf-8 -*-
"""``primer.claims.retrieve`` 的单元测试：分词、分窗、打分与状态归类。

夹具是短文本，纯标准库，不读真实语料、不联网。
"""

from __future__ import annotations

import pytest

from primer.claims import retrieve as rt

SOURCE = """\
> **Citation** — ref 001 · class A
> **Title** — Alpha
> **Source** — x.pdf
> **Pages** — 3

# Alpha

The Alpha mission measured the water inventory of the inner accretion disk.

A second section discusses the orbital period of the Dimorphos satellite after the DART
kinetic impact, reporting a change of 33 minutes.

A third section is about something else entirely, with no shared vocabulary at all.
"""


def test_tokenize_makes_cjk_bigrams_and_lowercased_latin_words():
    tokens = rt.tokenize("火星水 ICE DART 2022")
    assert "火星" in tokens and "星水" in tokens
    assert "ice" in tokens and "dart" in tokens and "2022" in tokens
    assert "a" not in tokens


def test_a_single_cjk_character_survives_tokenisation():
    assert rt.tokenize("磷") == ["磷"]


def test_strip_header_removes_the_citation_block_only_when_it_is_one():
    body, dropped = rt.strip_header(SOURCE)
    assert body.startswith("# Alpha")
    assert dropped == len(SOURCE) - len(body)
    plain, dropped_plain = rt.strip_header("# 标题\n\n正文。\n")
    assert plain.startswith("# 标题") and dropped_plain == 0


def test_chunks_overlap_and_cover_the_whole_text():
    text = "x" * 2000
    spans = rt.chunk(text, window=800, overlap=200)
    assert spans[0] == (0, 800)
    assert spans[1][0] == 600
    assert spans[-1][1] == len(text)
    for previous, current in zip(spans, spans[1:]):
        assert previous[1] - current[0] == 200


def test_chunk_rejects_an_overlap_that_is_not_smaller_than_the_window():
    with pytest.raises(ValueError, match="0 <= overlap < window"):
        rt.chunk("abc", window=10, overlap=10)


def test_retrieve_finds_the_matching_passage_with_offsets():
    index = rt.build_index(SOURCE)
    result = rt.retrieve_passages("Dimorphos orbital period change after the DART kinetic impact", index, k=3)
    assert result.status == rt.STATUS_OK
    top = result.passages[0]
    assert "Dimorphos" in top.text
    assert index.text[top.start : top.end] == top.text
    assert top.score == 1.0


def test_retrieve_is_cjk_aware():
    target = "火星水的就位证据已经确立。"
    index = rt.build_index("第三段讲的是完全无关的内容。" * 40 + target + "别的段落。" * 40, window=200, overlap=50)
    result = rt.retrieve_passages("火星水的就位证据已经确立", index, k=3)
    assert result.status == rt.STATUS_OK
    assert target in result.passages[0].text
    assert result.passages[0].score == 1.0
    assert result.query_tokens > 0


def test_a_weak_match_is_returned_and_labelled_rather_than_dropped():
    index = rt.build_index(SOURCE)
    result = rt.retrieve_passages("Dimorphos unrelated filler words that barely overlap", index, k=3, min_score=0.99)
    assert result.status == rt.STATUS_WEAK
    assert result.passages


def test_no_overlap_at_all_is_reported_as_no_candidates():
    index = rt.build_index(SOURCE)
    result = rt.retrieve_passages("完全不同的中文句子", index, k=3)
    assert result.status == rt.STATUS_NONE
    assert result.passages == []
    assert result.matched_tokens == 0


def test_an_empty_source_yields_no_candidates():
    result = rt.retrieve_passages("anything", rt.build_index(""))
    assert result.status == rt.STATUS_NONE


def test_top_k_never_repeats_nearly_identical_windows():
    index = rt.build_index("the quick brown fox jumps over the lazy dog. " * 200)
    result = rt.retrieve_passages("quick brown fox", index, k=5)
    spans = [(passage.start, passage.end) for passage in result.passages]
    assert len(spans) == len(set(spans))
    for left, right in zip(spans, spans[1:]):
        assert right[0] - left[0] >= rt.WINDOW - rt.OVERLAP


def test_the_tie_break_is_deterministic():
    index = rt.build_index(SOURCE)
    first = rt.retrieve_passages("Alpha", index, k=5)
    second = rt.retrieve_passages("Alpha", index, k=5)
    assert [passage.start for passage in first.passages] == [passage.start for passage in second.passages]


def test_estimate_tokens_counts_cjk_and_latin_separately():
    assert rt.estimate_tokens("") == 0
    assert rt.estimate_tokens("abc") == 1
    assert rt.estimate_tokens("中文中文") == 2
    assert rt.estimate_tokens("中文中文中") == 3
