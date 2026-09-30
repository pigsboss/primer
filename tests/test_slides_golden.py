# -*- coding: utf-8 -*-
"""``primer.slides golden``：从候选表里确定性筛"像结论"的金句，递料给人拍板。

正例证明断言句入选、分数与信号对得上；反例证明长句、纯疑问、纯罗马字、列表标记句被挡住
或被减分。最后两条走一遍 ``run`` 与命令行：产物落在 outline 同目录，stdout 有摘要与反例。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from primer.slides import golden as gld
from primer.slides.__main__ import main

CANDIDATES_MD = """\
# 示例演示 · 幻灯片主题句候选表

## 四、逐章候选

### 第一章　示例章

| id | 类型 | 信号 | 引用 | 字数 | 页面指针 | 句子 |
|:--|:--|:--|--:|--:|:--|:--|
| s1-0p01 | 数据 | DS | 0 | 18 | p.17（第一章 起始页） | 1995 年是两个科学时代的分水岭。 |
| s1-0p02 | 断言 | BS | 0 | 14 | p.17（第一章 起始页） | 行星科学为什么重要？ |
| s1-0p03 | 断言 | S | 0 | 41 | p.17（第一章 起始页） | 这一句很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长，放不进金句表。 |
| s1-0p04 | 断言 | S | 0 | 20 | p.17（第一章 起始页） | HR 8799 b is a planet. |
"""

OUTLINE_MD = """\
deck:
  title: 示例演示
  slides: 12
chapters: []
"""


@pytest.fixture
def table(tmp_path):
    (tmp_path / "outline.yaml").write_text(OUTLINE_MD, encoding="utf-8")
    (tmp_path / "candidates.md").write_text(CANDIDATES_MD, encoding="utf-8")
    return tmp_path


def test_an_assertion_sentence_is_shortlisted():
    outcome = gld.evaluate("1995 年是两个科学时代的分水岭。")

    assert outcome.entry is not None
    assert "分水岭" in outcome.entry.signals
    # 分水岭 3 + 数字 1 + 四位数 1995 再加 1
    assert outcome.entry.score == 5


def test_a_pure_question_is_skipped():
    outcome = gld.evaluate("行星科学为什么重要？")

    assert outcome.entry is None
    assert outcome.reason == "纯疑问"


def test_a_sentence_longer_than_the_limit_is_skipped():
    outcome = gld.evaluate("长" * 40 + "。")

    assert outcome.entry is None
    assert outcome.reason == "长句（41 字）"


def test_a_sentence_without_cjk_is_skipped():
    outcome = gld.evaluate("HR 8799 b is a planet.")

    assert outcome.entry is None
    assert outcome.reason == "无中文（纯罗马字）"


def test_a_list_marker_deducts_from_a_signal_sentence():
    plain = gld.evaluate("首次实现这一跨越。")
    listed = gld.evaluate("首次实现这一跨越，其次意义在于口径。")
    leading = gld.evaluate("第三，首次实现这一跨越。")

    assert plain.entry is not None and plain.entry.score == 3
    # 首次 3 − 列表标记 2 = 1，低于入选线
    assert listed.entry is None
    assert listed.reason == "信号不足（分数 1）"
    assert leading.entry is None and leading.reason == "信号不足（分数 1）"


def test_a_trailing_dash_is_penalised():
    outcome = gld.evaluate("首次实现这一跨越—")

    assert outcome.entry is None and outcome.reason == "信号不足（分数 1）"


def test_select_golden_ranks_by_score_then_length_then_id():
    rows = {
        "s1-0p02": _row("s1-0p02", "行星科学为什么重要？"),
        "s1-0p01": _row("s1-0p01", "1995 年是两个科学时代的分水岭。"),
        "s1-0p05": _row("s1-0p05", "从此再未出现断档。"),  # 从此 2 + 再未 2 + 断档 3 = 7
        "s1-0p06": _row("s1-0p06", "从此再未出现断档，而是一路推进。"),  # 同分但更长，排后
    }

    selected, skipped = gld.select_golden(rows)

    assert [entry.identifier for entry in selected] == ["s1-0p05", "s1-0p06", "s1-0p01"]
    assert [reason for reason, _ in skipped] == ["纯疑问"]


def test_run_writes_golden_md_next_to_the_outline(table, capsys):
    result = gld.run(table / "outline.yaml", top=12)

    assert result.total == 4
    assert result.path == table / "golden.md"
    written = result.path.read_text(encoding="utf-8")
    assert "| 句子 | 候选 id | 章/节指针 | 命中信号 | 分数 |" in written
    assert "1995 年是两个科学时代的分水岭。" in written
    assert "s1-0p01" in written

    summary = "\n".join(gld.report_lines(result))
    assert "共 4 条候选，入选 1 条" in summary
    assert "自动跳过的反例：" in summary
    assert "纯疑问" in summary and "长句" in summary


def test_the_cli_golden_subcommand_writes_and_reports(table, capsys):
    code = main(["golden", str(table / "outline.yaml"), "--top", "3"])

    captured = capsys.readouterr()
    assert code == 0
    assert "[5] 1995 年是两个科学时代的分水岭。" in captured.out
    assert (table / "golden.md").is_file()


def test_golden_missing_outline_is_reported(tmp_path, capsys):
    code = main(["golden", str(tmp_path / "nope.yaml")])

    assert code == 2
    assert "slides golden error:" in capsys.readouterr().err


def _row(identifier, text):
    from primer.slides.candidates import CandidateRow

    return CandidateRow(
        id=identifier,
        type="断言",
        signal_codes="S",
        citations=0,
        length=len(text),
        pointer="p.17（第一章 起始页）",
        display=text,
    )
