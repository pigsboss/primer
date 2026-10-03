# -*- coding: utf-8 -*-
"""primer 的通用聊天客户端：多轮消息、文本与图片分片，端点由配置的 role 决定。

``primer.slides.client`` 与 ``primer.claims.client`` 各有一份"一条提示词发出去"的最小
客户端，都是为各自的单发场景写的（一次一问、回信取 JSON）。会话舱环路要的是另一种
东西：**多轮对话**（system/user/assistant 的完整历史）、**可选图片分片**（视觉模型看
刚渲染出来的成图）、以及**可注入的传输层**（测试一次网络都不发）。这一份把这些收在
一处，不重复实现传输与回信解析。

与 slides 那份一致的两点：

* 端点由 :func:`primer.config.resolve_role` 解析（provider／model／max_tokens／
  temperature），密钥经 :func:`primer.config.api_key` 只从环境变量读。本模块记的、
  报的，永远是**变量名**，绝不回显密钥值，也不把密钥写进日志或产物。
* ``temperature`` 是端点级事实而不是偏好：写死在端点上的值优先，``None`` 时兜底 0。

回信里的图片走 ``image_url`` 分片，URL 是 ``data:image/jpeg;base64,…``（端点要的是
自包含的 data-URI，不接受本地路径）。:func:`image_data_uri` 把本地图片转成 JPEG，
超过字节预算时先降质量、再逐级缩小，直到装得下。

错误分三级，调用方据此分派（会话舱环路拿 HTTP 4xx 判"端点拒绝图片"并降级为纯文本）：

* :class:`LlmTransportError` —— 端点不可达、DNS／连接／超时，**唯一可重试**的一类；
* :class:`LlmHttpError` —— 端点回了非 200，消息带状态码与正文片段；
* :class:`LlmReplyError` —— 回了 200 但信封不是 chat completions，或正文为空。

网络错误默认退避重试 1 次（:data:`HTTP_RETRIES`）；HTTP 与回信错误不重试——重发
一个被端点拒绝的请求只会再被拒一次，还多花一次钱。
"""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, replace
from io import BytesIO
from pathlib import Path
from typing import Any, Callable, List, Mapping, Optional, Sequence, Union

from .config import Config, Endpoint, api_key, resolve_role

__all__ = [
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_TEMPERATURE",
    "HTTP_RETRIES",
    "HTTP_TIMEOUT",
    "IMAGE_JPEG_QUALITY",
    "IMAGE_MAX_BYTES",
    "ChatClient",
    "ChatReply",
    "HttpRequest",
    "LlmError",
    "LlmHttpError",
    "LlmReplyError",
    "LlmTransportError",
    "Transport",
    "chat_payload",
    "chat_url",
    "client_for_role",
    "content_for",
    "image_data_uri",
    "parse_chat_reply",
    "urllib_transport",
    "usage_of",
    "user_message",
]

# 输出预算的兜底值。思考型模型的推理链与正文共享预算，给足余量。
DEFAULT_MAX_TOKENS = 16384
# 端点没写 temperature 时的兜底：0（判定与建模不需要采样）。
DEFAULT_TEMPERATURE = 0.0
# 单次请求的超时；长回信（整章 JSON）在慢端点上可能要跑满。
HTTP_TIMEOUT = 180.0
# 传输层错误的重试次数与退避基数（仅网络错误）。
HTTP_RETRIES = 1
RETRY_BACKOFF_SECONDS = 1.0
# 单张内联图片的预算与编码质量。
IMAGE_MAX_BYTES = 4 * 1024 * 1024
IMAGE_JPEG_QUALITY = 80


class LlmError(Exception):
    """本模块所有错误的基类。消息一律英文（见 CODING_STANDARDS v1.1 §2.2）。"""


class LlmTransportError(LlmError):
    """端点不可达：DNS 失败、连接被拒、读超时。**唯一可重试**的一类错误。"""


class LlmHttpError(LlmError):
    """端点回了非 200。``status`` 供调用方分派（4xx 常是"这个模型不收图片"）。"""

    def __init__(self, status: int, url: str, detail: str = "") -> None:
        self.status = int(status)
        self.url = url
        self.detail = detail
        super().__init__(f"HTTP {self.status} from {url}: {detail[:300]}")


class LlmReplyError(LlmError):
    """回了 200，但信封不是 chat completions，或正文为空。"""


@dataclass(frozen=True)
class HttpRequest:
    """一次 HTTP 调用；传输层可注入，测试不碰网络。"""

    url: str
    headers: Mapping[str, str]
    body: bytes
    timeout: float


Transport = Callable[[HttpRequest], bytes]


@dataclass(frozen=True)
class ChatReply:
    """一次成功的调用：正文、端点报回的模型名、用量、耗时与尝试次数。"""

    content: str
    model: str
    finish_reason: str = ""
    usage: Mapping[str, Any] = field(default_factory=dict)
    seconds: float = 0.0
    attempts: int = 1


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def chat_url(base_url: str) -> str:
    """OpenAI 兼容的 chat completions 地址。"""
    return base_url.rstrip("/") + "/chat/completions"


# ---------------------------------------------------------------- 消息与图片


def content_for(text: str, images: Sequence[Union[str, os.PathLike]] = ()) -> Union[str, List[dict]]:
    """一条消息的内容：没有图片时是纯文本，有图片时是文本＋image_url 分片。

    图片一律转成自包含的 JPEG data-URI（见 :func:`image_data_uri`）；端点不收本地
    路径，只认 ``image_url.url`` 里的 data-URI。
    """
    if not images:
        return text
    parts: List[dict] = [{"type": "text", "text": text}]
    for path in images:
        parts.append({"type": "image_url", "image_url": {"url": image_data_uri(path)}})
    return parts


def user_message(
    text: str, images: Sequence[Union[str, os.PathLike]] = ()
) -> dict:
    """一条 user 消息；图片以 data-URI 分片附上。"""
    return {"role": "user", "content": content_for(text, images)}


def image_data_uri(
    path: Union[str, os.PathLike],
    *,
    max_bytes: int = IMAGE_MAX_BYTES,
    quality: int = IMAGE_JPEG_QUALITY,
) -> str:
    """本地图片 → ``data:image/jpeg;base64,…``；超过预算先降质量、再逐级缩小。"""
    from PIL import Image  # 延迟导入：本模块的纯 HTTP 用法不必拖上 Pillow

    source = Path(path)
    try:
        with Image.open(source) as image:
            frame = image.convert("RGB")
    except Exception as exc:  # Pillow 的异常种类很多，一律收成一条英文消息
        raise LlmError(f"cannot read image {source}: {exc}") from exc
    data = _encode_jpeg(frame, max_bytes, quality)
    return "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")


def _encode_jpeg(frame: Any, max_bytes: int, quality: int) -> bytes:
    """把一帧编成不超过 ``max_bytes`` 的 JPEG：先试几个质量档，再逐级缩小。"""
    for _ in range(5):
        for q in (quality, max(quality - 20, 40), 40, 30):
            buffer = BytesIO()
            frame.save(buffer, "JPEG", quality=q, optimize=True)
            if buffer.tell() <= max_bytes:
                return buffer.getvalue()
        width, height = frame.size
        frame = frame.resize((max(1, width // 2), max(1, height // 2)))
    raise LlmError(
        "image does not fit the inline payload budget even after downscaling; "
        "raise max_bytes or shrink the source"
    )


# ---------------------------------------------------------------- 请求与回信


def chat_payload(
    model: str,
    messages: Sequence[Mapping[str, Any]],
    max_tokens: int = DEFAULT_MAX_TOKENS,
    temperature: Optional[float] = None,
) -> dict:
    """构造一次 chat completions 的请求体（保持 messages 的原始顺序与分片）。"""
    return {
        "model": model,
        "temperature": DEFAULT_TEMPERATURE if temperature is None else temperature,
        "max_tokens": max_tokens,
        "messages": [dict(message) for message in messages],
    }


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
        raise LlmHttpError(error.code, request.url, detail) from error
    except (urllib.error.URLError, OSError, TimeoutError) as error:
        raise LlmTransportError(f"cannot reach {request.url}: {error}") from error


def usage_of(payload: Mapping[str, Any]) -> dict:
    """把端点报的 usage 收成四项；``reasoning_tokens`` 若有就单列（它同样是钱）。"""
    usage = payload.get("usage")
    usage = usage if isinstance(usage, Mapping) else {}
    details = usage.get("completion_tokens_details")
    if isinstance(details, Mapping):
        reasoning = _int(details.get("reasoning_tokens"))
    else:
        reasoning = _int(usage.get("reasoning_tokens"))
    prompt = _int(usage.get("prompt_tokens"))
    completion = _int(usage.get("completion_tokens"))
    total = _int(usage.get("total_tokens")) or prompt + completion
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "reasoning_tokens": reasoning,
    }


def parse_chat_reply(raw: bytes) -> ChatReply:
    """从回信里取出 ``choices[0]`` 的正文；信封不对或正文为空都**报错**。"""
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except (json.JSONDecodeError, ValueError) as error:
        raise LlmReplyError(f"endpoint returned non-JSON: {raw[:200]!r}") from error
    if not isinstance(payload, Mapping):
        raise LlmReplyError("endpoint returned a JSON value that is not an object")
    if "choices" not in payload and payload.get("error"):
        raise LlmReplyError(f"endpoint returned an error: {str(payload['error'])[:300]}")
    choices = payload.get("choices") or []
    if not choices or not isinstance(choices[0], Mapping):
        raise LlmReplyError(f"endpoint returned no choices: {json.dumps(payload)[:200]}")
    choice = choices[0]
    message = choice.get("message") or {}
    content = message.get("content") if isinstance(message, Mapping) else None
    if isinstance(content, list):
        content = "".join(
            part.get("text", "")
            for part in content
            if isinstance(part, Mapping) and isinstance(part.get("text"), str)
        )
    finish = str(choice.get("finish_reason") or "")
    model = str(payload.get("model") or "")
    usage = usage_of(payload)
    if not isinstance(content, str) or not content.strip():
        # 空正文不是"模型没有意见"：思考型模型把预算花在推理链上时正文可能整个为空。
        raise LlmReplyError(
            f"endpoint returned an empty message (finish_reason={finish or 'unknown'}, "
            f"usage={json.dumps(usage)})"
        )
    return ChatReply(content=content, model=model, finish_reason=finish, usage=usage)


# ---------------------------------------------------------------- 客户端


class ChatClient:
    """一个端点 + 一个模型的聊天客户端；网络错误退避重试，其余错误直接上报。"""

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
        retries: int = HTTP_RETRIES,
        sleep: Optional[Callable[[float], None]] = None,
    ) -> None:
        self.base_url = base_url
        self.model = model
        self.key = key
        self.transport = transport or urllib_transport
        self.timeout = float(timeout)
        self.max_tokens = int(max_tokens)
        self.temperature = DEFAULT_TEMPERATURE if temperature is None else temperature
        self.retries = max(0, int(retries))
        # 退避用可注入的 sleep：测试不想真等。
        self.sleep = sleep or time.sleep

    @property
    def url(self) -> str:
        return chat_url(self.base_url)

    def payload(
        self, messages: Sequence[Mapping[str, Any]], max_tokens: Optional[int] = None
    ) -> dict:
        """本次请求的 JSON 体（测试直接看它，不必解析字节流）。"""
        return chat_payload(
            self.model,
            messages,
            max_tokens if max_tokens is not None else self.max_tokens,
            self.temperature,
        )

    def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        max_tokens: Optional[int] = None,
        retries: Optional[int] = None,
    ) -> ChatReply:
        """发一串多轮消息，返回正文；网络错误退避重试 ``retries`` 次。"""
        limit = self.retries if retries is None else max(0, int(retries))
        attempt = 0
        start = time.monotonic()
        while True:
            request = HttpRequest(
                url=self.url,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.key}",
                },
                body=json.dumps(
                    self.payload(messages, max_tokens), ensure_ascii=False
                ).encode("utf-8"),
                timeout=self.timeout,
            )
            try:
                raw = self.transport(request)
            except LlmTransportError:
                if attempt < limit:
                    attempt += 1
                    self.sleep(RETRY_BACKOFF_SECONDS * attempt)
                    continue
                raise
            reply = parse_chat_reply(raw)
            return replace(
                reply,
                model=reply.model or self.model,
                seconds=time.monotonic() - start,
                attempts=attempt + 1,
            )


def client_for_role(
    config: Config,
    role: str,
    *,
    environ: Optional[Mapping[str, str]] = None,
    transport: Optional[Transport] = None,
    timeout: float = HTTP_TIMEOUT,
    max_tokens: Optional[int] = None,
    retries: int = HTTP_RETRIES,
    sleep: Optional[Callable[[float], None]] = None,
) -> ChatClient:
    """按配置里的 role 建一个客户端：端点与模型来自配置，密钥来自环境变量。

    缺密钥时 :func:`primer.config.api_key` 报的错只点名**变量名**，本函数原样让它
    冒泡——报错里不含密钥值。``max_tokens`` 给了就覆盖 role 里的值。
    """
    env = os.environ if environ is None else environ
    endpoint: Endpoint = resolve_role(config, role, max_tokens=max_tokens)
    return ChatClient(
        base_url=endpoint.base_url,
        model=endpoint.model,
        key=api_key(endpoint, env),
        transport=transport,
        timeout=timeout,
        max_tokens=endpoint.max_tokens or DEFAULT_MAX_TOKENS,
        temperature=endpoint.temperature,
        retries=retries,
        sleep=sleep,
    )
