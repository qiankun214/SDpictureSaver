"""串行任务队列测试（用桩 service，不碰真实文件系统，保证确定性）。

覆盖：不并发、FIFO、前台插队、排队位置、排队中取消、关闭（dismiss）、
只淘汰终局任务、brief 概要字段。
"""

from __future__ import annotations

import threading
import time
from datetime import date
from pathlib import Path

import pytest

from picsaver.api.jobs import CapacityError, JobManager, JobSpec
from picsaver.core.events import PHASE_COPYING, ProgressEvent
from picsaver.core.models import CopyConfig, CopyPlan, CopyReport, ScanResult
from picsaver.core.service import CopyService

DELAY = 0.12


class StubService(CopyService):
    """把三种任务换成"睡一会儿并记录并发"的假实现。"""

    def __init__(self, delay: float = DELAY) -> None:
        self.delay = delay
        self.calls: list[str] = []
        self.running = 0
        self.max_concurrent = 0
        self._lock = threading.Lock()

    def _work(self, mode: str, progress=None) -> None:
        with self._lock:
            self.running += 1
            self.max_concurrent = max(self.max_concurrent, self.running)
            self.calls.append(mode)
        try:
            time.sleep(self.delay)
        finally:
            with self._lock:
                self.running -= 1

    def scan(self, source_dir, suffixes, *, recursive=True, exclude_dirs=(), progress=None, cancel=None):
        self._work("scan", progress)
        return ScanResult(source_dir=Path(source_dir), matched_files=3)

    def plan_only(self, config, *, progress=None, cancel=None):
        self._work("preview", progress)
        scan = ScanResult(source_dir=config.source_dir, matched_files=3)
        plan = CopyPlan(config=config, folder_name=config.folder_name, target_dir=config.target_dir)
        return scan, plan

    def run(self, config, *, progress=None, cancel=None, dry_run=False):
        self._work("copy", progress)
        plan = CopyPlan(config=config, folder_name=config.folder_name, target_dir=config.target_dir)
        report = CopyReport(
            folder_name=config.folder_name,
            target_dir=config.target_dir,
            total_files=0,
            copied_files=0,
            skipped_existing=0,
            failed_files=0,
            no_time_files=0,
            out_of_range_files=0,
            total_bytes=0,
            copied_bytes=0,
            elapsed_seconds=self.delay,
            dry_run=dry_run,
        )
        return plan, report


def make_config(tmp_path: Path, place: str = "北海道") -> CopyConfig:
    source = tmp_path / "card"
    source.mkdir(exist_ok=True)
    target = tmp_path / "out"
    target.mkdir(exist_ok=True)
    return CopyConfig(
        source_dir=source,
        suffixes=("jpg",),
        date_from=date(2026, 7, 4),
        date_to=date(2026, 7, 6),
        target_parent=target,
        place=place,
        note="家庭旅行",
    )


def make_spec(tmp_path: Path, mode: str = "preview", place: str = "北海道") -> JobSpec:
    config = make_config(tmp_path, place)
    return JobSpec(mode=mode, source_dir=config.source_dir, suffixes=config.suffixes, config=config)


@pytest.fixture()
def manager():
    mgr = JobManager(service=StubService(), max_jobs=30)
    yield mgr
    mgr.shutdown()


def wait_terminal(manager: JobManager, job_id: str, timeout: float = 20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = manager.get(job_id)
        if job is not None and not job.active:
            return job
        time.sleep(0.01)
    raise AssertionError("任务超时未结束")


def test_jobs_run_strictly_one_at_a_time(tmp_path) -> None:
    service = StubService()
    manager = JobManager(service=service, max_jobs=30)
    try:
        jobs = [manager.submit(make_spec(tmp_path, place=f"P{i}")) for i in range(4)]
        time.sleep(DELAY * 0.6)
        statuses = [job.status for job in jobs]
        assert statuses.count("running") <= 1, statuses
        assert statuses.count("queued") >= 2, statuses

        for job in jobs:
            wait_terminal(manager, job.id)
        assert service.max_concurrent == 1, "任何时候都不允许两个任务并发执行"
        assert service.calls == ["preview"] * 4
        # FIFO：完成顺序 = 提交顺序
        finished = sorted(jobs, key=lambda item: item.finished_at or 0)
        assert [job.id for job in finished] == [job.id for job in jobs]
    finally:
        manager.shutdown()


def test_foreground_job_jumps_ahead_of_queued_background_jobs(tmp_path) -> None:
    service = StubService()
    manager = JobManager(service=service, max_jobs=30)
    try:
        first = manager.submit(make_spec(tmp_path, place="后台1"))
        second = manager.submit(make_spec(tmp_path, place="后台2"))
        time.sleep(DELAY * 0.4)  # 让第一个开始跑
        foreground = manager.submit(make_spec(tmp_path, place="前台"), priority=True)

        assert manager.queue_position(foreground) == 2  # 排在正在运行的任务之后、后台2 之前
        for job in (first, second, foreground):
            wait_terminal(manager, job.id)
        order = sorted([first, second, foreground], key=lambda item: item.started_at or 0)
        assert [job.id for job in order] == [first.id, foreground.id, second.id]
        assert service.max_concurrent == 1
    finally:
        manager.shutdown()


def test_queue_positions(tmp_path, manager) -> None:
    first = manager.submit(make_spec(tmp_path, place="A"))
    second = manager.submit(make_spec(tmp_path, place="B"))
    third = manager.submit(make_spec(tmp_path, place="C"))
    positions = [manager.queue_position(job) for job in (first, second, third)]
    # 第一个可能已经在跑（位置 1），其余按顺序排队
    assert positions[0] == 1
    assert positions[1:] == [2, 3], positions
    for job in (first, second, third):
        wait_terminal(manager, job.id)
    assert all(manager.queue_position(job) == 0 for job in (first, second, third))


def test_cancel_queued_job_never_runs(tmp_path) -> None:
    service = StubService()
    manager = JobManager(service=service, max_jobs=30)
    try:
        first = manager.submit(make_spec(tmp_path, place="跑"))
        second = manager.submit(make_spec(tmp_path, place="等"))
        assert manager.cancel(second.id) is True

        assert second.status == "canceled"
        assert second.report is None
        assert manager.queue_position(second) == 0
        assert second.cancel_event.is_set()

        wait_terminal(manager, first.id)
        assert service.calls == ["preview"], "被取消的排队任务绝不能开始执行"
        assert second.finished_at is not None
    finally:
        manager.shutdown()


def test_dismiss_rules(tmp_path, manager) -> None:
    job = manager.submit(make_spec(tmp_path))
    assert manager.dismiss(job.id) == "active"  # 进行中不允许关闭
    wait_terminal(manager, job.id)
    assert manager.dismiss(job.id) == "ok"
    assert manager.get(job.id) is None
    assert manager.dismiss(job.id) == "not_found"


def test_eviction_prefers_terminal_jobs(tmp_path) -> None:
    service = StubService()
    manager = JobManager(service=service, max_jobs=2)
    try:
        running = manager.submit(make_spec(tmp_path, place="跑"))
        time.sleep(DELAY * 0.4)
        waiting = manager.submit(make_spec(tmp_path, place="等"))
        # 两个都在进行中：再提交应当因为无法淘汰而报错
        with pytest.raises(CapacityError):
            manager.submit(make_spec(tmp_path, place="第三个"))

        wait_terminal(manager, running.id)
        wait_terminal(manager, waiting.id)
        # 出现终局任务后，可以继续提交（淘汰最老的终局任务）
        extra = manager.submit(make_spec(tmp_path, place="第四个"))
        assert extra.status in ("queued", "running")
        assert manager.get(running.id) is None
    finally:
        manager.shutdown()


def test_brief_payload_has_no_report_body(tmp_path, manager) -> None:
    job = manager.submit(make_spec(tmp_path, mode="copy"))
    wait_terminal(manager, job.id)

    brief = manager.brief_payload(job)
    assert "report" not in brief
    assert brief["has_report"] is True
    assert brief["status"] == "done"
    assert brief["queue_position"] == 0
    assert brief["age_seconds"] is not None
    assert brief["mode"] == "copy"

    full = manager.payload(job)
    assert full["report"]["mode"] == "copy"

    listed = manager.list_jobs(brief=True)
    assert listed and all("report" not in item for item in listed)
    assert manager.list_jobs(brief=False)[0]["report"]["mode"] == "copy"


def test_target_conflict_only_between_copies(tmp_path, manager) -> None:
    target = tmp_path / "out"
    copy_a = manager.submit(make_spec(tmp_path, mode="copy", place="同名"))
    conflict = manager.find_target_conflict(target / copy_a.spec.config.folder_name)
    assert conflict is not None and conflict.id == copy_a.id

    # 预览任务不参与冲突
    preview = manager.submit(make_spec(tmp_path, mode="preview", place="只读"))
    assert manager.find_target_conflict(target / preview.spec.config.folder_name) is None
