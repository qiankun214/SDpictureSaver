"""CDP（Chrome DevTools Protocol）极简客户端，供 scripts/ 下的浏览器验证脚本复用。"""

from __future__ import annotations

import base64
import json
import time
import urllib.request
from pathlib import Path

from websockets.sync.client import connect


class CDP:
    """极简 Chrome DevTools Protocol 客户端。"""

    def __init__(self, ws_url: str) -> None:
        self.ws = connect(ws_url, max_size=256 * 1024 * 1024, open_timeout=20)
        self.counter = 0

    def call(self, method: str, **params):
        """发一条 CDP 命令并等同 id 的响应。"""
        self.counter += 1
        message_id = self.counter
        self.ws.send(json.dumps({"id": message_id, "method": method, "params": params}))
        deadline = time.time() + 60
        while time.time() < deadline:
            message = json.loads(self.ws.recv(timeout=60))
            if message.get("id") == message_id:
                if "error" in message:
                    raise RuntimeError(f"{method}: {message['error']}")
                return message.get("result", {})
        raise TimeoutError(method)

    def evaluate(self, expression: str):
        """执行 JS 并取回值（支持 await Promise）。"""
        result = self.call("Runtime.evaluate", expression=expression, returnByValue=True, awaitPromise=True)
        return result.get("result", {}).get("value")

    def shot(self, shots_dir: Path, name: str) -> None:
        """整页截图。"""
        data = self.call("Page.captureScreenshot", format="png")["data"]
        (shots_dir / name).write_bytes(base64.b64decode(data))


def find_page_ws(base_url: str, debug_port: int, timeout: float = 30.0) -> str:
    """在调试端口上找到目标页面并返回其 WebSocket 调试地址。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{debug_port}/json/list", timeout=3) as response:
                targets = json.load(response)
            for target in targets:
                if target.get("type") == "page" and base_url.split("//")[-1] in target.get("url", ""):
                    return target["webSocketDebuggerUrl"]
        except Exception:  # noqa: BLE001 - 浏览器/端口还没就绪
            pass
        time.sleep(0.5)
    raise RuntimeError(f"在 {debug_port} 端口找不到已打开页面的调试目标")


def wait_for(cdp: CDP, expression: str, timeout: float = 60.0, interval: float = 0.05):
    """轮询等待 JS 表达式为真，返回最后取值。"""
    deadline = time.time() + timeout
    value = None
    while time.time() < deadline:
        value = cdp.evaluate(expression)
        if value:
            return value
        time.sleep(interval)
    return value
