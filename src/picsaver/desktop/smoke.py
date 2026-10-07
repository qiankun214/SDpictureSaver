"""``--smoke`` 自检模式：只起服务、校验接口与静态资源，然后退出（**不创建窗口**）。

窗口模式下 exe 没有控制台，因此结果同时写入
``%TEMP%\\picsaver2-smoke.json``，供构建脚本与自动化验证读取。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

from .server import DEFAULT_HOST, DEFAULT_PORT, fetch_index, start_server, wait_health

#: smoke 结果文件（固定路径，便于脚本查找）
SMOKE_REPORT_NAME = "picsaver2-smoke.json"


def smoke_report_path() -> Path:
    """smoke 结果文件路径。"""
    return Path(tempfile.gettempdir()) / SMOKE_REPORT_NAME


def run_smoke(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, *, timeout: float = 30.0) -> int:
    """起服务 → 校验 ``/api/health`` 与首页 → 写结果 → 退出。

    :return: 0 表示成功，1 表示失败
    """
    handle = start_server(host, port)
    payload: dict = {"ok": False, "url": handle.url, "pid": os.getpid(), "health": False, "index": False, "detail": ""}
    try:
        healthy = wait_health(handle.url, timeout=timeout)
        payload["health"] = healthy
        if healthy:
            try:
                body = fetch_index(handle.url)
                payload["index"] = "pictureSaver" in body and len(body) > 500
                payload["index_bytes"] = len(body)
            except Exception as exc:  # noqa: BLE001 - 首页失败也要给出原因
                payload["detail"] = f"{type(exc).__name__}: {exc}"
        else:
            payload["detail"] = f"{timeout:.0f} 秒内 /api/health 未就绪"
        payload["ok"] = bool(payload["health"] and payload["index"])
    finally:
        handle.stop()

    try:
        smoke_report_path().write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass
    if sys.stdout is not None:
        try:
            print(json.dumps(payload, ensure_ascii=False))
        except Exception:  # noqa: BLE001 - 无控制台的窗口模式会走到这里
            pass
    return 0 if payload["ok"] else 1
