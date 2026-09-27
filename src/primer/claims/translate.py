# -*- coding: utf-8 -*-
"""把论断句子译成英文：中文论断 × 英文原文之间那道墙，先在这里拆掉。

本工程的原文**全是英文**（128 份转换产物的中日韩字符比中位数 0.0005），论断**全是
中文**（0.62–0.66）。词面检索跨不过去：289 条能取到原文的对里，151 条与原文**零
token 重合**，检索只在论断恰好带着拉丁专名（DART、Dimorphos、LIFE…）时才命中。
先把论断译成英文再检索，是把这个检索器从"基本不工作"变成"能用"的最省一步——
比把整篇原文塞给模型便宜得多。

三条约定：

* **按论断句子翻译，不按对翻译**。同一句话挂多个引用时只译一次（键是句子文本的
  哈希），1215 对里其实只有 243 个不同的句子。
* **缓存落在磁盘上**，键是论断文本的 sha256 前 16 位；重跑不花一分钱。产物里同时
  记原句与译文——译文是**取证的辅助**，不是改写，人要能对照着查。
* **翻译失败不是致命错误**：记下失败、保留原文继续走，检索退回到"只用原句"的老
  行为，而不是让整条链停下。

批次很小是有意的：``deepseek-flash`` 是始终思考的模型，推理链与正文共享输出预算，
一批塞太多会把预算耗在推理上、正文回空。故按**字符预算**与**条数上限**双重切批，
一批失败就折半重试，单条仍失败才记失败。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from .client import ChatClient, LlmCallError, LlmReplyError, parse_json_reply

TRANSLATIONS_JSON = "translations.json"
CACHE_VERSION = 1

MAX_BATCH_CHARS = 1200
"""一批输入的总字符上限：短句一批几条，长句自己一批。"""

MAX_BATCH_ITEMS = 4
"""一批最多几条：始终思考的模型在小项目上更稳。"""

MAX_RETRIES = 2
"""单条（或无法再切的一批）的重试次数上限。"""

STATUS_OK = "ok"
STATUS_FAILED = "failed"

PROMPT_HEADER = """\
You translate Chinese sentences from a planetary-science review manuscript into English.

Translate each Chinese sentence into natural, literal English. Rules:
- Render Chinese names of missions, spacecraft, instruments, organisations and places in their
  standard English forms (罗塞塔号 -> Rosetta, 韦布望远镜 -> the James Webb Space Telescope,
  毅力号 -> Perseverance, 嫦娥五号 -> Chang'e 5, 国际空间站 -> the International Space Station).
- Keep Latin abbreviations and Latin proper nouns exactly as written (DART, JWST, NASA, LIFE).
- Keep numbers, units, dates and citation-free wording as written.
- Do not summarise, do not explain, do not merge or split sentences, do not add anything that is
  not in the Chinese. Never leave Chinese characters in the output.

Reply with JSON only, with one entry per input id and no prose around it:

{"translations": [{"id": 1, "en": "<English sentence>"}, ...]}

Inputs:
"""


@dataclass(frozen=True)
class Translation:
    """一句话的译文（或失败记录）。``source`` 是原句，永远是取证时该看的那一份。"""

    source: str
    translation: Optional[str]
    status: str
    error: Optional[str] = None
    cached: bool = False

    @property
    def usable(self) -> bool:
        return self.status == STATUS_OK and bool(self.translation)


def claim_key(text: str) -> str:
    """论断文本的缓存键：sha256 前 16 位，稳定且不泄露正文。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def plan_batches(
    items: Sequence[tuple[str, str]],
    *,
    max_chars: int = MAX_BATCH_CHARS,
    max_items: int = MAX_BATCH_ITEMS,
) -> list[list[tuple[str, str]]]:
    """把 ``(键, 文本)`` 按字符预算与条数上限切批；单条超预算时自己占一批。"""
    batches: list[list[tuple[str, str]]] = []
    current: list[tuple[str, str]] = []
    size = 0
    for key, text in items:
        if current and (len(current) >= max_items or size + len(text) > max_chars):
            batches.append(current)
            current, size = [], 0
        current.append((key, text))
        size += len(text)
    if current:
        batches.append(current)
    return batches


def build_translation_prompt(items: Sequence[tuple[str, str]]) -> str:
    """一次翻译请求的提示词；编号从 1 起，与回信里的 ``id`` 对应。"""
    lines = [PROMPT_HEADER]
    for index, (_, text) in enumerate(items, 1):
        lines.append(f"{index}. {text.replace(chr(10), ' ')}")
    return "\n".join(lines) + "\n"


def parse_translation_reply(payload: object, items: Sequence[tuple[str, str]]) -> dict[str, str]:
    """把回信归一成 ``{键: 译文}``；认不出的条目算缺失，由调用方重试。

    回信的形状以 ``{"translations": [...]}`` 为准，但列表、``result`` / ``data`` 包装、
    ``id`` 用编号或键、译文放在 ``en`` / ``translation`` / ``text`` 都能认——修复优先于
    报错，缺失条目由调用方按单条重试。
    """
    entries: list = []
    if isinstance(payload, list):
        entries = payload
    elif isinstance(payload, dict):
        for key in ("translations", "items", "result", "data", "output"):
            value = payload.get(key)
            if isinstance(value, list):
                entries = value
                break
        else:
            entries = [payload]
    by_position = {str(index): key for index, (key, _) in enumerate(items, 1)}
    by_key = {key: key for key, _ in items}
    found: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        label = entry.get("id", entry.get("key", entry.get("index")))
        text = None
        for name in ("en", "translation", "english", "text", "output"):
            value = entry.get(name)
            if isinstance(value, str) and value.strip():
                text = value.strip()
                break
        if text is None:
            continue
        label_text = str(label).strip() if label is not None else ""
        key = by_position.get(label_text) or by_key.get(label_text)
        if key is None:
            continue
        found[key] = text
    return found


class TranslationCache:
    """磁盘上的译文缓存：``{键: {source, translation, model, version}}``。"""

    def __init__(self, path: Path, *, model: str = ""):
        self.path = Path(path)
        self.model = model
        self.entries: dict[str, dict] = self._load()
        self.hits = 0

    def _load(self) -> dict[str, dict]:
        if not self.path.is_file():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, ValueError, OSError):
            # 缓存坏了只当没有缓存：重译一遍的代价远小于把坏缓存当证据用。
            return {}
        entries = payload.get("translations") if isinstance(payload, dict) else None
        if not isinstance(entries, dict):
            return {}
        return {
            key: value
            for key, value in entries.items()
            if isinstance(value, dict) and isinstance(value.get("translation"), str)
        }

    def get(self, text: str) -> Optional[str]:
        entry = self.entries.get(claim_key(text))
        if entry is None:
            return None
        self.hits += 1
        return entry["translation"]

    def put(self, text: str, translation: str) -> None:
        self.entries[claim_key(text)] = {
            "source": text,
            "translation": translation,
            "model": self.model,
            "version": CACHE_VERSION,
        }

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": CACHE_VERSION,
            "model": self.model,
            "translations": {key: self.entries[key] for key in sorted(self.entries)},
        }
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
        )


@dataclass
class TranslationStats:
    """一次翻译阶段的记账：花了几次请求、命中多少缓存、失败几条。"""

    sentences: int = 0
    cache_hits: int = 0
    requested: int = 0
    batches: int = 0
    failed: int = 0

    def as_dict(self) -> dict:
        return {
            "sentences": self.sentences,
            "cache_hits": self.cache_hits,
            "translated": self.sentences - self.failed,
            "failed": self.failed,
            "requests": self.requested,
            "batches": self.batches,
        }


class Translator:
    """批量翻译器：缓存优先，失败折半重试，单条失败只记失败。"""

    def __init__(
        self,
        client: ChatClient,
        cache: TranslationCache,
        *,
        max_chars: int = MAX_BATCH_CHARS,
        max_items: int = MAX_BATCH_ITEMS,
        max_retries: int = MAX_RETRIES,
    ):
        self.client = client
        self.cache = cache
        self.max_chars = max_chars
        self.max_items = max_items
        self.max_retries = max_retries
        self.stats = TranslationStats()
        self.requests_before = client.usage.requests

    def translate_all(self, texts: Sequence[str]) -> dict[str, Translation]:
        """把一批句子译成英文，返回 ``{原句: Translation}``（含缓存命中与失败）。"""
        unique: list[str] = []
        for text in texts:
            if text not in unique:
                unique.append(text)
        results: dict[str, Translation] = {}
        pending: list[tuple[str, str]] = []
        for text in unique:
            cached = self.cache.get(text)
            if cached:
                results[text] = Translation(source=text, translation=cached, status=STATUS_OK, cached=True)
            else:
                pending.append((claim_key(text), text))
        for batch in plan_batches(pending, max_chars=self.max_chars, max_items=self.max_items):
            self._translate_batch(batch, results)
        self.cache.save()
        self.stats.sentences = len(unique)
        self.stats.cache_hits = sum(1 for item in results.values() if item.cached)
        self.stats.failed = sum(1 for item in results.values() if item.status == STATUS_FAILED)
        self.stats.requested = self.client.usage.requests - self.requests_before
        return results

    # ---- 内部 ----

    def _translate_batch(self, batch: list[tuple[str, str]], out: dict[str, Translation]) -> None:
        """一条批次的（重试）流程：整批失败就折半，单条失败才认输。"""
        if len(batch) == 1:
            key, text = batch[0]
            payload, error = self._call_with_retries(batch)
            if payload is not None:
                extracted = parse_translation_reply(payload, batch)
                if key in extracted:
                    out[text] = self._record(text, extracted[key], None)
                    return
                error = "reply carried no translation for this item"
            out[text] = self._record(text, None, error)
            return
        payload, error = self._call_once(batch)
        if error is not None:
            middle = len(batch) // 2
            self._translate_batch(batch[:middle], out)
            self._translate_batch(batch[middle:], out)
            return
        got = parse_translation_reply(payload, batch)
        missing: list[tuple[str, str]] = []
        for key, text in batch:
            if key in got:
                out[text] = self._record(text, got[key], None)
            else:
                missing.append((key, text))
        if missing:
            if len(missing) == len(batch):
                # 整批的条目一个都没认出来：这多半不是"模型没译"，而是回信形状不对，
                # 折半后单条再试——单条请求最简单，形状问题通常就没了。
                middle = max(len(missing) // 2, 1)
                self._translate_batch(missing[:middle], out)
                self._translate_batch(missing[middle:], out)
            else:
                # 回信里少了几条：把缺的当更小的一批重试，已拿到的不要再问一遍。
                self._translate_batch(missing, out)

    def _call_with_retries(self, batch: list[tuple[str, str]]) -> tuple[Optional[object], Optional[str]]:
        error: Optional[str] = None
        for _ in range(self.max_retries + 1):
            payload, error = self._call_once(batch)
            if error is None:
                return payload, None
        return None, error

    def _call_once(self, batch: list[tuple[str, str]]) -> tuple[Optional[object], Optional[str]]:
        self.stats.batches += 1
        prompt = build_translation_prompt(batch)
        try:
            reply = self.client.complete(prompt)
        except LlmCallError as error:
            return None, str(error)
        try:
            return parse_json_reply(reply.content), None
        except LlmReplyError as error:
            return None, str(error)

    def _record(self, text: str, translation: Optional[str], error: Optional[str]) -> Translation:
        """把一句话的结果落成 :class:`Translation`；成功时顺手写进缓存。"""
        if translation:
            self.cache.put(text, translation)
            return Translation(source=text, translation=translation, status=STATUS_OK)
        return Translation(source=text, translation=None, status=STATUS_FAILED, error=error)


__all__ = [
    "CACHE_VERSION",
    "MAX_BATCH_CHARS",
    "MAX_BATCH_ITEMS",
    "MAX_RETRIES",
    "STATUS_FAILED",
    "STATUS_OK",
    "TRANSLATIONS_JSON",
    "Translation",
    "TranslationCache",
    "TranslationStats",
    "Translator",
    "build_translation_prompt",
    "claim_key",
    "parse_translation_reply",
    "plan_batches",
]
