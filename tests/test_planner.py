"""计划器测试：日期范围、目录命名、后缀子目录、防重名与参数校验。"""

from __future__ import annotations

from datetime import date, datetime

import pytest
from fixtures import make_jpeg, make_pair

from picsaver.core.models import ConfigError, CopyConfig
from picsaver.core.planner import build_plan
from picsaver.core.scanner import scan_directory


def make_config(card, out, **overrides) -> CopyConfig:
    params = {
        "source_dir": card,
        "suffixes": ("jpg", "arw", "orf"),
        "date_from": date(2026, 7, 4),
        "date_to": date(2026, 7, 6),
        "target_parent": out,
        "place": "北海道",
        "note": "家庭旅行",
    }
    params.update(overrides)
    return CopyConfig(**params)


def test_folder_name_and_target_dir(tmp_path) -> None:
    card = tmp_path / "card"
    card.mkdir()
    config = make_config(card, tmp_path / "out")
    assert config.folder_name == "[20260704]北海道-家庭旅行"
    assert config.target_dir == tmp_path / "out" / "[20260704]北海道-家庭旅行"


def test_date_range_is_inclusive(tmp_path) -> None:
    card = tmp_path / "card"
    make_pair(card / "a", "IN_FIRST_DAY", datetime(2026, 7, 4, 0, 0, 1), suffixes=("jpg",))
    make_pair(card / "a", "IN_LAST_DAY", datetime(2026, 7, 6, 23, 59, 59), suffixes=("jpg",))
    make_pair(card / "a", "BEFORE", datetime(2026, 7, 3, 23, 59, 59), suffixes=("jpg",))
    make_pair(card / "a", "AFTER", datetime(2026, 7, 7, 0, 0, 0), suffixes=("jpg",))

    config = make_config(card, tmp_path / "out", suffixes=("jpg",))
    plan = build_plan(scan_directory(card, ["jpg"]), config)
    assert sorted(item.source.path.name for item in plan.items) == ["IN_FIRST_DAY.jpg", "IN_LAST_DAY.jpg"]
    assert plan.out_of_range_files == 2


def test_suffix_subdirs_are_uppercase(tmp_path) -> None:
    card = tmp_path / "card"
    make_pair(card, "DSC00001", datetime(2026, 7, 5, 10, 0, 0), suffixes=("jpg", "arw", "orf"))
    config = make_config(card, tmp_path / "out")
    plan = build_plan(scan_directory(card, ["jpg", "arw", "orf"]), config)
    assert plan.planned_files == 3
    assert plan.suffix_dirs == ["ARW", "JPG", "ORF"]
    targets = {item.target.parent.name for item in plan.items}
    assert targets == {"ARW", "JPG", "ORF"}


def test_only_selected_suffixes_are_planned(tmp_path) -> None:
    card = tmp_path / "card"
    make_pair(card, "DSC00001", datetime(2026, 7, 5, 10, 0, 0), suffixes=("jpg", "arw"))
    config = make_config(card, tmp_path / "out", suffixes=("arw",))
    plan = build_plan(scan_directory(card, ["arw"]), config)
    assert plan.planned_files == 1
    assert plan.items[0].source.suffix == "arw"
    assert plan.suffix_dirs == ["ARW"]


def test_no_time_files_are_reported_not_planned(tmp_path) -> None:
    card = tmp_path / "card"
    make_pair(card, "NOTIME", None, suffixes=("jpg", "arw"))
    config = make_config(card, tmp_path / "out")
    plan = build_plan(scan_directory(card, ["jpg", "arw"]), config)
    assert plan.planned_files == 0
    assert len(plan.no_time) == 2
    assert {item.suffix for item in plan.no_time} == {"jpg", "arw"}
    assert all(item.reason for item in plan.no_time)


def test_illegal_characters_are_sanitized(tmp_path) -> None:
    card = tmp_path / "card"
    card.mkdir()
    config = make_config(card, tmp_path / "out", place='北海道/夏:旅*行?', note='  <家 庭>  ')
    assert config.folder_name == "[20260704]北海道夏旅行-家 庭"


def test_empty_place_or_note_raises(tmp_path) -> None:
    card = tmp_path / "card"
    card.mkdir()
    with pytest.raises(ConfigError):
        make_config(card, tmp_path / "out", place="  ").folder_name
    with pytest.raises(ConfigError):
        make_config(card, tmp_path / "out", note="").folder_name


def test_invalid_date_range_raises(tmp_path) -> None:
    card = tmp_path / "card"
    card.mkdir()
    config = make_config(card, tmp_path / "out", date_from=date(2026, 7, 6), date_to=date(2026, 7, 4))
    with pytest.raises(ConfigError):
        config.validate()


def test_target_inside_source_raises(tmp_path) -> None:
    card = tmp_path / "card"
    card.mkdir()
    config = make_config(card, card / "backup")
    with pytest.raises(ConfigError):
        config.validate()


def test_source_missing_raises(tmp_path) -> None:
    config = make_config(tmp_path / "nope", tmp_path / "out")
    with pytest.raises(ConfigError):
        config.validate()


def test_collision_between_source_subdirs_is_renamed(tmp_path) -> None:
    card = tmp_path / "card"
    make_jpeg(card / "100MSDCF" / "DSC00001.JPG", datetime(2026, 7, 5, 10, 0, 0))
    make_jpeg(card / "101MSDCF" / "DSC00001.JPG", datetime(2026, 7, 5, 11, 0, 0))
    config = make_config(card, tmp_path / "out", suffixes=("jpg",))
    plan = build_plan(scan_directory(card, ["jpg"]), config)
    names = sorted(item.target.name for item in plan.items)
    assert names == ["DSC00001.JPG", "DSC00001_1.JPG"]
    assert plan.renamed == 1


def test_existing_same_size_is_marked_skip(tmp_path) -> None:
    card = tmp_path / "card"
    make_jpeg(card / "DSC00001.JPG", datetime(2026, 7, 5, 10, 0, 0))
    config = make_config(card, tmp_path / "out", suffixes=("jpg",))
    first = build_plan(scan_directory(card, ["jpg"]), config)
    target = first.items[0].target
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(card.joinpath("DSC00001.JPG").read_bytes())

    second = build_plan(scan_directory(card, ["jpg"]), config)
    assert second.items[0].skip_existing is True
    assert second.skipped_existing_files == 1


def test_existing_different_size_is_renamed(tmp_path) -> None:
    card = tmp_path / "card"
    make_jpeg(card / "DSC00001.JPG", datetime(2026, 7, 5, 10, 0, 0))
    config = make_config(card, tmp_path / "out", suffixes=("jpg",))
    target = config.target_dir / "JPG" / "DSC00001.JPG"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"different-content")

    plan = build_plan(scan_directory(card, ["jpg"]), config)
    assert plan.items[0].target.name == "DSC00001_1.JPG"
    assert plan.items[0].skip_existing is False
