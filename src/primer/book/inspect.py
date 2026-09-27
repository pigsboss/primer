# -*- coding: utf-8 -*-
"""视觉校对：把已排好的 PDF 逐页渲染，交给多模态模型挑出"排版不对"的地方。

``check`` 走确定性的一路——读 ``.log``、比装配时的发现；本模块走视觉的一路：
选择有代表性的页面，用 ``pdftoppm`` 渲染成 PNG，按 OpenAI 兼容的 chat completions
线格式（``image_url`` + base64 data URL）发给多模态模型，把回信解析成机器可读的
发现。回路是 ``inspect`` → 改 markdown→TeX 的代码 → 重新编译 → 再 ``inspect``，
所以每条发现都带 ``converter_hint``：**最可能该改的转换器部件**，而不是泛泛的
评语。

页面选择（默认，有界）按优先级取四类，去重后合计不超过 ``--max-pages``：

1. 每篇／每章的起始页（页码取自编译产物 ``.toc``，不猜）；
2. 装配时被 ``table-layout`` 发现点名的表格所在页（用表格题注／表头在页面文本里定位）；
3. 横向页（``pdfinfo`` 报出的页面旋转角非零）；
4. 固定数量的正文页抽样（由 jobname 派生的种子决定，可复现）。

没有配置端点也照样有用：渲染、写出自包含的提示词包（``inspect.prompt.md`` 与
``inspect.pages.json``），并回报"未配置视觉端点、此包供外部模型使用"，退出码 0。
密钥只从环境变量读（变量名可配），绝不读任何凭据文件。

发图前的两道保险：``pages/`` 目录每次运行整目录重建（旧命名的残留图不会混进来），
渲染后再按字节哈希去重，保证同一张图不会进模型两次——否则模型会报出"重复页"
之类的假发现。``--batch-size`` 与 ``--max-tokens`` 是一对相反的旋钮：页数越多越省
往返，但思考型模型（推理链也吃输出预算）越容易把预算耗尽——实测 ``deepseek-flash``
两页一请求时会推理到 16k token 的上限、正文整个为空，一页一请求则稳定给出 JSON。
故内置默认是每请求 2 页、``max_tokens`` 取足够大的值；把实测出来的可靠取值记在
清单的 ``vision.batch_size`` / ``vision.max_tokens`` 里比让人记住一条魔法参数更
靠得住（取值优先级：命令行显式给出 > 清单里记下的 > 内置默认）。回信
``finish_reason`` 为 ``length`` 且正文为空（推理链把输出预算吃光）时，这一批页
**重试一次**，``max_tokens`` 翻倍但不超过 :data:`RETRY_MAX_TOKENS_CAP`；第一次尝试
仍单报一条 ``vision-truncated``，重试若再空则把这批页判为 ``vision-undetermined``
（error）。正文非空但被截断时维持原路由：``vision-truncated``（能解析则附发现）。

没有任何回信可用的页不能装作"干净"：每页要么有裁决、要么落一条 ``vision-undetermined``
（error，带页码与终止原因，如 ``empty reply after retry`` / ``timeout``），退出码据此非零——
一次没看全的校对不是通过。汇总时显式打印 ``judged X/Y page(s); Z undetermined``，
同样的计数进入 JSON 的 ``coverage`` 字段，让 ``inspect.findings.json`` 的读者能区分
"干净"与"压根没看过"。

每次请求的 token 用量都从回信的 ``usage`` 里取回来并累计：思考型模型的**输出**侧
（推理链也算 completion tokens）才是账单上的大头，所以 prompt 与 completion 分开
记，汇总时一并打印。清单没有配每千 token 的价格就不给费用估算——不猜价钱。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import random
import re
import shutil
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from ..paths import relative_to_root
from . import markdown as md
from . import pdf as pdf_tools
from .builder import BookBuilder
from .findings import SEVERITIES, Finding, summarize
from .manifest import BookManifest

INSPECT_DIRNAME = "inspect"
PAGES_DIRNAME = "pages"
IMAGE_PREFIX = "page"
DEFAULT_MAX_PAGES = 40
DEFAULT_RESOLUTION = 110
# 内置的每请求页数：这个数字是"请求数"与"回信被截断的风险"之间的折中——多页一请求省
# 往返，但图片越多、模型的推理链越长，回信越容易在 JSON 中途撞上 max_tokens。思考型
# 模型（如 deepseek-flash 这类 always-thinking 模型）尤其明显，故取小值。模型自己的
# 可靠取值记在清单的 ``vision.batch_size`` 里，优先级高于本默认。
DEFAULT_BATCH_SIZE = 2
# 内置的回信上限。思考型模型的推理链也吃这个预算：服务端默认值太小的时候，推理链会在
# 写出 JSON 之前就把预算耗尽，回信正文为空（content=""）或被截断（finish_reason=length），
# 看起来像"模型没报问题"。给足余量（实测两页一请求的推理约 4k token，取 16k 留一倍余量），
# 并允许按需调。清单的 ``vision.max_tokens`` 优先于本默认。
DEFAULT_MAX_TOKENS = 16384
# 空回信重试时把 ``max_tokens`` 翻倍，但绝不越过这个上限：再高只是把下一次的账单做大，
# 换不来更多信息（实测 deepseek-flash 的推理链在 16k 就已耗尽，翻倍到 32k 足够）。
RETRY_MAX_TOKENS_CAP = 32768
DEFAULT_KEY_ENV = "PRIMER_VISION_API_KEY"
HTTP_TIMEOUT = 180.0
# 表格页定位用的最短纯文本针（去掉空白与强调标记后的字符数）：太短的针会命中不相干页面。
MIN_NEEDLE = 6
# 抽样种子由 jobname 派生，保证同一本书每次选同一批页。
SEED_BASE = 20260927

# 模型给的严重度别名 → 本项目的三档。认不出的按 warning。
SEVERITY_ALIASES = {
    "critical": "error", "fatal": "error", "high": "error", "major": "error", "blocker": "error",
    "medium": "warning", "minor": "warning", "moderate": "warning",
    "low": "info", "note": "info", "hint": "info", "suggestion": "info",
}

RUBRIC = """\
# Visual quality-assurance of a typeset book PDF

You are reviewing rasterized pages of a Chinese book with CJK fonts and English citations
that `primer-book` converted from markdown to LaTeX (`ctexbook` class, xelatex) and
compiled to PDF. This is not a prose review: report concrete visible layout defects that a
programmer can fix in the markdown -> LaTeX converter, and only defects that the rendered
page alone can show.

## What counts as a defect

1. A table scaled or wrapped below legibility: cells colliding, words broken mid-word,
   single-character orphan lines, or a header drifting away from its rows.
2. A figure too small to read, blank, all-grey, or cropped at the frame.
3. A heading orphaned at the foot of a page, or stranded from its text by a page break.
4. A landscape page used where a portrait page would do, or a wide table left in portrait
   when it needed a landscape page.
5. Conspicuous whitespace: a large gap mid-page, a mostly empty page, content pushed onto
   a page of its own for no reason.
6. Anything else a reader would call wrong that only the rendered page can show.

## Verified elsewhere — do not report

Content crossing the text-block edge, chapter/figure/table numbering consistency, and
missing or garbled glyphs are already guaranteed by the deterministic `primer-book check
--deep` layer (the LaTeX log's `Missing character` line and the font-coverage check cover
the glyph class). Reason: that layer proves these at 100% recall and zero cost, so a
vision reply can add nothing — do not spend reasoning on them and do not report them.

## Converter elements `converter_hint` should name

- table layout plan (`tables.plan_table`: portrait fit / portrait wrap / portrait scale /
  landscape fit / landscape wrap, font ladder `-4` / `-5` / `-6`)
- table emitter (`tex.render_table`: xltabular for wrap, adjustbox for scale)
- figure placement (`tex.render_blocks`: includegraphics width=0.96\\linewidth,
  height=0.68\\textheight, caption/note handling)
- heading emitter (`tex.render_blocks`: \\part / \\chapter / \\section)
- page geometry (`geometry` margin, `pdflscape` landscape pages)
- float placement and page breaks (`tex.render_blocks`: float specifiers, \\clearpage)

Phrase the hint as an action for the programmer, for example: "wide table chose the
portrait-wrap tier but still overran; consider the landscape tier" or "figure scaled below
legibility although adjustbox was available".

## Output

Reply with JSON only, no prose around it:

{"findings": [{"page": <printed page number>, "code": "<kebab-case>",
  "severity": "error|warning|info", "what": "<the visible defect>",
  "where": "table|figure|listing|heading|page-geometry|glyph",
  "converter_hint": "<the markdown->TeX element most likely responsible, and what to change>"}]}

Use severity "error" only for defects that make the book wrong or unreadable. Return
{"findings": []} when the pages are clean. Suggested codes: table-illegible, figure-small,
figure-blank, figure-crop, heading-orphan, heading-stray, landscape-unnecessary,
landscape-missing, whitespace-gap, other.
"""


class InspectError(Exception):
    """inspect 的参数或前置条件不成立（消息英文）。"""


class VisionCallError(Exception):
    """多模态端点不可用或回信无法当 HTTP 响应解析。"""


class VisionReplyError(Exception):
    """模型回信不是可用的 JSON（含无法修复的残缺结构）。"""


# ---------------------------------------------------------------- 数据结构


@dataclass(frozen=True)
class PageChoice:
    """一个入选页面及其入选理由。"""

    page: int
    reasons: Tuple[str, ...]
    image: Optional[Path] = None
    size: int = 0

    def with_image(self, image: Path) -> "PageChoice":
        return PageChoice(self.page, self.reasons, image, image.stat().st_size if image.is_file() else 0)


@dataclass(frozen=True)
class VisualFinding:
    """一条视觉发现：看得见的缺陷 + 最可能该改的转换器部件。"""

    page: int
    code: str
    severity: str
    what: str
    where: str
    converter_hint: str

    def as_dict(self) -> Mapping[str, object]:
        return {
            "page": self.page,
            "code": self.code,
            "severity": self.severity,
            "what": self.what,
            "where": self.where,
            "converter_hint": self.converter_hint,
        }


@dataclass(frozen=True)
class HttpRequest:
    """一次 HTTP 调用；传输层可注入，测试不碰网络。"""

    url: str
    headers: Mapping[str, str]
    body: bytes
    timeout: float


Transport = Callable[[HttpRequest], bytes]


# ---------------------------------------------------------------- 页面选择


def parse_page_spec(spec: str) -> List[int]:
    """``1-5,79,200`` → 去重后的页码列表。"""
    pages: List[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start_text, _, end_text = part.partition("-")
            if not (start_text.strip().isdigit() and end_text.strip().isdigit()):
                raise InspectError(f"invalid page range: {part!r}")
            start, end = int(start_text), int(end_text)
            if start < 1 or end < start:
                raise InspectError(f"invalid page range: {part!r}")
            pages.extend(range(start, end + 1))
        elif part.isdigit() and int(part) >= 1:
            pages.append(int(part))
        else:
            raise InspectError(f"invalid page number: {part!r}")
    if not pages:
        raise InspectError(f"no pages in --pages {spec!r}")
    ordered: List[int] = []
    for page in pages:
        if page not in ordered:
            ordered.append(page)
    return ordered


def seed_for(jobname: str) -> int:
    """由 jobname 派生抽样种子：同一本书每次选同一批正文页。"""
    return (SEED_BASE + zlib.crc32(jobname.encode("utf-8"))) % (2**31)


def _split_groups(line: str, count: int) -> Optional[List[str]]:
    """按 LaTeX 花括号层级取前 ``count`` 个顶层花括号组。"""
    groups: List[str] = []
    index = 0
    total = len(line)
    for _ in range(count):
        while index < total and line[index] != "{":
            index += 1
        if index >= total:
            return None
        depth = 0
        start = index + 1
        cursor = index
        while cursor < total:
            if line[cursor] == "{":
                depth += 1
            elif line[cursor] == "}":
                depth -= 1
                if depth == 0:
                    break
            cursor += 1
        if cursor >= total:
            return None
        groups.append(line[start:cursor])
        index = cursor + 1
    return groups


def parse_toc_pages(text: str) -> List[Tuple[str, int, str]]:
    """从 ``.toc`` 取篇／章起始页，返回 ``(kind, page, title)``。"""
    out: List[Tuple[str, int, str]] = []
    for raw in text.splitlines():
        line = raw.strip().rstrip("%").strip()
        if not line.startswith("\\contentsline"):
            continue
        groups = _split_groups(line[len("\\contentsline"):], 4)
        if groups is None:
            continue
        kind, title, page = groups[0].strip(), groups[1], groups[2].strip()
        if kind not in ("part", "chapter") or not page.isdigit():
            continue
        out.append((kind, int(page), _plain_title(title)))
    return out


def _plain_title(markup: str) -> str:
    """去掉目录项里的 LaTeX 标记，留下可读标题。"""
    text = re.sub(r"\\numberline\s*\{[^{}]*\}", "", markup)
    text = re.sub(r"\\hspace\s*\{[^{}]*\}", " ", text)
    text = re.sub(r"\\[a-zA-Z]+\s*", " ", text)
    text = text.replace("{", " ").replace("}", " ")
    return re.sub(r"\s+", " ", text).strip()


def _body_start(toc_entries: Sequence[Tuple[str, int, str]], page_count: int) -> int:
    """正文从哪一页开始：首篇起始页；没有篇时取首个起始章；都没有则第 1 页。"""
    parts = [page for kind, page, _ in toc_entries if kind == "part"]
    if parts:
        return min(parts)
    chapters = [page for kind, page, _ in toc_entries if kind == "chapter"]
    if chapters:
        return min(chapters)
    return 1 if page_count else 1


def _squeeze(text: str) -> str:
    """去掉空白与强调标记：页面文本与 markdown 里的针按同一口径比对。"""
    return re.sub(r"[\s*`]+", "", text)


def table_needles(volume_blocks: Mapping[str, Sequence[object]]) -> Dict[str, Tuple[str, ...]]:
    """``volume#block`` → 用于在页面文本中定位该表的字符串针。

    优先用题注（题注在 PDF 里通常整行可抽），无题注时用表头前几格拼成的针。
    """
    out: Dict[str, Tuple[str, ...]] = {}
    for volume_id, blocks in volume_blocks.items():
        for index, block in enumerate(blocks, 1):
            if not isinstance(block, md.Table):
                continue
            needles: List[str] = []
            if block.caption:
                needles.append(_squeeze(block.caption))
            if block.rows:
                needles.append("".join(_squeeze(cell) for cell in block.rows[0][:4]))
            needles = [needle for needle in needles if len(needle) >= MIN_NEEDLE]
            if needles:
                out[f"{volume_id}#{index}"] = tuple(needles)
    return out


def table_locations(payload: Mapping[str, object]) -> List[str]:
    """从发现清单里取被 ``table-layout`` 点名的表格位置（去重、保序）。"""
    out: List[str] = []
    for item in payload.get("findings") or []:
        if not isinstance(item, dict) or item.get("code") != "table-layout":
            continue
        location = str(item.get("location") or "")
        if "#" in location and location not in out:
            out.append(location)
    return out


def locate_pages(
    needles: Sequence[str], page_texts: Sequence[str], page_count: int, from_page: int = 1
) -> Tuple[List[int], List[int]]:
    """在页面文本里找含针的页，返回 ``(命中页, 每段连续命中页的首个)``。

    用最强的针（题注优先，其次表头）：命中即止。连续命中要折成"段首"——跨页的
    表格会在每一页重复表头，段首才是表格起始页；同一本书里题注相同的多张表则按
    段首依次分配。前端页（目录、表格目录）里的题注不算数，故从 ``from_page`` 起找。
    """
    squeezed = [_squeeze(text) for text in page_texts]
    last = min(len(squeezed), page_count)
    for needle in needles:
        if not needle:
            continue
        hits = [page for page in range(from_page, last + 1) if needle in squeezed[page - 1]]
        if hits:
            runs: List[int] = []
            previous: Optional[int] = None
            for page in hits:
                if previous is None or page != previous + 1:
                    runs.append(page)
                previous = page
            return hits, runs
    return [], []


def select_pages(
    *,
    page_count: int,
    rotations: Mapping[int, int],
    page_texts: Sequence[str],
    toc_entries: Sequence[Tuple[str, int, str]],
    table_locations_: Sequence[str],
    needles: Mapping[str, Tuple[str, ...]],
    max_pages: int,
    explicit: Optional[Sequence[int]] = None,
    seed: int = 0,
) -> Tuple[List[PageChoice], List[Finding]]:
    """按优先级选出待渲染的页面与每页的入选理由。"""
    findings: List[Finding] = []
    reasons: Dict[int, List[str]] = {}
    order: List[int] = []

    def add(page: int, reason: str) -> None:
        if not 1 <= page <= page_count:
            return
        if page not in reasons:
            reasons[page] = []
            order.append(page)
        if reason not in reasons[page]:
            reasons[page].append(reason)

    if explicit is not None:
        for page in explicit:
            if not 1 <= page <= page_count:
                findings.append(
                    Finding(
                        code="page-out-of-range",
                        severity="warning",
                        message=f"page {page} is outside the {page_count}-page document",
                        location="--pages",
                    )
                )
                continue
            add(page, "explicitly selected with --pages")
    else:
        for kind, page, title in toc_entries:
            add(page, f"{kind} opening: {title or '(untitled)'}")
        body_start = _body_start(toc_entries, page_count)
        taken: set = set()
        for location in table_locations_:
            hits, runs = locate_pages(needles.get(location, ()), page_texts, page_count, body_start)
            chosen = next((page for page in runs if page not in taken), None)
            if chosen is None:
                chosen = next((page for page in hits if page not in taken), None)
            if chosen is None and hits:
                chosen = hits[0]
            if chosen is None:
                findings.append(
                    Finding(
                        code="table-page-unmapped",
                        severity="info",
                        message="could not locate this table in the PDF text, so its page is not checked",
                        location=location,
                    )
                )
            else:
                taken.add(chosen)
                add(chosen, f"table-layout finding at {location}")
        for page in sorted(rotations):
            if rotations[page] % 360 != 0:
                add(page, f"landscape page (rotation {rotations[page]} deg)")
        candidates = [page for page in range(body_start, page_count + 1) if page not in reasons]
        room = max_pages - len(order)
        if room > 0 and candidates:
            picked = sorted(random.Random(seed).sample(candidates, min(room, len(candidates))))
            for page in picked:
                add(page, f"deterministic body-page sample (seed {seed})")

    if len(order) > max_pages:
        dropped = order[max_pages:]
        order = order[:max_pages]
        findings.append(
            Finding(
                code="pages-truncated",
                severity="info",
                message=(
                    f"{len(dropped)} page(s) beyond --max-pages {max_pages} were dropped: "
                    + ", ".join(str(page) for page in dropped[:20])
                ),
            )
        )

    choices = [PageChoice(page, tuple(reasons[page])) for page in sorted(order)]
    return choices, findings


# ---------------------------------------------------------------- 提示词与端点


def render_page_list(entries: Sequence[Tuple[int, str]], image_names: Sequence[str]) -> str:
    """提示词里的页面表：图像序号、页码、入选理由。"""
    lines = ["| image | page | why this page |", "|---|---|---|"]
    for index, ((page, reason), name) in enumerate(zip(entries, image_names), 1):
        lines.append(f"| {index} ({name}) | {page} | {reason} |")
    return "\n".join(lines)


def render_prompt(header: str, page_table: str) -> str:
    """一次请求（或提示词包）的完整文本：话头 + 评分规则 + 页面表。"""
    return f"{header}\n\n{RUBRIC}\n## Pages in this request\n\n{page_table}\n"


def chat_url(base_url: str) -> str:
    """OpenAI 兼容的 chat completions 地址。"""
    return base_url.rstrip("/") + "/chat/completions"


def vision_payload(
    model: str,
    prompt: str,
    images: Sequence[Path],
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> bytes:
    """构造 ``image_url`` + base64 data URL 的请求体。

    ``max_tokens`` 必须显式给出：思考型模型的推理链与服务端默认值共享这个预算，
    默认值偏小时 JSON 发现会在中途被截断。
    """
    content: List[Mapping[str, object]] = [{"type": "text", "text": prompt}]
    for image in images:
        encoded = base64.b64encode(image.read_bytes()).decode("ascii")
        content.append(
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}}
        )
    body = {
        "model": model,
        "temperature": 0,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": content}],
    }
    return json.dumps(body).encode("utf-8")


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
        raise VisionCallError(f"HTTP {error.code} from {request.url}: {detail}") from error
    except (urllib.error.URLError, OSError, TimeoutError) as error:
        raise VisionCallError(f"cannot reach {request.url}: {error}") from error


def extract_message_content(raw: bytes) -> str:
    """从 chat completions 回信里取出 ``choices[0].message.content``。"""
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except (json.JSONDecodeError, ValueError) as error:
        raise VisionCallError(f"endpoint returned non-JSON: {raw[:200]!r}") from error
    if not isinstance(payload, dict):
        raise VisionCallError("endpoint returned a JSON value that is not an object")
    if "choices" not in payload and payload.get("error"):
        raise VisionCallError(f"endpoint returned an error: {str(payload['error'])[:300]}")
    choices = payload.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        raise VisionCallError(f"endpoint returned no choices: {json.dumps(payload)[:200]}")
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, list):
        content = "".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        )
    if not isinstance(content, str) or not content.strip():
        # 空内容不是"没发现问题"：思考型模型把预算花在推理链上时，正文可能整个为空。
        # 把 finish_reason 与用量带回，便于判断是该调 max_tokens 还是换批大小。
        finish = str(choices[0].get("finish_reason") or "unknown")
        usage = payload.get("usage")
        raise VisionCallError(
            f"endpoint returned an empty message (finish_reason={finish}, usage={usage})"
        )
    return content


def _reply_is_empty_message(raw: bytes) -> bool:
    """回信结构完整但 ``choices[0].message.content`` 为空——推理链吃光预算的形态。

    与 :func:`extract_message_content` 的区别：非 JSON、缺 choices、端点错误载荷都返回
    ``False``（那类走 ``vision-failed``），只有"有 choices、有 message、正文却空空"
    才返回 ``True``。空正文绝不能当成"没发现问题"。
    """
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
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, list):
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return not (isinstance(content, str) and content.strip())


def _terminal_reason(error: Exception) -> str:
    """把一次失败的调用归成一个短原因，写进 ``vision-undetermined`` 的台账。"""
    text = str(error).lower()
    if "timeout" in text or "timed out" in text:
        return "timeout"
    return "endpoint error"


def extract_finish_reason(raw: bytes) -> str:
    """回信的 ``choices[0].finish_reason``；读不出时返回空串。

    ``"length"`` 表示模型还没写完就撞上了 ``max_tokens``（思考型模型的推理链很常见），
    此时 JSON 多半是残缺的，要单独说明而不是笼统地报"回信无法解析"。
    """
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except (json.JSONDecodeError, ValueError):
        return ""
    if not isinstance(payload, dict):
        return ""
    choices = payload.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        return ""
    return str(choices[0].get("finish_reason") or "")


def extract_usage(raw: bytes) -> Mapping[str, int]:
    """回信 ``usage`` 里的 token 计数，缺字段按 0。

    思考型模型的推理链也计入 ``completion_tokens``，所以输出侧才是账单的大头，两个
    方向分开记。读不出时一律返回 0：一次记账失败不该影响校对本身。
    """
    empty = {"prompt_tokens": 0, "completion_tokens": 0}
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except (json.JSONDecodeError, ValueError):
        return empty
    if not isinstance(payload, dict):
        return empty
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return empty
    return {key: _token_count(usage.get(key)) for key in empty}


def _token_count(value: object) -> int:
    """非负整数才算 token 数；其余（缺失、布尔、小数、字符串）一律按 0。"""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def _json_candidates(text: str) -> List[str]:
    """模型回信里可能的 JSON 片段：整段、围栏内、首个平衡结构、逐行对象。"""
    candidates: List[str] = []
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


def _payload_items(payload: object) -> List[object]:
    """把回信 JSON 归一成"发现条目列表"；结构不可识别时抛 :class:`VisionReplyError`。"""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("findings", "issues", "problems", "result"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
        if "page" in payload or "code" in payload or "what" in payload:
            return [payload]
        if isinstance(payload.get("data"), list):
            return payload["data"]
    raise VisionReplyError(f"reply JSON carries no findings list: {json.dumps(payload)[:200]}")


def _severity(value: object) -> str:
    text = str(value or "").strip().lower()
    text = SEVERITY_ALIASES.get(text, text)
    return text if text in SEVERITIES else "warning"


def _code(value: object) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")
    return text or "vision-issue"


def _clean(value: object, limit: int = 600) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _page_number(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    matched = re.search(r"\d+", str(value or ""))
    return int(matched.group(0)) if matched else 0


def parse_vision_reply(text: str) -> List[VisualFinding]:
    """把模型回信解析成发现；围栏、前后闲话、残缺结构都尽量修复。"""
    payload: object = None
    for candidate in _json_candidates(text):
        try:
            payload = json.loads(candidate)
            break
        except (json.JSONDecodeError, ValueError):
            continue
    if payload is None:
        raise VisionReplyError(f"reply is not parseable JSON: {_clean(text, 300)!r}")
    findings: List[VisualFinding] = []
    for item in _payload_items(payload):
        if not isinstance(item, dict):
            continue
        findings.append(
            VisualFinding(
                page=_page_number(item.get("page")),
                code=_code(item.get("code")),
                severity=_severity(item.get("severity")),
                what=_clean(item.get("what") or item.get("message") or item.get("problem")) or "(no description)",
                where=_clean(item.get("where") or item.get("element"), 120) or "unknown",
                converter_hint=_clean(item.get("converter_hint") or item.get("hint") or item.get("suggestion")),
            )
        )
    return findings


# ---------------------------------------------------------------- 编排


def run_inspect(
    manifest: BookManifest,
    *,
    out_dir: Optional[Path] = None,
    project_root: Optional[Path] = None,
    pages_spec: Optional[str] = None,
    max_pages: int = DEFAULT_MAX_PAGES,
    resolution: int = DEFAULT_RESOLUTION,
    base_url: Optional[str] = None,
    model: Optional[str] = None,
    key_env: Optional[str] = None,
    batch_size: Optional[int] = None,
    max_tokens: Optional[int] = None,
    strict: bool = False,
    json_path: Optional[Path] = None,
    transport: Optional[Transport] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> int:
    """运行视觉校对，写出报告，返回退出码。

    ``batch_size``、``max_tokens`` 与三个端点字段一样，取值优先级是"命令行显式给出 >
    清单里记下的（``vision:`` 块）> 内置默认"，故三者默认都是 ``None``（= 没说）。
    """
    if max_pages < 1:
        raise InspectError("--max-pages must be at least 1")
    if resolution < 36:
        raise InspectError("--resolution must be at least 36 dpi")
    vision = manifest.vision
    batch_size, batch_source = _effective_setting(
        batch_size, vision.batch_size if vision else None, DEFAULT_BATCH_SIZE
    )
    max_tokens, max_tokens_source = _effective_setting(
        max_tokens, vision.max_tokens if vision else None, DEFAULT_MAX_TOKENS
    )
    if batch_size < 1:
        raise InspectError("--batch-size must be at least 1")
    if max_tokens < 1:
        raise InspectError("--max-tokens must be at least 1")
    explicit = parse_page_spec(pages_spec) if pages_spec else None

    builder = BookBuilder(manifest, out_dir=out_dir, project_root=project_root)
    # 装配一遍只为拿到各篇正文（表格页要靠题注／表头定位）；装配期的发现归 check 管。
    builder.inspect()
    reports = builder.out_dir / INSPECT_DIRNAME
    pages_dir = reports / PAGES_DIRNAME
    reports.mkdir(parents=True, exist_ok=True)

    controller = _Controller(
        manifest=manifest,
        builder=builder,
        reports=reports,
        pages_dir=pages_dir,
        resolution=resolution,
        max_pages=max_pages,
        batch_size=batch_size,
        batch_source=batch_source,
        max_tokens=max_tokens,
        max_tokens_source=max_tokens_source,
        strict=strict,
        explicit=explicit,
        transport=transport or urllib_transport,
        environ=dict(environ if environ is not None else os.environ),
        base_url=(base_url if base_url is not None else (manifest.vision.base_url if manifest.vision else "")),
        model=(model if model is not None else (manifest.vision.model if manifest.vision else "")),
        key_env=(
            key_env
            if key_env is not None
            else (manifest.vision.key_env if manifest.vision else DEFAULT_KEY_ENV)
        ),
    )
    return controller.run(json_path)


def _effective_setting(
    override: Optional[int], recorded: Optional[int], default: int
) -> Tuple[int, str]:
    """一个旋钮的取值与来源：命令行优先，其次清单，最后内置默认。"""
    if override is not None:
        return override, "cli"
    if recorded is not None:
        return recorded, "manifest"
    return default, "default"


class _Controller:
    """把一次 inspect 的状态收在一处，避免在自由函数之间传来传去。"""

    def __init__(
        self,
        *,
        manifest: BookManifest,
        builder: BookBuilder,
        reports: Path,
        pages_dir: Path,
        resolution: int,
        max_pages: int,
        batch_size: int,
        batch_source: str,
        max_tokens: int,
        max_tokens_source: str,
        strict: bool,
        explicit: Optional[Sequence[int]],
        transport: Transport,
        environ: Mapping[str, str],
        base_url: str,
        model: str,
        key_env: str,
    ) -> None:
        self.manifest = manifest
        self.builder = builder
        self.reports = reports
        self.pages_dir = pages_dir
        self.resolution = resolution
        self.max_pages = max_pages
        self.batch_size = batch_size
        self.batch_source = batch_source
        self.max_tokens = max_tokens
        self.max_tokens_source = max_tokens_source
        self.strict = strict
        self.explicit = explicit
        self.transport = transport
        self.environ = environ
        self.base_url = base_url
        self.model = model
        self.key_env = key_env or DEFAULT_KEY_ENV
        self.findings: List[object] = []
        self.choices: List[PageChoice] = []
        self.page_count = 0
        self.images_sent = 0
        # 覆盖率账目：发出去的每一页要么有裁决（judged），要么落一条 vision-undetermined。
        self.pages_sent = 0
        self.judged: set = set()
        self.undetermined: Dict[int, str] = {}
        # 用量账目：思考型模型的输出侧才是大头，prompt 与 completion 分开记。
        self.requests = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0

    # ------------------------------------------------------------ 主流程

    def run(self, json_path: Optional[Path]) -> int:
        pdf = self.builder.plan.pdf
        if not pdf.is_file():
            self.findings.append(
                Finding(
                    code="missing-pdf",
                    severity="error",
                    message="no built PDF to inspect; run build first",
                    location=relative_to_root(pdf, self.builder.project_root),
                )
            )
            return self._finish(json_path)

        try:
            self.page_count = pdf_tools.page_count(pdf)
            rotations = pdf_tools.page_rotations(pdf, self.page_count)
            texts = pdf_tools.page_texts(pdf)
        except pdf_tools.PdfToolError as error:
            self.findings.append(
                Finding(code="renderer-missing", severity="error", message=str(error))
            )
            return self._finish(json_path)

        print(f"[inspect] pdf: {relative_to_root(pdf, self.builder.project_root)} ({self.page_count} pages)")
        self.choices = self._select(rotations, texts)
        self._rasterize(pdf)
        self._deduplicate_images()
        images = [choice.image for choice in self.choices if choice.image is not None]
        self.images_sent = len(images)
        total_bytes = pdf_tools.byte_total(images)
        print(
            f"[inspect] images: {self.images_sent} sent of {len(self.choices)} selected page(s), "
            f"{total_bytes} bytes under {relative_to_root(self.pages_dir, self.builder.project_root)}"
        )
        self._call_vision()
        return self._finish(json_path, total_bytes)

    def _select(self, rotations: Mapping[int, int], texts: Sequence[str]) -> List[PageChoice]:
        toc = self.builder.out_dir / f"{self.manifest.output.jobname}.toc"
        entries = parse_toc_pages(toc.read_text(errors="ignore")) if toc.is_file() else []
        if not toc.is_file():
            self.findings.append(
                Finding(
                    code="missing-toc",
                    severity="info",
                    message="no .toc file found; part/chapter opening pages could not be selected",
                )
            )
        locations, origin = self._table_locations()
        blocks = {volume.spec.id: md.parse_blocks(volume.text.splitlines()) for volume in self.builder.volumes}
        choices, findings = select_pages(
            page_count=self.page_count,
            rotations=rotations,
            page_texts=texts,
            toc_entries=entries,
            table_locations_=locations,
            needles=table_needles(blocks),
            max_pages=self.max_pages,
            explicit=self.explicit,
            seed=seed_for(self.manifest.output.jobname),
        )
        for finding in findings:
            self.findings.append(finding)
        if locations:
            print(f"[inspect] table-layout findings from {origin}: {len(locations)}")
        self._print_selection(choices)
        return choices

    def _table_locations(self) -> Tuple[List[str], str]:
        for name in (f"{self.manifest.output.jobname}.findings.json", f"{self.manifest.output.jobname}.check.json"):
            path = self.builder.out_dir / name
            if not path.is_file():
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, ValueError, OSError) as error:
                self.findings.append(
                    Finding(
                        code="unreadable-findings",
                        severity="info",
                        message=f"could not read the build findings ({error}); table pages not selected",
                        location=relative_to_root(path, self.builder.project_root),
                    )
                )
                continue
            return table_locations(payload), relative_to_root(path, self.builder.project_root)
        return [], "(none)"

    def _print_selection(self, choices: Sequence[PageChoice]) -> None:
        print(f"[inspect] page selection: {len(choices)} page(s) at {self.resolution} dpi")
        if not choices:
            print("[inspect]   (none)")
            return
        print(f"[inspect]   {'page':>5}  why")
        for choice in choices:
            print(f"[inspect]   {choice.page:>5}  {'; '.join(choice.reasons)}")

    def _rasterize(self, pdf: Path) -> None:
        # 整目录重建：只删 ``page-*.png`` 会留下早期命名（如 ``page-0003 2.png``）
        # 或其他来源的旧图，脏目录会混进发给模型的图里，制造"重复页"之类的假发现。
        if self.pages_dir.exists():
            shutil.rmtree(self.pages_dir)
        self.pages_dir.mkdir(parents=True, exist_ok=True)
        updated: List[PageChoice] = []
        for choice in self.choices:
            try:
                image = pdf_tools.rasterize_page(
                    pdf, choice.page, self.pages_dir, self.resolution, IMAGE_PREFIX
                )
            except pdf_tools.PdfToolError as error:
                self.findings.append(
                    Finding(
                        code="rasterize-failed",
                        severity="error",
                        message=str(error),
                        location=f"page {choice.page}",
                    )
                )
                updated.append(choice)
                continue
            updated.append(choice.with_image(image))
        self.choices = updated

    def _deduplicate_images(self) -> None:
        """按内容哈希去重：内容相同的两页只送一张，绝不让同一张图进模型两次。

        同一页在干净目录里只会渲染一次；两道保险——一是渲染前整目录重建，二是这里
        按字节哈希去重——保证发给模型的图不会重复（重复会让模型报出"重复页"假发现）。
        """
        seen: Dict[str, int] = {}
        kept: List[PageChoice] = []
        for choice in self.choices:
            if choice.image is None:
                kept.append(choice)
                continue
            digest = hashlib.sha256(choice.image.read_bytes()).hexdigest()
            if digest in seen:
                self.findings.append(
                    Finding(
                        code="duplicate-page-images",
                        severity="info",
                        message=(
                            f"page {choice.page} rendered byte-identical to page {seen[digest]}; "
                            "its image was dropped so the model is not shown the same page twice"
                        ),
                        location=f"page {choice.page}",
                    )
                )
                continue
            seen[digest] = choice.page
            kept.append(choice)
        self.choices = kept

    # ------------------------------------------------------------ 端点

    def _call_vision(self) -> None:
        key = self.environ.get(self.key_env, "")
        if not self.base_url or not self.model:
            self.findings.append(
                Finding(
                    code="vision-not-configured",
                    severity="info",
                    message=(
                        "no vision endpoint configured; wrote the prompt bundle for an external model "
                        "(set vision.base_url/model in the manifest, or use --vision-base-url/--vision-model)"
                    ),
                )
            )
            print(
                "[inspect] no vision endpoint configured: the images and the prompt bundle are "
                "for an external model; nothing was sent."
            )
            return
        if not key:
            self.findings.append(
                Finding(
                    code="vision-not-configured",
                    severity="info",
                    message=f"no API key in the environment variable {self.key_env}; nothing was sent",
                )
            )
            print(f"[inspect] no API key in ${self.key_env}: nothing was sent.")
            return

        images = [choice for choice in self.choices if choice.image is not None]
        payload_bytes = pdf_tools.byte_total([choice.image for choice in images])
        print(
            f"[inspect] calling {self.model} at {chat_url(self.base_url)}: "
            f"{len(images)} image(s), {payload_bytes} image bytes, "
            f"batch size {self.batch_size} ({self.batch_source}), "
            f"max_tokens {self.max_tokens} ({self.max_tokens_source})"
        )
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {key}"}
        for start in range(0, len(images), self.batch_size):
            batch = images[start : start + self.batch_size]
            request_text = render_prompt(
                self._batch_header(batch),
                render_page_list(
                    [(choice.page, "; ".join(choice.reasons)) for choice in batch],
                    [
                        os.path.relpath(choice.image, self.reports) if choice.image else ""
                        for choice in batch
                    ],
                ),
            )
            self._inspect_batch(headers, batch, request_text)
        print(
            f"[inspect] judged {len(self.judged)}/{self.pages_sent} page(s); "
            f"{len(self.undetermined)} undetermined"
        )

    def _inspect_batch(
        self, headers: Mapping[str, str], batch: Sequence[PageChoice], request_text: str
    ) -> None:
        """问模型一批页，并把"这批页有没有拿到裁决"记进覆盖率账目。

        重试只有一次：空正文 + ``finish_reason=length`` 是推理链吃光输出预算（不是"没
        发现问题"），把 ``max_tokens`` 翻倍（不超过 :data:`RETRY_MAX_TOKENS_CAP`）再问
        一次；再空就判 ``vision-undetermined``，不烧第三次请求。
        """
        pages = [choice.page for choice in batch]
        label = f"batch pages {pages[0]}-{pages[-1]}"
        self.pages_sent += len(pages)
        attempt_max = self.max_tokens
        retried = False
        while True:
            request = HttpRequest(
                url=chat_url(self.base_url),
                headers=headers,
                body=vision_payload(
                    self.model,
                    request_text,
                    [choice.image for choice in batch if choice.image],
                    attempt_max,
                ),
                timeout=HTTP_TIMEOUT,
            )
            try:
                raw = self.transport(request)
            except VisionCallError as error:
                self.findings.append(
                    Finding(code="vision-failed", severity="error", message=str(error), location="vision endpoint")
                )
                reason = _terminal_reason(error)
                self._mark_undetermined(pages, reason)
                print(f"[inspect] {label}: call failed ({reason}); {len(pages)} page(s) undetermined")
                return
            # 用量先记账、再解析正文：正文为空（推理链吃光预算）时回信里恰恰有最该看的
            # 数字，不能因为解析失败就把这笔账丢掉。
            usage = extract_usage(raw)
            self.requests += 1
            self.prompt_tokens += usage["prompt_tokens"]
            self.completion_tokens += usage["completion_tokens"]
            finish = extract_finish_reason(raw)
            truncated = finish == "length"

            if _reply_is_empty_message(raw):
                if not truncated:
                    # 结构完整却空正文、又不是被截断：维持原有的 vision-failed 路由。
                    self.findings.append(
                        Finding(
                            code="vision-failed",
                            severity="error",
                            message=(
                                f"endpoint returned an empty message (finish_reason={finish or 'unknown'}, "
                                f"usage={dict(usage)})"
                            ),
                            location="vision endpoint",
                        )
                    )
                    self._mark_undetermined(pages, "empty reply")
                    print(f"[inspect] {label}: empty reply; {len(pages)} page(s) undetermined")
                    return
                self.findings.append(
                    Finding(
                        code="vision-truncated",
                        severity="warning",
                        message=(
                            "the model reply was empty at max_tokens (finish_reason=length); the "
                            f"reasoning chain consumed all {attempt_max} output tokens and produced no verdict"
                        ),
                        location=label,
                    )
                )
                retry_max = min(attempt_max * 2, RETRY_MAX_TOKENS_CAP)
                if not retried and retry_max > attempt_max:
                    retried = True
                    print(
                        f"[inspect] {label}: empty reply (finish_reason=length); retry at "
                        f"max_tokens {retry_max} (was {attempt_max})"
                    )
                    attempt_max = retry_max
                    continue
                self._mark_undetermined(pages, "empty reply after retry")
                print(
                    f"[inspect] {label}: still empty after retry; {len(pages)} page(s) undetermined"
                )
                return

            try:
                content = extract_message_content(raw)
                parsed = parse_vision_reply(content)
            except VisionCallError as error:
                self.findings.append(
                    Finding(code="vision-failed", severity="error", message=str(error), location="vision endpoint")
                )
                self._mark_undetermined(pages, _terminal_reason(error))
                print(f"[inspect] {label}: call failed; {len(pages)} page(s) undetermined")
                return
            except VisionReplyError as error:
                if truncated:
                    # 回信撞上 max_tokens：JSON 多半在中间断了，单列一条说明并给行动建议，
                    # 比笼统的"无法解析"更能指向解决办法。
                    self.findings.append(
                        Finding(
                            code="vision-truncated",
                            severity="warning",
                            message=(
                                "the model reply was cut off at max_tokens (finish_reason=length); "
                                f"raise --max-tokens (now {self.max_tokens}) or lower --batch-size "
                                f"(now {self.batch_size})"
                            ),
                            location=label,
                        )
                    )
                else:
                    self.findings.append(
                        Finding(
                            code="vision-unparsed",
                            severity="warning",
                            message=(
                                f"the model reply could not be parsed into findings "
                                f"(finish_reason={finish or 'unknown'}): {error}"
                            ),
                            location="vision endpoint",
                        )
                    )
                return
            if truncated:
                self.findings.append(
                    Finding(
                        code="vision-truncated",
                        severity="warning",
                        message=(
                            "the model reply hit max_tokens (finish_reason=length); the parsed "
                            "findings may be incomplete — raise --max-tokens or lower --batch-size"
                        ),
                        location=label,
                    )
                )
            self.judged.update(pages)
            self.findings.extend(parsed)
            print(
                f"[inspect] {label}: {len(parsed)} finding(s), "
                f"tokens prompt {usage['prompt_tokens']} + completion {usage['completion_tokens']}"
            )
            return

    def _mark_undetermined(self, pages: Sequence[int], reason: str) -> None:
        """给没能拿到裁决的页各落一条 error，页码与终止原因都在里面。"""
        for page in pages:
            self.undetermined[page] = reason
            self.findings.append(
                Finding(
                    code="vision-undetermined",
                    severity="error",
                    message=(
                        f"the vision pass reached no verdict for this page ({reason}); "
                        "its layout is unverified"
                    ),
                    location=f"page {page}",
                )
            )

    def _batch_header(self, batch: Sequence[PageChoice]) -> str:
        title = self.manifest.book.title
        return (
            f"Inspecting *{title}* — pages {batch[0].page} to {batch[-1].page} of "
            f"{self.page_count}, rasterized at {self.resolution} dpi. Image 1 is the first page in "
            "the table below; report the printed page number, not the image index."
        )

    # ------------------------------------------------------------ 报告

    def _finish(self, json_path: Optional[Path], total_bytes: int = 0) -> int:
        payload = self._payload(total_bytes)
        destination = json_path or (self.reports / "inspect.findings.json")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        (self.reports / "inspect.pages.json").write_text(
            json.dumps(self._pages_payload(total_bytes), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (self.reports / "inspect.prompt.md").write_text(self._prompt_bundle(), encoding="utf-8")
        (self.reports / "inspect.md").write_text(self._markdown_summary(total_bytes), encoding="utf-8")

        self._print_report()
        self._print_usage()
        print(f"[inspect] report: {relative_to_root(destination, self.builder.project_root)}")
        print(f"[inspect] bundle: {relative_to_root(self.reports / 'inspect.prompt.md', self.builder.project_root)}")
        return 1 if payload["failed"] else 0

    def _payload(self, total_bytes: int) -> Mapping[str, object]:
        return {
            "jobname": self.manifest.output.jobname,
            "pdf": relative_to_root(self.builder.plan.pdf, self.builder.project_root),
            "vision": {
                "configured": bool(self.base_url and self.model and self.environ.get(self.key_env)),
                "base_url": self.base_url or None,
                "model": self.model or None,
                "key_env": self.key_env,
                "max_tokens": self.max_tokens,
                "batch_size": self.batch_size,
            },
            "selection": {
                "max_pages": self.max_pages,
                "resolution": self.resolution,
                "seed": seed_for(self.manifest.output.jobname),
                "selected": [self._choice_dict(choice) for choice in self.choices],
            },
            "images": {
                "directory": relative_to_root(self.pages_dir, self.builder.project_root),
                "selected_pages": len(self.choices),
                "sent": self.images_sent,
                "count": sum(1 for choice in self.choices if choice.image is not None),
                "bytes": total_bytes,
            },
            "usage": {
                "requests": self.requests,
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "total_tokens": self.prompt_tokens + self.completion_tokens,
            },
            "coverage": {
                "pages_sent": self.pages_sent,
                "judged": len(self.judged),
                "undetermined": len(self.undetermined),
                "undetermined_pages": [
                    {"page": page, "reason": reason}
                    for page, reason in sorted(self.undetermined.items())
                ],
            },
            "findings": [item.as_dict() for item in self.findings],
            "summary": summarize(self.findings),
            "failed": self._failed(),
        }

    def _pages_payload(self, total_bytes: int) -> Mapping[str, object]:
        return {
            "jobname": self.manifest.output.jobname,
            "pdf": relative_to_root(self.builder.plan.pdf, self.builder.project_root),
            "resolution": self.resolution,
            "images_bytes": total_bytes,
            "pages": [self._choice_dict(choice) for choice in self.choices],
        }

    def _choice_dict(self, choice: PageChoice) -> Mapping[str, object]:
        return {
            "page": choice.page,
            "reasons": list(choice.reasons),
            "image": relative_to_root(choice.image, self.builder.project_root) if choice.image else None,
            "bytes": choice.size,
        }

    def _failed(self) -> bool:
        if any(item.severity == "error" for item in self.findings):
            return True
        return self.strict and any(item.severity == "warning" for item in self.findings)

    def _prompt_bundle(self) -> str:
        header = (
            f"Self-contained inspection bundle for *{self.manifest.book.title}* "
            f"(`{self.manifest.output.jobname}.pdf`, {self.page_count} pages).\n"
            f"The page images are listed below; paths are relative to this file "
            f"(`{INSPECT_DIRNAME}/`), rasterized at {self.resolution} dpi. Attach the images to the "
            "model in the listed order and ask it to judge them against the rubric."
        )
        table = render_page_list(
            [(choice.page, "; ".join(choice.reasons)) for choice in self.choices],
            [
                os.path.relpath(choice.image, self.reports) if choice.image else "(no image)"
                for choice in self.choices
            ],
        )
        return render_prompt(header, table)

    def _markdown_summary(self, total_bytes: int) -> str:
        counts = {severity: 0 for severity in SEVERITIES}
        for item in self.findings:
            counts[item.severity] += 1
        lines = [
            f"# Visual inspection: {self.manifest.output.jobname}.pdf",
            "",
            f"- pages selected: {len(self.choices)} (max {self.max_pages}, {self.resolution} dpi)",
            f"- images sent: {self.images_sent} of {len(self.choices)} selected page(s), "
            f"{total_bytes} bytes",
            f"- vision endpoint: {'configured' if self._vision_ready() else 'not configured'}",
            f"- coverage: judged {len(self.judged)}/{self.pages_sent} page(s); "
            f"{len(self.undetermined)} undetermined",
            f"- api usage: {self.requests} request(s), prompt {self.prompt_tokens} tokens "
            f"+ completion {self.completion_tokens} tokens "
            f"= {self.prompt_tokens + self.completion_tokens} tokens",
            f"- findings: {counts['error']} error, {counts['warning']} warning, {counts['info']} info",
            "",
        ]
        grouped: Dict[str, List[object]] = {}
        for item in self.findings:
            grouped.setdefault(item.code, []).append(item)
        if not grouped:
            lines.append("No findings.")
        for code, items in grouped.items():
            lines.append(f"## {code} ({len(items)}, {items[0].severity})")
            for item in items:
                lines.append(f"- {_describe(item)}")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def _vision_ready(self) -> bool:
        return bool(self.base_url and self.model and self.environ.get(self.key_env))

    def _print_report(self) -> None:
        rows = summarize(self.findings)
        if not rows:
            print("[inspect] findings: none")
            return
        counts = {severity: 0 for severity in SEVERITIES}
        for item in self.findings:
            counts[item.severity] += 1
        print(
            f"[inspect] findings: {counts['error']} error(s), {counts['warning']} warning(s), "
            f"{counts['info']} note(s)"
        )
        for item in self.findings:
            if item.severity != "error":
                continue
            if isinstance(item, VisualFinding):
                print(
                    f"[inspect] error: page {item.page}: {item.what} [{item.where}] "
                    f"-> {item.converter_hint}"
                )
            elif isinstance(item, Finding):
                location = f" ({item.location})" if item.location else ""
                print(f"[inspect] error: [{item.code}] {item.message}{location}")

    def _print_usage(self) -> None:
        """一次运行的 token 账目；没有配置每千 token 的价格就不给费用估算。"""
        if not self.requests:
            return
        total = self.prompt_tokens + self.completion_tokens
        print(
            f"[inspect] api usage: {self.requests} request(s), "
            f"prompt {self.prompt_tokens} tokens + completion {self.completion_tokens} tokens "
            f"= {total} tokens (no per-1k price configured, so no cost estimate)"
        )


def _describe(item: object) -> str:
    if isinstance(item, VisualFinding):
        hint = f" — hint: {item.converter_hint}" if item.converter_hint else ""
        return f"page {item.page} [{item.where}] {item.what}{hint}"
    if isinstance(item, Finding):
        location = f" ({item.location})" if item.location else ""
        return f"{item.message}{location}"
    return str(item)
