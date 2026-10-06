# -*- coding: utf-8 -*-
"""web 服务与 JSON API 的单元测试。

自造库＋线程服务器（127.0.0.1 临时端口）：首启流程、记录 CRUD 与错误映射、
扫描存在性、打开文件白名单、静态页与路径穿越防护、端口占用回退、外部改动回滚。
不访问外网、不启动浏览器。
"""

from __future__ import annotations

import json
import socket
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from primer.literature import Library
from primer.literature.web import server as S


def _record_payload(**overrides):
    payload = {
        "title": "样例论文",
        "type": "journal-article",
        "year": 2022,
        "files": [
            {"path": "原文/a.pdf", "nature": "doi-consistent"},
            {"path": "原文/b.pdf", "nature": "manual-upload"},
        ],
        "projects": ["甲/乙"],
    }
    payload.update(overrides)
    return payload


def _request(base, method, path, payload=None):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(base + path, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            body = response.read()
            try:
                return response.status, json.loads(body.decode("utf-8"))
            except json.JSONDecodeError:
                return response.status, body
    except urllib.error.HTTPError as exc:
        body = exc.read()
        try:
            return exc.code, json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            return exc.code, body


class _Server:
    def __init__(self, service):
        self.service = service
        self.httpd, self.port = S.create_server(service, 0)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.port}"

    def request(self, method, path, payload=None):
        return _request(self.base, method, path, payload)

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


@pytest.fixture()
def live(tmp_path):
    """已加载空库的服务。"""
    path = tmp_path / "primer.literature.json"
    Library.create(path)
    server = _Server(S.LibraryService.initial(str(path)))
    yield server
    server.close()


@pytest.fixture()
def live_bare(tmp_path):
    """未加载库的服务（库文件不存在）。"""
    server = _Server(S.LibraryService.initial(str(tmp_path / "nope.json")))
    yield server
    server.close()


def test_state_loaded(live):
    status, state = live.request("GET", "/api/state")
    assert status == 200
    assert state["loaded"] is True
    assert state["records"] == []
    assert state["tree"] == {"roots": [], "total": 0, "unfiled": 0}
    assert state["scan"] is None


def test_state_without_library(live_bare):
    status, state = live_bare.request("GET", "/api/state")
    assert status == 200
    assert state["loaded"] is False
    assert state["default_path"].endswith("nope.json")
    assert state["error"] == ""


def test_initial_load_error_surfaces(tmp_path):
    broken = tmp_path / "broken.json"
    broken.write_text("not json", encoding="utf-8")
    service = S.LibraryService.initial(str(broken))
    state = service.state_payload()
    assert state["loaded"] is False
    assert "invalid JSON" in state["error"]


def test_setup_new_and_open(live_bare, tmp_path):
    target = tmp_path / "fresh.json"

    status, state = live_bare.request("POST", "/api/library", {"path": str(target), "mode": "new"})
    assert status == 200 and state["loaded"] is True
    assert target.read_text(encoding="utf-8") == "[]\n"

    status, err = live_bare.request("POST", "/api/library", {"path": str(target), "mode": "new"})
    assert status == 409 and err["error"] == "library_error"

    status, state = live_bare.request("POST", "/api/library", {"path": str(target), "mode": "open"})
    assert status == 200 and state["loaded"] is True

    status, err = live_bare.request("POST", "/api/library", {"path": str(target), "mode": "wipe"})
    assert status == 400

    status, err = live_bare.request("POST", "/api/library", {"mode": "new"})
    assert status == 400 and "path is required" in err["message"]


def test_open_broken_json_reports_position(live_bare, tmp_path):
    broken = tmp_path / "broken.json"
    broken.write_text('[{"uuid": }]', encoding="utf-8")
    status, err = live_bare.request("POST", "/api/library", {"path": str(broken), "mode": "open"})
    assert status == 400
    assert "invalid JSON" in err["message"] and "line 1" in err["message"]


def test_record_crud_roundtrip(live):
    status, created = live.request("POST", "/api/records", _record_payload())
    assert status == 201
    uid = created["record"]["uuid"]

    _, state = live.request("GET", "/api/state")
    assert len(state["records"]) == 1
    assert state["tree"]["roots"][0]["name"] == "甲"

    status, updated = live.request("PUT", f"/api/records/{uid}", {"title": "改后标题"})
    assert status == 200 and updated["record"]["title"] == "改后标题"

    status, err = live.request("PUT", "/api/records/missing-uuid", {"title": "x"})
    assert status == 404

    status, removed = live.request("DELETE", f"/api/records/{uid}")
    assert status == 200 and removed["record"]["uuid"] == uid

    _, state = live.request("GET", "/api/state")
    assert state["records"] == []

    status, err = live.request("DELETE", f"/api/records/{uid}")
    assert status == 404


def test_validation_and_duplicate_mapping(live):
    status, err = live.request("POST", "/api/records", {"title": ""})
    assert status == 400 and "new record.title" in err["message"]

    _, created = live.request("POST", "/api/records", _record_payload())
    uid = created["record"]["uuid"]
    status, err = live.request("POST", "/api/records", _record_payload(uuid=uid))
    assert status == 409 and "duplicate uuid" in err["message"]


def test_crud_before_setup(tmp_path):
    server = _Server(S.LibraryService.initial(str(tmp_path / "nope.json")))
    try:
        status, err = server.request("POST", "/api/records", {"title": "x"})
        assert status == 400 and "no library loaded" in err["message"]
        status, err = server.request("POST", "/api/scan")
        assert status == 400 and "no library loaded" in err["message"]
    finally:
        server.close()


def test_scan_reports_file_presence_and_keeps_library(live, tmp_path):
    data_dir = tmp_path / "原文"
    data_dir.mkdir()
    (data_dir / "a.pdf").write_bytes(b"%PDF-1.4")
    _, created = live.request("POST", "/api/records", _record_payload())
    uid = created["record"]["uuid"]

    library_path = tmp_path / "primer.literature.json"
    before = library_path.read_text(encoding="utf-8")

    status, scan = live.request("POST", "/api/scan")
    assert status == 200
    assert [entry["exists"] for entry in scan["scan"][uid]] == [True, False]

    assert library_path.read_text(encoding="utf-8") == before
    _, state = live.request("GET", "/api/state")
    assert state["scan"] == scan["scan"]


def test_open_only_registered_paths(live, tmp_path, monkeypatch):
    real = tmp_path / "原文" / "a.pdf"
    real.parent.mkdir()
    real.write_bytes(b"%PDF-1.4")
    opened = []
    monkeypatch.setattr(S, "open_path", lambda path: opened.append(path))

    _, created = live.request("POST", "/api/records", _record_payload())
    uid = created["record"]["uuid"]

    status, response = live.request("POST", "/api/open", {"uuid": uid, "index": 0})
    assert status == 200 and response["opened"] == "原文/a.pdf"
    assert opened == [real]

    status, err = live.request("POST", "/api/open", {"uuid": uid, "index": 1})
    assert status == 400 and "not found on disk" in err["message"]

    status, err = live.request("POST", "/api/open", {"uuid": uid, "index": 5})
    assert status == 400 and "out of range" in err["message"]

    status, err = live.request("POST", "/api/open", {"uuid": uid, "index": "0"})
    assert status == 400 and "index" in err["message"]

    status, err = live.request("POST", "/api/open", {"uuid": "missing", "index": 0})
    assert status == 404


def test_static_index_and_traversal_guard(live):
    status, body = live.request("GET", "/")
    assert status == 200 and b"PRIMER" in body

    for probe in ("/static/%2e%2e/server.py", "/static/../server.py", "/static/nope.js"):
        status, _ = live.request("GET", probe)
        assert status == 404

    status, err = live.request("GET", "/api/nope")
    assert status == 404 and err["error"] == "not_found"


def test_invalid_json_body(live):
    request = urllib.request.Request(
        live.base + "/api/records",
        data=b"{nope",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(request, timeout=10)
    assert excinfo.value.code == 400
    payload = json.loads(excinfo.value.read().decode("utf-8"))
    assert payload["error"] == "invalid_json"


def test_external_change_conflict_and_rollback(live, tmp_path):
    library_path = tmp_path / "primer.literature.json"
    external = json.dumps(
        [{"uuid": "ext-1", "title": "外部版本"}], ensure_ascii=False, indent=2
    ) + "\n"
    library_path.write_text(external, encoding="utf-8")

    status, err = live.request("POST", "/api/records", {"title": "内存新增"})
    assert status == 409 and "changed on disk" in err["message"]

    _, state = live.request("GET", "/api/state")
    assert [record["title"] for record in state["records"]] == ["外部版本"]


def test_port_fallback_when_busy(tmp_path):
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    busy_port = blocker.getsockname()[1]
    service = S.LibraryService.initial(str(tmp_path / "nope.json"))
    try:
        httpd, actual_port = S.create_server(service, busy_port)
        try:
            assert actual_port != busy_port
        finally:
            httpd.server_close()
    finally:
        blocker.close()


def test_static_assets_served(live):
    status, body = live.request("GET", "/static/app.js")
    assert status == 200 and len(body) > 1000
    status, body = live.request("GET", "/static/style.css")
    assert status == 200 and len(body) > 500


def test_index_references_assets(live):
    status, body = live.request("GET", "/")
    assert status == 200
    assert b"/static/app.js" in body
    assert b"/static/style.css" in body


def test_save_flush(live):
    live.request("POST", "/api/records", _record_payload())
    status, data = live.request("POST", "/api/save")
    assert status == 200 and data["saved"] is True


def test_save_requires_library(tmp_path):
    server = _Server(S.LibraryService.initial(str(tmp_path / "nope.json")))
    try:
        status, err = server.request("POST", "/api/save")
        assert status == 400 and "no library loaded" in err["message"]
    finally:
        server.close()


def test_save_as_copies_switches_and_rejects(live, tmp_path):
    live.request("POST", "/api/records", _record_payload())
    current = tmp_path / "primer.literature.json"

    status, err = live.request("POST", "/api/save-as", {"path": str(current)})
    assert status == 400 and "current library" in err["message"]

    target = tmp_path / "copy.json"
    status, state = live.request("POST", "/api/save-as", {"path": str(target)})
    assert status == 200 and state["path"] == str(target)
    assert len(json.loads(target.read_text(encoding="utf-8"))) == 1

    status, err = live.request("POST", "/api/save-as", {"path": str(target)})
    assert status == 400 and "current library" in err["message"]  # 目标即当前库

    status, err = live.request("POST", "/api/save-as", {"path": str(current)})
    assert status == 409  # 目标已存在（且不是当前库）

    live.request("POST", "/api/records", {"title": "切换后的第二条"})
    assert len(json.loads(target.read_text(encoding="utf-8"))) == 2


def test_export_all_and_subset(live, tmp_path):
    _, created = live.request("POST", "/api/records", _record_payload())
    uid = created["record"]["uuid"]
    live.request("POST", "/api/records", {"title": "第二条"})

    export_all = tmp_path / "all.json"
    status, data = live.request("POST", "/api/export", {"path": str(export_all)})
    assert status == 200 and data["exported"] == 2
    assert len(json.loads(export_all.read_text(encoding="utf-8"))) == 2

    export_one = tmp_path / "one.json"
    status, data = live.request("POST", "/api/export", {"path": str(export_one), "uuids": [uid]})
    assert status == 200 and data["exported"] == 1
    records = json.loads(export_one.read_text(encoding="utf-8"))
    assert [record["uuid"] for record in records] == [uid]

    _, state = live.request("GET", "/api/state")
    assert state["path"].endswith("primer.literature.json")

    status, err = live.request("POST", "/api/export", {"path": str(export_all)})
    assert status == 409


def test_import_merges_renames_and_invalidates_scan(live, tmp_path):
    _, created = live.request("POST", "/api/records", _record_payload())
    uid = created["record"]["uuid"]

    source = tmp_path / "source.json"
    source.write_text(
        json.dumps(
            [
                {"uuid": uid, "title": "同 uuid 的记录"},
                {"uuid": "fresh-1", "title": "新记录"},
            ],
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    status, _ = live.request("POST", "/api/scan")
    assert status == 200

    status, data = live.request("POST", "/api/import", {"path": str(source)})
    assert status == 200 and data["imported"] == 2 and data["renamed"] == 1

    _, state = live.request("GET", "/api/state")
    assert len(state["records"]) == 3
    assert sorted(r["title"] for r in state["records"]) == ["同 uuid 的记录", "新记录", "样例论文"]
    assert state["scan"] is None  # 导入后旧扫描作废

    status, err = live.request("POST", "/api/import", {"path": state["path"]})
    assert status == 400 and "into itself" in err["message"]

    bad = tmp_path / "bad.json"
    bad.write_text("nope", encoding="utf-8")
    status, err = live.request("POST", "/api/import", {"path": str(bad)})
    assert status == 400 and "invalid JSON" in err["message"]


def _upload(base, path, body: bytes):
    request = urllib.request.Request(base + path, data=body, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def test_import_upload_csv_mapping_flow(live):
    csv_bytes = "标题,作者,年份\n甲,张三; 李四,2022\n,缺标题行,2020\n".encode("utf-8")
    status, preview = _upload(live.base, "/api/import/parse?name=refs.csv", csv_bytes)
    assert status == 200
    assert preview["kind"] == "table" and preview["total"] == 2
    assert preview["suggested"]["标题"] == "title"

    status, result = live.request("POST", "/api/import/commit", {
        "token": preview["token"],
        "mapping": {"标题": "title", "作者": "authors", "年份": "year"},
    })
    assert status == 200
    assert result == {"imported": 1, "renamed": 0, "skipped": 1, "files": 0, "files_renamed": 0}

    _, state = live.request("GET", "/api/state")
    assert [record["title"] for record in state["records"]] == ["甲"]
    assert state["records"][0]["authors"] == ["张三", "李四"]
    assert state["records"][0]["year"] == 2022


def test_import_upload_json_library_conflict_rename(live):
    _, created = live.request("POST", "/api/records", _record_payload())
    uid = created["record"]["uuid"]
    source = [
        {"uuid": uid, "title": "冲突记录"},
        {"uuid": "fresh-2", "title": "新记录"},
        {"kind": "file", "uuid": "file-1", "path": "x.pdf", "record_uuid": uid},
    ]
    body = json.dumps(source, ensure_ascii=False).encode("utf-8")
    status, preview = _upload(live.base, "/api/import/parse?name=lib.json", body)
    assert status == 200 and preview["kind"] == "library" and preview["total"] == 2

    status, result = live.request("POST", "/api/import/commit", {"token": preview["token"]})
    assert status == 200 and result["imported"] == 2 and result["renamed"] == 1
    assert result["files"] == 1

    # uuid 冲突的记录被改名后，导入文件记录的关联必须跟着改写
    _, state = live.request("GET", "/api/state")
    file_item = state["file_records"][0]
    renamed = [item for item in state["records"] if item["title"] == "冲突记录"][0]
    assert renamed["uuid"] != uid
    assert file_item["record_uuid"] == renamed["uuid"]


def test_import_upload_bib_flow(live):
    bib = (
        "@article{k1,\n"
        "  title = {Bib 记录},\n"
        "  author = {A and B},\n"
        "  year = 2021,\n"
        "}\n"
    ).encode("utf-8")
    status, preview = _upload(live.base, "/api/import/parse?name=refs.bib", bib)
    assert status == 200 and preview["kind"] == "table"
    assert preview["suggested"]["title"] == "title"

    status, result = live.request("POST", "/api/import/commit", {
        "token": preview["token"],
        "mapping": {"title": "title", "author": "authors", "year": "year"},
    })
    assert status == 200 and result["imported"] == 1


def test_import_upload_markdown_flow(live):
    md = (
        "# 综述参考文献\n"
        "## A 战略报告与规划\n"
        "[1] Author A. Title One. Journal X, 2003. ［原文：待图书馆获取］\n"
        "[2] 张三, 李四. 标题二. 中文期刊, 2020.\n"
    ).encode("utf-8")
    status, preview = _upload(live.base, "/api/import/parse?name=refs.md", md)
    assert status == 200 and preview["kind"] == "table" and preview["total"] == 2
    assert preview["suggested"]["标题"] == "title"
    assert preview["suggested"]["出处"] == "venue"

    status, result = live.request("POST", "/api/import/commit", {
        "token": preview["token"],
        "mapping": {"标题": "title", "作者": "authors", "年份": "year", "出处": "venue"},
    })
    assert status == 200 and result["imported"] == 2

    _, state = live.request("GET", "/api/state")
    by_title = {record["title"]: record for record in state["records"]}
    assert by_title["Title One"]["year"] == 2003
    assert by_title["Title One"]["venue"] == "Journal X, 2003"
    assert by_title["标题二"]["authors"] == ["张三, 李四"]


def test_import_commit_unknown_token(live):
    status, err = live.request("POST", "/api/import/commit", {"token": "nope"})
    assert status == 400 and "token" in err["message"]


def test_import_parse_rejects_unsupported_and_empty(live):
    status, err = _upload(live.base, "/api/import/parse?name=a.docx", b"data")
    assert status == 400 and "unsupported" in err["message"]

    status, err = _upload(live.base, "/api/import/parse?name=a.csv", b"title,author\n")
    assert status == 400 and "no records" in err["message"]


def test_import_parse_requires_library(tmp_path):
    server = _Server(S.LibraryService.initial(str(tmp_path / "nope.json")))
    try:
        status, err = _upload(server.base, "/api/import/parse?name=a.csv", b"t\nx\n")
        assert status == 400 and "no library loaded" in err["message"]
    finally:
        server.close()


def _poll_refine(server, token, attempts=100):
    import time

    result = {}
    status = 0
    for _ in range(attempts):
        status, result = server.request("POST", "/api/import/refine/status", {"token": token})
        if status != 200 or result.get("status") != "running":
            return status, result
        time.sleep(0.02)
    return status, result


def test_import_refine_flow_with_fake_chat(tmp_path):
    path = tmp_path / "primer.literature.json"
    Library.create(path)
    reply = json.dumps(
        [
            {"i": 0, "title": "Title One", "authors": ["Author A"], "year": 2003,
             "venue": "Journal X", "type": "journal-article", "doi": None},
            {"i": 1, "title": "修补标题", "authors": ["某甲"], "year": 2022,
             "venue": "期刊Y", "type": "report", "doi": "10.9999/编造"},
        ],
        ensure_ascii=False,
    )
    service = S.LibraryService.initial(str(path), chat_sender=lambda system, user: reply)
    server = _Server(service)
    try:
        md = (
            "# 参考\n"
            "## A 战略\n"
            "[1] Author A. Title One. Journal X, 2003.\n"
            "[2] 只有一段没有句点\n"
        ).encode("utf-8")
        status, preview = _upload(server.base, "/api/import/parse?name=refs.md", md)
        assert status == 200 and preview["low_count"] == 1

        status, started = server.request(
            "POST", "/api/import/refine", {"token": preview["token"], "scope": "all"}
        )
        assert status == 200 and started["started"] is True and started["total"] == 2

        status, result = _poll_refine(server, preview["token"])
        assert status == 200 and result["status"] == "done", result
        assert result["refined"] == 2
        assert "类型" in result["preview"]["columns"]
        assert result["preview"]["suggested"]["类型"] == "type"

        status, commit = server.request("POST", "/api/import/commit", {
            "token": preview["token"],
            "mapping": {"标题": "title", "作者": "authors", "年份": "year",
                        "出处": "venue", "类型": "type", "DOI": "doi"},
        })
        assert status == 200 and commit["imported"] == 2
        _, state = server.request("GET", "/api/state")
        by_title = {record["title"]: record for record in state["records"]}
        assert by_title["修补标题"]["authors"] == ["某甲"]
        assert by_title["Title One"]["venue"] == "Journal X"
        assert by_title["修补标题"].get("doi") is None  # 编造 DOI 被丢弃
    finally:
        server.close()


def test_import_refine_failure_reports_and_still_commits(tmp_path):
    path = tmp_path / "primer.literature.json"
    Library.create(path)

    def broken(system, user):
        raise RuntimeError("no api key for test")

    service = S.LibraryService.initial(str(path), chat_sender=broken)
    server = _Server(service)
    try:
        md = "# 参考\n## A\n[1] Author A. Title One. Journal X, 2003.\n".encode("utf-8")
        status, preview = _upload(server.base, "/api/import/parse?name=refs.md", md)
        assert status == 200

        status, started = server.request(
            "POST", "/api/import/refine", {"token": preview["token"], "scope": "all"}
        )
        assert status == 200 and started["started"] is True

        status, result = _poll_refine(server, preview["token"])
        assert result["status"] == "failed" and "no api key" in result["error"]

        status, commit = server.request(
            "POST", "/api/import/commit", {"token": preview["token"], "mapping": {"标题": "title"}}
        )
        assert status == 200 and commit["imported"] == 1
    finally:
        server.close()


def test_import_refine_unknown_token(live):
    status, err = live.request("POST", "/api/import/refine", {"token": "nope", "scope": "all"})
    assert status == 400 and "unknown" in err["message"]


def test_clear_library_endpoint(live, tmp_path):
    live.request("POST", "/api/records", _record_payload())
    live.request("POST", "/api/records", {"title": "第二条"})

    status, result = live.request("POST", "/api/library/clear")
    assert status == 200 and result["cleared"] == 2

    _, state = live.request("GET", "/api/state")
    assert state["records"] == []
    assert (tmp_path / "primer.literature.json").read_text(encoding="utf-8") == "[]\n"

    status, result = live.request("POST", "/api/library/clear")
    assert status == 200 and result["cleared"] == 0


def test_find_config_root_prefers_library_location(tmp_path):
    project = tmp_path / "proj"
    (project / "_primer").mkdir(parents=True)
    (project / "_primer" / "config.yaml").write_text("roles: {}\n", encoding="utf-8")
    library = project / "_primer" / "literature" / "db.json"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    assert S.LibraryService._find_config_root(library, elsewhere) == project


def test_find_config_root_falls_back_to_cwd(tmp_path):
    assert S.LibraryService._find_config_root(None, tmp_path) == tmp_path


def test_build_sender_reports_missing_role_with_path(tmp_path, monkeypatch):
    from primer.config import ConfigError

    project = tmp_path / "proj"
    (project / "_primer").mkdir(parents=True)
    (project / "_primer" / "config.yaml").write_text(
        "providers: {}\nroles: {}\n", encoding="utf-8"
    )
    library_path = project / "_primer" / "literature" / "db.json"
    library_path.parent.mkdir(parents=True)
    Library.create(library_path)
    service = S.LibraryService.initial(str(library_path))
    monkeypatch.chdir(project)

    with pytest.raises(ConfigError, match="no LLM role"):
        service._build_sender()


def test_import_verify_flow_with_fake_lookup(tmp_path):
    path = tmp_path / "primer.literature.json"
    Library.create(path)

    def fake_lookup(title):
        if "title one" in title.lower():
            return [{
                "title": "Title One", "authors": ["Author A"], "year": 2003,
                "venue": "Journal X", "type": "journal-article", "doi": "10.1000/xyz",
            }]
        return []

    service = S.LibraryService.initial(str(path), web_lookup=fake_lookup)
    server = _Server(service)
    try:
        md = (
            "# 参考\n## A\n"
            "[1] Author A. Title One. Journal X, 2003.\n"
            "[2] 只有一段没有句点\n"
        ).encode("utf-8")
        status, preview = _upload(server.base, "/api/import/parse?name=refs.md", md)
        assert status == 200

        status, started = server.request(
            "POST", "/api/import/verify", {"token": preview["token"], "scope": "all"}
        )
        assert status == 200 and started["started"] is True and started["total"] == 2

        status, result = _poll_refine(server, preview["token"])
        assert status == 200 and result["status"] == "done", result
        assert result["refined"] == 1 and result["failed"] == 1

        status, commit = server.request("POST", "/api/import/commit", {
            "token": preview["token"],
            "mapping": {"标题": "title", "作者": "authors", "年份": "year",
                        "出处": "venue", "DOI": "doi"},
        })
        assert status == 200 and commit["imported"] == 2
        _, state = server.request("GET", "/api/state")
        by_title = {record["title"]: record for record in state["records"]}
        assert by_title["Title One"]["doi"] == "10.1000/xyz"
        assert by_title["Title One"]["notes"].startswith("原始记录：")
    finally:
        server.close()


def _poll_enrich(server, token):
    import time

    for _ in range(100):
        status, payload = server.request("POST", "/api/records/enrich/status", {"token": token})
        assert status == 200, payload
        if payload["status"] != "running":
            return status, payload
        time.sleep(0.05)
    raise AssertionError("enrich job did not finish")


def test_records_enrich_verify_and_bulk_ops(tmp_path):
    path = tmp_path / "primer.literature.json"
    Library.create(path)

    def fake_lookup(title):
        if "kepler" in title.lower():
            return [{
                "title": "Kepler Planet-Detection Mission: Introduction and First Results",
                "authors": ["William J. Borucki", "David Koch"],
                "year": 2010,
                "venue": "Science",
                "type": "journal-article",
                "doi": "10.1126/science.1185402",
            }]
        return []

    service = S.LibraryService.initial(str(path), web_lookup=fake_lookup)
    server = _Server(service)
    try:
        _, data = server.request(
            "POST", "/api/records",
            _record_payload(title="Kepler planet detection mission intro and first results", year=2010),
        )
        first = data["record"]["uuid"]
        _, data = server.request("POST", "/api/records", _record_payload(title="行星探测三十年综述"))
        second = data["record"]["uuid"]

        status, started = server.request(
            "POST", "/api/records/enrich", {"uuids": [first, second], "mode": "verify"}
        )
        assert status == 200 and started["started"] is True and started["total"] == 2

        status, result = _poll_enrich(server, started["token"])
        assert result["status"] == "done", result
        assert (result["updated"], result["skipped"], result["failed"]) == (1, 1, 0)

        # 两阶段：未应用前记录不变
        _, snapshot = server.request("GET", "/api/state")
        record = next(item for item in snapshot["records"] if item["uuid"] == first)
        assert record["doi"] is None

        status, preview = server.request(
            "POST", "/api/records/enrich/preview", {"token": started["token"]}
        )
        assert status == 200
        assert [item["uuid"] for item in preview["pending"]] == [first]
        fields = {change["field"]: change["new"] for change in preview["pending"][0]["changes"]}
        assert fields["doi"] == "10.1126/science.1185402"
        assert fields["venue"] == "Science"

        status, applied = server.request(
            "POST", "/api/records/enrich/apply", {"token": started["token"]}
        )
        assert status == 200 and applied["updated"] == 1

        _, snapshot = server.request("GET", "/api/state")
        record = next(item for item in snapshot["records"] if item["uuid"] == first)
        assert record["doi"] == "10.1126/science.1185402"
        assert record["venue"] == "Science"
        assert record["authors"] == ["William J. Borucki", "David Koch"]

        # 重复应用：已无待应用变更
        status, _ = server.request(
            "POST", "/api/records/enrich/apply", {"token": started["token"]}
        )
        assert status == 400

        status, data = server.request(
            "POST", "/api/records/bulk-update",
            {"uuids": [first, second], "fields": {"venue": "合集"}},
        )
        assert status == 200 and data["updated"] == 2

        status, data = server.request("POST", "/api/records/delete", {"uuids": [second]})
        assert status == 200 and data["deleted"] == 1
        _, snapshot = server.request("GET", "/api/state")
        assert [item["uuid"] for item in snapshot["records"]] == [first]
    finally:
        server.close()


def test_records_enrich_ai_with_fake_chat(tmp_path):
    path = tmp_path / "primer.literature.json"
    Library.create(path)

    def fake_chat(system, user):
        return ('[{"i": 0, "title": "题录校订样例", "authors": ["张三"], "year": 2001, '
                '"venue": "样例期刊", "type": "journal-article", "doi": null}]')

    service = S.LibraryService.initial(str(path), chat_sender=fake_chat)
    server = _Server(service)
    try:
        _, data = server.request(
            "POST", "/api/records", _record_payload(title="题录校订样例", year=None, venue="")
        )
        uuid = data["record"]["uuid"]

        status, started = server.request(
            "POST", "/api/records/enrich", {"uuids": [uuid], "mode": "ai"}
        )
        assert status == 200 and started["started"] is True

        status, result = _poll_enrich(server, started["token"])
        assert result["status"] == "done", result
        assert result["updated"] == 1

        status, preview = server.request(
            "POST", "/api/records/enrich/preview", {"token": started["token"]}
        )
        assert status == 200 and len(preview["pending"]) == 1
        fields = {change["field"]: change["new"] for change in preview["pending"][0]["changes"]}
        assert fields["venue"] == "样例期刊" and fields["year"] == 2001

        # 未应用前记录不变
        _, snapshot = server.request("GET", "/api/state")
        assert snapshot["records"][0]["venue"] == ""

        status, applied = server.request(
            "POST", "/api/records/enrich/apply", {"token": started["token"]}
        )
        assert status == 200 and applied["updated"] == 1

        _, snapshot = server.request("GET", "/api/state")
        record = snapshot["records"][0]
        assert record["venue"] == "样例期刊"
        assert record["authors"] == ["张三"]
        assert record["year"] == 2001
    finally:
        server.close()


def test_records_enrich_engine_fallback(tmp_path):
    path = tmp_path / "primer.literature.json"
    Library.create(path)

    def broken(title):
        raise ConnectionError("down")

    def fallback(title):
        return [{
            "title": "Fallback Title", "authors": [], "year": None,
            "venue": None, "type": "journal-article", "doi": "10.1000/fb",
        }]

    service = S.LibraryService.initial(str(path), web_lookup=[broken, fallback])
    server = _Server(service)
    try:
        _, data = server.request("POST", "/api/records", _record_payload(title="Fallback Title"))
        uuid = data["record"]["uuid"]

        status, started = server.request(
            "POST", "/api/records/enrich", {"uuids": [uuid], "mode": "verify"}
        )
        assert status == 200 and started["started"] is True

        status, result = _poll_enrich(server, started["token"])
        assert result["status"] == "done", result
        assert result["updated"] == 1

        status, preview = server.request(
            "POST", "/api/records/enrich/preview", {"token": started["token"]}
        )
        assert status == 200 and preview["pending"][0]["uuid"] == uuid

        status, applied = server.request(
            "POST", "/api/records/enrich/apply", {"token": started["token"]}
        )
        assert status == 200 and applied["updated"] == 1
        _, snapshot = server.request("GET", "/api/state")
        assert snapshot["records"][0]["doi"] == "10.1000/fb"
    finally:
        server.close()


def test_import_verify_engine_fallback(tmp_path):
    path = tmp_path / "primer.literature.json"
    Library.create(path)

    def broken(title):
        raise ConnectionError("down")

    def fallback(title):
        if "title one" in title.lower():
            return [{
                "title": "Title One", "authors": ["Author A"], "year": 2003,
                "venue": "Journal X", "type": "journal-article", "doi": "10.1000/xyz",
            }]
        return []

    service = S.LibraryService.initial(str(path), web_lookup=[broken, fallback])
    server = _Server(service)
    try:
        md = (
            "# 参考\n## A\n"
            "[1] Author A. Title One. Journal X, 2003.\n"
            "[2] 只有一段没有句点\n"
        ).encode("utf-8")
        status, preview = _upload(server.base, "/api/import/parse?name=refs.md", md)
        assert status == 200

        status, started = server.request(
            "POST", "/api/import/verify", {"token": preview["token"], "scope": "all"}
        )
        assert status == 200 and started["started"] is True

        status, result = _poll_refine(server, preview["token"])
        assert result["status"] == "done", result
        assert result["refined"] == 1 and result["failed"] == 1

        status, commit = server.request("POST", "/api/import/commit", {
            "token": preview["token"],
            "mapping": {"标题": "title", "作者": "authors", "年份": "year",
                        "出处": "venue", "DOI": "doi"},
        })
        assert status == 200 and commit["imported"] == 2
        _, state = server.request("GET", "/api/state")
        by_title = {record["title"]: record for record in state["records"]}
        assert by_title["Title One"]["doi"] == "10.1000/xyz"
    finally:
        server.close()


def _tree_paths(tree):
    paths = []

    def walk(nodes):
        for node in nodes:
            paths.append(node["path"])
            walk(node.get("children") or [])

    walk(tree.get("roots") or [])
    return paths


def test_project_operations(tmp_path):
    path = tmp_path / "primer.literature.json"
    Library.create(path)
    service = S.LibraryService.initial(str(path))
    server = _Server(service)
    try:
        _, data = server.request(
            "POST", "/api/records", _record_payload(title="甲记录", projects=["旧目录/子项"])
        )
        first = data["record"]["uuid"]

        # 新建空项目（含子项目）：进树、计数 0
        status, data = server.request("POST", "/api/projects/create", {"path": "新目录/空子项"})
        assert status == 200 and data["path"] == "新目录/空子项" and data["updated"] == 0
        _, state = server.request("GET", "/api/state")
        paths = _tree_paths(state["tree"])
        assert "新目录" in paths and "新目录/空子项" in paths

        # 新建并挂到选中记录
        status, data = server.request(
            "POST", "/api/projects/create", {"path": "挂载项", "uuids": [first]}
        )
        assert data["updated"] == 1

        # 移动：旧目录/子项 → 挂载项 下
        status, data = server.request(
            "POST", "/api/projects/move", {"path": "旧目录/子项", "target": "挂载项"}
        )
        assert status == 200 and data["path"] == "挂载项/子项" and data["updated"] == 1

        # 重命名：挂载项 → 已挂载（记录路径同步）
        status, data = server.request(
            "POST", "/api/projects/rename", {"path": "挂载项", "name": "已挂载"}
        )
        assert status == 200 and data["path"] == "已挂载" and data["updated"] == 1
        _, state = server.request("GET", "/api/state")
        assert sorted(state["records"][0]["projects"]) == ["已挂载", "已挂载/子项"]

        sidecar = tmp_path / "primer.literature.projects.json"
        assert sidecar.is_file()
        declared = json.loads(sidecar.read_text(encoding="utf-8"))["projects"]
        assert "新目录/空子项" in declared and "已挂载" in declared

        # 删除（含子项目）：记录与注册表同步
        status, data = server.request("POST", "/api/projects/delete", {"path": "已挂载"})
        assert status == 200 and data["updated"] == 1
        _, state = server.request("GET", "/api/state")
        assert state["records"][0]["projects"] == []
        assert "已挂载" not in _tree_paths(state["tree"])

        # 非法操作：移入自身子孙 / 名称带斜杠
        status, _ = server.request(
            "POST", "/api/projects/move", {"path": "新目录", "target": "新目录/空子项"}
        )
        assert status == 400
        status, _ = server.request(
            "POST", "/api/projects/rename", {"path": "新目录", "name": "a/b"}
        )
        assert status == 400

        # 重新起一个服务实例：声明项目从旁车文件恢复
        reloaded = S.LibraryService.initial(str(path))
        assert "新目录/空子项" in reloaded.declared
    finally:
        server.close()


def test_project_attach(tmp_path):
    path = tmp_path / "primer.literature.json"
    Library.create(path)
    service = S.LibraryService.initial(str(path))
    server = _Server(service)
    try:
        _, data = server.request(
            "POST", "/api/records", _record_payload(title="记录甲", projects=[])
        )
        first = data["record"]["uuid"]
        _, data = server.request(
            "POST", "/api/records", _record_payload(title="记录乙", projects=[])
        )
        second = data["record"]["uuid"]

        # 挂到既有项目
        server.request("POST", "/api/projects/create", {"path": "甲组"})
        status, data = server.request(
            "POST", "/api/projects/attach", {"path": "甲组", "uuids": [first, second]}
        )
        assert status == 200 and data["created"] is False and data["updated"] == 2

        # 重复挂载：跳过计数
        status, data = server.request(
            "POST", "/api/projects/attach", {"path": "甲组", "uuids": [first]}
        )
        assert data["updated"] == 0

        # 挂到不存在路径：自动声明（空项目机制）
        status, data = server.request(
            "POST", "/api/projects/attach", {"path": "新组/子组", "uuids": [first]}
        )
        assert data["created"] is True and data["updated"] == 1
        _, state = server.request("GET", "/api/state")
        record = next(item for item in state["records"] if item["uuid"] == first)
        assert sorted(record["projects"]) == ["新组/子组", "甲组"]

        # 空 uuids 拒绝
        status, _ = server.request(
            "POST", "/api/projects/attach", {"path": "甲组", "uuids": []}
        )
        assert status == 400
    finally:
        server.close()


def test_files_scan_parse_and_delete(tmp_path):
    import time
    import zipfile as _zipfile

    from primer.literature.backends import ParseOutcome

    path = tmp_path / "primer.literature.json"
    Library.create(path)
    scans = tmp_path / "scans"
    scans.mkdir()
    (scans / "a.pdf").write_bytes(b"%PDF-1.4 fake")
    (scans / "b.pdf").write_bytes(b"%PDF-1.4 fake-b")
    (scans / "note.txt").write_text("ignore me", encoding="utf-8")

    parsed: list = []

    def fake_parse(sources, output_dir):
        outputs = {}
        for source in sources:
            parsed.append(Path(source).name)
            archive = Path(output_dir) / (Path(source).stem + ".zip")
            with _zipfile.ZipFile(archive, "w") as handle:
                handle.writestr("markdown.md", "# parsed\n\n![](images/img1.png)\n")
                handle.writestr("images/img1.png", b"PNG")
                handle.writestr("model_output.json", "{}")
            outputs[Path(source)] = archive
        return ParseOutcome(command=("fake",), returncode=0, outputs=outputs)

    service = S.LibraryService.initial(
        str(path), parser=fake_parse, picker=lambda kind: [str(scans)]
    )
    server = _Server(service)
    try:
        status, data = server.request("POST", "/api/files/scan", {"path": str(scans)})
        assert status == 200 and data["added"] == 2 and data["skipped"] == 0

        # 二次扫描：全部跳过（按绝对路径去重）
        status, data = server.request("POST", "/api/files/scan", {"path": str(scans)})
        assert data["added"] == 0 and data["skipped"] == 2

        # 等解析队列跑完
        deadline = time.time() + 10
        listing = {"files": [], "parsing": -1}
        while time.time() < deadline:
            _, listing = server.request("GET", "/api/files")
            if listing["parsing"] == 0:
                break
            time.sleep(0.05)
        assert listing["parsing"] == 0
        assert sorted(item["status"] for item in listing["files"]) == ["done", "done"]
        assert sorted(parsed) == ["a.pdf", "b.pdf"]

        done = listing["files"][0]
        md = tmp_path / done["md_path"]
        assert done["md_path"].startswith("parsed/") and md.is_file()
        assert (md.parent / "images" / "img1.png").is_file()
        assert not (md.parent / "model_output.json").exists()
        assert not list((tmp_path / "parsed" / "_zips").rglob("*.zip"))

        _, state = server.request("GET", "/api/state")
        assert len(state["file_records"]) == 2

        status, data = server.request("POST", "/api/files/pick", {"kind": "files"})
        assert status == 200 and data["paths"] == [str(scans)]

        status, data = server.request("POST", "/api/files/delete", {"uuid": done["uuid"]})
        assert status == 200
        _, listing = server.request("GET", "/api/files")
        assert len(listing["files"]) == 1
        assert md.is_file()  # 磁盘产物不动
    finally:
        server.close()


def test_files_parse_failure_and_retry(tmp_path):
    import time

    from primer.literature.backends import ParseOutcome

    path = tmp_path / "primer.literature.json"
    Library.create(path)
    source = tmp_path / "bad.pdf"
    source.write_bytes(b"%PDF-1.4 fake")

    calls = {"count": 0}

    def failing_parse(sources, output_dir):
        calls["count"] += 1
        return ParseOutcome(command=("fake",), returncode=1, error="boom")

    service = S.LibraryService.initial(str(path), parser=failing_parse)
    server = _Server(service)
    try:
        status, data = server.request("POST", "/api/files/add", {"paths": [str(source)]})
        assert status == 200 and data["added"] == 1

        deadline = time.time() + 10
        listing = {"files": [], "parsing": -1}
        while time.time() < deadline:
            _, listing = server.request("GET", "/api/files")
            if listing["parsing"] == 0:
                break
            time.sleep(0.05)
        record = listing["files"][0]
        assert record["status"] == "failed" and "boom" in record["error"]

        status, data = server.request("POST", "/api/files/parse", {"uuids": [record["uuid"]]})
        assert status == 200 and data["queued"] == 1

        deadline = time.time() + 10
        while time.time() < deadline:
            _, listing = server.request("GET", "/api/files")
            if listing["parsing"] == 0:
                break
            time.sleep(0.05)
        assert listing["files"][0]["status"] == "failed"
        assert calls["count"] == 2
    finally:
        server.close()


def _files(server):
    """GET /api/files，并补齐 ``to_dict`` 省略的空字段（record_uuid／nature／dup）。"""
    _, listing = server.request("GET", "/api/files")
    for item in listing["files"]:
        item.setdefault("record_uuid", "")
        item.setdefault("nature", "")
        item.setdefault("dup", {})
    return listing


def _wait_parsing(server):
    """轮询直到解析队列清空（最多 ~10 秒），返回 ``_files`` 格式的清单。"""
    import time

    listing = {"files": [], "parsing": -1}
    deadline = time.time() + 10
    while time.time() < deadline:
        listing = _files(server)
        if listing["parsing"] == 0:
            break
        time.sleep(0.05)
    return listing


def test_files_content_dedup_and_auto_link(tmp_path):
    import zipfile as _zipfile

    from primer.literature.backends import ParseOutcome

    path = tmp_path / "primer.literature.json"
    Library.create(path)
    scans = tmp_path / "scans"
    scans.mkdir()
    (scans / "a.pdf").write_bytes(b"SAME-BYTES")
    (scans / "a-copy.pdf").write_bytes(b"SAME-BYTES")
    (scans / "b.pdf").write_bytes(b"OTHER-BYTES")

    def fake_parse(sources, output_dir):
        outputs = {}
        for source in sources:
            archive = Path(output_dir) / (Path(source).stem + ".zip")
            with _zipfile.ZipFile(archive, "w") as handle:
                handle.writestr(
                    "markdown.md", "# Parsed\n\narXiv:2101.00001\n\nDOI: 10.1234/dup.test\n"
                )
            outputs[Path(source)] = archive
        return ParseOutcome(command=("fake",), returncode=0, outputs=outputs)

    service = S.LibraryService.initial(str(path), parser=fake_parse)
    server = _Server(service)
    try:
        status, created = server.request(
            "POST", "/api/records", _record_payload(title="既有文献", doi="10.1234/dup.test")
        )
        assert status == 201
        record_uuid = created["record"]["uuid"]

        status, data = server.request("POST", "/api/files/scan", {"path": str(scans)})
        assert status == 200
        assert data["added"] == 2 and data["skipped"] == 0
        # 内容级：a-copy.pdf 与 a.pdf 同 MD5（先到先得，扫描顺序不定），只登记一份
        assert len(data["duplicates"]) == 1
        entry = data["duplicates"][0]
        assert Path(entry["path"]).name in ("a.pdf", "a-copy.pdf")
        assert entry["same_as"] in ("a.pdf", "a-copy.pdf")
        assert Path(entry["path"]).name != entry["same_as"]

        files = {item["name"]: item for item in _wait_parsing(server)["files"]}
        assert set(files) in ({"a.pdf", "b.pdf"}, {"a-copy.pdf", "b.pdf"})
        assert len({item["md5"] for item in files.values()}) == 2

        # 语义级：两份都按 DOI 唯一命中既有文献 → 自动挂链，不再留「疑似重复」
        for item in files.values():
            assert item["doi"] == "10.1234/dup.test"
            assert item["eprint"] == "2101.00001"
            assert item["record_uuid"] == record_uuid
            assert item["nature"] == "doi-consistent"
            assert item["dup"] == {}
    finally:
        server.close()


def test_files_auto_link_arxiv_and_ambiguity(tmp_path):
    import zipfile as _zipfile

    from primer.literature.backends import ParseOutcome

    path = tmp_path / "primer.literature.json"
    Library.create(path)
    scans = tmp_path / "scans"
    scans.mkdir()
    (scans / "p.pdf").write_bytes(b"PREPRINT")
    (scans / "q.pdf").write_bytes(b"PUBLISHED")
    (scans / "r.pdf").write_bytes(b"AMBIGUOUS")

    bodies = {
        "p": "# P\n\narXiv:2101.00002\n",
        "q": "# Q\n\narXiv:2101.00003\n",
        "r": "# R\n\nDOI: 10.1234/two.test\n",
    }

    def fake_parse(sources, output_dir):
        outputs = {}
        for source in sources:
            stem = Path(source).stem
            archive = Path(output_dir) / (stem + ".zip")
            with _zipfile.ZipFile(archive, "w") as handle:
                handle.writestr("markdown.md", bodies[stem])
            outputs[Path(source)] = archive
        return ParseOutcome(command=("fake",), returncode=0, outputs=outputs)

    service = S.LibraryService.initial(str(path), parser=fake_parse)
    server = _Server(service)
    try:
        _, pre = server.request(
            "POST",
            "/api/records",
            _record_payload(title="预印本", type="preprint", eprint="2101.00002"),
        )
        _, pub = server.request(
            "POST",
            "/api/records",
            _record_payload(
                title="已发表",
                type="journal-article",
                eprint="2101.00003",
                doi="10.1234/pub.test",
            ),
        )
        server.request(
            "POST", "/api/records", _record_payload(title="同 DOI 甲", doi="10.1234/two.test")
        )
        server.request(
            "POST", "/api/records", _record_payload(title="同 DOI 乙", doi="10.1234/two.test")
        )

        status, data = server.request(
            "POST", "/api/files/add", {"paths": [str(scans / f"{stem}.pdf") for stem in "pqr"]}
        )
        assert status == 200 and data["added"] == 3

        files = {item["name"]: item for item in _wait_parsing(server)["files"]}
        assert len(files) == 3

        # 仅 arXiv、且唯一天命中的是预印本记录 → 自动挂链
        assert files["p.pdf"]["record_uuid"] == pre["record"]["uuid"]
        assert files["p.pdf"]["nature"] == "preprint-substitute"
        assert files["p.pdf"]["dup"] == {}

        # 仅 arXiv、只命中已发表记录（有 DOI）→ 不挂链，留候选
        assert files["q.pdf"]["record_uuid"] == ""
        assert files["q.pdf"]["dup"]["kind"] == "eprint"
        assert [item["uuid"] for item in files["q.pdf"]["dup"]["matches"]] == [
            pub["record"]["uuid"]
        ]

        # DOI 命中两条记录 → 不挂链，列出 2 个候选
        assert files["r.pdf"]["record_uuid"] == ""
        assert files["r.pdf"]["dup"]["kind"] == "doi"
        assert len(files["r.pdf"]["dup"]["matches"]) == 2
    finally:
        server.close()


def test_files_file_candidate_when_no_records(tmp_path):
    import zipfile as _zipfile

    from primer.literature.backends import ParseOutcome

    path = tmp_path / "primer.literature.json"
    Library.create(path)
    scans = tmp_path / "scans"
    scans.mkdir()
    (scans / "m.pdf").write_bytes(b"M")
    (scans / "n.pdf").write_bytes(b"N")

    def fake_parse(sources, output_dir):
        outputs = {}
        for source in sources:
            archive = Path(output_dir) / (Path(source).stem + ".zip")
            with _zipfile.ZipFile(archive, "w") as handle:
                handle.writestr("markdown.md", "# Same\n\nDOI: 10.1234/only-files.test\n")
            outputs[Path(source)] = archive
        return ParseOutcome(command=("fake",), returncode=0, outputs=outputs)

    service = S.LibraryService.initial(str(path), parser=fake_parse)
    server = _Server(service)
    try:
        server.request("POST", "/api/files/add", {"paths": [str(scans / name) for name in ("m.pdf", "n.pdf")]})
        files = {item["name"]: item for item in _wait_parsing(server)["files"]}
        assert len(files) == 2
        # 库里没有同标识记录：后解析的一份标记「与另一份文件重复」，都不挂链
        flagged = [item for item in files.values() if item["dup"]]
        assert len(flagged) == 1
        dup = flagged[0]["dup"]
        assert dup["kind"] == "doi"
        assert [item["kind"] for item in dup["matches"]] == ["file"]
        assert dup["matches"][0]["uuid"] != flagged[0]["uuid"]
        assert all(item["record_uuid"] == "" for item in files.values())
    finally:
        server.close()


def test_link_endpoints_attach_detach(tmp_path):
    from primer.literature.backends import ParseOutcome

    path = tmp_path / "primer.literature.json"
    Library.create(path)
    source = tmp_path / "x.pdf"
    source.write_bytes(b"%PDF-1.4 fake")

    def failing_parse(sources, output_dir):
        return ParseOutcome(command=("fake",), returncode=1, error="skip")

    service = S.LibraryService.initial(str(path), parser=failing_parse)
    server = _Server(service)
    try:
        _, first = server.request("POST", "/api/records", _record_payload(title="甲"))
        _, second = server.request("POST", "/api/records", _record_payload(title="乙"))
        server.request("POST", "/api/files/add", {"paths": [str(source)]})
        item = _wait_parsing(server)["files"][0]
        assert item["status"] == "failed"
        file_uuid = item["uuid"]
        first_uuid = first["record"]["uuid"]
        second_uuid = second["record"]["uuid"]

        # 未关联 → 挂到甲（给性质）
        status, data = server.request(
            "POST",
            "/api/links/attach",
            {"record_uuid": first_uuid, "file_uuids": [file_uuid], "nature": "manual-upload"},
        )
        assert status == 200 and data["linked"] == 1 and data["replaced"] == 0
        listing = _files(server)
        assert listing["files"][0]["record_uuid"] == first_uuid
        assert listing["files"][0]["nature"] == "manual-upload"

        # 改挂到乙 → replaced 计 1，性质默认 doi-consistent
        status, data = server.request(
            "POST", "/api/links/attach", {"record_uuid": second_uuid, "file_uuids": [file_uuid]}
        )
        assert status == 200 and data["linked"] == 1 and data["replaced"] == 1
        listing = _files(server)
        assert listing["files"][0]["record_uuid"] == second_uuid
        assert listing["files"][0]["nature"] == "doi-consistent"

        # 解除关联
        status, data = server.request("POST", "/api/links/detach", {"file_uuids": [file_uuid]})
        assert status == 200 and data["detached"] == 1
        listing = _files(server)
        assert listing["files"][0]["record_uuid"] == ""
        assert listing["files"][0]["nature"] == ""

        # 重新挂到甲，删除甲 → 级联解除
        server.request(
            "POST", "/api/links/attach", {"record_uuid": first_uuid, "file_uuids": [file_uuid]}
        )
        status, _ = server.request("DELETE", f"/api/records/{first_uuid}")
        assert status == 200
        listing = _files(server)
        assert listing["files"][0]["record_uuid"] == ""
        assert listing["files"][0]["nature"] == ""

        # 参数校验：记录不存在 404；文件不存在／性质非法 400
        status, _ = server.request(
            "POST", "/api/links/attach", {"record_uuid": "missing", "file_uuids": [file_uuid]}
        )
        assert status == 404
        status, _ = server.request(
            "POST", "/api/links/attach", {"record_uuid": second_uuid, "file_uuids": ["missing"]}
        )
        assert status == 400
        status, _ = server.request(
            "POST",
            "/api/links/attach",
            {"record_uuid": second_uuid, "file_uuids": [file_uuid], "nature": "bogus"},
        )
        assert status == 400
    finally:
        server.close()


def test_link_cascade_bulk_clear_and_dup_prune(tmp_path):
    import zipfile as _zipfile

    from primer.literature.backends import ParseOutcome

    path = tmp_path / "primer.literature.json"
    Library.create(path)
    source = tmp_path / "r.pdf"
    source.write_bytes(b"AMBIGUOUS")

    def fake_parse(sources, output_dir):
        outputs = {}
        for source in sources:
            archive = Path(output_dir) / (Path(source).stem + ".zip")
            with _zipfile.ZipFile(archive, "w") as handle:
                handle.writestr("markdown.md", "# R\n\nDOI: 10.1234/two.test\n")
            outputs[Path(source)] = archive
        return ParseOutcome(command=("fake",), returncode=0, outputs=outputs)

    service = S.LibraryService.initial(str(path), parser=fake_parse)
    server = _Server(service)
    try:
        _, first = server.request(
            "POST", "/api/records", _record_payload(title="甲", doi="10.1234/two.test")
        )
        _, second = server.request(
            "POST", "/api/records", _record_payload(title="乙", doi="10.1234/two.test")
        )
        first_uuid = first["record"]["uuid"]
        second_uuid = second["record"]["uuid"]

        server.request("POST", "/api/files/add", {"paths": [str(source)]})
        item = _wait_parsing(server)["files"][0]
        assert item["record_uuid"] == ""
        assert [entry["uuid"] for entry in item["dup"]["matches"]] == [first_uuid, second_uuid]

        # 批量删除乙 → 候选剪到只剩甲
        status, data = server.request("POST", "/api/records/delete", {"uuids": [second_uuid]})
        assert status == 200 and data["deleted"] == 1
        item = _files(server)["files"][0]
        assert item["record_uuid"] == ""
        assert [entry["uuid"] for entry in item["dup"]["matches"]] == [first_uuid]

        # 挂到甲 → dup 清空；清空整库 → 自动解除
        server.request(
            "POST", "/api/links/attach", {"record_uuid": first_uuid, "file_uuids": [item["uuid"]]}
        )
        item = _files(server)["files"][0]
        assert item["record_uuid"] == first_uuid
        assert item["dup"] == {}
        assert item["nature"] == "doi-consistent"

        status, data = server.request("POST", "/api/library/clear", {})
        assert status == 200 and data["cleared"] == 1
        item = _files(server)["files"][0]
        assert item["record_uuid"] == ""
        assert item["nature"] == ""
    finally:
        server.close()


def test_import_records_remaps_file_links(tmp_path):
    path = tmp_path / "primer.literature.json"
    Library.create(path)
    source_path = tmp_path / "src.json"
    source_path.write_text(
        json.dumps(
            [
                {"uuid": "r1", "title": "源文献甲", "type": "journal-article"},
                {"uuid": "r2", "title": "源文献乙", "type": "journal-article"},
                {"kind": "file", "uuid": "f1", "path": "x.pdf", "record_uuid": "r1"},
                {"kind": "file", "uuid": "f2", "path": "y.pdf", "record_uuid": "r2"},
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    service = S.LibraryService.initial(str(path))
    server = _Server(service)
    try:
        _, existing = server.request(
            "POST", "/api/records", _record_payload(title="本地已有", uuid="r1")
        )
        assert existing["record"]["uuid"] == "r1"

        status, data = server.request("POST", "/api/import", {"path": str(source_path)})
        assert status == 200
        assert data["imported"] == 2 and data["renamed"] == 1 and data["files"] == 2

        # 改名的那份记录的关联跟到新 uuid；无冲突的原样保留
        _, state = server.request("GET", "/api/state")
        titles = {item["uuid"]: item["title"] for item in state["records"]}
        files = {item["uuid"]: item for item in state["file_records"]}
        assert titles["r1"] == "本地已有"
        assert files["f1"]["record_uuid"] != "r1"
        assert titles[files["f1"]["record_uuid"]] == "源文献甲"
        assert files["f2"]["record_uuid"] == "r2"
    finally:
        server.close()


def test_parse_batch_covers_pending_files_in_one_call(tmp_path):
    import zipfile as _zipfile

    from primer.literature.backends import ParseOutcome

    path = tmp_path / "primer.literature.json"
    Library.create(path)
    sources = []
    for name in ("a.pdf", "b.pdf", "c.pdf"):
        item = tmp_path / name
        item.write_bytes(("PDF-" + name).encode())
        sources.append(item)

    calls: list = []

    def fake_parse(items, output_dir):
        calls.append([Path(item).name for item in items])
        outputs = {}
        for item in items:
            archive = Path(output_dir) / (Path(item).stem + ".zip")
            with _zipfile.ZipFile(archive, "w") as handle:
                handle.writestr("markdown.md", "# " + Path(item).stem + "\n")
            outputs[Path(item)] = archive
        return ParseOutcome(command=("fake",), returncode=0, outputs=outputs)

    service = S.LibraryService.initial(str(path), parser=fake_parse)
    server = _Server(service)
    try:
        status, data = server.request(
            "POST", "/api/files/add", {"paths": [str(item) for item in sources]}
        )
        assert status == 200 and data["added"] == 3
        listing = _wait_parsing(server)
        assert sorted(item["status"] for item in listing["files"]) == ["done", "done", "done"]
        # 不超过 PARSE_BATCH_SIZE 时整批一次调用（不再每份起一次）
        assert calls == [["a.pdf", "b.pdf", "c.pdf"]]
        assert all(item["md_path"].startswith("parsed/") for item in listing["files"])
        assert not list((tmp_path / "parsed" / "_zips").rglob("*.zip"))
    finally:
        server.close()


def test_parse_batch_failure_falls_back_per_file(tmp_path):
    import zipfile as _zipfile

    from primer.literature.backends import ParseOutcome

    path = tmp_path / "primer.literature.json"
    Library.create(path)
    sources = []
    for name in ("a.pdf", "bad.pdf", "c.pdf"):
        item = tmp_path / name
        item.write_bytes(("PDF-" + name).encode())
        sources.append(item)

    calls: list = []

    def flaky_parse(items, output_dir):
        calls.append([Path(item).name for item in items])
        outputs = {}
        for item in items:
            if Path(item).name == "bad.pdf":
                # mineru-kit 批量语义：首个失败即中止整批，已产出的归档仍然有效
                return ParseOutcome(
                    command=("fake",),
                    returncode=1,
                    error="Failed to parse bad.pdf: boom",
                    outputs=outputs,
                )
            archive = Path(output_dir) / (Path(item).stem + ".zip")
            with _zipfile.ZipFile(archive, "w") as handle:
                handle.writestr("markdown.md", "# " + Path(item).stem + "\n")
            outputs[Path(item)] = archive
        return ParseOutcome(command=("fake",), returncode=0, outputs=outputs)

    service = S.LibraryService.initial(str(path), parser=flaky_parse)
    server = _Server(service)
    try:
        server.request("POST", "/api/files/add", {"paths": [str(item) for item in sources]})
        listing = _wait_parsing(server)
        by_name = {item["name"]: item for item in listing["files"]}
        assert by_name["a.pdf"]["status"] == "done"
        assert by_name["c.pdf"]["status"] == "done"
        assert by_name["bad.pdf"]["status"] == "failed"
        assert "bad.pdf" in by_name["bad.pdf"]["error"]
        # 先整批一次；缺产物者逐文件重跑（已有产物的 a.pdf 不重跑）
        assert calls[0] == ["a.pdf", "bad.pdf", "c.pdf"]
        assert calls[1:] == [["bad.pdf"], ["c.pdf"]]
    finally:
        server.close()


def test_parse_workers_run_in_parallel(tmp_path):
    import threading
    import zipfile as _zipfile

    from primer.literature.backends import ParseOutcome

    path = tmp_path / "primer.literature.json"
    Library.create(path)
    sources = []
    for name in ("a.pdf", "b.pdf"):
        item = tmp_path / name
        item.write_bytes(("PDF-" + name).encode())
        sources.append(item)

    barrier = threading.Barrier(2, timeout=10)

    def parallel_parse(items, output_dir):
        barrier.wait()  # 两路 worker 必须同时在跑，否则超时失败
        outputs = {}
        for item in items:
            archive = Path(output_dir) / (Path(item).stem + ".zip")
            with _zipfile.ZipFile(archive, "w") as handle:
                handle.writestr("markdown.md", "# " + Path(item).stem + "\n")
            outputs[Path(item)] = archive
        return ParseOutcome(command=("fake",), returncode=0, outputs=outputs)

    service = S.LibraryService.initial(str(path), parser=parallel_parse)
    service.PARSE_BATCH_SIZE = 1  # 逼两路并发各领一份
    server = _Server(service)
    try:
        server.request("POST", "/api/files/add", {"paths": [str(item) for item in sources]})
        listing = _wait_parsing(server)
        assert sorted(item["status"] for item in listing["files"]) == ["done", "done"]
    finally:
        server.close()


def test_worker_retire_handshake_marks_and_rechecks(tmp_path):
    import threading

    from primer.literature.library import FileRecord

    path = tmp_path / "primer.literature.json"
    Library.create(path)
    service = S.LibraryService.initial(str(path))

    # 无活可干：承诺退场，并把本线程记入 _retiring（ensure 不再把它当在班）
    assert service._worker_retire() is True
    assert threading.get_ident() in service._retiring

    # 有新活：握手上拒绝退场（新登记的文件优先由本线程接着领）
    service.library.file_records.append(
        FileRecord(uuid="f1", path=str(tmp_path / "a.pdf"), name="a.pdf")
    )
    assert service._worker_retire() is False


def test_retiring_worker_gives_way_to_new_file(tmp_path):
    import zipfile as _zipfile

    from primer.literature.backends import ParseOutcome

    path = tmp_path / "primer.literature.json"
    Library.create(path)
    (tmp_path / "a.pdf").write_bytes(b"PDF-A")
    (tmp_path / "b.pdf").write_bytes(b"PDF-B")

    calls: list = []

    def fake_parse(items, output_dir):
        calls.append([Path(item).name for item in items])
        outputs = {}
        for item in items:
            archive = Path(output_dir) / (Path(item).stem + ".zip")
            with _zipfile.ZipFile(archive, "w") as handle:
                handle.writestr("markdown.md", "# " + Path(item).stem + "\n")
            outputs[Path(item)] = archive
        return ParseOutcome(command=("fake",), returncode=0, outputs=outputs)

    service = S.LibraryService.initial(str(path), parser=fake_parse)
    service.PARSE_WORKERS = 1  # 单路，竞态窗口才可控
    server = _Server(service)
    entered = threading.Event()
    release = threading.Event()
    original = service._worker_retire
    held: list = []

    def held_retire():
        if not original():
            return False
        if not held:  # 只把第一路卡在"已承诺退场、尚未死"的窗口里
            held.append(True)
            entered.set()
            release.wait(10)
        return True

    service._worker_retire = held_retire
    try:
        server.request("POST", "/api/files/add", {"paths": [str(tmp_path / "a.pdf")]})
        assert entered.wait(10), "worker 没有走到退场握手"
        # 就在退场窗口里登记新文件：ensure 不得把退场中的线程当在班，应另起一路接管
        status, data = server.request(
            "POST", "/api/files/add", {"paths": [str(tmp_path / "b.pdf")]}
        )
        assert status == 200 and data["added"] == 1
        listing = _wait_parsing(server)
        by_name = {item["name"]: item for item in listing["files"]}
        assert by_name["a.pdf"]["status"] == "done"
        assert by_name["b.pdf"]["status"] == "done"
        assert sorted(name for batch in calls for name in batch) == ["a.pdf", "b.pdf"]
    finally:
        release.set()
        server.close()


def test_link_batch_preview_and_apply(tmp_path):
    import time

    path = tmp_path / "primer.literature.json"
    Library.create(path)
    (tmp_path / "parsed" / "alpha").mkdir(parents=True)
    (tmp_path / "parsed" / "alpha" / "markdown.md").write_text(
        "# Alpha Mission Study Report\n", encoding="utf-8"
    )
    (tmp_path / "parsed" / "beta").mkdir(parents=True)
    (tmp_path / "parsed" / "beta" / "markdown.md").write_text(
        "# Completely unrelated wording about tides\n", encoding="utf-8"
    )

    service = S.LibraryService.initial(str(path))
    server = _Server(service)
    try:
        _, created = server.request("POST", "/api/records", {"title": "Alpha mission study report"})
        alpha_uuid = created["record"]["uuid"]
        server.request("POST", "/api/records", {"title": "Beta oceans study", "year": 2021})

        (tmp_path / "alpha.pdf").write_bytes(b"PDF")
        library = service.library
        library.add_file_record({
            "path": str(tmp_path / "alpha.pdf"),
            "name": "alpha.pdf",
            "size": 1,
            "md_path": "parsed/alpha/markdown.md",
            "status": "done",
        })
        library.add_file_record({
            "path": str(tmp_path / "beta.pdf"),
            "name": "beta.pdf",
            "size": 1,
            "md_path": "parsed/beta/markdown.md",
            "status": "done",
        })
        service._persist(library, invalidate_scan=False)

        status, started = server.request("POST", "/api/links/batch/start", {"scope": "unlinked"})
        assert status == 200 and started["started"] is True
        token = started["token"]
        payload = None
        deadline = time.time() + 10
        while time.time() < deadline:
            _, payload = server.request("POST", "/api/links/batch/status", {"token": token})
            if payload["status"] != "running":
                break
            time.sleep(0.05)
        assert payload["status"] == "done"
        result = payload["result"]
        assert [item["name"] for item in result["proposals"]] == ["alpha.pdf"]
        proposal = result["proposals"][0]
        assert proposal["record_uuid"] == alpha_uuid and proposal["tier"] == "strong"
        assert [item["title"] for item in result["missing"]] == ["Beta oceans study"]

        pair = {
            "file_uuid": proposal["file_uuid"],
            "record_uuid": alpha_uuid,
            "nature": "title-match",
        }
        status, data = server.request("POST", "/api/links/batch/apply", {"pairs": [pair]})
        assert status == 200 and data["linked"] == 1
        _, files = server.request("GET", "/api/files")
        item = next(entry for entry in files["files"] if entry["name"] == "alpha.pdf")
        assert item["record_uuid"] == alpha_uuid and item["nature"] == "title-match"
        assert item["exists"] is True
        beta_item = next(entry for entry in files["files"] if entry["name"] == "beta.pdf")
        assert beta_item["exists"] is False

        _, again = server.request("POST", "/api/links/batch/apply", {"pairs": [pair]})
        assert again["linked"] == 0

        out = tmp_path / "missing.csv"
        status, data = server.request(
            "POST", "/api/links/batch/export", {"kind": "missing", "path": str(out)}
        )
        assert status == 200 and data["exported"] == 1
        assert "Beta oceans study" in out.read_text(encoding="utf-8-sig")
        status, err = server.request(
            "POST", "/api/links/batch/export", {"kind": "missing", "path": str(out)}
        )
        assert status == 409 and "already exists" in err["message"]

        status, err = server.request("POST", "/api/links/batch/status", {"token": "missing"})
        assert status == 400 and "no link batch job" in err["message"]
    finally:
        server.close()


def test_link_batch_online_upgrade(tmp_path):
    import time

    path = tmp_path / "primer.literature.json"
    Library.create(path)
    (tmp_path / "parsed" / "a").mkdir(parents=True)
    (tmp_path / "parsed" / "a" / "markdown.md").write_text(
        "# SCIENTIFIC REPORTS\n", encoding="utf-8"
    )
    doi_titles = {}

    def fake_doi_title(doi):
        return doi_titles.get(doi, "")

    service = S.LibraryService.initial(str(path))
    service._doi_title_lookup = fake_doi_title
    server = _Server(service)
    try:
        _, created = server.request(
            "POST",
            "/api/records",
            {"title": "Real time detection of tsunamigenic earthquakes using GNSS"},
        )
        target_uuid = created["record"]["uuid"]
        library = service.library
        library.add_file_record({
            "path": str(tmp_path / "a.pdf"),
            "name": "a.pdf",
            "size": 1,
            "md_path": "parsed/a/markdown.md",
            "status": "done",
            "doi": "10.1000/online.test",
        })
        service._persist(library, invalidate_scan=False)
        doi_titles["10.1000/online.test"] = (
            "Real Time Detection of Tsunamigenic Earthquakes Using GNSS"
        )

        status, started = server.request(
            "POST", "/api/links/batch/start", {"scope": "unlinked", "online": True}
        )
        assert status == 200 and started["online"] is True
        token = started["token"]
        payload = None
        deadline = time.time() + 10
        while time.time() < deadline:
            _, payload = server.request("POST", "/api/links/batch/status", {"token": token})
            if payload["status"] != "running":
                break
            time.sleep(0.05)
        assert payload["status"] == "done"
        result = payload["result"]
        assert result["stats"]["online_checked"] == 1
        assert result["stats"]["online_upgraded"] == 1
        proposal = result["proposals"][0]
        assert proposal["how"] == "online" and proposal["record_uuid"] == target_uuid
        assert proposal["tier"] == "weak"
        assert [item["uuid"] for item in result["missing"]] == [target_uuid]
    finally:
        server.close()


def test_fetch_scan_and_download(tmp_path):
    import time

    path = tmp_path / "primer.literature.json"
    Library.create(path)
    service = S.LibraryService.initial(str(path))
    service._fetch_check_pdfinfo = False  # 假 PDF 通不过 pdfinfo
    service._web_lookup = [lambda title: []]  # 禁网：解析链只用记录自带链接

    pdf_bytes = b"%PDF-1.4\n" + b"z" * 200 + b"\n%%EOF\n"

    class _FakeResponse:
        headers = {"Content-Type": "application/pdf"}

        def __init__(self):
            self._done = False

        def read(self, size=-1):
            if self._done:
                return b""
            self._done = True
            return pdf_bytes

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

    service._fetch_opener = lambda request, timeout=None: _FakeResponse()
    server = _Server(service)
    record_uuid = ""
    try:
        _, created = server.request(
            "POST", "/api/records", {"title": "K2 mission characterization and early results"}
        )
        record_uuid = created["record"]["uuid"]
        library = service.library
        library.find(record_uuid).download_url = "https://example.org/k2.pdf"
        service._persist(library, invalidate_scan=False)

        target = tmp_path / "refs"
        status, started = server.request(
            "POST", "/api/fetch/scan", {"scope": "missing", "target_dir": str(target)}
        )
        assert status == 200 and started["started"] is True and started["total"] == 1
        token = started["token"]
        payload = None
        deadline = time.time() + 10
        while time.time() < deadline:
            _, payload = server.request("POST", "/api/fetch/status", {"token": token})
            if payload["status"] != "running":
                break
            time.sleep(0.05)
        assert payload["status"] == "done" and payload["phase"] == "scan"
        assert payload["result"]["stats"] == {"direct": 1, "manual": 0, "none": 0}
        plan = payload["result"]["plans"][0]
        assert plan["record_uuid"] == record_uuid and plan["expected"] == "direct"
        assert plan["url"] == "https://example.org/k2.pdf" and plan["source"] == "download-url"

        status, started = server.request(
            "POST", "/api/fetch/download", {"token": token, "uuids": [record_uuid]}
        )
        assert status == 200 and started["total"] == 1
        payload = None
        deadline = time.time() + 10
        while time.time() < deadline:
            _, payload = server.request("POST", "/api/fetch/status", {"token": token})
            if payload["status"] != "running":
                break
            time.sleep(0.05)
        assert payload["status"] == "done" and payload["phase"] == "download"
        stats = payload["download"]["stats"]
        assert stats["downloaded"] == 1 and stats["failed"] == 0 and stats["skipped"] == 0
        item = payload["download"]["items"][0]
        assert item["status"] == "downloaded" and item["size"] == len(pdf_bytes)

        saved = list(target.glob("*.pdf"))
        assert len(saved) == 1 and saved[0].read_bytes() == pdf_bytes

        status, files = server.request("GET", "/api/files")
        entry = next(item for item in files["files"] if item["record_uuid"] == record_uuid)
        assert entry["nature"] == "auto-download" and entry["status"] == "downloaded"
        assert entry["exists"] is True and entry["md5"]
        assert entry["name"] == saved[0].name
    finally:
        server.close()

    reloaded = S.LibraryService.initial(str(path))
    file_record = next(
        item for item in reloaded.library.file_records if item.record_uuid == record_uuid
    )
    assert file_record.nature == "auto-download" and file_record.status == "downloaded"


def test_fetch_scan_online_toggle(tmp_path):
    import time

    path = tmp_path / "primer.literature.json"
    Library.create(path)
    service = S.LibraryService.initial(str(path))
    calls = []

    def fake_engine(title):
        calls.append(title)
        return [{"title": title, "year": 2020, "download_url": "https://arxiv.org/pdf/1234.5678"}]

    service._web_lookup = [fake_engine]
    server = _Server(service)
    try:
        _, created = server.request("POST", "/api/records", {"title": "White paper on space science"})
        assert created["record"]["uuid"]

        def scan(online):
            status, started = server.request(
                "POST",
                "/api/fetch/scan",
                {"scope": "missing", "target_dir": str(tmp_path / "refs"), "online": online},
            )
            assert status == 200
            deadline = time.time() + 10
            payload = None
            while time.time() < deadline:
                _, payload = server.request("POST", "/api/fetch/status", {"token": started["token"]})
                if payload["status"] != "running":
                    break
                time.sleep(0.05)
            assert payload["status"] == "done"
            return payload["result"]

        result = scan(False)
        assert calls == []
        plan = result["plans"][0]
        assert plan["expected"] == "none" and plan["source"] == "none"

        result = scan(True)
        assert calls == ["White paper on space science"]
        plan = result["plans"][0]
        assert plan["expected"] == "direct" and plan["source"] == "live-lookup"
        assert plan["url"] == "https://arxiv.org/pdf/1234.5678"
    finally:
        server.close()


def test_fetch_download_retry_failure_reports_item(tmp_path):
    import time

    path = tmp_path / "primer.literature.json"
    Library.create(path)
    service = S.LibraryService.initial(str(path))
    service._fetch_check_pdfinfo = False
    service._web_lookup = [lambda title: []]

    def broken(request, timeout=None):
        raise OSError("network down")

    service._fetch_opener = broken
    server = _Server(service)
    try:
        _, created = server.request("POST", "/api/records", {"title": "Retry failure case"})
        record_uuid = created["record"]["uuid"]
        library = service.library
        library.find(record_uuid).download_url = "https://example.org/x.pdf"
        service._persist(library, invalidate_scan=False)

        target = tmp_path / "refs"
        status, started = server.request(
            "POST", "/api/fetch/scan", {"scope": "missing", "target_dir": str(target), "online": False}
        )
        assert status == 200
        token = started["token"]
        payload = None
        deadline = time.time() + 10
        while time.time() < deadline:
            _, payload = server.request("POST", "/api/fetch/status", {"token": token})
            if payload["status"] != "running":
                break
            time.sleep(0.05)
        assert payload["status"] == "done" and payload["result"]["stats"]["direct"] == 1

        status, started = server.request(
            "POST", "/api/fetch/download", {"token": token, "uuids": [record_uuid]}
        )
        assert status == 200
        payload = None
        deadline = time.time() + 15
        while time.time() < deadline:
            _, payload = server.request("POST", "/api/fetch/status", {"token": token})
            if payload["status"] != "running":
                break
            time.sleep(0.05)
        assert payload["status"] == "done" and payload["phase"] == "download"
        assert payload["download"]["stats"]["failed"] == 1
        item = payload["download"]["items"][0]
        assert item["status"] == "failed" and "network down" in item["error"]
        assert not list(target.glob("*.pdf"))
    finally:
        server.close()


def test_parse_unfinished_endpoint(tmp_path):
    path = tmp_path / "primer.literature.json"
    Library.create(path)
    service = S.LibraryService.initial(str(path), parser=lambda source: "# t\n")
    service._ensure_parse_worker = lambda: None  # 避免后台 worker 与断言竞态
    server = _Server(service)
    try:
        library = service.library
        (tmp_path / "a.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
        done = library.add_file_record(
            {"path": str(tmp_path / "a.pdf"), "name": "a.pdf", "size": 1, "status": "done"}
        )
        failed = library.add_file_record(
            {"path": str(tmp_path / "b.pdf"), "name": "b.pdf", "size": 1, "status": "failed"}
        )
        downloaded = library.add_file_record(
            {"path": str(tmp_path / "c.pdf"), "name": "c.pdf", "size": 1, "status": "downloaded"}
        )
        parsing = library.add_file_record(
            {"path": str(tmp_path / "d.pdf"), "name": "d.pdf", "size": 1, "status": "parsing"}
        )
        service._persist(library, invalidate_scan=False)

        status, data = server.request("POST", "/api/files/parse-unfinished", {})
        assert status == 200 and data["queued"] == 2
        assert data["pending"] == 0 and data["parsing"] == 1
        assert library.find_file(failed.uuid).status == "pending"
        assert library.find_file(downloaded.uuid).status == "pending"
        assert library.find_file(done.uuid).status == "done"
        assert library.find_file(parsing.uuid).status == "parsing"
    finally:
        server.close()


def test_project_infer_endpoint_and_apply(tmp_path):
    import time

    path = tmp_path / "primer.literature.json"
    Library.create(path)
    service = S.LibraryService.initial(str(path))
    replies = []

    def fake_chat(system, user):
        replies.append(user)
        return (
            '[{"i": 0, "projects": ["地震前兆探测"]},'
            ' {"i": 1, "projects": ["不存在的项目"]},'
            ' {"i": 2, "projects": []}]'
        )

    service._chat_sender = fake_chat
    server = _Server(service)
    try:
        titles = ["Seismic nucleation", "Storm surge model", "Planetary decadal survey"]
        uuids = []
        for title in titles:
            _, created = server.request("POST", "/api/records", {"title": title})
            uuids.append(created["record"]["uuid"])

        status, err = server.request(
            "POST", "/api/projects/infer", {"uuids": uuids, "candidates": []}
        )
        assert status == 400 and "candidates" in err["message"]

        status, started = server.request(
            "POST",
            "/api/projects/infer",
            {"uuids": uuids, "candidates": ["地震前兆探测", "风暴海啸预报"]},
        )
        assert status == 200 and started["total"] == 3
        token = started["token"]
        payload = None
        deadline = time.time() + 10
        while time.time() < deadline:
            _, payload = server.request("POST", "/api/projects/infer/status", {"token": token})
            if payload["status"] != "running":
                break
            time.sleep(0.05)
        assert payload["status"] == "done"
        result = payload["result"]
        assert result["stats"] == {"updated": 1, "skipped": 2, "failed": 0}
        assert result["proposals"][0]["uuid"] == uuids[0]
        assert result["proposals"][0]["projects"] == ["地震前兆探测"]
        assert "候选项目" in replies[0]

        status, data = server.request(
            "POST", "/api/projects/attach", {"path": "地震前兆探测", "uuids": [uuids[0]]}
        )
        assert status == 200 and data["updated"] == 1

        status, err = server.request("POST", "/api/projects/infer/status", {"token": "missing"})
        assert status == 400 and "no project infer job" in err["message"]
    finally:
        server.close()

    reloaded = S.LibraryService.initial(str(path))
    assert reloaded.library.find(uuids[0]).projects == ["地震前兆探测"]
