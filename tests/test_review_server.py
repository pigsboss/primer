# -*- coding: utf-8 -*-
"""review 会话舱服务测试（``python3 -m primer.review.server``）。

自造会话目录＋线程服务器（127.0.0.1 临时端口），覆盖：状态合并
（canvas／cards／record／uploads／pages／answers）、作答落盘（覆盖＋追加流水）、
上传（图片入页池、PDF 光栅化与降级、命名消毒、防路径穿越）、错误码与静态页。
不依赖任何任务侧产物；PDF 真光栅化只在 pdftoppm 存在时执行，否则走降级分支。
"""

from __future__ import annotations

import json
import shutil
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from primer.review import server as S

PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d4944415478da63fcffff3f0300050001a5f6457f0000000049454e44ae426082"
)


def _get(base: str, path: str):
    try:
        with urllib.request.urlopen(base + path, timeout=10) as r:
            body = r.read()
            ctype = r.headers.get("Content-Type", "")
            if "json" in ctype:
                return r.status, json.loads(body)
            return r.status, body
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            return e.code, json.loads(body)
        except json.JSONDecodeError:
            return e.code, body


def _post_json(base: str, path: str, obj):
    req = urllib.request.Request(base + path, data=json.dumps(obj).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _post_bytes(base: str, path: str, data: bytes):
    req = urllib.request.Request(base + path, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


@pytest.fixture()
def live(tmp_path):
    session = S.Session(tmp_path, dpi=72, max_pages=3)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), S.make_handler(session, verbose=False))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = "http://127.0.0.1:%d" % httpd.server_address[1]
    yield session, base
    httpd.shutdown()
    httpd.server_close()


# ------------------------------------------------------------------ 状态

def test_state_empty(live):
    _, base = live
    status, st = _get(base, "/state")
    assert status == 200
    assert st["canvas"] == {"items": []}
    assert st["cards"] == {"cards": []}
    assert st["record"] == {"rows": []}
    assert st["uploads"] == [] and st["pages"] == [] and st["answers"] == {}


def test_state_reflects_files(live):
    session, base = live
    (session.root / "canvas.json").write_text(
        json.dumps({"items": [{"id": "i1", "image": "pages/a.png", "w": 10, "h": 10}]},
                   ensure_ascii=False), encoding="utf-8")
    (session.root / "cards.json").write_text(
        json.dumps({"cards": [{"id": "q1", "title": "题"}]}, ensure_ascii=False),
        encoding="utf-8")
    (session.root / "record.json").write_text(
        json.dumps({"rows": [{"id": "r1", "subject": "罩形"}]}, ensure_ascii=False),
        encoding="utf-8")
    _, st = _get(base, "/state")
    assert st["canvas"]["items"][0]["id"] == "i1"
    assert st["cards"]["cards"][0]["id"] == "q1"
    assert st["record"]["rows"][0]["subject"] == "罩形"


def test_healthz_and_index(live):
    _, base = live
    status, h = _get(base, "/healthz")
    assert status == 200 and h["ok"] is True
    status, html = _get(base, "/")
    assert status == 200 and "会话舱".encode("utf-8") in html


# ------------------------------------------------------------------ 作答

def test_answer_roundtrip_and_log(live):
    session, base = live
    payload = {"card_id": "q1", "choice": "A", "text": "确认八边形",
               "selections": [{"image": "pages/fig3c.png", "mode": "lasso",
                               "points": [[10, 10], [50, 10], [50, 50]]}]}
    status, r = _post_json(base, "/answer", payload)
    assert status == 200 and r["ok"]
    assert (session.root / "answers" / "q1.json").is_file()
    lines = (session.root / "answers" / "_log.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["card_id"] == "q1"

    _, st = _get(base, "/state")
    assert st["answers"]["q1"]["choice"] == "A"
    assert st["answers"]["q1"]["received_at"]

    # 覆盖写：同一卡只留一份文件，流水追加
    status, r = _post_json(base, "/answer", {"card_id": "q1", "choice": "B"})
    assert status == 200 and r["ok"]
    assert len(list((session.root / "answers").glob("q*.json"))) == 1
    lines = (session.root / "answers" / "_log.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2


def test_answer_bad_json_and_traversal_name(live):
    session, base = live
    status, _ = _post_bytes(base, "/answer", b"{not json")
    assert status == 400
    status, r = _post_json(base, "/answer", {"card_id": "../../evil", "choice": "A"})
    assert status == 200 and r["ok"]
    assert (session.root / "answers" / "evil.json").is_file()


# ------------------------------------------------------------------ 上传

def test_upload_image(live):
    session, base = live
    status, r = _post_bytes(base, "/upload?name=shot.png&desc=%E6%B5%8B%E8%AF%95",
                            PNG_1PX)
    assert status == 200 and r["ok"], r
    up = r["upload"]
    assert up["name"] == "shot.png" and up["desc"] == "测试"
    assert (session.root / "uploads" / "shot.png").is_file()
    assert (session.root / "pages" / "shot.png").is_file()
    _, st = _get(base, "/state")
    assert "pages/shot.png" in st["pages"]
    assert st["uploads"][0]["pages"] == ["pages/shot.png"]


def test_upload_dedupe_and_name_sanitize(live):
    _, base = live
    _post_bytes(base, "/upload?name=shot.png", PNG_1PX)
    _, r = _post_bytes(base, "/upload?name=shot.png", PNG_1PX)
    assert r["upload"]["name"] == "shot-2.png"
    _, r = _post_bytes(base, "/upload?name=..%2F..%2Fevil.png", PNG_1PX)
    assert r["ok"] and "/" not in r["upload"]["name"] and r["upload"]["name"] != ".."


def test_upload_pdf_fallback_note(live, monkeypatch):
    session, base = live
    monkeypatch.setattr(S.shutil, "which", lambda _name: None)
    status, r = _post_bytes(base, "/upload?name=paper.pdf", b"%PDF-1.4 fake")
    assert status == 200 and r["ok"]
    assert "pdftoppm not found" in r["upload"]["note"]
    assert r["upload"]["pages"] == []


@pytest.mark.skipif(shutil.which("pdftoppm") is None, reason="pdftoppm not installed")
def test_upload_pdf_rasterizes(live):
    session, base = live
    pdf = _minimal_pdf()
    status, r = _post_bytes(base, "/upload?name=paper.pdf", pdf)
    assert status == 200 and r["ok"], r
    pages = r["upload"]["pages"]
    assert len(pages) == 1 and pages[0].endswith(".png")
    assert (session.root / pages[0]).is_file()


# ------------------------------------------------------------------ 防穿越

def test_path_traversal_guarded(live):
    _, base = live
    weird = urllib.parse.quote("../../etc/passwd", safe="")
    status, _ = _get(base, "/f/" + weird)
    assert status == 403
    status, _ = _get(base, "/static/" + urllib.parse.quote("../server.py", safe=""))
    assert status == 404


# ------------------------------------------------------------------ 工具

def _minimal_pdf() -> bytes:
    """One blank page, valid xref (generated so offsets are correct)."""
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj " % i + body + b" endobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n" % (len(objs) + 1)
    out += b"0000000000 65535 f \n"
    for off in offsets[1:]:
        out += b"%010d 00000 n \n" % off
    out += b"trailer << /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objs) + 1, xref)
    return bytes(out)
