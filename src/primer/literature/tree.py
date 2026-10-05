# -*- coding: utf-8 -*-
"""项目树：由记录的 ``projects`` 路径字符串构建的层级视图（纯函数，无 IO）。

规则（见《设计规格.md》§4）：

* 路径按 ``/`` 拆分，忽略空段；大小写敏感；
* 节点计数是**子树内去重记录数**——同一条记录挂在兄弟分支下时，父节点只计一次；
* 点选节点＝过滤该项目**及其子孙**下的记录；「未归项目」＝没有任何有效项目路径的记录。

对外接口：:func:`normalize_project_path`、:func:`active_projects`、
:func:`build_tree`、:func:`records_in_subtree`、:func:`unfiled_records`、
:class:`ProjectNode`、:class:`ProjectTree`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Sequence

if TYPE_CHECKING:  # 仅类型检查用：树模块不依赖库模块的运行时实现
    from .library import Record

__all__ = [
    "ProjectNode",
    "ProjectTree",
    "active_projects",
    "build_tree",
    "normalize_project_path",
    "records_in_subtree",
    "unfiled_records",
]


def normalize_project_path(raw: str) -> str:
    """归一化项目路径：按 ``/`` 拆分、去空白与空段后重新拼接；全空返回空串。"""
    parts = [part.strip() for part in str(raw).split("/")]
    return "/".join(part for part in parts if part)


def active_projects(record: "Record") -> list[str]:
    """记录的有效项目路径（归一化、去重、保持书写顺序）。"""
    seen: set[str] = set()
    paths: list[str] = []
    for raw in record.projects:
        path = normalize_project_path(raw)
        if path and path not in seen:
            seen.add(path)
            paths.append(path)
    return paths


@dataclass
class ProjectNode:
    """项目树节点：``path`` 为完整路径，``count`` 为子树去重记录数。"""

    name: str
    path: str
    count: int
    children: list["ProjectNode"] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "path": self.path,
            "count": self.count,
            "children": [child.to_dict() for child in self.children],
        }


@dataclass
class ProjectTree:
    """项目森林，附全库计数与未归计数。"""

    roots: list[ProjectNode]
    total: int
    unfiled: int

    def to_dict(self) -> dict:
        return {
            "roots": [node.to_dict() for node in self.roots],
            "total": self.total,
            "unfiled": self.unfiled,
        }


def build_tree(records: Sequence["Record"], declared: Sequence[str] = ()) -> ProjectTree:
    """由记录的项目路径构建树；``declared`` 是额外声明的项目路径（可为空项目）。

    子节点按名称排序，计数按子树去重；声明路径只保证节点存在（不计入记录数）。
    """
    roots: dict[str, dict] = {}
    for record in records:
        for path in active_projects(record):
            _insert(roots, path.split("/"), record.uuid)
    for raw in declared:
        path = normalize_project_path(raw)
        if path:
            _insert(roots, path.split("/"), None)
    nodes = [_finalize(name, name, roots[name])[0] for name in sorted(roots)]
    return ProjectTree(
        roots=nodes,
        total=len({record.uuid for record in records}),
        unfiled=len(unfiled_records(records)),
    )


def records_in_subtree(records: Sequence["Record"], project_path: str) -> list["Record"]:
    """该项目及其子孙下的记录（保持输入顺序）；空路径返回全部。"""
    target = normalize_project_path(project_path)
    if not target:
        return list(records)
    prefix = target + "/"
    result: list["Record"] = []
    for record in records:
        for path in active_projects(record):
            if path == target or path.startswith(prefix):
                result.append(record)
                break
    return result


def unfiled_records(records: Sequence["Record"]) -> list["Record"]:
    """没有任何有效项目路径的记录。"""
    return [record for record in records if not active_projects(record)]


def _insert(roots: dict, parts: list[str], uuid: str | None) -> None:
    children = roots
    for part in parts[:-1]:
        children = children.setdefault(part, {"children": {}, "direct": set()})["children"]
    leaf = children.setdefault(parts[-1], {"children": {}, "direct": set()})
    if uuid is not None:
        leaf["direct"].add(uuid)


def _finalize(name: str, path: str, entry: dict) -> tuple[ProjectNode, set[str]]:
    uuids: set[str] = set(entry["direct"])
    children: list[ProjectNode] = []
    for child_name in sorted(entry["children"]):
        child, child_uuids = _finalize(
            child_name, f"{path}/{child_name}", entry["children"][child_name]
        )
        children.append(child)
        uuids |= child_uuids
    return ProjectNode(name=name, path=path, count=len(uuids), children=children), uuids
