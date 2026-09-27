# -*- coding: utf-8 -*-
"""MinerU 命令行后端：以子进程调用 ``mineru-kit parse``。

本模块**从不 import MinerU**：它的依赖树很重，且按「uv tool」装在独立虚拟环境
里，只应通过命令行调用。以下细节来自实测（4.0.7）：

* 目录批量 + ``-f zip`` 会按输入主干名写出**一个** ``<stem>.zip``；
* 同一目录里两个输入主干相同会整批中止且什么都不写，故输入一律走
  :func:`primer.literature.stage.stage_batch` 的唯一主干；
* ``-f markdown`` 只写 ``.md``（不含图片），因此本后端固定用 ``zip``；
* ``-p`` 是 ``--pages`` 而不是路径，``--backend`` 参数已不存在（换成 ``--tier``）；
* 任意一个输入解析失败都会让整个进程非零退出，但**失败之前**写出的产物仍然有效；
* 子进程的 stdin 必须显式指向 ``DEVNULL``：MinerU 会读 stdin，若继承了父进程的
  管道或 socket（后台任务框架就是这样），它会一直等输入，进程 0% CPU 挂到天荒地老。

HuggingFace 在本机网络上不可达（首次下载以 ``[Errno 60]`` 超时收场），而
``MINERU_MODEL_SOURCE`` 未设置时 ``model.source`` 默认为 ``auto``，会先去探测
HF 再回退 ModelScope。因此默认把 ``MINERU_MODEL_SOURCE=modelscope`` 注入子进程
环境，彻底避免那一轮超时。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Mapping, Optional, Sequence

from ..config import DEFAULT_PARSE_TIMEOUT_SECONDS
from .base import ParseOutcome

# MinerU CLI 的 ``--tier`` 允许取值；配置词汇（见 config.TIERS）与之完全一致，
# 因此不需要任何别名表，这里只做一次校验性质的直通。
CLI_TIERS = ("flash", "basic", "standard", "advanced")

ARCHIVE_FORMAT = "zip"
# 后端直接使用（不经 RunConfig）时的时限默认值，与配置默认值同一个数。
DEFAULT_TIMEOUT_SECONDS = DEFAULT_PARSE_TIMEOUT_SECONDS
_ENGINE_LINE = re.compile(r"pages=(\d+),\s*cost=([0-9.]+)s,\s*speed=([0-9.]+)\s*page/s")
_ERROR_TAIL_CHARS = 400


def to_cli_tier(tier: str) -> str:
    """校验档位并原样返回；非法档位抛出点明允许取值的 :class:`ValueError`。"""
    if tier not in CLI_TIERS:
        raise ValueError(f"unknown tier: {tier!r} (valid: {', '.join(CLI_TIERS)})")
    return tier


class MineruBackend:
    """用 ``mineru-kit parse`` 做提取的后端。"""

    name = "mineru"

    def __init__(
        self,
        command: str = "mineru-kit",
        *,
        model_source: str = "modelscope",
        ocr_mode: str = "auto",
        extra_args: Sequence[str] = (),
        timeout: Optional[float] = DEFAULT_TIMEOUT_SECONDS,
        env: Optional[Mapping[str, str]] = None,
    ):
        self.command = command
        self.model_source = model_source
        self.ocr_mode = ocr_mode
        self.extra_args = list(extra_args)
        self.timeout = timeout
        self.env = dict(env) if env is not None else None

    def is_available(self) -> bool:
        """``command`` 是否能在当前环境里找到。"""
        return _resolve_command(self.command) is not None

    def build_command(
        self, inputs: Sequence[Path], output_dir: Path, tier: str
    ) -> tuple[str, ...]:
        """构造完整 argv；对外公开便于测试与日志。"""
        if not inputs:
            raise ValueError("mineru backend needs at least one input file")
        argv = [
            self.command,
            "parse",
            *(str(Path(path)) for path in inputs),
            "-o",
            str(Path(output_dir)),
            "-f",
            ARCHIVE_FORMAT,
            "--tier",
            to_cli_tier(tier),
        ]
        if self.ocr_mode:
            argv.extend(["--ocr-mode", self.ocr_mode])
        argv.extend(self.extra_args)
        return tuple(argv)

    def build_env(self) -> dict[str, str]:
        """子进程环境：继承当前环境并固定模型来源。"""
        env = dict(os.environ) if self.env is None else dict(self.env)
        if self.model_source:
            env["MINERU_MODEL_SOURCE"] = self.model_source
        return env

    def parse(
        self,
        inputs: Sequence[Path],
        output_dir: Path,
        tier: str,
        *,
        timeout: Optional[float] = None,
    ) -> ParseOutcome:
        """调用一次 ``mineru-kit parse``，返回实际写出的 ``<stem>.zip``。

        ``timeout`` 覆盖本次调用的时限（秒），不给则用构造时设定的 ``self.timeout``；
        编排层按这次调用的页数算出时限后从这里传进来。
        """
        argv = self.build_command(inputs, output_dir, tier)
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)
        limit = self.timeout if timeout is None else timeout
        started = time.monotonic()
        try:
            completed = subprocess.run(
                list(argv),
                capture_output=True,
                text=True,
                # 绝不继承父进程的 stdin：后台任务框架给的是 socket，MinerU 读它会
                # 一直等下去（生产事故：12 s CPU / 34 min，0% CPU）。
                stdin=subprocess.DEVNULL,
                env=self.build_env(),
                timeout=limit,
            )
        except FileNotFoundError as exc:
            raise ValueError(f"mineru command not found: {self.command}") from exc
        except subprocess.TimeoutExpired:
            seconds = time.monotonic() - started
            return ParseOutcome(
                command=argv,
                returncode=-1,
                seconds=seconds,
                error=f"timed out after {limit:g}s",
                timed_out=True,
            )
        seconds = time.monotonic() - started
        text = f"{completed.stdout or ''}{completed.stderr or ''}"
        pages, engine_seconds, engine_speed = _parse_engine_log(text)
        return ParseOutcome(
            command=argv,
            returncode=completed.returncode,
            outputs=_collect_outputs(inputs, output_path),
            seconds=seconds,
            error="" if completed.returncode == 0 else _error_tail(text),
            pages=pages,
            engine_seconds=engine_seconds,
            engine_speed=engine_speed,
        )


def _error_tail(text: str, limit: int = _ERROR_TAIL_CHARS) -> str:
    """截取日志末尾作为错误摘要：折叠空白并限制长度。"""
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return "…" + collapsed[-limit:]


def _collect_outputs(inputs: Sequence[Path], output_dir: Path) -> dict[Path, Path]:
    """按键于主干名的约定，收集本次调用真正写出的归档。"""
    outputs: dict[Path, Path] = {}
    for raw in inputs:
        path = Path(raw)
        candidate = output_dir / f"{path.stem}.zip"
        if candidate.is_file():
            outputs[path] = candidate
    return outputs


def _parse_engine_log(text: str) -> tuple[Optional[int], Optional[float], Optional[float]]:
    """从引擎日志里取 ``pages=<n>, cost=<s>, speed=<p> page/s`` 的合计值。"""
    pages = 0
    seconds = 0.0
    found = False
    for match in _ENGINE_LINE.finditer(text):
        found = True
        pages += int(match.group(1))
        seconds += float(match.group(2))
    if not found:
        return None, None, None
    speed = pages / seconds if seconds > 0 else None
    return pages, seconds, speed


def _resolve_command(command: str) -> Optional[str]:
    """解析后端可执行文件路径：含分隔符者按路径查，否则走 ``PATH``。"""
    if os.sep in command or (os.altsep and os.altsep in command):
        return command if Path(command).is_file() else None
    return shutil.which(command)
