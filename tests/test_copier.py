"""复制执行器测试：真实复制、进度事件、跳过已存在、dry-run、取消、失败与临时文件清理。"""

from __future__ import annotations

import threading
from datetime import date, datetime

from fixtures import make_jpeg, make_pair

from picsaver.core.copier import CHUNK_SIZE, TEMP_PREFIX, copy_plan
from picsaver.core.events import PHASE_COPYING, PHASE_FINISHED, ProgressEvent
from picsaver.core.models import CopyConfig, CopyItem, CopyPlan, SourceFile
from picsaver.core.planner import build_plan
from picsaver.core.scanner import scan_directory


def make_config(card, out, **overrides) -> CopyConfig:
    params = {
        "source_dir": card,
        "suffixes": ("jpg", "arw"),
        "date_from": date(2026, 7, 4),
        "date_to": date(2026, 7, 6),
        "target_parent": out,
        "place": "北海道",
        "note": "家庭旅行",
    }
    params.update(overrides)
    return CopyConfig(**params)


def prepare(tmp_path, files: int = 2) -> tuple[CopyConfig, CopyPlan]:
    card = tmp_path / "card"
    for index in range(files):
        make_pair(
            card / "DCIM",
            f"DSC{index:05d}",
            datetime(2026, 7, 5, 10, index, 0),
            suffixes=("jpg", "arw"),
        )
    config = make_config(card, tmp_path / "out")
    plan = build_plan(scan_directory(card, ["jpg", "arw"]), config)
    return config, plan


def test_copy_creates_files_and_reports(tmp_path) -> None:
    config, plan = prepare(tmp_path, files=2)
    events: list[ProgressEvent] = []
    report = copy_plan(plan, progress=events.append)

    assert report.total_files == 4
    assert report.copied_files == 4
    assert report.failed_files == 0
    assert report.copied_bytes == plan.planned_bytes
    assert report.suffix_dirs == ("ARW", "JPG")
    assert (report.target_dir / "JPG" / "DSC00000.jpg").exists()
    assert (report.target_dir / "ARW" / "DSC00000.arw").exists()
    # 复制内容与源文件一致
    assert (report.target_dir / "JPG" / "DSC00001.jpg").read_bytes() == (
        config.source_dir / "DCIM" / "DSC00001.jpg"
    ).read_bytes()
    # 源文件保持不动
    assert (config.source_dir / "DCIM" / "DSC00001.arw").exists()


def test_progress_events_are_monotonic_and_real(tmp_path) -> None:
    _config, plan = prepare(tmp_path, files=3)
    events: list[ProgressEvent] = []
    copy_plan(plan, progress=events.append)

    copying = [event for event in events if event.phase == PHASE_COPYING]
    assert copying, "必须产生复制阶段进度事件"
    assert events[-1].phase == PHASE_FINISHED
    assert events[-1].done_bytes == plan.planned_bytes
    assert events[-1].done_files == plan.planned_files
    assert [event.done_bytes for event in copying] == sorted(event.done_bytes for event in copying)
    assert [event.done_files for event in copying] == sorted(event.done_files for event in copying)
    assert all(event.total_bytes == plan.planned_bytes for event in copying)


def test_second_run_skips_existing(tmp_path) -> None:
    _config, plan = prepare(tmp_path, files=2)
    copy_plan(plan)
    second_plan = build_plan(scan_directory(plan.config.source_dir, ["jpg", "arw"]), plan.config)
    report = copy_plan(second_plan)
    assert report.skipped_existing == 4
    assert report.copied_files == 0


def test_dry_run_writes_nothing(tmp_path) -> None:
    config, plan = prepare(tmp_path, files=2)
    report = copy_plan(plan, dry_run=True)
    assert report.dry_run is True
    assert report.copied_files == 4
    assert not config.target_dir.exists()


def test_cancel_stops_midway(tmp_path) -> None:
    _config, plan = prepare(tmp_path, files=4)
    cancel = threading.Event()

    def on_progress(event: ProgressEvent) -> None:
        if event.phase == PHASE_COPYING and event.done_files >= 1:
            cancel.set()

    report = copy_plan(plan, progress=on_progress, cancel=cancel)
    assert report.canceled is True
    assert report.copied_files < plan.planned_files


def test_failure_is_recorded_and_loop_continues(tmp_path) -> None:
    config, plan = prepare(tmp_path, files=2)
    missing = plan.items[0].source
    broken = CopyItem(
        source=SourceFile(path=missing.path.with_name("GONE.JPG"), suffix="jpg", size=123, stem="GONE", rel_dir=""),
        target=config.target_dir / "JPG" / "GONE.JPG",
    )
    plan.items.insert(0, broken)
    report = copy_plan(plan)
    assert report.failed_files == 1
    assert report.failures[0].source.name == "GONE.JPG"
    assert report.copied_files == plan.planned_files - 1


def test_no_temp_files_left_behind(tmp_path) -> None:
    _config, plan = prepare(tmp_path, files=3)
    report = copy_plan(plan)
    leftovers = list(report.target_dir.rglob(f"{TEMP_PREFIX}*"))
    assert leftovers == []


def test_chunk_size_is_one_mib() -> None:
    assert CHUNK_SIZE == 1024 * 1024
