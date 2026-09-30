# -*- coding: utf-8 -*-
"""终端表格的列宽工具：按**显示宽度**算，一个汉字占两列。

``outline`` 的分配表与 ``check`` 的对账表都要把中英混排的章名对齐。``len`` 数的是
码位，数不出"第八章"比"第八章标题"窄多少；这里统一按东亚宽字符两列的规矩算，两张
表因此能对齐。函数只是字符算术，不碰产物，也不写盘。
"""

from __future__ import annotations

import unicodedata
from typing import List


def display_width(text: str) -> int:
    """终端显示宽度：东亚宽字符与全角字符各占两列。"""
    return sum(2 if unicodedata.east_asian_width(char) in ("W", "F") else 1 for char in text)


def clip(text: str, width: int) -> str:
    """按显示宽度截断，截断时补省略号。"""
    if display_width(text) <= width:
        return text
    kept: List[str] = []
    used = 0
    for char in text:
        step = 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
        if used + step > width - 1:
            break
        kept.append(char)
        used += step
    return "".join(kept) + "…"


def pad(text: str, width: int, align: str = "left") -> str:
    """按显示宽度补空格。"""
    room = max(0, width - display_width(text))
    padding = " " * room
    return text + padding if align == "left" else padding + text
