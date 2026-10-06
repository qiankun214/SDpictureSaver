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

from fixtures import make_pair
from picsaver.api.app import app

from datetime import datetime


@pytest.fixture()
def live_server():
    """在后台线程里启动真实 uvicorn 服务，产出 base_url。"""
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
        percents = [event["percent"] for event in events]
        assert percents == sorted(percents), "进度百分比必须单调不减"
        assert events[-1]["done_bytes"] == events[-1]["total_bytes"]
        assert events[-1]["total_files"] == 10
        assert terminal["status"] == "done"
        assert terminal["report"]["copied_files"] == 10

        status = client.get(f"/api/jobs/{job_id}").json()
        assert status["progress"]["percent"] == 100.0
        assert (out / "[20260704]北海道-家庭旅行" / "JPG" / "DSC00000.jpg").exists()
        assert (out / "[20260704]北海道-家庭旅行" / "ARW" / "DSC00000.arw").exists()
