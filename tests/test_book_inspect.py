# -*- coding: utf-8 -*-
"""视觉校对：页面选择策略、提示词与回信解析、可注入的端点，以及端到端回路。

除最后那个用真实 poppler 与本地 ``http.server`` 的用例（两者都在本机，不碰公网）
外，其余测试只用合成的页面文本与假传输层，绝不联网、不读任何凭据。
"""

import json
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from book_fixtures import MANIFEST, tiny_book

from primer.book.__main__ import main
from primer.book.inspect import (
    DEFAULT_MAX_TOKENS,
    RETRY_MAX_TOKENS_CAP,
    RUBRIC,
    InspectError,
    VisionCallError,
    VisionReplyError,
    parse_page_spec,
    parse_toc_pages,
    parse_vision_reply,
    run_inspect,
    select_pages,
    table_needles,
    locate_pages,
    extract_finish_reason,
    extract_message_content,
    extract_usage,
    vision_payload,
)
from primer.book.manifest import load_manifest

PDF_TOOLS = all(shutil.which(tool) for tool in ("pdfinfo", "pdftotext", "pdftoppm"))
needs_poppler = pytest.mark.skipif(not PDF_TOOLS, reason="poppler tools are not installed")

TOC = """\
\\contentsline {chapter}{凡例}{1}{chapter*.2}%
\\contentsline {chapter}{目录}{1}{section*.3}%
\\contentsline {part}{第一篇\\hspace {1em}科学篇}{1}{part.1}%
\\contentsline {chapter}{\\numberline {第一章\\hspace {.3em}}导言}{2}{chapter.1}%
\\contentsline {section}{\\numberline {1.1}小节}{2}{section.1.1}%
"""


def mini_pdf(pages=2, rotate=None, same_content=False):
    """手写一个最小 PDF：够 poppler 打开、分页，并可指定某页旋转 90 度。"""
    rotate = rotate or {}
    objects = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[" + b" ".join(b"%d 0 R" % (4 + i) for i in range(pages))
        + b"]/Count %d>>" % pages,
        b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",
    ]
    first_stream = 4 + pages
    for index in range(pages):
        flag = b"/Rotate 90" if (index + 1) in rotate else b""
        objects.append(
            b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 300 400]" + flag
            + b"/Contents %d 0 R/Resources<</Font<</F1 3 0 R>>>>>>" % (first_stream + index)
        )
    for index in range(pages):
        number = 1 if same_content else index + 1
        stream = b"BT /F1 20 Tf 20 250 Td (page %d) Tj ET" % number
        objects.append(b"<</Length %d>>\nstream\n" % len(stream) + stream + b"\nendstream")

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    size = len(objects) + 1
    out += b"xref\n0 %d\n0000000000 65535 f \n" % size
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<</Size %d/Root 1 0 R>>\nstartxref\n%d\n%%%%EOF\n" % (size, xref)
    return bytes(out)


def chat_reply(findings, usage=None):
    """把发现包成一份 chat completions 回信（可带 usage）。"""
    content = json.dumps({"findings": findings}, ensure_ascii=False)
    payload = {"choices": [{"message": {"content": content}}]}
    if usage is not None:
        payload["usage"] = usage
    return json.dumps(payload).encode("utf-8")


def empty_reply(finish_reason="length", usage=None):
    """思考型模型把输出预算耗在推理链上时的回信：结构完整、正文为空。"""
    payload = {"choices": [{"finish_reason": finish_reason, "message": {"content": ""}}]}
    if usage is not None:
        payload["usage"] = usage
    return json.dumps(payload).encode("utf-8")


def prepare(
    tmp_path,
    pages=3,
    rotate=None,
    toc=None,
    findings=None,
    same_content=False,
    manifest_text=MANIFEST,
):
    """微型书 + 一份"已编译"的 PDF（可带 .toc 与发现清单），返回清单路径。"""
    path = tiny_book(tmp_path, manifest_text)
    out = tmp_path / "_primer" / "book"
    out.mkdir(parents=True, exist_ok=True)
    (out / "tiny.pdf").write_bytes(mini_pdf(pages, rotate, same_content=same_content))
    if toc:
        (out / "tiny.toc").write_text(toc, encoding="utf-8")
    if findings is not None:
        (out / "tiny.findings.json").write_text(
            json.dumps({"jobname": "tiny", "findings": findings}), encoding="utf-8"
        )
    return path


def snapshot(root):
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


# ---------------------------------------------------------------- 参数与目录


def test_parse_page_spec_expands_ranges_and_rejects_junk():
    assert parse_page_spec("1-3,79,200") == [1, 2, 3, 79, 200]
    assert parse_page_spec("2,2-3") == [2, 3]

    for bad in ("abc", "5-2", "", "1-"):
        with pytest.raises(InspectError):
            parse_page_spec(bad)


def test_parse_toc_pages_reads_part_and_chapter_openings_only():
    entries = parse_toc_pages(TOC)

    assert ("chapter", 1, "凡例") in entries
    assert ("part", 1, "第一篇 科学篇") in entries
    assert ("chapter", 2, "第一章 导言") in entries
    assert all(kind in ("part", "chapter") for kind, _, _ in entries)
    assert not any("小节" in title for _, _, title in entries)


def test_parse_toc_pages_survives_a_truncated_line():
    assert parse_toc_pages("\\contentsline {chapter}{半行}") == []


# ---------------------------------------------------------------- 表格定位


def test_table_needles_use_the_caption_then_the_header_row():
    from primer.book.markdown import Table

    blocks = {
        "V1": [
            Table(rows=[["未决问题", "三十年卡点", "新契机"], ["甲", "乙", "丙"]]),
            Table(rows=[["科学问题判定阈值与工程能力缺口对照核验", "根技术指向"], ["甲", "乙"]],
                  caption="科学问题判定阈值与工程能力缺口对照核验"),
        ]
    }

    needles = table_needles(blocks)

    assert needles["V1#1"] == ("未决问题三十年卡点新契机",)
    assert needles["V1#2"][0] == "科学问题判定阈值与工程能力缺口对照核验"


def test_locate_pages_collapses_repeated_headers_into_run_starts():
    texts = ["", "x", "页二 未决问题三十年卡点新契机", "页三 未决问题三十年卡点新契机 续"] + [""] * 5
    texts[8] = "页九 未决问题三十年卡点新契机"

    hits, runs = locate_pages(("未决问题三十年卡点新契机",), texts, 10)

    assert hits == [3, 4, 9]
    assert runs == [3, 9]


# ---------------------------------------------------------------- 选择策略


def test_select_pages_prioritizes_structure_tables_landscape_and_sample():
    texts = [""] * 30
    texts[8] = "表头 未决问题三十年卡点新契机"

    choices, findings = select_pages(
        page_count=30,
        rotations={12: 90},
        page_texts=texts,
        toc_entries=[("part", 5, "第一篇"), ("chapter", 7, "第一章")],
        table_locations_=["V1#43"],
        needles={"V1#43": ("未决问题三十年卡点新契机",)},
        max_pages=6,
        seed=7,
    )

    by_page = {choice.page: choice.reasons for choice in choices}
    assert len(choices) == 6
    assert any("part opening" in reason for reason in by_page[5])
    assert any("chapter opening" in reason for reason in by_page[7])
    assert by_page[9] == ("table-layout finding at V1#43",)
    assert any("landscape" in reason for reason in by_page[12])
    sampled = [page for page, reasons in by_page.items() if "sample" in reasons[0]]
    assert len(sampled) == 2
    assert all("seed 7" in by_page[page][0] for page in sampled)
    assert [item.code for item in findings] == []


def test_select_pages_ignores_captions_on_front_matter_pages():
    """表格目录里也有题注：定位必须从正文首页起，否则宽表全落到目录页上。"""
    texts = [""] * 30
    texts[1] = "表格目录 科学问题判定阈值与工程能力缺口对照核验"
    texts[19] = "科学问题判定阈值与工程能力缺口对照核验"

    choices, _ = select_pages(
        page_count=30,
        rotations={},
        page_texts=texts,
        toc_entries=[("part", 10, "第二篇")],
        table_locations_=["V2#117"],
        needles={"V2#117": ("科学问题判定阈值与工程能力缺口对照核验",)},
        max_pages=4,
        seed=1,
    )

    table_pages = [
        choice.page for choice in choices if choice.reasons == ("table-layout finding at V2#117",)
    ]
    assert table_pages == [20]


def test_select_pages_truncates_beyond_max_pages_and_reports_it():
    entries = [("chapter", page, f"第{page}章") for page in range(1, 7)]

    choices, findings = select_pages(
        page_count=30,
        rotations={},
        page_texts=[""] * 30,
        toc_entries=entries,
        table_locations_=[],
        needles={},
        max_pages=2,
        seed=0,
    )

    assert [choice.page for choice in choices] == [1, 2]
    assert [item.code for item in findings] == ["pages-truncated"]


def test_select_pages_explicit_override_ignores_the_policy():
    choices, findings = select_pages(
        page_count=30,
        rotations={12: 90},
        page_texts=[""] * 30,
        toc_entries=[("part", 5, "第一篇")],
        table_locations_=["V1#1"],
        needles={"V1#1": ("未决问题三十年卡点新契机",)},
        max_pages=5,
        explicit=[2, 4],
        seed=0,
    )

    assert [(choice.page, choice.reasons) for choice in choices] == [
        (2, ("explicitly selected with --pages",)),
        (4, ("explicitly selected with --pages",)),
    ]
    assert findings == []


def test_select_pages_reports_an_unmappable_table():
    choices, findings = select_pages(
        page_count=10,
        rotations={},
        page_texts=[""] * 10,
        toc_entries=[("part", 1, "第一篇")],
        table_locations_=["V1#9"],
        needles={"V1#9": ("找不到的表头",)},
        max_pages=5,
        seed=0,
    )

    assert [item.code for item in findings] == ["table-page-unmapped"]
    assert all(choice.page != 9 for choice in choices)


# ---------------------------------------------------------------- 回信解析


def test_parse_vision_reply_accepts_fenced_json_and_severity_aliases():
    text = (
        "```json\n"
        '{"findings": [{"page": "p. 15", "code": "Table Overflow", "severity": "critical",'
        ' "what": "rows run past the right margin", "where": "table",'
        ' "converter_hint": "use the landscape tier"}]}\n```'
    )

    findings = parse_vision_reply(text)

    assert len(findings) == 1
    assert (findings[0].page, findings[0].code, findings[0].severity) == (
        15, "table-overflow", "error",
    )
    assert findings[0].where == "table"
    assert findings[0].converter_hint == "use the landscape tier"


def test_parse_vision_reply_recovers_a_bare_array_between_prose():
    text = 'Sure! Here you go:\n[{"page": 3, "severity": "info", "what": "gap"}]\nHope that helps.'

    findings = parse_vision_reply(text)

    assert len(findings) == 1
    assert (findings[0].page, findings[0].code, findings[0].severity) == (3, "vision-issue", "info")


def test_parse_vision_reply_accepts_an_empty_findings_list():
    assert parse_vision_reply('{"findings": []}') == []


def test_parse_vision_reply_rejects_a_malformed_reply():
    with pytest.raises(VisionReplyError):
        parse_vision_reply("I could not inspect these pages, sorry.")


def test_extract_message_content_rejects_an_error_payload():
    with pytest.raises(VisionCallError, match="returned an error"):
        extract_message_content(b'{"error": {"message": "quota exceeded"}}')


def test_vision_payload_carries_the_images_as_base64_data_urls(tmp_path):
    image = tmp_path / "page-0001.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)

    body = json.loads(vision_payload("k3", "PROMPT", [image]).decode("utf-8"))

    content = body["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "PROMPT"}
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert body["model"] == "k3"


def test_vision_payload_sends_a_generous_max_tokens_by_default(tmp_path):
    image = tmp_path / "page-0001.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)

    default = json.loads(vision_payload("k3", "PROMPT", [image]).decode("utf-8"))
    custom = json.loads(vision_payload("k3", "PROMPT", [image], 1234).decode("utf-8"))

    assert default["max_tokens"] == DEFAULT_MAX_TOKENS
    assert custom["max_tokens"] == 1234


def test_extract_finish_reason_reads_the_stop_reason():
    raw = json.dumps({"choices": [{"finish_reason": "length", "message": {"content": "x"}}]}).encode()

    assert extract_finish_reason(raw) == "length"
    assert extract_finish_reason(b"not json") == ""
    assert extract_finish_reason(json.dumps({"choices": []}).encode()) == ""


def test_extract_usage_reads_prompt_and_completion_tokens():
    raw = json.dumps(
        {"usage": {"prompt_tokens": 1200, "completion_tokens": 16384, "total_tokens": 17584}}
    ).encode()

    assert extract_usage(raw) == {"prompt_tokens": 1200, "completion_tokens": 16384}


@pytest.mark.parametrize(
    "raw",
    [
        b"not json",
        b"[]",
        json.dumps({"choices": []}).encode(),
        json.dumps({"usage": {"prompt_tokens": "many", "completion_tokens": 1.5}}).encode(),
        json.dumps({"usage": {"prompt_tokens": True}}).encode(),
    ],
)
def test_extract_usage_falls_back_to_zero(raw):
    """读不出用量不影响校对：一律按 0 记。"""
    assert extract_usage(raw) == {"prompt_tokens": 0, "completion_tokens": 0}


# ---------------------------------------------------------------- 无端点


@needs_poppler
def test_no_endpoint_writes_the_bundle_and_exits_zero(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("PRIMER_VISION_API_KEY", raising=False)
    path = prepare(tmp_path, pages=3, toc=TOC)

    code = main(["inspect", str(path), "--max-pages", "3"])

    assert code == 0
    out = tmp_path / "_primer" / "book" / "inspect"
    assert (out / "pages" / "page-0001.png").is_file()
    assert (out / "pages" / "page-0002.png").is_file()
    prompt = (out / "inspect.prompt.md").read_text(encoding="utf-8")
    assert "What counts as a defect" in prompt
    assert "converter_hint" in prompt
    assert "| image | page | why this page |" in prompt
    pages = json.loads((out / "inspect.pages.json").read_text(encoding="utf-8"))
    assert [entry["page"] for entry in pages["pages"]][:2] == [1, 2]
    assert pages["pages"][0]["page"] == 1
    assert "part opening: 第一篇 科学篇" in pages["pages"][0]["reasons"]
    assert "chapter opening: 第一章 导言" in pages["pages"][1]["reasons"]
    assert pages["images_bytes"] > 0
    report = json.loads((out / "inspect.findings.json").read_text(encoding="utf-8"))
    assert report["vision"]["configured"] is False
    assert report["failed"] is False
    assert [item["code"] for item in report["findings"]] == ["vision-not-configured"]
    assert "not configured" in (out / "inspect.md").read_text(encoding="utf-8")
    assert "no vision endpoint configured" in capsys.readouterr().out


@needs_poppler
def test_missing_api_key_is_treated_as_no_endpoint(tmp_path, capsys):
    path = prepare(tmp_path, pages=1, toc=TOC)

    code = run_inspect(
        load_manifest(path),
        pages_spec="1",
        base_url="http://127.0.0.1:1/v1",
        model="k3",
        key_env="PRIMER_MISSING_KEY",
        environ={},
    )

    assert code == 0
    assert "no API key in $PRIMER_MISSING_KEY" in capsys.readouterr().out


@needs_poppler
def test_inspect_writes_only_under_its_own_directory(tmp_path, monkeypatch):
    monkeypatch.delenv("PRIMER_VISION_API_KEY", raising=False)
    path = prepare(tmp_path, pages=2, toc=TOC)
    before = snapshot(tmp_path)

    assert main(["inspect", str(path), "--max-pages", "2"]) == 0

    new = snapshot(tmp_path) - before
    assert new
    assert all(name.startswith("_primer/book/inspect/") for name in new)


def test_missing_pdf_is_an_error_finding(tmp_path):
    path = tiny_book(tmp_path)

    code = main(["inspect", str(path)])

    assert code == 1
    report = json.loads(
        (tmp_path / "_primer" / "book" / "inspect" / "inspect.findings.json").read_text(
            encoding="utf-8"
        )
    )
    assert report["findings"][0]["code"] == "missing-pdf"
    assert report["findings"][0]["severity"] == "error"


# ---------------------------------------------------------------- 端点回路


@needs_poppler
def test_findings_from_a_fake_transport_drive_the_exit_code(tmp_path):
    path = prepare(tmp_path, pages=2, toc=TOC)
    requests = []

    def transport(request):
        requests.append(request)
        return chat_reply(
            [
                {
                    "page": 2,
                    "code": "table-overflow",
                    "severity": "error",
                    "what": "the table runs past the right margin",
                    "where": "table",
                    "converter_hint": "portrait-wrap still overran; consider the landscape tier",
                },
                {
                    "page": 1,
                    "code": "whitespace-gap",
                    "severity": "info",
                    "what": "half of the page is empty",
                    "where": "page-geometry",
                    "converter_hint": "the figure float has no top-level text to sit with",
                },
            ]
        )

    code = run_inspect(
        load_manifest(path),
        pages_spec="1-2",
        base_url="http://endpoint.invalid/v1",
        model="k3",
        key_env="PRIMER_TEST_KEY",
        environ={"PRIMER_TEST_KEY": "secret"},
        transport=transport,
    )

    assert code == 1
    assert len(requests) == 1
    assert requests[0].url == "http://endpoint.invalid/v1/chat/completions"
    assert requests[0].headers["Authorization"] == "Bearer secret"
    body = json.loads(requests[0].body.decode("utf-8"))
    images = [part for part in body["messages"][0]["content"] if part["type"] == "image_url"]
    assert len(images) == 2
    report = json.loads(
        (tmp_path / "_primer" / "book" / "inspect" / "inspect.findings.json").read_text(
            encoding="utf-8"
        )
    )
    codes = [item["code"] for item in report["findings"]]
    assert codes == ["table-overflow", "whitespace-gap"]
    assert report["failed"] is True
    assert report["vision"]["configured"] is True
    table = report["findings"][0]
    assert table["converter_hint"].startswith("portrait-wrap still overran")
    summary = {row["code"]: row["count"] for row in report["summary"]}
    assert summary == {"table-overflow": 1, "whitespace-gap": 1}


@needs_poppler
def test_a_malformed_reply_is_reported_not_crashed(tmp_path):
    path = prepare(tmp_path, pages=1, toc=TOC)
    transport = lambda request: json.dumps(
        {"choices": [{"message": {"content": "I am unable to inspect these images."}}]}
    ).encode("utf-8")

    code = run_inspect(
        load_manifest(path),
        pages_spec="1",
        base_url="http://endpoint.invalid/v1",
        model="k3",
        environ={"PRIMER_VISION_API_KEY": "secret"},
        transport=transport,
    )

    assert code == 0
    out = tmp_path / "_primer" / "book" / "inspect"
    report = json.loads((out / "inspect.findings.json").read_text(encoding="utf-8"))
    assert [(item["code"], item["severity"]) for item in report["findings"]] == [
        ("vision-unparsed", "warning")
    ]
    assert "could not be parsed" in (out / "inspect.md").read_text(encoding="utf-8")


@needs_poppler
def test_a_truncated_reply_is_reported_as_truncated_not_unparsed(tmp_path):
    path = prepare(tmp_path, pages=1, toc=TOC)
    cut_off = '{"findings": [{"page": 1, "code": "overflow", "sev'

    def transport(request):
        return json.dumps(
            {"choices": [{"finish_reason": "length", "message": {"content": cut_off}}]}
        ).encode("utf-8")

    code = run_inspect(
        load_manifest(path),
        pages_spec="1",
        base_url="http://endpoint.invalid/v1",
        model="deepseek-flash",
        environ={"PRIMER_VISION_API_KEY": "secret"},
        transport=transport,
    )

    assert code == 0
    report = json.loads(
        (tmp_path / "_primer" / "book" / "inspect" / "inspect.findings.json").read_text(
            encoding="utf-8"
        )
    )
    codes = [item["code"] for item in report["findings"]]
    assert "vision-truncated" in codes
    assert "vision-unparsed" not in codes


@needs_poppler
def test_identical_pages_are_sent_once_and_the_extra_is_reported(tmp_path):
    path = prepare(tmp_path, pages=3, toc=TOC, same_content=True)
    requests = []

    def transport(request):
        requests.append(request)
        return chat_reply([])

    code = run_inspect(
        load_manifest(path),
        pages_spec="1-3",
        base_url="http://endpoint.invalid/v1",
        model="deepseek-flash",
        environ={"PRIMER_VISION_API_KEY": "secret"},
        transport=transport,
    )

    assert code == 0
    assert len(requests) == 1
    body = json.loads(requests[0].body.decode("utf-8"))
    images = [part for part in body["messages"][0]["content"] if part["type"] == "image_url"]
    assert len(images) == 1
    report = json.loads(
        (tmp_path / "_primer" / "book" / "inspect" / "inspect.findings.json").read_text(
            encoding="utf-8"
        )
    )
    assert report["images"]["sent"] == 1
    assert report["images"]["selected_pages"] == 1
    codes = [item["code"] for item in report["findings"]]
    assert codes.count("duplicate-page-images") == 2


@needs_poppler
def test_stale_pages_are_cleared_before_each_run(tmp_path, monkeypatch):
    monkeypatch.delenv("PRIMER_VISION_API_KEY", raising=False)
    path = prepare(tmp_path, pages=2, toc=TOC)
    pages = tmp_path / "_primer" / "book" / "inspect" / "pages"
    pages.mkdir(parents=True, exist_ok=True)
    junk = pages / "page-0003 2.png"
    junk.write_bytes(b"stale duplicate from an earlier run")
    (pages / "orphan.png").write_bytes(b"stale")

    assert main(["inspect", str(path), "--max-pages", "2"]) == 0

    assert not junk.exists()
    assert not (pages / "orphan.png").exists()
    assert sorted(entry.name for entry in pages.iterdir()) == [
        "page-0001.png",
        "page-0002.png",
    ]
    report = json.loads(
        (tmp_path / "_primer" / "book" / "inspect" / "inspect.findings.json").read_text(
            encoding="utf-8"
        )
    )
    assert report["images"]["sent"] == 2


@needs_poppler
def test_a_transport_failure_is_an_error_finding(tmp_path):
    path = prepare(tmp_path, pages=1, toc=TOC)

    def transport(request):
        raise VisionCallError("cannot reach the endpoint: connection refused")

    code = run_inspect(
        load_manifest(path),
        pages_spec="1",
        base_url="http://endpoint.invalid/v1",
        model="k3",
        environ={"PRIMER_VISION_API_KEY": "secret"},
        transport=transport,
    )

    assert code == 1
    report = json.loads(
        (tmp_path / "_primer" / "book" / "inspect" / "inspect.findings.json").read_text(
            encoding="utf-8"
        )
    )
    assert report["findings"][0]["code"] == "vision-failed"


@needs_poppler
def test_strict_fails_on_a_warning_only_reply(tmp_path):
    path = prepare(tmp_path, pages=1, toc=TOC)
    transport = lambda request: chat_reply(
        [{"page": 1, "severity": "warning", "what": "tight spacing"}]
    )

    kwargs = dict(
        pages_spec="1",
        base_url="http://endpoint.invalid/v1",
        model="k3",
        environ={"PRIMER_VISION_API_KEY": "secret"},
        transport=transport,
    )

    assert run_inspect(load_manifest(path), **kwargs) == 0
    assert run_inspect(load_manifest(path), strict=True, **kwargs) == 1


@needs_poppler
def test_token_usage_is_summed_and_reported(tmp_path, capsys):
    """思考型模型的输出侧是大头：prompt 与 completion 分开累计并打印。"""
    path = prepare(tmp_path, pages=3, toc=TOC)
    replies = [
        {"prompt_tokens": 100, "completion_tokens": 16384},
        {"prompt_tokens": 90, "completion_tokens": 20},
    ]

    def transport(request):
        return chat_reply([], usage=replies.pop(0))

    code = run_inspect(
        load_manifest(path),
        pages_spec="1-3",
        base_url="http://endpoint.invalid",
        model="deepseek-flash",
        environ={"PRIMER_VISION_API_KEY": "secret"},
        batch_size=2,
        transport=transport,
    )

    assert code == 0
    assert replies == []
    out = tmp_path / "_primer" / "book" / "inspect"
    report = json.loads((out / "inspect.findings.json").read_text(encoding="utf-8"))
    assert report["usage"] == {
        "requests": 2,
        "prompt_tokens": 190,
        "completion_tokens": 16404,
        "total_tokens": 16594,
    }
    printed = capsys.readouterr().out
    assert "batch size 2 (cli), max_tokens 16384 (default)" in printed
    assert "2 request(s), prompt 190 tokens + completion 16404 tokens = 16594 tokens" in printed
    assert "no per-1k price configured" in printed
    assert "api usage: 2 request(s)" in (out / "inspect.md").read_text(encoding="utf-8")


@needs_poppler
def test_manifest_vision_settings_drive_the_requests(tmp_path, capsys):
    """清单里记下的端点与两个旋钮直接生效——批大小按一页一请求走。"""
    text = MANIFEST + (
        "\nvision:\n"
        "  base_url: http://endpoint.invalid\n"
        "  model: deepseek-flash\n"
        "  key_env: PRIMER_TEST_KEY\n"
        "  batch_size: 1\n"
        "  max_tokens: 20000\n"
    )
    path = prepare(tmp_path, pages=3, toc=TOC, manifest_text=text)
    bodies = []

    def transport(request):
        bodies.append(json.loads(request.body.decode("utf-8")))
        return chat_reply([], usage={"prompt_tokens": 10, "completion_tokens": 5})

    code = run_inspect(
        load_manifest(path),
        pages_spec="1-3",
        environ={"PRIMER_TEST_KEY": "secret"},
        transport=transport,
    )

    assert code == 0
    # 内置默认是两页一请求；清单记的是 1，故三次请求、每次一张图。
    assert len(bodies) == 3
    assert all(body["model"] == "deepseek-flash" for body in bodies)
    assert all(body["max_tokens"] == 20000 for body in bodies)
    printed = capsys.readouterr().out
    assert "batch size 1 (manifest), max_tokens 20000 (manifest)" in printed


@needs_poppler
def test_cli_vision_settings_beat_the_manifest(tmp_path, capsys):
    """命令行显式给出的值胜出：清单记的是 1 页/20000 token，这里按 2 页/1000 走。"""
    text = MANIFEST + (
        "\nvision:\n"
        "  base_url: http://endpoint.invalid\n"
        "  model: deepseek-flash\n"
        "  key_env: PRIMER_TEST_KEY\n"
        "  batch_size: 1\n"
        "  max_tokens: 20000\n"
    )
    path = prepare(tmp_path, pages=3, toc=TOC, manifest_text=text)
    bodies = []

    def transport(request):
        bodies.append(json.loads(request.body.decode("utf-8")))
        return chat_reply([])

    code = run_inspect(
        load_manifest(path),
        pages_spec="1-3",
        batch_size=2,
        max_tokens=1000,
        environ={"PRIMER_TEST_KEY": "secret"},
        transport=transport,
    )

    assert code == 0
    assert len(bodies) == 2
    assert all(body["max_tokens"] == 1000 for body in bodies)
    assert "batch size 2 (cli), max_tokens 1000 (cli)" in capsys.readouterr().out


def test_inspect_rejects_non_positive_knobs(tmp_path):
    """两个旋钮在 run_inspect 的入口就被挡住（清单里的值在载入时已校验）。"""
    manifest = load_manifest(tiny_book(tmp_path))

    for kwargs in ({"batch_size": 0}, {"max_tokens": -1}):
        with pytest.raises(InspectError, match="must be at least 1"):
            run_inspect(manifest, **kwargs)


class _Handler(BaseHTTPRequestHandler):
    """最小的 OpenAI 兼容端点：记录请求，回一份含一条 error 的发现。"""

    received = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        type(self).received.append({"path": self.path, "headers": dict(self.headers), "body": body})
        payload = chat_reply(
            [
                {
                    "page": 1,
                    "code": "figure-small",
                    "severity": "error",
                    "what": "the figure is too small to read",
                    "where": "figure",
                    "converter_hint": "the planner had adjustbox available; grow the figure",
                }
            ]
        )
        response = payload.decode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response.encode("utf-8"))

    def log_message(self, *args):  # 静音，免得污染测试输出
        pass


@needs_poppler
def test_local_http_server_endpoint_is_reached_with_the_real_transport(tmp_path, monkeypatch):
    """真 HTTP、真 urllib，但只打到本机的 http.server：证明端点回路可用而不碰网络。"""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _Handler.received = []
    monkeypatch.setenv("PRIMER_TEST_KEY", "local-secret")
    try:
        path = prepare(tmp_path, pages=1, toc=TOC)

        code = main(
            [
                "inspect", str(path),
                "--pages", "1",
                "--vision-base-url", f"http://127.0.0.1:{server.server_port}/v1",
                "--vision-model", "k3",
                "--vision-key-env", "PRIMER_TEST_KEY",
            ]
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert code == 1
    assert len(_Handler.received) == 1
    request = _Handler.received[0]
    assert request["path"] == "/v1/chat/completions"
    assert request["headers"]["Authorization"] == "Bearer local-secret"
    assert b"data:image/png;base64," in request["body"]
    report = json.loads(
        (tmp_path / "_primer" / "book" / "inspect" / "inspect.findings.json").read_text(
            encoding="utf-8"
        )
    )
    finding = report["findings"][0]
    assert finding["code"] == "figure-small"
    assert finding["page"] == 1
    assert "adjustbox" in finding["converter_hint"]
    assert report["failed"] is True


def test_cli_rejects_a_bad_page_spec(tmp_path, capsys):
    path = tiny_book(tmp_path)

    assert main(["inspect", str(path), "--pages", "abc"]) == 2
    assert "inspect error" in capsys.readouterr().err


# ---------------------------------------------------------------- 评分规则的作用域


def test_rubric_asks_only_for_what_a_rendered_page_can_show():
    """确定性 check 层保证的类别不再出现在"要报什么"里；只有渲染页看得见的五类还在。"""
    defects = RUBRIC.split("## What counts as a defect", 1)[1].split("## Verified elsewhere", 1)[0]
    lowered = defects.lower()

    for dropped in ("overflow", "text-block", "margin", "numbering", "glyph", "tofu", "garbled"):
        assert dropped not in lowered

    for kept in (
        "below legibility",
        "single-character orphan",
        "too small to read",
        "cropped at the frame",
        "orphaned at the foot of a page",
        "stranded from its text",
        "landscape page used where a portrait page would do",
        "conspicuous whitespace",
        "large gap mid-page",
    ):
        assert kept in lowered


def test_rubric_names_the_other_layers_as_out_of_scope():
    """被删的类别要显式说"别报"并给一行理由，免得模型自己重新推导。"""
    out_of_scope = " ".join(
        RUBRIC.split("## Verified elsewhere", 1)[1].split("## Converter elements", 1)[0].split()
    )

    assert "do not report" in out_of_scope.lower()
    assert "check --deep" in out_of_scope
    assert "Missing character" in out_of_scope
    assert "Reason:" in out_of_scope


def test_rubric_keeps_the_json_contract_and_the_hint_instruction():
    assert '{"findings": [{"page": <printed page number>, "code": "<kebab-case>",' in RUBRIC
    assert '"severity": "error|warning|info"' in RUBRIC
    assert '"where": "table|figure|listing|heading|page-geometry|glyph"' in RUBRIC
    assert "markdown->TeX element most likely responsible" in RUBRIC
    for gone in ("table-overflow", "numbering-inconsistent", "glyph-missing"):
        assert gone not in RUBRIC
    for kept in ("table-illegible", "figure-small", "heading-orphan", "landscape-missing", "whitespace-gap"):
        assert kept in RUBRIC


# ---------------------------------------------------------------- 空回信重试与覆盖率


@pytest.mark.parametrize(
    "configured, expected_retry",
    [(DEFAULT_MAX_TOKENS, DEFAULT_MAX_TOKENS * 2), (30000, RETRY_MAX_TOKENS_CAP)],
)
@needs_poppler
def test_an_empty_truncated_reply_is_retried_once_with_double_the_tokens(
    tmp_path, configured, expected_retry, capsys
):
    """空正文 + finish_reason=length 是推理链吃光预算：把 max_tokens 翻倍重问一次（封顶）。"""
    path = prepare(tmp_path, pages=1, toc=TOC)
    bodies = []

    def transport(request):
        bodies.append(json.loads(request.body.decode("utf-8")))
        if len(bodies) == 1:
            return empty_reply(
                "length", usage={"prompt_tokens": 100, "completion_tokens": configured}
            )
        return chat_reply([], usage={"prompt_tokens": 100, "completion_tokens": 20})

    code = run_inspect(
        load_manifest(path),
        pages_spec="1",
        base_url="http://endpoint.invalid/v1",
        model="deepseek-flash",
        environ={"PRIMER_VISION_API_KEY": "secret"},
        max_tokens=configured,
        transport=transport,
    )

    assert code == 0
    assert len(bodies) == 2
    assert bodies[0]["max_tokens"] == configured
    assert bodies[1]["max_tokens"] == expected_retry
    assert expected_retry <= RETRY_MAX_TOKENS_CAP
    report = json.loads(
        (tmp_path / "_primer" / "book" / "inspect" / "inspect.findings.json").read_text(
            encoding="utf-8"
        )
    )
    # 第一次尝试仍留一条诊断记录，但这一页已经拿到裁决，不算未覆盖。
    assert [item["code"] for item in report["findings"]] == ["vision-truncated"]
    assert report["coverage"] == {
        "pages_sent": 1,
        "judged": 1,
        "undetermined": 0,
        "undetermined_pages": [],
    }
    printed = capsys.readouterr().out
    assert f"retry at max_tokens {expected_retry} (was {configured})" in printed
    assert "judged 1/1 page(s); 0 undetermined" in printed


@needs_poppler
def test_a_second_empty_reply_is_undetermined_and_the_run_does_not_pass(tmp_path, capsys):
    """重试再空就判未裁决：只烧两次请求，页面带页码落 error，退出码非零。"""
    path = prepare(tmp_path, pages=1, toc=TOC)
    bodies = []

    def transport(request):
        bodies.append(json.loads(request.body.decode("utf-8")))
        return empty_reply("length", usage={"prompt_tokens": 80, "completion_tokens": 32768})

    code = run_inspect(
        load_manifest(path),
        pages_spec="1",
        base_url="http://endpoint.invalid/v1",
        model="deepseek-flash",
        environ={"PRIMER_VISION_API_KEY": "secret"},
        transport=transport,
    )

    assert code == 1
    assert len(bodies) == 2
    report = json.loads(
        (tmp_path / "_primer" / "book" / "inspect" / "inspect.findings.json").read_text(
            encoding="utf-8"
        )
    )
    undetermined = [item for item in report["findings"] if item["code"] == "vision-undetermined"]
    assert len(undetermined) == 1
    assert undetermined[0]["severity"] == "error"
    assert undetermined[0]["location"] == "page 1"
    assert "empty reply after retry" in undetermined[0]["message"]
    assert report["coverage"] == {
        "pages_sent": 1,
        "judged": 0,
        "undetermined": 1,
        "undetermined_pages": [{"page": 1, "reason": "empty reply after retry"}],
    }
    assert report["failed"] is True
    assert "judged 0/1 page(s); 1 undetermined" in capsys.readouterr().out


@needs_poppler
def test_a_timed_out_call_is_recorded_as_undetermined_timeout(tmp_path):
    path = prepare(tmp_path, pages=1, toc=TOC)

    def transport(request):
        raise VisionCallError("cannot reach http://endpoint.invalid/v1/chat/completions: timed out")

    code = run_inspect(
        load_manifest(path),
        pages_spec="1",
        base_url="http://endpoint.invalid/v1",
        model="k3",
        environ={"PRIMER_VISION_API_KEY": "secret"},
        transport=transport,
    )

    assert code == 1
    report = json.loads(
        (tmp_path / "_primer" / "book" / "inspect" / "inspect.findings.json").read_text(
            encoding="utf-8"
        )
    )
    codes = [item["code"] for item in report["findings"]]
    assert codes[0] == "vision-failed"          # 诊断：这次调用本身失败了
    assert "vision-undetermined" in codes       # 台账：这一页没有被裁决
    assert report["coverage"]["undetermined_pages"] == [{"page": 1, "reason": "timeout"}]


@needs_poppler
def test_a_normal_reply_is_judged_and_the_coverage_reaches_both_reports(tmp_path, capsys):
    """正常回信不受影响：一次请求、全部裁决，计数同时进终端与两份 JSON/markdown。"""
    path = prepare(tmp_path, pages=2, toc=TOC)
    calls = []

    def transport(request):
        calls.append(request)
        return chat_reply([])

    code = run_inspect(
        load_manifest(path),
        pages_spec="1-2",
        base_url="http://endpoint.invalid/v1",
        model="k3",
        environ={"PRIMER_VISION_API_KEY": "secret"},
        transport=transport,
    )

    assert code == 0
    assert len(calls) == 1
    out = tmp_path / "_primer" / "book" / "inspect"
    report = json.loads((out / "inspect.findings.json").read_text(encoding="utf-8"))
    assert report["coverage"] == {
        "pages_sent": 2,
        "judged": 2,
        "undetermined": 0,
        "undetermined_pages": [],
    }
    assert [item["code"] for item in report["findings"]] == []
    assert "judged 2/2 page(s); 0 undetermined" in capsys.readouterr().out
    assert "judged 2/2 page(s); 0 undetermined" in (out / "inspect.md").read_text(encoding="utf-8")
