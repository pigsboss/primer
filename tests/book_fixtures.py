# -*- coding: utf-8 -*-
"""成书测试的共享素材：在 ``tmp_path`` 下拼一本微型书（清单 + 源文件）。

微型书的正文刻意覆盖 markdown 子集的各个分支（标题、列表、代码块、引用块、
图片＋两行题注、表格＋题注、行内强调与链接），带上手写的图／表编号与正文交叉
引用，并附一个附录篇；另有一条应被丢弃的编辑性尾注。插图写的是一个真实的最小
PNG——集成测试真的让排版引擎去嵌入它，假字节会让驱动在 libpng 里崩掉。
"""

import base64
from pathlib import Path

ONE_MD = """# 第一篇 科学篇：示例

> 版本：v1（示例）
> 引用：方括号编号对应参考文献库。

---

## 1.1 导言

正文一段，引用 [3][1]、区间 [2]–[4]、已归一区间 [6–8] 与单体 [12]。

![图 1-1](图/one.png)

*图 1-1　示例图*

*资料来源*

*说明行保留原样。*

正文里提一次图 1-1 与表 1-1。

**（一）加粗段落。** 含 `code`、$x^2$ 与 α 符号，见 [站点](https://example.org/a_b)。

### 1.1.1 小节

- 项目一
- 项目二

1. 有序一
2. 有序二

> 引用一行

```
verbatim 行
├── 树形行
```

**表 1-1　示例表**

| 列一 | 列二 |
|---|---|
| a | b |
| c | d |

*（第一篇完）*
"""

TWO_MD = """# 附录 A　示例附录

## A.1 第一节

附录正文，引用 [1]，见图 A-1 与表 A-1。

![图 A-1](图/appendix.png)

*图 A-1　附录示例图*

**表 A-1　附录示例表**

| 列一 |
|---|
| a |
"""

REFS_MD = """# 参考文献库（示例）

> 版本：v1

---

## A 战略报告与规划

[1] First entry.
[2] Second entry, continued
on the next line.
[3] Third entry.
[4] Fourth entry.
[5] Fifth entry.
[6] Sixth entry.
[7] Seventh entry.
[8] Eighth entry.
[12] Twelfth entry.
"""

MANIFEST = """\
book:
  title: 微型书
  subtitle: （示例）
  tagline: 一句话
  institution: 测试组
  date: 某年某月某日

source_root: sources

bibliography:
  file: refs.md
  section_title: 总参考文献列表

output:
  jobname: tiny
  engine: xelatex
  engine_runs: 2
  min_pdf_bytes: 1
  emit_markdown: true

fonts:
  cjk_main: Songti SC
  main: Times New Roman

typography:
  body_font_size: "4"

drop_lines:
  - '^\\*（第[一二三]篇.*完.*）\\*$'

heading_schemes:
  plain:
    rules:
      - match: '^## 1\\.(\\d+)\\s*(.*)'
        template: '## 第 {g1} 章　{g2}'
        numbers: [g1]
      - match: '^### 1\\.(\\d+)\\.(\\d+)\\s*(.*)'
        template: '### {g1}.{g2}　{g3}'
        numbers: [g1, g2]
    replacements:
      - ['1.2 节', '第 2 章']

volumes:
  - id: V1
    title: 第一篇　科学篇
    standalone: true
    sources:
      - file: one.md
        scheme: plain
  - id: VA
    title: 附录 A　示例附录
    part_label: appA
    appendix: true
    sources:
      - file: two.md

body_order: [V1, VA, "@bibliography"]

front_matter:
  foreword_heading: 凡　例
  foreword:
    - '一、示例凡例，引用编号上限 [{max_reference}]。'
    - '二、示例末段。'
  appendix_catalog:
    - title: 附录 A　示例附录
      label: appA
"""


ONE_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def write_sources(root: Path, one_md: str = ONE_MD, two_md: str = TWO_MD) -> Path:
    """写出 sources/ 下的源文件与文献库，返回 sources 目录。"""
    sources = root / "sources"
    sources.mkdir(parents=True, exist_ok=True)
    (sources / "one.md").write_text(one_md, encoding="utf-8")
    (sources / "two.md").write_text(two_md, encoding="utf-8")
    (sources / "refs.md").write_text(REFS_MD, encoding="utf-8")
    (sources / "图").mkdir(parents=True, exist_ok=True)
    for name in ("one.png", "appendix.png"):
        (sources / "图" / name).write_bytes(ONE_PIXEL_PNG)
    return sources


def write_manifest(root: Path, text: str = MANIFEST) -> Path:
    """写出清单文件，返回其路径。"""
    path = root / "book.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def tiny_book(root: Path, manifest_text: str = MANIFEST, one_md: str = ONE_MD) -> Path:
    """写出微型书（源文件 + 清单），返回清单路径。"""
    write_sources(root, one_md=one_md)
    return write_manifest(root, manifest_text)
