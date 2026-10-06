"""API 请求 / 响应模型（pydantic v1，与 requirements 中锁定的版本一致）。"""

from __future__ import annotations

from datetime import date as date_type
from pathlib import Path
from typing import List

from pydantic import BaseModel, Field, validator

from ..core.models import DEFAULT_SUFFIXES, CopyConfig, normalize_suffixes


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
    """一次拷贝任务的输入参数。"""

    source_dir: str
    suffixes: List[str] = Field(default_factory=lambda: list(DEFAULT_SUFFIXES))
    date_from: date_type
    date_to: date_type
    target_parent: str
    place: str
    note: str
    recursive: bool = True

    @validator("source_dir", "target_parent")
    def _check_path(cls, value: str) -> str:  # noqa: D102
        text = (value or "").strip().strip('"')
        if not text:
            raise ValueError("文件夹路径不能为空")
        return text

    @validator("place", "note")
    def _check_text(cls, value: str) -> str:  # noqa: D102
        text = (value or "").strip()
        if not text:
            raise ValueError("地点与备注都必须填写")
        return text

    @validator("suffixes")
    def _check_suffixes(cls, value: List[str]) -> List[str]:  # noqa: D102
        return list(normalize_suffixes(value))

    def to_config(self) -> CopyConfig:
        """转换成 core 的参数对象。"""
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
    """创建拷贝任务的请求。"""

    config: CopyRequest
    dry_run: bool = False
