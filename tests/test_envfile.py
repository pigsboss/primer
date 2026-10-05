# -*- coding: utf-8 -*-
"""primer.envfile：.env 解析、免覆盖与发现顺序；literature web 的接线。"""

from __future__ import annotations

import os

import pytest

from primer.envfile import discover_env_file, load_env_file


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
    assert discover_env_file(cwd=cwd, package_candidates=package) is None
    (repo / ".env").write_text("A=1\n", encoding="utf-8")
    assert discover_env_file(cwd=cwd, package_candidates=package) == repo / ".env"
    (cwd / ".env").write_text("B=1\n", encoding="utf-8")
    assert discover_env_file(cwd=cwd, package_candidates=package) == cwd / ".env"


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
