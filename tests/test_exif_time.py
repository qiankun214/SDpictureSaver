"""EXIF 拍摄时间读取的测试（ExifRead / Pillow / 内置 TIFF 读取器）。"""

from __future__ import annotations

from datetime import datetime

from fixtures import make_jpeg, make_raw

from picsaver.core.exif_time import parse_exif_datetime, read_capture_time


def test_parse_exif_datetime_standard() -> None:
    assert parse_exif_datetime("2026:07:04 10:20:30") == datetime(2026, 7, 4, 10, 20, 30)


def test_parse_exif_datetime_alternate_formats() -> None:
    assert parse_exif_datetime("2026-07-04 10:20:30") == datetime(2026, 7, 4, 10, 20, 30)
    assert parse_exif_datetime("2026-07-04T10:20:30") == datetime(2026, 7, 4, 10, 20, 30)
    assert parse_exif_datetime("2026:07:04 10:20:30.500") == datetime(2026, 7, 4, 10, 20, 30, 500000)


def test_parse_exif_datetime_invalid() -> None:
    assert parse_exif_datetime("") is None
    assert parse_exif_datetime("0000:00:00 00:00:00") is None
    assert parse_exif_datetime("not a date") is None
    assert parse_exif_datetime("2026:13:45 99:99:99") is None


def test_read_capture_time_from_jpeg(tmp_path) -> None:
    path = make_jpeg(tmp_path / "DSC00001.JPG", datetime(2026, 7, 4, 10, 20, 30))
    found = read_capture_time(path)
    assert found is not None
    assert found.value == datetime(2026, 7, 4, 10, 20, 30)
    assert found.tool == "exifread"
    assert found.tag == "EXIF DateTimeOriginal"
    assert found.suffix == "jpg"


def test_read_capture_time_without_exif(tmp_path) -> None:
    path = make_jpeg(tmp_path / "NOEXIF.JPG", None)
    assert read_capture_time(path) is None


def test_read_capture_time_from_arw(tmp_path) -> None:
    path = make_raw(tmp_path / "DSC00001.ARW", datetime(2026, 8, 9, 1, 2, 3))
    found = read_capture_time(path)
    assert found is not None
    assert found.value == datetime(2026, 8, 9, 1, 2, 3)
    assert found.tag == "EXIF DateTimeOriginal"


def test_read_capture_time_from_orf(tmp_path) -> None:
    path = make_raw(tmp_path / "P1010001.ORF", datetime(2026, 9, 10, 4, 5, 6))
    found = read_capture_time(path)
    assert found is not None
    assert found.value == datetime(2026, 9, 10, 4, 5, 6)


def test_read_capture_time_from_big_endian_raw(tmp_path) -> None:
    path = make_raw(tmp_path / "BE.ARW", datetime(2026, 1, 2, 3, 4, 5), big_endian=True)
    found = read_capture_time(path)
    assert found is not None
    assert found.value == datetime(2026, 1, 2, 3, 4, 5)


def test_builtin_tiff_reader_used_when_exifread_fails(tmp_path, monkeypatch) -> None:
    """ExifRead 与 Pillow 都失败时，内置 TIFF 读取器仍要能拿到时间。"""
    from picsaver.core import exif_time

    path = make_raw(tmp_path / "FALLBACK.ARW", datetime(2025, 12, 31, 23, 59, 58))
    monkeypatch.setattr(exif_time, "_from_exifread", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(exif_time, "_from_pillow", lambda *_args, **_kwargs: None)
    found = exif_time.read_capture_time(path)
    assert found is not None
    assert found.tool == "tiff"
    assert found.value == datetime(2025, 12, 31, 23, 59, 58)


def test_read_capture_time_missing_file(tmp_path) -> None:
    assert read_capture_time(tmp_path / "not-exists.JPG") is None
