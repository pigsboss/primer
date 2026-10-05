# -*- coding: utf-8 -*-
"""primer.literature.web —— 文献库 WebUI 的本地服务与静态前端。

对外接口：:func:`serve`（``python -m primer.literature web`` 用它）、
:class:`LibraryService`（服务状态：当前库、扫描结果、错误）、
:func:`create_server` / :func:`make_handler`（测试与嵌入用）。
"""

from .server import LibraryService, create_server, make_handler, open_path, serve

__all__ = ["LibraryService", "create_server", "make_handler", "open_path", "serve"]
