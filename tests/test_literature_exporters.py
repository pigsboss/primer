# -*- coding: utf-8 -*-
"""exporters：BibLaTeX／RIS／图书馆清单的映射、转义、引文键与著录拼装。"""

from types import SimpleNamespace

from primer.literature.exporters import (
    citation_text,
    to_biblatex,
    to_library_list,
    to_ris,
)
from primer.literature.library import Record


def _record(uuid: str = "r1", **overrides) -> Record:
    payload = {
        "uuid": uuid,
        "type": "journal-article",
        "title": "Seven temperate planets",
        "authors": ["Gillon M.", "Triaud A.H.M.J."],
        "year": 2017,
        "venue": "Nature",
        "doi": "10.1038/nature21360",
        "pages": "456–459",
        "volume": "542",
        "number": "7642",
        "notes": "原始记录：Gillon M. Seven temperate planets. Nature, 2017.",
    }
    payload.update(overrides)
    return Record.from_dict(payload)


def test_biblatex_mapping_and_escaping():
    record = _record(title="A & B {test} 100%", keywords="行星; 探测", eprint="1703.01424")
    text = to_biblatex([record])
    assert "@article{gillon2017test," in text
    assert "title = {A \\& B \\{test\\} 100\\%}" in text
    assert "author = {Gillon M. and Triaud A.H.M.J.}" in text
    assert "journaltitle = {Nature}" in text
    assert "date = {2017}" in text
    assert "pages = {456--459}" in text
    assert "doi = {10.1038/nature21360}" in text
    assert "eprinttype = {arXiv}" in text and "eprint = {1703.01424}" in text
    assert "keywords = {行星, 探测}" in text

    classic = to_biblatex([record], classic=True)
    assert "journal = {Nature}" in classic and "journaltitle" not in classic
    assert "year = {2017}" in classic and "date = {" not in classic
    assert "archiveprefix = {arXiv}" in classic and "eprinttype" not in classic


def test_biblatex_key_fallback_collision_and_notes_files():
    chinese = _record(
        "abcd1234", type="book", authors=["傅承义"], title="地球十讲", year=1976, venue="科学出版社"
    )
    first = _record("r2")
    second = _record("r3")
    text = to_biblatex(
        [chinese, first, second],
        notes=True,
        files={"r2": ["pdf/ref.pdf"]},
    )
    assert "@book{refabcd1234," in text
    assert "@article{gillon2017seven," in text
    assert "@article{gillon2017sevena," in text  # 冲突加 a
    assert "note = {原始记录：" in text
    assert "file = {:pdf/ref.pdf:PDF}" in text


def test_ris_mapping_and_page_split():
    text = to_ris([_record()])
    assert text.startswith("TY  - JOUR\n")
    assert "AU  - Gillon M.\n" in text and text.count("AU  - ") == 2
    assert "TI  - Seven temperate planets" in text
    assert "JO  - Nature" in text
    assert "PY  - 2017" in text
    assert "VL  - 542" in text and "IS  - 7642" in text
    assert "SP  - 456" in text and "EP  - 459" in text
    assert "DO  - 10.1038/nature21360" in text
    assert text.rstrip().endswith("ER  -")

    conf = to_ris([_record(type="conference-paper", venue="SPIE", doi=None)])
    assert "TY  - CONF" in conf and "T2  - SPIE" in conf

    report = to_ris([_record(type="report", venue="NASA", doi=None)])
    assert "TY  - RPRT" in report and "PB  - NASA" in report


def test_citation_text_and_fallback():
    record = _record()
    assert citation_text(record).startswith(
        "Gillon M., Triaud A.H.M.J. Seven temperate planets."
    )
    assert "Nature, 2017, 542(7642): 456–459." in citation_text(record)

    fallback = SimpleNamespace(
        uuid="r9", title="", authors=[], year=None, venue="",
        notes="原始记录：某白皮书. 2021.",
    )
    assert citation_text(fallback) == "某白皮书. 2021."

    junk = _record("r10", title="张衡一号卫星工程", authors=["中国地震局"], year=None,
                   venue="**M3 统计地震学与临界现象（宏观系综视角）**")
    cleaned = citation_text(junk)
    assert "M3 统计地震学与临界现象（宏观系综视角）" in cleaned
    assert "**" not in cleaned


def test_library_list_md_and_csv():
    article = _record("r1")
    report = _record("r2", type="report", title="Survey paper", authors=["NASA"], year=2020,
                     venue="NASA", doi=None)
    book = SimpleNamespace(
        uuid="r3", title="", authors=[], year=None, venue="", type="report",
        notes="原始记录：某白皮书. 2021.",
    )
    md, csv_text = to_library_list([article, report, book], group_by="venue")
    assert "# 图书馆原文获取清单" in md
    assert "共 3 条" in md
    assert "## Nature（1 条）" in md
    assert "## NASA（1 条）" in md
    assert "## 未注明刊名/出版者（1 条）" in md
    assert "DOI：10.1038/nature21360" in md
    assert "获取结果：＿＿＿＿＿＿" in md
    lines = csv_text.splitlines()
    assert lines[0] == "序号,著录,DOI,URL,类型,年份,获取结果,备注"
    assert len(lines) == 4
    assert "某白皮书. 2021." in lines[3]

    by_year, _ = to_library_list([article, report], group_by="year")
    assert "## 2020（1 条）" in by_year and "## 2017（1 条）" in by_year

    no_links, csv_plain = to_library_list([article], links=False)
    assert "链接：" not in no_links
    assert ",," in csv_plain  # URL 列为空

    junk = _record("r4", venue="**M3 组**")
    md2, _ = to_library_list([junk])
    assert "## M3 组（1 条）" in md2
