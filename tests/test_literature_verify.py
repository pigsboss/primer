# -*- coding: utf-8 -*-
"""verify 的单元测试：命中判定、年份护栏、失败隔离、字段回填与防全败上报。

全部用假查询（不发网络）；SourceData 用真实的 markdown 解析器构造。
"""

from primer.literature.importers import parse_source
from primer.literature.verify import verify_source


def _source():
    md = (
        "# 参考\n"
        "## A\n"
        "[1] Author A. Title One. Journal X, 2003.\n"
        "[2] 只有一段没有句点\n"
    ).encode("utf-8")
    return parse_source("refs.md", md)


def test_verify_fills_fields_on_confident_match():
    def fake_lookup(title):
        if "title one" in title.lower():
            return [{
                "title": "Title One", "authors": ["Author A"], "year": 2003,
                "venue": "Journal X", "type": "journal-article", "doi": "10.1000/xyz",
            }]
        return []

    source = _source()
    report = verify_source(source, [0, 1], fake_lookup)

    assert report.matched == 1 and report.unmatched == 1 and report.failed == 0
    assert source.rows[0]["DOI"] == "10.1000/xyz"
    assert source.rows[0]["出处"] == "Journal X"
    assert source.rows[0]["作者"] == "Author A"
    assert "类型" in source.columns
    assert source.suggested["类型"] == "type"


def test_verify_year_guard_rejects_mismatch():
    def fake_lookup(title):
        return [{
            "title": "Title One", "authors": [], "year": 1999,
            "venue": "V", "type": "journal-article", "doi": "10.1/x",
        }]

    source = _source()
    report = verify_source(source, [0], fake_lookup)

    assert report.matched == 0 and report.unmatched == 1
    assert source.rows[0]["DOI"] == ""


def test_verify_partial_failure_counts_without_raising():
    def flaky_lookup(title):
        if "title one" in title.lower():
            return []
        raise RuntimeError("network hiccup")

    source = _source()
    report = verify_source(source, [0, 1], flaky_lookup)

    assert report.matched == 0 and report.unmatched == 1 and report.failed == 1
    assert source.rows[0]["标题"] == "Title One"


def test_verify_all_failures_raise_first_error():
    def boom(title):
        raise RuntimeError("network down")

    source = _source()
    raised = ""
    try:
        verify_source(source, [0, 1], boom)
    except RuntimeError as exc:
        raised = str(exc)
    assert raised == "network down"


def test_verify_engine_chain_fallbacks():
    good = {
        "title": "Title One", "authors": ["Author A"], "year": 2003,
        "venue": "Journal X", "type": "journal-article", "doi": "10.1000/xyz",
    }

    def broken(title):
        raise RuntimeError("engine down")

    def empty(title):
        return []

    def hit(title):
        return [good]

    source = _source()
    report = verify_source(source, [0], [broken, hit])
    assert report.matched == 1 and report.failed == 0
    assert source.rows[0]["DOI"] == "10.1000/xyz"

    source = _source()
    report = verify_source(source, [0], [empty, hit])
    assert report.matched == 1 and source.rows[0]["DOI"] == "10.1000/xyz"

    source = _source()
    report = verify_source(source, [0], [broken, empty])
    assert (report.matched, report.unmatched, report.failed) == (0, 1, 0)

    source = _source()
    raised = ""
    try:
        verify_source(source, [0, 1], [broken, broken])
    except RuntimeError as exc:
        raised = str(exc)
    assert raised == "engine down"


class _FakeResponse:
    """urlopen 成功响应的最小替身（上下文管理器 + read）。"""

    def __init__(self, payload):
        import json as _json

        self._body = _json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class _FakeHTTP:
    """urlopen 替身：按序返回预置响应（Exception 则抛出），并记录每次请求的 URL。"""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.urls: list[str] = []

    def __call__(self, request, timeout=None):
        self.urls.append(request.full_url)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return _FakeResponse(outcome)


def _openalex_payload():
    return {"results": [{
        "title": "Title One",
        "doi": "https://doi.org/10.1000/xyz",
        "publication_year": 2003,
        "authorships": [{"author": {"display_name": "Author A"}}],
        "primary_location": {"source": {"display_name": "Journal X"}},
        "type": "article",
        "biblio": {"volume": "1", "issue": "2", "first_page": "3", "last_page": "4"},
    }]}


def _http_429(url):
    import urllib.error

    return urllib.error.HTTPError(url, 429, "Too Many Requests", hdrs=None, fp=None)


def test_openalex_public_success_keeps_no_key(monkeypatch):
    import urllib.request

    from primer.literature.verify import OPENALEX_KEY_ENV, openalex_lookup

    monkeypatch.setenv(OPENALEX_KEY_ENV, "test-personal-key")
    fake = _FakeHTTP([_openalex_payload()])
    monkeypatch.setattr(urllib.request, "urlopen", fake)

    out = openalex_lookup("Title One")

    assert len(fake.urls) == 1 and "api_key" not in fake.urls[0]
    assert out[0]["title"] == "Title One" and out[0]["doi"] == "10.1000/xyz"


def test_openalex_429_falls_back_to_personal_key(monkeypatch):
    import urllib.request

    from primer.literature.verify import OPENALEX_KEY_ENV, openalex_lookup

    monkeypatch.setenv(OPENALEX_KEY_ENV, "test-personal-key")
    fake = _FakeHTTP([
        _http_429("https://api.openalex.org/works?search=x"),
        _openalex_payload(),
    ])
    monkeypatch.setattr(urllib.request, "urlopen", fake)

    out = openalex_lookup("Title One")

    assert len(fake.urls) == 2
    assert "api_key" not in fake.urls[0]
    assert "api_key=test-personal-key" in fake.urls[1]
    assert [candidate["title"] for candidate in out] == ["Title One"]


def test_openalex_429_without_key_raises_named_error(monkeypatch):
    import urllib.request

    from primer.literature.verify import OPENALEX_KEY_ENV, openalex_lookup

    monkeypatch.delenv(OPENALEX_KEY_ENV, raising=False)
    fake = _FakeHTTP([_http_429("https://api.openalex.org/works?search=x")])
    monkeypatch.setattr(urllib.request, "urlopen", fake)

    raised = ""
    try:
        openalex_lookup("Title One")
    except RuntimeError as exc:
        raised = str(exc)
    assert OPENALEX_KEY_ENV in raised
    assert len(fake.urls) == 1


def test_openalex_non_429_error_is_not_retried(monkeypatch):
    import urllib.error
    import urllib.request

    from primer.literature.verify import OPENALEX_KEY_ENV, openalex_lookup

    monkeypatch.setenv(OPENALEX_KEY_ENV, "test-personal-key")
    fake = _FakeHTTP([
        urllib.error.HTTPError("https://api.openalex.org/works?search=x", 500, "boom", None, None)
    ])
    monkeypatch.setattr(urllib.request, "urlopen", fake)

    raised = None
    try:
        openalex_lookup("Title One")
    except urllib.error.HTTPError as exc:
        raised = exc
    assert raised is not None and raised.code == 500
    assert len(fake.urls) == 1


def test_openalex_work_maps_open_access_and_metadata():
    from primer.literature.verify import _openalex_work

    work = _openalex_work({
        "title": "A review article",
        "doi": "https://doi.org/10.5194/nhess-25-747-2025",
        "publication_year": 2025,
        "authorships": [{"author": {"display_name": "A. Author"}}],
        "primary_location": {"source": {"display_name": "NHESS"}},
        "type": "review",
        "biblio": {"volume": "25", "issue": "2", "first_page": "747", "last_page": "780"},
        "open_access": {"oa_url": "https://example.org/oa"},
        "best_oa_location": {
            "pdf_url": "https://example.org/pdf",
            "landing_page_url": "https://example.org/landing",
        },
    })
    assert work["type"] == "journal-article"
    assert work["download_url"] == "https://example.org/pdf"
    assert work["pages"] == "747-780" and work["venue"] == "NHESS"

    with_arxiv = _openalex_work({
        "title": "T",
        "open_access": {},
        "best_oa_location": {"pdf_url": "https://repo.example/a.pdf"},
        "locations": [
            {"is_oa": True, "pdf_url": "https://arxiv.org/pdf/1703.01424"},
            {"is_oa": False, "pdf_url": "https://closed.example/x.pdf"},
        ],
    })
    assert with_arxiv["download_url"] == "https://arxiv.org/pdf/1703.01424"

    closed = _openalex_work({"title": "T", "open_access": None, "best_oa_location": None})
    assert closed["download_url"] is None
