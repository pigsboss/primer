# -*- coding: utf-8 -*-
"""importers 的单元测试：CSV／JSON／BibTeX 解析、建议映射与行转换。"""

import json

import pytest

from primer.literature.importers import (
    parse_source,
    rows_to_payloads,
    split_authors,
)
from primer.literature.library import LibraryError


def test_csv_parse_with_bom_and_header():
    data = "\ufeff标题,作者,年份\n甲,张三; 李四,2022\n乙,王五,\n".encode("utf-8")
    source = parse_source("x.csv", data)

    assert source.kind == "table"
    assert source.total == 2
    assert source.columns == ["标题", "作者", "年份"]
    assert source.sample["标题"] == ["甲", "乙"]
    assert source.suggested["标题"] == "title"
    assert source.suggested["作者"] == "authors"
    assert source.suggested["年份"] == "year"


def test_csv_semicolon_delimiter_sniffed():
    source = parse_source("x.csv", b"title;author\nA;B\n")
    assert source.columns == ["title", "author"]


def test_json_library_detection():
    payload = [{"uuid": "u-1", "title": "记录"}]
    source = parse_source("db.json", json.dumps(payload).encode("utf-8"))

    assert source.kind == "library"
    assert source.total == 1
    assert source.records[0].title == "记录"


def test_json_table_falls_back_to_mapping():
    payload = [{"篇名": "A", "作者": "B"}]
    source = parse_source("x.json", json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    assert source.kind == "table"
    assert source.columns == ["篇名", "作者"]
    assert source.suggested["篇名"] == "title"


def test_bib_parse_entries_and_cleanup():
    text = """
@article{key1,
  title = {The {DNA} Story},
  author = {Last, First and Second, Author},
  year = 2022,
  journal = "Nature",
  doi = {10.1000/xyz},
}
@inproceedings{key2,
  title = {Another},
  author = "Solo",
  year = {2021},
}
"""
    source = parse_source("x.bib", text.encode("utf-8"))

    assert source.kind == "table"
    assert source.total == 2
    assert "entrytype" in source.columns
    first = source.rows[0]
    assert first["title"] == "The DNA Story"
    assert first["journal"] == "Nature"
    assert first["doi"] == "10.1000/xyz"
    assert first["entrytype"] == "article"


def test_unsupported_extension():
    with pytest.raises(LibraryError, match="unsupported"):
        parse_source("x.docx", b"whatever")


def test_empty_source_rejected():
    with pytest.raises(LibraryError, match="no records"):
        parse_source("x.csv", b"title,author\n")


def test_split_authors():
    assert split_authors("A and B") == ["A", "B"]
    assert split_authors("张三；李四") == ["张三", "李四"]
    assert split_authors(["A", "B"]) == ["A", "B"]
    assert split_authors("Last, First") == ["Last, First"]
    assert split_authors("") == []


def test_suggestions_containment_and_ignore():
    source = parse_source(
        "x.csv", b"Publication Year,Author Full Names,Random\n2022,X,1\n"
    )
    assert source.suggested["Publication Year"] == "year"
    assert source.suggested["Author Full Names"] == "authors"
    assert source.suggested["Random"] == "ignore"


def test_rows_to_payloads_mapping_and_skip():
    rows = [
        {"标题": "甲", "作者": "张三; 李四", "年份": "2022-01-01", "DOI": "10.1000/x"},
        {"标题": "", "作者": "王五", "年份": "", "DOI": ""},
        {"标题": "丙", "作者": "", "年份": "不是年份", "DOI": ""},
    ]
    columns = ["标题", "作者", "年份", "DOI"]
    mapping = {"标题": "title", "作者": "authors", "年份": "year", "DOI": "doi"}

    payloads, skipped = rows_to_payloads(rows, columns, mapping)

    assert skipped == 1
    assert len(payloads) == 2
    first = payloads[0]
    assert first["title"] == "甲"
    assert first["authors"] == ["张三", "李四"]
    assert first["year"] == 2022
    assert first["doi"] == "10.1000/x"
    assert first["uuid"]
    second = payloads[1]
    assert second["title"] == "丙"
    assert "year" not in second
    assert "authors" not in second


def test_markdown_reference_list_parse():
    md = (
        "# 综述参考文献\n"
        "> 共 2 条\n"
        "## A 战略报告与规划\n"
        "[1] Author A. Title One. Journal X, 2003. ［原文：待图书馆获取］\n"
        "[2] 张三, 李四. 标题二. 中文期刊, 2020.\n"
    ).encode("utf-8")

    source = parse_source("refs.md", md)

    assert source.kind == "table"
    assert source.total == 2
    assert source.columns == ["编号", "分类", "作者", "标题", "年份", "出处", "DOI", "arXiv", "原文"]
    assert source.suggested["作者"] == "authors"
    assert source.suggested["标题"] == "title"
    assert source.suggested["年份"] == "year"
    assert source.suggested["出处"] == "venue"
    assert source.rows[0]["年份"] == "2003"
    assert source.rows[0]["分类"] == "A 战略报告与规划"
    assert source.rows[0]["原文"] == "待图书馆获取"


def test_markdown_pipe_table_fallback():
    md = "| 标题 | 作者 |\n| --- | --- |\n| 甲 | 乙 |\n".encode("utf-8")

    source = parse_source("table.md", md)

    assert source.kind == "table"
    assert source.columns == ["标题", "作者"]
    assert source.rows == [{"标题": "甲", "作者": "乙"}]


def test_markdown_without_entries_rejected():
    with pytest.raises(LibraryError, match="no records"):
        parse_source("empty.md", "# 只有标题\n".encode("utf-8"))


def test_rows_to_payloads_appends_original_record_note():
    rows = [{"标题": "甲", "备注列": "自己写的"}]
    columns = ["标题", "备注列"]
    mapping = {"标题": "title", "备注列": "notes"}

    payloads, skipped = rows_to_payloads(rows, columns, mapping, raws=["原始A. 来源B."])
    assert skipped == 0
    assert payloads[0]["notes"] == "自己写的\n原始记录：原始A. 来源B."

    payloads2, _ = rows_to_payloads(rows, columns, mapping)
    assert payloads2[0]["notes"].startswith("自己写的\n原始记录：")
    assert "标题: 甲" in payloads2[0]["notes"]


def test_rows_to_payloads_maps_biblatex_fields():
    rows = [{
        "题名": "T",
        "卷": "14",
        "页码": "45-67",
        "编者": "E1; E2",
        "出版社": "科学出版社",
        "arXiv": "2201.00001",
    }]
    columns = ["题名", "卷", "页码", "编者", "出版社", "arXiv"]
    mapping = {
        "题名": "title",
        "卷": "volume",
        "页码": "pages",
        "编者": "editor",
        "出版社": "publisher",
        "arXiv": "eprint",
    }

    payloads, skipped = rows_to_payloads(rows, columns, mapping)

    assert skipped == 0
    payload = payloads[0]
    assert payload["title"] == "T"
    assert payload["volume"] == "14" and payload["pages"] == "45-67"
    assert payload["editor"] == ["E1", "E2"]
    assert payload["publisher"] == "科学出版社"
    assert payload["eprint"] == "2201.00001"


def test_suggestions_cover_biblatex_columns():
    source = parse_source("x.csv", "卷,期,页码,arXiv,ISBN,关键词,编者\n14,3,45-67,2201.1,978-1,复用,张三\n".encode("utf-8"))
    assert source.suggested["卷"] == "volume"
    assert source.suggested["期"] == "number"
    assert source.suggested["页码"] == "pages"
    assert source.suggested["arXiv"] == "eprint"
    assert source.suggested["ISBN"] == "isbn"
    assert source.suggested["关键词"] == "keywords"
    assert source.suggested["编者"] == "editor"


def test_json_library_with_file_records_is_detected():
    payload = [
        {"uuid": "11111111-1111-4111-8111-111111111111", "title": "T"},
        {
            "kind": "file",
            "uuid": "55555555-5555-4555-8555-555555555555",
            "path": "/tmp/a.pdf",
            "name": "a.pdf",
            "size": 1,
        },
    ]
    source = parse_source("db.json", json.dumps(payload).encode("utf-8"))
    assert source.kind == "library"
    assert len(source.records) == 1 and len(source.file_records) == 1
    assert source.file_records[0].name == "a.pdf"
