"""桌面外壳的 HTTP 服务引导：**不依赖 Qt**，便于单元测试与无窗口模式复用。

设计要点：

- 端口优先用调用方指定的（默认 8765），被占用时自动改用系统分配的空闲端口；
- 自己先绑定并监听 socket，再用 ``uvicorn.Server.run(sockets=[sock])`` 启动，
  这样能准确知道最终端口，也便于停止（这是 ``tests/test_api_sse.py`` 里已验证过的做法）；
- 所有对业务的访问都走现有 REST 接口（``/api/health``、``/api/jobs``），不复制任何业务逻辑。
"""

from __future__ import annotations

import json
import socket
import threading
import time
from dataclasses import dataclass, field
from urllib.error import URLError
from urllib.request import Request, urlopen

import uvicorn

#: 默认端口（与 run_web.cmd / CLI 保持一致）
DEFAULT_PORT = 8765
#: 本机回环地址
DEFAULT_HOST = "127.0.0.1"


def pick_port(host: str = DEFAULT_HOST, preferred: int = DEFAULT_PORT) -> socket.socket:
    """返回一个**已绑定并监听**的 socket：优先 ``preferred``，被占用则用系统分配的端口。"""
    candidates = [preferred] if preferred == 0 else [preferred, 0]
    last_error: OSError | None = None
    for candidate in candidates:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind((host, candidate))
            sock.listen(128)
            return sock
        except OSError as exc:
            last_error = exc
            sock.close()
    raise OSError(f"无法在 {host} 上绑定端口（首选 {preferred}）：{last_error}")


@dataclass
class ServerHandle:
    """一个已启动的本地服务。"""

    host: str
    port: int
    sock: socket.socket = field(repr=False)
    server: uvicorn.Server = field(repr=False)
    thread: threading.Thread = field(repr=False)

    @property
    def url(self) -> str:
        """服务根地址，例如 ``http://127.0.0.1:8765``。"""
        return f"http://{self.host}:{self.port}"

    @property
    def running(self) -> bool:
        """服务线程是否仍在运行。"""
        return self.thread.is_alive()

    def stop(self, timeout: float = 8.0) -> None:
        """请求优雅退出并等待线程结束（超时也不阻塞退出）。"""
        self.server.should_exit = True
        self.thread.join(timeout=timeout)
        try:
            self.sock.close()
        except OSError:
            pass


def start_server(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    *,
    log_level: str = "warning",
) -> ServerHandle:
    """启动本地 Web 服务（后台线程），返回句柄。"""
    from ..api.app import app  # 延迟导入：测试里可替换 / 避免 import 期副作用

    sock = pick_port(host, port)
    bound_port = int(sock.getsockname()[1])
    # log_config=None：不套用 uvicorn 的默认日志配置。
    # 打包成窗口模式（PyInstaller --windowed）时 sys.stdout/stderr 是 None，
    # uvicorn 默认配置会去写 stderr 而直接崩溃，因此这里完全交给桌面层的日志。
    server = uvicorn.Server(uvicorn.Config(app, log_level=log_level, log_config=None))
    thread = threading.Thread(
        target=server.run,
        kwargs={"sockets": [sock]},
        name="picsaver-desktop-http",
        daemon=True,
    )
    thread.start()
    return ServerHandle(host=host, port=bound_port, sock=sock, server=server, thread=thread)


def wait_health(url: str, timeout: float = 30.0, interval: float = 0.1) -> bool:
    """等待 ``/api/health`` 可用。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urlopen(f"{url}/api/health", timeout=2) as response:
                if response.status == 200:
                    return True
        except (URLError, OSError):
            pass
        time.sleep(interval)
    return False


def fetch_index(url: str, timeout: float = 5.0) -> str:
    """取回首页 HTML（供 smoke 校验静态资源是否真的可用）。"""
    with urlopen(f"{url}/", timeout=timeout) as response:
        return response.read().decode("utf-8", "ignore")


def active_jobs(url: str, timeout: float = 5.0) -> list[dict]:
    """列出排队中/运行中的任务（用于"退出前提示"）。失败时返回空列表。"""
    try:
        with urlopen(f"{url}/api/jobs?brief=1", timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - 桌面退出流程不能因为网络错误卡住
        return []
    jobs = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(jobs, list):
        return []
    return [item for item in jobs if isinstance(item, dict) and item.get("status") in ("queued", "running")]


def cancel_jobs(url: str, job_ids: list[str], timeout: float = 5.0) -> int:
    """请求取消指定任务，返回成功条数。"""
    cancelled = 0
    for job_id in job_ids:
        request = Request(
            f"{url}/api/jobs/{job_id}/cancel",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                if response.status == 200:
                    cancelled += 1
        except Exception:  # noqa: BLE001 - 尽力而为
            continue
    return cancelled


def wait_no_active_jobs(url: str, timeout: float = 5.0, interval: float = 0.2) -> bool:
    """等待所有任务结束（退出前给正在运行的任务一点收尾时间）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not active_jobs(url):
            return True
        time.sleep(interval)
    return not active_jobs(url)
