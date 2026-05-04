"""
Directory Digest - 处理器模块
包含文本文件、源代码、配置文件等各类文件的处理策略
"""

import os
import sys
import re
import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union, Any
from dataclasses import dataclass, field
from datetime import datetime
from abc import ABC, abstractmethod

# 导入基础模块
sys.path.insert(0, str(Path(__file__).parent.parent))
from base import (
    ProcessingStrategy,
    FileType,
    FileMetadata,
    FileDigest,
    STRATEGY_CONFIGS,
)

# 尝试导入分析器模块
try:
    from analyzers.semantics.base import (
        HumanReadableSummary,
        SourceCodeAnalysis,
        SmartTextProcessor,
    )
    from analyzers.semantics.sheets import ConfigAnalysisResult
    ANALYZERS_AVAILABLE = True
except ImportError:
    ANALYZERS_AVAILABLE = False
    # 定义简单的后备数据类
    @dataclass
    class HumanReadableSummary:
        """后备人类可读摘要"""
        title: Optional[str] = None
        line_count: int = 0
        word_count: int = 0
        character_count: int = 0
        encoding: Optional[str] = None
        first_lines: List[str] = field(default_factory=list)
        last_lines: List[str] = field(default_factory=list)
        summary: Optional[str] = None
        
        def to_dict(self) -> Dict:
            return {
                "title": self.title,
                "line_count": self.line_count,
                "word_count": self.word_count,
                "character_count": self.character_count,
                "encoding": self.encoding,
                "first_lines": self.first_lines,
                "last_lines": self.last_lines,
                "summary": self.summary
            }
    
    @dataclass
    class SourceCodeAnalysis:
        """后备源代码分析"""
        language: str = "unknown"
        total_lines: int = 0
        code_lines: int = 0
        comment_lines: int = 0
        blank_lines: int = 0
        imports: List[str] = field(default_factory=list)
        functions: List[Dict] = field(default_factory=list)
        classes: List[Dict] = field(default_factory=list)
        
        def to_dict(self) -> Dict:
            return {
                "language": self.language,
                "total_lines": self.total_lines,
                "code_lines": self.code_lines,
                "comment_lines": self.comment_lines,
                "blank_lines": self.blank_lines,
                "imports": self.imports[:20],
                "functions": self.functions[:20],
                "classes": self.classes[:20]
            }
    
    @dataclass
    class ConfigAnalysisResult:
        """后备配置分析结果"""
        keys: List[str] = field(default_factory=list)
        sections: List[str] = field(default_factory=list)
        structure_summary: Optional[str] = None
        
        def to_dict(self) -> Dict:
            return {
                "keys": self.keys[:20],
                "sections": self.sections[:20],
                "structure_summary": self.structure_summary
            }


# ==================== 处理器基类 ====================

class BaseFileProcessor(ABC):
    """文件处理器基类"""
    
    def __init__(self, config: Optional[Dict] = None):
        self.config = config or {}
        # 在full模式下，应该允许更大的文件大小限制
        # 默认值从1MB增加到10MB，但可以通过配置覆盖
        self.max_full_content_size = self.config.get('max_full_content_size', 10 * 1024 * 1024)  # 10MB
    
    @abstractmethod
    def can_handle(self, file_digest: FileDigest) -> bool:
        """判断是否能处理此文件"""
        pass
    
    @abstractmethod
    def process(self, file_digest: FileDigest, content: str, mode: str = "framework", 
                strategy: ProcessingStrategy = ProcessingStrategy.SUMMARY_ONLY) -> FileDigest:
        """
        处理文件内容
        
        Args:
            file_digest: 文件摘要对象
            content: 文件内容
            mode: 输出模式 ("full", "framework", "sort")
            strategy: 处理策略
            
        Returns:
            更新后的 FileDigest
        """
        pass
    
    def _should_include_full_content(self, file_digest: FileDigest, mode: str) -> bool:
        """判断是否应该包含完整内容"""
        if mode != "full":
            return False
        if file_digest.metadata.size > self.max_full_content_size:
            return False
        return True


# ==================== 文本文件处理器 ====================

class TextFileProcessor(BaseFileProcessor):
    """人类可读文本文件处理器"""
    
    TEXT_EXTENSIONS = {
        '.txt', '.md', '.markdown', '.rst', '.tex', '.html', '.htm', '.cmt',
        '.tls', '.tpc', '.ker'  # 添加SPICE内核文件，它们本质上是文本文件
    }
    
    def __init__(self, config: Optional[Dict] = None):
        super().__init__(config)
        self.debug = config.get('debug', False) if config else False
    
    def can_handle(self, file_digest: FileDigest) -> bool:
        # 添加调试输出
        if self.debug:
            import sys
            print(f"[DEBUG:TextFileProcessor.can_handle] Checking file: {file_digest.metadata.path}", file=sys.stderr)
            print(f"[DEBUG:TextFileProcessor.can_handle]   File type: {file_digest.metadata.file_type}", file=sys.stderr)
            print(f"[DEBUG:TextFileProcessor.can_handle]   Strategy: {file_digest.metadata.processing_strategy}", file=sys.stderr)
        
        # 优先使用分类阶段确定的类型/策略
        if file_digest.metadata.file_type in (FileType.CRITICAL_DOCS, FileType.REFERENCE_DOCS):
            if self.debug:
                import sys
                print(f"[DEBUG:TextFileProcessor.can_handle]   File type is CRITICAL_DOCS or REFERENCE_DOCS: True", file=sys.stderr)
            return True
        if file_digest.metadata.processing_strategy in (
            ProcessingStrategy.SUMMARY_ONLY, 
            ProcessingStrategy.FULL_CONTENT,
            ProcessingStrategy.HEADER_WITH_STATS  # 文档类也使用此策略
        ):
            if self.debug:
                import sys
                print(f"[DEBUG:TextFileProcessor.can_handle]   Strategy matches text file strategies: True", file=sys.stderr)
            return True
        
        # 后备：扩展名检查（用于未经过分类阶段的情况）
        suffix = file_digest.metadata.path.suffix.lower()
        result = suffix in self.TEXT_EXTENSIONS
        if self.debug:
            import sys
            print(f"[DEBUG:TextFileProcessor.can_handle]   Suffix {suffix} in TEXT_EXTENSIONS: {result}", file=sys.stderr)
        return result
    
    def process(self, file_digest: FileDigest, content: str, mode: str = "framework", 
                strategy: ProcessingStrategy = ProcessingStrategy.SUMMARY_ONLY) -> FileDigest:
        
        # 添加debug输出
        import sys
        debug = self.debug
        if debug:
            print(f"[DEBUG:TextFileProcessor] Processing file: {file_digest.metadata.path}", file=sys.stderr)
            print(f"[DEBUG:TextFileProcessor]   Strategy: {strategy}", file=sys.stderr)
            print(f"[DEBUG:TextFileProcessor]   Mode: {mode}", file=sys.stderr)
            print(f"[DEBUG:TextFileProcessor]   Content length: {len(content)} chars", file=sys.stderr)
            print(f"[DEBUG:TextFileProcessor]   File size: {file_digest.metadata.size} bytes", file=sys.stderr)
        
        if not content:
            if debug:
                print(f"[DEBUG:TextFileProcessor]   No content, returning early", file=sys.stderr)
            return file_digest
        
        filepath = file_digest.metadata.path
        
        # 根据策略决定输出内容，确保不冗余
        # 使用枚举值进行比较，而不是枚举对象本身
        strategy_value = strategy.value if hasattr(strategy, 'value') else str(strategy)
        
        if strategy_value == ProcessingStrategy.FULL_CONTENT.value:
            # FULL_CONTENT策略：只嵌入全文，不生成摘要
            if debug:
                print(f"[DEBUG:TextFileProcessor]   FULL_CONTENT strategy", file=sys.stderr)
                print(f"[DEBUG:TextFileProcessor]     File size: {file_digest.metadata.size}, max_full_content_size: {self.max_full_content_size}", file=sys.stderr)
            
            # FULL_CONTENT策略必须设置full_content，无论文件大小
            # 但在文件过大时进行截断
            if file_digest.metadata.size <= self.max_full_content_size:
                if debug:
                    print(f"[DEBUG:TextFileProcessor]     File size within limit, setting full_content", file=sys.stderr)
                file_digest.full_content = content
            else:
                # 如果文件太大，至少嵌入前部分内容
                # 限制为最大内容大小的一半，确保不会太大
                max_chars = self.max_full_content_size // 2
                if len(content) > max_chars:
                    if debug:
                        print(f"[DEBUG:TextFileProcessor]     File too large, truncating to {max_chars} chars", file=sys.stderr)
                    file_digest.full_content = content[:max_chars] + f"\n...[文件过大，已截断。完整文件大小: {file_digest.metadata.size} 字节]"
                else:
                    if debug:
                        print(f"[DEBUG:TextFileProcessor]     File within char limit, setting full_content", file=sys.stderr)
                    file_digest.full_content = content
            
            # 确保full_content被设置（双重检查）
            if file_digest.full_content is None:
                if debug:
                    print(f"[DEBUG:TextFileProcessor]   WARNING: full_content was None, setting it now", file=sys.stderr)
                file_digest.full_content = content
            
            # 确保不设置human_readable_summary
            file_digest.human_readable_summary = None
            
            if debug:
                print(f"[DEBUG:TextFileProcessor]   After FULL_CONTENT processing:", file=sys.stderr)
                print(f"[DEBUG:TextFileProcessor]     full_content set: {file_digest.full_content is not None}", file=sys.stderr)
                print(f"[DEBUG:TextFileProcessor]     human_readable_summary set: {file_digest.human_readable_summary is not None}", file=sys.stderr)
            
            return file_digest
            
        elif strategy_value == ProcessingStrategy.SUMMARY_ONLY.value:
            # SUMMARY_ONLY策略：只生成极简摘要，不嵌入全文
            if debug:
                print(f"[DEBUG:TextFileProcessor]   SUMMARY_ONLY strategy", file=sys.stderr)
            summary = self._generate_minimal_summary(filepath, content)
            file_digest.human_readable_summary = summary
            # 确保不设置full_content
            file_digest.full_content = None
            return file_digest
            
        elif strategy_value == ProcessingStrategy.HEADER_WITH_STATS.value:
            # HEADER_WITH_STATS策略：生成头部信息和统计
            if debug:
                print(f"[DEBUG:TextFileProcessor]   HEADER_WITH_STATS strategy", file=sys.stderr)
            summary = self._generate_header_stats_summary(filepath, content)
            file_digest.human_readable_summary = summary
            # 确保不设置full_content
            file_digest.full_content = None
            return file_digest
            
        else:
            # 其他策略，默认生成摘要
            if debug:
                print(f"[DEBUG:TextFileProcessor]   Default strategy", file=sys.stderr)
            summary = self._generate_minimal_summary(filepath, content)
            file_digest.human_readable_summary = summary
            # 确保不设置full_content
            file_digest.full_content = None
            return file_digest
    
    def _generate_minimal_summary(self, filepath: Path, content: str) -> HumanReadableSummary:
        """生成极简摘要"""
        lines = content.split('\n')
        return HumanReadableSummary(
            title=self._extract_title(filepath, lines),
            line_count=len(lines),
            word_count=len(re.findall(r'\b[\w\u4e00-\u9fff]+\b', content)),
            character_count=len(content),
            encoding=self._detect_encoding(content),
            first_lines=lines[:min(3, len(lines))],
            last_lines=[],
            summary=f"Minimal summary: {len(lines)} lines, {len(content)} characters"
        )
    
    def _generate_header_stats_summary(self, filepath: Path, content: str) -> HumanReadableSummary:
        """生成头部信息和统计摘要"""
        lines = content.split('\n')
        return HumanReadableSummary(
            title=self._extract_title(filepath, lines),
            line_count=len(lines),
            word_count=len(re.findall(r'\b[\w\u4e00-\u9fff]+\b', content)),
            character_count=len(content),
            encoding=self._detect_encoding(content),
            first_lines=lines[:min(10, len(lines))],
            last_lines=[],
            summary=f"Header with stats: {len(lines)} lines total"
        )
    
    def _generate_summary(self, filepath: Path, content: str, 
                         strategy: ProcessingStrategy) -> HumanReadableSummary:
        """生成文本摘要"""
        lines = content.split('\n')
        line_count = len(lines)
        
        # 基础统计
        words = re.findall(r'\b[\w\u4e00-\u9fff]+\b', content)
        word_count = len(words)
        
        # 提取标题
        title = self._extract_title(filepath, lines)
        
        # 提取首尾行
        first_lines = lines[:min(10, len(lines))]
        last_lines = lines[-min(5, len(lines)):] if len(lines) > 5 else []
        
        # 根据策略调整
        if strategy == ProcessingStrategy.HEADER_WITH_STATS:
            # 只保留头部
            first_lines = first_lines[:20]
            last_lines = []
        
        # 生成综合摘要
        summary_text = self._generate_summary_text(filepath, lines, strategy)
        
        return HumanReadableSummary(
            title=title,
            line_count=line_count,
            word_count=word_count,
            character_count=len(content),
            encoding=self._detect_encoding(content),
            first_lines=first_lines,
            last_lines=last_lines,
            summary=summary_text
        )
    
    def _extract_title(self, filepath: Path, lines: List[str]) -> Optional[str]:
        """提取标题"""
        # 1. 从文件名
        filename = filepath.stem
        if filename and len(filename) > 2:
            cleaned = filename.replace('_', ' ').replace('-', ' ').title()
            if 3 <= len(cleaned) <= 100:
                return cleaned
        
        # 2. 从内容
        for line in lines[:10]:
            line = line.strip()
            if not line:
                continue
            
            # Markdown 标题
            md_match = re.match(r'^#+\s+(.+)$', line)
            if md_match:
                return md_match.group(1).strip()
            
            # 其他标题模式
            if len(line) > 3 and len(line) < 100:
                if line[0].isalpha() or line[0] in ('【', '[', '*'):
                    return line
        
        return None
    
    def _detect_encoding(self, content: str) -> str:
        """检测编码"""
        try:
            content.encode('utf-8')
            return 'utf-8'
        except UnicodeEncodeError:
            return 'unknown'
    
    def _generate_summary_text(self, filepath: Path, lines: List[str], 
                               strategy: ProcessingStrategy) -> str:
        """生成摘要文本"""
        parts = []
        
        # 基础信息
        suffix = filepath.suffix.lower()
        parts.append(f"File type: {suffix[1:] if suffix else 'text'}")
        parts.append(f"Total lines: {len(lines)}")
        
        # 根据策略添加内容
        if strategy == ProcessingStrategy.FULL_CONTENT:
            parts.append("\n[FULL CONTENT INCLUDED]")
        elif strategy == ProcessingStrategy.SUMMARY_ONLY:
            # 包含前几行预览
            preview = '\n'.join(lines[:min(20, len(lines))])
            if len(lines) > 20:
                preview += f"\n... [and {len(lines) - 20} more lines]"
            parts.append(f"\nPreview:\n{preview}")
        
        return '\n'.join(parts)


# ==================== 源代码处理器 ====================

class SourceCodeProcessor(BaseFileProcessor):
    """源代码文件处理器"""
    
    CODE_EXTENSIONS = {
        '.py', '.java', '.cpp', '.c', '.h', '.hpp', '.js', '.ts', 
        '.jsx', '.tsx', '.go', '.rs', '.rb', '.php', '.swift',
        '.sh', '.bash', '.ps1', '.bat', '.cmd', '.css', '.scss'
    }
    
    def __init__(self, config: Optional[Dict] = None):
        super().__init__(config)
        self.debug = config.get('debug', False) if config else False
    
    def can_handle(self, file_digest: FileDigest) -> bool:
        # 优先使用分类阶段确定的类型/策略
        if file_digest.metadata.file_type == FileType.SOURCE_CODE:
            return True
        if file_digest.metadata.processing_strategy == ProcessingStrategy.CODE_SKELETON:
            return True
        
        # 后备：扩展名检查（用于未经过分类阶段的情况）
        suffix = file_digest.metadata.path.suffix.lower()
        return suffix in self.CODE_EXTENSIONS
    
    def process(self, file_digest: FileDigest, content: str, mode: str = "framework", 
                strategy: ProcessingStrategy = ProcessingStrategy.CODE_SKELETON) -> FileDigest:
        
        if not content:
            return file_digest
        
        filepath = file_digest.metadata.path
        
        # 使用枚举值进行比较，而不是枚举对象本身
        strategy_value = strategy.value if hasattr(strategy, 'value') else str(strategy)
        
        # 处理完整内容：对于 FULL_CONTENT 策略，总是设置完整内容（如果文件大小允许）
        if strategy_value == ProcessingStrategy.FULL_CONTENT.value and file_digest.metadata.size <= self.max_full_content_size:
            file_digest.full_content = content
            # FULL_CONTENT策略：不生成摘要
            file_digest.human_readable_summary = None
        else:
            # 对于其他策略，仅在 full 模式下且文件大小允许时设置完整内容
            if self._should_include_full_content(file_digest, mode):
                file_digest.full_content = content
            else:
                file_digest.full_content = None
        
        # 分析代码（结构分析对代码文件始终有价值）
        analysis = self._analyze_code(filepath, content, strategy)
        file_digest.source_code_analysis = analysis
        
        # 生成摘要：对于 FULL_CONTENT 策略，不生成摘要
        if strategy_value == ProcessingStrategy.FULL_CONTENT.value:
            # 已经设置为None，不需要额外处理
            pass
        else:
            # 对于其他策略，生成正常摘要
            summary = self._generate_code_summary(filepath, content, analysis)
            file_digest.human_readable_summary = summary
        
        return file_digest
    
    def _analyze_code(self, filepath: Path, content: str, 
                      strategy: ProcessingStrategy) -> SourceCodeAnalysis:
        """分析源代码"""
        lines = content.split('\n')
        total_lines = len(lines)
        
        # 统计行类型
        blank_lines = 0
        comment_lines = 0
        code_lines = 0
        
        for line in lines:
            stripped = line.strip()
            if not stripped:
                blank_lines += 1
            elif self._is_comment_line(stripped, filepath.suffix.lower()):
                comment_lines += 1
            else:
                code_lines += 1
        
        # 提取导入、函数、类
        imports = self._extract_imports(content, filepath.suffix.lower())
        functions = self._extract_functions(content, filepath.suffix.lower())
        classes = self._extract_classes(content, filepath.suffix.lower())
        
        # 语言识别
        language = self._identify_language(filepath)
        
        return SourceCodeAnalysis(
            language=language,
            total_lines=total_lines,
            code_lines=code_lines,
            comment_lines=comment_lines,
            blank_lines=blank_lines,
            imports=imports,
            functions=functions,
            classes=classes
        )
    
    def _is_comment_line(self, line: str, suffix: str) -> bool:
        """判断是否为注释行"""
        if suffix in ('.py', '.sh', '.bash', '.rb'):
            return line.startswith('#')
        elif suffix in ('.java', '.cpp', '.c', '.h', '.js', '.ts'):
            return line.startswith('//') or line.startswith('/*')
        return False
    
    def _extract_imports(self, content: str, suffix: str) -> List[str]:
        """提取导入语句"""
        imports = []
        
        if suffix == '.py':
            # Python 导入
            patterns = [
                r'^import\s+([a-zA-Z_][a-zA-Z0-9_\.]*)',
                r'^from\s+([a-zA-Z_][a-zA-Z0-9_\.]*)',
            ]
        elif suffix in ('.js', '.ts'):
            # JavaScript/TypeScript 导入
            patterns = [
                r'^import\s+.*from\s+[\'"]([^\'"]+)[\'"]',
                r'^const\s+.*=\s+require\([\'"]([^\'"]+)[\'"]\)',
            ]
        elif suffix in ('.java', '.cpp', '.c'):
            # Java/C/C++ 导入/包含
            patterns = [
                r'^import\s+([a-zA-Z0-9_\.]+);',
                r'^#include\s+[<"]([^>"]+)[>"]',
            ]
        else:
            patterns = []
        
        for pattern in patterns:
            matches = re.findall(pattern, content, re.MULTILINE)
            imports.extend(matches)
        
        return list(set(imports))[:50]  # 去重并限制数量
    
    def _extract_functions(self, content: str, suffix: str) -> List[Dict]:
        """提取函数定义"""
        functions = []
        
        if suffix == '.py':
            # Python 函数
            func_pattern = r'^def\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*\('
        elif suffix in ('.js', '.ts'):
            func_pattern = r'^\s*(?:function|const|let|var)\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*(?:\(|\s*=\s*(?:\([^)]*\)\s*=>|function))'
        else:
            func_pattern = r'^\s*(?:[a-zA-Z_][a-zA-Z0-9_:\*&]+\s+)+([a-zA-Z_][a-zA-Z0-9_]*)\s*\('
        
        lines = content.split('\n')
        for i, line in enumerate(lines):
            match = re.search(func_pattern, line)
            if match:
                func_name = match.group(1)
                if func_name not in ('if', 'for', 'while', 'switch', 'return'):
                    functions.append({
                        "name": func_name,
                        "line": i + 1
                    })
        
        return functions[:50]
    
    def _extract_classes(self, content: str, suffix: str) -> List[Dict]:
        """提取类定义"""
        classes = []
        
        class_pattern = r'^\s*(?:class|struct)\s+([a-zA-Z_][a-zA-Z0-9_]*)'
        
        lines = content.split('\n')
        for i, line in enumerate(lines):
            match = re.search(class_pattern, line)
            if match:
                classes.append({
                    "name": match.group(1),
                    "line": i + 1
                })
        
        return classes[:50]
    
    def _identify_language(self, filepath: Path) -> str:
        """识别编程语言"""
        suffix_map = {
            '.py': 'python',
            '.java': 'java',
            '.cpp': 'cpp', '.cc': 'cpp', '.hpp': 'cpp_header',
            '.c': 'c', '.h': 'c_header',
            '.js': 'javascript', '.jsx': 'jsx',
            '.ts': 'typescript', '.tsx': 'tsx',
            '.go': 'go',
            '.rs': 'rust',
            '.rb': 'ruby',
            '.php': 'php',
            '.swift': 'swift',
            '.sh': 'shell', '.bash': 'shell',
            '.ps1': 'powershell',
            '.bat': 'batch', '.cmd': 'batch',
            '.css': 'css', '.scss': 'scss', '.less': 'less',
        }
        return suffix_map.get(filepath.suffix.lower(), 'unknown')
    
    def _generate_code_summary(self, filepath: Path, content: str, 
                               analysis: SourceCodeAnalysis) -> HumanReadableSummary:
        """生成代码摘要"""
        lines = content.split('\n')
        
        summary_parts = [
            f"Language: {analysis.language}",
            f"Total lines: {analysis.total_lines}",
            f"Code lines: {analysis.code_lines}",
            f"Comment lines: {analysis.comment_lines}",
        ]
        
        if analysis.functions:
            summary_parts.append(f"Functions: {len(analysis.functions)}")
        if analysis.classes:
            summary_parts.append(f"Classes: {len(analysis.classes)}")
        if analysis.imports:
            summary_parts.append(f"Imports: {len(analysis.imports)}")
        
        return HumanReadableSummary(
            title=filepath.name,
            line_count=analysis.total_lines,
            character_count=len(content),
            first_lines=lines[:10],
            summary='\n'.join(summary_parts)
        )


# ==================== 配置文件处理器 ====================

class ConfigFileProcessor(BaseFileProcessor):
    """配置文件处理器"""
    
    CONFIG_EXTENSIONS = {
        '.yaml', '.yml', '.json', '.xml', '.toml', '.ini', 
        '.cfg', '.conf', '.env', '.properties', '.tf', '.tls'
    }
    
    def __init__(self, config: Optional[Dict] = None):
        super().__init__(config)
        self.debug = config.get('debug', False) if config else False
    
    def can_handle(self, file_digest: FileDigest) -> bool:
        # 优先使用分类阶段确定的策略
        if file_digest.metadata.processing_strategy == ProcessingStrategy.STRUCTURE_EXTRACT:
            return True
        
        # 其次检查文件类型
        if file_digest.metadata.file_type == FileType.TEXT_DATA:
            suffix = file_digest.metadata.path.suffix.lower()
            return suffix in self.CONFIG_EXTENSIONS
        
        return False
    
    def process(self, file_digest: FileDigest, content: str, mode: str = "framework", 
                strategy: ProcessingStrategy = ProcessingStrategy.STRUCTURE_EXTRACT) -> FileDigest:
        
        if not content:
            return file_digest
        
        filepath = file_digest.metadata.path
        
        # 使用枚举值进行比较，而不是枚举对象本身
        strategy_value = strategy.value if hasattr(strategy, 'value') else str(strategy)
        
        # 处理完整内容：对于 FULL_CONTENT 策略，总是设置完整内容（如果文件大小允许）
        if strategy_value == ProcessingStrategy.FULL_CONTENT.value and file_digest.metadata.size <= self.max_full_content_size:
            file_digest.full_content = content
            # FULL_CONTENT策略：不生成摘要
            file_digest.human_readable_summary = None
        else:
            # 对于其他策略，仅在 full 模式下且文件大小允许时设置完整内容
            if self._should_include_full_content(file_digest, mode):
                file_digest.full_content = content
            else:
                file_digest.full_content = None
        
        # 分析配置结构
        config_analysis = self._analyze_config(filepath, content, strategy)
        
        # 生成摘要：对于 FULL_CONTENT 策略，不生成摘要
        if strategy_value == ProcessingStrategy.FULL_CONTENT.value:
            # 已经设置为None，不需要额外处理
            pass
        else:
            # 对于其他策略，生成正常摘要
            summary = self._generate_config_summary(filepath, content, config_analysis, strategy)
            file_digest.human_readable_summary = summary
        
        return file_digest
    
    def _analyze_config(self, filepath: Path, content: str, 
                       strategy: ProcessingStrategy) -> ConfigAnalysisResult:
        """分析配置文件结构"""
        suffix = filepath.suffix.lower()
        keys = []
        sections = []
        structure_summary = None
        
        if suffix in ('.yaml', '.yml'):
            keys, sections, structure_summary = self._analyze_yaml(content)
        elif suffix == '.json':
            keys, sections, structure_summary = self._analyze_json(content)
        elif suffix in ('.ini', '.cfg', '.conf'):
            keys, sections, structure_summary = self._analyze_ini(content)
        elif suffix == '.toml':
            keys, sections, structure_summary = self._analyze_toml(content)
        elif suffix == '.xml':
            keys, sections, structure_summary = self._analyze_xml(content)
        
        return ConfigAnalysisResult(
            keys=keys,
            sections=sections,
            structure_summary=structure_summary
        )
    
    def _analyze_yaml(self, content: str) -> Tuple[List[str], List[str], Optional[str]]:
        """分析 YAML 配置"""
        keys = []
        sections = []
        
        # 简单的键提取（不依赖 PyYAML）
        lines = content.split('\n')
        for line in lines:
            line = line.strip()
            if line and not line.startswith('#') and ':' in line:
                key_part = line.split(':', 1)[0].strip()
                if key_part and not key_part.startswith('-'):
                    keys.append(key_part)
        
        structure_summary = f"YAML config with {len(set(keys))} top-level keys"
        return list(set(keys)), sections, structure_summary
    
    def _analyze_json(self, content: str) -> Tuple[List[str], List[str], Optional[str]]:
        """分析 JSON 配置"""
        keys = []
        sections = []
        
        try:
            data = json.loads(content)
            if isinstance(data, dict):
                keys = list(data.keys())
                structure_summary = f"JSON object with {len(keys)} keys"
            else:
                structure_summary = f"JSON {type(data).__name__}"
        except json.JSONDecodeError:
            structure_summary = "Invalid JSON"
        
        return keys, sections, structure_summary
    
    def _analyze_ini(self, content: str) -> Tuple[List[str], List[str], Optional[str]]:
        """分析 INI 配置"""
        keys = []
        sections = []
        
        lines = content.split('\n')
        for line in lines:
            line = line.strip()
            if line.startswith('[') and line.endswith(']'):
                sections.append(line[1:-1])
            elif line and not line.startswith('#') and '=' in line:
                key = line.split('=', 1)[0].strip()
                if key:
                    keys.append(key)
        
        structure_summary = f"INI config with {len(sections)} sections, {len(set(keys))} keys"
        return list(set(keys)), sections, structure_summary
    
    def _analyze_toml(self, content: str) -> Tuple[List[str], List[str], Optional[str]]:
        """分析 TOML 配置"""
        # 类似 INI 分析
        return self._analyze_ini(content)
    
    def _analyze_xml(self, content: str) -> Tuple[List[str], List[str], Optional[str]]:
        """分析 XML 配置"""
        keys = []
        sections = []
        
        # 简单标签提取
        tags = re.findall(r'<(\w+)[^>]*>', content)
        keys = list(set(tags))
        
        structure_summary = f"XML with {len(keys)} unique tags"
        return keys, sections, structure_summary
    
    def _generate_config_summary(self, filepath: Path, content: str,
                                 config_analysis: ConfigAnalysisResult,
                                 strategy: ProcessingStrategy) -> HumanReadableSummary:
        """生成配置文件摘要"""
        lines = content.split('\n')
        
        summary_parts = [f"Config type: {filepath.suffix[1:]}"]
        
        if config_analysis.structure_summary:
            summary_parts.append(config_analysis.structure_summary)
        
        if config_analysis.sections:
            summary_parts.append(f"Sections: {', '.join(config_analysis.sections[:10])}")
            if len(config_analysis.sections) > 10:
                summary_parts[-1] += f" (+{len(config_analysis.sections) - 10} more)"
        
        if config_analysis.keys:
            summary_parts.append(f"Keys: {', '.join(config_analysis.keys[:15])}")
            if len(config_analysis.keys) > 15:
                summary_parts[-1] += f" (+{len(config_analysis.keys) - 15} more)"
        
        return HumanReadableSummary(
            title=filepath.name,
            line_count=len(lines),
            character_count=len(content),
            first_lines=lines[:15],
            summary='\n'.join(summary_parts)
        )


# ==================== 数据文件处理器 ====================

class DataFileProcessor(BaseFileProcessor):
    """数据文件处理器（CSV、TSV、日志、SPICE内核等）"""
    
    DATA_EXTENSIONS = {
        '.csv', '.tsv', '.log', '.out', '.err', '.dat', '.txt',
        '.tls', '.tpc', '.ker', '.cmt', '.tm'  # 扩展SPICE内核文件
    }
    
    def __init__(self, config: Optional[Dict] = None):
        super().__init__(config)
        self.debug = config.get('debug', False) if config else False
        
        # 导入智能分析器 - 修正导入路径
        try:
            # 使用绝对导入路径
            from tools._directory_digest.analyzers.semantics.sheets import SmartDataFileAnalyzer
            self.smart_analyzer = SmartDataFileAnalyzer(debug=self.debug)
            self.smart_analyzer_available = True
            if self.debug:
                import sys
                print(f"[DEBUG:DataFileProcessor] SmartDataFileAnalyzer loaded successfully", file=sys.stderr)
        except ImportError as e:
            # 尝试相对导入作为后备
            try:
                from ..analyzers.semantics.sheets import SmartDataFileAnalyzer
                self.smart_analyzer = SmartDataFileAnalyzer(debug=self.debug)
                self.smart_analyzer_available = True
                if self.debug:
                    import sys
                    print(f"[DEBUG:DataFileProcessor] SmartDataFileAnalyzer loaded via relative import", file=sys.stderr)
            except ImportError as e2:
                self.smart_analyzer = None
                self.smart_analyzer_available = False
                if self.debug:
                    import sys
                    print(f"[DEBUG:DataFileProcessor] SmartDataFileAnalyzer not available: {e2}", file=sys.stderr)
    
    def can_handle(self, file_digest: FileDigest) -> bool:
        # 添加调试输出
        if self.debug:
            import sys
            print(f"[DEBUG:DataFileProcessor.can_handle] Checking file: {file_digest.metadata.path}", file=sys.stderr)
            print(f"[DEBUG:DataFileProcessor.can_handle]   File type: {file_digest.metadata.file_type}", file=sys.stderr)
            print(f"[DEBUG:DataFileProcessor.can_handle]   Strategy: {file_digest.metadata.processing_strategy}", file=sys.stderr)
        
        # 优先使用分类阶段确定的策略和类型
        if file_digest.metadata.file_type == FileType.TEXT_DATA:
            # 明确排除配置文件（应由ConfigFileProcessor处理）
            if file_digest.metadata.processing_strategy == ProcessingStrategy.HEADER_WITH_STATS:
                suffix = file_digest.metadata.path.suffix.lower()
                result = suffix in self.DATA_EXTENSIONS
                if self.debug:
                    import sys
                    print(f"[DEBUG:DataFileProcessor.can_handle]   TEXT_DATA with HEADER_WITH_STATS, suffix {suffix} in DATA_EXTENSIONS: {result}", file=sys.stderr)
                return result
            # 如果没有明确策略，根据扩展名判断，但排除已明确分类的
            if file_digest.metadata.processing_strategy is None:
                suffix = file_digest.metadata.path.suffix.lower()
                if suffix in self.DATA_EXTENSIONS:
                    # 避免与其他处理器冲突
                    result = file_digest.metadata.file_type not in (
                        FileType.CRITICAL_DOCS, FileType.REFERENCE_DOCS, FileType.SOURCE_CODE
                    )
                    if self.debug:
                        import sys
                        print(f"[DEBUG:DataFileProcessor.can_handle]   TEXT_DATA with no strategy, suffix {suffix} in DATA_EXTENSIONS, not other types: {result}", file=sys.stderr)
                    return result
        
        # 或者，如果文件策略明确为HEADER_WITH_STATS，即使不在扩展名列表中，也应该处理
        # 这是为了处理规则引擎分配了HEADER_WITH_STATS策略但扩展名不在列表中的情况
        if file_digest.metadata.processing_strategy == ProcessingStrategy.HEADER_WITH_STATS:
            # 检查是否是SPICE内核文件或其他数据文件
            suffix = file_digest.metadata.path.suffix.lower()
            if suffix in ('.tls', '.tpc', '.ker', '.cmt', '.tm'):  # SPICE内核文件
                if self.debug:
                    import sys
                    print(f"[DEBUG:DataFileProcessor.can_handle]   HEADER_WITH_STATS with SPICE suffix {suffix}: True", file=sys.stderr)
                return True
            
            # 使用智能分析器检测是否需要处理
            if self.smart_analyzer_available:
                result = self.smart_analyzer.can_handle(file_digest.metadata.path, None)
                if self.debug:
                    import sys
                    print(f"[DEBUG:DataFileProcessor.can_handle]   smart_analyzer.can_handle: {result}", file=sys.stderr)
                return result
            else:
                if self.debug:
                    import sys
                    print(f"[DEBUG:DataFileProcessor.can_handle]   smart_analyzer not available", file=sys.stderr)
        
        if self.debug:
            import sys
            print(f"[DEBUG:DataFileProcessor.can_handle]   No match, returning False", file=sys.stderr)
        return False
    
    def process(self, file_digest: FileDigest, content: str, mode: str = "framework", 
                strategy: ProcessingStrategy = ProcessingStrategy.HEADER_WITH_STATS) -> FileDigest:
        
        import sys
        
        if self.debug:
            print(f"[DEBUG:DataFileProcessor] Processing file: {file_digest.metadata.path}", file=sys.stderr)
            print(f"[DEBUG:DataFileProcessor]   Strategy: {strategy}", file=sys.stderr)
            print(f"[DEBUG:DataFileProcessor]   File type: {file_digest.metadata.file_type}", file=sys.stderr)
        
        if not content:
            return file_digest
        
        filepath = file_digest.metadata.path
        
        # 使用枚举值进行比较，而不是枚举对象本身
        strategy_value = strategy.value if hasattr(strategy, 'value') else str(strategy)
        
        # 处理完整内容：对于 FULL_CONTENT 策略，总是设置完整内容（如果文件大小允许）
        if strategy_value == ProcessingStrategy.FULL_CONTENT.value and file_digest.metadata.size <= self.max_full_content_size:
            file_digest.full_content = content
            # FULL_CONTENT策略：不生成摘要
            file_digest.human_readable_summary = None
        else:
            # 对于其他策略，仅在 full 模式下且文件大小允许时设置完整内容
            if mode == "full" and strategy_value == ProcessingStrategy.FULL_CONTENT.value:
                file_digest.full_content = content
            else:
                file_digest.full_content = None
        
        # 生成摘要：对于 FULL_CONTENT 策略，不生成摘要
        if strategy_value == ProcessingStrategy.FULL_CONTENT.value:
            # 已经设置为None，不需要额外处理
            pass
        else:
            # 对于其他策略，生成正常摘要
            summary = self._generate_data_summary(filepath, content, strategy)
            file_digest.human_readable_summary = summary
        
        return file_digest
    
    def _generate_data_summary(self, filepath: Path, content: str,
                               strategy: ProcessingStrategy) -> HumanReadableSummary:
        """生成数据文件摘要 - 增强版，支持智能分析"""
        lines = content.split('\n')
        line_count = len(lines)
        
        suffix = filepath.suffix.lower()
        
        # 尝试使用智能分析器
        smart_analysis = None
        if self.smart_analyzer_available:
            try:
                analysis_result = self.smart_analyzer.analyze(filepath, content)
                if analysis_result.success:
                    smart_analysis = analysis_result.metadata.get("data_analysis", {})
                    if self.debug:
                        import sys
                        print(f"[DEBUG:DataFileProcessor] Smart analysis successful for {filepath}", file=sys.stderr)
            except Exception as e:
                if self.debug:
                    import sys
                    print(f"[DEBUG:DataFileProcessor] Smart analysis failed for {filepath}: {e}", file=sys.stderr)
        
        # 计算通用统计信息
        stats = self._calculate_data_stats(content, suffix, smart_analysis)
        
        # 智能提取头部
        header_lines = self._extract_smart_header(filepath, content, suffix, smart_analysis)
        
        # 构建摘要
        summary_parts = self._build_summary_parts(filepath, content, suffix, stats, smart_analysis)
        
        return HumanReadableSummary(
            title=filepath.name,
            line_count=line_count,
            character_count=len(content),
            first_lines=header_lines,
            summary='\n'.join(summary_parts)
        )
    
    def _calculate_data_stats(self, content: str, suffix: str, smart_analysis: Optional[Dict]) -> Dict[str, Any]:
        """计算数据统计信息 - 增强版，结合智能分析"""
        stats = {}
        lines = content.split('\n')
        
        # 如果有智能分析结果，优先使用
        if smart_analysis:
            smart_stats = smart_analysis.get("stats", {})
            if smart_stats:
                stats.update(smart_stats)
                # 重命名一些键以保持一致性
                key_mapping = {
                    "total_lines": "Total lines",
                    "estimated_records": "Estimated records",
                    "estimated_columns": "Estimated columns",
                    "non_empty_lines": "Non-empty lines",
                }
                
                for smart_key, display_key in key_mapping.items():
                    if smart_key in smart_stats:
                        stats[display_key] = smart_stats[smart_key]
        
        # 如果没有智能分析或缺少某些统计，计算基本统计
        if "Total lines" not in stats:
            stats["Total lines"] = len(lines)
        
        if "Non-empty lines" not in stats:
            stats["Non-empty lines"] = sum(1 for l in lines if l.strip())
        
        # 文件特定统计
        if suffix == '.csv':
            # CSV 统计
            non_empty_lines = [l for l in lines if l.strip()]
            if non_empty_lines:
                stats["Data rows (approx)"] = len(non_empty_lines)
                # 估算列数
                first_line = non_empty_lines[0]
                stats["Columns (approx)"] = first_line.count(',') + 1
        
        elif suffix in ('.log', '.out', '.err'):
            # 日志统计
            error_count = sum(1 for l in lines if 'error' in l.lower() or 'exception' in l.lower())
            warning_count = sum(1 for l in lines if 'warn' in l.lower())
            
            if error_count > 0:
                stats["Errors"] = error_count
            if warning_count > 0:
                stats["Warnings"] = warning_count
        
        # 通用统计
        if "File size" not in stats:
            stats["File size"] = f"{len(content)} chars"
        
        return stats
    
    def _extract_smart_header(self, filepath: Path, content: str, suffix: str, 
                             smart_analysis: Optional[Dict]) -> List[str]:
        """智能提取数据文件头部"""
        lines = content.split('\n')
        
        # 如果有智能分析结果，使用其头部信息
        if smart_analysis:
            partitions = smart_analysis.get("partitions", {})
            header_lines = partitions.get("header_lines", [])
            if header_lines:
                return header_lines[:30]  # 限制头部大小
        
        # 否则使用通用头部提取
        header_lines = []
        
        for i, line in enumerate(lines):
            stripped = line.strip()
            
            # 保留注释行
            if stripped and stripped.startswith(('#', '//', '/*', '*', '!', 'C', 'CC')):
                header_lines.append(line)
                continue
            
            # 保留空行直到遇到数据
            if not stripped and header_lines:
                header_lines.append(line)
                continue
            
            # 数据文件的前几行
            if i < 20:
                header_lines.append(line)
            
            # 如果看起来像数据行了，停止
            if i > 10 and stripped and not stripped.startswith(('#', '//', '/*')):
                # 检查是否是纯数据行
                if len(re.findall(r'[a-zA-Z]', stripped)) / len(stripped) < 0.3:
                    break
        
        return header_lines[:30]
    
    def _build_summary_parts(self, filepath: Path, content: str, suffix: str,
                            stats: Dict[str, Any], smart_analysis: Optional[Dict]) -> List[str]:
        """构建摘要部分"""
        summary_parts = [
            f"Data type: {suffix[1:] if suffix else 'text'}"
        ]
        
        # 如果有智能分析，添加文件类型信息
        if smart_analysis:
            file_type = smart_analysis.get("file_type", "")
            structure_type = smart_analysis.get("structure_type", "")
            
            if file_type:
                summary_parts.append(f"File type: {file_type}")
            if structure_type:
                summary_parts.append(f"Structure: {structure_type}")
        
        # 添加统计信息
        for key, value in stats.items():
            # 只添加一些关键统计信息
            if key in ["Total lines", "Non-empty lines", "Estimated records", 
                      "Estimated columns", "Data rows (approx)", "Columns (approx)"]:
                summary_parts.append(f"{key}: {value}")
        
        # 添加智能分析的其他信息
        if smart_analysis:
            partitions = smart_analysis.get("partitions", {})
            if partitions.get("data_section_start") is not None:
                summary_parts.append(f"Data starts at line: {partitions['data_section_start']}")
            
            if "comment_ratio" in partitions:
                summary_parts.append(f"Comments: {partitions['comment_ratio']:.1%}")
        
        # 特殊文件类型提示
        if suffix in ('.tls', '.tpc', '.ker', '.cmt'):
            summary_parts.append("Note: SPICE kernel file - contains structured ephemeris data")
        
        return summary_parts


# ==================== 重构后的处理器注册表 ====================

class FileProcessorRegistry:
    """文件处理器注册表 - 整合处理流程协调与并行处理支持"""
    
    def __init__(self, 
                 rule_engine=None, 
                 context_manager=None, 
                 stats=None, 
                 config=None):
        """
        初始化处理器注册表
        
        Args:
            rule_engine: 规则引擎实例，用于文件分类
            context_manager: 上下文管理器实例，用于Token分配
            stats: 统计信息字典，用于更新处理统计
            config: 配置字典，包含大小限制等参数
        """
        self.processors: List[BaseFileProcessor] = []
        self.rule_engine = rule_engine
        self.context_manager = context_manager
        self.stats = stats or {}
        self.config = config or {}
        
        # 添加debug标志
        self.debug = self.config.get('debug', False)
        
        # 并行处理配置
        self.max_file_size = self.config.get('max_file_size', 10 * 1024 * 1024 * 1024)  # 默认10GB
        
        # 线程锁，用于并行处理时安全更新统计信息
        self._stats_lock = None
        self._context_lock = None
        
    def register(self, processor: BaseFileProcessor):
        """注册处理器"""
        self.processors.append(processor)
    
    def get_processor(self, file_digest: FileDigest) -> Optional[BaseFileProcessor]:
        """获取适合此文件的处理器"""
        if self.debug:
            import sys
            print(f"[DEBUG:ProcessorRegistry.get_processor] Finding processor for: {file_digest.metadata.path}", file=sys.stderr)
        
        for processor in self.processors:
            can_handle = processor.can_handle(file_digest)
            if self.debug:
                import sys
                print(f"[DEBUG:ProcessorRegistry.get_processor]   {type(processor).__name__}.can_handle: {can_handle}", file=sys.stderr)
            if can_handle:
                if self.debug:
                    import sys
                    print(f"[DEBUG:ProcessorRegistry.get_processor]   Selected processor: {type(processor).__name__}", file=sys.stderr)
                return processor
        
        if self.debug:
            import sys
            print(f"[DEBUG:ProcessorRegistry.get_processor]   No processor found", file=sys.stderr)
        return None
    
    def process_file(self, file_digest: FileDigest, mode: str = "framework", 
                    strategy: Optional[ProcessingStrategy] = None) -> bool:
        """
        处理单个文件 - 整合原 _process_file 逻辑
        
        Args:
            file_digest: 文件摘要对象
            mode: 输出模式 ("full", "framework", "sort")
            strategy: 指定的处理策略（可选，默认从元数据或重新分类获取）
            
        Returns:
            bool: 处理是否成功
        """
        import sys
        
        filepath = file_digest.metadata.path
        
        if self.debug:
            print(f"[DEBUG:ProcessorRegistry] Processing file: {filepath}", file=sys.stderr)
            print(f"[DEBUG:ProcessorRegistry]   Mode: {mode}", file=sys.stderr)
            print(f"[DEBUG:ProcessorRegistry]   Input strategy: {strategy}", file=sys.stderr)
            print(f"[DEBUG:ProcessorRegistry]   Metadata strategy: {file_digest.metadata.processing_strategy}", file=sys.stderr)
        
        try:
            # 1. 获取处理策略（优先使用传入的参数或元数据中的策略）
            if strategy is not None:
                # 使用传入的策略
                if self.debug:
                    print(f"[DEBUG:ProcessorRegistry]   Using provided strategy: {strategy}", file=sys.stderr)
                pass
            elif file_digest.metadata.processing_strategy is not None:
                # 使用元数据中已设置的策略
                strategy = file_digest.metadata.processing_strategy
                if self.debug:
                    print(f"[DEBUG:ProcessorRegistry]   Using metadata strategy: {strategy}", file=sys.stderr)
            else:
                # 重新分类（原有逻辑）
                if self.debug:
                    print(f"[DEBUG:ProcessorRegistry]   Reclassifying file", file=sys.stderr)
                if self.rule_engine:
                    strategy, force_binary = self.rule_engine.classify_file(filepath)
                else:
                    strategy, force_binary = self._default_classify(file_digest)
            
            # 强制二进制标记优先从元数据获取
            force_binary = getattr(file_digest.metadata, 'force_binary', False)
            
            if self.debug:
                print(f"[DEBUG:ProcessorRegistry]   Final strategy: {strategy}", file=sys.stderr)
                print(f"[DEBUG:ProcessorRegistry]   Force binary: {force_binary}", file=sys.stderr)
            
            # 2. 检查文件大小限制
            if file_digest.metadata.size > self.max_file_size:
                if self.debug:
                    print(f"[DEBUG:ProcessorRegistry]   File size {file_digest.metadata.size} exceeds max {self.max_file_size}, skipping", file=sys.stderr)
                self._update_stats('skipped_large_files')
                self._process_as_binary(file_digest, mode)
                return True
            
            # 4. 根据策略处理文件
            if force_binary or strategy == ProcessingStrategy.METADATA_ONLY:
                if self.debug:
                    print(f"[DEBUG:ProcessorRegistry]   Force binary or METADATA_ONLY, processing as binary", file=sys.stderr)
                success = self._process_as_binary(file_digest, mode)
                if success:
                    self._update_stats('binary_files')
            else:
                # 获取合适的处理器并执行处理
                processor = self.get_processor(file_digest)
                if processor:
                    if self.debug:
                        print(f"[DEBUG:ProcessorRegistry]   Found processor: {type(processor).__name__}", file=sys.stderr)
                    
                    # 读取文件内容
                    content = self._read_file_content(filepath)
                    if content is None:
                        if self.debug:
                            print(f"[DEBUG:ProcessorRegistry]   Failed to read content, processing as binary", file=sys.stderr)
                        self._process_as_binary(file_digest, mode)
                        self._update_stats('binary_files')
                        return True
                    
                    if self.debug:
                        print(f"[DEBUG:ProcessorRegistry]   Content read successfully, length: {len(content)} chars", file=sys.stderr)
                        if strategy == ProcessingStrategy.FULL_CONTENT:
                            print(f"[DEBUG:ProcessorRegistry]   FULL_CONTENT strategy, max_full_content_size: {processor.max_full_content_size}", file=sys.stderr)
                    
                    # 设置处理器的debug标志
                    if hasattr(processor, 'debug'):
                        processor.debug = self.debug
                    
                    # 执行处理（传入策略确保一致性）
                    original_content = content  # 保存原始内容
                    file_digest = processor.process(file_digest, content, mode, strategy)
                    
                    # 检查：如果策略是FULL_CONTENT，但full_content没有被设置，则用原始内容设置
                    if strategy == ProcessingStrategy.FULL_CONTENT and file_digest.full_content is None:
                        if self.debug:
                            print(f"[DEBUG:ProcessorRegistry]   WARNING: Processor didn't set full_content for FULL_CONTENT strategy, setting it now", file=sys.stderr)
                        file_digest.full_content = original_content
                        file_digest.human_readable_summary = None
                    
                    if self.debug:
                        print(f"[DEBUG:ProcessorRegistry]   After processor.process:", file=sys.stderr)
                        print(f"[DEBUG:ProcessorRegistry]     metadata.processing_strategy: {file_digest.metadata.processing_strategy}", file=sys.stderr)
                        print(f"[DEBUG:ProcessorRegistry]     strategy argument: {strategy}", file=sys.stderr)
                        print(f"[DEBUG:ProcessorRegistry]     full_content set: {file_digest.full_content is not None}", file=sys.stderr)
                        if file_digest.full_content:
                            print(f"[DEBUG:ProcessorRegistry]     full_content length: {len(file_digest.full_content)} chars", file=sys.stderr)
                        print(f"[DEBUG:ProcessorRegistry]     human_readable_summary set: {file_digest.human_readable_summary is not None}", file=sys.stderr)
                    
                    # 根据文件类型更新统计
                    self._update_stats_by_processor(file_digest, processor)
                    
                else:
                    # 无匹配处理器，作为二进制处理
                    if self.debug:
                        print(f"[DEBUG:ProcessorRegistry]   No matching processor found, processing as binary", file=sys.stderr)
                    self._process_as_binary(file_digest, mode)
                    self._update_stats('binary_files')
            
            return True
            
        except Exception as e:
            import sys
            print(f"Warning: Error processing file {filepath}: {e}", file=sys.stderr)
            if self.debug:
                import traceback
                print(f"[DEBUG:ProcessorRegistry]   Exception details:", file=sys.stderr)
                traceback.print_exc(file=sys.stderr)
            try:
                self._process_as_binary(file_digest, mode)
                self._update_stats('binary_files')
            except:
                pass
            return False
    
    def _check_and_allocate_context(self, estimated_tokens: int, 
                                   file_digest: FileDigest, 
                                   strategy: ProcessingStrategy) -> Tuple[bool, ProcessingStrategy, int]:
        """
        检查并分配上下文Token，支持策略降级
        
        Returns:
            Tuple[bool, ProcessingStrategy, int]: (是否成功, 最终策略, 最终估算tokens)
        """
        # 尝试分配Token
        if not self.context_manager.can_allocate(estimated_tokens):
            # Token不足，尝试降级策略
            downgraded_strategy = self.context_manager.downgrade_strategy(strategy)
            if self.rule_engine:
                downgraded_tokens = self.rule_engine.estimate_token_usage(file_digest.metadata.path, downgraded_strategy)
            else:
                downgraded_tokens = self._estimate_tokens(file_digest, downgraded_strategy)
            
            if not self.context_manager.can_allocate(downgraded_tokens):
                return False, strategy, estimated_tokens
            
            # 使用降级后的策略
            strategy = downgraded_strategy
            estimated_tokens = downgraded_tokens
        
        # 分配Token
        file_record = {
            "path": str(file_digest.metadata.path),
            "strategy": strategy.value,
            "estimated_tokens": estimated_tokens,
            "size": file_digest.metadata.size,
        }
        
        success = self.context_manager.allocate(estimated_tokens, file_record)
        return success, strategy, estimated_tokens
    
    def _process_as_binary(self, file_digest: FileDigest, mode: str) -> bool:
        """处理为二进制文件 - 仅计算哈希"""
        try:
            self._calculate_hashes(file_digest)
            file_digest.metadata.file_type = FileType.BINARY_FILES
            return True
        except Exception as e:
            import sys
            print(f"Warning: Failed to process binary file {file_digest.metadata.path}: {e}", file=sys.stderr)
            return False
    
    def _calculate_hashes(self, file_digest: FileDigest):
        """计算文件哈希值"""
        import hashlib
        filepath = file_digest.metadata.path
        
        md5_hash = hashlib.md5()
        sha256_hash = hashlib.sha256()
        
        with open(filepath, 'rb') as f:
            for chunk in iter(lambda: f.read(65536), b''):
                md5_hash.update(chunk)
                sha256_hash.update(chunk)
        
        file_digest.metadata.md5_hash = md5_hash.hexdigest()
        file_digest.metadata.sha256_hash = sha256_hash.hexdigest()
    
    def _read_file_content(self, filepath: Path) -> Optional[str]:
        """读取文件内容，处理编码问题"""
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                return f.read()
        except UnicodeDecodeError:
            try:
                with open(filepath, 'rb') as f:
                    raw_content = f.read()
                    encodings_to_try = ['utf-8', 'latin-1', 'iso-8859-1', 'cp1252']
                    for encoding in encodings_to_try:
                        try:
                            return raw_content.decode(encoding)
                        except UnicodeDecodeError:
                            continue
                    return raw_content.decode('latin-1', errors='ignore')
            except Exception:
                return None
    
    def _default_classify(self, file_digest: FileDigest) -> Tuple[ProcessingStrategy, bool]:
        """默认文件分类（当没有规则引擎时）"""
        suffix = file_digest.metadata.path.suffix.lower()
        size = file_digest.metadata.size
        
        # 二进制扩展名
        binary_exts = {'.exe', '.dll', '.so', '.dylib', '.zip', '.tar', '.gz', 
                      '.jpg', '.png', '.mp3', '.mp4', '.pdf'}
        if suffix in binary_exts:
            return ProcessingStrategy.METADATA_ONLY, True
        
        # 源代码扩展名
        code_exts = {'.py', '.java', '.cpp', '.c', '.js', '.ts', '.go', '.rs'}
        if suffix in code_exts:
            return ProcessingStrategy.CODE_SKELETON, False
        
        # 文档扩展名
        doc_exts = {'.md', '.txt', '.rst', '.html'}
        if suffix in doc_exts:
            if size < 500 * 1024:
                return ProcessingStrategy.SUMMARY_ONLY, False
            else:
                return ProcessingStrategy.HEADER_WITH_STATS, False
        
        # 默认
        if size > 1024 * 1024:  # > 1MB
            return ProcessingStrategy.METADATA_ONLY, False
        return ProcessingStrategy.SUMMARY_ONLY, False
    
    def _estimate_tokens(self, file_digest: FileDigest, strategy: ProcessingStrategy) -> int:
        """估算Token消耗（当没有规则引擎时）"""
        config = STRATEGY_CONFIGS.get(strategy, STRATEGY_CONFIGS[ProcessingStrategy.METADATA_ONLY])
        
        if strategy == ProcessingStrategy.METADATA_ONLY:
            return int(config.token_estimate * 100)
        
        file_size = file_digest.metadata.size
        if config.max_size and file_size > config.max_size:
            return STRATEGY_CONFIGS[ProcessingStrategy.METADATA_ONLY].token_estimate * 100
        
        estimated_chars = min(file_size, config.max_size or file_size)
        return int(estimated_chars * config.token_estimate)
    
    def _update_stats(self, key: str):
        """更新统计信息（线程安全）"""
        if key in self.stats:
            # 如果启用了并行处理，需要加锁
            if self._stats_lock:
                import threading
                with self._stats_lock:
                    self.stats[key] += 1
            else:
                self.stats[key] += 1
    
    def _update_stats_by_processor(self, file_digest: FileDigest, processor: BaseFileProcessor):
        """根据处理器类型更新统计"""
        processor_type = type(processor).__name__
        
        # 检查是否为关键文档
        is_critical = False
        if processor_type == 'TextFileProcessor':
            filename = file_digest.metadata.path.name.lower()
            critical_patterns = ['readme', 'license', 'copying', 'notice', 'changelog', 'changes', 
                                'contributing', 'install', 'authors', 'news', 'todo', 'roadmap']
            is_critical = any(pattern in filename for pattern in critical_patterns)
        
        # 统计键映射 - 与 FileType 枚举完全一致
        type_mapping = {
            'SourceCodeProcessor': 'source_code',
            'TextFileProcessor': 'critical_docs' if is_critical else 'reference_docs',
            'ConfigFileProcessor': 'text_data',
            'DataFileProcessor': 'text_data'
        }
        
        stat_key = type_mapping.get(processor_type, 'binary_files')
        self._update_stats(stat_key)
        
        # 同时更新文件类型元数据 - 与 FileType 枚举完全一致
        file_type_mapping = {
            'SourceCodeProcessor': FileType.SOURCE_CODE,
            'TextFileProcessor': FileType.CRITICAL_DOCS if is_critical else FileType.REFERENCE_DOCS,
            'ConfigFileProcessor': FileType.TEXT_DATA,
            'DataFileProcessor': FileType.TEXT_DATA
        }
        file_digest.metadata.file_type = file_type_mapping.get(
            processor_type, FileType.BINARY_FILES
        )
        
        # 如果没有匹配的处理器，设置为 UNKNOWN
        if processor_type not in type_mapping:
            file_digest.metadata.file_type = FileType.UNKNOWN
            self._update_stats('unknown')
    
    def process_directory(self, structure: Any, mode: str = "framework", 
                         parallel: bool = False, max_workers: int = 4):
        """
        处理整个目录结构，支持并行处理
        
        Args:
            structure: DirectoryStructure 对象
            mode: 输出模式
            parallel: 是否启用并行处理
            max_workers: 并行工作线程数
        """
        # 收集所有文件
        all_files = []
        
        def collect_files(node):
            all_files.extend(node.files)
            for subdir in node.subdirectories.values():
                collect_files(subdir)
        
        collect_files(structure)
        
        if parallel and len(all_files) > 10:
            self._process_parallel(all_files, mode, max_workers)
        else:
            self._process_sequential(all_files, mode)
    
    def _process_sequential(self, files: List[FileDigest], mode: str):
        """顺序处理文件"""
        for file_digest in files:
            self.process_file(file_digest, mode)
    
    def _process_parallel(self, files: List[FileDigest], mode: str, max_workers: int):
        """并行处理文件 - 修正版"""
        import concurrent.futures
        import threading
        import sys
        
        # 初始化线程锁
        self._stats_lock = threading.Lock()
        
        # 预筛选：顺序检查Token和大小限制（避免在worker中处理）
        files_to_process = []
        skipped_count = {'skipped_by_context': 0, 'skipped_large_files': 0}
        
        for file_digest in files:
            filepath = file_digest.metadata.path
            
            # 检查大小限制
            if file_digest.metadata.size > self.max_file_size:
                skipped_count['skipped_large_files'] += 1
                self._process_as_binary(file_digest, mode)
                with self._stats_lock:
                    self.stats['binary_files'] += 1
                continue
            
            # 预检查Token（不实际分配，只检查可行性）
            if self.rule_engine:
                strategy, _ = self.rule_engine.classify_file(filepath)
                estimated = self.rule_engine.estimate_token_usage(filepath, strategy)
            else:
                strategy, _ = self._default_classify(file_digest)
                estimated = self._estimate_tokens(file_digest, strategy)
            
            if self.context_manager and not self.context_manager.can_allocate(estimated):
                skipped_count['skipped_by_context'] += 1
                with self._stats_lock:
                    self.stats['skipped_by_context'] += 1
                continue
            
            files_to_process.append((file_digest, strategy, estimated))
        
        # 并行处理筛选后的文件
        processed_files = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_file = {
                executor.submit(self._process_file_worker, fd, mode, st): (fd, st, est)
                for fd, st, est in files_to_process
            }
            
            for future in concurrent.futures.as_completed(future_to_file):
                file_digest, strategy, estimated = future_to_file[future]
                try:
                    success = future.result()
                    if success:
                        processed_files.append((file_digest, strategy, estimated))
                except Exception as e:
                    print(f"Warning: Error in parallel processing {file_digest.metadata.path}: {e}", 
                          file=sys.stderr)
        
        # 在主线程中统一分配Token和更新最终统计
        for file_digest, strategy, estimated in processed_files:
            if self.context_manager:
                file_record = {
                    "path": str(file_digest.metadata.path),
                    "strategy": strategy.value,
                    "estimated_tokens": estimated,
                    "size": file_digest.metadata.size,
                }
                self.context_manager.allocate(estimated, file_record)
        
        # 清理锁
        self._stats_lock = None
    
    def _process_file_worker(self, file_digest: FileDigest, mode: str, strategy: ProcessingStrategy):
        """并行处理工作函数（处理单个文件内容）"""
        try:
            filepath = file_digest.metadata.path
            
            # 检查大小（虽然已经在主线程检查过，但这里作为二次确认）
            if file_digest.metadata.size > self.max_file_size:
                self._process_as_binary(file_digest, mode)
                self._update_stats('skipped_large_files')
                self._update_stats('binary_files')
                return False
            
            # 获取处理器
            processor = self.get_processor(file_digest)
            
            if processor and strategy != ProcessingStrategy.METADATA_ONLY:
                content = self._read_file_content(filepath)
                if content:
                    # 调用处理器处理，处理器会根据策略决定是否设置完整内容和摘要
                    processor.process(file_digest, content, mode, strategy)
                    self._update_stats_by_processor(file_digest, processor)
                    return True
                else:
                    self._process_as_binary(file_digest, mode)
                    self._update_stats('binary_files')
                    return False
            else:
                self._process_as_binary(file_digest, mode)
                self._update_stats('binary_files')
                return False
                
        except Exception as e:
            import sys
            print(f"Warning: Worker error for {file_digest.metadata.path}: {e}", file=sys.stderr)
            return False


# ==================== 公共 API ====================

def create_default_registry(rule_engine=None, 
                           context_manager=None, 
                           stats=None, 
                           config=None) -> FileProcessorRegistry:
    """
    创建默认处理器注册表
    
    Args:
        rule_engine: 规则引擎实例
        context_manager: 上下文管理器实例  
        stats: 统计信息字典
        config: 配置字典
        
    Returns:
        FileProcessorRegistry: 配置好的注册表实例
    """
    registry = FileProcessorRegistry(
        rule_engine=rule_engine,
        context_manager=context_manager,
        stats=stats,
        config=config
    )
    
    # 按优先级顺序注册（先注册的优先级高）
    # DataFileProcessor 应该在 TextFileProcessor 之前，以正确处理数据文件
    registry.register(DataFileProcessor(config))  # 增强的数据文件处理器
    registry.register(TextFileProcessor(config))
    registry.register(SourceCodeProcessor(config))
    registry.register(ConfigFileProcessor(config))
    
    return registry


__all__ = [
    # 基类
    'BaseFileProcessor',
    
    # 具体处理器
    'TextFileProcessor',
    'SourceCodeProcessor',
    'ConfigFileProcessor',
    'DataFileProcessor',
    
    # 注册表
    'FileProcessorRegistry',
    'create_default_registry',
]
