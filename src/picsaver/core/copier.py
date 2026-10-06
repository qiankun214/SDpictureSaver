"""拷贝执行器：分块复制 + 临时文件改名（防半截文件）+ 真实进度回调 + 可取消。"""

from __future__ import annotations

import os
import shutil
import time
import uuid
from pathlib import Path

from .events import (
    PHASE_CANCELED,
    PHASE_COPYING,
    PHASE_FINISHED,
    ProgressCallback,
    ProgressEvent,
    emit,
)
from .models import CopyItem, CopyPlan, CopyReport, FileFailure
from .planner import ensure_target_parent

#: 单次读写块大小（1 MiB）
CHUNK_SIZE = 1024 * 1024
#: 临时文件前缀（复制完成后改名，避免出现半截文件）
TEMP_PREFIX = ".picsaver-tmp-"
#: 进度回调的最小间隔（秒），避免刷爆 SSE / 终端
EMIT_INTERVAL = 0.1


class CanceledError(RuntimeError):
    """用户取消了任务。"""


class _Tracker:
    """进度状态与节流发送。"""

    def __init__(self, plan: CopyPlan, progress: ProgressCallback | None) -> None:
        self.plan = plan
        self.progress = progress
        self.total_files = plan.planned_files
        self.total_bytes = plan.planned_bytes
        self.done_files = 0
        self.done_bytes = 0
        self.current_file: str | None = None
        self._last_emit = 0.0

    def add_done(self, size: int, file_count: int = 0) -> None:
        self.done_bytes += int(size)
        self.done_files += int(file_count)

    def snapshot(self, phase: str, message: str = "") -> ProgressEvent:
        return ProgressEvent(
            phase=phase,
            total_files=self.total_files,
            done_files=self.done_files,
            total_bytes=self.total_bytes,
            done_bytes=self.done_bytes,
            current_file=self.current_file,
            message=message,
        )

    def push(self, phase: str, *, force: bool = False, message: str = "") -> None:
        now = time.monotonic()
        if not force and (now - self._last_emit) < EMIT_INTERVAL:
            return
        self._last_emit = now
        emit(self.progress, self.snapshot(phase, message))


def _copy_single_file(item: CopyItem, tracker: _Tracker, cancel) -> None:
    """复制单个文件：先写临时文件，成功后改名，尽量保留时间戳。"""
    target = item.target
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target.parent / f"{TEMP_PREFIX}{os.getpid()}-{uuid.uuid4().hex}"
    try:
        with open(item.source.path, "rb") as source_handle, open(temp_path, "wb") as target_handle:
            while True:
                if cancel is not None and cancel.is_set():
                    raise CanceledError()
                chunk = source_handle.read(CHUNK_SIZE)
                if not chunk:
                    break
                target_handle.write(chunk)
                tracker.add_done(len(chunk))
                tracker.push(PHASE_COPYING)
        os.replace(temp_path, target)
        try:
            shutil.copystat(item.source.path, target)
        except OSError:
            pass
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def copy_plan(
    plan: CopyPlan,
    *,
    progress: ProgressCallback | None = None,
    cancel=None,
    dry_run: bool = False,
    prepare_target: bool = True,
) -> CopyReport:
    """执行拷贝计划。

    :param plan: :func:`picsaver.core.planner.build_plan` 生成的计划
    :param progress: 进度回调（接收 :class:`~picsaver.core.events.ProgressEvent`）
    :param cancel: 可选的 ``threading.Event``，置位后尽力停止
    :param dry_run: 只统计不落盘（用于预览）
    :param prepare_target: 是否创建目标目录（dry-run 时无效）
    :return: :class:`~picsaver.core.models.CopyReport`
    """
    started = time.monotonic()
    tracker = _Tracker(plan, progress)
    failures: list[FileFailure] = []
    copied_files = 0
    skipped_existing = 0
    copied_bytes = 0
    canceled = False

    if not dry_run and prepare_target and plan.items:
        # 只有确实有文件要处理时才创建目标目录，避免空跑留下空文件夹
        ensure_target_parent(plan.config)
        plan.target_dir.mkdir(parents=True, exist_ok=True)

    tracker.push(PHASE_COPYING, force=True, message="开始复制" if not dry_run else "预览计划")

    for item in plan.items:
        if cancel is not None and cancel.is_set():
            canceled = True
            break
        tracker.current_file = str(item.source.path)
        must_skip = item.skip_existing
        if not must_skip and item.target.exists():
            try:
                must_skip = item.target.stat().st_size == item.source.size
            except OSError:
                must_skip = False

        if must_skip:
            skipped_existing += 1
            copied_bytes += item.source.size
            tracker.add_done(item.source.size, 1)
            tracker.push(PHASE_COPYING, force=True)
            continue

        if dry_run:
            copied_files += 1
            copied_bytes += item.source.size
            tracker.add_done(item.source.size, 1)
            tracker.push(PHASE_COPYING, force=True)
            continue

        try:
            _copy_single_file(item, tracker, cancel)
        except CanceledError:
            canceled = True
            break
        except Exception as exc:  # noqa: BLE001 - 单个文件失败不中断整体任务
            failures.append(
                FileFailure(source=item.source.path, target=item.target, error=f"{type(exc).__name__}: {exc}")
            )
            tracker.add_done(item.source.size, 1)
            tracker.push(PHASE_COPYING, force=True)
            continue

        copied_files += 1
        copied_bytes += item.source.size
        tracker.add_done(0, 1)
        tracker.push(PHASE_COPYING, force=True)

    elapsed = time.monotonic() - started
    tracker.push(
        PHASE_CANCELED if canceled else PHASE_FINISHED,
        force=True,
        message="任务已取消" if canceled else "复制完成",
    )

    return CopyReport(
        folder_name=plan.folder_name,
        target_dir=plan.target_dir,
        total_files=plan.planned_files,
        copied_files=copied_files,
        skipped_existing=skipped_existing,
        failed_files=len(failures),
        no_time_files=len(plan.no_time),
        out_of_range_files=plan.out_of_range_files,
        total_bytes=plan.planned_bytes,
        copied_bytes=copied_bytes,
        elapsed_seconds=elapsed,
        canceled=canceled,
        dry_run=dry_run,
        suffix_dirs=tuple(plan.suffix_dirs),
        failures=failures,
        no_time=list(plan.no_time),
        renamed=plan.renamed,
    )


def cleanup_temp_files(root: str | Path) -> int:
    """清理目标目录下遗留的临时文件（异常中断时可能残留）。

    :return: 删除的临时文件数量
    """
    count = 0
    base = Path(root)
    if not base.exists():
        return 0
    for path in base.rglob(f"{TEMP_PREFIX}*"):
        try:
            path.unlink()
            count += 1
        except OSError:
            continue
    return count
