"""
元数据分析器 - 提取文件元数据信息
包括创建时间、修改时间、访问时间、文件路径、文件大小等
"""

import os
from pathlib import Path
from typing import Optional, Dict, Any
from datetime import datetime
from dataclasses import dataclass

from .base import BaseAnalyzer, AnalysisResult
from ..base import FileMetadata, FileType, FileTypeDetector


@dataclass
class MetadataAnalysisResult(AnalysisResult):
    """元数据分析结果"""
    file_metadata: Optional[FileMetadata] = None
    additional_metadata: Dict[str, Any] = None
    
    def __post_init__(self):
        super().__post_init__()
        if self.additional_metadata is None:
            self.additional_metadata = {}
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        result = {
            "success": self.success,
            "error_message": self.error_message,
            "metadata": self.metadata,
            "additional_metadata": self.additional_metadata
        }
        if self.file_metadata:
            result["file_metadata"] = self.file_metadata.to_dict()
        return result


class MetadataAnalyzer(BaseAnalyzer):
    """文件元数据分析器"""
    
    def __init__(self):
        self.file_type_detector = FileTypeDetector()
    
    def can_handle(self, filepath: Path, content: Optional[str] = None) -> bool:
        """
        元数据分析器可以处理所有文件类型
        
        Args:
            filepath: 文件路径
            content: 文件内容（如果已读取）
            
        Returns:
            bool: 总是返回 True
        """
        return True
    
    def analyze(self, filepath: Path, content: Optional[str] = None) -> MetadataAnalysisResult:
        """
        分析文件元数据
        
        Args:
            filepath: 文件路径
            content: 文件内容（如果已读取）
            
        Returns:
            MetadataAnalysisResult: 元数据分析结果
        """
        result = MetadataAnalysisResult()
        
        try:
            # 获取文件状态
            stat_result = filepath.stat()
            
            # 基本元数据
            file_type = self.file_type_detector.detect(filepath)
            
            # 创建 FileMetadata 对象
            file_metadata = FileMetadata(
                path=filepath,
                size=stat_result.st_size,
                modified_time=datetime.fromtimestamp(stat_result.st_mtime),
                created_time=datetime.fromtimestamp(stat_result.st_ctime),
                file_type=file_type,
                mime_type=self._guess_mime_type(filepath)
            )
            
            result.file_metadata = file_metadata
            
            # 收集额外的元数据
            additional_metadata = self._collect_additional_metadata(filepath, stat_result)
            result.additional_metadata = additional_metadata
            
            result.success = True
            
        except Exception as e:
            result.success = False
            result.error_message = f"Failed to analyze metadata: {str(e)}"
        
        return result
    
    def _guess_mime_type(self, filepath: Path) -> Optional[str]:
        """
        猜测文件的 MIME 类型
        
        Args:
            filepath: 文件路径
            
        Returns:
            Optional[str]: MIME 类型，如果无法猜测则返回 None
        """
        import mimetypes
        mime_type, _ = mimetypes.guess_type(str(filepath))
        return mime_type
    
    def _collect_additional_metadata(self, filepath: Path, stat_result: os.stat_result) -> Dict[str, Any]:
        """
        收集额外的元数据信息
        
        Args:
            filepath: 文件路径
            stat_result: 文件状态对象
            
        Returns:
            Dict[str, Any]: 额外的元数据
        """
        additional = {}
        
        # 文件访问时间
        try:
            additional["accessed_time"] = datetime.fromtimestamp(stat_result.st_atime).isoformat()
        except Exception:
            pass
        
        # 文件权限模式
        try:
            additional["mode_octal"] = oct(stat_result.st_mode)[-4:]  # 只保留后4位
            additional["is_readable"] = os.access(filepath, os.R_OK)
            additional["is_writable"] = os.access(filepath, os.W_OK)
            additional["is_executable"] = os.access(filepath, os.X_OK)
        except Exception:
            pass
        
        # 文件所有者和组（Unix系统）
        try:
            if hasattr(stat_result, 'st_uid'):
                additional["uid"] = stat_result.st_uid
            if hasattr(stat_result, 'st_gid'):
                additional["gid"] = stat_result.st_gid
        except Exception:
            pass
        
        # 文件硬链接数
        try:
            if hasattr(stat_result, 'st_nlink'):
                additional["nlink"] = stat_result.st_nlink
        except Exception:
            pass
        
        # 文件系统相关信息
        try:
            if hasattr(stat_result, 'st_dev'):
                additional["device"] = stat_result.st_dev
            if hasattr(stat_result, 'st_ino'):
                additional["inode"] = stat_result.st_ino
        except Exception:
            pass
        
        # 文件扩展名
        additional["extension"] = filepath.suffix.lower()
        additional["stem"] = filepath.stem
        
        # 文件是否是符号链接
        additional["is_symlink"] = filepath.is_symlink()
        
        # 文件是否隐藏
        additional["is_hidden"] = filepath.name.startswith('.')
        
        return additional
    
    @staticmethod
    def format_metadata_summary(metadata_result: MetadataAnalysisResult) -> str:
        """
        格式化元数据摘要，用于快速查看
        
        Args:
            metadata_result: 元数据分析结果
            
        Returns:
            str: 格式化的摘要字符串
        """
        if not metadata_result.success or not metadata_result.file_metadata:
            return f"Metadata analysis failed: {metadata_result.error_message}"
        
        fm = metadata_result.file_metadata
        lines = []
        lines.append(f"Path: {fm.path}")
        lines.append(f"Size: {MetadataAnalyzer._format_size(fm.size)}")
        lines.append(f"Type: {fm.file_type.value}")
        if fm.mime_type:
            lines.append(f"MIME: {fm.mime_type}")
        lines.append(f"Modified: {fm.modified_time.strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(f"Created: {fm.created_time.strftime('%Y-%m-%d %H:%M:%S')}")
        
        return "\n".join(lines)
    
    @staticmethod
    def _format_size(size_bytes: int) -> str:
        """
        格式化文件大小为人类可读格式
        
        Args:
            size_bytes: 文件大小（字节）
            
        Returns:
            str: 格式化后的大小
        """
        if size_bytes == 0:
            return "0 B"
        
        import math
        units = ['B', 'KB', 'MB', 'GB', 'TB']
        i = int(math.floor(math.log(size_bytes, 1024)))
        p = math.pow(1024, i)
        s = round(size_bytes / p, 2)
        return f"{s} {units[i]}"


# ==================== 公共 API 导出 ====================

__all__ = [
    'MetadataAnalysisResult',
    'MetadataAnalyzer',
]
