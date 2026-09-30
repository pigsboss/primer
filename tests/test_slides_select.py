# -*- coding: utf-8 -*-
"""``primer.slides select``：模式判定、只填不覆盖、回信容错、提炼句数字校验、账目与续跑。

这一轮**不联网**：所有用例都注入一个假传输层（:data:`primer.slides.client.Transport`），
回信是构造好的字符串；密钥用假的，机器级配置指到一个空的 ``XDG_CONFIG_HOME``，
所以既碰不到真凭据，也碰不到 ``~/.config``。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

from primer.slides import candidates as cand
from primer.slides import client as client_mod
from primer.slides import select as sel
from primer.slides import validate as validate_mod
from primer.slides.__main__ import main
from primer.slides.client import SelectCallError
from primer.slides.plan import DEFAULT_CAPACITY_PER_PAGE, SlidesError

CANDIDATES_MD = """\
# 示例演示 · 幻灯片主题句候选表

## 四、逐章候选

### 第一章　示例章

| id | 类型 | 信号 | 引用 | 字数 | 页面指针 | 句子 |
|:--|:--|:--|--:|--:|:--|:--|
| s1-1p01 | 断言 | BCS | 2 | 18 | p.5（§1.1 起始页） | **第一条主题句。** |
| s1-1p02 | 数据 | DS | 1 | 22 | p.5（§1.1 起始页） | 这个口径是 2024 年定的。 |
| s1-1p03 | 框定 | F | 0 | 16 | p.6（§1.2 起始页） | 以下三条是判据。 |
| s1-1p04 | 小结 | CS | 3 | 20 | p.6（§1.2 起始页） | 综上，三条规律成立。 |

### 第二章　带方向的章

| id | 类型 | 信号 | 引用 | 字数 | 页面指针 | 句子 |
|:--|:--|:--|--:|--:|:--|:--|
| s2-1p01 | 断言 | BS | 1 | 15 | p.10（§2.1 起始页） | 第二句主题句。 |
| s2-1p02 | 数据 | D | 2 | 24 | p.10（§2.1 起始页） | 到了 2030 年这个数翻倍。 |
| s2-1p03 | 断言 | S | 0 | 14 | p.10（§2.1 起始页） | 第三句主题句。 |
| s2-1p04 | 小结 | CS | 1 | 19 | p.10（§2.1 起始页） | 综上，该结论成立。 |

### 第三章　只填不覆盖

| id | 类型 | 信号 | 引用 | 字数 | 页面指针 | 句子 |
|:--|:--|:--|--:|--:|:--|:--|
| s3-1p01 | 断言 | BS | 1 | 17 | p.20（§3.1 起始页） | 已经有人填过的句子。 |
| s3-1p02 | 数据 | D | 2 | 21 | p.20（§3.1 起始页） | 一共有 12 个项目在跑。 |
| s3-1p03 | 断言 | S | 0 | 15 | p.21（§3.2 起始页） | 第三句主题句。 |
| s3-1p04 | 小结 | CS | 1 | 18 | p.21（§3.2 起始页） | 综上，重写的结论。 |

### 第四章　会失败的章

| id | 类型 | 信号 | 引用 | 字数 | 页面指针 | 句子 |
|:--|:--|:--|--:|--:|:--|:--|
| s4-1p01 | 断言 | S | 0 | 14 | p.30（§4.1 起始页） | 第四句主题句。 |
"""

CONFIG_YAML = """\
providers:
  demo:
    base_url: https://api.example.invalid/v1
    key_env: PRIMER_DEMO_API_KEY
roles:
  select:
    provider: demo
    model: demo-select
  distill:
    provider: demo
    model: demo-distill
"""

KEY = "secret-demo-key"


def _frame(picks=(), review="pending", note=""):
    return {"picks": list(picks), "review": review, "note": note}


def demo_outline():
    """四章：1 无方向、2 节方向、3 只填不覆盖的四种情形、4 用来演示失败隔离。"""
    return {
        "deck": {
            "title": "示例演示",
            "deck": "demo",
            "presenter": "",
            "occasion": "",
            "slides": 25,
            "backup": 2,
            "capacity_per_page": 384,
            "min_per_chapter": 1,
            "max_per_chapter": 5,
        },
        "chapters": [
            {
                "chapter": "1",
                "label": "第一章",
                "title": "示例章",
                "start_page": 5,
                "end_page": 8,
                "printed_pages": 4,
                "budget": 2,
                "hint": "",
                "sections": [
                    {"level": "section", "number": "1.1", "title": "节一", "page": 5,
                     "speak": True, "hint": ""},
                    {"level": "section", "number": "1.2", "title": "节二", "page": 7,
                     "speak": True, "hint": ""},
                ],
                "frames": [_frame(), _frame()],
                "figure": None,
                "notes": "",
            },
            {
                "chapter": "2",
                "label": "第二章",
                "title": "带方向的章",
                "start_page": 10,
                "end_page": 12,
                "printed_pages": 3,
                "budget": 1,
                "hint": "",
                "sections": [
                    {"level": "section", "number": "2.1", "title": "节一", "page": 10,
                     "speak": True, "hint": "只讲结论，不要过程"},
                ],
                "frames": [_frame()],
                "figure": None,
                "notes": "",
            },
            {
                "chapter": "3",
                "label": "第三章",
                "title": "只填不覆盖",
                "start_page": 20,
                "end_page": 24,
                "printed_pages": 5,
                "budget": 4,
                "hint": "",
                "sections": [
                    {"level": "section", "number": "3.1", "title": "节一", "page": 20,
                     "speak": True, "hint": ""},
                    {"level": "section", "number": "3.2", "title": "节二", "page": 21,
                     "speak": True, "hint": ""},
                    {"level": "section", "number": "3.3", "title": "节三", "page": 22,
                     "speak": True, "hint": ""},
                    {"level": "section", "number": "3.4", "title": "节四", "page": 23,
                     "speak": True, "hint": ""},
                ],
                "frames": [
                    _frame(note="这一页只讲方法"),          # 可写（有方向）
                    _frame(picks=["s3-1p01"]),              # 跳过：picks 非空
                    _frame(review="ok"),                    # 跳过：review ok
                    _frame(review="redo", note="保留我"),    # 只在 --redo 时可写
                ],
                "figure": None,
                "notes": "",
            },
            {
                "chapter": "4",
                "label": "第四章",
                "title": "会失败的章",
                "start_page": 30,
                "end_page": 31,
                "printed_pages": 2,
                "budget": 1,
                "hint": "",
                "sections": [
                    {"level": "section", "number": "4.1", "title": "节一", "page": 30,
                     "speak": True, "hint": ""},
                ],
                "frames": [_frame()],
                "figure": None,
                "notes": "",
            },
        ],
    }


@pytest.fixture
def deck(tmp_path):
    """一个自足的小工程：项目级配置 + 骨架 + 候选表，全在 tmp_path 下。"""
    root = tmp_path / "工程"
    directory = root / "_primer" / "slides" / "demo"
    directory.mkdir(parents=True)
    (root / "_primer" / "config.yaml").write_text(CONFIG_YAML, encoding="utf-8")
    (directory / "candidates.md").write_text(CANDIDATES_MD, encoding="utf-8")
    outline = directory / "outline.yaml"
    outline.write_text(
        "".join(["# 头部注释：不许重排\n", _dump(demo_outline())]), encoding="utf-8"
    )
    return root, outline


def _dump(document):
    return yaml.safe_dump(
        document,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        width=10**6,
    )


def _environ(tmp_path, key=KEY):
    """不碰真机器配置、也不碰真凭据的一套环境变量。"""
    env = {"XDG_CONFIG_HOME": str(tmp_path / "no-machine-config")}
    if key is not None:
        env["PRIMER_DEMO_API_KEY"] = key
    return env


# ---------------------------------------------------------------- 假传输层


def ids_in(prompt):
    """提示词候选表里的 id，按出现次序（每行一个）。"""
    return [
        line.split("|")[1].strip()
        for line in prompt.splitlines()
        if line.startswith("| s") and line.count("|") >= 6
    ]


def frame_indexes(prompt):
    return [int(value) for value in re.findall(r"^- 帧 (\d+)（", prompt, re.M)]


def chat_completion(content, prompt_tokens=30, completion_tokens=12, reasoning=0):
    return json.dumps(
        {
            "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "completion_tokens_details": {"reasoning_tokens": reasoning},
            },
        },
        ensure_ascii=False,
    ).encode("utf-8")


def pool_answer(prompt, count=4):
    """按提示词里的候选池回一份规规矩矩的 `{"frames": [...]}`。"""
    ids = ids_in(prompt)[:count]
    frames = [
        {"index": index, "picks": [{"id": identifier} for identifier in ids], "why": "按信号排"}
        for index in frame_indexes(prompt)
    ]
    return json.dumps({"frames": frames}, ensure_ascii=False)


class FakeTransport:
    """可调用的假传输层：记下每次请求，按提示词给回信。"""

    def __init__(self, answer=None):
        self.answer = answer or pool_answer
        self.prompts = []
        self.requests = 0

    def __call__(self, request):
        self.requests += 1
        body = json.loads(request.body.decode("utf-8"))
        prompt = body["messages"][0]["content"]
        self.prompts.append(prompt)
        result = self.answer(prompt)
        if isinstance(result, Exception):
            raise result
        return chat_completion(result)

    @property
    def chapters(self):
        return [prompt.splitlines()[2] for prompt in self.prompts]


def _frames(outline):
    document = yaml.safe_load(outline.read_text(encoding="utf-8"))
    return {chapter["chapter"]: chapter["frames"] for chapter in document["chapters"]}


# ---------------------------------------------------------------- 模式判定


def _chapter(**overrides):
    chapter = {
        "chapter": "1",
        "label": "第一章",
        "title": "示例章",
        "start_page": 5,
        "end_page": 8,
        "printed_pages": 4,
        "budget": 2,
        "hint": "",
        "sections": [
            {"number": "1.1", "title": "节一", "page": 5, "hint": ""},
            {"number": "1.2", "title": "节二", "page": 7, "hint": ""},
        ],
        "frames": [_frame(), _frame()],
    }
    chapter.update(overrides)
    return chapter


def test_the_note_outranks_the_section_and_the_chapter():
    chapter = _chapter(hint="章方向")
    chapter["sections"][0]["hint"] = "节方向"
    chapter["frames"][0]["note"] = " 帧方向 "
    resolved = sel.frame_mode(chapter, 0, 2)
    assert (resolved.mode, resolved.hint, resolved.source) == (sel.MODE_HINT, "帧方向", sel.SOURCE_NOTE)


def test_the_section_hint_outranks_the_chapter_hint():
    chapter = _chapter(hint="章方向")
    chapter["sections"][0]["hint"] = "节方向"
    # 第 0 帧的代表页落在 §1.1（p.5–6）里。
    assert sel.frame_section(chapter, 0, 2)["number"] == "1.1"
    resolved = sel.frame_mode(chapter, 0, 2)
    assert (resolved.mode, resolved.hint, resolved.source) == (
        sel.MODE_HINT,
        "节方向",
        sel.SOURCE_SECTION,
    )


def test_the_chapter_hint_covers_frames_whose_section_has_none():
    chapter = _chapter(hint="章方向")
    chapter["sections"][0]["hint"] = "节方向"
    # 第 1 帧的代表页落在 §1.2（p.7–8）里，那一节没有方向。
    assert sel.frame_section(chapter, 1, 2)["number"] == "1.2"
    resolved = sel.frame_mode(chapter, 1, 2)
    assert (resolved.mode, resolved.hint, resolved.source) == (
        sel.MODE_HINT,
        "章方向",
        sel.SOURCE_CHAPTER,
    )


def test_no_hint_at_all_is_mode_two():
    resolved = sel.frame_mode(_chapter(), 0, 2)
    assert (resolved.mode, resolved.hint, resolved.source) == (
        sel.MODE_JUDGE,
        "",
        sel.SOURCE_NONE,
    )


def test_a_chapter_without_sections_still_falls_back_to_the_chapter_hint():
    chapter = _chapter(hint="章方向", sections=[])
    assert sel.frame_section(chapter, 0, 2) is None
    resolved = sel.frame_mode(chapter, 0, 2)
    assert (resolved.mode, resolved.hint, resolved.source) == (
        sel.MODE_HINT,
        "章方向",
        sel.SOURCE_CHAPTER,
    )


def test_forced_mode_overrides_the_decision_and_mode_two_drops_the_hint():
    chapter = _chapter(hint="章方向")
    assert sel.frame_mode(chapter, 0, 2, forced=sel.MODE_JUDGE).hint == ""
    assert sel.frame_mode(chapter, 0, 2, forced=sel.MODE_JUDGE).mode == sel.MODE_JUDGE
    forced_three = sel.frame_mode(chapter, 0, 2, forced=sel.MODE_DISTILL)
    assert (forced_three.mode, forced_three.hint) == (sel.MODE_DISTILL, "章方向")
    forced_one = sel.frame_mode(_chapter(), 0, 2, forced=sel.MODE_HINT)
    assert (forced_one.mode, forced_one.hint) == (sel.MODE_HINT, "")


def test_an_unknown_mode_is_rejected():
    with pytest.raises(SlidesError):
        sel.run(Path("nowhere.yaml"), Path("."), mode=9)


# ---------------------------------------------------------------- 只填不覆盖


def test_only_empty_pending_frames_are_written(deck, tmp_path):
    root, outline = deck
    transport = FakeTransport()
    result = sel.run(outline, root, transport=transport, environ=_environ(tmp_path))
    assert result.written
    frames = _frames(outline)["3"]
    assert frames[0]["picks"] and frames[0]["note"] == "这一页只讲方法"
    assert frames[1]["picks"] == ["s3-1p01"]          # 非空：不动
    assert frames[2]["picks"] == []                    # ok：不动
    assert frames[3]["picks"] == []                    # redo 未给 --redo：不动
    assert frames[3]["note"] == "保留我"
    skipped = [record.outcome for record in result.outcomes[2].records]
    assert skipped == ["filled", "skipped", "skipped", "skipped"]
    assert [record.reason for record in result.outcomes[2].records][1:] == ["picks", "ok", "redo"]


def test_redo_is_rewritten_only_with_the_flag(deck, tmp_path):
    root, outline = deck
    transport = FakeTransport()
    result = sel.run(outline, root, transport=transport, redo=True, environ=_environ(tmp_path))
    assert result.written
    frame = _frames(outline)["3"][3]
    assert frame["picks"], "redo 帧在 --redo 下应被重写"
    assert frame["note"] == "保留我", "重写要保留那一帧的 note"
    assert frame["review"] == "redo", "select 只碰 picks，review 原样"


def test_force_rewrites_non_empty_picks_but_never_review_ok(deck, tmp_path):
    root, outline = deck
    before = _frames(outline)["3"]
    transport = FakeTransport()
    result = sel.run(outline, root, transport=transport, force=True, environ=_environ(tmp_path))
    frames = _frames(outline)["3"]
    assert frames[0]["picks"] != []
    assert frames[1]["picks"] != before[1]["picks"], "--force 要重写非空 picks"
    assert frames[2]["picks"] == [], "review ok 永不触碰"
    assert frames[3]["picks"] != [], "--force 包含 redo"
    assert result.writable_frames == 2 + 1 + 3 + 1
    assert "--force 已开" in "\n".join(sel.report_lines(result))


def test_a_frame_is_skipped_when_the_chapter_has_no_candidates(tmp_path):
    root = tmp_path / "empty"
    directory = root / "_primer" / "slides" / "demo"
    directory.mkdir(parents=True)
    (root / "_primer" / "config.yaml").write_text(CONFIG_YAML, encoding="utf-8")
    (directory / "candidates.md").write_text(
        "### 第一章\n\n| id | 类型 | 信号 | 引用 | 字数 | 页面指针 | 句子 |\n|:--|:--|:--|--:|--:|:--|:--|\n",
        encoding="utf-8",
    )
    (directory / "outline.yaml").write_text(_dump(demo_outline()), encoding="utf-8")
    transport = FakeTransport()
    result = sel.run(
        directory / "outline.yaml", root, transport=transport, only=["1"],
        environ=_environ(tmp_path),
    )
    assert transport.requests == 0
    assert [record.reason for record in result.outcomes[0].records] == [
        sel.SKIP_NO_CANDIDATES
    ] * 2


# ---------------------------------------------------------------- 回信容错


def test_fenced_chatty_balanced_replies_are_salvaged():
    payload = '{"frames": [{"index": 0, "picks": [{"id": "s1-1p01"}], "why": "x"}]}'
    assert client_mod.parse_json_reply("```json\n" + payload + "\n```")["frames"]
    assert client_mod.parse_json_reply("好的，结果如下：\n" + payload + "\n希望有帮助。")["frames"]
    nested = '{"frames": [{"index": 0, "picks": [{"id": "s1-1p01"}], "why": "括号 } 在字符串里"}]}'
    assert client_mod.parse_json_reply("前言 {不是 JSON} 后语 " + nested)["frames"][0]["why"] == (
        "括号 } 在字符串里"
    )
    assert client_mod.parse_json_reply('[{"index": 0, "picks": []}]')[0]["index"] == 0


def test_an_unparseable_reply_is_a_reply_error():
    with pytest.raises(client_mod.SelectReplyError):
        client_mod.parse_json_reply("模型没有给 JSON，只说了几句话。")


def test_a_chatty_reply_still_fills_the_frames(deck, tmp_path):
    root, outline = deck

    def answer(prompt):
        return "下面是结果：\n```json\n" + pool_answer(prompt) + "\n```\n以上。"

    transport = FakeTransport(answer)
    result = sel.run(outline, root, transport=transport, only=["1"], environ=_environ(tmp_path))
    assert result.written
    assert all(frame["picks"] for frame in _frames(outline)["1"])


def test_the_reply_index_selects_the_frame_and_a_bare_id_is_accepted():
    payload = {
        "frames": [
            {"index": 2, "picks": ["s1-1p02"], "why": "靠 index 定位"},
            {"frame": 0, "picks": {"id": "s1-1p01"}, "why": "单个 pick 也认"},
        ]
    }
    replies = sel.parse_frames_reply(payload)
    assert [(reply.index, reply.picks) for reply in replies] == [
        (2, ("s1-1p02",)),
        (0, ({"id": "s1-1p01"},)),
    ]
    assert sel.parse_frames_reply({"frames": "坏了"}) == []


# ---------------------------------------------------------------- 提炼句的数字校验


@pytest.fixture
def rows():
    return cand.parse_markdown_table(CANDIDATES_MD)


def test_a_distilled_sentence_whose_digits_are_all_in_the_sources_is_kept(rows):
    kept, dropped = sel.clean_picks(
        [{"text": "2024 年定的口径后来翻了倍。", "derived_from": ["s1-1p02"], "verbatim": False}],
        rows,
        sel.MODE_DISTILL,
    )
    assert dropped == []
    # 落盘前经 prose.normalize_pick_text 规整：数字与单位（年）之间的空格去掉。
    assert kept == [{"text": "2024年定的口径后来翻了倍。", "derived_from": ["s1-1p02"]}]


def test_a_distilled_sentence_with_a_digit_outside_the_sources_is_dropped(rows):
    kept, dropped = sel.clean_picks(
        [{"text": "到了 2043 年这个数翻倍。", "derived_from": ["s1-1p02"], "verbatim": False}],
        rows,
        sel.MODE_DISTILL,
    )
    assert kept == []
    assert dropped == [("digit-mismatch", "2043")]


def test_mode_one_and_two_only_accept_original_sentences(rows):
    kept, dropped = sel.clean_picks(
        [
            {"id": "s1-1p01"},
            {"text": "自己改写的一句话。", "derived_from": ["s1-1p01"], "verbatim": False},
            {"text": "**第一条主题句。**", "derived_from": ["s1-1p01"], "verbatim": True},
        ],
        rows,
        sel.MODE_HINT,
    )
    # 第三条与第一条逐字相同，去重之后只留一个。
    assert kept == ["s1-1p01"]
    assert dropped == [("text-not-in-pool", "自己改写的一句话。")]


def test_picks_are_deduplicated_and_capped(rows):
    kept, dropped = sel.clean_picks(
        [{"id": "s1-1p01"}, {"id": "s1-1p01"}, {"id": "s1-1p02"}, {"id": "s1-1p03"},
         {"id": "s1-1p04"}, {"id": "s2-1p01"}, {"id": "s2-1p02"}],
        rows,
        sel.MODE_JUDGE,
    )
    assert kept == ["s1-1p01", "s1-1p02", "s1-1p03", "s1-1p04", "s2-1p01"]
    assert ("truncated", ">5") in dropped


def test_a_pick_pointing_outside_the_pool_is_dropped(rows):
    kept, dropped = sel.clean_picks(
        [
            {"id": "s9-9p99"},
            "s9-9p98",
            {"text": "有出处但出处不在表里。", "derived_from": ["s9-9p97"], "verbatim": False},
            {"text": "有出处但没写。", "verbatim": False},
            {"text": ""},
            "   ",
            42,
        ],
        rows,
        sel.MODE_DISTILL,
    )
    assert kept == []
    assert [code for code, _ in dropped] == [
        "unknown-id",
        "unknown-id",
        "derived-unknown",
        "no-derived-from",
        "shape",
        "empty",
        "shape",
    ]


def test_a_distilled_sentence_matching_a_source_verbatim_becomes_that_id(rows):
    kept, dropped = sel.clean_picks(
        [{"text": "综上，三条规律成立。", "derived_from": ["s1-1p04"], "verbatim": True}],
        rows,
        sel.MODE_DISTILL,
    )
    assert kept == ["s1-1p04"]
    assert dropped == []


def test_mode_three_uses_the_distill_role_and_fills_distilled_picks(deck, tmp_path):
    root, outline = deck

    def answer(prompt):
        ids = ids_in(prompt)
        frames = [
            {
                "index": index,
                "picks": [
                    {"text": "2024 年定的口径翻了倍。", "derived_from": [ids[1]], "verbatim": False},
                    {"text": "到了 2099 年就翻倍。", "derived_from": [ids[1]], "verbatim": False},
                ],
                "why": "提炼",
            }
            for index in frame_indexes(prompt)
        ]
        return json.dumps({"frames": frames}, ensure_ascii=False)

    transport = FakeTransport(answer)
    result = sel.run(
        outline, root, mode=sel.MODE_DISTILL, only=["1"], transport=transport,
        environ=_environ(tmp_path),
    )
    assert result.endpoints["distill"].model == "demo-distill"
    picks = _frames(outline)["1"][0]["picks"]
    assert picks == [{"text": "2024年定的口径翻了倍。", "derived_from": ["s1-1p02"]}]
    lines = (outline.parent / sel.SELECT_LOG_NAME).read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[0])
    assert record["role"] == "distill" and record["model"] == "demo-distill"
    assert record["mode"] == sel.MODE_DISTILL
    assert record["reason"] == "digit-mismatch"
    # 数字对不上的那一条在报告里计数。
    report = "\n".join(sel.report_lines(result))
    assert "丢弃的 picks" in report
    assert "句子里的数字没在 derived_from 的原文里出现：2 条" in report


# ---------------------------------------------------------------- dry-run


def test_dry_run_writes_nothing_and_needs_no_key(deck, tmp_path):
    root, outline = deck
    before = outline.read_bytes()
    transport = FakeTransport()
    # 环境里**没有**密钥：dry-run 连 api_key 都不读。
    result = sel.run(
        outline, root, dry_run=True, transport=transport, environ=_environ(tmp_path, key=None)
    )
    assert result.dry_run and not result.written
    assert transport.requests == 0
    assert outline.read_bytes() == before
    assert not (outline.parent / sel.SELECT_LOG_NAME).exists()
    lines = sel.report_lines(result)
    text = "\n".join(lines)
    assert "一个字节都不写" in text
    assert "会发 4 次请求" in text
    assert "prompt" in text and "token" in text
    assert "跳过帧 3" in text
    assert "picks 非空" in text and "review 已是 ok" in text and "未给 --redo" in text


def test_dry_run_cli_writes_nothing(deck, tmp_path, capsys):
    root, outline = deck
    before = outline.read_bytes()
    code = main(["select", str(outline), "--project-root", str(root), "--dry-run"])
    printed = capsys.readouterr().out
    assert code == 0
    assert outline.read_bytes() == before
    assert "--dry-run" in printed and "会发" in printed


# ---------------------------------------------------------------- 失败隔离与退出码


def _failing_transport(needle="第四章"):
    def answer(prompt):
        if needle in prompt:
            raise SelectCallError("endpoint returned an empty message for the demo")
        return pool_answer(prompt)

    return FakeTransport(answer)


def test_a_failing_chapter_does_not_stop_the_others(deck, tmp_path):
    root, outline = deck
    transport = _failing_transport()
    result = sel.run(outline, root, transport=transport, environ=_environ(tmp_path))
    assert [outcome.plan.number for outcome in result.failures] == ["4"]
    frames = _frames(outline)
    assert all(frame["picks"] for frame in frames["1"]), "第一章照样填好"
    assert frames["4"][0]["picks"] == [], "失败章不写"
    assert frames["4"][0]["review"] == "pending"
    lines = (outline.parent / sel.SELECT_LOG_NAME).read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[-1])
    assert record["outcome"] == sel.OUTCOME_FAILED and "empty message" in record["reason"]


def test_the_cli_exits_nonzero_when_a_chapter_fails_and_zero_otherwise(
    deck, tmp_path, monkeypatch, capsys
):
    root, outline = deck
    failing = _failing_transport()
    monkeypatch.setattr(client_mod, "urllib_transport", failing)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-machine-config"))
    monkeypatch.setenv("PRIMER_DEMO_API_KEY", KEY)
    assert main(["select", str(outline), "--project-root", str(root)]) == 2
    assert all(frame["picks"] for frame in _frames(outline)["1"])
    capsys.readouterr()

    # 再跑一次：可写帧只剩第四帧……不，第四帧仍失败；把失败章剔掉应当得 0。
    assert main(["select", str(outline), "--project-root", str(root), "--only", "1"]) == 0
    capsys.readouterr()


def test_retries_are_counted_in_the_log(deck, tmp_path):
    root, outline = deck
    calls = {"n": 0}

    def answer(prompt):
        if "第一章" in prompt and calls["n"] == 0:
            calls["n"] += 1
            return ""  # 空正文 = 可重试的调用失败
        return pool_answer(prompt)

    transport = FakeTransport(answer)
    result = sel.run(
        outline, root, transport=transport, only=["1"], environ=_environ(tmp_path)
    )
    assert transport.requests == 2, "第一章第一次回空正文，重试一次"
    lines = (outline.parent / sel.SELECT_LOG_NAME).read_text(encoding="utf-8").splitlines()
    records = [json.loads(line) for line in lines]
    assert sum(record["usage"]["requests"] for record in records) == 2, "重试也计入用量"
    assert result.written


# ---------------------------------------------------------------- 账目与续跑


def test_select_jsonl_carries_the_fields_and_never_the_key(deck, tmp_path):
    root, outline = deck
    transport = FakeTransport()
    sel.run(outline, root, transport=transport, environ=_environ(tmp_path))
    path = outline.parent / sel.SELECT_LOG_NAME
    text = path.read_text(encoding="utf-8")
    assert KEY not in text
    assert "PRIMER_DEMO_API_KEY" not in text
    records = [json.loads(line) for line in text.splitlines()]
    assert [record["frame"] for record in records[:2]] == [0, 1]
    for record in records:
        assert set(record) == {
            "time", "deck", "role", "provider", "model", "prompt_version", "chapter",
            "frame", "mode", "hint", "picks", "why", "chars", "usage", "outcome", "reason",
            "retried", "dropped", "base_url",
        }
        assert record["deck"] == "demo"
        assert record["provider"] == "demo" and record["model"] == "demo-select"
        assert record["prompt_version"] == sel.PROMPT_VERSION
        # 只记 scheme://host，路径与可能的凭据都不进日志。
        assert record["base_url"] == "https://api.example.invalid"
        assert set(record["usage"]) == {
            "requests", "prompt_tokens", "completion_tokens", "reasoning_tokens"
        }
    assert {record["outcome"] for record in records} <= {
        "filled", "skipped", "empty", "dropped", "failed"
    }


def test_a_second_run_skips_the_frames_it_already_filled(deck, tmp_path):
    root, outline = deck
    first = FakeTransport()
    sel.run(outline, root, transport=first, environ=_environ(tmp_path))
    after_first = outline.read_bytes()

    second = FakeTransport()
    result = sel.run(outline, root, transport=second, environ=_environ(tmp_path))
    assert second.requests == 0, "没有可写帧就不该发请求"
    assert not result.written
    assert outline.read_bytes() == after_first, "第二次运行不该动文件"
    lines = (outline.parent / sel.SELECT_LOG_NAME).read_text(encoding="utf-8").splitlines()
    tail = [json.loads(line) for line in lines[-4:]]
    assert all(
        record["outcome"] == "skipped" and record["reason"] in ("picks", "ok", "redo")
        for record in tail
    )


def test_only_and_limit_choose_the_chapters(deck, tmp_path):
    root, outline = deck
    transport = FakeTransport()
    result = sel.run(
        outline, root, only=["2"], transport=transport, environ=_environ(tmp_path)
    )
    assert [outcome.plan.number for outcome in result.outcomes] == ["2"]
    assert transport.requests == 1

    transport = FakeTransport()
    result = sel.run(
        outline, root, limit=2, transport=transport, environ=_environ(tmp_path)
    )
    assert [outcome.plan.number for outcome in result.outcomes] == ["1", "2"]

    with pytest.raises(SlidesError):
        sel.run(outline, root, only=["99"], transport=transport, environ=_environ(tmp_path))


def test_the_prompt_states_the_pool_the_frames_and_the_mode(deck, tmp_path):
    root, outline = deck
    transport = FakeTransport()
    sel.run(outline, root, transport=transport, environ=_environ(tmp_path))
    prompt = next(item for item in transport.prompts if "第二章" in item)
    assert "只讲结论，不要过程" in prompt, "方向要进提示词"
    assert "| s2-1p01 |" in prompt and "综上，该结论成立。" in prompt
    # 节清单写节号，不写页码
    assert "§2.1" in prompt
    assert "（p." not in prompt
    assert "每帧 3–5 条，默认 4 条。" in prompt
    assert '"index": 0' in prompt
    assert "s9-9p99" not in prompt, "别的章的候选不该出现"
    assert "方向（节 2.1 的 hint）" in prompt, "方向要标明是从哪儿来的"


def test_forced_mode_two_sends_no_direction_at_all(deck, tmp_path):
    root, outline = deck
    transport = FakeTransport()
    sel.run(
        outline, root, mode=sel.MODE_JUDGE, only=["2", "3"], transport=transport,
        environ=_environ(tmp_path),
    )
    second = next(item for item in transport.prompts if "第二章" in item)
    assert "只讲结论，不要过程" not in second
    third = next(item for item in transport.prompts if "第三章" in item)
    assert "这一页只讲方法" not in third
    assert "（模式 2 · 机器判断）" in third


def test_the_frame_note_reaches_the_prompt_as_the_frames_own_direction(deck, tmp_path):
    root, outline = deck
    transport = FakeTransport()
    sel.run(outline, root, only=["3"], transport=transport, environ=_environ(tmp_path))
    prompt = transport.prompts[0]
    assert "方向（本帧的 note）：这一页只讲方法" in prompt


def test_the_write_back_keeps_every_other_byte(deck, tmp_path):
    root, outline = deck
    before = outline.read_text(encoding="utf-8")
    transport = FakeTransport()
    result = sel.run(outline, root, transport=transport, environ=_environ(tmp_path))
    after = outline.read_text(encoding="utf-8")
    assert after.startswith("# 头部注释：不许重排\n")
    before_lines = before.splitlines()
    after_lines = after.splitlines()
    touched = 0
    cursor = 0
    for line in before_lines:
        if line.strip() == "- picks: []" and after_lines[cursor].strip() == "- picks: []":
            cursor += 1  # 没被填（比如 redo 未给 --redo），原样
            continue
        if line.strip() == "- picks: []":
            # 该帧被填了：跳过它新增的那几行。
            assert after_lines[cursor].strip() == "- picks:"
            cursor += 1
            while after_lines[cursor].lstrip().startswith("- "):
                cursor += 1
            touched += 1
            continue
        assert after_lines[cursor] == line, f"第 {cursor + 1} 行被动过：{line}"
        cursor += 1
    assert touched == result.writable_frames
    assert cursor == len(after_lines)


def test_every_pick_points_at_that_chapters_own_candidate_table(deck, tmp_path):
    root, outline = deck
    transport = FakeTransport()
    sel.run(outline, root, transport=transport, environ=_environ(tmp_path))
    rows = cand.parse_markdown_table(CANDIDATES_MD)
    for number, frames in _frames(outline).items():
        for frame in frames:
            for pick in frame["picks"]:
                identifier = pick if isinstance(pick, str) else pick["derived_from"][0]
                assert identifier in rows
                assert identifier.startswith(f"s{number}-")


# ---------------------------------------------------------------- 容量兜底


def _with_capacity(outline, capacity):
    """把副本骨架的容量改小：造"放不下"的场景，用的仍是骨架里那个字段。"""
    document = yaml.safe_load(outline.read_text(encoding="utf-8"))
    document["deck"]["capacity_per_page"] = capacity
    outline.write_text(_dump(document), encoding="utf-8")


def _log_records(outline):
    path = outline.parent / sel.SELECT_LOG_NAME
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _all_candidates_answer(prompt):
    """按提示词候选表的次序回满 4 条（第一章 4 条合计 76 字）。"""
    return json.dumps(
        {
            "frames": [
                {
                    "index": index,
                    "picks": [{"id": identifier} for identifier in ids_in(prompt)[:4]],
                    "why": "越长越好",
                }
                for index in frame_indexes(prompt)
            ]
        },
        ensure_ascii=False,
    )


def test_capacity_per_page_is_read_in_one_place():
    """容量只有**一处**读法：select 与 check 拿到的是同一个函数、同一个数字。"""
    assert sel.capacity_per_page is validate_mod.capacity_per_page
    assert validate_mod.capacity_per_page({"deck": {}}) == DEFAULT_CAPACITY_PER_PAGE == 336
    assert validate_mod.capacity_per_page({"deck": {"capacity_per_page": 200}}) == 200


def test_the_prompt_states_the_capacity_and_the_sum_constraint(deck, tmp_path):
    root, outline = deck
    transport = FakeTransport()
    sel.run(outline, root, only=["1"], transport=transport, environ=_environ(tmp_path))
    document = yaml.safe_load(outline.read_text(encoding="utf-8"))
    assert validate_mod.capacity_per_page(document) == 384
    prompt = transport.prompts[0]
    assert (
        "容量：每帧一页，一页只放得下 384 字（capacity_per_page）；"
        "每一帧挑中的句子，**字数之和不得超过 384 字**。" in prompt
    )
    assert "候选表的「字数」列就是每句的字数" in prompt
    assert "把这一帧的字数预算当成选择依据" in prompt
    assert "别在同一个帧里放两条长句" in prompt
    assert "3–5 条仍是目标，但**宁可少而精**——够不到 3 条也要先守住容量" in prompt
    assert "每帧所有句子的字数之和不得超过 384 字" in prompt
    assert "超了会被退回重挑一次，重挑仍超就由工具按字数从大到小丢掉" in prompt


def test_the_capacity_in_the_prompt_follows_the_skeleton(deck, tmp_path):
    """容量数字不是写死的：骨架写 200，提示词里就是 200，判据那边也是 200。"""
    root, outline = deck
    _with_capacity(outline, 200)
    transport = FakeTransport()
    sel.run(outline, root, only=["1"], transport=transport, environ=_environ(tmp_path))
    prompt = transport.prompts[0]
    assert "一页只放得下 200 字" in prompt
    assert "**字数之和不得超过 200 字**" in prompt
    document = yaml.safe_load(outline.read_text(encoding="utf-8"))
    assert validate_mod.capacity_per_page(document) == 200


def test_an_over_capacity_reply_is_retried_once_and_the_retry_is_used(deck, tmp_path):
    root, outline = deck
    _with_capacity(outline, 30)          # 第一章四句合计 76 字 > 30
    seen = []

    def answer(prompt):
        seen.append(prompt)
        if "上一版第" in prompt:
            picks = [{"id": "s1-1p01"}]  # 18 字，放得下
        else:
            picks = [{"id": identifier} for identifier in ids_in(prompt)[:4]]
        return json.dumps(
            {
                "frames": [
                    {"index": index, "picks": picks, "why": "试"}
                    for index in frame_indexes(prompt)
                ]
            },
            ensure_ascii=False,
        )

    transport = FakeTransport(answer)
    result = sel.run(
        outline, root, only=["1"], transport=transport, environ=_environ(tmp_path)
    )
    assert transport.requests == 2, "超容量只重问一次"
    assert "上一版第 0 帧超出容量 46 字（76 字 > 30 字），这次必须压到 30 以内" in seen[1]
    assert "上一版第 1 帧超出容量 46 字（76 字 > 30 字），这次必须压到 30 以内" in seen[1]
    assert "字数多的句子宁可留到别的帧单独用" in seen[1]
    assert _frames(outline)["1"][0]["picks"] == ["s1-1p01"], "用重问那一版的结果"
    records = _log_records(outline)
    assert [record["retried"] for record in records] == [True, True]
    assert [record["chars"] for record in records] == [18, 18]
    assert all(record["reason"] == "" for record in records), "重问之后合规，一条没丢"
    assert all(record["dropped"] == [] for record in records)
    assert sum(record["usage"]["requests"] for record in records) == 2, "重问也计入用量"
    report = "\n".join(sel.report_lines(result))
    assert "第一次回信超容量，整章重问过一次" in report
    assert "第一章 第 1 页：18 字 / 容量 30 字" in report


def test_a_reply_within_capacity_is_not_retried(deck, tmp_path):
    root, outline = deck
    transport = FakeTransport()
    result = sel.run(
        outline, root, only=["1"], transport=transport, environ=_environ(tmp_path)
    )
    assert transport.requests == 1, "没超容量就不重问"
    records = _log_records(outline)
    assert all(record["retried"] is False for record in records)
    assert all(record["reason"] == "" for record in records)
    assert [record["chars"] for record in records] == [76, 76], "18+22+16+20"
    report = "\n".join(sel.report_lines(result))
    assert "第一章 第 1 页：76 字 / 容量 384 字" in report


def test_a_still_over_capacity_reply_is_trimmed_from_the_longest(deck, tmp_path):
    root, outline = deck
    _with_capacity(outline, 30)
    transport = FakeTransport(_all_candidates_answer)
    result = sel.run(
        outline, root, only=["1"], transport=transport, environ=_environ(tmp_path)
    )
    assert transport.requests == 2, "只重问一次，不会一直重问"
    rows = cand.parse_markdown_table(CANDIDATES_MD)
    frames = _frames(outline)["1"]
    # 76 字放进 30 字：按字数从大到小丢 s1-1p02（22）、s1-1p04（20）、s1-1p01（18），
    # 只剩 s1-1p03（16）。
    assert frames[0]["picks"] == ["s1-1p03"]
    assert frames[1]["picks"] == ["s1-1p03"]
    assert sum(rows[identifier].length for identifier in frames[0]["picks"]) <= 30

    record = _log_records(outline)[0]
    assert record["reason"] == sel.CAPACITY_DROP == "over-capacity-drop"
    assert [item["code"] for item in record["dropped"]] == [sel.CAPACITY_DROP] * 3
    assert [item["detail"] for item in record["dropped"]] == [
        "s1-1p02（22 字）",
        "s1-1p04（20 字）",
        "s1-1p01（18 字）",
    ]
    assert record["picks"] == ["s1-1p03"] and record["chars"] == 16
    assert record["retried"] is True

    report = "\n".join(sel.report_lines(result))
    assert "容量对账（capacity_per_page 30 字/页" in report
    assert (
        "第一章 第 1 页：16 字 / 容量 30 字；第一次回信超容量，整章重问过一次；"
        "放不下，按字数从大到小丢了 s1-1p02（22 字）、s1-1p04（20 字）、s1-1p01（18 字）"
        in report
    )
    assert "这一帧放不下：字数之和超过容量，按字数从大到小丢到放得下：6 条" in report

    # 挑选时算的容量就是 check 报溢出的那个数：裁完之后 check 不再报 slide-overflow。
    _, _, findings = validate_mod.check_outline(outline, root)
    assert not [item for item in findings if item.code == "slide-overflow"]


def test_the_log_records_the_prompt_version(deck, tmp_path):
    root, outline = deck
    assert sel.PROMPT_VERSION == "slides-select-3"
    sel.run(outline, root, only=["1"], transport=FakeTransport(), environ=_environ(tmp_path))
    records = _log_records(outline)
    assert [record["prompt_version"] for record in records] == ["slides-select-3"] * 2


def test_a_failed_capacity_retry_fails_the_chapter(deck, tmp_path):
    """重问那次也坏了：整章记失败，不拿第一版超容量的答复带病写回。"""
    root, outline = deck
    _with_capacity(outline, 30)

    def answer(prompt):
        if "上一版第" in prompt:
            raise SelectCallError("endpoint returned an empty message for the demo")
        return _all_candidates_answer(prompt)

    transport = FakeTransport(answer)
    result = sel.run(
        outline, root, only=["1"], transport=transport, environ=_environ(tmp_path)
    )
    assert [outcome.plan.number for outcome in result.failures] == ["1"]
    assert "capacity retry failed" in result.outcomes[0].error
    assert _frames(outline)["1"][0]["picks"] == [], "失败的章一帧也不写"
    assert "capacity retry failed" in "\n".join(sel.report_lines(result))

