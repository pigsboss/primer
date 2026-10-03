# -*- coding: utf-8 -*-
"""``primer.scene.loop`` 的测试：一轮完整回合、动作白名单、合并与备份、升级标记、
动作块解析容错、游标续跑与锁。

全程**不联网、不跑 Blender**：LLM 用注入的假传输层（回信是构造好的字节串），
loop.yaml 里的命令是 ``python3 -c`` 的小假命令，临时 session／task 目录全在 tmp_path。
"""

from __future__ import annotations

import io
import json
import os
import time
from pathlib import Path

import pytest

from primer.config import load_config
from primer.llm import LlmHttpError, LlmTransportError
from primer.scene import loop as L

KEY = "unit-test-secret-key"

CONFIG_YAML = """\
providers:
  demo:
    base_url: https://api.example.invalid/v1
    key_env: PRIMER_DEMO_API_KEY
roles:
  scene:
    provider: demo
    model: demo-model
    max_tokens: 2048
  distill:
    provider: demo
    model: demo-strong
  vision:
    provider: demo
    model: demo-vision
"""

LOOP_YAML = """\
task: 测试任务
params_file: params.yaml
editable: [params.yaml]
commands:
  build: [python3, -c, 'print("built ok")']
  boom: [python3, -c, 'import sys; sys.stderr.write("nope\n"); sys.exit(3)']
images: {glob: "out/*.png", max: 2}
system_extra: |
  补充：测试用。
"""

PARAMS_YAML = "length_m: 4.42\nother: keep\n"


class FakeTransport:
    """假传输层：按次序回放构造好的字节串，或抛出构造好的异常。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        if not self.replies:
            raise AssertionError("unexpected extra LLM request")
        item = self.replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def bodies(self):
        return [json.loads(request.body.decode("utf-8")) for request in self.requests]


def llm_reply(text, model="demo-model", usage=None) -> bytes:
    payload = {
        "model": model,
        "choices": [
            {"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}
        ],
        "usage": usage
        if usage is not None
        else {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
    }
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _stamp() -> str:
    from datetime import datetime

    return datetime.now().astimezone().isoformat(timespec="microseconds")


def send_user(session: Path, text: str, model=None) -> None:
    with (session / L.CHAT_FILENAME).open("a", encoding="utf-8") as fh:
        fh.write(
            json.dumps({"ts": _stamp(), "role": "user", "text": text, "model": model},
                       ensure_ascii=False)
            + "\n"
        )


def read_chat(session: Path):
    return [
        json.loads(line)
        for line in (session / L.CHAT_FILENAME).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def agents(session: Path):
    """只取驱动的回信（role == "agent"），过程状态（system）与用户消息都跳过。"""
    return [record for record in read_chat(session) if record.get("role") == "agent"]


@pytest.fixture()
def tree(tmp_path):
    project = tmp_path / "proj"
    (project / "_primer").mkdir(parents=True)
    (project / "_primer" / "config.yaml").write_text(CONFIG_YAML, encoding="utf-8")
    task = project / "_primer" / "scene" / "t1"
    task.mkdir(parents=True)
    (task / "params.yaml").write_text(PARAMS_YAML, encoding="utf-8")
    (task / "loop.yaml").write_text(LOOP_YAML, encoding="utf-8")
    session = tmp_path / "session"
    session.mkdir()
    return project, task, session


def make_driver(tree, transport, **kwargs):
    project, task, session = tree
    environ = {"XDG_CONFIG_HOME": str(project / "xdg"), "HOME": str(project)}
    config = load_config(project, project / "_primer" / "config.yaml", environ=environ)
    loop = L.load_loop_config(task / "loop.yaml")
    defaults = dict(
        session_root=session,
        task_root=task,
        loop=loop,
        config=config,
        default_role="scene",
        transport=transport,
        environ={"PRIMER_DEMO_API_KEY": KEY},
        out=io.StringIO(),
        err=io.StringIO(),
    )
    defaults.update(kwargs)
    return L.Driver(**defaults)


def write_png(path: Path) -> Path:
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (8, 8), (10, 20, 30)).save(path)
    return path


# ------------------------------------------------------------------ 解析容错

def test_parse_actions_reads_the_actions_fence_and_strips_it():
    text = "我改一下参数。\n\n```actions\nactions:\n  - kind: edit_params\n    file: params.yaml\n    edits:\n      - before: \"a: 1\"\n        after: \"a: 2\"\n        note: \"理由\"\n```\n"
    body, actions, notes = L.parse_actions(text)
    assert body == "我改一下参数。"
    assert actions == [
        {"kind": "edit_params", "file": "params.yaml",
         "edits": [{"before": "a: 1", "after": "a: 2", "note": "理由"}]}
    ]
    assert notes == []


def test_parse_actions_accepts_yaml_and_json_fences():
    yaml_fence = "```yaml\nkind: run\nname: build\n```"
    _, actions, _ = L.parse_actions(yaml_fence)
    assert actions == [{"kind": "run", "name": "build"}]

    json_fence = '```json\n{"actions": [{"kind": "run", "name": "build"}]}\n```'
    _, actions, _ = L.parse_actions(json_fence)
    assert actions == [{"kind": "run", "name": "build"}]


def test_parse_actions_tolerates_fullwidth_punctuation():
    text = (
        "```actions\n"
        "actions：\n"
        "  - kind：run\n"
        "    name：build\n"
        "```"
    )
    _, actions, _ = L.parse_actions(text)
    assert actions == [{"kind": "run", "name": "build"}]


def test_parse_actions_leaves_a_plain_code_fence_alone():
    text = "看这段参数：\n\n```yaml\nfoo: bar\n```\n完了。"
    body, actions, notes = L.parse_actions(text)
    assert actions == [] and notes == []
    assert "foo: bar" in body


def test_parse_actions_reports_an_unparseable_actions_fence():
    text = "```actions\nthis: [is: not: valid\n```"
    body, actions, notes = L.parse_actions(text)
    assert actions == []
    assert notes and "解析失败" in notes[0]
    assert "this:" not in body


def test_merge_by_id_replaces_same_id_and_appends_new():
    merged, added, replaced = L.merge_by_id(
        [{"id": "r1", "value": "old"}], [{"id": "r1", "value": "new"}, {"id": "r2", "value": "x"}]
    )
    assert merged == [{"id": "r1", "value": "new"}, {"id": "r2", "value": "x"}]
    assert (added, replaced) == (1, 1)


# ------------------------------------------------------------------ 一轮回合

def test_round_writes_reply_meta_and_log(tree):
    project, task, session = tree
    transport = FakeTransport([llm_reply("收到，先看参数。")])
    driver = make_driver(tree, transport)
    send_user(session, "帮我核对长度")

    assert driver.run_once() == 1

    records = read_chat(session)
    assert records[0]["role"] == "user" and records[0]["text"] == "帮我核对长度"
    agent = records[1]
    assert agent["role"] == "agent" and agent["text"] == "收到，先看参数。"
    assert agent["model"] == "demo-model" and agent["provider"] == "demo"
    assert agent["actions_executed"] == [] and agent["refs"] == []
    assert agent["usage"]["total_tokens"] == 18
    assert agent["seconds"] >= 0.0

    meta = json.loads((session / L.META_FILENAME).read_text(encoding="utf-8"))
    assert meta["default_role"] == "scene"
    assert meta["heartbeat_ts"]
    assert {"id": "scene", "label": "demo/demo-model"} in meta["models"]
    assert [m["id"] for m in meta["models"]][:1] == ["scene"]

    state = json.loads((session / L.STATE_FILENAME).read_text(encoding="utf-8"))
    assert state["processed_user_messages"] == 1


def test_round_sends_the_whole_disk_context(tree):
    project, task, session = tree
    (session / "record.json").write_text(
        json.dumps({"rows": [{"id": "r1", "subject": "遮阳罩", "value": "八边形",
                              "status": "adjudicated", "evidence": "q1"}]}, ensure_ascii=False),
        encoding="utf-8",
    )
    transport = FakeTransport([llm_reply("好")])
    driver = make_driver(tree, transport)
    send_user(session, "问题在哪？")
    driver.run_once()

    body = transport.bodies()[0]
    system = body["messages"][0]["content"]
    user = body["messages"][1]["content"]
    assert body["messages"][0]["role"] == "system"
    assert "edit_params" in system and "escalate" in system
    assert "补充：测试用。" in system  # loop.yaml 的 system_extra
    assert "params.yaml" in system and "build" in system
    assert "length_m: 4.42" in user  # 参数库全文
    assert "遮阳罩" in user           # record 摘要
    assert "问题在哪？" in user


def test_user_message_model_switches_role(tree):
    project, task, session = tree
    transport = FakeTransport([llm_reply("强模型回答", model="demo-strong")])
    driver = make_driver(tree, transport)
    send_user(session, "这题难，换强的", model="distill")
    driver.run_once()
    assert transport.bodies()[0]["model"] == "demo-strong"
    assert transport.bodies()[0]["max_tokens"] == 16384  # distill 没写 max_tokens → 兜底
    assert agents(session)[-1]["model_role"] == "distill"


def test_unknown_model_falls_back_to_default_role(tree):
    project, task, session = tree
    transport = FakeTransport([llm_reply("好")])
    driver = make_driver(tree, transport)
    send_user(session, "随便选了个不存在的", model="nope")
    driver.run_once()
    assert transport.bodies()[0]["model"] == "demo-model"


# ------------------------------------------------------------------ edit_params

def test_edit_params_applies_a_unique_edit_and_logs_the_diff(tree):
    project, task, session = tree
    reply = llm_reply(
        "按裁决改长度。\n\n```actions\n"
        "actions:\n  - kind: edit_params\n    file: params.yaml\n    edits:\n"
        "      - before: \"length_m: 4.42\"\n        after: \"length_m: 4.8\"\n"
        "        note: \"裁决 q8=A\"\n```\n"
    )
    driver = make_driver(tree, FakeTransport([reply]))
    send_user(session, "把长度改成 4.8")
    driver.run_once()

    assert "length_m: 4.8" in (task / "params.yaml").read_text(encoding="utf-8")
    entries = [
        json.loads(line)
        for line in (session / L.LOG_FILENAME).read_text(encoding="utf-8").splitlines()
    ]
    assert entries[0]["kind"] == "edit_params" and entries[0]["file"] == "params.yaml"
    assert entries[0]["applied"][0]["after"] == "length_m: 4.8"
    agent = agents(session)[-1]
    assert agent["actions_executed"][0]["kind"] == "edit_params"
    assert agent["actions_executed"][0]["edits"] == 1


def test_edit_params_rejects_a_non_unique_before(tree):
    project, task, session = tree
    reply = llm_reply(
        "```actions\nactions:\n  - kind: edit_params\n    file: params.yaml\n    edits:\n"
        "      - before: \"e\"\n        after: \"E\"\n```\n"
    )
    driver = make_driver(tree, FakeTransport([reply]))
    send_user(session, "改")
    driver.run_once()
    assert (task / "params.yaml").read_text(encoding="utf-8") == PARAMS_YAML
    agent = agents(session)[-1]
    result = agent["actions_executed"][0]
    assert result["ok"] is False and result["failed"]
    assert "matched" in result["failed"][0]["reason"]
    assert "动作未完成" in agent["text"]


def test_edit_params_refuses_a_file_outside_the_whitelist(tree):
    project, task, session = tree
    (task / "other.yaml").write_text("secret: 1\n", encoding="utf-8")
    reply = llm_reply(
        "```actions\nactions:\n  - kind: edit_params\n    file: other.yaml\n    edits:\n"
        "      - before: \"secret: 1\"\n        after: \"secret: 2\"\n```\n"
    )
    driver = make_driver(tree, FakeTransport([reply]))
    send_user(session, "改别的文件")
    driver.run_once()
    assert (task / "other.yaml").read_text(encoding="utf-8") == "secret: 1\n"
    result = agents(session)[-1]["actions_executed"][0]
    assert result["ok"] is False
    assert "whitelist" in result["error"]


def test_edit_params_refuses_a_path_that_escapes_the_task(tree):
    project, task, session = tree
    reply = llm_reply(
        "```actions\nactions:\n  - kind: edit_params\n    file: ../../../../etc/passwd\n"
        "    edits:\n      - before: \"a\"\n        after: \"b\"\n```\n"
    )
    driver = make_driver(tree, FakeTransport([reply]))
    send_user(session, "越界")
    driver.run_once()
    result = agents(session)[-1]["actions_executed"][0]
    assert result["ok"] is False


# ------------------------------------------------------------------ run

def test_run_executes_a_whitelisted_command_and_logs_output(tree):
    project, task, session = tree
    reply = llm_reply("跑一遍构建。\n\n```actions\nactions:\n  - kind: run\n    name: build\n```")
    driver = make_driver(tree, FakeTransport([reply]))
    send_user(session, "重建")
    driver.run_once()

    result = agents(session)[-1]["actions_executed"][0]
    assert result["ok"] is True and result["exit_code"] == 0
    system = [r for r in read_chat(session) if r.get("role") == "system"]
    assert system and "开始执行命令" in system[0]["text"] and "build" in system[0]["text"]
    entries = [
        json.loads(line)
        for line in (session / L.LOG_FILENAME).read_text(encoding="utf-8").splitlines()
    ]
    run_entry = [e for e in entries if e["kind"] == "run"][0]
    assert run_entry["stdout_tail"] == ["built ok"]
    assert run_entry["cwd"] == str(task)


def test_run_refuses_a_name_outside_the_whitelist(tree):
    project, task, session = tree
    reply = llm_reply("```actions\nactions:\n  - kind: run\n    name: rm -rf /\n```")
    driver = make_driver(tree, FakeTransport([reply]))
    send_user(session, "跑")
    driver.run_once()
    result = agents(session)[-1]["actions_executed"][0]
    assert result["ok"] is False and "whitelist" in result["error"]


def test_run_reports_a_nonzero_exit_code(tree):
    project, task, session = tree
    reply = llm_reply("```actions\nactions:\n  - kind: run\n    name: boom\n```")
    driver = make_driver(tree, FakeTransport([reply]))
    send_user(session, "跑失败的那个")
    driver.run_once()
    result = agents(session)[-1]["actions_executed"][0]
    assert result["ok"] is False and result["exit_code"] == 3
    assert "nope" in result["error"]


def test_run_attaches_new_images_to_the_reply(tree):
    project, task, session = tree
    picture = write_png(task / "out" / "pic.png")
    reply = llm_reply("跑完看图。\n\n```actions\nactions:\n  - kind: run\n    name: build\n```")
    driver = make_driver(tree, FakeTransport([reply]), vision="off")
    send_user(session, "重建并出图")
    driver.run_once()

    agent = agents(session)[-1]
    assert agent["refs"] == ["renders/pic.png"]
    assert (session / "renders" / "pic.png").is_file()
    assert picture.is_file()


# ------------------------------------------------------------------ session_update

def test_session_update_merges_by_id_and_backs_up(tree):
    project, task, session = tree
    (session / "record.json").write_text(
        json.dumps({"rows": [{"id": "r1", "subject": "旧", "value": "1"}]}, ensure_ascii=False),
        encoding="utf-8",
    )
    (session / "cards.json").write_text(json.dumps({"cards": []}), encoding="utf-8")
    reply = llm_reply(
        "```actions\nactions:\n  - kind: session_update\n"
        "    record_rows:\n"
        "      - {id: r1, subject: 新, value: 2, status: adjudicated, evidence: q1}\n"
        "      - {id: r2, subject: 追加, value: 3}\n"
        "    cards:\n"
        "      - {id: c1, title: 问题一, text: 选哪个}\n```\n"
    )
    driver = make_driver(tree, FakeTransport([reply]))
    send_user(session, "落账")
    driver.run_once()

    rows = json.loads((session / "record.json").read_text(encoding="utf-8"))["rows"]
    assert [row["id"] for row in rows] == ["r1", "r2"]
    assert rows[0]["value"] == 2
    cards = json.loads((session / "cards.json").read_text(encoding="utf-8"))["cards"]
    assert cards[0]["id"] == "c1"
    backups = list((session / L.BACKUP_DIRNAME).glob("record.*.json"))
    assert backups, "record.json 写前必须备份"
    result = agents(session)[-1]["actions_executed"][0]
    assert result["ok"] and result["record_rows"] == 2
    assert result["added"] == 2 and result["replaced"] == 1


def test_session_update_with_nothing_to_merge_fails_honestly(tree):
    project, task, session = tree
    reply = llm_reply("```actions\nactions:\n  - kind: session_update\n    cards: []\n```")
    driver = make_driver(tree, FakeTransport([reply]))
    send_user(session, "空更新")
    driver.run_once()
    result = agents(session)[-1]["actions_executed"][0]
    assert result["ok"] is False


# ------------------------------------------------------------------ escalate

def test_escalate_writes_the_marker_and_a_record_row(tree):
    project, task, session = tree
    (session / "record.json").write_text(json.dumps({"rows": []}), encoding="utf-8")
    reply = llm_reply(
        "这个得改 primer 本身。\n\n```actions\nactions:\n  - kind: escalate\n"
        "    reason: \"需要修改 primer.scene 的布局算法\"\n```\n"
    )
    driver = make_driver(tree, FakeTransport([reply]))
    send_user(session, "布局算错了")
    driver.run_once()

    marker = (session / L.ESCALATION_FILENAME).read_text(encoding="utf-8")
    assert "需要修改 primer.scene 的布局算法" in marker
    assert "demo/demo-model" in marker
    rows = json.loads((session / "record.json").read_text(encoding="utf-8"))["rows"]
    assert rows and "升级" in rows[0]["subject"]
    assert rows[0]["status"] == "open"
    agent = agents(session)[-1]
    assert agent["actions_executed"][0]["kind"] == "escalate"
    assert "已升级 kimi code 回路" in agent["text"]


# ------------------------------------------------------------------ --no-actions

def test_no_actions_parses_but_does_not_execute(tree):
    project, task, session = tree
    reply = llm_reply(
        "```actions\nactions:\n  - kind: edit_params\n    file: params.yaml\n    edits:\n"
        "      - before: \"length_m: 4.42\"\n        after: \"length_m: 4.8\"\n```\n"
    )
    driver = make_driver(tree, FakeTransport([reply]), no_actions=True)
    send_user(session, "试着改一下")
    driver.run_once()

    assert (task / "params.yaml").read_text(encoding="utf-8") == PARAMS_YAML
    agent = agents(session)[-1]
    assert agent["actions_executed"] == []
    assert "未执行" in agent["text"]


# ------------------------------------------------------------------ 游标与锁

def test_cursor_resumes_and_processes_each_backlog_message_once(tree):
    project, task, session = tree
    transport = FakeTransport([llm_reply("回一"), llm_reply("回二"), llm_reply("回三")])
    driver = make_driver(tree, transport)
    send_user(session, "一")
    send_user(session, "二")

    assert driver.run_once() == 2
    assert [r["text"] for r in read_chat(session) if r["role"] == "agent"] == ["回一", "回二"]

    assert driver.run_once() == 0
    assert len(transport.requests) == 2

    send_user(session, "三")
    assert driver.run_once() == 1
    assert [r["text"] for r in read_chat(session) if r["role"] == "agent"] == ["回一", "回二", "回三"]


def test_cursor_resets_when_chat_is_truncated(tree):
    project, task, session = tree
    driver = make_driver(tree, FakeTransport([llm_reply("好")]))
    (session / L.STATE_FILENAME).write_text(
        json.dumps({"processed_user_messages": 9}), encoding="utf-8"
    )
    send_user(session, "新会话")
    assert driver.run_once() == 1


def test_lock_is_exclusive_and_stale_locks_are_taken_over(tree):
    project, task, session = tree
    first = L.acquire_lock(session)
    try:
        with pytest.raises(L.LoopError) as caught:
            L.acquire_lock(session)
        assert "another loop driver" in str(caught.value)
    finally:
        L.release_lock(first)

    second = L.acquire_lock(session)
    L.release_lock(second)
    assert not (session / L.LOCK_FILENAME).exists()

    # 心跳过期的锁可以被接管。
    stale = session / L.LOCK_FILENAME
    stale.write_text("{}", encoding="utf-8")
    old = time.time() - (L.LOCK_STALE_SECONDS + 5)
    os.utime(stale, (old, old))
    taken = L.acquire_lock(session)
    L.release_lock(taken)


def test_lock_heartbeat_is_refreshed_by_a_round(tree):
    project, task, session = tree
    lock = L.acquire_lock(session)
    try:
        old = time.time() - 5
        os.utime(lock, (old, old))
        driver = make_driver(tree, FakeTransport([llm_reply("好")]), lock_path=lock)
        driver.run_once()
        assert time.time() - (session / L.LOCK_FILENAME).stat().st_mtime < 2
    finally:
        L.release_lock(lock)


# ------------------------------------------------------------------ 视觉降级

def test_vision_auto_attaches_images_and_downgrades_on_http_4xx(tree):
    project, task, session = tree
    write_png(task / "out" / "render.png")
    transport = FakeTransport(
        [LlmHttpError(400, "https://api.example.invalid/v1/chat/completions", "bad content type"),
         llm_reply("换纯文本再看")]
    )
    driver = make_driver(tree, transport, vision="auto")
    send_user(session, "看看最新的渲染图")
    driver.run_once()

    bodies = transport.bodies()
    assert isinstance(bodies[0]["messages"][1]["content"], list), "第一轮必须带图片分片"
    assert isinstance(bodies[1]["messages"][1]["content"], str), "降级后必须是纯文本"
    agent = agents(session)[-1]
    assert "纯文本" in agent["text"]
    entries = [
        json.loads(line)
        for line in (session / L.LOG_FILENAME).read_text(encoding="utf-8").splitlines()
    ]
    assert entries[0]["kind"] == "vision_downgrade"


def test_vision_off_never_attaches_images(tree):
    project, task, session = tree
    write_png(task / "out" / "render.png")
    transport = FakeTransport([llm_reply("好")])
    driver = make_driver(tree, transport, vision="off")
    send_user(session, "看图")
    driver.run_once()
    assert isinstance(transport.bodies()[0]["messages"][1]["content"], str)


# ------------------------------------------------------------------ 失败与 CLI

def test_llm_failure_writes_an_honest_error_reply_and_advances(tree):
    project, task, session = tree
    transport = FakeTransport(
        [LlmTransportError("cannot reach host"), LlmTransportError("cannot reach host")]
    )
    driver = make_driver(tree, transport)
    send_user(session, "在吗")
    assert driver.run_once() == 1
    agent = agents(session)[-1]
    assert agent["role"] == "agent" and "调用模型失败" in agent["text"]
    assert json.loads((session / L.STATE_FILENAME).read_text(encoding="utf-8"))[
        "processed_user_messages"
    ] == 1


def test_main_once_without_messages_writes_meta_and_exits_zero(tree, capsys):
    project, task, session = tree
    code = L.main(
        [
            "--session", str(session),
            "--task", str(task),
            "--project-root", str(project),
            "--config", str(project / "_primer" / "config.yaml"),
            "--once",
        ]
    )
    assert code == 0
    assert "本轮没有新的用户消息" in capsys.readouterr().out
    meta = json.loads((session / L.META_FILENAME).read_text(encoding="utf-8"))
    assert meta["models"]
    assert not (session / L.LOCK_FILENAME).exists(), "退出后必须放锁"


def test_main_refuses_a_second_driver(tree, capsys):
    project, task, session = tree
    held = L.acquire_lock(session)
    try:
        code = L.main(
            [
                "--session", str(session),
                "--task", str(task),
                "--project-root", str(project),
                "--config", str(project / "_primer" / "config.yaml"),
                "--once",
            ]
        )
    finally:
        L.release_lock(held)
    assert code == 2
    assert "another loop driver" in capsys.readouterr().err


def test_main_rejects_an_unknown_role(tree, capsys):
    project, task, session = tree
    code = L.main(
        [
            "--session", str(session),
            "--task", str(task),
            "--project-root", str(project),
            "--config", str(project / "_primer" / "config.yaml"),
            "--role", "nope",
            "--once",
        ]
    )
    assert code == 2
    assert "role 'nope'" in capsys.readouterr().err


def test_loop_config_validation_reports_missing_fields(tmp_path):
    bad = tmp_path / "loop.yaml"
    bad.write_text("task: x\n", encoding="utf-8")
    with pytest.raises(L.LoopError) as caught:
        L.load_loop_config(bad)
    assert "params_file" in str(caught.value)
