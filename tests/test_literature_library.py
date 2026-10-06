# -*- coding: utf-8 -*-
"""library 的单元测试：字段校验、读写往返、未知字段保留、原子写与 .bak、外部改动检测。"""

import json
import uuid as uuid_module

import pytest

from primer.literature.library import (
    NATURES,
    FileEntry,
    Library,
    LibraryError,
)


def _record_payload(**overrides):
    payload = {
        "uuid": "11111111-1111-4111-8111-111111111111",
        "type": "journal-article",
        "title": "样例论文",
        "authors": ["A. Author", "B. Author"],
        "year": 2022,
        "venue": "JATM",
        "doi": "10.1590/jatm.v14.1271",
        "files": [{"path": "参考资料/参考文献原文/a.pdf", "nature": "doi-consistent"}],
        "projects": ["航班化航天运输/适航体系"],
        "notes": "",
        "created_at": "2026-10-05T10:00:00+08:00",
        "updated_at": "2026-10-05T10:05:00+08:00",
    }
    payload.update(overrides)
    return payload


def _write(path, records):
    path.write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def test_natures_tuple_is_stable():
    assert NATURES == (
        "doi-consistent",
        "preprint-substitute",
        "title-match",
        "manual-upload",
        "other",
    )


def test_load_missing_file_raises(tmp_path):
    with pytest.raises(LibraryError, match="not found"):
        Library.load(tmp_path / "missing.json")


def test_load_rejects_non_array_top_level(tmp_path):
    path = tmp_path / "db.json"
    path.write_text('{"records": []}', encoding="utf-8")
    with pytest.raises(LibraryError, match="top-level must be an array"):
        Library.load(path)


def test_load_reports_json_error_position(tmp_path):
    path = tmp_path / "db.json"
    path.write_text('[{"uuid": }]', encoding="utf-8")
    with pytest.raises(LibraryError, match=r"invalid JSON .* line 1"):
        Library.load(path)


def test_load_validates_fields_with_path_context(tmp_path):
    path = tmp_path / "db.json"

    _write(path, [_record_payload(title="")])
    with pytest.raises(LibraryError, match=r"records\[0\]\.title"):
        Library.load(path)

    _write(path, [_record_payload(files=[{"path": "x.pdf", "nature": "bogus"}])])
    with pytest.raises(LibraryError, match=r"records\[0\]\.files\[0\]\.nature"):
        Library.load(path)

    _write(path, [_record_payload(year=True)])
    with pytest.raises(LibraryError, match=r"records\[0\]\.year"):
        Library.load(path)


def test_load_rejects_duplicate_uuid(tmp_path):
    path = tmp_path / "db.json"
    payload = _record_payload()
    _write(path, [payload, dict(payload)])
    with pytest.raises(LibraryError, match="duplicate uuid"):
        Library.load(path)


def test_round_trip_preserves_unknown_fields_and_writes_backup(tmp_path):
    path = tmp_path / "db.json"
    payload = _record_payload()
    payload["custom_field"] = {"x": 1}
    payload["files"][0]["extra_flag"] = True
    _write(path, [payload])
    original = path.read_text(encoding="utf-8")

    library = Library.load(path)
    library.save()

    assert json.loads(path.read_text(encoding="utf-8")) == json.loads(original)
    assert (tmp_path / "db.json.bak").read_text(encoding="utf-8") == original
    assert not (tmp_path / "db.json.tmp").exists()
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved[0]["custom_field"] == {"x": 1}
    assert saved[0]["files"][0]["extra_flag"] is True


def test_create_writes_empty_array_and_refuses_existing(tmp_path):
    path = tmp_path / "new" / "db.json"
    library = Library.create(path)
    assert library.records == []
    assert path.read_text(encoding="utf-8") == "[]\n"
    assert Library.load(path).records == []
    with pytest.raises(LibraryError, match="already exists"):
        Library.create(path)


def test_add_record_fills_uuid_and_timestamps(tmp_path):
    library = Library.create(tmp_path / "db.json")
    record = library.add_record({"title": "新记录"})

    assert uuid_module.UUID(record.uuid).version == 4
    assert record.created_at and record.updated_at
    assert record in library.records

    with pytest.raises(LibraryError, match="duplicate uuid"):
        library.add_record({"title": "重复", "uuid": record.uuid})
    with pytest.raises(LibraryError, match=r"new record\.title"):
        library.add_record({"authors": []})


def test_update_record_replaces_content_keeps_uuid_and_created_at(tmp_path):
    library = Library.create(tmp_path / "db.json")
    created = library.add_record({"title": "旧标题", "extra_note": "before"})
    old_created = created.created_at

    updated = library.update_record(created.uuid, {
        "title": "新标题",
        "authors": ["C. Author"],
        "uuid": "ffffffff-ffff-4fff-8fff-ffffffffffff",
        "updated_at": "2020-01-01T00:00:00+08:00",
    })

    assert updated.uuid == created.uuid
    assert updated.created_at == old_created
    assert updated.updated_at != "2020-01-01T00:00:00+08:00"
    assert updated.title == "新标题"
    assert "extra_note" not in updated.to_dict()  # 整条替换：未知字段也随之替换

    with pytest.raises(LibraryError, match="not found"):
        library.update_record("missing-uuid", {"title": "x"})


def test_delete_record(tmp_path):
    library = Library.create(tmp_path / "db.json")
    record = library.add_record({"title": "待删"})

    removed = library.delete_record(record.uuid)

    assert removed.uuid == record.uuid
    assert library.records == []
    with pytest.raises(LibraryError, match="not found"):
        library.delete_record(record.uuid)


def test_save_detects_external_change_and_reload_recovers(tmp_path):
    path = tmp_path / "db.json"
    library = Library.create(path)
    library.add_record({"title": "内存中新增"})

    external = _record_payload(title="外部改写")
    _write(path, [external])

    with pytest.raises(LibraryError, match="changed on disk"):
        library.save()

    library.reload()
    assert library.records[0].title == "外部改写"
    library.save()  # 重载后可以保存


def test_doi_prefix_normalised_and_blank_becomes_null(tmp_path):
    path = tmp_path / "db.json"
    _write(path, [
        _record_payload(doi="https://doi.org/10.1/x"),
        _record_payload(uuid="22222222-2222-4222-8222-222222222222", doi="   "),
    ])

    library = Library.load(path)

    assert library.records[0].doi == "10.1/x"
    assert library.records[1].doi is None


def test_file_entry_note_optional_and_unknown_kept():
    entry = FileEntry.from_dict({"path": "a.pdf", "nature": "other", "suffix": ".pdf"})
    assert entry.to_dict() == {"path": "a.pdf", "nature": "other", "suffix": ".pdf"}

    with_note = FileEntry.from_dict({"path": "a.pdf", "nature": "other", "note": "说明"})
    assert with_note.to_dict()["note"] == "说明"

    with pytest.raises(LibraryError, match=r"file\.path"):
        FileEntry.from_dict({"nature": "other"})


def test_biblatex_fields_round_trip_and_empty_omitted(tmp_path):
    path = tmp_path / "db.json"
    full = _record_payload(
        uuid="33333333-3333-4333-8333-333333333333",
        editor=["E. Editor"],
        translator=["T. Translator"],
        volume="14",
        number="3",
        pages="45-67",
        eid="e12345",
        publisher="科学出版社",
        location="北京",
        institution="中国科学院",
        organization="中国宇航学会",
        series="航天技术丛书",
        edition="2",
        isbn="978-7-03-000000-0",
        issn="1000-0000",
        url="https://example.org/x",
        eprint="2201.00001",
        eventtitle="第 8 届空天会议",
        eventdate="2022-09",
        keywords="复用；适航",
    )
    lean = _record_payload(uuid="44444444-4444-4444-8444-444444444444")
    _write(path, [full, lean])

    library = Library.load(path)
    rich = library.records[0]
    assert rich.volume == "14" and rich.number == "3" and rich.pages == "45-67"
    assert rich.editor == ["E. Editor"] and rich.translator == ["T. Translator"]
    assert rich.publisher == "科学出版社" and rich.location == "北京"
    assert rich.eventtitle == "第 8 届空天会议" and rich.eprint == "2201.00001"
    assert rich.keywords == "复用；适航"

    payload = rich.to_dict()
    assert payload["volume"] == "14" and payload["pages"] == "45-67"
    assert payload["editor"] == ["E. Editor"]

    lean_payload = library.records[1].to_dict()
    for key in ("volume", "pages", "publisher", "editor", "keywords", "eventdate"):
        assert key not in lean_payload


def test_biblatex_fields_validation(tmp_path):
    path = tmp_path / "db.json"
    _write(path, [_record_payload(volume=14)])
    with pytest.raises(LibraryError, match=r"volume"):
        Library.load(path)

    path2 = tmp_path / "db2.json"
    _write(path2, [_record_payload(editor="单人字符串")])
    with pytest.raises(LibraryError, match=r"editor"):
        Library.load(path2)


def test_file_records_round_trip_partition_and_compatibility(tmp_path):
    from primer.literature.library import FileRecord

    path = tmp_path / "db.json"
    file_payload = {
        "kind": "file",
        "uuid": "55555555-5555-4555-8555-555555555555",
        "path": "/tmp/scan/a.pdf",
        "name": "a.pdf",
        "size": 1234,
        "status": "pending",
        "added_at": "2026-10-06T01:00:00+08:00",
        "updated_at": "2026-10-06T01:00:00+08:00",
    }
    _write(path, [_record_payload(), file_payload])

    library = Library.load(path)
    assert len(library.records) == 1 and len(library.file_records) == 1
    record = library.file_records[0]
    assert record.name == "a.pdf" and record.status == "pending" and record.size == 1234
    assert '"kind": "file"' in library.dumps()

    # 只有文献记录的库：序列化与旧格式一致（不出现 kind 判别）
    plain = tmp_path / "plain.json"
    _write(plain, [_record_payload()])
    lib2 = Library.load(plain)
    assert lib2.file_records == []
    assert '"kind"' not in lib2.dumps()

    # add/delete 助手与状态校验
    new = library.add_file_record({"path": "/tmp/scan/b.pdf", "name": "b.pdf", "size": 10})
    assert new.status == "pending" and new.uuid
    assert library.find_file(new.uuid) is new
    library.delete_file_record(new.uuid)
    assert len(library.file_records) == 1
    with pytest.raises(LibraryError, match="status"):
        FileRecord.from_dict({"uuid": "u", "path": "p", "status": "weird"})

    # 新字段（md5／doi／eprint／dup／record_uuid／nature／note）往返
    rich = library.add_file_record({
        "path": "/tmp/scan/c.pdf", "name": "c.pdf", "size": 7,
        "md5": "abc123", "doi": "10.1/x", "eprint": "2101.00001",
        "record_uuid": "rec-1", "nature": "doi-consistent", "note": "含附录",
        "dup": {"kind": "doi", "value": "10.1/x",
                "matches": [{"kind": "record", "uuid": "u", "label": "T"}]},
    })
    text = library.dumps()
    assert '"md5": "abc123"' in text and '"eprint": "2101.00001"' in text
    roundtrip = tmp_path / "roundtrip.json"
    roundtrip.write_text(text, encoding="utf-8")
    reloaded = Library.load(roundtrip)
    got = reloaded.find_file(rich.uuid)
    assert got.md5 == "abc123" and got.doi == "10.1/x" and got.eprint == "2101.00001"
    assert got.record_uuid == "rec-1" and got.nature == "doi-consistent" and got.note == "含附录"
    assert got.dup["kind"] == "doi" and got.dup["matches"][0]["label"] == "T"

    with pytest.raises(LibraryError, match="nature"):
        FileRecord.from_dict({"uuid": "u2", "path": "p", "nature": "bogus"})
