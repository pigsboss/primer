# -*- coding: utf-8 -*-
"""OpenAI 兼容端点的最小客户端，以及"只要 JSON"的回信解析。

只有两件事：把一条提示词发出去、把回信里的正文取出来；以及把回信正文里的 JSON
片段找出来。传输层可注入（:data:`Transport`），测试因此不碰网络。

写这一份而不复用 ``primer.book.inspect`` 是有意的：那个模块正被并发改动，而且它
是视觉校对，与本包没有共同接口。两个客户端的合并是一件应当另做的收尾工作（同样的
"chat completions + 空正文视为失败 + 围栏/平衡括号修复"在这两个包里各有一份）。

关于本工程实测到的端点行为（``deepseek-flash``）：

* 它是**始终思考**的模型，推理链与正文**共享**输出预算。给两页图片时推理链会吃掉
  全部 16,384 个输出 token、正文整个为空。因此每次请求只送一个小项目，
  ``max_tokens`` 显式给足（见 :data:`DEFAULT_MAX_TOKENS`），并且**把空正文当作可重试
  的调用失败**，绝不当作"模型没有意见"。
* 密钥只从环境变量读，本模块不读任何凭据文件，也不把密钥写进日志或产物。
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Mapping, Optional

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"
DEFAULT_KEY_ENV = "PRIMER_API_KEY"

DEFAULT_MAX_TOKENS = 16384
"""输出预算。思考型模型的推理链也吃这个预算，给足余量见模块开头。"""

HTTP_TIMEOUT = 180.0


class LlmCallError(Exception):
    """端点不可达、回信不是 HTTP 意义上的 chat completions，或正文为空。"""


class LlmReplyError(Exception):
    """回信正文里找不到可用的 JSON。"""


@dataclass(frozen=True)
class HttpRequest:
    """一次 HTTP 调用；传输层可注入，测试不碰网络。"""

    url: str
    headers: Mapping[str, str]
    body: bytes
    timeout: float


Transport = Callable[[HttpRequest], bytes]


@dataclass
class UsageTotals:
    """一次运行累计的用量。``requests`` 数的是**实际发出的 HTTP 请求**，重试也计入。"""

    requests: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0

    def add(self, usage: Optional[Mapping[str, object]]) -> None:
        self.requests += 1
        if not isinstance(usage, Mapping):
            return
        self.prompt_tokens += _int(usage.get("prompt_tokens"))
        self.completion_tokens += _int(usage.get("completion_tokens"))

    def as_dict(self) -> dict:
        return {
            "requests": self.requests,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.prompt_tokens + self.completion_tokens,
        }


@dataclass(frozen=True)
class ChatReply:
    """一次成功的调用：正文、结束原因、服务端报的用量。"""

    content: str
    finish_reason: str
    usage: Mapping[str, object] = field(default_factory=dict)


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def chat_url(base_url: str) -> str:
    """OpenAI 兼容的 chat completions 地址。"""
    return base_url.rstrip("/") + "/chat/completions"


def chat_payload(model: str, prompt: str, max_tokens: int = DEFAULT_MAX_TOKENS) -> bytes:
    """构造一次文本补全的请求体。``temperature`` 取 0：判定与翻译都不需要采样。"""
    body = {
        "model": model,
        "temperature": 0,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


def urllib_transport(request: HttpRequest) -> bytes:
    """默认传输层：stdlib ``urllib``，不引入任何依赖。"""
    http_request = urllib.request.Request(
        request.url, data=request.body, headers=dict(request.headers), method="POST"
    )
    try:
        with urllib.request.urlopen(http_request, timeout=request.timeout) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:400]
        raise LlmCallError(f"HTTP {error.code} from {request.url}: {detail}") from error
    except (urllib.error.URLError, OSError, TimeoutError) as error:
        raise LlmCallError(f"cannot reach {request.url}: {error}") from error


def parse_chat_reply(raw: bytes) -> ChatReply:
    """从回信里取出 ``choices[0]`` 的正文与结束原因；正文为空时**报错**而不是返回空串。"""
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except (json.JSONDecodeError, ValueError) as error:
        raise LlmCallError(f"endpoint returned non-JSON: {raw[:200]!r}") from error
    if not isinstance(payload, dict):
        raise LlmCallError("endpoint returned a JSON value that is not an object")
    if "choices" not in payload and payload.get("error"):
        raise LlmCallError(f"endpoint returned an error: {str(payload['error'])[:300]}")
    choices = payload.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        raise LlmCallError(f"endpoint returned no choices: {json.dumps(payload)[:200]}")
    choice = choices[0]
    message = choice.get("message") or {}
    content = message.get("content")
    if isinstance(content, list):
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    finish = str(choice.get("finish_reason") or "")
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    if not isinstance(content, str) or not content.strip():
        # 空正文不是"模型没有意见"：思考型模型把预算花在推理链上时正文可能整个为空。
        raise LlmCallError(
            f"endpoint returned an empty message (finish_reason={finish or 'unknown'}, "
            f"usage={json.dumps(usage) if usage else '{}'})"
        )
    return ChatReply(content=content, finish_reason=finish, usage=usage)


class ChatClient:
    """一个模型 + 一个端点的客户端；累计用量，不重试（重试策略由调用方决定）。"""

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        model: str = DEFAULT_MODEL,
        key: str = "",
        transport: Optional[Transport] = None,
        timeout: float = HTTP_TIMEOUT,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ):
        self.base_url = base_url
        self.model = model
        self.key = key
        self.transport = transport or urllib_transport
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.usage = UsageTotals()

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.model and self.key)

    def complete(self, prompt: str, *, max_tokens: Optional[int] = None) -> ChatReply:
        """发一条提示词，返回正文。空正文与传输错误一样抛 :class:`LlmCallError`。"""
        request = HttpRequest(
            url=chat_url(self.base_url),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.key}"},
            body=chat_payload(self.model, prompt, max_tokens or self.max_tokens),
            timeout=self.timeout,
        )
        raw = self.transport(request)
        try:
            reply = parse_chat_reply(raw)
        except LlmCallError:
            # 回信到了但正文为空（思考型模型把预算花在推理链上）：请求确实发出去了，
            # 记账要算它，否则"花了多少请求"会少算这一类失败。
            self.usage.requests += 1
            raise
        self.usage.add(reply.usage)
        return reply


# ---------------------------------------------------------------- 回信 JSON


def json_candidates(text: str) -> list[str]:
    """回信里可能的 JSON 片段：整段、围栏内、首个平衡结构、逐行对象。"""
    candidates: list[str] = []
    stripped = text.strip()
    if stripped:
        candidates.append(stripped)
    for block in re.findall(r"```(?:json)?\s*(.*?)```", text, flags=re.S):
        if block.strip():
            candidates.append(block.strip())
    for opener, closer in (("{", "}"), ("[", "]")):
        start = stripped.find(opener)
        while start != -1:
            balanced = _first_balanced(stripped[start:], opener, closer)
            if balanced:
                candidates.append(balanced)
                break
            start = stripped.find(opener, start + 1)
    for line in stripped.splitlines():
        line = line.strip().rstrip(",")
        if line.startswith("{") and line.endswith("}"):
            candidates.append(line)
    return candidates


def _first_balanced(text: str, opener: str, closer: str) -> Optional[str]:
    """从 ``text[0]`` 起取第一个括号平衡的片段（忽略字符串内的括号）。"""
    depth = 0
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return text[: index + 1]
    return None


def parse_json_reply(text: str) -> object:
    """把回信正文解析成 JSON；围栏、前后闲话都尽量修复，实在不行抛 :class:`LlmReplyError`。"""
    for candidate in json_candidates(text):
        try:
            return json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
    raise LlmReplyError(f"reply is not parseable JSON: {_squeeze(text, 300)!r}")


def _squeeze(text: str, limit: int) -> str:
    return re.sub(r"\s+", " ", text).strip()[:limit]


__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_KEY_ENV",
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_MODEL",
    "HTTP_TIMEOUT",
    "ChatClient",
    "ChatReply",
    "HttpRequest",
    "LlmCallError",
    "LlmReplyError",
    "Transport",
    "UsageTotals",
    "chat_payload",
    "chat_url",
    "json_candidates",
    "parse_chat_reply",
    "parse_json_reply",
    "urllib_transport",
]
