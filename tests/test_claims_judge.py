# -*- coding: utf-8 -*-
"""``primer.claims.judge`` 的单元测试：判定归一、四道硬规矩、第二遍与回信修复。

传输层是假的，不联网、不读真实语料。这里的重点是**判定口径**：没有逐字引文不准下
判定、没有候选只能判 ``unverifiable``、``partial``/``unsupported`` 必须跑第二遍。
"""

from __future__ import annotations

import json

import pytest

from primer.claims import judge as jd
from primer.claims.client import ChatClient
from primer.claims.retrieve import Passage, QueryStat, STATUS_NONE, STATUS_OK, STATUS_WEAK

SOURCE = (
    "# Alpha\n"
    "\n"
    "The Alpha mission measured the water inventory of the inner accretion disk.\n"
    "\n"
    "The DART kinetic impact changed the orbital period of Dimorphos by 33 minutes.\n"
)

PERIOD_QUOTE = "The DART kinetic impact changed the orbital period of Dimorphos by 33 minutes."


def chat_reply(content: str, *, finish: str = "stop", completion_tokens: int = 12) -> bytes:
    return json.dumps(
        {
            "choices": [{"message": {"content": content}, "finish_reason": finish}],
            "usage": {"prompt_tokens": 100, "completion_tokens": completion_tokens},
        }
    ).encode("utf-8")


def endpoint(responder):
    seen: list[str] = []

    def transport(request):
        prompt = json.loads(request.body.decode("utf-8"))["messages"][0]["content"]
        seen.append(prompt)
        return responder(prompt)

    transport.seen = seen
    return transport


def make_client(responder) -> ChatClient:
    return ChatClient(key="test-key", transport=endpoint(responder))


def item(**overrides) -> jd.JudgeInput:
    base = dict(
        pair_id="c0001#292",
        claim_id="c0001",
        citation=292,
        claim="DART 撞击使双卫一的轨道周期改变了 33 分钟。",
        translation="The DART impact changed the orbital period of Dimorphos by 33 minutes.",
        entry_title="Ejecta from the DART-produced active asteroid Dimorphos",
        entry_venue="2023",
        source_markdown="_primer/literature/flat/05580ecb_292.md",
        source_chars=len(SOURCE),
        candidates_status=STATUS_OK,
        passages=(
            Passage(start=0, end=len(SOURCE), score=0.4, text=SOURCE, queries=("translation",)),
        ),
        queries=(QueryStat("translation", 12, 9, STATUS_OK, 0.4),),
    )
    base.update(overrides)
    return jd.JudgeInput(**base)


# ---------------------------------------------------------------- 归一与引文定位


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("supported", "supported"),
        ("SUPPORTED", "supported"),
        ("fully supported", "supported"),
        ("partially supported", "partial"),
        ("not supported", "unsupported"),
        ("insufficient evidence", "unverifiable"),
        ("source-missing", "source-missing"),
        ("banana", None),
        ("", None),
        (None, None),
    ],
)
def test_verdict_spellings_are_normalised_or_refused(raw, expected):
    assert jd.normalise_verdict(raw) == expected


def test_a_verbatim_quote_is_located_with_its_offsets_and_line():
    start, end, exact = jd.locate_quote(SOURCE, PERIOD_QUOTE)
    assert exact is True
    assert SOURCE[start:end] == PERIOD_QUOTE
    assert SOURCE.count("\n", 0, start) + 1 == 5


def test_a_quote_that_only_differs_in_whitespace_is_located_but_flagged():
    located = jd.locate_quote(SOURCE, "water inventory\n   of the inner accretion disk")
    assert located is not None
    start, end, exact = located
    assert exact is False
    # 取回的是**原文里的那一段**（单空格），不是模型给的折行写法。
    assert SOURCE[start:end] == "water inventory of the inner accretion disk"


def test_a_quote_that_is_not_there_is_not_located():
    assert jd.locate_quote(SOURCE, "the mission found nothing at all") is None
    assert jd.locate_quote(SOURCE, "") is None


# ---------------------------------------------------------------- 四道硬规矩


def test_supported_with_a_verbatim_quote_keeps_the_source_text_not_the_reply():
    verdict, quote, start, end, line, notes = jd.apply_rules(
        "supported", PERIOD_QUOTE.upper().replace("THE DART", "The DART"), SOURCE
    )
    # 大小写不同 → 不是逐字片段（空白差异是唯一被容许的偏差）
    assert verdict == "unverifiable" and quote is None
    assert "not found verbatim" in notes[0]

    verdict, quote, start, end, line, notes = jd.apply_rules("supported", PERIOD_QUOTE, SOURCE)
    assert verdict == "supported"
    assert quote == PERIOD_QUOTE
    assert (start, end) == (len(SOURCE) - len(PERIOD_QUOTE) - 1, len(SOURCE) - 1)
    assert line == 5
    assert notes == []


def test_a_verdict_without_a_quote_is_downgraded():
    verdict, quote, *_rest, notes = jd.apply_rules("unsupported", None, SOURCE)
    assert verdict == "unverifiable"
    assert quote is None
    assert notes == ["verdict returned without a verbatim quote; downgraded to unverifiable"]


def test_a_quote_that_is_not_in_the_source_is_downgraded():
    verdict, quote, *_rest, notes = jd.apply_rules("unsupported", "the paper says the opposite", SOURCE)
    assert verdict == "unverifiable"
    assert quote is None
    assert notes == ["quote is not found verbatim in the cited source; downgraded to unverifiable"]


def test_a_quote_with_an_ellipsis_is_not_one_contiguous_span():
    verdict, quote, *_rest, notes = jd.apply_rules(
        "partial", "The Alpha mission measured … the inner accretion disk.", SOURCE
    )
    assert verdict == "unverifiable" and quote is None
    assert "ellipsis" in notes[0]


def test_a_model_reported_source_missing_does_not_override_a_resolving_chain():
    verdict, *_rest, notes = jd.apply_rules("source-missing", None, SOURCE)
    assert verdict == "unverifiable"
    assert "decided by the chain" in notes[0]


def test_an_unrecognised_verdict_is_recorded_as_unverifiable():
    verdict, *_rest, notes = jd.apply_rules("looks fine to me", PERIOD_QUOTE, SOURCE)
    assert verdict == "unverifiable"
    assert "unrecognised verdict" in notes[0]


def test_an_unverifiable_verdict_does_not_keep_the_offered_quote():
    verdict, quote, *_rest, notes = jd.apply_rules("unverifiable", PERIOD_QUOTE, SOURCE)
    assert verdict == "unverifiable" and quote is None
    assert notes == ["a quote was offered but no verdict rests on it, so it was not kept"]


def test_a_partial_verdict_keeps_its_quote():
    verdict, quote, start, end, line, notes = jd.apply_rules("partial", "water inventory", SOURCE)
    assert verdict == "partial" and quote == "water inventory" and notes == []


def test_a_quote_matching_only_after_collapsing_whitespace_is_flagged():
    verdict, quote, start, end, line, notes = jd.apply_rules(
        "supported", "water inventory\n   of the inner", SOURCE
    )
    assert verdict == "supported"
    assert quote == "water inventory of the inner"
    assert notes == ["quote matched after collapsing whitespace differences"]


# ---------------------------------------------------------------- 回信解析


def test_extract_verdict_reads_alternate_keys():
    assert jd.extract_verdict({"verdict": "partial", "quote": " q ", "reason": "why"}) == (
        "partial",
        "q",
        "why",
    )
    assert jd.extract_verdict({"label": "supported", "evidence": "x"}) == ("supported", "x", "")
    assert jd.extract_verdict([{"verdict": "supported", "quote": "x"}]) == ("supported", "x", "")
    assert jd.extract_verdict({"nope": 1}) == (None, None, "")
    assert jd.extract_verdict("a string") == (None, None, "")


# ---------------------------------------------------------------- 判定器


def test_no_candidates_are_never_judged_unsupported_and_never_call_the_model():
    def refuse(prompt):  # pragma: no cover
        raise AssertionError("no candidates must not reach the endpoint")

    result = jd.Judge(make_client(refuse)).judge(item(passages=(), candidates_status=STATUS_NONE), SOURCE)
    assert result.verdict == "unverifiable"
    assert result.second is None
    assert result.calls == 0
    assert "failed search is not evidence" in result.notes[0]


def test_a_supported_verdict_with_a_verbatim_quote_needs_no_second_pass():
    def responder(prompt):
        return chat_reply(json.dumps({"verdict": "supported", "quote": PERIOD_QUOTE, "rationale": "says so"}))

    client = make_client(responder)
    result = jd.Judge(client).judge(item(), SOURCE)
    assert result.verdict == "supported"
    assert result.quote == PERIOD_QUOTE
    assert result.quote_line == 5
    assert result.second is None and result.agree is None and result.needs_review is False
    assert result.calls == 1 and client.usage.requests == 1
    assert result.prompt_tokens == 100 and result.completion_tokens == 12


def test_the_prompt_carries_the_claim_the_translation_and_the_passages():
    holder = {}

    def responder(prompt):
        holder["first"] = prompt
        return chat_reply(json.dumps({"verdict": "unverifiable", "rationale": "not enough"}))

    jd.Judge(make_client(responder)).judge(item(), SOURCE)
    prompt = holder["first"]
    assert "DART 撞击使双卫一的轨道周期改变了 33 分钟。" in prompt
    assert "The DART impact changed the orbital period" in prompt
    assert PERIOD_QUOTE in prompt
    assert "Reply with JSON only" in prompt
    assert "Never answer \"unsupported\" merely because the search found nothing." in prompt


def test_unsupported_triggers_an_oppositely_framed_second_pass_and_keeps_both():
    seen: list[str] = []

    def responder(prompt):
        seen.append(prompt)
        if "Second review, opposite framing" in prompt:
            return chat_reply(
                json.dumps({"verdict": "supported", "quote": PERIOD_QUOTE, "rationale": "it does say that"})
            )
        return chat_reply(
            json.dumps({"verdict": "unsupported", "quote": "measured the water inventory", "rationale": "no"})
        )

    client = make_client(responder)
    result = jd.Judge(client).judge(item(), SOURCE)
    assert len(seen) == 2
    assert "A first reviewer judged this claim \"unsupported\" by the cited work." in seen[1]
    assert "Its rationale was:" in seen[1]
    assert result.verdict == "unsupported"       # 第一遍为准
    assert result.first.verdict == "unsupported"
    assert result.second is not None and result.second.verdict == "supported"
    assert result.agree is False and result.needs_review is True
    assert any("disagree" in note for note in result.notes)
    assert client.usage.requests == 2


def test_a_partial_first_pass_gets_the_partial_specific_framing():
    seen: list[str] = []

    def responder(prompt):
        seen.append(prompt)
        if "Second review" in prompt:
            return chat_reply(json.dumps({"verdict": "partial", "quote": "water inventory"}))
        return chat_reply(json.dumps({"verdict": "partial", "quote": "water inventory", "rationale": "half"}))

    result = jd.Judge(make_client(responder)).judge(item(), SOURCE)
    assert "said to be missing is in fact present" in seen[1]
    assert result.agree is True and result.needs_review is False


def test_an_accusation_without_a_quote_is_downgraded_yet_still_checked_from_the_other_side():
    seen: list[str] = []

    def responder(prompt):
        seen.append(prompt)
        return chat_reply(json.dumps({"verdict": "unsupported", "rationale": "the search found nothing"}))

    result = jd.Judge(make_client(responder)).judge(item(), SOURCE)
    assert result.verdict == "unverifiable"
    assert result.quote is None
    assert result.first.raw_verdict == "unsupported"
    assert result.second is not None
    assert len(seen) == 2
    assert any("downgraded" in note for note in result.notes)


def test_weak_candidates_are_flagged_on_the_record():
    def responder(prompt):
        return chat_reply(json.dumps({"verdict": "supported", "quote": "water inventory"}))

    result = jd.Judge(make_client(responder)).judge(item(candidates_status=STATUS_WEAK), SOURCE)
    assert result.verdict == "supported"
    assert any("weak" in note for note in result.notes)


def test_a_call_that_returns_nothing_becomes_unverifiable_not_a_verdict():
    def responder(prompt):
        return chat_reply("", finish="length", completion_tokens=16384)

    client = make_client(responder)
    result = jd.Judge(client, max_retries=0).judge(item(), SOURCE)
    assert result.verdict == "unverifiable"
    assert result.first.error and "empty message" in result.first.error
    assert result.calls == 1


def test_a_malformed_reply_becomes_unverifiable_after_the_retries():
    def responder(prompt):
        return chat_reply("I think the claim is mostly fine.")

    client = make_client(responder)
    result = jd.Judge(client, max_retries=1).judge(item(), SOURCE)
    assert result.verdict == "unverifiable"
    assert "could not be used" in result.first.notes[0]
    assert client.usage.requests == 2
    assert result.calls == 2


def test_a_fenced_json_reply_is_repaired():
    def responder(prompt):
        body = json.dumps({"verdict": "supported", "quote": PERIOD_QUOTE, "rationale": "ok"})
        return chat_reply(f"```json\n{body}\n```")

    assert jd.Judge(make_client(responder)).judge(item(), SOURCE).verdict == "supported"


def test_the_pair_record_keeps_both_passes_side_by_side():
    def responder(prompt):
        if "Second review" in prompt:
            return chat_reply(json.dumps({"verdict": "partial", "quote": "water inventory", "rationale": "some"}))
        return chat_reply(json.dumps({"verdict": "unsupported", "quote": PERIOD_QUOTE, "rationale": "no"}))

    payload = jd.Judge(make_client(responder)).judge(item(), SOURCE).as_dict()
    assert payload["verdict"] == "unsupported"
    assert payload["second_pass"]["verdict"] == "partial"
    assert payload["first_pass"]["verdict"] == "unsupported"
    assert payload["agree"] is False
    assert payload["needs_review"] is True
    assert payload["translation"].startswith("The DART impact")
    assert payload["citation"] == 292
