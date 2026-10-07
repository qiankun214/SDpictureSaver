"""后台任务管理：**串行队列** + 进度事件推送。

所有任务（前台的扫描 / 预览 / 拷贝，以及界面上"后台预览计划 / 后台拷贝"发起的任务）
共用同一条串行队列：任一时刻只有一个任务真正执行，其余处于 ``queued``。
这样磁盘不会被多个拷贝任务互相拖慢，结果也可预期。

- 前台任务（``priority=True``）插到等待队列最前面：显式点击只等待"当前正在跑的那一个"。
- 队列位置、排队原因都通过 ``progress`` 与 ``brief`` 列表暴露给界面。
- 任务结束后可被 ``dismiss``（界面上的 × 关闭）从管理器里移除，刷新后不再出现。
"""

from __future__ import annotations

import queue
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.events import (
    PHASE_CANCELED,
    PHASE_COPYING,
    PHASE_COUNTING,
    PHASE_FAILED,
    PHASE_FINISHED,
    PHASE_PLANNING,
    PHASE_QUEUED,
    PHASE_SCANNING,
    ProgressEvent,
)
from ..core.metrics import SpeedMeter
from ..core.models import CopyConfig, CopyPlan
from ..core.service import CopyService

#: 事件队列上限，避免慢消费者把内存撑爆（超出时丢弃最旧事件）
MAX_QUEUED_EVENTS = 400
#: 结束哨兵
END_MARKER = "__end__"
#: 扫描类阶段（进度按文件数计算）
SCAN_PHASES = (PHASE_COUNTING, PHASE_SCANNING, PHASE_PLANNING)
#: 未结束的状态
ACTIVE_STATUSES = ("queued", "running")
#: 终局状态
TERMINAL_STATUSES = ("done", "failed", "canceled")


class CapacityError(RuntimeError):
    """任务数量达到上限，且没有可淘汰的终局任务。"""


@dataclass
class JobSpec:
    """一次任务的输入（与界面无关）。"""

    mode: str
    source_dir: Path
    suffixes: tuple[str, ...]
    recursive: bool = True
    config: CopyConfig | None = None
    dry_run: bool = False

    @property
    def target_dir(self) -> Path | None:
        """写入型任务的目标目录（scan 任务为 None）。"""
        if self.config is None:
            return None
        return self.config.target_dir

    def summary(self) -> dict[str, Any]:
        """供界面展示的参数摘要。"""
        if self.config is not None:
            return self.config.summary()
        return {
            "source_dir": str(self.source_dir),
            "suffixes": list(self.suffixes),
            "recursive": self.recursive,
            "mode": self.mode,
        }


@dataclass
class Job:
    """一个后台任务。"""

    id: str
    spec: JobSpec
    #: 提交时刻（排队时间从此刻算起）
    created_at: float = field(default_factory=time.monotonic)
    #: 真正开始执行的时刻（排队期间为 None）
    started_at: float | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event)
    events: "queue.Queue[dict[str, Any]]" = field(default_factory=queue.Queue)
    status: str = "queued"  # queued | running | done | failed | canceled
    progress: dict[str, Any] = field(default_factory=dict)
    report: dict[str, Any] | None = None
    error: str | None = None
    plan: CopyPlan | None = None
    meter: SpeedMeter = field(default_factory=SpeedMeter)
    finished_at: float | None = None
    last_phase: str = ""
    #: 扫描阶段的最后一次 (已扫描文件数, 待扫描文件数)，复制阶段继续沿用
    last_scan: tuple[int, int] = (0, 0)

    @property
    def active(self) -> bool:
        """是否还没结束（排队中或运行中）。"""
        return self.status in ACTIVE_STATUSES


class JobManager:
    """进程内串行任务管理器（单人本地工具，够用且无额外依赖）。"""

    def __init__(self, service: CopyService | None = None, max_jobs: int = 30) -> None:
        self.service = service or CopyService()
        self.max_jobs = int(max_jobs)
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._pending: deque[Job] = deque()
        self._signal: "queue.Queue[Job | None]" = queue.Queue()
        self._lock = threading.RLock()
        self._dispatcher: threading.Thread | None = None
        #: 当前正在执行的任务（占用队列第 1 位）
        self._current: Job | None = None

    # ---- 生命周期 ------------------------------------------------------

    def submit(self, spec: JobSpec, *, priority: bool = False) -> Job:
        """创建任务并加入串行队列（不阻塞，真正的执行由分派线程按顺序进行）。

        :param priority: 前台任务置 True，插到等待队列最前面（仍不会与正在执行的任务并发）
        :raises CapacityError: 任务数达到上限且没有可淘汰的终局任务
        """
        job = Job(id=uuid.uuid4().hex, spec=spec)
        with self._lock:
            self._evict_if_needed()
            self._jobs[job.id] = job
            self._order.append(job.id)
            if priority:
                self._pending.appendleft(job)
            else:
                self._pending.append(job)
            self._refresh_queue_progress()
            self._ensure_dispatcher()
        self._signal.put(job)
        return job

    def get(self, job_id: str) -> Job | None:
        """按 id 取任务。"""
        with self._lock:
            return self._jobs.get(job_id)

    def cancel(self, job_id: str) -> bool:
        """请求取消任务；任务不存在时返回 False。

        - 排队中：立刻从队列摘除并置为 ``canceled``（绝不会开始执行）
        - 运行中：置位取消事件，由 core 尽快停止（已复制的文件保留）
        """
        job = self.get(job_id)
        if job is None:
            return False
        job.cancel_event.set()
        if job.status == "queued":
            with self._lock:
                if job in self._pending:
                    self._pending.remove(job)
                self._refresh_queue_progress()
            self._finalize_canceled(job)
        return True

    def dismiss(self, job_id: str) -> str:
        """关闭（遗忘）一个任务。

        :return: ``"ok"`` / ``"active"``（还在进行中）/ ``"not_found"``
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return "not_found"
            if job.active:
                return "active"
            self._jobs.pop(job_id, None)
            if job_id in self._order:
                self._order.remove(job_id)
        return "ok"

    def queue_position(self, job: Job) -> int:
        """任务在队列中的位置：正在执行 = 1、排队中 = 2..N、终局 = 0。"""
        with self._lock:
            if job is self._current or job.status == "running":
                return 1
            if job.status != "queued":
                return 0
            try:
                index = self._pending.index(job)
            except ValueError:
                return 0
            return self._queue_offset() + index + 1

    def _queue_offset(self) -> int:
        """正在执行的任务占用的位置数（0 或 1）。"""
        return 1 if self._current is not None else 0

    def find_target_conflict(self, target_dir: Path) -> Job | None:
        """查找排队中/运行中、目标目录相同的**拷贝**任务（用于拒绝重复写入）。"""
        key = str(target_dir).casefold()
        with self._lock:
            for job_id in self._order:
                job = self._jobs.get(job_id)
                if job is None or not job.active or job.spec.mode != "copy":
                    continue
                spec_dir = job.spec.target_dir
                if spec_dir is not None and str(spec_dir).casefold() == key:
                    return job
        return None

    def list_jobs(self, brief: bool = False) -> list[dict[str, Any]]:
        """列出最近的任务（最新的在前）。

        :param brief: True 时返回不含 report 正文的概要（给界面轮询用）
        """
        with self._lock:
            jobs = [self._jobs[job_id] for job_id in reversed(self._order) if job_id in self._jobs]
        return [self.brief_payload(job) if brief else self.payload(job) for job in jobs]

    def shutdown(self, timeout: float = 5.0) -> None:
        """停止分派线程（测试收尾用）。"""
        self._signal.put(None)
        thread = self._dispatcher
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)

    # ---- 队列与分派 ----------------------------------------------------

    def _evict_if_needed(self) -> None:
        """超限时只淘汰最老的**终局**任务；进行中的任务绝不驱逐。"""
        while len(self._order) >= self.max_jobs:
            victim: str | None = None
            for job_id in self._order:  # _order 按提交顺序，从最老开始找
                job = self._jobs.get(job_id)
                if job is not None and job.status in TERMINAL_STATUSES:
                    victim = job_id
                    break
            if victim is None:
                raise CapacityError(
                    f"后台任务已达上限（{self.max_jobs} 个且都在进行中），请等待部分任务结束后再试"
                )
            self._jobs.pop(victim, None)
            self._order.remove(victim)

    def _ensure_dispatcher(self) -> None:
        """懒启动单分派线程（保证串行执行）。"""
        if self._dispatcher is not None and self._dispatcher.is_alive():
            return
        self._dispatcher = threading.Thread(
            target=self._dispatch_loop, name="picsaver-dispatcher", daemon=True
        )
        self._dispatcher.start()

    def _dispatch_loop(self) -> None:
        """逐个取任务串行执行：任一时刻只有一个任务在跑。

        信号量只用于**唤醒**，真正取哪个任务由 ``_pending`` 的顺序决定，
        这样前台任务的"插到最前面"才会生效（信号自身的排队顺序不作数）。
        """
        while True:
            signal_job = self._signal.get()
            if signal_job is None:  # 退出信号
                return
            with self._lock:
                if not self._pending:
                    continue  # 该任务已被取消/已被其它信号取走
                job = self._pending.popleft()
                self._current = job
                self._refresh_queue_progress()
            try:
                if job.status in TERMINAL_STATUSES:
                    continue  # 排队期间已被取消并结算过
                if job.cancel_event.is_set():
                    self._finalize_canceled(job)
                    continue
                job.status = "running"
                job.started_at = time.monotonic()
                self._run_job(job)
            finally:
                with self._lock:
                    self._current = None
                    self._refresh_queue_progress()

    def _refresh_queue_progress(self) -> None:
        """刷新所有排队任务的"前面还有 N 个"提示（位置变化时推送一次）。"""
        offset = self._queue_offset()
        for index, job in enumerate(self._pending, start=1):
            payload = self._queued_progress(offset + index)
            if job.progress != payload:
                job.progress = payload
                self._push(job, payload)

    @staticmethod
    def _queued_progress(position: int) -> dict[str, Any]:
        """排队中的进度对象（与其它阶段的字段保持一致）。"""
        return {
            "phase": PHASE_QUEUED,
            "message": f"排队中：前面还有 {max(0, position - 1)} 个任务",
            "total_files": 0,
            "done_files": 0,
            "scanned_files": 0,
            "scan_total_files": 0,
            "total_bytes": 0,
            "done_bytes": 0,
            "current_file": None,
            "percent": 0.0,
            "speed_bps": 0.0,
            "eta_seconds": None,
            "elapsed_seconds": 0.0,
        }

    def _finalize_canceled(self, job: Job) -> None:
        """把任务直接置为已取消（排队中被取消时走这里，绝不执行文件操作）。"""
        with self._lock:
            if job.status in TERMINAL_STATUSES:
                return
            job.status = "canceled"
            job.finished_at = time.monotonic()
            job.progress = {**job.progress, "phase": PHASE_CANCELED, "message": "任务已取消（排队中取消，未执行）"}
        self._push(job, job.progress)
        self._push(job, {END_MARKER: True})

    # ---- 进度与事件 ----------------------------------------------------

    def build_progress(self, job: Job, event: ProgressEvent) -> dict[str, Any]:
        """把 core 的原始事件转成界面需要的进度对象（含百分比 / 速度 / ETA）。"""
        phase = event.phase
        if phase in SCAN_PHASES:
            scan_total = event.scan_total_files
            scanned = min(event.scanned_files, scan_total) if scan_total else event.scanned_files
            percent = (
                round(scanned / scan_total * 100, 1)
                if scan_total
                else (100.0 if phase == PHASE_PLANNING else 0.0)
            )
            speed = 0.0
            eta = None
            job.last_scan = (scanned, scan_total)
        else:
            total_bytes = event.total_bytes
            done_bytes = min(event.done_bytes, total_bytes) if total_bytes else event.done_bytes
            percent = (
                round(done_bytes / total_bytes * 100, 1)
                if total_bytes
                else (100.0 if phase in (PHASE_FINISHED, PHASE_FAILED) else 0.0)
            )
            if phase == PHASE_COPYING and job.last_phase != PHASE_COPYING:
                job.meter.reset()  # 扫描阶段的采样单位与复制不同，进入复制时重新计时
            job.meter.update(event.done_bytes)
            speed = job.meter.speed_bps()
            eta = job.meter.eta_seconds(max(0, total_bytes - event.done_bytes))
            # 复制阶段的事件不带扫描字段，沿用扫描结束时的值（界面要一直显示已扫描文件数）
            scanned = event.scanned_files or job.last_scan[0]
            scan_total = event.scan_total_files or job.last_scan[1]

        job.last_phase = phase
        return {
            "phase": phase,
            "message": event.message,
            "total_files": event.total_files,
            "done_files": event.done_files,
            "scanned_files": scanned,
            "scan_total_files": scan_total,
            "total_bytes": event.total_bytes,
            "done_bytes": event.done_bytes,
            "current_file": event.current_file,
            "percent": percent,
            "speed_bps": round(speed, 1),
            "eta_seconds": round(eta, 1) if eta is not None else None,
            "elapsed_seconds": round(time.monotonic() - (job.started_at or job.created_at), 2),
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
        age = round(time.monotonic() - job.finished_at, 1) if job.finished_at else None
        return {
            "job_id": job.id,
            "status": job.status,
            "mode": job.spec.mode,
            "dry_run": job.spec.dry_run,
            "queue_position": self.queue_position(job),
            "age_seconds": age,
            "has_report": job.report is not None,
            "progress": job.progress,
            "report": job.report,
            "error": job.error,
            "config": job.spec.summary(),
        }

    def brief_payload(self, job: Job) -> dict[str, Any]:
        """任务概要：不含 report 正文（界面 400ms 轮询用）。"""
        age = round(time.monotonic() - job.finished_at, 1) if job.finished_at else None
        return {
            "job_id": job.id,
            "status": job.status,
            "mode": job.spec.mode,
            "dry_run": job.spec.dry_run,
            "queue_position": self.queue_position(job),
            "age_seconds": age,
            "has_report": job.report is not None,
            "error": job.error,
            "config": job.spec.summary(),
            "progress": job.progress,
        }

    # ---- 任务执行 ------------------------------------------------------

    def _execute(self, job: Job, on_progress) -> dict[str, Any]:
        """按模式执行任务，返回报告字典。"""
        spec = job.spec
        if spec.mode == "scan":
            scan = self.service.scan(
                spec.source_dir,
                spec.suffixes,
                recursive=spec.recursive,
                progress=on_progress,
                cancel=job.cancel_event,
            )
            return {"mode": "scan", "dry_run": True, "canceled": scan.canceled, "scan": scan.to_dict()}
        if spec.mode == "preview":
            scan, plan = self.service.plan_only(
                spec.config, progress=on_progress, cancel=job.cancel_event
            )
            job.plan = plan
            return {
                "mode": "preview",
                "dry_run": True,
                "canceled": scan.canceled,
                "plan": plan.to_dict(),
                "scan": scan.to_dict(),
            }
        plan, report = self.service.run(
            spec.config, progress=on_progress, cancel=job.cancel_event, dry_run=spec.dry_run
        )
        job.plan = plan
        payload = report.to_dict()
        payload["mode"] = "copy"
        return payload

    def _run_job(self, job: Job) -> None:
        """执行一个任务（由分派线程串行调用）。"""

        def on_progress(event: ProgressEvent) -> None:
            payload = self.build_progress(job, event)
            job.progress = payload
            self._push(job, payload)

        try:
            report = self._execute(job, on_progress)
            job.report = report
            job.status = "canceled" if report.get("canceled") else "done"
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
