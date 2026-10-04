# -*- coding: utf-8 -*-
"""primer-review —— 会话舱本地服务（Phase 0：上传·画布·圈选·台账，＋ LLM 对话面板）。

**定位**：把从参考输入（技术报告页面、概念图、参数表）到参数台账、模型与 manifest
的决策过程摆进浏览器：人看图、圈注、作答；模型侧出图、提问、落账。界面是三栏——
左＝资产树（模型／画布投放／渲染产出／上传基准），中＝按资产类型自适应的可视化
（多图看板／单图二维画布／三维视图），右＝对话面板。服务本身是
**哑文件经纪人**——所有智能在会话两端，页面只是视图。对话面板（``chat.jsonl`` ＋
``chat_meta.json``）同样只读写文件：人在这里发消息，``primer.scene.loop`` 那个进程
把消息交给所配的 LLM 并把回信写回同一个文件。

**文件协议**（会话目录即协议）::

    <session>/
      uploads/       人上传的原件（PDF／图片）＋ uploads/index.jsonl 台账
      pages/         PDF 按页光栅化（pdftoppm -png -r <dpi>）与图片副本
      canvas.json    画布（模型侧写）：items[].image＋矢量覆盖层 overlays[]
      cards.json     问题卡（模型侧写）：cards[]{id,title,text,options[],attach,allow_text}
      record.json    参数台账（模型侧写；页面只读呈现）：rows[]{id,subject,value,status,evidence,highlight}
      answers/       人的提交（服务写）：<card_id>.json 每卡一份＋_log.jsonl 追加流水
      renders/       模型渲染回放区（资产树的"渲染产出"组）

**覆盖层语法**（canvas.json overlays 元素）::

    {"id":"oct1","type":"polygon","points":[[x,y],...],"color":"#ff3b30",
     "label":"……","fill":0.15,"width":2.5}
    {"id":"axis","type":"polyline","points":[[x,y],...],"dash":true,...}
    {"id":"c1","type":"ellipse","center":[x,y],"rx":60,"ry":40,...}
    {"id":"t1","type":"label","at":[x,y],"label":"……"}

数组点一律为**原图像素坐标**；人侧的套索／擦除同此口径，回传不折损。

**端点**::

    GET  /                静态页面        GET  /state   全量状态（含 chat 与 assets 资产树）
    GET  /f/<rel>         会话文件        GET  /healthz 健康检查
    GET  /model/<rel>     模型文件（--assets 目录，白名单后缀；供资产树的三维视图）
    GET  /static/<rel>    静态资源（含 vendor/ 子目录的 three.js）
    POST /answer          JSON {card_id, choice, text, selections:[{image,mode,points}]}
    POST /chat            JSON {text, model}     追加一条用户消息到 chat.jsonl
    POST /reset-cards     JSON {reason?}         问题卡整体退役（cards_retired.json）并清空卡片区
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
ASSET_SUFFIXES = (".stl", ".glb", ".gltf", ".bin", ".png", ".jpg", ".jpeg")
MODEL_SUFFIXES = (".stl",)                     # 资产树"模型"组只收 STL（三维视图用 STLLoader）
STATIC_DIR = Path(__file__).resolve().parent / "static"
_CRAFT_RE = re.compile(r"^(col[0-3]|cmb)$", re.IGNORECASE)
MODELS_GROUP_ID = "g:models"
BOARD_ALL_ID = "g:board#all"

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
MAX_CHAT_CHARS = 8000
CHAT_TAIL_MESSAGES = 200
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
                 max_pages: int = DEFAULT_MAX_PAGES,
                 assets: Optional[str | os.PathLike] = None) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        for sub in SUBDIRS:
            (self.root / sub).mkdir(exist_ok=True)
        self.dpi = int(dpi)
        self.max_pages = int(max_pages)
        # 资产树"模型"组要读的模型目录（任务根：*.stl 整机与分件）。给空＝该组为空并带 note 文案。
        self.assets_dir = Path(assets).expanduser().resolve() if assets else None

    # ------------------------------------------------------------ guards/readers

    def file_path(self, rel: str) -> Path:
        """Resolve a session-relative path (percent-decoded); refuse escapes."""
        p = (self.root / unquote(rel).lstrip("/")).resolve()
        if p != self.root and self.root not in p.parents:
            raise ReviewError("path escapes session root")
        return p

    def asset_path(self, rel: str) -> Path:
        """Resolve an asset-relative path (资产树模型）；未配置 assets 或越界/后缀不在白名单时拒绝。"""
        if self.assets_dir is None:
            raise ReviewError("assets directory is not configured (--assets)")
        rel = unquote(rel).lstrip("/")
        p = (self.assets_dir / rel).resolve()
        if p != self.assets_dir and self.assets_dir not in p.parents:
            raise ReviewError("asset path escapes assets root")
        if p.suffix.lower() not in ASSET_SUFFIXES:
            raise ReviewError(f"asset suffix not allowed: {p.suffix}")
        return p

    def assets_state(self) -> Dict[str, Any]:
        """资产树：模型（--assets 目录）／画布总览（canvas.json）／渲染产出（renders/）／上传（uploads 台账）。

        树是左栏"资产树"的数据源，也是中间面板自适应渲染的依据：节点 ``kind`` 决定视图
        （``board`` 多图看板、``image`` 单图二维画布、``stl`` 三维视图、``group`` 可折叠容器）。
        首节点 ``g:board#all`` 是伪节点，选中＝原看板行为。
        """
        return {
            "dir": str(self.assets_dir) if self.assets_dir else None,
            "tree": [
                {"id": BOARD_ALL_ID, "label": "画布总览（全部投放项）", "kind": "board"},
                self._models_node(),
                self._board_node(),
                self._renders_node(),
                self._uploads_node(),
            ],
        }

    @staticmethod
    def _mb(nbytes: int) -> str:
        return f"{nbytes / 1048576:.1f}"

    @staticmethod
    def _tier(text: str) -> str:
        """档位标签：文件名/目录名带 HQ 记号的算"高精度"，其余"标准"（沿用任务侧命名习惯）。"""
        return "高精度" if "hq" in text.lower() else "标准"

    def _stl_node(self, path: Path, rel: str, label: str) -> Dict[str, Any]:
        node: Dict[str, Any] = {
            "id": "m:" + rel,
            "label": label,
            "kind": "stl",
            "url": "/model/" + rel,
            "rel": rel,
            "bytes": path.stat().st_size,
        }
        craft = _CRAFT_RE.match(path.stem)
        if craft:                                  # 分件名 COL0–3／CMB：三维视图按器配色
            node["craft"] = craft.group(1).upper()
        return node

    def _models_node(self) -> Dict[str, Any]:
        """模型组：``--assets`` 目录里的 ``*.stl``（depth ≤2）；整机直接挂组下，分件按目录成子组。"""
        node: Dict[str, Any] = {
            "id": MODELS_GROUP_ID, "label": "场景 · 模型 · 部件", "kind": "group", "children": [],
        }
        if self.assets_dir is None or not self.assets_dir.is_dir():
            node["note"] = "服务端未配置模型目录（--assets）；三维视图无模型可载。"
            return node
        roots: List[Path] = []
        subdirs: Dict[str, List[Path]] = {}
        for path in sorted(self.assets_dir.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in MODEL_SUFFIXES:
                continue
            rel = path.relative_to(self.assets_dir)
            if len(rel.parts) == 1:
                roots.append(path)
            elif len(rel.parts) == 2:
                subdirs.setdefault(rel.parts[0], []).append(path)
        for path in roots:
            rel = str(path.relative_to(self.assets_dir))
            node["children"].append(self._stl_node(
                path, rel, f"整机（{self._tier(path.stem)}）· {self._mb(path.stat().st_size)} MB"))
        for name in sorted(subdirs):
            kids = []
            for path in sorted(subdirs[name]):
                rel = str(path.relative_to(self.assets_dir))
                kids.append(self._stl_node(
                    path, rel, f"{path.stem} · {self._mb(path.stat().st_size)} MB"))
            node["children"].append({
                "id": "g:" + name,
                "label": f"分件（{self._tier(name)}）· {len(kids)} 件",
                "kind": "group",
                "children": kids,
            })
        if not node["children"]:
            node["note"] = f"目录里没有 *.stl 模型：{self.assets_dir}"
        return node

    def _board_node(self) -> Dict[str, Any]:
        """画布组：canvas.json 的 items 逐项透出（title/caption/overlays 原样，另给会话内相对路径 rel）。"""
        document = self._read_json(self.root / "canvas.json", {"items": []})
        children: List[Dict[str, Any]] = []
        for item in document.get("items") or []:
            if not isinstance(item, dict):
                continue
            image = str(item.get("image") or "")
            item_id = str(item.get("id") or image)
            node: Dict[str, Any] = {
                "id": "c:" + item_id,
                "label": str(item.get("title") or item_id),
                "kind": "image",
                "url": "/f/" + image,
                "rel": image,
                "overlays": item.get("overlays") or [],
            }
            for key in ("w", "h", "title", "caption"):
                if item.get(key) is not None:
                    node[key] = item[key]
            children.append(node)
        return {"id": "g:board", "label": "画布总览（模型投放）", "kind": "group",
                "children": children}

    def _renders_node(self) -> Dict[str, Any]:
        """渲染产出组：会话 ``renders/`` 下的图片（模型侧回放区）。"""
        children: List[Dict[str, Any]] = []
        for path in sorted((self.root / "renders").glob("*")):
            if not path.is_file() or path.suffix.lower() not in IMAGE_SUFFIXES:
                continue
            rel = f"renders/{path.name}"
            children.append({
                "id": "r:" + path.name, "label": path.name, "kind": "image",
                "url": "/f/" + rel, "rel": rel, "bytes": path.stat().st_size,
            })
        return {"id": "g:renders", "label": "渲染产出（renders/）", "kind": "group",
                "children": children}

    def _uploads_node(self) -> Dict[str, Any]:
        """上传组：``uploads/index.jsonl`` 台账逐条成节点；多页（PDF）成子组，逐页一个图片节点。"""
        children: List[Dict[str, Any]] = []
        for record in self._read_jsonl(self.root / "uploads" / "index.jsonl"):
            name = str(record.get("name") or "")
            if not name:
                continue
            pages = [str(p) for p in (record.get("pages") or [])]
            is_pdf = Path(name).suffix.lower() == ".pdf"
            if len(pages) > 1 or (is_pdf and pages):
                label = name + (f"（PDF · {len(pages)} 页）" if is_pdf else f"（{len(pages)} 页）")
                node: Dict[str, Any] = {
                    "id": "u:" + name, "label": label, "kind": "group",
                    "children": [{
                        "id": f"u:{name}#{i}", "label": f"第 {i} 页", "kind": "image",
                        "url": "/f/" + page, "rel": page,
                    } for i, page in enumerate(pages, 1)],
                }
            else:
                rel = pages[0] if pages else ""
                node = {
                    "id": "u:" + name, "label": name, "kind": "image",
                    "url": "/f/" + rel if rel else None, "rel": rel or None,
                }
                if record.get("note"):
                    node["note"] = str(record["note"])
            if record.get("desc"):
                node["caption"] = str(record["desc"])
            children.append(node)
        return {"id": "g:uploads", "label": "我上传的基准输入", "kind": "group",
                "children": children}

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
            "chat": self.chat_state(),
            "assets": self.assets_state(),
        }

    # ------------------------------------------------------------ chat

    def chat_state(self) -> Dict[str, Any]:
        """对话节点：最近 ``CHAT_TAIL_MESSAGES`` 条消息＋驱动写的 meta＋升级标记。

        三个文件都可能缺失（会话刚建、驱动还没跑过）：消息回空表，meta 回 ``None``，
        升级标记回 ``False``——页面据此显示"驱动未运行"的禁用态，而不是报错。
        """
        messages = self._read_jsonl(self.root / "chat.jsonl")
        meta = self._read_json(self.root / "chat_meta.json", None)
        return {
            "messages": messages[-CHAT_TAIL_MESSAGES:],
            "meta": meta if isinstance(meta, dict) else None,
            "escalation": (self.root / "ESCALATION.md").is_file(),
        }

    def save_chat(self, payload: dict) -> dict:
        """追加一条用户消息（``role: "user"``）；驱动按 chat.jsonl 的次序消费它。"""
        if not isinstance(payload, dict):
            raise ReviewError("chat body must be a JSON object")
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ReviewError("chat text must be a non-empty string")
        if len(text) > MAX_CHAT_CHARS:
            raise ReviewError(
                f"chat text is too long ({len(text)} > {MAX_CHAT_CHARS} characters)"
            )
        model = payload.get("model")
        if model is not None and not isinstance(model, str):
            raise ReviewError("chat model must be a string")
        record = {
            "ts": _now(),
            "role": "user",
            "text": text,
            "model": (model or "").strip() or None,
        }
        with (self.root / "chat.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        return {"ts": record["ts"], "chars": len(text), "model": record["model"]}

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

    # ------------------------------------------------------------ cards reset

    def reset_cards(self, reason: str = "") -> Dict[str, Any]:
        """问题卡整体退役：活动卡移入 ``cards_retired.json``，``cards.json`` 清空。

        会话舱"重置问题卡"按钮（人的动作）走这里。两份 JSON 写前都复制进 ``_backup/``，
        命名沿用驱动侧约定（``<stem>.<时间戳>.<后缀>``），重置可回滚。
        """
        ts = _now()
        path = self.root / "cards.json"
        document = self._read_json(path, {"cards": []})
        cards = [c for c in (document.get("cards") or []) if isinstance(c, dict)]
        retired_path = self.root / "cards_retired.json"
        retired_doc = self._read_json(retired_path, {"cards": []})
        retired = [c for c in (retired_doc.get("cards") or []) if isinstance(c, dict)]
        if not cards:
            return {"ok": True, "reset": 0, "retired_total": len(retired)}
        note = (reason or "").strip() or "人在会话舱重置"
        for card in cards:
            record = dict(card)
            record["retired_at"] = ts
            record["retired_reason"] = note
            retired.append(record)
        self._backup("cards.json")
        self._backup("cards_retired.json")
        path.write_text(json.dumps({"cards": []}, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
        retired_path.write_text(json.dumps({"cards": retired}, ensure_ascii=False, indent=2) + "\n",
                                encoding="utf-8")
        return {"ok": True, "reset": len(cards), "retired_total": len(retired), "reason": note}

    def _backup(self, filename: str) -> Optional[str]:
        """Copy a session JSON into ``_backup/``（命名与驱动侧一致：``<stem>.<stamp>.json``）。"""
        source = self.root / filename
        if not source.is_file():
            return None
        backup_dir = self.root / "_backup"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        destination = backup_dir / f"{source.stem}.{stamp}{source.suffix}"
        try:
            shutil.copy2(source, destination)
        except OSError:
            return None
        return destination.name

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
                    rel = unquote(url.path[len("/static/"):]).lstrip("/")
                    path = (STATIC_DIR / rel).resolve()
                    if path != STATIC_DIR and STATIC_DIR not in path.parents:
                        raise ReviewError("static path escapes static root")
                    self._send_file(path)
                elif url.path == "/healthz":
                    self._json({"ok": True, "session": str(self.session.root),
                                "ts": time.time()})
                elif url.path == "/state":
                    self._json(self.session.state())
                elif url.path.startswith("/model/"):
                    self._send_file(self.session.asset_path(url.path[len("/model/"):]))
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
                elif url.path == "/chat":
                    payload = self._read_json_body()
                    record = self.session.save_chat(payload)
                    self._json({"ok": True, "chat": record})
                elif url.path == "/reset-cards":
                    payload = self._read_json_body()
                    reason = payload.get("reason") if isinstance(payload, dict) else None
                    self._json(self.session.reset_cards(str(reason or "")))
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
        f"台账 {len(st['record'].get('rows', []))} 行｜页面图 {len(st['pages'])} 张｜"
        f"对话 {len(st['chat']['messages'])} 条",
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
    ap.add_argument("--assets", default=None,
                    help="optional model directory for the asset tree's 3D view "
                         "(served read-only at /model/<rel>)")
    ap.add_argument("--no-open", action="store_true",
                    help="do not open the browser automatically")
    ap.add_argument("--verbose", action="store_true", help="log requests to stderr")
    args = ap.parse_args(argv)

    try:
        session = Session(args.session, dpi=args.dpi, max_pages=args.max_pages,
                          assets=args.assets)
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
