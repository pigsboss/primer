# -*- coding: utf-8 -*-
"""金句清单：从候选表里**确定性地**筛"读起来像结论"的句子，递给**人**去拍。

幻灯片到最后一轮总缺几句能立住整页的话。金句不能由工具写（写了就是代笔），但工具可以
把候选表里**像结论**的那些先挑出来，省掉人从头翻 258 条的眼力活。所以这一步只递料：
读 ``candidates.md``，按一组写死的信号打分，排序，落成 ``golden.md``；**不联网、不调用
模型、不写回骨架**。哪一句真的用、放进哪一帧，由人把句子粘进那一帧的 ``picks``（自由
文本形状），或写进章／节的 ``hint``。

怎么判"像结论"（都用写死的表，确定性、可复算）：

* **长度 ≤ :data:`GOLDEN_MAX_CHARS` 字**：金句是一屏一眼看完的，长句一律落选。
* **断言／标志词**（:data:`ASSERTION_SIGNALS`）：``首次``／``唯一``／``决定性``／
  ``分水岭``／``从此``／``断档``／``临界``／``最…之一``／``改写``／``终结``……这些词
  来自"读起来像结论"的语感——一句下判断的话才会用它们，铺陈与提问不会。命中的词按权重
  累加，也是报告里"命中信号"那一列。
* **含数字加权**：年份、量级是结论的锚点，给 ``＋1``；四位数以上的数字再 ``＋1``。
* **减分**：列表标记（``一是``／``第二``／``首先``……）、括号套括号、连接符结尾
  （破折号、分号、冒号、逗号）——这些是"未完待续"或"分点罗列"的形状，不像一句能独立
  立住的金句。

**硬落选**三种：长度超标、纯罗马字（一个中日韩字母都没有）、整句是疑问（以 ``？``
结尾）。其余按分数取，分数低于 :data:`GOLDEN_MIN_SCORE` 的也落选——宁可少给几条，不要
用一堆"信号不足"的句子把清单稀释掉。

用法::

    python -m primer.slides golden _primer/slides/review-seminar/outline.yaml --top 12

输出 ``golden.md`` 与 ``outline.yaml`` 同目录；stdout 给摘要（候选总数、入选数、前 N 条
与几条被自动跳过的反例）。圈中的句子直接粘进对应帧的 ``picks``（自由文本形状），或写进
章／节 ``hint``。
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Mapping, Optional, Sequence, Tuple

from . import candidates as cand
from .terminal import clip
from .validate import CANDIDATES_NAME, load_outline_document

GOLDEN_NAME = "golden.md"
DEFAULT_TOP = 12
# 金句的长度上限：一屏一眼看完，长过这个数就不是金句而是段落了。
GOLDEN_MAX_CHARS = 34
# 入选的分数线。命中一个权重 2 的断言词、或数字加一个权重 2 的信号才够得上。
GOLDEN_MIN_SCORE = 2

# 断言／标志词表：来源是"读起来像结论"的语感——一句下判断的话才会用它们。
# (模式, 人读标签, 权重)。模式是正则；标点类的模式不跨句读。
ASSERTION_SIGNALS: Tuple[Tuple[str, str, int], ...] = (
    ("首次", "首次", 3),
    ("首个", "首个", 3),
    ("首例", "首例", 3),
    ("首颗", "首颗", 3),
    ("唯一", "唯一", 3),
    ("决定性", "决定性", 3),
    ("分水岭", "分水岭", 3),
    ("分界", "分界", 3),
    ("里程碑", "里程碑", 3),
    ("断档", "断档", 3),
    ("开创", "开创", 2),
    ("改写", "改写", 2),
    ("终结", "终结", 2),
    ("从此", "从此", 2),
    ("再未", "再未", 2),
    ("临界", "临界", 2),
    ("转折", "转折", 2),
    ("确立", "确立", 2),
    ("奠定", "奠定", 2),
    ("领先", "领先", 2),
    ("标志", "标志", 2),
    ("证明", "证明", 2),
    ("证实", "证实", 2),
    ("推翻", "推翻", 2),
    ("划定", "划定", 2),
    ("重启", "重启", 2),
    ("空白", "空白", 2),
    ("尚无", "尚无", 2),
    ("尚未", "尚未", 2),
    ("从未", "从未", 2),
    ("最[^，。；！？]{0,12}之一", "最…之一", 2),
    # 第二组：结论性谓词／名词。语气不如第一组重，但同样"读起来像结论"——提示一句在
    # 收敛判断、划定形状，而不是在铺陈。这一组是可调的：清单太窄就加词，太宽就减词。
    ("关键在", "关键在", 2),
    ("本质", "本质", 2),
    ("转向", "转向", 2),
    ("转变", "转变", 2),
    ("收敛", "收敛", 2),
    ("分岔", "分岔", 2),
    ("空档", "空档", 2),
    ("缺口", "缺口", 2),
    ("门槛", "门槛", 2),
    ("锚点", "锚点", 2),
    ("重塑", "重塑", 2),
    ("催生", "催生", 2),
    ("垄断", "垄断", 2),
    ("稀缺", "稀缺", 2),
    ("可定量", "可定量", 2),
    ("递增", "递增", 2),
    ("递减", "递减", 2),
    ("升级", "升级", 2),
    ("汇流", "汇流", 2),
    ("反推", "反推", 2),
    ("收官", "收官", 2),
    ("定型", "定型", 2),
    ("重构", "重构", 2),
    ("印证", "印证", 2),
)

DIGIT_RE = re.compile(r"\d")
BIG_NUMBER_RE = re.compile(r"\d{4,}")
# 列表标记：行首编号或"第X，／其X，／启示X："这类分点开头，或句中的"一是／其二是／第二，／首先"。
LIST_MARK_RE = re.compile(
    r"(?:^\s*(?:[（(]?[一二三四五六七八九十0-9]+[）)、.．]"
    r"|第[一二三四五六七八九十]+[，、]"
    r"|其[一二三四五六七八九十]+[，、]"
    r"|启示[一二三四五六七八九十]+[：:，、]"
    r"|规律[一二三四五六七八九十]+[：:，、]"
    r"|[·•](?:\s|$))"
    r"|[，。；：](?:[一二三四五六七八九十]+是|其[一二三四五六七八九十]|"
    r"第[一二三四五六七八九十]|首先|其次|再次|最后))"
)
# 括号套括号：一层里还嵌一层。
NESTED_PAREN_RE = re.compile(r"[（(][^（()）]*[（(]")
# 中日韩字母（与 prose 同一套口径：标点不算）。
_CJK_RE = re.compile(
    r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U0003ffff]"
)
# 结尾是连接符（破折号、分号、冒号、逗号）：说明句子被切断了、或还在罗列，
# 不是一句能独立立住的话。与"破折号结尾"同一类形状。
DASH_END = ("—", "–", "―", "-", "；", ":", "：", "，", ",")

# 减分的名目与分值，写出来是给人核对口径用的。
LIST_MARK_PENALTY = 2
NESTED_PAREN_PENALTY = 2
DASH_END_PENALTY = 2


@dataclass(frozen=True)
class GoldenEntry:
    """"可当金句"的一条候选：原句、出处指针、命中的信号与分数。"""

    score: int
    identifier: str
    pointer: str
    text: str
    signals: Tuple[str, ...]


@dataclass(frozen=True)
class Evaluation:
    """一条候选的判定：入选时 ``entry`` 有值，否则 ``reason`` 说明为什么落选。"""

    entry: Optional[GoldenEntry]
    reason: str = ""


@dataclass(frozen=True)
class GoldenResult:
    """一次 ``golden`` 的结果：候选总数、入选顺序表、落选样本与产物路径。"""

    total: int
    selected: Tuple[GoldenEntry, ...]
    skipped: Tuple[Tuple[str, str], ...]
    path: Path
    top: int


def evaluate(text: str, identifier: str = "", pointer: str = "") -> Evaluation:
    """判一条候选能不能当金句：返回 :class:`Evaluation`（纯函数，确定性）。

    ``reason`` 是人读短语，落选时写进 stdout 的反例那一栏。
    """
    stripped = text.strip()
    if not stripped:
        return Evaluation(None, "空句")
    if len(stripped) > GOLDEN_MAX_CHARS:
        return Evaluation(None, f"长句（{len(stripped)} 字）")
    if _CJK_RE.search(stripped) is None:
        return Evaluation(None, "无中文（纯罗马字）")
    if stripped.rstrip().endswith(("？", "?")):
        return Evaluation(None, "纯疑问")
    hits: List[str] = []
    score = 0
    for pattern, label, weight in ASSERTION_SIGNALS:
        if re.search(pattern, stripped):
            hits.append(label)
            score += weight
    if DIGIT_RE.search(stripped):
        hits.append("数字")
        score += 1
    if BIG_NUMBER_RE.search(stripped):
        score += 1
    if LIST_MARK_RE.search(stripped):
        score -= LIST_MARK_PENALTY
    if NESTED_PAREN_RE.search(stripped):
        score -= NESTED_PAREN_PENALTY
    if stripped.rstrip().endswith(DASH_END):
        score -= DASH_END_PENALTY
    if score < GOLDEN_MIN_SCORE:
        return Evaluation(None, f"信号不足（分数 {score}）")
    entry = GoldenEntry(
        score=score,
        identifier=identifier,
        pointer=pointer,
        text=stripped,
        signals=tuple(hits),
    )
    return Evaluation(entry)


def select_golden(
    rows: Mapping[str, cand.CandidateRow],
) -> Tuple[List[GoldenEntry], List[Tuple[str, str]]]:
    """整张候选表 → （按分数降序的入选表，落选样本）。

    排序确定性：分数降序 → 句长升序（短句更"金"）→ 候选 id 升序。落选样本按候选 id。
    """
    ranked: List[GoldenEntry] = []
    skipped: List[Tuple[str, str]] = []
    for identifier in sorted(rows):
        row = rows[identifier]
        outcome = evaluate(row.display, identifier, row.pointer)
        if outcome.entry is not None:
            ranked.append(outcome.entry)
        else:
            skipped.append((outcome.reason, row.display))
    ranked.sort(key=lambda entry: (-entry.score, len(entry.text), entry.identifier))
    return ranked, skipped


def _table_cell(text: str) -> str:
    """表格单元格里的竖线转义，免得把一行句子拆成两列。"""
    return text.replace("|", "\\|").replace("\n", " ")


def golden_markdown(result: GoldenResult, title: str = "") -> str:
    """整份 ``golden.md`` 的正文。"""
    heading = "# 金句清单" + (f" · {title}" if title else "")
    lines = [
        heading,
        "",
        f"来源：`{CANDIDATES_NAME}`（共 {result.total} 条候选）。筛选：长度 ≤"
        f"{GOLDEN_MAX_CHARS} 字、有断言信号（首次、唯一、决定性、分水岭、从此、断档、"
        "临界、最…之一、改写、终结……）、非疑问句；"
        f"分数 = 信号词权重之和 ＋ 数字权重（四位数以上再加一） − 列表标记／套括号／"
        "连接符结尾减分。",
        "",
        "圈中的句子可直接粘进对应帧的 `picks`（自由文本形状），或写进章／节 `hint`。"
        "工具只递料，用哪一句由人拍。",
        "",
        "| 句子 | 候选 id | 章/节指针 | 命中信号 | 分数 |",
        "|:--|:--|:--|:--|--:|",
    ]
    for entry in result.selected:
        signals = "、".join(entry.signals) if entry.signals else "—"
        lines.append(
            f"| {_table_cell(entry.text)} | {entry.identifier} | "
            f"{_table_cell(entry.pointer)} | {signals} | {entry.score} |"
        )
    return "\n".join(lines) + "\n"


def write_golden(result: GoldenResult, title: str = "") -> Path:
    """把清单写到 ``result.path``，返回该路径。"""
    result.path.write_text(golden_markdown(result, title), encoding="utf-8")
    return result.path


def _sample_skips(
    skipped: Sequence[Tuple[str, str]], limit: int = 3
) -> List[Tuple[str, str]]:
    """落选样本里挑几条**理由各不相同**的，让反例覆盖多种落选原因。"""
    picked: List[Tuple[str, str]] = []
    seen: set = set()
    for reason, text in skipped:
        key = reason.split("（")[0]
        if key in seen:
            continue
        seen.add(key)
        picked.append((reason, text))
        if len(picked) >= limit:
            break
    if not picked:
        return list(skipped[:limit])
    return picked


def report_lines(result: GoldenResult) -> List[str]:
    """stdout 摘要：候选总数、入选数、前 N 条与几条被自动跳过的反例。"""
    lines = [
        f"金句清单：{CANDIDATES_NAME} 共 {result.total} 条候选，"
        f"入选 {len(result.selected)} 条"
        f"（长度 ≤{GOLDEN_MAX_CHARS} 字、分数 ≥{GOLDEN_MIN_SCORE}、非疑问句）",
        f"输出：{result.path}",
    ]
    top = result.top if result.top > 0 else len(result.selected)
    shown = result.selected[:top]
    lines.append(f"前 {len(shown)} 条：" if shown else "没有入选的句子。")
    for index, entry in enumerate(shown, 1):
        lines.append(
            f"  {index}. [{entry.score}] {clip(entry.text, 46)}"
            f"  —— {entry.identifier}（{entry.pointer}）"
        )
    samples = _sample_skips(result.skipped)
    if samples:
        lines.append("自动跳过的反例：")
        for reason, text in samples:
            lines.append(f"  - {reason}：{clip(text, 40)}")
    return lines


def run(
    outline_path: Path,
    project_root: Path = Path("."),
    *,
    top: int = DEFAULT_TOP,
    candidates_path: Optional[Path] = None,
) -> GoldenResult:
    """跑一次 ``golden``：读候选表、筛、写 ``golden.md``，返回结果。**不联网、不写骨架**。

    ``project_root`` 只为与其它子命令参数一致而收下；候选表默认取 outline 同目录的
    ``candidates.md``，不经过工程根。
    """
    outline_path = Path(outline_path).expanduser().resolve()
    if not outline_path.is_file():
        raise FileNotFoundError(f"outline not found: {outline_path}")
    table_path = (
        Path(candidates_path).expanduser().resolve()
        if candidates_path
        else outline_path.parent / CANDIDATES_NAME
    )
    if not table_path.is_file():
        raise FileNotFoundError(f"candidate table not found: {table_path}")
    rows = cand.parse_markdown_table(table_path.read_text(encoding="utf-8"))
    selected, skipped = select_golden(rows)
    result = GoldenResult(
        total=len(rows),
        selected=tuple(selected),
        skipped=tuple(skipped),
        path=outline_path.parent / GOLDEN_NAME,
        top=top,
    )
    write_golden(result, _deck_title(outline_path))
    return result


def _deck_title(outline_path: Path) -> str:
    """骨架里的 ``deck.title``；读不出就不写标题（金句清单照旧产出）。"""
    try:
        document = load_outline_document(outline_path)
    except Exception:  # noqa: BLE001 - 标题只是装饰，读不出不该挡住清单
        return ""
    deck = document.get("deck")
    if isinstance(deck, Mapping):
        title = deck.get("title")
        if isinstance(title, str):
            return title
    return ""


# ---------------------------------------------------------------- 命令行


GOLDEN_SUMMARY = (
    "deterministically shortlist 'golden quote' candidates from candidates.md (<=34 chars, "
    "assertion signals, digits weighted, list/nested-paren/trailing-dash penalised) and write "
    "golden.md next to the outline; no network, no model, never touches outline.yaml. The tool "
    "only hands over the material: paste a highlighted sentence into a frame's picks (free-text "
    "shape) or into a chapter/section hint."
)

GOLDEN_EPILOG = (
    "examples:\n"
    "  python -m primer.slides golden _primer/slides/review-seminar/outline.yaml\n"
    "  python -m primer.slides golden outline.yaml --top 20\n"
    "candidates.md defaults to the file next to the outline; skipped sentences (too long, a "
    "pure question, no CJK, or too few signals) are listed with their reason on stdout"
)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """golden 的全部参数（独立入口与 ``__main__.py`` 的子命令共用同一份）。"""
    parser.add_argument("outline", metavar="OUTLINE", help="path to the outline.yaml of the deck")
    parser.add_argument(
        "--project-root", default=".", metavar="DIR",
        help="project root, accepted for parity with the other subcommands (default .)",
    )
    parser.add_argument(
        "--top", type=int, default=DEFAULT_TOP, metavar="N",
        help=f"how many top entries to print (default {DEFAULT_TOP}); golden.md keeps them all",
    )
    parser.add_argument(
        "--candidates", metavar="FILE",
        help="candidate table to read; defaults to candidates.md next to the outline",
    )


def command(args: argparse.Namespace) -> int:
    """跑一次 golden 并打印摘要，返回退出码。``main`` 与 ``__main__.py`` 共用这一段。"""
    try:
        result = run(
            Path(args.outline),
            Path(getattr(args, "project_root", ".") or "."),
            top=int(getattr(args, "top", DEFAULT_TOP)),
            candidates_path=(
                Path(args.candidates) if getattr(args, "candidates", None) else None
            ),
        )
    except FileNotFoundError as exc:
        print(f"slides golden error: {exc}", file=sys.stderr)
        return 2
    for line in report_lines(result):
        print(line)
    return 0


def build_parser() -> argparse.ArgumentParser:
    """独立运行 ``python -m primer.slides.golden`` 时的解析器（选项名与说明一律英文）。"""
    parser = argparse.ArgumentParser(
        prog="python -m primer.slides golden",
        description=GOLDEN_SUMMARY,
        epilog=GOLDEN_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_arguments(parser)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """命令行入口。"""
    args = build_parser().parse_args(argv)
    return command(args)


__all__ = [
    "ASSERTION_SIGNALS",
    "DEFAULT_TOP",
    "GOLDEN_MAX_CHARS",
    "GOLDEN_MIN_SCORE",
    "GOLDEN_NAME",
    "Evaluation",
    "GoldenEntry",
    "GoldenResult",
    "evaluate",
    "golden_markdown",
    "report_lines",
    "run",
    "select_golden",
]


if __name__ == "__main__":
    raise SystemExit(main())
