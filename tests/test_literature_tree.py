# -*- coding: utf-8 -*-
"""tree 的单元测试：层级、多挂载去重计数、归一化、子树过滤与未归。"""

from primer.literature.library import Record
from primer.literature.tree import (
    active_projects,
    build_tree,
    normalize_project_path,
    records_in_subtree,
    unfiled_records,
)


def _record(uuid, projects):
    return Record(uuid=uuid, title=f"记录 {uuid}", projects=list(projects))


def test_build_tree_hierarchy_counts_and_unfiled():
    records = [
        _record("r1", ["a/b/c"]),
        _record("r2", ["a/b"]),
        _record("r3", ["a"]),
        _record("r4", ["a/b/c", "a/x"]),
        _record("r5", []),
    ]

    tree = build_tree(records)

    assert tree.total == 5
    assert tree.unfiled == 1
    root = tree.roots[0]
    assert (root.name, root.path, root.count) == ("a", "a", 4)
    by_name = {node.name: node for node in root.children}
    assert by_name["b"].count == 3
    assert by_name["x"].count == 1
    assert by_name["b"].children[0].path == "a/b/c"
    assert by_name["b"].children[0].count == 2


def test_multi_mount_counts_once_per_node():
    records = [_record("r1", ["a/b", "a/c"])]

    tree = build_tree(records)

    root = tree.roots[0]
    assert root.count == 1
    assert {node.count for node in root.children} == {1}


def test_normalize_project_path():
    assert normalize_project_path(" a // b / ") == "a/b"
    assert normalize_project_path("") == ""
    assert normalize_project_path(" / ") == ""
    assert normalize_project_path("A/b") == "A/b"


def test_active_projects_normalises_and_dedups():
    record = _record("r1", [" a/b ", "a/b", "", "x//y"])
    assert active_projects(record) == ["a/b", "x/y"]


def test_duplicate_path_strings_count_once():
    records = [_record("r1", ["a/b", "a/b", " a / b "])]

    tree = build_tree(records)

    assert tree.roots[0].children[0].count == 1


def test_records_in_subtree_excludes_prefix_traps():
    records = [
        _record("r1", ["a"]),
        _record("r2", ["a/b"]),
        _record("r3", ["a/b/c"]),
        _record("r4", ["ab"]),
        _record("r5", []),
    ]

    assert [record.uuid for record in records_in_subtree(records, "a")] == ["r1", "r2", "r3"]
    assert [record.uuid for record in records_in_subtree(records, "a/b")] == ["r2", "r3"]
    assert records_in_subtree(records, "") == records  # 空路径＝全部


def test_unfiled_records():
    records = [_record("r1", []), _record("r2", ["  "]), _record("r3", ["a"])]

    assert [record.uuid for record in unfiled_records(records)] == ["r1", "r2"]


def test_tree_to_dict_shape():
    tree = build_tree([_record("r1", ["a/b"])])

    payload = tree.to_dict()

    assert set(payload) == {"roots", "total", "unfiled"}
    node = payload["roots"][0]
    assert set(node) == {"name", "path", "count", "children"}
    assert node["children"][0]["path"] == "a/b"


def test_children_sorted_by_name():
    records = [
        _record("r1", ["root/b"]),
        _record("r2", ["root/a"]),
        _record("r3", ["root/c"]),
    ]

    tree = build_tree(records)

    assert [node.name for node in tree.roots[0].children] == ["a", "b", "c"]


def test_declared_projects_join_the_tree_with_zero_count():
    records = [_record("r1", ["甲/乙"])]

    tree = build_tree(records, declared=["甲/丙", "丁"])

    by_name = {node.name: node for node in tree.roots}
    assert by_name["甲"].count == 1
    assert by_name["丁"].count == 0
    children = {node.name: node for node in by_name["甲"].children}
    assert children["乙"].count == 1 and children["丙"].count == 0
    assert tree.total == 1 and tree.unfiled == 0
