"""
分析器基础模块 - 定义内容分析器接口和描述原始流程

原始 directory_digest.py 的内容分析流程:
===========================================================================
1. 文件类型检测 (FileTypeDetector)
   - 首先通过扩展名检测
   - 然后通过内容检测（空字节、可打印字符比例、代码模式）

2. 规则引擎分类 (RuleEngine)
   - 应用显式规则（YAML配置或默认规则）
   - 确定处理策略 (ProcessingStrategy)
   - 估算token消耗

3. 上下文管理 (ContextManager)
   - 检查token是否足够
   - 策略降级（当token不足时）
   - 分配token

4. 内容分析与处理
   a) 元数据分析 (MetadataAnalyzer)
      - 提取文件系统元数据（大小、时间、权限等）
      - 检测文件类型
      - 收集额外文件属性
   
   b) 文本文件 (HumanReadableSummarizer)
      - 检测编码
      - 分析文本指标（阅读时间、难度、主题、情感）
      - 提取标题、关键章节
      - 智能截断（结构化数据文件）
   
   c) 源代码文件 (SourceCodeAnalyzer)
      - AST分析（Python）或正则分析（其他语言）
      - 提取导入、函数、类
      - 代码复杂度分析 (ComplexityAnalyzer)
      - 代码风格检查
   
   d) 配置/结构化文件 (StructureExtract)
      - 提取键值对结构
      - 智能文本处理 (SmartTextProcessor)
      - 保留头部和元数据，截断数据区
   
   e) 二进制文件
      - 仅计算哈希值
      - 记录元数据

5. 输出格式化 (FormatConverter)
   - JSON/YAML/Markdown/HTML等格式
   - 目录树生成
   - 文件详情展示

核心数据类:
- HumanReadableSummary: 文本摘要
- SourceCodeAnalysis: 源代码分析结果
- FileDigest: 单个文件摘要
- DirectoryStructure: 目录结构
- MetadataAnalysisResult: 元数据分析结果
===========================================================================
"""

import abc
from pathlib import Path
from typing import Dict, List, Optional, Any
from dataclasses import dataclass


# ==================== 分析结果基类 ====================

@dataclass
class AnalysisResult:
    """分析结果基类"""
    success: bool = True
    error_message: Optional[str] = None
    metadata: Dict[str, Any] = None
    
    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}


# ==================== 分析器接口 ====================

class BaseAnalyzer(abc.ABC):
    """内容分析器基类"""
    
    @abc.abstractmethod
    def can_handle(self, filepath: Path, content: Optional[str] = None) -> bool:
        """
        判断是否能处理此文件
        
        Args:
            filepath: 文件路径
            content: 文件内容（如果已读取）
            
        Returns:
            bool: 是否能处理
        """
        pass
    
    @abc.abstractmethod
    def analyze(self, filepath: Path, content: Optional[str] = None) -> AnalysisResult:
        """
        分析文件内容
        
        Args:
            filepath: 文件路径
            content: 文件内容（如果已读取）
            
        Returns:
            AnalysisResult: 分析结果
        """
        pass
    
    def get_name(self) -> str:
        """获取分析器名称"""
        return self.__class__.__name__


class CompositeAnalyzer(BaseAnalyzer):
    """组合分析器 - 管理多个分析器"""
    
    def __init__(self):
        self.analyzers: List[BaseAnalyzer] = []
    
    def add_analyzer(self, analyzer: BaseAnalyzer):
        """添加分析器"""
        self.analyzers.append(analyzer)
    
    def can_handle(self, filepath: Path, content: Optional[str] = None) -> bool:
        """只要有一个分析器能处理就返回True"""
        return any(analyzer.can_handle(filepath, content) for analyzer in self.analyzers)
    
    def analyze(self, filepath: Path, content: Optional[str] = None) -> AnalysisResult:
        """
        使用所有能处理的分析器进行分析
        
        Returns:
            AnalysisResult: 组合分析结果
        """
        results = []
        for analyzer in self.analyzers:
            if analyzer.can_handle(filepath, content):
                try:
                    result = analyzer.analyze(filepath, content)
                    result.metadata['analyzer'] = analyzer.get_name()
                    results.append(result)
                except Exception as e:
                    error_result = AnalysisResult(
                        success=False,
                        error_message=f"Analyzer {analyzer.get_name()} failed: {str(e)}"
                    )
                    error_result.metadata['analyzer'] = analyzer.get_name()
                    results.append(error_result)
        
        # 组合结果
        combined = AnalysisResult(
            success=any(r.success for r in results),
            metadata={
                'analyzer_count': len(results),
                'results': results
            }
        )
        
        if not results:
            combined.success = False
            combined.error_message = "No analyzers could handle this file"
        
        return combined


# ==================== 公共 API 导出 ====================

__all__ = [
    'AnalysisResult',
    'BaseAnalyzer',
    'CompositeAnalyzer',
]
