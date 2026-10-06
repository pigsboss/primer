# -*- coding: utf-8 -*-
"""primer.literature.engines：引擎解析的纯函数与引擎链装配（不碰网络）。"""

from __future__ import annotations

from primer.literature.engines import _ads_doc, _clean_title
from primer.literature.web import server as S


def test_ads_doc_maps_fields():
    candidate = _ads_doc({
        "title": ["Kepler Planet-Detection Mission: Introduction and First Results"],
        "author": ["Borucki, William J.", "Koch, David"],
        "year": "2010",
        "pub": "Science",
        "doi": ["10.1126/science.1185402"],
        "doctype": "article",
        "volume": "327",
        "issue": "5968",
        "page": "977-980",
        "issn": ["0036-8075"],
    })
    assert candidate["title"] == "Kepler Planet-Detection Mission: Introduction and First Results"
    assert candidate["doi"] == "10.1126/science.1185402"
    assert candidate["year"] == 2010
    assert candidate["authors"] == ["Borucki, William J.", "Koch, David"]
    assert candidate["venue"] == "Science"
    assert candidate["type"] == "journal-article"
    assert candidate["volume"] == "327"
    assert candidate["number"] == "5968"
    assert candidate["pages"] == "977-980"
    assert candidate["issn"] == "0036-8075"
    assert candidate["isbn"] is None and candidate["publisher"] is None

    eprint = _ads_doc({"title": ["A preprint"], "doctype": "eprint"})
    assert eprint["type"] == "preprint"
    assert eprint["year"] is None and eprint["doi"] is None and eprint["venue"] is None


def test_ads_doc_arxiv_eprint_and_links_data():
    from primer.literature.engines import _ads_doc

    doc = _ads_doc({
        "title": ["Seven temperate terrestrial planets"],
        "identifier": ["2017Natur.542..456G", "arXiv:1703.01424", "10.1038/nature21360"],
        "links_data": ['{"type": "simbad", "url": "https://simbad.u-strasbg.fr/x"}'],
    })
    assert doc["eprint"] == "1703.01424"
    assert doc["download_url"] == "https://arxiv.org/pdf/1703.01424"

    pdf_only = _ads_doc({
        "title": ["Something"],
        "identifier": ["2013HEAD...1330201D"],
        "links_data": ['not json', '{"type": "esource", "url": "https://example.org/a.pdf"}'],
    })
    assert pdf_only["eprint"] is None
    assert pdf_only["download_url"] == "https://example.org/a.pdf"

    none_at_all = _ads_doc({"title": ["Nothing here"]})
    assert none_at_all["download_url"] is None and none_at_all["eprint"] is None


def test_crossref_item_maps_biblatex_fields():
    from primer.literature.engines import _crossref_item

    candidate = _crossref_item({
        "DOI": "10.1126/science.1185402",
        "title": ["Kepler <i>x</i> &amp; y"],
        "issued": {"date-parts": [[2010, 3]]},
        "author": [{"given": "William J.", "family": "Borucki"}],
        "editor": [{"given": "E.", "family": "Ditor"}],
        "container-title": ["Science"],
        "type": "journal-article",
        "volume": "327",
        "issue": "5968",
        "page": "977-980",
        "publisher": "AAAS",
        "ISBN": ["978-1"],
        "ISSN": ["0036-8075"],
        "URL": "https://doi.org/10.1126/science.1185402",
        "event": {"name": "AAAS Meeting", "location": "San Diego"},
        "institution": [{"name": "NASA"}],
    })
    assert candidate["title"] == "Kepler x & y"
    assert candidate["volume"] == "327" and candidate["number"] == "5968"
    assert candidate["pages"] == "977-980"
    assert candidate["editor"] == ["E. Ditor"]
    assert candidate["isbn"] == "978-1" and candidate["issn"] == "0036-8075"
    assert candidate["eventtitle"] == "AAAS Meeting" and candidate["location"] == "San Diego"
    assert candidate["institution"] == "NASA"
    assert candidate["publisher"] == "AAAS"


def test_crossref_item_pdf_link():
    from primer.literature.engines import _crossref_item

    item = _crossref_item({
        "DOI": "10.1/x",
        "title": ["T"],
        "link": [
            {"content-type": "text/html", "URL": "https://x/landing"},
            {"content-type": "application/pdf", "URL": "https://x/paper.pdf"},
        ],
    })
    assert item["download_url"] == "https://x/paper.pdf"
    bare = _crossref_item({"DOI": "10.1/y", "title": ["U"], "link": ["oops"]})
    assert bare["download_url"] is None


def test_clean_title_strips_markup_and_entities():
    assert _clean_title(["Atom &amp; cosmos <i>v2</i>"]) == "Atom & cosmos v2"
    assert _clean_title([]) == ""


def test_build_engines_token_gate(monkeypatch, tmp_path):
    service = S.LibraryService.initial(str(tmp_path / "nope.json"))
    monkeypatch.delenv("PRIMER_NASA_ADS_API_KEY", raising=False)
    assert [fn.__name__ for fn in service._build_engines()] == [
        "openalex_lookup",
        "crossref_lookup",
    ]
    monkeypatch.setenv("PRIMER_NASA_ADS_API_KEY", "test-token")
    assert [fn.__name__ for fn in service._build_engines()] == [
        "openalex_lookup",
        "ads_lookup",
        "crossref_lookup",
    ]


def test_crossref_doi_payload_title_mapping():
    from primer.literature.engines import _crossref_doi_payload_title

    assert (
        _crossref_doi_payload_title({"message": {"title": ["Kepler <i>x</i> &amp; y"]}})
        == "Kepler x & y"
    )
    assert _crossref_doi_payload_title({"message": {}}) == ""
    assert _crossref_doi_payload_title({"status": "ok"}) == ""
