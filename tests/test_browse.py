"""目录浏览与新建文件夹的测试。

重点覆盖用户反馈的两个问题：

1. 点选目录时列表太慢 —— 浏览接口只列文件夹、不对普通文件做 stat，
   这里用"两万个文件的目录"做回归：老实现约 1.4 秒，新实现十几毫秒。
2. 缺少新建文件夹 —— 覆盖成功与各类非法输入。
"""

from __future__ import annotations

import os
import time

import pytest
from fastapi.testclient import TestClient

from picsaver.api.app import MAX_BROWSE_DIRS, app

#: 回归用的大目录文件数（文件全部用硬链接创建，避免写盘拖慢测试）
BIG_FILE_COUNT = 20000


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


def make_files(directory, count: int) -> None:
    """快速造出 `count` 个文件：优先硬链接，失败则退回创建空文件。"""
    template = directory / "_template.bin"
    template.write_bytes(b"x")
    for index in range(count):
        path = directory / f"DSC{index:05d}.JPG"
        try:
            os.link(template, path)
        except OSError:
            path.touch()
    template.unlink()


def test_browse_lists_only_dirs_in_a_huge_folder(client: TestClient, tmp_path) -> None:
    inner = tmp_path / "card" / "100MSDCF"
    inner.mkdir(parents=True)
    make_files(inner, BIG_FILE_COUNT)
    for name in ("aaa", "bbb", "ccc"):
        (inner / name).mkdir()

    started = time.perf_counter()
    response = client.get("/api/browse", params={"path": str(inner)})
    wall_ms = (time.perf_counter() - started) * 1000

    assert response.status_code == 200
    payload = response.json()
    # 只列文件夹，两万个文件一个都不能出现
    assert [item["name"] for item in payload["dirs"]] == ["aaa", "bbb", "ccc"]
    assert payload["total_dirs"] == 3
    assert payload["truncated"] is False
    assert payload["error"] is None
    # 老实现（Path.iterdir + is_dir）在这个规模下需要 1 秒以上
    assert payload["elapsed_ms"] < 1000, f"浏览耗时 {payload['elapsed_ms']} ms"
    assert wall_ms < 3000, f"含 HTTP 开销 {wall_ms:.0f} ms"


def test_browse_truncates_huge_folder_lists(client: TestClient, tmp_path) -> None:
    parent = tmp_path / "many"
    parent.mkdir()
    total = MAX_BROWSE_DIRS + 5
    for index in range(total):
        (parent / f"dir{index:05d}").mkdir()

    payload = client.get("/api/browse", params={"path": str(parent)}).json()
    assert payload["total_dirs"] == total
    assert payload["truncated"] is True
    assert len(payload["dirs"]) == MAX_BROWSE_DIRS


def test_browse_root_and_missing_dir(client: TestClient, tmp_path) -> None:
    roots = client.get("/api/browse").json()
    assert roots["truncated"] is False
    assert roots["total_dirs"] == len(roots["dirs"])

    missing = client.get("/api/browse", params={"path": str(tmp_path / "nope")}).json()
    assert missing["error"]
    assert missing["dirs"] == []
    assert missing["total_dirs"] == 0


def test_mkdir_creates_folder(client: TestClient, tmp_path) -> None:
    parent = tmp_path / "父目录"
    parent.mkdir()
    response = client.post("/api/mkdir", json={"parent": str(parent), "name": "2026北海道"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["created"] is True
    assert payload["path"] == str(parent / "2026北海道")
    assert (parent / "2026北海道").is_dir()
    # 新建后可以被浏览接口列出来
    listed = client.get("/api/browse", params={"path": str(parent)}).json()
    assert [item["name"] for item in listed["dirs"]] == ["2026北海道"]


@pytest.mark.parametrize(
    "payload, keyword",
    [
        ({"parent": "", "name": "x"}, "父目录"),
        ({"parent": "E:\\this-path-does-not-exist-9f3a", "name": "x"}, "父目录"),
        ({"parent": "@parent@", "name": "   "}, "文件夹名"),
        ({"parent": "@parent@", "name": " 前后有空格 "}, "空格"),
        ({"parent": "@parent@", "name": ".开头有点"}, "点"),
        ({"parent": "@parent@", "name": "结尾有点."}, "点"),
        ({"parent": "@parent@", "name": "bad/name"}, "文件夹名"),
        ({"parent": "@parent@", "name": "bad:name"}, "文件夹名"),
        ({"parent": "@parent@", "name": "bad*name"}, "文件夹名"),
        ({"parent": "@parent@", "name": "CON"}, "保留"),
        ({"parent": "@parent@", "name": "x" * 101}, "过长"),
    ],
)
def test_mkdir_rejects_invalid_input(client: TestClient, tmp_path, payload, keyword) -> None:
    """所有名称/父目录错误都应该是 400 + 中文字符串 detail（便于界面直接展示）。"""
    parent = tmp_path / "parent"
    parent.mkdir()
    data = {key: (str(parent) if value == "@parent@" else value) for key, value in payload.items()}
    response = client.post("/api/mkdir", json=data)
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert isinstance(detail, str), detail
    assert keyword in detail


def test_mkdir_rejects_existing_name(client: TestClient, tmp_path) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    (parent / "已存在").mkdir()
    response = client.post("/api/mkdir", json={"parent": str(parent), "name": "已存在"})
    assert response.status_code == 400
    assert "已存在" in response.json()["detail"]

    (parent / "同名文件.txt").write_text("x", encoding="utf-8")
    file_response = client.post("/api/mkdir", json={"parent": str(parent), "name": "同名文件.txt"})
    assert file_response.status_code == 400
