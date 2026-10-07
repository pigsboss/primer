# -*- coding: utf-8 -*-
"""由图稿 yaml 抽文字清单、拼装中文提示词。

出图的提示词不是散文，而是**逐字清单**：模型画的是图，字错了没有人会替它改。所以提示词
必须由机器从图稿 yaml 里**抽**出来，而不是手抄——手抄一次就会有第二个版本。两块：

* :func:`collect_labels` —— 递归走 ``content`` 子树，按序收集所有非空字符串叶子，
  返回 ``(路径, 文字)`` 清单。路径是给人（与台账）看的定位符，例如
  ``content.left_arm[0].title``。
* :func:`assemble_spec` —— 把标题、版式段与这份清单拼成一段中文提示词：引言句 ＋
  版式段 ＋ **文字清单（逐字，不得增删）** 段 ＋ 硬性要求段。

**清单里只出现文字本身，不出现 YAML 路径**：提示词是给图像模型看的，往里塞
``content.left_arm[0].title`` 这种英文 token，等于请它把这些字也画进画面。路径留在
返回值里，供人核对"哪一条来自哪个字段"。

图稿 yaml 里的 ``meta``／``layout``／``provenance``／``caption_draft`` 四块是**过程性**
字段（引用、草稿、来源），不构成画面上的字，默认不进清单；``content`` 子树不存在时
才退化成"全文减去这四块"。这两个默认值都可以用参数改，但改之前先想清楚为什么。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence, Union

import yaml

__all__ = [
    "CONTENT_KEY",
    "DEFAULT_FRAME_RULE",
    "DEFAULT_STYLE_RULE",
    "SKIP_KEYS",
    "assemble_spec",
    "collect_labels",
    "default_hard_rules",
    "fig_dict",
    "label_texts",
    "load_figure_yaml",
]

CONTENT_KEY = "content"
# 过程性字段：引用、版式草稿、来源、图注草稿——都不是画面上的字。
SKIP_KEYS = ("meta", "layout", "provenance", "caption_draft")

# 默认硬性要求里"风格／画幅"那两条（其余三条是文字纪律）。具体风格由调用方按图改，
# 这两条给的是底线口径。
DEFAULT_STYLE_RULE = ("风格：柔和光晕、细腻渐变，不要霓虹灯效果；配色协调统一、高分辨率，"
                      "信息图质感，允许少量写实点缀。")
DEFAULT_FRAME_RULE = ("画幅：整幅按版式段指定的比例与横竖方向成图，不得改变；主体居中留白适度。")

DEFAULT_TEXT_RULES = (
    "全图**只用简体中文**；所有文字必须**逐字准确、清晰可读**，无错字、无乱码、无缺笔、无变形；"
    "卡片／条目标题略大且加粗，次要文字小一号；深色底上用白字或浅色字。",
    "**除上述文字清单中的文字外，画面中不得出现任何其他文字、数字、英文单词或水印**；"
    "清单中的文字一字不得增、不得删、不得改写。",
)


def collect_labels(fig: Mapping[str, Any], *, root: str = CONTENT_KEY,
                   skip: Sequence[str] = SKIP_KEYS) -> list[tuple[str, str]]:
    """递归收集文字清单，返回 ``(路径, 文字)``，**按 yaml 里的出现顺序**。

    ``root`` 给的键存在时只走那棵子树（路径带上根键名），否则走全文但跳过 ``skip``
    里的键。非字符串叶子（数字、布尔、``None``）不是"画面上的字"，不进清单；
    字符串两侧空白去掉后为空的不进清单，内部换行原样保留。
    """
    if not isinstance(fig, Mapping):
        raise TypeError("figure must be a mapping, got: %r" % type(fig).__name__)
    if root in fig:
        labels: list[tuple[str, str]] = []
        _walk(fig[root], root, set(skip), labels)
        return labels
    labels = []
    _walk(fig, "", set(skip), labels)
    return labels


def _walk(node: Any, path: str, skip: set, out: list[tuple[str, str]]) -> None:
    if isinstance(node, str):
        text = node.strip()
        if text:
            out.append((path, text))
        return
    if isinstance(node, Mapping):
        for key, value in node.items():
            name = str(key)
            if name in skip:
                continue
            _walk(value, "%s.%s" % (path, name) if path else name, skip, out)
        return
    if isinstance(node, (list, tuple)):
        for index, value in enumerate(node):
            _walk(value, "%s[%d]" % (path, index), skip, out)


def label_texts(labels: Iterable[Union[str, tuple]]) -> list[str]:
    """清单归一成纯文字列表：``(路径, 文字)`` 取文字，字符串原样（两侧空白去掉）。"""
    texts = []
    for item in labels:
        text = item[1] if isinstance(item, (tuple, list)) and len(item) >= 2 else item
        stripped = str(text).strip()
        if stripped:
            texts.append(stripped)
    return texts


def default_hard_rules() -> list[str]:
    """默认硬性要求：文字纪律（只用简体中文、逐字准确、清单外无任何文字）＋风格＋画幅。"""
    return [*DEFAULT_TEXT_RULES, DEFAULT_STYLE_RULE, DEFAULT_FRAME_RULE]


def assemble_spec(title: str, layout_text: str, labels: Iterable[Union[str, tuple]],
                  hard_rules: Optional[Sequence[str]] = None) -> str:
    """拼装中文提示词：引言句 ＋ 版式段 ＋ 文字清单段 ＋ 硬性要求段。

    ``labels`` 收 :func:`collect_labels` 的 ``(路径, 文字)`` 或纯字符串；
    ``hard_rules=None`` 用 :func:`default_hard_rules`，给了就**完全**照给的写
    （替换而不是追加——要加一条就先把它并进自己的列表）。
    """
    name = str(title).strip()
    if not name:
        raise ValueError("title must not be empty")
    texts = label_texts(labels)
    rules = list(default_hard_rules() if hard_rules is None else hard_rules)
    lines = ["画一幅科研报告信息图：「%s」。" % name, ""]
    lines.append("**版式**：")
    lines.append(str(layout_text).strip() or "（未给版式说明；请按标题与文字清单自定版式，"
                                            "保持信息层级清晰。）")
    lines.append("")
    lines.append("**文字清单（逐字，不得增删）**：")
    if texts:
        for index, text in enumerate(texts, start=1):
            lines.append("%d. %s" % (index, text))
    else:
        lines.append("（无指定文字：画面中不得出现任何文字。）")
    lines.append("")
    lines.append("**硬性要求（最重要）**：")
    for index, rule in enumerate(rules, start=1):
        lines.append("%d. %s" % (index, rule))
    return "\n".join(lines).rstrip() + "\n"


def load_figure_yaml(path: Union[str, Path]) -> Any:
    """读图稿 yaml；解析失败抛 :class:`ValueError`（点名文件，消息英文）。"""
    target = Path(path)
    try:
        return yaml.safe_load(target.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise ValueError("cannot parse figure yaml %s: %s" % (target, exc)) from exc


def fig_dict(doc: Any) -> Mapping[str, Any]:
    """图稿 yaml 的顶层容器：有 ``fig:`` 键就用它，否则整份 document 就是图。"""
    if isinstance(doc, Mapping) and isinstance(doc.get("fig"), Mapping):
        return doc["fig"]
    if not isinstance(doc, Mapping):
        raise ValueError("figure yaml must be a mapping (or carry a 'fig' mapping)")
    return doc
