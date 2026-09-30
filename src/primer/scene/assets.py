# -*- coding: utf-8 -*-
"""贴图资产管理：声明、下载、校验与出处账本。

贴图是唯一需要联网的东西，所以这一段的规矩单独写清楚：

* **只用标准库**。本项目的运行期依赖只有 PyYAML 一个，为几个 JPEG 引入 requests/httpx
  不划算；:func:`urllib.request.urlopen` 够用，出错的形态也认识（HTTPError / URLError）。
* **账本是唯一出处记录**。``_primer/scene/assets/credits.yaml`` 逐文件记下文件名、URL、
  sha256、字节数、许可与署名行、下载日期。CC BY 4.0 要求署名，署名行写在这里、也写进
  规格（``assets.credit``），两处一致才算合规。
* **重复运行是幂等的**。文件已在且 sha256 与账本相符就跳过，**连下载日期都沿用账本里的
  那一个**——否则每次 fetch 都把账本改一遍，"有没有变"就看不出来了。
* **落盘文件名用规格里的名字**。规格是契约，发射器只认 ``textures/<规格里的文件名>``；
  实际取到的是哪个上游文件由账本的 ``source_file`` / ``url`` 说明。规格里的几个名字在上游
  并不都存在（``4k_jupiter.jpg`` 就没有，只有 2k 与 8k），所以 :func:`candidate_urls` 会
  按尺寸前缀依次试探，并把**真正用了哪个**如实记进账本，``check`` 另出一条
  ``asset_resolution_fallback`` 提示——换分辨率是事实，不是可以吞掉的细节。

下载顺序：规格里的原名优先，然后按 2k → 8k 试探。选 2k 打底是因为本图的半径都是"示意
比例"：海王星在地球 5.48 倍显示轨道半径上只占画面宽度的百分之几，2K 贴图配 4K 成片绰绰
有余，而 8K 贴图在 Cycles 里要吃掉几百 MB 显存。
"""

from __future__ import annotations

import hashlib
import http.client
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

import yaml

from ..paths import output_dir_for, relative_to_root
from .spec import AssetFile, SceneError, SceneSpec

__all__ = [
    "FEATURE",
    "LEDGER_NAME",
    "TEXTURE_DIRNAME",
    "AssetDecl",
    "AssetEntry",
    "FetchResult",
    "asset_url",
    "candidate_urls",
    "declared_assets",
    "fetch",
    "ledger_path",
    "orphan_textures",
    "prune_orphaned_textures",
    "read_ledger",
    "sha256_file",
    "texture_dir",
    "texture_path",
    "verify_ledger",
]

# 产物边界内的两处固定位置：贴图目录与账本文件。
TEXTURE_DIRNAME = "assets/textures"
LEDGER_NAME = "assets/credits.yaml"
# 本功能在 ``primer.paths.FEATURES`` 里的名字；产物根一律由它推出，不手拼 ``_primer``。
FEATURE = "scene"
# 上游按 ``<尺寸>_<名字>.jpg`` 命名；试探顺序里原名优先，其余按这里的偏好。
_SIZE_PREFERENCE = ("2k", "4k", "8k")
_SIZE_PATTERN = re.compile(r"^(?P<size>\d+k)_(?P<rest>.+)$")
USER_AGENT = "primer-scene/0.1 (+https://github.com/primer; texture fetch for a diagram build)"
DEFAULT_TIMEOUT = 300.0


@dataclass(frozen=True)
class AssetDecl:
    """一条贴图声明：规格给的用法与出处，加账本给出的"已知内容指纹"。"""

    texture: str
    url: str
    license: str
    credit: str
    used_by: str
    credit_extra: str = ""
    expected_sha256: Optional[str] = None
    expected_bytes: Optional[int] = None


@dataclass(frozen=True)
class AssetEntry:
    """账本里的一行：某个规格贴图真正是从哪来的、内容是什么。"""

    texture: str
    url: str
    source_file: str
    path: str
    sha256: str
    bytes: int
    license: str
    credit: str
    downloaded: str


@dataclass(frozen=True)
class FetchResult:
    """一次 fetch 的结果：下载了哪些、跳过了哪些、清掉了哪些孤儿、账本落在哪。"""

    downloaded: Tuple[str, ...]
    skipped: Tuple[str, ...]
    pruned: Tuple[str, ...]
    entries: Tuple[AssetEntry, ...]
    ledger: Path


def texture_dir(project_root: Path) -> Path:
    return output_dir_for(project_root, FEATURE) / TEXTURE_DIRNAME


def ledger_path(project_root: Path) -> Path:
    return output_dir_for(project_root, FEATURE) / LEDGER_NAME


def texture_path(project_root: Path, texture: str) -> Path:
    """规格里的贴图名 → 磁盘路径（落盘文件名就是规格里的名字）。"""
    return texture_dir(project_root) / texture


def asset_url(base_url: str, filename: str) -> str:
    return f"{base_url.rstrip('/')}/{filename}"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def candidate_urls(decl: AssetDecl, base_url: str) -> Tuple[str, ...]:
    """一条声明的候选 URL：原名优先，再按尺寸前缀换着试。

    换尺寸是必要的：上游只发布了部分 4k 文件，``4k_jupiter.jpg`` 这类名字会 404，而
    ``2k_jupiter.jpg`` 与 ``8k_jupiter.jpg`` 都在。试的顺序固定，所以同一份规格在任何
    机器上都会落到同一个文件。
    """
    urls = [asset_url(base_url, decl.texture)]
    match = _SIZE_PATTERN.match(decl.texture)
    if match:
        for size in _SIZE_PREFERENCE:
            if size == match.group("size"):
                continue
            urls.append(asset_url(base_url, f"{size}_{match.group('rest')}"))
    return tuple(urls)


def declared_assets(spec: SceneSpec, ledger: Optional[Mapping[str, AssetEntry]] = None) -> Tuple[AssetDecl, ...]:
    """规格 + 账本 → 声明表；账本里已有的指纹成为"预期指纹"。"""
    known = ledger or {}
    declarations: List[AssetDecl] = []
    for entry in spec.assets.files:
        recorded = known.get(entry.texture)
        declarations.append(
            AssetDecl(
                texture=entry.texture,
                url=recorded.url if recorded else asset_url(spec.assets.base_url, entry.texture),
                license=spec.assets.license,
                credit=_credit_line(spec, entry),
                used_by=entry.used_by,
                credit_extra=entry.credit_extra,
                expected_sha256=recorded.sha256 if recorded else None,
                expected_bytes=recorded.bytes if recorded else None,
            )
        )
    return tuple(declarations)


def _credit_line(spec: SceneSpec, entry: AssetFile) -> str:
    """署名行：规格的总署名，加这条贴图自己的额外出处（如银河全景另有作者）。"""
    if entry.credit_extra:
        return f"{spec.assets.credit}; {entry.credit_extra}"
    return spec.assets.credit


def read_ledger(path: Path) -> Dict[str, AssetEntry]:
    """读账本；文件不存在返回空表，格式不对则抛错（账本是产物，坏了必须叫）。"""
    return _entries(_read_document(path), Path(path))


def _read_document(path: Path) -> Mapping[str, object]:
    path = Path(path)
    if not path.is_file():
        return {}
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise SceneError(f"credits ledger is not valid YAML: {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise SceneError(f"credits ledger must be a mapping: {path}")
    return document


def _entries(document: Mapping[str, object], path: Path) -> Dict[str, AssetEntry]:
    entries: Dict[str, AssetEntry] = {}
    for index, item in enumerate(document.get("files") or []):
        where = f"{path}: files[{index}]"
        if not isinstance(item, dict) or not item.get("texture"):
            raise SceneError(f"{where} must be a mapping with a texture name")
        try:
            entries[str(item["texture"])] = AssetEntry(
                texture=str(item["texture"]),
                url=str(item.get("url", "")),
                source_file=str(item.get("source_file", item["texture"])),
                path=str(item.get("path", "")),
                sha256=str(item.get("sha256", "")),
                bytes=int(item.get("bytes", 0)),
                license=str(item.get("license", "")),
                credit=str(item.get("credit", "")),
                downloaded=str(item.get("downloaded", "")),
            )
        except (TypeError, ValueError) as exc:
            raise SceneError(f"{where} is malformed: {exc}") from exc
    return entries


def _ledger_document(
    spec: SceneSpec, entries: Sequence[AssetEntry], fetched: str
) -> Mapping[str, object]:
    return {
        "source": spec.assets.source,
        "license": spec.assets.license,
        "credit": spec.assets.credit,
        "base_url": spec.assets.base_url,
        "fetched": fetched,
        "note": (
            "filename is the name the spec declares; url and source_file say where the bytes "
            "actually came from — they differ only when a declared name is not published "
            "upstream and a size variant had to stand in"
        ),
        "files": [
            {
                "texture": entry.texture,
                "source_file": entry.source_file,
                "path": entry.path,
                "url": entry.url,
                "sha256": entry.sha256,
                "bytes": entry.bytes,
                "license": entry.license,
                "credit": entry.credit,
                "downloaded": entry.downloaded,
            }
            for entry in entries
        ],
    }


def _write_ledger(path: Path, document: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            document,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
            width=10**6,
        ),
        encoding="utf-8",
    )


def _http_get(url: str, timeout: float) -> bytes:
    """标准库下载。UA 必须给：上游对空 UA 直接回 404。"""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _download_one(
    decl: AssetDecl,
    base_url: str,
    destination: Path,
    opener,
    timeout: float,
) -> Tuple[str, bytes]:
    """按候选 URL 依次试探，返回（真正命中的 URL，字节）。

    每个 URL 试两次：一次读超时既可能真是这个文件取不到，也可能只是这半分钟网络不好，
    重试一次再换分辨率，免得一次抖动就把 4k 悄悄降成 2k。HTTP 4xx 不重试——那是"这个
    文件不存在"，重试没有意义。全军覆没时抛 :class:`SceneError`，**点名所有试过的 URL**。
    """
    attempted: List[str] = []
    reason = "no candidate URL answered"
    transport_error = ""
    for url in candidate_urls(decl, base_url):
        attempted.append(url)
        for _attempt in range(2):
            try:
                payload = opener(url, timeout)
            except urllib.error.HTTPError as exc:
                reason = f"HTTP {exc.code}"
                print(f"scene: {url} returned HTTP {exc.code}", file=sys.stderr)
                break
            except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
                text = str(getattr(exc, "reason", exc))
                # 传输层错误比"退到下一个分辨率后拿到的 404"更接近真正的原因，留它。
                transport_error = transport_error or f"{url}: {text}"
                reason = text
                print(f"scene: {url}: {text}", file=sys.stderr)
                continue
            if not payload:
                reason = "empty body"
                print(f"scene: {url} returned an empty body", file=sys.stderr)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_name(destination.name + ".part")
            temporary.write_bytes(payload)
            os.replace(temporary, destination)
            return url, payload
    raise SceneError(
        f"failed to download {decl.texture}: {transport_error or reason} "
        f"(tried: {', '.join(attempted)})"
    )


def fetch(
    spec: SceneSpec,
    project_root: Path,
    force: bool = False,
    opener=None,
    today: Optional[str] = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> FetchResult:
    """把规格声明的贴图备齐：缺的下载、有的核对、账本重写。

    ``opener`` 与 ``today`` 是给测试用的注入点（测试不联网、也不该关心今天是几号）。
    """
    project_root = Path(project_root)
    download = opener or _http_get
    stamp = today or date.today().isoformat()
    ledger_file = ledger_path(project_root)
    previous = _read_document(ledger_file)
    known = _entries(previous, ledger_file)

    downloaded: List[str] = []
    skipped: List[str] = []
    entries: List[AssetEntry] = []

    for decl in declared_assets(spec, known):
        destination = texture_path(project_root, decl.texture)
        recorded = known.get(decl.texture)
        if not force and destination.is_file() and recorded is not None:
            digest = sha256_file(destination)
            if digest == recorded.sha256:
                skipped.append(decl.texture)
                entries.append(recorded)
                continue
            print(f"scene: {decl.texture} does not match the ledger, downloading again", file=sys.stderr)

        url, payload = _download_one(decl, spec.assets.base_url, destination, download, timeout)
        digest = hashlib.sha256(payload).hexdigest()
        downloaded.append(decl.texture)
        entries.append(
            AssetEntry(
                texture=decl.texture,
                url=url,
                source_file=url.rsplit("/", 1)[-1],
                path=relative_to_root(destination, project_root),
                sha256=digest,
                bytes=len(payload),
                license=decl.license,
                credit=decl.credit,
                downloaded=stamp,
            )
        )

    # 一个字都没下载时，连顶层的 ``fetched`` 也沿用旧值：否则"再跑一遍"会把账本改一遍，
    # 幂等就只对贴图成立、对账本不成立了。
    fetched = stamp if downloaded or not previous else str(previous.get("fetched") or stamp)
    _write_ledger(ledger_file, _ledger_document(spec, entries, fetched))
    pruned = prune_orphaned_textures(spec, project_root)
    return FetchResult(
        downloaded=tuple(downloaded),
        skipped=tuple(skipped),
        pruned=pruned,
        entries=tuple(entries),
        ledger=ledger_file,
    )


def orphan_textures(spec: SceneSpec, project_root: Path) -> Tuple[Path, ...]:
    """贴图目录里**规格没有声明**的文件，按文件名排序。

    贴图目录（``_primer/scene/assets/textures/``）整个归 primer 管：里面除了规格声明的
    那几张，不该有别的东西。规格改一次文件名（比如上游把 4k 换成了 8k），旧文件就留成
    孤儿——它不影响这次渲染，但会一直占着磁盘、让"目录 == 规格"这句话不成立，下一次看图
    的人会以为它是被用到的。所以孤儿要被认出来并删掉，而不是留着。

    只认**文件**，且只在这个目录的顶层；账本（``assets/credits.yaml``）与别的子目录不在
    此列。隐藏文件（``.DS_Store`` 之类，macOS 会自己放进来）不算孤儿：它们不是下载的产物，
    删了还会再出现，报出来只是噪声。
    """
    declared = {texture_path(project_root, texture).name for texture in spec.textures}
    directory = texture_dir(project_root)
    if not directory.is_dir():
        return ()
    return tuple(
        sorted(
            (
                path
                for path in directory.iterdir()
                if path.is_file() and not path.name.startswith(".") and path.name not in declared
            ),
            key=lambda path: path.name,
        )
    )


def prune_orphaned_textures(spec: SceneSpec, project_root: Path) -> Tuple[str, ...]:
    """删掉孤儿贴图，返回被删的文件名（按文件名排序）。

    只删 :func:`orphan_textures` 认出来的那些——它按名字判定，所以下载中断留下的
    ``*.part`` 半个文件同样落在里面，不必另写一套。删之前一条条写到 stderr：删东西是
    诊断行为，说清楚删了什么、为什么。
    """
    removed: List[str] = []
    for path in orphan_textures(spec, project_root):
        print(
            f"scene: removing orphaned texture {path.name}: the spec declares no such file "
            f"({path.stat().st_size} bytes)",
            file=sys.stderr,
        )
        path.unlink()
        removed.append(path.name)
    return tuple(removed)


def verify_ledger(spec: SceneSpec, project_root: Path) -> Tuple[str, ...]:
    """核对账本与磁盘：返回英文问题清单（空表表示齐备且指纹相符）。

    三件事一起查：账本里有这个贴图、文件真的在、sha256 与账本相符。少了任何一件，署名
    就没有着落，成图也就不该出——CC BY 的图少了署名行比少一张贴图严重。
    """
    project_root = Path(project_root)
    problems: List[str] = []
    known = read_ledger(ledger_path(project_root))
    for entry in spec.assets.files:
        recorded = known.get(entry.texture)
        if recorded is None:
            problems.append(f"{entry.texture} is missing from the credits ledger")
            continue
        path = texture_path(project_root, entry.texture)
        if not path.is_file():
            problems.append(f"{entry.texture} is recorded in the ledger but missing on disk")
            continue
        digest = sha256_file(path)
        if digest != recorded.sha256:
            problems.append(
                f"{entry.texture} sha256 does not match the ledger "
                f"({digest[:12]} != {recorded.sha256[:12]})"
            )
        if not recorded.credit.strip():
            problems.append(f"{entry.texture} has no credit line in the ledger")
    return tuple(problems)
