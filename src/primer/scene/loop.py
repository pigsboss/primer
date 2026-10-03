# -*- coding: utf-8 -*-
"""``python3 -m primer.scene.loop`` —— 会话舱的 LLM 环路驱动。

把"机器重建 → 人目检 → 机器重建"的决策环路收进 primer：人在浏览器会话舱里发消息、
看渲染图、点选问题卡；本进程盯着同一个会话目录，把新消息连同**磁盘上的全部上下文**
（loop.yaml、参数库、会话台账、近 40 条聊天记录、可选渲染图）交给所配的 LLM，解析
它的回信，执行白名单内的动作，再把结果与回信写回会话。界面只是视图，状态都在磁盘上，
所以模型随时可换（GUI 的下拉框里选 role，下一轮就用新模型）。

协议（会话目录里的文件）::

    <session>/
      chat.jsonl       消息流：人写 {ts, role:"user", text, model}；驱动写 {role:"agent", ...}
      chat_meta.json   驱动写：{heartbeat_ts, models:[{id,label}], default_role, vision}（GUI 读）
      loop_state.json  驱动写：游标 {processed_user_messages, ...}（断点续跑）
      loop_log.jsonl   驱动写：全动作 append-only 日志（编辑 diff、命令输出末尾、备份名）
      loop.lock        驱动持有：心跳 <30s 的锁文件，防两个驱动同抢一个会话
      ESCALATION.md    升级标记：需要改 primer 代码时写入，回给 kimi code 回路
      _backup/         会话 JSON 写前备份（每份保留最近 10 个）
      canvas.json / cards.json / record.json   会话状态（session_update 按 id 合并）

动作块（驱动从回信正文中识别；正文其余部分就是给人读的回信）::

    ```actions
    actions:
      - kind: edit_params      # 只改 loop.yaml editable 白名单里的文件；before 必须唯一命中
        file: 参数库_2034.yaml
        edits:
          - before: "length_m: 4.42"
            after:  "length_m: 4.8"
            note: "裁决 q8=A"
      - kind: run              # 只跑 loop.yaml commands 里的名字；cwd＝任务根
        name: build
      - kind: session_update   # 按 id 合并进 canvas.json / cards.json / record.json
        record_rows: [...]
        canvas_items: [...]
        cards: [...]
      - kind: escalate         # 只有"需要改 primer 代码"才用；其余全在 primer 内闭环
        reason: "需要修改 primer.scene 的 X"
    ```

**升级边界**：只有"需要改 primer 代码"才置 escalate 标记（写 ``ESCALATION.md`` ＋ 台账
加一行醒目行）；参数修改、跑构建、渲染、更新会话、出问题卡全部在 primer 内闭环。

语言纪律（docs/guides/CODING_STANDARDS.md v1.1）：stdout 中文人读简报；stderr／异常
消息英文；JSON 键英文。密钥只从环境变量读（``primer.config.api_key``），报错只点名
变量名，绝不回显密钥值。
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
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import yaml

from .. import llm as llm_mod
from ..config import CONFIG_FILENAME, ConfigError, load_config, resolve_role
from ..llm import ChatClient, LlmError, LlmHttpError, Transport, client_for_role
from ..paths import OUTPUT_DIRNAME

__all__ = [
    "CHAT_FILENAME",
    "LOCK_FILENAME",
    "LoopConfig",
    "LoopError",
    "Driver",
    "acquire_lock",
    "build_models",
    "collect_images",
    "load_loop_config",
    "main",
    "merge_by_id",
    "parse_actions",
    "release_lock",
    "touch_lock",
]

CHAT_FILENAME = "chat.jsonl"
META_FILENAME = "chat_meta.json"
STATE_FILENAME = "loop_state.json"
LOG_FILENAME = "loop_log.jsonl"
LOCK_FILENAME = "loop.lock"
ESCALATION_FILENAME = "ESCALATION.md"
BACKUP_DIRNAME = "_backup"

BACKUP_KEEP = 10
CHAT_CONTEXT_MESSAGES = 40
CHAT_TEXT_LIMIT = 1500
STDIO_TAIL_LINES = 120
LOCK_STALE_SECONDS = 30.0
HEARTBEAT_STALE_SECONDS = 60.0
DEFAULT_COMMAND_TIMEOUT = 1800.0
DEFAULT_LOOP_FILENAME = "loop.yaml"
DEFAULT_IMAGES_MAX = 2

# 回信中动作块的围栏标签：明写 actions 的必须收；yaml/json 只有内容确实像动作块才收，
# 免得把参数库片段之类的普通代码块当动作执行。
_ACTION_FENCE_LABELS = {"actions", "action", "yaml", "yml", "json", ""}
_STRICT_FENCE_LABELS = {"actions", "action"}
_FENCE_RE = re.compile(r"```[ \t]*([A-Za-z0-9_+\-]*)[ \t]*\r?\n(.*?)```", re.S)
# 模型时不时把 YAML 语法键打成全角；先按原样解析，失败才归一化重试（不碰值的原文）。
# 全角冒号按中文习惯是紧贴值的（``kind：run``），所以补一个空格才成 YAML 的映射分隔符。
_FULLWIDTH_TABLE = str.maketrans(
    {
        "：": ": ",
        "，": ", ",
        "“": '"',
        "”": '"',
        "‘": "'",
        "’": "'",
        "（": "(",
        "）": ")",
        "【": "[",
        "】": "]",
        "－": "-",
        "＝": "=",
        "　": " ",
    }
)

_SYSTEM_PROMPT = """你是 primer 会话舱环路里的建模／评审代理，人与你用中文对话。

纪律：
1. 每一条改动都要说明理由：依据来自参数库、会话台账、聊天记录或渲染图。
2. 参数修改只能走动作块；正文给人看，动作块给程序执行。不要把新值只写在正文里。
3. 不确定的事写成问题卡（session_update 的 cards）问人，不要自己拍板。
4. 需要修改 primer 源码（src/primer/**）才能推进时，用 escalate 动作块交给 kimi code
   回路；你不得自行修改 primer 源码，也不要声称已经改过。
5. 与用户对话用中文；参数名、文件名、命令名、状态码保持原文。
6. **看清再答**：回答前先声明本轮你实际看到哪些图（按文件名，见"随信图片"清单）。
   若用户的问题指向某张图或某个问题卡，而那张图不在随信图片里，直接说"这张图我看不到"，
   不得用其他图片替代回答，也不要凭想象描述它。

动作块：放在正文之后，一个 ```actions 围栏，内容是 YAML（JSON 也认）。没有动作就整个省略。
```actions
actions:
  - kind: edit_params
    file: <loop.yaml 的 editable 白名单里的文件>
    edits:
      - before: "<文件里精确出现一次的原文子串>"
        after: "<替换成什么>"
        note: "<为什么这么改>"
  - kind: run
    name: <loop.yaml commands 里的名字>
  - kind: session_update
    record_rows: [{id, subject, value, status, evidence, highlight}]
    canvas_items: [{id, image, w, h, overlays: []}]
    cards: [{id, title, text, options: [], attach: {}}]
  - kind: escalate
    reason: "<需要改 primer 的哪一处、为什么>"
```

本任务可用的东西：
- 参数库：<<PARAMS_FILE>>（edit_params 只能改 editable 白名单里的文件）
- 可执行命令：<<COMMANDS>>
- 渲染图：<<IMAGES>>
"""


class LoopError(Exception):
    """环路配置、会话协议或锁的故障。消息英文（见 CODING_STANDARDS v1.1 §2.2）。"""


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------- loop.yaml


@dataclass(frozen=True)
class LoopConfig:
    """任务侧的环路声明（``loop.yaml``）：参数库、白名单、命令、图片与补充提示。"""

    path: Path
    task: str
    params_file: str
    editable: Tuple[str, ...]
    commands: Mapping[str, Tuple[str, ...]]
    images_glob: str = ""
    images_max: int = DEFAULT_IMAGES_MAX
    system_extra: str = ""
    command_timeout: float = DEFAULT_COMMAND_TIMEOUT
    models: Tuple[Mapping[str, str], ...] = ()


def load_loop_config(path: Any) -> LoopConfig:
    """读并校验 ``loop.yaml``；缺字段报错，字段类型不对也报错（消息英文）。"""
    source = Path(path).expanduser()
    if not source.is_file():
        raise LoopError(f"loop file {source} does not exist")
    try:
        raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise LoopError(f"loop file {source} is not valid YAML: {exc}") from exc
    except OSError as exc:
        raise LoopError(f"loop file {source} cannot be read: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise LoopError(f"loop file {source} must be a mapping at the top level")

    params_file = raw.get("params_file")
    if not isinstance(params_file, str) or not params_file.strip():
        raise LoopError(f"loop file {source}: `params_file` must be a non-empty string")

    editable_raw = raw.get("editable")
    if not isinstance(editable_raw, list) or not all(isinstance(x, str) for x in editable_raw):
        raise LoopError(f"loop file {source}: `editable` must be a list of strings")
    editable = tuple(x.strip() for x in editable_raw if x.strip())
    if not editable:
        raise LoopError(f"loop file {source}: `editable` must name at least one file")

    commands_raw = raw.get("commands")
    if not isinstance(commands_raw, Mapping) or not commands_raw:
        raise LoopError(f"loop file {source}: `commands` must be a non-empty mapping")
    commands: Dict[str, Tuple[str, ...]] = {}
    for name, argv in commands_raw.items():
        if not isinstance(name, str) or not name.strip():
            raise LoopError(f"loop file {source}: command names must be non-empty strings")
        if not isinstance(argv, (list, tuple)) or not argv or not all(
            isinstance(x, str) and x for x in argv
        ):
            raise LoopError(
                f"loop file {source}: `commands.{name}` must be a non-empty list of strings"
            )
        commands[name.strip()] = tuple(argv)

    images = raw.get("images")
    images_glob = ""
    images_max = DEFAULT_IMAGES_MAX
    if images is not None:
        if not isinstance(images, Mapping):
            raise LoopError(f"loop file {source}: `images` must be a mapping")
        glob = images.get("glob", "")
        if not isinstance(glob, str):
            raise LoopError(f"loop file {source}: `images.glob` must be a string")
        images_glob = glob.strip()
        limit = images.get("max", DEFAULT_IMAGES_MAX)
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise LoopError(f"loop file {source}: `images.max` must be an integer >= 0")
        images_max = int(limit)

    extra = raw.get("system_extra", "")
    if not isinstance(extra, str):
        raise LoopError(f"loop file {source}: `system_extra` must be a string")

    timeout = raw.get("command_timeout", DEFAULT_COMMAND_TIMEOUT)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise LoopError(f"loop file {source}: `command_timeout` must be a positive number")

    task = raw.get("task", "")
    if not isinstance(task, str):
        raise LoopError(f"loop file {source}: `task` must be a string")

    models_raw = raw.get("models")
    models: List[Dict[str, str]] = []
    if models_raw is not None:
        if not isinstance(models_raw, list):
            raise LoopError(f"loop file {source}: `models` must be a list")
        for entry in models_raw:
            if isinstance(entry, str) and entry.strip():
                models.append({"role": entry.strip(), "label": ""})
            elif isinstance(entry, Mapping):
                role = entry.get("role")
                if not isinstance(role, str) or not role.strip():
                    raise LoopError(
                        f"loop file {source}: `models[].role` must be a non-empty string"
                    )
                label = entry.get("label", "")
                if not isinstance(label, str):
                    raise LoopError(f"loop file {source}: `models[].label` must be a string")
                models.append({"role": role.strip(), "label": label.strip()})
            else:
                raise LoopError(
                    f"loop file {source}: `models` entries must be strings or mappings"
                )

    return LoopConfig(
        path=source.resolve(),
        task=task,
        params_file=params_file.strip(),
        editable=editable,
        commands=commands,
        images_glob=images_glob,
        images_max=images_max,
        system_extra=extra,
        command_timeout=float(timeout),
        models=tuple(models),
    )


# ---------------------------------------------------------------- 动作块解析


def parse_actions(text: str) -> Tuple[str, List[dict], List[str]]:
    """把回信拆成（给人读的正文，动作列表，解析说明）。

    认得 ``\u0060\u0060\u0060actions``／``\u0060\u0060\u0060action`` 围栏，也容忍写成 yaml／json 的；
    后两者只有内容确实像动作块（有 ``actions`` 或 ``kind``）才收。围栏内容先按原样解析，
    失败再按全角归一化重试一次。解析不出来的 actions 围栏**不执行**，但会在说明里记一笔。
    """
    actions: List[dict] = []
    notes: List[str] = []
    spans: List[Tuple[int, int]] = []
    for match in _FENCE_RE.finditer(text):
        label = (match.group(1) or "").strip().lower()
        if label not in _ACTION_FENCE_LABELS:
            continue
        block = _parse_action_block(match.group(2))
        if block is None:
            if label in _STRICT_FENCE_LABELS:
                notes.append("（动作块解析失败：内容不是合法的 YAML／JSON 动作表）")
                spans.append(match.span())
            continue
        actions.extend(block)
        spans.append(match.span())
    clean = _remove_spans(text, spans).strip()
    return clean, actions, notes


def _parse_action_block(raw: str) -> Optional[List[dict]]:
    for candidate in (raw, raw.translate(_FULLWIDTH_TABLE)):
        loaded = _load_yaml_or_json(candidate)
        if loaded is None:
            continue
        if _looks_like_actions(loaded):
            return _as_action_list(loaded)
    return None


def _load_yaml_or_json(text: str) -> Any:
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError:
        pass
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


def _looks_like_actions(obj: Any) -> bool:
    if isinstance(obj, Mapping):
        if "actions" in obj:
            return isinstance(obj["actions"], list)
        return "kind" in obj
    if isinstance(obj, list) and obj:
        return all(isinstance(item, Mapping) and "kind" in item for item in obj)
    return False


def _as_action_list(obj: Any) -> List[dict]:
    if isinstance(obj, Mapping):
        if "actions" in obj:
            return [dict(item) for item in obj["actions"] if isinstance(item, Mapping)]
        return [dict(obj)]
    return [dict(item) for item in obj if isinstance(item, Mapping)]


def _remove_spans(text: str, spans: Sequence[Tuple[int, int]]) -> str:
    if not spans:
        return text
    pieces: List[str] = []
    cursor = 0
    for start, end in sorted(spans):
        if start < cursor:
            continue
        pieces.append(text[cursor:start])
        cursor = end
    pieces.append(text[cursor:])
    return "".join(pieces)


# ---------------------------------------------------------------- 合并与工具


def _item_id(item: Any) -> Optional[str]:
    if not isinstance(item, Mapping):
        return None
    value = item.get("id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return None


def merge_by_id(existing: Sequence[Any], incoming: Sequence[Any]) -> Tuple[List[Any], int, int]:
    """按 ``id`` 合并两列对象：同 id 就地替换，新 id 追加；无 id 的追加。"""
    merged: List[Any] = [dict(item) if isinstance(item, Mapping) else item for item in existing]
    index: Dict[str, int] = {}
    for position, item in enumerate(merged):
        key = _item_id(item)
        if key is not None:
            index[key] = position
    added = replaced = 0
    for item in incoming:
        if not isinstance(item, Mapping):
            continue
        key = _item_id(item)
        if key is not None and key in index:
            merged[index[key]] = dict(item)
            replaced += 1
            continue
        merged.append(dict(item))
        added += 1
        if key is not None:
            index[key] = len(merged) - 1
    return merged, added, replaced


_CARD_NUMBER_RE = re.compile(r"(?:第\s*)?(\d{1,3})\s*(?:个)?(?:问题)?卡|卡\s*(\d{1,3})|[Qq](\d{1,3})(?!\d)")
_FILENAME_RE = re.compile(r"([\w\-.]+\.[Pp][Nn][Gg]|[\w\-.]+\.[Jj][Pp][Ee]?[Gg]|[\w\-.]+\.[Ww][Ee][Bb][Pp])")
_TOKEN_RE = re.compile(r"[\w\-]{4,}")


def _read_json_lenient(path: Path, default: Any) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _add_card_by_title(cards: Any, number: int, add_card_image: Any) -> bool:
    """按卡面标题号（"14. …" / "14、…"）找卡并附图；命中返回 True。"""
    hit = False
    for card in cards:
        if not isinstance(card, Mapping):
            continue
        title = str(card.get("title") or "")
        if re.match(rf"^{number}\s*[.、]", title):
            add_card_image(card)
            hit = True
    return hit


def resolve_referenced_images(
    session_root: Any, task_root: Any, text: str, limit: int
) -> List[Path]:
    """从用户消息里解析"被引用的图"：问题卡号／图片文件名／画布项 id。

    返回按引用顺序去重的绝对路径（只收确实存在的文件，最多 ``limit`` 张）。
    定位不到的引用保持"找不到"——由系统提示词的纪律要求模型如实说看不到。
    """
    text = str(text or "")
    if not text or limit <= 0:
        return []
    session_root = Path(session_root)
    task_root = Path(task_root)
    found: List[Path] = []

    def add(path: Path) -> None:
        if path.is_file() and path not in found and len(found) < limit:
            found.append(path)

    cards = _read_json_lenient(session_root / "cards.json", {}).get("cards") or []
    canvas = _read_json_lenient(session_root / "canvas.json", {}).get("items") or []

    def add_card_image(card: Any) -> None:
        if not isinstance(card, Mapping):
            return
        attach = card.get("attach")
        image = attach.get("image") if isinstance(attach, Mapping) else None
        if isinstance(image, str) and image:
            add(session_root / image)

    for match in _CARD_NUMBER_RE.finditer(text):
        number = int(next(g for g in match.groups() if g))
        # 标题号优先：卡面自编号（"14. …"）是给用户看的，列表序不是。
        if not _add_card_by_title(cards, number, add_card_image):
            if 1 <= number <= len(cards):
                add_card_image(cards[number - 1])

    name_dirs = (
        session_root, session_root / "pages", session_root / "renders",
        task_root, task_root / "out" / "still",
        task_root / "out" / "still" / "view", task_root / "out" / "still" / "match",
    )
    for match in _FILENAME_RE.finditer(text):
        name = match.group(1)
        for base in name_dirs:
            add(base / name)

    ids = {str(item.get("id")): item for item in canvas if isinstance(item, Mapping)}
    for token in _TOKEN_RE.findall(text):
        item = ids.get(token)
        if item and isinstance(item.get("image"), str):
            add(session_root / item["image"])
    return found


def collect_images(task_root: Any, pattern: str, limit: int) -> List[Path]:
    """任务根下按 glob 找图片，按修改时间从新到旧，取前 ``limit`` 张。"""
    if not pattern or limit <= 0:
        return []
    try:
        files = [p for p in Path(task_root).glob(pattern) if p.is_file()]
    except (ValueError, OSError):
        return []
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files[:limit]


def build_models(
    config: Any, default_role: str, explicit: Sequence[Mapping[str, str]] = ()
) -> List[dict]:
    """chat_meta 的模型表（下拉框的数据源）。

    显式清单（``loop.yaml`` 的 ``models:``）优先：**按清单顺序原样呈现**，label 缺省
    时用 ``provider/model``；未显式声明时＝config 全部 roles 按 **provider/model 端点
    去重**（同一端点的多个 role 名折叠为一项，取排序靠前的那个）。默认 role 永远在表
    内（被去重掉时置顶补回），免得下拉框选不到默认值。
    """
    roles = {
        name: role
        for name, role in config.roles.items()
        if role.provider and role.model
    }
    models: List[dict] = []
    seen_ids = set()

    def label_for(role_name: str) -> str:
        role = roles.get(role_name)
        return f"{role.provider}/{role.model}" if role is not None else f"{role_name}（未配置）"

    if explicit:
        for entry in explicit:
            role_name = str(entry.get("role") or "").strip()
            if not role_name or role_name in seen_ids:
                continue
            label = str(entry.get("label") or "").strip() or label_for(role_name)
            models.append({"id": role_name, "label": label})
            seen_ids.add(role_name)
        if default_role and default_role not in seen_ids:
            models.insert(0, {"id": default_role, "label": label_for(default_role)})
        return models

    preferred = ("scene", "distill", "vision", "claims", "select")
    ordered = [name for name in preferred if name in roles]
    ordered += sorted(name for name in roles if name not in preferred)
    seen_endpoints = set()
    by_endpoint: Dict[tuple, dict] = {}
    for name in ordered:
        role = roles[name]
        endpoint = (role.provider, role.model)
        if endpoint in seen_endpoints:
            continue
        seen_endpoints.add(endpoint)
        entry = {"id": name, "label": f"{role.provider}/{role.model}"}
        models.append(entry)
        by_endpoint[endpoint] = entry
        seen_ids.add(name)

    if default_role and default_role not in seen_ids:
        role = roles.get(default_role)
        covered = by_endpoint.get((role.provider, role.model)) if role is not None else None
        if covered is not None:
            # 默认 role 的端点已被保留项覆盖：改名（同一端点的 role 名等价），
            # 而不是再加一行同标签的重复项。
            covered["id"] = default_role
        else:
            models.insert(0, {"id": default_role, "label": label_for(default_role)})
    return models


def _tail_lines(text: str, limit: int) -> List[str]:
    lines = (text or "").splitlines()
    return lines[-limit:] if limit > 0 else lines


def _decode_stream(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


# ---------------------------------------------------------------- 锁


def acquire_lock(session_root: Any, *, stale: float = LOCK_STALE_SECONDS) -> Path:
    """``loop.lock`` 排他文件：心跳新鲜（<``stale`` 秒）时报错，过期则接管。"""
    root = Path(session_root)
    root.mkdir(parents=True, exist_ok=True)
    path = root / LOCK_FILENAME
    for _ in range(3):
        try:
            handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            try:
                age = time.time() - path.stat().st_mtime
            except OSError:
                continue
            if age < stale:
                raise LoopError(
                    f"another loop driver holds {path} (heartbeat {age:.0f}s old, "
                    f"stale threshold {stale:.0f}s)"
                )
            path.unlink(missing_ok=True)
            continue
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump({"pid": os.getpid(), "ts": _now()}, stream)
        return path
    raise LoopError(f"cannot acquire {path}: it keeps reappearing")


def touch_lock(path: Any) -> None:
    """刷新锁文件的心跳（mtime）。"""
    try:
        os.utime(Path(path), None)
    except OSError:
        pass


def release_lock(path: Any) -> None:
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass


# ---------------------------------------------------------------- 驱动


class Driver:
    """一个会话 + 一个任务 + 一个所配模型的环路驱动。"""

    def __init__(
        self,
        *,
        session_root: Any,
        task_root: Any,
        loop: LoopConfig,
        config: Any,
        default_role: str = "scene",
        transport: Optional[Transport] = None,
        environ: Optional[Mapping[str, str]] = None,
        vision: str = "auto",
        no_actions: bool = False,
        timeout: float = llm_mod.HTTP_TIMEOUT,
        command_timeout: Optional[float] = None,
        sleep: Optional[Any] = None,
        lock_path: Optional[Path] = None,
        out: Any = None,
        err: Any = None,
    ) -> None:
        self.session_root = Path(session_root).expanduser().resolve()
        self.session_root.mkdir(parents=True, exist_ok=True)
        self.task_root = Path(task_root).expanduser().resolve()
        self.loop = loop
        self.config = config
        self.default_role = default_role
        self.transport = transport
        self.environ = os.environ if environ is None else environ
        self.vision = vision
        self.no_actions = no_actions
        self.timeout = float(timeout)
        self.command_timeout = float(
            loop.command_timeout if command_timeout is None else command_timeout
        )
        self.sleep = sleep or time.sleep
        self.lock_path = Path(lock_path) if lock_path else self.session_root / LOCK_FILENAME
        self.out = out if out is not None else sys.stdout
        self.err = err if err is not None else sys.stderr
        self._clients: Dict[str, ChatClient] = {}
        self._endpoints: Dict[str, Any] = {}
        self._default_label = ""

    # -------------------------------------------------------- 小工具

    def _diag(self, message: str) -> None:
        """英文诊断走 stderr。"""
        self.err.write(f"[sceneloop] {message}\n")
        self.err.flush()

    def _read_json(self, path: Path, default: Any) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return default
        except (json.JSONDecodeError, OSError) as exc:
            self._diag(f"cannot read {path.name}: {exc}")
            return default

    def _write_json(self, path: Path, payload: Any) -> None:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)

    def _read_jsonl(self, path: Path) -> List[dict]:
        records: List[dict] = []
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return records
        except OSError as exc:
            self._diag(f"cannot read {path.name}: {exc}")
            return records
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                records.append(item)
        return records

    def _append_jsonl(self, path: Path, record: dict) -> None:
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _append_log(self, record: dict) -> None:
        self._append_jsonl(self.session_root / LOG_FILENAME, record)

    # -------------------------------------------------------- 模型

    def _client(self, role: str) -> ChatClient:
        if role not in self._clients:
            endpoint = resolve_role(self.config, role)
            client = client_for_role(
                self.config,
                role,
                environ=self.environ,
                transport=self.transport,
                timeout=self.timeout,
                sleep=self.sleep,
            )
            self._clients[role] = client
            self._endpoints[role] = endpoint
        return self._clients[role]

    def _label(self, role: str) -> str:
        endpoint = self._endpoints.get(role)
        if endpoint is None:
            try:
                # 只解析端点拿 provider/model 标签；不碰密钥、不建客户端。
                endpoint = resolve_role(self.config, role)
                self._endpoints[role] = endpoint
            except ConfigError:
                return role
        return f"{endpoint.provider}/{endpoint.model}"

    def _role_for(self, message: Mapping[str, Any]) -> str:
        """人在这条消息上选的模型（role id）；没选或认不得就用默认 role。"""
        chosen = message.get("model")
        if isinstance(chosen, str) and chosen.strip() and chosen.strip() in self.config.roles:
            return chosen.strip()
        return self.default_role

    # -------------------------------------------------------- 上下文

    def _system_prompt(self) -> str:
        glob = self.loop.images_glob or "（未声明）"
        prompt = (
            _SYSTEM_PROMPT.replace("<<PARAMS_FILE>>", self.loop.params_file)
            .replace("<<COMMANDS>>", ", ".join(self.loop.commands) or "（无）")
            .replace(
                "<<IMAGES>>",
                f"{glob}（vision={self.vision}，最多 {self.loop.images_max} 张）；"
                "当用户消息引用了某问题卡/图片文件名/画布项时，被引用的图优先随附",
            )
        )
        if self.loop.system_extra.strip():
            prompt += "\n任务补充说明：\n" + self.loop.system_extra.strip() + "\n"
        if self.no_actions:
            prompt += "\n注意：本次以 --no-actions 运行，动作块会被忽略、不会执行。\n"
        return prompt

    def _params_text(self) -> str:
        path = self.task_root / self.loop.params_file
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            self._diag(f"cannot read params file {path}: {exc}")
            return f"（参数库文件读取失败：{self.loop.params_file}）"

    def _session_summary(self) -> str:
        record = self._read_json(self.session_root / "record.json", {"rows": []})
        rows = record.get("rows") if isinstance(record.get("rows"), list) else []
        cards = self._read_json(self.session_root / "cards.json", {"cards": []})
        card_list = cards.get("cards") if isinstance(cards.get("cards"), list) else []
        lines = ["## 会话台账 record.json（摘要）"]
        if rows:
            for row in rows:
                if not isinstance(row, Mapping):
                    continue
                lines.append(
                    f"- {row.get('id', '?')}｜{row.get('subject', '')}｜{row.get('value', '')}"
                    f"｜{row.get('status', '')}｜{row.get('evidence', '')}"
                )
        else:
            lines.append("（空）")
        lines.append("")
        lines.append("## 问题卡 cards.json（摘要）")
        if card_list:
            for card in card_list:
                if not isinstance(card, Mapping):
                    continue
                text = str(card.get("text") or "")
                lines.append(f"- {card.get('id', '?')}｜{card.get('title', '')}｜{text[:80]}")
        else:
            lines.append("（空）")
        return "\n".join(lines)

    def _history_text(self, before_index: int) -> str:
        chat = self._read_jsonl(self.session_root / CHAT_FILENAME)
        window = chat[max(0, before_index - CHAT_CONTEXT_MESSAGES) : before_index]
        if not window:
            return "（无）"
        lines: List[str] = []
        for item in window:
            role = str(item.get("role") or "?")
            text = str(item.get("text") or "")
            if len(text) > CHAT_TEXT_LIMIT:
                text = text[:CHAT_TEXT_LIMIT] + "…"
            lines.append(f"[{item.get('ts', '')}] {role}: {text}")
        return "\n".join(lines)

    def _context_images(self, message: Mapping[str, Any]) -> List[str]:
        """本轮随信图片：用户消息引用的图优先（问题卡附图/文件名/画布项 id），
        再用 loop.yaml 的 images.glob 按时间补足到上限。"""
        if self.vision == "off":
            return []
        images: List[Path] = resolve_referenced_images(
            self.session_root, self.task_root,
            str(message.get("text") or ""), self.loop.images_max,
        )
        if len(images) < self.loop.images_max and self.loop.images_glob:
            for path in collect_images(self.task_root, self.loop.images_glob, self.loop.images_max):
                if path not in images:
                    images.append(path)
                if len(images) >= self.loop.images_max:
                    break
        return [str(p) for p in images[: self.loop.images_max]]

    def _chat_index(self, message: Mapping[str, Any]) -> int:
        """这条消息在 chat.jsonl 里的下标；找不着就当作最后一条。"""
        chat = self._read_jsonl(self.session_root / CHAT_FILENAME)
        for position, item in enumerate(chat):
            if (
                item.get("ts") == message.get("ts")
                and item.get("role") == message.get("role")
                and item.get("text") == message.get("text")
            ):
                return position
        return max(0, len(chat) - 1)

    def _images_section(self, message: Mapping[str, Any], images: Sequence[str]) -> str:
        """随信图片清单：模型据此在回答前声明"我看到了什么"。"""
        if not images:
            return "## 随信图片\n（本轮无图——你看不到任何图片，回答前先说明这一点）"
        referenced = {
            p.name
            for p in resolve_referenced_images(
                self.session_root, self.task_root,
                str(message.get("text") or ""), self.loop.images_max,
            )
        }
        lines = ["## 随信图片（你实际看到的就是这些，回答前先在此清单内声明）"]
        for path in images:
            name = Path(path).name
            tag = "用户引用" if name in referenced else "默认最近图"
            lines.append(f"- {name}（{tag}）")
        return "\n".join(lines)

    def _build_messages(self, message: Mapping[str, Any], images: Sequence[str]) -> List[dict]:
        index = self._chat_index(message)
        body = "\n\n".join(
            [
                "## loop.yaml\n```yaml\n" + self.loop.path.read_text(encoding="utf-8") + "\n```",
                f"## 参数库 {self.loop.params_file}\n```yaml\n{self._params_text()}\n```",
                self._session_summary(),
                self._images_section(message, images),
                "## 最近 %d 条对话\n%s" % (CHAT_CONTEXT_MESSAGES, self._history_text(index)),
                "## 本轮用户消息\n" + str(message.get("text") or ""),
            ]
        )
        return [
            {"role": "system", "content": self._system_prompt()},
            llm_mod.user_message(body, images),
        ]

    # -------------------------------------------------------- 往返

    def pending_messages(self) -> Tuple[List[dict], int]:
        """（待处理的用户消息，从哪条开始）。游标存在 loop_state.json，断点续跑。"""
        chat = self._read_jsonl(self.session_root / CHAT_FILENAME)
        users = [item for item in chat if item.get("role") == "user"]
        state = self._read_json(self.session_root / STATE_FILENAME, {})
        cursor = state.get("processed_user_messages")
        cursor = int(cursor) if isinstance(cursor, int) and not isinstance(cursor, bool) else 0
        if cursor < 0 or cursor > len(users):
            self._diag(
                f"loop_state cursor {cursor} out of range (0..{len(users)}); "
                "restarting from the beginning of chat.jsonl"
            )
            cursor = 0
        return users[cursor:], cursor

    def _save_state(self, cursor: int, message: Mapping[str, Any]) -> None:
        state = self._read_json(self.session_root / STATE_FILENAME, {})
        state["processed_user_messages"] = int(cursor)
        state["last_message_ts"] = message.get("ts")
        state["last_role"] = self._role_for(message)
        state["updated_ts"] = _now()
        self._write_json(self.session_root / STATE_FILENAME, state)

    def run_once(self) -> int:
        """处理所有未读的用户消息（逐条顺序、各自回一条），返回处理条数。"""
        pending, cursor = self.pending_messages()
        for offset, message in enumerate(pending):
            self._handle(message)
            self._save_state(cursor + offset + 1, message)
        self._touch_lock()
        self._update_meta()
        return len(pending)

    def run_forever(self, interval: float) -> None:
        while True:
            self.run_once()
            time.sleep(max(0.1, interval))

    def _handle(self, message: Mapping[str, Any]) -> None:
        role = self._role_for(message)
        started = time.monotonic()
        try:
            client = self._client(role)
            reply = self._exchange(client, message)
        except (LlmError, ConfigError) as exc:
            self._diag(f"role '{role}' round failed: {exc}")
            self._write_reply(
                {
                    "ts": _now(),
                    "role": "agent",
                    "text": f"调用模型失败（role={role}）：{exc}",
                    "model": role,
                    "provider": "",
                    "actions_executed": [],
                    "refs": [],
                    "usage": {},
                    "seconds": round(time.monotonic() - started, 3),
                    "error": str(exc),
                }
            )
            self._flush()
            return
        self._respond(role, reply, started)

    def _exchange(self, client: ChatClient, message: Mapping[str, Any]):
        """调 LLM；端点因图片报 4xx 时降级为纯文本重试一次。"""
        images = self._context_images(message)
        messages = self._build_messages(message, images)
        try:
            return client.complete(messages), False
        except LlmHttpError as exc:
            if images and 400 <= exc.status < 500:
                self._diag(
                    f"endpoint rejected the image payload (HTTP {exc.status}); "
                    "retrying without images"
                )
                self._append_log(
                    {
                        "ts": _now(),
                        "kind": "vision_downgrade",
                        "http_status": exc.status,
                        "images": [Path(p).name for p in images],
                    }
                )
                return client.complete(self._build_messages(message, ())), True

    def _respond(
        self,
        role: str,
        pair: Tuple[Any, bool],
        started: float,
    ) -> None:
        reply, degraded = pair
        label = self._label(role)
        body, actions, notes = parse_actions(reply.content)
        if degraded:
            notes.append("（端点拒绝了图片输入，本轮已按纯文本重试）")
        executed: List[dict] = []
        refs: List[str] = []
        if self.no_actions:
            if actions:
                notes.append(f"（--no-actions：{len(actions)} 项动作未执行）")
        else:
            runs = [
                str(action.get("name"))
                for action in actions
                if isinstance(action, Mapping) and str(action.get("kind") or "").strip() == "run"
            ]
            if runs:
                # 重建/渲染是长任务：先回一条过程状态，界面不必等命令跑完才知道在动。
                self._append_jsonl(
                    self.session_root / CHAT_FILENAME,
                    {
                        "ts": _now(),
                        "role": "system",
                        "text": "开始执行命令：" + "、".join(runs) + "（过程日志见 loop_log.jsonl）",
                        "model": label,
                    },
                )
            for action in actions:
                result = self._execute(action, label)
                executed.append(result)
                refs.extend(result.get("refs") or [])
                note = _action_note(result)
                if note:
                    notes.append(note)
        text = body
        if notes:
            text = (text + "\n\n" + "\n".join(notes)).strip()
        seconds = time.monotonic() - started
        refs = list(dict.fromkeys(refs))
        self._write_reply(
            {
                "ts": _now(),
                "role": "agent",
                "text": text,
                "model": reply.model,
                "provider": self._endpoints[role].provider if role in self._endpoints else "",
                "model_role": role,
                "actions_executed": executed,
                "refs": refs,
                "usage": dict(reply.usage),
                "llm_seconds": round(reply.seconds, 3),
                "seconds": round(seconds, 3),
            }
        )
        self.out.write(
            f"[{time.strftime('%H:%M:%S')}] {label}｜动作 {len(executed)} 项｜{seconds:.1f}s\n"
        )
        self.out.flush()

    def _write_reply(self, record: dict) -> None:
        self._append_jsonl(self.session_root / CHAT_FILENAME, record)

    def _flush(self) -> None:
        self.out.flush()
        self.err.flush()

    def _touch_lock(self) -> None:
        touch_lock(self.lock_path)

    def _update_meta(self) -> None:
        source = (
            "loop.yaml:models（显式清单）"
            if self.loop.models
            else "config:roles（按 provider/model 去重）"
        )
        meta = {
            "heartbeat_ts": _now(),
            "models": build_models(self.config, self.default_role, self.loop.models),
            "models_source": source,
            "default_role": self.default_role,
            "vision": self.vision,
        }
        self._write_json(self.session_root / META_FILENAME, meta)

    # -------------------------------------------------------- 动作

    def _execute(self, action: Any, label: str) -> dict:
        if not isinstance(action, Mapping):
            return {"kind": "invalid", "ok": False, "error": "action is not a mapping"}
        kind = str(action.get("kind") or "").strip()
        if kind == "edit_params":
            return self._do_edit_params(action)
        if kind == "run":
            return self._do_run(action)
        if kind == "session_update":
            return self._do_session_update(action)
        if kind == "escalate":
            return self._do_escalate(action, label)
        return {"kind": kind or "invalid", "ok": False, "error": f"unknown action kind: {kind!r}"}

    def _resolve_editable(self, file: Any) -> Tuple[Path, str]:
        if not isinstance(file, str) or not file.strip():
            raise LoopError("edit_params needs a `file`")
        candidate = (self.task_root / file.strip()).resolve()
        if candidate != self.task_root and self.task_root not in candidate.parents:
            raise LoopError(f"edit_params file escapes the task root: {file}")
        if not candidate.is_file():
            raise LoopError(f"edit_params file does not exist: {file}")
        rel = candidate.relative_to(self.task_root).as_posix()
        allowed = set(self.loop.editable)
        if rel not in allowed and candidate.name not in allowed:
            raise LoopError(f"file '{file}' is not in the loop.yaml editable whitelist")
        return candidate, rel

    def _do_edit_params(self, action: Mapping[str, Any]) -> dict:
        try:
            target, rel = self._resolve_editable(action.get("file"))
        except LoopError as exc:
            return {"kind": "edit_params", "ok": False, "file": str(action.get("file")), "error": str(exc)}
        edits = action.get("edits")
        if not isinstance(edits, list) or not edits:
            return {"kind": "edit_params", "file": rel, "ok": False, "error": "edits must be a non-empty list"}
        try:
            text = target.read_text(encoding="utf-8")
        except OSError as exc:
            return {"kind": "edit_params", "file": rel, "ok": False, "error": f"cannot read file: {exc}"}
        applied: List[dict] = []
        failed: List[dict] = []
        for entry in edits:
            if not isinstance(entry, Mapping):
                failed.append({"reason": "edit is not a mapping"})
                continue
            before = entry.get("before")
            after = entry.get("after")
            if not isinstance(before, str) or not before or not isinstance(after, str):
                failed.append(
                    {"before": before if isinstance(before, str) else "",
                     "reason": "before/after must be non-empty strings"}
                )
                continue
            count = text.count(before)
            if count != 1:
                failed.append(
                    {"before": before, "reason": f"before matched {count} times, expected exactly 1"}
                )
                continue
            text = text.replace(before, after, 1)
            applied.append({"before": before, "after": after, "note": str(entry.get("note") or "")})
        if applied:
            tmp = target.with_name(target.name + ".tmp")
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, target)
            self._append_log(
                {"ts": _now(), "kind": "edit_params", "file": rel,
                 "applied": applied, "failed": failed}
            )
        return {
            "kind": "edit_params",
            "file": rel,
            "ok": bool(applied),
            "edits": len(applied),
            "failed": failed,
        }

    def _do_run(self, action: Mapping[str, Any]) -> dict:
        name = action.get("name")
        if not isinstance(name, str) or not name.strip():
            return {"kind": "run", "ok": False, "error": "run needs a `name`"}
        name = name.strip()
        command = self.loop.commands.get(name)
        if command is None:
            return {
                "kind": "run",
                "name": name,
                "ok": False,
                "error": f"run '{name}' is not in the loop.yaml commands whitelist",
            }
        started = time.monotonic()
        stdout_text = ""
        stderr_text = ""
        exit_code: Optional[int] = None
        try:
            process = subprocess.run(
                list(command),
                cwd=str(self.task_root),
                capture_output=True,
                text=True,
                timeout=self.command_timeout,
                env=os.environ.copy(),
            )
            stdout_text = process.stdout or ""
            stderr_text = process.stderr or ""
            exit_code = process.returncode
        except subprocess.TimeoutExpired as exc:
            stdout_text = _decode_stream(exc.stdout)
            stderr_text = _decode_stream(exc.stderr) + f"\ntimeout after {self.command_timeout:g}s"
            exit_code = -1
        except OSError as exc:
            stderr_text = f"cannot start {command[0]!r}: {exc}"
            exit_code = -1
        seconds = time.monotonic() - started
        refs = self._attach_images() if exit_code == 0 else []
        self._append_log(
            {
                "ts": _now(),
                "kind": "run",
                "name": name,
                "command": list(command),
                "cwd": str(self.task_root),
                "exit_code": exit_code,
                "seconds": round(seconds, 3),
                "stdout_tail": _tail_lines(stdout_text, STDIO_TAIL_LINES),
                "stderr_tail": _tail_lines(stderr_text, STDIO_TAIL_LINES),
                "refs": refs,
            }
        )
        result = {
            "kind": "run",
            "name": name,
            "ok": exit_code == 0,
            "exit_code": exit_code,
            "seconds": round(seconds, 1),
            "refs": refs,
        }
        if exit_code != 0:
            tail = _tail_lines(stderr_text or stdout_text, 5)
            result["error"] = " / ".join(tail)[-400:] or f"exit code {exit_code}"
        return result

    def _do_session_update(self, action: Mapping[str, Any]) -> dict:
        plan = (
            ("record_rows", "record.json", "rows"),
            ("canvas_items", "canvas.json", "items"),
            ("cards", "cards.json", "cards"),
        )
        counts: Dict[str, int] = {}
        added = replaced = 0
        for key, filename, list_key in plan:
            incoming = action.get(key)
            if incoming is None:
                continue
            if not isinstance(incoming, list):
                return {"kind": "session_update", "ok": False, "error": f"{key} must be a list"}
            if not incoming:
                continue
            path = self.session_root / filename
            document = self._read_json(path, {list_key: []})
            existing = document.get(list_key) if isinstance(document.get(list_key), list) else []
            merged, n_added, n_replaced = merge_by_id(existing, incoming)
            self._backup(filename)
            document[list_key] = merged
            self._write_json(path, document)
            counts[key] = len(incoming)
            added += n_added
            replaced += n_replaced
        if not counts:
            return {"kind": "session_update", "ok": False, "error": "nothing to merge"}
        self._append_log({"ts": _now(), "kind": "session_update", **counts,
                          "added": added, "replaced": replaced})
        return {"kind": "session_update", "ok": True, "added": added, "replaced": replaced, **counts}

    def _do_escalate(self, action: Mapping[str, Any], label: str) -> dict:
        reason = action.get("reason")
        reason = reason.strip() if isinstance(reason, str) and reason.strip() else "(no reason given)"
        ts = _now()
        path = self.session_root / ESCALATION_FILENAME
        header = (
            "# 升级到 kimi code 回路（ESCALATION）\n\n"
            "本文件由会话舱环路（primer.scene.loop）追加。出现它说明有一项决定需要修改 "
            "primer 代码，环路内无法闭环，已交给 kimi code 回路处理。\n"
        )
        if not path.exists():
            path.write_text(header, encoding="utf-8")
        with path.open("a", encoding="utf-8") as stream:
            stream.write(
                f"\n## {ts}\n\n- 模型：{label}\n- 理由：{reason}\n\n"
                "此问题需要修改 primer 源码，已升级到 kimi code 回路。\n"
            )
        row = {
            "id": f"escalation.{ts}",
            "subject": "⚠ 已升级 kimi code 回路",
            "value": reason,
            "status": "open",
            "evidence": f"{label} 于 {ts} 置升级标记（见 ESCALATION.md）",
        }
        record_path = self.session_root / "record.json"
        document = self._read_json(record_path, {"rows": []})
        rows = document.get("rows") if isinstance(document.get("rows"), list) else []
        merged, _added, _replaced = merge_by_id(rows, [row])
        self._backup("record.json")
        document["rows"] = merged
        self._write_json(record_path, document)
        self._append_log({"ts": ts, "kind": "escalate", "reason": reason, "model": label})
        return {"kind": "escalate", "ok": True, "reason": reason}

    def _attach_images(self) -> List[str]:
        paths = collect_images(self.task_root, self.loop.images_glob, self.loop.images_max)
        if not paths:
            return []
        renders = self.session_root / "renders"
        renders.mkdir(parents=True, exist_ok=True)
        refs: List[str] = []
        for source in paths:
            try:
                shutil.copy2(source, renders / source.name)
            except OSError as exc:
                self._diag(f"cannot copy {source} into renders/: {exc}")
                continue
            refs.append(f"renders/{source.name}")
        return refs

    def _backup(self, filename: str) -> Optional[str]:
        """写会话 JSON 前备份到 ``_backup/``，每份保留最近 ``BACKUP_KEEP`` 个。"""
        source = self.session_root / filename
        if not source.is_file():
            return None
        backup_dir = self.session_root / BACKUP_DIRNAME
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        destination = backup_dir / f"{source.stem}.{stamp}{source.suffix}"
        try:
            shutil.copy2(source, destination)
        except OSError as exc:
            self._diag(f"cannot back up {filename}: {exc}")
            return None
        prefix = source.stem + "."
        related = sorted(
            (
                item
                for item in backup_dir.iterdir()
                if item.is_file() and item.name.startswith(prefix) and item.suffix == source.suffix
            ),
            key=lambda item: item.name,
            reverse=True,
        )
        for stale in related[BACKUP_KEEP:]:
            try:
                stale.unlink()
            except OSError:
                pass
        return destination.name


def _action_note(result: Mapping[str, Any]) -> str:
    """把动作结果里需要人看见的失败/升级写成一行中文说明。"""
    kind = str(result.get("kind") or "")
    if result.get("ok"):
        if kind == "escalate":
            return "**已升级 kimi code 回路**（写入 ESCALATION.md，并记入台账）。"
        return ""
    detail = str(result.get("error") or "").strip()
    if kind == "edit_params":
        failed = result.get("failed") or []
        detail = detail or "；".join(str(item.get("reason", "")) for item in failed if isinstance(item, Mapping))
        return f"动作未完成：edit_params（{detail or '无有效编辑'}）。"
    if kind == "run":
        return f"动作未完成：run({result.get('name', '?')})（{detail or '见 loop_log.jsonl'}）。"
    return f"动作未完成：{kind or '?'}（{detail}）。"


# ---------------------------------------------------------------- 项目根


def _discover_project_root(task_root: Path) -> Path:
    """从任务根向上找带 ``_primer/config.yaml`` 的那一层；找不到就用任务根。"""
    for candidate in (task_root, *task_root.parents):
        if (candidate / OUTPUT_DIRNAME / CONFIG_FILENAME).is_file():
            return candidate
    return task_root


# ---------------------------------------------------------------- 命令行


def _startup_report(driver: Driver, project_root: Path, interval: float) -> str:
    models = build_models(driver.config, driver.default_role, driver.loop.models)
    labels = "、".join(f"{m['id']}={m['label']}" for m in models) or "（无）"
    commands = "、".join(driver.loop.commands) or "（无）"
    editable = "、".join(driver.loop.editable)
    images = (
        f"{driver.loop.images_glob}（最多 {driver.loop.images_max} 张）"
        if driver.loop.images_glob
        else "（未声明）"
    )
    return "\n".join(
        [
            "primer 会话舱环路",
            f"  会话目录：{driver.session_root}",
            f"  任务根：{driver.task_root}",
            f"  工程根：{project_root}",
            f"  默认模型：{driver.default_role}={driver._label(driver.default_role)}",
            f"  可选模型：{labels}",
            f"  参数库：{driver.loop.params_file}（可改：{editable}）",
            f"  可执行命令：{commands}",
            f"  渲染图：{images}｜视觉输入：{driver.vision}",
            f"  动作执行：{'否（--no-actions）' if driver.no_actions else '是'}",
            f"  轮询间隔：{interval:g}s｜锁：{driver.lock_path.name}",
        ]
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m primer.scene.loop",
        description=(
            "Watch a review session for new user messages, assemble the on-disk context, "
            "ask the configured LLM, execute the whitelisted actions in its reply, and "
            "write the answer back to the session. Actions: edit_params, run, "
            "session_update, escalate."
        ),
    )
    parser.add_argument("--session", required=True, metavar="DIR", help="session directory")
    parser.add_argument("--task", required=True, metavar="DIR", help="task root (loop.yaml lives here)")
    parser.add_argument("--role", default="scene", metavar="NAME", help="default model role (default scene)")
    parser.add_argument("--interval", type=float, default=2.0, metavar="SECONDS", help="poll interval (default 2)")
    parser.add_argument("--no-actions", action="store_true", help="parse but do not execute action blocks")
    parser.add_argument("--once", action="store_true", help="handle the pending messages once, then exit")
    parser.add_argument("--vision", choices=("auto", "off"), default="auto",
                        help="attach the newest render images when loop.yaml declares them (default auto)")
    parser.add_argument("--loop", metavar="FILE", help="loop.yaml path (default <task>/loop.yaml)")
    parser.add_argument("--project-root", metavar="DIR",
                        help="project root holding _primer/config.yaml (default: search upward from --task)")
    parser.add_argument("--config", metavar="FILE", help="explicit config file on top of the others")
    parser.add_argument("--timeout", type=float, default=llm_mod.HTTP_TIMEOUT, metavar="SECONDS",
                        help=f"LLM HTTP timeout (default {llm_mod.HTTP_TIMEOUT:g})")
    parser.add_argument("--command-timeout", type=float, default=None, metavar="SECONDS",
                        help="wall-clock limit for a whitelisted command (default loop.yaml or 1800)")
    parser.add_argument("--verbose", action="store_true", help="log diagnostics to stderr")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    task_root = Path(args.task).expanduser().resolve()
    loop_path = Path(args.loop).expanduser() if args.loop else task_root / DEFAULT_LOOP_FILENAME
    project_root = (
        Path(args.project_root).expanduser().resolve()
        if args.project_root
        else _discover_project_root(task_root)
    )
    try:
        loop = load_loop_config(loop_path)
        config = load_config(project_root, args.config)
    except (LoopError, ConfigError) as exc:
        sys.stderr.write(f"[sceneloop] {exc}\n")
        return 2
    if args.role not in config.roles:
        sys.stderr.write(
            f"[sceneloop] role '{args.role}' is not defined in the configuration "
            f"(known: {', '.join(sorted(config.roles)) or 'none'})\n"
        )
        return 2
    if not task_root.is_dir():
        sys.stderr.write(f"[sceneloop] task root is not a directory: {task_root}\n")
        return 2

    try:
        lock_path = acquire_lock(Path(args.session))
    except LoopError as exc:
        sys.stderr.write(f"[sceneloop] {exc}\n")
        return 2
    driver = Driver(
        session_root=args.session,
        task_root=task_root,
        loop=loop,
        config=config,
        default_role=args.role,
        vision=args.vision,
        no_actions=args.no_actions,
        timeout=args.timeout,
        command_timeout=args.command_timeout,
        lock_path=lock_path,
    )
    try:
        print(_startup_report(driver, project_root, args.interval))
        sys.stdout.flush()
        if args.once:
            processed = driver.run_once()
            if not processed:
                print("本轮没有新的用户消息。")
            return 0
        print("按 Ctrl-C 停止。")
        sys.stdout.flush()
        try:
            driver.run_forever(args.interval)
        except KeyboardInterrupt:
            print("已停止。")
        return 0
    finally:
        release_lock(lock_path)


if __name__ == "__main__":  # pragma: no cover - 由命令行入口走到
    raise SystemExit(main())
