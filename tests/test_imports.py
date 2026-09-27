"""回归测试：同一份实现只能有一个模块对象，枚举与策略对象必须全局唯一。

`processors/base.py` 曾把 `src/primer/digest` 插进 `sys.path`，再用裸名 `import base`
取基础模块，于是同一个 `base.py` 被加载成两个互不相干的模块对象。Enum 成员按身份比较，
两侧的 `FileType`/`ProcessingStrategy` 永不相等，处理器里所有依赖分类结果的分支因此失效。

输出路径策略是同一类问题的另一种形态：`primer.literature.paths` 与 `primer.book.paths`
曾各自实现一遍，结果只有一份带大小写回退，另一份把绝对路径写进了产物。现在实现只有
`primer.paths` 一份，两个包模块只做再导出——函数对象相同，才谈得上"改一处、三处生效"。
"""

import sys
from pathlib import Path


def test_no_shadow_modules_are_loaded():
    import primer.digest.processors  # noqa: F401

    assert "base" not in sys.modules
    assert "analyzers" not in sys.modules


def test_types_are_shared_between_base_and_processors():
    from primer.digest import base as digest_base
    from primer.digest.processors import base as processors_base

    assert processors_base.FileType is digest_base.FileType
    assert processors_base.ProcessingStrategy is digest_base.ProcessingStrategy
    assert processors_base.FileMetadata is digest_base.FileMetadata
    assert processors_base.FileDigest is digest_base.FileDigest
    assert processors_base.STRATEGY_CONFIGS is digest_base.STRATEGY_CONFIGS


def test_digest_package_dir_is_not_on_sys_path():
    import primer.digest.base as digest_base
    import primer.digest.processors  # noqa: F401

    digest_dir = Path(digest_base.__file__).resolve().parent
    assert all(Path(entry or ".").resolve() != digest_dir for entry in sys.path)


def test_the_output_path_policy_is_one_implementation_shared_by_three_packages():
    """再导出必须是同一个函数对象；复制一份新实现会在这里立刻暴露。"""
    import primer.book.paths as book_paths
    import primer.literature.paths as literature_paths
    import primer.paths as canonical

    for name in (
        "OUTPUT_DIRNAME",
        "normalize_project_root",
        "resolve_project_root",
        "output_dir_for",
        "relative_to_root",
        "strip_root_prefix",
    ):
        assert getattr(canonical, name) is getattr(literature_paths, name), name
        assert getattr(canonical, name) is getattr(book_paths, name), name
