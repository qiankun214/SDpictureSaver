"""picsaver.desktop —— 桌面外壳：用 QtWebEngine 承载现有 Web 界面。

依赖方向（见 Agent.md 第 4 节）：``desktop → api → core``，
桌面层只做"起服务 + 开窗口 + 退出清理"，不包含任何业务逻辑，也不直接调用 core。

注意：本包在导入时**不会**加载 PySide6（Qt 相关导入都在函数内部），
因此缺少 PySide6 的环境仍可使用 CLI 的 ``web`` 模式，单元测试也无需图形环境。
"""

from .main import run_desktop

__all__ = ["run_desktop"]
