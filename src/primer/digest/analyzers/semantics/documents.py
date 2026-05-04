"""
文本文档分析器 - 专门处理各种文档格式的语义分析
包括：
- 语言检测
- 摘要生成
- 标题提取
- 关键章节提取
- 文本指标分析
- 阅读时间估计
- 智能截断处理
"""

import re
import json
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
from collections import Counter

from .base import (
    BaseDocumentAnalyzer,
    SemanticAnalysisResult,
    HumanReadableSummary,
    SmartTextProcessor,
    ContentAnalyzer,
)
from ...base import YAML_AVAILABLE, CHARDET_AVAILABLE

if YAML_AVAILABLE:
    import yaml


# ==================== 文档分析器 ====================

class HumanReadableDocumentAnalyzer(BaseDocumentAnalyzer):
    """人类可读文档分析器"""
    
    def __init__(self, max_lines: int = 10):
        super().__init__()
        self.max_lines = max_lines
    
    def can_handle(self, filepath: Path, content: Optional[str] = None) -> bool:
        """判断是否为文档文件"""
        doc_extensions = {
            '.md', '.markdown', '.rst', '.txt', '.html', '.htm', 
            '.tex', '.latex', '.cmt'
        }
        return filepath.suffix.lower() in doc_extensions
    
    def analyze(self, filepath: Path, content: Optional[str] = None) -> SemanticAnalysisResult:
        """
        分析文本文档
        
        Args:
            filepath: 文件路径
            content: 文件内容（如果已读取）
            
        Returns:
            SemanticAnalysisResult: 分析结果
        """
        result = SemanticAnalysisResult(
            content_type="document",
            language="unknown"
        )
        
        try:
            # 读取内容（如果未提供）
            if content is None:
                content = self._read_file_content(filepath)
            
            if content is None:
                result.success = False
                result.error_message = "Could not read file content"
                return result
            
            # 检查是否为结构化数据文件，需要智能截断
            if SmartTextProcessor.is_structured_data_file(filepath):
                result = self._analyze_structured_data(filepath, content, result)
            else:
                result = self._analyze_regular_document(filepath, content, result)
            
            result.success = True
            
        except Exception as e:
            result.success = False
            result.error_message = f"Document analysis failed: {str(e)}"
        
        return result
    
    def _read_file_content(self, filepath: Path) -> Optional[str]:
        """读取文件内容"""
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                return f.read()
        except UnicodeDecodeError:
            try:
                with open(filepath, 'rb') as f:
                    raw_content = f.read()
                    
                    if not CHARDET_AVAILABLE:
                        encodings_to_try = ['utf-8', 'latin-1', 'iso-8859-1', 'cp1252']
                        for encoding in encodings_to_try:
                            try:
                                return raw_content.decode(encoding)
                            except UnicodeDecodeError:
                                continue
                        return raw_content.decode('latin-1', errors='ignore')
                    else:
                        import chardet
                        chardet_result = chardet.detect(raw_content)
                        encoding = chardet_result['encoding'] if chardet_result['encoding'] else 'latin-1'
                        return raw_content.decode(encoding, errors='ignore')
            except Exception:
                return None
    
    def _analyze_regular_document(self, filepath: Path, content: str, 
                                   result: SemanticAnalysisResult) -> SemanticAnalysisResult:
        """分析普通文本文档"""
        lines = content.split('\n')
        
        # 生成人类可读摘要
        summary = self.summarize(filepath, content, self.max_lines)
        
        result.language = summary.language
        result.summary = summary.summary
        result.keywords = summary.key_topics[:10]
        result.metadata["human_readable_summary"] = summary
        
        return result
    
    def _analyze_structured_data(self, filepath: Path, content: str,
                                  result: SemanticAnalysisResult) -> SemanticAnalysisResult:
        """分析结构化数据文件（使用智能截断）"""
        lines = content.split('\n')
        is_large = len(lines) > 50
        
        if is_large:
            # 使用智能处理器提取人类可读部分
            human_content = SmartTextProcessor.extract_human_relevant_content(
                content, filepath, max_human_lines=50
            )
            
            human_lines = human_content.split('\n')
            
            # 分析截断后的内容
            summary = self.summarize(filepath, human_content, self.max_lines)
            
            # 保留原始文件的总行数信息
            summary.line_count = len(lines)
            
            # 更新摘要，说明是截断的
            summary.summary = (
                f"[Intelligently Truncated] File contains structured data. "
                f"Original: {len(lines)} lines, preserved {len(human_lines)} lines of metadata. "
                f"Type: {filepath.suffix} kernel/data file."
            )
            
            result.language = summary.language
            result.summary = summary.summary
            result.keywords = summary.key_topics[:10]
            result.metadata["human_readable_summary"] = summary
            result.metadata["truncated"] = True
            result.metadata["original_line_count"] = len(lines)
            result.metadata["preserved_line_count"] = len(human_lines)
        else:
            # 文件不大，直接分析
            result = self._analyze_regular_document(filepath, content, result)
        
        return result
    
    def summarize(self, filepath: Path, content: str, max_lines: int = 10) -> HumanReadableSummary:
        """
        生成人类可读文本摘要
        
        Args:
            filepath: 文件路径
            content: 文件内容
            max_lines: 提取的最大行数
            
        Returns:
            HumanReadableSummary: 摘要对象
        """
        if not content:
            return HumanReadableSummary(
                line_count=0,
                word_count=0,
                character_count=0
            )
        
        lines = content.split('\n')
        line_count = len(lines)
        
        # 统计基本信息
        words = re.findall(r'\b[\w\u4e00-\u9fff]+\b', content)
        word_count = len(words)
        
        # 提取标题
        title = self._extract_title(filepath, content, lines)
        
        # 检测语言
        language = self._detect_language(content)
        
        # 提取关键章节
        key_sections = self._extract_key_sections(filepath, content, lines)
        
        # 生成文本摘要
        summary_text = self._generate_text_summary(filepath, content, lines, max_lines)
        
        # 分析文本指标
        text_metrics = self._analyze_text_metrics(content)
        
        # 提取首尾行
        first_lines = lines[:min(5, len(lines))]
        last_lines = lines[-min(3, len(lines)):] if len(lines) > 3 else []
        
        # 检测编码
        encoding = self._detect_encoding(content)
        
        return HumanReadableSummary(
            title=title,
            line_count=line_count,
            word_count=word_count,
            character_count=len(content),
            language=language,
            encoding=encoding,
            first_lines=first_lines,
            last_lines=last_lines,
            key_sections=key_sections[:10],
            summary=summary_text,
            reading_time_minutes=text_metrics.get('reading_time_minutes', 0),
            reading_level=text_metrics.get('reading_level', 'unknown'),
            key_topics=text_metrics.get('key_topics', []),
            sentiment_score=text_metrics.get('sentiment_score')
        )
    
    def _detect_encoding(self, content: str) -> str:
        """检测文本编码"""
        try:
            content.encode('utf-8')
            return 'utf-8'
        except UnicodeEncodeError:
            if not CHARDET_AVAILABLE:
                return 'unknown'
            
            try:
                if isinstance(content, str):
                    byte_content = content.encode('latin-1', errors='ignore')
                else:
                    byte_content = content
                
                if CHARDET_AVAILABLE:
                    import chardet
                    detection = chardet.detect(byte_content)
                    return detection.get('encoding', 'unknown')
            except Exception:
                return 'unknown'
    
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
            (r'^###\s+(.+)$', 1),
            (r'^(.+)\n=+$', 1),
            (r'^(.+)\n-+$', 1),
            (r'^<h1[^>]*>(.+?)</h1>', 1),
            (r'^<title[^>]*>(.+?)</title>', 1),
            (r'^\[(.+)\]$', 1),
            (r'^([A-Z][A-Za-z\s]{5,40})$', 0),
            (r'^([\u4e00-\u9fff\s]{4,30})$', 0),
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
    
    def _detect_language(self, content: str) -> Optional[str]:
        """检测文本语言"""
        chinese_chars = re.findall(r'[\u4e00-\u9fff]', content)
        chinese_count = len(chinese_chars)
        
        english_words = re.findall(r'\b[a-zA-Z]{3,}\b', content)
        english_count = len(english_words)
        
        total_meaningful = chinese_count + english_count
        if total_meaningful == 0:
            return None
        
        chinese_ratio = chinese_count / total_meaningful
        
        if chinese_ratio > 0.7:
            return "zh"
        elif chinese_ratio < 0.3:
            return "en"
        else:
            return "mixed"
    
    def _extract_key_sections(self, filepath: Path, content: str, lines: List[str]) -> List[Tuple[str, str]]:
        """提取关键章节"""
        suffix = filepath.suffix.lower()
        
        if suffix in ['.md', '.markdown', '.rst']:
            return self._extract_markdown_sections(lines)
        elif suffix == '.cmt':
            return self._extract_cmt_sections(content, lines)
        elif suffix in ['.json', '.yaml', '.yml']:
            return self._extract_structured_sections(content, suffix)
        elif suffix in ['.ini', '.cfg', '.conf', '.toml', '.properties']:
            return self._extract_config_sections(lines)
        elif suffix in ['.xml', '.html', '.htm']:
            return self._extract_xml_sections(content)
        else:
            return self._extract_general_sections(lines)
    
    def _extract_markdown_sections(self, lines: List[str]) -> List[Tuple[str, str]]:
        """提取Markdown文档的章节"""
        sections = []
        current_section = None
        current_content = []
        
        for line in lines:
            match = re.match(r'^(#{1,6})\s+(.+)$', line.strip())
            if match:
                if current_section and current_content:
                    sections.append((current_section, '\n'.join(current_content[:5])))
                
                level = len(match.group(1))
                title = match.group(2).strip()
                current_section = f"{'#' * level} {title}"
                current_content = []
            elif current_section:
                current_content.append(line)
        
        if current_section and current_content:
            sections.append((current_section, '\n'.join(current_content[:5])))
        
        return sections
    
    def _extract_cmt_sections(self, content: str, lines: List[str]) -> List[Tuple[str, str]]:
        """提取.cmt文件的章节"""
        sections = []
        current_section = None
        current_content = []
        
        for i, line in enumerate(lines):
            line_stripped = line.strip()
            
            section_found = False
            cmt_patterns = [
                (r'^\s*\*+\s*$', 'separator'),
                (r'^\s*([Ss]ection|[Cc]hapter|[Pp]art)\s+([A-Za-z0-9\.\-]+)', 'section_header'),
                (r'^\s*(\d{4}-\d{2}-\d{2}|\d{2}:\d{2}:\d{2})', 'timestamp'),
            ]
            
            for pattern, section_type in cmt_patterns:
                if re.match(pattern, line_stripped):
                    if current_section and current_content:
                        sections.append((current_section, '\n'.join(current_content[:3])))
                    
                    if section_type == 'separator':
                        current_section = f"Separator (line {i+1})"
                    else:
                        current_section = f"{line_stripped[:50]}... (line {i+1})"
                    
                    current_content = []
                    section_found = True
                    break
            
            if not section_found and current_section:
                if line_stripped and len(current_content) < 10:
                    current_content.append(line)
        
        if current_section and current_content:
            sections.append((current_section, '\n'.join(current_content[:3])))
        
        return sections
    
    def _extract_structured_sections(self, content: str, suffix: str) -> List[Tuple[str, str]]:
        """提取结构化数据文件的章节"""
        sections = []
        
        try:
            if suffix == '.json':
                data = json.loads(content)
                if isinstance(data, dict):
                    for key in list(data.keys())[:10]:
                        value = data[key]
                        if isinstance(value, (dict, list)):
                            sections.append((f"Key: {key}", f"Type: {type(value).__name__}"))
                        else:
                            sections.append((f"Key: {key}", f"Value: {str(value)[:100]}"))
            
            elif suffix in ['.yaml', '.yml']:
                if not YAML_AVAILABLE:
                    lines = content.split('\n')
                    for line in lines[:20]:
                        line = line.strip()
                        if line and not line.startswith('#'):
                            if ':' in line:
                                key = line.split(':', 1)[0].strip()
                                sections.append((f"YAML Key: {key}", "..."))
                    return sections
                
                data = yaml.safe_load(content)
                if isinstance(data, dict):
                    for key in list(data.keys())[:10]:
                        value = data[key]
                        if isinstance(value, (dict, list)):
                            sections.append((f"Key: {key}", f"Type: {type(value).__name__}"))
                        else:
                            sections.append((f"Key: {key}", f"Value: {str(value)[:100]}"))
        
        except Exception:
            pass
        
        return sections
    
    def _extract_config_sections(self, lines: List[str]) -> List[Tuple[str, str]]:
        """提取配置文件的章节"""
        sections = []
        current_section = None
        current_content = []
        
        for line in lines:
            line_stripped = line.strip()
            
            ini_match = re.match(r'^\[(.+)\]$', line_stripped)
            if ini_match:
                if current_section and current_content:
                    sections.append((current_section, '\n'.join(current_content[:5])))
                
                current_section = f"[{ini_match.group(1)}]"
                current_content = []
            elif line_stripped.startswith('[') and ']' in line_stripped:
                if current_section and current_content:
                    sections.append((current_section, '\n'.join(current_content[:5])))
                
                current_section = line_stripped
                current_content = []
            elif current_section:
                if '=' in line and not line.startswith('#') and not line.startswith(';'):
                    current_content.append(line.strip())
        
        if current_section and current_content:
            sections.append((current_section, '\n'.join(current_content[:5])))
        
        return sections
    
    def _extract_xml_sections(self, content: str) -> List[Tuple[str, str]]:
        """提取XML/HTML文件的章节"""
        sections = []
        
        xml_patterns = [
            r'<(\w+)[^>]*>.*?</\1>',
            r'<([a-zA-Z][a-zA-Z0-9]*)[^>]*/>',
            r'<!--(.*?)-->',
        ]
        
        for pattern in xml_patterns:
            matches = re.findall(pattern, content, re.DOTALL)
            for match in matches[:10]:
                if isinstance(match, tuple):
                    tag = match[0] if match else "Unknown"
                    sections.append((f"XML Tag: {tag}", "..."))
                else:
                    sections.append((f"XML Element", str(match)[:100]))
        
        return sections
    
    def _extract_general_sections(self, lines: List[str]) -> List[Tuple[str, str]]:
        """提取通用文本文件的章节"""
        sections = []
        
        section_patterns = [
            (r'^(第[一二三四五六七八九十]+章|[0-9]+(\.[0-9]+)*\s+[^\s].*)$', 1),
            (r'^[A-Z][A-Za-z\s]{10,80}$', 0),
            (r'^[\u4e00-\u9fff\s]{4,50}$', 0),
            (r'^(.+)\n[-=]{3,}$', 1),
        ]
        
        for i in range(min(50, len(lines))):
            line = lines[i].strip()
            if not line or len(line) < 5:
                continue
            
            for pattern, group_idx in section_patterns:
                match = re.match(pattern, line)
                if match:
                    if group_idx == 0:
                        title = line
                    else:
                        title = match.group(group_idx)
                    
                    content_lines = []
                    for j in range(i+1, min(i+6, len(lines))):
                        if lines[j].strip():
                            content_lines.append(lines[j].strip()[:100])
                    
                    if content_lines:
                        sections.append((title, ' '.join(content_lines)[:200]))
                    break
        
        return sections
    
    def _generate_text_summary(self, filepath: Path, content: str, lines: List[str], max_lines: int) -> Optional[str]:
        """生成文本摘要"""
        if not content:
            return None
        
        suffix = filepath.suffix.lower()
        line_count = len(lines)
        
        summary_parts = []
        
        summary_parts.append(f"文件类型: {suffix[1:] if suffix else '文本文件'}")
        summary_parts.append(f"总行数: {line_count}")
        summary_parts.append(f"总字数: {len(re.findall(r'\\b[\\w\\u4e00-\\u9fff]+\\b', content))}")
        
        if suffix in ['.md', '.markdown', '.rst']:
            heading_count = sum(1 for line in lines if re.match(r'^#{1,6}\s+', line.strip()))
            summary_parts.append(f"标题数量: {heading_count}")
        
        elif suffix == '.json':
            try:
                data = json.loads(content)
                if isinstance(data, dict):
                    summary_parts.append(f"顶层键数量: {len(data)}")
                    keys = list(data.keys())[:5]
                    summary_parts.append(f"主要键: {', '.join(keys)}")
            except Exception:
                summary_parts.append("JSON结构: 无效或无法解析")
        
        elif suffix in ['.yaml', '.yml']:
            if not YAML_AVAILABLE:
                summary_parts.append("YAML结构: 未安装PyYAML，跳过详细解析")
            else:
                try:
                    data = yaml.safe_load(content)
                    if isinstance(data, dict):
                        summary_parts.append(f"顶层键数量: {len(data)}")
                        keys = list(data.keys())[:5]
                        summary_parts.append(f"主要键: {', '.join(keys)}")
                except Exception:
                    summary_parts.append("YAML结构: 无效或无法解析")
        
        if line_count > max_lines * 2:
            first_part = '\n'.join(lines[:max_lines])
            last_part = '\n'.join(lines[-max_lines:])
            summary_parts.append(f"\n开头部分（前{max_lines}行）：")
            summary_parts.append(first_part)
            summary_parts.append(f"\n结尾部分（后{max_lines}行）：")
            summary_parts.append(last_part)
        else:
            summary_parts.append("\n完整内容：")
            summary_parts.append(content[:500] + ("..." if len(content) > 500 else ""))
        
        return '\n'.join(summary_parts)
    
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
        
        stop_words = {'的', '了', '在', '是', '我', '有', '和', '就', '不', '人', '都', '一', '一个',
                     'to', 'the', 'and', 'of', 'in', 'a', 'is', 'that', 'it', 'for', 'on', 'with', 'as'}
        
        chinese_words = [w for w in words if re.match(r'[\u4e00-\u9fff]', w)]
        english_words = [w.lower() for w in words if re.match(r'[a-zA-Z]', w)]
        
        chinese_freq = Counter([w for w in chinese_words if w not in stop_words]).most_common(5)
        english_freq = Counter([w for w in english_words if w not in stop_words]).most_common(5)
        
        key_topics = [word for word, _ in chinese_freq + english_freq]
        
        positive_words = {'好', '优秀', '成功', '喜欢', '爱', '高兴', '快乐', '开心', '满意'}
        negative_words = {'坏', '失败', '讨厌', '恨', '悲伤', '难过', '生气', '失望', '问题'}
        
        positive_count = sum(1 for w in words if w in positive_words)
        negative_count = sum(1 for w in words if w in negative_words)
        
        total_sentiment = positive_count + negative_count
        if total_sentiment > 0:
            sentiment_score = (positive_count - negative_count) / total_sentiment
        else:
            sentiment_score = 0.0
        
        return {
            "reading_time_minutes": reading_time_minutes,
            "reading_level": reading_level,
            "key_topics": key_topics,
            "sentiment_score": sentiment_score,
            "word_count": word_count,
            "sentence_count": len(sentences),
            "avg_sentence_length": avg_sentence_length
        }


# ==================== Markdown 文档分析器 ====================

class MarkdownDocumentAnalyzer(HumanReadableDocumentAnalyzer):
    """专门优化的 Markdown 文档分析器"""
    
    def can_handle(self, filepath: Path, content: Optional[str] = None) -> bool:
        """判断是否为 Markdown 文件"""
        return filepath.suffix.lower() in ['.md', '.markdown']
    
    def analyze(self, filepath: Path, content: Optional[str] = None) -> SemanticAnalysisResult:
        """分析 Markdown 文档"""
        result = super().analyze(filepath, content)
        
        if result.success and result.metadata.get("human_readable_summary"):
            summary = result.metadata["human_readable_summary"]
            # Markdown 特有的分析
            result.metadata["markdown_analysis"] = {
                "heading_count": sum(1 for line in (content or '').split('\n') 
                                   if re.match(r'^#{1,6}\s+', line.strip())),
                "has_code_blocks": bool(re.search(r'```[\s\S]*?```', content or '')),
                "has_links": bool(re.search(r'\[.+?\]\(.+?\)', content or '')),
                "has_images": bool(re.search(r'!\[.*?\]\(.+?\)', content or '')),
            }
        
        return result


# ==================== 组合文档分析器 ====================

class CompositeDocumentAnalyzer(BaseDocumentAnalyzer):
    """组合文档分析器 - 使用多个专用分析器"""
    
    def __init__(self):
        super().__init__()
        self.analyzers = [
            MarkdownDocumentAnalyzer(),
            HumanReadableDocumentAnalyzer(),
        ]
    
    def can_handle(self, filepath: Path, content: Optional[str] = None) -> bool:
        """只要有一个分析器能处理就返回True"""
        return any(analyzer.can_handle(filepath, content) for analyzer in self.analyzers)
    
    def analyze(self, filepath: Path, content: Optional[str] = None) -> SemanticAnalysisResult:
        """使用合适的分析器进行分析"""
        for analyzer in self.analyzers:
            if analyzer.can_handle(filepath, content):
                return analyzer.analyze(filepath, content)
        
        # 默认使用通用分析器
        return HumanReadableDocumentAnalyzer().analyze(filepath, content)


# ==================== 公共 API 导出 ====================

__all__ = [
    'HumanReadableDocumentAnalyzer',
    'MarkdownDocumentAnalyzer',
    'CompositeDocumentAnalyzer',
]
