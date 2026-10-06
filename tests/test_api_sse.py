"""SSE 实时进度的真实服务测试：用 uvicorn 起一个真实服务，走 HTTP 收进度事件。

TestClient 对 SSE 支持有限，这里直接起 uvicorn（端口由系统分配），验证真实链路。
"""

from __future__ import annotations

import json
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from sse_starlette.sse import AppStatus

from fixtures import make_pair
from picsaver.api.app import app

from datetime import datetime


@pytest.fixture(scope="session")
def live_server():
    """在后台线程里启动**一个**真实 uvicorn 服务，供所有 SSE 测试共用。

    注意：``sse-starlette`` 的 ``AppStatus.should_exit_event`` 是进程级的
    asyncio.Event，与第一个事件循环绑定；同进程内起第二个服务会报
    "bound to a different event loop"，所以这里用会话级共享服务，
    并在启动前显式复位该状态。
    """
    AppStatus.should_exit_event = None

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    port = sock.getsockname()[1]

    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started, "uvicorn 未能启动"

    yield f"http://127.0.0.1:{port}"

    server.should_exit = True
    thread.join(timeout=15)
    sock.close()


@pytest.fixture()
def card(tmp_path):
    root = tmp_path / "card"
    for index in range(5):
        make_pair(root / "DCIM", f"DSC{index:05d}", datetime(2026, 7, 4, 10, index, 0), suffixes=("jpg", "arw"))
    return root


def test_http_and_sse_progress(live_server, card, tmp_path) -> None:
    out = tmp_path / "out"
    config = {
        "source_dir": str(card),
        "suffixes": ["jpg", "arw"],
        "date_from": "2026-07-04",
        "date_to": "2026-07-06",
        "target_parent": str(out),
        "place": "北海道",
        "note": "家庭旅行",
        "recursive": True,
    }
    with httpx.Client(base_url=live_server, timeout=30.0) as client:
        assert client.get("/api/health").json()["status"] == "ok"
        assert client.get("/").status_code == 200
        assert client.get("/static/app.js").status_code == 200

        job_id = client.post("/api/jobs", json={"config": config, "dry_run": False}).json()["job_id"]

        events: list[dict] = []
        terminal: dict = {}
        with client.stream("GET", f"/api/jobs/{job_id}/events", timeout=60.0) as stream:
            assert stream.status_code == 200
            for line in stream.iter_lines():
                if not line.startswith("data:"):
                    continue
                payload = json.loads(line[5:].strip())
                if "report" in payload or "status" in payload:
                    terminal = payload
                    break
                events.append(payload)

        assert events, "SSE 必须推送实时进度事件"
        # 同一阶段内百分比必须单调不减；扫描与复制是两个阶段，各自从 0 到 100
        for phase in ("scanning", "copying"):
            series = [event["percent"] for event in events if event["phase"] == phase]
            assert series, f"缺少 {phase} 阶段的进度事件"
            assert series == sorted(series), f"{phase} 阶段进度必须单调不减：{series}"

        scanning = [event for event in events if event["phase"] == "scanning"]
        assert scanning[-1]["scan_total_files"] == 10
        assert scanning[-1]["scanned_files"] == 10
        assert scanning[-1]["total_files"] == 10

        copying = [event for event in events if event["phase"] == "copying"]
        assert copying[-1]["done_bytes"] == copying[-1]["total_bytes"]
        assert copying[-1]["total_files"] == 10
        assert terminal["status"] == "done"
        assert terminal["report"]["copied_files"] == 10

        status = client.get(f"/api/jobs/{job_id}").json()
        assert status["progress"]["percent"] == 100.0
        assert (out / "[20260704]北海道-家庭旅行" / "JPG" / "DSC00000.jpg").exists()
        assert (out / "[20260704]北海道-家庭旅行" / "ARW" / "DSC00000.arw").exists()


def test_preview_job_streams_scan_progress(live_server, card, tmp_path) -> None:
    """预览计划也要有真实进度：SSE 里能看到"已扫描/共多少"，且全程不落盘。"""
    out = tmp_path / "preview-out"
    config = {
        "source_dir": str(card),
        "suffixes": ["jpg", "arw"],
        "date_from": "2026-07-04",
        "date_to": "2026-07-06",
        "target_parent": str(out),
        "place": "北海道",
        "note": "预览",
        "recursive": True,
    }
    with httpx.Client(base_url=live_server, timeout=30.0) as client:
        job = client.post("/api/jobs", json={"config": config, "mode": "preview"}).json()
        assert job["mode"] == "preview"

        events: list[dict] = []
        terminal: dict = {}
        with client.stream("GET", f"/api/jobs/{job['job_id']}/events", timeout=60.0) as stream:
            for line in stream.iter_lines():
                if not line.startswith("data:"):
                    continue
                payload = json.loads(line[5:].strip())
                if "report" in payload or "status" in payload:
                    terminal = payload
                    break
                events.append(payload)

        scanning = [event for event in events if event["phase"] == "scanning"]
        assert scanning, "预览任务必须推送扫描阶段进度"
        assert scanning[-1]["scan_total_files"] == 10
        assert scanning[-1]["scanned_files"] == 10
        percents = [event["percent"] for event in scanning]
        assert percents == sorted(percents)
        assert percents[-1] == 100.0

        assert terminal["status"] == "done"
        assert terminal["report"]["mode"] == "preview"
        assert terminal["report"]["plan"]["planned_files"] == 10
        assert not out.exists(), "预览任务绝不能写盘"


def test_terminal_event_always_has_terminal_phase(live_server, card) -> None:
    """终局事件的 phase 必须是终局阶段（finished/canceled），不能停留在 scanning。"""
    config = {
        "source_dir": str(card),
        "suffixes": ["jpg", "arw"],
        "date_from": "2026-07-04",
        "date_to": "2026-07-06",
        "target_parent": "",
        "place": "",
        "note": "",
        "recursive": True,
    }
    with httpx.Client(base_url=live_server, timeout=30.0) as client:
        job = client.post("/api/jobs", json={"config": config, "mode": "scan"}).json()
        # 立刻尝试取消（可能来不及，两种结果都合法，但 phase 必须与 status 对应）
        client.post(f"/api/jobs/{job['job_id']}/cancel")

        terminal = {}
        with client.stream("GET", f"/api/jobs/{job['job_id']}/events", timeout=60.0) as stream:
            for line in stream.iter_lines():
                if not line.startswith("data:"):
                    continue
                payload = json.loads(line[5:].strip())
                if "status" in payload:
                    terminal = payload
                    break

        expected = {"done": "finished", "canceled": "canceled", "failed": "failed"}
        assert terminal, "没有收到终局事件"
        assert terminal["phase"] == expected[terminal["status"]], terminal
