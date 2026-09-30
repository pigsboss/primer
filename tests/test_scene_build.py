# -*- coding: utf-8 -*-
"""``primer.scene.build`` 与命令行：发射、跑 Blender、日志、以及那道正式的闸门。

测试**不启动 Blender**：:func:`primer.scene.build.run` 的 ``runner`` 是注入点，这里换成
替身；二进制查找也换成替身（本机装了 Blender，但测试不该依赖它）。真正要钉住的是流程本身：
脚本与指纹先落盘、日志逐字存下、日志里有 Traceback 就算失败——哪怕 Blender 的退出码是零
（实测过：发射出来的脚本抛异常时，Blender 仍然以 0 退出，只看退出码会漏掉）。

``--final`` 那道闸门单独测：没有 ``--allow-final`` 时连 Blender 都不许被启动。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import scene_fixtures as fixtures

from primer.scene import build as build_module
from primer.scene.__main__ import build_parser, main
from primer.scene.build import BuildError, SceneBuildPlan, default_spec_path, find_blender, run
from primer.scene.checks import ERROR
from primer.scene.emit import SCRIPT_NAME
from primer.scene.spec import SceneError


class _Completed:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture
def real_find_blender():
    """真正的二进制查找（autouse 夹具把它换掉了，个别用例要拿回来测它自己）。"""
    return build_module.find_blender


@pytest.fixture(autouse=True)
def _no_real_blender(monkeypatch):
    """把二进制查找换成替身：测试不依赖本机是否装了 Blender。"""
    monkeypatch.setattr(build_module, "find_blender", lambda explicit=None: Path("/usr/bin/true"))


@pytest.fixture
def root(tmp_path):
    fixtures.write_spec(tmp_path)
    return tmp_path


def _runner(returncode=0, stdout="", stderr=""):
    calls = []

    def execute(command, cwd, timeout):
        calls.append((command, cwd, timeout))
        return _Completed(returncode, stdout, stderr)

    execute.calls = calls
    return execute


# ---------------------------------------------------------------- 闸门


def test_final_is_refused_without_the_explicit_allowance(root):
    runner = _runner()

    with pytest.raises(SceneError) as excinfo:
        run(root, final=True, allow_final=False, runner=runner)

    message = str(excinfo.value)
    assert "--final needs --allow-final" in message
    assert "human decision" in message
    # 拒绝发生在启动 Blender 之前：一个进程都没起。
    assert runner.calls == []
    assert not (root / "_primer" / "scene" / "build" / SCRIPT_NAME).exists()


def test_final_runs_when_a_human_has_allowed_it(root):
    result = run(root, final=True, allow_final=True, runner=_runner())

    assert result.plan.final is True
    assert result.plan.log.name == "build-final.log"
    # 替身没有真的渲出东西，所以现场报告仍然缺——验收如实报出来，不假装成功。
    assert [finding.code for finding in result.findings] == ["report_missing"]


def test_the_cli_refuses_final_without_the_flag_and_exits_nonzero(root, capsys):
    code = main(["build", "--final", "--root", str(root)])

    assert code == 2
    assert "--allow-final" in capsys.readouterr().err


def test_the_cli_accepts_final_with_the_flag(root, capsys, monkeypatch):
    import primer.scene.__main__ as cli

    monkeypatch.setattr(cli, "build_run", lambda *a, **k: _fake_result(root))

    assert main(["build", "--final", "--allow-final", "--root", str(root)]) == 0
    out = capsys.readouterr().out
    assert "正式" in out
    assert "指纹" in out


def _fake_result(root):
    from primer.scene.build import BuildResult

    plan = SceneBuildPlan(
        project_root=root, spec_path=fixtures.write_spec(root), final=True, blender=Path("/usr/bin/true")
    )
    return BuildResult(
        plan=plan,
        fingerprint=build_module.compute_fingerprint(
            build_module.load_spec(plan.spec_path), root
        ),
        log=plan.log,
        findings=(),
        report=None,
    )


# ---------------------------------------------------------------- 流程


def test_preview_is_the_default_mode(root):
    result = run(root, runner=_runner())

    assert result.plan.final is False
    assert result.plan.log.name == "build.log"


def test_the_build_writes_the_script_the_fingerprint_and_the_log(root):
    runner = _runner(stdout="Blender 5.1.1\n")

    result = run(root, runner=runner)

    scene = root / "_primer" / "scene"
    assert (scene / "build" / SCRIPT_NAME).is_file()
    fingerprint = json.loads((scene / "build" / "fingerprint.json").read_text(encoding="utf-8"))
    assert set(fingerprint) == {"spec", "spec_sha256", "digest", "assets"}
    assert fingerprint["spec"] == "_primer/scene/mission_layout.yaml"
    log = (scene / "build" / "build.log").read_text(encoding="utf-8")
    assert log.startswith("$ ")
    assert "-b -P" in log
    assert "# exit code: 0" in log
    assert "Blender 5.1.1" in log
    # 命令与工作目录：Blender 从工程根启动，绝对路径的脚本。
    command, cwd, _timeout = runner.calls[0]
    assert command[1:3] == ["-b", "-P"]
    assert Path(command[3]).name == SCRIPT_NAME
    assert cwd == root
    assert result.log == scene / "build" / "build.log"


def test_a_nonzero_exit_code_is_a_build_error_naming_the_log(root):
    runner = _runner(returncode=3, stderr="boom\n")

    with pytest.raises(BuildError) as excinfo:
        run(root, runner=runner)

    message = str(excinfo.value)
    assert "exited with code 3" in message
    assert "_primer/scene/build/build.log" in message
    assert "boom" in message


def test_a_traceback_in_the_log_is_a_failure_even_when_blender_exits_zero(root):
    """实测过：脚本里抛异常时 Blender 仍以 0 退出，只信退出码会漏掉整整一类失败。"""
    runner = _runner(stdout="Traceback (most recent call last):\nKeyError: 'color'\n")

    with pytest.raises(BuildError) as excinfo:
        run(root, runner=runner)

    assert "blender reported errors" in str(excinfo.value)
    assert "KeyError" in str(excinfo.value)


def test_an_error_line_in_the_log_is_a_failure(root):
    with pytest.raises(BuildError, match="blender reported errors"):
        run(root, runner=_runner(stdout="Error: Python script failed\n"))


def test_a_successful_run_without_a_report_is_reported_by_the_checks(root):
    """替身不建场景，所以现场报告不存在——验收必须如实说"先运行 build"，而不是放行。"""
    result = run(root, runner=_runner())

    assert result.report is None
    assert [finding.code for finding in result.findings] == ["report_missing"]
    assert result.fatal and result.fatal[0].severity == ERROR


def test_the_log_records_the_timeout_the_build_used(root):
    runner = _runner()

    run(root, runner=runner, timeout=42.0)

    assert runner.calls[0][2] == 42.0


# ---------------------------------------------------------------- 查找与默认值


def test_find_blender_reports_every_place_it_looked(monkeypatch, tmp_path, real_find_blender):
    monkeypatch.setattr(build_module, "find_blender", real_find_blender)
    monkeypatch.delenv(build_module.BLENDER_ENV, raising=False)
    monkeypatch.setattr(build_module, "DEFAULT_BLENDER", str(tmp_path / "also-missing"))

    with pytest.raises(SceneError) as excinfo:
        find_blender(str(tmp_path / "missing-blender"))

    message = str(excinfo.value)
    assert str(tmp_path / "missing-blender") in message
    assert build_module.BLENDER_ENV in message


def test_find_blender_prefers_an_explicit_path(tmp_path, real_find_blender, monkeypatch):
    monkeypatch.setattr(build_module, "find_blender", real_find_blender)
    fake = tmp_path / "blender"
    fake.write_text("#!/bin/sh\n", encoding="utf-8")
    fake.chmod(0o755)

    assert find_blender(str(fake)) == fake


def test_the_environment_variable_overrides_the_default(monkeypatch, tmp_path, real_find_blender):
    monkeypatch.setattr(build_module, "find_blender", real_find_blender)
    fake = tmp_path / "blender"
    fake.write_text("#!/bin/sh\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv(build_module.BLENDER_ENV, str(fake))

    assert find_blender() == fake


def test_the_default_spec_is_the_single_yaml_under_the_scene_directory(tmp_path):
    fixtures.write_spec(tmp_path)

    assert default_spec_path(tmp_path) == tmp_path / "_primer" / "scene" / "mission_layout.yaml"


def test_an_ambiguous_or_missing_spec_is_reported(tmp_path):
    with pytest.raises(SceneError, match="no scene spec found"):
        default_spec_path(tmp_path)

    fixtures.write_spec(tmp_path, name="a.yaml")
    fixtures.write_spec(tmp_path, name="b.yaml")
    with pytest.raises(SceneError, match="several scene specs found, pass --spec"):
        default_spec_path(tmp_path)


def test_the_plan_derives_every_path_from_the_output_boundary(tmp_path):
    plan = SceneBuildPlan(
        project_root=tmp_path, spec_path=tmp_path / "_primer" / "scene" / "mission_layout.yaml"
    )

    assert plan.scene_dir == tmp_path / "_primer" / "scene"
    assert plan.script == plan.scene_dir / "build" / SCRIPT_NAME
    assert plan.blend == plan.scene_dir / "mission_layout.blend"
    assert plan.commands()[0][1] == tmp_path


# ---------------------------------------------------------------- 命令行


def test_the_parser_registers_the_four_subcommands():
    parser = build_parser()
    actions = {}
    for action in parser._actions:
        if hasattr(action, "choices") and action.choices:
            actions = action.choices
    assert set(actions) == {"fetch", "build", "check", "report"}
    for name, sub in actions.items():
        assert sub.get_default("handler") is not None, name


def test_the_report_subcommand_prints_the_resolved_numbers(root, capsys):
    code = main(["report", "--root", str(root)])

    assert code == 0
    out = capsys.readouterr().out
    assert "布局参数：夹具场景" in out
    assert "monotonic_sequence" in out
    assert "earth" in out and "jupiter" in out
    assert "取景" in out


def test_check_reports_a_missing_build_without_crashing(root, capsys):
    code = main(["check", "--root", str(root)])

    assert code == 2
    out = capsys.readouterr().out
    assert "report_missing" in out
    assert "先运行 build" in out


def test_check_passes_on_a_staged_scene(tmp_path, capsys):
    fixtures.staged_scene(tmp_path)

    code = main(["check", "--root", str(tmp_path)])

    assert code == 0
    assert "结论：全部通过" in capsys.readouterr().out


def test_a_missing_spec_is_an_english_error_on_stderr(tmp_path, capsys):
    code = main(["report", "--root", str(tmp_path)])

    assert code == 2
    assert "scene error:" in capsys.readouterr().err


def test_fetch_without_the_network_reports_an_english_scene_error(root, capsys, monkeypatch):
    def explode(url, timeout):
        raise OSError("connection refused")

    monkeypatch.setattr(build_module, "_subprocess_runner", build_module._subprocess_runner)
    monkeypatch.setattr("primer.scene.assets._http_get", explode)

    code = main(["fetch", "--root", str(root)])

    assert code == 2
    error = capsys.readouterr().err
    assert "scene error: failed to download" in error
    assert "https://example.invalid" in error
