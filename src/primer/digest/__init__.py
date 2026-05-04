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

__version__ = "0.1.0"
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
