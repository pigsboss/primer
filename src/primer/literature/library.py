# -*- coding: utf-8 -*-
"""文献库 JSON 文件的数据层：记录模型、校验、原子写与备份。

库文件是**用户资产**（不受"工程目录只读"约束）：顶层为记录数组，每笔记录含
标准书目字段、DOI、本地文件列表（每项自带文件性质）与项目路径列表（``/`` 分层的
字符串，界面只读）。本模块提供：

* :class:`Record` / :class:`FileEntry`：记录与文件条目的数据模型；**未知字段**
  （将来新增或外部工具写入的键）在读写往返中原样保留；
* :class:`Library`：载入、校验、增删改与保存；
* :data:`NATURES`：本地文件性质的允许取值。

写入策略：先生成同目录临时文件，再 ``os.replace`` 原子替换；替换前把现有文件
复制为 ``<库名>.bak``。载入时记下磁盘状态（mtime_ns＋大小），保存前复核——
库被外部改动过就拒绝覆盖，提示先重载。

错误一律抛 :class:`LibraryError`（:class:`ValueError` 子类；消息英文，见
CODING_STANDARDS v1.1），由命令行与本地服务统一转成中文提示。
"""

from __future__ import annotations

import json
import os
import shutil
import uuid as _uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Optional

# 本地文件性质的允许取值（界面徽章见《设计规格.md》§3.4）。
NATURES = (
    "doi-consistent",
    "preprint-substitute",
    "title-match",
    "auto-download",
    "manual-upload",
    "other",
)

_DOI_PREFIXES = (
    "https://doi.org/",
    "http://doi.org/",
    "https://dx.doi.org/",
    "http://dx.doi.org/",
)

# 有值才落盘的书目字段（biblatex 规范名；空值不写入库文件）。
_RECORD_TEXT_FIELDS = (
    "volume", "number", "pages", "eid", "publisher", "location",
    "institution", "organization", "series", "edition", "isbn", "issn",
    "url", "eprint", "eventtitle", "eventdate", "keywords", "download_url",
)
# 列表型书目字段（与 authors 同规：每项一个字符串）。
_RECORD_LIST_FIELDS = ("editor", "translator")

_RECORD_KNOWN = frozenset((
    "uuid", "type", "title", "authors", "year", "venue", "doi",
    "files", "projects", "notes", "created_at", "updated_at",
)) | frozenset(_RECORD_TEXT_FIELDS) | frozenset(_RECORD_LIST_FIELDS)
_FILE_KNOWN = frozenset(("path", "nature", "note"))

# 本地文件记录（与文献记录同库共存，kind="file" 判别）的状态。
FILE_STATUSES = ("pending", "parsing", "done", "failed", "downloaded")
_FILE_RECORD_KNOWN = frozenset((
    "kind", "uuid", "path", "name", "size", "md5", "status",
    "md_path", "doi", "eprint", "dup", "error",
    "record_uuid", "nature", "note", "added_at", "updated_at",
))

__all__ = [
    "FILE_STATUSES",
    "NATURES",
    "FileEntry",
    "FileRecord",
    "Library",
    "LibraryError",
    "Record",
    "now_iso",
]


class LibraryError(ValueError):
    """库文件与记录校验相关的错误；消息英文（见 CODING_STANDARDS v1.1）。"""


def now_iso() -> str:
    """当前本地时刻（秒级 ISO 8601，带时区）；记录时间戳统一用它。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass
class FileEntry:
    """记录下的一条本地文件：相对库文件的路径、性质与备注。

    ``extra`` 保存本条目的未知字段，序列化时原样写回。
    """

    path: str
    nature: str
    note: str = ""
    extra: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, payload: Any, where: str = "file") -> "FileEntry":
        if not isinstance(payload, Mapping):
            raise LibraryError(f"{where}: expected an object, got {_typename(payload)}")
        path = payload.get("path")
        if not isinstance(path, str) or not path.strip():
            raise LibraryError(f"{where}.path: expected non-empty string")
        nature = payload.get("nature")
        if nature not in NATURES:
            allowed = "/".join(NATURES)
            raise LibraryError(f"{where}.nature: expected one of {allowed}, got {nature!r}")
        note = payload.get("note") or ""
        if not isinstance(note, str):
            raise LibraryError(f"{where}.note: expected string")
        extra = {key: value for key, value in payload.items() if key not in _FILE_KNOWN}
        return cls(path=path.strip(), nature=nature, note=note, extra=extra)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"path": self.path, "nature": self.nature}
        if self.note:
            payload["note"] = self.note
        payload.update(self.extra)
        return payload


@dataclass
class Record:
    """一笔文献记录；字段与《设计规格.md》§3.2 对应，``extra`` 保留未知字段。

    书目字段（volume…keywords）采用 biblatex 规范名，**有值才落盘**。
    """

    uuid: str
    title: str
    type: str = ""
    authors: list[str] = field(default_factory=list)
    year: Optional[int] = None
    venue: str = ""
    doi: Optional[str] = None
    editor: list[str] = field(default_factory=list)
    translator: list[str] = field(default_factory=list)
    volume: str = ""
    number: str = ""
    pages: str = ""
    eid: str = ""
    publisher: str = ""
    location: str = ""
    institution: str = ""
    organization: str = ""
    series: str = ""
    edition: str = ""
    isbn: str = ""
    issn: str = ""
    url: str = ""
    eprint: str = ""
    eventtitle: str = ""
    eventdate: str = ""
    keywords: str = ""
    download_url: str = ""
    files: list[FileEntry] = field(default_factory=list)
    projects: list[str] = field(default_factory=list)
    notes: str = ""
    created_at: str = ""
    updated_at: str = ""
    extra: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, payload: Any, where: str = "record") -> "Record":
        if not isinstance(payload, Mapping):
            raise LibraryError(f"{where}: expected an object, got {_typename(payload)}")
        raw_uuid = payload.get("uuid")
        if not isinstance(raw_uuid, str) or not raw_uuid.strip():
            raise LibraryError(f"{where}.uuid: expected non-empty string")
        title = payload.get("title")
        if not isinstance(title, str) or not title.strip():
            raise LibraryError(f"{where}.title: expected non-empty string")
        type_text = _optional_string(payload.get("type"), f"{where}.type") or ""
        authors = _string_list(payload.get("authors"), f"{where}.authors")
        year = _year_value(payload.get("year"), f"{where}.year")
        venue = _optional_string(payload.get("venue"), f"{where}.venue") or ""
        doi = _normalize_doi(payload.get("doi"), f"{where}.doi")
        editor = _string_list(payload.get("editor"), f"{where}.editor")
        translator = _string_list(payload.get("translator"), f"{where}.translator")
        texts = {
            key: (_optional_string(payload.get(key), f"{where}.{key}") or "")
            for key in _RECORD_TEXT_FIELDS
        }
        files = _file_entries(payload.get("files"), f"{where}.files")
        projects = _project_list(payload.get("projects"), f"{where}.projects")
        notes = _optional_string(payload.get("notes"), f"{where}.notes") or ""
        created_at = _optional_string(payload.get("created_at"), f"{where}.created_at") or ""
        updated_at = _optional_string(payload.get("updated_at"), f"{where}.updated_at") or ""
        extra = {key: value for key, value in payload.items() if key not in _RECORD_KNOWN}
        return cls(
            uuid=raw_uuid.strip(),
            title=title,
            type=type_text,
            authors=authors,
            year=year,
            venue=venue,
            doi=doi,
            **texts,
            editor=editor,
            translator=translator,
            files=files,
            projects=projects,
            notes=notes,
            created_at=created_at,
            updated_at=updated_at,
            extra=extra,
        )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "uuid": self.uuid,
            "type": self.type,
            "title": self.title,
            "authors": list(self.authors),
            "year": self.year,
            "venue": self.venue,
            "doi": self.doi,
            "files": [entry.to_dict() for entry in self.files],
            "projects": list(self.projects),
            "notes": self.notes,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        for key in _RECORD_TEXT_FIELDS:
            value = getattr(self, key)
            if value:
                payload[key] = value
        if self.editor:
            payload["editor"] = list(self.editor)
        if self.translator:
            payload["translator"] = list(self.translator)
        payload.update(self.extra)
        return payload


@dataclass
class FileRecord:
    """一条本地文件记录：与文献记录同库共存，序列化时带 ``kind="file"`` 判别。

    解析产物（markdown 与插图）落在库文件旁的 ``parsed/`` 下；``md_path``
    相对库文件所在目录存储。``extra`` 保留未知字段。
    """

    uuid: str
    path: str
    name: str = ""
    size: int = 0
    md5: str = ""
    status: str = "pending"
    md_path: str = ""
    doi: str = ""
    eprint: str = ""
    record_uuid: str = ""
    nature: str = ""
    note: str = ""
    error: str = ""
    added_at: str = ""
    updated_at: str = ""
    dup: dict[str, Any] = field(default_factory=dict, repr=False)
    extra: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, payload: Any, where: str = "file record") -> "FileRecord":
        if not isinstance(payload, Mapping):
            raise LibraryError(f"{where}: expected an object, got {_typename(payload)}")
        raw_uuid = payload.get("uuid")
        if not isinstance(raw_uuid, str) or not raw_uuid.strip():
            raise LibraryError(f"{where}.uuid: expected non-empty string")
        path = payload.get("path")
        if not isinstance(path, str) or not path.strip():
            raise LibraryError(f"{where}.path: expected non-empty string")
        name = payload.get("name") or ""
        if not isinstance(name, str):
            raise LibraryError(f"{where}.name: expected string")
        size = payload.get("size") or 0
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise LibraryError(f"{where}.size: expected a non-negative integer")
        md5 = _optional_string(payload.get("md5"), f"{where}.md5") or ""
        status = payload.get("status") or "pending"
        if status not in FILE_STATUSES:
            allowed = "/".join(FILE_STATUSES)
            raise LibraryError(f"{where}.status: expected one of {allowed}, got {status!r}")
        md_path = _optional_string(payload.get("md_path"), f"{where}.md_path") or ""
        doi = _optional_string(payload.get("doi"), f"{where}.doi") or ""
        eprint = _optional_string(payload.get("eprint"), f"{where}.eprint") or ""
        record_uuid = _optional_string(payload.get("record_uuid"), f"{where}.record_uuid") or ""
        nature = _optional_string(payload.get("nature"), f"{where}.nature") or ""
        if nature and nature not in NATURES:
            allowed = "/".join(NATURES)
            raise LibraryError(f"{where}.nature: expected one of {allowed}, got {nature!r}")
        note = _optional_string(payload.get("note"), f"{where}.note") or ""
        error = _optional_string(payload.get("error"), f"{where}.error") or ""
        dup = payload.get("dup") or {}
        if not isinstance(dup, dict):
            raise LibraryError(f"{where}.dup: expected an object")
        added_at = _optional_string(payload.get("added_at"), f"{where}.added_at") or ""
        updated_at = _optional_string(payload.get("updated_at"), f"{where}.updated_at") or ""
        extra = {key: value for key, value in payload.items() if key not in _FILE_RECORD_KNOWN}
        return cls(
            uuid=raw_uuid.strip(),
            path=path.strip(),
            name=name,
            size=size,
            md5=md5,
            status=status,
            md_path=md_path,
            doi=doi,
            eprint=eprint,
            record_uuid=record_uuid,
            nature=nature,
            note=note,
            error=error,
            added_at=added_at,
            updated_at=updated_at,
            dup=dict(dup),
            extra=extra,
        )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "kind": "file",
            "uuid": self.uuid,
            "path": self.path,
            "name": self.name,
            "size": self.size,
            "status": self.status,
            "added_at": self.added_at,
            "updated_at": self.updated_at,
        }
        if self.md5:
            payload["md5"] = self.md5
        if self.md_path:
            payload["md_path"] = self.md_path
        if self.doi:
            payload["doi"] = self.doi
        if self.eprint:
            payload["eprint"] = self.eprint
        if self.record_uuid:
            payload["record_uuid"] = self.record_uuid
        if self.nature:
            payload["nature"] = self.nature
        if self.note:
            payload["note"] = self.note
        if self.error:
            payload["error"] = self.error
        if self.dup:
            payload["dup"] = dict(self.dup)
        payload.update(self.extra)
        return payload

    def prune_dup(self, removed_record_uuids: set[str]) -> None:
        """从「疑似重复」候选中剔除已删除的文献记录；候选清空则整个清掉。"""
        matches = self.dup.get("matches") if isinstance(self.dup, dict) else None
        if not isinstance(matches, list):
            return
        kept = [
            item
            for item in matches
            if not (
                isinstance(item, dict)
                and item.get("kind") == "record"
                and item.get("uuid") in removed_record_uuids
            )
        ]
        if kept:
            self.dup["matches"] = kept
        else:
            self.dup = {}


class Library:
    """一个库文件：记录列表＋磁盘状态指纹。

    打开用 :meth:`load`（既有文件）或 :meth:`create`（新建空库）；改动用
    :meth:`add_record` / :meth:`update_record` / :meth:`delete_record`，
    最后 :meth:`save` 落盘。``save`` 前复核磁盘状态：库被外部改动过就抛出
    :class:`LibraryError`，提示先 :meth:`reload`。
    """

    def __init__(
        self,
        path: Path,
        records: list[Record],
        file_records: Optional[list[FileRecord]] = None,
    ):
        self.path = Path(path)
        self.records = records
        self.file_records: list[FileRecord] = list(file_records) if file_records else []
        self._stamp: Optional[tuple[int, int]] = None

    @classmethod
    def load(cls, path: Path) -> "Library":
        """载入并校验库文件；不存在／非法／字段不合法均抛 :class:`LibraryError`。

        数组里 ``kind="file"`` 的条目进 ``file_records``，其余按文献记录解析。
        """
        path = Path(path)
        if not path.is_file():
            raise LibraryError(f"library file not found: {path}")
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise LibraryError(f"cannot read library file: {exc}") from exc
        payload = _parse_json(text, path)
        if not isinstance(payload, list):
            raise LibraryError(
                f"{path.name}: top-level must be an array of records, got {_typename(payload)}"
            )
        records: list[Record] = []
        file_records: list[FileRecord] = []
        for index, item in enumerate(payload):
            if isinstance(item, Mapping) and item.get("kind") == "file":
                file_records.append(FileRecord.from_dict(item, f"file_records[{index}]"))
            else:
                records.append(Record.from_dict(item, f"records[{index}]"))
        _check_unique(records)
        _check_unique(file_records)
        library = cls(path, records, file_records)
        library._stamp = _stamp_of(path)
        return library

    @classmethod
    def create(cls, path: Path) -> "Library":
        """新建空库（内容为 ``[]``）；目标已存在时拒绝。"""
        path = Path(path)
        if path.exists():
            raise LibraryError(f"file already exists: {path}")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise LibraryError(f"cannot create directory: {exc}") from exc
        _atomic_write(path, "[]\n")
        library = cls(path, [])
        library._stamp = _stamp_of(path)
        return library

    def save(self) -> None:
        """原子落盘；替换前备份 ``<库名>.bak``。库被外部改动时拒绝覆盖。"""
        current = _stamp_of(self.path)
        if self._stamp is not None and current != self._stamp:
            raise LibraryError(
                "library file changed on disk since it was loaded; reload before saving"
            )
        if self.path.is_file():
            try:
                shutil.copy2(self.path, self.path.with_name(self.path.name + ".bak"))
            except OSError as exc:
                raise LibraryError(f"cannot write backup file: {exc}") from exc
        _atomic_write(self.path, self.dumps())
        self._stamp = _stamp_of(self.path)

    def reload(self) -> None:
        """丢弃内存改动，从磁盘重新载入。"""
        fresh = Library.load(self.path)
        self.records = fresh.records
        self._stamp = fresh._stamp

    def dumps(self) -> str:
        """序列化为库文件文本（UTF-8、缩进 2、不转义中文）。

        文献记录在前、本地文件记录（``kind="file"``）在后；两类皆空时是 ``[]``。
        """
        payload = [record.to_dict() for record in self.records]
        payload.extend(record.to_dict() for record in self.file_records)
        return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"

    def find(self, uuid: str) -> Optional[Record]:
        """按 uuid 查找记录；没有则返回 ``None``。"""
        for record in self.records:
            if record.uuid == uuid:
                return record
        return None

    def find_file(self, uuid: str) -> Optional[FileRecord]:
        """按 uuid 查找本地文件记录；没有则返回 ``None``。"""
        for record in self.file_records:
            if record.uuid == uuid:
                return record
        return None

    def add_file_record(self, data: Mapping[str, Any]) -> FileRecord:
        """追加一条本地文件记录：缺 ``uuid`` 时生成 v4，缺时间戳时补当前时刻。"""
        payload = dict(data or {})
        payload["uuid"] = payload.get("uuid") or str(_uuid.uuid4())
        payload["added_at"] = payload.get("added_at") or now_iso()
        payload["updated_at"] = payload.get("updated_at") or now_iso()
        record = FileRecord.from_dict(payload, where="new file record")
        if self.find_file(record.uuid) is not None:
            raise LibraryError(f"duplicate uuid: {record.uuid}")
        self.file_records.append(record)
        return record

    def delete_file_record(self, uuid: str) -> FileRecord:
        """删除一条本地文件记录并返回它；不存在时抛错。"""
        for index, record in enumerate(self.file_records):
            if record.uuid == uuid:
                return self.file_records.pop(index)
        raise LibraryError(f"file record not found: {uuid}")

    def add_record(self, data: Mapping[str, Any]) -> Record:
        """追加一笔记录：缺 ``uuid`` 时生成 v4，缺时间戳时补当前时刻。"""
        payload = dict(data or {})
        payload["uuid"] = payload.get("uuid") or str(_uuid.uuid4())
        payload["created_at"] = payload.get("created_at") or now_iso()
        payload["updated_at"] = payload.get("updated_at") or now_iso()
        record = Record.from_dict(payload, where="new record")
        if self.find(record.uuid) is not None:
            raise LibraryError(f"duplicate uuid: {record.uuid}")
        self.records.append(record)
        return record

    def update_record(self, uuid: str, data: Mapping[str, Any]) -> Record:
        """整条替换（``uuid`` 与 ``created_at`` 不可改；``updated_at`` 刷新）。"""
        index = self._require_index(uuid)
        previous = self.records[index]
        payload = dict(data or {})
        payload["uuid"] = uuid
        payload["created_at"] = previous.created_at
        payload["updated_at"] = now_iso()
        record = Record.from_dict(payload, where=f"record {uuid}")
        self.records[index] = record
        return record

    def delete_record(self, uuid: str) -> Record:
        """删除记录并返回被删的那条；不存在时抛错；其关联文件自动解除关联。"""
        record = self.records.pop(self._require_index(uuid))
        for file_record in self.file_records:
            if file_record.record_uuid == uuid:
                file_record.record_uuid = ""
                file_record.nature = ""
            file_record.prune_dup({uuid})
        return record

    def _require_index(self, uuid: str) -> int:
        for index, record in enumerate(self.records):
            if record.uuid == uuid:
                return index
        raise LibraryError(f"record not found: {uuid}")


def _typename(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    return type(value).__name__


def _optional_string(value: Any, where: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise LibraryError(f"{where}: expected string, got {_typename(value)}")
    return value


def _string_list(value: Any, where: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise LibraryError(f"{where}: expected a list of strings")
    return list(value)


def _project_list(value: Any, where: str) -> list[str]:
    return [text.strip() for text in _string_list(value, where) if text.strip()]


def _year_value(value: Any, where: str) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise LibraryError(f"{where}: expected integer or null, got {_typename(value)}")
    return value


def _normalize_doi(value: Any, where: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise LibraryError(f"{where}: expected string or null, got {_typename(value)}")
    text = value.strip()
    if not text:
        return None
    for prefix in _DOI_PREFIXES:
        if text.lower().startswith(prefix):
            text = text[len(prefix):].strip()
            break
    return text or None


def _file_entries(value: Any, where: str) -> list[FileEntry]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise LibraryError(f"{where}: expected an array, got {_typename(value)}")
    return [FileEntry.from_dict(item, f"{where}[{index}]") for index, item in enumerate(value)]


def _check_unique(records: list[Record]) -> None:
    seen: set[str] = set()
    for index, record in enumerate(records):
        if record.uuid in seen:
            raise LibraryError(f"records[{index}].uuid: duplicate uuid {record.uuid}")
        seen.add(record.uuid)


def _parse_json(text: str, path: Path) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise LibraryError(
            f"invalid JSON in {path.name} at line {exc.lineno} column {exc.colno}: {exc.msg}"
        ) from exc


def _stamp_of(path: Path) -> Optional[tuple[int, int]]:
    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise LibraryError(f"cannot write library file: {exc}") from exc
