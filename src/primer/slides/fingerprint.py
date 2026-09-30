# -*- coding: utf-8 -*-
"""``outline`` 的出生指纹：这一份骨架是从哪一版**规格与源文件**上派生的。

骨架里的章、节、候选句都来自 ``deck.yaml`` 与它列的那几个源 markdown。源文件改了（改一句话、
加一节、重排标题），骨架里已经圈好的 picks 就可能指着另一句话，或者干脆指不到——所以生成时
把派生所依赖的文件状态记进 ``outline.yaml`` 的 ``fingerprint`` 块，``check`` 再读一遍当前状态
逐项比对，把"变了什么、该怎么办"报到人面前；``build`` 用同一份比对拒绝在过期的骨架上生成。

记的是**文件内容的摘要**（sha256 短哈希），不是 mtime：mtime 会因为一次 ``touch`` 而改变，
内容却一模一样，那是假警报，而**内容**才是判据。

三样东西，按差异实际作废了什么分两级：

* ``spec_sha256``：``deck.yaml`` 的内容——篇序、层级、指针口径、章号方案都出自它。变了就没法
  保证骨架还对着同一份规格，记 ``error``，挡住生成；
* ``structure_sha256``：复算出来的章与节**身份**（章号／章标签／章标题／卷键／源文件，以及
  每个节的层级、节号、标题）的摘要。节号变了，picks 的归属与候选 id 都可能变，记 ``error``；
* ``sources[].sha256``：每个源文件各自的内容——只是候选池可能变了、章与节的身份没变，记
  ``warning``，提醒人重跑 ``--merge`` 并重读 picks，但不挡。

路径一律记成相对工程根（见 :mod:`primer.paths`），产物搬移或复制到别的机器之后仍可读。
摘要不含时间戳，重生成才逐字节相同。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import List, Mapping, Optional, Sequence, Tuple

from ..paths import relative_to_root
from .plan import SlidesError
from .spec import DeckSpec, DeckSpecError, load_spec
from .structure import DeckStructure, read_structure

FINGERPRINT_KEY = "fingerprint"
# 短哈希的十进制位数：够区分两版材料，又不至于把 YAML 撑得难读。
SHORT_HASH_CHARS = 12
# 差异的两级严重度：身份动了是 error（挡生成），只是候选池动了是 warning（不挡）。
ERROR = "error"
WARNING = "warning"


class FingerprintError(SlidesError):
    """指纹算不出来——交给命令行按 slides 错误统一处理。"""


@dataclass(frozen=True)
class FingerprintDifference:
    """出生指纹的一处差异。

    ``code`` 是机器可读的字段名（英文，如 ``structure_sha256``、``source_sha256``），
    ``severity`` 是它对生成的影响——章与节的身份动了是 ``error``（挡住生成），只是候选池
    动了是 ``warning``（人该重读 picks，但不挡）；``message`` 是中文人读句。
    """

    code: str
    severity: str
    message: str


def short_sha256(path: Optional[Path]) -> Optional[str]:
    """文件内容的短 sha256；读不到返回 ``None``。"""
    if path is None:
        return None
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return hashlib.sha256(data).hexdigest()[:SHORT_HASH_CHARS]


def structure_digest(structure: DeckStructure) -> str:
    """章与节**身份**的摘要：编号、标签、标题、卷键、源文件，加每个节的层级与节号。

    不含正文、不含候选、不含页码——所以"改了一句话"不会让它变，而"加了一节""章号变了"会。
    这正是"picks 的归属还成不成立"的判据。
    """
    lines: List[str] = []
    for chapter in structure.chapters:
        lines.append(
            "\t".join(
                (
                    "chapter",
                    chapter.number,
                    chapter.label,
                    chapter.title,
                    chapter.volume_id,
                    chapter.source,
                )
            )
        )
        for section in chapter.sections:
            lines.append(
                "\t".join(("section", section.level, section.number, section.title))
            )
    payload = "\n".join(lines) + "\n"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:SHORT_HASH_CHARS]


def source_paths(spec: DeckSpec, structure: DeckStructure) -> Tuple[str, ...]:
    """参与派生的源文件：按章的出现次序去重，路径相对工程根。"""
    ordered: List[str] = []
    for chapter in structure.chapters:
        if chapter.source not in ordered:
            ordered.append(chapter.source)
    return tuple(ordered)


def _source_entries(
    project_root: Path, relative_paths: Sequence[str]
) -> List[Mapping[str, object]]:
    return [
        {"path": relative_path, "sha256": short_sha256(project_root / relative_path)}
        for relative_path in relative_paths
    ]


def compute_fingerprint(
    spec: DeckSpec, structure: DeckStructure
) -> Mapping[str, object]:
    """生成时的指纹块，写进 ``outline.yaml``。"""
    return {
        "spec": relative_to_root(spec.path, spec.root),
        "spec_sha256": short_sha256(spec.path),
        "structure_sha256": structure_digest(structure),
        "sources": _source_entries(spec.root, source_paths(spec, structure)),
    }


def current_fingerprint(
    outline: Mapping[str, object],
    project_root: Path,
    spec_path: Optional[Path] = None,
) -> Tuple[Mapping[str, object], List[FingerprintDifference], Optional[DeckStructure]]:
    """按当前磁盘状态重算指纹：返回（当前指纹，读盘过程中的问题，当前结构）。

    路径取自骨架自己：指纹块里的 ``spec`` 是相对工程根记的（``spec_path`` 给出时用它，那是
    命令行的 ``--spec``）。规格读不出来、或源文件改动到复算不出结构时，结构记 ``None`` 并把
    那件事报成 ``error``——比对时表现为"变了"，随差异一起报，不抛异常（``check`` 要把话说
    完整，而不是在第一步就崩）。
    """
    stored = outline.get(FINGERPRINT_KEY)
    recorded = stored.get("spec") if isinstance(stored, Mapping) else None
    spec_rel = (
        relative_to_root(spec_path, project_root)
        if spec_path is not None
        else recorded
    )
    problems: List[FingerprintDifference] = []
    current: dict = {"spec": spec_rel, "spec_sha256": None, "structure_sha256": None, "sources": []}
    if not isinstance(spec_rel, str) or not spec_rel:
        problems.append(
            FingerprintDifference(
                "spec",
                ERROR,
                "骨架没有记录派生它的 deck.yaml，无法判断章、节与候选是否还成立；"
                "请重新运行 outline --spec <deck.yaml> 生成骨架",
            )
        )
        return current, problems, None

    spec_file = project_root / spec_rel
    current["spec_sha256"] = short_sha256(spec_file)
    structure: Optional[DeckStructure] = None
    spec: Optional[DeckSpec] = None
    try:
        spec = load_spec(spec_file, project_root)
        structure = read_structure(spec)
    except (DeckSpecError, SlidesError) as exc:
        problems.append(
            FingerprintDifference(
                "structure_sha256",
                ERROR,
                f"按当前的 deck.yaml 与源文件复算不出结构：{exc}；"
                "请修好源文件或规格后重新运行 outline --merge",
            )
        )
    if structure is not None:
        current["structure_sha256"] = structure_digest(structure)
        stored_sources = [
            str(entry.get("path"))
            for entry in (stored.get("sources") if isinstance(stored, Mapping) else None) or []
            if isinstance(entry, Mapping) and entry.get("path")
        ]
        paths = source_paths(spec, structure) if spec is not None else ()
        known = list(dict.fromkeys([*stored_sources, *paths]))
        current["sources"] = _source_entries(project_root, known)
    return current, problems, structure


def fingerprint_differences(
    stored: Optional[Mapping[str, object]], current: Mapping[str, object]
) -> List[FingerprintDifference]:
    """比对两份指纹，返回差异（中文人读句）。没有差异就是空表。

    纯函数，只吃两份映射、不碰磁盘，所以"内容没变、只是被 touch 过"必然给出空表。
    ``stored`` 缺失表示这是一份旧骨架（还没有指纹块），此时给一条"整个都没有可比"的
    ``error``，而不是把每项都报成"从无到有"。
    """
    if not isinstance(stored, Mapping):
        return [
            FingerprintDifference(
                "fingerprint",
                ERROR,
                "骨架没有出生指纹，无从判断它从哪一版规格派生；"
                "请重新运行 outline --spec <deck.yaml> 生成骨架",
            )
        ]

    differences: List[FingerprintDifference] = []

    before = stored.get("spec_sha256")
    after = current.get("spec_sha256")
    if before != after:
        differences.append(
            FingerprintDifference(
                "spec_sha256",
                ERROR,
                f"deck.yaml 已变（spec_sha256 {before or '(无)'} → {after or '(无)'}）："
                "章与节是怎么数出来的、指针发不发都可能不同，"
                "请用 outline --spec <deck.yaml> --merge 重新生成骨架",
            )
        )

    before_structure = stored.get("structure_sha256")
    after_structure = current.get("structure_sha256")
    if before_structure != after_structure:
        differences.append(
            FingerprintDifference(
                "structure_sha256",
                ERROR,
                f"章与节的结构已变（structure_sha256 {before_structure or '(无)'} → "
                f"{after_structure or '(无)'}）：节号与候选 id 可能已经对不上，"
                "请用 outline --spec <deck.yaml> --merge 重新生成骨架，并重读各帧的 picks",
            )
        )

    for difference in _source_differences(stored.get("sources"), current.get("sources")):
        differences.append(difference)
    return differences


def _source_differences(before: object, after: object) -> List[FingerprintDifference]:
    before_sources = _sources_by_path(before)
    after_sources = _sources_by_path(after)
    differences: List[FingerprintDifference] = []
    for path in sorted(set(before_sources) | set(after_sources)):
        if path not in after_sources:
            differences.append(
                FingerprintDifference(
                    "source_sha256",
                    WARNING,
                    f"候选源文件已消失：{path}；候选池可能已变，但结构没变，"
                    "请核对 deck.yaml 后用 outline --merge 重生成",
                )
            )
        elif path not in before_sources:
            differences.append(
                FingerprintDifference(
                    "source_sha256",
                    WARNING,
                    f"出现了新的候选源文件：{path}；候选池可能已变，但结构没变，"
                    "请用 outline --merge 重生成",
                )
            )
        elif before_sources[path] != after_sources[path]:
            differences.append(
                FingerprintDifference(
                    "source_sha256",
                    WARNING,
                    f"候选源文件内容已变：{path}（sha256 "
                    f"{before_sources[path] or '(无)'} → {after_sources[path] or '(无)'}）："
                    "该章候选句可能已变，但节号仍然有效，请用 outline --merge 重生成并重读 picks",
                )
            )
    return differences


def _sources_by_path(entries: object) -> Mapping[str, Optional[str]]:
    result: dict = {}
    for entry in entries or []:
        if isinstance(entry, Mapping) and entry.get("path"):
            digest = entry.get("sha256")
            result[str(entry["path"])] = digest if isinstance(digest, str) else None
    return result
