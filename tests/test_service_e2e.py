"""端到端测试：模拟一张真实存储卡，走完 扫描 -> 计划 -> 复制 -> 报告 全流程。"""

from __future__ import annotations

import json
from datetime import date, datetime

from fixtures import make_jpeg, make_pair

from picsaver.core.models import CopyConfig
from picsaver.core.service import CopyService


def build_sd_card(card) -> None:
    """构造一张"存储卡"：DCIM 下两个目录，含 arw / orf、范围外照片、无 EXIF 照片。"""
    day1 = card / "DCIM" / "100MSDCF"
    day2 = card / "DCIM" / "101MSDCF"
    make_pair(day1, "DSC00001", datetime(2026, 7, 4, 9, 0, 0), suffixes=("jpg", "arw"))
    make_pair(day1, "DSC00002", datetime(2026, 7, 5, 14, 30, 0), suffixes=("jpg", "orf"))
    make_pair(day2, "P1000003", datetime(2026, 7, 6, 18, 45, 0), suffixes=("jpg", "orf"))
    make_pair(day2, "DSC00004", datetime(2026, 7, 20, 8, 0, 0), suffixes=("jpg", "arw"))  # 范围外
    make_jpeg(day1 / "NOSHOT.JPG", None)  # 无 EXIF，应跳过
    (card / "DCIM" / "100MSDCF" / "README.TXT").write_text("not a photo", encoding="utf-8")


def test_full_flow(tmp_path) -> None:
    card = tmp_path / "card"
    out = tmp_path / "相册"
    build_sd_card(card)
    service = CopyService()

    config = CopyConfig(
        source_dir=card,
        suffixes=("jpg", "arw", "orf"),
        date_from=date(2026, 7, 4),
        date_to=date(2026, 7, 6),
        target_parent=out,
        place="北海道",
        note="家庭旅行",
    )
    config.validate()

    events = []
    plan, report = service.run(config, progress=events.append)

    # 目录命名：日期取范围第一天
    assert config.folder_name == "[20260704]北海道-家庭旅行"
    assert report.target_dir == out / "[20260704]北海道-家庭旅行"

    # 6 张范围内的照片（jpg+raw 成对）都被复制
    assert plan.planned_files == 6
    assert report.copied_files == 6
    assert report.failed_files == 0
    assert report.out_of_range_files == 2
    assert report.no_time_files == 1
    assert report.total_bytes == plan.planned_bytes

    # 按后缀分文件夹
    target = report.target_dir
    assert sorted(p.name for p in (target / "JPG").iterdir()) == [
        "DSC00001.jpg",
        "DSC00002.jpg",
        "P1000003.jpg",
    ]
    assert sorted(p.name for p in (target / "ARW").iterdir()) == ["DSC00001.arw"]
    assert sorted(p.name for p in (target / "ORF").iterdir()) == ["DSC00002.orf", "P1000003.orf"]
    assert not (target / "TXT").exists()

    # 源文件必须原封不动
    assert (card / "DCIM" / "100MSDCF" / "DSC00001.arw").exists()
    assert (card / "DCIM" / "100MSDCF" / "DSC00001.jpg").exists()

    # 进度事件完整且真实：统计 -> 读取时间 -> 生成计划 -> 复制 -> 完成
    phases = [event.phase for event in events]
    assert phases[0] == "counting"
    assert "scanning" in phases
    assert "planning" in phases
    assert "copying" in phases
    assert events[-1].phase == "finished"
    scanning = [event for event in events if event.phase == "scanning"]
    assert scanning[-1].scan_total_files == 9  # 卡上命中的 jpg/arw/orf 共 9 个
    assert scanning[-1].scanned_files == 9
    assert events[-1].done_files == 6
    assert events[-1].done_bytes == plan.planned_bytes
    assert any(event.message for event in events)

    # 报告可序列化且内容正确
    payload = json.loads(json.dumps(report.to_dict(), ensure_ascii=False))
    assert payload["folder_name"] == "[20260704]北海道-家庭旅行"
    assert payload["no_time_files"] == 1
    assert payload["no_time"][0]["path"].endswith("NOSHOT.JPG")
    assert "拷贝结果报告" in report.to_text()

    # 再跑一次：全部跳过，不重复复制
    _, second = service.run(config)
    assert second.skipped_existing == 6
    assert second.copied_files == 0

    # dry-run：不落盘
    dry_config = CopyConfig(
        source_dir=card,
        suffixes=("jpg",),
        date_from=date(2026, 7, 4),
        date_to=date(2026, 7, 6),
        target_parent=tmp_path / "预览输出",
        place="测试",
        note="预览",
    )
    _, dry_report = service.run(dry_config, dry_run=True)
    assert dry_report.dry_run is True
    assert dry_report.copied_files == 3
    assert not dry_config.target_dir.exists()


def test_preview_does_not_write(tmp_path) -> None:
    card = tmp_path / "card"
    build_sd_card(card)
    config = CopyConfig(
        source_dir=card,
        suffixes=("jpg", "arw", "orf"),
        date_from=date(2026, 7, 4),
        date_to=date(2026, 7, 6),
        target_parent=tmp_path / "out",
        place="京都",
        note="赏枫",
    )
    plan = CopyService().preview(config)
    assert plan.planned_files == 6
    assert not config.target_dir.exists()
    payload = plan.to_dict()
    assert payload["folder_name"] == "[20260704]京都-赏枫"
    assert payload["suffix_dirs"] == ["ARW", "JPG", "ORF"]
    assert payload["skipped_existing_files"] == 0
