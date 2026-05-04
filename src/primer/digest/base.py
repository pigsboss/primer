"""
Directory Digest - 基础模块
包含输入输出处理、文件系统分析、规则引擎等核心功能
"""

import os
import sys
import json
import hashlib
import mimetypes
import fnmatch
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union, Any
from dataclasses import dataclass, field
from datetime import datetime
import re
from enum import Enum
from collections import Counter

# 依赖检测
try:
    import chardet
    CHARDET_AVAILABLE = True
except ImportError:
    CHARDET_AVAILABLE = False
    print("Warning: chardet not installed, using simplified encoding detection", file=sys.stderr)

try:
    import yaml
    YAML_AVAILABLE = True
except ImportError:
    YAML_AVAILABLE = False
    print("Warning: PyYAML not installed, YAML parsing limited", file=sys.stderr)


# ==================== 枚举定义 ====================

class ProcessingStrategy(Enum):
    """文件处理策略枚举"""
    FULL_CONTENT = "full_content"
    SUMMARY_ONLY = "summary_only"
    CODE_SKELETON = "code_skeleton"
    STRUCTURE_EXTRACT = "structure_extract"
    HEADER_WITH_STATS = "header_with_stats"
    METADATA_ONLY = "metadata_only"


class OutputMode(Enum):
    """输出模式枚举"""
    SORT = "sort"
    FRAMEWORK = "framework"
    FULL = "full"


class FileType(Enum):
    """文件类型枚举"""
    CRITICAL_DOCS = "critical_docs"
    REFERENCE_DOCS = "reference_docs"
    SOURCE_CODE = "source_code"
    TEXT_DATA = "text_data"
    BINARY_FILES = "binary_files"
    UNKNOWN = "unknown"


class OutputFormats(Enum):
    """支持的输出格式"""
    JSON = "json"
    YAML = "yaml"
    MARKDOWN = "md"
    HTML = "html"
    TOML = "toml"
    PLAINTEXT = "txt"


# ==================== 数据类定义 ====================

@dataclass
class StrategyConfig:
    """策略配置"""
    token_estimate: float
    max_size: Optional[int] = None
    max_lines: Optional[int] = None
    include_metadata: bool = True
    include_functions: bool = False
    include_classes: bool = False
    max_keys: Optional[int] = None
    include_stats: bool = False


@dataclass
class FileRule:
    """文件规则定义"""
    name: str
    patterns: List[str]
    strategy: ProcessingStrategy
    priority: int = 50
    force_binary: bool = False
    max_size: Optional[int] = None
    comment: Optional[str] = None
    
    def matches(self, filepath: Path) -> bool:
        """检查文件是否匹配此规则"""
        if self.max_size:
            try:
                if filepath.stat().st_size > self.max_size:
                    return False
            except (OSError, IOError):
                return False
                
        for pattern in self.patterns:
            if fnmatch.fnmatch(filepath.name, pattern):
                return True
            if fnmatch.fnmatch(str(filepath), pattern):
                return True
        return False


@dataclass
class FileClassification:
    """文件分类结果 - 统一的数据契约"""
    file_type: FileType
    strategy: ProcessingStrategy
    force_binary: bool = False
    estimated_tokens: int = 0
    rule_name: Optional[str] = None
    
    def to_dict(self) -> Dict:
        return {
            "file_type": self.file_type.value,
            "strategy": self.strategy.value,
            "force_binary": self.force_binary,
            "estimated_tokens": self.estimated_tokens,
            "rule_name": self.rule_name
        }


@dataclass
class FileMetadata:
    """文件元数据基类"""
    path: Path
    size: int
    modified_time: datetime
    created_time: datetime
    file_type: FileType
    mime_type: Optional[str] = None
    md5_hash: Optional[str] = None
    sha256_hash: Optional[str] = None
    # 新增：分类阶段确定的策略和标记
    processing_strategy: Optional[ProcessingStrategy] = None
    force_binary: bool = False
    
    def to_dict(self) -> Dict:
        """转换为字典"""
        result = {
            "path": str(self.path),
            "size": self.size,
            "modified_time": self.modified_time.isoformat(),
            "created_time": self.created_time.isoformat(),
            "file_type": self.file_type.value,
            "mime_type": self.mime_type,
            "md5_hash": self.md5_hash,
            "sha256_hash": self.sha256_hash
        }
        if self.processing_strategy:
            result["processing_strategy"] = self.processing_strategy.value
        result["force_binary"] = self.force_binary
        return result


@dataclass
class FileDigest:
    """单个文件摘要"""
    metadata: FileMetadata
    full_content: Optional[str] = None
    human_readable_summary: Optional[Any] = None
    source_code_analysis: Optional[Any] = None
    # 添加：记录实际使用的处理策略
    actual_strategy: Optional[str] = None
    
    def to_dict(self, mode: str = "framework") -> Dict:
        """转换为字典"""
        import sys  # 添加导入
        # 这里我们无法直接获取debug标志，但可以通过环境变量或其他方式
        # 暂时设置为False，如果需要调试可以修改
        debug = False
        
        result = {"metadata": self.metadata.to_dict()}
        
        # 记录实际使用的策略
        if self.actual_strategy:
            result["actual_processing_strategy"] = self.actual_strategy
        
        # 根据策略决定输出哪些内容
        strategy = self.metadata.processing_strategy
        
        # 添加调试输出
        if debug:
            print(f"[DEBUG:FileDigest.to_dict] Converting file: {self.metadata.path}", file=sys.stderr)
            print(f"[DEBUG:FileDigest.to_dict]   Mode: {mode}", file=sys.stderr)
            print(f"[DEBUG:FileDigest.to_dict]   Strategy: {strategy}", file=sys.stderr)
            print(f"[DEBUG:FileDigest.to_dict]   full_content is None: {self.full_content is None}", file=sys.stderr)
            if self.full_content:
                print(f"[DEBUG:FileDigest.to_dict]   full_content length: {len(self.full_content)}", file=sys.stderr)
        
        # 对于 FULL_CONTENT 策略，总是输出完整内容（如果已设置）
        if strategy == ProcessingStrategy.FULL_CONTENT and self.full_content:
            # FULL_CONTENT策略：输出完整内容
            result["full_content"] = self.full_content
            if debug:
                print(f"[DEBUG:FileDigest.to_dict]   Added full_content to output", file=sys.stderr)
            
        elif strategy == ProcessingStrategy.SUMMARY_ONLY and self.human_readable_summary:
            # SUMMARY_ONLY策略：只输出摘要
            result["summary"] = getattr(self.human_readable_summary, 'to_dict', lambda: {})()
            
        elif strategy == ProcessingStrategy.CODE_SKELETON:
            # CODE_SKELETON策略：输出源代码分析
            if self.source_code_analysis:
                result["source_analysis"] = getattr(self.source_code_analysis, 'to_dict', lambda: {})()
            if self.human_readable_summary:
                result["summary"] = getattr(self.human_readable_summary, 'to_dict', lambda: {})()
                
        elif strategy in [ProcessingStrategy.STRUCTURE_EXTRACT, ProcessingStrategy.HEADER_WITH_STATS]:
            # 结构提取或头部统计：输出摘要
            if self.human_readable_summary:
                result["summary"] = getattr(self.human_readable_summary, 'to_dict', lambda: {})()
        
        if debug:
            print(f"[DEBUG:FileDigest.to_dict]   Result keys: {list(result.keys())}", file=sys.stderr)
        
        return result


@dataclass
class DirectoryStructure:
    """目录结构表示"""
    path: Path
    files: List[FileDigest] = field(default_factory=list)
    subdirectories: Dict[str, 'DirectoryStructure'] = field(default_factory=dict)
    
    def to_dict(self, mode: str = "framework") -> Dict:
        """转换为嵌套字典结构"""
        return {
            "path": str(self.path),
            "files": [f.to_dict(mode) for f in self.files],
            "subdirectories": {name: d.to_dict(mode) for name, d in self.subdirectories.items()}
        }


# ==================== 初始嵌入策略配置 ====================

class InitialEmbeddingStrategy:
    """初始嵌入策略配置"""
    
    @staticmethod
    def get_initial_strategy(output_mode: OutputMode, file_type: FileType) -> ProcessingStrategy:
        """根据输出模式和文件类型获取初始嵌入策略"""
        if output_mode == OutputMode.SORT:
            # sort模式：所有文件只输出基本metadata
            return ProcessingStrategy.METADATA_ONLY
            
        elif output_mode == OutputMode.FRAMEWORK:
            # framework模式
            if file_type == FileType.CRITICAL_DOCS:
                return ProcessingStrategy.FULL_CONTENT
            elif file_type == FileType.REFERENCE_DOCS:
                return ProcessingStrategy.SUMMARY_ONLY  # 极简摘要
            elif file_type == FileType.SOURCE_CODE:
                return ProcessingStrategy.CODE_SKELETON
            elif file_type == FileType.TEXT_DATA:
                return ProcessingStrategy.HEADER_WITH_STATS
            else:  # BINARY_FILES, UNKNOWN
                return ProcessingStrategy.METADATA_ONLY
                
        elif output_mode == OutputMode.FULL:
            # full模式
            if file_type in [FileType.CRITICAL_DOCS, FileType.REFERENCE_DOCS, 
                           FileType.SOURCE_CODE, FileType.TEXT_DATA]:
                return ProcessingStrategy.FULL_CONTENT
            else:  # BINARY_FILES, UNKNOWN
                return ProcessingStrategy.METADATA_ONLY
        
        return ProcessingStrategy.METADATA_ONLY
    
    @staticmethod
    def get_priority(file_type: FileType) -> int:
        """获取文件类型优先级（数值越小优先级越高）"""
        priority_map = {
            FileType.CRITICAL_DOCS: 1,      # 最高优先级
            FileType.REFERENCE_DOCS: 2,     # 次高优先级
            FileType.TEXT_DATA: 3,          # 中等优先级
            FileType.SOURCE_CODE: 4,        # 较低优先级
            FileType.BINARY_FILES: 5,       # 不调整（已是最低）
            FileType.UNKNOWN: 5             # 不调整（已是最低）
        }
        return priority_map.get(file_type, 5)
    
    @staticmethod
    def get_strategy_hierarchy(file_type: FileType) -> List[ProcessingStrategy]:
        """获取策略降级序列（从高到低）"""
        from typing import List
        
        if file_type == FileType.CRITICAL_DOCS:
            return [
                ProcessingStrategy.FULL_CONTENT,
                ProcessingStrategy.SUMMARY_ONLY,
                ProcessingStrategy.METADATA_ONLY
            ]
        elif file_type == FileType.REFERENCE_DOCS:
            return [
                ProcessingStrategy.FULL_CONTENT,
                ProcessingStrategy.SUMMARY_ONLY,
                ProcessingStrategy.HEADER_WITH_STATS,
                ProcessingStrategy.METADATA_ONLY
            ]
        elif file_type == FileType.SOURCE_CODE:
            return [
                ProcessingStrategy.FULL_CONTENT,
                ProcessingStrategy.CODE_SKELETON,
                ProcessingStrategy.METADATA_ONLY
            ]
        elif file_type == FileType.TEXT_DATA:
            return [
                ProcessingStrategy.FULL_CONTENT,
                ProcessingStrategy.STRUCTURE_EXTRACT,
                ProcessingStrategy.HEADER_WITH_STATS,
                ProcessingStrategy.METADATA_ONLY
            ]
        else:  # BINARY_FILES, UNKNOWN
            return [ProcessingStrategy.METADATA_ONLY]


# ==================== 策略配置映射 ====================

STRATEGY_CONFIGS: Dict[ProcessingStrategy, StrategyConfig] = {
    ProcessingStrategy.FULL_CONTENT: StrategyConfig(
        token_estimate=0.25,
        max_size=100 * 1024,
    ),
    ProcessingStrategy.SUMMARY_ONLY: StrategyConfig(
        token_estimate=0.05,
        max_lines=50,
    ),
    ProcessingStrategy.CODE_SKELETON: StrategyConfig(
        token_estimate=0.02,
        include_functions=True,
        include_classes=True,
    ),
    ProcessingStrategy.STRUCTURE_EXTRACT: StrategyConfig(
        token_estimate=0.03,
        max_keys=20,
    ),
    ProcessingStrategy.HEADER_WITH_STATS: StrategyConfig(
        token_estimate=0.01,
        max_lines=10,
        include_stats=True,
    ),
    ProcessingStrategy.METADATA_ONLY: StrategyConfig(
        token_estimate=0.001,
    ),
}


# ==================== 文件类型检测器 ====================

class FileTypeDetector:
    """智能文件类型检测器"""
    
    EXTENSION_MAPPING = {
        FileType.REFERENCE_DOCS: ['.md', '.markdown', '.rst', '.html', '.htm'],
        FileType.SOURCE_CODE: [
            '.py', '.java', '.cpp', '.c', '.h', '.hpp',
            '.js', '.ts', '.jsx', '.tsx',
            '.go', '.rs', '.rb', '.php', '.swift',
            '.sh', '.bash', '.ps1', '.bat', '.cmd',
            '.css', '.scss', '.less'
        ],
        FileType.TEXT_DATA: [
            '.txt', '.log', '.csv', '.tsv',
            '.yaml', '.yml', '.json', '.xml', '.toml', 
            '.ini', '.cfg', '.conf', '.env',
            '.tf', '.tls', '.tpc', '.ker', '.cmt'
        ],
        FileType.BINARY_FILES: [
            '.exe', '.dll', '.so', '.dylib',
            '.zip', '.tar', '.gz', '.bz2', '.xz', '.7z', '.rar',
            '.jpg', '.jpeg', '.png', '.gif', '.bmp',
            '.mp3', '.mp4', '.avi', '.mkv',
            '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
            '.bin', '.dat', '.db', '.sqlite',
            '.h5', '.hdf5', '.fits'
        ]
    }
    
    @staticmethod
    def detect_by_extension(filepath: Path) -> Optional[FileType]:
        """通过扩展名检测文件类型"""
        suffix = filepath.suffix.lower()
        for file_type, extensions in FileTypeDetector.EXTENSION_MAPPING.items():
            if suffix in extensions:
                return file_type
        return None
    
    @staticmethod
    def detect_by_content(filepath: Path) -> FileType:
        """通过内容分析检测文件类型"""
        try:
            with open(filepath, 'rb') as f:
                sample = f.read(4096)
                
                if b'\x00' in sample:
                    return FileType.BINARY_FILES
                
                printable_count = 0
                for byte in sample:
                    if 32 <= byte <= 126 or byte in (9, 10, 13):
                        printable_count += 1
                
                printable_ratio = printable_count / len(sample) if sample else 0
                
                if printable_ratio < 0.7:
                    return FileType.BINARY_FILES
                
                try:
                    decoded = sample.decode('utf-8', errors='ignore')
                    if FileTypeDetector._looks_like_source_code(decoded):
                        return FileType.SOURCE_CODE
                except:
                    pass
                
                return FileType.TEXT_DATA
                
        except Exception:
            return FileType.BINARY_FILES
    
    @staticmethod
    def _looks_like_source_code(content: str) -> bool:
        """判断内容是否像源代码"""
        patterns = [
            r'^\s*import\s+', r'^\s*package\s+', r'^\s*#include\s+',
            r'^\s*def\s+\w+\s*\(', r'^\s*function\s+\w+', r'^\s*class\s+\w+',
            r'^\s*public\s+', r'^\s*private\s+', r'^\s*protected\s+',
            r'^\s*static\s+', r'^\s*const\s+', r'^\s*let\s+\w+\s*=',
            r'^\s*var\s+\w+\s*=', r'^\s*console\.log', r'^\s*print\(',
            r'^\s*System\.out\.', r'^\s*//', r'^\s*/\*', r'^\s*\*/', r'^\s*#\s*',
        ]
        
        lines = content.split('\n')[:50]
        code_pattern_count = 0
        
        for line in lines:
            for pattern in patterns:
                if re.search(pattern, line):
                    code_pattern_count += 1
                    break
        
        return code_pattern_count >= 3
    
    @staticmethod
    def detect(filepath: Path) -> FileType:
        """综合检测文件类型"""
        type_by_ext = FileTypeDetector.detect_by_extension(filepath)
        if type_by_ext:
            return type_by_ext
        return FileTypeDetector.detect_by_content(filepath)


# ==================== 规则引擎 ====================

class RuleEngine:
    """规则引擎"""
    
    def __init__(self, rules_file: Optional[Path] = None):
        self.rules: List[FileRule] = []
        self.default_strategy = ProcessingStrategy.METADATA_ONLY
        
        if rules_file and rules_file.exists():
            self.load_rules(rules_file)
        else:
            self.load_default_rules()
        
        self.rules.sort(key=lambda r: r.priority, reverse=True)
    
    def load_default_rules(self):
        """加载内置默认规则"""
        default_rules = [
            # 关键文档 - 对应 FileType.CRITICAL_DOCS
            FileRule("critical_readme", ["README*", "readme*"], 
                    ProcessingStrategy.FULL_CONTENT, priority=100, max_size=256*1024),
            FileRule("critical_license", ["LICENSE*", "COPYING*", "NOTICE*"], 
                    ProcessingStrategy.FULL_CONTENT, priority=100, max_size=128*1024),
            FileRule("critical_changelog", ["CHANGELOG*", "CHANGES*"], 
                    ProcessingStrategy.SUMMARY_ONLY, priority=95, max_size=256*1024),
            FileRule("critical_contrib", ["CONTRIBUTING*", "INSTALL*", "AUTHORS*", "NEWS*", "TODO*", "ROADMAP*"], 
                    ProcessingStrategy.SUMMARY_ONLY, priority=95, max_size=256*1024),
            
            # 二进制文件 - 对应 FileType.BINARY_FILES
            FileRule("binary_archives", ["*.gz", "*.bz2", "*.xz", "*.7z", "*.rar", "*.zip", "*.tar"], 
                    ProcessingStrategy.METADATA_ONLY, priority=90, force_binary=True),
            FileRule("media_files", ["*.avi", "*.mp4", "*.mov", "*.wav", "*.mp3", "*.jpg", "*.png"],
                    ProcessingStrategy.METADATA_ONLY, priority=90, force_binary=True),
            
            # 参考文档 - 对应 FileType.REFERENCE_DOCS
            FileRule("reference_docs", ["*.md", "*.markdown", "*.rst", "*.tex", "*.html", "*.htm"],
                    ProcessingStrategy.SUMMARY_ONLY, priority=80, max_size=512*1024),
            
            # 源代码 - 对应 FileType.SOURCE_CODE
            FileRule("source_code", ["*.py", "*.c", "*.cpp", "*.h", "*.java", "*.js", "*.ts", "*.go", "*.rs"],
                    ProcessingStrategy.CODE_SKELETON, priority=70),
            
            # 文本数据 - 对应 FileType.TEXT_DATA
            FileRule("config_files", ["*.yaml", "*.yml", "*.json", "*.toml", "*.conf", "*.ini", "*.cfg"],
                    ProcessingStrategy.STRUCTURE_EXTRACT, priority=60),
            FileRule("data_files", ["*.csv", "*.tsv", "*.log", "*.dat", "*.txt"],
                    ProcessingStrategy.HEADER_WITH_STATS, priority=55),
        ]
        self.rules.extend(default_rules)
    
    def load_rules(self, rules_file: Path):
        """从YAML文件加载规则"""
        try:
            with open(rules_file, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f)
        except Exception as e:
            print(f"Warning: Could not load rules file {rules_file}: {e}", file=sys.stderr)
            print("Using built-in default rules", file=sys.stderr)
            self.load_default_rules()
            return
        
        if 'file_classifications' not in data:
            for category, patterns in data.items():
                if not patterns:
                    continue
                try:
                    strategy_map = {
                        'critical_docs': ProcessingStrategy.FULL_CONTENT,
                        'reference_docs': ProcessingStrategy.SUMMARY_ONLY,
                        'source_code': ProcessingStrategy.CODE_SKELETON,
                        'text_data': ProcessingStrategy.STRUCTURE_EXTRACT,
                        'binary_files': ProcessingStrategy.METADATA_ONLY
                    }
                    strategy = strategy_map.get(category, ProcessingStrategy.METADATA_ONLY)
                    
                    rule = FileRule(
                        name=category,
                        patterns=patterns,
                        strategy=strategy,
                        priority=100 if category == 'critical_docs' else 
                                90 if category == 'binary_files' else 50,
                        force_binary=(category == 'binary_files'),
                        comment=f"From rules file: {category}"
                    )
                    self.rules.append(rule)
                except Exception as e:
                    print(f"Warning: Error parsing rule category {category}: {e}", file=sys.stderr)
        else:
            rule_defs = data.get('file_classifications', [])
            for rule_def in rule_defs:
                try:
                    strategy_name = rule_def.get('strategy', 'metadata_only')
                    strategy = ProcessingStrategy(strategy_name)
                    patterns = rule_def.get('patterns', [])
                    if not patterns:
                        continue
                    
                    rule = FileRule(
                        name=rule_def.get('name', 'unnamed'),
                        patterns=patterns,
                        strategy=strategy,
                        priority=rule_def.get('priority', 50),
                        force_binary=rule_def.get('force_binary', False),
                        max_size=rule_def.get('max_size_kb', 0) * 1024 if rule_def.get('max_size_kb') else None,
                        comment=rule_def.get('comment')
                    )
                    self.rules.append(rule)
                except Exception as e:
                    print(f"Warning: Error parsing rule: {rule_def.get('name', 'unnamed')} - {e}", file=sys.stderr)
        
        self.rules.sort(key=lambda r: r.priority, reverse=True)
    
    def classify_file(self, filepath: Path) -> Tuple[ProcessingStrategy, bool]:
        """分类文件并返回处理策略"""
        try:
            stat_result = filepath.stat()
        except (OSError, IOError):
            return ProcessingStrategy.METADATA_ONLY, True
        
        for rule in self.rules:
            if rule.matches(filepath):
                return rule.strategy, rule.force_binary
        
        file_size = stat_result.st_size
        
        if file_size > 1024 * 1024:
            return ProcessingStrategy.METADATA_ONLY, True
        
        suffix = filepath.suffix.lower()
        
        if suffix in ['.txt', '.md', '.rst']:
            if file_size < 500 * 1024:
                return ProcessingStrategy.SUMMARY_ONLY, False
            else:
                return ProcessingStrategy.HEADER_WITH_STATS, False
        
        return ProcessingStrategy.METADATA_ONLY, False
    
    def estimate_token_usage(self, filepath: Path, strategy: ProcessingStrategy) -> int:
        """估算文件使用特定策略的token消耗"""
        try:
            file_size = filepath.stat().st_size
        except (OSError, IOError):
            return 0
        
        config = STRATEGY_CONFIGS.get(strategy, STRATEGY_CONFIGS[ProcessingStrategy.METADATA_ONLY])
        
        if strategy == ProcessingStrategy.METADATA_ONLY:
            return int(config.token_estimate * 100)
        
        if config.max_size and file_size > config.max_size:
            return STRATEGY_CONFIGS[ProcessingStrategy.METADATA_ONLY].token_estimate * 100
        
        estimated_chars = min(file_size, config.max_size or file_size)
        return int(estimated_chars * config.token_estimate)


# ==================== 上下文管理器 ====================

class ContextManager:
    """LLM上下文管理器"""
    
    def __init__(self, max_tokens: int = 128000):
        self.max_tokens = max_tokens
        self.reserved_tokens = 4000
        self.used_tokens = 0
        self.file_records: List[Dict] = []
        
    @property
    def available_tokens(self) -> int:
        return self.max_tokens - self.reserved_tokens - self.used_tokens
    
    def can_allocate(self, estimated_tokens: int) -> bool:
        return self.used_tokens + estimated_tokens <= self.available_tokens
    
    def allocate(self, estimated_tokens: int, file_record: Dict) -> bool:
        if not self.can_allocate(estimated_tokens):
            return False
        self.used_tokens += estimated_tokens
        self.file_records.append(file_record)
        return True
    
    def downgrade_strategy(self, current_strategy: ProcessingStrategy) -> ProcessingStrategy:
        strategy_hierarchy = [
            ProcessingStrategy.FULL_CONTENT,
            ProcessingStrategy.SUMMARY_ONLY,
            ProcessingStrategy.CODE_SKELETON,
            ProcessingStrategy.STRUCTURE_EXTRACT,
            ProcessingStrategy.HEADER_WITH_STATS,
            ProcessingStrategy.METADATA_ONLY,
        ]
        
        try:
            current_index = strategy_hierarchy.index(current_strategy)
            if current_index + 1 < len(strategy_hierarchy):
                return strategy_hierarchy[current_index + 1]
        except ValueError:
            pass
        
        return ProcessingStrategy.METADATA_ONLY
    
    def get_summary(self) -> Dict[str, Any]:
        return {
            "max_tokens": self.max_tokens,
            "reserved_tokens": self.reserved_tokens,
            "used_tokens": self.used_tokens,
            "available_tokens": self.available_tokens,
            "file_count": len(self.file_records),
            "token_utilization": self.used_tokens / (self.max_tokens - self.reserved_tokens),
        }


# ==================== 格式转换器（基础版） ====================

class FormatConverter:
    """格式转换器（基础版）"""
    
    @staticmethod
    def convert(digest_data: Dict, format: str, mode: str = None) -> str:
        """转换为指定格式"""
        # 优先处理 sort 模式 - 强制使用 ls -l 格式（与原始代码一致）
        if mode == "sort" or digest_data.get('metadata', {}).get('output_mode') == "sort":
            return FormatConverter._to_sort_format(digest_data)
        
        # 其他模式原有逻辑
        if format == "json":
            return json.dumps(digest_data, indent=2, ensure_ascii=False)
        elif format == "yaml":
            return FormatConverter._to_yaml(digest_data)
        elif format == "markdown" or format == "md":
            return FormatConverter._to_markdown_basic(digest_data)
        elif format == "html":
            return FormatConverter._to_html_basic(digest_data)
        elif format == "toml":
            return FormatConverter._to_toml(digest_data)
        elif format == "txt" or format == "text":
            return FormatConverter._to_plaintext(digest_data)
        else:
            raise ValueError(f"Unsupported format: {format}")
    
    @staticmethod
    def _to_yaml(digest_data: Dict) -> str:
        """转换为YAML格式"""
        if not YAML_AVAILABLE:
            return json.dumps(digest_data, indent=2, ensure_ascii=False)
        try:
            return yaml.dump(digest_data, allow_unicode=True, default_flow_style=False)
        except Exception:
            return json.dumps(digest_data, indent=2, ensure_ascii=False)
    
    @staticmethod
    def _to_markdown_basic(digest_data: Dict) -> str:
        """基础Markdown转换"""
        lines = ["# Directory Digest Report", ""]
        metadata = digest_data.get('metadata', {})
        if metadata:
            lines.append("## Metadata")
            lines.append("")
            for key, value in metadata.items():
                if isinstance(value, dict):
                    lines.append(f"- **{key}**:")
                    for k, v in value.items():
                        lines.append(f"  - {k}: {v}")
                else:
                    lines.append(f"- **{key}**: {value}")
            lines.append("")
        lines.append("## Raw Data")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(digest_data, indent=2, ensure_ascii=False))
        lines.append("```")
        return '\n'.join(lines)
    
    @staticmethod
    def _to_html_basic(digest_data: Dict) -> str:
        """基础HTML转换"""
        md_content = FormatConverter._to_markdown_basic(digest_data)
        return f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>Directory Digest Report</title>
    <style>
        body {{ font-family: Arial, sans-serif; margin: 40px; line-height: 1.6; }}
        h1, h2, h3 {{ color: #333; }}
        code {{ background-color: #f4f4f4; padding: 2px 4px; border-radius: 3px; }}
        pre {{ background-color: #f8f8f8; padding: 10px; border-radius: 5px; overflow: auto; }}
    </style>
</head>
<body>
<pre>{md_content}</pre>
</body>
</html>"""
    
    @staticmethod
    def _to_toml(digest_data: Dict) -> str:
        """转换为TOML格式（简化版）"""
        return f"# TOML format not fully implemented, using JSON representation\n{json.dumps(digest_data, indent=2, ensure_ascii=False)}"
    
    @staticmethod
    def _to_plaintext(digest_data: Dict) -> str:
        """转换为纯文本"""
        return json.dumps(digest_data, indent=2, ensure_ascii=False)
    
    @staticmethod
    def _to_sort_format(digest_data: Dict) -> str:
        """Generate ls -l style output for sort mode (与原始代码一致)"""
        lines = []
        root_dir = digest_data.get('metadata', {}).get('root_directory', '.')
        
        lines.append(f"Directory Digest: {root_dir}")
        lines.append(f"Generated: {digest_data.get('metadata', {}).get('generated_at', 'unknown')}")
        lines.append("")
        
        # 类型映射 (与实际的6种分类一致)
        type_names = {
            'critical_docs': ('Critical Docs', 'C'),
            'reference_docs': ('Reference Docs', 'R'),
            'source_code': ('Source Code', 'S'),
            'text_data': ('Text Data', 'T'),
            'binary_files': ('Binary Files', 'B'),
            'unknown': ('Unknown', 'U')  # 修正：使用 'U' 而不是 '?'
        }
        
        listings = digest_data.get('file_listings', {})
        
        for type_key, (type_name, type_char) in type_names.items():
            if type_key not in listings or not listings[type_key]:
                continue
            
            files = listings[type_key]
            total_size = sum(f.get('size', 0) for f in files)
            
            lines.append(f"{type_name} ({len(files)} files, {FormatConverter._format_size(total_size)})")
            lines.append("-" * 80)
            
            # 类 ls -l 格式：类型 大小 日期 路径
            for f in files[:100]:  # 限制显示数量
                path = f.get('path', 'unknown')
                # 优先使用size_formatted，否则计算
                if 'size_formatted' in f:
                    size = f['size_formatted']
                else:
                    size = FormatConverter._format_size(f.get('size', 0))
                modified = f.get('modified', 'unknown')
                
                # 格式化日期 (与原始代码一致)
                if modified != 'unknown':
                    try:
                        dt = datetime.fromisoformat(modified)
                        date_str = dt.strftime("%b %d %H:%M")
                    except:
                        date_str = modified[:16] if len(modified) > 16 else modified
                else:
                    date_str = "unknown"
                
                # 格式：类型 大小 日期 路径
                lines.append(f"{type_char}  {size:>10}  {date_str:>12}  {path}")
            
            if len(files) > 100:
                lines.append(f"... ({len(files) - 100} more files)")
            
            lines.append("")
        
        # 统计摘要 - 添加 unknown 统计
        stats = digest_data.get('metadata', {}).get('statistics', {})
        lines.append("Summary:")
        lines.append(f"  Total: {stats.get('total_files', 0)} files, "
                    f"{FormatConverter._format_size(stats.get('total_size', 0))}")
        lines.append(f"  Critical Docs: {stats.get('critical_docs', 0)}")
        lines.append(f"  Reference Docs: {stats.get('reference_docs', 0)}")
        lines.append(f"  Source Code: {stats.get('source_code', 0)}")
        lines.append(f"  Text Data: {stats.get('text_data', 0)}")
        lines.append(f"  Binary Files: {stats.get('binary_files', 0)}")
        lines.append(f"  Unknown: {stats.get('unknown', 0)}")
        lines.append(f"  Skipped (>limit): {stats.get('skipped_large_files', 0)}")
        
        return '\n'.join(lines)
    
    @staticmethod
    def _format_size(size_bytes: int) -> str:
        """格式化文件大小"""
        if size_bytes == 0:
            return "0 B"
        import math
        units = ['B', 'KB', 'MB', 'GB', 'TB']
        i = int(math.floor(math.log(size_bytes, 1024)))
        p = math.pow(1024, i)
        s = round(size_bytes / p, 2)
        return f"{s} {units[i]}"


# ==================== 目录摘要生成器（基础版） ====================

class DirectoryDigestBase:
    """目录摘要生成器（基础版 - 包含输入输出和文件系统分析）"""
    
    def __init__(self, 
                 root_path: Union[str, Path],
                 config: Optional[Dict] = None):
        """
        初始化摘要生成器
        
        Args:
            root_path: 根目录路径
            config: 配置字典
        """
        self.root = Path(root_path).resolve()
        self.config = config or {}
        
        self.max_file_size = self.config.get('max_file_size', 10 * 1024 * 1024 * 1024)
        self.ignore_patterns = self.config.get('ignore_patterns', [
            '*.pyc', '*.pyo', '*.so', '*.dll', '__pycache__', 
            '.git', '.svn', '.hg', '.DS_Store', '*.swp', '*.swo'
        ])
        
        self.rules_file = self.config.get('rules_file')
        self.context_size = self.config.get('context_size', 128000)
        self.rule_engine = RuleEngine(self.rules_file)
        self.context_manager = ContextManager(self.context_size)
        
        self.file_type_detector = FileTypeDetector()
        
        # 并行处理配置
        self.use_parallel = self.config.get('use_parallel', False)
        self.max_workers = self.config.get('max_workers', os.cpu_count() or 4)
        
        self.structure: Optional[DirectoryStructure] = None
        self.stats = {
            'total_files': 0,
            'critical_docs': 0,
            'reference_docs': 0,
            'source_code': 0,
            'text_data': 0,
            'binary_files': 0,
            'unknown': 0,  # 添加 unknown 统计
            'skipped_large_files': 0,
            'skipped_by_context': 0,
            'total_size': 0,
            'processing_time': 0
        }
    
    def _should_ignore(self, path: Path) -> bool:
        """检查路径是否应该被忽略"""
        for pattern in self.ignore_patterns:
            if fnmatch.fnmatch(path.name, pattern):
                return True
            if pattern.startswith('*') and path.name.endswith(pattern[1:]):
                return True
        return False
    
    def _build_directory_structure(self, path: Path) -> DirectoryStructure:
        """递归构建目录结构"""
        structure = DirectoryStructure(path=path)
        
        try:
            for item in path.iterdir():
                if self._should_ignore(item):
                    continue
                
                if item.is_dir():
                    sub_structure = self._build_directory_structure(item)
                    structure.subdirectories[item.name] = sub_structure
                else:
                    structure.files.append(FileDigest(
                        metadata=FileMetadata(
                            path=item,
                            size=item.stat().st_size,
                            modified_time=datetime.fromtimestamp(item.stat().st_mtime),
                            created_time=datetime.fromtimestamp(item.stat().st_ctime),
                            file_type=FileType.UNKNOWN,
                            mime_type=mimetypes.guess_type(str(item))[0]
                        )
                    ))
                    self.stats['total_files'] += 1
                    self.stats['total_size'] += item.stat().st_size
                    
        except PermissionError:
            print(f"Warning: Permission denied accessing directory {path}", file=sys.stderr)
        
        return structure
    
    def _read_file_content(self, filepath: Path) -> Optional[str]:
        """统一读取文件内容，处理编码问题"""
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
                        result = chardet.detect(raw_content)
                        encoding = result['encoding'] if result['encoding'] else 'latin-1'
                        return raw_content.decode(encoding, errors='ignore')
            except Exception:
                return None
    
    def _calculate_hashes(self, file_digest: FileDigest):
        """计算文件的哈希值（流式处理）"""
        filepath = file_digest.metadata.path
        
        try:
            md5_hash = hashlib.md5()
            sha256_hash = hashlib.sha256()
            
            with open(filepath, 'rb') as f:
                for chunk in iter(lambda: f.read(65536), b''):
                    md5_hash.update(chunk)
                    sha256_hash.update(chunk)
            
            file_digest.metadata.md5_hash = md5_hash.hexdigest()
            file_digest.metadata.sha256_hash = sha256_hash.hexdigest()
            
        except (OSError, IOError) as e:
            print(f"Warning: Could not read file for hash calculation: {filepath} - {e}", file=sys.stderr)
            file_digest.metadata.md5_hash = "read_error"
            file_digest.metadata.sha256_hash = "read_error"
        except Exception as e:
            print(f"Warning: Hash calculation failed for {filepath}: {e}", file=sys.stderr)
            file_digest.metadata.md5_hash = "hash_error"
            file_digest.metadata.sha256_hash = "hash_error"
    
    def _collect_all_files_flat(self) -> List[FileDigest]:
        """扁平化收集所有文件"""
        all_files = []
        
        def collect(node: DirectoryStructure):
            all_files.extend(node.files)
            for subdir in node.subdirectories.values():
                collect(subdir)
        
        if self.structure:
            collect(self.structure)
        return all_files
    
    def create_basic_digest(self, mode: str = "framework") -> Dict:
        """
        创建基础目录摘要（仅包含文件系统分析和元数据）
        
        Args:
            mode: 输出模式
        """
        import time
        start_time = time.time()
        
        self.structure = self._build_directory_structure(self.root)
        
        for file_digest in self._collect_all_files_flat():
            filepath = file_digest.metadata.path
            
            file_type = self.file_type_detector.detect(filepath)
            file_digest.metadata.file_type = file_type
            
            type_stat_key = file_type.value
            if type_stat_key in self.stats:
                self.stats[type_stat_key] += 1
            
            self._calculate_hashes(file_digest)
        
        self.stats['processing_time'] = time.time() - start_time
        
        return self._generate_basic_output(mode)
    
    def _generate_basic_output(self, mode: str) -> Dict:
        """生成基础输出"""
        if not self.structure:
            return {}
        
        return {
            "metadata": {
                "generated_at": datetime.now().isoformat(),
                "root_directory": str(self.root),
                "output_mode": mode,
                "statistics": self.stats,
                "context_usage": self.context_manager.get_summary(),
            },
            "structure": self.structure.to_dict(mode)
        }
    
    def _generate_sort_output(self) -> Dict:
        """
        生成分类排序输出（完整版，与原始代码逻辑一致）
        包含文件类型分类、大小分类、扩展名统计和建议
        """
        all_files = self._collect_all_files_flat()
        
        # 按类型分组，同时保留完整元数据
        by_type = {
            FileType.CRITICAL_DOCS.value: [],
            FileType.REFERENCE_DOCS.value: [],
            FileType.SOURCE_CODE.value: [],
            FileType.TEXT_DATA.value: [],
            FileType.BINARY_FILES.value: [],
            FileType.UNKNOWN.value: []
        }
        
        # 按大小分组
        large_files = []      # > 1MB
        medium_files = []     # 100KB - 1MB
        small_files = []      # < 100KB
        
        for f in all_files:
            file_type = f.metadata.file_type.value
            file_info = {
                'path': str(f.metadata.path.relative_to(self.root)),
                'size': f.metadata.size,
                'size_formatted': self._format_size(f.metadata.size),
                'modified': f.metadata.modified_time.isoformat() if f.metadata.modified_time else 'unknown',
                'type': file_type,
                'is_binary': file_type == FileType.BINARY_FILES.value
            }
            
            if file_type in by_type:
                by_type[file_type].append(file_info)
            else:
                by_type[FileType.UNKNOWN.value].append(file_info)
            
            # 按大小分组
            size = f.metadata.size
            if size > 1024 * 1024:
                large_files.append(file_info)
            elif size > 100 * 1024:
                medium_files.append(file_info)
            else:
                small_files.append(file_info)
        
        # 构建报告 (与原始代码一致)
        sort_report = {
            "metadata": {
                "generated_at": datetime.now().isoformat(),
                "root_directory": str(self.root),
                "output_mode": "sort",
                "statistics": self.stats,
                "context_usage": self.context_manager.get_summary()
            },
            "classification": {},
            "by_size": {
                "large_files": large_files,
                "medium_files": medium_files,
                "small_files": small_files
            },
            "file_listings": {
                k: sorted(v, key=lambda x: x['path']) 
                for k, v in by_type.items() if v
            }
        }
        
        # 为每种类型生成详细信息（包括扩展名统计）
        for type_name, files in by_type.items():
            if not files:
                continue
                
            # 按扩展名分组
            by_ext = {}
            for f in files:
                path = f['path']
                ext = Path(path).suffix.lower() or "(no extension)"
                if ext not in by_ext:
                    by_ext[ext] = []
                by_ext[ext].append(path)
            
            # 计算总大小
            total_size = sum(f['size'] for f in files)
            
            sort_report["classification"][type_name] = {
                "count": len(files),
                "total_size_bytes": total_size,
                "total_size_formatted": self._format_size(total_size),
                "extensions": {
                    ext: {
                        "count": len(paths),
                        "files": sorted(paths)[:10],  # 只显示前10个
                        "truncated": len(paths) > 10,
                        "total_count": len(paths)
                    }
                    for ext, paths in sorted(by_ext.items(), key=lambda x: len(x[1]), reverse=True)
                }
            }
        
        # 添加建议 (与原始代码一致)
        recommendations = []
        if large_files:
            recommendations.append(
                f"Found {len(large_files)} large files (>1MB). "
                f"In 'full' mode, use --max-content-size to limit full content output."
            )

        if by_type.get(FileType.UNKNOWN.value, []):
            count = len(by_type[FileType.UNKNOWN.value])
            if count > 5:
                recommendations.append(
                    f"Found {count} unknown type files. Consider reviewing or adding to ignore patterns."
                )
        
        sort_report["recommendations"] = recommendations
        
        return sort_report
    
    @staticmethod
    def _format_size(size_bytes: int) -> str:
        """格式化文件大小"""
        if size_bytes == 0:
            return "0 B"
        import math
        units = ['B', 'KB', 'MB', 'GB', 'TB']
        i = int(math.floor(math.log(size_bytes, 1024)))
        p = math.pow(1024, i)
        s = round(size_bytes / p, 2)
        return f"{s} {units[i]}"
    
    def save_output(self, output: Dict, format: str = "json", output_path: Optional[Path] = None, mode: str = None):
        """保存输出到文件"""
        if not output_path:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            ext = format.lower()
            if ext == "markdown":
                ext = "md"
            output_path = self.root / f"directory_digest_{timestamp}.{ext}"
        
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        content = FormatConverter.convert(output, format, mode)
        
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(content)
        
        print(f"Digest saved to: {output_path}")
        return output_path


def parse_context_size(size_str: str) -> int:
    """解析上下文大小字符串（如 '128k', '64k' 等）"""
    size_str = size_str.lower().strip()
    
    if size_str.endswith('k'):
        multiplier = 1000
        size_str = size_str[:-1]
    else:
        multiplier = 1
    
    try:
        base_value = float(size_str) if '.' in size_str else int(size_str)
        return int(base_value * multiplier)
    except ValueError:
        print(f"Warning: Could not parse context size '{size_str}', using default 128000", file=sys.stderr)
        return 128000


# ==================== 公共 API 导出 ====================

__all__ = [
    # 枚举
    'ProcessingStrategy',
    'FileType',
    'OutputFormats',
    
    # 数据类
    'StrategyConfig',
    'FileRule',
    'FileMetadata',
    'FileDigest',
    'DirectoryStructure',
    
    # 核心类
    'FileTypeDetector',
    'RuleEngine',
    'ContextManager',
    'FormatConverter',
    'DirectoryDigestBase',
    
    # 配置
    'STRATEGY_CONFIGS',
    
    # 工具函数
    'parse_context_size',
]
