# -*- coding: utf-8 -*-
"""参考文献库：解析 ``[n] 条目`` 形式的编号文献表。

条目正文按 ``[n]`` 起始行与其续行（非空、不以 ``#`` 起始）拼成一行；条目编号
就是引用编号，全书沿用。原始文本另存一份，用于生成书末的总参考文献列表。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Mapping, Optional

ENTRY_RE = re.compile(r"^\[(\d+)\]\s*(.*)")


class Bibliography:
    """编号文献库。"""

    def __init__(self, text: str = "", entries: Optional[Mapping[int, str]] = None) -> None:
        self.text = text
        self.entries: Dict[int, str] = dict(entries or {})

    @classmethod
    def load(cls, path: Path) -> "Bibliography":
        """读取文献库文件（UTF-8）。"""
        return cls.from_text(Path(path).read_text(encoding="utf-8"))

    @classmethod
    def from_text(cls, text: str) -> "Bibliography":
        """解析文献库文本：``[n]`` 起始行与其续行拼成一条。"""
        entries: Dict[int, str] = {}
        current: Optional[int] = None
        buffer: List[str] = []
        for line in text.splitlines():
            matched = ENTRY_RE.match(line)
            if matched:
                if current is not None:
                    entries[current] = " ".join(buffer).strip()
                current, buffer = int(matched.group(1)), [matched.group(2)]
            elif current is not None and line.strip() and not line.startswith("#"):
                buffer.append(line.strip())
        if current is not None:
            entries[current] = " ".join(buffer).strip()
        return cls(text=text, entries=entries)

    def get(self, number: int) -> Optional[str]:
        """取某条目的正文，缺失时返回 ``None``。"""
        return self.entries.get(number)

    @property
    def max_number(self) -> int:
        """最大编号（库为空时返回 0）。"""
        return max(self.entries) if self.entries else 0
