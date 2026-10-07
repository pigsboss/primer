# -*- coding: utf-8 -*-
"""figure —— 科研图件直出流水线（提示词 → 参考物 → 成图 → 台账）。

四块：

* :mod:`primer.figure.routing` —— 代理通道：多环境档案、探活、缓存与失败换道；
* :mod:`primer.figure.pricing` —— 价目表与估算器（USD，Standard／Batch 档）；
* :mod:`primer.figure.adapters` —— 图像模型的调用（payload 构造与回信解析）；
* :mod:`primer.figure.runner` —— 迭代出图主环：续号、降级、换道、台账；
* :mod:`primer.figure.meta` —— 由图稿 yaml 抽文字清单、拼装中文提示词。

入口见 ``pyproject`` 的 ``primer-figure``（本仓按模块方式调用：``python3 -m primer.figure``）。
密钥一律只从环境变量／``.env`` 读（见 :mod:`primer.envfile`），**任何路径都不回显值**；
代理 URL 一律经 :func:`primer.figure.routing.mask_proxy_url` 掩码后再进输出。
语言纪律按 docs/guides/CODING_STANDARDS.md v1.1：stdout 只出中文人读报告；
stderr／异常／选项名／JSON 键一律英文。
"""

__all__ = ["DEFAULT_MODEL", "MODE_ZH", "SIZES"]

# 迭代档默认模型：1K 与 2K 同价，文字质量更高（见 pricing 的价目事实）。
DEFAULT_MODEL = "gemini-3-pro-image"
# 本仓只出"中文标注版"一种成图，台账 mode 字段固定。
MODE_ZH = "zh"
# 可选的输出档位。1K/2K 同价，4K 另价。
SIZES = ("1K", "2K", "4K")
