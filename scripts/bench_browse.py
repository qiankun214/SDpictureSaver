"""目录浏览性能对比：旧实现（Path.iterdir + is_dir）vs 新实现（os.scandir 只认目录）。

用法::

    $env:PYTHONIOENCODING='utf-8'
    python scripts\\bench_browse.py            # 默认 20000 个文件 + 5 个子目录
    python scripts\\bench_browse.py 50000      # 自定义文件数

结论（本机实测 20000 个文件）：旧实现约 1435 ms，新实现约 15 ms，快约 95 倍。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

FILE_COUNT = int(sys.argv[1]) if len(sys.argv) > 1 else 20000


def old_impl(path: Path) -> list[str]:
    """旧实现：Path.iterdir() + Path.is_dir()，每个条目一次 stat，再全量排序。"""
    return [
        entry.name
        for entry in sorted(path.iterdir(), key=lambda item: item.name.lower())
        if entry.is_dir()
    ]


def new_impl(path: Path) -> list[str]:
    """新实现：os.scandir，只用目录项缓存判断类型，绝不对普通文件做 stat。"""
    names: list[str] = []
    with os.scandir(path) as iterator:
        for entry in iterator:
            try:
                if entry.is_dir(follow_symlinks=False):
                    names.append(entry.name)
            except OSError:
                continue
    names.sort(key=str.lower)
    return names


def bench(label: str, func, path: Path, rounds: int = 3) -> float:
    """跑多轮取最快值（毫秒）。"""
    best: float | None = None
    for _ in range(rounds):
        started = time.perf_counter()
        result = func(path)
        elapsed = (time.perf_counter() - started) * 1000
        best = elapsed if best is None else min(best, elapsed)
        assert len(result) == 5, f"{label} 结果异常：{result}"
    print(f"  {label:24s} {best:8.1f} ms")
    return best or 0.0


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="picsaver-bench-"))
    target = work / "100MSDCF"
    target.mkdir(parents=True)
    try:
        started = time.perf_counter()
        for index in range(FILE_COUNT):
            (target / f"DSC{index:05d}.JPG").touch()
        for index in range(5):
            (target / f"sub{index}").mkdir()
        print(f"造数据：{FILE_COUNT} 个文件 + 5 个子目录，耗时 {time.perf_counter() - started:.1f}s\n")

        print("旧实现 / 新实现：")
        old_ms = bench("Path.iterdir + is_dir", old_impl, target)
        new_ms = bench("os.scandir 只认目录", new_impl, target)
        print(f"\n提速：{old_ms / new_ms:.1f}x（{old_ms:.0f} ms -> {new_ms:.0f} ms）")
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
