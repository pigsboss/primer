# -*- coding: utf-8 -*-
"""清单载入与校验：必填字段、路径基准、篇号唯一、源文件可读、引用合法。"""

import pytest

from book_fixtures import MANIFEST, tiny_book, write_manifest, write_sources

from primer.book import ManifestError, load_manifest


def test_loads_valid_manifest_and_resolves_paths(tmp_path):
    path = tiny_book(tmp_path)

    manifest = load_manifest(path)

    assert manifest.book.title == "微型书"
    assert manifest.project_root == tmp_path.resolve()
    assert manifest.source_root == (tmp_path / "sources").resolve()
    assert manifest.bibliography == (tmp_path / "sources" / "refs.md").resolve()
    assert [volume.id for volume in manifest.volumes] == ["V1", "VA"]
    assert manifest.body_order == ("V1", "VA", "@bibliography")
    assert manifest.volume("VA").part_label == "appA"
    assert manifest.volume("VA").appendix is True
    assert manifest.volume("V1").appendix is False
    assert manifest.volume("V1").sources[0].path.name == "one.md"
    assert manifest.scheme("plain").rules[0].numbers == ("g1",)
    assert len(manifest.drop_lines) == 1


def test_output_defaults_to_the_underscore_primer_tree(tmp_path):
    path = tiny_book(tmp_path)

    manifest = load_manifest(path)

    assert manifest.output.directory == (tmp_path / "_primer" / "book").resolve()
    assert manifest.output.engine_runs == 2
    assert manifest.typography.table_font_size == "-4"
    assert manifest.typography.toc_depth == 1


def test_explicit_output_directory_is_relative_to_the_project_root(tmp_path):
    path = tiny_book(tmp_path, MANIFEST.replace("  jobname: tiny", "  directory: build/out\n  jobname: tiny"))

    manifest = load_manifest(path)

    assert manifest.output.directory == (tmp_path / "build" / "out").resolve()


def test_project_root_rebases_source_root(tmp_path):
    book = tmp_path / "_primer" / "book"
    book.mkdir(parents=True)
    sources = tmp_path / "成果文件"
    sources.mkdir()
    (sources / "one.md").write_text("# 一\n\n正文。\n", encoding="utf-8")
    (sources / "refs.md").write_text("[1] 条目。\n", encoding="utf-8")
    text = """\
book:
  title: 微型书

project_root: ../..
source_root: 成果文件

bibliography:
  file: refs.md

output:
  jobname: tiny

volumes:
  - id: V1
    title: 第一篇
    sources:
      - file: one.md

body_order: [V1, "@bibliography"]
"""
    manifest_path = write_manifest(book, text)

    manifest = load_manifest(manifest_path)

    assert manifest.project_root == tmp_path.resolve()
    assert manifest.source_root == sources.resolve()
    assert manifest.output.directory == (tmp_path / "_primer" / "book").resolve()


def test_paths_are_relative_to_the_manifest_not_the_cwd(tmp_path, monkeypatch):
    path = tiny_book(tmp_path)
    monkeypatch.chdir(tmp_path.parent)

    manifest = load_manifest(path)

    assert manifest.source_root == (tmp_path / "sources").resolve()


def test_missing_book_title_is_rejected(tmp_path):
    write_sources(tmp_path)
    write_manifest(tmp_path, MANIFEST.replace("  title: 微型书\n", ""))

    with pytest.raises(ManifestError, match=r"manifest\.book\.title is required"):
        load_manifest(tmp_path / "book.yaml")


def test_duplicate_volume_ids_are_rejected(tmp_path):
    path = tiny_book(tmp_path, MANIFEST.replace("  - id: VA\n", "  - id: V1\n"))

    with pytest.raises(ManifestError, match="duplicate volume id: V1"):
        load_manifest(path)


def test_missing_source_file_is_rejected(tmp_path):
    path = tiny_book(tmp_path, MANIFEST.replace("file: two.md", "file: missing.md"))

    with pytest.raises(ManifestError, match="is not readable"):
        load_manifest(path)


def test_unknown_top_level_key_is_rejected(tmp_path):
    path = tiny_book(tmp_path, MANIFEST + "\nvolumes2: []\n")

    with pytest.raises(ManifestError, match="unknown key"):
        load_manifest(path)


def test_unknown_scheme_reference_is_rejected(tmp_path):
    path = tiny_book(tmp_path, MANIFEST.replace("scheme: plain", "scheme: nope"))

    with pytest.raises(ManifestError, match="scheme is unknown: nope"):
        load_manifest(path)


def test_unknown_body_order_entry_is_rejected(tmp_path):
    path = tiny_book(tmp_path, MANIFEST.replace("[V1, VA,", "[V1, V2,"))

    with pytest.raises(ManifestError, match="unknown volume: V2"):
        load_manifest(path)


def test_volume_missing_from_body_order_is_rejected(tmp_path):
    path = tiny_book(tmp_path, MANIFEST.replace("[V1, VA,", "[V1,"))

    with pytest.raises(ManifestError, match="volumes missing from body_order: VA"):
        load_manifest(path)


def test_bibliography_placeholder_requires_a_bibliography(tmp_path):
    text = MANIFEST.replace(
        "bibliography:\n  file: refs.md\n  section_title: 总参考文献列表\n", ""
    )
    path = tiny_book(tmp_path, text)

    with pytest.raises(ManifestError, match="no bibliography is configured"):
        load_manifest(path)


def test_invalid_rule_regex_is_rejected(tmp_path):
    path = tiny_book(tmp_path, MANIFEST.replace("match: '^## 1\\.(\\d+)", "match: '^## 1\\.(\\d+"))

    with pytest.raises(ManifestError, match="not a valid regular expression"):
        load_manifest(path)


def test_rule_offsets_must_refer_to_a_numbered_group(tmp_path):
    path = tiny_book(
        tmp_path,
        MANIFEST.replace(
            "numbers: [g1]\n      - match", "numbers: [g1]\n        offsets: {g2: 5}\n      - match"
        ),
    )

    with pytest.raises(ManifestError, match="has no matching entry in numbers"):
        load_manifest(path)


def test_template_must_only_use_capture_groups(tmp_path):
    path = tiny_book(
        tmp_path, MANIFEST.replace("template: '## 第 {g1} 章　{g2}'", "template: '## 第 {g9} 章'")
    )

    with pytest.raises(ManifestError, match="refers to a capture group the pattern lacks"):
        load_manifest(path)


def test_citation_mode_is_validated(tmp_path):
    path = tiny_book(
        tmp_path,
        MANIFEST.replace("    standalone: true", "    standalone: true\n    citation_mode: sideways"),
    )

    with pytest.raises(ManifestError, match="citation_mode must be one of"):
        load_manifest(path)


def test_typography_rejects_unknown_keys(tmp_path):
    path = tiny_book(tmp_path, MANIFEST.replace('body_font_size: "4"', 'body_font_size: "4"\n  nope: 1'))

    with pytest.raises(ManifestError, match="unknown key"):
        load_manifest(path)


def test_regex_compilation_failure_is_manifest_error(tmp_path):
    path = tiny_book(tmp_path, MANIFEST.replace("^\\*（第[一二三]篇", "^\\*（第["))

    with pytest.raises(ManifestError):
        load_manifest(path)


VISION_BLOCK = """\

vision:
  base_url: https://api.kimi.com/coding/v1
  model: k3
  key_env: MY_VISION_KEY
"""


def test_vision_endpoint_is_optional(tmp_path):
    assert load_manifest(tiny_book(tmp_path)).vision is None


def test_vision_endpoint_is_parsed_from_the_manifest(tmp_path):
    path = tiny_book(tmp_path, MANIFEST + VISION_BLOCK)

    vision = load_manifest(path).vision

    assert vision.base_url == "https://api.kimi.com/coding/v1"
    assert vision.model == "k3"
    assert vision.key_env == "MY_VISION_KEY"


def test_vision_key_env_defaults_to_the_standard_variable(tmp_path):
    path = tiny_book(tmp_path, MANIFEST + "\nvision:\n  base_url: http://localhost:8000/v1\n  model: m\n")

    assert load_manifest(path).vision.key_env == "PRIMER_VISION_API_KEY"


def test_vision_knobs_are_recorded_next_to_the_model(tmp_path):
    """实测出来的可靠取值记在清单里，而不是靠人记住一条命令行参数。"""
    path = tiny_book(
        tmp_path, MANIFEST + VISION_BLOCK + "  batch_size: 1\n  max_tokens: 16384\n"
    )

    vision = load_manifest(path).vision

    assert (vision.batch_size, vision.max_tokens) == (1, 16384)


def test_vision_knobs_default_to_unset(tmp_path):
    """不写就是"没说"，由 inspect 用内置默认值。"""
    vision = load_manifest(tiny_book(tmp_path, MANIFEST + VISION_BLOCK)).vision

    assert vision.batch_size is None
    assert vision.max_tokens is None


@pytest.mark.parametrize(
    "knob", ["  batch_size: 0\n", "  batch_size: -1\n", "  max_tokens: 0\n"]
)
def test_vision_knobs_must_be_positive_integers(tmp_path, knob):
    path = tiny_book(tmp_path, MANIFEST + VISION_BLOCK + knob)

    with pytest.raises(ManifestError, match="must be >= 1"):
        load_manifest(path)


def test_vision_knobs_reject_non_integers(tmp_path):
    path = tiny_book(tmp_path, MANIFEST + VISION_BLOCK + "  batch_size: two\n")

    with pytest.raises(ManifestError, match="must be an integer"):
        load_manifest(path)


def test_vision_rejects_unknown_keys_and_a_plaintext_key(tmp_path):
    path = tiny_book(tmp_path, MANIFEST + "\nvision:\n  api_key: sk-oops\n")

    with pytest.raises(ManifestError, match="unknown key"):
        load_manifest(path)
