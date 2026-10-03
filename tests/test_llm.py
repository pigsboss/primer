# -*- coding: utf-8 -*-
"""``primer.llm`` 的测试：请求报文形状、图片分片、用量解析、错误分级与重试、密钥纪律。

全程**不联网**：所有用例都注入假传输层（:data:`primer.llm.Transport`），回信是构造好
的字节串；密钥用假的，机器级配置指到一个空的 ``XDG_CONFIG_HOME``。缺密钥的用例只断言
**变量名**出现在错误里，并断言假密钥的值一个字都没泄漏。
"""

from __future__ import annotations

import json

import pytest

from primer.config import ConfigError, load_config
from primer.llm import (
    ChatClient,
    LlmError,
    LlmHttpError,
    LlmReplyError,
    LlmTransportError,
    chat_url,
    client_for_role,
    content_for,
    image_data_uri,
    parse_chat_reply,
    usage_of,
    user_message,
)

def _write_png(path):
    """写一张真正能被 Pillow 读回来的小图（不是伪造的字节串）。"""
    from PIL import Image

    Image.new("RGB", (8, 8), (10, 20, 30)).save(path)
    return path


KEY = "unit-test-secret-key"

CONFIG_YAML = """\
providers:
  demo:
    base_url: https://api.example.invalid/v1
    key_env: PRIMER_DEMO_API_KEY
    temperature: 0.25
roles:
  scene:
    provider: demo
    model: demo-model
    max_tokens: 2048
"""


class FakeTransport:
    """假传输层：按次序回放构造好的字节串，或抛出构造好的异常。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        if not self.replies:
            raise AssertionError("unexpected extra LLM request")
        item = self.replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def reply_bytes(content="你好", model="demo-model", usage=None) -> bytes:
    payload = {
        "model": model,
        "choices": [
            {"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
        ],
        "usage": usage
        if usage is not None
        else {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def make_config(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(CONFIG_YAML, encoding="utf-8")
    environ = {"XDG_CONFIG_HOME": str(tmp_path / "xdg"), "HOME": str(tmp_path)}
    return load_config(tmp_path, config_file, environ=environ)


def make_client(transport, **kwargs):
    defaults = dict(
        base_url="https://api.example.invalid/v1",
        model="demo-model",
        key="fake-key",
        transport=transport,
        max_tokens=1024,
        temperature=0.0,
        retries=0,
    )
    defaults.update(kwargs)
    return ChatClient(**defaults)


# ------------------------------------------------------------------ 请求形状

def test_request_shape_carries_multi_turn_messages_and_knobs():
    transport = FakeTransport([reply_bytes()])
    client = make_client(transport, max_tokens=512, temperature=0.3)
    messages = [
        {"role": "system", "content": "系统提示"},
        {"role": "user", "content": "第一条"},
        {"role": "assistant", "content": "回话"},
        {"role": "user", "content": "第二条"},
    ]
    reply = client.complete(messages)

    assert transport.requests, "client must send exactly one request"
    request = transport.requests[0]
    assert request.url == chat_url("https://api.example.invalid/v1")
    assert request.url.endswith("/chat/completions")
    assert request.headers["Content-Type"] == "application/json"
    assert request.headers["Authorization"].startswith("Bearer ")
    # 密钥值不出现在 header 之外的任何地方是本模块的纪律；这里只确认它确实随 Bearer 走。
    assert request.headers["Authorization"] == "Bearer fake-key"

    body = json.loads(request.body.decode("utf-8"))
    assert body["model"] == "demo-model"
    assert body["max_tokens"] == 512
    assert body["temperature"] == 0.3
    assert [m["role"] for m in body["messages"]] == ["system", "user", "assistant", "user"]
    assert body["messages"][3]["content"] == "第二条"
    assert reply.content == "你好"
    assert reply.model == "demo-model"
    assert reply.finish_reason == "stop"
    assert reply.attempts == 1


def test_request_body_is_callers_messages_not_mutated():
    transport = FakeTransport([reply_bytes()])
    client = make_client(transport)
    messages = [{"role": "user", "content": "原样"}]
    client.complete(messages)
    assert messages == [{"role": "user", "content": "原样"}]


# ------------------------------------------------------------------ 图片分片

def test_image_part_is_a_data_uri_and_text_only_when_no_images(tmp_path):
    image = _write_png(tmp_path / "shot.png")

    assert content_for("看图") == "看图"
    parts = content_for("看图", [image])
    assert isinstance(parts, list)
    assert parts[0] == {"type": "text", "text": "看图"}
    assert parts[1]["type"] == "image_url"
    assert parts[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert image_data_uri(image).startswith("data:image/jpeg;base64,")


def test_image_message_survives_into_the_request_body(tmp_path):
    image = _write_png(tmp_path / "shot.png")
    transport = FakeTransport([reply_bytes()])
    client = make_client(transport)
    client.complete([user_message("看看这张", [image])])
    body = json.loads(transport.requests[0].body.decode("utf-8"))
    content = body["messages"][0]["content"]
    assert isinstance(content, list)
    assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


# ------------------------------------------------------------------ 用量

def test_usage_parses_reasoning_tokens_from_both_shapes():
    nested = usage_of(
        {
            "usage": {
                "prompt_tokens": 7,
                "completion_tokens": 9,
                "total_tokens": 16,
                "completion_tokens_details": {"reasoning_tokens": 4},
            }
        }
    )
    assert nested == {
        "prompt_tokens": 7,
        "completion_tokens": 9,
        "total_tokens": 16,
        "reasoning_tokens": 4,
    }
    flat = usage_of({"usage": {"prompt_tokens": 1, "completion_tokens": 2, "reasoning_tokens": 3}})
    assert flat["reasoning_tokens"] == 3
    assert flat["total_tokens"] == 3  # 端点没报 total 时按输入＋输出兜底


def test_reply_usage_and_seconds_come_back_on_the_result():
    usage = {
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "total_tokens": 120,
        "completion_tokens_details": {"reasoning_tokens": 60},
    }
    transport = FakeTransport([reply_bytes(usage=usage)])
    reply = make_client(transport).complete([{"role": "user", "content": "hi"}])
    assert reply.usage["reasoning_tokens"] == 60
    assert reply.usage["total_tokens"] == 120
    assert reply.seconds >= 0.0


# ------------------------------------------------------------------ 错误分级与重试

def test_transport_error_retries_once_and_then_succeeds():
    slept = []
    transport = FakeTransport([LlmTransportError("cannot reach host"), reply_bytes("第二次才好")])
    client = make_client(transport, retries=1, sleep=slept.append)
    reply = client.complete([{"role": "user", "content": "hi"}])
    assert reply.content == "第二次才好"
    assert reply.attempts == 2
    assert len(transport.requests) == 2
    assert slept, "the retry must back off through the injected sleep"


def test_transport_error_raises_after_retries_are_exhausted():
    transport = FakeTransport([LlmTransportError("down"), LlmTransportError("still down")])
    client = make_client(transport, retries=1, sleep=lambda _s: None)
    with pytest.raises(LlmTransportError):
        client.complete([{"role": "user", "content": "hi"}])
    assert len(transport.requests) == 2


def test_http_error_is_not_retried_and_carries_the_status():
    transport = FakeTransport([LlmHttpError(400, "https://api.example.invalid/v1/chat/completions", "bad image")])
    client = make_client(transport, retries=1, sleep=lambda _s: None)
    with pytest.raises(LlmHttpError) as caught:
        client.complete([{"role": "user", "content": "hi"}])
    assert caught.value.status == 400
    assert len(transport.requests) == 1, "an HTTP rejection must not be retried"


def test_empty_content_is_a_reply_error_and_is_not_retried():
    transport = FakeTransport([reply_bytes(content="")])
    client = make_client(transport, retries=1, sleep=lambda _s: None)
    with pytest.raises(LlmReplyError):
        client.complete([{"role": "user", "content": "hi"}])
    assert len(transport.requests) == 1


def test_garbage_body_is_a_reply_error():
    transport = FakeTransport([b"not json at all"])
    with pytest.raises(LlmReplyError):
        make_client(transport).complete([{"role": "user", "content": "hi"}])


def test_error_envelope_without_choices_is_reported():
    transport = FakeTransport([json.dumps({"error": {"message": "boom"}}).encode("utf-8")])
    with pytest.raises(LlmReplyError) as caught:
        make_client(transport).complete([{"role": "user", "content": "hi"}])
    assert "boom" in str(caught.value)


def test_parse_chat_reply_joins_list_content():
    raw = json.dumps(
        {
            "model": "demo-model",
            "choices": [
                {"message": {"content": [{"type": "text", "text": "甲"}, {"type": "text", "text": "乙"}]}}
            ],
        }
    ).encode("utf-8")
    assert parse_chat_reply(raw).content == "甲乙"


# ------------------------------------------------------------------ 配置与密钥

def test_client_for_role_reads_endpoint_and_key_from_config(tmp_path):
    config = make_config(tmp_path)
    transport = FakeTransport([reply_bytes()])
    client = client_for_role(
        config,
        "scene",
        environ={"PRIMER_DEMO_API_KEY": KEY},
        transport=transport,
    )
    assert client.model == "demo-model"
    assert client.max_tokens == 2048
    assert client.temperature == 0.25  # 端点级事实：provider 上的值
    client.complete([{"role": "user", "content": "hi"}])
    body = json.loads(transport.requests[0].body.decode("utf-8"))
    assert body["model"] == "demo-model"
    assert body["max_tokens"] == 2048


def test_missing_key_names_the_variable_and_leaks_no_value(tmp_path):
    config = make_config(tmp_path)
    with pytest.raises(ConfigError) as caught:
        client_for_role(config, "scene", environ={})
    message = str(caught.value)
    assert "PRIMER_DEMO_API_KEY" in message
    assert KEY not in message
    assert "unit-test-secret" not in message


def test_blank_key_is_treated_as_missing(tmp_path):
    config = make_config(tmp_path)
    with pytest.raises(ConfigError) as caught:
        client_for_role(config, "scene", environ={"PRIMER_DEMO_API_KEY": "   "})
    assert "PRIMER_DEMO_API_KEY" in str(caught.value)


def test_undefined_role_is_a_config_error(tmp_path):
    config = make_config(tmp_path)
    with pytest.raises(ConfigError):
        client_for_role(config, "nope", environ={"PRIMER_DEMO_API_KEY": KEY})


def test_a_failed_call_never_writes_the_key_anywhere(tmp_path):
    """回信错误里带上端点原文时，Authorization 头里的密钥仍不得出现在异常文本里。"""
    transport = FakeTransport([LlmTransportError("cannot reach host")])
    client = make_client(transport, key=KEY, retries=0)
    with pytest.raises(LlmError) as caught:
        client.complete([{"role": "user", "content": "hi"}])
    assert KEY not in str(caught.value)
