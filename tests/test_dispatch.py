"""回归测试：分类结果必须真正决定处理器分发。

修复模块重复加载之前，靠 `file_type`/`strategy` 命中处理器的文件（`.yaml`、`.json`、
`.csv` 等）因为枚举不相等而匹配不到任何处理器，最终退化为"只算哈希的二进制文件"。
"""

from datetime import datetime
from pathlib import Path

import pytest

from primer.digest import FileDigest, FileMetadata, FileType, ProcessingStrategy
from primer.digest.processors import create_default_registry


def _file_digest(path, file_type, strategy, force_binary=False):
    """按分类阶段的产出构造一个 FileDigest，绕开目录扫描。"""
    stat = Path(path).stat()
    metadata = FileMetadata(
        path=Path(path),
        size=stat.st_size,
        modified_time=datetime.fromtimestamp(stat.st_mtime),
        created_time=datetime.fromtimestamp(stat.st_ctime),
        file_type=file_type,
        processing_strategy=strategy,
        force_binary=force_binary,
    )
    return FileDigest(metadata=metadata)


@pytest.fixture
def registry():
    return create_default_registry(config={})


@pytest.mark.parametrize(
    "filename, file_type, strategy, expected",
    [
        ("README.md", FileType.CRITICAL_DOCS, ProcessingStrategy.FULL_CONTENT, "TextFileProcessor"),
        ("notes.md", FileType.REFERENCE_DOCS, ProcessingStrategy.SUMMARY_ONLY, "TextFileProcessor"),
        ("main.py", FileType.SOURCE_CODE, ProcessingStrategy.CODE_SKELETON, "SourceCodeProcessor"),
        ("config.yaml", FileType.TEXT_DATA, ProcessingStrategy.STRUCTURE_EXTRACT, "ConfigFileProcessor"),
        ("settings.json", FileType.TEXT_DATA, ProcessingStrategy.STRUCTURE_EXTRACT, "ConfigFileProcessor"),
        ("data.csv", FileType.TEXT_DATA, ProcessingStrategy.HEADER_WITH_STATS, "DataFileProcessor"),
        ("image.png", FileType.BINARY_FILES, ProcessingStrategy.METADATA_ONLY, None),
    ],
)
def test_dispatch_follows_the_classification(registry, tmp_path, filename, file_type, strategy, expected):
    target = tmp_path / filename
    target.write_text("placeholder\n", encoding="utf-8")
    force_binary = file_type is FileType.BINARY_FILES

    processor = registry.get_processor(_file_digest(target, file_type, strategy, force_binary=force_binary))
    actual = type(processor).__name__ if processor else None

    assert actual == expected


def test_config_processor_turns_text_into_a_summary(tmp_path):
    """`.yaml` 走完整条处理链后必须产出摘要，而不是只剩哈希。"""
    registry = create_default_registry(config={})
    target = tmp_path / "config.yaml"
    target.write_text("mission: neosurvey\ntelescope:\n  aperture_m: 1.2\n", encoding="utf-8")
    digest = _file_digest(target, FileType.TEXT_DATA, ProcessingStrategy.STRUCTURE_EXTRACT)

    assert registry.process_file(digest, mode="framework", strategy=ProcessingStrategy.STRUCTURE_EXTRACT) is True
    assert digest.metadata.file_type == FileType.TEXT_DATA
    assert digest.human_readable_summary is not None
    assert "summary" in digest.to_dict()
