#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Directory Digest Refactor - Refactored main entry point
Uses tools/_directory_digest module to implement full functionality
"""

import os
import sys
import argparse
import mimetypes
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple
from datetime import datetime

# Import base modules
from .base import (
    ProcessingStrategy,
    FileType,
    OutputFormats,
    FileMetadata,
    FileDigest,
    DirectoryStructure,
    FileTypeDetector,
    RuleEngine,
    ContextManager,
    FormatConverter,
    DirectoryDigestBase,
    STRATEGY_CONFIGS,
    FileClassification,
    OutputMode,
    InitialEmbeddingStrategy,
)

# Import processor registry
from .processors import create_default_registry


# 尝试导入高级分析器（如果可用）
try:
    from .analyzers.semantics.base import (
        HumanReadableSummary,
        SourceCodeAnalysis,
        HumanReadableSummarizer,
        SourceCodeAnalyzer,
        SmartTextProcessor,
    )
    SEMANTICS_AVAILABLE = True
except ImportError:
    SEMANTICS_AVAILABLE = False
    # 定义简化的后备类
    from dataclasses import dataclass, field
    
    @dataclass
    class HumanReadableSummary:
        title: Optional[str] = None
        line_count: int = 0
        word_count: int = 0
        character_count: int = 0
        summary: Optional[str] = None
        first_lines: List[str] = field(default_factory=list)
        
        def to_dict(self) -> Dict:
            return {
                "title": self.title,
                "line_count": self.line_count,
                "word_count": self.word_count,
                "character_count": self.character_count,
                "summary": self.summary,
                "first_lines": self.first_lines
            }
    
    @dataclass
    class SourceCodeAnalysis:
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


class DirectoryDigest(DirectoryDigestBase):
    """完整的目录摘要生成器 - 继承自基础版并扩展功能"""
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        
        # 从配置中获取debug标志
        self.debug = self.config.get('debug', False)
        
        # 初始化处理器注册表（新的核心组件）
        # Note: We'll configure it properly in create_digest based on the mode
        self.processor_registry = create_default_registry(
            rule_engine=self.rule_engine,
            context_manager=self.context_manager,
            stats=self.stats,
            config=self.config.copy()
        )
        
        # 保留高级分析器组件（如果语义分析可用）
        if SEMANTICS_AVAILABLE:
            self.human_summarizer = HumanReadableSummarizer()
            self.source_analyzer = SourceCodeAnalyzer()
        else:
            self.human_summarizer = None
            self.source_analyzer = None
            
        # 添加两阶段策略相关的状态
        self.final_strategies = {}  # 存储最终确定的策略
    
    def _classify_file(self, filepath: Path) -> FileClassification:
        """
        统一文件分类方法
        只在分类阶段确定文件类型和处理策略，后续阶段使用此分类结果
        
        Args:
            filepath: 文件路径
            
        Returns:
            FileClassification: 完整的分类结果
        """
        try:
            # 1. 使用规则引擎进行完整分类
            if self.rule_engine:
                strategy, force_binary = self.rule_engine.classify_file(filepath)
                estimated_tokens = self.rule_engine.estimate_token_usage(filepath, strategy)
                rule_name = "rule_engine"
            else:
                # 无规则引擎时的默认分类
                strategy, force_binary = self._default_classify(filepath)
                estimated_tokens = self._estimate_tokens(filepath, strategy)
                rule_name = "default_classification"
            
            # 2. 根据分类结果确定文件类型（不再重复判断）
            file_type = self._determine_file_type_from_classification(
                filepath, strategy, force_binary
            )
            
            return FileClassification(
                file_type=file_type,
                strategy=strategy,
                force_binary=force_binary,
                estimated_tokens=estimated_tokens,
                rule_name=rule_name
            )
            
        except Exception as e:
            # 分类失败时返回默认分类
            import sys
            print(f"Warning: Classification failed for {filepath}: {e}", file=sys.stderr)
            return FileClassification(
                file_type=FileType.UNKNOWN,
                strategy=ProcessingStrategy.METADATA_ONLY,
                force_binary=True,
                estimated_tokens=100,
                rule_name="error_fallback"
            )
    
    def _determine_file_type_from_classification(self, filepath: Path, 
                                                strategy: ProcessingStrategy,
                                                force_binary: bool) -> FileType:
        """
        根据分类结果确定文件类型（不再重复判断）
        
        Args:
            filepath: 文件路径（仅用于日志）
            strategy: 处理策略
            force_binary: 是否强制二进制
            
        Returns:
            FileType: 确定的文件类型
        """
        # 如果强制二进制，直接返回BINARY_FILES
        if force_binary:
            return FileType.BINARY_FILES
        
        # 根据策略类型映射到文件类型（不再检查文件名或内容）
        if strategy == ProcessingStrategy.METADATA_ONLY:
            return FileType.BINARY_FILES
        elif strategy in [ProcessingStrategy.FULL_CONTENT, ProcessingStrategy.SUMMARY_ONLY]:
            # 检查是否为关键文档（这部分逻辑保留，但只在分类阶段执行一次）
            filename = filepath.name.lower()
            critical_patterns = ['readme', 'license', 'copying', 'notice', 'changelog', 'changes', 
                                'contributing', 'install', 'authors', 'news', 'todo', 'roadmap']
            if any(pattern in filename for pattern in critical_patterns):
                return FileType.CRITICAL_DOCS
            else:
                return FileType.REFERENCE_DOCS
        elif strategy == ProcessingStrategy.CODE_SKELETON:
            return FileType.SOURCE_CODE
        elif strategy in [ProcessingStrategy.STRUCTURE_EXTRACT, ProcessingStrategy.HEADER_WITH_STATS]:
            return FileType.TEXT_DATA
        else:
            return FileType.UNKNOWN
    
    def _default_classify(self, filepath: Path) -> Tuple[ProcessingStrategy, bool]:
        """默认文件分类（当没有规则引擎时）- 提取自FileProcessorRegistry"""
        try:
            suffix = filepath.suffix.lower()
            size = filepath.stat().st_size
        except (OSError, IOError):
            return ProcessingStrategy.METADATA_ONLY, True
        
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
    
    def _estimate_tokens(self, filepath: Path, strategy: ProcessingStrategy) -> int:
        """估算Token消耗（当没有规则引擎时）"""
        config = STRATEGY_CONFIGS.get(strategy, STRATEGY_CONFIGS[ProcessingStrategy.METADATA_ONLY])
        
        if strategy == ProcessingStrategy.METADATA_ONLY:
            return int(config.token_estimate * 100)
        
        try:
            file_size = filepath.stat().st_size
        except (OSError, IOError):
            file_size = 0
            
        if config.max_size and file_size > config.max_size:
            return STRATEGY_CONFIGS[ProcessingStrategy.METADATA_ONLY].token_estimate * 100
        
        estimated_chars = min(file_size, config.max_size or file_size)
        return int(estimated_chars * config.token_estimate)
    
    @staticmethod
    def _format_size(size_bytes: int) -> str:
        """格式化文件大小（与原始代码一致）"""
        if size_bytes == 0:
            return "0 B"
        import math
        units = ['B', 'KB', 'MB', 'GB', 'TB']
        i = int(math.floor(math.log(size_bytes, 1024)))
        p = math.pow(1024, i)
        s = round(size_bytes / p, 2)
        return f"{s} {units[i]}"
    
    def create_digest(self, mode: str = "framework") -> Dict:
        """
        创建完整目录摘要 - 使用两阶段策略
        
        Args:
            mode: 输出模式，"framework"、"full" 或 "sort"
        """
        import time
        start_time = time.time()
        
        # 输出debug信息
        if self.debug:
            import sys
            print(f"[DEBUG] create_digest called with mode: {mode}", file=sys.stderr)
            print(f"[DEBUG] Config: {self.config}", file=sys.stderr)
            print(f"[DEBUG] Root: {self.root}", file=sys.stderr)
        
        # 重新配置处理器注册表以适应当前模式
        processor_config = self.config.copy()
        # 在full模式下增加全文大小限制
        if mode == "full":
            # full模式下增加全文大小限制到100MB或根据配置
            processor_config['max_full_content_size'] = processor_config.get('max_full_content_size', 100 * 1024 * 1024)
        
        # 重新创建处理器注册表
        self.processor_registry = create_default_registry(
            rule_engine=self.rule_engine,
            context_manager=self.context_manager,
            stats=self.stats,
            config=processor_config
        )
        
        # 构建目录结构
        self.structure = self._build_directory_structure(self.root)
        
        # 收集所有文件
        all_files = self._collect_all_files_flat()
        
        # 第一阶段：制定初始策略
        initial_strategies = {}
        for file_digest in all_files:
            # 使用规则引擎进行文件分类（优先于简单的扩展名检测）
            classification = self._classify_file(file_digest.metadata.path)
            file_type = classification.file_type
            file_digest.metadata.file_type = file_type
            file_digest.metadata.force_binary = classification.force_binary
            
            # 将字符串模式转换为枚举
            try:
                output_mode_enum = OutputMode(mode)
            except ValueError:
                # 如果mode不是有效的OutputMode，使用FRAMEWORK作为默认
                output_mode_enum = OutputMode.FRAMEWORK
            
            # 获取初始策略，优先使用规则引擎指定的策略
            # 如果规则引擎指定了非METADATA_ONLY策略，则直接使用
            if classification.strategy != ProcessingStrategy.METADATA_ONLY:
                initial_strategy = classification.strategy
            else:
                # 否则根据文件类型和输出模式获取默认策略
                initial_strategy = InitialEmbeddingStrategy.get_initial_strategy(
                    output_mode_enum, file_type
                )
            
            # 根据输出模式进行最终调整
            if mode == "sort":
                # sort模式：强制使用METADATA_ONLY
                initial_strategy = ProcessingStrategy.METADATA_ONLY
            
            # 将策略写入元数据
            file_digest.metadata.processing_strategy = initial_strategy
            
            # 存储策略使用文件路径作为键（可哈希）
            file_path = str(file_digest.metadata.path)
            initial_strategies[file_path] = (file_digest, initial_strategy)
            
            # 更新统计和计算哈希
            type_stat_key = file_type.value
            if type_stat_key in self.stats:
                self.stats[type_stat_key] += 1
            self._calculate_hashes(file_digest)
        
        # 第二阶段：根据token约束调整策略
        adjusted_strategies = self._adjust_strategies_by_token_limit(
            initial_strategies, mode
        )
        
        # 存储最终策略
        self.final_strategies = adjusted_strategies
        
        # 第三阶段：执行最终策略
        for file_path, (file_digest, final_strategy) in adjusted_strategies.items():
            # 更新元数据中的策略
            file_digest.metadata.processing_strategy = final_strategy
            
            # 执行处理
            self._process_with_final_strategy(file_digest, final_strategy, mode)
        
        # 更新处理时间
        self.stats['processing_time'] = time.time() - start_time
        
        # 根据模式生成输出
        if mode == "sort":
            return self._generate_sort_output_unified(all_files)
        else:
            return self._generate_output(mode)
    
    def _adjust_strategies_by_token_limit(self, initial_strategies: dict, mode: str) -> dict:
        """
        根据token资源约束调整策略
        
        Args:
            initial_strategies: 初始策略映射（file_path -> (file_digest, strategy)）
            mode: 输出模式
            
        Returns:
            调整后的策略映射（file_path -> (file_digest, strategy)）
        """
        # 在full模式下，不应该降级策略，因为用户明确要求完整内容
        if mode == "full":
            return initial_strategies
        
        # 如果没有上下文管理器，直接使用初始策略
        if not self.context_manager:
            return initial_strategies
        
        # 估算总token消耗
        total_tokens = 0
        token_estimates = {}
        
        for file_path, (file_digest, strategy) in initial_strategies.items():
            if self.rule_engine:
                estimated = self.rule_engine.estimate_token_usage(
                    file_digest.metadata.path, strategy
                )
            else:
                estimated = self._estimate_tokens(
                    file_digest.metadata.path, strategy
                )
            token_estimates[file_path] = estimated
            total_tokens += estimated
        
        # 检查是否超出限制
        available_tokens = self.context_manager.available_tokens
        if total_tokens <= available_tokens:
            return initial_strategies
        
        # 如果超出限制，按优先级降级策略
        import sys
        print(f"Warning: Estimated tokens ({total_tokens:,}) exceed available ({available_tokens:,}). Adjusting strategies...", 
              file=sys.stderr)
        
        # 准备调整：按优先级排序文件
        files_by_priority = []
        for file_path, (file_digest, strategy) in initial_strategies.items():
            file_type = file_digest.metadata.file_type
            priority = InitialEmbeddingStrategy.get_priority(file_type)
            files_by_priority.append((priority, file_type, file_digest, strategy, file_path))
        
        # 按优先级排序（优先级低的先调整）
        files_by_priority.sort(key=lambda x: (-x[0], x[1].value))
        
        adjusted_strategies = initial_strategies.copy()
        
        # 逐步降级策略直到满足token限制
        while total_tokens > available_tokens and files_by_priority:
            priority, file_type, file_digest, current_strategy, file_path = files_by_priority.pop(0)
            
            current_estimate = token_estimates[file_path]
            
            # 获取策略降级序列
            strategy_hierarchy = InitialEmbeddingStrategy.get_strategy_hierarchy(file_type)
            
            try:
                current_index = strategy_hierarchy.index(current_strategy)
                if current_index + 1 < len(strategy_hierarchy):
                    # 降级到下一个策略
                    new_strategy = strategy_hierarchy[current_index + 1]
                    
                    # 重新估算token
                    if self.rule_engine:
                        new_estimate = self.rule_engine.estimate_token_usage(
                            file_digest.metadata.path, new_strategy
                        )
                    else:
                        new_estimate = self._estimate_tokens(
                            file_digest.metadata.path, new_strategy
                        )
                    
                    # 更新总token数
                    total_tokens = total_tokens - current_estimate + new_estimate
                    
                    # 更新策略和估算
                    adjusted_strategies[file_path] = (file_digest, new_strategy)
                    token_estimates[file_path] = new_estimate
                    
                    print(f"  - Downgraded {file_digest.metadata.path.name} from {current_strategy.value} to {new_strategy.value}", 
                          file=sys.stderr)
                else:
                    # 已经是最后一级，无法再降级
                    continue
                    
            except ValueError:
                # 当前策略不在降级序列中（如METADATA_ONLY）
                continue
        
        if total_tokens > available_tokens:
            print(f"Warning: Could not fit within token limit even after downgrading. "
                  f"Final estimate: {total_tokens:,} tokens, available: {available_tokens:,}", 
                  file=sys.stderr)
        
        return adjusted_strategies
    
    def _process_with_final_strategy(self, file_digest: FileDigest, 
                                   final_strategy: ProcessingStrategy, 
                                   mode: str):
        """
        使用最终确定的策略处理文件 - 增强错误处理
        """
        try:
            filepath = file_digest.metadata.path
            
            # 添加debug输出
            if self.debug:
                import sys
                print(f"[DEBUG] Processing file: {filepath}", file=sys.stderr)
                print(f"[DEBUG]   Size: {file_digest.metadata.size} bytes", file=sys.stderr)
                print(f"[DEBUG]   Strategy: {final_strategy}", file=sys.stderr)
                print(f"[DEBUG]   File type: {file_digest.metadata.file_type}", file=sys.stderr)
                print(f"[DEBUG]   Force binary: {file_digest.metadata.force_binary}", file=sys.stderr)
            
            # 对于METADATA_ONLY策略，只处理元数据
            if final_strategy == ProcessingStrategy.METADATA_ONLY:
                if self.debug:
                    import sys
                    print(f"[DEBUG]   METADATA_ONLY strategy, skipping content processing", file=sys.stderr)
                # 哈希值已在第一阶段计算
                file_digest.actual_strategy = final_strategy.value
                return
            
            # 使用处理器注册表处理文件，传入确定的策略
            success = self.processor_registry.process_file(file_digest, mode, strategy=final_strategy)
            
            if success:
                # 记录实际使用的策略
                file_digest.actual_strategy = final_strategy.value
                
                # 检查是否成功生成了摘要
                if (final_strategy == ProcessingStrategy.HEADER_WITH_STATS and 
                    file_digest.human_readable_summary is None):
                    # HEADER_WITH_STATS策略必须有摘要，如果缺失则尝试降级
                    if self.debug:
                        print(f"[DEBUG]   WARNING: HEADER_WITH_STATS strategy but no summary generated", file=sys.stderr)
                    
                    # 尝试作为简单文本处理
                    try:
                        content = self._read_file_content(filepath)
                        if content:
                            simple_summary = HumanReadableSummary(
                                title=filepath.name,
                                line_count=len(content.split('\n')),
                                character_count=len(content),
                                first_lines=content.split('\n')[:10],
                                summary=f"Basic text file: {len(content)} characters"
                            )
                            file_digest.human_readable_summary = simple_summary
                    except Exception:
                        pass
                
                # 添加debug输出
                if self.debug:
                    import sys
                    print(f"[DEBUG]   Processor registry returned success", file=sys.stderr)
                    print(f"[DEBUG]   Full content set: {file_digest.full_content is not None}", file=sys.stderr)
                    if file_digest.full_content:
                        print(f"[DEBUG]   Full content length: {len(file_digest.full_content)} chars", file=sys.stderr)
                    else:
                        print(f"[DEBUG]   Full content is None", file=sys.stderr)
                    print(f"[DEBUG]   human_readable_summary set: {file_digest.human_readable_summary is not None}", file=sys.stderr)
                
            else:
                # 处理失败，作为二进制处理
                file_digest.metadata.file_type = FileType.BINARY_FILES
                file_digest.actual_strategy = "PROCESS_FAILED"
                
                if self.debug:
                    import sys
                    print(f"[DEBUG]   Processor registry returned failure", file=sys.stderr)
            
        except Exception as e:
            import sys
            print(f"Warning: Error processing file {file_digest.metadata.path}: {e}", file=sys.stderr)
            if self.debug:
                import traceback
                print(f"[DEBUG]   Exception details:", file=sys.stderr)
                traceback.print_exc(file=sys.stderr)
            # 出错时作为二进制文件处理
            file_digest.metadata.file_type = FileType.BINARY_FILES
            file_digest.actual_strategy = "ERROR"
    
    def _process_single_file_with_classification(self, file_digest: FileDigest,
                                                classification: FileClassification,
                                                mode: str):
        """使用分类结果处理单个文件"""
        try:
            filepath = file_digest.metadata.path
            
            # 检查文件大小限制
            if file_digest.metadata.size > self.max_file_size:
                self.stats['skipped_large_files'] += 1
                self._process_as_binary(file_digest, mode)
                self.stats['binary_files'] += 1
                return
            
            # 对于 FULL_CONTENT 策略，总是尝试处理内容
            # 对于其他策略，检查Token限制（仅在非full模式时）
            if self.context_manager and mode != "full":
                if not self._check_and_allocate_context(classification, file_digest):
                    self.stats['skipped_by_context'] += 1
                    return
            
            # 使用处理器注册表动态获取处理器
            processor = self.processor_registry.get_processor(file_digest)
            
            if processor and not classification.force_binary:
                # 读取文件内容
                content = self._read_file_content(filepath)
                if content:
                    # 使用分类中的策略进行处理
                    processor.process(file_digest, content, mode, classification.strategy)
                    return
                else:
                    self._process_as_binary(file_digest, mode)
                    self.stats['binary_files'] += 1
            else:
                self._process_as_binary(file_digest, mode)
                self.stats['binary_files'] += 1
                
        except Exception as e:
            import sys
            print(f"Warning: Error processing file {file_digest.metadata.path}: {e}", file=sys.stderr)
            try:
                self._process_as_binary(file_digest, mode)
                self.stats['binary_files'] += 1
            except:
                pass
    
    
    def _read_file_content(self, filepath: Path) -> Optional[str]:
        """读取文件内容"""
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
    
    def _process_as_binary(self, file_digest: FileDigest, mode: str):
        """处理为二进制文件"""
        # 哈希值已在分类阶段计算
        file_digest.metadata.file_type = FileType.BINARY_FILES
    
    def _check_and_allocate_context(self, classification: FileClassification,
                                   file_digest: FileDigest) -> bool:
        """检查并分配上下文Token"""
        if not self.context_manager:
            return True
        
        if not self.context_manager.can_allocate(classification.estimated_tokens):
            return False
        
        file_record = {
            "path": str(file_digest.metadata.path),
            "strategy": classification.strategy.value,
            "estimated_tokens": classification.estimated_tokens,
            "size": file_digest.metadata.size,
        }
        
        return self.context_manager.allocate(classification.estimated_tokens, file_record)
    
    def _generate_sort_output_unified(self, all_files) -> Dict:
        """
        生成分类排序输出（使用所有文件）
        
        Args:
            all_files: 所有文件摘要列表
            
        Returns:
            Dict: 分类报告
        """
        # 按类型分组
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
        
        for file_digest in all_files:
            file_type = file_digest.metadata.file_type.value
            file_info = {
                'path': str(file_digest.metadata.path.relative_to(self.root)),
                'size': file_digest.metadata.size,
                'size_formatted': self._format_size(file_digest.metadata.size),
                'modified': file_digest.metadata.modified_time.isoformat() if file_digest.metadata.modified_time else 'unknown',
                'type': file_type,
                'is_binary': file_type == FileType.BINARY_FILES.value,
                'strategy': file_digest.metadata.processing_strategy.value if file_digest.metadata.processing_strategy else 'unknown',
                'actual_strategy': file_digest.actual_strategy if hasattr(file_digest, 'actual_strategy') else 'unknown'
            }
            
            if file_type in by_type:
                by_type[file_type].append(file_info)
            else:
                by_type[FileType.UNKNOWN.value].append(file_info)
            
            # 按大小分组
            size = file_digest.metadata.size
            if size > 1024 * 1024:
                large_files.append(file_info)
            elif size > 100 * 1024:
                medium_files.append(file_info)
            else:
                small_files.append(file_info)
        
        # 构建报告
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
        
        # 为每种类型生成详细信息
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
                        "files": sorted(paths)[:10],
                        "truncated": len(paths) > 10,
                        "total_count": len(paths)
                    }
                    for ext, paths in sorted(by_ext.items(), key=lambda x: len(x[1]), reverse=True)
                }
            }
        
        # 添加建议
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
    
    def _generate_output(self, mode: str) -> Dict:
        """生成完整输出"""
        if not self.structure:
            return {}
        
        output = {
            "metadata": {
                "generated_at": datetime.now().isoformat(),
                "root_directory": str(self.root),
                "output_mode": mode,
                "statistics": self.stats,
                "context_usage": self.context_manager.get_summary(),
            },
            "structure": self.structure.to_dict(mode)
        }
        
        # 添加策略使用统计
        if hasattr(self, 'final_strategies') and self.final_strategies:
            strategy_counts = {}
            for file_path, (file_digest, strategy) in self.final_strategies.items():
                strategy_name = strategy.value
                strategy_counts[strategy_name] = strategy_counts.get(strategy_name, 0) + 1
            
            output["strategy_statistics"] = {
                "total_files": len(self.final_strategies),
                "by_strategy": strategy_counts
            }
        
        if self.context_manager.file_records:
            output["context_allocation"] = {
                "file_records": self.context_manager.file_records
            }
        
        return output
    




def parse_context_size(size_str: str) -> int:
    """解析上下文大小字符串"""
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


def main():
    """Command line entry point"""
    
    # 获取程序名用于帮助信息格式化
    prog_name = os.path.basename(sys.argv[0])
    
    # 检测终端颜色支持（跨平台）
    def _supports_color():
        """检测终端是否支持 ANSI 颜色"""
        if sys.platform == 'win32':
            return ('ANSICON' in os.environ or 'WT_SESSION' in os.environ or 
                    'CONEMUPID' in os.environ or os.environ.get('TERM_PROGRAM') == 'vscode')
        return hasattr(sys.stdout, 'isatty') and sys.stdout.isatty()
    
    USE_COLOR = _supports_color()
    # 颜色代码定义（移除白色，使用更安全的颜色）
    C = {
        'RST': '\033[0m', 'BOLD': '\033[1m', 'DIM': '\033[2m',
        'RED': '\033[91m', 'GRN': '\033[92m', 'YLW': '\033[93m',
        'BLU': '\033[94m', 'MAG': '\033[95m', 'CYN': '\033[96m',
        # 移除 WHT，使用 BOLD 代替作为高亮
    } if USE_COLOR else {k: '' for k in ['RST', 'BOLD', 'DIM', 'RED', 'GRN', 'YLW', 'BLU', 'MAG', 'CYN']}
    
    # 使用自定义的帮助格式化器
    class HelpFormatter(argparse.RawDescriptionHelpFormatter):
        """自定义帮助格式化器 - 使用 RawDescriptionHelpFormatter 保留描述格式"""
        def __init__(self, *args, **kwargs):
            kwargs['width'] = 80  # 设置为更标准的宽度
            super().__init__(*args, **kwargs)
        
        def _format_action(self, action):
            # 移除颜色处理逻辑
            return super()._format_action(action)
    
    FormatterClass = HelpFormatter
    
    # 构建简洁的帮助文本（移除颜色和图标）
    description = """Directory Digest Tool
Generate structured summaries of directory contents for LLM consumption.

File Classification (6 categories):
  Critical Docs    (README, LICENSE, CHANGELOG, etc.)     -> Full content or Summary
  Reference Docs   (Markdown, RST, HTML, etc.)           -> Summary or Header with stats
  Source Code      (Python, C++, Java, JavaScript, etc.) -> Code skeleton
  Text Data        (CSV, JSON, YAML, logs, etc.)         -> Structure extract or Header with stats
  Binary Files     (Images, archives, media, etc.)       -> Metadata only (hashes)
  Unknown          (Unclassifiable files)                -> Metadata only

Operation Modes:
  framework    Generate structure and metadata (default)
  full         Include complete file contents
  sort         List files by type and size with statistics"""

    epilog = f"""Examples:
  # Basic usage - output JSON to stdout
  {prog_name} /path/to/project
  
  # Save to file with custom rules
  {prog_name} . --rules .digest_rules.yaml --save report.json
  
  # Full content mode with custom context size
  {prog_name} . --context-size 64k --mode full
  
  # Sort mode to analyze file types
  {prog_name} . --mode sort
  
  # Skip large files (>100MB) and use parallel processing
  {prog_name} /data --max-size 100 --parallel --workers 8"""
    
    parser = argparse.ArgumentParser(
        description=description,
        formatter_class=FormatterClass,
        epilog=epilog,
    )
    
    # Core options group
    core_group = parser.add_argument_group("Core Options", "Specify input directory and operation mode")
    core_group.add_argument(
        "directory",
        metavar="PATH",
        help="Directory path to analyze"
    )
    core_group.add_argument(
        "-m", "--mode",
        choices=["full", "framework", "sort"],
        default="framework",
        metavar="MODE",
        help="Operation mode (default: %(default)s)"
    )
    
    # Rules and context control
    rule_group = parser.add_argument_group(
        "Rules and Context Control",
        "Control file classification and LLM context optimization"
    )
    rule_group.add_argument(
        "-r", "--rules",
        metavar="FILE",
        help="""
        Path to rules file (YAML format).
        Defines file classification and processing strategies.
        If not provided, searches for default rule files in current directory:
          .digest_rules.yaml, .digest_rules.yml, 
          digest_rules.yaml, digest_rules.yml,
          rules.yaml, rules.yml
        If no rule file found, uses built-in heuristic rules.
        """
    )
    rule_group.add_argument(
        "--context-size",
        type=str,
        default="128k",
        metavar="SIZE",
        help="""
        Target LLM context size (tokens).
        Supports formats: "64k", "128k", "256k" or specific numbers.
        Used for optimizing token allocation.
        (default: %(default)s)
        """
    )
    
    # Output control group
    output_group = parser.add_argument_group(
        "Output Control",
        "Control output format, destination, and content detail"
    )
    output_group.add_argument(
        "-o", "--output",
        choices=["json", "yaml", "md", "html", "toml", "txt"],
        default="json",
        metavar="FORMAT",
        help="Output format (default: %(default)s)"
    )
    output_group.add_argument(
        "-s", "--save",
        metavar="FILE",
        help="""
        Specify output file path. Special cases:
          - Omit this option: output to stdout (suitable for piping)
          - Use "-": Force output to stdout (even if this option is provided)
          - Other paths: Write to specified file (directories created automatically)
        (default: output to stdout)
        """
    )
    output_group.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Show detailed processing information (including per-file status)"
    )
    
    # Debug option
    debug_group = parser.add_argument_group(
        "Debug Options",
        "Troubleshooting and diagnostic output"
    )
    debug_group.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug mode for troubleshooting processing issues"
    )
    
    # Size limits group
    size_group = parser.add_argument_group(
        "Size Limits",
        "Control thresholds for file processing"
    )
    size_group.add_argument(
        "--max-size",
        type=int,
        default=10240,
        metavar="MB",
        help="""
        File size threshold (MB). Files exceeding this size will be **completely skipped**:
        No checksum calculation, no content analysis, only path and size metadata retained.
        Useful for excluding oversized logs, VM images, datasets, media files.
        (default: %(default)s MB = 10 GB)
        """
    )
    
    # Processing options group
    proc_group = parser.add_argument_group("Processing Options", "Control parallel processing and file filtering")
    proc_group.add_argument(
        "--ignore",
        default=".git,__pycache__,*.pyc,*.pyo,node_modules,.venv,venv,*.min.js,*.map",
        metavar="PATTERNS",
        help="""
        Ignore patterns, comma-separated glob patterns.
        Default ignores version control, cache, dependency directories and minified files: %(default)s
        """
    )
    proc_group.add_argument(
        "-p", "--parallel",
        action="store_true",
        help="Enable parallel processing (recommended for large projects with >1000 files)"
    )
    proc_group.add_argument(
        "-w", "--workers",
        type=int,
        default=0,
        metavar="N",
        help="""
        Number of parallel worker threads.
        0 means auto-detect CPU core count (default: %(default)s -> actually uses %(const)s threads)
        """ % {'default': 0, 'const': os.cpu_count() or 4}
    )
    
    args = parser.parse_args()
    
    # Parse context size
    context_size = parse_context_size(args.context_size)
    
    # Configuration conversion
    config = {
        'max_file_size': args.max_size * 1024 * 1024,  # MB to Bytes
        'ignore_patterns': [p.strip() for p in args.ignore.split(',') if p.strip()],
        'use_parallel': args.parallel,
        'max_workers': args.workers if args.workers > 0 else os.cpu_count() or 4,
        'rules_file': Path(args.rules) if args.rules else None,
        'context_size': context_size,
        'debug': args.debug,  # 添加debug标志
    }
    
    # 如果没有提供 --rules 参数，尝试从默认位置查找规则文件
    if not args.rules:
        target_dir = Path(args.directory)  # 在目标目录下查找，而不是CWD
        default_rules_paths = [
            target_dir / ".digest_rules.yaml",
            target_dir / ".digest_rules.yml",
            target_dir / "digest_rules.yaml",
            target_dir / "digest_rules.yml",
            target_dir / "rules.yaml",
            target_dir / "rules.yml",
            target_dir / ".rules.yaml",
            target_dir / ".rules.yml",
        ]
        
        for rules_path in default_rules_paths:
            if rules_path.exists():
                config['rules_file'] = rules_path
                if args.verbose:
                    print(f"Found rules file at default location: {rules_path}", file=sys.stderr)
                break
    
    # 加载规则文件中的忽略模式（如果存在）
    if config['rules_file'] and config['rules_file'].exists():
        try:
            import yaml
            with open(config['rules_file'], 'r', encoding='utf-8') as f:
                rules_content = yaml.safe_load(f)
            
            if rules_content and isinstance(rules_content, dict) and 'ignore_patterns' in rules_content:
                file_patterns = rules_content['ignore_patterns']
                if isinstance(file_patterns, list):
                    # 合并并去重：命令行参数优先，规则文件补充
                    existing_patterns = set(config['ignore_patterns'])
                    new_patterns = [p for p in file_patterns if p not in existing_patterns]
                    if new_patterns:
                        config['ignore_patterns'].extend(new_patterns)
                        if args.verbose:
                            print(f"Loaded {len(new_patterns)} additional ignore patterns from rules file", 
                                  file=sys.stderr)
        except Exception as e:
            print(f"Warning: Could not load ignore patterns from rules file: {e}", file=sys.stderr)
    
    if args.debug:
        print(f"[DEBUG MODE ENABLED]", file=sys.stderr)
        print(f"[DEBUG] Args: {args}", file=sys.stderr)
        print(f"[DEBUG] Config: {config}", file=sys.stderr)
    
    # Create digest generator
    digest = DirectoryDigest(args.directory, config)
    
    if args.verbose:
        print(f"Analyzing directory: {args.directory}", file=sys.stderr)
        print(f"Mode: {args.mode}, Format: {args.output}", file=sys.stderr)
        print(f"Skip files larger than: {args.max_size} MB ({args.max_size/1024:.1f} GB)", file=sys.stderr)
        print(f"Context window: {context_size:,} tokens", file=sys.stderr)
        
        # 显示规则文件信息
        if args.rules:
            print(f"Rules file (explicit): {args.rules}", file=sys.stderr)
        elif config.get('rules_file'):
            print(f"Rules file (auto-detected): {config['rules_file']}", file=sys.stderr)
        else:
            print(f"Rules file: Using built-in default rules", file=sys.stderr)
            
        if args.parallel:
            print(f"Parallel processing enabled with {config['max_workers']} workers", file=sys.stderr)
    
    # Generate digest
    output = digest.create_digest(args.mode)
    
    # Handle output: default stdout, --save specifies file path, --save - forces stdout
    output_to_stdout = (args.save is None or args.save == '-')
    
    if output_to_stdout:
        # 输出到标准输出（支持管道处理）
        try:
            content = FormatConverter.convert(output, args.output, mode=args.mode)
            sys.stdout.write(content)
            if not content.endswith('\n'):
                sys.stdout.write('\n')
            sys.stdout.flush()
        except BrokenPipeError:
            # 忽略管道中断错误（如输出被 head/tail 截断）
            pass
            
        # 统计信息输出到 stderr - 与原始代码一致
        if args.verbose or args.mode == "sort":
            stats = output['metadata']['statistics']
            ctx_usage = output['metadata'].get('context_usage')
                
            print(f"\n[Summary] Files: {stats['total_files']}, "
                  f"Critical: {stats.get('critical_docs', 0)}, "
                  f"Reference: {stats.get('reference_docs', 0)}, "
                  f"Source: {stats.get('source_code', 0)}, "
                  f"Text Data: {stats.get('text_data', 0)}, "
                  f"Binary: {stats.get('binary_files', 0)}, "
                  f"Unknown: {stats.get('unknown', 0)}",  # 添加 Unknown
                  file=sys.stderr)
                
            if stats.get('skipped_large_files', 0) > 0:
                print(f"         Skipped (size): {stats['skipped_large_files']}", file=sys.stderr)
                
            if stats.get('skipped_by_context', 0) > 0:
                print(f"         Skipped (context): {stats['skipped_by_context']}", file=sys.stderr)
                
            if ctx_usage:
                print(f"[Context] Used: {ctx_usage['used_tokens']:,}/{ctx_usage['max_tokens']:,} tokens "
                      f"({ctx_usage['token_utilization']:.1%})", file=sys.stderr)
            else:
                print(f"[Context] Not applicable for sort mode", file=sys.stderr)
                
            if args.mode == "sort" and "recommendations" in output:
                for rec in output["recommendations"]:
                    print(f"[Tip] {rec}", file=sys.stderr)
            
        return None  # stdout 模式返回 None
        
    else:
        # Write to specified file
        output_path = Path(args.save)
        saved_path = digest.save_output(output, args.output, output_path, args.mode)
        
        # Display processing results (to stderr, avoid mixing with file content)
        stats = output['metadata']['statistics']
        ctx_usage = output['metadata'].get('context_usage')
        
        print(f"\n{'='*50}", file=sys.stderr)
        print(f"Directory Digest Summary", file=sys.stderr)
        print(f"{'='*50}", file=sys.stderr)
        print(f"Total files scanned:     {stats['total_files']}", file=sys.stderr)
        print(f"  ├── Critical docs:     {stats.get('critical_docs', 0)}", file=sys.stderr)
        print(f"  ├── Reference docs:    {stats.get('reference_docs', 0)}", file=sys.stderr)
        print(f"  ├── Source code:       {stats.get('source_code', 0)}", file=sys.stderr)
        print(f"  ├── Text data:         {stats.get('text_data', 0)}", file=sys.stderr)
        print(f"  ├── Binary files:      {stats.get('binary_files', 0)}", file=sys.stderr)
        if stats.get('skipped_large_files', 0) > 0:
            print(f"  ├── Skipped (size):    {stats['skipped_large_files']}", file=sys.stderr)
        if stats.get('skipped_by_context', 0) > 0:
            print(f"  └── Skipped (context): {stats['skipped_by_context']}", file=sys.stderr)
        
        if ctx_usage:
            print(f"Context window:          {ctx_usage['max_tokens']:,} tokens", file=sys.stderr)
            print(f"Context used:            {ctx_usage['used_tokens']:,} tokens "
                  f"({ctx_usage['token_utilization']:.1%})", file=sys.stderr)
        else:
            print(f"Context window:          Not applicable for sort mode", file=sys.stderr)
        
        print(f"Processing time:         {stats['processing_time']:.2f} s", file=sys.stderr)
        print(f"Output saved to:         {saved_path}", file=sys.stderr)
        print(f"{'='*50}", file=sys.stderr)
        
        if args.mode == "sort" and "recommendations" in output:
            print(f"\nRecommendations:", file=sys.stderr)
            for rec in output["recommendations"]:
                print(f"  • {rec}", file=sys.stderr)
        
        return saved_path


if __name__ == "__main__":
    main()
