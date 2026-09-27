# -*- coding: utf-8 -*-
"""成书清单（manifest）：数据结构、YAML 载入与校验。

构书脚本里原先写死的东西全部外置到清单：篇（卷）与排列顺序、源文件根目录、
输出目录与作业名、编译引擎与遍数、字体与字号、每篇的章节重编号方案、
封面与前置页文本。路径字段的解析基准如下：

* ``project_root``：相对清单文件所在目录；不给时就是清单文件所在目录；
* ``source_root``、``output.directory``：相对 ``project_root``；``output.directory``
  不给时取 ``<project_root>/_primer/book``；
* ``volumes[].sources[].file``、``bibliography.file``：相对 ``source_root``。

字段缺失、类型不对、引用了不存在的篇或方案、源文件不可读，一律在载入时抛出
:class:`ManifestError`。异常消息为英文。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence, Tuple

import yaml

from .paths import output_dir_for

BIBLIOGRAPHY_KEY = "@bibliography"
CITATION_MODES = ("global", "local")

_HEADING_PLACEHOLDER = re.compile(r"\{(\w+)\}")


class ManifestError(Exception):
    """清单缺失或非法。"""


# ---------------------------------------------------------------- 字段解析


def _mapping(value: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ManifestError(f"{where} must be a mapping")
    return value


def _reject_unknown(value: Mapping[str, Any], allowed: Sequence[str], where: str) -> None:
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        raise ManifestError(f"{where} has unknown key(s): {', '.join(unknown)}")


def _required(value: Mapping[str, Any], key: str, where: str) -> Any:
    if value.get(key) is None:
        raise ManifestError(f"{where}.{key} is required")
    return value[key]


def _text(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(f"{where} must be a non-empty string")
    return value


def _optional_text(value: Any, where: str, default: str = "") -> str:
    if value is None:
        return default
    if not isinstance(value, str):
        raise ManifestError(f"{where} must be a string")
    return value


def _atoms(value: Any, where: str) -> Tuple[str, ...]:
    """接受字符串或字符串列表，统一为元组。"""
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list) and all(isinstance(item, (str, int, float)) for item in value):
        return tuple(str(item) for item in value)
    raise ManifestError(f"{where} must be a string or a list of strings")


def _integer(value: Any, where: str, minimum: Optional[int] = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ManifestError(f"{where} must be an integer")
    if minimum is not None and value < minimum:
        raise ManifestError(f"{where} must be >= {minimum}")
    return value


def _number(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ManifestError(f"{where} must be a number")
    return float(value)


def _flag(value: Any, where: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ManifestError(f"{where} must be a boolean")
    return value


def _path(value: Any, base: Path, where: str) -> Path:
    """相对路径按 ``base`` 解析，绝对路径原样使用。"""
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(f"{where} must be a non-empty path string")
    raw = Path(value).expanduser()
    return raw.resolve() if raw.is_absolute() else (base / raw).resolve()


def _pairs(value: Any, where: str) -> Tuple[Tuple[str, str], ...]:
    """形如 ``[[旧, 新], ...]`` 的字面替换表。"""
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ManifestError(f"{where} must be a list of [from, to] pairs")
    pairs = []
    for index, item in enumerate(value):
        if not (isinstance(item, list) and len(item) == 2):
            raise ManifestError(f"{where}[{index}] must be a [from, to] pair")
        pairs.append((str(item[0]), str(item[1])))
    return tuple(pairs)


# ---------------------------------------------------------------- 数据结构


@dataclass(frozen=True)
class HeadingRule:
    """一条标题重编号规则：命中 ``pattern`` 的行按 ``template`` 重写。

    ``numbers`` 列出按数字处理的捕获组（如 ``["g1"]``），``offsets`` 给出
    每个数字组的偏移量（缺省 0）；未列入的捕获组按文本处理并去掉首尾空白。
    模板用 ``{g1}``、``{g2}`` 引用捕获组。
    """

    pattern: re.Pattern
    template: str
    numbers: Tuple[str, ...] = ()
    offsets: Mapping[str, int] = field(default_factory=dict)

    def render(self, matched: re.Match) -> str:
        """按模板重写标题：数字组加偏移后取整，其余组按文本并去空白。"""

        def replace(placeholder: re.Match) -> str:
            name = placeholder.group(1)
            value = matched.group(int(name[1:]))
            if name in self.numbers:
                return str(int(value) + self.offsets.get(name, 0))
            return value.strip()

        return _HEADING_PLACEHOLDER.sub(replace, self.template)


@dataclass(frozen=True)
class HeadingScheme:
    """一篇（或一篇内某个源文件）的章节重编号方案。

    ``rules``：按序尝试的标题重写规则，命中即止，未命中再做 ``replacements``。
    ``replacements``：正文交叉引用的字面替换表。
    """

    name: str
    rules: Tuple[HeadingRule, ...] = ()
    replacements: Tuple[Tuple[str, str], ...] = ()


@dataclass(frozen=True)
class SourceSpec:
    """一个源 markdown 文件。"""

    path: Path
    scheme: Optional[str] = None
    banner: Optional[str] = None


@dataclass(frozen=True)
class VolumeSpec:
    """一篇（卷）：源文件序列及其在成书中的身份。

    ``appendix`` 为真时该篇按附录处理：正文里先插入 ``\\appendix``，篇内各章
    改用字母编号（附录 A、附录 B……）。
    """

    id: str
    title: str
    sources: Tuple[SourceSpec, ...]
    standalone: bool = False
    part_label: Optional[str] = None
    citation_mode: str = "global"
    appendix: bool = False


@dataclass(frozen=True)
class BookInfo:
    """封面信息。"""

    title: str
    subtitle: str = ""
    tagline: str = ""
    institution: str = ""
    date: str = ""


@dataclass(frozen=True)
class Fonts:
    """字体族名。"""

    main: str = "Times New Roman"
    sans: str = "Helvetica Neue"
    mono: str = "Menlo"
    cjk_main: str = "Songti SC"
    cjk_sans: str = "Heiti SC"
    cjk_mono: str = "Songti SC"


@dataclass(frozen=True)
class Typography:
    """版面参数。

    ``table_font_size`` 是表格字号的 ``\\zihao`` 代码（``-4`` 即小四）；表格放不下
    时排版系统会按阶梯自动降号。``toc_depth`` 是目录深度（0 = 篇与章，1 = 加节）。

    ``normalize_quotes`` 为真时（默认）在装配阶段把正文里的 ASCII 直引号配成中文
    弯引号——引号方向是形式，由排版系统决定，源文件不改；关掉它可用命令行的
    ``--no-normalize-quotes``。

    ``cjk_fake_bold`` 是 CJK 主、等宽两族的合成粗体权重（``AutoFakeBold``）：
    宋体没有可用的粗体字面，取 0 会让正文的 ``\\textbf`` 悄悄退回常规字重，所以
    默认保留 2.5 来"假粗"。``cjk_sans_fake_bold`` 只作用于 CJK 无衬线（标题）族，
    空值表示沿用 ``cjk_fake_bold``。装了真实半粗字面（如冬青黑体 W6）的族要在其上
    再叠合成粗体就是双重加粗、观感发糊，这时把该族单独设为 ``"0"``。
    """

    paper: str = "a4paper"
    margin: str = "2.54cm"
    body_font_size: str = "4"
    line_spread: float = 1.25
    par_skip: str = "0.2em"
    caption_skip: str = "6pt"
    cjk_fake_bold: str = "2.5"
    cjk_sans_fake_bold: str = ""
    cjk_fake_slant: str = "0.15"
    toc_name: str = "目录"
    figure_list_name: str = "插图目录"
    table_list_name: str = "表格目录"
    table_font_size: str = "-4"
    toc_depth: int = 1
    normalize_quotes: bool = True


@dataclass(frozen=True)
class Output:
    """输出与编译设置。

    ``directory`` 是产物目录（相对工程根）；``engine_runs`` 是 ``xelatex -no-pdf``
    的遍数，之后固定跑一次 ``xdvipdfmx`` 出 PDF。
    """

    jobname: str
    directory: Optional[Path] = None
    engine: str = "xelatex"
    engine_runs: int = 2
    min_pdf_bytes: int = 20000
    emit_markdown: bool = True


@dataclass(frozen=True)
class VisionConfig:
    """多模态检查端点（``inspect`` 用）。

    ``key_env`` 是**环境变量名**，不是密钥本身：密钥只从环境读，绝不从清单或任何
    凭据文件里取。三项端点字段都为空即视为"未配置端点"。

    ``base_url`` 按原样拼上 ``/chat/completions``，带不带 ``/v1`` 由调用方决定，
    两种写法都可用。

    ``batch_size`` 与 ``max_tokens`` 是"每请求几页"与"回信上限"这一对相反的旋钮，
    不写则用 inspect 的内置默认（2 页、16 384 token），写下来的优先，命令行显式
    给出的再优先。记在模型名旁边是有意的：思考型模型的推理链也吃输出预算，可靠
    取值取决于模型本身，不该靠人记住一条魔法命令行参数——实测 ``deepseek-flash``
    两页一请求时推理链会把 16 384 个输出 token 全部耗光、正文为空
    （``finish_reason=length``），一页一请求才稳定给出 JSON，所以该模型的清单里
    ``batch_size: 1`` 才是可复现的取值。
    """

    base_url: str = ""
    model: str = ""
    key_env: str = "PRIMER_VISION_API_KEY"
    batch_size: Optional[int] = None
    max_tokens: Optional[int] = None


@dataclass(frozen=True)
class CatalogEntry:
    """附录目录的一行。"""

    title: str
    label: str


@dataclass(frozen=True)
class FrontMatter:
    """前置页文本。"""

    foreword_heading: str = "凡　例"
    foreword: Tuple[str, ...] = ()
    catalog_heading: str = "附录目录"
    appendix_catalog: Tuple[CatalogEntry, ...] = ()


@dataclass(frozen=True)
class BookManifest:
    """一份完整的成书清单。

    ``drop_lines`` 是全书通用的整行丢弃正则（编辑性尾注、工作记录等），对每个
    源文件都生效，与篇章编号方案无关。
    """

    path: Path
    project_root: Path
    source_root: Path
    book: BookInfo
    output: Output
    volumes: Tuple[VolumeSpec, ...]
    body_order: Tuple[str, ...]
    schemes: Mapping[str, HeadingScheme]
    drop_lines: Tuple[re.Pattern, ...] = ()
    bibliography: Optional[Path] = None
    bibliography_section_title: str = "参考文献"
    fonts: Fonts = field(default_factory=Fonts)
    typography: Typography = field(default_factory=Typography)
    front_matter: FrontMatter = field(default_factory=FrontMatter)
    vision: Optional[VisionConfig] = None

    def volume(self, volume_id: str) -> VolumeSpec:
        for spec in self.volumes:
            if spec.id == volume_id:
                return spec
        raise ManifestError(f"unknown volume id: {volume_id}")

    def scheme(self, name: str) -> HeadingScheme:
        try:
            return self.schemes[name]
        except KeyError:
            raise ManifestError(f"unknown heading scheme: {name}") from None


# ---------------------------------------------------------------- 载入


def load_manifest(path: Path) -> BookManifest:
    """读取并校验清单文件，返回 :class:`BookManifest`。"""
    manifest_path = Path(path).expanduser().resolve()
    if not manifest_path.is_file():
        raise ManifestError(f"manifest not found: {manifest_path}")
    try:
        raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ManifestError(f"manifest is not valid YAML: {exc}") from exc
    if raw is None:
        raise ManifestError(f"manifest is empty: {manifest_path}")
    return _parse(_mapping(raw, "manifest"), manifest_path)


_TOP_KEYS = (
    "book",
    "project_root",
    "source_root",
    "bibliography",
    "output",
    "fonts",
    "typography",
    "heading_schemes",
    "volumes",
    "body_order",
    "drop_lines",
    "front_matter",
    "vision",
)

_VOLUME_KEYS = (
    "id",
    "title",
    "sources",
    "standalone",
    "part_label",
    "citation_mode",
    "appendix",
)
_SOURCE_KEYS = ("file", "scheme", "banner")


def _parse(raw: Mapping[str, Any], manifest_path: Path) -> BookManifest:
    _reject_unknown(raw, _TOP_KEYS, "manifest")
    base = manifest_path.parent
    project_root = _path(
        raw.get("project_root", "."), base, "manifest.project_root"
    )
    root = _path(_required(raw, "source_root", "manifest"), project_root, "manifest.source_root")
    if not root.is_dir():
        raise ManifestError(f"manifest.source_root is not a directory: {root}")

    schemes = _parse_schemes(raw.get("heading_schemes"))
    volumes = _parse_volumes(raw, root, schemes)
    known = {spec.id for spec in volumes}

    bibliography = None
    bibliography_section_title = "参考文献"
    if raw.get("bibliography") is not None:
        section = _mapping(raw["bibliography"], "manifest.bibliography")
        _reject_unknown(section, ("file", "section_title"), "manifest.bibliography")
        bibliography = _path(
            _required(section, "file", "manifest.bibliography"), root, "manifest.bibliography.file"
        )
        if not bibliography.is_file():
            raise ManifestError(f"bibliography file is not readable: {bibliography}")
        bibliography_section_title = _optional_text(
            section.get("section_title"), "manifest.bibliography.section_title",
            "全文总参考文献列表",
        )

    body_order = _atoms(_required(raw, "body_order", "manifest"), "manifest.body_order")
    if not body_order:
        raise ManifestError("manifest.body_order must not be empty")
    for key in body_order:
        if key == BIBLIOGRAPHY_KEY:
            if bibliography is None:
                raise ManifestError(
                    f"manifest.body_order uses {BIBLIOGRAPHY_KEY} but no bibliography is configured"
                )
        elif key not in known:
            raise ManifestError(f"manifest.body_order references unknown volume: {key}")
    missing = [spec.id for spec in volumes if spec.id not in body_order]
    if missing:
        raise ManifestError(f"volumes missing from body_order: {', '.join(missing)}")

    return BookManifest(
        path=manifest_path,
        project_root=project_root,
        source_root=root,
        book=_parse_book(raw.get("book")),
        output=_parse_output(raw, project_root),
        volumes=volumes,
        body_order=body_order,
        schemes=schemes,
        drop_lines=_compile_patterns(raw.get("drop_lines"), "manifest.drop_lines"),
        bibliography=bibliography,
        bibliography_section_title=bibliography_section_title,
        fonts=_parse_fonts(raw.get("fonts")),
        typography=_parse_typography(raw.get("typography")),
        front_matter=_parse_front_matter(raw.get("front_matter")),
        vision=_parse_vision(raw.get("vision")),
    )


def _parse_book(raw: Any) -> BookInfo:
    section = _mapping(raw, "manifest.book")
    _reject_unknown(
        section, ("title", "subtitle", "tagline", "institution", "date"), "manifest.book"
    )
    return BookInfo(
        title=_text(_required(section, "title", "manifest.book"), "manifest.book.title"),
        subtitle=_optional_text(section.get("subtitle"), "manifest.book.subtitle"),
        tagline=_optional_text(section.get("tagline"), "manifest.book.tagline"),
        institution=_optional_text(section.get("institution"), "manifest.book.institution"),
        date=_optional_text(section.get("date"), "manifest.book.date"),
    )


def _parse_fonts(raw: Any) -> Fonts:
    section = _mapping(raw, "manifest.fonts") if raw is not None else {}
    _reject_unknown(section, tuple(Fonts.__dataclass_fields__), "manifest.fonts")
    defaults = Fonts()
    return Fonts(
        **{
            name: _optional_text(
                section.get(name), f"manifest.fonts.{name}", getattr(defaults, name)
            )
            for name in Fonts.__dataclass_fields__
        }
    )


def _parse_typography(raw: Any) -> Typography:
    section = _mapping(raw, "manifest.typography") if raw is not None else {}
    _reject_unknown(section, tuple(Typography.__dataclass_fields__), "manifest.typography")
    defaults = Typography()
    values: dict = {}
    for name in Typography.__dataclass_fields__:
        value = section.get(name)
        where = f"manifest.typography.{name}"
        default = getattr(defaults, name)
        if value is None:
            values[name] = default
        elif isinstance(default, bool):
            values[name] = _flag(value, where, default)
        elif isinstance(default, int):
            values[name] = _integer(value, where)
        elif isinstance(default, float):
            values[name] = _number(value, where)
        else:
            values[name] = _text(str(value), where)
    return Typography(**values)


def _parse_output(raw: Mapping[str, Any], project_root: Path) -> Output:
    section = _mapping(_required(raw, "output", "manifest"), "manifest.output")
    _reject_unknown(
        section,
        ("directory", "jobname", "engine", "engine_runs", "min_pdf_bytes", "emit_markdown"),
        "manifest.output",
    )
    directory = None
    if section.get("directory") is not None:
        directory = _path(section["directory"], project_root, "manifest.output.directory")
    else:
        directory = output_dir_for(project_root)
    return Output(
        directory=directory,
        jobname=_text(_required(section, "jobname", "manifest.output"), "manifest.output.jobname"),
        engine=_optional_text(section.get("engine"), "manifest.output.engine", "xelatex"),
        engine_runs=_integer(section.get("engine_runs", 2), "manifest.output.engine_runs", 1),
        min_pdf_bytes=_integer(
            section.get("min_pdf_bytes", 20000), "manifest.output.min_pdf_bytes", 0
        ),
        emit_markdown=_flag(section.get("emit_markdown"), "manifest.output.emit_markdown", True),
    )


def _parse_vision(raw: Any) -> Optional[VisionConfig]:
    if raw is None:
        return None
    section = _mapping(raw, "manifest.vision")
    _reject_unknown(
        section,
        ("base_url", "model", "key_env", "batch_size", "max_tokens"),
        "manifest.vision",
    )
    defaults = VisionConfig()
    batch_size = section.get("batch_size")
    max_tokens = section.get("max_tokens")
    return VisionConfig(
        base_url=_optional_text(section.get("base_url"), "manifest.vision.base_url"),
        model=_optional_text(section.get("model"), "manifest.vision.model"),
        key_env=_optional_text(
            section.get("key_env"), "manifest.vision.key_env", defaults.key_env
        ),
        batch_size=(
            None
            if batch_size is None
            else _integer(batch_size, "manifest.vision.batch_size", 1)
        ),
        max_tokens=(
            None
            if max_tokens is None
            else _integer(max_tokens, "manifest.vision.max_tokens", 1)
        ),
    )


def _parse_schemes(raw: Any) -> Mapping[str, HeadingScheme]:
    if raw is None:
        return {}
    schemes = {}
    for name, value in _mapping(raw, "manifest.heading_schemes").items():
        where = f"manifest.heading_schemes.{name}"
        section = _mapping(value, where)
        _reject_unknown(section, ("rules", "replacements"), where)
        schemes[str(name)] = HeadingScheme(
            name=str(name),
            rules=_parse_rules(section.get("rules"), where),
            replacements=_pairs(section.get("replacements"), f"{where}.replacements"),
        )
    return schemes


def _parse_rules(raw: Any, where: str) -> Tuple[HeadingRule, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ManifestError(f"{where}.rules must be a list")
    rules = []
    for index, item in enumerate(raw):
        at = f"{where}.rules[{index}]"
        section = _mapping(item, at)
        _reject_unknown(section, ("match", "template", "numbers", "offsets"), at)
        pattern = _compile(_required(section, "match", at), f"{at}.match")
        template = _text(_required(section, "template", at), f"{at}.template")
        numbers = _atoms(section.get("numbers"), f"{at}.numbers")
        offsets_raw = section.get("offsets") or {}
        offsets = {
            str(key): _integer(value, f"{at}.offsets.{key}")
            for key, value in _mapping(offsets_raw, f"{at}.offsets").items()
        }
        for group in numbers:
            if not group.startswith("g") or not group[1:].isdigit():
                raise ManifestError(f"{at}.numbers entries must look like g1")
            if int(group[1:]) > pattern.groups:
                raise ManifestError(f"{at}.numbers refers to a capture group the pattern lacks")
        for group in offsets:
            if group not in numbers:
                raise ManifestError(f"{at}.offsets.{group} has no matching entry in numbers")
        for placeholder in _HEADING_PLACEHOLDER.findall(template):
            if not placeholder.startswith("g") or not placeholder[1:].isdigit():
                raise ManifestError(f"{at}.template placeholder {{{placeholder}}} is not a capture group")
            if int(placeholder[1:]) > pattern.groups:
                raise ManifestError(f"{at}.template refers to a capture group the pattern lacks")
        rules.append(HeadingRule(pattern=pattern, template=template, numbers=numbers, offsets=offsets))
    return tuple(rules)


def _compile(pattern: str, where: str) -> re.Pattern:
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise ManifestError(f"{where} is not a valid regular expression: {exc}") from exc


def _compile_patterns(raw: Any, where: str) -> Tuple[re.Pattern, ...]:
    return tuple(_compile(item, where) for item in _atoms(raw, where))


def _parse_volumes(
    raw: Mapping[str, Any], root: Path, schemes: Mapping[str, HeadingScheme]
) -> Tuple[VolumeSpec, ...]:
    items = _required(raw, "volumes", "manifest")
    if not isinstance(items, list) or not items:
        raise ManifestError("manifest.volumes must be a non-empty list")
    volumes = []
    seen = set()
    for index, item in enumerate(items):
        where = f"manifest.volumes[{index}]"
        section = _mapping(item, where)
        _reject_unknown(section, _VOLUME_KEYS, where)
        volume_id = _text(_required(section, "id", where), f"{where}.id")
        if volume_id in seen:
            raise ManifestError(f"duplicate volume id: {volume_id}")
        if volume_id == BIBLIOGRAPHY_KEY:
            raise ManifestError(f"volume id {BIBLIOGRAPHY_KEY} is reserved")
        seen.add(volume_id)
        citation_mode = _optional_text(
            section.get("citation_mode"), f"{where}.citation_mode", "global"
        )
        if citation_mode not in CITATION_MODES:
            raise ManifestError(
                f"{where}.citation_mode must be one of: {', '.join(CITATION_MODES)}"
            )
        volumes.append(
            VolumeSpec(
                id=volume_id,
                title=_text(_required(section, "title", where), f"{where}.title"),
                sources=_parse_sources(section, where, root, schemes),
                standalone=_flag(section.get("standalone"), f"{where}.standalone", False),
                part_label=section.get("part_label"),
                citation_mode=citation_mode,
                appendix=_flag(section.get("appendix"), f"{where}.appendix", False),
            )
        )
    return tuple(volumes)


def _parse_sources(
    section: Mapping[str, Any], where: str, root: Path, schemes: Mapping[str, HeadingScheme]
) -> Tuple[SourceSpec, ...]:
    items = _required(section, "sources", where)
    if not isinstance(items, list) or not items:
        raise ManifestError(f"{where}.sources must be a non-empty list")
    sources = []
    for index, item in enumerate(items):
        at = f"{where}.sources[{index}]"
        entry = _mapping(item, at)
        _reject_unknown(entry, _SOURCE_KEYS, at)
        path = _path(_required(entry, "file", at), root, f"{at}.file")
        if not path.is_file():
            raise ManifestError(f"{at}.file is not readable: {path}")
        scheme = entry.get("scheme")
        if scheme is not None:
            scheme = str(scheme)
            if scheme not in schemes:
                raise ManifestError(f"{at}.scheme is unknown: {scheme}")
        sources.append(
            SourceSpec(
                path=path,
                scheme=scheme,
                banner=entry.get("banner"),
            )
        )
    return tuple(sources)


def _parse_front_matter(raw: Any) -> FrontMatter:
    if raw is None:
        return FrontMatter()
    section = _mapping(raw, "manifest.front_matter")
    _reject_unknown(
        section, ("foreword_heading", "foreword", "catalog_heading", "appendix_catalog"),
        "manifest.front_matter",
    )
    entries = []
    for index, item in enumerate(section.get("appendix_catalog") or []):
        at = f"manifest.front_matter.appendix_catalog[{index}]"
        entry = _mapping(item, at)
        _reject_unknown(entry, ("title", "label"), at)
        entries.append(
            CatalogEntry(
                title=_text(_required(entry, "title", at), f"{at}.title"),
                label=_text(_required(entry, "label", at), f"{at}.label"),
            )
        )
    return FrontMatter(
        foreword_heading=_optional_text(
            section.get("foreword_heading"), "manifest.front_matter.foreword_heading", "凡　例"
        ),
        foreword=_atoms(section.get("foreword"), "manifest.front_matter.foreword"),
        catalog_heading=_optional_text(
            section.get("catalog_heading"), "manifest.front_matter.catalog_heading", "附录目录"
        ),
        appendix_catalog=tuple(entries),
    )
