# -*- coding: utf-8 -*-
"""imgcmp —— 参考图/渲染图对照工具链。

四件套与入口（见 pyproject `[project.scripts]`）：

- `primer-imgrefgen`  参考生成器：3D 素材 → 白底 CAD／黑底渲染／ID 图／剪影 ＋ 真值 JSON
- `primer-imgtile`    T1 多尺度多部位切图器（供多模态 LLM 按序读图）
- `primer-imgfeat`    T2a 组件特征表（确定性图像处理，不含训练模型）
- `primer-imgtriage`  H1 差异分诊与交付门禁

共享约定见 :mod:`primer.imgcmp.common`。语言纪律按 docs/guides/CODING_STANDARDS.md v1.1：
stdout 只出中文人读报告；stderr／异常／选项名／JSON 键一律英文。
"""

TOOLS = ("refgen", "tiler", "feat", "triage")

__all__ = ["TOOLS"]
