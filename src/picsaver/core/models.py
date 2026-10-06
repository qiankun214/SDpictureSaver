"""核心数据模型：纯数据结构，不依赖 Web / UI / CLI 框架。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

#: 允许出现在界面上的后缀（小写、不含点）
SUPPORTED_SUFFIXES: tuple[str, ...] = ("jpg", "jpeg", "arw", "orf")
#: 界面默认勾选的后缀
DEFAULT_SUFFIXES: tuple[str, ...] = ("jpg", "arw", "orf")
#: RAW 后缀（索尼 arw / 奥林巴斯 orf）
RAW_SUFFIXES: frozenset[str] = frozenset({"arw", "orf"})
#: JPEG 后缀
JPEG_SUFFIXES: frozenset[str] = frozenset({"jpg", "jpeg"})

_ILLEGAL_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WHITESPACE = re.compile(r"\s+")


class ConfigError(ValueError):
    """用户输入不合法（会被 API 转成 400、被 CLI 转成友好报错）。"""


class PlanError(RuntimeError):
    """计划阶段发现无法继续的问题。"""


def normalize_suffixes(suffixes: Iterable[str]) -> tuple[str, ...]:
    """把用户输入的后缀规范化为小写、去重、按输入顺序的元组。

    :param suffixes: 例如 ``["JPG", ".arw", "orf"]``
    :raises ConfigError: 出现不支持的后缀，或最终为空
    """
    result: list[str] = []
    for item in suffixes:
        text = str(item).strip().lower().lstrip(".")
        if not text:
            continue
        if text not in SUPPORTED_SUFFIXES:
            raise ConfigError(f"不支持的后缀：{item}（目前支持 {', '.join(SUPPORTED_SUFFIXES)}）")
        if text not in result:
            result.append(text)
    if not result:
        raise ConfigError("请至少选择一个需要处理的后缀")
    return tuple(result)


def sanitize_component(text: str, *, field_name: str) -> str:
    """清理用于目录名的文本（去掉 Windows 非法字符、首尾空白与点）。

    :param text: 原始文本（地点 / 备注）
    :param field_name: 出错时提示用的字段名
    """
    cleaned = _ILLEGAL_NAME_CHARS.sub("", str(text or "")).strip()
    cleaned = _WHITESPACE.sub(" ", cleaned).strip().strip(".")
    if not cleaned:
        raise ConfigError(f"{field_name}不能为空，也不能只由非法字符组成")
    if len(cleaned) > 80:
        raise ConfigError(f"{field_name}过长（最多 80 个字符）")
    return cleaned


@dataclass(frozen=True)
class CopyConfig:
    """一次拷贝任务的全部输入参数（与界面无关的纯数据）。"""

    source_dir: Path
    suffixes: tuple[str, ...]
    date_from: date
    date_to: date
    target_parent: Path
    place: str
    note: str
    recursive: bool = True

    # ---- 校验与派生属性 -------------------------------------------------

    def validate(self) -> None:
        """校验参数，非法时抛出 :class:`ConfigError`。"""
        if self.date_from > self.date_to:
            raise ConfigError("开始日期不能晚于结束日期")
        source = Path(self.source_dir)
        if not source.exists():
            raise ConfigError(f"原始文件夹不存在：{source}")
        if not source.is_dir():
            raise ConfigError(f"原始文件夹不是目录：{source}")
        if not self.suffixes:
            raise ConfigError("请至少选择一个需要处理的后缀")
        normalize_suffixes(self.suffixes)
        source_norm = str(source.resolve()).casefold()
        parent_norm = str(Path(self.target_parent).resolve()).casefold()
        separator = "\\" if "\\" in source_norm else "/"
        # 目标父目录在源目录内部 -> 目标目录也在源目录内部 -> 会把自己拷出来的文件再当成源文件
        if parent_norm == source_norm or parent_norm.startswith(source_norm.rstrip("\\/") + separator):
            raise ConfigError("目的父文件夹不能位于原始文件夹内部，否则会把刚拷贝的文件再次当作源文件")
        if str(self.target_dir.resolve()).casefold() == source_norm:
            raise ConfigError("目标文件夹与原始文件夹是同一个目录，请改选其它位置")

    @property
    def place_clean(self) -> str:
        """清理后的地点。"""
        return sanitize_component(self.place, field_name="地点")

    @property
    def note_clean(self) -> str:
        """清理后的备注。"""
        return sanitize_component(self.note, field_name="备注")

    @property
    def folder_name(self) -> str:
        """目标文件夹名：``[YYYYMMDD]地点-备注``，日期取用户所选范围的第一天。"""
        day = self.date_from.strftime("%Y%m%d")
        return f"[{day}]{self.place_clean}-{self.note_clean}"

    @property
    def target_dir(self) -> Path:
        """目标文件夹完整路径（父文件夹 / ``[YYYYMMDD]地点-备注``）。"""
        return Path(self.target_parent) / self.folder_name

    def summary(self) -> dict[str, Any]:
        """供界面展示的参数摘要。"""
        return {
            "source_dir": str(self.source_dir),
            "suffixes": list(self.suffixes),
            "date_from": self.date_from.isoformat(),
            "date_to": self.date_to.isoformat(),
            "target_parent": str(self.target_parent),
            "place": self.place,
            "note": self.note,
            "folder_name": self.folder_name,
            "target_dir": str(self.target_dir),
            "recursive": self.recursive,
        }


@dataclass(frozen=True)
class SourceFile:
    """源目录中的一个文件。"""

    path: Path
    suffix: str  # 小写、不含点
    size: int
    stem: str  # 不含后缀的主文件名
    rel_dir: str  # 相对源目录的父目录（POSIX 风格，源目录本身为 ""）

    def to_dict(self) -> dict[str, Any]:
        return {"path": str(self.path), "suffix": self.suffix, "size": self.size}


@dataclass(frozen=True)
class NoTimeFile:
    """无法确定拍摄时间、将被跳过的文件。"""

    path: Path
    suffix: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {"path": str(self.path), "suffix": self.suffix, "reason": self.reason}


@dataclass
class PhotoGroup:
    """一张照片：同目录下同名（忽略大小写与后缀）的 jpg / raw 文件集合。"""

    key: str  # 配对键：相对目录 + "/" + 主文件名（小写）
    files: dict[str, SourceFile] = field(default_factory=dict)
    capture_time: datetime | None = None
    time_source: str = ""  # 例：exifread:EXIF DateTimeOriginal（JPG）
    time_reason: str = ""  # 无法确定时间时的原因描述

    @property
    def stem(self) -> str:
        return next(iter(self.files.values())).stem

    @property
    def total_size(self) -> int:
        return sum(f.size for f in self.files.values())

    @property
    def capture_date(self) -> date | None:
        return self.capture_time.date() if self.capture_time else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "stem": self.stem,
            "files": {s: f.to_dict() for s, f in sorted(self.files.items())},
            "capture_time": self.capture_time.isoformat() if self.capture_time else None,
            "time_source": self.time_source,
            "time_reason": self.time_reason,
        }


@dataclass
class ScanResult:
    """扫描结果汇总。"""

    source_dir: Path
    groups: list[PhotoGroup] = field(default_factory=list)
    suffix_counts: dict[str, int] = field(default_factory=dict)
    matched_files: int = 0
    total_bytes: int = 0
    elapsed_seconds: float = 0.0
    no_time_files: list[NoTimeFile] = field(default_factory=list)

    @property
    def with_time_files(self) -> int:
        """能确定拍摄时间的文件数。"""
        return sum(
            len(g.files) for g in self.groups if g.capture_time is not None
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_dir": str(self.source_dir),
            "groups": len(self.groups),
            "matched_files": self.matched_files,
            "total_bytes": self.total_bytes,
            "suffix_counts": dict(self.suffix_counts),
            "with_time": self.with_time_files,
            "no_time": len(self.no_time_files),
            "no_time_files": [f.to_dict() for f in self.no_time_files],
            "elapsed_seconds": round(self.elapsed_seconds, 3),
        }


@dataclass(frozen=True)
class CopyItem:
    """一个待复制的文件。"""

    source: SourceFile
    target: Path
    #: 计划阶段已判定"目标已存在同名且同大小"，执行时直接跳过
    skip_existing: bool = False

    @property
    def target_dir(self) -> Path:
        return self.target.parent

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": str(self.source.path),
            "target": str(self.target),
            "size": self.source.size,
            "suffix": self.source.suffix,
            "skip_existing": self.skip_existing,
        }


@dataclass
class CopyPlan:
    """拷贝计划（预览与执行共用）。"""

    config: CopyConfig
    folder_name: str
    target_dir: Path
    items: list[CopyItem] = field(default_factory=list)
    no_time: list[NoTimeFile] = field(default_factory=list)
    out_of_range_files: int = 0
    renamed: int = 0  # 因重名而被改名的文件数

    @property
    def planned_files(self) -> int:
        return len(self.items)

    @property
    def skipped_existing_files(self) -> int:
        """计划中目标已存在同大小文件、执行时会跳过的数量。"""
        return sum(1 for item in self.items if item.skip_existing)

    @property
    def planned_bytes(self) -> int:
        return sum(i.source.size for i in self.items)

    @property
    def suffix_dirs(self) -> list[str]:
        """目标目录下会创建的按后缀分类的子目录名（大写，已排序）。"""
        return sorted({i.source.suffix.upper() for i in self.items})

    def to_dict(self) -> dict[str, Any]:
        return {
            "folder_name": self.folder_name,
            "target_dir": str(self.target_dir),
            "planned_files": self.planned_files,
            "planned_bytes": self.planned_bytes,
            "out_of_range_files": self.out_of_range_files,
            "no_time_files": len(self.no_time),
            "suffix_dirs": self.suffix_dirs,
            "no_time": [f.to_dict() for f in self.no_time],
            "renamed": self.renamed,
            "skipped_existing_files": self.skipped_existing_files,
        }


@dataclass(frozen=True)
class FileFailure:
    """单个文件复制失败。"""

    source: Path
    target: Path
    error: str

    def to_dict(self) -> dict[str, Any]:
        return {"source": str(self.source), "target": str(self.target), "error": self.error}


@dataclass
class CopyReport:
    """执行结果报告。"""

    folder_name: str
    target_dir: Path
    total_files: int
    copied_files: int
    skipped_existing: int
    failed_files: int
    no_time_files: int
    out_of_range_files: int
    total_bytes: int
    copied_bytes: int
    elapsed_seconds: float
    canceled: bool = False
    dry_run: bool = False
    suffix_dirs: tuple[str, ...] = ()
    failures: list[FileFailure] = field(default_factory=list)
    no_time: list[NoTimeFile] = field(default_factory=list)
    renamed: int = 0

    @property
    def success(self) -> bool:
        return not self.canceled and not self.failures and not self.dry_run

    def to_dict(self) -> dict[str, Any]:
        return {
            "folder_name": self.folder_name,
            "target_dir": str(self.target_dir),
            "total_files": self.total_files,
            "copied_files": self.copied_files,
            "skipped_existing": self.skipped_existing,
            "failed_files": self.failed_files,
            "no_time_files": self.no_time_files,
            "out_of_range_files": self.out_of_range_files,
            "total_bytes": self.total_bytes,
            "copied_bytes": self.copied_bytes,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "canceled": self.canceled,
            "dry_run": self.dry_run,
            "suffix_dirs": list(self.suffix_dirs),
            "renamed": self.renamed,
            "failures": [f.to_dict() for f in self.failures],
            "no_time": [f.to_dict() for f in self.no_time],
        }

    def to_text(self) -> str:
        """人类可读的纯文本报告（CLI 使用）。"""
        lines = [
            "=" * 62,
            "拷贝结果报告" + ("（预览 / dry-run）" if self.dry_run else ""),
            "=" * 62,
            f"目标文件夹      : {self.target_dir}",
            f"计划文件数      : {self.total_files}",
            f"已复制          : {self.copied_files}",
            f"已存在跳过      : {self.skipped_existing}",
            f"复制失败        : {self.failed_files}",
            f"因无时间跳过    : {self.no_time_files}",
            f"日期范围外      : {self.out_of_range_files}",
            f"重名改名        : {self.renamed}",
            f"数据量          : {human_bytes(self.copied_bytes)} / {human_bytes(self.total_bytes)}",
            f"耗时            : {self.elapsed_seconds:.2f} 秒",
            f"是否被取消      : {'是' if self.canceled else '否'}",
        ]
        if self.failures:
            lines.append("-" * 62)
            lines.append(f"失败明细（{len(self.failures)} 条，最多显示 20 条）：")
            for item in self.failures[:20]:
                lines.append(f"  ! {item.source} -> {item.error}")
        if self.no_time:
            lines.append("-" * 62)
            lines.append(f"无 EXIF 拍摄时间而跳过的文件（{len(self.no_time)} 条，最多显示 20 条）：")
            for item in self.no_time[:20]:
                lines.append(f"  - {item.path}（{item.reason}）")
        return "\n".join(lines)


def human_bytes(size: float) -> str:
    """把字节数格式化成人可读字符串。"""
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} TB"


def suffix_counts_to_dict(counts: Mapping[str, int]) -> dict[str, int]:
    """按后缀排序输出计数（供界面展示）。"""
    return {key: int(counts[key]) for key in sorted(counts)}
