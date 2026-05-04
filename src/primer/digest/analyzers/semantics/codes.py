"""
源代码分析器 - 专门处理各种编程语言的源代码分析
包括：
- Python AST 分析
- 其他语言的正则表达式分析
- 代码复杂度分析
- 导入/依赖提取
- 函数/类结构分析
"""

import re
import ast
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field

from .base import (
    BaseSourceCodeAnalyzer,
    SemanticAnalysisResult,
    SourceCodeAnalysis,
    ComplexityAnalyzer,
)
from ...base import CHARDET_AVAILABLE


# ==================== Python 源代码分析器 ====================

class PythonSourceCodeAnalyzer(BaseSourceCodeAnalyzer):
    """Python 源代码分析器 - 使用 AST 进行精确分析"""
    
    def __init__(self):
        super().__init__()
    
    def can_handle(self, filepath: Path, content: Optional[str] = None) -> bool:
        """判断是否为 Python 文件"""
        return filepath.suffix.lower() == '.py'
    
    def analyze(self, filepath: Path, content: Optional[str] = None) -> SemanticAnalysisResult:
        """
        分析 Python 源代码
        
        Args:
            filepath: 文件路径
            content: 文件内容（如果已读取）
            
        Returns:
            SemanticAnalysisResult: 分析结果
        """
        result = SemanticAnalysisResult(
            content_type="source_code",
            language="python"
        )
        
        try:
            # 读取内容（如果未提供）
            if content is None:
                content = self._read_file_content(filepath)
            
            if content is None:
                result.success = False
                result.error_message = "Could not read file content"
                return result
            
            # 分析源代码
            analysis = self._analyze_python(content)
            
            # 提取关键词
            result.keywords = self._extract_keywords(content)
            
            # 生成摘要
            result.summary = self._generate_summary(analysis)
            
            # 设置详细分析结果
            result.metadata["source_code_analysis"] = analysis
            
            result.success = True
            
        except Exception as e:
            result.success = False
            result.error_message = f"Python analysis failed: {str(e)}"
        
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
                        result = chardet.detect(raw_content)
                        encoding = result['encoding'] if result['encoding'] else 'latin-1'
                        return raw_content.decode(encoding, errors='ignore')
            except Exception:
                return None
    
    def _analyze_python(self, content: str) -> SourceCodeAnalysis:
        """分析 Python 代码"""
        lines = content.split('\n')
        total_lines = len(lines)
        
        # 统计行数
        blank_lines = 0
        comment_lines = 0
        
        for line in lines:
            stripped = line.strip()
            if not stripped:
                blank_lines += 1
            elif stripped.startswith('#'):
                comment_lines += 1
        
        code_lines = total_lines - blank_lines - comment_lines
        
        # 使用 AST 分析
        imports = []
        functions = []
        classes = []
        global_vars = []
        constants = []
        
        try:
            tree = ast.parse(content)
            
            # 遍历 AST 节点
            for node in ast.walk(tree):
                # 导入语句
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        imports.append(alias.name)
                elif isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    for alias in node.names:
                        if module:
                            imports.append(f"{module}.{alias.name}")
                        else:
                            imports.append(alias.name)
                
                # 函数定义
                elif isinstance(node, ast.FunctionDef):
                    functions.append({
                        "name": node.name,
                        "args": len(node.args.args),
                        "defaults": len(node.args.defaults) if node.args.defaults else 0,
                        "docstring": ast.get_docstring(node),
                        "line": node.lineno,
                        "decorators": [d.id for d in node.decorator_list if hasattr(d, 'id')]
                    })
                
                # 类定义
                elif isinstance(node, ast.ClassDef):
                    bases = []
                    for base in node.bases:
                        if isinstance(base, ast.Name):
                            bases.append(base.id)
                        elif isinstance(base, ast.Attribute):
                            # 对于 ast.Attribute，尝试获取其名称
                            try:
                                # 尝试使用 ast.unparse（Python 3.9+）
                                if hasattr(ast, 'unparse'):
                                    bases.append(ast.unparse(base))
                                else:
                                    # 回退方案：构建字符串表示
                                    attr_parts = []
                                    current = base
                                    while isinstance(current, ast.Attribute):
                                        attr_parts.append(current.attr)
                                        current = current.value
                                    if isinstance(current, ast.Name):
                                        attr_parts.append(current.id)
                                    bases.append('.'.join(reversed(attr_parts)))
                            except Exception:
                                bases.append(str(base))
                    
                    classes.append({
                        "name": node.name,
                        "bases": bases,
                        "docstring": ast.get_docstring(node),
                        "line": node.lineno,
                        "methods": len([n for n in node.body if isinstance(n, ast.FunctionDef)])
                    })
                
                # 全局变量
                elif isinstance(node, ast.Assign):
                    if not hasattr(node, 'parent') or isinstance(node.parent, ast.Module):
                        for target in node.targets:
                            if isinstance(target, ast.Name):
                                var_name = target.id
                                try:
                                    if isinstance(node.value, ast.Constant):
                                        constants.append(var_name)
                                    else:
                                        global_vars.append(var_name)
                                except Exception:
                                    global_vars.append(var_name)
        
        except SyntaxError:
            # AST 解析失败，使用正则表达式
            return self._analyze_python_with_regex(content)
        except Exception as e:
            import sys
            print(f"Warning: Python AST analysis failed: {e}", file=sys.stderr)
            return self._analyze_python_with_regex(content)
        
        # 代码复杂度分析
        complexity_metrics = ComplexityAnalyzer.analyze_python(content)
        
        # 代码风格检查
        style_issues = self._check_python_style(lines)
        
        return SourceCodeAnalysis(
            language="python",
            total_lines=total_lines,
            code_lines=code_lines,
            comment_lines=comment_lines,
            blank_lines=blank_lines,
            imports=imports,
            functions=functions,
            classes=classes,
            global_vars=global_vars,
            constants=constants,
            dependencies=self._extract_python_dependencies(imports),
            complexity_metrics=complexity_metrics,
            style_issues=style_issues,
            security_issues=[],
            test_coverage=None
        )
    
    def _analyze_python_with_regex(self, content: str) -> SourceCodeAnalysis:
        """使用正则表达式分析 Python 代码（AST 解析失败时的后备方案）"""
        lines = content.split('\n')
        total_lines = len(lines)
        
        # 统计行数
        blank_lines = 0
        comment_lines = 0
        for line in lines:
            stripped = line.strip()
            if not stripped:
                blank_lines += 1
            elif stripped.startswith('#'):
                comment_lines += 1
        
        code_lines = total_lines - blank_lines - comment_lines
        
        # 使用正则表达式提取信息
        imports = []
        functions = []
        classes = []
        
        # 提取导入语句
        import_patterns = [
            r'^import\s+([a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*)*)',
            r'^from\s+([a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*)*)\s+import'
        ]
        
        for line in lines:
            stripped = line.strip()
            for pattern in import_patterns:
                match = re.match(pattern, stripped)
                if match:
                    imports.append(match.group(1))
                    break
        
        # 提取函数定义
        func_pattern = r'^def\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*\('
        for i, line in enumerate(lines):
            match = re.match(func_pattern, line.strip())
            if match:
                functions.append({
                    "name": match.group(1),
                    "args": 0,
                    "line": i + 1,
                    "docstring": None
                })
        
        # 提取类定义
        class_pattern = r'^class\s+([a-zA-Z_][a-zA-Z0-9_]*)'
        for i, line in enumerate(lines):
            match = re.match(class_pattern, line.strip())
            if match:
                classes.append({
                    "name": match.group(1),
                    "bases": [],
                    "line": i + 1,
                    "methods": 0
                })
        
        return SourceCodeAnalysis(
            language="python",
            total_lines=total_lines,
            code_lines=code_lines,
            comment_lines=comment_lines,
            blank_lines=blank_lines,
            imports=imports,
            functions=functions,
            classes=classes,
            global_vars=[],
            constants=[],
            dependencies=self._extract_python_dependencies(imports),
            complexity_metrics=ComplexityAnalyzer.analyze_generic(content)
        )
    
    def _extract_python_dependencies(self, imports: List[str]) -> List[str]:
        """从导入语句中提取依赖包名"""
        dependencies = set()
        
        stdlib = {
            'os', 'sys', 'math', 're', 'json', 'datetime', 'time',
            'collections', 'itertools', 'functools', 'typing',
            'pathlib', 'hashlib', 'random', 'statistics', 'decimal'
        }
        
        for imp in imports:
            parts = imp.split('.')
            if parts:
                top_level = parts[0]
                if top_level and not top_level.startswith('_') and top_level not in stdlib:
                    dependencies.add(top_level)
        
        return list(dependencies)
    
    def _check_python_style(self, lines: List[str]) -> List[Dict]:
        """检查 Python 代码风格（简化版）"""
        style_issues = []
        
        # 检查行长度
        for i, line in enumerate(lines, 1):
            if len(line) > 100:
                style_issues.append({
                    "type": "line_too_long",
                    "line": i,
                    "message": f"Line {i} exceeds 100 characters",
                    "severity": "warning"
                })
        
        # 检查导入顺序
        import_lines = []
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith('import') or stripped.startswith('from'):
                import_lines.append((i, stripped))
        
        if len(import_lines) > 1:
            std_imports = []
            third_party_imports = []
            
            std_patterns = ['os', 'sys', 'math', 're', 'json', 'datetime']
            
            for line_num, import_stmt in import_lines:
                if any(pattern in import_stmt for pattern in std_patterns):
                    std_imports.append((line_num, import_stmt))
                else:
                    third_party_imports.append((line_num, import_stmt))
            
            if std_imports and third_party_imports:
                last_std = std_imports[-1][0]
                first_third = third_party_imports[0][0]
                
                if first_third < last_std:
                    style_issues.append({
                        "type": "import_order",
                        "line": first_third,
                        "message": "Third-party imports should come after standard library imports",
                        "severity": "suggestion"
                    })
        
        return style_issues
    
    def _generate_summary(self, analysis: SourceCodeAnalysis) -> str:
        """生成代码摘要"""
        parts = []
        parts.append(f"Python source code with {analysis.total_lines} lines")
        parts.append(f"  - {analysis.code_lines} code lines")
        parts.append(f"  - {analysis.comment_lines} comment lines")
        parts.append(f"  - {analysis.blank_lines} blank lines")
        
        if analysis.classes:
            parts.append(f"  - {len(analysis.classes)} classes")
        if analysis.functions:
            parts.append(f"  - {len(analysis.functions)} functions")
        if analysis.imports:
            parts.append(f"  - {len(analysis.imports)} imports")
        
        if analysis.complexity_metrics:
            complexity_level = analysis.complexity_metrics.get('complexity_level', 'unknown')
            parts.append(f"  - Complexity: {complexity_level}")
        
        return '\n'.join(parts)


# ==================== C/C++/Java 源代码分析器 ====================

class CFamilySourceCodeAnalyzer(BaseSourceCodeAnalyzer):
    """C/C++/Java 家族源代码分析器"""
    
    def __init__(self):
        super().__init__()
    
    def can_handle(self, filepath: Path, content: Optional[str] = None) -> bool:
        """判断是否为 C/C++/Java 文件"""
        return filepath.suffix.lower() in ['.c', '.cpp', '.h', '.hpp', '.cc', '.java']
    
    def analyze(self, filepath: Path, content: Optional[str] = None) -> SemanticAnalysisResult:
        """分析 C/C++/Java 源代码"""
        result = SemanticAnalysisResult(
            content_type="source_code",
            language=self._get_language(filepath)
        )
        
        try:
            if content is None:
                content = self._read_file_content(filepath)
            
            if content is None:
                result.success = False
                result.error_message = "Could not read file content"
                return result
            
            analysis = self._analyze_c_family(content, filepath)
            result.keywords = self._extract_keywords(content)
            result.summary = self._generate_summary(analysis)
            result.metadata["source_code_analysis"] = analysis
            result.success = True
            
        except Exception as e:
            result.success = False
            result.error_message = f"C-family analysis failed: {str(e)}"
        
        return result
    
    def _get_language(self, filepath: Path) -> str:
        """获取语言类型"""
        suffix = filepath.suffix.lower()
        language_map = {
            '.java': 'java',
            '.cpp': 'cpp',
            '.c': 'c',
            '.h': 'c_header',
            '.hpp': 'cpp_header',
            '.cc': 'cpp'
        }
        return language_map.get(suffix, 'c_family')
    
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
    
    def _analyze_c_family(self, content: str, filepath: Path) -> SourceCodeAnalysis:
        """分析 C/C++/Java 代码"""
        lines = content.split('\n')
        total_lines = len(lines)
        
        # 统计行数
        blank_lines = 0
        comment_lines = 0
        in_block_comment = False
        
        for line in lines:
            stripped = line.strip()
            
            if not stripped:
                blank_lines += 1
                continue
            
            if in_block_comment:
                comment_lines += 1
                if '*/' in stripped:
                    in_block_comment = False
                continue
            
            if stripped.startswith('//'):
                comment_lines += 1
            elif stripped.startswith('/*'):
                comment_lines += 1
                in_block_comment = True
                if '*/' in stripped and stripped.index('*/') > stripped.index('/*'):
                    in_block_comment = False
        
        code_lines = total_lines - blank_lines - comment_lines
        
        # 提取导入/包含语句
        imports = []
        suffix = filepath.suffix.lower()
        
        if suffix in ['.cpp', '.c', '.h', '.hpp', '.cc']:
            pattern = r'^#include\s+[<"]([^>"]+)[>"]'
        else:
            pattern = r'^import\s+([a-zA-Z0-9_.]+)'
        
        for line in lines:
            match = re.match(pattern, line.strip())
            if match:
                imports.append(match.group(1))
        
        # 提取函数/方法定义
        functions = []
        func_patterns = [
            r'^\s*(?:[a-zA-Z_][a-zA-Z0-9_:<>]*\s+)+([a-zA-Z_][a-zA-Z0-9_]*)\s*\(',
            r'^\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\([^)]*\)\s*{'
        ]
        
        for i, line in enumerate(lines):
            for pattern in func_patterns:
                match = re.search(pattern, line)
                if match:
                    func_name = match.group(1)
                    if func_name not in ['if', 'for', 'while', 'switch', 'return', 'int', 'void', 'float', 'double']:
                        functions.append({
                            "name": func_name,
                            "line": i + 1
                        })
                    break
        
        # 提取类定义
        classes = []
        class_pattern = r'^\s*(?:public|private|protected|class|struct)\s+([a-zA-Z_][a-zA-Z0-9_]*)'
        
        for i, line in enumerate(lines):
            match = re.search(class_pattern, line)
            if match:
                classes.append({
                    "name": match.group(1),
                    "line": i + 1
                })
        
        return SourceCodeAnalysis(
            language=self._get_language(filepath),
            total_lines=total_lines,
            code_lines=code_lines,
            comment_lines=comment_lines,
            blank_lines=blank_lines,
            imports=imports,
            functions=functions,
            classes=classes,
            complexity_metrics=ComplexityAnalyzer.analyze_generic(content)
        )
    
    def _generate_summary(self, analysis: SourceCodeAnalysis) -> str:
        """生成代码摘要"""
        parts = []
        parts.append(f"{analysis.language} source code with {analysis.total_lines} lines")
        
        if analysis.classes:
            parts.append(f"  - {len(analysis.classes)} classes")
        if analysis.functions:
            parts.append(f"  - {len(analysis.functions)} functions")
        if analysis.imports:
            parts.append(f"  - {len(analysis.imports)} includes/imports")
        
        return '\n'.join(parts)


# ==================== JavaScript/TypeScript 源代码分析器 ====================

class JavaScriptSourceCodeAnalyzer(BaseSourceCodeAnalyzer):
    """JavaScript/TypeScript 源代码分析器"""
    
    def __init__(self):
        super().__init__()
    
    def can_handle(self, filepath: Path, content: Optional[str] = None) -> bool:
        """判断是否为 JavaScript/TypeScript 文件"""
        return filepath.suffix.lower() in ['.js', '.ts', '.jsx', '.tsx']
    
    def analyze(self, filepath: Path, content: Optional[str] = None) -> SemanticAnalysisResult:
        """分析 JavaScript/TypeScript 源代码"""
        result = SemanticAnalysisResult(
            content_type="source_code",
            language=self._get_language(filepath)
        )
        
        try:
            if content is None:
                content = self._read_file_content(filepath)
            
            if content is None:
                result.success = False
                result.error_message = "Could not read file content"
                return result
            
            analysis = self._analyze_javascript(content, filepath)
            result.keywords = self._extract_keywords(content)
            result.summary = self._generate_summary(analysis)
            result.metadata["source_code_analysis"] = analysis
            result.success = True
            
        except Exception as e:
            result.success = False
            result.error_message = f"JavaScript analysis failed: {str(e)}"
        
        return result
    
    def _get_language(self, filepath: Path) -> str:
        """获取语言类型"""
        suffix = filepath.suffix.lower()
        if suffix == '.ts':
            return 'typescript'
        elif suffix == '.tsx':
            return 'tsx'
        elif suffix == '.jsx':
            return 'jsx'
        return 'javascript'
    
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
    
    def _analyze_javascript(self, content: str, filepath: Path) -> SourceCodeAnalysis:
        """分析 JavaScript/TypeScript 代码"""
        lines = content.split('\n')
        total_lines = len(lines)
        
        # 统计行数
        blank_lines = 0
        comment_lines = 0
        in_block_comment = False
        
        for line in lines:
            stripped = line.strip()
            
            if not stripped:
                blank_lines += 1
                continue
            
            if in_block_comment:
                comment_lines += 1
                if '*/' in stripped:
                    in_block_comment = False
                continue
            
            if stripped.startswith('//'):
                comment_lines += 1
            elif stripped.startswith('/*'):
                comment_lines += 1
                in_block_comment = True
                if '*/' in stripped and stripped.index('*/') > stripped.index('/*'):
                    in_block_comment = False
        
        code_lines = total_lines - blank_lines - comment_lines
        
        # 提取导入语句
        imports = []
        import_patterns = [
            r'^import\s+.*from\s+[\'"]([^\'"]+)[\'"]',
            r'^const\s+.*=\s+require\([\'"]([^\'"]+)[\'"]\)',
            r'^require\([\'"]([^\'"]+)[\'"]\)'
        ]
        
        for line in lines:
            for pattern in import_patterns:
                match = re.search(pattern, line.strip())
                if match:
                    imports.append(match.group(1))
                    break
        
        # 提取函数定义
        functions = []
        func_patterns = [
            r'^\s*(?:export\s+)?(?:async\s+)?function\s+([a-zA-Z_][a-zA-Z0-9_]*)',
            r'^\s*(?:export\s+)?(?:const|let|var)\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*(?:\([^)]*\)\s*=>|function)',
        ]
        
        for i, line in enumerate(lines):
            for pattern in func_patterns:
                match = re.search(pattern, line)
                if match:
                    functions.append({
                        "name": match.group(1),
                        "line": i + 1
                    })
                    break
        
        # 提取类定义
        classes = []
        class_pattern = r'^\s*(?:export\s+)?class\s+([a-zA-Z_][a-zA-Z0-9_]*)'
        
        for i, line in enumerate(lines):
            match = re.search(class_pattern, line)
            if match:
                classes.append({
                    "name": match.group(1),
                    "line": i + 1
                })
        
        return SourceCodeAnalysis(
            language=self._get_language(filepath),
            total_lines=total_lines,
            code_lines=code_lines,
            comment_lines=comment_lines,
            blank_lines=blank_lines,
            imports=imports,
            functions=functions,
            classes=classes,
            complexity_metrics=ComplexityAnalyzer.analyze_generic(content)
        )
    
    def _generate_summary(self, analysis: SourceCodeAnalysis) -> str:
        """生成代码摘要"""
        parts = []
        parts.append(f"{analysis.language} source code with {analysis.total_lines} lines")
        
        if analysis.classes:
            parts.append(f"  - {len(analysis.classes)} classes")
        if analysis.functions:
            parts.append(f"  - {len(analysis.functions)} functions")
        if analysis.imports:
            parts.append(f"  - {len(analysis.imports)} imports")
        
        return '\n'.join(parts)


# ==================== 通用源代码分析器 ====================

class GenericSourceCodeAnalyzer(BaseSourceCodeAnalyzer):
    """通用源代码分析器 - 处理其他编程语言"""
    
    def __init__(self):
        super().__init__()
    
    def can_handle(self, filepath: Path, content: Optional[str] = None) -> bool:
        """通用分析器可以处理所有源代码文件"""
        # 检查是否为已知的源代码扩展名
        code_extensions = {
            '.py', '.java', '.cpp', '.c', '.h', '.hpp', '.cc',
            '.js', '.ts', '.jsx', '.tsx',
            '.go', '.rs', '.rb', '.php', '.swift',
            '.sh', '.bash', '.ps1', '.bat', '.cmd',
            '.sql', '.r', '.m', '.scala', '.kt',
            '.html', '.htm', '.css', '.scss', '.less',
            '.xml', '.yaml', '.yml', '.json', '.toml'
        }
        return filepath.suffix.lower() in code_extensions
    
    def analyze(self, filepath: Path, content: Optional[str] = None) -> SemanticAnalysisResult:
        """分析通用源代码"""
        result = SemanticAnalysisResult(
            content_type="source_code",
            language=filepath.suffix.lstrip('.') or "unknown"
        )
        
        try:
            if content is None:
                content = self._read_file_content(filepath)
            
            if content is None:
                result.success = False
                result.error_message = "Could not read file content"
                return result
            
            analysis = self._analyze_generic(content, filepath)
            result.keywords = self._extract_keywords(content)
            result.summary = self._generate_summary(analysis)
            result.metadata["source_code_analysis"] = analysis
            result.success = True
            
        except Exception as e:
            result.success = False
            result.error_message = f"Generic code analysis failed: {str(e)}"
        
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
    
    def _analyze_generic(self, content: str, filepath: Path) -> SourceCodeAnalysis:
        """分析通用代码"""
        lines = content.split('\n')
        total_lines = len(lines)
        
        # 统计行数
        blank_lines = 0
        comment_lines = 0
        
        # 根据后缀确定注释模式
        comment_patterns = self._get_comment_patterns(filepath)
        
        for line in lines:
            stripped = line.strip()
            if not stripped:
                blank_lines += 1
                continue
            
            is_comment = False
            for pattern in comment_patterns:
                if stripped.startswith(pattern):
                    comment_lines += 1
                    is_comment = True
                    break
            if is_comment:
                continue
        
        code_lines = total_lines - blank_lines - comment_lines
        
        # 提取可能的函数定义
        functions = []
        func_patterns = [
            r'^\s*def\s+([a-zA-Z_][a-zA-Z0-9_]*)',
            r'^\s*function\s+([a-zA-Z_][a-zA-Z0-9_]*)',
            r'^\s*(?:public|private|protected)?\s*\w+\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*\('
        ]
        
        for i, line in enumerate(lines):
            for pattern in func_patterns:
                match = re.search(pattern, line.strip())
                if match:
                    functions.append({
                        "name": match.group(1),
                        "line": i + 1
                    })
                    break
        
        # 提取可能的类定义
        classes = []
        class_patterns = [
            r'^\s*class\s+([a-zA-Z_][a-zA-Z0-9_]*)',
            r'^\s*struct\s+([a-zA-Z_][a-zA-Z0-9_]*)'
        ]
        
        for i, line in enumerate(lines):
            for pattern in class_patterns:
                match = re.search(pattern, line.strip())
                if match:
                    classes.append({
                        "name": match.group(1),
                        "line": i + 1
                    })
                    break
        
        return SourceCodeAnalysis(
            language=filepath.suffix.lstrip('.') or "unknown",
            total_lines=total_lines,
            code_lines=code_lines,
            comment_lines=comment_lines,
            blank_lines=blank_lines,
            functions=functions,
            classes=classes,
            complexity_metrics=ComplexityAnalyzer.analyze_generic(content)
        )
    
    def _get_comment_patterns(self, filepath: Path) -> List[str]:
        """根据文件扩展名获取注释模式"""
        suffix = filepath.suffix.lower()
        
        if suffix in ['.py', '.sh', '.bash', '.zsh', '.ps1', '.bat', '.cmd', '.rb', '.pl', '.pm']:
            return ['#']
        elif suffix in ['.js', '.ts', '.jsx', '.tsx', '.java', '.cpp', '.c', '.h', '.hpp', '.cc', '.go', '.rs', '.swift', '.kt', '.scala']:
            return ['//', '/*']
        elif suffix in ['.php']:
            return ['//', '#', '/*']
        else:
            return ['//', '#', '/*']
    
    def _generate_summary(self, analysis: SourceCodeAnalysis) -> str:
        """生成代码摘要"""
        parts = []
        parts.append(f"{analysis.language} source code with {analysis.total_lines} lines")
        
        if analysis.classes:
            parts.append(f"  - {len(analysis.classes)} classes")
        if analysis.functions:
            parts.append(f"  - {len(analysis.functions)} functions")
        
        return '\n'.join(parts)


# ==================== 组合源代码分析器 ====================

class CompositeSourceCodeAnalyzer(BaseSourceCodeAnalyzer):
    """组合源代码分析器 - 使用多个专用分析器"""
    
    def __init__(self):
        super().__init__()
        self.analyzers = [
            PythonSourceCodeAnalyzer(),
            CFamilySourceCodeAnalyzer(),
            JavaScriptSourceCodeAnalyzer(),
        ]
        self.generic_analyzer = GenericSourceCodeAnalyzer()
    
    def can_handle(self, filepath: Path, content: Optional[str] = None) -> bool:
        """只要有一个分析器能处理就返回True"""
        return (any(analyzer.can_handle(filepath, content) for analyzer in self.analyzers) or
                self.generic_analyzer.can_handle(filepath, content))
    
    def analyze(self, filepath: Path, content: Optional[str] = None) -> SemanticAnalysisResult:
        """使用合适的分析器进行分析"""
        for analyzer in self.analyzers:
            if analyzer.can_handle(filepath, content):
                return analyzer.analyze(filepath, content)
        
        return self.generic_analyzer.analyze(filepath, content)


# ==================== 公共 API 导出 ====================

__all__ = [
    'PythonSourceCodeAnalyzer',
    'CFamilySourceCodeAnalyzer',
    'JavaScriptSourceCodeAnalyzer',
    'GenericSourceCodeAnalyzer',
    'CompositeSourceCodeAnalyzer',
]
