"""单实例控制与桌面状态目录。

- **锁**：在本机固定端口（默认 8766）上绑定一个不设 SO_REUSEADDR 的 socket。
  第二个实例绑定失败即说明已有实例在运行（Windows 下第二个 bind 会直接失败，
  不会像 SO_REUSEADDR 那样抢同一个端口）。
- **状态文件**：持有锁的实例把自己的 URL 写进 ``instance.json``，
  第二个实例据此直接打开已有服务的界面，而不是再起一个服务。
- 状态目录：``%LOCALAPPDATA%\\pictureSaver2``（日志也放这里）。
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
from pathlib import Path

#: 单实例锁端口（与 HTTP 端口区分开）
LOCK_PORT = 8766
#: 应用名（状态目录、日志名）
APP_DIR_NAME = "pictureSaver2"


def state_dir() -> Path:
    """桌面状态目录（日志、instance.json）。"""
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or tempfile.gettempdir()
    path = Path(base) / APP_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def instance_file() -> Path:
    """记录"当前实例 URL"的文件。"""
    return state_dir() / "instance.json"


def log_file() -> Path:
    """桌面模式日志文件（窗口模式无控制台，出问题看这里）。"""
    return state_dir() / "desktop.log"


def read_instance_url() -> str | None:
    """读取已有实例的 URL；文件不存在或损坏时返回 None。"""
    try:
        data = json.loads(instance_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    url = data.get("url") if isinstance(data, dict) else None
    return url if isinstance(url, str) and url else None


class InstanceLock:
    """基于本地端口的单实例锁。"""

    def __init__(self, port: int = LOCK_PORT) -> None:
        self.port = int(port)
        self._sock: socket.socket | None = None
        self._owns = False

    @property
    def is_owner(self) -> bool:
        """本进程是否持有锁。"""
        return self._owns

    def acquire(self) -> tuple[bool, str | None]:
        """尝试成为唯一实例。

        :return: ``(是否成为唯一实例, 已有实例的 URL 或 None)``
        """
        if self._sock is not None:
            return self._owns, None if self._owns else read_instance_url()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            # 故意不设置 SO_REUSEADDR：Windows 下它会让两个进程绑到同一端口，锁就失效了
            sock.bind(("127.0.0.1", self.port))
            sock.listen(1)
        except OSError:
            sock.close()
            self._owns = False
            return False, read_instance_url()
        self._sock = sock
        self._owns = True
        return True, None

    def publish(self, url: str) -> None:
        """把自己服务的 URL 写进状态文件（仅持有锁时有意义）。"""
        if not self._owns:
            return
        try:
            instance_file().write_text(
                json.dumps({"pid": os.getpid(), "url": url}, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            pass

    def release(self) -> None:
        """释放锁并清理状态文件。"""
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        if self._owns:
            try:
                data = json.loads(instance_file().read_text(encoding="utf-8"))
                if int(data.get("pid", -1)) == os.getpid():
                    instance_file().unlink(missing_ok=True)
            except (OSError, ValueError):
                pass
        self._owns = False
