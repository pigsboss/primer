# -*- coding: utf-8 -*-
"""语料编目：递归发现 PDF、流式取内容 md5、去重，并挂接引文元数据。

引文按优先级两路补齐：

1. ``reflib_log`` CSV（``参考资料/reflib_download_log.csv``，含
   ``ref,class,title,arxiv,status,saved`` 列），按 ``saved`` 的绝对路径建索引；
2. 文件名解析，支持 ``020_A_Ice_Giants_....pdf``（ref/class/title）与
   ``MISC_R_arxiv2206_06693.pdf``（class/arxiv）两种命名；另外任意文件名中
   出现 ``arxiv<4位年>[._-]<4–5位序号>`` 时也会给出规范化 arXiv 号。

与主题 ``文献清单/*.csv`` 的模糊标题匹配不做，留待后续。
"""

from __future__ import annotations

import csv
import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Sequence

from .paths import relative_to_root

CHUNK_SIZE = 1 << 16
_SKIP_DIRS = frozenset({"__pycache__"})

# 文件名解析的两条模式：先试 arXiv 尾缀，再试 ref/class/title。
_ARXIV_SUFFIX = re.compile(r"^(?P<ref>[A-Za-z0-9]+)_(?P<cls>[A-Z])_arxiv(?P<year>\d{4})[._](?P<num>\d{4,5})$")
_REF_NAME = re.compile(r"^(?P<ref>\d{3})_(?P<cls>[A-Z])_(?P<title>.+)$")
_ARXIV_VALUE = re.compile(r"^\d{4}\.\d{4,5}$")
# 兜底模式：arXiv 号出现在文件名的任意位置，如 ``paper_arxiv1301_6674_v2.pdf``。
_ARXIV_ANYWHERE = re.compile(r"arxiv[_-]?(?P<year>\d{4})[._-](?P<num>\d{4,5})", re.IGNORECASE)
_ARXIV_REF_PREFIX = re.compile(r"^(?P<ref>\d{3})_(?P<cls>[A-Z])_")


@dataclass(frozen=True)
class Citation:
    """一条文献的引用元数据。``source`` 说明来自账本还是文件名。"""

    ref: Optional[str] = None
    cls: Optional[str] = None
    title: Optional[str] = None
    arxiv: Optional[str] = None
    source: str = "filename"


@dataclass
class DocRecord:
    """一个 PDF 文件的编目记录。"""

    path: Path
    rel_path: str
    size: int
    md5: str
    duplicate_of: Optional[Path] = None
    citation: Optional[Citation] = None


@dataclass(frozen=True)
class UnreadableFile:
    """扫描时读不动的文件。

    它进不了 :class:`DocRecord`：账本按键于内容 md5，而这里恰恰是算不出 md5 的那些。
    所以只留下路径与原因，由扫描报告点名，等它变回可读时下一轮自然会被收进来。
    """

    path: Path
    reason: str


@dataclass
class Catalog:
    """一次扫描得到的全部记录，以及若干派生视图。

    ``project_root`` 只用于记录路径：``records[].rel_path`` 与报告里的各个根目录
    都相对它书写，工程被搬移后产物仍然可读。``unreadable`` 是扫描当时读不动的
    文件（一般是语料正被别的进程编辑），它们不进 ``records``，但必须在报告里点名。
    """

    roots: list[Path]
    records: list[DocRecord]
    project_root: Optional[Path] = None
    unreadable: list[UnreadableFile] = field(default_factory=list)

    @property
    def unique(self) -> list[DocRecord]:
        """每个内容 md5 的首次出现（未标记为重复的记录）。"""
        return [record for record in self.records if record.duplicate_of is None]

    @property
    def duplicates(self) -> list[DocRecord]:
        """内容重复、被标记了 ``duplicate_of`` 的记录。"""
        return [record for record in self.records if record.duplicate_of is not None]

    @property
    def total_bytes(self) -> int:
        return sum(record.size for record in self.records)

    @property
    def unique_bytes(self) -> int:
        return sum(record.size for record in self.unique)

    @property
    def redundant_bytes(self) -> int:
        """去重后可省下的字节数。"""
        return self.total_bytes - self.unique_bytes

    def duplicate_groups_map(self) -> dict[str, list[DocRecord]]:
        """按 md5 分组，只保留组内多于一个文件的组。"""
        groups: dict[str, list[DocRecord]] = {}
        for record in self.records:
            if record.md5:
                groups.setdefault(record.md5, []).append(record)
        return {md5: group for md5, group in groups.items() if len(group) > 1}

    @property
    def duplicate_groups(self) -> int:
        return len(self.duplicate_groups_map())

    def by_root(self) -> dict[Path, list[DocRecord]]:
        """按根目录分组；归属取第一个包含该路径的根。"""
        groups: dict[Path, list[DocRecord]] = {root: [] for root in self.roots}
        for record in self.records:
            groups[_root_for(record.path, self.roots)].append(record)
        return groups


def iter_pdf_paths(roots: Sequence[Path]) -> list[Path]:
    """递归列出各根目录下的 PDF（扩展名不区分大小写），去重后按路径排序。

    跳过点目录与 ``__pycache__``；根目录重叠时同一文件只保留一次。
    """
    seen: set[str] = set()
    found: list[Path] = []
    for root in roots:
        root = Path(root)
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [
                name
                for name in dirnames
                if not name.startswith(".") and name not in _SKIP_DIRS
            ]
            for name in filenames:
                if not name.lower().endswith(".pdf"):
                    continue
                path = Path(dirpath) / name
                key = os.path.realpath(path)
                if key in seen:
                    continue
                seen.add(key)
                found.append(path)
    found.sort(key=lambda item: str(item))
    return found


def file_md5(path: Path, chunk_size: int = CHUNK_SIZE) -> str:
    """流式计算文件 md5，始终分块读取，不把整份文件读进内存。"""
    digest = hashlib.md5()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_reflib_log(path: Path) -> dict[str, Citation]:
    """读取 reflib 下载账本 CSV，按 ``saved`` 绝对路径索引引文。"""
    index: dict[str, Citation] = {}
    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            saved = (row.get("saved") or "").strip()
            if not saved:
                continue
            arxiv = (row.get("arxiv") or "").strip()
            title = (row.get("title") or "").strip()
            ref = (row.get("ref") or "").strip()
            cls = (row.get("class") or "").strip()
            index[os.path.realpath(saved)] = Citation(
                ref=ref or None,
                cls=cls or None,
                title=title or None,
                arxiv=arxiv if _ARXIV_VALUE.match(arxiv) else None,
                source="reflib_log",
            )
    return index


def normalize_arxiv(value: Optional[str]) -> Optional[str]:
    """把 ``arXiv:0705.0993v2``、``0705_0993`` 等写法规范成 ``0705.0993``。

    无法识别为 arXiv 号（形如 ``XXXX.XXXXX``）时返回 ``None``。
    """
    if not value:
        return None
    token = value.strip()
    token = re.sub(r"^arxiv[:\s]*", "", token, flags=re.IGNORECASE)
    token = re.sub(r"v\d+$", "", token.strip())
    match = re.fullmatch(r"(\d{4})[._](\d{4,5})", token.strip(" .,;"))
    return f"{match.group(1)}.{match.group(2)}" if match else None


def parse_filename(name: str) -> Optional[Citation]:
    """从文件名解析引文；两种已知命名之外一律返回 ``None``。

    额外的兜底：文件名任意位置出现 ``arxiv<4位年>[._-]<4–5位序号>`` 时，
    也给出规范化的 arXiv 号（若文件名同样以 ``NNN_X_`` 开头，则一并给出 ref/class）。
    """
    stem = Path(name).stem
    arxiv_match = _ARXIV_SUFFIX.match(stem)
    if arxiv_match:
        ref = arxiv_match.group("ref")
        return Citation(
            ref=ref if ref.isdigit() else None,
            cls=arxiv_match.group("cls"),
            title=None,
            arxiv=f"{arxiv_match.group('year')}.{arxiv_match.group('num')}",
            source="filename",
        )
    ref_match = _REF_NAME.match(stem)
    anywhere = _ARXIV_ANYWHERE.search(stem)
    if anywhere:
        prefix = _ARXIV_REF_PREFIX.match(stem)
        return Citation(
            ref=prefix.group("ref") if prefix else (ref_match.group("ref") if ref_match else None),
            cls=prefix.group("cls") if prefix else (ref_match.group("cls") if ref_match else None),
            title=None,
            arxiv=f"{anywhere.group('year')}.{anywhere.group('num')}",
            source="filename",
        )
    if ref_match:
        title = ref_match.group("title").replace("_", " ").strip()
        return Citation(
            ref=ref_match.group("ref"),
            cls=ref_match.group("cls"),
            title=title or None,
            arxiv=None,
            source="filename",
        )
    return None


def build_catalog(
    roots: Sequence[Path],
    *,
    project_root: Optional[Path] = None,
    reflib_log: Optional[Path] = None,
    on_progress: Optional[Callable[[int, int, Path], None]] = None,
    hash_files: bool = True,
) -> Catalog:
    """扫描 ``roots`` 生成 :class:`Catalog`。

    ``hash_files=False`` 时跳过 md5（``--no-hash`` 的快速计数扫描），此时不做去重。
    ``on_progress(done, total, path)`` 在每个文件处理完后调用，供命令行报告进度。
    ``project_root`` 用于把 ``rel_path`` 记成相对工程根的形式。

    语料可能是别人正在编辑的目录，因此扫描**逐份隔离失败**：某个文件在列目录之后、
    取哈希之前消失或变得不可读，就把它记进 ``Catalog.unreadable`` 并跳过，而不是让
    整轮扫描炸掉——扫描阶段的崩溃与暂存阶段的崩溃一样，都会白白废掉几个小时。
    """
    root_paths = [Path(root) for root in roots]
    paths = iter_pdf_paths(root_paths)
    citations = load_reflib_log(reflib_log) if reflib_log is not None else {}
    total = len(paths)
    first_by_md5: dict[str, Path] = {}
    records: list[DocRecord] = []
    unreadable: list[UnreadableFile] = []
    for done, path in enumerate(paths, start=1):
        try:
            size = path.stat().st_size
            md5 = file_md5(path) if hash_files else ""
        except OSError as exc:
            unreadable.append(UnreadableFile(path=path, reason=_unreadable_reason(exc)))
            if on_progress is not None:
                on_progress(done, total, path)
            continue
        citation = citations.get(os.path.realpath(path)) or parse_filename(path.name)
        duplicate_of = None
        if md5:
            duplicate_of = first_by_md5.setdefault(md5, path)
            if duplicate_of == path:
                duplicate_of = None
        records.append(
            DocRecord(
                path=path,
                rel_path=_rel_path(path, root_paths, project_root),
                size=size,
                md5=md5,
                duplicate_of=duplicate_of,
                citation=citation,
            )
        )
        if on_progress is not None:
            on_progress(done, total, path)
    return Catalog(
        roots=root_paths, records=records, project_root=project_root, unreadable=unreadable
    )


def _unreadable_reason(exc: OSError) -> str:
    """把扫描期的读取失败翻成一句人读原因。"""
    if isinstance(exc, FileNotFoundError):
        return "vanished while scanning"
    if isinstance(exc, PermissionError):
        return "permission denied while scanning"
    return f"unreadable while scanning: {exc.strerror or exc}"


def _rel_path(path: Path, roots: Sequence[Path], project_root: Optional[Path]) -> str:
    """记录用的相对路径：优先相对工程根，工程根之外退回相对第一个根。

    ``tier_rules`` 的 glob 是拿 ``rel_path`` 匹配的，相对哪个根都行，但报告的
    "Source" 行要能被工程搬移后仍然读懂，所以工程根在时一律以它为准。
    """
    if project_root is not None:
        relative = relative_to_root(path, project_root)
        if not Path(relative).is_absolute():
            return relative
    return _rel_to_first_root(path, roots)


def _rel_to_first_root(path: Path, roots: Sequence[Path]) -> str:
    for root in roots:
        try:
            return str(path.relative_to(root))
        except ValueError:
            continue
    return path.name


def _root_for(path: Path, roots: Sequence[Path]) -> Path:
    for root in roots:
        try:
            path.relative_to(root)
        except ValueError:
            continue
        return root
    return roots[0] if roots else Path("")
