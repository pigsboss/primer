"""共享夹具：一个覆盖四种处理策略的微型"科研项目"目录。"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"


@pytest.fixture
def project_tree(tmp_path):
    """构造样例目录：每个文件对应一种预期的处理策略。"""
    (tmp_path / "README.md").write_text("# Demo project\n\nA tiny fixture.\n", encoding="utf-8")
    (tmp_path / "main.py").write_text(
        "import os\n"
        "\n"
        "\n"
        "def greet(name):\n"
        '    """Say hello."""\n'
        '    return "hello " + name\n'
        "\n"
        "\n"
        "class Widget:\n"
        "    def spin(self):\n"
        "        return 42\n",
        encoding="utf-8",
    )
    (tmp_path / "config.yaml").write_text(
        "mission: neosurvey\ntelescope:\n  aperture_m: 1.2\n  filters: [r, i, z]\n",
        encoding="utf-8",
    )
    (tmp_path / "data.csv").write_text("ra,dec,mag\n10.1,2.3,19.4\n10.2,2.4,20.1\n", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("plain notes\nsecond line\n", encoding="utf-8")
    (tmp_path / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    return tmp_path


@pytest.fixture
def run_cli():
    """以子进程方式运行 `python -m primer.digest`，返回 CompletedProcess。"""

    def _run(target, *args):
        env = {**os.environ, "PYTHONPATH": str(SRC_DIR)}
        return subprocess.run(
            [sys.executable, "-m", "primer.digest", str(target), *args],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(REPO_ROOT),
        )

    return _run


@pytest.fixture
def flatten_digest():
    """把 structure 的嵌套字典摊平成 {文件名: 文件条目}。"""

    def _flatten(node, out=None):
        out = {} if out is None else out
        for entry in node.get("files", []):
            out[Path(entry["metadata"]["path"]).name] = entry
        for sub in node.get("subdirectories", {}).values():
            _flatten(sub, out)
        return out

    return _flatten
