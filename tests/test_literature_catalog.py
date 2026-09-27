# -*- coding: utf-8 -*-
"""catalog 与 config 的单元测试：发现、哈希、去重、引文挂接与 YAML 解析。"""

import hashlib
from pathlib import Path

import pytest

from primer.literature.catalog import (
    build_catalog,
    file_md5,
    iter_pdf_paths,
    load_reflib_log,
    normalize_arxiv,
    parse_filename,
)
from primer.literature.config import apply_overrides, load_config


def _write(path: Path, payload: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def test_iter_pdf_paths_is_recursive_case_insensitive_and_sorted(tmp_path):
    _write(tmp_path / "b.PDF", b"b")
    _write(tmp_path / "a.pdf", b"a")
    _write(tmp_path / "nested" / "c.pdf", b"c")
    _write(tmp_path / "notes.txt", b"skip")
    _write(tmp_path / ".hidden" / "d.pdf", b"skip")
    _write(tmp_path / "__pycache__" / "e.pdf", b"skip")

    found = iter_pdf_paths([tmp_path])

    assert found == [
        tmp_path / "a.pdf",
        tmp_path / "b.PDF",
        tmp_path / "nested" / "c.pdf",
    ]


def test_iter_pdf_paths_dedups_overlapping_roots(tmp_path):
    _write(tmp_path / "a.pdf", b"a")
    _write(tmp_path / "nested" / "b.pdf", b"b")

    found = iter_pdf_paths([tmp_path, tmp_path / "nested"])

    assert len(found) == 2


def test_file_md5_matches_hashlib_and_streams(tmp_path):
    path = _write(tmp_path / "a.pdf", b"x" * 5000)

    assert file_md5(path) == hashlib.md5(b"x" * 5000).hexdigest()
    assert file_md5(path, chunk_size=7) == hashlib.md5(b"x" * 5000).hexdigest()


def test_build_catalog_marks_duplicates_and_reports_bytes(tmp_path):
    first = _write(tmp_path / "a.pdf", b"same")
    duplicate = _write(tmp_path / "nested" / "b.pdf", b"same")
    _write(tmp_path / "c.pdf", b"different")

    catalog = build_catalog([tmp_path])

    assert len(catalog.records) == 3
    assert len(catalog.unique) == 2
    assert len(catalog.duplicates) == 1
    assert catalog.duplicates[0].path == duplicate
    assert catalog.duplicates[0].duplicate_of == first
    assert catalog.total_bytes == 4 + 4 + 9
    assert catalog.unique_bytes == 4 + 9
    assert catalog.redundant_bytes == 4
    assert catalog.duplicate_groups == 1
    assert len(catalog.by_root()[tmp_path]) == 3

    rel_paths = {record.rel_path for record in catalog.records}
    assert rel_paths == {"a.pdf", "c.pdf", str(Path("nested") / "b.pdf")}


def test_build_catalog_without_hash_skips_dedup(tmp_path):
    _write(tmp_path / "a.pdf", b"same")
    _write(tmp_path / "b.pdf", b"same")

    catalog = build_catalog([tmp_path], hash_files=False)

    assert [record.md5 for record in catalog.records] == ["", ""]
    assert catalog.duplicates == []
    assert catalog.duplicate_groups == 0


def test_build_catalog_reports_progress(tmp_path):
    _write(tmp_path / "a.pdf", b"a")
    _write(tmp_path / "b.pdf", b"b")
    seen = []

    build_catalog([tmp_path], on_progress=lambda done, total, path: seen.append((done, total, path.name)))

    assert seen == [(1, 2, "a.pdf"), (2, 2, "b.pdf")]


def test_parse_filename_ref_class_title():
    citation = parse_filename("020_A_Ice_Giants_Pre-Decadal_Survey.pdf")

    assert citation is not None
    assert citation.ref == "020"
    assert citation.cls == "A"
    assert citation.title == "Ice Giants Pre-Decadal Survey"
    assert citation.arxiv is None
    assert citation.source == "filename"


def test_parse_filename_arxiv_suffix():
    citation = parse_filename("MISC_R_arxiv2206_06693.pdf")

    assert citation is not None
    assert citation.cls == "R"
    assert citation.arxiv == "2206.06693"
    assert citation.ref is None

    numbered = parse_filename("040_R_arxiv2205_10510.pdf")
    assert numbered is not None
    assert numbered.ref == "040"
    assert numbered.arxiv == "2205.10510"


def test_parse_filename_unmatched_returns_none():
    assert parse_filename("astro2020.pdf") is None
    assert parse_filename("从L5到L6：宇宙标尺.pdf") is None


@pytest.mark.parametrize(
    "name, arxiv, ref, cls",
    [
        ("paper_arxiv1301_6674.pdf", "1301.6674", None, None),
        ("arxiv1404.7495.pdf", "1404.7495", None, None),
        ("123_R_arxiv2205_10510_extra.pdf", "2205.10510", "123", "R"),
        ("ARXIV0705-0993_something.pdf", "0705.0993", None, None),
        ("MISC_R_arxiv2310_11168.pdf", "2310.11168", None, "R"),
        ("040_R_arxiv2205_10510.pdf", "2205.10510", "040", "R"),
    ],
)
def test_parse_filename_extracts_arxiv_from_anywhere(name, arxiv, ref, cls):
    citation = parse_filename(name)

    assert citation is not None
    assert citation.arxiv == arxiv
    assert citation.ref == ref
    assert citation.cls == cls


@pytest.mark.parametrize(
    "raw, normalized",
    [
        ("arXiv:0705.0993", "0705.0993"),
        ("arXiv:1301.6674v2", "1301.6674"),
        ("1404_7495", "1404.7495"),
        (" 2206.07211 ", "2206.07211"),
        ("no-arxiv", None),
        ("", None),
        ("2020", None),
    ],
)
def test_normalize_arxiv(raw, normalized):
    assert normalize_arxiv(raw) == normalized


def test_build_catalog_joins_reflib_log_by_saved_path(tmp_path):
    pdf = _write(tmp_path / "020_A_Foo.pdf", b"x")
    log = tmp_path / "reflib_download_log.csv"
    log.write_text(
        "ref,class,title,arxiv,status,saved\n"
        f"20,A,Foo,2511.13946,ok(1KB),{pdf}\n"
        "1,A,No File,,no-arxiv,\n",
        encoding="utf-8",
    )

    catalog = build_catalog([tmp_path], reflib_log=log)

    citation = catalog.records[0].citation
    assert citation is not None
    assert citation.source == "reflib_log"
    assert citation.ref == "20"
    assert citation.arxiv == "2511.13946"


def test_load_reflib_log_ignores_rows_without_saved(tmp_path):
    log = tmp_path / "log.csv"
    log.write_text("ref,class,title,arxiv,status,saved\n1,A,T,,no-arxiv,\n", encoding="utf-8")

    assert load_reflib_log(log) == {}


def test_load_config_defaults_are_valid():
    config = load_config()

    assert config.roots
    assert all(root.is_dir() for root in config.roots)
    assert config.state_path == config.output_dir / "state.jsonl"
    assert config.staging_dir == config.output_dir / "staging"


def test_load_config_resolves_relative_paths_against_yaml_dir(tmp_path):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    manifest = config_dir / "literature.yaml"
    manifest.write_text(
        "roots:\n  - ../corpus\noutput_dir: ../out\ntier: advanced\n",
        encoding="utf-8",
    )

    config = load_config(manifest)

    assert config.roots == [corpus.resolve()]
    assert config.output_dir == (tmp_path / "out").resolve()
    assert config.tier == "advanced"


@pytest.mark.parametrize(
    "yaml_text, field",
    [
        ("roots: []\n", "roots"),
        ("roots:\n  - /no/such/dir\n", "roots"),
        ("chunk_size: 0\n", "chunk_size"),
        ("tier: turbo\n", "tier"),
        ("output_format: docx\n", "output_format"),
    ],
)
def test_load_config_rejects_invalid_fields(tmp_path, yaml_text, field):
    manifest = tmp_path / "literature.yaml"
    manifest.write_text(yaml_text, encoding="utf-8")

    with pytest.raises(ValueError, match=field):
        load_config(manifest)


def test_apply_overrides_takes_precedence(tmp_path):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    manifest = tmp_path / "literature.yaml"
    manifest.write_text(f"roots:\n  - {corpus}\n", encoding="utf-8")
    other = tmp_path / "other"
    other.mkdir()

    config = apply_overrides(load_config(manifest), roots=[str(other)], tier="flash")

    assert config.roots == [other.resolve()]
    assert config.tier == "flash"
