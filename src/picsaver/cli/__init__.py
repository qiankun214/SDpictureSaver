"""picsaver.cli —— 命令行入口（只调用 core 与可选的本地 Web 服务）。"""

from .main import app, main

__all__ = ["app", "main"]
