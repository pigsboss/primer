# -*- coding: utf-8 -*-
"""幻灯片测试的共享素材：在 ``tmp_path`` 下拼一份"小 deck"（源 markdown + deck.yaml）。

比真 deck 小，但结构齐全：两篇正常材料（``## `` 开章、``### `` 开节，第二篇的章号从 3
起数）、一篇附录（整篇一章，``## `` 是节、``### `` 是小节）、两幅插图、一条应被丢弃的
编辑性尾注。

**没有成书产物**：幻灯片的结构由这些源文件与 ``deck.yaml`` 直接复算——测试因此不依赖
``_primer/book/`` 下的任何东西（这正是这一轮要断掉的耦合）。
"""

import base64
from pathlib import Path

VOLUME_ONE = """\
# 第一篇　甲篇

> 体例说明，抬头部分。

---

## 1.1 第一章标题

第一句带一个引用 [1]。第二句是别的话。

**第二段有粗体**，并且提到了 2024 年。还有一句。

![图 1-1](图/one.png)

*图 1-1　示例图*

### 1.1.1 节一

本节第一句。

### 1.1.2 节二

**第一，粗体开头的句子。** 后半句。

| 表头 |
|---|
| 值 |

![图 1-2](图/two.png)

*图 1-2　第二幅示例图*

> 引用块不该成为候选

---

## 1.2 第二章标题

三十年后回看，问题清单更长。以下三条是判据。

### 1.2.1 节一

第二章第一节的第一句。

### 1.2.2 节二

综上，三条规律成立。

*（第一篇完）*
"""

VOLUME_TWO = """\
# 第二篇　乙篇

## 2.1 第三章标题

第三章的第一句 [2]。

### 2.1.1 节一

一个没有句号的段落；
"""

APPENDIX = """\
# 附录 A　附录标题

## A.1 附录节一

附录的第一句 [3]。

![图 A-1](图/appendix.png)

*图 A-1　附录示例图*

### A.1.1 附录小节

**小节里的粗体句**。

## A.2 附录节二

附录第二节的句子。
"""

DECK_SPEC = """\
version: 1

book:
  title: 合成书
  subtitle: （测试）
  institution: 测试组

source_root: 成果文件

drop_lines:
  - '^\\*（第[一二三]篇.*完.*）\\*$'

pointers: section
split:
  chapter: 2
  section: 3
numbering:
  label: 第{cn}章
  appendix_label: 附录 {number}

sources:
  - id: V1
    title: 第一篇　甲篇
    path: 第一篇_甲篇.md
    chapter_start: 1
  - id: V2
    title: 第二篇　乙篇
    path: 第二篇_乙篇.md
    chapter_start: 3
  - id: VA
    title: 附录 A　附录标题
    path: 附录A.md
    chapter_start: A
    appendix: true
"""

DECK_NAME = "review-seminar"

# 一幅真实的最小 PNG：集成测试真的让排版引擎去嵌它，假字节会让驱动在 libpng 里崩掉。
ONE_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
IMAGES = ("one.png", "two.png", "appendix.png")


def write_sources(
    root: Path,
    one: str = VOLUME_ONE,
    two: str = VOLUME_TWO,
    appendix: str = APPENDIX,
) -> Path:
    """写出 ``成果文件/`` 下的三个源文件与插图，返回那个目录。"""
    sources = root / "成果文件"
    (sources / "图").mkdir(parents=True, exist_ok=True)
    (sources / "第一篇_甲篇.md").write_text(one, encoding="utf-8")
    (sources / "第二篇_乙篇.md").write_text(two, encoding="utf-8")
    (sources / "附录A.md").write_text(appendix, encoding="utf-8")
    for name in IMAGES:
        (sources / "图" / name).write_bytes(ONE_PIXEL_PNG)
    return sources


def write_spec(root: Path, text: str = DECK_SPEC, deck: str = DECK_NAME) -> Path:
    """写出 ``<工程根>/_primer/slides/<deck>/deck.yaml``，返回它的路径。"""
    deck_dir = root / "_primer" / "slides" / deck
    deck_dir.mkdir(parents=True, exist_ok=True)
    path = deck_dir / "deck.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def deck_tree(root: Path, spec_text: str = DECK_SPEC) -> Path:
    """写出源文件与规格，返回规格路径。"""
    write_sources(root)
    return write_spec(root, spec_text)
