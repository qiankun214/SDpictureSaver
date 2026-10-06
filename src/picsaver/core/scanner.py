"""源目录扫描：过滤后缀、递归遍历、把同名 jpg/raw 配对成一张照片。

扫描分两遍，以便界面显示**真实进度**：

1. ``counting``：只遍历目录、按后缀收集文件（用 :func:`os.scandir`，不 stat 文件内容），
   得到"需要处理多少个文件"；
2. ``scanning``：逐个配对读取 EXIF 拍摄时间，边读边回报"已扫描 / 共多少"。

遍历只用 ``os.scandir`` 的目录项缓存判断目录，不对普通文件做额外 stat，
因此在大目录（几万个文件）下比 ``Path.iterdir() + is_dir()`` 快约两个数量级。
"""

from __future__ import annotations

import os
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Iterable, Iterator

from .events import (
    PHASE_COUNTING,
    PHASE_SCANNING,
    ProgressCallback,
    ProgressEvent,
    emit,
)
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

#: 扫描阶段进度回调的最小间隔（秒）
PROGRESS_INTERVAL = 0.1
#: 每处理多少个配对至少回报一次进度
PROGRESS_EVERY_GROUPS = 16


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
    """遍历源目录下的文件，跳过系统目录与被排除目录（使用 scandir，不额外 stat）。"""
    excluded_norms = [_norm(item) for item in excluded]
    pending: list[Path] = [root]
    while pending:
        current = pending.pop()
        try:
            with os.scandir(current) as iterator:
                for entry in iterator:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if not recursive:
                                continue
                            name = entry.name
                            if name in SKIP_DIR_NAMES or name.startswith("."):
                                continue
                            path = Path(entry.path)
                            if any(_is_inside(path, item) for item in excluded_norms):
                                continue
                            pending.append(path)
                        elif entry.is_file(follow_symlinks=False):
                            yield Path(entry.path)
                    except OSError:
                        continue
        except OSError:
            continue


def _collect_groups(
    root: Path,
    wanted: set[str],
    recursive: bool,
    excluded: Iterable[str],
) -> tuple[dict[str, PhotoGroup], Counter[str], int, int]:
    """第一遍：按后缀收集文件并配对，返回 ``(分组, 后缀计数, 命中文件数, 总字节)``。"""
    groups: dict[str, PhotoGroup] = {}
    suffix_counts: Counter[str] = Counter()
    total_bytes = 0
    matched_files = 0

    for path in _iter_files(root, recursive, excluded):
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

    return groups, suffix_counts, matched_files, total_bytes


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


def _resolve_times(
    groups: dict[str, PhotoGroup],
    total_files: int,
    progress: ProgressCallback | None,
    cancel,
) -> tuple[list[NoTimeFile], int, bool]:
    """第二遍：逐个配对读取拍摄时间，并回报扫描进度。

    :return: ``(无时间文件清单, 已扫描文件数, 是否被取消)``
    """
    processed = 0
    scanned = 0
    no_time: list[NoTimeFile] = []
    last_emit = 0.0
    canceled = False

    for group in groups.values():
        if cancel is not None and cancel.is_set():
            canceled = True
            break
        _resolve_group_time(group)
        processed += 1
        scanned += max(1, len(group.files))
        if group.capture_time is None:
            for suffix in sorted(group.files):
                source_file = group.files[suffix]
                no_time.append(
                    NoTimeFile(path=source_file.path, suffix=suffix, reason=group.time_reason)
                )
        now = time.monotonic()
        if (
            now - last_emit >= PROGRESS_INTERVAL
            or processed % PROGRESS_EVERY_GROUPS == 0
            or processed == len(groups)
        ):
            last_emit = now
            done = min(scanned, total_files)
            first_suffix = sorted(group.files)[0] if group.files else None
            emit(
                progress,
                ProgressEvent(
                    phase=PHASE_SCANNING,
                    total_files=total_files,
                    done_files=done,
                    scanned_files=done,
                    scan_total_files=total_files,
                    current_file=str(group.files[first_suffix].path) if first_suffix else None,
                    message=f"正在读取拍摄时间：{done}/{total_files}",
                ),
            )
    return no_time, min(scanned, total_files), canceled


def scan_directory(
    source_dir: str | Path,
    suffixes: Iterable[str],
    *,
    recursive: bool = True,
    exclude_dirs: Iterable[str] = (),
    progress: ProgressCallback | None = None,
    cancel=None,
) -> ScanResult:
    """扫描源目录，返回按同名配对聚合后的 :class:`ScanResult`。

    :param source_dir: 源目录（SD 卡挂载点）
    :param suffixes: 需要处理的后缀（大小写不敏感，可带点）
    :param recursive: 是否递归子目录
    :param exclude_dirs: 需要排除的目录（例如目标目录，防止自我递归）
    :param progress: 进度回调，会依次收到 ``counting`` 与 ``scanning`` 两个阶段的事件
    :param cancel: 可选的 ``threading.Event``，置位后尽快停止扫描（结果标记 ``canceled``）
    """
    started = time.monotonic()
    root = Path(source_dir)
    if not root.exists():
        raise FileNotFoundError(f"原始文件夹不存在：{root}")
    if not root.is_dir():
        raise NotADirectoryError(f"原始文件夹不是目录：{root}")

    wanted = set(normalize_suffixes(suffixes))
    emit(progress, ProgressEvent(phase=PHASE_COUNTING, message="正在统计需要处理的文件…"))

    groups, suffix_counts, matched_files, total_bytes = _collect_groups(
        root, wanted, recursive, exclude_dirs
    )

    emit(
        progress,
        ProgressEvent(
            phase=PHASE_SCANNING,
            total_files=matched_files,
            scan_total_files=matched_files,
            message=f"共 {matched_files} 个文件，正在读取拍摄时间…",
        ),
    )

    no_time: list[NoTimeFile] = []
    canceled = False
    if matched_files:
        no_time, _scanned, canceled = _resolve_times(groups, matched_files, progress, cancel)
    no_time.sort(key=lambda item: str(item.path))

    ordered_groups = sorted(
        groups.values(),
        key=lambda g: (g.capture_time or datetime.min, g.key),
    )

    return ScanResult(
        source_dir=root,
        groups=ordered_groups,
        suffix_counts={key: int(value) for key, value in sorted(suffix_counts.items())},
        matched_files=matched_files,
        total_bytes=total_bytes,
        elapsed_seconds=time.monotonic() - started,
        no_time_files=no_time,
        canceled=canceled,
    )


#: 供外部判断某后缀是否属于 RAW
RAW_SUFFIX_SET = RAW_SUFFIXES
