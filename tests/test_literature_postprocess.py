# -*- coding: utf-8 -*-
"""后处理单元测试：解包安全、链接改写、引文头、幂等与 model_output 取舍。"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from primer.literature.catalog import Citation, DocRecord
from primer.literature.postprocess import (
    HEADER_MARK,
    MODEL_OUTPUT_NAME,
    PostprocessResult,
    page_count,
    postprocess_document,
    render_citation_header,
    rewrite_links,
    strip_citation_header,
    unpack_archive,
)

MARKDOWN = "# Ion beam shepherd\n\nSome body text.\n\n![](images/page_1_image_0.png)\n"


def _record(**kwargs) -> DocRecord:
    defaults = dict(
        path=Path("/corpus/053_R_arxiv0811_3583.pdf"),
        rel_path="053_R_arxiv0811_3583.pdf",
        size=123,
        md5="a" * 32,
        citation=Citation(ref="053", cls="R", title="A map of the night sky", arxiv="0811.3583"),
    )
    defaults.update(kwargs)
    return DocRecord(**defaults)


def _archive(path: Path, *, markdown: str = MARKDOWN, pages: int = 2, model_output: bytes = b"x" * 4096) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as handle:
        handle.writestr("markdown.md", markdown)
        handle.writestr("images/page_1_image_0.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
        handle.writestr("middle_json.json", '{"pages": [' + ",".join("{}" for _ in range(pages)) + "]}")
        handle.writestr("structured_content.json", '{"pages": []}')
        handle.writestr(MODEL_OUTPUT_NAME, model_output)
    return path


def _postprocess(tmp_path: Path, **kwargs) -> PostprocessResult:
    keep_model_output = kwargs.pop("keep_model_output", False)
    pages = kwargs.pop("pages", 2)
    archive = _archive(tmp_path / "raw" / "stem.zip", pages=pages, **kwargs)
    return postprocess_document(
        _record(),
        archive,
        stem="stem",
        raw_dir=tmp_path / "raw",
        flat_dir=tmp_path / "flat",
        keep_model_output=keep_model_output,
    )


def test_render_citation_header_carries_citation_source_and_pages():
    header = render_citation_header(_record(), 5)

    assert header.startswith(f"{HEADER_MARK} — ref 053 · class R\n")
    assert "> **Title** — A map of the night sky\n" in header
    assert "> **arXiv** — 0811.3583\n" in header
    assert "> **Source** — 053_R_arxiv0811_3583.pdf\n" in header
    assert "> **Pages** — 5\n" in header
    assert header.endswith("\n\n")


def test_render_citation_header_handles_missing_and_unknown_citation():
    header = render_citation_header(_record(citation=None), None)

    assert header.startswith(f"{HEADER_MARK} — no metadata matched\n")
    assert "> **Pages** — unknown\n" in header
    assert "> **Title**" not in header


def test_strip_citation_header_removes_only_the_leading_block():
    body = strip_citation_header(render_citation_header(_record(), 5) + MARKDOWN)

    assert body == MARKDOWN


def test_strip_citation_header_leaves_untouched_text_alone():
    assert strip_citation_header(MARKDOWN) == MARKDOWN


def test_unpack_archive_writes_nested_members(tmp_path):
    archive = _archive(tmp_path / "stem.zip")

    written = unpack_archive(archive, tmp_path / "doc")

    assert {path.relative_to(tmp_path / "doc").as_posix() for path in written} >= {
        "markdown.md",
        "images/page_1_image_0.png",
    }
    assert (tmp_path / "doc" / "images" / "page_1_image_0.png").read_bytes().startswith(b"\x89PNG")


def test_unpack_archive_keeps_previous_files_unless_replacing(tmp_path):
    dest = tmp_path / "doc"
    unpack_archive(_archive(tmp_path / "first.zip"), dest)
    (dest / "images" / "stale.png").write_bytes(b"old")

    unpack_archive(_archive(tmp_path / "second.zip", markdown="# second\n"), dest)

    assert (dest / "images" / "stale.png").is_file()
    assert "# second" in (dest / "markdown.md").read_text(encoding="utf-8")


def test_unpack_archive_with_replace_drops_stale_material(tmp_path):
    dest = tmp_path / "doc"
    unpack_archive(_archive(tmp_path / "first.zip"), dest)
    (dest / "images" / "stale.png").write_bytes(b"old")

    unpack_archive(_archive(tmp_path / "second.zip", markdown="# second\n"), dest, replace_existing=True)

    assert not (dest / "images" / "stale.png").exists()
    assert (dest / "markdown.md").read_text(encoding="utf-8") == "# second\n"


def test_replacing_unpack_validates_before_deleting(tmp_path):
    dest = tmp_path / "doc"
    unpack_archive(_archive(tmp_path / "good.zip"), dest)
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as handle:
        handle.writestr("../escape.txt", "boom")

    with pytest.raises(ValueError, match="escaping the destination"):
        unpack_archive(evil, dest, replace_existing=True)

    assert (dest / "markdown.md").is_file()


def test_unpack_archive_rejects_parent_traversal(tmp_path):
    archive = tmp_path / "evil.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("../evil.txt", "boom")

    with pytest.raises(ValueError, match="escaping the destination"):
        unpack_archive(archive, tmp_path / "doc")

    assert not (tmp_path / "evil.txt").exists()


def test_unpack_archive_rejects_absolute_member_path(tmp_path):
    archive = tmp_path / "evil.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("/tmp/evil.txt", "boom")

    with pytest.raises(ValueError, match="absolute path"):
        unpack_archive(archive, tmp_path / "doc")


def test_unpack_archive_rejects_symlink_member(tmp_path):
    archive = tmp_path / "evil.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        info = zipfile.ZipInfo("link")
        info.external_attr = (0o120777 << 16) | 0o777
        handle.writestr(info, "target")

    with pytest.raises(ValueError, match="symbolic link"):
        unpack_archive(archive, tmp_path / "doc")


def test_rewrite_links_points_at_unpacked_files(tmp_path):
    doc_dir = tmp_path / "raw" / "stem"
    (doc_dir / "images").mkdir(parents=True)
    (doc_dir / "images" / "page_1_image_0.png").write_bytes(b"png")
    flat_dir = tmp_path / "flat"
    flat_dir.mkdir()

    rewritten = rewrite_links(MARKDOWN, doc_dir=doc_dir, flat_dir=flat_dir)

    link = rewritten.split("](")[1].split(")")[0]
    assert link == "../raw/stem/images/page_1_image_0.png"
    assert (flat_dir / link).resolve().is_file()


def test_rewrite_links_rewrites_html_src_and_keeps_other_targets(tmp_path):
    doc_dir = tmp_path / "raw" / "stem"
    (doc_dir / "images").mkdir(parents=True)
    (doc_dir / "images" / "page_2_chart_0.jpg").write_bytes(b"jpg")
    flat_dir = tmp_path / "flat"
    flat_dir.mkdir()
    text = (
        '<img src="images/page_2_chart_0.jpg" />\n'
        "![](images/missing.png)\n"
        "![](https://example.org/page.png)\n"
        "[anchor](#section)\n"
    )

    rewritten = rewrite_links(text, doc_dir=doc_dir, flat_dir=flat_dir)

    assert 'src="../raw/stem/images/page_2_chart_0.jpg"' in rewritten
    assert "![](images/missing.png)" in rewritten
    assert "![](https://example.org/page.png)" in rewritten
    assert "[anchor](#section)" in rewritten


def test_page_count_reads_middle_json_and_falls_back(tmp_path):
    doc_dir = tmp_path / "doc"
    doc_dir.mkdir()
    (doc_dir / "middle_json.json").write_text('{"pages": [{}, {}, {}]}', encoding="utf-8")
    (doc_dir / "structured_content.json").write_text('{"pages": [{}]}', encoding="utf-8")

    assert page_count(doc_dir) == 3
    (doc_dir / "middle_json.json").unlink()
    assert page_count(doc_dir) == 1
    (doc_dir / "structured_content.json").unlink()
    assert page_count(doc_dir) is None


def test_postprocess_document_writes_flat_markdown_with_resolving_links(tmp_path):
    result = _postprocess(tmp_path)

    flat = result.flat_path.read_text(encoding="utf-8")
    assert flat.startswith(f"{HEADER_MARK} — ref 053 · class R\n")
    assert "> **Pages** — 2\n" in flat
    assert "Some body text." in flat
    link = flat.split("](")[1].split(")")[0]
    assert (result.flat_path.parent / link).resolve().is_file()
    assert result.pages == 2
    assert result.images == 1


def test_postprocess_document_rejects_archive_without_markdown(tmp_path):
    archive = tmp_path / "raw" / "stem.zip"
    archive.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("middle_json.json", "{}")

    with pytest.raises(ValueError, match="without markdown.md"):
        postprocess_document(
            _record(), archive, stem="stem", raw_dir=tmp_path / "raw", flat_dir=tmp_path / "flat"
        )


@pytest.mark.parametrize("keep_model_output", [False, True])
def test_postprocess_document_is_idempotent(tmp_path, keep_model_output):
    archive = _archive(tmp_path / "raw" / "stem.zip")

    def run() -> tuple[str, PostprocessResult]:
        result = postprocess_document(
            _record(),
            archive,
            stem="stem",
            raw_dir=tmp_path / "raw",
            flat_dir=tmp_path / "flat",
            keep_model_output=keep_model_output,
        )
        return result.flat_path.read_text(encoding="utf-8"), result

    first_text, first = run()
    second_text, second = run()

    assert first_text == second_text
    assert first_text.count(HEADER_MARK) == 1
    assert first_text.count("Some body text.") == 1
    assert first.dropped_bytes == second.dropped_bytes
    assert first.total_bytes == second.total_bytes


def test_postprocess_document_drops_model_output_by_default(tmp_path):
    result = _postprocess(tmp_path)

    assert not (result.doc_dir / MODEL_OUTPUT_NAME).exists()
    assert result.dropped_bytes == 4096
    assert result.total_with_model_output == result.total_bytes + 4096


def test_postprocess_document_keeps_model_output_when_asked(tmp_path):
    result = _postprocess(tmp_path, keep_model_output=True)

    assert (result.doc_dir / MODEL_OUTPUT_NAME).is_file()
    assert result.dropped_bytes == 0
    assert result.total_with_model_output == result.total_bytes
