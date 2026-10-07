"""进度事件模型：core 只产出原始计数，展示格式化交给调用方（API / CLI）。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

# 阶段常量
PHASE_QUEUED = "queued"  # API 层排队中（尚未开始执行）
PHASE_COUNTING = "counting"  # 统计文件数量（不读 EXIF）
PHASE_SCANNING = "scanning"  # 逐个读取拍摄时间
PHASE_PLANNING = "planning"
PHASE_COPYING = "copying"
PHASE_FINISHED = "finished"
PHASE_CANCELED = "canceled"
PHASE_FAILED = "failed"


@dataclass(frozen=True)
class ProgressEvent:
    """一次进度回调携带的原始数据。"""

    phase: str
    total_files: int = 0
    done_files: int = 0
    total_bytes: int = 0
    done_bytes: int = 0
    current_file: str | None = None
    message: str = ""
    #: 扫描阶段：已读取（已判定）的文件数 / 需要读取的文件总数
    scanned_files: int = 0
    scan_total_files: int = 0

    def to_dict(self) -> dict[str, Any]:
        """转成可直接 JSON 序列化的字典。"""
        return {
            "phase": self.phase,
            "total_files": self.total_files,
            "done_files": self.done_files,
            "total_bytes": self.total_bytes,
            "done_bytes": self.done_bytes,
            "current_file": self.current_file,
            "message": self.message,
            "scanned_files": self.scanned_files,
            "scan_total_files": self.scan_total_files,
        }


#: 进度回调签名
ProgressCallback = Callable[[ProgressEvent], None]


def emit(callback: ProgressCallback | None, event: ProgressEvent) -> None:
    """安全触发进度回调：回调自身的异常不得影响拷贝流程。"""
    if callback is None:
        return
    try:
        callback(event)
    except Exception:  # noqa: BLE001 - 进度展示失败不能中断拷贝
        pass
