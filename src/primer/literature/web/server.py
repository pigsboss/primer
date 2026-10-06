# -*- coding: utf-8 -*-
"""primer.literature.web.server —— 文献库 WebUI 的本地服务。

**定位**：把库文件（JSON 记录数组）摆进浏览器——项目树｜文献清单｜记录详情。
服务是本地哑后端：只读写**库文件本身**，不移动、不修改被引用的文献文件；
仅绑定 ``127.0.0.1``，不发起外网请求（打开 DOI 由浏览器自行跳转）。

端点（请求与响应均为 JSON，UTF-8）::

    GET    /                      静态页面（库未决时进入首启页）
    GET    /api/state             库路径＋记录全量＋本地文件记录＋项目树＋最近扫描
    GET    /api/files             本地文件记录全量＋待解析计数
    POST   /api/library           首启：{"path": ..., "mode": "new"|"open"}
    POST   /api/save              将当前库立即落盘（原子写＋.bak）
    POST   /api/save-as           另存到新文件并切换为当前库（目标已存在则拒绝）
    POST   /api/import            从另一份库 JSON 合并记录（含本地文件记录；uuid 冲突自动重分配）
    POST   /api/import/parse      上传 CSV／JSON／BibTeX／Markdown（原始字节，?name= 带文件名）→ 预览与建议映射
    POST   /api/import/commit     {token, mapping?} 提交导入（库格式可省 mapping；缺标题行跳过）
    POST   /api/import/refine     {token, scope} 启动 LLM 精析（后台线程；进度轮询 /api/import/refine/status）
    POST   /api/import/verify     {token, scope} 启动联网比对（OpenAlex 公共 API；命中置信门槛才回填）
    POST   /api/export            导出拷贝（可传 uuids 限子集；不切换当前库）
    POST   /api/records           新建记录（服务端补 uuid／时间戳），201
    POST   /api/records/enrich    对选中记录启动后台补全（{uuids, mode: "verify"|"ai"}；只预演，进度轮询 …/enrich/status）
    POST   /api/records/enrich/preview  {token} 取待应用变更清单（旧值→新值，供预览对话框）
    POST   /api/records/enrich/apply    {token} 用户确认后把预演变更落库
    POST   /api/records/bulk-update  {uuids, fields} 把字段合并进多条记录（只改给定字段）
    POST   /api/records/delete    删除多条记录（{uuids}；一次落盘）
    POST   /api/projects/create   新建项目（{path, uuids?}；可建空项目，声明存 <库名>.projects.json）
    POST   /api/projects/attach   把记录挂到项目（{path, uuids}；项目不存在就先声明，已挂的跳过）
    POST   /api/projects/move     移动项目（{path, target}；记录里的路径同步改写，含子孙）
    POST   /api/projects/rename   重命名项目（{path, name}；只改最后一段，记录同步替换）
    POST   /api/projects/delete   删除项目及全部子项目（{path}；从记录与注册表移除）
    PUT    /api/records/<uuid>    更新记录（整条替换）
    DELETE /api/records/<uuid>    删除记录
    POST   /api/scan              校验全部 files[].path 的存在性（不改库文件）
    POST   /api/open              用系统程序打开本地文件（仅限库中登记路径）
    POST   /api/files/pick        {kind: "folder"|"files"} 弹系统选择窗口（macOS）并返回所选路径
    POST   /api/files/scan        {path} 递归扫描文件夹，登记支持格式（pdf／图片）的文件记录，随后自动解析
    POST   /api/files/add         {paths} 登记指定文件（去重），随后自动解析
    POST   /api/files/parse       {uuids} 把文件记录重新排入解析队列
    POST   /api/files/open        {uuid, which: "source"|"markdown"} 用系统程序打开
    POST   /api/files/delete      {uuid} 删除文件记录（磁盘上的文件与产物不动）
    POST   /api/links/attach      {record_uuid, file_uuids, nature?} 把文件挂到文献记录（改挂／改性质）
    POST   /api/links/detach      {file_uuids} 解除文件记录的文献关联
    POST   /api/links/batch/start  {scope: "unlinked"|"all", online?} 启动「文件 × 文献」批量自动关联（后台计算，不改库；online 启用联网增强）
    POST   /api/links/batch/status {token} 关联任务进度；完成后附提案清单＋缺文清单
    POST   /api/links/batch/apply  {pairs} 用户确认后把关联对批量落库（一次落盘）
    POST   /api/links/batch/export {kind: "missing"|"pairs", path} 把最近一轮结果导出为 CSV
    POST   /api/fetch/scan        {scope: "missing"|"all"|"selected", uuids?, target_dir?, online?} 启动原文下载解析（后台；只生成计划；online 默认 true，false 则不联网补查）
    POST   /api/fetch/status      {token} 下载任务进度；解析完成附计划，下载完成附逐条结果
    POST   /api/fetch/download    {token, uuids, auto_parse?} 对勾选的「可直接下载」条目执行下载（后台；校验后登记挂链）
    GET    /static/<rel>          静态资源（限包内 static/ 目录）

错误响应统一为 ``{"error": <机器码>, "message": <英文>}``；状态码：校验 400、
未找到 404、冲突（重复 uuid／外部改动／文件已存在）409、其余 500。
人读启动报告写 stdout（中文）；请求日志与诊断写 stderr（英文）——
语言纪律见 docs/guides/CODING_STANDARDS.md v1.1。

命令行（经 ``python -m primer.literature web``）：默认库 ``./primer.literature.json``；
``--port`` 默认 8801，**被占用时回退随机空闲端口并在 stdout 打印**；``--no-open``
关闭自动开浏览器。每次改动立即落盘（库层原子写＋``.bak``）；库被外部改动过时
此次改动拒绝且内存回载磁盘版本（见 :meth:`LibraryService._persist`）。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
import traceback
import urllib.parse
import uuid as _uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional

from ..importers import SourceData, parse_source, rows_to_payloads
from ..library import FileRecord, Library, LibraryError, NATURES, Record, now_iso
from ..refine import refine_source, suspicious_rows
from ..tree import active_projects, build_tree, normalize_project_path

__all__ = ["LibraryService", "create_server", "make_handler", "open_path", "serve"]

DEFAULT_PORT = 8801
DEFAULT_DB_NAME = "primer.literature.json"
HOST = "127.0.0.1"

MAX_BODY_BYTES = 8 * 1024 * 1024
MAX_UPLOAD_BYTES = 32 * 1024 * 1024
IMPORT_KEEP = 4
STATIC_DIR = Path(__file__).resolve().parent / "static"

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}

_NOT_FOUND_PREFIXES = ("record not found",)
_CONFLICT_PREFIXES = (
    "duplicate uuid",
    "library file changed on disk",
    "file already exists",
)


class _HttpError(Exception):
    """路由层错误：状态码＋机器码＋英文消息。"""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def status_for(exc: LibraryError) -> int:
    """把库层错误映射为 HTTP 状态码（按库层消息前缀约定）。"""
    text = str(exc)
    if text.startswith(_NOT_FOUND_PREFIXES):
        return 404
    if text.startswith(_CONFLICT_PREFIXES):
        return 409
    return 400


def open_path(path: Path) -> None:
    """用系统默认程序打开一个本地文件（macOS ``open``／Linux ``xdg-open``）。"""
    if sys.platform == "darwin":
        command = "open"
    elif sys.platform.startswith("linux"):
        command = "xdg-open"
    else:
        raise LibraryError(f"opening files is not supported on this platform: {sys.platform}")
    if shutil.which(command) is None:
        raise LibraryError(f"opener command not found: {command}")
    try:
        subprocess.Popen(
            [command, str(path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        raise LibraryError(f"cannot open file: {exc}") from exc


class LibraryService:
    """服务状态：当前库（可能尚未加载）、最近一次扫描结果与串行锁。"""

    def __init__(
        self,
        default_path: Path,
        *,
        chat_sender: Optional[Callable[[str, str], str]] = None,
        web_lookup: Optional[Callable[[str], list[dict[str, Any]]]] = None,
        parser: Optional[Callable[[list[Path], Path], Any]] = None,
        picker: Optional[Callable[[str], Optional[list[str]]]] = None,
    ):
        self.default_path = Path(default_path)
        self.path: Optional[Path] = None
        self.library: Optional[Library] = None
        self.scan: Optional[dict[str, list[dict[str, Any]]]] = None
        self.imports: dict[str, Any] = {}
        self.refines: dict[str, dict[str, Any]] = {}
        self.enrich_job: dict[str, Any] = {}
        self.link_job: dict[str, Any] = {}
        self.fetch_job: dict[str, Any] = {}
        self.declared: list[str] = []
        self._chat_sender = chat_sender
        self._web_lookup = web_lookup
        self._parser = parser
        self._picker = picker
        self._doi_title_lookup: Optional[Callable[[str], str]] = None
        self._fetch_opener: Optional[Callable] = None
        self._fetch_check_pdfinfo = True
        self._parse_threads: list[threading.Thread] = []
        self._retiring: set[int] = set()
        self.load_error = ""
        self._lock = threading.RLock()

    @classmethod
    def initial(
        cls,
        db_arg: Optional[str],
        *,
        chat_sender: Optional[Callable[[str, str], str]] = None,
        web_lookup: Optional[Callable[[str], list[dict[str, Any]]]] = None,
        parser: Optional[Callable[[list[Path], Path], Any]] = None,
        picker: Optional[Callable[[str], Optional[list[str]]]] = None,
    ) -> "LibraryService":
        """按 ``--db`` 或默认路径准备服务；库文件在就加载，不在就等首启页。"""
        default = Path(db_arg).expanduser() if db_arg else Path.cwd() / DEFAULT_DB_NAME
        service = cls(
            default,
            chat_sender=chat_sender,
            web_lookup=web_lookup,
            parser=parser,
            picker=picker,
        )
        if default.is_file():
            try:
                with service._lock:
                    service.library = Library.load(default)
                    service.path = default
                service._load_declared()
                service._ensure_parse_worker()
            except LibraryError as exc:
                service.load_error = str(exc)
        return service

    # ------------------------------------------------------------- 状态

    def state_payload(self) -> dict[str, Any]:
        with self._lock:
            if self.library is None:
                return {
                    "loaded": False,
                    "default_path": str(self.default_path),
                    "error": self.load_error,
                }
            return {
                "loaded": True,
                "path": str(self.library.path),
                "records": [record.to_dict() for record in self.library.records],
                "file_records": [record.to_dict() for record in self.library.file_records],
                "tree": build_tree(self.library.records, declared=self.declared).to_dict(),
                "scan": self.scan,
            }

    def record_count(self) -> Optional[int]:
        with self._lock:
            return len(self.library.records) if self.library else None

    def display_path(self) -> str:
        with self._lock:
            return str(self.library.path if self.library else self.default_path)

    # ------------------------------------------------------------- 首启

    def set_library(self, path_text: Any, mode: Any) -> dict[str, Any]:
        """首启页动作：``mode="new"`` 新建空库，``"open"`` 载入既有库。"""
        if not isinstance(path_text, str) or not path_text.strip():
            raise LibraryError("library path is required")
        if mode not in ("new", "open"):
            raise LibraryError(f"unknown mode: {mode!r} (expected 'new' or 'open')")
        path = Path(path_text).expanduser()
        library = Library.create(path) if mode == "new" else Library.load(path)
        with self._lock:
            self.library = library
            self.path = path
            self.scan = None
            self.load_error = ""
        self._load_declared()
        self._ensure_parse_worker()
        return self.state_payload()

    # ------------------------------------------------------------- 记录

    def add_record(self, data: Any) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise LibraryError("record payload must be an object")
        with self._lock:
            library = self._require()
            record = library.add_record(data)
            self._persist(library)
            return record.to_dict()

    def update_record(self, uuid: str, data: Any) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise LibraryError("record payload must be an object")
        with self._lock:
            library = self._require()
            record = library.update_record(uuid, data)
            self._persist(library)
            return record.to_dict()

    def delete_record(self, uuid: str) -> dict[str, Any]:
        with self._lock:
            library = self._require()
            record = library.delete_record(uuid)
            self._persist(library)
            return record.to_dict()

    def clear_records(self) -> dict[str, Any]:
        """清空整个库（全部记录）；落盘走 ``_persist``（原子写＋``.bak``）。

        文件记录保留，但其关联与指向已删记录的「疑似重复」候选一并清除。
        """
        with self._lock:
            library = self._require()
            removed = len(library.records)
            if not removed:
                return {"cleared": 0}
            removed_uuids = {record.uuid for record in library.records}
            library.records = []
            for file_record in library.file_records:
                file_record.record_uuid = ""
                file_record.nature = ""
                file_record.prune_dup(removed_uuids)
            self._persist(library)
            return {"cleared": removed}

    # ------------------------------------------------------------- 补全

    def start_enrich(self, uuids: Any, mode: Any) -> dict[str, Any]:
        """对选中记录启动后台补全任务：``mode`` = ``"verify"``（学术引擎）｜``"ai"``（LLM）。"""
        if mode not in ("verify", "ai"):
            raise LibraryError(f"unknown enrich mode: {mode!r} (expected 'verify' or 'ai')")
        if not isinstance(uuids, list) or not all(isinstance(item, str) for item in uuids):
            raise LibraryError("uuids must be a list of strings")
        with self._lock:
            library = self._require()
            job = self.enrich_job
            if job.get("status") == "running":
                raise LibraryError("an enrich job is already running")
            known = {record.uuid: record for record in library.records}
            records: list[Record] = []
            for uuid_text in uuids:
                record = known.get(uuid_text)
                if record is not None and record not in records:
                    records.append(record)
            if not records:
                raise LibraryError("no matching records for the given uuids")
            token = str(_uuid.uuid4())
            self.enrich_job = {
                "token": token,
                "mode": mode,
                "status": "running",
                "done": 0,
                "total": len(records),
                "updated": 0,
                "skipped": 0,
                "failed": 0,
                "error": "",
            }
            thread = threading.Thread(
                target=self._run_enrich, args=(token, records, mode), daemon=True
            )
        thread.start()
        return {"started": True, "token": token, "total": len(records)}

    def enrich_status(self, token: Any) -> dict[str, Any]:
        """补全任务进度；完成后附 更新／未变化／失败 条数。"""
        if not isinstance(token, str) or not token:
            raise LibraryError("enrich token is required")
        with self._lock:
            job = self.enrich_job
            if not job or job.get("token") != token:
                raise LibraryError(f"no enrich job for token: {token}")
            payload = {
                "status": job["status"],
                "mode": job["mode"],
                "done": job["done"],
                "total": job["total"],
                "updated": job["updated"],
                "skipped": job["skipped"],
                "failed": job["failed"],
            }
            if job["status"] == "failed":
                payload["error"] = job["error"]
            return payload

    def _run_enrich(self, token: str, records: list[Record], mode: str) -> None:
        """后台补全线程：只**预演**变更（不改记录、不落盘），待前端预览后应用。

        任何异常都收成 job 的 failed；预演清单存 ``job["pending"]``。
        """
        from ..enrich import preview_ai_parse, preview_verify_records

        job = self.enrich_job
        if job.get("token") != token:
            return

        def progress(done: int) -> None:
            with self._lock:
                job["done"] = done

        try:
            if mode == "verify":
                report, pending = preview_verify_records(
                    records, self._build_engines(), on_progress=progress
                )
            else:
                sender = self._chat_sender or self._build_sender()
                report, pending = preview_ai_parse(
                    records, list(range(len(records))), sender, on_progress=progress
                )
        except Exception as exc:  # 配置/网络错误：整任务失败，记录保持原值
            with self._lock:
                job["status"] = "failed"
                job["error"] = str(exc)[:300] or type(exc).__name__
            return
        with self._lock:
            job["updated"] = report.updated
            job["skipped"] = report.skipped
            job["failed"] = report.failed
            job["done"] = job["total"]
            job["pending"] = list(pending)
            job["status"] = "done"

    def enrich_preview(self, token: Any) -> dict[str, Any]:
        """取预演清单（供前端「变更预览」对话框展示）。"""
        if not isinstance(token, str) or not token:
            raise LibraryError("enrich token is required")
        with self._lock:
            job = self.enrich_job
            if not job or job.get("token") != token:
                raise LibraryError(f"no enrich job for token: {token}")
            if job.get("status") != "done":
                raise LibraryError("enrich job is not ready for preview")
            return {"pending": list(job.get("pending") or [])}

    def apply_enrich(self, token: Any) -> dict[str, Any]:
        """把预演清单落库（用户确认后才调用）；返回实际更新的记录数。"""
        if not isinstance(token, str) or not token:
            raise LibraryError("enrich token is required")
        from ..enrich import apply_pending

        with self._lock:
            job = self.enrich_job
            if not job or job.get("token") != token:
                raise LibraryError(f"no enrich job for token: {token}")
            if job.get("status") != "done":
                raise LibraryError("enrich job is not ready to apply")
            pending = list(job.get("pending") or [])
            if not pending:
                raise LibraryError("no pending changes to apply")
            library = self._require()
            updated = apply_pending(library.records, pending)
            job["pending"] = []
            if updated:
                self._persist(library, invalidate_scan=False)
            return {"updated": updated}

    # ----------------------------------------------------- 批量自动关联

    def start_link_batch(self, scope: Any, online: Any = False) -> dict[str, Any]:
        """启动一轮「文件 × 文献」自动关联（后台线程；只计算，不改库）。

        ``online`` 为真时启用联网增强：对本地未命中的文件按 DOI／题名查学术引擎。
        """
        if scope not in ("unlinked", "all"):
            raise LibraryError(f"unknown scope: {scope!r} (expected 'unlinked' or 'all')")
        online = bool(online)
        with self._lock:
            library = self._require()
            job = self.link_job
            if job.get("status") == "running":
                raise LibraryError("a link batch job is already running")
            records = list(library.records)
            files = list(library.file_records)
            library_dir = library.path.parent
            token = str(_uuid.uuid4())
            self.link_job = {
                "token": token,
                "scope": scope,
                "online": online,
                "status": "running",
                "done": 0,
                "total": len(files),
                "result": None,
                "error": "",
            }
            thread = threading.Thread(
                target=self._run_link_batch,
                args=(token, records, files, scope, online, library_dir),
                daemon=True,
            )
        thread.start()
        return {"started": True, "token": token, "total": len(files), "online": online}

    def link_batch_status(self, token: Any) -> dict[str, Any]:
        """关联任务进度；完成后附 ``result``（提案＋缺文清单＋统计）。"""
        if not isinstance(token, str) or not token:
            raise LibraryError("link batch token is required")
        with self._lock:
            job = self.link_job
            if not job or job.get("token") != token:
                raise LibraryError(f"no link batch job for token: {token}")
            payload = {
                "status": job["status"],
                "scope": job["scope"],
                "online": job.get("online", False),
                "done": job["done"],
                "total": job["total"],
            }
            if job["status"] == "failed":
                payload["error"] = job["error"]
            if job["status"] == "done" and job.get("result") is not None:
                payload["result"] = job["result"]
            return payload

    def _run_link_batch(
        self,
        token: str,
        records: list[Record],
        files: list[FileRecord],
        scope: str,
        online: bool,
        library_dir: Path,
    ) -> None:
        """后台关联线程：读解析产物、跑匹配（可选联网增强）；异常收成 job failed。"""
        from ..link import propose_links

        job = self.link_job
        if job.get("token") != token:
            return

        def read_markdown(file_record: FileRecord) -> str:
            md_path = getattr(file_record, "md_path", "")
            if not md_path:
                return ""
            try:
                return (library_dir / md_path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                return ""

        def progress(done: int, total: int) -> None:
            with self._lock:
                job["done"] = done
                job["total"] = total

        online_lookup = None
        if online:

            def online_lookup(file_record: FileRecord, candidates: list[str]) -> list[str]:
                """联网增强：DOI → Crossref 规范题名；没有就按题名走引擎链取候选题名。"""
                titles: list[str] = []
                doi = str(getattr(file_record, "doi", "") or "").strip()
                if doi:
                    try:
                        if self._doi_title_lookup is not None:
                            title = self._doi_title_lookup(doi) or ""
                        else:
                            from ..engines import crossref_doi_title

                            title = crossref_doi_title(doi) or ""
                    except Exception:
                        title = ""
                    if title:
                        titles.append(str(title))
                if not titles:
                    for engine in self._build_engines():
                        for candidate in list(candidates)[:2]:
                            try:
                                results = engine(candidate) or []
                            except Exception:
                                continue
                            if results:
                                best = str(results[0].get("title") or "").strip()
                                if best:
                                    titles.append(best)
                                break
                        if titles:
                            break
                return titles

        try:
            result = propose_links(
                records,
                files,
                scope=scope,
                read_markdown=read_markdown,
                online_lookup=online_lookup,
                on_progress=progress,
            )
        except Exception as exc:
            with self._lock:
                job["status"] = "failed"
                job["error"] = str(exc)[:300] or type(exc).__name__
            return
        with self._lock:
            job["result"] = result
            job["done"] = job["total"]
            job["status"] = "done"

    def apply_link_batch(self, pairs: Any) -> dict[str, Any]:
        """把用户确认的关联对批量落库（一次落盘）；返回 关联／改挂 计数。"""
        if (
            not isinstance(pairs, list)
            or not pairs
            or not all(
                isinstance(item, dict)
                and isinstance(item.get("file_uuid"), str)
                and isinstance(item.get("record_uuid"), str)
                for item in pairs
            )
        ):
            raise LibraryError("pairs must be a non-empty list of {file_uuid, record_uuid, nature?}")
        with self._lock:
            library = self._require()
            resolved: list[tuple[FileRecord, Record, str]] = []
            for item in pairs:
                file_record = library.find_file(item["file_uuid"])
                if file_record is None:
                    raise LibraryError(f"file record not found: {item['file_uuid']}")
                record = library.find(item["record_uuid"])
                if record is None:
                    raise LibraryError(f"record not found: {item['record_uuid']}")
                nature = item.get("nature") or "title-match"
                if nature not in NATURES:
                    allowed = "/".join(NATURES)
                    raise LibraryError(f"nature: expected one of {allowed}, got {nature!r}")
                resolved.append((file_record, record, nature))
            linked = 0
            replaced = 0
            for file_record, record, nature in resolved:
                if file_record.record_uuid == record.uuid and file_record.nature == nature:
                    continue
                if file_record.record_uuid and file_record.record_uuid != record.uuid:
                    replaced += 1
                file_record.record_uuid = record.uuid
                file_record.nature = nature
                file_record.dup = {}
                file_record.updated_at = now_iso()
                linked += 1
            if linked:
                self._persist(library, invalidate_scan=False)
            return {"linked": linked, "replaced": replaced}

    def export_link_batch(self, kind: Any, path_text: Any) -> dict[str, Any]:
        """把最近一轮关联结果导出为 CSV（``kind`` = ``"missing"``｜``"pairs"``）；不切换库。"""
        import csv as _csv

        if kind not in ("missing", "pairs"):
            raise LibraryError(f"unknown export kind: {kind!r} (expected 'missing' or 'pairs')")
        if not isinstance(path_text, str) or not path_text.strip():
            raise LibraryError("export path is required")
        path = Path(path_text).expanduser()
        with self._lock:
            self._require()
            job = self.link_job
            if not job or job.get("status") != "done" or job.get("result") is None:
                raise LibraryError("no link batch result to export")
            result = job["result"]
            if path.exists():
                raise LibraryError(f"file already exists: {path}")
            if kind == "missing":
                header = ["题名", "年份", "来源", "最像的文件", "相似度"]
                rows = [
                    [
                        item.get("title", ""),
                        item.get("year", ""),
                        item.get("venue", ""),
                        item.get("closest_file", ""),
                        item.get("closest_ratio", ""),
                    ]
                    for item in result.get("missing") or []
                ]
            else:
                header = ["文件", "层级", "相似度", "记录题名", "记录年份", "证据", "nature"]
                rows = [
                    [
                        item.get("name", ""),
                        item.get("tier", ""),
                        item.get("ratio", ""),
                        item.get("record_title", ""),
                        item.get("record_year", ""),
                        item.get("how", ""),
                        item.get("nature", ""),
                    ]
                    for item in result.get("proposals") or []
                ]
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = _csv.writer(handle)
            writer.writerow(header)
            writer.writerows(rows)
        return {"exported": len(rows), "path": str(path)}

    # ------------------------------------------------- 批量下载原文

    def _default_fetch_dir(self, library: Library) -> Path:
        """默认下载目录：现存已关联文件最集中的目录；没有则库文件旁的「下载」。"""
        counts: dict[Path, int] = {}
        for record in library.file_records:
            try:
                parent = self._resolve_source(library, record.path).parent
            except OSError:
                continue
            counts[parent] = counts.get(parent, 0) + 1
        if counts:
            return max(counts, key=lambda key: counts[key])
        return library.path.parent / "下载"

    def start_fetch_scan(
        self, scope: Any, uuids: Any = None, target_dir: Any = None, online: Any = True
    ) -> dict[str, Any]:
        """启动「批量下载原文」的解析阶段（后台线程；只生成计划，不下载、不改库）。

        ``online`` 为假时不做现场引擎补查——计划只用记录里已有的链接（秒级出结果）。
        """
        if scope not in ("missing", "all", "selected"):
            raise LibraryError(
                f"unknown scope: {scope!r} (expected 'missing'/'all'/'selected')"
            )
        with self._lock:
            library = self._require()
            job = self.fetch_job
            if job.get("status") == "running":
                raise LibraryError("a fetch job is already running")
            if scope == "selected":
                if (
                    not isinstance(uuids, list)
                    or not uuids
                    or not all(isinstance(item, str) for item in uuids)
                ):
                    raise LibraryError("uuids must be a non-empty list of strings")
                wanted = set(uuids)
                records = [record for record in library.records if record.uuid in wanted]
            elif scope == "missing":
                linked = {f.record_uuid for f in library.file_records if f.record_uuid}
                records = [record for record in library.records if record.uuid not in linked]
            else:
                records = list(library.records)
            if not records:
                raise LibraryError("no records for the given scope")
            if isinstance(target_dir, str) and target_dir.strip():
                target = Path(target_dir.strip()).expanduser()
                if not target.is_absolute():
                    target = library.path.parent / target
            else:
                target = self._default_fetch_dir(library)
            engines = self._build_engines() if online else []
            token = str(_uuid.uuid4())
            self.fetch_job = {
                "token": token,
                "scope": scope,
                "phase": "scan",
                "status": "running",
                "done": 0,
                "total": len(records),
                "target_dir": str(target),
                "result": None,
                "download": None,
                "error": "",
            }
            thread = threading.Thread(
                target=self._run_fetch_scan, args=(token, records, engines), daemon=True
            )
        thread.start()
        return {
            "started": True,
            "token": token,
            "total": len(records),
            "target_dir": str(target),
        }

    def fetch_status(self, token: Any) -> dict[str, Any]:
        """下载任务进度；解析完成附 ``result``（计划清单），下载完成附 ``download``（逐条结果）。"""
        if not isinstance(token, str) or not token:
            raise LibraryError("fetch token is required")
        with self._lock:
            job = self.fetch_job
            if not job or job.get("token") != token:
                raise LibraryError(f"no fetch job for token: {token}")
            payload = {
                "status": job["status"],
                "phase": job["phase"],
                "scope": job["scope"],
                "done": job["done"],
                "total": job["total"],
                "target_dir": job["target_dir"],
            }
            if job["status"] == "failed":
                payload["error"] = job["error"]
            if job.get("result") is not None:
                payload["result"] = job["result"]
            if job.get("download") is not None:
                payload["download"] = job["download"]
            return payload

    def _run_fetch_scan(
        self, token: str, records: list[Record], engines: list[Callable]
    ) -> None:
        """解析线程：逐条生成下载计划（不改库、不下载）。"""
        from ..fetch import resolve_download_plan

        job = self.fetch_job
        if job.get("token") != token:
            return
        plans: list[dict[str, Any]] = []
        stats = {"direct": 0, "manual": 0, "none": 0}
        try:
            for index, record in enumerate(records, start=1):
                plan = resolve_download_plan(record, engines=engines)
                plans.append(
                    {
                        "record_uuid": record.uuid,
                        "title": record.title,
                        "year": record.year,
                        "doi": record.doi or "",
                        "url": plan["url"],
                        "source": plan["source"],
                        "expected": plan["expected"],
                        "reason": plan["reason"],
                    }
                )
                stats[plan["expected"]] += 1
                with self._lock:
                    job["done"] = index
        except Exception as exc:
            with self._lock:
                job["status"] = "failed"
                job["error"] = str(exc)[:300] or type(exc).__name__
            return
        with self._lock:
            job["result"] = {"plans": plans, "stats": stats}
            job["done"] = job["total"]
            job["status"] = "done"

    def start_fetch_download(
        self, token: Any, uuids: Any, auto_parse: Any = False
    ) -> dict[str, Any]:
        """进入下载阶段：只下载勾选且为「可直接下载」的条目；后台线程执行。"""
        if not isinstance(token, str) or not token:
            raise LibraryError("fetch token is required")
        if (
            not isinstance(uuids, list)
            or not uuids
            or not all(isinstance(item, str) for item in uuids)
        ):
            raise LibraryError("uuids must be a non-empty list of strings")
        with self._lock:
            job = self.fetch_job
            if not job or job.get("token") != token:
                raise LibraryError(f"no fetch job for token: {token}")
            if job.get("phase") != "scan" or job.get("status") != "done":
                raise LibraryError("fetch scan is not ready")
            result = job.get("result") or {}
            wanted = set(uuids)
            plans = [
                plan
                for plan in (result.get("plans") or [])
                if plan["record_uuid"] in wanted and plan["expected"] == "direct"
            ]
            if not plans:
                raise LibraryError("no downloadable items in the selection")
            job["phase"] = "download"
            job["status"] = "running"
            job["done"] = 0
            job["total"] = len(plans)
            job["download"] = None
            target = Path(job["target_dir"])
            auto = bool(auto_parse)
            opener = self._fetch_opener
            check_pdfinfo = self._fetch_check_pdfinfo
            thread = threading.Thread(
                target=self._run_fetch_download,
                args=(token, plans, target, auto, opener, check_pdfinfo),
                daemon=True,
            )
        thread.start()
        return {"started": True, "total": len(plans), "target_dir": str(target)}

    def _run_fetch_download(
        self,
        token: str,
        plans: list[dict[str, Any]],
        target_dir: Path,
        auto_parse: bool,
        opener: Optional[Callable],
        check_pdfinfo: bool,
    ) -> None:
        """下载线程：逐条下载→校验→登记→挂链；每 5 条落盘一次，结束再落盘一次。"""
        from ..fetch import HostThrottle, download_pdf, safe_filename, unique_path

        job = self.fetch_job
        if job.get("token") != token:
            return
        linked_snapshot: set[str] = set()
        with self._lock:
            library = self.library
            if library is not None:
                linked_snapshot = {f.record_uuid for f in library.file_records if f.record_uuid}
        items: list[dict[str, Any]] = []
        counts = {"downloaded": 0, "manual": 0, "failed": 0, "skipped": 0}
        pending_persist = 0
        try:
            throttle = HostThrottle(0.0 if opener is not None else 1.0)
            target_dir.mkdir(parents=True, exist_ok=True)
            for index, plan in enumerate(plans, start=1):
                record_uuid = plan["record_uuid"]
                item: dict[str, Any] = {
                    "record_uuid": record_uuid,
                    "title": plan.get("title", ""),
                    "url": plan.get("url", ""),
                    "status": "",
                    "error": "",
                    "name": "",
                    "size": 0,
                }
                with self._lock:
                    library = self.library
                    record = library.find(record_uuid) if library is not None else None
                if record is None:
                    item.update(status="missing-record", error="记录已不存在")
                    counts["failed"] += 1
                elif record_uuid in linked_snapshot:
                    item.update(status="already-have", error="已有本地文件")
                    counts["skipped"] += 1
                else:
                    dest = unique_path(target_dir, safe_filename(record.title, record.year))
                    outcome: dict[str, Any] = {}
                    for attempt in range(2):
                        throttle.wait(plan.get("url", ""))
                        outcome = download_pdf(
                            plan.get("url", ""),
                            dest,
                            opener=opener,
                            check_pdfinfo=check_pdfinfo,
                        )
                        if outcome.get("ok") or outcome.get("status") != "failed":
                            break
                        time.sleep(1.0)
                    status = str(outcome.get("status", "failed"))
                    if outcome.get("ok"):
                        with self._lock:
                            library = self.library
                            record = library.find(record_uuid) if library is not None else None
                            if library is None or record is None:
                                item.update(status="failed", error="库已切换")
                                counts["failed"] += 1
                            else:
                                file_record = library.add_file_record(
                                    {
                                        "path": self._stored_path(library, dest),
                                        "name": dest.name,
                                        "size": int(outcome.get("size") or 0),
                                        "md5": self._file_md5(dest),
                                        "status": "pending" if auto_parse else "downloaded",
                                    }
                                )
                                file_record.record_uuid = record.uuid
                                file_record.nature = "auto-download"
                                file_record.updated_at = now_iso()
                                linked_snapshot.add(record_uuid)
                                pending_persist += 1
                                item.update(
                                    status="downloaded",
                                    name=dest.name,
                                    size=outcome.get("size") or 0,
                                )
                                counts["downloaded"] += 1
                                if pending_persist >= 5:
                                    try:
                                        self._persist(library, invalidate_scan=False)
                                        pending_persist = 0
                                    except LibraryError:
                                        pass
                    else:
                        item.update(status=status, error=str(outcome.get("error") or ""))
                        if status in ("html", "blocked"):
                            counts["manual"] += 1
                        else:
                            counts["failed"] += 1
                items.append(item)
                with self._lock:
                    job["done"] = index
        except Exception as exc:
            with self._lock:
                job["status"] = "failed"
                job["error"] = str(exc)[:300] or type(exc).__name__
        finally:
            with self._lock:
                library = self.library
                if library is not None and pending_persist:
                    try:
                        self._persist(library, invalidate_scan=False)
                    except LibraryError:
                        pass
                job["download"] = {"items": items, "stats": counts}
                if job.get("status") == "running":
                    job["status"] = "done"
        if auto_parse and counts["downloaded"]:
            self._ensure_parse_worker()

    def bulk_update_records(self, uuids: Any, fields: Any) -> dict[str, Any]:
        """把 ``fields`` 合并进选中记录（只改给定字段）；返回更新条数。"""
        from ..library import _RECORD_TEXT_FIELDS, now_iso

        if not isinstance(uuids, list) or not all(isinstance(item, str) for item in uuids):
            raise LibraryError("uuids must be a list of strings")
        if not isinstance(fields, dict) or not fields:
            raise LibraryError("fields must be a non-empty object")
        patch: dict[str, Any] = {}
        for key, value in fields.items():
            if key == "title":
                if not isinstance(value, str) or not value.strip():
                    raise LibraryError("title must be a non-empty string")
                patch["title"] = value.strip()
            elif key in ("authors", "editor", "translator"):
                if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                    raise LibraryError(f"{key} must be a list of strings")
                patch[key] = [item.strip() for item in value if item.strip()]
            elif key == "year":
                if value is None:
                    patch["year"] = None
                elif isinstance(value, int) and not isinstance(value, bool):
                    patch["year"] = value
                else:
                    raise LibraryError("year must be an integer or null")
            elif key in ("type", "venue", "notes") or key in _RECORD_TEXT_FIELDS:
                if not isinstance(value, str):
                    raise LibraryError(f"{key} must be a string")
                patch[key] = value.strip() if key != "notes" else value
            elif key == "doi":
                if value is None:
                    patch["doi"] = None
                elif isinstance(value, str):
                    patch["doi"] = value.strip() or None
                else:
                    raise LibraryError("doi must be a string or null")
            else:
                raise LibraryError(f"field is not editable: {key}")
        with self._lock:
            library = self._require()
            updated = 0
            for uuid_text in uuids:
                record = library.find(uuid_text)
                if record is None:
                    continue
                changed = False
                for key, value in patch.items():
                    current = getattr(record, key)
                    if key in ("authors", "editor", "translator"):
                        same = list(current or []) == list(value)
                    else:
                        same = (current or None) == (value or None)
                    if same:
                        continue
                    setattr(record, key, value)
                    changed = True
                if changed:
                    record.updated_at = now_iso()
                    updated += 1
            if updated:
                self._persist(library, invalidate_scan=False)
            return {"updated": updated}

    def bulk_delete_records(self, uuids: Any) -> dict[str, Any]:
        """删除选中的多条记录（一次落盘）；其关联文件自动解除关联。"""
        if not isinstance(uuids, list) or not all(isinstance(item, str) for item in uuids):
            raise LibraryError("uuids must be a list of strings")
        with self._lock:
            library = self._require()
            wanted = set(uuids)
            before = len(library.records)
            library.records = [record for record in library.records if record.uuid not in wanted]
            deleted = before - len(library.records)
            if deleted:
                for file_record in library.file_records:
                    if file_record.record_uuid in wanted:
                        file_record.record_uuid = ""
                        file_record.nature = ""
                    file_record.prune_dup(wanted)
                self._persist(library)
            return {"deleted": deleted}

    # ------------------------------------------------------------- 项目

    @staticmethod
    def _registry_path(library: Library) -> Path:
        """项目注册表旁车文件：与库同目录、``<库名>.projects.json``。"""
        return library.path.with_suffix(".projects.json")

    def _load_declared(self) -> None:
        """从旁车文件读"声明的项目"（可为空项目）；缺失或损坏时置空并记 stderr。"""
        self.declared = []
        if self.library is None:
            return
        path = self._registry_path(self.library)
        if not path.is_file():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            items = payload.get("projects") if isinstance(payload, dict) else payload
            if not isinstance(items, list):
                raise ValueError("projects must be a list")
        except (ValueError, OSError) as exc:
            print(f"[web] cannot read project registry {path}: {exc}", file=sys.stderr)
            return
        seen: set[str] = set()
        declared: list[str] = []
        for raw in items:
            if not isinstance(raw, str):
                continue
            norm = normalize_project_path(raw)
            if norm and norm not in seen:
                seen.add(norm)
                declared.append(norm)
        self.declared = declared

    def _save_declared(self, library: Library) -> None:
        path = self._registry_path(library)
        try:
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(
                json.dumps({"projects": self.declared}, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            tmp.replace(path)
        except OSError as exc:
            raise LibraryError(f"cannot write project registry: {exc}") from exc

    def _all_projects(self, library: Library) -> set[str]:
        """现有全部项目路径：记录派生的 ＋ 声明的。"""
        paths: set[str] = set(self.declared)
        for record in library.records:
            paths.update(active_projects(record))
        return paths

    @staticmethod
    def _project_exists(existing: set[str], path: str) -> bool:
        """路径存在（显式）或其下已有子孙（隐式父节点也算存在）。"""
        prefix = path + "/"
        return path in existing or any(item.startswith(prefix) for item in existing)

    @staticmethod
    def _path_conflict(existing: set[str], new_path: str) -> bool:
        """目标路径是否与现有项目冲突（同名，或其下已有其它项目）。"""
        prefix = new_path + "/"
        return new_path in existing or any(item.startswith(prefix) for item in existing)

    def _rewrite_projects(self, library: Library, old_prefix: str, new_prefix: str) -> int:
        """把记录里 old_prefix（含子孙）改写为 new_prefix；返回更新的记录数。"""
        from ..library import now_iso

        old_with_slash = old_prefix + "/"
        updated = 0
        for record in library.records:
            paths = active_projects(record)
            new_paths: list[str] = []
            changed = False
            for path in paths:
                if path == old_prefix:
                    new_paths.append(new_prefix)
                    changed = True
                elif path.startswith(old_with_slash):
                    new_paths.append(new_prefix + path[len(old_prefix):])
                    changed = True
                else:
                    new_paths.append(path)
            if not changed:
                continue
            seen: set[str] = set()
            deduped: list[str] = []
            for item in new_paths:
                if item not in seen:
                    seen.add(item)
                    deduped.append(item)
            record.projects = deduped
            record.updated_at = now_iso()
            updated += 1
        return updated

    @staticmethod
    def _rewrite_declared(declared: list[str], old_prefix: str, new_prefix: str) -> list[str]:
        old_with_slash = old_prefix + "/"
        out: list[str] = []
        seen: set[str] = set()
        for path in declared:
            if path == old_prefix:
                path = new_prefix
            elif path.startswith(old_with_slash):
                path = new_prefix + path[len(old_prefix):]
            if path not in seen:
                seen.add(path)
                out.append(path)
        return out

    def create_project(self, path_text: Any, uuids: Any = None) -> dict[str, Any]:
        """新建项目（可为空项目）：写注册表；给了 ``uuids`` 就同时挂到这些记录上。"""
        if not isinstance(path_text, str):
            raise LibraryError("project path is required")
        path = normalize_project_path(path_text)
        if not path:
            raise LibraryError("project path is empty after normalization")
        if uuids is not None and (
            not isinstance(uuids, list) or not all(isinstance(item, str) for item in uuids)
        ):
            raise LibraryError("uuids must be a list of strings")
        from ..library import now_iso

        with self._lock:
            library = self._require()
            if self._project_exists(self._all_projects(library), path):
                raise LibraryError(f"project already exists: {path}")
            self.declared.append(path)
            self.declared.sort()
            updated = 0
            if uuids:
                known = {record.uuid: record for record in library.records}
                for uuid_text in uuids:
                    record = known.get(uuid_text)
                    if record is None:
                        continue
                    current = active_projects(record)
                    if path in current:
                        continue
                    record.projects = current + [path]
                    record.updated_at = now_iso()
                    updated += 1
            if updated:
                self._persist(library, invalidate_scan=False)
            self._save_declared(library)
            return {"path": path, "updated": updated}

    def attach_project(self, path_text: Any, uuids: Any) -> dict[str, Any]:
        """把记录挂到指定项目：项目不存在就先声明；已挂的记录跳过。"""
        if not isinstance(path_text, str):
            raise LibraryError("project path is required")
        path = normalize_project_path(path_text)
        if not path:
            raise LibraryError("project path is empty after normalization")
        if (
            not isinstance(uuids, list)
            or not uuids
            or not all(isinstance(item, str) for item in uuids)
        ):
            raise LibraryError("uuids must be a non-empty list of strings")
        from ..library import now_iso

        with self._lock:
            library = self._require()
            created = False
            if not self._project_exists(self._all_projects(library), path):
                self.declared.append(path)
                self.declared.sort()
                created = True
            known = {record.uuid: record for record in library.records}
            updated = 0
            for uuid_text in uuids:
                record = known.get(uuid_text)
                if record is None:
                    continue
                current = active_projects(record)
                if path in current:
                    continue
                record.projects = current + [path]
                record.updated_at = now_iso()
                updated += 1
            if updated:
                self._persist(library, invalidate_scan=False)
            if created or updated:
                self._save_declared(library)
            return {"path": path, "created": created, "updated": updated}

    def move_project(self, path_text: Any, target_text: Any) -> dict[str, Any]:
        """把项目（含子孙）整体移到目标父项目下；目标空串＝移到顶层。"""
        if not isinstance(path_text, str) or not isinstance(target_text, str):
            raise LibraryError("project path and target are required")
        path = normalize_project_path(path_text)
        target = normalize_project_path(target_text)
        if not path:
            raise LibraryError("project path is empty after normalization")
        if target == path or target.startswith(path + "/"):
            raise LibraryError("cannot move a project into itself or its descendant")
        with self._lock:
            library = self._require()
            existing = self._all_projects(library)
            if not self._project_exists(existing, path):
                raise LibraryError(f"project not found: {path}")
            if target and not self._project_exists(existing, target):
                raise LibraryError(f"target project not found: {target}")
            leaf = path.rsplit("/", 1)[-1]
            new_path = f"{target}/{leaf}" if target else leaf
            if new_path == path:
                return {"path": path, "updated": 0}
            if self._path_conflict(existing, new_path):
                raise LibraryError(f"target already has a project named {leaf!r}: {new_path}")
            updated = self._rewrite_projects(library, path, new_path)
            self.declared = sorted(self._rewrite_declared(self.declared, path, new_path))
            if updated:
                self._persist(library, invalidate_scan=False)
            self._save_declared(library)
            return {"path": new_path, "updated": updated}

    def rename_project(self, path_text: Any, name_text: Any) -> dict[str, Any]:
        """重命名项目最后一段；子孙路径随之替换。"""
        if not isinstance(path_text, str) or not isinstance(name_text, str):
            raise LibraryError("project path and name are required")
        path = normalize_project_path(path_text)
        name = str(name_text).strip()
        if not path:
            raise LibraryError("project path is empty after normalization")
        if not name or "/" in name:
            raise LibraryError("name must be a single path segment without '/'")
        parent = path.rsplit("/", 1)[0] if "/" in path else ""
        new_path = f"{parent}/{name}" if parent else name
        with self._lock:
            library = self._require()
            existing = self._all_projects(library)
            if not self._project_exists(existing, path):
                raise LibraryError(f"project not found: {path}")
            if new_path == path:
                return {"path": path, "updated": 0}
            if self._path_conflict(existing, new_path):
                raise LibraryError(f"project already exists: {new_path}")
            updated = self._rewrite_projects(library, path, new_path)
            self.declared = sorted(self._rewrite_declared(self.declared, path, new_path))
            if updated:
                self._persist(library, invalidate_scan=False)
            self._save_declared(library)
            return {"path": new_path, "updated": updated}

    def delete_project(self, path_text: Any) -> dict[str, Any]:
        """删除项目及其全部子项目：从记录与注册表中移除这些路径。"""
        if not isinstance(path_text, str):
            raise LibraryError("project path is required")
        path = normalize_project_path(path_text)
        if not path:
            raise LibraryError("project path is empty after normalization")
        prefix = path + "/"
        from ..library import now_iso

        with self._lock:
            library = self._require()
            before = len(self.declared)
            self.declared = [
                item for item in self.declared if item != path and not item.startswith(prefix)
            ]
            updated = 0
            for record in library.records:
                paths = active_projects(record)
                kept = [item for item in paths if item != path and not item.startswith(prefix)]
                if len(kept) != len(paths):
                    record.projects = kept
                    record.updated_at = now_iso()
                    updated += 1
            if updated:
                self._persist(library, invalidate_scan=False)
            if updated or len(self.declared) != before:
                self._save_declared(library)
            return {"path": path, "updated": updated}

    # ------------------------------------------------------------- 本地文件

    #: 扫描/添加时接受的源文件格式。
    SUPPORTED_SUFFIXES = (".pdf", ".png", ".jpg", ".jpeg", ".webp")

    #: 解析队列：并发工作线程数，以及单次 ``mineru-kit`` 调用最多携带的文件数。
    PARSE_WORKERS = 2
    PARSE_BATCH_SIZE = 4

    def file_records_payload(self) -> dict[str, Any]:
        """本地文件记录全量＋待解析计数（前端轮询用）；每条附源文件存在性 ``exists``。"""
        with self._lock:
            library = self._require()
            files = []
            for record in library.file_records:
                payload = record.to_dict()
                try:
                    payload["exists"] = self._resolve_source(library, record.path).is_file()
                except OSError:
                    payload["exists"] = False
                files.append(payload)
            parsing = sum(
                1 for record in library.file_records if record.status in ("pending", "parsing")
            )
            return {"files": files, "parsing": parsing}

    def pick_paths(self, kind: Any) -> dict[str, Any]:
        """弹出系统文件选择窗口（macOS）：``folder`` 选文件夹、``files`` 多选文件。"""
        if kind not in ("folder", "files"):
            raise LibraryError(f"unknown pick kind: {kind!r} (expected 'folder' or 'files')")
        picker = self._picker or self._default_pick
        paths = picker(kind)
        if paths is None:
            return {"canceled": True, "paths": []}
        return {"canceled": False, "paths": paths}

    @staticmethod
    def _default_pick(kind: str) -> Optional[list[str]]:
        """macOS 原生选择窗口：返回所选 POSIX 路径；用户取消返回 ``None``。"""
        if sys.platform != "darwin":
            raise LibraryError("native file picker is only supported on macOS")
        if kind == "folder":
            script = 'POSIX path of (choose folder with prompt "选择要扫描的文件夹")'
        else:
            script = (
                'set theFiles to (choose file with prompt "选择文件（可多选）" '
                "with multiple selections allowed)\n"
                'set out to ""\n'
                "repeat with f in theFiles\n"
                "set out to out & (POSIX path of f) & linefeed\n"
                "end repeat\n"
                "return out"
            )
        try:
            completed = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=600,
            )
        except subprocess.TimeoutExpired:
            raise LibraryError("file picker timed out")
        if completed.returncode != 0:
            message = (completed.stderr or "").strip()
            if "-128" in message or "canceled" in message.lower():
                return None
            raise LibraryError(f"file picker failed: {message[:200] or completed.returncode}")
        paths = [line.strip() for line in (completed.stdout or "").splitlines() if line.strip()]
        if kind == "folder":
            return paths[:1]
        return paths

    def scan_files_folder(self, path_text: Any) -> dict[str, Any]:
        """递归扫描文件夹下所有支持格式并新增文件记录（跳过已有）；随后自动开始解析。"""
        import os

        if not isinstance(path_text, str) or not path_text.strip():
            raise LibraryError("folder path is required")
        folder = Path(path_text).expanduser()
        if not folder.is_dir():
            raise LibraryError(f"folder not found: {folder}")
        with self._lock:
            library = self._require()
            parsed_root = (library.path.parent / "parsed").resolve()
        found: list[Path] = []
        for dirpath, dirnames, filenames in os.walk(folder):
            current = Path(dirpath)
            try:
                resolved = current.resolve()
            except OSError:
                continue
            if resolved == parsed_root or parsed_root in resolved.parents:
                dirnames[:] = []
                continue
            dirnames[:] = [name for name in dirnames if not name.startswith(".")]
            for filename in filenames:
                if filename.startswith("."):
                    continue
                if Path(filename).suffix.lower() in self.SUPPORTED_SUFFIXES:
                    found.append(current / filename)
        with self._lock:
            result = self._add_paths(library, found)
        self._ensure_parse_worker()
        return result

    def add_files(self, paths: Any) -> dict[str, Any]:
        """按给定路径列表新增文件记录（跳过已有）；随后自动开始解析。"""
        if (
            not isinstance(paths, list)
            or not paths
            or not all(isinstance(item, str) and item.strip() for item in paths)
        ):
            raise LibraryError("paths must be a non-empty list of strings")
        candidates = [Path(item).expanduser() for item in paths]
        missing = [str(path) for path in candidates if not path.is_file()]
        if missing:
            raise LibraryError(f"file not found: {missing[0]}")
        with self._lock:
            library = self._require()
            result = self._add_paths(library, candidates)
        self._ensure_parse_worker()
        return result

    def _resolve_source(self, library: Library, path_text: str) -> Path:
        path = Path(path_text).expanduser()
        if not path.is_absolute():
            path = library.path.parent / path
        return path

    def _stored_path(self, library: Library, path: Path) -> str:
        """库目录内→相对路径；库外→原样绝对（与文献记录的 files 约定一致）。"""
        try:
            return str(path.resolve().relative_to(library.path.parent.resolve()))
        except (OSError, ValueError):
            return str(path)

    @staticmethod
    def _file_md5(path: Path) -> str:
        """文件内容 MD5（分块读取）；读不动时返回空串。"""
        import hashlib

        digest = hashlib.md5()
        try:
            with open(path, "rb") as handle:
                while True:
                    chunk = handle.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
        except OSError:
            return ""
        return digest.hexdigest()

    def _add_paths(self, library: Library, paths: list[Path]) -> dict[str, Any]:
        """（需持锁）新增文件记录：按绝对路径与**内容 MD5** 双重查重。

        路径重复 → 静默跳过（``skipped``）；内容重复（MD5 命中已有记录或
        本批先登记者）→ 不登记，记入 ``duplicates``（``{"path", "same_as"}``，
        便于界面提示"哪份与哪份重复"）。
        """
        known: set[str] = set()
        known_md5: dict[str, str] = {}
        for record in library.file_records:
            try:
                known.add(str(self._resolve_source(library, record.path).resolve()))
            except OSError:
                known.add(record.path)
            if record.md5:
                known_md5.setdefault(record.md5, record.name or record.path)
        added = 0
        skipped = 0
        duplicates: list[dict[str, str]] = []
        for path in paths:
            try:
                key = str(path.resolve())
            except OSError:
                key = str(path)
            if key in known:
                skipped += 1
                continue
            md5 = self._file_md5(path)
            if md5 and md5 in known_md5:
                duplicates.append({"path": str(path), "same_as": known_md5[md5]})
                continue
            known.add(key)
            if md5:
                known_md5.setdefault(md5, path.name)
            size = 0
            try:
                size = path.stat().st_size
            except OSError:
                pass
            library.add_file_record(
                {
                    "path": self._stored_path(library, path),
                    "name": path.name,
                    "size": size,
                    "md5": md5,
                }
            )
            added += 1
        if added:
            self._persist(library, invalidate_scan=False)
        return {
            "added": added,
            "skipped": skipped,
            "duplicates": duplicates,
            "total": len(library.file_records),
        }

    def delete_file_record(self, uuid: Any) -> dict[str, Any]:
        """删除一条本地文件记录（磁盘上的文件与解析产物不动）。"""
        if not isinstance(uuid, str) or not uuid:
            raise LibraryError("file record uuid is required")
        with self._lock:
            library = self._require()
            record = library.delete_file_record(uuid)
            self._persist(library, invalidate_scan=False)
            return record.to_dict()

    def parse_files(self, uuids: Any) -> dict[str, Any]:
        """把指定文件记录置为待解析并启动解析队列（重试失败／重跑）。"""
        if (
            not isinstance(uuids, list)
            or not uuids
            or not all(isinstance(item, str) for item in uuids)
        ):
            raise LibraryError("uuids must be a non-empty list of strings")
        with self._lock:
            library = self._require()
            queued = 0
            for uuid_text in uuids:
                record = library.find_file(uuid_text)
                if record is None or record.status in ("pending", "parsing"):
                    continue
                record.status = "pending"
                record.error = ""
                record.updated_at = now_iso()
                queued += 1
            if queued:
                self._persist(library, invalidate_scan=False)
        self._ensure_parse_worker()
        return {"queued": queued}

    def open_file_record(self, uuid: Any, which: Any = "source") -> dict[str, Any]:
        """用系统程序打开源文件或解析产物（markdown）。"""
        if not isinstance(uuid, str) or not uuid:
            raise LibraryError("file record uuid is required")
        if which not in ("source", "markdown"):
            raise LibraryError(f"unknown target: {which!r} (expected 'source' or 'markdown')")
        with self._lock:
            library = self._require()
            record = library.find_file(uuid)
            if record is None:
                raise LibraryError(f"file record not found: {uuid}")
            if which == "source":
                target = self._resolve_source(library, record.path)
            else:
                if not record.md_path:
                    raise LibraryError("this file has no parsed markdown yet")
                target = library.path.parent / record.md_path
            if not target.is_file():
                raise LibraryError(f"file not found on disk: {target}")
            open_path(target)
            return {"opened": str(target)}

    # ------------------------------------------------------------- 关联

    def attach_files_to_record(
        self, record_uuid: Any, file_uuids: Any, nature: Any = "doi-consistent"
    ) -> dict[str, Any]:
        """把若干文件记录挂到一条文献记录下（支持改挂与改性质）。"""
        if not isinstance(record_uuid, str) or not record_uuid:
            raise LibraryError("record uuid is required")
        if (
            not isinstance(file_uuids, list)
            or not file_uuids
            or not all(isinstance(item, str) and item for item in file_uuids)
        ):
            raise LibraryError("file_uuids must be a non-empty list of strings")
        if nature not in NATURES:
            allowed = "/".join(NATURES)
            raise LibraryError(f"nature: expected one of {allowed}, got {nature!r}")
        with self._lock:
            library = self._require()
            if library.find(record_uuid) is None:
                raise LibraryError(f"record not found: {record_uuid}")
            for uuid_text in file_uuids:
                if library.find_file(uuid_text) is None:
                    raise LibraryError(f"file record not found: {uuid_text}")
            linked = 0
            replaced = 0
            for uuid_text in file_uuids:
                file_record = library.find_file(uuid_text)
                if file_record.record_uuid == record_uuid and file_record.nature == nature:
                    continue
                if file_record.record_uuid and file_record.record_uuid != record_uuid:
                    replaced += 1
                file_record.record_uuid = record_uuid
                file_record.nature = nature
                file_record.dup = {}
                file_record.updated_at = now_iso()
                linked += 1
            if linked:
                self._persist(library, invalidate_scan=False)
            return {"linked": linked, "replaced": replaced, "record_uuid": record_uuid}

    def detach_files(self, file_uuids: Any) -> dict[str, Any]:
        """解除若干文件记录的文献关联（文件记录本身与其产物不动）。"""
        if (
            not isinstance(file_uuids, list)
            or not file_uuids
            or not all(isinstance(item, str) and item for item in file_uuids)
        ):
            raise LibraryError("file_uuids must be a non-empty list of strings")
        with self._lock:
            library = self._require()
            for uuid_text in file_uuids:
                if library.find_file(uuid_text) is None:
                    raise LibraryError(f"file record not found: {uuid_text}")
            detached = 0
            for uuid_text in file_uuids:
                file_record = library.find_file(uuid_text)
                if not file_record.record_uuid and not file_record.nature:
                    continue
                file_record.record_uuid = ""
                file_record.nature = ""
                file_record.updated_at = now_iso()
                detached += 1
            if detached:
                self._persist(library, invalidate_scan=False)
            return {"detached": detached}

    def _ensure_parse_worker(self) -> None:
        """有待解析记录时补齐解析工作线程（幂等；最多 ``PARSE_WORKERS`` 个在班）。"""
        with self._lock:
            library = self.library
            if library is None:
                return
            self._parse_threads = [thread for thread in self._parse_threads if thread.is_alive()]
            self._retiring &= {thread.ident for thread in self._parse_threads}
            active = [
                thread for thread in self._parse_threads if thread.ident not in self._retiring
            ]
            if len(active) >= self.PARSE_WORKERS:
                return
            # 进程崩溃遗留的 parsing 视作待重试（仅在无在班线程时重置，避免踩到在跑的批次）
            if not active:
                for record in library.file_records:
                    if record.status == "parsing":
                        record.status = "pending"
            if not any(record.status == "pending" for record in library.file_records):
                return
            threads: list[threading.Thread] = []
            while len(active) + len(threads) < self.PARSE_WORKERS:
                thread = threading.Thread(target=self._parse_worker, daemon=True)
                self._parse_threads.append(thread)
                threads.append(thread)
        for thread in threads:
            thread.start()

    def _parse_worker(self) -> None:
        """后台解析队列（``PARSE_WORKERS`` 路并发）：每轮领一批 pending 交给 MinerU。"""
        while True:
            try:
                claimed = self._claim_batch()
            except LibraryError:
                return  # 领取时落盘失败（外部改动等）：本路退场，等下一次 ensure 补人
            if claimed is None:
                if self._worker_retire():
                    return
                continue
            library, batch = claimed
            self._parse_batch(library, batch)

    def _worker_retire(self) -> bool:
        """（持锁）无活可干时的退场握手：真退场返回 ``True``，有新活返回 ``False``。

        退场承诺与二次查活放在同一个临界区，关闭"worker 退场瞬间恰好有人登记新文件、
        而 :meth:`_ensure_parse_worker` 又看到本线程尚活不补人"的竞态：两端谁先谁后，
        都有一方兜住新文件。
        """
        with self._lock:
            library = self.library
            if library is None:
                return True
            if any(item.status == "pending" for item in library.file_records):
                return False
            self._retiring.add(threading.get_ident())
            return True

    def _claim_batch(self) -> Optional[tuple[Library, list[Any]]]:
        """（持锁）领取一批待解析记录并置为 parsing；没有可领的返回 ``None``。"""
        with self._lock:
            library = self.library
            if library is None:
                return None
            batch = [
                item for item in library.file_records if item.status == "pending"
            ][: self.PARSE_BATCH_SIZE]
            if not batch:
                return None
            for record in batch:
                record.status = "parsing"
                record.updated_at = now_iso()
            self._persist(library, invalidate_scan=False)
            return library, batch

    def _parse_batch(self, library: Library, batch: list[Any]) -> None:
        """处理一批待解析文件（worker 线程里跑，不持锁）。

        优先**单次批量调用**（省去每份文件的进程与模型加载开销）。mineru-kit
        批量遇首个错误即中止整批，因此缺产物者**逐文件单独重跑**，保住失败隔离。
        """
        parsed_root = library.path.parent / "parsed"
        staging = parsed_root / "_zips" / f"w{threading.get_ident()}"
        entries = [(record, self._resolve_source(library, record.path)) for record in batch]
        archives: dict[Path, Path] = {}
        error = ""
        try:
            staging.mkdir(parents=True, exist_ok=True)
            outcome = self._run_parser([source for _, source in entries], staging)
            for key, value in (outcome.outputs or {}).items():
                candidate = Path(value)
                if candidate.is_file():
                    archives[Path(key)] = candidate
            if outcome.returncode != 0:
                error = outcome.error or f"mineru exited with code {outcome.returncode}"
        except Exception as exc:  # 命令缺失、超时等
            error = str(exc)[:300] or type(exc).__name__
        for record, source in entries:
            archive = archives.get(source)
            if archive is not None:
                self._finish_one(library, record, source, archive)
        if len(entries) > 1:
            for record, source in entries:
                if source not in archives:
                    self._parse_batch(library, [record])
        elif entries and entries[0][1] not in archives:
            self._fail_one(library, entries[0][0], error or "mineru finished but wrote no archive")
        try:
            staging.rmdir()  # 空目录顺手清掉；有残留时忽略
        except OSError:
            pass

    def _finish_one(self, library: Library, record: Any, source: Path, archive: Path) -> None:
        """解包一份产物并落库（worker 线程里跑）；失败记入 ``record.error``。"""
        from ..postprocess import unpack_archive

        parsed_root = library.path.parent / "parsed"
        with self._lock:
            dest = self._parsed_dir_for(library, record, parsed_root, source.stem)
            reserved = not record.md_path
            if reserved:  # 先占住目录名，避免并发的同主干文件撞目录
                record.md_path = str((dest / "markdown.md").relative_to(library.path.parent))
        error = ""
        try:
            unpack_archive(archive, dest, replace_existing=True)
            if not (dest / "markdown.md").is_file():
                error = "archive has no markdown.md"
            else:
                (dest / "model_output.json").unlink(missing_ok=True)
        except Exception as exc:
            error = f"cannot unpack archive: {exc}"[:300]
        try:
            archive.unlink(missing_ok=True)
        except OSError:
            pass
        doi = ""
        eprint = ""
        if not error:
            doi, eprint = self._extract_identifiers(dest / "markdown.md")
        with self._lock:
            if error and reserved:
                record.md_path = ""
            record.updated_at = now_iso()
            if error:
                record.status = "failed"
                record.error = error
            else:
                record.status = "done"
                record.error = ""
                record.md_path = str((dest / "markdown.md").relative_to(library.path.parent))
                record.doi = doi
                record.eprint = eprint
                record.dup = {}
                if record.record_uuid and library.find(record.record_uuid) is None:
                    record.record_uuid = ""
                    record.nature = ""
                if not record.record_uuid:
                    link_uuid, nature, dup = self._resolve_link(library, record)
                    if link_uuid:
                        record.record_uuid = link_uuid
                        record.nature = nature
                    else:
                        record.dup = dup
            try:
                self._persist(library, invalidate_scan=False)
            except LibraryError:
                pass

    def _fail_one(self, library: Library, record: Any, error: str) -> None:
        """把一条记录标为解析失败并落盘（worker 线程里跑）。"""
        with self._lock:
            record.updated_at = now_iso()
            record.status = "failed"
            record.error = error
            try:
                self._persist(library, invalidate_scan=False)
            except LibraryError:
                pass

    @staticmethod
    def _extract_identifiers(markdown_path: Path) -> tuple[str, str]:
        """从解析产物 markdown 里提取 DOI 与 arXiv 编号（取第一个；无则空串）。"""
        import re

        try:
            text = markdown_path.read_text(encoding="utf-8", errors="replace")[:500_000]
        except OSError:
            return "", ""
        doi = ""
        doi_match = re.search(r"10\.\d{4,9}/[^\s\"'<>()\[\]{}]+", text)
        if doi_match:
            doi = doi_match.group(0).rstrip(".,;）)】]")
        eprint = ""
        for pattern in (
            r"arxiv\.org/abs/(\d{4}\.\d{4,5})(?:v\d+)?",
            r"(?i)arxiv[:\s]*(\d{4}\.\d{4,5})(?:v\d+)?",
        ):
            match = re.search(pattern, text)
            if match:
                eprint = match.group(1)
                break
        return doi, eprint

    @staticmethod
    def _resolve_link(library: Library, record: Any) -> tuple[str, str, dict[str, Any]]:
        """解析完成后的自动关联：DOI 优先、其次 arXiv。

        返回 ``(record_uuid, nature, dup)``。唯一命中文献记录时自动挂链（DOI →
        ``doi-consistent``，arXiv → ``preprint-substitute``）；候选多个、arXiv
        只命中已发表记录、或只能与未关联文件比对时，空链接＋``dup`` 待人工确认。
        """
        for kind in ("doi", "eprint"):
            value = (record.doi if kind == "doi" else record.eprint) or ""
            if not value:
                continue
            needle = value.strip().lower()

            def _matches(other: Any) -> bool:
                other_value = (other.doi or "") if kind == "doi" else (other.eprint or "")
                return bool(other_value) and other_value.strip().lower() == needle

            candidates: list[Any] = []
            seen: set[str] = set()

            def _add(candidate: Any) -> None:
                if candidate.uuid not in seen:
                    seen.add(candidate.uuid)
                    candidates.append(candidate)

            for other in library.file_records:
                if other is record or not other.record_uuid or not _matches(other):
                    continue
                target = library.find(other.record_uuid)
                if target is not None:
                    _add(target)
            direct = [item for item in library.records if _matches(item)]
            direct_candidates = (
                [item for item in direct if item.type == "preprint" or not item.doi]
                if kind == "eprint"
                else direct
            )
            for target in direct_candidates:
                _add(target)

            if len(candidates) == 1:
                nature = "doi-consistent" if kind == "doi" else "preprint-substitute"
                return candidates[0].uuid, nature, {}
            if candidates:
                return "", "", {
                    "kind": kind,
                    "value": value,
                    "matches": [
                        {"kind": "record", "uuid": item.uuid, "label": item.title or item.uuid}
                        for item in candidates
                    ],
                }
            if direct:
                return "", "", {
                    "kind": kind,
                    "value": value,
                    "matches": [
                        {"kind": "record", "uuid": item.uuid, "label": item.title or item.uuid}
                        for item in direct
                    ],
                }
            file_hits = [
                other
                for other in library.file_records
                if other is not record and not other.record_uuid and _matches(other)
            ]
            if file_hits:
                return "", "", {
                    "kind": kind,
                    "value": value,
                    "matches": [
                        {"kind": "file", "uuid": item.uuid, "label": item.name or item.path}
                        for item in file_hits
                    ],
                }
        return "", "", {}

    def _parsed_dir_for(
        self, library: Library, record: Any, parsed_root: Path, stem: str
    ) -> Path:
        """解析产物目录：重跑复用旧目录；首次挑一个不与现有记录冲突的名字。"""
        if record.md_path:
            existing = library.path.parent / record.md_path
            return existing.parent
        taken: set[str] = set()
        for other in library.file_records:
            if other is record or not other.md_path:
                continue
            taken.add(str((library.path.parent / other.md_path).parent))
        target = parsed_root / stem
        counter = 2
        while str(target) in taken or target.exists():
            target = parsed_root / f"{stem}-{counter}"
            counter += 1
        return target

    def _run_parser(self, sources: list[Path], output_dir: Path) -> Any:
        """调用解析器：注入者优先，否则本机 ``mineru-kit``（档位 standard）。

        批量调用按文件数放宽时限（每份一份基础时限），避免整批被单份拖死。
        """
        if self._parser is not None:
            return self._parser(list(sources), output_dir)
        from ..backends import MineruBackend

        backend = MineruBackend()
        timeout = None
        if backend.timeout is not None:
            timeout = backend.timeout * max(1, len(sources))
        return backend.parse(list(sources), output_dir, "standard", timeout=timeout)

    # ------------------------------------------------------------- 扫描

    def scan_files(self) -> dict[str, list[dict[str, Any]]]:
        """校验每个文件的存在性；只读，不碰库文件。"""
        with self._lock:
            library = self._require()
            base = library.path.parent
            result: dict[str, list[dict[str, Any]]] = {}
            for record in library.records:
                entries = []
                for entry in record.files:
                    file_path = Path(entry.path)
                    resolved = file_path if file_path.is_absolute() else base / file_path
                    entries.append({"path": entry.path, "exists": resolved.is_file()})
                result[record.uuid] = entries
            self.scan = result
            return result

    # ------------------------------------------------------------- 打开

    def open_file(self, uuid: Any, index: Any) -> dict[str, Any]:
        """打开某条记录的第 ``index`` 个本地文件；只认库中登记过的路径。"""
        if not isinstance(uuid, str) or not uuid:
            raise LibraryError("record uuid is required")
        if isinstance(index, bool) or not isinstance(index, int):
            raise LibraryError("file index must be an integer")
        with self._lock:
            library = self._require()
            record = library.find(uuid)
            if record is None:
                raise LibraryError(f"record not found: {uuid}")
            if not 0 <= index < len(record.files):
                raise LibraryError(f"file index out of range: {index}")
            entry = record.files[index]
            file_path = Path(entry.path)
            resolved = file_path if file_path.is_absolute() else library.path.parent / file_path
            if not resolved.is_file():
                raise LibraryError(f"file not found on disk: {entry.path}")
            open_path(resolved)
            return {"opened": entry.path}

    # ------------------------------------------------------------- 文件

    def save_library(self) -> dict[str, Any]:
        """把当前库立即落盘（原子写＋``.bak``）；内容未变时等于再写一遍。"""
        with self._lock:
            library = self._require()
            self._persist(library, invalidate_scan=False)
            return {"saved": True, "path": str(library.path)}

    def save_as(self, path_text: Any) -> dict[str, Any]:
        """把当前库另存到新文件，并切换为当前库（目标已存在则拒绝）。"""
        if not isinstance(path_text, str) or not path_text.strip():
            raise LibraryError("library path is required")
        path = Path(path_text).expanduser()
        with self._lock:
            library = self._require()
            if path.resolve() == library.path.resolve():
                raise LibraryError(f"target is the current library: {path}")
            if path.exists():
                raise LibraryError(f"file already exists: {path}")
            new_library = Library(path, list(library.records))
            new_library.save()
            self.library = new_library
            self.path = path
            self.scan = None
            self.load_error = ""
            if self.declared:
                self._save_declared(new_library)
            return self.state_payload()

    def import_records(self, path_text: Any) -> dict[str, Any]:
        """从另一份库 JSON 合并记录（含本地文件记录）；uuid 冲突者自动重新分配。"""
        if not isinstance(path_text, str) or not path_text.strip():
            raise LibraryError("import path is required")
        path = Path(path_text).expanduser()
        with self._lock:
            library = self._require()
            if path.resolve() == library.path.resolve():
                raise LibraryError("cannot import the current library into itself")
            source = Library.load(path)
            known = {record.uuid for record in library.records}
            file_known = {record.uuid for record in library.file_records}
            imported = 0
            renamed = 0
            uuid_map: dict[str, str] = {}
            for record in source.records:
                if record.uuid in known:
                    new_uuid = str(_uuid.uuid4())
                    uuid_map[record.uuid] = new_uuid
                    record.uuid = new_uuid
                    renamed += 1
                known.add(record.uuid)
                library.records.append(record)
                imported += 1
            files_imported = 0
            files_renamed = 0
            for record in source.file_records:
                if record.uuid in file_known:
                    record.uuid = str(_uuid.uuid4())
                    files_renamed += 1
                file_known.add(record.uuid)
                if record.record_uuid:
                    record.record_uuid = uuid_map.get(record.record_uuid, record.record_uuid)
                library.file_records.append(record)
                files_imported += 1
            if imported or files_imported:
                self._persist(library)
            return {
                "imported": imported,
                "renamed": renamed,
                "files": files_imported,
                "files_renamed": files_renamed,
            }

    def export_records(self, path_text: Any, uuids: Any = None) -> dict[str, Any]:
        """把当前库（或 ``uuids`` 子集）导出为一份拷贝；不切换当前库。

        全量导出连同本地文件记录一起写出；子集导出只含选中的文献记录。
        """
        if not isinstance(path_text, str) or not path_text.strip():
            raise LibraryError("export path is required")
        path = Path(path_text).expanduser()
        with self._lock:
            library = self._require()
            if uuids is None:
                records = list(library.records)
                file_records = list(library.file_records)
            else:
                if not isinstance(uuids, list) or any(
                    not isinstance(item, str) for item in uuids
                ):
                    raise LibraryError("uuids must be a list of strings")
                wanted = set(uuids)
                records = [record for record in library.records if record.uuid in wanted]
                file_records = []
            if path.exists():
                raise LibraryError(f"file already exists: {path}")
            Library(path, records, file_records).save()
            return {"exported": len(records), "files": len(file_records), "path": str(path)}

    # ------------------------------------------------------------- 导入

    def parse_import(self, name: Any, data: bytes) -> dict[str, Any]:
        """解析上传的导入文件，返回预览与建议映射（按 token 暂存待提交）。"""
        if not isinstance(name, str) or not name.strip():
            raise LibraryError("file name is required")
        if not isinstance(data, (bytes, bytearray)) or not data:
            raise LibraryError("file content is required")
        with self._lock:
            self._require()
            source = parse_source(name.strip(), bytes(data))
            token = _uuid.uuid4().hex
            self.imports[token] = source
            while len(self.imports) > IMPORT_KEEP:
                evicted = next(iter(self.imports))
                self.imports.pop(evicted)
                self.refines.pop(evicted, None)
            return self._preview_payload(token, source)

    def commit_import(self, token: Any, mapping: Any = None) -> dict[str, Any]:
        """按映射把暂存的导入数据合并进当前库（uuid 冲突重分配；缺标题行跳过）。"""
        if not isinstance(token, str) or not token:
            raise LibraryError("import token is required")
        with self._lock:
            library = self._require()
            job = self.refines.get(token)
            if job is not None and job.get("status") == "running":
                raise LibraryError("refinement is still running; wait for it to finish")
            source = self.imports.pop(token, None)
            self.refines.pop(token, None)
            if source is None:
                raise LibraryError(f"unknown or expired import token: {token}")
            skipped = 0
            if source.kind == "library":
                records = list(source.records)
            else:
                if not isinstance(mapping, dict):
                    raise LibraryError("mapping is required for table imports")
                payloads, skipped = rows_to_payloads(
                    source.rows, source.columns, mapping, raws=source.raws
                )
                records = [Record.from_dict(item, "imported row") for item in payloads]
            known = {record.uuid for record in library.records}
            imported = 0
            renamed = 0
            uuid_map: dict[str, str] = {}
            for record in records:
                if record.uuid in known:
                    new_uuid = str(_uuid.uuid4())
                    uuid_map[record.uuid] = new_uuid
                    record.uuid = new_uuid
                    renamed += 1
                known.add(record.uuid)
                library.records.append(record)
                imported += 1
            files_imported = 0
            files_renamed = 0
            file_known = {record.uuid for record in library.file_records}
            for record in source.file_records:
                if record.uuid in file_known:
                    record.uuid = str(_uuid.uuid4())
                    files_renamed += 1
                file_known.add(record.uuid)
                if record.record_uuid:
                    record.record_uuid = uuid_map.get(record.record_uuid, record.record_uuid)
                library.file_records.append(record)
                files_imported += 1
            if imported or files_imported:
                self._persist(library, invalidate_scan=False)
            return {
                "imported": imported,
                "renamed": renamed,
                "skipped": skipped,
                "files": files_imported,
                "files_renamed": files_renamed,
            }

    def start_refine(self, token: Any, scope: Any = "low") -> dict[str, Any]:
        """启动 LLM 精析（后台线程）：``low`` 只处理疑似条目，``all`` 全部条目。"""
        if not isinstance(token, str) or not token:
            raise LibraryError("import token is required")
        if scope not in ("low", "all"):
            raise LibraryError(f"unknown refine scope: {scope!r} (expected 'low' or 'all')")
        with self._lock:
            source = self.imports.get(token)
            if source is None:
                raise LibraryError(f"unknown or expired import token: {token}")
            job = self.refines.get(token)
            if job is not None and job.get("status") == "running":
                raise LibraryError("refinement already running for this import")
            if scope == "all":
                indices = list(range(len(source.rows)))
            else:
                indices = suspicious_rows(source.rows, source.columns, source.suggested)
            if not indices:
                return {"started": False, "reason": "no-suspicious", "total": 0}
            self.refines[token] = {
                "status": "running",
                "done": 0,
                "total": len(indices),
                "refined": 0,
                "failed": 0,
                "error": "",
            }
            thread = threading.Thread(
                target=self._run_refine, args=(token, source, indices), daemon=True
            )
        thread.start()
        return {"started": True, "total": len(indices)}

    def refine_status(self, token: Any) -> dict[str, Any]:
        """精析进度；完成时附最新预览（列／样本／建议映射可能已刷新）。"""
        if not isinstance(token, str) or not token:
            raise LibraryError("import token is required")
        with self._lock:
            job = self.refines.get(token)
            if job is None:
                raise LibraryError(f"no refinement for import token: {token}")
            payload: dict[str, Any] = {
                "status": job["status"],
                "done": job["done"],
                "total": job["total"],
            }
            if job["status"] == "done":
                payload["refined"] = job["refined"]
                payload["failed"] = job["failed"]
                source = self.imports.get(token)
                if source is not None:
                    payload["preview"] = self._preview_payload(token, source)
            elif job["status"] == "failed":
                payload["error"] = job["error"]
            return payload

    def _run_refine(self, token: str, source: SourceData, indices: list[int]) -> None:
        """后台精析线程：任何异常都收成 job 的 failed（行保持原值）。"""
        job = self.refines.get(token)
        if job is None:
            return
        try:
            sender = self._chat_sender or self._build_sender()

            def progress(done: int) -> None:
                with self._lock:
                    job["done"] = done

            report = refine_source(source, indices, sender, on_progress=progress)
        except Exception as exc:  # 配置/网络错误：整任务失败，行保持原值
            with self._lock:
                job["status"] = "failed"
                job["error"] = str(exc)[:300] or type(exc).__name__
            return
        with self._lock:
            job["status"] = "done"
            job["done"] = len(indices)
            job["refined"] = report.refined
            job["failed"] = report.failed

    @staticmethod
    def _find_config_root(library_path: Optional[Path], cwd: Path) -> Path:
        """定位 ``<工程根>/_primer/config.yaml`` 的工程根。

        先沿**库文件所在目录链**找（标准布局：库在 ``<工程>/_primer/literature/`` 下），
        再沿**启动目录链**找；都没有就返回启动目录（由 :func:`primer.config.load_config`
        读内置与机器级层）。
        """
        candidates: list[Path] = []
        if library_path is not None:
            try:
                resolved = Path(library_path).resolve()
            except OSError:
                resolved = Path(library_path)
            candidates.extend(list(resolved.parents)[:8])
        cwd = Path(cwd)
        candidates.append(cwd)
        candidates.extend(list(cwd.parents)[:6])
        seen: set[Path] = set()
        for candidate in candidates:
            if candidate in seen:
                continue
            seen.add(candidate)
            if (candidate / "_primer" / "config.yaml").is_file():
                return candidate
        return Path(cwd)

    def _build_sender(self) -> Callable[[str, str], str]:
        """按库文件所在工程（或启动目录）的 ``_primer/config.yaml`` 建默认聊天发送器。

        角色按 ``literature`` → ``extract`` → ``select`` 取第一个已定义的；
        缺密钥等配置问题在这里抛出（错误信息点名环境变量名，不回显密钥）。
        """
        from ...config import ConfigError, load_config
        from ...llm import client_for_role, user_message

        root = self._find_config_root(
            self.library.path if self.library else None, Path.cwd()
        )
        config = load_config(root)
        for role in ("literature", "extract", "select"):
            if role in config.roles:
                break
        else:
            config_file = root / "_primer" / "config.yaml"
            raise ConfigError(
                "no LLM role for import refine: add `roles.literature` "
                f"(or `roles.extract` / `roles.select`) in {config_file} "
                "(the project is located near the library file or the working directory)"
            )
        client = client_for_role(config, role)

        def send(system: str, user: str) -> str:
            reply = client.complete(
                [{"role": "system", "content": system}, user_message(user)]
            )
            return reply.content

        return send

    def start_verify(self, token: Any, scope: Any = "all") -> dict[str, Any]:
        """启动联网比对（后台线程）：``low`` 只处理疑似条目，``all`` 全部条目。"""
        if not isinstance(token, str) or not token:
            raise LibraryError("import token is required")
        if scope not in ("low", "all"):
            raise LibraryError(f"unknown verify scope: {scope!r} (expected 'low' or 'all')")
        with self._lock:
            source = self.imports.get(token)
            if source is None:
                raise LibraryError(f"unknown or expired import token: {token}")
            job = self.refines.get(token)
            if job is not None and job.get("status") == "running":
                raise LibraryError("a background job is already running for this import")
            if scope == "all":
                indices = list(range(len(source.rows)))
            else:
                indices = suspicious_rows(source.rows, source.columns, source.suggested)
            if not indices:
                return {"started": False, "reason": "no-suspicious", "total": 0}
            self.refines[token] = {
                "status": "running",
                "kind": "verify",
                "done": 0,
                "total": len(indices),
                "refined": 0,
                "failed": 0,
                "error": "",
            }
            thread = threading.Thread(
                target=self._run_verify, args=(token, source, indices), daemon=True
            )
        thread.start()
        return {"started": True, "total": len(indices)}

    def _run_verify(self, token: str, source: SourceData, indices: list[int]) -> None:
        """后台联网比对线程：任何异常都收成 job 的 failed（行保持原值）。"""
        from ..verify import verify_source

        job = self.refines.get(token)
        if job is None:
            return
        try:
            def progress(done: int) -> None:
                with self._lock:
                    job["done"] = done

            report = verify_source(source, indices, self._build_engines(), on_progress=progress)
        except Exception as exc:  # 网络/配置错误：整任务失败，行保持原值
            with self._lock:
                job["status"] = "failed"
                job["error"] = str(exc)[:300] or type(exc).__name__
            return
        with self._lock:
            job["status"] = "done"
            job["done"] = len(indices)
            job["refined"] = report.matched
            job["failed"] = report.unmatched + report.failed

    def _build_engines(self) -> list[Callable[[str], list[dict[str, Any]]]]:
        """联网比对的引擎链（导入「联网比对」与记录补全共用）：OpenAlex → NASA ADS → Crossref。

        NASA ADS 需要 ``PRIMER_NASA_ADS_API_KEY``（.env 自动加载）；未配置时跳过。
        测试注入约定：``web_lookup`` 传单个假引擎或一组假引擎。
        """
        import os

        from ..engines import ADS_TOKEN_ENV, ads_lookup, crossref_lookup
        from ..verify import openalex_lookup

        injected = self._web_lookup
        if injected is None:
            engines = [openalex_lookup]
            if os.environ.get(ADS_TOKEN_ENV, "").strip():
                engines.append(ads_lookup)
            engines.append(crossref_lookup)
            return engines
        if callable(injected):
            return [injected]
        return list(injected)

    def _preview_payload(self, token: str, source: SourceData) -> dict[str, Any]:
        """解析／精析后的预览载荷（含疑似条目数）。"""
        return {
            "token": token,
            "kind": source.kind,
            "total": source.total,
            "columns": list(source.columns),
            "sample": source.sample,
            "suggested": source.suggested,
            "low_count": len(suspicious_rows(source.rows, source.columns, source.suggested)),
        }

    # ------------------------------------------------------------- 内部

    def _require(self) -> Library:
        if self.library is None:
            raise LibraryError("no library loaded; set one up first")
        return self.library

    def _persist(self, library: Library, *, invalidate_scan: bool = True) -> None:
        """落盘；失败（外部改动等）时回载磁盘版本，保证内存与磁盘一致。

        库内容变了，先前的扫描结果随之作废（``invalidate_scan=False`` 供"保存"
        这类不改内容的调用使用；失败回载时无论哪种都要作废）。
        """
        if invalidate_scan:
            self.scan = None
        try:
            library.save()
        except LibraryError:
            self.scan = None
            try:
                library.reload()
            except LibraryError as reload_exc:
                self.library = None
                self.load_error = str(reload_exc)
            raise


def _static_path(rel: str) -> Optional[Path]:
    """解析静态资源相对路径；越界（``..``、绝对路径）返回 ``None``。"""
    if not rel:
        return None
    rel_path = Path(rel)
    if rel_path.is_absolute() or ".." in rel_path.parts:
        return None
    candidate = (STATIC_DIR / rel_path).resolve()
    try:
        candidate.relative_to(STATIC_DIR.resolve())
    except ValueError:
        return None
    return candidate


def make_handler(service: LibraryService):
    """构造绑定到该 service 的请求处理器类（review 会话舱同款做法）。"""

    class Handler(BaseHTTPRequestHandler):
        server_version = "primer-literature-web/0.1"

        def do_GET(self) -> None:  # noqa: N802（http.server 约定命名）
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch("POST")

        def do_PUT(self) -> None:  # noqa: N802
            self._dispatch("PUT")

        def do_DELETE(self) -> None:  # noqa: N802
            self._dispatch("DELETE")

        # --------------------------------------------------------- 分发

        def _dispatch(self, method: str) -> None:
            try:
                path = urllib.parse.urlparse(self.path).path
                self._route(method, path)
            except _HttpError as exc:
                self._send_json(exc.status, {"error": exc.code, "message": exc.message})
            except LibraryError as exc:
                self._send_json(status_for(exc), {"error": "library_error", "message": str(exc)})
            except Exception as exc:  # 兜底：客户端只拿到错误名，traceback 进 stderr
                traceback.print_exc(file=sys.stderr)
                self._send_json(500, {"error": "internal", "message": type(exc).__name__})

        def _route(self, method: str, path: str) -> None:
            if method == "GET":
                if path == "/":
                    return self._send_static("index.html")
                if path.startswith("/static/"):
                    return self._send_static(urllib.parse.unquote(path[len("/static/"):]))
                if path == "/api/state":
                    return self._send_json(200, service.state_payload())
                if path == "/api/files":
                    return self._send_json(200, service.file_records_payload())
                raise _HttpError(404, "not_found", f"no route: GET {path}")

            if path.startswith("/api/records/") and method in ("PUT", "DELETE"):
                uuid_text = urllib.parse.unquote(path[len("/api/records/"):])
                if not uuid_text or "/" in uuid_text:
                    raise _HttpError(404, "not_found", f"no route: {method} {path}")
                if method == "PUT":
                    payload = self._read_json()
                    return self._send_json(200, {"record": service.update_record(uuid_text, payload)})
                if method == "DELETE":
                    return self._send_json(200, {"record": service.delete_record(uuid_text)})
                raise _HttpError(404, "not_found", f"no route: {method} {path}")

            if method == "POST":
                if path == "/api/import/parse":
                    length_header = self.headers.get("Content-Length") or "0"
                    try:
                        length = int(length_header)
                    except ValueError:
                        raise _HttpError(400, "invalid_request", "Content-Length must be an integer")
                    if length <= 0:
                        raise _HttpError(400, "invalid_request", "empty request body")
                    if length > MAX_UPLOAD_BYTES:
                        raise _HttpError(413, "request_too_large", "file too large")
                    data = self.rfile.read(length)
                    query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                    name = (query.get("name") or [""])[0]
                    return self._send_json(200, service.parse_import(name, data))
                payload = self._read_json()
                if path == "/api/library":
                    return self._send_json(
                        200, service.set_library(payload.get("path"), payload.get("mode"))
                    )
                if path == "/api/library/clear":
                    return self._send_json(200, service.clear_records())
                if path == "/api/save":
                    return self._send_json(200, service.save_library())
                if path == "/api/save-as":
                    return self._send_json(200, service.save_as(payload.get("path")))
                if path == "/api/import":
                    return self._send_json(200, service.import_records(payload.get("path")))
                if path == "/api/import/commit":
                    return self._send_json(
                        200, service.commit_import(payload.get("token"), payload.get("mapping"))
                    )
                if path == "/api/import/refine":
                    return self._send_json(
                        200, service.start_refine(payload.get("token"), payload.get("scope", "low"))
                    )
                if path == "/api/import/refine/status":
                    return self._send_json(
                        200, service.refine_status(payload.get("token"))
                    )
                if path == "/api/import/verify":
                    return self._send_json(
                        200, service.start_verify(payload.get("token"), payload.get("scope", "all"))
                    )
                if path == "/api/export":
                    return self._send_json(
                        200, service.export_records(payload.get("path"), payload.get("uuids"))
                    )
                if path == "/api/records":
                    return self._send_json(201, {"record": service.add_record(payload)})
                if path == "/api/records/enrich":
                    return self._send_json(
                        200, service.start_enrich(payload.get("uuids"), payload.get("mode"))
                    )
                if path == "/api/records/enrich/status":
                    return self._send_json(200, service.enrich_status(payload.get("token")))
                if path == "/api/records/enrich/preview":
                    return self._send_json(200, service.enrich_preview(payload.get("token")))
                if path == "/api/records/enrich/apply":
                    return self._send_json(200, service.apply_enrich(payload.get("token")))
                if path == "/api/links/batch/start":
                    return self._send_json(
                        200,
                        service.start_link_batch(
                            payload.get("scope", "unlinked"), payload.get("online", False)
                        ),
                    )
                if path == "/api/links/batch/status":
                    return self._send_json(200, service.link_batch_status(payload.get("token")))
                if path == "/api/links/batch/apply":
                    return self._send_json(200, service.apply_link_batch(payload.get("pairs")))
                if path == "/api/links/batch/export":
                    return self._send_json(
                        200,
                        service.export_link_batch(payload.get("kind"), payload.get("path")),
                    )
                if path == "/api/fetch/scan":
                    return self._send_json(
                        200,
                        service.start_fetch_scan(
                            payload.get("scope"),
                            payload.get("uuids"),
                            payload.get("target_dir"),
                            payload.get("online", True),
                        ),
                    )
                if path == "/api/fetch/status":
                    return self._send_json(200, service.fetch_status(payload.get("token")))
                if path == "/api/fetch/download":
                    return self._send_json(
                        200,
                        service.start_fetch_download(
                            payload.get("token"),
                            payload.get("uuids"),
                            payload.get("auto_parse", False),
                        ),
                    )
                if path == "/api/records/bulk-update":
                    return self._send_json(
                        200,
                        service.bulk_update_records(payload.get("uuids"), payload.get("fields")),
                    )
                if path == "/api/records/delete":
                    return self._send_json(200, service.bulk_delete_records(payload.get("uuids")))
                if path == "/api/projects/create":
                    return self._send_json(
                        200, service.create_project(payload.get("path"), payload.get("uuids"))
                    )
                if path == "/api/projects/attach":
                    return self._send_json(
                        200, service.attach_project(payload.get("path"), payload.get("uuids"))
                    )
                if path == "/api/projects/move":
                    return self._send_json(
                        200, service.move_project(payload.get("path"), payload.get("target"))
                    )
                if path == "/api/projects/rename":
                    return self._send_json(
                        200, service.rename_project(payload.get("path"), payload.get("name"))
                    )
                if path == "/api/projects/delete":
                    return self._send_json(200, service.delete_project(payload.get("path")))
                if path == "/api/scan":
                    return self._send_json(200, {"scan": service.scan_files()})
                if path == "/api/open":
                    return self._send_json(
                        200, service.open_file(payload.get("uuid"), payload.get("index"))
                    )
                if path == "/api/files/pick":
                    return self._send_json(200, service.pick_paths(payload.get("kind")))
                if path == "/api/files/scan":
                    return self._send_json(200, service.scan_files_folder(payload.get("path")))
                if path == "/api/files/add":
                    return self._send_json(200, service.add_files(payload.get("paths")))
                if path == "/api/files/parse":
                    return self._send_json(200, service.parse_files(payload.get("uuids")))
                if path == "/api/files/open":
                    return self._send_json(
                        200,
                        service.open_file_record(payload.get("uuid"), payload.get("which", "source")),
                    )
                if path == "/api/files/delete":
                    return self._send_json(200, service.delete_file_record(payload.get("uuid")))
                if path == "/api/links/attach":
                    return self._send_json(
                        200,
                        service.attach_files_to_record(
                            payload.get("record_uuid"),
                            payload.get("file_uuids"),
                            payload.get("nature", "doi-consistent"),
                        ),
                    )
                if path == "/api/links/detach":
                    return self._send_json(200, service.detach_files(payload.get("file_uuids")))
                raise _HttpError(404, "not_found", f"no route: POST {path}")

            raise _HttpError(404, "not_found", f"no route: {method} {path}")

        # --------------------------------------------------------- 请求体

        def _read_json(self) -> dict:
            length_header = self.headers.get("Content-Length") or "0"
            try:
                length = int(length_header)
            except ValueError:
                raise _HttpError(400, "invalid_request", "Content-Length must be an integer")
            if length <= 0:
                return {}
            if length > MAX_BODY_BYTES:
                raise _HttpError(413, "request_too_large", "request body too large")
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise _HttpError(400, "invalid_json", f"invalid JSON body: {exc}")
            if not isinstance(payload, dict):
                raise _HttpError(400, "invalid_json", "request body must be a JSON object")
            return payload

        # --------------------------------------------------------- 响应

        def _send_json(self, status: int, payload: Any) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _send_static(self, rel: str) -> None:
            target = _static_path(rel)
            if target is None or not target.is_file():
                return self._send_json(
                    404, {"error": "not_found", "message": f"no such file: {rel}"}
                )
            data = target.read_bytes()
            content_type = CONTENT_TYPES.get(target.suffix.lower(), "application/octet-stream")
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

    return Handler


def create_server(service: LibraryService, port: int = DEFAULT_PORT, host: str = HOST):
    """绑定端口并返回 ``(httpd, actual_port)``；端口被占用时回退随机空闲端口。"""
    handler = make_handler(service)
    try:
        httpd = ThreadingHTTPServer((host, port), handler)
        return httpd, httpd.server_address[1]
    except OSError:
        httpd = ThreadingHTTPServer((host, 0), handler)
        return httpd, httpd.server_address[1]


def serve(db: Optional[str] = None, port: int = DEFAULT_PORT, open_browser: bool = True) -> int:
    """命令行入口：启动服务并阻塞，直到 Ctrl-C。返回进程退出码。"""
    service = LibraryService.initial(db)
    httpd, actual_port = create_server(service, port)
    url = f"http://{HOST}:{actual_port}/"
    count = service.record_count()
    lines = [
        "文献库服务已启动",
        f"  库文件: {service.display_path()}",
        f"  记录数: {count if count is not None else '—（等待首启页新建/指定）'}",
        f"  地址: {url}",
    ]
    if actual_port != port:
        lines.append(f"  端口: {port} 被占用，已回退到随机端口 {actual_port}")
    print("\n".join(lines), flush=True)
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:  # 无图形环境时静默
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        sys.stderr.write("[web] interrupted\n")
    finally:
        httpd.server_close()
    return 0
