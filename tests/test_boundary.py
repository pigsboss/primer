# -*- coding: utf-8 -*-
"""输出边界策略的机械检查：工程目录只读、产物只在 ``_primer/`` 下、路径一律相对工程根。

策略本体见 ``src/primer/paths.py``。这里不读源码、不看意图，只看磁盘：

1. 在 ``tmp_path`` 里搭一棵微型工程树（PDF、文献表、下载账本、救援缓存、抽取文本）；
2. 快照整棵树：相对路径 → (字节数, mtime_ns, sha256)；
3. 用 argv 直接调 ``main()``（不是子进程，覆盖率才算数），跑完所有只读入口；
4. 对账：新增或改动的文件必须都在 ``<工程根>/_primer/`` 下，原有文件一个都不能少、
   不能被改写——**这是核心断言，任何写进输入树的代码路径都会在这里炸掉**；
5. 再把 ``_primer/`` 下每个文件读一遍，断言里面不含工程根的绝对路径。

失败模式是显式的：越界写入 / 删除输入 / 产物里带绝对路径，都报出具体文件名与前缀
（见文件末尾的三个自检用例，它们用"故意犯规"证明这套机器真的会叫）。
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Mapping

import pytest

from primer.literature.__main__ import main as literature_main
from primer.literature.config import RunConfig
from primer.literature.paths import OUTPUT_DIRNAME, relative_to_root, strip_root_prefix
from primer.references.__main__ import main as references_main

PROJECT_NAME = "行星探测工程"
LITERATURE_OUT = f"{OUTPUT_DIRNAME}/literature"
REFERENCES_OUT = f"{OUTPUT_DIRNAME}/references"
REFERENCE_MD = "成果文件/参考文献.md"
BODY_MD = "成果文件/正文.md"
LOCAL_ROOT = "参考资料/参考文献原文"
REFLIB_LOG = "参考资料/reflib_download_log.csv"
RESCUE_CACHE = "中间文件/arxiv_rescue_cache.json"
TEXT_ROOT = "中间文件/文献文本"

# 假 MinerU：按 ``<stem>.zip`` 写出 MinerU 4.0 的那几个成员。设了 ``STUB_FAIL``
# 就改成失败退出，并把**绝对**输入路径写进 stderr——真实 MinerU 就是这么干的。
STUB_BACKEND = '''#!/usr/bin/env python3
import json
import os
import sys
import zipfile
from pathlib import Path

args = sys.argv[1:]
output = Path(args[args.index("-o") + 1])
inputs = [Path(item) for item in args if item.endswith(".pdf")]
if os.environ.get("STUB_FAIL"):
    for src in inputs:
        print(f"Cannot read document metadata: failed to parse {src}", file=sys.stderr)
    sys.exit(3)
for src in inputs:
    with zipfile.ZipFile(output / f"{src.stem}.zip", "w") as handle:
        handle.writestr("markdown.md", f"# {src.stem}\\n\\nbody of {src.stem}\\n")
        handle.writestr("images/page_1_image_0.png", b"\\x89PNG\\r\\n\\x1a\\n" + b"\\x00" * 16)
        handle.writestr("middle_json.json", json.dumps({"pages": [{}, {}]}))
        handle.writestr("structured_content.json", json.dumps({"pages": [{}, {}]}))
        handle.writestr("model_output.json", "x" * 512)
print(f"Parsed {len(inputs)} input(s).")
'''

REFERENCE_TREE = {
    "参考资料/reflib_download_log.csv": "ref,class,title,arxiv,status,saved\n",
    LOCAL_ROOT + "/020_A_Ice_Giants_Review.pdf": b"%PDF-1.4 ice giants",
    LOCAL_ROOT + "/MISC_R_arxiv2206_06693.pdf": b"%PDF-1.4 ice giants",
    RESCUE_CACHE: json.dumps(
        {
            "2": {"status": "rescued", "arxiv": "2202.0001", "file": "002_R_arxiv2202_0001.pdf"},
            "3": {"status": "mismatch_misc", "arxiv": "2206.06693", "file": "003_B_arxiv2206_06693.pdf"},
        },
        ensure_ascii=False,
    ),
    TEXT_ROOT + "/020_A_Ice_Giants_Review.txt": "extracted text",
    REFERENCE_MD: (
        "# 参考文献库\n"
        "\n"
        "> 共 4 条\n"
        "> 结构：A 战略规划 [1–2] / B 系外行星 [3–4]\n"
        "\n"
        "## A 战略规划\n"
        "\n"
        "[1] Alpha A. Ice giants in the outer solar system. Icarus 1, 1–2, 2001. "
        "［原文：arXiv:2511.13946 已存本地］\n"
        "[2] Beta B. Title two. Icarus 2, 3–4, 2002. ［原文：待图书馆获取］\n"
        "\n"
        "## B 系外行星\n"
        "\n"
        "[3] Gamma C. Title three. Icarus 3, 5–6, 2003. ［原文：arXiv:2206.06693 已存本地］\n"
        "[4] Delta D. Title four. Icarus 4, 7–8, 2004. ［原文：NASA 官网公开］\n"
    ),
    BODY_MD: "# 正文\n\n本节引用 [3]，也提及 [4]。\n",
}


def _build_project(tmp_path: Path, name: str = PROJECT_NAME) -> Path:
    """搭一棵微型工程树，返回工程根；``name`` 用于演大小写不一致的拼法。"""
    project = tmp_path / name
    for relative, payload in REFERENCE_TREE.items():
        path = project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(payload, bytes):
            path.write_bytes(payload)
        else:
            path.write_text(payload, encoding="utf-8")
    saved = project / LOCAL_ROOT / "020_A_Ice_Giants_Review.pdf"
    (project / REFLIB_LOG).write_text(
        "ref,class,title,arxiv,status,saved\n"
        f"1,A,Ice giants in the outer solar system,2511.13946,ok(24KB),{saved}\n",
        encoding="utf-8",
    )
    return project


def _stub_backend(tmp_path: Path) -> Path:
    """在**工程之外**放一个可执行的假后端，并返回它的路径。"""
    stub = tmp_path / "stub-mineru"
    stub.write_text(STUB_BACKEND, encoding="utf-8")
    stub.chmod(0o755)
    return stub


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _snapshot(root: Path) -> dict[str, tuple[int, int, str]]:
    """工程树快照：相对路径 → (字节数, mtime_ns, sha256)。"""
    shot: dict[str, tuple[int, int, str]] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            stat = path.stat()
            shot[path.relative_to(root).as_posix()] = (stat.st_size, stat.st_mtime_ns, _sha256(path))
    return shot


def _assert_writes_stay_inside(
    before: Mapping[str, tuple[int, int, str]], after: Mapping[str, tuple[int, int, str]]
) -> list[str]:
    """核心断言：改动只能发生在 ``_primer/`` 下，返回被新建或改动的相对路径。"""
    deleted = sorted(set(before) - set(after))
    assert not deleted, f"primer deleted files it does not own: {deleted}"
    changed = sorted(name for name, meta in after.items() if before.get(name) != meta)
    stray = [name for name in changed if not name.startswith(f"{OUTPUT_DIRNAME}/")]
    assert not stray, f"primer wrote outside its cache dir <project-root>/{OUTPUT_DIRNAME}/: {stray}"
    return changed


def _assert_no_absolute_paths(project: Path, base: Path, *extra_needles: str) -> None:
    """产物里不许出现工程根的绝对写法（含 realpath 归一化后的形式）。

    ``extra_needles`` 给"同一个目录的另一种写法"用：真实账本里的 ``saved`` 就可能是
    大小写不同的拼法，只查一种拼法是查不出漏网的。
    """
    needles = {str(project), os.path.realpath(project), *extra_needles}
    for path in sorted(base.rglob("*")):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for needle in needles:
            assert needle not in text, f"{path} embeds the absolute path {needle}"


def _literature_scan_argv(project: Path, *extra: str) -> list[str]:
    return [
        "scan",
        "--project-root", str(project),
        "--root", str(project / "参考资料"),
        "--quiet",
        *extra,
    ]


def _literature_run_argv(project: Path, *extra: str) -> list[str]:
    return [
        "run",
        "--project-root", str(project),
        "--root", str(project / "参考资料"),
        *extra,
    ]


def _references_argv(project: Path, command: str, *extra: str) -> list[str]:
    return [
        command,
        "--project-root", str(project),
        "--ref", str(project / REFERENCE_MD),
        "--local-root", str(project / LOCAL_ROOT),
        *extra,
    ]


def test_read_only_entry_points_touch_nothing_outside_the_cache_dir(tmp_path, capsys):
    project = _build_project(tmp_path)
    before = _snapshot(project)

    codes = [
        literature_main(_literature_scan_argv(project, "--json")),
        literature_main(_literature_run_argv(project, "--dry-run")),
        literature_main(["status", "--project-root", str(project)]),
        references_main(
            _references_argv(
                project,
                "audit",
                "--reflib-log", str(project / REFLIB_LOG),
                "--rescue-cache", str(project / RESCUE_CACHE),
                "--text-root", str(project / TEXT_ROOT),
            )
        ),
        references_main(
            _references_argv(
                project,
                "list",
                "--body", str(project / BODY_MD),
            )
        ),
    ]
    assert codes == [0, 0, 0, 0, 0]

    created = _assert_writes_stay_inside(before, _snapshot(project))
    assert created, "expected the run to produce artifacts under the cache dir"

    # 默认产物目录就是就地约定的那两个，用户没有给 --out / --output-dir
    assert (project / LITERATURE_OUT / "index.json").is_file()
    for name in ("index.json", "index.csv", "audit.md", "图书馆文献获取清单.new.csv", "图书馆文献获取清单.new.md"):
        assert (project / REFERENCES_OUT / name).is_file(), name

    _assert_no_absolute_paths(project, project / OUTPUT_DIRNAME)

    captured = capsys.readouterr()
    for needle in (str(project), os.path.realpath(project)):
        assert needle not in captured.out, f"stdout printed the absolute root {needle}"
        assert needle not in captured.err, f"stderr printed the absolute root {needle}"


def test_ledger_and_citation_header_of_a_real_run_are_project_relative(tmp_path):
    """真跑一次转换（假后端）：账本与扁平 markdown 都只能出现相对路径。"""
    project = _build_project(tmp_path)
    config_path = project / "literature.yaml"
    config_path.write_text(f"mineru_command: {_stub_backend(tmp_path)}\n", encoding="utf-8")
    before = _snapshot(project)

    code = literature_main(
        _literature_run_argv(project, "--config", str(config_path))
    )

    assert code == 0
    _assert_writes_stay_inside(before, _snapshot(project))
    assert (project / LITERATURE_OUT / "state.jsonl").is_file()

    ledger = (project / LITERATURE_OUT / "state.jsonl").read_text(encoding="utf-8")
    assert f'"rel_path": "{LOCAL_ROOT}/020_A_Ice_Giants_Review.pdf"' in ledger
    assert f'"output_dir": "{LITERATURE_OUT}/raw/' in ledger

    flat = sorted((project / LITERATURE_OUT / "flat").glob("*.md"))
    assert len(flat) == 1
    text = flat[0].read_text(encoding="utf-8")
    assert f"> **Source** — {LOCAL_ROOT}/020_A_Ice_Giants_Review.pdf\n" in text

    _assert_no_absolute_paths(project, project / OUTPUT_DIRNAME)


def test_backend_error_text_keeps_no_absolute_path(tmp_path, monkeypatch, capsys):
    """后端把绝对输入路径塞进 stderr，入账前必须被抹成相对写法。"""
    project = _build_project(tmp_path)
    config_path = project / "literature.yaml"
    config_path.write_text(f"mineru_command: {_stub_backend(tmp_path)}\n", encoding="utf-8")
    monkeypatch.setenv("STUB_FAIL", "1")
    before = _snapshot(project)

    code = literature_main(_literature_run_argv(project, "--config", str(config_path)))

    assert code == 1
    _assert_writes_stay_inside(before, _snapshot(project))

    ledger = (project / LITERATURE_OUT / "state.jsonl").read_text(encoding="utf-8")
    assert "Cannot read document metadata" in ledger
    assert f"{LITERATURE_OUT}/staging/" in ledger
    _assert_no_absolute_paths(project, project / OUTPUT_DIRNAME)

    captured = capsys.readouterr()
    assert "## Failures" in captured.out
    assert str(project) not in captured.out


def test_run_config_defaults_resolve_under_the_project_cache_dir(tmp_path):
    project = tmp_path / PROJECT_NAME
    project.mkdir()

    config = RunConfig(project_root=project)

    assert config.output_dir == project / OUTPUT_DIRNAME / "literature"
    assert config.state_path == config.output_dir / "state.jsonl"
    assert config.staging_dir == config.output_dir / "staging"
    assert config.raw_dir == config.output_dir / "raw"
    assert config.flat_dir == config.output_dir / "flat"
    assert config.index_path == config.output_dir / "index.json"
    assert config.roots == [project]


def test_relative_to_root_falls_back_for_paths_outside_the_project_root(tmp_path):
    """判据的边界：工程根内换算，工程根外无从换算，只能原样保留。"""
    project = tmp_path / PROJECT_NAME
    inside = project / LOCAL_ROOT / "020_A_Ice_Giants_Review.pdf"
    outside = tmp_path / "elsewhere" / "borrowed.pdf"

    assert relative_to_root(inside, project) == f"{LOCAL_ROOT}/020_A_Ice_Giants_Review.pdf"
    assert relative_to_root(outside, project) == outside.as_posix()
    assert relative_to_root(inside, None) == inside.as_posix()
    assert relative_to_root(LOCAL_ROOT, project) == LOCAL_ROOT


def test_relative_to_root_treats_a_case_differing_prefix_as_the_same_root(tmp_path):
    """macOS/Windows 的文件系统不区分大小写，而账本里的路径完全可能是另一种拼法。"""
    project = tmp_path / "PlanetProbe"
    other_spelling = tmp_path / "planetprobe"

    assert relative_to_root(other_spelling / LOCAL_ROOT / "a.pdf", project) == f"{LOCAL_ROOT}/a.pdf"
    assert relative_to_root(other_spelling / "a.pdf", project) == "a.pdf"
    # 相对工程根按当前工作目录补齐，与 normalize_project_root 同一套规矩
    assert relative_to_root(Path.cwd() / "a.pdf", ".") == "a.pdf"
    # 只是前缀相同、并不是同一层目录的路径，照旧不动
    deeper = tmp_path / "planetprobe-extra" / "a.pdf"
    assert relative_to_root(deeper, project) == deeper.as_posix()
    # 自由文本里的前缀同样大小写不敏感
    assert strip_root_prefix(f"parse failed: {other_spelling}/a.pdf", project) == "parse failed: a.pdf"


def test_a_case_differing_root_spelling_still_lands_relative_in_the_artifacts(tmp_path):
    """真实账本的 ``saved`` 写的是 ``Documents/Kimi/…``，工程根却是小写 ``kimi``。

    这一层以前漏了：文件系统认为两者是同一个目录，字符串比较却认为不是，于是
    ``references audit`` 把 90 处绝对路径写进了 index.csv / index.json。
    """
    project = _build_project(tmp_path, name="PlanetProbe")
    other_spelling = Path(str(project).replace("PlanetProbe", "planetprobe"))
    log = project / REFLIB_LOG
    log.write_text(
        "ref,class,title,arxiv,status,saved\n"
        "1,A,Ice giants in the outer solar system,2511.13946,ok(24KB),"
        f"{other_spelling / LOCAL_ROOT / '020_A_Ice_Giants_Review.pdf'}\n",
        encoding="utf-8",
    )
    before = _snapshot(project)

    code = references_main(_references_argv(project, "audit", "--reflib-log", str(log)))

    assert code == 0
    _assert_writes_stay_inside(before, _snapshot(project))

    index = (project / REFERENCES_OUT / "index.json").read_text(encoding="utf-8")
    assert f"saved={LOCAL_ROOT}/020_A_Ice_Giants_Review.pdf" in index
    _assert_no_absolute_paths(
        project,
        project / OUTPUT_DIRNAME,
        str(other_spelling),
        os.path.realpath(str(other_spelling)),
    )


def test_the_boundary_check_catches_a_write_into_the_input_tree(tmp_path):
    project = _build_project(tmp_path)
    before = _snapshot(project)

    (project / "成果文件" / "stray.md").write_text("written by a buggy code path", encoding="utf-8")

    with pytest.raises(AssertionError, match="stray.md"):
        _assert_writes_stay_inside(before, _snapshot(project))


def test_the_boundary_check_catches_a_rewritten_input_file(tmp_path):
    project = _build_project(tmp_path)
    before = _snapshot(project)

    (project / REFERENCE_MD).write_text("# 参考文献库\n", encoding="utf-8")

    with pytest.raises(AssertionError, match="参考文献.md"):
        _assert_writes_stay_inside(before, _snapshot(project))


def test_the_boundary_check_catches_a_deleted_input_file(tmp_path):
    project = _build_project(tmp_path)
    before = _snapshot(project)

    (project / BODY_MD).unlink()

    with pytest.raises(AssertionError, match="deleted files it does not own"):
        _assert_writes_stay_inside(before, _snapshot(project))


def test_the_absolute_path_check_catches_a_leaking_artifact(tmp_path):
    project = _build_project(tmp_path)
    leak = project / LITERATURE_OUT
    leak.mkdir(parents=True)
    (leak / "index.json").write_text(
        json.dumps({"path": str(project / LOCAL_ROOT)}, ensure_ascii=False), encoding="utf-8"
    )

    with pytest.raises(AssertionError, match="embeds the absolute path"):
        _assert_no_absolute_paths(project, project / OUTPUT_DIRNAME)
