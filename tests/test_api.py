"""API 测试：REST 接口、参数校验、后台任务与进度轮询（SSE 另在实机验证中检查）。"""

from __future__ import annotations

import time
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from fixtures import make_jpeg, make_pair

from picsaver.api.app import app


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture()
def card(tmp_path):
    root = tmp_path / "card"
    make_pair(root / "DCIM", "DSC00001", datetime(2026, 7, 4, 9, 0, 0), suffixes=("jpg", "arw"))
    make_pair(root / "DCIM", "DSC00002", datetime(2026, 7, 5, 10, 0, 0), suffixes=("jpg", "orf"))
    make_pair(root / "DCIM", "DSC00003", datetime(2026, 7, 20, 10, 0, 0), suffixes=("jpg", "arw"))
    make_jpeg(root / "DCIM" / "NOTIME.JPG", None)
    return root


def job_payload(card, out, **overrides) -> dict:
    body = {
        "source_dir": str(card),
        "suffixes": ["jpg", "arw", "orf"],
        "date_from": "2026-07-04",
        "date_to": "2026-07-06",
        "target_parent": str(out),
        "place": "北海道",
        "note": "家庭旅行",
        "recursive": True,
    }
    body.update(overrides)
    return body


def wait_for(client: TestClient, job_id: str, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    payload: dict = {}
    while time.time() < deadline:
        response = client.get(f"/api/jobs/{job_id}")
        assert response.status_code == 200
        payload = response.json()
        if payload["status"] != "running":
            return payload
        time.sleep(0.05)
    raise AssertionError(f"任务超时未结束：{payload}")


def test_health_and_defaults(client: TestClient) -> None:
    health = client.get("/api/health").json()
    assert health["status"] == "ok"
    assert health["version"]

    defaults = client.get("/api/defaults").json()
    assert defaults["suffixes"] == ["jpg", "arw", "orf"]
    assert len(defaults["today"]) == 10  # YYYY-MM-DD


def test_browse_root_lists_drives(client: TestClient) -> None:
    payload = client.get("/api/browse").json()
    assert payload["path"] == ""
    assert payload["dirs"], "Windows 下至少应列出一个盘符"
    assert all(item["path"].endswith("\\") for item in payload["dirs"])


def test_browse_directory_and_missing(client: TestClient, card) -> None:
    payload = client.get("/api/browse", params={"path": str(card)}).json()
    assert payload["error"] is None
    assert [item["name"] for item in payload["dirs"]] == ["DCIM"]

    missing = client.get("/api/browse", params={"path": str(card / "nope")}).json()
    assert missing["error"]
    assert missing["dirs"] == []


def test_scan_endpoint(client: TestClient, card) -> None:
    response = client.post("/api/scan", json={"source_dir": str(card), "suffixes": ["jpg", "arw", "orf"]})
    assert response.status_code == 200
    payload = response.json()
    assert payload["groups"] == 4
    assert payload["matched_files"] == 7
    assert payload["suffix_counts"] == {"arw": 2, "jpg": 4, "orf": 1}
    assert payload["no_time"] == 1
    assert payload["no_time_files"][0]["path"].endswith("NOTIME.JPG")


def test_scan_invalid_suffix(client: TestClient, card) -> None:
    response = client.post("/api/scan", json={"source_dir": str(card), "suffixes": ["png"]})
    assert response.status_code == 422  # pydantic 校验失败


def test_scan_missing_dir(client: TestClient, tmp_path) -> None:
    response = client.post("/api/scan", json={"source_dir": str(tmp_path / "nope"), "suffixes": ["jpg"]})
    assert response.status_code == 400
    assert "不存在" in response.json()["detail"]


def test_preview_endpoint(client: TestClient, card, tmp_path) -> None:
    response = client.post("/api/preview", json=job_payload(card, tmp_path / "out"))
    assert response.status_code == 200
    payload = response.json()
    assert payload["folder_name"] == "[20260704]北海道-家庭旅行"
    assert payload["planned_files"] == 4
    assert payload["out_of_range_files"] == 2
    assert payload["no_time_files"] == 1
    assert payload["suffix_dirs"] == ["ARW", "JPG", "ORF"]
    assert not (tmp_path / "out").exists()


def test_preview_invalid_date_range(client: TestClient, card, tmp_path) -> None:
    response = client.post(
        "/api/preview",
        json=job_payload(card, tmp_path / "out", date_from="2026-07-10", date_to="2026-07-01"),
    )
    assert response.status_code == 400
    assert "开始日期" in response.json()["detail"]


def test_preview_empty_place_rejected(client: TestClient, card, tmp_path) -> None:
    """地点为空：schema 允许（扫描任务不需要），但预览/拷贝会在 to_config 阶段拒绝。"""
    response = client.post("/api/preview", json=job_payload(card, tmp_path / "out", place="   "))
    assert response.status_code == 400
    assert "地点" in response.json()["detail"]


def test_job_lifecycle_and_report(client: TestClient, card, tmp_path) -> None:
    out = tmp_path / "out"
    response = client.post("/api/jobs", json={"config": job_payload(card, out), "dry_run": False})
    assert response.status_code == 200
    job_id = response.json()["job_id"]

    payload = wait_for(client, job_id)
    assert payload["status"] == "done", payload
    assert payload["error"] is None
    report = payload["report"]
    assert report["copied_files"] == 4
    assert report["no_time_files"] == 1
    assert report["out_of_range_files"] == 2
    assert (out / "[20260704]北海道-家庭旅行" / "JPG" / "DSC00001.jpg").exists()
    assert (out / "[20260704]北海道-家庭旅行" / "ARW" / "DSC00001.arw").exists()
    assert payload["progress"]["percent"] == 100.0
    assert payload["progress"]["phase"] == "finished"
    assert payload["progress"]["done_bytes"] == payload["progress"]["total_bytes"]


def test_job_dry_run_writes_nothing(client: TestClient, card, tmp_path) -> None:
    out = tmp_path / "out-dry"
    response = client.post("/api/jobs", json={"config": job_payload(card, out), "dry_run": True})
    job_id = response.json()["job_id"]
    payload = wait_for(client, job_id)
    assert payload["status"] == "done"
    assert payload["report"]["dry_run"] is True
    assert not out.exists()


def test_job_invalid_config_returns_400(client: TestClient, card, tmp_path) -> None:
    response = client.post(
        "/api/jobs",
        json={"config": job_payload(card, card / "inside"), "dry_run": False},
    )
    assert response.status_code == 400
    assert "原始文件夹" in response.json()["detail"]


def test_job_not_found(client: TestClient) -> None:
    assert client.get("/api/jobs/deadbeef").status_code == 404
    assert client.post("/api/jobs/deadbeef/cancel").status_code == 404


def test_cancel_after_finish_is_accepted(client: TestClient, card, tmp_path) -> None:
    out = tmp_path / "out-cancel"
    response = client.post("/api/jobs", json={"config": job_payload(card, out), "dry_run": True})
    job_id = response.json()["job_id"]
    wait_for(client, job_id)
    cancel = client.post(f"/api/jobs/{job_id}/cancel")
    assert cancel.status_code == 200
    assert cancel.json()["canceled"] is True


def test_jobs_list(client: TestClient, card, tmp_path) -> None:
    response = client.post("/api/jobs", json={"config": job_payload(card, tmp_path / "out-list"), "dry_run": True})
    job_id = response.json()["job_id"]
    wait_for(client, job_id)
    payload = client.get("/api/jobs").json()
    assert any(job["job_id"] == job_id for job in payload["jobs"])


# ---- 任务模式：scan / preview / copy ---------------------------------------


def test_job_scan_mode(client: TestClient, card) -> None:
    """扫描任务：只统计，不落盘，报告里带 scan 结果。"""
    response = client.post("/api/jobs", json={"config": job_payload(card, ""), "mode": "scan"})
    assert response.status_code == 200
    assert response.json()["mode"] == "scan"

    payload = wait_for(client, response.json()["job_id"])
    assert payload["status"] == "done"
    assert payload["mode"] == "scan"
    assert payload["report"]["mode"] == "scan"
    assert payload["report"]["canceled"] is False
    scan = payload["report"]["scan"]
    assert scan["matched_files"] == 7
    assert scan["groups"] == 4
    assert scan["no_time"] == 1
    assert payload["progress"]["percent"] == 100.0


def test_job_scan_mode_accepts_incomplete_form(client: TestClient, card) -> None:
    """扫描任务只需源目录与后缀：日期 / 地点 / 备注 / 目标目录都可以是空的。"""
    body = {
        "source_dir": str(card),
        "suffixes": ["jpg"],
        "date_from": "",
        "date_to": None,
        "target_parent": "",
        "place": "",
        "note": "",
        "recursive": True,
    }
    response = client.post("/api/jobs", json={"config": body, "mode": "scan"})
    assert response.status_code == 200, response.text
    payload = wait_for(client, response.json()["job_id"])
    assert payload["status"] == "done"
    assert payload["report"]["scan"]["suffix_counts"] == {"jpg": 4}


def test_job_preview_mode(client: TestClient, card, tmp_path) -> None:
    """预览任务：给出计划但绝不落盘。"""
    out = tmp_path / "out-preview"
    response = client.post("/api/jobs", json={"config": job_payload(card, out), "mode": "preview"})
    assert response.status_code == 200
    assert response.json()["mode"] == "preview"

    payload = wait_for(client, response.json()["job_id"])
    assert payload["status"] == "done"
    assert payload["report"]["mode"] == "preview"
    assert payload["report"]["plan"]["folder_name"] == "[20260704]北海道-家庭旅行"
    assert payload["report"]["plan"]["planned_files"] == 4
    assert payload["report"]["scan"]["matched_files"] == 7
    assert payload["progress"]["percent"] == 100.0
    assert not out.exists()


def test_job_preview_requires_full_config(client: TestClient, card, tmp_path) -> None:
    response = client.post(
        "/api/jobs",
        json={"config": job_payload(card, tmp_path / "out", place=""), "mode": "preview"},
    )
    assert response.status_code == 400
    assert "地点" in response.json()["detail"]


def test_job_invalid_mode_rejected(client: TestClient, card) -> None:
    response = client.post("/api/jobs", json={"config": job_payload(card, ""), "mode": "move"})
    assert response.status_code == 422


def test_job_copy_mode_progress_has_scan_fields(client: TestClient, card, tmp_path) -> None:
    """复制任务的进度里保留扫描阶段的字段（界面要显示已扫描文件数）。"""
    response = client.post("/api/jobs", json={"config": job_payload(card, tmp_path / "out-scan-fields"), "mode": "copy"})
    job_id = response.json()["job_id"]
    payload = wait_for(client, job_id)
    progress = payload["progress"]
    assert progress["scan_total_files"] == 7
    assert progress["scanned_files"] == 7
    assert progress["phase"] == "finished"
    assert progress["percent"] == 100.0
