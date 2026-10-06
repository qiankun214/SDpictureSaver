"""core 的统一门面：CLI 与 API 都只通过它调用业务逻辑（架构约定见 Agent.md 第 4 节）。"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from .copier import CanceledError, cleanup_temp_files, copy_plan
from .events import PHASE_PLANNING, ProgressCallback, ProgressEvent, emit
from .models import CopyConfig, CopyPlan, CopyReport, ScanResult
from .planner import build_plan, ensure_target_parent
from .scanner import scan_directory

__all__ = ["CopyService", "CanceledError", "cleanup_temp_files", "ensure_target_parent"]


class CopyService:
    """照片拷贝业务门面：扫描 -> 生成计划 -> 执行复制。"""

    def scan(
        self,
        source_dir: str | Path,
        suffixes: Iterable[str],
        *,
        recursive: bool = True,
        exclude_dirs: Iterable[str | Path] = (),
        progress: ProgressCallback | None = None,
        cancel=None,
    ) -> ScanResult:
        """扫描源目录并解析每张照片的拍摄时间。

        :param progress: 进度回调，会收到 ``counting`` 与 ``scanning`` 两个阶段的事件
        :param cancel: 可选的 ``threading.Event``，置位后尽快停止扫描
        """
        return scan_directory(
            source_dir,
            suffixes,
            recursive=recursive,
            exclude_dirs=exclude_dirs,
            progress=progress,
            cancel=cancel,
        )

    def scan_for_config(
        self,
        config: CopyConfig,
        *,
        progress: ProgressCallback | None = None,
        cancel=None,
    ) -> ScanResult:
        """按配置扫描源目录（自动排除目标目录）。"""
        return self.scan(
            config.source_dir,
            config.suffixes,
            recursive=config.recursive,
            exclude_dirs=[config.target_dir],
            progress=progress,
            cancel=cancel,
        )

    def preview(self, config: CopyConfig) -> CopyPlan:
        """只生成计划，不落盘（脚本与测试用；界面走带进度的任务模式）。"""
        return build_plan(self.scan_for_config(config), config)

    def plan_only(
        self,
        config: CopyConfig,
        *,
        progress: ProgressCallback | None = None,
        cancel=None,
    ) -> tuple[ScanResult, CopyPlan]:
        """扫描 + 生成计划，但不复制（界面上的"预览计划"）。

        :return: ``(扫描结果, 计划)``
        """
        scan = self.scan_for_config(config, progress=progress, cancel=cancel)
        plan = build_plan(scan, config)
        self._emit_plan_ready(plan, scan, progress)
        return scan, plan

    def run(
        self,
        config: CopyConfig,
        *,
        progress: ProgressCallback | None = None,
        cancel=None,
        dry_run: bool = False,
    ) -> tuple[CopyPlan, CopyReport]:
        """扫描 + 生成计划 + 执行复制。

        :return: ``(计划, 报告)``
        """
        scan = self.scan_for_config(config, progress=progress, cancel=cancel)
        plan = build_plan(scan, config)
        self._emit_plan_ready(plan, scan, progress)
        report = copy_plan(plan, progress=progress, cancel=cancel, dry_run=dry_run)
        return plan, report

    @staticmethod
    def _emit_plan_ready(
        plan: CopyPlan,
        scan: ScanResult,
        progress: ProgressCallback | None,
    ) -> None:
        """计划生成后的进度事件（此时扫描已完成）。"""
        emit(
            progress,
            ProgressEvent(
                phase=PHASE_PLANNING,
                total_files=plan.planned_files,
                done_files=plan.planned_files,
                total_bytes=plan.planned_bytes,
                scanned_files=scan.matched_files,
                scan_total_files=scan.matched_files,
                message=f"计划已生成：{plan.planned_files} 个文件，"
                f"{plan.out_of_range_files} 个在日期范围外，{len(plan.no_time)} 个无拍摄时间",
            ),
        )
