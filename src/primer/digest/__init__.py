"""
Directory Digest Package
包含输入输出处理、文件系统分析等基础功能
"""

from .base import (
    # 枚举
    ProcessingStrategy,
    FileType,
    OutputFormats,
    
    # 数据类
    StrategyConfig,
    FileRule,
    FileClassification,
    FileMetadata,
    FileDigest,
    DirectoryStructure,
    
    # 核心类
    FileTypeDetector,
    RuleEngine,
    ContextManager,
    FormatConverter,
    DirectoryDigestBase,
    
    # 配置
    STRATEGY_CONFIGS,
)

# 版本号统一由顶层包提供，避免多处字面量漂移
from .. import __version__
__all__ = [
    'ProcessingStrategy',
    'FileType',
    'OutputFormats',
    'StrategyConfig',
    'FileRule',
    'FileClassification',
    'FileMetadata',
    'FileDigest',
    'DirectoryStructure',
    'FileTypeDetector',
    'RuleEngine',
    'ContextManager',
    'FormatConverter',
    'DirectoryDigestBase',
    'STRATEGY_CONFIGS',
]
