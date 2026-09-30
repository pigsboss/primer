# -*- coding: utf-8 -*-
"""场景指纹：这份场景是从哪一版规格、哪一批贴图上派生的。

规格改了、贴图换了，``mission_layout.blend`` 与那些预览 PNG 就都过期了，而文件名一个
字节都不会变——``renders/previews/mission_layout_preview_854x480.png`` 昨天和今天同名。
所以派生时把依赖的状态记下来：写进 ``build/fingerprint.json``，也写进场景的 Blender
自定义属性（``scene["primer_scene_fingerprint"]``），让 ``.blend`` 自己说得出它是从哪
一版派生的。

记的是**内容摘要**（sha256）而不是 mtime。mtime 会因为一次 ``touch``、一次重新下载而改变，
内容却一模一样；**内容**才是判据。短哈希取前 12 位十六进制：够区分两版，又不至于把 YAML
和自定义属性撑得难读。

差异分两级：规格内容变了 → ``error``（场景要重画）；某个贴图内容变了 → ``warning``
（几何还对，但贴图换了，``.blend`` 里的像不再是这一版）。总摘要 ``digest`` 一个值就够
判断"是不是同一版"，逐项差异只用来告诉人**变了什么**。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import List, Mapping, Optional, Sequence, Tuple

from ..paths import relative_to_root
from .assets import texture_path
from .spec import SceneSpec

__all__ = [
    "ERROR",
    "FINGERPRINT_KEY",
    "SHORT_HASH_CHARS",
    "WARNING",
    "AssetFingerprint",
    "Fingerprint",
    "FingerprintDifference",
    "compute_fingerprint",
    "differences",
    "fingerprint_from_mapping",
]

# 自定义属性名与产物 JSON 里的键名统一用这一个。
FINGERPRINT_KEY = "fingerprint"
SHORT_HASH_CHARS = 12
ERROR = "error"
WARNING = "warning"


@dataclass(frozen=True)
class AssetFingerprint:
    """一张贴图的内容摘要；``sha256`` 为空表示文件当时不在磁盘上。"""

    texture: str
    path: str
    sha256: str


@dataclass(frozen=True)
class Fingerprint:
    """一次派生所依赖的全部内容摘要。"""

    spec: str
    spec_sha256: str
    assets: Tuple[AssetFingerprint, ...]
    digest: str

    def as_mapping(self) -> Mapping[str, object]:
        """写进 JSON／自定义属性／报告的形式（键全英文，值全是标量）。"""
        return {
            "spec": self.spec,
            "spec_sha256": self.spec_sha256,
            "digest": self.digest,
            "assets": [
                {"texture": item.texture, "path": item.path, "sha256": item.sha256}
                for item in self.assets
            ],
        }


@dataclass(frozen=True)
class FingerprintDifference:
    """一处差异：``code`` 机器可读，``severity`` 决定挡不挡，``message`` 中文人读。"""

    code: str
    severity: str
    message: str


def _short_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:SHORT_HASH_CHARS]


def _file_digest(path: Optional[Path]) -> str:
    if path is None or not Path(path).is_file():
        return ""
    try:
        return _short_sha256(Path(path).read_bytes())
    except OSError:
        return ""


def _digest_of(spec_sha256: str, assets: Sequence[AssetFingerprint]) -> str:
    """总体摘要：对"规格摘要 + 逐张贴图摘要"的规范 JSON 再取一次哈希。

    ``sort_keys`` 与固定次序让同一批输入必然得到同一个 digest——指纹若自身不稳定，
    拿它判断"是不是同一版"就没有意义。
    """
    payload = {
        "spec_sha256": spec_sha256,
        "assets": [{"texture": item.texture, "sha256": item.sha256} for item in assets],
    }
    canonical = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return _short_sha256(canonical.encode("utf-8"))


def compute_fingerprint(spec: SceneSpec, project_root: Path) -> Fingerprint:
    """按当前磁盘状态算一次指纹：规格文件 + 规格声明的每一张贴图。"""
    project_root = Path(project_root)
    assets = tuple(
        AssetFingerprint(
            texture=entry.texture,
            path=relative_to_root(texture_path(project_root, entry.texture), project_root),
            sha256=_file_digest(texture_path(project_root, entry.texture)),
        )
        for entry in spec.assets.files
    )
    spec_sha256 = _file_digest(spec.path)
    return Fingerprint(
        spec=relative_to_root(spec.path, project_root),
        spec_sha256=spec_sha256,
        assets=assets,
        digest=_digest_of(spec_sha256, assets),
    )


def fingerprint_from_mapping(document: Mapping[str, object]) -> Fingerprint:
    """从 JSON 读回指纹（``check`` 重新读产物时用）。"""
    block = document.get(FINGERPRINT_KEY)
    block = block if isinstance(block, Mapping) else document
    assets: List[AssetFingerprint] = []
    for item in block.get("assets") or []:
        if isinstance(item, Mapping):
            assets.append(
                AssetFingerprint(
                    texture=str(item.get("texture", "")),
                    path=str(item.get("path", "")),
                    sha256=str(item.get("sha256", "")),
                )
            )
    return Fingerprint(
        spec=str(block.get("spec", "")),
        spec_sha256=str(block.get("spec_sha256", "")),
        assets=tuple(assets),
        digest=str(block.get("digest", "")),
    )


def differences(stored: Optional[Fingerprint], current: Fingerprint) -> List[FingerprintDifference]:
    """比对两份指纹，返回差异（中文人读句）。没有差异就是空表。"""
    if stored is None or not stored.digest:
        return [
            FingerprintDifference(
                "fingerprint",
                ERROR,
                "场景没有出生指纹，无从判断它是从哪一版规格派生；请重新运行 build 生成",
            )
        ]

    found: List[FingerprintDifference] = []
    if stored.spec_sha256 != current.spec_sha256:
        found.append(
            FingerprintDifference(
                "spec_sha256",
                ERROR,
                f"规格内容已变（spec_sha256 {stored.spec_sha256 or '(无)'} → "
                f"{current.spec_sha256 or '(无)'}）：场景几何与构图都可能变，必须重新运行 build",
            )
        )

    before = {item.texture: item.sha256 for item in stored.assets}
    after = {item.texture: item.sha256 for item in current.assets}
    for texture in sorted(set(before) | set(after)):
        if texture not in after:
            found.append(
                FingerprintDifference(
                    "asset_sha256",
                    WARNING,
                    f"贴图已不再被规格声明：{texture}；地面几何不变，但请核对署名账本",
                )
            )
        elif texture not in before:
            found.append(
                FingerprintDifference(
                    "asset_sha256",
                    WARNING,
                    f"贴图是新的：{texture}；场景几何不变，但需要重新运行 build 才贴上",
                )
            )
        elif before[texture] != after[texture]:
            found.append(
                FingerprintDifference(
                    "asset_sha256",
                    WARNING,
                    f"贴图内容已变：{texture}（sha256 {before[texture] or '(无)'} → "
                    f"{after[texture] or '(无)'}）：几何不变，但像已换，建议重新运行 build",
                )
            )
    return found
