# -*- coding: utf-8 -*-
"""迭代出图主环：续号、比例降级、换道重试、台账。

一次运行吃三样东西：**提示词 md**（按 ``## figX-Y`` 分节，一节一图）、**图号**、
**档位／参考图／输出目录**；吐出一张一行的 ``log.jsonl`` 台账。

* **自动续号**：落盘名一律 ``<fig>-zh_v<N><ext>``，``N`` = 扫输出目录里已有同名前缀的
  最大号 + 1。绝不覆盖已有成图——一张定稿被一次"再试一张"抹掉，是最贵的事故。
* **比例降级**：候选比例逐个试，被 400 拒就退下一个，全拒再退到不带比例（见
  :func:`primer.figure.adapters.gemini.generate`）。
* **换道重试一次**：传输类失败（端不可达）不是提示词的问题，重发同一个通道没意义——
  先把该档 :func:`primer.figure.routing.mark_unhealthy`，再重新解析一次通道，在新通道上
  重试**一次**；第二次仍失败就如实记错（台账里 ``ok: false`` 与 ``error``），不假装成功。
* **台账字段**：``{ts, fig, mode, attempt, model, size, ref, ok, path, sec, bytes, mime,
  ratio, channel[, error]}``——``ref`` 只记 basename，``channel`` 记这一次实际走的通道
  （换道这件事不记下来，事后就无从复盘）。

runner 本身不打印、不联网（传输层可注入），报告归 CLI：测试因此能只验逻辑。
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence, Union

from . import DEFAULT_MODEL, MODE_ZH, routing
from .adapters import AdapterError, AdapterTransportError, Transport, gemini

__all__ = [
    "LOG_NAME",
    "PROMPT_MARKER",
    "FigureRun",
    "build_log_row",
    "load_prompts",
    "next_index",
    "run_figures",
]

LOG_NAME = "log.jsonl"
# 提示词段里的起点标记：段首的说明性文字（用途、存放约定）不属于提示词本身。
PROMPT_MARKER = "画一幅"

_HEADING_RE = re.compile(r"^##\s+(\S+)")
_TS_FORMAT = "%Y-%m-%d %H:%M:%S"


# ---------------------------------------------------------------- 提示词

def load_prompts(path: Union[str, Path]) -> dict[str, str]:
    """按 ``## figX-Y`` 分节加载提示词 md，返回 ``{图号: 提示词}``。

    每节取标题后的正文，砍掉第一条 ``---`` 分隔线之后的内容，再从 :data:`PROMPT_MARKER`
    （"画一幅"）开始截断——节标题与说明性文字是给人看的，不进提示词。
    """
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    prompts: dict[str, str] = {}
    current: Optional[str] = None
    buffer: list[str] = []
    for raw in text.splitlines():
        match = _HEADING_RE.match(raw)
        if match:
            _store(prompts, current, buffer)
            current, buffer = match.group(1), []
            continue
        if current is not None:
            buffer.append(raw)
    _store(prompts, current, buffer)
    return prompts


def _store(prompts: dict[str, str], figure: Optional[str], buffer: Sequence[str]) -> None:
    if figure is None:
        return
    body = _section_body("\n".join(buffer))
    if body:
        prompts[figure] = body


def _section_body(text: str) -> str:
    """一节正文 → 提示词：砍掉 ``---`` 之后的内容，再截到 :data:`PROMPT_MARKER` 起。

    没有标记的一节**不是提示词**（说明、约定、台账表格这类也常用 ``##`` 起头），
    返回空串由 :func:`load_prompts` 丢掉——把说明文字当提示词发给模型，等于花钱画一段
    自己写给自己的话。标记由 :func:`primer.figure.meta.assemble_spec` 保证出现在段首。
    """
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.strip() == "---":
            lines = lines[:index]
            break
    body = "\n".join(lines).strip()
    marker = body.find(PROMPT_MARKER)
    if marker < 0:
        return ""
    return body[marker:].strip()


# ---------------------------------------------------------------- 续号与台账

def next_index(out_dir: Union[str, Path], fig: str, *, mode: str = MODE_ZH) -> int:
    """下一个可用序号：扫描 ``<fig>-<mode>_v<N>.<ext>``，取最大 ``N`` + 1（没有则 1）。"""
    pattern = re.compile(r"^%s-%s_v(\d+)\.[^.]+$" % (re.escape(fig), re.escape(mode)))
    directory = Path(out_dir)
    if not directory.is_dir():
        return 1
    numbers = []
    for entry in directory.iterdir():
        match = pattern.match(entry.name)
        if match:
            numbers.append(int(match.group(1)))
    return (max(numbers) + 1) if numbers else 1


def build_log_row(*, fig: str, attempt: int, model: str, size: Optional[str],
                  ref: Optional[Union[str, Path]], ok: bool, mode: str = MODE_ZH,
                  path: Optional[Union[str, Path]] = None, sec: float = 0.0,
                  size_bytes: Optional[int] = None, mime: Optional[str] = None,
                  ratio: Optional[str] = None, channel: Optional[str] = None,
                  error: Optional[str] = None, now: Optional[float] = None) -> dict:
    """台账一行；成功与失败共用同一套键（失败时 ``error`` 非空，其余为 ``None``）。"""
    stamp = time.time() if now is None else now
    row = {
        "ts": time.strftime(_TS_FORMAT, time.localtime(stamp)),
        "fig": fig,
        "mode": mode,
        "attempt": int(attempt),
        "model": model,
        "size": size,
        "ref": Path(ref).name if ref else None,
        "ok": bool(ok),
        "path": None if path is None else str(path),
        "sec": round(float(sec), 1),
        "bytes": None if size_bytes is None else int(size_bytes),
        "mime": mime,
        "ratio": ratio,
        "channel": channel,
    }
    if error:
        row["error"] = str(error)
    return row


@dataclass
class FigureRun:
    """一次运行的结果：逐张台账、输出目录、台账路径、起手选中的通道。"""

    rows: list[dict] = field(default_factory=list)
    out_dir: Path = Path(".")
    log_path: Optional[Path] = None
    channel: Optional[routing.ChannelChoice] = None

    @property
    def ok_count(self) -> int:
        return sum(1 for row in self.rows if row["ok"])

    @property
    def failure_count(self) -> int:
        return len(self.rows) - self.ok_count

    @property
    def failures(self) -> list[dict]:
        return [row for row in self.rows if not row["ok"]]


# ---------------------------------------------------------------- 主环

def run_figures(figs: Iterable[str], prompt_md: Union[str, Path], *,
                out_dir: Union[str, Path], n: int = 1, size: Optional[str] = None,
                ref: Optional[Union[str, Path]] = None, model: str = DEFAULT_MODEL,
                api_key: Optional[str] = None, proxy: Optional[str] = None,
                ratios: Sequence[str] = (), ratio_map: Optional[Mapping[str, Sequence[str]]] = None,
                transport: Optional[Transport] = None, provider: str = "gemini",
                profiles: Optional[routing.ChannelProfiles] = None,
                profiles_path: Optional[Union[str, Path]] = None,
                preference: Optional[Union[str, Path]] = None,
                cache_path: Optional[Union[str, Path]] = None,
                tcp: Optional[routing.TcpCheck] = None, get: Optional[routing.HttpGet] = None,
                environ: Optional[Mapping[str, str]] = None, now: Optional[float] = None,
                log_name: str = LOG_NAME, route: bool = True,
                on_row: Optional[Callable[[dict], None]] = None) -> FigureRun:
    """对每个图号连出 ``n`` 张（自动续号），逐张落盘并追加台账。

    ``route=False`` 只跳过"按 default_order 探活"这一级（显式／环境变量给的通道照旧生效），
    CLI 的 ``--no-probe`` 与测试用它避免顺手发起网络请求。``api_key=None`` 时从
    ``PRIMER_GEMINI_API_KEY`` 读（回退裸名 ``GEMINI_API_KEY``）；读不到就报错（消息只点名变量，不带值）。
    ``tcp``／``get`` 是探活用的可注入网络原语（同 :mod:`primer.figure.routing`），
    测试传假函数即一次真网络都不发。
    """
    env = os.environ if environ is None else environ
    prompts = load_prompts(prompt_md)
    directory = Path(out_dir)
    if api_key is None:
        api_key = gemini.api_key_from_env(env)   # 密钥缺失要在建目录、探活之前就报出来
    directory.mkdir(parents=True, exist_ok=True)
    log_path = directory / log_name

    channel = routing.resolve_channel(
        provider, explicit=proxy, profiles=profiles,
        profiles_path=None if profiles_path is None else Path(profiles_path),
        preference=None if preference is None else Path(preference),
        cache_path=None if cache_path is None else Path(cache_path),
        environ=env, now=now, allow_probe=route, tcp=tcp, get=get)

    rows: list[dict] = []
    with log_path.open("a", encoding="utf-8") as log:
        for fig in figs:
            if fig not in prompts:
                row = build_log_row(fig=fig, attempt=0, model=model, size=None, ref=None,
                                    ok=False, channel=channel.name, now=now,
                                    error="prompt section missing in %s" % prompt_md)
                rows.append(row)
                _write(log, row, on_row)
                continue
            start = next_index(directory, fig)
            for attempt in range(start, start + max(1, int(n))):
                row = _one_attempt(
                    fig=fig, prompt=prompts[fig], attempt=attempt, out_dir=directory,
                    size=size, ref=ref, model=model, api_key=api_key,
                    ratios=tuple((ratio_map or {}).get(fig, ratios)), transport=transport,
                    provider=provider, proxy=proxy, profiles=profiles,
                    profiles_path=profiles_path, preference=preference, cache_path=cache_path,
                    tcp=tcp, get=get, environ=env, now=now, route=route, channel=channel)
                rows.append(row)
                _write(log, row, on_row)
    return FigureRun(rows=rows, out_dir=directory, log_path=log_path, channel=channel)


def _write(log, row: Mapping[str, Any], on_row: Optional[Callable[[dict], None]]) -> None:
    log.write(json.dumps(dict(row), ensure_ascii=False) + "\n")
    log.flush()
    if on_row is not None:
        on_row(dict(row))


def _one_attempt(*, fig: str, prompt: str, attempt: int, out_dir: Path,
                 size: Optional[str], ref: Optional[Union[str, Path]], model: str,
                 api_key: str, ratios: Sequence[str], transport: Optional[Transport],
                 provider: str, proxy: Optional[str],
                 profiles: Optional[routing.ChannelProfiles],
                 profiles_path: Optional[Union[str, Path]],
                 preference: Optional[Union[str, Path]],
                 cache_path: Optional[Union[str, Path]],
                 tcp: Optional[routing.TcpCheck], get: Optional[routing.HttpGet],
                 environ: Mapping[str, str], now: Optional[float], route: bool,
                 channel: routing.ChannelChoice) -> dict:
    """出一张：失败换道重试一次，第二次仍失败就记错（不抛，主环继续走下一张）。

    换道时**不带显式 pin**（``explicit=None``）：带着它重新解析只会拿回同一条通道，
    等于原地重试。环境变量的表态仍然尊重——用户显式设了 ``<PROVIDER>_API_PROXY``，
    那是"这次就走这条"，我们照走，只是重试一次。已经失败的档由
    :func:`primer.figure.routing.mark_unhealthy` 写进缓存，探活那级自然会绕过它。
    """
    target = out_dir / ("%s-%s_v%d" % (fig, MODE_ZH, attempt))
    choice = channel
    started = time.time()
    try:
        result = gemini.generate(prompt, target, model=model, api_key=api_key,
                                 proxy=choice.url, ratios=ratios, size=size, ref=ref,
                                 transport=transport)
    except AdapterTransportError as exc:
        routing.mark_unhealthy(choice.name, url=choice.url, detail=str(exc),
                               cache_path=None if cache_path is None else Path(cache_path),
                               environ=environ, now=now)
        choice = routing.resolve_channel(
            provider, explicit=None, profiles=profiles,
            profiles_path=None if profiles_path is None else Path(profiles_path),
            preference=None if preference is None else Path(preference),
            cache_path=None if cache_path is None else Path(cache_path),
            environ=environ, now=now, allow_probe=route, tcp=tcp, get=get)
        try:
            result = gemini.generate(prompt, target, model=model, api_key=api_key,
                                     proxy=choice.url, ratios=ratios, size=size, ref=ref,
                                     transport=transport)
        except AdapterError as retry_error:
            return build_log_row(fig=fig, attempt=attempt, model=model, size=size, ref=ref,
                                 ok=False, channel=choice.name, now=now,
                                 sec=time.time() - started,
                                 error="%s (after failover to %s: %s)"
                                       % (exc, choice.name, retry_error))
    except AdapterError as exc:
        return build_log_row(fig=fig, attempt=attempt, model=model, size=size, ref=ref,
                             ok=False, channel=choice.name, now=now,
                             sec=time.time() - started, error=str(exc))
    return build_log_row(fig=fig, attempt=attempt, model=model, size=size, ref=ref, ok=True,
                         channel=choice.name, path=result.path, sec=result.seconds or
                         (time.time() - started), size_bytes=result.size_bytes,
                         mime=result.mime, ratio=result.ratio, now=now)
