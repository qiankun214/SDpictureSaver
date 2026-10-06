"""picsaver.core —— 纯业务内核（不依赖任何界面 / Web / CLI 框架）。"""

from .copier import CanceledError, cleanup_temp_files, copy_plan
from .events import (
    PHASE_CANCELED,
    PHASE_COPYING,
    PHASE_FAILED,
    PHASE_FINISHED,
    PHASE_PLANNING,
    ProgressCallback,
    ProgressEvent,
)
from .exif_time import CaptureTime, read_capture_time
from .metrics import SpeedMeter
from .models import (
    DEFAULT_SUFFIXES,
    JPEG_SUFFIXES,
    RAW_SUFFIXES,
    SUPPORTED_SUFFIXES,
    ConfigError,
    CopyConfig,
    CopyItem,
    CopyPlan,
    CopyReport,
    FileFailure,
    NoTimeFile,
    PhotoGroup,
    ScanResult,
    SourceFile,
    human_bytes,
    normalize_suffixes,
    sanitize_component,
)
from .planner import build_plan, ensure_target_parent
from .scanner import NO_TIME_REASON, scan_directory
from .service import CopyService

__all__ = [
    "CanceledError",
    "CaptureTime",
    "ConfigError",
    "CopyConfig",
    "CopyItem",
    "CopyPlan",
    "CopyReport",
    "CopyService",
    "DEFAULT_SUFFIXES",
    "FileFailure",
    "JPEG_SUFFIXES",
    "NO_TIME_REASON",
    "NoTimeFile",
    "PHASE_CANCELED",
    "PHASE_COPYING",
    "PHASE_FAILED",
    "PHASE_FINISHED",
    "PHASE_PLANNING",
    "PhotoGroup",
    "ProgressCallback",
    "ProgressEvent",
    "RAW_SUFFIXES",
    "SUPPORTED_SUFFIXES",
    "ScanResult",
    "SourceFile",
    "SpeedMeter",
    "build_plan",
    "cleanup_temp_files",
    "copy_plan",
    "ensure_target_parent",
    "human_bytes",
    "normalize_suffixes",
    "read_capture_time",
    "sanitize_component",
    "scan_directory",
]
