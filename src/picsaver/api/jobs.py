"""后台任务管理：线程执行 core，把进度事件推给 SSE / 轮询接口。"""

from __future__ import annotations

import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from ..core.events import PHASE_FAILED, PHASE_FINISHED, ProgressEvent
from ..core.metrics import SpeedMeter
from ..core.models import CopyConfig, CopyPlan
from ..core.service import CopyService

#: 事件队列上限，避免慢消费者把内存撑爆（超出时丢弃最旧事件）
MAX_QUEUED_EVENTS = 400
#: 结束哨兵
END_MARKER = "__end__"


@dataclass
class Job:
    """一个拷贝任务。"""

    id: str
    config: CopyConfig
    dry_run: bool
    started_at: float = field(default_factory=time.monotonic)
    cancel_event: threading.Event = field(default_factory=threading.Event)
    events: "queue.Queue[dict[str, Any]]" = field(default_factory=queue.Queue)
    status: str = "running"  # running | done | failed | canceled
    progress: dict[str, Any] = field(default_factory=dict)
    report: dict[str, Any] | None = None
    error: str | None = None
    plan: CopyPlan | None = None
    meter: SpeedMeter = field(default_factory=SpeedMeter)
    thread: threading.Thread | None = None
    finished_at: float | None = None


class JobManager:
    """进程内任务管理器（单人本地工具，够用且无额外依赖）。"""

    def __init__(self, service: CopyService | None = None, max_jobs: int = 30) -> None:
        self.service = service or CopyService()
        self.max_jobs = int(max_jobs)
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()

    # ---- 生命周期 ------------------------------------------------------

    def submit(self, config: CopyConfig, dry_run: bool = False) -> Job:
        """创建并启动一个后台拷贝任务。"""
        job = Job(id=uuid.uuid4().hex, config=config, dry_run=dry_run)
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
            while len(self._order) > self.max_jobs:
                stale = self._order.pop(0)
                self._jobs.pop(stale, None)
        job.thread = threading.Thread(target=self._worker, args=(job,), name=f"picsaver-{job.id[:8]}", daemon=True)
        job.thread.start()
        return job

    def get(self, job_id: str) -> Job | None:
        """按 id 取任务。"""
        with self._lock:
            return self._jobs.get(job_id)

    def cancel(self, job_id: str) -> bool:
        """请求取消任务；任务不存在时返回 False。"""
        job = self.get(job_id)
        if job is None:
            return False
        job.cancel_event.set()
        return True

    def list_jobs(self) -> list[dict[str, Any]]:
        """列出最近的任务概要。"""
        with self._lock:
            jobs = [self._jobs[job_id] for job_id in reversed(self._order) if job_id in self._jobs]
        return [self.payload(job) for job in jobs]

    # ---- 进度与事件 ----------------------------------------------------

    def build_progress(self, job: Job, event: ProgressEvent) -> dict[str, Any]:
        """把 core 的原始事件转成界面需要的进度对象（含百分比 / 速度 / ETA）。"""
        total_bytes = event.total_bytes
        done_bytes = min(event.done_bytes, total_bytes) if total_bytes else event.done_bytes
        if total_bytes > 0:
            percent = round(done_bytes / total_bytes * 100, 1)
        else:
            percent = 100.0 if event.phase in (PHASE_FINISHED, PHASE_FAILED) else 0.0
        job.meter.update(event.done_bytes)
        speed = job.meter.speed_bps()
        remaining = max(0, total_bytes - event.done_bytes)
        eta = job.meter.eta_seconds(remaining)
        return {
            "phase": event.phase,
            "total_files": event.total_files,
            "done_files": event.done_files,
            "total_bytes": total_bytes,
            "done_bytes": done_bytes,
            "current_file": event.current_file,
            "percent": percent,
            "speed_bps": round(speed, 1),
            "eta_seconds": round(eta, 1) if eta is not None else None,
            "elapsed_seconds": round(time.monotonic() - job.started_at, 2),
            "message": event.message,
        }

    def _push(self, job: Job, payload: dict[str, Any]) -> None:
        """写入事件队列（带丢弃最旧事件的保护）。"""
        if job.events.qsize() >= MAX_QUEUED_EVENTS:
            try:
                job.events.get_nowait()
            except queue.Empty:
                pass
        job.events.put(payload)

    def payload(self, job: Job) -> dict[str, Any]:
        """任务状态对象（GET /api/jobs/{id} 的响应体）。"""
        return {
            "job_id": job.id,
            "status": job.status,
            "dry_run": job.dry_run,
            "progress": job.progress,
            "report": job.report,
            "error": job.error,
            "config": job.config.summary(),
        }

    # ---- 工作线程 ------------------------------------------------------

    def _worker(self, job: Job) -> None:
        """后台线程：调用 core 执行任务，把每个进度事件推入队列。"""

        def on_progress(event: ProgressEvent) -> None:
            payload = self.build_progress(job, event)
            job.progress = payload
            self._push(job, payload)

        try:
            plan, report = self.service.run(
                job.config, progress=on_progress, cancel=job.cancel_event, dry_run=job.dry_run
            )
            job.plan = plan
            job.report = report.to_dict()
            job.status = "canceled" if report.canceled else "done"
        except Exception as exc:  # noqa: BLE001 - 失败要落回任务状态，不能杀死服务
            job.status = "failed"
            job.error = f"{type(exc).__name__}: {exc}"
            self._push(
                job,
                {
                    **job.progress,
                    "phase": PHASE_FAILED,
                    "message": job.error,
                },
            )
        finally:
            job.finished_at = time.monotonic()
            self._push(job, {END_MARKER: True})
