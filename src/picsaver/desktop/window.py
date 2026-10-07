"""Qt 桌面窗口：用 QtWebEngine 直接承载**现有 Web 界面**（不复制任何前端代码）。

- 默认窗口 1400×1000、最小 1024×720、居中显示，使用 ``icon.ico``；
- F5 重新加载页面；
- 关闭窗口时，如果还有排队中/运行中的任务，先弹确认框：确认后先请求取消、等任务收尾，再退出；
- ``cdp_port`` 打开 Chromium 调试端口（仅自动化验证用），可在无交互的情况下核对窗口内页面并截图。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from .server import active_jobs, cancel_jobs, wait_no_active_jobs

#: 窗口标题（与 Web 界面标题保持一致）
WINDOW_TITLE = "pictureSaver · 按拍摄日期拷贝照片"


class DesktopWindowError(RuntimeError):
    """Qt / QtWebEngine 初始化失败（缺依赖、没有可用桌面会话等）。"""


def icon_path() -> Path:
    """窗口图标路径（与打包脚本共用同一个 .ico）。"""
    return Path(__file__).resolve().parent / "icon.ico"


def show_error(title: str, message: str) -> None:
    """尽力弹出原生错误框：窗口模式（无控制台）下这是唯一可见的报错途径。"""
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox

        app = QApplication.instance() or QApplication(sys.argv)
        QMessageBox.critical(None, title, message)
        return
    except Exception:  # noqa: BLE001 - 继续尝试下一种方式
        pass
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(0, message, title, 0x10)  # MB_ICONERROR
        return
    except Exception:  # noqa: BLE001
        pass
    print(f"[{title}] {message}", file=sys.stderr)


def enable_remote_debugging(port: int) -> None:
    """在创建 QApplication 之前设置 QtWebEngine 的调试端口（自动化验证用）。"""
    flag = f"--remote-debugging-port={int(port)}"
    current = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "")
    if flag not in current:
        os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = f"{current} {flag}".strip()


def run_window(
    url: str,
    *,
    owns_server: bool = True,
    cdp_port: int | None = None,
    title: str = WINDOW_TITLE,
) -> int:
    """打开窗口并进入事件循环，返回 Qt 的退出码。

    :param url: 要加载的本地服务地址
    :param owns_server: 本进程是否拥有服务（False 时关闭窗口不会去停别人的服务）
    :param cdp_port: 需要自动化验证时传入调试端口
    :raises DesktopWindowError: Qt / QtWebEngine 无法初始化
    """
    if cdp_port:
        enable_remote_debugging(cdp_port)

    try:
        from PySide6.QtCore import QCoreApplication, Qt, QUrl
        from PySide6.QtGui import QIcon
        from PySide6.QtWebEngineWidgets import QWebEngineView
        from PySide6.QtWidgets import QApplication, QMainWindow, QMessageBox
    except Exception as exc:  # noqa: BLE001 - 统一转成可读错误
        raise DesktopWindowError(f"无法加载 PySide6 / QtWebEngine：{exc}") from exc

    # QtWebEngine 需要共享 OpenGL 上下文；必须在 QApplication 之前设置
    try:
        QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
    except Exception:  # noqa: BLE001 - 新版本 Qt 可能已默认开启
        pass

    try:
        app = QApplication.instance() or QApplication(sys.argv)

        class MainWindow(QMainWindow):
            """只做外壳的主窗口。"""

            def __init__(self) -> None:
                super().__init__()
                self.setWindowTitle(title)
                self.resize(1400, 1000)
                self.setMinimumSize(1024, 720)
                if icon_path().exists():
                    self.setWindowIcon(QIcon(str(icon_path())))
                self.view = QWebEngineView(self)
                self.view.load(QUrl(url))
                self.setCentralWidget(self.view)
                self._confirming = False

            def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt 命名
                if event.key() == Qt.Key_F5:
                    self.view.reload()
                    return
                super().keyPressEvent(event)

            def closeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
                if self._confirming or not owns_server:
                    event.accept()
                    return
                if self._confirm_exit():
                    event.accept()
                else:
                    event.ignore()

            def _confirm_exit(self) -> bool:
                """还有任务在跑时先确认；确认后请求取消并等待收尾。"""
                jobs = active_jobs(url)
                if not jobs:
                    return True
                self._confirming = True
                try:
                    answer = QMessageBox.question(
                        self,
                        "仍有任务在进行",
                        f"还有 {len(jobs)} 个任务正在进行。\n\n"
                        "退出会中断它们：已经复制到磁盘的文件会保留，"
                        "正在复制的文件不会留下半截（采用临时文件 + 改名）。\n\n"
                        "确定要退出吗？",
                        QMessageBox.Yes | QMessageBox.No,
                        QMessageBox.No,
                    )
                finally:
                    self._confirming = False
                if answer != QMessageBox.Yes:
                    return False
                cancel_jobs(url, [job.get("job_id", "") for job in jobs if job.get("job_id")])
                wait_no_active_jobs(url, timeout=5.0)
                return True

        window = MainWindow()
        window.show()
        return int(app.exec())
    except Exception as exc:  # noqa: BLE001
        raise DesktopWindowError(f"窗口初始化失败：{type(exc).__name__}: {exc}") from exc
