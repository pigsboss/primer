# -*- coding: utf-8 -*-
"""``select`` 自己的最小端点客户端：一条提示词发出去，回信正文里的一段 JSON 取回来。

与 :mod:`primer.claims.client` 是**两份独立实现**，不是同一份的两个入口——那边正在被并发
改动，而两个客户端的合并是一件应当另做的收尾工作。这一份只服务幻灯片圈要点，独立成文，
好让 slides 包的读者不必跨包去读另一份的逻辑。

与 claims 那份有两处**有意不同**：

* ``temperature`` 由 :class:`primer.config.Endpoint` 给出，不是写死的常量。这是端点级
  事实而不是偏好：Kimi Code 的端点上所有模型**只接受 1**，给 0 会被 400 拒掉；DeepSeek
  用 0。端点为 ``None`` 时由调用方兜底 0（见 :data:`DEFAULT_TEMPERATURE`）。
* 用量多记一项 ``reasoning_tokens``（思考型模型花在推理链上的那部分）。它同样是钱，
  账单里不能当它不存在。

传输层可注入（:data:`Transport`）：测试给一个构造好回信的可调用对象，一次网络都不会发。
密钥只从环境变量读（由 :func:`primer.config.api_key` 取出后传进来），本模块不读任何凭据
文件，也不把密钥写进日志或产物。
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Mapping, Optional, Tuple

# 输出预算的兜底值。圈要点一次要回整章的 JSON（若干帧 × 3–5 条 + 理由），给 8192 够宽；
# 思考型模型的推理链也吃这个预算，所以调用方可以按端点用 --max-tokens 调大。
DEFAULT_MAX_TOKENS = 8192
# 端点没写 temperature 时的兜底：0（判定与选取不需要采样）。
DEFAULT_TEMPERATURE = 0.0

HTTP_TIMEOUT = 180.0


class SelectCallError(Exception):
    """端点不可达、回信不是 HTTP 意义上的 chat completions，或正文为空。"""


class SelectReplyError(Exception):
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
    reasoning_tokens: int = 0

    def add(self, usage: Optional[Mapping[str, object]]) -> None:
        self.requests += 1
        if not isinstance(usage, Mapping):
            return
        self.prompt_tokens += _int(usage.get("prompt_tokens"))
        self.completion_tokens += _int(usage.get("completion_tokens"))
        self.reasoning_tokens += _reasoning(usage)

    def snapshot(self) -> Tuple[int, int, int, int]:
        """当前计数的快照，用来算某一章的增量（:func:`delta`）。"""
        return (self.requests, self.prompt_tokens, self.completion_tokens, self.reasoning_tokens)

    def as_dict(self) -> dict:
        return {
            "requests": self.requests,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "reasoning_tokens": self.reasoning_tokens,
        }


def delta(usage: UsageTotals, snapshot: Tuple[int, int, int, int]) -> Tuple[int, int, int, int]:
    """快照之后的增量（请求数、输入、输出、推理）。"""
    return (
        usage.requests - snapshot[0],
        usage.prompt_tokens - snapshot[1],
        usage.completion_tokens - snapshot[2],
        usage.reasoning_tokens - snapshot[3],
    )


@dataclass(frozen=True)
class ChatReply:
    """一次成功的调用：正文、结束原因、服务端报的用量。"""

    content: str
    finish_reason: str
    usage: Mapping[str, object] = field(default_factory=dict)


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _reasoning(usage: Mapping[str, object]) -> int:
    """推理 token 的两个位置：``completion_tokens_details.reasoning_tokens`` 与平铺的同名键。"""
    details = usage.get("completion_tokens_details")
    if isinstance(details, Mapping):
        return _int(details.get("reasoning_tokens"))
    return _int(usage.get("reasoning_tokens"))


def chat_url(base_url: str) -> str:
    """OpenAI 兼容的 chat completions 地址。"""
    return base_url.rstrip("/") + "/chat/completions"


def chat_payload(
    model: str,
    prompt: str,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    temperature: Optional[float] = None,
) -> bytes:
    """构造一次文本补全的请求体；``temperature`` 为 ``None`` 时取 0（见模块文档）。"""
    body = {
        "model": model,
        "temperature": DEFAULT_TEMPERATURE if temperature is None else temperature,
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
        raise SelectCallError(f"HTTP {error.code} from {request.url}: {detail}") from error
    except (urllib.error.URLError, OSError, TimeoutError) as error:
        raise SelectCallError(f"cannot reach {request.url}: {error}") from error


def parse_chat_reply(raw: bytes) -> ChatReply:
    """从回信里取出 ``choices[0]`` 的正文与结束原因；正文为空时**报错**而不是返回空串。"""
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except (json.JSONDecodeError, ValueError) as error:
        raise SelectCallError(f"endpoint returned non-JSON: {raw[:200]!r}") from error
    if not isinstance(payload, dict):
        raise SelectCallError("endpoint returned a JSON value that is not an object")
    if "choices" not in payload and payload.get("error"):
        raise SelectCallError(f"endpoint returned an error: {str(payload['error'])[:300]}")
    choices = payload.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        raise SelectCallError(f"endpoint returned no choices: {json.dumps(payload)[:200]}")
    choice = choices[0]
    message = choice.get("message") or {}
    content = message.get("content")
    if isinstance(content, list):
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    finish = str(choice.get("finish_reason") or "")
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    if not isinstance(content, str) or not content.strip():
        # 空正文不是"模型没有意见"：思考型模型把预算花在推理链上时正文可能整个为空。
        raise SelectCallError(
            f"endpoint returned an empty message (finish_reason={finish or 'unknown'}, "
            f"usage={json.dumps(usage) if usage else '{}'})"
        )
    return ChatReply(content=content, finish_reason=finish, usage=usage)


class ChatClient:
    """一个模型 + 一个端点的客户端；累计用量，不重试（重试策略由调用方决定）。"""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        key: str = "",
        transport: Optional[Transport] = None,
        timeout: float = HTTP_TIMEOUT,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: Optional[float] = None,
    ):
        self.base_url = base_url
        self.model = model
        self.key = key
        self.transport = transport or urllib_transport
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.temperature = DEFAULT_TEMPERATURE if temperature is None else temperature
        self.usage = UsageTotals()

    def complete(self, prompt: str, *, max_tokens: Optional[int] = None) -> ChatReply:
        """发一条提示词，返回正文。空正文与传输错误一样抛 :class:`SelectCallError`。"""
        request = HttpRequest(
            url=chat_url(self.base_url),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.key}"},
            body=chat_payload(
                self.model, prompt, max_tokens or self.max_tokens, self.temperature
            ),
            timeout=self.timeout,
        )
        raw = self.transport(request)
        try:
            reply = parse_chat_reply(raw)
        except SelectCallError:
            # 回信到了但正文为空（思考型模型把预算花在推理链上）：请求确实发出去了，
            # 记账要算它，否则"花了多少请求"会少算这一类失败。
            self.usage.requests += 1
            raise
        self.usage.add(reply.usage)
        return reply


# ---------------------------------------------------------------- 回信 JSON


def json_candidates(text: str) -> list:
    """回信里可能的 JSON 片段：整段、围栏内、首个平衡结构、逐行对象。

    "只要 JSON"的回信经常被围上一圈话或一个 ```json 围栏。这里把所有可能的片段按
    "越像整体越靠前"的次序排出来，交给 :func:`parse_json_reply` 逐个试。
    """
    candidates: list = []
    stripped = text.strip()
    if stripped:
        candidates.append(stripped)
    for block in re.findall(r"```(?:json)?\s*(.*?)```", text, flags=re.S):
        if block.strip():
            candidates.append(block.strip())
    for opener, closer in (("{", "}"), ("[", "]")):
        # 逐个开括号都试：前面可能先有一个"不是 JSON 的"花括号（模型爱拿它举例），
        # 只看第一个平衡结构会把真正的 JSON 漏掉。
        position = 0
        collected = 0
        while collected < 20:
            start = stripped.find(opener, position)
            if start == -1:
                break
            balanced = first_balanced(stripped[start:], opener, closer)
            if balanced:
                candidates.append(balanced)
                collected += 1
                position = start + len(balanced)
            else:
                position = start + 1
    for line in stripped.splitlines():
        line = line.strip().rstrip(",")
        if line.startswith("{") and line.endswith("}"):
            candidates.append(line)
    return candidates


def first_balanced(text: str, opener: str, closer: str) -> Optional[str]:
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
    """把回信正文解析成 JSON；围栏、前后闲话都尽量修复，实在不行抛 :class:`SelectReplyError`。"""
    for candidate in json_candidates(text):
        try:
            return json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
    raise SelectReplyError(f"reply is not parseable JSON: {_squeeze(text, 300)!r}")


def _squeeze(text: str, limit: int) -> str:
    return re.sub(r"\s+", " ", text).strip()[:limit]


__all__ = [
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_TEMPERATURE",
    "HTTP_TIMEOUT",
    "ChatClient",
    "ChatReply",
    "HttpRequest",
    "SelectCallError",
    "SelectReplyError",
    "Transport",
    "UsageTotals",
    "chat_payload",
    "chat_url",
    "delta",
    "first_balanced",
    "json_candidates",
    "parse_chat_reply",
    "parse_json_reply",
    "urllib_transport",
]
