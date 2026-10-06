# -*- coding: utf-8 -*-
"""自动批量下载原文：解析链＋下载核心（只碰合法 OA 来源，不碰付费墙）。

解析优先级（:func:`resolve_download_plan`）：

1. ``eprint``（arXiv 编号）→ ``arxiv.org/pdf/<id>``；
2. 记录既有 ``download_url``（arXiv／直链 → 可下载；doi.org 落地页 → 需人工）；
3. 现场重解析（注入的引擎链，取置信匹配候选的 ``download_url``；无 → 「无 OA」）。

下载（:func:`download_pdf`）吸取人工下载截断事故的教训，全部**内容级校验**：
``%PDF`` 头＋尾部 ``%%EOF``（＋可选 ``pdfinfo``）通过才算成功；HTML／登录页／
挑战页判 ``html``，绝不落盘。先写 ``.part`` 临时文件、校验通过后原子改名。

本模块只做纯计算与单文件下载（``opener`` 可注入，便于测试）；批量编排、
落库与挂链由服务端负责。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from .enrich import match_candidate

__all__ = [
    "USER_AGENT",
    "HostThrottle",
    "classify_url",
    "download_pdf",
    "resolve_download_plan",
    "safe_filename",
    "unique_path",
    "validate_pdf",
]

USER_AGENT = "primer-literature-web/0.1 (research)"
DEFAULT_TIMEOUT = 60.0
DEFAULT_MAX_BYTES = 200 * 1024 * 1024

_INVALID_CHARS = re.compile(r'[\\/:*?"<>|\r\n\t]+')
_ARXIV_PREFIX = re.compile(r"(?i)^arxiv[:\s]*")


def _clean_eprint(value: Any) -> str:
    """arXiv 编号清洗：去 ``arXiv:`` 前缀与空白（保留 ``vN`` 版本后缀）。"""
    return _ARXIV_PREFIX.sub("", str(value or "").strip())


def arxiv_pdf_url(eprint: Any) -> str:
    """由 arXiv 编号构造 PDF 直链。"""
    return f"https://arxiv.org/pdf/{_clean_eprint(eprint)}"


def classify_url(url: Any) -> tuple[str, str]:
    """把候选 URL 归成 ``(expected, normalized)``；``expected`` ∈ ``direct``／``manual``／``none``。

    ``direct``：可尝试自动下载（arxiv 链接会归一到 ``/pdf/``）；``manual``：落地页
    （doi.org 一类），只供人工打开；``none``：无 URL。
    """
    text = str(url or "").strip()
    if not text:
        return "none", ""
    lowered = text.lower()
    if "arxiv.org/abs/" in lowered:
        return "direct", text.replace("/abs/", "/pdf/")
    if "arxiv.org/pdf/" in lowered:
        return "direct", text
    if lowered.startswith(("https://doi.org/", "http://doi.org/", "https://dx.doi.org/")):
        return "manual", text
    plain = lowered.split("?", 1)[0].rstrip("/")
    if plain.endswith(".pdf"):
        return "direct", text
    if "/pdf" in plain or "download" in plain or "/document" in plain:
        return "direct", text
    return "manual", text


def resolve_download_plan(record: Any, *, engines: Sequence[Callable] = ()) -> dict[str, Any]:
    """为一条记录解析下载计划：返回 ``{url, source, expected, reason}``。

    ``engines`` 为现场重解析用的引擎链（可注入假引擎；异常按"该引擎无结果"跳过）。
    """
    title = str(getattr(record, "title", "") or "").strip()
    eprint = _clean_eprint(getattr(record, "eprint", ""))
    if eprint:
        return {
            "url": arxiv_pdf_url(eprint),
            "source": "arxiv-eprint",
            "expected": "direct",
            "reason": "",
        }
    expected, url = classify_url(getattr(record, "download_url", ""))
    if expected == "direct":
        source = "arxiv" if "arxiv.org" in url.lower() else "download-url"
        return {"url": url, "source": source, "expected": "direct", "reason": ""}
    if expected == "manual":
        return {"url": url, "source": "doi-landing", "expected": "manual", "reason": "落地页（需人工）"}
    for engine in engines:
        if not title:
            break
        try:
            candidates = engine(title) or []
        except Exception:
            continue
        best = match_candidate(record, candidates)
        if best is None:
            continue
        expected2, url2 = classify_url(best.get("download_url"))
        if expected2 == "direct":
            return {"url": url2, "source": "live-lookup", "expected": "direct", "reason": ""}
        if expected2 == "manual":
            return {"url": url2, "source": "live-lookup", "expected": "manual", "reason": "落地页（需人工）"}
    return {"url": "", "source": "none", "expected": "none", "reason": "无 OA 链接"}


def safe_filename(title: Any, year: Any = None, *, stem_limit: int = 80) -> str:
    """把标题变成安全的 PDF 文件名：``<标题>_<年份>.pdf``（净化非法字符、限长）。"""
    name = _INVALID_CHARS.sub("_", str(title or "").strip())
    name = re.sub(r"\s+", " ", name).strip(" ._")
    if year:
        name = f"{name}_{year}"
    name = name[:stem_limit].strip(" ._")
    return (name or "download") + ".pdf"


def unique_path(directory: Path, filename: str) -> Path:
    """在目录里挑一个不冲突的文件名：撞名自动 ``-2``、``-3``……"""
    directory = Path(directory)
    path = directory / filename
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    counter = 2
    while True:
        candidate = directory / f"{stem}-{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def validate_pdf(path: Path, *, check_pdfinfo: bool = True) -> tuple[bool, str]:
    """内容级校验：``%PDF`` 头＋尾部 ``%%EOF``（＋可选 pdfinfo）。返回 ``(ok, 原因)``。"""
    path = Path(path)
    try:
        with open(path, "rb") as handle:
            if handle.read(4) != b"%PDF":
                return False, "not a PDF"
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - 2048))
            tail = handle.read()
    except OSError as exc:
        return False, f"unreadable: {exc}"
    if b"%%EOF" not in tail:
        return False, "truncated (no %%EOF)"
    if check_pdfinfo and shutil.which("pdfinfo"):
        try:
            proc = subprocess.run(
                ["pdfinfo", str(path)], capture_output=True, timeout=30
            )
            if proc.returncode != 0:
                return False, "pdfinfo rejected the file"
        except Exception:  # pdfinfo 缺失/超时：不阻断（头尾校验已过）
            pass
    return True, ""


def download_pdf(
    url: str,
    dest: Path,
    *,
    opener: Optional[Callable] = None,
    timeout: float = DEFAULT_TIMEOUT,
    max_bytes: int = DEFAULT_MAX_BYTES,
    check_pdfinfo: bool = True,
) -> dict[str, Any]:
    """下载单个直链 PDF 到 ``dest``；返回 ``{ok, status, error, size, url}``。

    ``status``：``downloaded``（成功）／``html``（疑似登录/付费墙/反爬）／
    ``blocked``（401/402/403）／``not-found``（404/410）／``invalid``（内容校验
    不过）／``too-large``／``failed``（网络错误等）。任何失败都不留临时文件。
    """
    dest = Path(dest)
    opener = opener or urllib.request.urlopen
    part = dest.with_name(dest.name + ".part")
    result: dict[str, Any] = {"ok": False, "status": "failed", "error": "", "size": 0, "url": url}
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/pdf,*/*"}
    )
    try:
        with opener(request, timeout=timeout) as response:
            ctype = str(getattr(response, "headers", {}).get("Content-Type") or "").lower()
            if "text/html" in ctype or "application/xhtml" in ctype:
                result.update(status="html", error="HTML 响应（疑似登录／付费墙／反爬）")
                return result
            total = 0
            with open(part, "wb") as handle:
                while True:
                    chunk = response.read(1024 * 256)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        result.update(status="too-large", error="exceeds size limit")
                        return _cleanup(part, result)
                    handle.write(chunk)
            result["size"] = total
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 402, 403):
            result.update(status="blocked", error=f"HTTP {exc.code}（疑似付费墙／需登录）")
        elif exc.code in (404, 410):
            result.update(status="not-found", error=f"HTTP {exc.code}")
        else:
            result.update(status="failed", error=f"HTTP {exc.code}")
        return _cleanup(part, result)
    except Exception as exc:
        result.update(status="failed", error=str(exc)[:200] or type(exc).__name__)
        return _cleanup(part, result)
    ok, why = validate_pdf(part, check_pdfinfo=check_pdfinfo)
    if not ok:
        result.update(status="invalid", error=why)
        return _cleanup(part, result)
    try:
        os.replace(part, dest)
    except OSError as exc:
        result.update(status="failed", error=f"cannot save: {exc}")
        return _cleanup(part, result)
    result.update(ok=True, status="downloaded", error="")
    return result


def _cleanup(part: Path, result: dict[str, Any]) -> dict[str, Any]:
    try:
        part.unlink(missing_ok=True)
    except OSError:
        pass
    return result


class HostThrottle:
    """按主机限速：同一 host 两次请求之间至少间隔 ``interval`` 秒。"""

    def __init__(self, interval: float = 1.0):
        self.interval = float(interval)
        self._last: dict[str, float] = {}

    def wait(self, url: str) -> None:
        if self.interval <= 0:
            return
        host = urllib.parse.urlsplit(str(url)).netloc or "-"
        now = time.monotonic()
        delay = self.interval - (now - self._last.get(host, 0.0))
        if delay > 0:
            time.sleep(delay)
        self._last[host] = time.monotonic()
