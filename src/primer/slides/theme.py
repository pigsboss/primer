# -*- coding: utf-8 -*-
"""主题：画布、字号梯级与配色 token——**全都是数据**。

它们和页数一样写在 ``outline.yaml`` 的 ``theme:`` 块里，随时能在 YAML 里改：换画布、
换字号、换一套颜色，``build`` 跟着变，页面代码一行不动。这个模块只做三件事：

* 给出**默认值**——Palette A · 学术极简，取自 ``_primer/slides/theme-samples/samples.tex``
  的画布（960 × 540 bp、页边 5%）、字号数据表、字体栈与 theme/A 块的颜色键；
* 把 YAML 块**读成** :class:`Theme`：缺的键取默认值，认不出的键与读不出的值**报问题**
  （由 ``check`` 变成 error，挡住生成）；
* 把 :class:`Theme` **写回** YAML 块（``outline`` 用它发射）。

一页正文的容量（字/行 × 行/页）也在这里由画布与字号算出来，见 :func:`frame_metrics`：
``check`` 的 ``capacity_per_page`` 取它的保守值——旧值 176 是按纸面估的（``⌊453.54/20⌋``
字/行 × ``⌊255.12/32⌋`` 行），从来没有按版心量过。

**画布单位必须写 bp**（1 bp = 1/72 in = 一个 PDF 点）：``geometry`` 的 ``pt`` 是 TeX 点
（1/72.27 in），``paperwidth=960pt`` 出来的是 956.41 × 537.98 bp 的纸，不是 960 × 540。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Mapping, Tuple

MM_PER_PT = 25.4 / 72.0
MM_PER_BP = 25.4 / 72.0

# ---------------------------------------------------------------- 版式常量（实测）

# 画布：PowerPoint 标准 16:9，960 × 540 bp = 13.3333 × 7.5 in = 338.67 × 190.5 mm。
# 磅值只有在同一块画布上才是同一件事：22 pt 正文在它上面占纸宽 2.29%。
DEFAULT_CANVAS_WIDTH_BP = 960.0
DEFAULT_CANVAS_HEIGHT_BP = 540.0
# 页边 = 纸宽的 5%（旧样 8 mm / 160 mm，同一个比例）→ 版心 = 纸宽的 90% = 304.8 mm。
DEFAULT_MARGIN_RATIO = 0.05

# 字号梯级：帧标题、正文、表格表头、表格正文、图注、页脚（字号 / 行距，pt）。
# 表头 18 pt 与图注 18 pt 同为一级；表头行距沿用表格栅格 24 pt，于是它不与 16 pt 数据行
# 差高（行盒高由 \strutbox 定）。
TYPE_LEVEL_ORDER = ("title", "body", "table_head", "table_body", "caption", "footer")
TYPE_LEVEL_LABELS = {
    "title": "帧标题",
    "body": "正文（要点、段落）",
    "table_head": "表格表头",
    "table_body": "表格正文",
    "caption": "图注 / 题注",
    "footer": "页脚指针 / 进度条文字",
}
DEFAULT_TYPE_SCALE = {
    "title": (28.0, 34.0),
    # 正文行距 30 pt（= 22 × 1.36）：26 pt（1.18 倍）在 22 pt 中文上太挤，用户看过实物后点名要松。
    "body": (22.0, 30.0),
    "table_head": (18.0, 24.0),
    "table_body": (16.0, 24.0),
    "caption": (18.0, 24.0),
    "footer": (14.0, 17.0),
}

# 幻灯片**自己的**字体栈——不再跟着成书清单的 ``manifest.fonts``（那是 Times + 宋体的
# 成书面孔）。正文与结构都走无衬线：拉丁 Helvetica Neue、中文冬青黑体（W3 常规 / W6 半粗），
# 等宽 Menlo。可在 ``outline.yaml`` 的 ``theme.fonts`` 块里逐个覆盖（见 :func:`parse_theme`）。
FONT_KEYS = ("main", "sans", "mono", "cjk_main", "cjk_sans", "cjk_mono")
DEFAULT_FONTS = {
    "main": "Helvetica Neue",
    "sans": "Helvetica Neue",
    "mono": "Menlo",
    "cjk_main": "Hiragino Sans GB W3",
    "cjk_sans": "Hiragino Sans GB W6",
    "cjk_mono": "Hiragino Sans GB W3",
}
# 合成粗体（fontspec 的 AutoFakeBold）：W6 是真实半粗字面，再叠合成粗体会发糊，取 0；
# W3 没有粗体字面，\textbf 靠 2.5 合成。AutoFakeSlant 给 WOFF/无斜体字面的拉丁字留一条斜体。
DEFAULT_CJK_FAKE_BOLD = "2.5"
DEFAULT_CJK_SANS_FAKE_BOLD = "0"
DEFAULT_CJK_FAKE_SLANT = "0.15"

# 配色：四个颜色 token 加篇色（卷键 → 颜色）。Palette A · 学术极简——单色蓝 + 灰阶，
# 层次交给字重与一道发丝线，表头与斑马纹都不填色。
PALETTE_TOKEN_ORDER = ("ink", "muted", "hairline", "accent")
VOLUME_TOKEN_ORDER = ("V1", "V2", "V3", "VA")
DEFAULT_PALETTE = {
    "ink": "#1a1a1a",
    "muted": "#6b7280",
    "hairline": "#d1d5db",
    "accent": "#1f4e79",
}
DEFAULT_VOLUME_PALETTE = {
    "V1": "#1f4e79",
    "V2": "#2f6f9f",
    "V3": "#4a90c2",
    "VA": "#9aa5b1",
}

# 内容页的正文框 = 0.63\textwidth（样张的内容页：右栏 0.36 让给图与图注）。
BODY_COLUMN_RATIO = 0.63
# 标题带的上下空：\tmTopPad 2.03 + 标题前空 1.6 + 标题后空 3.6（样张的版心常量）。
TITLE_BAND_PADDING_MM = 2.03 + 1.6 + 3.6
# 一行标题的标题盒高（\ht+\dp）与行距的比：实测 24/29 → 23.016 TeX pt、28/34 → 26.852 pt，
# 两者都是 0.793 × 行距。标题带高 = 上下空 + 这个盒高 +（每多一行再加一个行距）。
TITLE_BOX_PER_LEADING = 0.7925
# 页脚墨迹顶到纸面下沿的距离（样张实测：190.5 − 182.7 = 7.8 mm，页脚三串字里最高的那串）。
FOOTER_INK_FROM_BOTTOM_MM = 7.8
# 正文与页脚之间留的净距。
BODY_FOOTER_CLEARANCE_MM = 2.0
# 封面副题行与它上面那行（书目副题，书名下的年份）之间的净距：换一段再写一行的小空，
# 取 1.4 mm——与封面里"主标题 → 书目副题"那一段同一个量级（见 beamer._cover_frame）。
COVER_SUBLINE_GAP_MM = 1.4

HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


@dataclass(frozen=True)
class TypeLevel:
    """一级字号：``size`` 是字号、``leading`` 是行距，都按 pt。"""

    size: float
    leading: float


@dataclass(frozen=True)
class FrameMetrics:
    """一页正文的几何与容量，全部由画布与字号算出（mm）。

    ``lines`` 与 ``capacity_chars`` 取**保守**的那一个：按两行标题的页算。生产版的章标题
    在 304.8 mm 的版心上一行放得下（28 字最长的一个占 277 mm），但 ``check`` 宁可少算一行，
    也不让"放不下"在编译时才暴露出来。
    """

    text_width_mm: float
    body_column_mm: float
    chars_per_line: int
    title_band_one_line_mm: float
    title_band_two_line_mm: float
    body_area_one_line_mm: float
    body_area_two_line_mm: float
    lines: int
    capacity_chars: int


@dataclass(frozen=True)
class Theme:
    """一套主题：画布、字号梯级、字体栈与配色 token。

    值一律是**数据**，不认预设名：YAML 里写的就是页面上用的。
    """

    width_bp: float = DEFAULT_CANVAS_WIDTH_BP
    height_bp: float = DEFAULT_CANVAS_HEIGHT_BP
    margin_ratio: float = DEFAULT_MARGIN_RATIO
    type_scale: Mapping[str, TypeLevel] = None  # type: ignore[assignment]
    palette: Mapping[str, str] = None  # type: ignore[assignment]
    volume: Mapping[str, str] = None  # type: ignore[assignment]
    fonts: Mapping[str, str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.type_scale is None:
            object.__setattr__(
                self,
                "type_scale",
                {name: TypeLevel(*DEFAULT_TYPE_SCALE[name]) for name in TYPE_LEVEL_ORDER},
            )
        if self.palette is None:
            object.__setattr__(self, "palette", dict(DEFAULT_PALETTE))
        if self.volume is None:
            object.__setattr__(self, "volume", dict(DEFAULT_VOLUME_PALETTE))
        if self.fonts is None:
            object.__setattr__(self, "fonts", dict(DEFAULT_FONTS))

    # ---- 供页面代码取用的量 ----

    @property
    def height_mm(self) -> float:
        return self.height_bp * MM_PER_BP

    @property
    def text_width_mm(self) -> float:
        return self.width_bp * MM_PER_BP * (1 - 2 * self.margin_ratio)

    def level(self, name: str) -> TypeLevel:
        """一级字号；名字认不出时按正文算（读的是默认值，不抛异常）。"""
        return self.type_scale.get(name) or TypeLevel(*DEFAULT_TYPE_SCALE["body"])

    def font(self, name: str) -> str:
        """字体族名；名字认不出时退回该族的默认值（认不出的键在 parse 时已报问题）。"""
        return self.fonts.get(name) or DEFAULT_FONTS.get(name, "")

    def volume_keys(self) -> Tuple[str, ...]:
        return tuple(self.volume)

    def volume_colour(self, key: str) -> str:
        """卷键 → 颜色；不在表里的卷退回第一篇（框架里没有的颜色宏画不出来）。"""
        return self.volume.get(key) or self.volume.get(
            VOLUME_TOKEN_ORDER[0], DEFAULT_VOLUME_PALETTE[VOLUME_TOKEN_ORDER[0]]
        )

    def as_dict(self) -> Mapping[str, object]:
        """写回 YAML 的 ``theme:`` 块；键名自带单位（bp / pt），免得把 pt 当 bp。"""
        return {
            "canvas": {
                "width_bp": _number(self.width_bp),
                "height_bp": _number(self.height_bp),
                "margin_ratio": _number(self.margin_ratio),
            },
            "type": {
                name: {
                    "size_pt": _number(self.level(name).size),
                    "leading_pt": _number(self.level(name).leading),
                }
                for name in TYPE_LEVEL_ORDER
            },
            "fonts": {key: self.font(key) for key in FONT_KEYS},
            "palette": {
                **{token: self.palette.get(token, DEFAULT_PALETTE[token]) for token in PALETTE_TOKEN_ORDER},
                "volume": {
                    key: self.volume.get(key, DEFAULT_VOLUME_PALETTE[key])
                    for key in VOLUME_TOKEN_ORDER
                },
            },
        }


def _number(value: float) -> object:
    """整数就写成整数（960 而不是 960.0），YAML 里读起来干净。"""
    return int(value) if float(value).is_integer() else float(value)


def default_theme() -> Theme:
    """Palette A · 学术极简。"""
    return Theme()


def _positive_number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return float("nan")
    return float(value)


def parse_theme(raw: object) -> Tuple[Theme, Tuple[Tuple[str, str], ...]]:
    """YAML 的 ``theme:`` 块 → （主题，问题清单）。

    缺的键取默认值（人手写一小块只想改一处也是允许的），**认不出的键**与**读不出的值**
    各报一条问题：颜色必须是 ``#rrggbb``，字号与画布尺寸必须是正数，字体名必须是非空串。
    问题不抛异常，由 ``check`` 变成 error。
    """
    problems: List[Tuple[str, str]] = []
    if raw is None:
        return default_theme(), ()
    if not isinstance(raw, Mapping):
        return default_theme(), (
            ("theme-canvas", f"theme 必须是一张映射表，读到的是 {type(raw).__name__}"),
        )

    for key in raw:
        if key not in ("canvas", "type", "fonts", "palette"):
            problems.append(
                (
                    "theme-canvas",
                    f"theme 里有未知的一段 {key!r}（认得的段：canvas、type、fonts、palette）",
                )
            )

    width, height, margin = (
        DEFAULT_CANVAS_WIDTH_BP,
        DEFAULT_CANVAS_HEIGHT_BP,
        DEFAULT_MARGIN_RATIO,
    )
    canvas = raw.get("canvas")
    if canvas is not None and not isinstance(canvas, Mapping):
        problems.append(("theme-canvas", f"theme.canvas 必须是一张映射表，读到的是 {type(canvas).__name__}"))
        canvas = None
    if isinstance(canvas, Mapping):
        for key in canvas:
            if key not in ("width_bp", "height_bp", "margin_ratio"):
                problems.append(
                    (
                        "theme-canvas",
                        f"theme.canvas 里有未知的键 {key!r}"
                        "（认得的键：width_bp、height_bp、margin_ratio）",
                    )
                )
        for key, default in (
            ("width_bp", DEFAULT_CANVAS_WIDTH_BP),
            ("height_bp", DEFAULT_CANVAS_HEIGHT_BP),
        ):
            if key not in canvas:
                continue
            value = _positive_number(canvas[key])
            if value != value or value <= 0:
                problems.append(
                    ("theme-canvas", f"theme.canvas.{key} 必须是正数（bp）：{canvas[key]!r}")
                )
                continue
            if key == "width_bp":
                width = value
            else:
                height = value
        if "margin_ratio" in canvas:
            value = _positive_number(canvas["margin_ratio"])
            if value != value or not 0.0 < value < 0.5:
                problems.append(
                    (
                        "theme-canvas",
                        f"theme.canvas.margin_ratio 必须是 0 与 0.5 之间的页边比例：{canvas['margin_ratio']!r}",
                    )
                )
            else:
                margin = value

    type_scale: Dict[str, TypeLevel] = {
        name: TypeLevel(*DEFAULT_TYPE_SCALE[name]) for name in TYPE_LEVEL_ORDER
    }
    blocks = raw.get("type")
    if blocks is not None and not isinstance(blocks, Mapping):
        problems.append(("theme-type", f"theme.type 必须是一张映射表，读到的是 {type(blocks).__name__}"))
        blocks = None
    if isinstance(blocks, Mapping):
        for name in blocks:
            if name not in TYPE_LEVEL_ORDER:
                problems.append(
                    (
                        "theme-type",
                        f"theme.type 里有未知的档位 {name!r}（认得的档位："
                        + "、".join(TYPE_LEVEL_ORDER)
                        + "）",
                    )
                )
        for name in TYPE_LEVEL_ORDER:
            entry = blocks.get(name)
            if entry is None:
                continue
            if not isinstance(entry, Mapping):
                problems.append(
                    ("theme-type", f"theme.type.{name} 必须是一张映射表，读到的是 {type(entry).__name__}")
                )
                continue
            for key in entry:
                if key not in ("size_pt", "leading_pt"):
                    problems.append(
                        (
                            "theme-type",
                            f"theme.type.{name} 里有未知的键 {key!r}（认得的键：size_pt、leading_pt）",
                        )
                    )
            values = [type_scale[name].size, type_scale[name].leading]
            for index, key in enumerate(("size_pt", "leading_pt")):
                if key not in entry:
                    continue
                value = _positive_number(entry[key])
                if value != value or value <= 0:
                    problems.append(
                        ("theme-type", f"theme.type.{name}.{key} 必须是正数（pt）：{entry[key]!r}")
                    )
                    continue
                values[index] = value
            type_scale[name] = TypeLevel(*values)
        # 行距不得小于字号：小到那个地步，两行会叠在一起
        for name in TYPE_LEVEL_ORDER:
            level = type_scale[name]
            if level.leading < level.size:
                problems.append(
                    (
                        "theme-type",
                        f"theme.type.{name} 的行距 {level.leading:g} pt 小于字号 "
                        f"{level.size:g} pt，两行会叠在一起",
                    )
                )

    fonts = dict(DEFAULT_FONTS)
    font_block = raw.get("fonts")
    if font_block is not None and not isinstance(font_block, Mapping):
        problems.append(
            ("theme-fonts", f"theme.fonts 必须是一张映射表，读到的是 {type(font_block).__name__}")
        )
        font_block = None
    if isinstance(font_block, Mapping):
        for key in font_block:
            if key not in FONT_KEYS:
                problems.append(
                    (
                        "theme-fonts",
                        f"theme.fonts 里有未知的键 {key!r}（认得的键：" + "、".join(FONT_KEYS) + "）",
                    )
                )
                continue
            value = font_block[key]
            # 字体名是给排版器认的字面：空串与空白串等于没写，报问题而不是悄悄退回默认值
            if not isinstance(value, str) or not value.strip():
                problems.append(
                    ("theme-fonts", f"theme.fonts.{key} 必须是非空字体名字符串：{value!r}")
                )
                continue
            fonts[key] = value

    palette = dict(DEFAULT_PALETTE)
    volume = dict(DEFAULT_VOLUME_PALETTE)
    block = raw.get("palette")
    if block is not None and not isinstance(block, Mapping):
        problems.append(("theme-token", f"theme.palette 必须是一张映射表，读到的是 {type(block).__name__}"))
        block = None
    if isinstance(block, Mapping):
        for token in block:
            if token not in PALETTE_TOKEN_ORDER and token != "volume":
                problems.append(
                    (
                        "theme-token",
                        f"theme.palette 里有未知的 token {token!r}（认得的 token："
                        + "、".join((*PALETTE_TOKEN_ORDER, "volume"))
                        + "）",
                    )
                )
        for token in PALETTE_TOKEN_ORDER:
            if token not in block:
                continue
            value = block[token]
            if not isinstance(value, str) or not HEX_RE.match(value):
                problems.append(
                    ("theme-token", f"theme.palette.{token} 必须是 #rrggbb 颜色：{value!r}")
                )
                continue
            palette[token] = value
        volume_block = block.get("volume")
        if volume_block is not None and not isinstance(volume_block, Mapping):
            problems.append(
                ("theme-token", f"theme.palette.volume 必须是一张映射表，读到的是 {type(volume_block).__name__}")
            )
            volume_block = None
        if isinstance(volume_block, Mapping):
            for key in volume_block:
                if key not in VOLUME_TOKEN_ORDER:
                    problems.append(
                        (
                            "theme-token",
                            f"theme.palette.volume 里有未知的卷键 {key!r}（认得的卷键："
                            + "、".join(VOLUME_TOKEN_ORDER)
                            + "）",
                        )
                    )
                    continue
                value = volume_block[key]
                if not isinstance(value, str) or not HEX_RE.match(value):
                    problems.append(
                        ("theme-token", f"theme.palette.volume.{key} 必须是 #rrggbb 颜色：{value!r}")
                    )
                    continue
                volume[key] = value

    theme = Theme(
        width_bp=width,
        height_bp=height,
        margin_ratio=margin,
        type_scale=type_scale,
        palette=palette,
        volume=volume,
        fonts=fonts,
    )
    return theme, tuple(problems)


def read_theme(document: Mapping[str, object]) -> Tuple[Theme, Tuple[Tuple[str, str], ...]]:
    """骨架（``outline.yaml`` 的数据）→ （主题，问题清单）。没有 ``theme:`` 段就用默认值。"""
    return parse_theme(document.get("theme"))


def frame_metrics(theme: Theme) -> FrameMetrics:
    """一页正文的几何与容量：字/行由正文框宽与汉字宽定，行/页由正文区高与行距定。

    正文框 = 0.63 × 版心；汉字宽 = 字号（中文一字一 em）；正文区 = 标题带下沿到
    「页脚墨迹顶再让 2 mm 净距」。页脚与图表都不许挤占正文框，所以正文区的下沿是页脚，
    不是纸边。
    """
    text_width_mm = theme.text_width_mm
    body_column_mm = BODY_COLUMN_RATIO * text_width_mm
    body = theme.level("body")
    char_mm = body.size * MM_PER_PT
    chars_per_line = int(body_column_mm // char_mm) if char_mm > 0 else 0

    leading_mm = body.leading * MM_PER_PT
    title_leading_mm = theme.level("title").leading * MM_PER_PT
    title_box_mm = TITLE_BOX_PER_LEADING * theme.level("title").leading * MM_PER_PT
    band_one = TITLE_BAND_PADDING_MM + title_box_mm
    band_two = band_one + title_leading_mm
    body_bottom = theme.height_mm - FOOTER_INK_FROM_BOTTOM_MM - BODY_FOOTER_CLEARANCE_MM
    area_one = max(body_bottom - band_one, 0.0)
    area_two = max(body_bottom - band_two, 0.0)
    lines_one = int(area_one // leading_mm) if leading_mm > 0 else 0
    lines_two = int(area_two // leading_mm) if leading_mm > 0 else 0
    lines = min(lines_one, lines_two)
    return FrameMetrics(
        text_width_mm=text_width_mm,
        body_column_mm=body_column_mm,
        chars_per_line=chars_per_line,
        title_band_one_line_mm=band_one,
        title_band_two_line_mm=band_two,
        body_area_one_line_mm=area_one,
        body_area_two_line_mm=area_two,
        lines=lines,
        capacity_chars=chars_per_line * lines,
    )


def default_capacity_per_page() -> int:
    """默认主题下一页正文的字数（保守值：按两行标题的页算）。"""
    return frame_metrics(default_theme()).capacity_chars


def floor_level_name(theme: Theme) -> str:
    """字号梯级里最小的一档——一份文件的**字号下限**。

    备份页的查找表就是拿它排的：条目多、每页放不下时只收摘要长度，不缩字号，所以
    需要一个明确的下限。同值时按 :data:`TYPE_LEVEL_ORDER` 的次序取前面那个，结果与
    主题块里写的次序无关。
    """
    return min(
        TYPE_LEVEL_ORDER,
        key=lambda name: (theme.level(name).size, TYPE_LEVEL_ORDER.index(name)),
    )


def type_listing(theme: Theme) -> str:
    """字号梯级的一行人话，给报告用：``帧标题 28/34、正文 22/26、…``（pt）。"""
    return "、".join(
        f"{TYPE_LEVEL_LABELS[name]} {theme.level(name).size:g}/{theme.level(name).leading:g}"
        for name in TYPE_LEVEL_ORDER
    )
