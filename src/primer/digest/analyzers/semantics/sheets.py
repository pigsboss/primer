"""
文本数据和配置文件分析器 - 专门处理结构化文本数据
包括：
- 配置文件分析（key-value 结构提取）
- CSV/TSV 表格数据分析
- 结构化数据文件分析（SPICE, XML, JSON等）
- 表单规模和表头提取
- 数据统计和采样
"""

import re
import json
import csv
import io
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
from collections import Counter
import sys

from .base import (
    BaseDataSheetAnalyzer,
    BaseConfigAnalyzer,
    SemanticAnalysisResult,
    SmartTextProcessor,
    ContentAnalyzer,
)
from ...base import YAML_AVAILABLE, CHARDET_AVAILABLE

if YAML_AVAILABLE:
    import yaml


# ==================== 智能文本数据文件分析器 ====================

class SmartDataFileAnalyzer(BaseDataSheetAnalyzer):
    """智能文本数据文件分析器 - 专门处理缺乏标准定义的数据文件"""
    
    def __init__(self, debug: bool = False):
        super().__init__()
        self.debug = debug
    
    def can_handle(self, filepath: Path, content: Optional[str] = None) -> bool:
        """判断是否为需要智能分析的数据文件"""
        suffix = filepath.suffix.lower()
        
        # 识别缺乏标准定义的数据文件
        unstructured_data_extensions = {
            '.tls', '.tpc', '.ker', '.cmt', '.tm',  # SPICE内核文件
            '.dat', '.data', '.raw',                 # 通用数据文件
            '.out', '.log', '.err',                  # 输出/日志文件
            '.aux', '.tmp', '.temp',                 # 临时/辅助文件
        }
        
        # 检查是否是已知的无标准定义文件
        if suffix in unstructured_data_extensions:
            return True
        
        # 通过文件名模式检测
        filename = filepath.name.lower()
        unstructured_patterns = [
            'kernel', 'spk', 'bsp', 'pck', 'ik', 'fk',  # SPICE相关
            'ephemeris', 'trajectory', 'orbit',         # 轨道相关
            'telemetry', 'telecommand', 'tlm',          # 遥测相关
            'output', 'result', 'dump',                 # 输出文件
            'backup', 'archive', 'snapshot',            # 备份文件
        ]
        
        if any(pattern in filename for pattern in unstructured_patterns):
            return True
        
        # 通过内容检测（如果提供了内容）
        if content:
            structure_info = ContentAnalyzer.detect_structure(content)
            if structure_info['structure_type'] in ['tabular_data', 'mixed', 'unknown']:
                # 非自然语言、非代码、非配置文件的文本数据
                return True
        
        return False
    
    def analyze(self, filepath: Path, content: Optional[str] = None) -> SemanticAnalysisResult:
        """
        智能分析文本数据文件
        
        Args:
            filepath: 文件路径
            content: 文件内容
            
        Returns:
            SemanticAnalysisResult: 分析结果
        """
        result = SemanticAnalysisResult(
            content_type="data_sheet",
            language="unstructured_data"
        )
        
        try:
            if content is None:
                content = self._read_file_content(filepath)
            
            if content is None:
                result.success = False
                result.error_message = "Could not read file content"
                return result
            
            # 分析文件结构和内容
            analysis = self._analyze_unstructured_data(filepath, content)
            
            # 生成摘要
            result.summary = self._generate_smart_summary(analysis, filepath)
            result.keywords = self._extract_keywords_from_content(content)
            result.metadata["data_analysis"] = analysis
            result.metadata["sample_content"] = self._extract_sample_content(content, analysis)
            
            result.success = True
            
        except Exception as e:
            result.success = False
            result.error_message = f"Smart data analysis failed: {str(e)}"
            if self.debug:
                print(f"[DEBUG:SmartDataFileAnalyzer] Error analyzing {filepath}: {e}", file=sys.stderr)
        
        return result
    
    def _read_file_content(self, filepath: Path) -> Optional[str]:
        """读取文件内容"""
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                return f.read()
        except UnicodeDecodeError:
            try:
                with open(filepath, 'r', encoding='latin-1') as f:
                    return f.read()
            except Exception:
                return None
    
    def _analyze_unstructured_data(self, filepath: Path, content: str) -> Dict[str, Any]:
        """分析非结构化数据文件"""
        lines = content.split('\n')
        total_lines = len(lines)
        
        # 检测文件类型和结构
        file_type = self._detect_file_type(filepath, content)
        structure_info = ContentAnalyzer.detect_structure(content)
        
        # 分析内容分区
        partitions = self._partition_content(lines, file_type)
        
        # 统计信息
        stats = {
            "total_lines": total_lines,
            "total_characters": len(content),
            "non_empty_lines": sum(1 for l in lines if l.strip()),
            "estimated_records": 0,
            "estimated_columns": 0,
            "comment_ratio": partitions.get("comment_ratio", 0.0),
            "data_ratio": partitions.get("data_ratio", 0.0),
            "text_ratio": partitions.get("text_ratio", 0.0),
        }
        
        # 提取头部/注释区
        header_lines = partitions.get("header_lines", [])
        if header_lines:
            stats["header_lines"] = len(header_lines)
            stats["header_sample"] = '\n'.join(header_lines[:20])
        
        # 提取数据区特征
        if "data_section_start" in partitions:
            data_start = partitions["data_section_start"]
            data_end = min(data_start + 50, total_lines)  # 分析前50行数据
            data_lines = lines[data_start:data_end]
            
            if data_lines:
                data_stats = self._analyze_data_section(data_lines)
                stats.update(data_stats)
                stats["estimated_records"] = self._estimate_total_records(lines, data_start)
        
        return {
            "file_type": file_type,
            "structure_type": structure_info['structure_type'],
            "structure_info": structure_info,
            "partitions": partitions,
            "stats": stats,
            "metadata": {
                "filename": filepath.name,
                "extension": filepath.suffix,
                "path": str(filepath)
            }
        }
    
    def _detect_file_type(self, filepath: Path, content: str) -> str:
        """检测具体的文件类型"""
        suffix = filepath.suffix.lower()
        filename = filepath.name.lower()
        lines = content.split('\n')
        
        # 特定文件类型检测
        if suffix == '.tls':
            # SPICE leapseconds kernel
            if any('LEAPSECONDS KERNEL' in line for line in lines[:10]):
                return 'SPICE_LEAPSECONDS_KERNEL'
            return 'SPICE_TIME_KERNEL'
        
        elif suffix == '.tpc':
            # SPICE text kernel
            if any('TEXT KERNEL' in line for line in lines[:10]):
                return 'SPICE_TEXT_KERNEL'
            return 'SPICE_PLANETARY_KERNEL'
        
        elif suffix == '.ker':
            # Generic SPICE kernel
            if any('KPL/' in line for line in lines[:10]) or any('Kernel Pool' in line for line in lines[:10]):
                return 'SPICE_GENERIC_KERNEL'
            return 'BINARY_KERNEL'
        
        elif suffix == '.cmt':
            # Comments file
            return 'COMMENTS_FILE'
        
        elif suffix in ['.dat', '.data']:
            # Generic data file
            if any('=' in line and not line.strip().startswith('#') for line in lines[:20]):
                return 'KEY_VALUE_DATA'
            elif any(',' in line or '\t' in line for line in lines[:20]):
                return 'TABULAR_DATA'
            else:
                return 'RAW_DATA'
        
        # 基于内容检测
        content_lower = content[:1000].lower()
        
        if any(keyword in content_lower for keyword in ['spice', 'kernel', 'naif', 'leapseconds']):
            return 'SPICE_RELATED'
        
        # 通过常见模式检测
        has_comments = any(line.strip().startswith('#') for line in lines[:20])
        has_numbers = any(re.search(r'\b\d+\.?\d*\b', line) for line in lines[:20])
        
        if has_comments and has_numbers:
            return 'COMMENTED_DATA'
        elif has_numbers and not has_comments:
            return 'NUMERIC_DATA'
        else:
            return 'TEXT_DATA'
    
    def _partition_content(self, lines: List[str], file_type: str) -> Dict[str, Any]:
        """分区内容：头部、注释、数据、尾部"""
        total_lines = len(lines)
        partitions = {
            "header_lines": [],
            "comment_lines": [],
            "data_section_start": None,
            "data_section_end": None,
            "footer_lines": [],
            "comment_ratio": 0.0,
            "data_ratio": 0.0,
            "text_ratio": 0.0,
        }
        
        if not lines:
            return partitions
        
        # SPICE内核文件的特殊处理
        if file_type.startswith('SPICE_'):
            return self._partition_spice_content(lines, file_type)
        
        # 通用文件分区
        in_comment_block = False
        data_started = False
        
        for i, line in enumerate(lines):
            stripped = line.strip()
            
            # 注释行检测
            if stripped.startswith(('#', '//', '/*', '!', '*', 'C ', 'CC ')):
                if not data_started:
                    partitions["header_lines"].append(line)
                partitions["comment_lines"].append(line)
                continue
            
            # 块注释开始/结束
            if '/*' in line and not in_comment_block:
                in_comment_block = True
                if not data_started:
                    partitions["header_lines"].append(line)
                partitions["comment_lines"].append(line)
                continue
            if '*/' in line and in_comment_block:
                in_comment_block = False
                if not data_started:
                    partitions["header_lines"].append(line)
                partitions["comment_lines"].append(line)
                continue
            if in_comment_block:
                if not data_started:
                    partitions["header_lines"].append(line)
                partitions["comment_lines"].append(line)
                continue
            
            # 数据区开始检测
            if not data_started and stripped:
                # 检测是否看起来像数据行
                if self._looks_like_data_line(stripped):
                    partitions["data_section_start"] = i
                    data_started = True
                elif not data_started:
                    # 仍处于头部区域
                    partitions["header_lines"].append(line)
        
        # 计算比例
        if partitions["data_section_start"] is not None:
            data_lines = total_lines - partitions["data_section_start"]
            partitions["data_ratio"] = data_lines / total_lines
            partitions["comment_ratio"] = len(partitions["comment_lines"]) / total_lines
            partitions["text_ratio"] = 1.0 - partitions["data_ratio"] - partitions["comment_ratio"]
        
        return partitions
    
    def _partition_spice_content(self, lines: List[str], file_type: str) -> Dict[str, Any]:
        """分区SPICE内核文件内容"""
        partitions = {
            "header_lines": [],
            "comment_lines": [],
            "data_section_start": None,
            "data_section_end": None,
            "meta_sections": [],
            "comment_ratio": 0.0,
            "data_ratio": 0.0,
            "text_ratio": 0.0,
        }
        
        current_section = "header"
        sections = []
        
        for i, line in enumerate(lines):
            stripped = line.strip()
            
            # SPICE特定标记
            if stripped.startswith('\\begindata'):
                partitions["data_section_start"] = i + 1
                current_section = "data"
                sections.append({"type": "data", "start": i + 1})
                partitions["header_lines"].append(line)  # 包含标记行
                continue
            elif stripped.startswith('\\begintext'):
                if current_section == "data":
                    partitions["data_section_end"] = i
                    current_section = "text"
                    sections.append({"type": "text", "start": i + 1})
                partitions["header_lines"].append(line)
                continue
            
            # 根据当前区域处理
            if current_section == "header":
                partitions["header_lines"].append(line)
                if stripped.startswith('C') or stripped.startswith('*') or stripped.startswith('CC'):
                    partitions["comment_lines"].append(line)
            elif current_section == "data":
                # 数据区行
                pass
            elif current_section == "text":
                # 文本区行
                partitions["header_lines"].append(line)
                if stripped:
                    partitions["comment_lines"].append(line)
        
        # 统计
        total_lines = len(lines)
        partitions["meta_sections"] = sections
        
        if partitions["data_section_start"] is not None:
            if partitions["data_section_end"] is not None:
                data_lines = partitions["data_section_end"] - partitions["data_section_start"]
            else:
                data_lines = total_lines - partitions["data_section_start"]
            
            partitions["data_ratio"] = data_lines / total_lines
        
        partitions["comment_ratio"] = len(partitions["comment_lines"]) / total_lines
        partitions["text_ratio"] = 1.0 - partitions["data_ratio"] - partitions["comment_ratio"]
        
        return partitions
    
    def _looks_like_data_line(self, line: str) -> bool:
        """判断一行是否看起来像数据行"""
        if not line.strip():
            return False
        
        # 数据行常见模式
        patterns = [
            r'\b\d+\s*,\s*\d+',          # 数字,数字
            r'\b\d+\s+\d+',              # 数字 数字
            r'[A-Za-z_]+\s*=',           # 键=值
            r'\b\d+\.\d+',               # 浮点数
            r'\b\d+\s*@',                # 数字@（SPICE时间格式）
            r'^\s*[\d\.\-]+\s+[\d\.\-]+', # 以数字开始的两列
            r'^\s*\d+\s+[A-Za-z]+',      # 数字+文本
            r'^\s*[A-Z]+\s+\d+',         # 文本+数字
        ]
        
        for pattern in patterns:
            if re.search(pattern, line):
                return True
        
        # 字符组成分析
        alpha_count = sum(1 for c in line if c.isalpha())
        digit_count = sum(1 for c in line if c.isdigit())
        special_count = sum(1 for c in line if c in ',;|:\t=@()[]{}')
        
        total_chars = len(line)
        if total_chars == 0:
            return False
        
        # 如果数字和特殊字符占比较高，可能是数据
        if (digit_count + special_count) / total_chars > 0.4:
            return True
        
        return False
    
    def _analyze_data_section(self, data_lines: List[str]) -> Dict[str, Any]:
        """分析数据区特征"""
        if not data_lines:
            return {}
        
        # 统计信息
        stats = {
            "data_lines_analyzed": len(data_lines),
            "unique_patterns": 0,
            "max_columns": 0,
            "avg_line_length": 0,
            "numeric_content_ratio": 0.0,
        }
        
        # 分析行模式和列数
        line_lengths = []
        column_counts = []
        numeric_line_counts = 0
        
        patterns = []
        
        for line in data_lines:
            stripped = line.strip()
            if not stripped:
                continue
            
            line_lengths.append(len(stripped))
            
            # 估算列数（按常见分隔符）
            for delimiter in [',', '\t', ';', '|', ' ']:
                if delimiter in stripped:
                    columns = stripped.split(delimiter)
                    column_counts.append(len(columns))
                    break
            else:
                # 没有明显分隔符，按空格分割
                columns = stripped.split()
                column_counts.append(len(columns))
            
            # 检测是否主要为数字内容
            if re.search(r'\b\d+\.?\d*\b', stripped):
                numeric_line_counts += 1
            
            # 记录行模式（简化版本）
            simplified = re.sub(r'\d+', '#', stripped)
            simplified = re.sub(r'"[^"]*"', '"STRING"', simplified)
            if simplified not in patterns:
                patterns.append(simplified)
        
        # 计算统计
        if line_lengths:
            stats["avg_line_length"] = sum(line_lengths) / len(line_lengths)
            stats["max_line_length"] = max(line_lengths)
        
        if column_counts:
            stats["max_columns"] = max(column_counts)
            stats["avg_columns"] = sum(column_counts) / len(column_counts)
        
        if data_lines:
            stats["numeric_content_ratio"] = numeric_line_counts / len(data_lines)
        
        stats["unique_patterns"] = len(patterns)
        
        return stats
    
    def _estimate_total_records(self, lines: List[str], data_start: int) -> int:
        """估算总记录数"""
        if data_start >= len(lines):
            return 0
        
        # 分析前100行数据区的模式
        sample_size = min(100, len(lines) - data_start)
        if sample_size <= 0:
            return 0
        
        sample_lines = lines[data_start:data_start + sample_size]
        
        # 计算数据行数（排除空行和注释）
        data_lines_in_sample = 0
        for line in sample_lines:
            stripped = line.strip()
            if stripped and not stripped.startswith(('#', '//', '/*', '*', '!')):
                data_lines_in_sample += 1
        
        if data_lines_in_sample == 0:
            return 0
        
        # 估算总数据行数
        total_lines = len(lines)
        estimated_data_lines = (data_lines_in_sample / sample_size) * (total_lines - data_start)
        
        return int(estimated_data_lines)
    
    def _extract_keywords_from_content(self, content: str) -> List[str]:
        """从内容中提取关键词"""
        # 使用前1000个字符
        sample = content[:1000]
        
        # 提取单词（中英文）
        words = re.findall(r'\b[\w\u4e00-\u9fff]{3,}\b', sample.lower())
        
        # 过滤停用词
        stop_words = {'the', 'and', 'for', 'with', 'that', 'this', 'from', 
                     '的', '了', '在', '是', '我', '有', '和', '就'}
        filtered = [w for w in words if w not in stop_words]
        
        # 统计频率
        counter = Counter(filtered)
        return [word for word, _ in counter.most_common(10)]
    
    def _extract_sample_content(self, content: str, analysis: Dict[str, Any]) -> str:
        """提取样例内容（智能截断）"""
        lines = content.split('\n')
        partitions = analysis.get("partitions", {})
        
        # 优先使用头部区域
        header_lines = partitions.get("header_lines", [])
        if header_lines:
            sample = '\n'.join(header_lines[:50])  # 最多50行头部
            if len(header_lines) > 50:
                sample += f"\n... [省略 {len(header_lines) - 50} 行头部内容]"
            return sample
        
        # 否则使用前50行
        sample_lines = lines[:50]
        sample = '\n'.join(sample_lines)
        if len(lines) > 50:
            sample += f"\n... [省略 {len(lines) - 50} 行]"
        
        return sample
    
    def _generate_smart_summary(self, analysis: Dict[str, Any], filepath: Path) -> str:
        """生成智能摘要"""
        parts = []
        
        # 基本文件信息
        file_type = analysis.get("file_type", "unknown")
        stats = analysis.get("stats", {})
        partitions = analysis.get("partitions", {})
        
        parts.append(f"File: {filepath.name}")
        parts.append(f"Type: {file_type}")
        parts.append(f"Analysis: {analysis.get('structure_type', 'unknown')}")
        
        # 结构信息
        if partitions:
            if partitions.get("data_section_start") is not None:
                parts.append(f"Data section starts at line: {partitions['data_section_start']}")
            
            if "comment_ratio" in partitions:
                parts.append(f"Comment ratio: {partitions['comment_ratio']:.1%}")
            if "data_ratio" in partitions:
                parts.append(f"Data ratio: {partitions['data_ratio']:.1%}")
        
        # 统计信息
        if stats:
            parts.append(f"Total lines: {stats.get('total_lines', 0)}")
            parts.append(f"Non-empty lines: {stats.get('non_empty_lines', 0)}")
            
            if stats.get("estimated_records", 0) > 0:
                parts.append(f"Estimated records: ~{stats['estimated_records']}")
            
            if stats.get("estimated_columns", 0) > 0:
                parts.append(f"Estimated columns: {stats['estimated_columns']}")
            
            if stats.get("unique_patterns", 0) > 0:
                parts.append(f"Unique patterns: {stats['unique_patterns']}")
        
        # 根据文件类型添加特定信息
        if file_type.startswith('SPICE_'):
            parts.append("This appears to be a SPICE kernel file")
            parts.append("Structure: Header + Data sections")
        
        return '\n'.join(parts)


# ==================== 配置文件分析结果数据类 ====================

@dataclass
class ConfigAnalysisResult:
    """配置文件分析结果"""
    key_count: int = 0
    section_count: int = 0
    top_level_keys: List[str] = field(default_factory=list)
    sections: List[str] = field(default_factory=list)
    is_hierarchical: bool = False
    estimated_size: str = "unknown"
    sample_content: str = ""
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "key_count": self.key_count,
            "section_count": self.section_count,
            "top_level_keys": self.top_level_keys,
            "sections": self.sections,
            "is_hierarchical": self.is_hierarchical,
            "estimated_size": self.estimated_size,
            "sample_content": self.sample_content[:500]
        }


# ==================== 表格数据分析结果数据类 ====================

@dataclass
class TableAnalysisResult:
    """表格数据文件分析结果"""
    row_count: int = 0
    column_count: int = 0
    headers: List[str] = field(default_factory=list)
    delimiter: Optional[str] = None
    has_header: bool = False
    sample_rows: List[List[str]] = field(default_factory=list)
    column_types: Dict[str, str] = field(default_factory=dict)
    estimated_total_rows: Optional[int] = None
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "row_count": self.row_count,
            "column_count": self.column_count,
            "headers": self.headers,
            "delimiter": self.delimiter,
            "has_header": self.has_header,
            "sample_rows": self.sample_rows[:5],
            "column_types": self.column_types,
            "estimated_total_rows": self.estimated_total_rows
        }


# ==================== 配置文件分析器 ====================

class ConfigFileAnalyzer(BaseConfigAnalyzer):
    """配置文件分析器 - 提取 key-value 结构"""
    
    def __init__(self):
        super().__init__()
    
    def can_handle(self, filepath: Path, content: Optional[str] = None) -> bool:
        """判断是否为配置文件"""
        config_extensions = {
            '.yaml', '.yml', '.json', '.toml',
            '.ini', '.cfg', '.conf', '.env',
            '.properties', '.rc', '.xml'
        }
        return filepath.suffix.lower() in config_extensions
    
    def analyze(self, filepath: Path, content: Optional[str] = None) -> SemanticAnalysisResult:
        """
        分析配置文件
        
        Args:
            filepath: 文件路径
            content: 文件内容
            
        Returns:
            SemanticAnalysisResult: 分析结果
        """
        result = SemanticAnalysisResult(
            content_type="config",
            language="config"
        )
        
        try:
            if content is None:
                content = self._read_file_content(filepath)
            
            if content is None:
                result.success = False
                result.error_message = "Could not read file content"
                return result
            
            # 根据文件类型进行分析
            suffix = filepath.suffix.lower()
            
            if suffix in ['.yaml', '.yml']:
                config_result = self._analyze_yaml(content)
            elif suffix == '.json':
                config_result = self._analyze_json(content)
            elif suffix == '.toml':
                config_result = self._analyze_toml(content)
            elif suffix in ['.ini', '.cfg', '.conf']:
                config_result = self._analyze_ini(content)
            elif suffix in ['.env', '.properties', '.rc']:
                config_result = self._analyze_key_value(content)
            elif suffix == '.xml':
                config_result = self._analyze_xml(content)
            else:
                config_result = self._analyze_generic_config(content)
            
            # 生成摘要
            result.summary = self._generate_config_summary(config_result)
            result.keywords = config_result.top_level_keys[:10]
            result.metadata["config_analysis"] = config_result
            result.metadata["config_sample"] = self._extract_sample_content(content, suffix)
            
            result.success = True
            
        except Exception as e:
            result.success = False
            result.error_message = f"Config analysis failed: {str(e)}"
        
        return result
    
    def _read_file_content(self, filepath: Path) -> Optional[str]:
        """读取文件内容"""
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                return f.read()
        except UnicodeDecodeError:
            try:
                with open(filepath, 'r', encoding='latin-1') as f:
                    return f.read()
            except Exception:
                return None
    
    def _analyze_yaml(self, content: str) -> ConfigAnalysisResult:
        """分析 YAML 配置文件"""
        result = ConfigAnalysisResult()
        
        if not YAML_AVAILABLE:
            result.sample_content = "[YAML parser not available]"
            return result
        
        try:
            data = yaml.safe_load(content)
            
            if isinstance(data, dict):
                result.top_level_keys = list(data.keys())[:20]
                result.key_count = len(data)
                result.is_hierarchical = True
                result.section_count = self._count_nested_sections(data)
            
            result.sample_content = content[:500]
            
        except Exception as e:
            result.sample_content = f"[YAML parse error: {str(e)}]"
        
        return result
    
    def _analyze_json(self, content: str) -> ConfigAnalysisResult:
        """分析 JSON 配置文件"""
        result = ConfigAnalysisResult()
        
        try:
            data = json.loads(content)
            
            if isinstance(data, dict):
                result.top_level_keys = list(data.keys())[:20]
                result.key_count = len(data)
                result.is_hierarchical = True
                result.section_count = self._count_nested_sections(data)
            elif isinstance(data, list):
                result.key_count = len(data)
                result.is_hierarchical = False
            
            result.sample_content = content[:500]
            
        except Exception as e:
            result.sample_content = f"[JSON parse error: {str(e)}]"
        
        return result
    
    def _analyze_toml(self, content: str) -> ConfigAnalysisResult:
        """分析 TOML 配置文件"""
        result = ConfigAnalysisResult()
        
        # 简化处理 - 提取键值对和节
        lines = content.split('\n')
        sections = []
        keys = []
        
        for line in lines:
            stripped = line.strip()
            if stripped.startswith('[') and stripped.endswith(']'):
                sections.append(stripped[1:-1])
            elif '=' in stripped and not stripped.startswith('#'):
                key_part = stripped.split('=', 1)[0].strip()
                keys.append(key_part)
        
        result.top_level_keys = list(set(keys))[:20]
        result.sections = sections
        result.key_count = len(keys)
        result.section_count = len(sections)
        result.sample_content = content[:500]
        
        return result
    
    def _analyze_ini(self, content: str) -> ConfigAnalysisResult:
        """分析 INI 配置文件"""
        result = ConfigAnalysisResult()
        
        lines = content.split('\n')
        sections = []
        keys = []
        current_section = None
        
        for line in lines:
            stripped = line.strip()
            
            if stripped.startswith('[') and stripped.endswith(']'):
                current_section = stripped[1:-1]
                sections.append(current_section)
            elif '=' in stripped and not stripped.startswith('#') and not stripped.startswith(';'):
                key_part = stripped.split('=', 1)[0].strip()
                if current_section:
                    keys.append(f"{current_section}.{key_part}")
                else:
                    keys.append(key_part)
        
        result.top_level_keys = list(set(keys))[:20]
        result.sections = sections
        result.key_count = len(keys)
        result.section_count = len(sections)
        result.sample_content = content[:500]
        
        return result
    
    def _analyze_key_value(self, content: str) -> ConfigAnalysisResult:
        """分析简单 key-value 配置文件"""
        result = ConfigAnalysisResult()
        
        lines = content.split('\n')
        keys = []
        
        for line in lines:
            stripped = line.strip()
            if '=' in stripped and not stripped.startswith('#'):
                key_part = stripped.split('=', 1)[0].strip()
                if key_part:
                    keys.append(key_part)
        
        result.top_level_keys = list(set(keys))[:20]
        result.key_count = len(keys)
        result.sample_content = content[:500]
        
        return result
    
    def _analyze_xml(self, content: str) -> ConfigAnalysisResult:
        """分析 XML 配置文件"""
        result = ConfigAnalysisResult()
        
        # 简化处理 - 提取标签名
        tag_pattern = r'<(\w+)[^>]*>'
        tags = re.findall(tag_pattern, content)
        unique_tags = list(set(tags))
        
        result.top_level_keys = unique_tags[:20]
        result.key_count = len(tags)
        result.is_hierarchical = True
        result.sample_content = content[:500]
        
        return result
    
    def _analyze_generic_config(self, content: str) -> ConfigAnalysisResult:
        """分析通用配置文件"""
        result = ConfigAnalysisResult()
        
        # 提取看起来像键值对的行
        lines = content.split('\n')
        keys = []
        
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith('#') or stripped.startswith(';'):
                continue
            
            if ':' in stripped or '=' in stripped:
                delimiter = ':' if ':' in stripped else '='
                key_part = stripped.split(delimiter, 1)[0].strip()
                if key_part:
                    keys.append(key_part)
        
        result.top_level_keys = list(set(keys))[:20]
        result.key_count = len(keys)
        result.sample_content = content[:500]
        
        return result
    
    def _count_nested_sections(self, data: Any, depth: int = 0) -> int:
        """计算嵌套节的数量"""
        if depth > 5:  # 限制深度
            return 0
        
        count = 0
        if isinstance(data, dict):
            count += len(data)
            for value in data.values():
                count += self._count_nested_sections(value, depth + 1)
        elif isinstance(data, list) and depth > 0:  # 只统计嵌套列表
            count += 1
        
        return count
    
    def _extract_sample_content(self, content: str, suffix: str) -> str:
        """提取样例内容"""
        lines = content.split('\n')
        return '\n'.join(lines[:20])  # 前20行
    
    def _generate_config_summary(self, config_result: ConfigAnalysisResult) -> str:
        """生成配置文件摘要"""
        parts = []
        parts.append(f"Configuration file with {config_result.key_count} keys")
        
        if config_result.section_count > 0:
            parts.append(f"  - {config_result.section_count} sections")
        
        if config_result.is_hierarchical:
            parts.append(f"  - Hierarchical structure")
        
        if config_result.top_level_keys:
            parts.append(f"  - Top keys: {', '.join(config_result.top_level_keys[:5])}")
        
        return '\n'.join(parts)


# ==================== 修改现有的表格数据文件分析器 ====================

class TableDataAnalyzer(BaseDataSheetAnalyzer):
    """表格数据文件分析器 - CSV/TSV 等"""
    
    def __init__(self, debug: bool = False):
        super().__init__()
        self.debug = debug
        self.smart_analyzer = SmartDataFileAnalyzer(debug)
    
    def can_handle(self, filepath: Path, content: Optional[str] = None) -> bool:
        """判断是否为表格数据文件或智能数据文件"""
        # 首先检查是否是表格数据文件
        table_extensions = {
            '.csv', '.tsv', '.dat', '.data', '.txt'
        }
        
        suffix = filepath.suffix.lower()
        if suffix in table_extensions:
            return True
        
        # 如果不是标准表格文件，检查是否需要智能分析
        return self.smart_analyzer.can_handle(filepath, content)
    
    def analyze(self, filepath: Path, content: Optional[str] = None) -> SemanticAnalysisResult:
        """
        分析表格数据文件或智能数据文件
        
        Args:
            filepath: 文件路径
            content: 文件内容
            
        Returns:
            SemanticAnalysisResult: 分析结果
        """
        # 首先尝试作为标准表格文件分析
        if filepath.suffix.lower() in ['.csv', '.tsv']:
            return self._analyze_standard_table(filepath, content)
        
        # 否则使用智能分析器
        return self.smart_analyzer.analyze(filepath, content)
    
    def _analyze_standard_table(self, filepath: Path, content: Optional[str] = None) -> SemanticAnalysisResult:
        """分析标准表格文件（原有逻辑）"""
        result = SemanticAnalysisResult(
            content_type="data_sheet",
            language="data"
        )
        
        try:
            if content is None:
                content = self._read_file_content(filepath)
            
            if content is None:
                result.success = False
                result.error_message = "Could not read file content"
                return result
            
            # 检测是否为结构化数据文件，需要智能截断
            if SmartTextProcessor.is_structured_data_file(filepath):
                result = self._analyze_structured_data(filepath, content, result)
            else:
                result = self._analyze_csv_table(filepath, content, result)
            
            result.success = True
            
        except Exception as e:
            result.success = False
            result.error_message = f"Table analysis failed: {str(e)}"
        
        return result
    
    def _read_file_content(self, filepath: Path) -> Optional[str]:
        """读取文件内容"""
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                return f.read()
        except UnicodeDecodeError:
            try:
                with open(filepath, 'r', encoding='latin-1') as f:
                    return f.read()
            except Exception:
                return None
    
    def _analyze_csv_table(self, filepath: Path, content: str,
                          result: SemanticAnalysisResult) -> SemanticAnalysisResult:
        """分析 CSV/TSV 表格数据"""
        lines = content.split('\n')
        table_result = TableAnalysisResult()
        
        # 尝试自动检测分隔符
        delimiters = [',', '\t', ';', '|', ' ']
        best_delimiter = None
        max_columns = 0
        
        for delimiter in delimiters:
            column_counts = []
            for line in lines[:20]:
                stripped = line.strip()
                if not stripped or stripped.startswith(('#', '//', '/*', '*', 'C', 'CC', '!')):
                    continue
                columns = [c.strip() for c in stripped.split(delimiter)]
                if len(columns) > 1:
                    column_counts.append(len(columns))
            
            if column_counts:
                avg_columns = sum(column_counts) / len(column_counts)
                if avg_columns > max_columns and avg_columns >= 2:
                    max_columns = avg_columns
                    best_delimiter = delimiter
        
        if best_delimiter:
            table_result.delimiter = best_delimiter
            table_result = self._parse_with_delimiter(content, best_delimiter, table_result)
        else:
            # 没有明确的分隔符，尝试通用分析
            table_result = self._analyze_generic_table(content, table_result)
        
        # 生成摘要
        result.summary = self._generate_table_summary(table_result)
        result.keywords = table_result.headers[:10]
        result.metadata["table_analysis"] = table_result
        result.metadata["table_sample"] = self._extract_table_sample(content, table_result)
        
        return result
    
    def _parse_with_delimiter(self, content: str, delimiter: str,
                             table_result: TableAnalysisResult) -> TableAnalysisResult:
        """使用指定分隔符解析表格"""
        lines = content.split('\n')
        rows = []
        header_candidate = None
        
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith(('#', '//', '/*', '*', 'C', 'CC', '!')):
                continue
            
            columns = [c.strip() for c in stripped.split(delimiter)]
            rows.append(columns)
            
            if len(rows) >= 100:  # 只分析前100行
                break
        
        if rows:
            # 检查第一行是否为表头
            first_row = rows[0]
            table_result.column_count = len(first_row)
            
            # 判断是否有表头：检查第一行是否有较多非数字内容
            non_numeric_count = sum(1 for cell in first_row 
                                   if cell and not cell.replace('.', '').replace('-', '').isdigit())
            
            if non_numeric_count >= len(first_row) * 0.5:
                table_result.headers = first_row
                table_result.has_header = True
                table_result.sample_rows = rows[1:6]
            else:
                table_result.headers = [f"col_{i}" for i in range(len(first_row))]
                table_result.has_header = False
                table_result.sample_rows = rows[:5]
            
            table_result.row_count = len(rows)
            
            # 估算总行数
            total_lines = len([l for l in lines if l.strip()])
            table_result.estimated_total_rows = total_lines
            
            # 分析列类型
            table_result.column_types = self._analyze_column_types(rows, table_result.has_header)
        
        return table_result
    
    def _analyze_column_types(self, rows: List[List[str]], has_header: bool) -> Dict[str, str]:
        """分析列数据类型"""
        column_types = {}
        start_idx = 1 if has_header else 0
        sample_rows = rows[start_idx:start_idx+20]
        
        if not sample_rows:
            return column_types
        
        num_columns = len(sample_rows[0])
        
        for col_idx in range(num_columns):
            type_counts = Counter()
            
            for row in sample_rows:
                if col_idx < len(row):
                    cell = row[col_idx].strip()
                    if not cell:
                        type_counts['empty'] += 1
                    elif cell.replace('.', '').replace('-', '').replace('e', '').isdigit():
                        type_counts['numeric'] += 1
                    else:
                        type_counts['text'] += 1
            
            # 确定主要类型
            if type_counts:
                main_type = type_counts.most_common(1)[0][0]
                column_types[f"col_{col_idx}"] = main_type
        
        return column_types
    
    def _analyze_generic_table(self, content: str, 
                               table_result: TableAnalysisResult) -> TableAnalysisResult:
        """分析通用表格数据（无明确分隔符）"""
        lines = content.split('\n')
        
        # 统计行数
        table_result.row_count = len([l for l in lines if l.strip()])
        table_result.estimated_total_rows = table_result.row_count
        
        # 检查是否为固定宽度格式
        if table_result.row_count > 5:
            sample_lines = lines[:20]
            line_lengths = [len(l) for l in sample_lines if l.strip()]
            if line_lengths and max(line_lengths) - min(line_lengths) < 10:
                table_result.column_count = 1  # 简化处理
        
        table_result.sample_content = content[:500]
        
        return table_result
    
    def _analyze_structured_data(self, filepath: Path, content: str,
                                 result: SemanticAnalysisResult) -> SemanticAnalysisResult:
        """分析结构化数据文件（SPICE 等）"""
        # 使用智能处理器提取人类可读部分
        human_content = SmartTextProcessor.extract_human_relevant_content(
            content, filepath, max_human_lines=50
        )
        
        result.summary = (
            f"[Structured Data File] Type: {filepath.suffix}\n"
            f"Original lines: {len(content.split('\n'))}\n"
            f"Preserved lines: {len(human_content.split('\n'))}"
        )
        
        result.keywords = self._extract_keywords_from_content(human_content)
        result.metadata["structured_content"] = human_content
        result.metadata["is_truncated"] = len(content.split('\n')) > 50
        
        return result
    
    def _extract_keywords_from_content(self, content: str) -> List[str]:
        """从内容中提取关键词"""
        words = re.findall(r'\b[\w\u4e00-\u9fff]{3,}\b', content.lower())
        stop_words = {'the', 'and', 'for', 'with', 'that', 'this', 'from', 
                     '的', '了', '在', '是', '我', '有', '和', '就'}
        filtered = [w for w in words if w not in stop_words]
        counter = Counter(filtered)
        return [word for word, _ in counter.most_common(10)]
    
    def _extract_table_sample(self, content: str, table_result: TableAnalysisResult) -> str:
        """提取表格样例"""
        lines = content.split('\n')
        sample_lines = []
        
        for line in lines[:20]:  # 前20行
            stripped = line.strip()
            if not stripped or stripped.startswith(('#', '//', '/*', '*', 'C', 'CC', '!')):
                continue
            sample_lines.append(line)
            
            if len(sample_lines) >= 10:  # 保留10行数据
                break
        
        return '\n'.join(sample_lines)
    
    def _generate_table_summary(self, table_result: TableAnalysisResult) -> str:
        """生成表格数据摘要"""
        parts = []
        
        if table_result.estimated_total_rows:
            parts.append(f"Table data with ~{table_result.estimated_total_rows} rows")
        else:
            parts.append(f"Table data with {table_result.row_count} rows")
        
        if table_result.column_count > 0:
            parts.append(f"  - {table_result.column_count} columns")
        
        if table_result.headers:
            parts.append(f"  - Headers: {', '.join(table_result.headers[:5])}")
        
        if table_result.delimiter:
            delimiter_name = {
                ',': 'comma',
                '\t': 'tab',
                ';': 'semicolon',
                '|': 'pipe',
                ' ': 'space'
            }.get(table_result.delimiter, 'custom')
            parts.append(f"  - Delimiter: {delimiter_name}")
        
        return '\n'.join(parts)


# ==================== 修改组合分析器以包含智能分析器 ====================

class CompositeSheetAnalyzer(BaseDataSheetAnalyzer):
    """组合配置/表格数据文件分析器 - 增强版，包含智能分析"""
    
    def __init__(self, debug: bool = False):
        super().__init__()
        self.debug = debug
        self.config_analyzer = ConfigFileAnalyzer()
        self.table_analyzer = TableDataAnalyzer(debug)  # 使用增强版
        self.smart_analyzer = SmartDataFileAnalyzer(debug)
    
    def can_handle(self, filepath: Path, content: Optional[str] = None) -> bool:
        """只要有一个分析器能处理就返回True"""
        return (self.config_analyzer.can_handle(filepath, content) or
                self.table_analyzer.can_handle(filepath, content) or
                self.smart_analyzer.can_handle(filepath, content))
    
    def analyze(self, filepath: Path, content: Optional[str] = None) -> SemanticAnalysisResult:
        """使用合适的分析器进行分析"""
        if self.config_analyzer.can_handle(filepath, content):
            return self.config_analyzer.analyze(filepath, content)
        elif self.table_analyzer.can_handle(filepath, content):
            return self.table_analyzer.analyze(filepath, content)
        else:
            # 默认使用智能分析器
            return self.smart_analyzer.analyze(filepath, content)


# ==================== 公共 API 导出 ====================

__all__ = [
    'ConfigAnalysisResult',
    'TableAnalysisResult',
    'ConfigFileAnalyzer',
    'TableDataAnalyzer',
    'CompositeSheetAnalyzer',
    'SmartDataFileAnalyzer',  # 新增
]
