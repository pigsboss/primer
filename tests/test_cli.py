"""端到端测试：命令行跑通，且每类文件都得到与策略相符的输出结构。"""

import json
from pathlib import Path


def _payload(run_cli, target, *args):
    result = run_cli(target, *args)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_json_output_contract(run_cli, project_tree):
    payload = _payload(run_cli, project_tree, "--mode", "framework", "--output", "json")

    assert set(payload) == {"metadata", "structure", "strategy_statistics"}
    assert Path(payload["metadata"]["root_directory"]).resolve() == project_tree.resolve()
    assert payload["metadata"]["output_mode"] == "framework"
    assert payload["strategy_statistics"]["total_files"] == 6


def test_critical_document_keeps_full_content(run_cli, project_tree, flatten_digest):
    files = flatten_digest(_payload(run_cli, project_tree, "--output", "json")["structure"])

    entry = files["README.md"]
    assert entry["metadata"]["file_type"] == "critical_docs"
    assert entry["metadata"]["processing_strategy"] == "full_content"
    assert "# Demo project" in entry["full_content"]


def test_source_code_is_skeletonised(run_cli, project_tree, flatten_digest):
    files = flatten_digest(_payload(run_cli, project_tree, "--output", "json")["structure"])

    entry = files["main.py"]
    assert entry["metadata"]["processing_strategy"] == "code_skeleton"
    analysis = entry["source_analysis"]
    assert analysis["language"] == "python"
    assert "greet" in [f["name"] for f in analysis["functions"]]
    assert "Widget" in [c["name"] for c in analysis["classes"]]
    assert "summary" in entry


def test_config_and_table_files_are_not_degraded_to_binary(run_cli, project_tree, flatten_digest):
    files = flatten_digest(_payload(run_cli, project_tree, "--output", "json")["structure"])

    for name in ("config.yaml", "data.csv"):
        entry = files[name]
        assert entry["metadata"]["file_type"] != "binary_files", entry
        assert entry["actual_processing_strategy"] != "PROCESS_FAILED", entry
        assert entry["summary"]["summary"], entry


def test_binary_file_stays_metadata_only(run_cli, project_tree, flatten_digest):
    files = flatten_digest(_payload(run_cli, project_tree, "--output", "json")["structure"])

    entry = files["image.png"]
    assert entry["metadata"]["file_type"] == "binary_files"
    assert entry["metadata"]["processing_strategy"] == "metadata_only"
    assert entry["metadata"]["force_binary"] is True
    assert entry["metadata"]["md5_hash"]
    assert "summary" not in entry
    assert "full_content" not in entry


def test_save_writes_the_digest_to_a_file(run_cli, project_tree, tmp_path):
    out = tmp_path / "digest.json"

    result = run_cli(project_tree, "--output", "json", "--save", str(out))

    assert result.returncode == 0, result.stderr
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["metadata"]["output_mode"] == "framework"
