"""
语义分析器基类 - 定义高级内容分析接口
包括：
- 源代码分析（SourceCodeAnalyzer）
- 文档分析（DocumentAnalyzer）
- 配置文件分析（ConfigAnalyzer）
- 结构化数据分析（DataSheetAnalyzer）
- 代码复杂度分析（ComplexityAnalyzer）
- 智能文本处理（SmartTextProcessor）
"""

import abc
import re
import ast
import json
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
from collections import Counter

from ..base import BaseAnalyzer, AnalysisResult
from ...base import YAML_AVAILABLE, CHARDET_AVAILABLE

if YAML_AVAILABLE:
    import yaml


# ==================== 语义分析结果基类 ====================

@dataclass
class SemanticAnalysisResult(AnalysisResult):
    """语义分析结果基类"""
    content_type: Optional[str] = None  # 'source_code', 'document', 'config', 'data_sheet'
    language: Optional[str] = None
    summary: Optional[str] = None
    keywords: List[str] = field(default_factory=list)
    
    def __post_init__(self):
        super().__post_init__()
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        result = {
            "success": self.success,
            "error_message": self.error_message,
            "metadata": self.metadata,
            "content_type": self.content_type,
            "language": self.language,
            "summary": self.summary,
            "keywords": self.keywords
        }
        return result


# ==================== 文本摘要数据类 ====================

@dataclass
class HumanReadableSummary:
    """人类可读文本摘要"""
    title: Optional[str] = None
    line_count: int = 0
    word_count: int = 0
    character_count: int = 0
    language: Optional[str] = None
    encoding: Optional[str] = None
    first_lines: List[str] = field(default_factory=list)
    last_lines: List[str] = field(default_factory=list)
    key_sections: List[Tuple[str, str]] = field(default_factory=list)
    summary: Optional[str] = None
    reading_time_minutes: float = 0.0
    reading_level: Optional[str] = None
    key_topics: List[str] = field(default_factory=list)
    sentiment_score: Optional[float] = None
    
    def to_dict(self) -> Dict:
        return {
            "title": self.title,
            "line_count": self.line_count,
            "word_count": self.word_count,
            "character_count": self.character_count,
            "language": self.language,
            "encoding": self.encoding,
            "first_lines": self.first_lines,
            "last_lines": self.last_lines,
            "key_sections": [{"title": t, "content": c[:200]} for t, c in self.key_sections],
            "summary": self.summary,
            "reading_time_minutes": self.reading_time_minutes,
            "reading_level": self.reading_level,
            "key_topics": self.key_topics[:10],
            "sentiment_score": self.sentiment_score
        }


@dataclass
class SourceCodeAnalysis:
    """源代码分析结果"""
    language: str
    total_lines: int
    code_lines: int
    comment_lines: int
    blank_lines: int
    imports: List[str] = field(default_factory=list)
    functions: List[Dict] = field(default_factory=list)
    classes: List[Dict] = field(default_factory=list)
    global_vars: List[str] = field(default_factory=list)
    constants: List[str] = field(default_factory=list)
    dependencies: List[str] = field(default_factory=list)
    complexity_metrics: Dict[str, Any] = field(default_factory=dict)
    style_issues: List[Dict] = field(default_factory=list)
    security_issues: List[Dict] = field(default_factory=list)
    test_coverage: Optional[float] = None
    
    def to_dict(self) -> Dict:
        return {
            "language": self.language,
            "total_lines": self.total_lines,
            "code_lines": self.code_lines,
            "comment_lines": self.comment_lines,
            "blank_lines": self.blank_lines,
            "imports": self.imports,
            "functions": self.functions[:20],
            "classes": self.classes[:20],
            "global_vars": self.global_vars[:20],
            "constants": self.constants[:20],
            "dependencies": self.dependencies[:20],
            "complexity_metrics": self.complexity_metrics,
            "style_issues": self.style_issues[:10],
            "security_issues": self.security_issues[:10],
            "test_coverage": self.test_coverage
        }


# ==================== 语义分析器基类 ====================

class SemanticAnalyzer(BaseAnalyzer, abc.ABC):
    """语义分析器基类"""
    
    @abc.abstractmethod
    def get_content_type(self) -> str:
        """获取处理的内容类型"""
        pass
    
    def _extract_keywords(self, content: str, max_count: int = 10) -> List[str]:
        """提取关键词（简化版）"""
        words = re.findall(r'\b[\w\u4e00-\u9fff]{3,}\b', content.lower())
        stop_words = {'the', 'and', 'for', 'with', 'that', 'this', 'from', 'have', 'has', 'had',
                     '的', '了', '在', '是', '我', '有', '和', '就', '不', '人', '都', '一', '一个'}
        filtered = [w for w in words if w not in stop_words]
        counter = Counter(filtered)
        return [word for word, _ in counter.most_common(max_count)]


# ==================== 源代码分析器基类 ====================

class BaseSourceCodeAnalyzer(SemanticAnalyzer):
    """源代码分析器基类"""
    
    def get_content_type(self) -> str:
        return "source_code"
    
    def can_handle(self, filepath: Path, content: Optional[str] = None) -> bool:
        """判断是否为源代码文件（基于扩展名）"""
        code_extensions = {
            '.py', '.java', '.cpp', '.c', '.h', '.hpp', '.cc',
            '.js', '.ts', '.jsx', '.tsx',
            '.go', '.rs', '.rb', '.php', '.swift',
            '.sh', '.bash', '.ps1', '.bat', '.cmd',
            '.sql', '.r', '.m', '.scala', '.kt'
        }
        return filepath.suffix.lower() in code_extensions
    
    def _analyze_line_counts(self, content: str, comment_prefix: str = '#') -> Tuple[int, int, int]:
        """分析代码行数统计"""
        lines = content.split('\n')
        total_lines = len(lines)
        blank_lines = 0
        comment_lines = 0
        
        for line in lines:
            stripped = line.strip()
            if not stripped:
                blank_lines += 1
            elif stripped.startswith(comment_prefix):
                comment_lines += 1
        
        code_lines = total_lines - blank_lines - comment_lines
        return total_lines, code_lines, comment_lines, blank_lines


# ==================== 文档分析器基类 ====================

class BaseDocumentAnalyzer(SemanticAnalyzer):
    """文档分析器基类"""
    
    def get_content_type(self) -> str:
        return "document"
    
    def can_handle(self, filepath: Path, content: Optional[str] = None) -> bool:
        """判断是否为文档文件"""
        doc_extensions = {'.md', '.markdown', '.rst', '.txt', '.html', '.htm', '.tex'}
        return filepath.suffix.lower() in doc_extensions
    
    def _extract_title(self, filepath: Path, content: str, lines: List[str]) -> Optional[str]:
        """提取文档标题"""
        # 1. 从文件名提取
        filename = filepath.stem
        if filename and filename != filepath.name:
            cleaned = filename.replace('_', ' ').replace('-', ' ').title()
            if 3 < len(cleaned) < 50:
                return cleaned
        
        # 2. 从内容中提取
        title_patterns = [
            (r'^#\s+(.+)$', 1),
            (r'^##\s+(.+)$', 1),
            (r'^(.+)\n=+$', 1),
            (r'^(.+)\n-+$', 1),
        ]
        
        for i in range(min(10, len(lines))):
            line = lines[i].strip()
            if not line:
                continue
            
            for pattern, group_idx in title_patterns:
                match = re.match(pattern, line)
                if match:
                    title_candidate = match.group(group_idx).strip()
                    if 3 <= len(title_candidate) <= 100:
                        return title_candidate
        
        # 3. 使用第一行非空行
        for line in lines:
            line = line.strip()
            if line and len(line) < 80:
                return line[:80]
        
        return None
    
    def _analyze_text_metrics(self, content: str) -> Dict[str, Any]:
        """分析文本指标"""
        words = re.findall(r'\b[\w\u4e00-\u9fff]+\b', content)
        word_count = len(words)
        reading_time_minutes = word_count / 200.0
        
        sentences = re.split(r'[.!?。！？]+', content)
        sentences = [s for s in sentences if s.strip()]
        avg_sentence_length = word_count / len(sentences) if sentences else 0
        
        if avg_sentence_length < 15:
            reading_level = "容易"
        elif avg_sentence_length < 25:
            reading_level = "中等"
        else:
            reading_level = "困难"
        
        return {
            "word_count": word_count,
            "reading_time_minutes": reading_time_minutes,
            "reading_level": reading_level,
            "sentence_count": len(sentences),
            "avg_sentence_length": avg_sentence_length
        }


# ==================== 配置文件分析器基类 ====================

class BaseConfigAnalyzer(SemanticAnalyzer):
    """配置文件分析器基类"""
    
    def get_content_type(self) -> str:
        return "config"
    
    def can_handle(self, filepath: Path, content: Optional[str] = None) -> bool:
        """判断是否为配置文件"""
        config_extensions = {
            '.yaml', '.yml', '.json', '.toml', 
            '.ini', '.cfg', '.conf', '.env',
            '.properties', '.rc', '.xml'
        }
        return filepath.suffix.lower() in config_extensions
    
    def _extract_config_structure(self, content: str, suffix: str) -> List[Dict]:
        """提取配置结构"""
        keys = []
        try:
            if suffix in ['.yaml', '.yml'] and YAML_AVAILABLE:
                data = yaml.safe_load(content)
                if isinstance(data, dict):
                    for i, key in enumerate(list(data.keys())[:20]):
                        keys.append({"name": f"key:{key}", "line": i+1, "type": "config_key"})
            elif suffix == '.json':
                data = json.loads(content)
                if isinstance(data, dict):
                    for i, key in enumerate(list(data.keys())[:20]):
                        keys.append({"name": f"json:{key}", "line": i+1, "type": "config_key"})
            elif suffix in ['.ini', '.cfg', '.conf', '.env', '.rc']:
                for i, line in enumerate(content.split('\n')[:50]):
                    line = line.strip()
                    if line and not line.startswith('#') and not line.startswith(';'):
                        match = re.match(r'^\s*([^=;#\s]+)\s*=', line)
                        if match:
                            keys.append({"name": match.group(1).strip(), "line": i+1, "type": "config_key"})
        except Exception:
            pass
        return keys


# ==================== 数据分析器基类 ====================

class BaseDataSheetAnalyzer(SemanticAnalyzer):
    """数据表分析器基类"""
    
    def get_content_type(self) -> str:
        return "data_sheet"
    
    def can_handle(self, filepath: Path, content: Optional[str] = None) -> bool:
        """判断是否为数据文件"""
        data_extensions = {
            '.csv', '.tsv', '.dat', '.data',
            '.log', '.out', '.err',
            '.tf', '.tls', '.tpc', '.ker', '.cmt'
        }
        return filepath.suffix.lower() in data_extensions
    
    def _extract_table_header(self, content: str, max_lines: int = 20) -> str:
        """提取表格头部"""
        lines = content.split('\n')
        header_lines = []
        
        for line in lines:
            stripped = line.strip()
            
            if not stripped and header_lines:
                break
            
            if stripped.startswith(('#', '//', '/*', '*', 'C', 'CC', '!')):
                header_lines.append(line)
                continue
            
            if re.match(r'^[a-zA-Z_][a-zA-Z0-9_\s,;:|]*$', stripped) and len(stripped) < 200:
                header_lines.append(line)
                continue
            
            if re.match(r'^[\d\.,;\s\t|]+$', stripped) and len(stripped) > 10:
                break
            
            header_lines.append(line)
            
            if len(header_lines) >= max_lines:
                break
        
        return '\n'.join(header_lines)


# ==================== 代码复杂度分析器 ====================

class ComplexityAnalyzer:
    """代码复杂度分析器"""
    
    @staticmethod
    def analyze_python(content: str) -> Dict[str, Any]:
        """分析Python代码复杂度"""
        try:
            tree = ast.parse(content)
            
            metrics = {
                "cyclomatic_complexity": 0,
                "function_count": 0,
                "class_count": 0,
                "average_function_length": 0,
                "max_nesting_depth": 0,
                "import_count": 0
            }
            
            def analyze_node(node, depth: int = 0):
                nonlocal metrics
                metrics["max_nesting_depth"] = max(metrics["max_nesting_depth"], depth)
                
                if isinstance(node, (ast.If, ast.While, ast.For, ast.Try)):
                    metrics["cyclomatic_complexity"] += 1
                    for child in ast.iter_child_nodes(node):
                        analyze_node(child, depth + 1)
                elif isinstance(node, ast.FunctionDef):
                    metrics["function_count"] += 1
                    stmt_count = len([n for n in ast.walk(node) if isinstance(n, ast.stmt)])
                    metrics["average_function_length"] += stmt_count
                elif isinstance(node, ast.ClassDef):
                    metrics["class_count"] += 1
                elif isinstance(node, (ast.Import, ast.ImportFrom)):
                    metrics["import_count"] += 1
                else:
                    for child in ast.iter_child_nodes(node):
                        analyze_node(child, depth)
            
            analyze_node(tree)
            
            if metrics["function_count"] > 0:
                metrics["average_function_length"] /= metrics["function_count"]
            
            complexity_score = metrics["cyclomatic_complexity"]
            if complexity_score < 10:
                metrics["complexity_level"] = "简单"
            elif complexity_score < 20:
                metrics["complexity_level"] = "中等"
            elif complexity_score < 30:
                metrics["complexity_level"] = "复杂"
            else:
                metrics["complexity_level"] = "非常复杂"
            
            return metrics
            
        except SyntaxError:
            return {
                "cyclomatic_complexity": 0,
                "function_count": 0,
                "class_count": 0,
                "average_function_length": 0,
                "max_nesting_depth": 0,
                "import_count": 0,
                "complexity_level": "无法分析"
            }
    
    @staticmethod
    def analyze_generic(content: str) -> Dict[str, Any]:
        """通用代码复杂度分析"""
        lines = content.split('\n')
        
        metrics = {
            "line_count": len(lines),
            "estimated_complexity": 0
        }
        
        complexity_patterns = [
            (r'\bif\b', 1),
            (r'\belse\b', 1),
            (r'\bfor\b', 2),
            (r'\bwhile\b', 2),
            (r'\btry\b', 1),
            (r'\bcatch\b', 1),
            (r'\bswitch\b', 2),
            (r'\bcase\b', 1)
        ]
        
        for line in lines:
            line_lower = line.lower()
            for pattern, weight in complexity_patterns:
                if re.search(pattern, line_lower):
                    metrics["estimated_complexity"] += weight
        
        if metrics["estimated_complexity"] < 10:
            metrics["complexity_level"] = "简单"
        elif metrics["estimated_complexity"] < 30:
            metrics["complexity_level"] = "中等"
        elif metrics["estimated_complexity"] < 50:
            metrics["complexity_level"] = "复杂"
        else:
            metrics["complexity_level"] = "非常复杂"
        
        return metrics


# ==================== 智能文本处理器 ====================

class SmartTextProcessor:
    """智能文本处理器 - 提取人类关心的内容，截断机器数据结构"""
    
    STRUCTURED_EXTENSIONS = {'.tf', '.tls', '.tpc', '.ker', '.csv', '.dat', '.xml', '.cmt'}
    
    @staticmethod
    def is_structured_data_file(filepath: Path) -> bool:
        """判断是否为结构化数据文件"""
        return filepath.suffix.lower() in SmartTextProcessor.STRUCTURED_EXTENSIONS
    
    @staticmethod
    def extract_human_relevant_content(content: str, filepath: Path, max_human_lines: int = 50) -> str:
        """从面向机器的文件中提取人类可读部分"""
        lines = content.split('\n')
        suffix = filepath.suffix.lower()
        
        if suffix in ['.tf', '.tls', '.tpc', '.ker']:
            return SmartTextProcessor._extract_spice_content(lines, filepath, content)
        elif suffix == '.csv':
            return SmartTextProcessor._extract_csv_content(lines, filepath)
        elif suffix == '.cmt':
            return SmartTextProcessor._extract_cmt_content(lines, filepath)
        else:
            return '\n'.join(lines[:max_human_lines])
    
    @staticmethod
    def _extract_spice_content(lines: List[str], filepath: Path, full_content: str) -> str:
        """提取SPICE文件的人类可读部分"""
        header_lines = []
        in_text_block = False
        data_started = False
        
        for line in lines:
            stripped = line.strip()
            
            if '\\begintext' in stripped:
                in_text_block = True
                header_lines.append(line)
                continue
            elif '\\begindata' in stripped:
                data_started = True
                header_lines.append(line)
                break
            
            if (stripped.startswith('C') or stripped.startswith('*') or 
                stripped.startswith('/*') or stripped.startswith('#') or
                stripped.startswith('CC') or in_text_block):
                header_lines.append(line)
            elif not data_started and not stripped.startswith('\\'):
                if not re.match(r'^\s*[\d\.\-\+eE\s]+$', stripped):
                    header_lines.append(line)
        
        result_lines = header_lines[:50]
        
        if data_started or len(header_lines) < len(lines):
            result_lines.append(f"\n[DATA SECTION TRUNCATED]")
            result_lines.append(f"File type: SPICE Kernel ({filepath.suffix})")
            result_lines.append(f"Total lines: {len(lines)}")
        
        return '\n'.join(result_lines)
    
    @staticmethod
    def _extract_csv_content(lines: List[str], filepath: Path) -> str:
        """提取CSV文件的头部和统计信息"""
        header_lines = []
        
        if lines:
            header_lines.append(lines[0])
            header_lines.append("")
        
        sample_count = 0
        for line in lines[1:]:
            if line.strip() and sample_count < 5:
                header_lines.append(line)
                sample_count += 1
            elif sample_count >= 5:
                break
        
        total_rows = len([l for l in lines if l.strip()])
        header_lines.append(f"\n[CSV DATA SUMMARY]")
        header_lines.append(f"Total rows: ~{total_rows}")
        
        return '\n'.join(header_lines)
    
    @staticmethod
    def _extract_cmt_content(lines: List[str], filepath: Path) -> str:
        """提取.cmt文件的人类可读部分"""
        header_lines = []
        
        for line in lines[:100]:
            stripped = line.strip()
            if stripped:
                header_lines.append(line)
        
        if len(lines) > len(header_lines):
            header_lines.append(f"\n[FILE SUMMARY]")
            header_lines.append(f"Total lines: {len(lines)}")
            header_lines.append(f"Preserved: {len(header_lines)} lines")
        
        return '\n'.join(header_lines[:100])


# ==================== 内容分析器 ====================

class ContentAnalyzer:
    """基于内容特征的动态分类器"""
    
    @staticmethod
    def calculate_entropy(content: str) -> float:
        """计算香农熵"""
        if not content:
            return 0.0
        import math
        
        char_counts = Counter(content)
        total = len(content)
        entropy = 0.0
        
        for count in char_counts.values():
            p = count / total
            entropy -= p * math.log2(p) if p > 0 else 0
        
        return entropy
    
    @staticmethod
    def detect_structure(content: str) -> Dict[str, Any]:
        """检测内容结构特征"""
        lines = content.split('\n')
        total_lines = len(lines)
        if total_lines == 0:
            return {'is_tabular': False, 'is_natural_language': False, 'is_code': False,
                   'structure_type': 'empty', 'entropy': 0}
        
        sample = content[:10000]
        sample_lines = lines[:min(100, total_lines)]
        
        # Tabular Data 检测
        delimiter_pattern = re.compile(r'[,;\t|]{2,}')
        numeric_pattern = re.compile(r'^[\s\d\.\-\+eE,;\t|]+$')
        delimiter_lines = 0
        numeric_lines = 0
        
        for line in sample_lines:
            stripped = line.strip()
            if not stripped:
                continue
            if delimiter_pattern.search(stripped) or stripped.count(',') > 3:
                delimiter_lines += 1
            if numeric_pattern.match(stripped) and len(stripped) > 5:
                numeric_lines += 1
        
        tabular_ratio = (delimiter_lines + numeric_lines) / max(len(sample_lines), 1)
        is_tabular = tabular_ratio > 0.3
        
        # Natural Language 检测
        words = re.findall(r'\b[a-zA-Z]{2,}\b', sample.lower())
        chinese_chars = len(re.findall(r'[\u4e00-\u9fff]', sample))
        
        word_diversity = 0
        stop_words_found = 0
        if words:
            unique = len(set(words))
            word_diversity = unique / len(words)
            stop_words = {'the', 'and', 'is', 'of', 'to', 'in', 'that', 'have', 'it'}
            stop_words_found = sum(1 for w in words if w in stop_words)
        
        sentence_endings = sample.count('.') + sample.count('?') + sample.count('!') + \
                          sample.count('。') + sample.count('？') + sample.count('！')
        
        is_natural_language = (
            (sentence_endings > 3 and word_diversity > 0.1 and stop_words_found > 5) or
            (chinese_chars > 50 and sentence_endings > 0)
        )
        
        # Code 检测
        code_patterns = [
            r'\b(def|class|function|if|for|while|return|import|from|#include)\b',
            r'[{}\[\]()]+',
            r'^(\s{4}|\t)',
        ]
        code_matches = sum(1 for p in code_patterns if re.search(p, sample[:2000], re.M))
        is_code = code_matches > 3
        
        # Config 检测
        config_patterns = [
            r'^[a-zA-Z_][a-zA-Z0-9_]*\s*[=:]\s*.+$',
            r'^[^:]+:\s+.+$',
        ]
        config_lines = sum(1 for line in sample_lines if any(re.match(p, line.strip()) for p in config_patterns))
        is_config = (config_lines / max(len(sample_lines), 1)) > 0.3 and not is_code
        
        if is_tabular and is_natural_language:
            structure_type = 'mixed'
        elif is_tabular:
            structure_type = 'tabular_data'
        elif is_code:
            structure_type = 'code'
        elif is_config:
            structure_type = 'config'
        elif is_natural_language:
            structure_type = 'natural_language'
        else:
            structure_type = 'unknown'
        
        return {
            'is_tabular': is_tabular,
            'is_natural_language': is_natural_language,
            'is_code': is_code,
            'tabular_ratio': tabular_ratio,
            'structure_type': structure_type,
            'entropy': ContentAnalyzer.calculate_entropy(content)
        }


# ==================== 公共 API 导出 ====================

__all__ = [
    'SemanticAnalysisResult',
    'HumanReadableSummary',
    'SourceCodeAnalysis',
    'SemanticAnalyzer',
    'BaseSourceCodeAnalyzer',
    'BaseDocumentAnalyzer',
    'BaseConfigAnalyzer',
    'BaseDataSheetAnalyzer',
    'ComplexityAnalyzer',
    'SmartTextProcessor',
    'ContentAnalyzer',
]
