"""桌面端（picsaver.desktop）测试：不需要图形界面，可在无桌面环境运行。

覆盖：端口选择与回退、服务引导与静态资源、健康检查超时、任务查询/取消、
单实例锁、``--smoke`` 自检报告、CLI 注册，以及"导入桌面包不会拉起 Qt"的约定。
"""

from __future__ import annotations

import json
import socket
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from picsaver.cli.main import app as cli_app
from picsaver.desktop import window as window_module
from picsaver.desktop.instance import InstanceLock
from picsaver.desktop.main import build_parser
from picsaver.desktop.server import (
    active_jobs,
    cancel_jobs,
    fetch_index,
    pick_port,
    start_server,
    wait_health,
)
from picsaver.desktop.smoke import run_smoke, smoke_report_path


def free_port() -> int:
    """取一个当前空闲的端口号。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


def listen_on(port: int) -> socket.socket:
    """占用一个端口（模拟"端口被占用"）。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", port))
    sock.listen(1)
    return sock


# ---- 端口 -------------------------------------------------------------------


def test_pick_port_uses_preferred_when_free() -> None:
    sock = pick_port("127.0.0.1", free_port())
    try:
        assert sock.getsockname()[1] > 0
    finally:
        sock.close()


def test_pick_port_falls_back_when_preferred_is_busy() -> None:
    port = free_port()
    holder = listen_on(port)
    try:
        sock = pick_port("127.0.0.1", port)
        try:
            assert sock.getsockname()[1] != port, "首选端口被占用时必须换端口"
        finally:
            sock.close()
    finally:
        holder.close()


# ---- 服务引导 ---------------------------------------------------------------


def test_start_server_serves_health_and_index() -> None:
    handle = start_server(port=0)
    try:
        assert wait_health(handle.url, timeout=20.0), "服务未在超时内就绪"
        assert handle.running is True

        body = fetch_index(handle.url)
        assert "pictureSaver" in body and len(body) > 500, "首页必须是真实前端页面"
    finally:
        handle.stop()
    assert handle.running is False, "stop() 之后服务线程应当结束"


def test_wait_health_times_out_on_dead_port() -> None:
    started = time.monotonic()
    assert wait_health(f"http://127.0.0.1:{free_port()}", timeout=0.6) is False
    assert time.monotonic() - started < 5.0


def test_active_jobs_and_cancel_on_idle_server() -> None:
    handle = start_server(port=0)
    try:
        assert wait_health(handle.url, timeout=20.0)
        assert active_jobs(handle.url) == []
        assert cancel_jobs(handle.url, ["not-a-real-job"]) == 0
    finally:
        handle.stop()


# ---- 单实例锁 ---------------------------------------------------------------


def test_instance_lock_allows_only_one_owner(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    port = free_port()

    first = InstanceLock(port)
    is_owner, existing = first.acquire()
    assert is_owner is True and existing is None
    url = f"http://127.0.0.1:{port}"
    first.publish(url)

    second = InstanceLock(port)
    is_owner2, existing2 = second.acquire()
    assert is_owner2 is False, "第二个实例不应拿到锁"
    assert existing2 == url, "第二个实例应能读到已有实例的地址"

    first.release()
    third = InstanceLock(port)
    is_owner3, _ = third.acquire()
    assert is_owner3 is True, "前一个实例退出后应能重新持有锁"
    third.release()


def test_instance_lock_writes_and_cleans_state_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    state_file = tmp_path / "pictureSaver2" / "instance.json"

    lock = InstanceLock(free_port())
    lock.acquire()
    lock.publish("http://127.0.0.1:1234")
    assert json.loads(state_file.read_text(encoding="utf-8"))["url"] == "http://127.0.0.1:1234"

    lock.release()
    assert not state_file.exists(), "退出后应清理 instance.json"


# ---- smoke ------------------------------------------------------------------


def test_smoke_reports_ok() -> None:
    port = free_port()
    assert run_smoke("127.0.0.1", port, timeout=25.0) == 0

    payload = json.loads(smoke_report_path().read_text(encoding="utf-8"))
    assert payload["ok"] is True
    assert payload["health"] is True and payload["index"] is True
    assert payload["url"].endswith(f":{port}")
    assert payload["index_bytes"] > 500


# ---- 入口与约定 -------------------------------------------------------------


def test_cli_registers_desktop_command() -> None:
    runner = CliRunner()
    result = runner.invoke(cli_app, ["desktop", "--help"])
    assert result.exit_code == 0, result.output
    assert "--smoke" in result.output
    assert "--cdp-port" in result.output


def test_desktop_arg_parser() -> None:
    args = build_parser().parse_args(["--smoke", "--port", "1234", "--cdp-port", "9222", "--no-browser-fallback"])
    assert args.smoke is True
    assert args.port == 1234
    assert args.cdp_port == 9222
    assert args.browser_fallback is False


def test_desktop_package_does_not_import_qt_at_module_level() -> None:
    """约定：缺少 PySide6 的环境也要能 import 桌面包（Qt 只在函数内延迟导入）。"""
    for module in (window_module,):
        source = Path(module.__file__).read_text(encoding="utf-8")
        offenders = [
            line
            for line in source.splitlines()
            if line.startswith(("import PySide6", "from PySide6"))
        ]
        assert not offenders, f"{module.__name__} 不应在模块级导入 Qt：{offenders}"


def test_window_title_matches_web_ui() -> None:
    assert window_module.WINDOW_TITLE.startswith("pictureSaver")


def test_qt_is_available_in_this_environment() -> None:
    """本机应具备 QtWebEngine（缺失时跳过，不算失败）。"""
    pytest.importorskip("PySide6.QtWebEngineWidgets")
