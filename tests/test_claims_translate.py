# -*- coding: utf-8 -*-
"""``primer.claims.translate`` 的单元测试：切批、缓存、回信解析与失败降级。

传输层是假的（一个把提示词映射成回信的纯函数），不联网、不读真实语料，
也**不读任何凭据文件**。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from primer.claims import translate as tr
from primer.claims.client import ChatClient, LlmCallError


def chat_reply(content: str, *, finish: str = "stop", prompt_tokens: int = 7, completion_tokens: int = 3) -> bytes:
    """把一段正文包成一份 chat completions 回信。"""
    return json.dumps(
        {
            "choices": [{"message": {"content": content}, "finish_reason": finish}],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
        }
    ).encode("utf-8")


def endpoint(responder):
    """假传输层：把请求体里的提示词交给 ``responder``，回什么就发什么。"""
    seen: list[str] = []

    def transport(request):
        prompt = json.loads(request.body.decode("utf-8"))["messages"][0]["content"]
        seen.append(prompt)
        return responder(prompt)

    transport.seen = seen
    return transport


def make_client(responder) -> ChatClient:
    return ChatClient(key="test-key", transport=endpoint(responder))


# ---------------------------------------------------------------- 键与切批


def test_the_cache_key_is_a_stable_hash_of_the_claim_text():
    key = tr.claim_key("罗塞塔号实现人类首次彗星伴飞与着陆。")
    assert key == tr.claim_key("罗塞塔号实现人类首次彗星伴飞与着陆。")
    assert key != tr.claim_key("另一句话。")
    assert len(key) == 16 and all(char in "0123456789abcdef" for char in key)


def test_batches_respect_both_the_character_budget_and_the_item_limit():
    items = [(f"k{index}", "字" * 30) for index in range(6)]
    batches = tr.plan_batches(items, max_chars=70, max_items=2)
    assert [len(batch) for batch in batches] == [2, 2, 2]
    batches = tr.plan_batches(items, max_chars=45, max_items=10)
    assert [len(batch) for batch in batches] == [1, 1, 1, 1, 1, 1]


def test_a_single_claim_longer_than_the_budget_gets_a_batch_of_its_own():
    items = [("a", "x" * 5000), ("b", "y")]
    batches = tr.plan_batches(items, max_chars=100, max_items=4)
    assert [len(batch) for batch in batches] == [1, 1]
    assert batches[0][0][0] == "a"


def test_the_prompt_numbers_the_inputs_from_one_and_carries_every_claim():
    prompt = tr.build_translation_prompt([("a", "第一句。"), ("b", "Second line\nbroken.")])
    assert "Reply with JSON only" in prompt
    assert "1. 第一句。" in prompt
    assert "2. Second line broken." in prompt
    assert "罗塞塔号 -> Rosetta" in prompt


# ---------------------------------------------------------------- 回信解析


def test_a_well_formed_reply_is_read_in_order():
    payload = {"translations": [{"id": 1, "en": "One."}, {"id": 2, "en": "Two."}]}
    got = tr.parse_translation_reply(payload, [("a", "一。"), ("b", "二。")])
    assert got == {"a": "One.", "b": "Two."}


def test_a_list_reply_and_alternate_keys_are_repaired():
    payload = [{"id": "a", "translation": "One."}, {"id": 2, "text": "Two."}]
    got = tr.parse_translation_reply(payload, [("a", "一。"), ("b", "二。")])
    assert got == {"a": "One.", "b": "Two."}


def test_items_missing_from_the_reply_are_left_out_for_the_caller_to_retry():
    payload = {"translations": [{"id": 1, "en": "One."}]}
    got = tr.parse_translation_reply(payload, [("a", "一。"), ("b", "二。")])
    assert got == {"a": "One."}


def test_an_unusable_payload_yields_nothing_instead_of_raising():
    assert tr.parse_translation_reply({"nope": []}, [("a", "一。")]) == {}


# ---------------------------------------------------------------- 缓存


def test_the_cache_round_trips_through_disk(tmp_path):
    path = tmp_path / "translations.json"
    cache = tr.TranslationCache(path, model="m")
    assert cache.get("句子。") is None
    cache.put("句子。", "A sentence.")
    cache.save()
    again = tr.TranslationCache(path, model="m")
    assert again.get("句子。") == "A sentence."
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["version"] == tr.CACHE_VERSION
    entry = next(iter(payload["translations"].values()))
    assert entry["source"] == "句子。" and entry["model"] == "m"


def test_a_corrupt_cache_is_treated_as_an_empty_cache(tmp_path):
    path = tmp_path / "translations.json"
    path.write_text("{not json", encoding="utf-8")
    assert tr.TranslationCache(path).get("句子。") is None
    path.write_text('{"translations": "nope"}', encoding="utf-8")
    assert tr.TranslationCache(path).get("句子。") is None


# ---------------------------------------------------------------- 翻译器


def test_a_batch_is_translated_in_one_request_and_written_to_the_cache(tmp_path):
    texts = ["第一句。", "第二句。"]

    def responder(prompt):
        assert prompt.count("1. 第一句。") == 1
        return chat_reply(
            json.dumps({"translations": [{"id": 1, "en": "One."}, {"id": 2, "en": "Two."}]})
        )

    client = make_client(responder)
    cache = tr.TranslationCache(tmp_path / "t.json", model=client.model)
    translator = tr.Translator(client, cache)
    out = translator.translate_all(texts)
    assert out["第一句。"].translation == "One."
    assert out["第二句。"].translation == "Two."
    assert all(item.status == tr.STATUS_OK for item in out.values())
    assert client.usage.requests == 1
    assert translator.stats.as_dict() == {
        "sentences": 2,
        "cache_hits": 0,
        "translated": 2,
        "failed": 0,
        "requests": 1,
        "batches": 1,
    }
    assert tr.TranslationCache(tmp_path / "t.json").get("第一句。") == "One."


def test_a_second_run_costs_nothing(tmp_path):
    def responder(prompt):
        return chat_reply(json.dumps({"translations": [{"id": 1, "en": "One."}]}))

    tr.Translator(make_client(responder), tr.TranslationCache(tmp_path / "t.json")).translate_all(["第一句。"])

    def refuse(prompt):  # pragma: no cover - 走不到这里就该让测试红
        raise AssertionError("the second run must not touch the endpoint")

    client = make_client(refuse)
    translator = tr.Translator(client, tr.TranslationCache(tmp_path / "t.json"))
    out = translator.translate_all(["第一句。"])
    assert out["第一句。"].translation == "One."
    assert out["第一句。"].cached is True
    assert client.usage.requests == 0
    assert translator.stats.cache_hits == 1


def test_an_empty_reply_splits_the_batch_into_singletons(tmp_path):
    calls: list[str] = []

    def responder(prompt):
        calls.append(prompt)
        if "2. 第二句。" in prompt:
            # 整批的请求：正文为空（思考型模型把预算花在推理链上的典型形状）
            return chat_reply("", finish="length", completion_tokens=16384)
        return chat_reply(json.dumps({"translations": [{"id": 1, "en": prompt.split("1. ")[1].split("\n")[0]}]}))

    client = make_client(responder)
    translator = tr.Translator(client, tr.TranslationCache(tmp_path / "t.json"))
    out = translator.translate_all(["第一句。", "第二句。"])
    assert len(calls) == 3
    assert out["第一句。"].translation == "第一句。"
    assert out["第二句。"].translation == "第二句。"
    assert translator.stats.failed == 0


def test_a_claim_that_keeps_failing_is_recorded_and_does_not_stop_the_run(tmp_path):
    def responder(prompt):
        return chat_reply("not json at all")

    client = make_client(responder)
    translator = tr.Translator(client, tr.TranslationCache(tmp_path / "t.json"), max_retries=1)
    out = translator.translate_all(["第一句。", "第二句。"])
    assert all(item.status == tr.STATUS_FAILED for item in out.values())
    assert all(item.translation is None for item in out.values())
    assert all(item.error for item in out.values())
    assert translator.stats.failed == 2
    # 一批失败 → 折半成两条单条，各重试 max_retries+1 次。
    assert client.usage.requests == 1 + 2 * 2


def test_a_partial_reply_only_retries_the_missing_items(tmp_path):
    seen: list[str] = []

    def responder(prompt):
        seen.append(prompt)
        if "1. 第一句。" in prompt and "2. 第二句。" in prompt:
            return chat_reply(json.dumps({"translations": [{"id": 1, "en": "One."}]}))
        return chat_reply(json.dumps({"translations": [{"id": 1, "en": "Two."}]}))

    client = make_client(responder)
    translator = tr.Translator(client, tr.TranslationCache(tmp_path / "t.json"))
    out = translator.translate_all(["第一句。", "第二句。"])
    assert out["第一句。"].translation == "One."
    assert out["第二句。"].translation == "Two."
    assert len(seen) == 2
    assert "1. 第一句。" not in seen[1]


def test_a_reply_that_never_matches_is_a_failure_not_a_silent_empty_translation(tmp_path):
    def responder(prompt):
        return chat_reply(json.dumps({"translations": []}))

    client = make_client(responder)
    translator = tr.Translator(client, tr.TranslationCache(tmp_path / "t.json"), max_retries=0)
    out = translator.translate_all(["第一句。"])
    assert out["第一句。"].status == tr.STATUS_FAILED
    assert out["第一句。"].error == "reply carried no translation for this item"


def test_a_fenced_reply_is_repaired(tmp_path):
    def responder(prompt):
        body = json.dumps({"translations": [{"id": 1, "en": "One."}]})
        return chat_reply(f"Here you go:\n```json\n{body}\n```\n")

    client = make_client(responder)
    translator = tr.Translator(client, tr.TranslationCache(tmp_path / "t.json"))
    out = translator.translate_all(["第一句。"])
    assert out["第一句。"].translation == "One."


def test_a_transport_failure_is_a_translation_failure_not_an_exception(tmp_path):
    def responder(prompt):
        raise LlmCallError("cannot reach the endpoint")

    client = make_client(responder)
    translator = tr.Translator(client, tr.TranslationCache(tmp_path / "t.json"), max_retries=0)
    out = translator.translate_all(["第一句。"])
    assert out["第一句。"].status == tr.STATUS_FAILED
    assert "cannot reach" in (out["第一句。"].error or "")


def test_duplicate_claims_are_translated_once(tmp_path):
    seen: list[str] = []

    def responder(prompt):
        seen.append(prompt)
        return chat_reply(json.dumps({"translations": [{"id": 1, "en": "One."}]}))

    client = make_client(responder)
    translator = tr.Translator(client, tr.TranslationCache(tmp_path / "t.json"))
    out = translator.translate_all(["第一句。", "第一句。", "第一句。"])
    assert list(out) == ["第一句。"]
    assert len(seen) == 1
    assert translator.stats.sentences == 1
    assert translator.stats.cache_hits == 0
