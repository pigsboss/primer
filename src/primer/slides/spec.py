# -*- coding: utf-8 -*-
"""幻灯片输入规格（deck spec）：``<deck 目录>/deck.yaml`` 的数据结构、载入与校验。

规格文件是**唯一信息源**：它写什么，幻灯片的结构就是什么。幻灯片不再读成书的清单与产物
（``_primer/book/`` 下的 ``.toc``／``.log``／``.lof``）——章、节的身份与插图都由源 markdown
按这里的规则直接复算（见 :mod:`primer.slides.structure`），于是"书没重编"不再挡住幻灯片，
"书重编后页码变了"也不再让每一枚指针作废。

规格是**人写**的，``outline`` 从不覆盖它。校验分四层，任何一层不过就抛 :class:`DeckSpecError`：

* **未知键**：每一层都列出允许键，多一个字就报错。拼错的字段名被静默忽略时，规格看起来
  生效了、实际用的是默认值，这是最难查的一类错。
* **必填键**：缺失时点名 ``<路径>.<键>``。
* **取值**：``pointers`` 只能是 ``section``／``none``，标题层级是 1–6 的整数且章在节之上，
  ``sources[].id`` 不重复，``chapter_start`` 是十进制整数（正常篇）或非空字母号（附录篇）。
* **文件**：``source_root`` 与每个源文件都必须读得到；源文件里复算不出章时由
  :mod:`primer.slides.structure` 报错。

异常消息与 ``code`` 一律英文，人读报告是中文。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Mapping, Optional, Sequence, Tuple, Union

import yaml

__all__ = [
    "DECK_SPEC_NAME",
    "POINTER_MODES",
    "SOURCE_ROOT_DEFAULT",
    "DeckSpec",
    "Numbering",
    "SourceSpec",
    "Split",
    "DeckSpecError",
    "find_spec",
    "load_spec",
    "parse_spec",
]

DECK_SPEC_NAME = "deck.yaml"
SOURCE_ROOT_DEFAULT = "."
# 指针模式：``section`` 发节号指针（``§7.4``），``none`` 干脆不发指针列与页脚指针。
POINTER_MODES = ("section", "none")
# 规格文件自己的版本，留给二期改格式时辨认；这一期只有 1。
SPEC_VERSION = 1


class DeckSpecError(Exception):
    """幻灯片输入规格缺失或非法。"""


# ---------------------------------------------------------------- 字段解析


def _mapping(value: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise DeckSpecError(f"{where} must be a mapping")
    return value


def _reject_unknown(value: Mapping[str, Any], allowed: Sequence[str], where: str) -> None:
    unknown = sorted(str(key) for key in set(value) - set(allowed))
    if unknown:
        raise DeckSpecError(f"{where} has unknown key(s): {', '.join(unknown)}")


def _required(value: Mapping[str, Any], key: str, where: str) -> Any:
    if value.get(key) is None:
        raise DeckSpecError(f"{where}.{key} is required")
    return value[key]


def _text(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DeckSpecError(f"{where} must be a non-empty string")
    return value


def _optional_text(value: Any, where: str, default: str = "") -> str:
    if value is None:
        return default
    if not isinstance(value, str):
        raise DeckSpecError(f"{where} must be a string")
    return value


def _integer(value: Any, where: str, minimum: Optional[int] = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DeckSpecError(f"{where} must be an integer")
    if minimum is not None and value < minimum:
        raise DeckSpecError(f"{where} must be >= {minimum}")
    return value


def _string_list(value: Any, where: str) -> Tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise DeckSpecError(f"{where} must be a list of strings")
    return tuple(_text(item, f"{where}[{index}]") for index, item in enumerate(value))


# ---------------------------------------------------------------- 值对象


@dataclass(frozen=True)
class Numbering:
    """章号与章标签的写法。

    ``label`` 是正常章的纸面写法，占位符：``{n}`` 十进制章号、``{cn}`` 同一号的中文数字。
    ``appendix_label`` 是附录的写法，``{number}`` 是附录短号（``sources[].chapter_start``）。
    """

    label: str = "第{cn}章"
    appendix_label: str = "附录 {number}"

    def chapter_label(self, number: int, chinese: str) -> str:
        return self.label.replace("{cn}", chinese).replace("{n}", str(number))

    def appendix_name(self, number: str) -> str:
        return self.appendix_label.replace("{number}", number)


@dataclass(frozen=True)
class Split:
    """标题层级：``chapter`` 那一级的标题开一章，``section`` 那一级的标题开一节。

    附录篇例外：整篇是一章，篇题是源文件的一级标题，所以它的节从 ``chapter`` 那一级起算
    （篇内 ``## `` 是节、``### `` 是小节，整体下沉一级——与成书把附录篇题做成 ``\\chapter``
    是同一件事）。
    """

    chapter: int = 2
    section: int = 3


@dataclass(frozen=True)
class SourceSpec:
    """一篇源材料：id（卷键）、篇名、源 markdown 路径、首章号、是不是附录篇。

    ``path`` 一律相对 ``DeckSpec.source_root`` 记；``chapter_start`` 正常篇是十进制整数
    （该篇第一章的章号），附录篇是附录短号（如 ``A``）。
    """

    id: str
    title: str
    path: str
    chapter_start: Union[int, str]
    appendix: bool = False

    @property
    def first_number(self) -> str:
        """这一篇第一章的**短号**（``"1"``／``"A"``），给章号与 id 用。"""
        return self.chapter_start if isinstance(self.chapter_start, str) else str(self.chapter_start)

    def number_at(self, ordinal: int) -> str:
        """篇内第 ``ordinal``（从 0 数）章的短号。"""
        if self.appendix:
            return str(self.chapter_start)
        return str(int(self.chapter_start) + ordinal)


@dataclass(frozen=True)
class DeckSpec:
    """一份校验过的幻灯片输入规格；``path`` 是它来自哪个文件（不进 YAML）。"""

    title: str
    subtitle: str
    institution: str
    source_root: str
    sources: Tuple[SourceSpec, ...]
    drop_lines: Tuple[str, ...]
    pointers: str
    split: Split
    numbering: Numbering
    path: Path
    # 工程根：``source_root``、源文件路径与产物路径都相对它解析（载入时确定，不进 YAML）。
    root: Path = Path(".")
    version: int = SPEC_VERSION

    @property
    def book_line(self) -> str:
        """封面上一行的书目（书名 + 副题），与成书同一个写法。"""
        return f"{self.title}{self.subtitle}"

    def source(self, source_id: str) -> SourceSpec:
        for entry in self.sources:
            if entry.id == source_id:
                return entry
        raise DeckSpecError(f"unknown source id: {source_id}")


# ---------------------------------------------------------------- 各段落


def _parse_book(value: Any) -> Tuple[str, str, str]:
    where = "book"
    mapping = _mapping(value, where)
    _reject_unknown(mapping, ("title", "subtitle", "institution"), where)
    return (
        _text(_required(mapping, "title", where), f"{where}.title"),
        _optional_text(mapping.get("subtitle"), f"{where}.subtitle"),
        _optional_text(mapping.get("institution"), f"{where}.institution"),
    )


def _parse_split(value: Any) -> Split:
    where = "split"
    if value is None:
        return Split()
    mapping = _mapping(value, where)
    _reject_unknown(mapping, ("chapter", "section"), where)
    chapter = _integer(mapping.get("chapter", 2), f"{where}.chapter", 1)
    section = _integer(mapping.get("section", 3), f"{where}.section", 1)
    if chapter > 6 or section > 6:
        raise DeckSpecError(f"{where} heading levels must be at most 6")
    if section <= chapter:
        raise DeckSpecError(
            f"{where}.section ({section}) must be deeper than {where}.chapter ({chapter})"
        )
    return Split(chapter=chapter, section=section)


def _parse_numbering(value: Any) -> Numbering:
    where = "numbering"
    if value is None:
        return Numbering()
    mapping = _mapping(value, where)
    _reject_unknown(mapping, ("label", "appendix_label"), where)
    numbering = Numbering(
        label=_optional_text(mapping.get("label"), f"{where}.label", Numbering.label),
        appendix_label=_optional_text(
            mapping.get("appendix_label"), f"{where}.appendix_label", Numbering.appendix_label
        ),
    )
    if "{cn}" not in numbering.label and "{n}" not in numbering.label:
        raise DeckSpecError(f"{where}.label must use {{n}} or {{cn}} for the chapter number")
    if "{number}" not in numbering.appendix_label:
        raise DeckSpecError(f"{where}.appendix_label must use {{number}} for the appendix number")
    return numbering


def _parse_source(value: Any, index: int, seen: List[str]) -> SourceSpec:
    where = f"sources[{index}]"
    mapping = _mapping(value, where)
    _reject_unknown(
        mapping, ("id", "title", "path", "chapter_start", "appendix"), where
    )
    source_id = _text(_required(mapping, "id", where), f"{where}.id")
    if source_id in seen:
        raise DeckSpecError(f"duplicate source id: {source_id}")
    appendix = mapping.get("appendix", False)
    if not isinstance(appendix, bool):
        raise DeckSpecError(f"{where}.appendix must be a boolean")
    raw_start = _required(mapping, "chapter_start", where)
    if appendix:
        if not isinstance(raw_start, str) or not raw_start.strip():
            raise DeckSpecError(
                f"{where}.chapter_start must be the appendix number (a non-empty string) "
                "for an appendix source"
            )
        start: Union[int, str] = raw_start.strip()
    else:
        start = _integer(raw_start, f"{where}.chapter_start", 1)
    return SourceSpec(
        id=source_id,
        title=_text(_required(mapping, "title", where), f"{where}.title"),
        path=_text(_required(mapping, "path", where), f"{where}.path"),
        chapter_start=start,
        appendix=appendix,
    )


def _check_files(spec: DeckSpec, base: Path) -> None:
    if not base.is_dir():
        raise DeckSpecError(f"source_root is not a directory: {base}")
    for index, entry in enumerate(spec.sources):
        path = base / entry.path
        if not path.is_file():
            raise DeckSpecError(f"sources[{index}].path not found: {path}")


def parse_spec(document: Any, path: Path, base: Path) -> DeckSpec:
    """把已载入的 YAML 文档解析成 :class:`DeckSpec`，任何一处不对就抛错。

    ``base`` 是 ``source_root`` 解析的基准（工程根），用来核对每个源文件真的读得到。
    """
    where = "spec"
    mapping = _mapping(document, where)
    _reject_unknown(
        mapping,
        ("version", "book", "source_root", "sources", "drop_lines", "pointers", "split", "numbering"),
        where,
    )
    version = _integer(mapping.get("version", SPEC_VERSION), f"{where}.version", 1)
    title, subtitle, institution = _parse_book(_required(mapping, "book", where))
    source_root = _optional_text(
        mapping.get("source_root"), f"{where}.source_root", SOURCE_ROOT_DEFAULT
    ) or SOURCE_ROOT_DEFAULT
    pointers = _optional_text(mapping.get("pointers"), f"{where}.pointers", "section")
    if pointers not in POINTER_MODES:
        raise DeckSpecError(f"{where}.pointers must be one of: {', '.join(POINTER_MODES)}")

    raw_sources = _required(mapping, "sources", where)
    if not isinstance(raw_sources, list) or not raw_sources:
        raise DeckSpecError(f"{where}.sources must be a non-empty list")
    seen: List[str] = []
    sources: List[SourceSpec] = []
    for index, item in enumerate(raw_sources):
        source = _parse_source(item, index, seen)
        seen.append(source.id)
        sources.append(source)

    spec = DeckSpec(
        title=title,
        subtitle=subtitle,
        institution=institution,
        source_root=source_root,
        sources=tuple(sources),
        drop_lines=_string_list(mapping.get("drop_lines"), f"{where}.drop_lines"),
        pointers=pointers,
        split=_parse_split(mapping.get("split")),
        numbering=_parse_numbering(mapping.get("numbering")),
        path=Path(path),
        root=Path(base).expanduser().resolve(),
        version=version,
    )
    _check_files(spec, base / source_root)
    return spec


def load_spec(path: Path, base: Path) -> DeckSpec:
    """读取并校验一份 ``deck.yaml``；出错时报 **``<文件>: <路径>``**。"""
    if not Path(path).is_file():
        raise DeckSpecError(f"deck spec not found: {path}")
    try:
        document = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise DeckSpecError(f"deck spec is not valid YAML: {path}: {exc}") from exc
    try:
        return parse_spec(document, Path(path), Path(base))
    except DeckSpecError as exc:
        raise DeckSpecError(f"{path}: {exc}") from exc


def find_spec(project_root: Path, deck: str) -> Path:
    """``<工程根>/_primer/slides/<deck>/deck.yaml``——产物目录里那一份规格。"""
    return Path(project_root) / "_primer" / "slides" / deck / DECK_SPEC_NAME
