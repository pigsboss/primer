# -*- coding: utf-8 -*-
"""结果后处理：解包归档、重写图片链接、加引文头，写出扁平 markdown。

MinerU 的 ``<stem>.zip`` 里是 ``markdown.md``、``middle_json.json``、
``structured_content.json``、``model_output.json`` 与 ``images/...``。本模块把
它解到 ``<raw_dir>/<stem>/``，再把 markdown 整理成 ``<flat_dir>/<stem>.md``：

* 图片链接从归档内的相对路径（``images/...``）改写成相对**扁平文件**可解析的
  路径，正文一字不改；
* 文件开头加一段引用块（引文 + 来源 + 页数），块后跟一个空行；
* ``model_output.json`` 占输出的大头（实测约 419 KB / 12 页），默认删掉并在
  账本里留档它的字节数。

写扁平文件的动作是纯函数式的（先剥掉已有引文头，再由干净的正文重新拼），
因此对同一个文件重复后处理不会出现两个引文头或重复正文。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .catalog import DocRecord

MARKDOWN_NAME = "markdown.md"
MIDDLE_JSON_NAME = "middle_json.json"
STRUCTURED_JSON_NAME = "structured_content.json"
MODEL_OUTPUT_NAME = "model_output.json"
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff", ".svg"})

# 引文头首行的固定前缀，既是人读标识，也是幂等重写时的识别标记。
HEADER_MARK = "> **Citation**"
_MARKDOWN_TARGET = re.compile(r"(?P<prefix>\]\()(?P<target>[^)\s]+)(?P<suffix>(?:\s+\"[^\"]*\")?\))")
_HTML_TARGET = re.compile(r"(?P<prefix>\b(?:src|href)=\")(?P<target>[^\"]*)(?P<suffix>\")")
_EXTERNAL_SCHEMES = ("http://", "https://", "ftp://", "data:", "mailto:", "file:")


@dataclass(frozen=True)
class PostprocessResult:
    """一个文档后处理完之后落盘的全部产物。"""

    flat_path: Path
    doc_dir: Path
    stem: str
    pages: Optional[int]
    images: int
    archive_bytes: int
    doc_dir_bytes: int
    flat_bytes: int
    dropped_bytes: int

    @property
    def total_bytes(self) -> int:
        """该文档当前占用的字节数（归档 + 解包目录 + 扁平 markdown）。"""
        return self.archive_bytes + self.doc_dir_bytes + self.flat_bytes

    @property
    def total_with_model_output(self) -> int:
        """若保留 ``model_output.json``，该文档将占用的字节数。"""
        return self.total_bytes + self.dropped_bytes


def unpack_archive(archive: Path, dest: Path, *, replace_existing: bool = False) -> list[Path]:
    """把 ``archive`` 解到 ``dest``，返回写出的文件列表。

    成员名先整体校验再落地：绝对路径、``..`` 段、驱动器盘符与符号链接成员一律
    拒绝（抛出 :class:`ValueError`），不使用 :meth:`zipfile.ZipFile.extractall`
    的静默清洗，以便 zip slip 直接暴露成错误而不是被悄悄改写。

    ``replace_existing`` 为真时先清掉 ``dest``：同一文件换档重跑会产出不同的
    素材集合（例如 flash 档不抽图），留着上一轮的残留文件会虚增目录体积、留下
    没人引用的孤儿图片。清空发生在成员校验之后，因此损坏或越界的归档不会破坏
    上一轮已有的结果。
    """
    written: list[Path] = []
    with zipfile.ZipFile(archive) as handle:
        targets = [(info, _member_target(info, dest)) for info in handle.infolist()]
        if replace_existing and Path(dest).exists():
            shutil.rmtree(dest)
        for info, target in targets:
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with handle.open(info) as source, open(target, "wb") as sink:
                shutil.copyfileobj(source, sink)
            written.append(target)
    return written


def render_citation_header(record: DocRecord, pages: Optional[int]) -> str:
    """渲染引文头：引用块若干行，末尾跟一个空行。

    字段取自 ``record.citation``（ref / class / title / arXiv），再补来源相对
    路径与解析页数；没有匹配到任何引文信息时只留来源与页数。
    """
    citation = record.citation
    parts: list[str] = []
    if citation is not None:
        if citation.ref:
            parts.append(f"ref {citation.ref}")
        if citation.cls:
            parts.append(f"class {citation.cls}")
    summary = " · ".join(parts) if parts else "no metadata matched"
    lines = [f"{HEADER_MARK} — {summary}"]
    if citation is not None and citation.title:
        lines.append(f"> **Title** — {citation.title}")
    if citation is not None and citation.arxiv:
        lines.append(f"> **arXiv** — {citation.arxiv}")
    lines.append(f"> **Source** — {record.rel_path}")
    lines.append(f"> **Pages** — {pages}" if pages is not None else "> **Pages** — unknown")
    return "\n".join(lines) + "\n\n"


def strip_citation_header(text: str) -> str:
    """剥掉开头的引文头（若有），保证重复后处理不会叠加。"""
    if not text.startswith(HEADER_MARK):
        return text
    lines = text.split("\n")
    index = 0
    while index < len(lines) and lines[index].startswith(">"):
        index += 1
    while index < len(lines) and not lines[index].strip():
        index += 1
    return "\n".join(lines[index:])


def rewrite_links(markdown: str, *, doc_dir: Path, flat_dir: Path) -> str:
    """把指向解包目录内文件的链接改写成相对扁平目录的路径。

    只改写真正落在 ``doc_dir`` 里、且磁盘上确实存在的相对链接；外链、锚点、
    绝对路径与指向其他位置的相对路径一律原样保留。
    """
    doc_root = Path(doc_dir).resolve()
    flat_root = Path(flat_dir).resolve()

    def replace(match: re.Match[str]) -> str:
        target = match.group("target")
        resolved = _resolve_target(target, doc_root, flat_root)
        if resolved is None:
            return match.group(0)
        return f"{match.group('prefix')}{resolved}{match.group('suffix')}"

    markdown = _MARKDOWN_TARGET.sub(replace, markdown)
    return _HTML_TARGET.sub(replace, markdown)


def page_count(doc_dir: Path) -> Optional[int]:
    """从 ``middle_json.json``（回退 ``structured_content.json``）读取页数。"""
    for name in (MIDDLE_JSON_NAME, STRUCTURED_JSON_NAME):
        path = Path(doc_dir) / name
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        pages = payload.get("pages") if isinstance(payload, dict) else None
        if isinstance(pages, list):
            return len(pages)
    return None


def postprocess_document(
    record: DocRecord,
    archive: Path,
    *,
    stem: str,
    raw_dir: Path,
    flat_dir: Path,
    keep_model_output: bool = False,
) -> PostprocessResult:
    """把 ``archive`` 整理成 ``<raw_dir>/<stem>/`` + ``<flat_dir>/<stem>.md``。"""
    archive = Path(archive)
    raw_dir = Path(raw_dir)
    flat_dir = Path(flat_dir)
    doc_dir = raw_dir / stem
    unpack_archive(archive, doc_dir, replace_existing=True)

    markdown_path = doc_dir / MARKDOWN_NAME
    if not markdown_path.is_file():
        raise ValueError(f"archive without {MARKDOWN_NAME}: {archive}")
    body = markdown_path.read_text(encoding="utf-8")
    pages = page_count(doc_dir)

    flat_path = flat_dir / f"{stem}.md"
    flat_dir.mkdir(parents=True, exist_ok=True)
    flat_path.write_text(
        _render_flat(record, body, pages, doc_dir=doc_dir, flat_dir=flat_dir),
        encoding="utf-8",
    )

    dropped = 0
    model_output = doc_dir / MODEL_OUTPUT_NAME
    if model_output.is_file() and not keep_model_output:
        dropped = model_output.stat().st_size
        model_output.unlink()

    return PostprocessResult(
        flat_path=flat_path,
        doc_dir=doc_dir,
        stem=stem,
        pages=pages,
        images=sum(1 for path in doc_dir.rglob("*") if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES),
        archive_bytes=archive.stat().st_size,
        doc_dir_bytes=sum(path.stat().st_size for path in doc_dir.rglob("*") if path.is_file()),
        flat_bytes=flat_path.stat().st_size,
        dropped_bytes=dropped,
    )


def _render_flat(
    record: DocRecord, body: str, pages: Optional[int], *, doc_dir: Path, flat_dir: Path
) -> str:
    cleaned = rewrite_links(strip_citation_header(body), doc_dir=doc_dir, flat_dir=flat_dir)
    return render_citation_header(record, pages) + cleaned


def _resolve_target(target: str, doc_root: Path, flat_root: Path) -> Optional[str]:
    """返回改写后的链接，或 ``None`` 表示保持原样。"""
    if not target or target.startswith(("#", "/", "<")):
        return None
    if any(target.startswith(scheme) for scheme in _EXTERNAL_SCHEMES):
        return None
    candidate = (doc_root / target).resolve()
    try:
        candidate.relative_to(doc_root)
    except ValueError:
        return None
    if not candidate.is_file():
        return None
    return Path(os.path.relpath(candidate, flat_root)).as_posix()


def _member_target(info: zipfile.ZipInfo, dest: Path) -> Path:
    """校验成员名并映射到目标路径，越界或符号链接成员直接拒绝。"""
    name = info.filename
    if not name or name.endswith("/"):
        name = name.rstrip("/")
    if not name:
        raise ValueError("zip member with an empty name")
    if name.startswith(("/", "\\")) or (len(name) > 1 and name[1] == ":"):
        raise ValueError(f"zip member with an absolute path: {info.filename}")
    parts = [part for part in re.split(r"[\\/]+", name) if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise ValueError(f"zip member escaping the destination: {info.filename}")
    if stat.S_ISLNK(info.external_attr >> 16):
        raise ValueError(f"zip member is a symbolic link: {info.filename}")
    return Path(dest).joinpath(*parts)
