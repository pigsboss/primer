# -*- coding: utf-8 -*-
"""输出边界：产物根目录，以及"把路径记成相对工程根"的换算（全项目唯一实现）。

primer 把工程目录一律当作**只读**输入：它自己写出的东西只能落在
``<工程根>/_primer/<功能>/`` 里，绝不在工程目录的别处落文件。为了让产物在工程
被搬移或复制到别的机器之后仍然可读，凡是记进产物的路径都写成相对**工程根**的
形式，不落绝对路径。

成书、文献、参考文献三个功能包共用这一份实现（``primer.literature.paths`` 与
``primer.book.paths`` 只是再导出）。同一套规则曾被各自独立实现过两份，没有大小写
回退的那一份把 ``/Users/huo/Documents/Kimi/…`` 这样的绝对路径留在了产物里——策略
只有一份，"改一处、三处生效"才是常态。

:func:`relative_to_root` 是路径字段的换算入口。直接比较算不出相对路径时，用
``realpath`` 归一化后再比一次（同一个目录被两种前缀指向时仍能算对，macOS 的
``/var`` 与 ``/private/var`` 就是这种情形），最后再比一次 casefold 后的形式
（macOS/Windows 的文件系统不区分大小写，而下载账本里的 ``saved`` 完全可能是
另一种大小写拼法：生产里的 ``/Users/huo/Documents/Kimi/…`` 对上小写 ``kimi``
的工程根，就是这里漏了才把绝对路径写进产物）；路径确实落在工程根之外时退回原
字符串——这是唯一会记录绝对路径的情形，因为此时根本不存在"相对工程根"的写法。

:func:`strip_root_prefix` 处理另一半：路径被塞进自由文本（错误信息、源 markdown
里的链接）的情形，此时只有前缀可删，没有结构可换算；大小写同样不敏感。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional, Union

__all__ = [
    "FEATURES",
    "OUTPUT_DIRNAME",
    "PathLike",
    "normalize_project_root",
    "output_dir_for",
    "relative_to_root",
    "resolve_project_root",
    "strip_root_prefix",
]

PathLike = Union[str, "os.PathLike[str]"]

# 唯一的产物根目录名，以及它下面允许的功能子目录。
OUTPUT_DIRNAME = "_primer"
FEATURES = ("literature", "references", "book", "claims")


def normalize_project_root(value: PathLike) -> Path:
    """规范化工程根：相对路径按当前工作目录补齐，但不解析符号链接。

    不解析符号链接是有意的：``output_dir`` 由工程根拼出，解析会让调用者拿到的
    路径与他传进来的那个不是同一个字符串，测试与日志都会变得难以对照。
    """
    root = Path(value).expanduser()
    return root if root.is_absolute() else Path.cwd() / root


def resolve_project_root(value: PathLike) -> Path:
    """规范化工程根并校验它确实存在，供没有独立校验步骤的调用方使用。"""
    root = normalize_project_root(value)
    if not root.is_dir():
        raise ValueError(f"project root is not an existing directory: {root}")
    return root


def output_dir_for(project_root: PathLike, feature: str = "book") -> Path:
    """某功能在工程里的产物根 ``<工程根>/_primer/<功能>``。

    ``feature`` 取 :data:`FEATURES` 之一，给错就报错而不是悄悄造一个新目录。缺省
    ``"book"``：成书包只有一种产物，它的调用方历来只写 ``output_dir_for(root)``。
    """
    if feature not in FEATURES:
        raise ValueError(f"unknown feature: {feature} (expected one of: {', '.join(FEATURES)})")
    return normalize_project_root(project_root) / OUTPUT_DIRNAME / feature


def relative_to_root(path: PathLike, project_root: Optional[PathLike]) -> str:
    """把 ``path`` 记成相对工程根的斜杠分隔字符串。

    已经是相对路径的输入原样返回——它本来就相对某个根，再换算一次只会被当成
    相对当前工作目录，反而算错。工程根为 ``None``、或路径确实在工程根之外时，
    退回原字符串。
    """
    candidate = Path(path)
    if project_root is None or not candidate.is_absolute():
        return candidate.as_posix()
    root = normalize_project_root(project_root)
    attempts = (
        (str(candidate), str(root)),
        (os.path.realpath(candidate), os.path.realpath(root)),
    )
    for left, right in attempts:
        try:
            return Path(left).relative_to(right).as_posix()
        except ValueError:
            pass
        folded = _strip_prefix_folded(left, right)
        if folded is not None:
            return Path(folded).as_posix()
    return candidate.as_posix()


def _strip_prefix_folded(candidate: str, root: str) -> Optional[str]:
    """大小写不同、但实为同一目录时切掉前缀，返回相对部分；不匹配返回 ``None``。

    只在后面紧跟一个路径分隔符时才算命中，因此 ``/a/Proj`` 不会把 ``/a/projects``
    当成自己的子目录。切片位置按**原字符串**里根的长度算：casefold 可能改变字符
    长度（``ß`` → ``ss``），拿折叠后的长度去切原文会错位。
    """
    prefix = f"{root}{os.sep}"
    if not candidate.casefold().startswith(prefix.casefold()):
        return None
    return candidate[len(root) + len(os.sep) :]


def strip_root_prefix(text: str, project_root: Optional[PathLike]) -> str:
    """抹掉自由文本里出现的工程根前缀，使残留路径变成相对写法。

    后端（MinerU）会把输入文件的绝对路径连同自己的日志一起写进错误信息，源
    markdown 里的"本地存档"链接也常常是绝对路径，这两段文本都会原样进产物。
    这里只做一件事：删掉 ``<工程根>/`` 这个前缀；与工程根无关的绝对路径一律不动，
    因为无从判断它相对谁。大小写不敏感，理由与 :func:`relative_to_root` 相同。
    """
    if project_root is None or not text:
        return text
    root = normalize_project_root(project_root)
    prefixes = {str(root), os.path.realpath(root)}
    for prefix in prefixes:
        if len(prefix) > 1:
            pattern = re.escape(f"{prefix}{os.sep}")
            text = re.sub(pattern, "", text, flags=re.IGNORECASE)
    return text
