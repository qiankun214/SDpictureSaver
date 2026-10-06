"""拷贝计划：日期范围筛选、目标目录命名 ``[YYYYMMDD]地点-备注``、按后缀分子目录、防重名。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .models import (
    ConfigError,
    CopyConfig,
    CopyItem,
    CopyPlan,
    NoTimeFile,
    ScanResult,
    SourceFile,
)

#: 无法确定时间时的原因文案（与 scanner 保持一致）
NO_TIME_REASON = "未找到可读的 EXIF 拍摄时间（已尝试 ExifRead / Pillow / TIFF 解析）"


def _resolve_target(base_dir: Path, source_file: SourceFile, used: set[str]) -> tuple[Path, bool]:
    """为源文件确定目标路径。

    :return: ``(目标路径, 是否命中"已存在同大小文件"而可跳过)``
    """
    suffix_dir = base_dir / source_file.suffix.upper()
    candidate = suffix_dir / source_file.path.name
    key = str(candidate).casefold()
    if key not in used and not candidate.exists():
        used.add(key)
        return candidate, False
    if key not in used and candidate.exists():
        try:
            if candidate.stat().st_size == source_file.size:
                used.add(key)
                return candidate, True
        except OSError:
            pass
    stem = candidate.stem
    ext = candidate.suffix
    index = 1
    while True:
        renamed = suffix_dir / f"{stem}_{index}{ext}"
        renamed_key = str(renamed).casefold()
        if renamed_key not in used and not renamed.exists():
            used.add(renamed_key)
            return renamed, False
        index += 1


def build_plan(scan: ScanResult, config: CopyConfig) -> CopyPlan:
    """根据扫描结果与用户参数生成拷贝计划。

    :param scan: :func:`picsaver.core.scanner.scan_directory` 的结果
    :param config: 用户参数
    :raises ConfigError: 参数不合法
    """
    config.validate()
    target_dir = config.target_dir
    items: list[CopyItem] = []
    no_time: list[NoTimeFile] = []
    out_of_range = 0
    renamed = 0
    used: set[str] = set()

    for group in sorted(scan.groups, key=lambda g: (g.capture_time or datetime.min, g.key)):
        selected = [group.files[s] for s in config.suffixes if s in group.files]
        if not selected:
            continue
        if group.capture_time is None:
            reason = group.time_reason or NO_TIME_REASON
            for source_file in selected:
                no_time.append(
                    NoTimeFile(path=source_file.path, suffix=source_file.suffix, reason=reason)
                )
            continue
        day = group.capture_time.date()
        if day < config.date_from or day > config.date_to:
            out_of_range += len(selected)
            continue
        for source_file in selected:
            target, skip_existing = _resolve_target(target_dir, source_file, used)
            if target.name != source_file.path.name:
                renamed += 1
            items.append(
                CopyItem(source=source_file, target=target, skip_existing=skip_existing)
            )

    return CopyPlan(
        config=config,
        folder_name=config.folder_name,
        target_dir=target_dir,
        items=items,
        no_time=no_time,
        out_of_range_files=out_of_range,
        renamed=renamed,
    )


def ensure_target_parent(config: CopyConfig) -> Path:
    """确保目标父文件夹存在（不存在则创建）。

    :raises ConfigError: 路径存在但不是目录
    """
    parent = Path(config.target_parent)
    if parent.exists() and not parent.is_dir():
        raise ConfigError(f"目的父文件夹不是一个目录：{parent}")
    return parent
