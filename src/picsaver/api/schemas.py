"""API 请求 / 响应模型（pydantic v1，与 requirements 中锁定的版本一致）。

注意：``CopyRequest`` 对"扫描任务"是宽松的——扫描只需要源目录与后缀，
因此日期 / 地点 / 备注 / 目标目录允许为空；真正要生成目录名的
``preview`` 与 ``copy`` 会在 :meth:`CopyRequest.to_config` 里强制校验。
"""

from __future__ import annotations

import re
from datetime import date as date_type
from pathlib import Path
from typing import List, Optional

from pydantic import BaseModel, Field, validator

from ..core.models import DEFAULT_SUFFIXES, ConfigError, CopyConfig, normalize_suffixes

#: 任务模式
JOB_MODES = ("scan", "preview", "copy")

#: Windows 非法文件名字符
_INVALID_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
#: Windows 保留设备名
_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


class ScanRequest(BaseModel):
    """扫描源目录的请求。"""

    source_dir: str = Field(..., description="原始文件夹（SD 卡目录）")
    suffixes: List[str] = Field(default_factory=lambda: list(DEFAULT_SUFFIXES))
    recursive: bool = True

    @validator("source_dir")
    def _check_source(cls, value: str) -> str:  # noqa: D102 - pydantic 校验
        text = (value or "").strip().strip('"')
        if not text:
            raise ValueError("原始文件夹不能为空")
        return text

    @validator("suffixes")
    def _check_suffixes(cls, value: List[str]) -> List[str]:  # noqa: D102
        return list(normalize_suffixes(value))


class CopyRequest(BaseModel):
    """一次拷贝任务的输入参数（扫描任务允许部分字段为空）。"""

    source_dir: str
    suffixes: List[str] = Field(default_factory=lambda: list(DEFAULT_SUFFIXES))
    date_from: Optional[date_type] = None
    date_to: Optional[date_type] = None
    target_parent: str = ""
    place: str = ""
    note: str = ""
    recursive: bool = True

    @validator("source_dir")
    def _check_source(cls, value: str) -> str:  # noqa: D102
        text = (value or "").strip().strip('"')
        if not text:
            raise ValueError("原始文件夹不能为空")
        return text

    @validator("target_parent")
    def _clean_target(cls, value: str) -> str:  # noqa: D102
        return (value or "").strip().strip('"')

    @validator("place", "note")
    def _clean_text(cls, value: str) -> str:  # noqa: D102
        return (value or "").strip()

    @validator("date_from", "date_to", pre=True)
    def _empty_date_is_none(cls, value):  # noqa: D102
        # 扫描任务里日期输入框可能是空字符串
        return None if value in ("", None) else value

    @validator("suffixes")
    def _check_suffixes(cls, value: List[str]) -> List[str]:  # noqa: D102
        return list(normalize_suffixes(value))

    def to_config(self) -> CopyConfig:
        """转换成 core 的参数对象（预览 / 拷贝任务要求字段齐全）。

        :raises ConfigError: 日期、地点、备注或目标父目录缺失
        """
        missing: list[str] = []
        if self.date_from is None:
            missing.append("开始日期")
        if self.date_to is None:
            missing.append("结束日期")
        if not self.place:
            missing.append("地点")
        if not self.note:
            missing.append("备注")
        if not self.target_parent:
            missing.append("目的父文件夹")
        if missing:
            raise ConfigError("请先填写：" + "、".join(missing))
        return CopyConfig(
            source_dir=Path(self.source_dir),
            suffixes=tuple(self.suffixes),
            date_from=self.date_from,
            date_to=self.date_to,
            target_parent=Path(self.target_parent),
            place=self.place,
            note=self.note,
            recursive=self.recursive,
        )


class JobCreateRequest(BaseModel):
    """创建任务的请求。

    - ``mode`` 省略时按 ``dry_run ? "preview" : "copy"`` 处理（兼容旧调用）
    - ``background=True`` 表示界面上的"后台任务"：按提交顺序 FIFO 排队；
      默认 False 表示前台任务，会插到等待队列最前面（两者都不与正在运行的任务并发）
    """

    config: CopyRequest
    mode: Optional[str] = None
    dry_run: bool = False
    background: bool = False

    @validator("mode")
    def _check_mode(cls, value):  # noqa: D102
        if value is None:
            return None
        text = str(value).strip().lower()
        if text not in JOB_MODES:
            raise ValueError(f"不支持的任务模式：{value}（支持 {', '.join(JOB_MODES)}）")
        return text

    @property
    def resolved_mode(self) -> str:
        """最终任务模式。"""
        if self.mode:
            return self.mode
        return "preview" if self.dry_run else "copy"


class MkdirRequest(BaseModel):
    """在指定父目录下新建文件夹的请求。

    名称的具体规则在 :func:`validate_folder_name` 里校验，以便统一返回
    HTTP 400 + 中文字符串错误（与"父目录不存在"等错误保持一致）。
    """

    parent: str
    name: str


def validate_folder_name(name: str) -> str:
    """校验新建文件夹的名称。

    :return: 去掉首尾空白后的名称
    :raises ConfigError: 名称为空、含首尾空白/点、含非法字符、是保留名或过长
    """
    raw = name or ""
    text = raw.strip()
    if not text:
        raise ConfigError("文件夹名不能为空")
    if text != raw:
        raise ConfigError("文件夹名首尾不能有空格")
    if text != text.strip("."):
        raise ConfigError("文件夹名首尾不能是点")
    if _INVALID_NAME.search(text):
        raise ConfigError('文件夹名不能包含 < > : " / \\ | ? * 等字符')
    if text.upper() in _RESERVED_NAMES:
        raise ConfigError(f"{text} 是 Windows 保留名称，请换一个")
    if len(text) > 100:
        raise ConfigError("文件夹名过长（最多 100 个字符）")
    return text
