# -*- coding: utf-8 -*-
"""``primer.claims.extract`` 的单元测试：引用标记的识别、展开与上下文排除。

全部用小型夹具，不读真实语料、不联网。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from primer.claims import extract as ex
from primer.claims.__main__ import main as claims_main

BODY = """\
# 1.1 导言

三十年过去，火星水的证据已从河谷影像变为就位钻探 [111–113]。罗塞塔号实现人类首次彗星伴飞与着陆 [245][253]。

> 体例：以问题为纲，学《Enduring Quests, Daring Visions》之法 [14]；新增输运机制证据链（[351]–[360]）。

```text
[12] 代码围栏里的方括号不算引用。
```

| 问题 | 现状 |
|:--|:--|
| 火星是否有过生命 | ALH84001 生物印记之争三十年未决 [110]；就位仪器灵敏度不足 |

*全球尺度火山重铺事件抹去了更早的地质记录。科学内容引自 [154]。*

[1] Alpha A. Title one. Icarus 1, 1–2, 2001. ［原文：arXiv:2511.13946 已存本地］
[9] 这一行以方括号编号开头，但它不像文献条目。

插图编号 [图 1-2] 与 [199 类] 都不算引用。
"""


@pytest.fixture
def body_file(tmp_path):
    path = tmp_path / "body.md"
    path.write_text(BODY, encoding="utf-8")
    return path


def _extract(path, **kwargs):
    return ex.extract_claims([path], project_root=path.parent, **kwargs)


def test_offsets_point_back_into_the_source(body_file):
    result = _extract(body_file)
    text = BODY
    for record in result.claims:
        assert text[record.claim_start : record.claim_end] == record.claim


def test_one_record_per_claim_citation_pair(body_file):
    result = _extract(body_file)
    pairs = [(record.citation, record.token) for record in result.claims]
    assert [number for number, _ in pairs] == [111, 112, 113, 245, 253, 14] + list(range(351, 361)) + [154]
    assert pairs[0] == (111, "[111–113]")
    assert pairs[3] == (245, "[245]")
    assert pairs[5] == (14, "[14]")
    assert pairs[6] == (351, "[351]–[360]")
    assert [record.id for record in result.claims] == [f"c{index:04d}" for index in range(1, len(pairs) + 1)]
    assert result.pairs_by_file == {"body.md": 20}


def test_a_range_expands_and_stays_together_as_one_sentence(body_file):
    result = _extract(body_file)
    sentence = "三十年过去，火星水的证据已从河谷影像变为就位钻探 [111–113]。"
    first = result.claims[0]
    assert first.claim == sentence
    assert first.range_size == 3
    assert {record.claim for record in result.claims[:3]} == {sentence}


def test_paragraph_context_is_attached_and_bounded(body_file):
    result = _extract(body_file)
    paragraph = result.claims[0].paragraph
    assert paragraph.startswith("三十年过去")
    assert len(paragraph) <= ex.MAX_PARAGRAPH_CHARS


def test_headings_code_fences_and_bibliography_lines_are_excluded(body_file):
    result = _extract(body_file)
    reasons = {batch.reason: batch for batch in result.excluded}
    assert reasons[ex.SKIP_FENCE].pairs == 1
    assert reasons[ex.SKIP_BIB].pairs == 1
    assert ex.SKIP_HEADING not in reasons
    assert all(record.citation != 12 for record in result.claims)
    assert all(record.citation != 1 for record in result.claims)
    assert all(record.line != 8 for record in result.claims)


def test_table_rows_are_skipped_by_default_and_kept_on_request(body_file):
    skipped = _extract(body_file)
    kept = _extract(body_file, include_tables=True)
    reasons = {batch.reason: batch for batch in skipped.excluded}
    assert reasons[ex.SKIP_TABLE].pairs == 1
    assert [record.citation for record in kept.claims if record.origin == ex.ORIGIN_TABLE] == [110]
    assert len(kept.claims) == len(skipped.claims) + 1
    assert skipped.table_pairs == 1


def test_caption_and_blockquote_origins_are_recorded_without_markup(body_file):
    result = _extract(body_file)
    caption = next(record for record in result.claims if record.citation == 154)
    quote = next(record for record in result.claims if record.citation == 14)
    assert caption.origin == ex.ORIGIN_CAPTION
    assert caption.claim == "科学内容引自 [154]。"
    assert quote.origin == ex.ORIGIN_BLOCKQUOTE
    assert quote.claim.startswith("体例：以问题为纲")
    assert ">" not in quote.claim
    assert "《Enduring Quests, Daring Visions》" in quote.claim


def test_ambiguous_line_is_reported_rather_than_guessed(body_file):
    result = _extract(body_file)
    assert len(result.ambiguous) == 1
    assert "[9]" in result.ambiguous[0] or "9 这一行" in result.ambiguous[0]
    assert all(record.citation != 9 for record in result.claims)


def test_brackets_that_look_like_citations_are_reported(body_file):
    result = _extract(body_file)
    assert "[图 1-2]" in result.rejected
    assert "[199 类]" in result.rejected
    assert "[111–113]" not in result.rejected


def test_the_three_dash_spellings_are_all_ranges():
    tokens = ex.citation_tokens("见 [1–3] 与 [4-6] 与 [7—9]。")
    assert [token.numbers for token in tokens] == [(1, 2, 3), (4, 5, 6), (7, 8, 9)]


def test_a_dash_between_two_brackets_is_one_range():
    tokens = ex.citation_tokens("（[351]–[360]）")
    assert len(tokens) == 1
    assert tokens[0].numbers == tuple(range(351, 361))


def test_consecutive_brackets_stay_separate():
    tokens = ex.citation_tokens("[245][253]")
    assert [token.numbers for token in tokens] == [(245,), (253,)]


def test_a_backwards_range_is_normalised():
    tokens = ex.citation_tokens("[9–7]")
    assert tokens[0].numbers == (7, 8, 9)


def test_strip_citations_keeps_the_character_count():
    claim = "火星古代水环境 [111–113]，罗塞塔号 [245][253]。"
    stripped = ex.strip_citations(claim)
    assert len(stripped) == len(claim)
    assert "111" not in stripped and "245" not in stripped
    assert "火星古代水环境" in stripped


def test_ascii_period_after_a_list_marker_or_a_number_does_not_split():
    spans = ex._iter_sentences("1. **阈值中心设计**：指标先于构型。指标不是设计的结果。")
    assert len(spans) == 2
    text = "1. **阈值中心设计**：指标先于构型。指标不是设计的结果。"
    assert text[spans[0][0] : spans[0][1]] == "1. **阈值中心设计**：指标先于构型。"
    assert text[spans[1][0] : spans[1][1]] == "指标不是设计的结果。"


def test_a_long_paragraph_is_cut_around_the_claim(tmp_path):
    path = tmp_path / "long.md"
    path.write_text(
        "填充句。" * 200 + "火星水的就位证据已经确立 [7]；同位素标尺也已建立 [8]。" + "填充句。" * 200,
        encoding="utf-8",
    )
    result = ex.extract_claims([path], project_root=tmp_path)
    assert len(result.claims) == 2
    paragraph = result.claims[0].paragraph
    assert paragraph.startswith("…") and paragraph.endswith("…")
    assert "[7]" in paragraph
    assert len(paragraph) <= ex.MAX_PARAGRAPH_CHARS + 2


def test_empty_body_yields_no_records(tmp_path):
    path = tmp_path / "empty.md"
    path.write_text("# 标题\n\n没有引用的一段话。\n", encoding="utf-8")
    result = ex.extract_claims([path], project_root=tmp_path)
    assert result.claims == []
    assert result.tokens_by_file == {"empty.md": 0}


def test_default_body_list_covers_the_four_manuscript_files():
    assert ex.DEFAULT_BODY_FILES[0].endswith("第一篇_科学篇.md")
    assert any(name.endswith("科产融合专题_深空探测专用技术产业链调研.md") for name in ex.DEFAULT_BODY_FILES)


# ---- 命令行：产物只落在 _primer/claims/ 下 ----


REFERENCE_MD = (
    "# 参考文献库\n"
    "\n"
    "> 共 1 条\n"
    "\n"
    "[1] Alpha A. Title one. Icarus 1, 1–2, 2001. ［原文：arXiv:2511.13946 已存本地］\n"
)

STATE = {
    "md5": "abc12345deadbeefabc12345deadbeef",
    "rel_path": "参考资料/参考文献原文/001_B_Alpha.pdf",
    "tier": "standard",
    "status": "done",
    "output_dir": "_primer/literature/raw/abc12345_001_B_Alpha",
    "pages": 3,
}

FLAT = (
    "> **Citation** — ref 001 · class B\n"
    "> **Title** — Alpha\n"
    "> **Source** — 参考资料/参考文献原文/001_B_Alpha.pdf\n"
    "> **Pages** — 3\n"
    "\n"
    + "# Alpha\n\n"
    + "The Alpha mission measured the water inventory of the inner disk in 2001. " * 20
    + "\n\n"
    + "火星水的就位证据已经确立，同位素标尺也已建立。\n" * 40
)


def _snapshot(root: Path) -> dict[str, tuple[int, str]]:
    shot: dict[str, tuple[int, str]] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            shot[path.relative_to(root).as_posix()] = (path.stat().st_size, digest)
    return shot


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "行星探测工程"
    (root / "成果文件").mkdir(parents=True)
    (root / "成果文件" / "正文.md").write_text(
        "# 正文\n\n火星水的就位证据已经确立 [1]，它的同位素标尺也已建立 [1]。\n", encoding="utf-8"
    )
    (root / "成果文件" / "参考文献.md").write_text(REFERENCE_MD, encoding="utf-8")
    local = root / "参考资料" / "参考文献原文"
    local.mkdir(parents=True)
    (local / "001_B_Alpha.pdf").write_bytes(b"%PDF-1.4 alpha")
    literature = root / "_primer" / "literature"
    (literature / "flat").mkdir(parents=True)
    (literature / "state.jsonl").write_text(json.dumps(STATE, ensure_ascii=False) + "\n", encoding="utf-8")
    (literature / "flat" / "abc12345_001_B_Alpha.md").write_text(FLAT, encoding="utf-8")
    return root


def test_cli_writes_only_under_the_claims_cache_dir(tmp_path, capsys):
    project = _project(tmp_path)
    before = _snapshot(project)

    code = claims_main(
        [
            "extract",
            "--project-root", str(project),
            "--body", str(project / "成果文件" / "正文.md"),
            "--ref", str(project / "成果文件" / "参考文献.md"),
        ]
    )

    assert code == 0
    after = _snapshot(project)
    assert not set(before) - set(after)
    changed = [name for name, meta in after.items() if before.get(name) != meta]
    assert changed
    assert all(name.startswith("_primer/claims/") for name in changed), changed
    assert (project / "_primer" / "claims" / "claims.json").is_file()
    assert (project / "_primer" / "claims" / "claims.md").is_file()

    payload = json.loads((project / "_primer" / "claims" / "claims.json").read_text(encoding="utf-8"))
    assert payload["summary"]["claim_pairs"] == 2
    assert payload["summary"]["resolution"]["ok"] == 1
    assert payload["resolution_index"]["1"]["source"]["local_file"] == (
        "参考资料/参考文献原文/001_B_Alpha.pdf"
    )
    assert payload["claims"][0]["candidates_status"] in ("ok", "candidates-weak")

    out = capsys.readouterr()
    assert str(project) not in out.out
    assert "claim-citation pairs: 2" in out.out
    for artifact in ("claims.json", "claims.md"):
        text = (project / "_primer" / "claims" / artifact).read_text(encoding="utf-8")
        assert str(project) not in text
        assert str(project.resolve()) not in text


def test_cli_reports_a_missing_body_instead_of_writing_anything(tmp_path, capsys):
    project = _project(tmp_path)
    before = _snapshot(project)

    code = claims_main(["extract", "--project-root", str(project), "--body", str(tmp_path / "nope.md")])

    assert code == 2
    assert "not found" in capsys.readouterr().err
    assert _snapshot(project) == before
