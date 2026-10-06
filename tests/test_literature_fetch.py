# -*- coding: utf-8 -*-
"""fetch 下载核心的单元测试：URL 归类、解析链、命名、校验、下载（opener 注入）。"""

from types import SimpleNamespace

from primer.literature.fetch import (
    classify_url,
    download_pdf,
    resolve_download_plan,
    safe_filename,
    unique_path,
    validate_pdf,
)


def _record(title="Some paper about detectors", *, year=2020, download_url="", eprint="", doi=""):
    return SimpleNamespace(title=title, year=year, download_url=download_url, eprint=eprint, doi=doi)


def test_classify_url_variants():
    assert classify_url("https://arxiv.org/abs/1703.01424") == (
        "direct",
        "https://arxiv.org/pdf/1703.01424",
    )
    assert classify_url("https://arxiv.org/pdf/1402.5163")[0] == "direct"
    assert classify_url("https://example.org/paper.pdf")[0] == "direct"
    assert classify_url("https://example.org/paper.pdf?download=1")[0] == "direct"
    assert classify_url("https://iopscience.iop.org/article/10.1086/676406/pdf")[0] == "direct"
    assert classify_url("https://www.nap.edu/download/12951")[0] == "direct"
    assert classify_url("https://doi.org/10.1038/nature21360") == (
        "manual",
        "https://doi.org/10.1038/nature21360",
    )
    assert classify_url("") == ("none", "")


def test_resolve_download_plan_priority():
    record = _record(eprint="arXiv:1703.01424", download_url="https://doi.org/10.1/x")
    plan = resolve_download_plan(record)
    assert plan["source"] == "arxiv-eprint"
    assert plan["url"] == "https://arxiv.org/pdf/1703.01424" and plan["expected"] == "direct"

    direct = _record(download_url="https://example.org/a.pdf")
    assert resolve_download_plan(direct)["expected"] == "direct"

    landing = _record(download_url="https://doi.org/10.1/x")
    plan = resolve_download_plan(landing)
    assert plan["expected"] == "manual" and plan["source"] == "doi-landing"

    empty = _record()
    plan = resolve_download_plan(empty)
    assert plan["expected"] == "none" and plan["source"] == "none"


def test_resolve_download_plan_live_lookup():
    record = _record(
        title="Seven temperate terrestrial planets around the nearby ultracool dwarf", year=2017
    )

    def engine(title):
        return [{
            "title": "Seven temperate terrestrial planets around the nearby ultracool dwarf",
            "year": 2017,
            "download_url": "https://arxiv.org/pdf/1703.01424",
        }]

    plan = resolve_download_plan(record, engines=[engine])
    assert plan["source"] == "live-lookup" and plan["expected"] == "direct"

    def broken(title):
        raise RuntimeError("engine down")

    assert resolve_download_plan(record, engines=[broken])["expected"] == "none"


def test_safe_filename_and_unique_path(tmp_path):
    assert safe_filename("Hello: World?", 2020) == "Hello_ World_2020.pdf"
    assert safe_filename("", None) == "download.pdf"
    long_name = safe_filename("x" * 200, None)
    assert len(long_name) <= 84 and long_name.endswith(".pdf")

    (tmp_path / "a.pdf").write_bytes(b"x")
    assert unique_path(tmp_path, "a.pdf").name == "a-2.pdf"


def test_validate_pdf(tmp_path):
    good = tmp_path / "good.pdf"
    good.write_bytes(b"%PDF-1.4\n" + b"y" * 50 + b"\n%%EOF\n")
    assert validate_pdf(good, check_pdfinfo=False) == (True, "")

    bad_head = tmp_path / "bad.pdf"
    bad_head.write_bytes(b"<html>...</html> %%EOF")
    ok, why = validate_pdf(bad_head, check_pdfinfo=False)
    assert not ok and "not a PDF" in why

    truncated = tmp_path / "tr.pdf"
    truncated.write_bytes(b"%PDF-1.4\nno end")
    ok, why = validate_pdf(truncated, check_pdfinfo=False)
    assert not ok and "truncated" in why


class _FakeResponse:
    def __init__(self, chunks, content_type="application/pdf"):
        self._chunks = list(chunks)
        self.headers = {"Content-Type": content_type}

    def read(self, size=-1):
        return self._chunks.pop(0) if self._chunks else b""

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def test_download_pdf_success_and_failures(tmp_path):
    import urllib.error

    pdf_bytes = b"%PDF-1.4\n" + b"x" * 100 + b"\n%%EOF\n"
    dest = tmp_path / "out.pdf"
    seen = []

    def opener(request, timeout=None):
        seen.append(request.full_url)
        return _FakeResponse([pdf_bytes[:10], pdf_bytes[10:]])

    result = download_pdf("https://example.org/a.pdf", dest, opener=opener, check_pdfinfo=False)
    assert result["ok"] and result["status"] == "downloaded"
    assert dest.read_bytes() == pdf_bytes and result["size"] == len(pdf_bytes)
    assert seen == ["https://example.org/a.pdf"]

    html_dest = tmp_path / "h.pdf"
    result = download_pdf(
        "https://example.org/h",
        html_dest,
        opener=lambda request, timeout=None: _FakeResponse([b"<html>"], "text/html; charset=utf-8"),
        check_pdfinfo=False,
    )
    assert result["status"] == "html" and not html_dest.exists()

    result = download_pdf(
        "https://example.org/b",
        tmp_path / "b.pdf",
        opener=lambda request, timeout=None: _FakeResponse([b"<html>not pdf %%EOF"]),
        check_pdfinfo=False,
    )
    assert result["status"] == "invalid" and not (tmp_path / "b.pdf").exists()

    result = download_pdf(
        "https://example.org/d",
        tmp_path / "d.pdf",
        opener=lambda request, timeout=None: _FakeResponse([b"%PDF-1.4\ntruncated"]),
        check_pdfinfo=False,
    )
    assert result["status"] == "invalid"

    result = download_pdf(
        "https://example.org/e",
        tmp_path / "e.pdf",
        opener=lambda request, timeout=None: _FakeResponse([b"z" * 64]),
        max_bytes=8,
        check_pdfinfo=False,
    )
    assert result["status"] == "too-large" and not (tmp_path / "e.pdf").exists()

    def blocked(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", None, None)

    result = download_pdf("https://example.org/c", tmp_path / "c.pdf", opener=blocked, check_pdfinfo=False)
    assert result["status"] == "blocked" and not (tmp_path / "c.pdf").exists()

    assert not list(tmp_path.glob("*.part"))
