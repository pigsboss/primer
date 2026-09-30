# -*- coding: utf-8 -*-
"""``primer-slides inspect``：对一份已生成的 ``slides.pdf`` 做**版面质检**。

模板与判据来自用户在生成 deck 上的人工视觉质检反馈：封面横线穿字、段落误居中、内容挤在
上半区、正文误用衬线、行距过挤、要点残留原文编号。检查分两层，缺一不可：

**确定性层**（无网络、无模型，只用 poppler 命令行 + 标准库）。字体族**只在这一层判**：
视觉模型分不清宋体/黑体（实测同一次运行里主旨页误判、内容页判对），所以 ``serif-body`` 与
``font-substitution`` 只认 ``pdftohtml -xml`` 的 family 名与 ``pdffonts`` 的全表。

* ``line-through-text``：栅格化（``pdftoppm -gray``）扫长横线，与文本盒求交。
* ``centered-body``：正文块按行求左边缘/中心离散度；只在期望左对齐的角色上报。
* ``vertical-imbalance``：正文块上下净空之比超阈值且内容不足一页 —— **角色感知**。
* ``serif-body``：正文区（标题带以下、页脚以上、左文字栏内）出现宋体族。
* ``cramped-leading``：正文基线间距/字号 < 1.3。
* ``numerator-residue``：outline 的 picks 里可剥的原文编号（``（一）``／``一、``…）。
* ``font-substitution``／``glyph-fallback``：``pdffonts`` 全表里出现允许集合外的族。

**视觉层**（``roles.vision``，逐页一次请求）：``pdftoppm -r 140`` 渲染整页，把角色与确定性
层量到的数值写进提示词做上下文；线程池并发（``--workers``，默认 4）；空正文重试一次、
预算翻倍。视觉层的发现一律记 **warning**：判错字体那类事已由确定性层担下，这一层只补
确定性层看不见的（裁切、贴边、重叠、乱码、失衡）。

命令形态::

    primer-slides inspect <outline.yaml> [--pdf FILE] [--project-root R] [--pages A-B]
                          [--no-vision] [--workers N] [--json] [--report FILE] [--dry-run]

产物：outline 同目录 ``inspect.jsonl``（每页一条，**只记 base_url 的 host，绝不写密钥**）、
stdout 中文报告（先总结、再逐页、再按"该改哪里"汇总）、``--report FILE`` 另写 markdown。
退出码与 ``check`` 的约定一致：有 error 级发现 → 2，只有 warning → 0。

本模块只新增、不改既有文件：注册子命令由 ``__main__.py`` 的维护者做。对外接口是
:func:`run`（程序化入口，可注入传输层与 poppler 封装）与 :func:`main`（命令行入口）。
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Mapping, Optional, Sequence, Tuple

from ..config import ConfigError, Endpoint, api_key, load_config, resolve_role
from . import candidates as cand
from . import client as client_mod
from .plan import SlidesError, read_page_entries
from .prose import strip_lead_enumerator
from .select import base_url_host
from .validate import load_outline_document

PROG = "primer-slides inspect"
VISION_ROLE = "vision"

PROMPT_VERSION = "slides-inspect-1"
INSPECT_LOG_NAME = "inspect.jsonl"
DEFAULT_PDF_NAME = "slides.pdf"
DEFAULT_WORKERS = 4
DEFAULT_RENDER_DPI = 140
DEFAULT_VISION_MAX_TOKENS = 16384
RETRY_MAX_TOKENS_CAP = 32768
HTTP_TIMEOUT = client_mod.HTTP_TIMEOUT

SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"

# pdftohtml -xml 的画布单位是 1.5 px/pt（960×540 pt 出 1440×810）。
XML_SCALE = 1.5

# 横线：宽过页宽 30%、线厚薄于 2.5pt（粗色块/图块不是发丝线）。线要伸进左文字栏，只活在
# 右栏图里的横线是插图内部元素，不是页级分隔线。穿过字盒"内部"才报——上下各留 0.12 字高，
# 免得紧贴字盒边缘的基线/下划线被当成穿字（实测封面色带压到署名盒下缘 2.5pt，15% 会漏判）。
LINE_MIN_WIDTH_FRAC = 0.30
LINE_MAX_THICKNESS_PT = 2.5
LINE_TEXT_MARGIN_FRAC = 0.12
LINE_TEXT_OVERLAP_FRAC = 0.30

# 正文区：标题带以下、页脚以上、左文字栏内。左栏右界取 0.62 页宽——版心正文列止于
# ~592pt（0.617 页宽），右栏图注起于 ~601pt（0.626 页宽），0.62 正好把图注排除，也在
# 任务给的 0.65 界内。页脚墨迹 y>510pt，取 0.94 页高（507.6pt）为正文底界。
BODY_LEFT_FRAC = 0.62
REGION_TOP_GAP_PT = 6.0
REGION_BOTTOM_FRAC = 0.94
TITLE_BAND_FRAC = 0.25
TITLE_FALLBACK_FRAC = 0.12

# 行分组容差：同一行的 span 顶边相差约 3pt（数学行内盒略高），4pt 把它们并成一行。
ROW_TOLERANCE_PT = 4.0

# 居中：中心离散度小而左边缘离散度大。两端对齐（左右都齐）中心离散度也大，不误判。
CENTER_SD_CENT_PT = 6.0
CENTER_SD_LEFT_PT = 12.0
CENTER_MIN_LINES = 3

# 失衡：上下净空之比 > 1.8 且内容 < 45% 页高。
VI_RATIO = 1.8
VI_MAX_FILL = 0.45

# 行距：基线间距/字号 < 1.3 报挤；取"段内"间距（不超过 1.5 字号的多行间距）的中位数，
# 段间空档（通常更大）不算——那是段落间距，不是行距。
LEADING_MIN_RATIO = 1.3
LEADING_MAX_RATIO = 1.5
LEADING_MIN_DELTAS = 2
MIN_BODY_SPANS = 3

RASTER_THRESHOLD = 230

# 允许的字体族（去 subset 前缀后按前缀匹配）。数学（CM*／LMSans）放行。
ALLOWED_FONT_PREFIXES = ("HelveticaNeue", "HiraginoSansGB", "Menlo", "CM", "LMSans")
# 宋体/衬线族的标记（CJK 宋体 + 常见拉丁衬线）。纯 ASCII 的拉丁串（p.17、§2.1、1.2）不算
# 正文，另按 font-substitution 处理，故 serif-body 只认非 ASCII 文本。
SERIF_MARKERS = (
    "Songti", "STSong", "SimSun", "SimSong", "MingLiU", "Ming",
    "Mincho", "Roman", "Times", "Serif", "Georgia", "NimbusRom",
)
_SUBSET_PREFIX_RE = re.compile(r"^[A-Z]{6}\+")

CENTERED_ROLES = {"quote", "points"}
# 判"内容挤在上半区"的角色：目录页是封面之后唯一的整页结构页（旧版的结构图／阅读路径那两页
# 也判这一条），内容页同理。封面与主旨是刻意上部重构图，不判。
VERTICAL_ROLES = {"table", "points"}
LEADING_ROLES = {"quote", "points"}
# 视觉层的"失衡"类发现码：只在这类角色上收，与确定性层同一套豁免（封面/主旨这类上部重构图
# 不算缺陷；实测视觉模型会对封面报"下方空"）。
VISION_IMBALANCE_CODES = ("vertical-imbalance",)
# 视觉层量不准距离：几何类发现（裁切/穿字线）必须拿到确定性层的旁证才算数。
VISION_GEOMETRY_CODES = ("clipped-text", "line-through-text")
# 文本 span 距画布边多近才算"压边/越界"（视觉层的 clipped-text 拿这个做旁证）。
EDGE_TOLERANCE_PT = 2.0
# 丢掉一条视觉发现的两种原因（英文 token；报告里的中文见 SUPPRESSION_REASON_NAMES）。
SUPPRESSION_ROLE_EXEMPT = "role-exempt"
SUPPRESSION_GEOMETRY_UNCONFIRMED = "geometry-unconfirmed"
SUPPRESSION_REASON_NAMES = {
    SUPPRESSION_ROLE_EXEMPT: "角色豁免",
    SUPPRESSION_GEOMETRY_UNCONFIRMED: "几何未证实",
}
SUPPRESSION_REASON_NOTES = {
    SUPPRESSION_ROLE_EXEMPT: "封面/主旨这类上部重构图不算\"下方空\"",
    SUPPRESSION_GEOMETRY_UNCONFIRMED: (
        "视觉模型量不准距离：确定性层没有量到同一处（贴边/越界、线穿字都要求旁证）"
    ),
}

ROLE_NAMES = {
    "title": "封面",
    "quote": "主旨",
    "table": "目录",
    "points": "内容页",
    "backup": "备份",
    "unknown": "未知",
}

# 每种发现的"该改哪里"提示（报告与账本里都带着它）。
HINTS = {
    "line-through-text": (
        "封面色带底由标题块实测高决定（beamer._cover_frame）；页级分隔线要不落在文字盒内"
    ),
    "centered-body": "quote 帧应左对齐（beamer 的主旨帧版式）；正文块一律左对齐",
    "vertical-imbalance": (
        "按页面角色调整内容或版式：目录页/内容页内容不足时补正文，或改版心（theme.canvas.margin_ratio）"
    ),
    "serif-body": (
        "outline 的 theme.fonts / 导言区：正文族应走 sans（HelveticaNeue / HiraginoSansGB-*），"
        "宋体只在必要时出现"
    ),
    "cramped-leading": "theme.type.body.leading_pt：行距比应 ≥ 1.3（22pt 正文配 30pt 行距）",
    "numerator-residue": (
        "页面上不该印原文编号：build._pick_text 里的 prose.strip_lead_enumerator 没生效，"
        "或这一帧绕过了它；确认 picks 走的是同一条渲染路径"
    ),
    "font-substitution": (
        "允许集合：HelveticaNeue / HiraginoSansGB-* / Menlo，数学 CM*/LMSans；检查导言区的字体设置"
    ),
    "glyph-fallback": "缺字属硬失败（build 的日志复核 Missing character）；检查导言区的字体覆盖",
    "frame-count-mismatch": "build 出来的 PDF 页数应等于 outline 的帧数（slides）；先重跑 build",
    "clipped-text": "检查该元素是否超出画布/版心：beamer 帧版式与元素宽度（theme、figure 宽度）",
    "overlap": "检查该页元素的落位：beamer 的帧版式把图与文字分栏摆放",
    "garbled-glyph": "缺字是硬失败，见 build 的日志复核；字体族判定以确定性层为准",
    "alignment-mismatch": "同一块各行左边缘要齐；本该左对齐的段落不要居中（见 beamer 的主旨帧版式）",
    "layout-other": "人工复核这一页的版式",
}

# 视觉层逐条的编号 → 发现码；编号就是提示词里那七问的序号。
VISION_QUESTIONS: Tuple[Tuple[str, str], ...] = (
    ("line-through-text", "有没有横线/色带/边框压在文字上（像删除线、或横穿字形）？"),
    ("clipped-text", "有没有文字被裁切、超出画布，或紧贴边缘？"),
    ("overlap", "有没有元素互相重叠（文字压图、图压文字、文字叠印）？"),
    ("garbled-glyph", "有没有乱码、方块、缺字，或字形明显变形、某处过粗？"),
    ("vertical-imbalance", "文字块分布是否明显失衡（上方挤、下方空出超过页面高度的 40%）？"),
    ("alignment-mismatch", "对齐是否一致（同一块各行左边缘是否齐；本该左对齐的段落是否居中）？"),
    ("layout-other", "其它一眼可辨的排版缺陷。"),
)
VISION_CODE_BY_NUMBER = {index: code for index, (code, _) in enumerate(VISION_QUESTIONS, 1)}


class InspectError(SlidesError):
    """质检跑不下去（PDF／poppler 缺失、参数非法、端点配置读不出来）。"""


# ---------------------------------------------------------------- 数据


@dataclass(frozen=True)
class Canvas:
    """一页的 pt 画布。"""

    width: float
    height: float


@dataclass(frozen=True)
class Span:
    """``pdftohtml -xml`` 的一个文本 span，坐标已换算成 pt（原点左上）。"""

    top: float
    left: float
    right: float
    bottom: float
    size: float
    family: str
    text: str

    @property
    def width(self) -> float:
        return self.right - self.left

    @property
    def height(self) -> float:
        return self.bottom - self.top


@dataclass(frozen=True)
class HLine:
    """一条横线（栅格化扫出来的水平长条），坐标 pt。"""

    y0: float
    y1: float
    x0: float
    x1: float

    @property
    def thickness(self) -> float:
        return self.y1 - self.y0


@dataclass(frozen=True)
class Region:
    """正文区（标题带以下、页脚以上、左文字栏内）。"""

    top: float
    bottom: float
    left_max: float


@dataclass(frozen=True)
class FrameRef:
    """一帧 → 一页：角色、种类、纸面标签，以及它自己的 picks。"""

    page: int
    role: str
    kind: str
    label: str
    picks: Tuple[object, ...] = ()


@dataclass(frozen=True)
class Finding:
    """一条发现。``code``／``severity`` 英文，说明与证据中文。"""

    code: str
    severity: str
    page: int
    message: str
    evidence: str = ""
    hint: str = ""

    @property
    def fatal(self) -> bool:
        return self.severity == SEVERITY_ERROR

    def as_dict(self) -> Mapping[str, object]:
        return {
            "code": self.code,
            "severity": self.severity,
            "page": self.page,
            "message": self.message,
            "evidence": self.evidence,
            "hint": self.hint,
        }


@dataclass(frozen=True)
class Suppression:
    """一条被丢掉的视觉层发现：``code``（发现码）、``reason``（两种丢法之一）、``count``。"""

    code: str
    reason: str
    count: int = 1

    def as_dict(self) -> Mapping[str, object]:
        return {"code": self.code, "reason": self.reason, "count": self.count}


@dataclass
class VisionResult:
    """一页的视觉层结果。"""

    status: str = "skipped"  # ok / empty / error / disabled / skipped
    findings: Tuple[Finding, ...] = ()
    reply: str = ""
    error: str = ""
    usage: Tuple[int, int, int, int] = (0, 0, 0, 0)
    retried: bool = False
    # 被丢掉的视觉层发现，按（code, reason）聚合：角色豁免（封面/主旨上的"下方空"）与
    # 几何未证实（裁切/穿字线拿不到确定性层的旁证）。找齐证据之前不打扰人。
    suppressed: Tuple[Suppression, ...] = ()

    @property
    def suppressed_total(self) -> int:
        return sum(item.count for item in self.suppressed)

    def suppressed_count(self, code: str) -> int:
        return sum(item.count for item in self.suppressed if item.code == code)


@dataclass
class PageResult:
    """一页的确定性数值与发现。"""

    ref: FrameRef
    metrics: Mapping[str, object] = field(default_factory=dict)
    findings: Tuple[Finding, ...] = ()
    vision: Optional[VisionResult] = None

    @property
    def code(self) -> str:
        return self.ref.role


@dataclass
class InspectResult:
    """一次 ``inspect`` 的全部结果。"""

    outline_path: Path
    project_root: Path
    pdf_path: Path
    deck: str
    pages_total: int
    dry_run: bool
    no_vision: bool
    workers: int
    refs: Tuple[FrameRef, ...]
    frame_mismatch: bool
    page_results: Tuple[PageResult, ...]
    deck_findings: Tuple[Finding, ...] = ()
    endpoint: Optional[Endpoint] = None
    log_path: Optional[Path] = None
    report_path: Optional[Path] = None
    written: bool = False
    requests: int = 0
    failures: Tuple[str, ...] = ()

    @property
    def findings(self) -> Tuple[Finding, ...]:
        collected: list = list(self.deck_findings)
        for result in self.page_results:
            collected.extend(result.findings)
            if result.vision is not None:
                collected.extend(result.vision.findings)
        return tuple(collected)

    @property
    def errors(self) -> Tuple[Finding, ...]:
        return tuple(item for item in self.findings if item.fatal)

    @property
    def warnings(self) -> Tuple[Finding, ...]:
        return tuple(item for item in self.findings if not item.fatal)

    @property
    def selected_pages(self) -> Tuple[int, ...]:
        return tuple(result.ref.page for result in self.page_results)


# ---------------------------------------------------------------- 纯函数：解析


def _parse_pnm_header(raw: bytes) -> Tuple[bytes, int, int, int, int]:
    """解 PBM/PGM 头：magic、宽、高、maxval、光栅起点。注释与任意空白都容得下。"""
    position = 0
    total = len(raw)

    def token() -> bytes:
        nonlocal position
        while position < total:
            char = raw[position: position + 1]
            if char in b" \t\r\n":
                position += 1
            elif char == b"#":
                while position < total and raw[position: position + 1] not in b"\r\n":
                    position += 1
            else:
                break
        start = position
        while position < total and raw[position: position + 1] not in b" \t\r\n":
            position += 1
        return raw[start:position]

    magic = token()
    try:
        width = int(token())
        height = int(token())
        # PBM(P4) 没有 maxval，height 之后直接是光栅。
        maxval = 1 if magic == b"P4" else int(token())
    except ValueError as exc:  # 头不是数字
        raise InspectError(f"cannot parse PBM/PGM header: {raw[:24]!r}") from exc
    position += 1  # maxval（或 P4 的 height）之后恰有一个空白
    return magic, width, height, maxval, position


def decode_raster(raw: bytes, threshold: int = RASTER_THRESHOLD) -> Tuple[int, int, list]:
    """PBM(P4)/PGM(P5) → （宽, 高, 逐行 0/1 字节串），1 表示墨。

    不引 Pillow：栅格是 ``pdftoppm -gray`` 的 PGM，头几行很好解。P4 的位先解成 0/1。
    """
    magic, width, height, maxval, offset = _parse_pnm_header(raw)
    if magic == b"P4":
        stride = (width + 7) // 8
        rows: list = []
        for y in range(height):
            base = offset + y * stride
            line = raw[base: base + stride]
            packed = bytearray(width)
            for x in range(width):
                byte = line[x >> 3] if (x >> 3) < len(line) else 0
                packed[x] = 1 if (byte >> (7 - (x & 7))) & 1 else 0
            rows.append(bytes(packed))
        return width, height, rows
    if magic == b"P5":
        if height <= 0 or width <= 0 or offset + width * height > len(raw):
            raise InspectError("PGM raster is truncated")
        scale = 255.0 / maxval if maxval else 1.0
        table = bytes(1 if (value * scale) < threshold else 0 for value in range(256))
        rows = [
            raw[offset + y * width: offset + (y + 1) * width].translate(table)
            for y in range(height)
        ]
        return width, height, rows
    raise InspectError(f"unsupported raster format: {magic!r} (expected P4 or P5)")


def _font_family_map(xml: str) -> Mapping[str, Tuple[float, str]]:
    found: dict = {}
    for match in re.finditer(
        r'<fontspec id="(\d+)" size="([\d.]+)" family="([^"]+)"', xml
    ):
        found[match.group(1)] = (float(match.group(2)), match.group(3))
    return found


def parse_page_xml(xml: str) -> Tuple[Canvas, list]:
    """``pdftohtml -xml`` 的一页 → （画布, span 列表）。坐标换算成 pt（除以 1.5）。"""
    page = re.search(r'<page[^>]*height="(\d+)"[^>]*width="(\d+)"', xml)
    if page is None:
        raise InspectError("pdftohtml -xml output has no <page> element")
    canvas = Canvas(width=int(page.group(2)) / XML_SCALE, height=int(page.group(1)) / XML_SCALE)
    families = _font_family_map(xml)
    spans: list = []
    for match in re.finditer(
        r'<text top="(-?\d+)" left="(-?\d+)" width="(\d+)" height="(\d+)" font="(\d+)">(.*?)</text>',
        xml,
    ):
        top, left, width, height = (int(match.group(i)) for i in range(1, 5))
        size, family = families.get(match.group(5), (0.0, "?"))
        spans.append(
            Span(
                top=top / XML_SCALE,
                left=left / XML_SCALE,
                right=(left + width) / XML_SCALE,
                bottom=(top + height) / XML_SCALE,
                size=size / XML_SCALE,
                family=family,
                text=re.sub(r"<[^>]+>", "", match.group(6)),
            )
        )
    return canvas, spans


def scan_hlines(
    rows: Sequence[bytes],
    width: int,
    scale: float,
    min_width_frac: float = LINE_MIN_WIDTH_FRAC,
) -> list:
    """逐行扫墨，聚出横向长条 → :class:`HLine` 列表（坐标 pt）。

    ``rows`` 是逐行的 0/1 字节串（1 为墨）。同一段里各行归并成一条线，x 范围取并集。
    """
    min_run = int(width * min_width_frac)
    segments: list = []
    current: Optional[list] = None
    for y, row in enumerate(rows):
        best = 0
        best_x = 0
        position = 0
        length = len(row)
        while min_run > 0:
            start = row.find(1, position)
            if start < 0:
                break
            end = row.find(0, start)
            if end < 0:
                end = length
            if end - start > best:
                best = end - start
                best_x = start
            position = end
        if best > min_run:
            if current is None:
                current = [y, y, best_x, best_x + best]
            else:
                current[1] = y
                current[2] = min(current[2], best_x)
                current[3] = max(current[3], best_x + best)
        elif current is not None:
            segments.append(current)
            current = None
    if current is not None:
        segments.append(current)
    return [
        HLine(y0=a * scale, y1=(b + 1) * scale, x0=c * scale, x1=d * scale)
        for a, b, c, d in segments
    ]


def strip_subset(name: str) -> str:
    """去掉 ``ABCDEF+`` 的 subset 前缀。"""
    return _SUBSET_PREFIX_RE.sub("", name or "")


def font_allowed(name: str) -> bool:
    """族名是否在允许集合内（去 subset 前缀后按前缀匹配）。"""
    base = strip_subset(name)
    return base.startswith(ALLOWED_FONT_PREFIXES)


def font_is_serif(name: str) -> bool:
    """族名是否是宋体/衬线族。"""
    base = strip_subset(name)
    return any(marker in base for marker in SERIF_MARKERS)


# ---------------------------------------------------------------- 纯函数：几何


def title_bottom(spans: Sequence[Span], canvas: Canvas) -> float:
    """标题带的下沿：页顶 25% 内最大字号的 span 的底边。"""
    band = [s for s in spans if s.text.strip() and s.top < TITLE_BAND_FRAC * canvas.height]
    if not band:
        return TITLE_FALLBACK_FRAC * canvas.height
    biggest = max(s.size for s in band)
    if biggest <= 0:
        return TITLE_FALLBACK_FRAC * canvas.height
    bottoms = [s.bottom for s in band if s.size >= biggest - 0.6]
    return max(bottoms) if bottoms else TITLE_FALLBACK_FRAC * canvas.height


def body_region(canvas: Canvas, spans: Sequence[Span]) -> Region:
    """正文区：标题带下 6pt 起、94% 页高止、左文字栏内。"""
    top = title_bottom(spans, canvas) + REGION_TOP_GAP_PT
    bottom = REGION_BOTTOM_FRAC * canvas.height
    if top >= bottom:  # 标题把正文区吃光了：退回一个最小可用区
        top = max(bottom - 1.0, 0.0)
    return Region(top=top, bottom=bottom, left_max=BODY_LEFT_FRAC * canvas.width)


def body_spans(spans: Sequence[Span], region: Region) -> list:
    """正文区里的文本 span：标题带以下、页脚以上、左文字栏内。"""
    return [
        span
        for span in spans
        if span.text.strip()
        and span.left < region.left_max
        and span.top >= region.top
        and span.bottom <= region.bottom
    ]


def region_spans(spans: Sequence[Span], region: Region) -> list:
    """正文区里的全部文本 span（不分栏）。

    "失衡"看的是页面上**看得见的内容**：右栏有图或图注时，下半页并不空，只量左栏会把
    这类页误判成"内容挤在上半区"。所以上下净空按两栏合起来算。
    """
    return [
        span
        for span in spans
        if span.text.strip()
        and span.top >= region.top
        and span.bottom <= region.bottom
    ]


def group_rows(spans: Sequence[Span], tolerance: float = ROW_TOLERANCE_PT) -> list:
    """按顶边容差聚行：top 相差不超过 ``tolerance`` 的 span 算同一行。"""
    rows: list = []
    for span in sorted(spans, key=lambda item: item.top):
        if rows and span.top - rows[-1][-1].top <= tolerance:
            rows[-1].append(span)
        else:
            rows.append([span])
    return rows


def mode_size(spans: Sequence[Span]) -> float:
    """正文 span 里最常见的字号（= 正文正文字号）。"""
    sizes = [round(span.size, 1) for span in spans if span.size > 0]
    if not sizes:
        return 0.0
    return Counter(sizes).most_common(1)[0][0]


# ---------------------------------------------------------------- 纯函数：判定


def find_line_hits(spans: Sequence[Span], hlines: Sequence[HLine], canvas: Canvas) -> list:
    """:func:`HLine` 穿进文本盒内部 → （线, span）列表。"""
    hits: list = []
    for line in hlines:
        if line.thickness > LINE_MAX_THICKNESS_PT:
            continue
        if line.x0 > BODY_LEFT_FRAC * canvas.width:  # 只活在右栏图里的线：插图内部元素
            continue
        for span in spans:
            if not span.text.strip() or span.width <= 0:
                continue
            overlap = min(line.x1, span.right) - max(line.x0, span.left)
            if overlap <= LINE_TEXT_OVERLAP_FRAC * span.width:
                continue
            low = span.top + LINE_TEXT_MARGIN_FRAC * span.height
            high = span.bottom - LINE_TEXT_MARGIN_FRAC * span.height
            if low <= line.y0 <= high:
                hits.append((line, span))
    return hits


def find_centered_body(
    spans: Sequence[Span], region: Region, role: str
) -> Tuple[Optional[Finding], Mapping[str, object]]:
    """正文块是否居中（只在期望左对齐的角色上判）。"""
    body = body_spans(spans, region)
    if role not in CENTERED_ROLES or len(body) < MIN_BODY_SPANS:
        return None, {}
    rows = group_rows(body)
    if len(rows) < CENTER_MIN_LINES:
        return None, {}
    lefts = [min(span.left for span in row) for row in rows]
    rights = [max(span.right for span in row) for row in rows]
    centers = [(left + right) / 2 for left, right in zip(lefts, rights)]
    sd_left = statistics.pstdev(lefts) if len(lefts) > 1 else 0.0
    sd_cent = statistics.pstdev(centers) if len(centers) > 1 else 0.0
    metrics = {
        "lines": len(rows),
        "sd_left_pt": round(sd_left, 1),
        "sd_cent_pt": round(sd_cent, 1),
    }
    if sd_cent < CENTER_SD_CENT_PT and sd_left > CENTER_SD_LEFT_PT:
        finding = Finding(
            code="centered-body",
            severity=SEVERITY_ERROR,
            page=0,
            message="正文块看起来是居中的，但这一页的正文应当左对齐",
            evidence=(
                f"{len(rows)} 行：左边缘离散度 {sd_left:.1f}pt（> {CENTER_SD_LEFT_PT:g}），"
                f"行中心离散度 {sd_cent:.1f}pt（< {CENTER_SD_CENT_PT:g}）"
            ),
            hint=HINTS["centered-body"],
        )
        return finding, metrics
    return None, metrics


def find_vertical_imbalance(
    spans: Sequence[Span], region: Region, canvas: Canvas, role: str
) -> Tuple[Optional[Finding], Mapping[str, object]]:
    """内容是否挤在上半区（只在目录页/内容页上判；封面/主旨页上部重构图不算缺陷）。

    上下净空按页面**看得见的内容**算（两栏合起来，见 :func:`region_spans`）：
    右栏有图的页不会因为左栏短就被判成"下半页空"。
    """
    body = region_spans(spans, region)
    if role not in VERTICAL_ROLES or not body:
        return None, {}
    content_top = min(span.top for span in body)
    content_bottom = max(span.bottom for span in body)
    top_gap = content_top - region.top
    bottom_gap = region.bottom - content_bottom
    fill = (content_bottom - content_top) / canvas.height if canvas.height else 0.0
    if top_gap <= 0.5 or bottom_gap <= 0.5:
        return None, {}
    ratio = max(top_gap, bottom_gap) / min(top_gap, bottom_gap)
    metrics = {
        "content_top_pt": round(content_top, 1),
        "content_bottom_pt": round(content_bottom, 1),
        "fill_frac": round(fill, 3),
        "gap_ratio": round(ratio, 2),
    }
    if ratio > VI_RATIO and fill < VI_MAX_FILL:
        finding = Finding(
            code="vertical-imbalance",
            severity=SEVERITY_ERROR,
            page=0,
            message="内容挤在页面上部，下方空出太多",
            evidence=(
                f"内容占页高 {fill * 100:.0f}%（< {VI_MAX_FILL * 100:.0f}%），"
                f"上净空 {top_gap:.0f}pt : 下净空 {bottom_gap:.0f}pt = {ratio:.1f}:1"
                f"（> {VI_RATIO:g}:1）"
            ),
            hint=HINTS["vertical-imbalance"],
        )
        return finding, metrics
    return None, metrics


def find_serif_body(spans: Sequence[Span], region: Region) -> Tuple[Optional[Finding], Mapping[str, object]]:
    """正文区里的宋体/衬线（纯 ASCII 的拉丁数字串不算正文）。"""
    hits = [
        span
        for span in body_spans(spans, region)
        if font_is_serif(span.family) and not span.text.isascii()
    ]
    if not hits:
        return None, {}
    families = sorted({strip_subset(span.family) for span in hits})
    sample = "、".join(span.text.strip()[:12] for span in hits[:3])
    finding = Finding(
        code="serif-body",
        severity=SEVERITY_ERROR,
        page=0,
        message="正文区出现了宋体/衬线字体",
        evidence=f"{len(hits)} 处，族：{'、'.join(families)}；例：{sample}",
        hint=HINTS["serif-body"],
    )
    return finding, {"serif_families": families, "serif_count": len(hits)}


def find_cramped_leading(
    spans: Sequence[Span], region: Region, role: str
) -> Tuple[Optional[Finding], Mapping[str, object]]:
    """正文行距比 < 1.3 → 报。段内间距取中位数，段间空档不算。"""
    body = body_spans(spans, region)
    if role not in LEADING_ROLES or len(body) < MIN_BODY_SPANS:
        return None, {}
    size = mode_size(body)
    if size <= 0:
        return None, {}
    rows = group_rows(body)
    if len(rows) < 2:
        return None, {}
    tops = [row[0].top for row in rows]
    deltas = [tops[i + 1] - tops[i] for i in range(len(tops) - 1)]
    inner = [delta for delta in deltas if 0 < delta <= LEADING_MAX_RATIO * size]
    metrics = {
        "body_size_pt": size,
        "row_count": len(rows),
        "leading_pt": round(statistics.median(inner), 1) if inner else None,
    }
    if len(inner) < LEADING_MIN_DELTAS:
        return None, metrics
    leading = statistics.median(inner)
    ratio = leading / size
    metrics["leading_ratio"] = round(ratio, 2)
    if ratio < LEADING_MIN_RATIO:
        finding = Finding(
            code="cramped-leading",
            severity=SEVERITY_ERROR,
            page=0,
            message="正文行距过挤",
            evidence=(
                f"段内基线间距 {leading:.1f}pt / 字号 {size:.1f}pt = {ratio:.2f}"
                f"（< {LEADING_MIN_RATIO:g}）"
            ),
            hint=HINTS["cramped-leading"],
        )
        return finding, metrics
    return None, metrics


def check_page(
    ref: FrameRef, canvas: Canvas, spans: Sequence[Span], hlines: Sequence[HLine]
) -> Tuple[Mapping[str, object], list]:
    """一页的确定性检查：返回（数值, 发现）。"""
    region = body_region(canvas, spans)
    body = body_spans(spans, region)
    findings: list = []

    hits = find_line_hits(spans, hlines, canvas)
    for line, span in hits:
        findings.append(
            Finding(
                code="line-through-text",
                severity=SEVERITY_ERROR,
                page=ref.page,
                message="一条横线压在文字上（像删除线）",
                evidence=(
                    f"线 y={line.y0:.1f}pt（厚 {line.thickness:.2f}pt，x "
                    f"{line.x0:.0f}–{line.x1:.0f}）穿过文本「{span.text.strip()[:20]}」"
                    f"（盒 y {span.top:.1f}–{span.bottom:.1f}）"
                ),
                hint=HINTS["line-through-text"],
            )
        )

    centered_finding, centered_metrics = find_centered_body(spans, region, ref.role)
    imbalance_finding, imbalance_metrics = find_vertical_imbalance(spans, region, canvas, ref.role)
    leading_finding, leading_metrics = find_cramped_leading(spans, region, ref.role)
    serif_finding, serif_metrics = find_serif_body(spans, region)
    for finding in (centered_finding, imbalance_finding, leading_finding, serif_finding):
        if finding is not None:
            findings.append(replace(finding, page=ref.page))

    metrics: dict = {
        "title_bottom_pt": round(title_bottom(spans, canvas), 1),
        "body_spans": len(body),
        "line_hits": len(hits),
    }
    metrics.update(centered_metrics)
    metrics.update(imbalance_metrics)
    metrics.update(leading_metrics)
    metrics.update(serif_metrics)
    return metrics, findings


# ---------------------------------------------------------------- 纯函数：帧序与角色


def frame_refs(document: Mapping[str, object]) -> Tuple[FrameRef, ...]:
    """从 outline 推出帧序与页角色，顺序与 ``build.compose_frames`` 一致。

    组序固定：开场（封面/主旨）→ 目录（一篇／章的索引表）→ 各章正文页 →
    横向议题 → 讨论 → 备份。PDF 页序与帧序一致。
    """
    entries = read_page_entries(document)
    refs: list = []
    page = 1

    for index, entry in enumerate([item for item in entries if item.kind == "opening"]):
        refs.append(
            FrameRef(page, "title" if index == 0 else "quote", "opening", entry.label,
                     tuple(entry.picks))
        )
        page += 1

    nav_roles = ("table",)
    for index, entry in enumerate([item for item in entries if item.kind == "navigation"]):
        refs.append(FrameRef(page, nav_roles[index % 3], "navigation", entry.label, ()))
        page += 1

    chapters = document.get("chapters") or []
    for chapter in chapters:
        if not isinstance(chapter, Mapping):
            continue
        label = str(chapter.get("label") or chapter.get("chapter") or "")
        for frame in chapter.get("frames") or []:
            if not isinstance(frame, Mapping):
                continue
            picks = frame.get("picks")
            picks = tuple(picks) if isinstance(picks, list) else ()
            refs.append(FrameRef(page, "points", "content", label, picks))
            page += 1

    for kind in ("cross_cutting", "discussion"):
        for entry in entries:
            if entry.kind == kind:
                refs.append(FrameRef(page, "points", kind, entry.label, tuple(entry.picks)))
                page += 1

    for entry in entries:
        if entry.kind == "backup":
            refs.append(FrameRef(page, "backup", "backup", entry.label, ()))
            page += 1
    return tuple(refs)


def lead_enumerator(text: str) -> Optional[str]:
    """行首可剥的原文编号本体（``（一）``／``1.``…）；没有就返回 ``None``。

    与 :func:`primer.slides.prose.strip_lead_enumerator` 同一个判据：它剥得掉什么，这里就
    返回什么。剥不掉时返回 ``None``（连行首空白也不动）。
    """
    stripped = strip_lead_enumerator(text)
    if stripped == text:
        return None
    return text[: len(text) - len(stripped)].strip()


# 行首的短横 bullet 与空白（beamer 的要点符号），比对编号前先去掉。
_LEAD_MARK_RE = re.compile(r"^[\s\u3000\-–—•·*]+")


def printed_enumerators(spans: Sequence[Span], region: Region) -> set:
    """这一页**正文区**真的印出来的行首编号（按行取每行第一个非 bullet 片段）。

    picks 里可剥的编号不代表页面上看得见：``build._pick_text`` 在渲染时已经剥掉，页面是
    干净的。编号残留只有在页面上真的印出来时才算缺陷，所以用这一页自己的 span 来核对。
    """
    found: set = set()
    for row in group_rows(body_spans(spans, region)):
        text = "".join(span.text for span in sorted(row, key=lambda item: item.left))
        token = lead_enumerator(_LEAD_MARK_RE.sub("", text))
        if token:
            found.add(token)
    return found


def numerator_residue_findings(
    refs: Sequence[FrameRef],
    rows: Mapping[str, cand.CandidateRow],
    printed: Mapping[int, set],
) -> list:
    """``numerator-residue``：picks 里可剥的编号**且页面上真的印出了**才报。

    ``printed`` 是逐页：``printed_enumerators`` 量到的行首编号集合。页面已剥（集合里没有
    这个编号）就不报 —— 渲染层剥编号是既成事实，报出来只是噪声。
    """
    findings: list = []
    seen: set = set()
    for ref in refs:
        page_tokens = printed.get(ref.page, set())
        for value in ref.picks:
            try:
                pick = cand.parse_pick(value, rows)
            except cand.PickError:
                continue
            display = cand.pick_display(pick, rows)
            if not display:
                continue
            token = lead_enumerator(display)
            if token is None or token not in page_tokens:
                continue
            key = (ref.page, token, display)
            if key in seen:
                continue
            seen.add(key)
            findings.append(
                Finding(
                    code="numerator-residue",
                    severity=SEVERITY_WARNING,
                    page=ref.page,
                    message="页面上印出了原文编号（要点已经带短横 bullet，编号是多余的）",
                    evidence=(
                        f"{pick.identifier or '（自由文本）'}：页面上以「{token}」开头 "
                        f"（picks 原文「{display[:24]}」）"
                    ),
                    hint=HINTS["numerator-residue"],
                )
            )
    return findings


def font_findings(names: Sequence[str]) -> list:
    """``pdffonts`` 全表里允许集合外的族 → ``font-substitution``／``glyph-fallback``。"""
    outside = sorted({strip_subset(name) for name in names if name and not font_allowed(name)})
    if not outside:
        return []
    return [
        Finding(
            code="font-substitution",
            severity=SEVERITY_WARNING,
            page=0,
            message="PDF 里用了允许集合之外的字体族（可能是替代字体或回退）",
            evidence="、".join(outside),
            hint=HINTS["font-substitution"],
        )
    ]


# ---------------------------------------------------------------- 视觉层


def vision_role_name(role: str) -> str:
    return ROLE_NAMES.get(role, role)


def vision_prompt(canvas: Canvas, role: str, facts: str) -> str:
    """一页的视觉层提示词：角色 + 确定性层量到的数 + 逐条 7 问。"""
    questions = "\n".join(
        f"{index}. {text}" for index, (_, text) in enumerate(VISION_QUESTIONS, 1)
    )
    header = (
        f"这是一张 16:9 幻灯片整页渲染图（画布 {canvas.width:.0f}×{canvas.height:.0f} pt）。"
        f"页面角色：{vision_role_name(role)}。"
    )
    context = f"\n{facts}" if facts else ""
    return (
        f"{header}{context}\n"
        "只报告你在这张图上**看得见**的具体缺陷。每条必须指出位置与证据（什么和什么冲突/错位）。"
        "不评价整体美感；\"有留白\"本身不算缺陷（除非它明显失衡）；不猜看不见的东西。"
        "字体族（宋体还是黑体）由确定性层判定，你不必判字体族，只报乱码/方块/缺字/变形。\n"
        f"逐条回答，没有就写\"无\"：\n{questions}\n"
        "输出格式：每行 `编号 | 有/无 | 一句话证据`。"
    )


def metrics_facts(metrics: Mapping[str, object], hits: int) -> str:
    """把确定性层的数写成提示词里的事实句。"""
    parts: list = []
    if hits:
        parts.append(f"确定性检测：这一页有 {hits} 条横线穿到文字盒内部。")
    if metrics.get("sd_cent_pt") is not None and metrics.get("lines"):
        parts.append(
            f"确定性检测：正文各行左边缘离散度 {metrics['sd_left_pt']}pt、"
            f"行中心离散度 {metrics['sd_cent_pt']}pt。"
        )
    if metrics.get("fill_frac") is not None:
        parts.append(
            f"确定性检测：内容占到页高 {float(metrics['fill_frac']) * 100:.0f}%，"
            f"上下净空比 {metrics.get('gap_ratio')}。"
        )
    if metrics.get("leading_ratio") is not None:
        parts.append(
            f"确定性检测：正文行距 {metrics['leading_pt']}pt / 字号 "
            f"{metrics['body_size_pt']}pt = {metrics['leading_ratio']}。"
        )
    return "".join(parts)


def vision_payload(
    model: str, prompt: str, png: bytes, max_tokens: int, temperature: Optional[float]
) -> bytes:
    """多模态请求体：提示词 + base64 data URL 图像。"""
    content = [
        {"type": "text", "text": prompt},
        {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64," + base64.b64encode(png).decode("ascii")},
        },
    ]
    body = {
        "model": model,
        "temperature": 0 if temperature is None else temperature,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": content}],
    }
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


def reply_is_empty_message(raw: bytes) -> bool:
    """回信结构完整但正文为空（思考型模型把预算花在推理链上）→ 可重试。"""
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except (json.JSONDecodeError, ValueError):
        return False
    if not isinstance(payload, dict):
        return False
    if "choices" not in payload and payload.get("error"):
        return False
    choices = payload.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        return False
    content = (choices[0].get("message") or {}).get("content")
    if isinstance(content, list):
        content = "".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        )
    return not (isinstance(content, str) and content.strip())


def parse_vision_reply(text: str) -> list:
    """解析 ``编号 | 有/无 | 证据`` 的回信 → :class:`Finding` 列表（severity warning）。

    识别不出的行跳过；只有明确答"有/是"的行才算一条发现。
    """
    findings: list = []
    for line in (text or "").splitlines():
        line = line.strip().strip("`").strip()
        if not line:
            continue
        parts = [part.strip() for part in line.split("|")]
        if len(parts) < 2:
            continue
        number = re.match(r"^(\d+)", parts[0])
        if number is None:
            continue
        verdict = parts[1]
        evidence = " | ".join(parts[2:]).strip()
        # 只有明确答"有/是"的行才算一条发现；"无"、"否"、"none" 都跳过。
        if "有" not in verdict and not verdict.startswith("是"):
            continue
        code = VISION_CODE_BY_NUMBER.get(int(number.group(1)), "layout-other")
        findings.append(
            Finding(
                code=code,
                severity=SEVERITY_WARNING,
                page=0,
                message=evidence or "视觉层报了这一条",
                evidence=line,
                hint=HINTS.get(code, HINTS["layout-other"]),
            )
        )
    return findings


def edge_distance(span: Span, canvas: Canvas) -> float:
    """一个文本 span 到最近画布边的距离（负值表示越界）。"""
    return min(
        span.left,
        span.top,
        canvas.width - span.right,
        canvas.height - span.bottom,
    )


def edge_violations(
    spans: Sequence[Span], canvas: Canvas, tolerance: float = EDGE_TOLERANCE_PT
) -> list:
    """压在画布边上（或越界）的文本 span，最近的排在前面。

    视觉层的 ``clipped-text`` 拿它做旁证：实测模型会把正常版心边距（48pt）读成"约 9pt"，
    所以"贴边"只在确定性层量到距边 < :data:`EDGE_TOLERANCE_PT`（或越界）时才算数。
    """
    found = [
        (edge_distance(span, canvas), span)
        for span in spans
        if span.text.strip()
    ]
    hits = [(distance, span) for distance, span in found if distance <= tolerance]
    return sorted(hits, key=lambda item: item[0])


def merge_suppressions(items: Sequence[Suppression]) -> Tuple[Suppression, ...]:
    """把 ``(code, reason)`` 相同的丢掉项聚成一条（保持首次出现的次序）。"""
    order: list = []
    counts: dict = {}
    for item in items:
        key = (item.code, item.reason)
        if key not in counts:
            counts[key] = 0
            order.append(key)
        counts[key] += item.count
    return tuple(Suppression(code, reason, counts[(code, reason)]) for code, reason in order)


def _vision_confirm(
    role: str,
    findings: Sequence[Finding],
    *,
    canvas: Canvas,
    spans: Sequence[Span],
    hlines: Sequence[HLine],
) -> Tuple[list, Tuple[Suppression, ...]]:
    """视觉层的几何类发现要有确定性层的旁证；返回（保留的, 丢掉的）。

    * ``clipped-text``：只在确定性层量到某个文本 span 压边/越界时才收。
    * ``line-through-text``：只在确定性层同一页扫到"线 × 文本盒相交"时才收。
    * ``vertical-imbalance``：角色豁免照旧（封面/主旨这类上部重构图不算"下方空"）。
    * 其余定性发现（``overlap``／``alignment-mismatch``／``glyph-fallback``／
      ``layout-other``）本来就要人复核，原样保留。

    收下的几何类发现会把确定性层的实测数补进证据里，报告里因此看得见它凭什么算数。
    """
    line_hits = find_line_hits(spans, hlines, canvas)
    violations = edge_violations(spans, canvas)
    nearest = violations[0] if violations else None
    kept: list = []
    dropped: list = []
    for item in findings:
        if item.code in VISION_IMBALANCE_CODES:
            if role in VERTICAL_ROLES:
                kept.append(item)
            else:
                dropped.append(Suppression(item.code, SUPPRESSION_ROLE_EXEMPT))
            continue
        if item.code == "line-through-text":
            if line_hits:
                line, span = line_hits[0]
                kept.append(
                    replace(
                        item,
                        evidence=(
                            f"{item.evidence}（确定性层：y={line.y0:.1f}pt 的线穿过"
                            f"「{span.text.strip()[:16]}」，共 {len(line_hits)} 处）"
                        ),
                    )
                )
            else:
                dropped.append(Suppression(item.code, SUPPRESSION_GEOMETRY_UNCONFIRMED))
            continue
        if item.code == "clipped-text":
            if nearest is not None:
                distance, span = nearest
                where = "越界" if distance < 0 else f"距画布边 {distance:.1f}pt"
                kept.append(
                    replace(
                        item,
                        evidence=(
                            f"{item.evidence}（确定性层：「{span.text.strip()[:16]}」"
                            f"{where}，共 {len(violations)} 处）"
                        ),
                    )
                )
            else:
                dropped.append(Suppression(item.code, SUPPRESSION_GEOMETRY_UNCONFIRMED))
            continue
        kept.append(item)
    return kept, merge_suppressions(dropped)


def _ask_page(
    *,
    url: str,
    key: str,
    model: str,
    temperature: Optional[float],
    png: bytes,
    prompt: str,
    transport,
    timeout: float,
    max_tokens: int,
) -> Tuple[VisionResult, tuple]:
    """一页一次请求（空正文重试一次、预算翻倍）。worker 自带用量，不共享可变状态。"""
    usage = client_mod.UsageTotals()
    budget = max_tokens
    error = ""
    retried = False
    for attempt in range(2):
        request = client_mod.HttpRequest(
            url=url,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
            body=vision_payload(model, prompt, png, budget, temperature),
            timeout=timeout,
        )
        try:
            raw = transport(request)
        except Exception as exc:  # noqa: BLE001 — 传输层错误原样记下来，不吞
            usage.requests += 1
            error = f"{type(exc).__name__}: {exc}"
            break
        try:
            reply = client_mod.parse_chat_reply(raw)
        except client_mod.SelectCallError as exc:
            usage.requests += 1
            if reply_is_empty_message(raw) and attempt == 0:
                retried = True
                budget = min(budget * 2, RETRY_MAX_TOKENS_CAP)
                continue
            error = str(exc)
            break
        usage.add(reply.usage)
        findings = tuple(parse_vision_reply(reply.content))
        result = VisionResult(
            status="ok",
            findings=findings,
            reply=reply.content,
            usage=usage.snapshot(),
            retried=retried,
        )
        return result, usage.snapshot()
    status = "empty" if retried and not error else ("error" if error else "empty")
    result = VisionResult(
        status=status, error=error, usage=usage.snapshot(), retried=retried
    )
    return result, usage.snapshot()


# ---------------------------------------------------------------- poppler 封装


class PdfTools:
    """只用 poppler 命令行的最小封装（无 Pillow、无新依赖）。"""

    def __init__(self, pdf: Path, dpi: int = DEFAULT_RENDER_DPI,
                 threshold: int = RASTER_THRESHOLD):
        self.pdf = Path(pdf)
        self.dpi = dpi
        self.threshold = threshold

    def ensure_available(self) -> None:
        missing = [tool for tool in ("pdftohtml", "pdftoppm", "pdffonts", "pdfinfo")
                   if shutil.which(tool) is None]
        if missing:
            raise InspectError(
                "poppler tools not found on PATH: " + ", ".join(missing)
                + " (install poppler-utils; inspect needs pdftohtml, pdftoppm, pdffonts, pdfinfo)"
            )

    def _run(self, args: Sequence[str]) -> subprocess.CompletedProcess:
        completed = subprocess.run(list(args), capture_output=True)
        if completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", "replace").strip()
            raise InspectError(
                f"{args[0]} failed with exit code {completed.returncode}: {detail[:200]}"
            )
        return completed

    def page_count(self) -> int:
        text = self._run([shutil.which("pdfinfo") or "pdfinfo", str(self.pdf)]).stdout.decode(
            "utf-8", "replace"
        )
        match = re.search(r"^Pages:\s+(\d+)", text, re.M)
        if match is None:
            raise InspectError(f"cannot read page count from pdfinfo for {self.pdf}")
        return int(match.group(1))

    def page_spans(self, page: int) -> Tuple[Canvas, list]:
        completed = self._run([
            shutil.which("pdftohtml") or "pdftohtml", "-xml", "-i", "-stdout",
            "-f", str(page), "-l", str(page), str(self.pdf),
        ])
        return parse_page_xml(completed.stdout.decode("utf-8", "replace"))

    def page_hlines(self, page: int) -> list:
        with tempfile.TemporaryDirectory() as directory:
            prefix = os.path.join(directory, "page")
            self._run([
                shutil.which("pdftoppm") or "pdftoppm", "-gray", "-r", str(self.dpi),
                "-f", str(page), "-l", str(page), str(self.pdf), prefix,
            ])
            files = sorted(Path(directory).glob("page*.pgm"))
            if not files:
                raise InspectError(f"pdftoppm produced no PGM for page {page}")
            raw = files[0].read_bytes()
        width, height, rows = decode_raster(raw, self.threshold)
        return scan_hlines(rows, width, 72.0 / self.dpi)

    def page_png(self, page: int) -> bytes:
        with tempfile.TemporaryDirectory() as directory:
            prefix = os.path.join(directory, "page")
            self._run([
                shutil.which("pdftoppm") or "pdftoppm", "-png", "-r", str(self.dpi),
                "-f", str(page), "-l", str(page), str(self.pdf), prefix,
            ])
            files = sorted(Path(directory).glob("page*.png"))
            if not files:
                raise InspectError(f"pdftoppm produced no PNG for page {page}")
            return files[0].read_bytes()

    def font_names(self) -> list:
        text = self._run([shutil.which("pdffonts") or "pdffonts", str(self.pdf)]).stdout.decode(
            "utf-8", "replace"
        )
        names: list = []
        for line in text.splitlines()[2:]:  # 前两行是表头与分隔线
            parts = line.split()
            if parts:
                names.append(parts[0])
        return names


# ---------------------------------------------------------------- 运行


def parse_pages(text: Optional[str], total: int) -> Optional[Tuple[int, int]]:
    """``A-B`` 或 ``A`` → 页区间；非法就报错。"""
    if not text:
        return None
    match = re.fullmatch(r"\s*(\d+)\s*(?:-\s*(\d+)\s*)?", text)
    if match is None:
        raise InspectError(f"invalid --pages {text!r}: use A-B or A")
    start = int(match.group(1))
    end = int(match.group(2)) if match.group(2) else start
    if start < 1 or end < start:
        raise InspectError(f"invalid --pages {text!r}: need 1 <= A <= B")
    return start, end


def _resolve_endpoint(
    role: str, root: Path, config_path: Optional[Path],
    environ: Mapping[str, str], max_tokens: Optional[int], *, tolerate: bool,
) -> Optional[Endpoint]:
    try:
        config = load_config(root, config_path, environ=environ)
        return resolve_role(config, role, max_tokens=max_tokens)
    except ConfigError as exc:
        if tolerate:
            return None
        raise InspectError(str(exc)) from exc


def run(
    outline_path: Path,
    project_root: Path = Path("."),
    *,
    pdf_path: Optional[Path] = None,
    pages: Optional[str] = None,
    no_vision: bool = False,
    workers: int = DEFAULT_WORKERS,
    dry_run: bool = False,
    report_path: Optional[Path] = None,
    transport=None,
    environ: Optional[Mapping[str, str]] = None,
    config_path: Optional[Path] = None,
    tools=None,
    max_tokens: Optional[int] = None,
    timeout: Optional[float] = None,
    dpi: int = DEFAULT_RENDER_DPI,
) -> InspectResult:
    """跑一次 ``inspect``：确定性层逐页检查 +（可选）视觉层逐页提问 + 记账。"""
    outline = Path(outline_path).expanduser().resolve()
    if not outline.is_file():
        raise InspectError(f"outline not found: {outline}")
    if workers < 1:
        raise InspectError(f"--workers must be >= 1 (got {workers})")
    root = Path(project_root).expanduser().resolve()

    document = load_outline_document(outline)
    deck_block = document.get("deck")
    deck_block = deck_block if isinstance(deck_block, Mapping) else {}
    deck = str(deck_block.get("deck") or "slides")

    pdf = Path(pdf_path).expanduser().resolve() if pdf_path else outline.parent / DEFAULT_PDF_NAME
    if not pdf.is_file():
        raise InspectError(
            f"slides PDF not found: {pdf} (build the deck first, or pass --pdf FILE)"
        )

    owner = tools if tools is not None else PdfTools(pdf, dpi=dpi)
    owner.ensure_available()

    refs = frame_refs(document)
    total = owner.page_count()
    mismatch = len(refs) != total
    selected = parse_pages(pages, total)
    page_numbers = [
        number for number in range(1, total + 1)
        if selected is None or selected[0] <= number <= selected[1]
    ]

    env: Mapping[str, str] = os.environ if environ is None else environ
    result = InspectResult(
        outline_path=outline,
        project_root=root,
        pdf_path=pdf,
        deck=deck,
        pages_total=total,
        dry_run=dry_run,
        no_vision=no_vision,
        workers=workers,
        refs=refs,
        frame_mismatch=mismatch,
        page_results=(),
    )

    endpoint: Optional[Endpoint] = None
    if not no_vision:
        endpoint = _resolve_endpoint(
            VISION_ROLE, root, config_path, env, max_tokens, tolerate=dry_run
        )
    result.endpoint = endpoint

    deck_findings: list = []
    if mismatch:
        deck_findings.append(
            Finding(
                code="frame-count-mismatch",
                severity=SEVERITY_WARNING,
                page=0,
                message="outline 的帧数与 PDF 页数不一致，按序号尽力对齐",
                evidence=f"outline 推出 {len(refs)} 帧，PDF 有 {total} 页",
                hint=HINTS["frame-count-mismatch"],
            )
        )

    if dry_run:
        result.deck_findings = tuple(deck_findings)
        return result

    # ---------------- 确定性层
    page_results: list = []
    page_io: dict = {}
    for number in page_numbers:
        ref = refs[number - 1] if number - 1 < len(refs) else FrameRef(
            number, "unknown", "unknown", ""
        )
        canvas, spans = owner.page_spans(number)
        hlines = owner.page_hlines(number)
        metrics, findings = check_page(ref, canvas, spans, hlines)
        page_io[number] = (canvas, spans, hlines)
        page_results.append(PageResult(ref=ref, metrics=metrics, findings=tuple(findings)))

    # 整份的确定性检查：要点残留编号（以页面为准）、字体全表。
    rows = _load_candidate_rows(outline, document)
    printed = {
        number: printed_enumerators(spans, body_region(canvas, spans))
        for number, (canvas, spans, _) in page_io.items()
    }
    residue = numerator_residue_findings(refs, rows, printed)
    if selected is not None:
        residue = [
            item for item in residue
            if item.page == 0 or selected[0] <= item.page <= selected[1]
        ]
    deck_findings.extend(residue)
    deck_findings.extend(font_findings(owner.font_names()))

    result.page_results = tuple(page_results)
    result.deck_findings = tuple(deck_findings)

    # ---------------- 视觉层
    if not no_vision and endpoint is not None and page_results:
        try:
            key = api_key(endpoint, env)
        except ConfigError as exc:
            raise InspectError(str(exc)) from exc
        result.failures, requests = _run_vision(
            result, endpoint, key, owner, page_io, transport,
            timeout if timeout is not None else HTTP_TIMEOUT,
        )
        result.requests = requests

    if report_path is not None:
        path = Path(report_path).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(markdown_report(result) + "\n", encoding="utf-8")
        result.report_path = path

    log_path = outline.parent / INSPECT_LOG_NAME
    _append_log(log_path, result)
    result.log_path = log_path
    result.written = True
    return result


def _load_candidate_rows(outline: Path, document: Mapping[str, object]) -> Mapping[str, cand.CandidateRow]:
    """候选表（与 outline 同一目录，缺了就退回空表 —— 编号残留因此查不到，不报错）。"""
    path = outline.parent / "candidates.md"
    if not path.is_file():
        return {}
    try:
        return cand.parse_markdown_table(path.read_text(encoding="utf-8"))
    except OSError:
        return {}


def _run_vision(
    result: InspectResult,
    endpoint: Endpoint,
    key: str,
    owner,
    page_io: Mapping[int, tuple],
    transport,
    timeout: float,
) -> Tuple[Tuple[str, ...], int]:
    """逐页并发提问；worker 只返回结果，写账本在父线程做（不开共享写）。"""
    budget = endpoint.max_tokens or DEFAULT_VISION_MAX_TOKENS
    url = client_mod.chat_url(endpoint.base_url)
    transport = transport or client_mod.urllib_transport

    payloads: dict = {}
    for page_result in result.page_results:
        page = page_result.ref.page
        canvas, spans, hlines = page_io[page]
        hits = find_line_hits(spans, hlines, canvas)
        prompt = vision_prompt(
            canvas, page_result.ref.role, metrics_facts(page_result.metrics, len(hits))
        )
        payloads[page] = (owner.page_png(page), prompt)

    failures: list = []
    totals = client_mod.UsageTotals()
    with concurrent.futures.ThreadPoolExecutor(max_workers=result.workers) as pool:
        futures = {
            pool.submit(
                _ask_page,
                url=url,
                key=key,
                model=endpoint.model,
                temperature=endpoint.temperature,
                png=payloads[page_result.ref.page][0],
                prompt=payloads[page_result.ref.page][1],
                transport=transport,
                timeout=timeout,
                max_tokens=budget,
            ): page_result
            for page_result in result.page_results
        }
        for future in concurrent.futures.as_completed(futures):
            page_result = futures[future]
            try:
                vision, usage = future.result()
            except Exception as exc:  # noqa: BLE001 — 单页失败不拖垮整份
                page_result.vision = VisionResult(status="error", error=f"{type(exc).__name__}: {exc}")
                failures.append(f"page {page_result.ref.page}: {exc}")
                continue
            canvas, spans, hlines = page_io[page_result.ref.page]
            allowed, suppressed = _vision_confirm(
                page_result.ref.role,
                vision.findings,
                canvas=canvas,
                spans=spans,
                hlines=hlines,
            )
            page_result.vision = VisionResult(
                status=vision.status,
                findings=tuple(replace(item, page=page_result.ref.page) for item in allowed),
                reply=vision.reply,
                error=vision.error,
                usage=vision.usage,
                retried=vision.retried,
                suppressed=suppressed,
            )
            totals.requests += usage[0]
            totals.prompt_tokens += usage[1]
            totals.completion_tokens += usage[2]
            totals.reasoning_tokens += usage[3]
            if vision.status == "error":
                failures.append(f"page {page_result.ref.page}: {vision.error}")
    return tuple(failures), totals.requests


# ---------------------------------------------------------------- 记账


def _append_log(path: Path, result: InspectResult) -> None:
    """逐页追加进 ``inspect.jsonl``。**只记 base_url 的 host，绝不写密钥。**"""
    when = datetime.now().astimezone().isoformat(timespec="seconds")
    records: list = []
    for page_result in result.page_results:
        records.append(_log_record(page_result, result, when))
    if not records:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=False) + "\n")


def _log_record(page_result: PageResult, result: InspectResult, when: str) -> Mapping[str, object]:
    endpoint = result.endpoint
    vision = page_result.vision
    return {
        "time": when,
        "deck": result.deck,
        "pdf": str(result.pdf_path),
        "page": page_result.ref.page,
        "role": page_result.ref.role,
        "label": page_result.ref.label,
        "provider": endpoint.provider if endpoint else "",
        "model": endpoint.model if endpoint else "",
        # 只记 scheme://host：URL 里可能嵌着凭据，端点身份到 host 就够了。
        "base_url": base_url_host(endpoint.base_url) if endpoint else "",
        "prompt_version": PROMPT_VERSION,
        "metrics": dict(page_result.metrics),
        "findings": [item.as_dict() for item in page_result.findings],
        "vision": {
            "status": "disabled" if result.no_vision else (vision.status if vision else "skipped"),
            "requests": vision.usage[0] if vision else 0,
            "retried": vision.retried if vision else False,
            "error": vision.error if vision else "",
            "suppressed": (
                [item.as_dict() for item in vision.suppressed] if vision else []
            ),
            "suppressed_total": vision.suppressed_total if vision else 0,
            "findings": [item.as_dict() for item in vision.findings] if vision else [],
            "reply": (vision.reply[:8000] if vision else ""),
        },
    }


# ---------------------------------------------------------------- 报告


def _relative(path: Path, root: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(Path(root).resolve()))
    except ValueError:
        return str(path)


def _role_counts(refs: Sequence[FrameRef]) -> str:
    counts: Counter = Counter(ref.role for ref in refs)
    order = ("title", "quote", "table", "points", "backup", "unknown")
    parts = [
        f"{ROLE_NAMES.get(role, role)} {counts[role]}"
        for role in order
        if counts.get(role)
    ]
    return "、".join(parts)


def report_lines(result: InspectResult) -> Tuple[str, ...]:
    """stdout 中文报告：先总结、再逐页、再按"该改哪里"汇总。"""
    lines: list = ["primer-slides inspect · 版面质检"]
    lines.append(f"outline: {_relative(result.outline_path, result.project_root)}")
    lines.append(f"pdf: {_relative(result.pdf_path, result.project_root)}（{result.pages_total} 页）")
    lines.append(f"页角色：{_role_counts(result.refs)}")
    if result.frame_mismatch:
        lines.append(
            f"注意：outline 推出 {len(result.refs)} 帧，PDF 有 {result.pages_total} 页，按序号尽力对齐"
        )
    if result.dry_run:
        lines.extend(_dry_run_lines(result))
        return tuple(lines)

    if result.no_vision:
        lines.append("检查：确定性层（--no-vision，视觉层已跳过）")
    elif result.endpoint is not None:
        lines.append(
            f"检查：确定性层 + 视觉层（{result.endpoint.provider}/{result.endpoint.model}，"
            f"每页 1 次请求，{result.workers} 线程）"
        )
    else:
        lines.append("检查：确定性层（视觉层端点没解析出来，已跳过）")

    lines.append("")
    lines.append(f"结论：error {len(result.errors)} 条，warning {len(result.warnings)} 条")
    lines.append(
        "退出码：" + ("2（有 error 级发现）" if result.errors else "0（只有 warning 或没有发现）")
    )
    suppressions = [
        item for page in result.page_results if page.vision is not None
        for item in page.vision.suppressed
    ]
    lines.extend(_suppression_lines(suppressions))
    if result.failures:
        lines.append(f"视觉层失败 {len(result.failures)} 页：" + "；".join(result.failures[:5]))

    # 整份的发现（不属于任何一页的）
    whole = [item for item in result.deck_findings if item.page == 0]
    if whole:
        lines.append("")
        lines.append("整份的发现")
        for item in whole:
            lines.append(f"  - {item.code}（{item.severity}）{item.message}")
            lines.append(f"    证据：{item.evidence}")

    # 逐页（把挂在某一页上的整份发现也算进来）
    by_page: dict = {}
    for item in result.deck_findings:
        if item.page:
            by_page.setdefault(item.page, []).append(item)
    lines.append("")
    lines.append("逐页")
    for page_result in result.page_results:
        ref = page_result.ref
        label = f"（{ref.label}）" if ref.label else ""
        head = f"  第 {ref.page} 页 {ROLE_NAMES.get(ref.role, ref.role)}{label}"
        deck_items = by_page.get(ref.page, [])
        if (
            not page_result.findings
            and not deck_items
            and (page_result.vision is None or not page_result.vision.findings)
            and (page_result.vision is None or page_result.vision.status == "ok")
        ):
            lines.append(f"{head}：无发现")
            continue
        lines.append(head)
        for item in list(page_result.findings) + deck_items:
            lines.append(f"    - {item.code}（{item.severity}）{item.message}")
            lines.append(f"      证据：{item.evidence}")
        if page_result.vision is not None:
            if page_result.vision.status != "ok":
                lines.append(
                    f"    - 视觉层：{page_result.vision.status}"
                    + (f"（{page_result.vision.error}）" if page_result.vision.error else "")
                )
            for item in page_result.vision.findings:
                lines.append(f"    - 视觉层 {item.code}（{item.severity}）{item.message}")
                lines.append(f"      证据：{item.evidence}")
            if page_result.vision.suppressed:
                lines.append(
                    f"    - 视觉层丢掉了 {page_result.vision.suppressed_total} 条："
                    + _suppression_text(page_result.vision.suppressed)
                )

    # 汇总
    lines.append("")
    lines.append("按发现的\"该改哪里\"汇总")
    lines.extend(_hint_summary(result.findings))
    return tuple(lines)


def _suppression_text(suppressions: Sequence[Suppression]) -> str:
    """一条内联写法：``clipped-text 2（几何未证实）；vertical-imbalance 1（角色豁免）``。"""
    return "；".join(
        f"{item.code} {item.count}"
        f"（{SUPPRESSION_REASON_NAMES.get(item.reason, item.reason)}）"
        for item in suppressions
    )


def _suppression_lines(suppressions: Sequence[Suppression]) -> list:
    """整份的丢掉说明：按"两种丢法"分门别类，与 --json／账本同一个口径。"""
    merged = merge_suppressions(suppressions)
    if not merged:
        return []
    total = sum(item.count for item in merged)
    parts: list = []
    for reason in (SUPPRESSION_ROLE_EXEMPT, SUPPRESSION_GEOMETRY_UNCONFIRMED):
        items = [item for item in merged if item.reason == reason]
        if not items:
            continue
        codes = "、".join(f"{item.code} {item.count}" for item in items)
        parts.append(
            f"{SUPPRESSION_REASON_NAMES[reason]} {sum(item.count for item in items)} 条（{codes}）"
        )
    lines = [f"视觉层丢掉了 {total} 条：{'；'.join(parts)}"]
    for reason in (SUPPRESSION_ROLE_EXEMPT, SUPPRESSION_GEOMETRY_UNCONFIRMED):
        if any(item.reason == reason for item in merged):
            lines.append(f"  {SUPPRESSION_REASON_NAMES[reason]}：{SUPPRESSION_REASON_NOTES[reason]}")
    return lines


def _hint_summary(findings: Sequence[Finding]) -> list:
    if not findings:
        return ["  （没有发现）"]
    grouped: dict = {}
    for item in findings:
        key = (item.code, item.hint)
        grouped.setdefault(key, []).append(item)
    lines: list = []
    for (code, hint), items in sorted(
        grouped.items(), key=lambda pair: (-max(item.fatal for item in pair[1]), pair[0][0])
    ):
        pages = sorted({item.page for item in items if item.page})
        where = "、".join(f"第 {page} 页" for page in pages) or "整份"
        severity = "error" if any(item.fatal for item in items) else "warning"
        lines.append(f"  {code}（{severity}，{len(items)} 条；{where}）→ {hint}")
        for item in items[:2]:
            lines.append(f"      证据：{item.evidence}")
    return lines


def _dry_run_lines(result: InspectResult) -> list:
    lines: list = ["", "dry-run：一个字节都不写、不联网，也不读密钥。将要做的："]
    lines.append("  - 确定性层：逐页扫描（横线/居中/失衡/宋体/行距/编号残留/字体全表）")
    if result.no_vision:
        lines.append("  - 视觉层：已用 --no-vision 关闭")
    elif result.endpoint is not None:
        lines.append(
            f"  - 视觉层：对 {result.pages_total} 页各发 1 次请求"
            f"（{result.endpoint.provider}/{result.endpoint.model}，temperature "
            f"{result.endpoint.temperature if result.endpoint.temperature is not None else 0}，"
            f"{result.workers} 线程，渲染 {DEFAULT_RENDER_DPI} dpi）"
        )
    else:
        lines.append("  - 视觉层：端点配置读不出来，真跑时会报错")
    lines.append(f"  - 账本：{_relative(result.outline_path.parent / INSPECT_LOG_NAME, result.project_root)}")
    return lines


def _report_pages(result: InspectResult) -> list:
    by_page: dict = {}
    for item in result.deck_findings:
        if item.page:
            by_page.setdefault(item.page, []).append(item)
    lines: list = []
    for page_result in result.page_results:
        ref = page_result.ref
        label = f"（{ref.label}）" if ref.label else ""
        lines.append(f"### 第 {ref.page} 页 · {ROLE_NAMES.get(ref.role, ref.role)}{label}")
        items = list(page_result.findings) + by_page.get(ref.page, [])
        if page_result.vision is not None:
            items.extend(page_result.vision.findings)
        if not items and (page_result.vision is None or page_result.vision.status == "ok"):
            lines.append("")
            lines.append("无发现。")
        for item in items:
            lines.append("")
            lines.append(f"- **{item.code}**（{item.severity}）{item.message}")
            lines.append(f"  - 证据：{item.evidence}")
            if item.hint:
                lines.append(f"  - 该改哪里：{item.hint}")
        if page_result.vision is not None and page_result.vision.status != "ok":
            lines.append("")
            lines.append(f"- 视觉层：{page_result.vision.status} {page_result.vision.error}")
        if page_result.vision is not None and page_result.vision.suppressed:
            lines.append("")
            lines.append(
                f"- 视觉层丢掉了 {page_result.vision.suppressed_total} 条："
                f"{_suppression_text(page_result.vision.suppressed)}"
            )
        lines.append("")
    return lines


def markdown_report(result: InspectResult) -> str:
    """``--report FILE`` 的 markdown（与 stdout 同语种：中文）。"""
    lines: list = ["# primer-slides inspect · 版面质检", ""]
    lines.append(f"- outline：`{result.outline_path}`")
    lines.append(f"- pdf：`{result.pdf_path}`（{result.pages_total} 页）")
    lines.append(f"- 页角色：{_role_counts(result.refs)}")
    lines.append(f"- 结论：error {len(result.errors)} 条，warning {len(result.warnings)} 条")
    if result.endpoint is not None and not result.no_vision:
        lines.append(
            f"- 视觉层：{result.endpoint.provider}/{result.endpoint.model}，{result.workers} 线程"
        )
    lines.append("")
    suppressions = [
        item for page in result.page_results if page.vision is not None
        for item in page.vision.suppressed
    ]
    if suppressions:
        lines.append("## 视觉层丢掉的")
        lines.append("")
        merged = merge_suppressions(suppressions)
        lines.append(
            f"共 {sum(item.count for item in merged)} 条"
            "（与 stdout／--json／inspect.jsonl 同一口径）。"
        )
        lines.append("")
        for item in merged:
            lines.append(
                f"- `{item.code}` {item.count} 条 — "
                f"{SUPPRESSION_REASON_NAMES.get(item.reason, item.reason)}"
                f"：{SUPPRESSION_REASON_NOTES.get(item.reason, '')}"
            )
        lines.append("")
    whole = [item for item in result.deck_findings if item.page == 0]
    if whole:
        lines.append("## 整份的发现")
        lines.append("")
        for item in whole:
            lines.append(f"- **{item.code}**（{item.severity}，整份）{item.message}")
            lines.append(f"  - 证据：{item.evidence}")
            if item.hint:
                lines.append(f"  - 该改哪里：{item.hint}")
        lines.append("")
    lines.append("## 逐页")
    lines.append("")
    lines.extend(_report_pages(result))
    lines.append("## 按\"该改哪里\"汇总")
    lines.append("")
    lines.extend(_hint_summary(result.findings))
    return "\n".join(lines)


def result_json(result: InspectResult) -> Mapping[str, object]:
    """``--json`` 的英文键结构。"""
    return {
        "deck": result.deck,
        "outline": str(result.outline_path),
        "pdf": str(result.pdf_path),
        "pages_total": result.pages_total,
        "frames": len(result.refs),
        "frame_mismatch": result.frame_mismatch,
        "no_vision": result.no_vision,
        "workers": result.workers,
        "dry_run": result.dry_run,
        "endpoint": (
            {
                "role": result.endpoint.role,
                "provider": result.endpoint.provider,
                "model": result.endpoint.model,
                "base_url": base_url_host(result.endpoint.base_url),
            }
            if result.endpoint
            else None
        ),
        "summary": {"errors": len(result.errors), "warnings": len(result.warnings)},
        "suppressed_total": sum(
            page.vision.suppressed_total for page in result.page_results if page.vision is not None
        ),
        "failed_pages": list(result.failures),
        "findings": [item.as_dict() for item in result.findings],
        "pages": [
            {
                "page": page_result.ref.page,
                "role": page_result.ref.role,
                "label": page_result.ref.label,
                "metrics": dict(page_result.metrics),
                "findings": [item.as_dict() for item in page_result.findings],
                "vision": (
                    {
                        "status": page_result.vision.status,
                        "requests": page_result.vision.usage[0],
                        "suppressed": [item.as_dict() for item in page_result.vision.suppressed],
                        "suppressed_total": page_result.vision.suppressed_total,
                        "findings": [item.as_dict() for item in page_result.vision.findings],
                    }
                    if page_result.vision
                    else None
                ),
            }
            for page_result in result.page_results
        ],
    }


# ---------------------------------------------------------------- 命令行


INSPECT_SUMMARY = (
    "layout quality-assurance of a generated slides.pdf: a deterministic geometry/font layer "
    "(poppler command line + stdlib only, no network, no model) plus an optional vision layer "
    "that renders each page and asks a multimodal model what is visibly wrong. The deterministic "
    "layer owns the font-family verdicts (a vision model cannot tell 宋体 from 黑体); the vision "
    "layer adds what geometry cannot see (clipping, overlap, garbled glyphs). Findings carry the "
    "place to fix (the outline's theme, beamer's frame layout, prose.strip_lead_enumerator). "
    "Errors exit non-zero; warnings do not."
)

INSPECT_EPILOG = (
    "examples:\n"
    f"  {PROG} _primer/slides/review-seminar/outline.yaml\n"
    f"  {PROG} outline.yaml --no-vision          # deterministic layer only\n"
    f"  {PROG} outline.yaml --pages 1-8 --workers 4\n"
    "the pdf defaults to slides.pdf next to the outline; the page roles (cover/quote/table/"
    "points/backup) come from the outline's frames, and the role decides which checks "
    "apply (the cover and the quote page are never judged top-heavy). --dry-run writes nothing "
    "and never reads the key; findings and per-page metrics are appended to inspect.jsonl next "
    "to the outline (base_url host only, never the key); the vision endpoint comes from "
    "roles.vision in <project-root>/_primer/config.yaml"
)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """inspect 的全部参数（独立入口与 ``__main__.py`` 的子命令共用同一份）。"""
    parser.add_argument("outline", metavar="OUTLINE", help="path to the outline.yaml of the deck")
    parser.add_argument(
        "--pdf", metavar="FILE",
        help="the generated slides PDF; defaults to slides.pdf next to the outline",
    )
    parser.add_argument(
        "--project-root", default=".", metavar="DIR",
        help="project root: the config is read from <root>/_primer/config.yaml (default .)",
    )
    parser.add_argument("--pages", metavar="A-B", help="only these pages (A-B or a single A)")
    parser.add_argument(
        "--no-vision", action="store_true",
        help="run the deterministic layer only; no requests, no key needed",
    )
    parser.add_argument(
        "--workers", type=int, default=DEFAULT_WORKERS, metavar="N",
        help=f"vision request threads (default {DEFAULT_WORKERS})",
    )
    parser.add_argument("--json", action="store_true", help="print the result as JSON (English keys)")
    parser.add_argument("--report", metavar="FILE", help="also write a Chinese markdown report")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="print what would be done; write nothing and never read the key",
    )
    parser.add_argument(
        "--config", metavar="FILE",
        help="explicit endpoint configuration file, merged on top of the machine and project layers",
    )
    parser.add_argument("--max-tokens", type=int, metavar="N", help="vision output budget per request")
    parser.add_argument("--timeout", type=float, metavar="SECONDS", help="HTTP timeout per request")
    parser.add_argument(
        "--dpi", type=int, default=DEFAULT_RENDER_DPI, metavar="N",
        help=f"rasterisation dpi for the deterministic scan and the vision images (default {DEFAULT_RENDER_DPI})",
    )


def build_parser() -> argparse.ArgumentParser:
    """独立运行 ``python -m primer.slides.inspect`` 时的解析器（选项名与说明一律英文）。"""
    parser = argparse.ArgumentParser(
        prog=PROG,
        description=INSPECT_SUMMARY,
        epilog=INSPECT_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_arguments(parser)
    return parser


def command(args: argparse.Namespace) -> int:
    """跑一次 inspect 并打印报告，返回退出码。``main`` 与 ``__main__.py`` 共用这一段。"""
    try:
        result = run(
            Path(args.outline),
            Path(getattr(args, "project_root", ".") or "."),
            pdf_path=Path(args.pdf) if getattr(args, "pdf", None) else None,
            pages=getattr(args, "pages", None),
            no_vision=bool(getattr(args, "no_vision", False)),
            workers=int(getattr(args, "workers", DEFAULT_WORKERS) or DEFAULT_WORKERS),
            dry_run=bool(getattr(args, "dry_run", False)),
            report_path=Path(args.report) if getattr(args, "report", None) else None,
            config_path=Path(args.config) if getattr(args, "config", None) else None,
            max_tokens=getattr(args, "max_tokens", None),
            timeout=getattr(args, "timeout", None),
            dpi=int(getattr(args, "dpi", DEFAULT_RENDER_DPI) or DEFAULT_RENDER_DPI),
        )
    except (InspectError, SlidesError) as exc:
        print(f"slides inspect error: {exc}", file=sys.stderr)
        return 2
    if getattr(args, "json", False):
        print(json.dumps(result_json(result), ensure_ascii=False, indent=2, sort_keys=False))
    else:
        for line in report_lines(result):
            print(line)
    return 2 if result.errors else 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    """命令行入口。返回退出码：有 error 级发现 → 2，只有 warning → 0。"""
    if argv is None:
        argv = sys.argv[1:]
    argv = list(argv)
    if argv and argv[0] == "inspect":  # 容忍被注册为子命令时带上命令名
        argv = argv[1:]
    return command(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
