"""扫描器测试：后缀过滤、jpg/raw 配对、无时间清单、目录排除。"""

from __future__ import annotations

from datetime import datetime

from fixtures import make_jpeg, make_pair, make_raw

from picsaver.core.scanner import scan_directory


def build_card(root):
    """构造一张小型"存储卡"：3 组正常照片 + 1 组无时间照片。"""
    day1 = root / "DCIM" / "100MSDCF"
    make_pair(day1, "DSC00001", datetime(2026, 7, 4, 10, 0, 0), suffixes=("jpg", "arw"))
    make_pair(day1, "DSC00002", datetime(2026, 7, 5, 11, 0, 0), suffixes=("jpg", "orf"))
    make_pair(root / "DCIM" / "101MSDCF", "DSC00003", datetime(2026, 7, 6, 12, 0, 0), suffixes=("jpg", "arw"))
    make_pair(day1, "DSC00004", None, suffixes=("jpg", "arw"))
    return root


def test_scan_matches_by_stem_and_extension(tmp_path) -> None:
    card = build_card(tmp_path / "card")
    result = scan_directory(card, ["jpg", "arw", "orf"])
    assert len(result.groups) == 4
    assert result.matched_files == 8
    assert result.suffix_counts == {"arw": 3, "jpg": 4, "orf": 1}
    assert result.total_bytes > 0


def test_scan_suffix_filter(tmp_path) -> None:
    card = build_card(tmp_path / "card")
    result = scan_directory(card, ["arw"])
    assert result.matched_files == 3
    assert result.suffix_counts == {"arw": 3}
    assert all(f.suffix == "arw" for g in result.groups for f in g.files.values())


def test_scan_pairs_share_capture_time(tmp_path) -> None:
    """同名 jpg/raw 视为同一张照片：JPG 没有 EXIF 时用 RAW 的时间。"""
    card = tmp_path / "card"
    make_pair(card, "DSC00010", datetime(2026, 3, 4, 5, 6, 7), suffixes=("jpg", "arw"), jpeg_time=None)
    result = scan_directory(card, ["jpg", "arw"])
    group = result.groups[0]
    assert group.capture_time == datetime(2026, 3, 4, 5, 6, 7)
    assert group.time_source.startswith("paired:")
    assert result.no_time_files == []


def test_scan_reports_files_without_time(tmp_path) -> None:
    card = tmp_path / "card"
    make_jpeg(card / "A.JPG", None)
    make_raw(card / "B.ARW", None)
    result = scan_directory(card, ["jpg", "arw"])
    assert len(result.no_time_files) == 2
    assert {item.suffix for item in result.no_time_files} == {"jpg", "arw"}
    assert all("ExifRead" in item.reason or "EXIF" in item.reason for item in result.no_time_files)
    assert result.with_time_files == 0


def test_scan_recursive_flag(tmp_path) -> None:
    card = build_card(tmp_path / "card")
    flat = scan_directory(card, ["jpg", "arw", "orf"], recursive=False)
    assert flat.matched_files == 0
    deep = scan_directory(card, ["jpg", "arw", "orf"], recursive=True)
    assert deep.matched_files == 8


def test_scan_skips_system_dirs(tmp_path) -> None:
    card = tmp_path / "card"
    make_jpeg(card / "keep.JPG", datetime(2026, 7, 4, 1, 1, 1))
    make_jpeg(card / "$RECYCLE.BIN" / "trash.JPG", datetime(2026, 7, 4, 1, 1, 1))
    make_jpeg(card / ".hidden" / "hidden.JPG", datetime(2026, 7, 4, 1, 1, 1))
    result = scan_directory(card, ["jpg"])
    assert result.matched_files == 1


def test_scan_excludes_target_dir(tmp_path) -> None:
    card = tmp_path / "card"
    make_jpeg(card / "A.JPG", datetime(2026, 7, 4, 1, 1, 1))
    excluded = card / "out"
    make_jpeg(excluded / "B.JPG", datetime(2026, 7, 4, 1, 1, 1))
    result = scan_directory(card, ["jpg"], exclude_dirs=[excluded])
    assert result.matched_files == 1


def test_scan_missing_dir(tmp_path) -> None:
    import pytest

    with pytest.raises(FileNotFoundError):
        scan_directory(tmp_path / "nope", ["jpg"])
