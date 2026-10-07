# -*- coding: utf-8 -*-
"""价目表与估算器（USD，Standard 档）。

数字照抄本工作流的价目事实（来源 ``https://aistudio.google.com/docs/pricing``，
经代理取回 2026-10-07；同源 ``https://ai.google.dev/gemini-api/docs/pricing``），
不在这里重新推导——要改价就改 :data:`MODEL_PRICES` 一处。

``gemini-3-pro-image``（Nano Banana Pro，本工作流实际使用）
----------------------------------------------------------

============================  ==============  ================================
项目                          单价            备注
============================  ==============  ================================
输出图像 1K 或 2K（≤2048²）   $0.134 / 张     1120 tokens @ $120/1M
输出图像 4K（≤4096²）         $0.240 / 张     2000 tokens
输入图像（``--ref`` 参考图）  $0.0011 / 张    560 tokens——≈ 免费
Batch 档（异步）              半价           1K/2K $0.067；4K $0.12
============================  ==============  ================================

**关键口径：1K 与 2K 同价。** 迭代档因此直接用 2K（同价、文字质量更好、插 PPT 即够用），
4K 只留给定稿——"先 1K 再 2K"没有意义。价目表把两档合成一个 ``1k2k`` 键，就是为了让
估算器算不出那个不存在的差价。

备选省钱档 ``gemini-3.1-flash-image``：0.5K $0.045｜1K $0.067｜2K $0.101｜4K $0.151
（Batch 减半）；文字保真需另测后再决定是否降档。它的 1K 与 2K **不同价**，而
:func:`estimate` 只收"1K/2K 合计张数"一个数——遇到这种模型，按 **2K** 单价计（偏高），
并在结果里标注；宁可估高，不要因为模型换了就悄悄少算。

``gemini-2.5-flash-image`` $0.039（已弃）：只出 1K，价目表里没有 2K／4K 键，给别的档位
会报错而不是拿一个不存在的价格去乘。价目表未列参考图输入单价的模型，``ref_image`` 为
``None``——估算时按 0 计并标注"价目未列"，不改写成 0 当事实。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional

__all__ = [
    "BATCH_FACTOR",
    "CNY_RATE_DEFAULT",
    "MODEL_PRICES",
    "PRICE_SOURCE",
    "ModelPrice",
    "PricingError",
    "estimate",
    "format_estimate",
    "model_price",
    "normalize_size",
    "price_table",
    "unit_price",
]

# 价目事实的出处（改价时一并改这里，报告里会照原样带出去）。
PRICE_SOURCE = ("https://aistudio.google.com/docs/pricing （2026-10-07 经代理取回；"
                "同源 https://ai.google.dev/gemini-api/docs/pricing）")
# Batch 档一律半价（异步，未在本工作流的即时迭代里使用）。
BATCH_FACTOR = 0.5
# USD → CNY 折算率，仅作换算示意。
CNY_RATE_DEFAULT = 7.2


class PricingError(Exception):
    """估算失败：模型不认识、档位不支持、张数为负。消息英文，CLI 折算成退出码 2。"""


@dataclass(frozen=True)
class ModelPrice:
    """一个模型的价目：各档位单价、参考图输入单价（未列为 ``None``）、是否已弃用。"""

    model: str
    label: str
    sizes: Mapping[str, float]
    ref_image: Optional[float] = None
    deprecated: bool = False
    note: str = ""

    def has_size(self, size: str) -> bool:
        return normalize_size(size) in self.sizes

    def resolve(self, size: str) -> tuple[float, Optional[str]]:
        """取某档位单价，返回 ``(单价, 说明)``。说明只在"口径是折算出来的"时非空。"""
        key = normalize_size(size)
        if key in self.sizes:
            return float(self.sizes[key]), None
        if key == "1k2k":
            one, two = self.sizes.get("1k"), self.sizes.get("2k")
            if one is not None and two is not None:
                if abs(one - two) < 1e-9:
                    return float(one), None
                return float(two), ("%s 的 1K 与 2K 不同价（$%.4f / $%.4f），"
                                    "此处按 2K 计（偏高）" % (self.model, one, two))
            if two is not None:
                return float(two), "按 2K 单价计"
            if one is not None:
                return float(one), "按 1K 单价计"
        raise PricingError("%s has no price for size %r (known: %s)"
                           % (self.model, size, ", ".join(sorted(self.sizes)) or "-"))


MODEL_PRICES: Mapping[str, ModelPrice] = {
    "gemini-3-pro-image": ModelPrice(
        model="gemini-3-pro-image",
        label="Nano Banana Pro（本工作流实际使用）",
        sizes={"1k": 0.134, "2k": 0.134, "1k2k": 0.134, "4k": 0.240},
        ref_image=0.0011,
        note="1K 与 2K 同价（$0.134）；4K $0.240；Batch 半价",
    ),
    "gemini-3.1-flash-image": ModelPrice(
        model="gemini-3.1-flash-image",
        label="Nano Banana 2（备选省钱档，未采用）",
        sizes={"0.5k": 0.045, "1k": 0.067, "2k": 0.101, "4k": 0.151},
        ref_image=None,
        note="文字保真需另测；1K 与 2K 不同价，Batch 减半",
    ),
    "gemini-2.5-flash-image": ModelPrice(
        model="gemini-2.5-flash-image",
        label="已弃",
        sizes={"1k": 0.039},
        ref_image=None,
        deprecated=True,
        note="已弃用；只列 1K 一档",
    ),
}

DEFAULT_MODEL = "gemini-3-pro-image"


def price_table() -> list[ModelPrice]:
    """价目表（按模型名排序），供 ``price --table`` 之类的人读输出用。"""
    return [MODEL_PRICES[name] for name in sorted(MODEL_PRICES)]


def model_price(model: str) -> ModelPrice:
    """按模型名取价目；不认识就报错（不猜、不套用别的模型的价）。"""
    try:
        return MODEL_PRICES[model]
    except KeyError as exc:
        raise PricingError("unknown model: %r (known: %s)"
                           % (model, ", ".join(sorted(MODEL_PRICES)))) from exc


def normalize_size(size: str) -> str:
    """档位写法归一：``"1K"``／``"1k"`` → ``"1k"``；``"1K/2K"``、``"1k2k"`` → ``"1k2k"``。"""
    text = re.sub(r"[\s/／、,]+", "", str(size)).strip().lower()
    if text in ("1k2k", "1k＆2k"):
        return "1k2k"
    return text


def unit_price(model: str, size: str, *, batch: bool = False) -> tuple[float, Optional[str]]:
    """某模型某档位的单价（USD），返回 ``(单价, 说明)``；Batch 档折半。"""
    price, note = model_price(model).resolve(size)
    if batch:
        price *= BATCH_FACTOR
        note = "Batch 半价" + ("；%s" % note if note else "")
    return round(price, 6), note


def _count(value: Any, field_name: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise PricingError("%s must be an integer, got %r" % (field_name, value)) from exc
    if number < 0:
        raise PricingError("%s must be >= 0, got %d" % (field_name, number))
    return number


def estimate(n_1k2k, n_4k, n_ref=0, model: str = DEFAULT_MODEL, *,
             batch: bool = False, cny_rate: Optional[float] = CNY_RATE_DEFAULT) -> dict:
    """一次出图预算（USD）：``n_1k2k`` 张 1K/2K ＋ ``n_4k`` 张 4K ＋ ``n_ref`` 张参考图输入。

    返回**含明细**的 dict（键英文，值可中文）：逐项张数、单价、小计，合计与可选 CNY 折算。
    Batch 档一律半价（含参考图输入）。模型没有的档位直接报错——拿一个不存在的价格去乘
    比报错更糟。
    """
    spec = model_price(model)
    counts = {"n_1k2k": _count(n_1k2k, "n_1k2k"), "n_4k": _count(n_4k, "n_4k"),
              "n_ref": _count(n_ref, "n_ref")}
    if counts["n_1k2k"] == 0 and counts["n_4k"] == 0:
        raise PricingError("nothing to estimate: n_1k2k and n_4k are both 0")

    items: list[dict] = []
    notes: list[str] = []
    for key, size, label in (("n_1k2k", "1k2k", "输出图像 1K/2K（≤2048²）"),
                             ("n_4k", "4k", "输出图像 4K（≤4096²）")):
        qty = counts[key]
        if not qty:
            continue
        unit, note = unit_price(model, size, batch=batch)
        items.append({"kind": "output", "size": size, "label": label, "qty": qty,
                      "unit_usd": unit, "usd": round(unit * qty, 6), "note": note})
        if note:
            notes.append("%s：%s" % (label, note))
    if counts["n_ref"]:
        if spec.ref_image is None:
            items.append({"kind": "ref_input", "size": None, "label": "参考图输入（--ref）",
                          "qty": counts["n_ref"], "unit_usd": 0.0, "usd": 0.0,
                          "note": "价目表未列该模型的参考图输入单价，按 0 计"})
            notes.append("参考图输入：%s 的价目表未列单价，按 0 计——「无法估算」不等于"
                         "「免费」，只是价目表上查不到这个数" % model)
        else:
            ref_unit = spec.ref_image * (BATCH_FACTOR if batch else 1.0)
            ref_note = "Batch 半价" if batch else None
            items.append({"kind": "ref_input", "size": None, "label": "参考图输入（--ref）",
                          "qty": counts["n_ref"], "unit_usd": round(ref_unit, 6),
                          "usd": round(ref_unit * counts["n_ref"], 6), "note": ref_note})
    if batch:
        notes.append("Batch 档（异步）一律半价；本工作流的即时迭代不走 Batch")

    total = round(sum(item["usd"] for item in items), 6)
    result = {
        "model": model,
        "label": spec.label,
        "deprecated": bool(spec.deprecated),
        "tier": "batch" if batch else "standard",
        "batch": bool(batch),
        "counts": counts,
        "items": items,
        "total_usd": total,
        "cny_rate": None if cny_rate is None else float(cny_rate),
        "total_cny": None if cny_rate is None else round(total * float(cny_rate), 2),
        "notes": notes,
        "source": PRICE_SOURCE,
    }
    if spec.note:
        result["model_note"] = spec.note
    return result


def format_estimate(result: Mapping[str, Any]) -> str:
    """把 :func:`estimate` 的结果渲染成中文人读报告（尺寸一致，直接 ``print``）。"""
    lines = ["出图预算：%s（%s）" % (result["model"], result["tier"]),
             "  %-24s %-8s %-12s %s" % ("项目", "张数", "单价 USD", "小计 USD")]
    for item in result["items"]:
        lines.append("  %-24s %-8d %-12.4f %.4f%s"
                     % (item["label"], item["qty"], item["unit_usd"], item["usd"],
                        "（%s）" % item["note"] if item["note"] else ""))
    lines.append("  合计：$%.4f%s" % (result["total_usd"],
                                     "" if result["total_cny"] is None
                                     else "（≈ ¥%.2f，按 1 USD ≈ %.2f CNY）"
                                          % (result["total_cny"], result["cny_rate"])))
    if result.get("deprecated"):
        lines.append("  · 该模型已弃用，价目表只作留档。")
    for note in result["notes"]:
        lines.append("  · %s" % note)
    lines.append("  · 价目来源：%s" % result["source"])
    return "\n".join(lines)
