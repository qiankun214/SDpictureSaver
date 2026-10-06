"""速率与剩余时间估算：core 里的纯计算工具，不涉及任何展示格式。"""

from __future__ import annotations

import time
from collections import deque


class SpeedMeter:
    """基于滑动窗口的瞬时速度与 ETA 估算器。

    :param window_seconds: 参与计算的最近时间窗口（秒）
    :param min_span: 计算速度所需的最小时间跨度（秒），避免早期抖动
    """

    def __init__(self, window_seconds: float = 6.0, min_span: float = 0.35) -> None:
        self.window_seconds = float(window_seconds)
        self.min_span = float(min_span)
        self._samples: deque[tuple[float, float]] = deque()

    def reset(self) -> None:
        """清空采样。"""
        self._samples.clear()

    def update(self, done_bytes: int, now: float | None = None) -> None:
        """记录一次采样。"""
        timestamp = time.monotonic() if now is None else now
        self._samples.append((timestamp, float(done_bytes)))
        horizon = timestamp - self.window_seconds
        while len(self._samples) > 2 and self._samples[0][0] < horizon:
            self._samples.popleft()

    def speed_bps(self) -> float:
        """最近窗口内的平均速度（字节/秒）；样本不足时返回 0。"""
        if len(self._samples) < 2:
            return 0.0
        first_ts, first_bytes = self._samples[0]
        last_ts, last_bytes = self._samples[-1]
        span = last_ts - first_ts
        if span < self.min_span:
            return 0.0
        return max(0.0, (last_bytes - first_bytes) / span)

    def eta_seconds(self, remaining_bytes: float) -> float | None:
        """剩余时间（秒）；速度未知时返回 ``None``。"""
        speed = self.speed_bps()
        if speed <= 0:
            return None
        return max(0.0, float(remaining_bytes) / speed)
