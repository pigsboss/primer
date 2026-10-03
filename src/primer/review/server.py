# -*- coding: utf-8 -*-
"""primer-review —— 会话舱本地服务（Phase 0：上传·画布·圈选·台账）。

**定位**：把从参考输入（技术报告页面、概念图、参数表）到参数台账、模型与 manifest
的决策过程摆进浏览器：人看图、圈注、作答；模型侧出图、提问、落账。服务本身是
**哑文件经纪人**——所有智能在会话两端，页面只是视图。

**文件协议**（会话目录即协议）::

    <session>/
      uploads/       人上传的原件（PDF／图片）＋ uploads/index.jsonl 台账
      pages/         PDF 按页光栅化（pdftoppm -png -r <dpi>）与图片副本
      canvas.json    画布（模型侧写）：items[].image＋矢量覆盖层 overlays[]
      cards.json     问题卡（模型侧写）：cards[]{id,title,text,options[],attach,allow_text}
      record.json    参数台账（模型侧写；页面只读呈现）：rows[]{id,subject,value,status,evidence,highlight}
      answers/       人的提交（服务写）：<card_id>.json 每卡一份＋_log.jsonl 追加流水
      renders/       模型渲染回放区（Phase 2 用）

**覆盖层语法**（canvas.json overlays 元素）::

    {"id":"oct1","type":"polygon","points":[[x,y],...],"color":"#ff3b30",
     "label":"……","fill":0.15,"width":2.5}
    {"id":"axis","type":"polyline","points":[[x,y],...],"dash":true,...}
    {"id":"c1","type":"ellipse","center":[x,y],"rx":60,"ry":40,...}
    {"id":"t1","type":"label","at":[x,y],"label":"……"}

数组点一律为**原图像素坐标**；人侧的套索／擦除同此口径，回传不折损。

**端点**::

    GET  /                静态页面        GET  /state   全量状态
    GET  /f/<rel>         会话文件        GET  /healthz 健康检查
    POST /answer          JSON {card_id, choice, text, selections:[{image,mode,points}]}
    POST /upload?name=&desc=   原始字节流；PDF 自动按页光栅化

**用法**::

    PYTHONPATH=src python3 -m primer.review.server --session <会话目录> [--port 8766] [--no-open]

语言纪律（docs/guides/CODING_STANDARDS.md v1.1）：stdout 中文人读报告；
stderr／异常／选项名／JSON 键英文。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, BinaryIO, Dict, List, Optional, Sequence
from urllib.parse import parse_qs, unquote, urlparse

SUBDIRS = ("uploads", "pages", "answers", "renders")
STATIC_DIR = Path(__file__).resolve().parent / "static"

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".pdf": "application/pdf",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/plain; charset=utf-8",
    ".yaml": "text/plain; charset=utf-8",
    ".yml": "text/plain; charset=utf-8",
}

IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")

MAX_ANSWER_BYTES = 4 * 1024 * 1024
MAX_UPLOAD_BYTES = 512 * 1024 * 1024
DEFAULT_DPI = 150
DEFAULT_MAX_PAGES = 40


class ReviewError(Exception):
    """Protocol or session fault. Message is English (see CODING_STANDARDS v1.1)."""


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _safe_name(name: str, fallback: str = "upload") -> str:
    """Collapse a user-supplied file/card name to a flat, harmless basename."""
    name = (name or "").strip().replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r"[^\w.\-]+", "_", name).lstrip(".").strip()
    if not name:
        return fallback
    if len(name) > 120:
        stem, dot, suffix = name.rpartition(".")
        name = (stem[:108] + dot + suffix[:9]) if dot else name[:120]
    return name


class Session:
    """The session directory: guards, state merge, answer/upload writes."""

    def __init__(self, root: str | os.PathLike, dpi: int = DEFAULT_DPI,
                 max_pages: int = DEFAULT_MAX_PAGES) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        for sub in SUBDIRS:
            (self.root / sub).mkdir(exist_ok=True)
        self.dpi = int(dpi)
        self.max_pages = int(max_pages)

    # ------------------------------------------------------------ guards/readers

    def file_path(self, rel: str) -> Path:
        """Resolve a session-relative path (percent-decoded); refuse escapes."""
        p = (self.root / unquote(rel).lstrip("/")).resolve()
        if p != self.root and self.root not in p.parents:
            raise ReviewError("path escapes session root")
        return p

    @staticmethod
    def _read_json(path: Path, default: Any) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return default
        except (json.JSONDecodeError, OSError) as exc:
            raise ReviewError(f"bad JSON in {path.name}: {exc}") from exc

    @staticmethod
    def _read_jsonl(path: Path) -> List[dict]:
        out: List[dict] = []
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        except FileNotFoundError:
            pass
        return out

    # ------------------------------------------------------------ state

    def state(self) -> Dict[str, Any]:
        answers: Dict[str, Any] = {}
        for f in sorted((self.root / "answers").glob("*.json")):
            if f.name.startswith("_"):
                continue
            data = self._read_json(f, None)
            if isinstance(data, dict):
                cid = str(data.get("card_id") or f.stem)
                answers[cid] = data
        pages = sorted(
            str(p.relative_to(self.root))
            for p in (self.root / "pages").glob("*") if p.is_file()
        )
        return {
            "session": str(self.root),
            "ts": time.time(),
            "canvas": self._read_json(self.root / "canvas.json", {"items": []}),
            "cards": self._read_json(self.root / "cards.json", {"cards": []}),
            "record": self._read_json(self.root / "record.json", {"rows": []}),
            "uploads": self._read_jsonl(self.root / "uploads" / "index.jsonl"),
            "pages": pages,
            "answers": answers,
        }

    # ------------------------------------------------------------ answers

    def save_answer(self, payload: dict) -> str:
        if not isinstance(payload, dict):
            raise ReviewError("answer body must be a JSON object")
        cid = _safe_name(str(payload.get("card_id") or "free"), fallback="free")
        record = dict(payload)
        record["card_id"] = cid
        record["received_at"] = _now()
        target = self.root / "answers" / f"{cid}.json"
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
        os.replace(tmp, target)
        line = {
            "card_id": cid, "ts": record["received_at"],
            "choice": record.get("choice"), "has_text": bool(record.get("text")),
            "selections": len(record.get("selections") or []),
        }
        with (self.root / "answers" / "_log.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")
        return str(target.relative_to(self.root))

    # ------------------------------------------------------------ uploads

    def reserve_upload(self, name: str) -> Path:
        """Pick a non-clobbering destination under uploads/ for a new file."""
        base = _safe_name(name)
        stem, dot, suffix = base.rpartition(".")
        stem, suffix = (stem, dot + suffix) if dot else (base, "")
        for i in range(0, 1000):
            cand = self.root / "uploads" / (f"{stem}{suffix}" if i == 0 else f"{stem}-{i + 1}{suffix}")
            if not cand.exists():
                return cand
        raise ReviewError("too many uploads with the same name")

    def finalize_upload(self, dest: Path, desc: str, nbytes: int) -> dict:
        """Rasterize/copy the stored file into pages/ and append the ledger line."""
        note = ""
        new_pages: List[str] = []
        suffix = dest.suffix.lower()
        if suffix == ".pdf":
            new_pages, note = self._rasterize_pdf(dest)
        elif suffix in IMAGE_SUFFIXES:
            page = self.root / "pages" / dest.name
            shutil.copy2(dest, page)
            new_pages = [str(page.relative_to(self.root))]
        else:
            note = f"no preview for suffix '{suffix or '(none)'}'"
        record = {
            "name": dest.name, "desc": desc or "", "ts": _now(),
            "bytes": int(nbytes), "pages": new_pages, "note": note,
        }
        with (self.root / "uploads" / "index.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        return record

    def save_upload_bytes(self, name: str, desc: str, data: bytes) -> dict:
        """Test/simple path: reserve, write bytes, finalize."""
        dest = self.reserve_upload(name)
        dest.write_bytes(data)
        return self.finalize_upload(dest, desc, len(data))

    def _rasterize_pdf(self, pdf: Path) -> (List[str], str):
        exe = shutil.which("pdftoppm")
        if not exe:
            return [], "pdftoppm not found; PDF stored without page rasterization"
        prefix = self.root / "pages" / f"{pdf.stem}-p"
        cmd = [exe, "-png", "-r", str(self.dpi)]
        if self.max_pages > 0:
            cmd += ["-l", str(self.max_pages)]
        cmd += [str(pdf), str(prefix)]
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=600)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            return [], f"pdftoppm failed: {exc}"
        out = sorted((self.root / "pages").glob(f"{pdf.stem}-p-*.png"))
        rel = [str(p.relative_to(self.root)) for p in out]
        return rel, ("" if rel else "pdftoppm produced no pages")


# ================================================================ HTTP layer


def make_handler(review_session: Session, verbose: bool = False):
    class Handler(BaseHTTPRequestHandler):
        server_version = "primer-review/0.1"
        protocol_version = "HTTP/1.1"
        session = review_session
        debug_verbose = verbose

        # -------------------------------------------------- plumbing

        def log_message(self, fmt: str, *args: Any) -> None:
            if self.debug_verbose:
                sys.stderr.write("[review] %s %s\n" % (self.address_string(), fmt % args))

        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, obj: Any, status: int = 200) -> None:
            self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")

        def _error(self, status: int, message: str) -> None:
            self._json({"ok": False, "error": message}, status=status)

        def _send_file(self, path: Path) -> None:
            if not path.is_file():
                self._error(404, f"not found: {path.name}")
                return
            ctype = CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")
            self._send(200, path.read_bytes(), ctype)

        def _read_json_body(self) -> dict:
            try:
                n = int(self.headers.get("Content-Length") or "")
            except ValueError:
                raise ReviewError("missing Content-Length")
            if n < 0 or n > MAX_ANSWER_BYTES:
                raise ReviewError("answer body too large")
            raw = self.rfile.read(n) if n else b"{}"
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ReviewError(f"bad JSON body: {exc}") from exc
            return payload

        # -------------------------------------------------- GET

        def do_GET(self) -> None:  # noqa: N802
            url = urlparse(self.path)
            try:
                if url.path in ("/", "/index.html"):
                    self._send_file(STATIC_DIR / "index.html")
                elif url.path.startswith("/static/"):
                    name = _safe_name(url.path[len("/static/"):])
                    self._send_file(STATIC_DIR / name)
                elif url.path == "/healthz":
                    self._json({"ok": True, "session": str(self.session.root),
                                "ts": time.time()})
                elif url.path == "/state":
                    self._json(self.session.state())
                elif url.path.startswith("/f/"):
                    self._send_file(self.session.file_path(url.path[len("/f/"):]))
                else:
                    self._error(404, f"no route: {url.path}")
            except ReviewError as exc:
                self._error(403, str(exc))

        # -------------------------------------------------- POST

        def do_POST(self) -> None:  # noqa: N802
            url = urlparse(self.path)
            try:
                if url.path == "/answer":
                    payload = self._read_json_body()
                    rel = self.session.save_answer(payload)
                    self._json({"ok": True, "file": rel})
                elif url.path == "/upload":
                    q = {k: v[0] for k, v in parse_qs(url.query).items()}
                    self._receive_upload(q.get("name") or "upload", q.get("desc") or "")
                else:
                    self._error(404, f"no route: {url.path}")
            except ReviewError as exc:
                self._error(400, str(exc))

        def _receive_upload(self, name: str, desc: str) -> None:
            try:
                n = int(self.headers.get("Content-Length") or "")
            except ValueError:
                raise ReviewError("missing Content-Length")
            if n < 0:
                raise ReviewError("missing Content-Length")
            if n > MAX_UPLOAD_BYTES:
                raise ReviewError("upload too large")
            dest = self.session.reserve_upload(name)
            remaining = n
            with dest.open("wb") as fh:
                while remaining > 0:
                    buf = self.rfile.read(min(1024 * 1024, remaining))
                    if not buf:
                        raise ReviewError("upload stream ended early")
                    fh.write(buf)
                    remaining -= len(buf)
            record = self.session.finalize_upload(dest, desc, n)
            self._json({"ok": True, "upload": record})

    return Handler


# ================================================================ CLI


def _report(session: Session, url: str) -> str:
    st = session.state()
    cards = st["cards"].get("cards", [])
    answered = len(st["answers"])
    lines = [
        "primer-review 会话舱",
        f"  会话目录：{session.root}",
        f"  地址：{url}",
        f"  内容：画布项 {len(st['canvas'].get('items', []))}｜"
        f"问题卡 {len(cards)}（已答 {answered}）｜"
        f"台账 {len(st['record'].get('rows', []))} 行｜页面图 {len(st['pages'])} 张",
        "  停止：Ctrl-C",
    ]
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python3 -m primer.review.server",
        description="Local session cockpit: upload references, review annotated "
                    "canvas, answer cards, read the parameter ledger.",
    )
    ap.add_argument("--session", required=True,
                    help="session directory (created if missing)")
    ap.add_argument("--host", default="127.0.0.1", help="bind host (default loopback)")
    ap.add_argument("--port", type=int, default=8766, help="bind port (default 8766)")
    ap.add_argument("--dpi", type=int, default=DEFAULT_DPI,
                    help=f"PDF rasterization DPI (default {DEFAULT_DPI})")
    ap.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES,
                    help=f"max PDF pages to rasterize (default {DEFAULT_MAX_PAGES})")
    ap.add_argument("--no-open", action="store_true",
                    help="do not open the browser automatically")
    ap.add_argument("--verbose", action="store_true", help="log requests to stderr")
    args = ap.parse_args(argv)

    try:
        session = Session(args.session, dpi=args.dpi, max_pages=args.max_pages)
    except ReviewError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2
    handler = make_handler(session, verbose=args.verbose)
    try:
        httpd = ThreadingHTTPServer((args.host, args.port), handler)
    except OSError as exc:
        sys.stderr.write(f"error: cannot bind {args.host}:{args.port} ({exc}); "
                         "try another --port\n")
        return 2
    host, port = httpd.server_address[:2]
    url = f"http://{host}:{port}/"
    print(_report(session, url))
    sys.stdout.flush()
    if not args.no_open:
        try:
            webbrowser.open(url)
        except Exception as exc:  # browser is best-effort only
            sys.stderr.write(f"warning: could not open browser: {exc}\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("已停止。")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
