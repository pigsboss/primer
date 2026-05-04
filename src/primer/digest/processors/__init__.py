"""
Directory Digest Processors
文件处理器模块
"""

from .base import (
    # 基类
    BaseFileProcessor,
    
    # 具体处理器
    TextFileProcessor,
    SourceCodeProcessor,
    ConfigFileProcessor,
    DataFileProcessor,
    
    # 注册表
    FileProcessorRegistry,
    create_default_registry,
)

__all__ = [
    'BaseFileProcessor',
    'TextFileProcessor',
    'SourceCodeProcessor',
    'ConfigFileProcessor',
    'DataFileProcessor',
    'FileProcessorRegistry',
    'create_default_registry',
]
