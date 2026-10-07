# -*- coding: utf-8 -*-
"""代理通道：多环境档案、分级解析、探活、缓存与失败换道。

出图要过的墙不止一处（家、办公室、机房出口各一个代理；出口地区还会被模型方按国家码
拒掉 400），所以"用哪条通道"不能写死成一个 URL。本模块把这件事拆成三层：

1. **档案**（``~/.config/primer/proxies.yaml``，可用 ``PRIMER_PROXY_PROFILES`` 或函数参数
   换到别处）。schema::

       default_order: [home, office, direct]
       profiles:
         home:   {url: "http://192.168.1.2:7890", note: "家里"}
         office: {url: "http://10.0.0.2:3128",   note: "办公室"}
         direct: {url: null, note: "直连"}
       region_block: [CN, HK]

   ``url: null``（或档名就叫 ``direct``）＝直连，不探活、永远算通过。

2. **解析优先级**（:func:`resolve_channel`，逐级短路，命中即定）::

       显式参数（档名｜URL｜"direct"）
         > 环境变量 <PROVIDER>_API_PROXY（如 GEMINI_API_PROXY）
         > 环境变量 PRIMER_PROXY
         > 用户偏好文件 ~/.config/primer/preferred_proxy（纯文本档名）
         > 按 default_order 顺序探活，取首个通过
         > direct

   环境变量与显式参数都沿用"给 URL 即 URL、给档名即档名、给 ``direct`` 即直连"的
   同一套判读，所以一处会写、处处会写。

3. **探活**（:func:`probe_channel`，每档 ≤ 约 6 s，三步全过才算通过）：

   ① TCP 连通（2 s）——代理进程在不在；
   ② 穿代理 GET ``https://www.google.com/generate_204``（4 s，200／204 算过）——代理会不会转发 HTTPS；
   ③ 地区检查——穿代理 GET ``https://ipinfo.io/country``，国家码**不在** ``region_block`` 才过。

   第 ③ 步是**失败即拒**（不做"查不出来就放过"）：出口地区被拒是出图的硬失败，
   放过去只会把一次 400 留到真正花钱的那一次调用上。

**缓存**（:func:`probe_all`）：``~/.cache/primer/channels.json``（``XDG_CACHE_HOME`` 优先）。
成功的结论留 ``ttl_ok``（默认 900 s），失败只留 ``ttl_fail``（默认 120 s）——通道状态是
会变的，成功可以信一会儿，失败要尽快重试。运行中撞上传输失败时调
:func:`mark_unhealthy` 把该档写成一条"刚失败"，于是下一次解析不会再挑它。

**可注入**：所有网络都走 ``tcp``／``get`` 两个可注入函数（:func:`probe_channel` 与
:func:`probe_all` 的参数），测试一次真网络都不发；``now`` 同样可注入，缓存 TTL 才测得住。
"""

from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

import yaml

from ..envfile import user_config_dir

__all__ = [
    "CACHE_TTL_FAIL",
    "CACHE_TTL_OK",
    "DEFAULT_ORDER",
    "DIRECT",
    "PROBE_URL",
    "REGION_URL",
    "ROUTE_RULES",
    "ChannelChoice",
    "ChannelProbe",
    "ChannelProfiles",
    "ProxyProfile",
    "RouteError",
    "cache_entry_fresh",
    "default_cache_path",
    "explain",
    "list_channels",
    "load_cache",
    "load_profiles",
    "mark_unhealthy",
    "mask_proxy_url",
    "preference_path",
    "probe_all",
    "probe_channel",
    "profiles_path",
    "proxy_env_var",
    "resolve_channel",
    "save_cache",
]

DIRECT = "direct"
# 档案全缺时的兜底顺序：只有直连。
DEFAULT_ORDER = (DIRECT,)
PROBE_URL = "https://www.google.com/generate_204"
REGION_URL = "https://ipinfo.io/country"
PROBE_OK_STATUS = (200, 204)
TCP_TIMEOUT = 2.0
HTTP_TIMEOUT = 4.0
CACHE_TTL_OK = 900.0
CACHE_TTL_FAIL = 120.0
CACHE_VERSION = 1

ENV_PROFILES = "PRIMER_PROXY_PROFILES"
ENV_PREFERENCE = "PRIMER_PREFERRED_PROXY"
GLOBAL_PROXY_ENV = "PRIMER_PROXY"

# 解析规则的级别名（英文键值，供 JSON／测试用）；explain() 另给中文说法。
RULE_EXPLICIT = "explicit"
RULE_ENV_PROVIDER = "env-provider"
RULE_ENV_GLOBAL = "env-primer"
RULE_PREFERENCE = "preference"
RULE_PROBE = "probe"
RULE_FALLBACK = "default"
ROUTE_RULES = (RULE_EXPLICIT, RULE_ENV_PROVIDER, RULE_ENV_GLOBAL, RULE_PREFERENCE,
               RULE_PROBE, RULE_FALLBACK)

RULE_LABELS_CN = {
    RULE_EXPLICIT: "显式参数（--proxy）",
    RULE_ENV_PROVIDER: "环境变量 <PROVIDER>_API_PROXY",
    RULE_ENV_GLOBAL: "环境变量 PRIMER_PROXY",
    RULE_PREFERENCE: "用户偏好文件 preferred_proxy",
    RULE_PROBE: "default_order 顺序探活首个通过",
    RULE_FALLBACK: "直连兜底（不探活）",
}


class RouteError(Exception):
    """通道解析失败（档案坏了、档名不认识）。消息英文，CLI 折算成退出码 2。"""


# ---------------------------------------------------------------- 档案

@dataclass(frozen=True)
class ProxyProfile:
    """一档通道：``url=None``（或档名叫 direct）即直连。"""

    name: str
    url: Optional[str] = None
    note: str = ""

    @property
    def direct(self) -> bool:
        """直连档：没有代理 URL，或档名就是 ``direct``。"""
        return self.name == DIRECT or not (self.url or "").strip()


@dataclass(frozen=True)
class ChannelProfiles:
    """一份档案：探活顺序、各档定义、阻断地区码，外加它是从哪读来的（便于报错点名）。"""

    default_order: tuple[str, ...] = DEFAULT_ORDER
    profiles: Mapping[str, ProxyProfile] = field(default_factory=dict)
    region_block: tuple[str, ...] = ()
    path: Optional[Path] = None

    def profile(self, name: str) -> ProxyProfile:
        """按档名取档；不认识就报错（不悄悄回退成直连）。"""
        if name in self.profiles:
            return self.profiles[name]
        raise RouteError("unknown proxy profile: %r (known: %s; file: %s)"
                         % (name, ", ".join(sorted(self.profiles)) or "-",
                            self.path or "<defaults>"))

    def order(self) -> list[str]:
        """探活顺序：``default_order`` 里认得的档在前，其余档按名字补上，``direct`` 永远在最后。"""
        seen: list[str] = []
        for name in self.default_order:
            if name in self.profiles and name not in seen and name != DIRECT:
                seen.append(name)
        for name in sorted(self.profiles):
            if name not in seen and name != DIRECT:
                seen.append(name)
        seen.append(DIRECT)
        return seen


def profiles_path(environ: Optional[Mapping[str, str]] = None) -> Path:
    """档案路径：``PRIMER_PROXY_PROFILES`` > ``$XDG_CONFIG_HOME/primer/proxies.yaml``。"""
    env = os.environ if environ is None else environ
    override = (env.get(ENV_PROFILES) or "").strip()
    if override:
        return Path(override).expanduser()
    return user_config_dir(env) / "proxies.yaml"


def preference_path(environ: Optional[Mapping[str, str]] = None) -> Path:
    """用户偏好文件：``PRIMER_PREFERRED_PROXY`` > ``<配置目录>/preferred_proxy``。"""
    env = os.environ if environ is None else environ
    override = (env.get(ENV_PREFERENCE) or "").strip()
    if override:
        return Path(override).expanduser()
    return user_config_dir(env) / "preferred_proxy"


def default_cache_path(environ: Optional[Mapping[str, str]] = None) -> Path:
    """探活缓存：``$XDG_CACHE_HOME/primer/channels.json``，否则 ``~/.cache/primer/channels.json``。"""
    env = os.environ if environ is None else environ
    xdg = (env.get("XDG_CACHE_HOME") or "").strip()
    base = Path(xdg).expanduser() if xdg else Path.home() / ".cache"
    return base / "primer" / "channels.json"


def load_profiles(path: Optional[Path] = None, *,
                  environ: Optional[Mapping[str, str]] = None) -> ChannelProfiles:
    """读档案。文件不存在时返回"只有直连"的缺省档案（不算错——没配代理是正常情形）。"""
    target = Path(path).expanduser() if path is not None else profiles_path(environ)
    if not target.is_file():
        return ChannelProfiles(profiles={DIRECT: ProxyProfile(DIRECT, None, "直连")})
    try:
        doc = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        raise RouteError("cannot parse proxy profiles %s: %s" % (target, exc)) from exc
    if not isinstance(doc, dict):
        raise RouteError("proxy profiles must be a mapping: %s" % target)

    raw_profiles = doc.get("profiles") or {}
    if not isinstance(raw_profiles, dict):
        raise RouteError("'profiles' must be a mapping: %s" % target)
    profiles: dict[str, ProxyProfile] = {}
    for name, spec in raw_profiles.items():
        key = str(name)
        if spec is None:
            profile = ProxyProfile(key, None)
        elif isinstance(spec, str):
            profile = ProxyProfile(key, spec.strip() or None)
        elif isinstance(spec, dict):
            url = spec.get("url")
            profile = ProxyProfile(key, None if url is None else str(url).strip() or None,
                                   str(spec.get("note") or ""))
        else:
            raise RouteError("profile %r must be a mapping or a URL string: %s" % (key, target))
        profiles[key] = profile
    profiles.setdefault(DIRECT, ProxyProfile(DIRECT, None, "直连"))

    order_raw = doc.get("default_order") or []
    if isinstance(order_raw, str):
        order_raw = [order_raw]
    if not isinstance(order_raw, (list, tuple)):
        raise RouteError("'default_order' must be a list: %s" % target)
    order = tuple(str(item) for item in order_raw) or DEFAULT_ORDER

    block_raw = doc.get("region_block") or []
    if isinstance(block_raw, str):
        block_raw = [block_raw]
    if not isinstance(block_raw, (list, tuple)):
        raise RouteError("'region_block' must be a list of country codes: %s" % target)
    region_block = tuple(str(item).strip().upper() for item in block_raw if str(item).strip())

    return ChannelProfiles(default_order=order, profiles=profiles,
                           region_block=region_block, path=target)


# ---------------------------------------------------------------- 地址与掩码

def proxy_env_var(provider: str) -> str:
    """provider 名 → 约定的代理环境变量名 ``<PROVIDER>_API_PROXY``（``gemini`` → ``GEMINI_API_PROXY``）。"""
    stem = "".join(ch if ch.isalnum() else "_" for ch in str(provider)).strip("_").upper()
    return "%s_API_PROXY" % stem


def mask_proxy_url(url: Optional[str]) -> str:
    """把代理 URL 里的凭据掩掉：``http://user:pass@h:1`` → ``http://user:***@h:1``。

    报告、日志、异常里凡出现代理 URL 一律先过这里——代理口令与 API 密钥同级，
    没有理由因为它是"网络设置"就照抄进终端历史。
    """
    if not url:
        return "-"
    text = str(url)
    scheme, sep, rest = text.partition("://")
    if not sep:
        scheme, sep, rest = "", "", text
    authority, slash, tail = rest.partition("/")
    if "@" in authority:
        creds, _at, host = authority.rpartition("@")
        user, colon, _secret = creds.partition(":")
        masked = "%s:%s" % (user, "***") if colon else "***"
        authority = "%s@%s" % (masked, host)
    return "%s%s%s%s%s" % (scheme, sep, authority, slash, tail)


def parse_host_port(url: str) -> tuple[str, int]:
    """代理 URL → ``(host, port)``；缺端口按 scheme 补（http 80／https 443）。"""
    text = str(url).strip()
    scheme, sep, rest = text.partition("://")
    if not sep:
        rest = text
        scheme = "http"
    authority = rest.split("/", 1)[0].split("@")[-1]
    if authority.startswith("["):                       # IPv6 字面量
        host, _br, tail = authority[1:].partition("]")
        port_text = tail.lstrip(":") if tail.startswith(":") else ""
    else:
        host, sep, port_text = authority.partition(":")
        if not sep:
            port_text = ""
    if not host:
        raise RouteError("proxy url has no host: %s" % mask_proxy_url(url))
    if port_text:
        try:
            port = int(port_text)
        except ValueError as exc:
            raise RouteError("proxy url has a bad port: %s" % mask_proxy_url(url)) from exc
    else:
        port = 443 if scheme == "https" else 80
    return host, port


# ---------------------------------------------------------------- 探活

@dataclass(frozen=True)
class ChannelProbe:
    """一档的探活结论。``detail`` 是英文诊断；``seconds`` 是这次探活的耗时。"""

    name: str
    url: Optional[str]
    ok: bool
    detail: str = ""
    tcp_ok: bool = False
    http_ok: bool = False
    region_ok: bool = True
    country: Optional[str] = None
    seconds: float = 0.0
    cached: bool = False
    ts: float = 0.0

    def as_cache(self) -> dict:
        return {"ok": bool(self.ok), "ts": float(self.ts), "detail": self.detail,
                "seconds": round(float(self.seconds), 3), "url": self.url,
                "tcp_ok": bool(self.tcp_ok), "http_ok": bool(self.http_ok),
                "region_ok": bool(self.region_ok), "country": self.country}

    @classmethod
    def from_cache(cls, name: str, entry: Mapping[str, Any]) -> "ChannelProbe":
        return cls(name=name, url=entry.get("url"), ok=bool(entry.get("ok")),
                   detail=str(entry.get("detail") or ""), tcp_ok=bool(entry.get("tcp_ok")),
                   http_ok=bool(entry.get("http_ok")), region_ok=bool(entry.get("region_ok")),
                   country=entry.get("country"), seconds=float(entry.get("seconds") or 0.0),
                   cached=True, ts=float(entry.get("ts") or 0.0))


# 可注入的两个网络原语：TCP 探测返回连通与否；HTTP 探测返回 (状态码, 正文片段)。
TcpCheck = Callable[[str, int, float], bool]
HttpGet = Callable[[str, Optional[str], float], "tuple[int, str]"]


def tcp_check(host: str, port: int, timeout: float) -> bool:
    """默认 TCP 探测：连得上即通（不握手、不发字节）。"""
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def urllib_get(url: str, proxy: Optional[str], timeout: float) -> tuple[int, str]:
    """默认 HTTP 探测：``proxy=None`` 时**忽略环境里的 HTTP(S)_PROXY**，走真直连。

    ``ProxyHandler({})`` 是这里的要点：不带它的话，环境变量的代理会悄悄生效，"直连"
    就不再是直连——探活结论与实际出图走的通道必须是同一条。
    """
    handlers = [] if proxy else [urllib.request.ProxyHandler({})]
    if proxy:
        handlers = [urllib.request.ProxyHandler({"http": proxy, "https": proxy})]
    opener = urllib.request.build_opener(*handlers)
    request = urllib.request.Request(url, method="GET")
    try:
        with opener.open(request, timeout=timeout) as response:
            return int(response.status), response.read(200).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read(200).decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return 0, "%s: %s" % (type(exc).__name__, exc)


def probe_channel(profile: ProxyProfile, *, region_block: Sequence[str] = (),
                  tcp: Optional[TcpCheck] = None, get: Optional[HttpGet] = None,
                  timeout_tcp: float = TCP_TIMEOUT, timeout_http: float = HTTP_TIMEOUT,
                  now: Optional[float] = None) -> ChannelProbe:
    """按三步探活一档（TCP → 穿代理取 generate_204 → 地区检查）。

    直连档不探活、直接算通过——它没有代理进程可连，探它只是测本机网络，结论无意义。
    """
    tcp = tcp_check if tcp is None else tcp
    get = urllib_get if get is None else get
    started = time.time()
    blocked = tuple(str(code).strip().upper() for code in region_block if str(code).strip())

    if profile.direct:
        return ChannelProbe(profile.name, None, True, detail="direct (no proxy)",
                            seconds=0.0, ts=time.time() if now is None else now)

    probe_now = time.time() if now is None else now
    try:
        host, port = parse_host_port(profile.url or "")
    except RouteError as exc:
        return ChannelProbe(profile.name, profile.url, False, detail=str(exc), ts=probe_now,
                            seconds=time.time() - started)
    if not tcp(host, port, timeout_tcp):
        return ChannelProbe(profile.name, profile.url, False,
                            detail="tcp connect failed (%s:%d)" % (host, port),
                            ts=probe_now, seconds=time.time() - started)
    try:
        status, body = get(PROBE_URL, profile.url, timeout_http)
    except Exception as exc:  # noqa: BLE001  —— 可注入的 get 也可能是任意实现
        status, body = 0, "%s: %s" % (type(exc).__name__, exc)
    if status not in PROBE_OK_STATUS:
        return ChannelProbe(profile.name, profile.url, False, tcp_ok=True,
                            detail="probe GET %s -> %s %s"
                                   % (PROBE_URL, status, str(body)[:80]),
                            ts=probe_now, seconds=time.time() - started)
    if not blocked:
        return ChannelProbe(profile.name, profile.url, True, tcp_ok=True, http_ok=True,
                            detail="tcp + https through proxy ok", ts=probe_now,
                            seconds=time.time() - started)
    try:
        status, body = get(REGION_URL, profile.url, timeout_http)
    except Exception as exc:  # noqa: BLE001
        status, body = 0, "%s: %s" % (type(exc).__name__, exc)
    country = (body or "").strip().split()[0][:2].upper() if status == 200 and body else None
    if status != 200 or not country:
        return ChannelProbe(profile.name, profile.url, False, tcp_ok=True, http_ok=True,
                            region_ok=False, country=country,
                            detail="region check failed: GET %s -> %s %s"
                                   % (REGION_URL, status, str(body)[:80]),
                            ts=probe_now, seconds=time.time() - started)
    if country in blocked:
        return ChannelProbe(profile.name, profile.url, False, tcp_ok=True, http_ok=True,
                            region_ok=False, country=country,
                            detail="exit region %s is blocked (%s)" % (country, ",".join(blocked)),
                            ts=probe_now, seconds=time.time() - started)
    return ChannelProbe(profile.name, profile.url, True, tcp_ok=True, http_ok=True,
                        region_ok=True, country=country,
                        detail="tcp + https + region %s ok" % country,
                        ts=probe_now, seconds=time.time() - started)


# ---------------------------------------------------------------- 缓存

def load_cache(path: Optional[Path] = None, *,
               environ: Optional[Mapping[str, str]] = None) -> dict:
    """读探活缓存；文件坏了／没有都返回空表（缓存只是加速，不该成为错误来源）。"""
    target = Path(path).expanduser() if path is not None else default_cache_path(environ)
    if not target.is_file():
        return {}
    try:
        doc = json.loads(target.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    channels = doc.get("channels") if isinstance(doc, dict) else None
    return dict(channels) if isinstance(channels, dict) else {}


def save_cache(entries: Mapping[str, Mapping[str, Any]], path: Optional[Path] = None, *,
               environ: Optional[Mapping[str, str]] = None) -> Path:
    """写探活缓存（父目录自动建）。"""
    target = Path(path).expanduser() if path is not None else default_cache_path(environ)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": CACHE_VERSION,
               "channels": {str(k): dict(v) for k, v in entries.items()}}
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
                      encoding="utf-8")
    return target


def cache_entry_fresh(entry: Mapping[str, Any], now: float, *,
                      ttl_ok: float = CACHE_TTL_OK, ttl_fail: float = CACHE_TTL_FAIL) -> bool:
    """缓存条目还算不算数：成功留 ``ttl_ok``，失败只留 ``ttl_fail``。"""
    try:
        ts = float(entry.get("ts") or 0.0)
    except (TypeError, ValueError):
        return False
    ttl = ttl_ok if entry.get("ok") else min(ttl_fail, ttl_ok)
    return (now - ts) < ttl


def probe_all(names: Optional[Sequence[str]] = None, *,
              profiles: Optional[ChannelProfiles] = None,
              cache_path: Optional[Path] = None,
              environ: Optional[Mapping[str, str]] = None,
              tcp: Optional[TcpCheck] = None, get: Optional[HttpGet] = None,
              now: Optional[float] = None, refresh: bool = False,
              ttl_ok: float = CACHE_TTL_OK, ttl_fail: float = CACHE_TTL_FAIL,
              persist: bool = True) -> list[ChannelProbe]:
    """按顺序探活（缓存内的结论不重复探），返回每档一条 :class:`ChannelProbe`。"""
    doc = load_profiles(environ=environ) if profiles is None else profiles
    clock = time.time() if now is None else now
    targets = list(names) if names is not None else doc.order()
    cache = {} if refresh else load_cache(cache_path, environ=environ)
    results: list[ChannelProbe] = []
    probed: dict[str, dict] = {}
    for name in targets:
        profile = doc.profile(name)
        entry = cache.get(name)
        if entry is not None and cache_entry_fresh(entry, clock, ttl_ok=ttl_ok, ttl_fail=ttl_fail):
            results.append(ChannelProbe.from_cache(name, entry))
            continue
        probed[name] = probe_channel(profile, region_block=doc.region_block,
                                     tcp=tcp, get=get, now=clock)
        results.append(probed[name])
    if persist and probed:
        merged = dict(cache)
        merged.update({name: probe.as_cache() for name, probe in probed.items()})
        save_cache(merged, cache_path, environ=environ)
    return results


def mark_unhealthy(name: str, *, url: Optional[str] = None, detail: Optional[str] = None,
                   cache_path: Optional[Path] = None,
                   environ: Optional[Mapping[str, str]] = None,
                   now: Optional[float] = None) -> ChannelProbe:
    """运行中撞上传输失败时把某档标成"刚失败"，于是下一次解析不会再挑它。

    写的是一条**失败**结论，所以它的 TTL 是 ``CACHE_TTL_FAIL``（≤120 s）——通道坏掉是
    暂时的，过一会儿还得让它有机会被重新探活；``refresh=True`` 或删掉缓存即可立刻重探。
    """
    clock = time.time() if now is None else now
    probe = ChannelProbe(name=name, url=url, ok=False,
                         detail=detail or "marked unhealthy after a runtime failure",
                         ts=clock, seconds=0.0)
    cache = load_cache(cache_path, environ=environ)
    cache[name] = probe.as_cache()
    save_cache(cache, cache_path, environ=environ)
    return probe


# ---------------------------------------------------------------- 解析

@dataclass(frozen=True)
class ChannelChoice:
    """一次解析的结果：选中的档、要用的代理 URL、依据的是哪一级规则。"""

    name: str
    url: Optional[str]
    rule: str
    detail: str = ""
    probe: Optional[ChannelProbe] = None

    @property
    def masked_url(self) -> str:
        return mask_proxy_url(self.url)

    @property
    def direct(self) -> bool:
        return self.url is None


def _choice_from_value(value: str, doc: ChannelProfiles, rule: str, detail: str) -> ChannelChoice:
    """把"档名｜URL｜direct"这三种写法统一判读成一条通道（显式参数与环境变量共用）。"""
    text = str(value).strip()
    if not text:
        raise RouteError("empty proxy value for %s" % rule)
    if text == DIRECT:
        return ChannelChoice(DIRECT, None, rule, detail)
    if text in doc.profiles:
        profile = doc.profiles[text]
        return ChannelChoice(profile.name, profile.url, rule, detail)
    if "://" in text:
        return ChannelChoice(text, text, rule, detail)
    if ":" in text:                                  # 裸 host:port
        url = "http://%s" % text
        return ChannelChoice(text, url, rule, detail)
    raise RouteError("proxy value %r is neither a profile name nor a URL (file: %s)"
                     % (text, doc.path or "<defaults>"))


def pref_path_of(preference: Optional[Path] = None,
                 environ: Optional[Mapping[str, str]] = None) -> Path:
    """偏好文件的实际路径（``None`` 时按约定推），供读取与说明共用同一个来源。"""
    return Path(preference).expanduser() if preference is not None else preference_path(environ)


def read_preference(path: Optional[Path] = None, *,
                    environ: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """读用户偏好文件里的档名（纯文本，忽略空行与 ``#`` 注释）；没有返回 ``None``。"""
    target = pref_path_of(path, environ)
    if not target.is_file():
        return None
    for raw in target.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            return line
    return None


def resolve_channel(provider: str = "gemini", *, explicit: Optional[str] = None,
                    profiles: Optional[ChannelProfiles] = None,
                    profiles_path: Optional[Path] = None,
                    preference: Optional[Path] = None,
                    cache_path: Optional[Path] = None,
                    environ: Optional[Mapping[str, str]] = None,
                    tcp: Optional[TcpCheck] = None, get: Optional[HttpGet] = None,
                    now: Optional[float] = None, allow_probe: bool = True,
                    ttl_ok: float = CACHE_TTL_OK, ttl_fail: float = CACHE_TTL_FAIL
                    ) -> ChannelChoice:
    """按优先级定这一次要用哪条通道（模块文档里的六级，逐级短路）。

    ``allow_probe=False`` 跳过第 5 级探活（``channels --list`` 这类只想知道"配了哪些档"
    的场景不该顺手发起网络请求），直接落到 direct。
    """
    env = os.environ if environ is None else environ
    doc = load_profiles(profiles_path, environ=env) if profiles is None else profiles

    if explicit:
        return _choice_from_value(explicit, doc, RULE_EXPLICIT, "explicit argument")

    provider_var = proxy_env_var(provider)
    raw = (env.get(provider_var) or "").strip()
    if raw:
        return _choice_from_value(raw, doc, RULE_ENV_PROVIDER, "env %s" % provider_var)

    raw = (env.get(GLOBAL_PROXY_ENV) or "").strip()
    if raw:
        return _choice_from_value(raw, doc, RULE_ENV_GLOBAL, "env %s" % GLOBAL_PROXY_ENV)

    preferred = read_preference(preference, environ=env)
    if preferred:
        return _choice_from_value(preferred, doc, RULE_PREFERENCE,
                                  "preference file %s" % pref_path_of(preference, env))

    if allow_probe:
        for probe in probe_all(doc.order(), profiles=doc, cache_path=cache_path, environ=env,
                               tcp=tcp, get=get, now=now, ttl_ok=ttl_ok, ttl_fail=ttl_fail):
            if probe.ok:
                return ChannelChoice(probe.name, probe.url, RULE_PROBE,
                                     "%s%s" % ("cached probe" if probe.cached else "probe",
                                               ": %s" % probe.detail if probe.detail else ""),
                                     probe=probe)

    direct_profile = doc.profiles.get(DIRECT, ProxyProfile(DIRECT, None, "直连"))
    return ChannelChoice(DIRECT, direct_profile.url, RULE_FALLBACK,
                         "no probe passed" if allow_probe else "probing disabled")


def list_channels(*, profiles: Optional[ChannelProfiles] = None,
                  cache_path: Optional[Path] = None,
                  environ: Optional[Mapping[str, str]] = None) -> list[dict]:
    """列出档案里的档（不探活、不写缓存）：档名、掩码后的 URL、备注、缓存结论。"""
    doc = load_profiles(environ=environ) if profiles is None else profiles
    cache = load_cache(cache_path, environ=environ)
    order = doc.order()
    rows = []
    for name in sorted(doc.profiles, key=lambda item: order.index(item) if item in order else 99):
        profile = doc.profiles[name]
        entry = cache.get(name)
        rows.append({
            "name": name,
            "url": mask_proxy_url(profile.url),
            "note": profile.note,
            "direct": profile.direct,
            "order_index": order.index(name) if name in order else None,
            "default_order": name in doc.default_order,
            "cached": None if entry is None else {
                "ok": bool(entry.get("ok")),
                "ts": float(entry.get("ts") or 0.0),
                "detail": str(entry.get("detail") or ""),
            },
        })
    return rows


def explain(provider: str = "gemini", *, profiles: Optional[ChannelProfiles] = None,
            probes: Optional[Sequence[ChannelProbe]] = None, **kwargs) -> str:
    """解析一次并给出中文说明：选了哪条、依据哪一级规则、探活结论如何。

    ``probes`` 给定时用它替代内部的探活结果（调用方已经探过一遍时不必再来一次）。
    只有"依据是探活"（第 5 级）才顺手把各档结论列出来——显式参数／环境变量已经定了
    通道，再去挨个探活只是白白等网络。
    """
    env = kwargs.get("environ")
    doc = load_profiles(kwargs.get("profiles_path"), environ=env) if profiles is None else profiles
    choice = resolve_channel(provider, profiles=doc, **kwargs)
    lines = ["通道：%s" % choice.name,
             "代理：%s" % choice.masked_url,
             "依据：%s（%s）" % (RULE_LABELS_CN.get(choice.rule, choice.rule),
                                choice.detail or "-")]
    profile = doc.profiles.get(choice.name)
    if profile is not None and profile.note:
        lines.append("备注：%s" % profile.note)
    if doc.region_block:
        lines.append("阻断地区码：%s" % "、".join(doc.region_block))

    report = list(probes) if probes is not None else None
    if report is None and kwargs.get("allow_probe", True) and choice.rule == RULE_PROBE:
        report = probe_all(doc.order(), profiles=doc, cache_path=kwargs.get("cache_path"),
                           environ=env, tcp=kwargs.get("tcp"), get=kwargs.get("get"),
                           now=kwargs.get("now"))
    if report:
        lines.append("探活：")
        for item in report:
            lines.append("  %-10s %-4s %-30s %s" % (
                item.name, "通过" if item.ok else "未通过", mask_proxy_url(item.url),
                "%s（%.1f s%s）" % (item.detail or "-", item.seconds,
                                    "，取自缓存" if item.cached else "")))
    elif choice.probe is not None:
        lines.append("探活：%s（%.1f s，%s）"
                     % ("通过" if choice.probe.ok else "未通过", choice.probe.seconds,
                        choice.probe.detail or "-"))
    return "\n".join(lines)
