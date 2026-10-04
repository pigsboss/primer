# -*- coding: utf-8 -*-
"""review 会话舱服务测试（``python3 -m primer.review.server``）。

自造会话目录＋线程服务器（127.0.0.1 临时端口），覆盖：状态合并
（canvas／cards／record／uploads／pages／answers／chat）、资产树（assets.tree：模型／画布
总览／渲染产出／上传四组＋伪节点）、作答落盘（覆盖＋追加流水）、
对话消息落盘与校验（/state.chat 合并、尾部截断、坏行容错）、上传（图片入页池、PDF
光栅化与降级、命名消毒、防路径穿越）、错误码与静态页。
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


# ------------------------------------------------------------------ 对话

def test_state_chat_empty_and_tolerant(live):
    _, base = live
    _, st = _get(base, "/state")
    assert st["chat"] == {"messages": [], "meta": None, "escalation": False}


def test_post_chat_appends_and_state_merges(live):
    session, base = live
    status, r = _post_json(base, "/chat", {"text": "先核对长度", "model": "scene"})
    assert status == 200 and r["ok"], r
    assert r["chat"]["chars"] == len("先核对长度")
    lines = (session.root / "chat.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["role"] == "user" and record["text"] == "先核对长度"
    assert record["model"] == "scene" and record["ts"]

    _, st = _get(base, "/state")
    assert [m["text"] for m in st["chat"]["messages"]] == ["先核对长度"]

    # 驱动写的 meta 与升级标记随后出现，页面据此显示运行态与警示条。
    (session.root / "chat_meta.json").write_text(
        json.dumps({"heartbeat_ts": "2026-10-03T12:00:00+08:00",
                    "models": [{"id": "scene", "label": "deepseek/deepseek-flash"}],
                    "default_role": "scene", "vision": "auto"}), encoding="utf-8")
    (session.root / "ESCALATION.md").write_text("# 升级\n", encoding="utf-8")
    status, st = _get(base, "/state")
    assert st["chat"]["meta"]["default_role"] == "scene"
    assert st["chat"]["escalation"] is True


def test_post_chat_validation(live):
    session, base = live
    status, r = _post_json(base, "/chat", {"text": "   "})
    assert status == 400 and r["ok"] is False
    status, r = _post_json(base, "/chat", {"text": 123})
    assert status == 400
    status, r = _post_json(base, "/chat", {"text": "x" * 8001})
    assert status == 400
    status, r = _post_json(base, "/chat", {"text": "x" * 8000})
    assert status == 200 and r["ok"]
    status, _ = _post_bytes(base, "/chat", b"{not json")
    assert status == 400
    assert (session.root / "chat.jsonl").read_text(encoding="utf-8").count("\n") == 1


def test_state_chat_tail_is_capped(live):
    session, base = live
    with (session.root / "chat.jsonl").open("a", encoding="utf-8") as fh:
        for i in range(205):
            fh.write(json.dumps({"ts": f"t{i}", "role": "user", "text": str(i)}) + "\n")
    _, st = _get(base, "/state")
    messages = st["chat"]["messages"]
    assert len(messages) == 200
    assert messages[0]["text"] == "5" and messages[-1]["text"] == "204"


def test_state_chat_survives_a_corrupt_line(live):
    session, base = live
    (session.root / "chat.jsonl").write_text(
        '{"role": "user", "text": "好的"}\nnot json\n', encoding="utf-8")
    _, st = _get(base, "/state")
    assert len(st["chat"]["messages"]) == 1


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


# ------------------------------------------------------------------ 问题卡重置

def test_reset_cards_moves_to_retired(live):
    session, base = live
    (session.root / "cards.json").write_text(json.dumps(
        {"cards": [{"id": "q1", "title": "卡一"}, {"id": "q2", "title": "卡二"}]},
        ensure_ascii=False), encoding="utf-8")
    (session.root / "cards_retired.json").write_text(json.dumps(
        {"cards": [{"id": "old", "title": "旧卡", "retired_at": "前次"}]},
        ensure_ascii=False), encoding="utf-8")

    status, r = _post_json(base, "/reset-cards", {"reason": "R29 重置"})
    assert status == 200 and r["ok"]
    assert r["reset"] == 2 and r["retired_total"] == 3

    live_cards = json.loads((session.root / "cards.json").read_text(encoding="utf-8"))
    assert live_cards["cards"] == []
    retired = json.loads((session.root / "cards_retired.json").read_text(encoding="utf-8"))["cards"]
    assert [c["id"] for c in retired] == ["old", "q1", "q2"]
    assert all(c.get("retired_reason") == "R29 重置" and c.get("retired_at") for c in retired[1:])

    backups = sorted(p.name for p in (session.root / "_backup").glob("cards*.json"))
    assert len(backups) == 2, backups

    _, st = _get(base, "/state")
    assert st["cards"]["cards"] == []


def test_reset_cards_empty_is_noop(live):
    session, base = live
    status, r = _post_json(base, "/reset-cards", {})
    assert status == 200 and r["ok"] and r["reset"] == 0
    backups = list((session.root / "_backup").glob("cards*.json")) if (session.root / "_backup").is_dir() else []
    assert backups == []


def test_static_assets_expose_resize_grips(live):
    """两个拖拽手柄（面板宽度／输入区高度）、三段版式、资产树与重置按钮的落地检查：资源缺一个就红。"""
    _, base = live
    status, html = _get(base, "/static/index.html")
    assert status == 200 and b'id="panel-grip"' in html and b'id="composer-grip"' in html
    # 版式：左＝资产树｜中＝可视化｜右＝对话面板，两条竖直分隔条各夹一边
    assert b'id="assets"' in html and b'id="left-grip"' in html
    assert (html.index(b'id="assets"') < html.index(b'id="left-grip"')
            < html.index(b'id="stage-wrap"') < html.index(b'id="panel-grip"')
            < html.index(b'id="panel"'))
    # 视图方式改由资产类型决定：旧的"渲染输出／三维交互"页签已删，工具栏按类型分两组
    assert b'id="viewtabs"' not in html and b'id="mode-render"' not in html and b'id="mode-3d"' not in html
    assert b'id="tools2d"' in html and b'id="tools3d"' in html and b'id="model-stats"' in html
    # 版式：对话框（free-text）独占一行，模型选择／状态提示／发送在下一行（freebar-actions）
    assert html.index(b'id="free-text"') < html.index(b'id="freebar-actions"')
    assert b'id="chat-model"' in html and b'id="chat-driver"' in html and b'id="free-send"' in html
    status, css = _get(base, "/static/style.css")
    assert status == 200 and b"#panel-grip" in css and b"ns-resize" in css and b"col-resize" in css
    assert b"#freebar-actions" in css and b"#chat-foot" not in css
    assert b"#panel.hidden-panel" in css and b"body.drag-col" in css
    assert b"#panel-grip.collapsed" in css
    assert b"#assets.hidden-assets" in css and b"#left-grip.collapsed" in css and b".trow" in css
    assert b"chat-foot" not in html
    assert b'id="panel-toggle"' in html and "隐藏面板".encode("utf-8") in html
    status, js = _get(base, "/static/app.js")
    assert status == 200 and b"bindGrips" in js and b'"/reset-cards"' in js
    assert b"labelPanelToggle" in js and b"hidden-panel" in js
    assert b"PANEL_MIN_W" in js and b"collapse" in js
    assert b"LEFT_MIN_W" in js and b"primerLeftWidth" in js and b"primerLeftHidden" in js
    assert b"primerTreeOpen" in js and b"primerAssetSelected" in js and b"_bindDragGrip" in js
    assert "重置问题卡".encode("utf-8") in js


# ------------------------------------------------------------------ 三维模型资产

def _assets_dir(tmp_path):
    root = tmp_path / "assets"
    root.mkdir()
    (root / "模型_2034.stl").write_bytes(b"solid x\nendsolid x\n")
    sub = root / "分件"
    sub.mkdir()
    (sub / "COL0.stl").write_bytes(b"solid a\nendsolid a\n")
    (sub / "note.txt").write_text("nope", encoding="utf-8")
    return root


@pytest.fixture()
def live_assets(tmp_path):
    session = S.Session(tmp_path / "review", assets=_assets_dir(tmp_path))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), S.make_handler(session, verbose=False))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = "http://127.0.0.1:%d" % httpd.server_address[1]
    yield session, base
    httpd.shutdown()
    httpd.server_close()


def test_assets_listing_groups_stl_only(live_assets):
    _, base = live_assets
    _, st = _get(base, "/state")
    tree = st["assets"]["tree"]
    assert tree[0]["id"] == "g:board#all" and tree[0]["kind"] == "board"   # 伪节点在最上

    models = _node(tree, "g:models")
    assert models["kind"] == "group" and "note" not in models
    assert [n["id"] for n in models["children"]] == ["m:模型_2034.stl", "g:分件"]
    root = models["children"][0]
    assert root["kind"] == "stl" and root["url"] == "/model/模型_2034.stl"
    assert root["rel"] == "模型_2034.stl" and root["bytes"] > 0
    assert root["label"].startswith("整机（标准）·") and "craft" not in root
    parts = models["children"][1]
    assert parts["label"] == "分件（标准）· 1 件"
    col0 = parts["children"][0]
    assert col0["kind"] == "stl" and col0["craft"] == "COL0"
    assert col0["url"] == "/model/分件/COL0.stl" and col0["rel"] == "分件/COL0.stl"
    assert "note.txt" not in json.dumps(tree, ensure_ascii=False)          # .txt 不入清单


def test_assets_tree_board_renders_uploads(live):
    session, base = live
    (session.root / "canvas.json").write_text(json.dumps({"items": [
        {"id": "i1", "image": "pages/a.png", "w": 10, "h": 10, "title": "题一", "caption": "说明",
         "overlays": [{"id": "o1", "type": "label", "at": [1, 2], "label": "L"}]},
        {"id": "i2", "image": "pages/b.png"},
    ]}, ensure_ascii=False), encoding="utf-8")
    (session.root / "renders" / "shot.png").write_bytes(PNG_1PX)
    with (session.root / "uploads" / "index.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"name": "paper.pdf", "desc": "论文",
                             "pages": ["pages/paper-p-1.png", "pages/paper-p-2.png"]},
                            ensure_ascii=False) + "\n")
        fh.write(json.dumps({"name": "shot.png", "pages": ["pages/shot.png"]},
                            ensure_ascii=False) + "\n")
    _, st = _get(base, "/state")
    tree = st["assets"]["tree"]

    board = _node(tree, "g:board")
    assert [c["id"] for c in board["children"]] == ["c:i1", "c:i2"]
    first = board["children"][0]
    assert first["kind"] == "image" and first["url"] == "/f/pages/a.png"
    assert first["rel"] == "pages/a.png" and first["w"] == 10 and first["h"] == 10
    assert first["title"] == "题一" and first["caption"] == "说明"
    assert first["overlays"][0]["id"] == "o1"                              # 覆盖层原样透出
    assert board["children"][1]["label"] == "i2"                           # 无 title 用 id

    renders = _node(tree, "g:renders")
    assert [c["url"] for c in renders["children"]] == ["/f/renders/shot.png"]
    assert renders["children"][0]["id"] == "r:shot.png"

    pdf, image = _node(tree, "g:uploads")["children"]
    assert pdf["kind"] == "group" and pdf["label"] == "paper.pdf（PDF · 2 页）"
    assert [c["label"] for c in pdf["children"]] == ["第 1 页", "第 2 页"]
    assert pdf["children"][1]["url"] == "/f/pages/paper-p-2.png"
    assert pdf["children"][1]["rel"] == "pages/paper-p-2.png"
    assert image["kind"] == "image" and image["url"] == "/f/pages/shot.png"


def _node(nodes, nid):
    """深度优先找节点（组可以嵌套：分件组／PDF 页组）。"""
    for n in nodes:
        if n["id"] == nid:
            return n
        hit = _node(n.get("children") or [], nid)
        if hit:
            return hit
    return None


def test_model_route_serves_and_guards(live_assets):
    session, base = live_assets
    status, body = _get(base, "/model/" + urllib.parse.quote("模型_2034.stl"))
    assert status == 200 and body.startswith(b"solid x")
    status, body = _get(base, "/model/" + urllib.parse.quote("分件/COL0.stl"))
    assert status == 200 and body.startswith(b"solid a")
    status, _ = _get(base, "/model/note.txt")                          # 后缀白名单
    assert status == 403
    status, _ = _get(base, "/model/%2e%2e/secret.stl")                 # 越界（编码绕过）
    assert status == 403
    with pytest.raises(S.ReviewError):
        session.asset_path("../secret.stl")


def test_model_route_without_assets(live):
    _, base = live
    status, r = _get(base, "/state")
    assert status == 200 and r["assets"]["dir"] is None
    tree = r["assets"]["tree"]
    assert _node(tree, "g:board#all")["kind"] == "board"        # 未配 --assets 树仍在
    models = _node(tree, "g:models")
    assert models["kind"] == "group" and models["children"] == [] and models["note"]
    assert _node(tree, "g:renders")["children"] == []
    status, _ = _get(base, "/model/anything.stl")
    assert status == 403


def test_static_serves_vendor_subdir(live):
    _, base = live
    status, body = _get(base, "/static/vendor/three/README.md")
    assert status == 200 and b"three.js" in body
    status, body = _get(base, "/static/vendor/three/three.module.js")
    assert status == 200 and b"REVISION" in body[:4000]
    status, _ = _get(base, "/static/%2e%2e/review/server.py")          # 越界（编码绕过）
    assert status == 403


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
    assert _node(st["assets"]["tree"], "g:uploads")["children"][0]["url"] == "/f/pages/shot.png"


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
    # /static/ 现在按"解析后仍在 STATIC_DIR 内"守卫（支持 vendor/ 子目录），越界一律 403
    status, _ = _get(base, "/static/" + urllib.parse.quote("../server.py", safe=""))
    assert status == 403


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
