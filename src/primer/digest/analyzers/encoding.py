"""
编码分析器 - 检测文件编码、二进制/文本类型、BOM标记等
包括：
- 编码检测（UTF-8, GBK, Latin-1等）
- 二进制/文本文件判断
- BOM（字节顺序标记）检测
- Magic Number（文件头）识别
- 文本可打印性分析
"""

import re
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple
from dataclasses import dataclass, field

from .base import BaseAnalyzer, AnalysisResult
from ..base import CHARDET_AVAILABLE

# 尝试导入chardet
if CHARDET_AVAILABLE:
    import chardet


# ==================== 数据类定义 ====================

@dataclass
class BomInfo:
    """BOM（字节顺序标记）信息"""
    has_bom: bool = False
    bom_type: Optional[str] = None
    bom_bytes: Optional[bytes] = None
    bom_length: int = 0
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "has_bom": self.has_bom,
            "bom_type": self.bom_type,
            "bom_length": self.bom_length
        }


@dataclass
class EncodingCandidate:
    """编码候选信息"""
    encoding: str
    confidence: float = 0.0
    is_valid: bool = False
    decode_error_rate: float = 0.0
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "encoding": self.encoding,
            "confidence": self.confidence,
            "is_valid": self.is_valid,
            "decode_error_rate": self.decode_error_rate
        }


@dataclass
class EncodingAnalysisResult(AnalysisResult):
    """编码分析结果"""
    # 基本信息
    is_text_file: bool = True
    is_binary_file: bool = False
    
    # 编码信息
    detected_encoding: Optional[str] = None
    encoding_confidence: float = 0.0
    encoding_candidates: List[EncodingCandidate] = field(default_factory=list)
    
    # BOM信息
    bom_info: BomInfo = field(default_factory=BomInfo)
    
    # 内容特征
    printable_ratio: float = 0.0
    null_byte_count: int = 0
    line_ending_type: Optional[str] = None  # 'lf', 'crlf', 'cr', 'mixed'
    estimated_language: Optional[str] = None
    
    # Magic Number信息
    magic_number: Optional[bytes] = None
    magic_number_hex: Optional[str] = None
    suspected_file_type: Optional[str] = None
    
    def __post_init__(self):
        super().__post_init__()
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        result = {
            "success": self.success,
            "error_message": self.error_message,
            "metadata": self.metadata,
            "is_text_file": self.is_text_file,
            "is_binary_file": self.is_binary_file,
            "detected_encoding": self.detected_encoding,
            "encoding_confidence": self.encoding_confidence,
            "encoding_candidates": [c.to_dict() for c in self.encoding_candidates],
            "bom_info": self.bom_info.to_dict(),
            "printable_ratio": self.printable_ratio,
            "null_byte_count": self.null_byte_count,
            "line_ending_type": self.line_ending_type,
            "estimated_language": self.estimated_language
        }
        
        if self.magic_number_hex:
            result["magic_number_hex"] = self.magic_number_hex
        if self.suspected_file_type:
            result["suspected_file_type"] = self.suspected_file_type
        
        return result


# ==================== 常量定义 ====================

# 常见BOM标记
BOM_MARKERS = [
    (b'\x00\x00\xFE\xFF', 'UTF-32 BE'),
    (b'\xFF\xFE\x00\x00', 'UTF-32 LE'),
    (b'\xFE\xFF', 'UTF-16 BE'),
    (b'\xFF\xFE', 'UTF-16 LE'),
    (b'\xEF\xBB\xBF', 'UTF-8'),
    (b'\x2B\x2F\x76\x38', 'UTF-7'),
    (b'\x2B\x2F\x76\x39', 'UTF-7'),
    (b'\x2B\x2F\x76\x2B', 'UTF-7'),
    (b'\x2B\x2F\x76\x2F', 'UTF-7'),
]

# 常见编码列表（按优先级排序）
COMMON_ENCODINGS = [
    'utf-8',
    'gbk',
    'gb2312',
    'gb18030',
    'big5',
    'shift_jis',
    'euc-jp',
    'euc-kr',
    'latin-1',
    'iso-8859-1',
    'cp1252',
    'utf-16',
    'utf-16-le',
    'utf-16-be',
]

# 常见文件Magic Number
MAGIC_NUMBERS = {
    # 图片格式
    b'\x89PNG\r\n\x1a\n': 'PNG Image',
    b'\xFF\xD8\xFF': 'JPEG Image',
    b'GIF87a': 'GIF Image',
    b'GIF89a': 'GIF Image',
    b'II*\x00': 'TIFF Image (Intel)',
    b'MM\x00*': 'TIFF Image (Motorola)',
    b'BM': 'BMP Image',
    b'RIFF': 'RIFF Container (WAV/AVI)',
    b'OggS': 'OGG Audio',
    b'ID3': 'MP3 Audio (ID3v2)',
    b'\xFF\xFB': 'MP3 Audio',
    b'\xFF\xFA': 'MP3 Audio',
    b'fLaC': 'FLAC Audio',
    
    # 视频格式
    b'\x00\x00\x00\x14ftyp': 'MP4 Video',
    b'\x00\x00\x00\x18ftyp': 'MP4 Video',
    b'\x1A\x45\xDF\xA3': 'MKV Video',
    b'\x00\x00\x01\xB3': 'MPEG Video',
    b'\x00\x00\x01\xBA': 'MPEG Video',
    
    # 文档格式
    b'%PDF': 'PDF Document',
    b'PK\x03\x04': 'ZIP Archive / Office Open XML',
    b'PK\x05\x06': 'ZIP Archive (empty)',
    b'PK\x07\x08': 'ZIP Archive (spanned)',
    b'\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1': 'Microsoft Office Document',
    b'\x1F\x8B': 'GZIP Archive',
    b'BZh': 'BZIP2 Archive',
    b'\xFD7zXZ\x00': 'XZ Archive',
    b'7z\xBC\xAF\x27\x1C': '7-Zip Archive',
    
    # 可执行文件
    b'MZ': 'Windows Executable (PE)',
    b'\x7FELF': 'Linux Executable (ELF)',
    b'\xCA\xFE\xBA\xBE': 'Java Class File',
    b'\xFE\xED\xFA\xCE': 'Mach-O Executable (32-bit)',
    b'\xFE\xED\xFA\xCF': 'Mach-O Executable (64-bit)',
    
    # 其他
    b'SQLite format 3': 'SQLite Database',
    b'\x89HDF': 'HDF5 File',
    b'SIMPLE =': 'FITS File',
    b'<?xml': 'XML Document',
    b'<!DOCTYPE': 'HTML/XML Document',
}

# 文本文件中常见的字符范围
PRINTABLE_ASCII = set(range(32, 127))
PRINTABLE_ASCII.add(9)   # Tab
PRINTABLE_ASCII.add(10)  # LF
PRINTABLE_ASCII.add(13)  # CR


# ==================== 编码分析器类 ====================

class EncodingAnalyzer(BaseAnalyzer):
    """文件编码分析器"""
    
    def __init__(self, sample_size: int = 65536):
        """
        初始化编码分析器
        
        Args:
            sample_size: 用于分析的样本大小（字节）
        """
        self.sample_size = sample_size
    
    def can_handle(self, filepath: Path, content: Optional[str] = None) -> bool:
        """
        编码分析器可以处理所有文件类型
        
        Returns:
            bool: 总是返回 True
        """
        return True
    
    def analyze(self, filepath: Path, content: Optional[str] = None) -> EncodingAnalysisResult:
        """
        分析文件编码和类型
        
        Args:
            filepath: 文件路径
            content: 文件内容（如果已读取）
            
        Returns:
            EncodingAnalysisResult: 编码分析结果
        """
        result = EncodingAnalysisResult()
        
        try:
            # 读取文件样本
            raw_bytes = self._read_file_sample(filepath)
            
            if not raw_bytes:
                result.success = False
                result.error_message = "File is empty or cannot be read"
                return result
            
            # 1. 检测 Magic Number
            self._detect_magic_number(raw_bytes, result)
            
            # 2. 检测 BOM
            self._detect_bom(raw_bytes, result)
            
            # 3. 分析二进制/文本特征
            self._analyze_binary_text_features(raw_bytes, result)
            
            # 4. 检测编码
            if result.is_text_file:
                self._detect_encoding(raw_bytes, result)
            
            # 5. 分析行尾类型
            self._analyze_line_endings(raw_bytes, result)
            
            # 6. 估计语言（如果是文本）
            if result.is_text_file and result.detected_encoding:
                self._estimate_language(raw_bytes, result)
            
            result.success = True
            
        except Exception as e:
            result.success = False
            result.error_message = f"Failed to analyze encoding: {str(e)}"
        
        return result
    
    def _read_file_sample(self, filepath: Path) -> Optional[bytes]:
        """
        读取文件样本
        
        Args:
            filepath: 文件路径
            
        Returns:
            Optional[bytes]: 文件样本字节数据
        """
        try:
            with open(filepath, 'rb') as f:
                return f.read(self.sample_size)
        except Exception:
            return None
    
    def _detect_magic_number(self, raw_bytes: bytes, result: EncodingAnalysisResult):
        """
        检测文件 Magic Number
        
        Args:
            raw_bytes: 文件字节数据
            result: 分析结果对象
        """
        # 保存前16字节作为magic number
        result.magic_number = raw_bytes[:16]
        result.magic_number_hex = ' '.join(f'{b:02X}' for b in raw_bytes[:16])
        
        # 检查已知的magic numbers
        for magic, file_type in MAGIC_NUMBERS.items():
            if raw_bytes.startswith(magic):
                result.suspected_file_type = file_type
                # 如果是已知的二进制格式，标记为二进制文件
                if file_type not in ['XML Document', 'HTML/XML Document']:
                    result.is_binary_file = True
                    result.is_text_file = False
                break
    
    def _detect_bom(self, raw_bytes: bytes, result: EncodingAnalysisResult):
        """
        检测 BOM（字节顺序标记）
        
        Args:
            raw_bytes: 文件字节数据
            result: 分析结果对象
        """
        for bom_bytes, bom_type in BOM_MARKERS:
            if raw_bytes.startswith(bom_bytes):
                result.bom_info = BomInfo(
                    has_bom=True,
                    bom_type=bom_type,
                    bom_bytes=bom_bytes,
                    bom_length=len(bom_bytes)
                )
                # BOM通常意味着Unicode文本
                result.is_text_file = True
                result.is_binary_file = False
                if 'UTF' in bom_type:
                    result.detected_encoding = bom_type.lower().replace(' ', '-')
                    result.encoding_confidence = 0.95
                break
    
    def _analyze_binary_text_features(self, raw_bytes: bytes, result: EncodingAnalysisResult):
        """
        分析二进制/文本特征
        
        Args:
            raw_bytes: 文件字节数据
            result: 分析结果对象
        """
        # 统计空字节
        result.null_byte_count = raw_bytes.count(b'\x00')
        
        # 统计可打印字符比例
        printable_count = 0
        for byte in raw_bytes:
            if byte in PRINTABLE_ASCII:
                printable_count += 1
        
        result.printable_ratio = printable_count / len(raw_bytes) if raw_bytes else 0.0
        
        # 如果已经通过magic number确定了类型，就不再判断
        if result.suspected_file_type:
            return
        
        # 判断是否为二进制文件
        # 规则1：如果有大量空字节，很可能是二进制
        if result.null_byte_count > 0:
            null_ratio = result.null_byte_count / len(raw_bytes)
            if null_ratio > 0.01:  # 超过1%的空字节
                result.is_binary_file = True
                result.is_text_file = False
                return
        
        # 规则2：可打印字符比例过低
        if result.printable_ratio < 0.7:
            result.is_binary_file = True
            result.is_text_file = False
            return
        
        # 默认为文本文件
        result.is_text_file = True
        result.is_binary_file = False
    
    def _detect_encoding(self, raw_bytes: bytes, result: EncodingAnalysisResult):
        """
        检测文本编码
        
        Args:
            raw_bytes: 文件字节数据
            result: 分析结果对象
        """
        candidates = []
        
        # 1. 如果有BOM，已经设置了编码，只需验证
        if result.bom_info.has_bom and result.detected_encoding:
            candidate = self._test_encoding(raw_bytes, result.detected_encoding)
            if candidate:
                candidates.append(candidate)
        
        # 2. 尝试使用chardet（如果可用）
        if CHARDET_AVAILABLE:
            try:
                chardet_result = chardet.detect(raw_bytes)
                encoding = chardet_result.get('encoding')
                confidence = chardet_result.get('confidence', 0.0)
                
                if encoding and confidence > 0.5:
                    candidate = self._test_encoding(raw_bytes, encoding)
                    if candidate:
                        candidate.confidence = confidence
                        candidates.append(candidate)
            except Exception:
                pass
        
        # 3. 尝试常见编码
        for encoding in COMMON_ENCODINGS:
            # 跳过已经测试过的编码
            if any(c.encoding.lower() == encoding.lower() for c in candidates):
                continue
            
            candidate = self._test_encoding(raw_bytes, encoding)
            if candidate:
                candidates.append(candidate)
        
        # 4. 按有效性和错误率排序
        candidates.sort(key=lambda x: (not x.is_valid, x.decode_error_rate, -x.confidence))
        
        result.encoding_candidates = candidates
        
        # 5. 选择最佳编码
        if candidates:
            best = candidates[0]
            result.detected_encoding = best.encoding
            result.encoding_confidence = best.confidence
            
            # 如果chardet没有给出高置信度，根据解码质量调整
            if result.encoding_confidence < 0.7:
                if best.is_valid and best.decode_error_rate < 0.01:
                    result.encoding_confidence = 0.9
                elif best.is_valid and best.decode_error_rate < 0.05:
                    result.encoding_confidence = 0.7
    
    def _test_encoding(self, raw_bytes: bytes, encoding: str) -> Optional[EncodingCandidate]:
        """
        测试特定编码是否能正确解码
        
        Args:
            raw_bytes: 文件字节数据
            encoding: 编码名称
            
        Returns:
            Optional[EncodingCandidate]: 编码候选信息
        """
        try:
            # 尝试严格解码
            try:
                raw_bytes.decode(encoding, errors='strict')
                return EncodingCandidate(
                    encoding=encoding,
                    confidence=0.8,
                    is_valid=True,
                    decode_error_rate=0.0
                )
            except UnicodeDecodeError:
                pass
            
            # 尝试容错解码，统计错误率
            test_size = min(len(raw_bytes), 4096)
            test_bytes = raw_bytes[:test_size]
            
            # 使用replace模式解码
            decoded = test_bytes.decode(encoding, errors='replace')
            
            # 统计替换字符（U+FFFD）的数量
            error_count = decoded.count('\uFFFD')
            total_chars = len(decoded)
            error_rate = error_count / total_chars if total_chars > 0 else 1.0
            
            if error_rate < 0.2:  # 错误率低于20%认为有效
                return EncodingCandidate(
                    encoding=encoding,
                    confidence=0.5 * (1.0 - error_rate),
                    is_valid=True,
                    decode_error_rate=error_rate
                )
            
            return None
            
        except Exception:
            return None
    
    def _analyze_line_endings(self, raw_bytes: bytes, result: EncodingAnalysisResult):
        """
        分析行尾类型
        
        Args:
            raw_bytes: 文件字节数据
            result: 分析结果对象
        """
        # 统计各种行尾
        crlf_count = raw_bytes.count(b'\r\n')
        lf_count = raw_bytes.count(b'\n') - crlf_count
        cr_count = raw_bytes.count(b'\r') - crlf_count
        
        types_found = []
        if crlf_count > 0:
            types_found.append(('crlf', crlf_count))
        if lf_count > 0:
            types_found.append(('lf', lf_count))
        if cr_count > 0:
            types_found.append(('cr', cr_count))
        
        if not types_found:
            result.line_ending_type = None
        elif len(types_found) == 1:
            result.line_ending_type = types_found[0][0]
        else:
            # 混合行尾，选择最常见的
            types_found.sort(key=lambda x: x[1], reverse=True)
            if types_found[0][1] > types_found[1][1] * 2:
                result.line_ending_type = types_found[0][0]
            else:
                result.line_ending_type = 'mixed'
    
    def _estimate_language(self, raw_bytes: bytes, result: EncodingAnalysisResult):
        """
        估计文本语言（基于字符特征）
        
        Args:
            raw_bytes: 文件字节数据
            result: 分析结果对象
        """
        if not result.detected_encoding:
            return
        
        try:
            # 解码文本（使用replace避免错误）
            text = raw_bytes.decode(result.detected_encoding, errors='replace')
            
            # 统计中文字符
            chinese_chars = re.findall(r'[\u4e00-\u9fff]', text)
            chinese_count = len(chinese_chars)
            
            # 统计日文假名
            japanese_chars = re.findall(r'[\u3040-\u309F\u30A0-\u30FF]', text)
            japanese_count = len(japanese_chars)
            
            # 统计韩文
            korean_chars = re.findall(r'[\uAC00-\uD7AF\u1100-\u11FF]', text)
            korean_count = len(korean_chars)
            
            # 统计英文单词
            english_words = re.findall(r'\b[a-zA-Z]{3,}\b', text)
            english_count = len(english_words)
            
            total_chars = len([c for c in text if not c.isspace()])
            if total_chars == 0:
                return
            
            # 计算比例
            chinese_ratio = chinese_count / total_chars
            japanese_ratio = japanese_count / total_chars
            korean_ratio = korean_count / total_chars
            
            # 判断主要语言
            if chinese_ratio > 0.1:
                result.estimated_language = 'zh'
            elif japanese_ratio > 0.05:
                result.estimated_language = 'ja'
            elif korean_ratio > 0.05:
                result.estimated_language = 'ko'
            elif english_count > 10:
                result.estimated_language = 'en'
            else:
                result.estimated_language = 'unknown'
                
        except Exception:
            pass
    
    @staticmethod
    def decode_file_safely(filepath: Path, encoding: Optional[str] = None, 
                          sample_size: Optional[int] = None) -> Tuple[Optional[str], EncodingAnalysisResult]:
        """
        安全解码文件
        
        Args:
            filepath: 文件路径
            encoding: 指定编码（如果为None则自动检测）
            sample_size: 样本大小
            
        Returns:
            Tuple[Optional[str], EncodingAnalysisResult]: (解码内容, 分析结果)
        """
        analyzer = EncodingAnalyzer(sample_size or 65536)
        result = analyzer.analyze(filepath)
        
        if not result.success or not result.is_text_file or not result.detected_encoding:
            return None, result
        
        try:
            # 使用检测到的编码读取文件
            with open(filepath, 'rb') as f:
                raw_bytes = f.read()
            
            # 跳过BOM
            if result.bom_info.has_bom:
                raw_bytes = raw_bytes[result.bom_info.bom_length:]
            
            content = raw_bytes.decode(result.detected_encoding, errors='replace')
            return content, result
            
        except Exception as e:
            result.success = False
            result.error_message = f"Failed to decode file: {str(e)}"
            return None, result


# ==================== 公共 API 导出 ====================

__all__ = [
    'BomInfo',
    'EncodingCandidate',
    'EncodingAnalysisResult',
    'EncodingAnalyzer',
]
