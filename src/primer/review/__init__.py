# -*- coding: utf-8 -*-
"""review —— primer 会话舱：把"参考输入 → 参数台账 → 模型"的决策环路摆进浏览器。

与 imgcmp 的区别：imgcmp 是**量具与自检**（对照、测量、分诊，服务施工一致性）；
review 是**人机会话界面**（人看图、圈注、作答；模型侧出图、提问、落账）。
界面只是视图——资产仍是会话目录里的 JSON/YAML（可 diff、可 git、可复算）。

Phase 0 范围：上传（PDF 按页光栅化）／画布（矢量圈注、套索、橡皮）／问题卡／
只读台账／LLM 对话面板（消息落盘，调模型与动作执行在 :mod:`primer.scene.loop`）。
协议与用法见 :mod:`primer.review.server` 与 docs/guides/review.md、
docs/guides/scene_loop.md。
"""

TOOLS = ("server",)

__all__ = ["TOOLS"]
