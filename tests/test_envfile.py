# -*- coding: utf-8 -*-
"""primer.envfile：.env 解析、免覆盖与发现顺序；literature web 的接线。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from primer.envfile import (
    discover_env_file,
    env_file_candidates,
    is_user_level_env_file,
    load_env_file,
    permission_warning,
    user_config_dir,
    user_env_file,
)


def test_load_env_file_parses_minimal_syntax(tmp_path, monkeypatch):
    monkeypatch.delenv("PRIMER_TEST_KEY", raising=False)
    monkeypatch.delenv("PRIMER_TEST_QUOTED", raising=False)
    path = tmp_path / ".env"
    path.write_text(
        "# 注释\n"
        "\n"
        "export PRIMER_TEST_KEY=value=with=equals\n"
        'PRIMER_TEST_QUOTED="quoted value"\n'
        "not a valid line\n"
        "123KEY=bad\n",
        encoding="utf-8",
    )
    report = load_env_file(path)
    assert report.injected == ["PRIMER_TEST_KEY", "PRIMER_TEST_QUOTED"]
    assert os.environ["PRIMER_TEST_KEY"] == "value=with=equals"
    assert os.environ["PRIMER_TEST_QUOTED"] == "quoted value"
    assert report.skipped == []
    assert report.ignored_lines == [5, 6]


def test_load_env_file_handles_crlf_and_bom(tmp_path, monkeypatch):
    monkeypatch.delenv("PRIMER_TEST_CRLF", raising=False)
    path = tmp_path / ".env"
    path.write_bytes("\ufeffPRIMER_TEST_CRLF=ok\r\n".encode("utf-8"))
    report = load_env_file(path)
    assert report.injected == ["PRIMER_TEST_CRLF"]
    assert os.environ["PRIMER_TEST_CRLF"] == "ok"


def test_load_env_file_does_not_override_existing(tmp_path, monkeypatch):
    monkeypatch.setenv("PRIMER_TEST_KEEP", "real")
    path = tmp_path / ".env"
    path.write_text("PRIMER_TEST_KEEP=file\nPRIMER_TEST_NEW=file\n", encoding="utf-8")
    report = load_env_file(path)
    assert os.environ["PRIMER_TEST_KEEP"] == "real"
    assert report.skipped == ["PRIMER_TEST_KEEP"]
    assert report.injected == ["PRIMER_TEST_NEW"]


def test_load_env_file_missing_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_env_file(tmp_path / "nope.env")


def test_discover_env_file_order(tmp_path):
    cwd = tmp_path / "work"
    cwd.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    package = [repo / ".env"]
    # user_candidates=[] 是"这一级我不看"的开关：本机 ~/.config/primer/keys.env 是真实密钥文件,
    # 测试既不该读它, 也不该让它的存在改变发现结论。
    assert discover_env_file(cwd=cwd, user_candidates=[], package_candidates=package) is None
    (repo / ".env").write_text("A=1\n", encoding="utf-8")
    assert discover_env_file(cwd=cwd, user_candidates=[],
                             package_candidates=package) == repo / ".env"
    (cwd / ".env").write_text("B=1\n", encoding="utf-8")
    assert discover_env_file(cwd=cwd, user_candidates=[],
                             package_candidates=package) == cwd / ".env"


def test_env_file_candidates_order_is_cwd_user_package(tmp_path):
    cwd = tmp_path / "work"
    cwd.mkdir()
    user = tmp_path / "config" / "keys.env"
    package = [tmp_path / "repo" / ".env"]
    assert env_file_candidates(cwd=cwd, user_candidates=[user],
                               package_candidates=package) == [
        cwd / ".env", user, package[0]]


def test_discover_env_file_user_level_sits_between_cwd_and_package(tmp_path):
    cwd = tmp_path / "work"
    cwd.mkdir()
    user = tmp_path / "config" / "keys.env"
    user.parent.mkdir()
    package = [tmp_path / "repo" / ".env"]
    package[0].parent.mkdir()

    user.write_text("USER=1\n", encoding="utf-8")
    assert discover_env_file(cwd=cwd, user_candidates=[user],
                             package_candidates=package) == user

    package[0].write_text("PKG=1\n", encoding="utf-8")
    assert discover_env_file(cwd=cwd, user_candidates=[user],
                             package_candidates=package) == user

    (cwd / ".env").write_text("CWD=1\n", encoding="utf-8")
    assert discover_env_file(cwd=cwd, user_candidates=[user],
                             package_candidates=package) == cwd / ".env"


def test_user_env_file_path_follows_xdg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert user_config_dir() == tmp_path / "xdg" / "primer"
    assert user_env_file() == tmp_path / "xdg" / "primer" / "keys.env"
    assert is_user_level_env_file(user_env_file())


def test_load_env_file_warns_when_user_level_is_group_or_world_readable(tmp_path, monkeypatch):
    monkeypatch.delenv("PRIMER_TEST_USER_KEY", raising=False)
    path = tmp_path / "keys.env"
    path.write_text("PRIMER_TEST_USER_KEY=super-secret\n", encoding="utf-8")
    path.chmod(0o644)

    report = load_env_file(path, warn_permissions=True)
    assert report.injected == ["PRIMER_TEST_USER_KEY"]
    assert len(report.warnings) == 1
    warning = report.warnings[0]
    assert str(path) in warning
    assert "PRIMER_TEST_USER_KEY" in warning
    assert "0o644" in warning
    assert "super-secret" not in warning

    path.chmod(0o600)
    assert load_env_file(path, warn_permissions=True).warnings == []
    assert load_env_file(path, warn_permissions=False).warnings == []


def test_load_env_file_warns_automatically_for_the_machine_level_file(tmp_path, monkeypatch):
    monkeypatch.delenv("PRIMER_TEST_AUTO_KEY", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    path = user_env_file()
    path.parent.mkdir(parents=True)
    path.write_text("PRIMER_TEST_AUTO_KEY=value\n", encoding="utf-8")
    path.chmod(0o640)

    report = load_env_file(path)                          # warn_permissions=None → 自动判定
    assert report.warnings, "机器级密钥文件权限过宽时必须警告"
    assert "value" not in report.warnings[0]


def test_load_env_file_does_not_warn_for_project_level_files(tmp_path, monkeypatch):
    monkeypatch.delenv("PRIMER_TEST_PROJ_KEY", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    path = tmp_path / ".env"
    path.write_text("PRIMER_TEST_PROJ_KEY=value\n", encoding="utf-8")
    path.chmod(0o644)

    assert load_env_file(path).warnings == []


def test_permission_warning_mentions_skip_list_without_values():
    warning = permission_warning(Path("/home/u/.config/primer/keys.env"),
                                 ["A_KEY", "B_KEY"], 0o666)
    assert "/home/u/.config/primer/keys.env" in warning
    assert "A_KEY" in warning and "B_KEY" in warning
    assert "0o666" in warning


def test_discover_env_file_explicit_wins(tmp_path):
    explicit = tmp_path / "custom.env"
    found = discover_env_file(explicit, cwd=tmp_path, package_candidates=[])
    assert found == explicit
    with pytest.raises(FileNotFoundError):
        load_env_file(found)


def test_web_command_loads_env_file(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("PRIMER_TEST_WEB", raising=False)
    env_file = tmp_path / "custom.env"
    env_file.write_text("PRIMER_TEST_WEB=on\n", encoding="utf-8")

    import primer.literature.web.server as web_server

    seen: dict = {}
    monkeypatch.setattr(web_server, "serve", lambda **kwargs: seen.update(kwargs) or 0)

    from primer.literature.__main__ import _web_command, build_parser

    args = build_parser().parse_args(["web", "--env-file", str(env_file), "--no-open"])
    assert _web_command(args) == 0
    assert os.environ["PRIMER_TEST_WEB"] == "on"
    out = capsys.readouterr().out
    assert "PRIMER_TEST_WEB" in out
    assert "PRIMER_TEST_WEB=on" not in out
    assert seen["open_browser"] is False


def test_web_command_rejects_missing_env_file(tmp_path, capsys):
    from primer.literature.__main__ import _web_command, build_parser

    args = build_parser().parse_args(
        ["web", "--env-file", str(tmp_path / "nope.env"), "--no-open"]
    )
    assert _web_command(args) == 1
    assert "nope.env" in capsys.readouterr().err
