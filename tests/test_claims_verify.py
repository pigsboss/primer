# -*- coding: utf-8 -*-
"""``primer.claims.verify`` 的单元测试：范围划分、账本与续跑、编排与两份报告。

夹具是一棵微型工程树（参考文献 + 本地 PDF + 下载账本 + 转换账本 + 扁平 markdown +
一份中文正文），端点由假传输层顶替：不联网、不读任何凭据文件、不碰真实语料。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from primer.claims import __main__ as cli
from primer.claims import verify as vf
from primer.claims.client import ChatClient

BIBLIOGRAPHY = """\
# 参考文献库

> 共 2 条

[A 战略规划]

[1] Alpha A. Rosetta at comet 67P. Icarus 1, 1–2, 2001. ［原文：arXiv:2511.13946 已存本地］
[2] Beta B. Title two. Icarus 2, 3–4, 2002. ［原文：待图书馆获取］
"""

BODY_MD = """\
# 科学篇

罗塞塔号实现了人类首次彗星伴飞与着陆 [1]。

该任务还测量了彗核的水冰储量 [2]。
"""

SOURCE_HEADER = "> **Citation** — ref 001 · class A\n> **Source** — x\n\n"
SOURCE_TEXT = (
    "The Rosetta mission achieved the first comet rendezvous and landing on a comet nucleus. "
    "Philae touched down on the surface and returned measurements of the nucleus. "
    "The DART kinetic impact changed the orbital period of Dimorphos by 33 minutes. "
) + ("Ice giants have not been revisited since Voyager 2 flew past them. " * 40)
ROSETTA_QUOTE = "The Rosetta mission achieved the first comet rendezvous and landing on a comet nucleus."
ROSETTA_TRANSLATION = "The Rosetta mission achieved the first comet rendezvous and landing."

LOCAL_ROOT = "参考资料/参考文献原文"
STATE_PATH = "_primer/literature/state.jsonl"
FLAT_DIR = "_primer/literature/flat"
BIB_NAME = "成果文件/行星探测三十年综述_参考文献.md"
MANUSCRIPT = "成果文件/正文.md"


def chat_reply(content: str, *, finish: str = "stop") -> bytes:
    return json.dumps(
        {
            "choices": [{"message": {"content": content}, "finish_reason": finish}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 30},
        }
    ).encode("utf-8")


class FakeEndpoint:
    """把一份 chat completions 端点顶替成纯函数：翻译与判定各认各的提示词。"""

    def __init__(self, *, judge_reply: dict | None = None, second_reply: dict | None = None,
                 fail_translation: bool = False):
        self.prompts: list[str] = []
        self.translation_calls = 0
        self.judge_calls = 0
        self.judge_reply = judge_reply or {
            "verdict": "supported",
            "quote": ROSETTA_QUOTE,
            "rationale": "the source says exactly that",
        }
        self.second_reply = second_reply
        self.fail_translation = fail_translation

    def __call__(self, request):
        prompt = json.loads(request.body.decode("utf-8"))["messages"][0]["content"]
        self.prompts.append(prompt)
        if "You translate Chinese sentences" in prompt:
            self.translation_calls += 1
            if self.fail_translation:
                return chat_reply("", finish="length")
            ids = []
            for line in prompt.splitlines():
                head, _, rest = line.partition(". ")
                if head.isdigit() and rest:
                    ids.append((int(head), rest))
            entries = [
                {"id": number, "en": self._translation(text)} for number, text in ids
            ]
            return chat_reply(json.dumps({"translations": entries}, ensure_ascii=False))
        if "You are auditing the citations" in prompt:
            self.judge_calls += 1
            if "Second review, opposite framing" in prompt and self.second_reply is not None:
                return chat_reply(json.dumps(self.second_reply, ensure_ascii=False))
            return chat_reply(json.dumps(self.judge_reply, ensure_ascii=False))
        raise AssertionError(f"unexpected prompt: {prompt[:120]}")

    @staticmethod
    def _translation(text: str) -> str:
        if "罗塞塔" in text:
            return ROSETTA_TRANSLATION
        return "The mission measured the water ice inventory of the comet nucleus."


def client_for(endpoint: FakeEndpoint) -> ChatClient:
    return ChatClient(key="test-key", transport=endpoint)


@pytest.fixture
def project(tmp_path):
    """001 四跳全通、002 无本地原文；正文里有两条中文论断各引一个编号。"""
    root = tmp_path / "行星探测工程"
    (root / "成果文件").mkdir(parents=True)
    (root / BIB_NAME).write_text(BIBLIOGRAPHY, encoding="utf-8")
    (root / MANUSCRIPT).write_text(BODY_MD, encoding="utf-8")

    local = root / LOCAL_ROOT
    local.mkdir(parents=True)
    (local / "001_A_Rosetta.pdf").write_bytes(b"%PDF-1.4 001")

    log = root / "参考资料" / "reflib_download_log.csv"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(
        "ref,class,title,arxiv,status,saved\n"
        f"1,A,Rosetta at comet 67P,2511.13946,ok(1KB),{local / '001_A_Rosetta.pdf'}\n"
        "2,A,Title two,no-arxiv,not_found,\n",
        encoding="utf-8",
    )

    literature = root / "_primer" / "literature"
    (literature / "flat").mkdir(parents=True)
    (literature / "state.jsonl").write_text(
        json.dumps(
            {
                "md5": "1" * 32,
                "rel_path": f"{LOCAL_ROOT}/001_A_Rosetta.pdf",
                "tier": "standard",
                "status": "done",
                "output_dir": f"{STATE_PATH.rsplit('/', 1)[0]}/raw/11111111_001_A_Rosetta",
                "pages": 4,
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    (literature / "flat" / "11111111_001_A_Rosetta.md").write_text(
        SOURCE_HEADER + SOURCE_TEXT, encoding="utf-8"
    )
    return root


def make_verifier(project: Path, endpoint: FakeEndpoint, **overrides) -> vf.Verifier:
    client = client_for(endpoint)
    return vf.Verifier(
        project_root=project,
        resolver=cli.Inputs(_args(project), project).resolver(min_text_chars=1200),
        client=client,
        out_dir=project / "_primer" / "claims",
        **overrides,
    )


def _args(project: Path):
    return cli._parser().parse_args(["verify", "--project-root", str(project), "--body", str(project / MANUSCRIPT)])


def records_of(project: Path):
    from primer.claims.extract import extract_claims

    return extract_claims([project / MANUSCRIPT], project_root=project).claims


def run_pass(project: Path, endpoint: FakeEndpoint, *, limit=None, **overrides):
    """跑一遍完整的 plan + run，返回 ``(verifier, plan, stats)``。"""
    verifier = make_verifier(project, endpoint, **overrides)
    records = records_of(project)
    plan = verifier.plan(
        records, numbers=sorted({record.citation for record in records}), limit=limit
    )
    stats = verifier.run(plan)
    return verifier, plan, stats


# ---------------------------------------------------------------- 范围与账本


def test_a_pair_key_is_the_claim_id_and_the_citation():
    assert vf.pair_key("c0007", 291) == "c0007#291"


def test_only_accepts_a_citation_number_a_claim_id_or_a_pair_key():
    record = _record()
    assert vf.match_only(record, []) is True
    assert vf.match_only(record, ["c0001"]) is True
    assert vf.match_only(record, ["c0001#292"]) is True
    assert vf.match_only(record, ["292"]) is True
    assert vf.match_only(record, ["c0002", "292"]) is True
    assert vf.match_only(record, ["291"]) is False
    assert vf.match_only(record, ["c0002"]) is False


def _record():
    from primer.claims.extract import ClaimRecord

    return ClaimRecord(
        id="c0001", file="f.md", line=1, citation=292, token="[292]",
        claim="句子。", paragraph="句子。", origin="prose", claim_start=0, claim_end=3,
    )


def test_select_pairs_filters_then_limits():
    from primer.claims.extract import ClaimRecord

    records = [
        ClaimRecord(id=f"c{index:04d}", file="f.md", line=index, citation=number, token=f"[{number}]",
                    claim="句子。", paragraph="句子。", origin="prose", claim_start=0, claim_end=3)
        for index, number in enumerate([10, 11, 12], start=1)
    ]
    assert [record.citation for record in vf.select_pairs(records, limit=2)] == [10, 11]
    assert [record.citation for record in vf.select_pairs(records, only=["11"])] == [11]
    assert [record.citation for record in vf.select_pairs(records, only=["c0003"])] == [12]


def test_the_ledger_keeps_the_last_entry_per_pair_and_skips_broken_lines(tmp_path):
    path = tmp_path / "ledger.jsonl"
    vf.append_ledger(path, {"pair": "c0001#1", "verdict": "unverifiable"})
    vf.append_ledger(path, {"pair": "c0001#1", "verdict": "supported"})
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("{half a line\n")
    vf.append_ledger(path, {"pair": "c0002#2", "verdict": "source-missing"})
    ledger = vf.load_ledger(path)
    assert ledger["c0001#1"]["verdict"] == "supported"
    assert ledger["c0002#2"]["verdict"] == "source-missing"
    assert len(ledger) == 2


def test_reading_a_missing_ledger_is_empty_not_an_error(tmp_path):
    assert vf.load_ledger(tmp_path / "nope.jsonl") == {}


def test_the_plan_separates_judgeable_from_corpus_blocked(project):
    verifier = make_verifier(project, FakeEndpoint())
    records = records_of(project)
    plan = verifier.plan(records, numbers=[1, 2])
    summary = plan.as_dict(limit=None, only=[], force=False)
    assert summary["pairs"] == 2
    assert summary["judgeable"] == 1 and summary["blocked_by_corpus"] == 1
    assert summary["blocked_citations"] == 1 and summary["citations"] == 2
    assert summary["to_process"] == 2


# ---------------------------------------------------------------- 编排


def test_one_run_translates_judges_and_writes_a_ledger(project):
    endpoint = FakeEndpoint()
    verifier, plan, stats = run_pass(project, endpoint)
    assert stats.processed == 2
    assert stats.verdicts == {"source-missing": 1, "supported": 1}
    assert client_calls(endpoint) == 2  # 一次翻译 + 一次判定；断链的那对不花钱

    ledger = vf.load_ledger(project / "_primer" / "claims" / "ledger.jsonl")
    judged = ledger["c0001#1"]
    assert judged["verdict"] == "supported"
    assert judged["quote"] == ROSETTA_QUOTE
    # 引文行号与候选偏移同口径：都相对**去掉引用头之后**的正文，故这里是第 1 行。
    assert judged["quote_line"] == 1
    assert judged["resolution"] == "ok" and judged["candidates_status"] == "ok"
    assert judged["translation"] == ROSETTA_TRANSLATION
    assert judged["translation_status"] == "ok"
    assert judged["second_pass"] is None and judged["agree"] is None

    missing = ledger["c0002#2"]
    assert missing["verdict"] == "source-missing"
    assert missing["calls"] == 0 and missing["retrieval"] is None
    assert missing["translation"] is None
    assert missing["notes"] == ["local: no usable local copy of [2]"]


def client_calls(endpoint: FakeEndpoint) -> int:
    return endpoint.translation_calls + endpoint.judge_calls


def test_the_translation_query_is_what_makes_the_pair_findable(project):
    verifier, plan, _ = run_pass(project, FakeEndpoint())
    ledger = vf.load_ledger(verifier.ledger_path)
    retrieval = ledger["c0001#1"]["retrieval"]
    assert retrieval["queries"]["original"]["matched_tokens"] == 0
    assert retrieval["queries"]["translation"]["matched_tokens"] > 0
    assert retrieval["matched_tokens"] > 0
    assert _candidate_sources(verifier)["c0001#1"] == ["translation"]


def _candidate_sources(verifier: vf.Verifier) -> dict:
    """从账本记录里取每对候选来自哪几路查询——报告里"译文救回了多少"就是这么算的。"""
    out = {}
    for key, entry in vf.load_ledger(verifier.ledger_path).items():
        queries = (entry.get("retrieval") or {}).get("queries") or {}
        out[key] = sorted(name for name, stat in queries.items() if stat.get("matched_tokens"))
    return out


def test_a_second_run_skips_what_is_already_done(project):
    run_pass(project, FakeEndpoint())
    second = FakeEndpoint()
    verifier = make_verifier(project, second)
    records = records_of(project)
    plan = verifier.plan(records, numbers=[1, 2])
    assert [record.id for record in plan.pending] == []
    assert plan.as_dict(limit=None, only=[], force=False)["skipped"] == 2
    assert verifier.run(plan).processed == 0
    assert second.prompts == []


def test_force_re_does_the_pairs_already_in_the_ledger(project):
    run_pass(project, FakeEndpoint())
    second = FakeEndpoint()
    verifier = make_verifier(project, second, force=True)
    records = records_of(project)
    plan = verifier.plan(records, numbers=[1, 2])
    assert len(plan.pending) == 2
    assert verifier.run(plan).processed == 2
    # 重做时译文仍走缓存，所以只有一次判定请求；`--force` 不会为同一句话再付一次翻译钱。
    assert client_calls(second) == 1
    assert second.translation_calls == 0 and second.judge_calls == 1


def test_a_failed_translation_is_recorded_and_the_run_continues(project):
    endpoint = FakeEndpoint(fail_translation=True)
    verifier, plan, stats = run_pass(project, endpoint)
    ledger = vf.load_ledger(verifier.ledger_path)
    judged = ledger["c0001#1"]
    assert judged["translation"] is None
    assert judged["translation_status"] == "failed"
    assert judged["verdict"] in ("unverifiable", "supported", "partial", "unsupported")
    assert stats.processed == 2


def test_a_broken_chain_never_reaches_the_endpoint_for_its_claim(project):
    endpoint = FakeEndpoint()
    run_pass(project, endpoint)
    # 只有可判的那一句被送去翻译，断链的那句连翻译都不做。
    translation_prompts = [p for p in endpoint.prompts if "You translate Chinese sentences" in p]
    assert len(translation_prompts) == 1
    assert "该任务还测量了彗核的水冰储量" not in translation_prompts[0]


def test_the_report_separates_judgeable_from_corpus_blocked(project):
    endpoint = FakeEndpoint()
    verifier, plan, stats = run_pass(project, endpoint)
    payload = verifier.render(
        plan, inputs={}, limit=None, only=[], force=False,
        usage=stats.as_dict(), translation=verifier.translator.stats.as_dict(),
    )
    summary = payload["summary"]
    assert summary["plan"]["judgeable"] == 1
    assert summary["plan"]["blocked_by_corpus"] == 1
    assert summary["plan"]["blocked_citations"] == 1
    assert summary["retrieval"]["pairs_with_zero_overlap_on_the_original_query"] == 1
    assert summary["retrieval"]["improved_to_non_zero_overlap_by_translation"] == 1
    assert summary["retrieval"]["still_zero_overlap_after_merging"] == 0
    assert summary["verdicts"]["supported"] == 1
    assert summary["verdicts"]["source-missing"] == 1
    assert payload["unjudgeable"] == [
        {"citation": 2, "resolution": "no-local-file", "pairs": 1,
         "example": "该任务还测量了彗核的水冰储量 [2]。"}
    ]
    assert payload["verdict_contract"]["status"] == "run"
    assert [record["pair"] for record in payload["pairs"]] == ["c0001#1", "c0002#2"]


def test_pairs_outside_this_runs_limit_are_pending_not_missing(project):
    verifier = make_verifier(project, FakeEndpoint())
    records = records_of(project)
    plan = verifier.plan(records, numbers=[1, 2], limit=1)
    stats = verifier.run(plan)
    assert stats.processed == 1
    payload = verifier.render(
        plan, inputs={}, limit=1, only=[], force=False,
        usage=stats.as_dict(), translation=verifier.translator.stats.as_dict(),
    )
    assert [record["pair"] for record in payload["pairs"]] == ["c0001#1", "c0002#2"]
    assert payload["summary"]["verdicts"]["pending"] == 1


# ---------------------------------------------------------------- 报价


def test_the_dry_run_estimate_spends_nothing_and_writes_nothing(project):
    endpoint = FakeEndpoint()
    verifier = make_verifier(project, endpoint)
    records = records_of(project)
    plan = verifier.plan(records, numbers=[1, 2])
    before = snapshot(project)
    estimate = verifier.estimate(plan, verifier.cached_translations(plan))
    assert endpoint.prompts == []
    assert snapshot(project) == before
    assert estimate["to_process"] == 2
    assert estimate["judgeable_to_process"] == 1
    assert estimate["blocked_to_process"] == 1
    assert estimate["translation"]["distinct_sentences_needing_translation"] == 1
    assert estimate["translation"]["batches"] == 1
    assert estimate["judge"]["first_pass_calls"] == 1
    assert estimate["judge"]["second_pass_calls_at_most"] == 1
    assert estimate["approx_prompt_tokens_total"] > 0
    assert estimate["min_score_recheck"]["count"] == 1


def test_the_dry_run_estimate_counts_cached_sentences_as_free(project):
    endpoint = FakeEndpoint()
    verifier = make_verifier(project, endpoint)
    records = records_of(project)
    from primer.claims.extract import strip_citations

    verifier.translator.translate_all([strip_citations(records[0].claim)])
    plan = verifier.plan(records, numbers=[1, 2])
    estimate = verifier.estimate(plan, verifier.cached_translations(plan))
    assert estimate["translation"]["distinct_sentences_needing_translation"] == 0
    assert estimate["translation"]["sentences_served_from_cache"] == 1


# ---------------------------------------------------------------- 报告文本


def test_the_markdown_leads_with_unsupported_then_partial_then_the_table(project):
    endpoint = FakeEndpoint(
        judge_reply={"verdict": "unsupported", "quote": ROSETTA_QUOTE, "rationale": "contradicts"},
        second_reply={"verdict": "partial", "quote": "Philae touched down", "rationale": "half"},
    )
    verifier, plan, stats = run_pass(project, endpoint)
    payload = verifier.render(
        plan, inputs={}, limit=None, only=[], force=False,
        usage=stats.as_dict(), translation=verifier.translator.stats.as_dict(),
    )
    from primer.claims.report import write_verdicts_md

    path = write_verdicts_md(project / "_primer" / "claims", payload)
    text = path.read_text(encoding="utf-8")
    assert text.index("## 一、判定为") < text.index("## 二、只判到") < text.index("## 五、汇总表")
    assert "判不了不等于有问题" in text
    assert "可以判" in text and "判不了" in text
    record = payload["pairs"][0]
    assert record["verdict"] == "unsupported"
    assert record["second_pass"]["verdict"] == "partial"
    assert record["agree"] is False and record["needs_review"] is True
    assert "两遍不一致" in text
    assert ROSETTA_QUOTE in text


# ---------------------------------------------------------------- 命令行


def snapshot(root: Path) -> set:
    return {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}


def test_the_cli_dry_run_sends_nothing_and_writes_nothing(project, capsys):
    before = snapshot(project)
    code = cli.main(["verify", "--project-root", str(project), "--body", str(project / MANUSCRIPT), "--dry-run"])
    out = capsys.readouterr().out
    assert code == 0
    assert snapshot(project) == before
    assert "cost estimate" in out
    assert "nothing was sent" in out
    assert "judgeable in scope: 1" in out


def test_the_cli_without_a_key_refuses_instead_of_guessing(tmp_path, monkeypatch, capsys):
    root = tmp_path / "empty"
    root.mkdir()
    monkeypatch.setenv("PRIMER_API_KEY", "")
    code = cli.main(["verify", "--project-root", str(root), "--body", str(root / "missing.md")])
    assert code == 2
    assert "body markdown not found" in capsys.readouterr().err


def test_the_cli_writes_only_under_the_claims_directory(project, monkeypatch):
    endpoint = FakeEndpoint()
    monkeypatch.setattr(cli, "build_client", lambda **kwargs: client_for(endpoint))
    before = snapshot(project)
    code = cli.main(["verify", "--project-root", str(project), "--body", str(project / MANUSCRIPT)])
    assert code == 0
    written = snapshot(project) - before
    assert written == {
        "_primer/claims/ledger.jsonl",
        "_primer/claims/translations.json",
        "_primer/claims/verdicts.json",
        "_primer/claims/verdicts.md",
    }
    payload = json.loads((project / "_primer" / "claims" / "verdicts.json").read_text(encoding="utf-8"))
    assert payload["summary"]["verdicts"]["supported"] == 1
    assert payload["summary"]["usage"]["requests"] == 2
    assert payload["inputs"]["body[0]"]["path"] == MANUSCRIPT


def test_the_cli_resumes_and_force_re_does(project, monkeypatch, capsys):
    endpoint = FakeEndpoint()
    monkeypatch.setattr(cli, "build_client", lambda **kwargs: client_for(endpoint))
    args = ["verify", "--project-root", str(project), "--body", str(project / MANUSCRIPT)]
    assert cli.main(args) == 0
    assert cli.main(args) == 0
    out = capsys.readouterr().out
    assert "already done 2, to process 0" in out
    assert cli.main(args + ["--force"]) == 0
    assert "already done 0, to process 2" in capsys.readouterr().out
    assert len(vf.load_ledger(project / "_primer" / "claims" / "ledger.jsonl")) == 2
