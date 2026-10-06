"""源目录扫描：过滤后缀、递归遍历、把同名 jpg/raw 配对成一张照片。"""

from __future__ import annotations

import os
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Iterable, Iterator

from .exif_time import read_capture_time
from .models import (
    JPEG_SUFFIXES,
    RAW_SUFFIXES,
    NoTimeFile,
    PhotoGroup,
    ScanResult,
    SourceFile,
    normalize_suffixes,
)

#: 扫描时跳过的目录名（系统/回收站目录）
SKIP_DIR_NAMES: frozenset[str] = frozenset(
    {
        "$RECYCLE.BIN",
        "System Volume Information",
        ".Trash",
        ".Trashes",
        ".Spotlight-V100",
        ".fseventsd",
        "__MACOSX",
    }
)

#: 读取时间的后缀优先级：先 JPG（读取快），再 RAW
_TIME_PRIORITY: tuple[str, ...] = ("jpg", "jpeg", "arw", "orf")

#: 无法确定时间时的原因文案
NO_TIME_REASON = "未找到可读的 EXIF 拍摄时间（已尝试 ExifRead / Pillow / TIFF 解析）"


def _norm(path: str | Path) -> str:
    """路径规范化（小写、统一分隔符），用于比较是否位于某个目录内。"""
    return os.path.normcase(os.path.abspath(str(path)))


def _is_inside(child: str | Path, parent: str | Path) -> bool:
    """判断 ``child`` 是否位于 ``parent`` 之内（含自身）。"""
    child_norm = _norm(child)
    parent_norm = _norm(parent)
    if child_norm == parent_norm:
        return True
    return child_norm.startswith(parent_norm.rstrip("\\/") + os.sep)


def _iter_files(root: Path, recursive: bool, excluded: Iterable[str] = ()) -> Iterator[Path]:
    """遍历源目录下的文件，跳过系统目录与被排除目录。"""
    excluded_norms = [_norm(item) for item in excluded]
    if not recursive:
        for entry in _safe_iterdir(root):
            if entry.is_file():
                yield entry
        return

    for current_root, dir_names, file_names in os.walk(root, onerror=lambda _e: None):
        current = Path(current_root)
        dir_names[:] = [
            name
            for name in dir_names
            if name not in SKIP_DIR_NAMES
            and not name.startswith(".")
            and not any(_is_inside(current / name, item) for item in excluded_norms)
        ]
        if any(_is_inside(current, item) and _norm(current) != item for item in excluded_norms):
            continue
        for name in file_names:
            yield current / name


def _safe_iterdir(path: Path) -> list[Path]:
    """列目录，忽略权限错误。"""
    try:
        return list(path.iterdir())
    except OSError:
        return []


def _resolve_group_time(group: PhotoGroup) -> None:
    """为一张照片确定拍摄时间：优先自身 EXIF，其次同名配对文件的时间。

    结果写回 ``group.capture_time`` / ``time_source`` / ``time_reason``。
    """
    ordered = [s for s in _TIME_PRIORITY if s in group.files]
    ordered += [s for s in group.files if s not in ordered]
    failures: list[str] = []
    for suffix in ordered:
        source_file = group.files[suffix]
        try:
            found = read_capture_time(source_file.path, suffix)
        except Exception as exc:  # noqa: BLE001 - 单个文件读取失败不影响其它文件
            failures.append(f"{source_file.path.name}: {type(exc).__name__}")
            continue
        if found is not None:
            group.capture_time = found.value
            if len(group.files) > 1 and suffix not in JPEG_SUFFIXES:
                group.time_source = f"paired:{found.describe()}"
            else:
                group.time_source = found.describe()
            group.time_reason = ""
            return
    group.capture_time = None
    group.time_source = ""
    group.time_reason = NO_TIME_REASON
    if failures:
        group.time_reason = f"{NO_TIME_REASON}；部分文件读取异常：{', '.join(failures[:3])}"


def scan_directory(
    source_dir: str | Path,
    suffixes: Iterable[str],
    *,
    recursive: bool = True,
    exclude_dirs: Iterable[str] = (),
) -> ScanResult:
    """扫描源目录，返回按同名配对聚合后的 :class:`ScanResult`。

    :param source_dir: 源目录（SD 卡挂载点）
    :param suffixes: 需要处理的后缀（大小写不敏感，可带点）
    :param recursive: 是否递归子目录
    :param exclude_dirs: 需要排除的目录（例如目标目录，防止自我递归）
    """
    started = time.monotonic()
    root = Path(source_dir)
    if not root.exists():
        raise FileNotFoundError(f"原始文件夹不存在：{root}")
    if not root.is_dir():
        raise NotADirectoryError(f"原始文件夹不是目录：{root}")

    wanted = set(normalize_suffixes(suffixes))
    groups: dict[str, PhotoGroup] = {}
    suffix_counts: Counter[str] = Counter()
    total_bytes = 0
    matched_files = 0

    for path in _iter_files(root, recursive, excluded=exclude_dirs):
        suffix = path.suffix.lstrip(".").lower()
        if suffix not in wanted:
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        try:
            rel_dir = path.parent.relative_to(root).as_posix()
        except ValueError:
            rel_dir = ""
        if rel_dir == ".":
            rel_dir = ""
        key = f"{rel_dir.lower()}/{path.stem.lower()}"
        group = groups.get(key)
        if group is None:
            group = PhotoGroup(key=key)
            groups[key] = group
        group.files[suffix] = SourceFile(
            path=path, suffix=suffix, size=size, stem=path.stem, rel_dir=rel_dir
        )
        suffix_counts[suffix] += 1
        total_bytes += size
        matched_files += 1

    no_time: list[NoTimeFile] = []
    for group in groups.values():
        _resolve_group_time(group)
        if group.capture_time is None:
            for suffix in sorted(group.files):
                source_file = group.files[suffix]
                no_time.append(
                    NoTimeFile(path=source_file.path, suffix=suffix, reason=group.time_reason)
                )

    ordered_groups = sorted(
        groups.values(),
        key=lambda g: (g.capture_time or datetime.min, g.key),
    )
    no_time.sort(key=lambda item: str(item.path))

    return ScanResult(
        source_dir=root,
        groups=ordered_groups,
        suffix_counts={key: int(value) for key, value in sorted(suffix_counts.items())},
        matched_files=matched_files,
        total_bytes=total_bytes,
        elapsed_seconds=time.monotonic() - started,
        no_time_files=no_time,
    )


#: 供外部判断某后缀是否属于 RAW
RAW_SUFFIX_SET = RAW_SUFFIXES
