"""桌面入口编排：单实例 → 启动本地服务 → 打开 Qt 窗口 → 退出清理。"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
import webbrowser

from .instance import InstanceLock, log_file, read_instance_url
from .server import DEFAULT_HOST, DEFAULT_PORT, start_server, wait_health
from .smoke import run_smoke
from .window import DesktopWindowError, run_window, show_error

log = logging.getLogger("picsaver.desktop")


def ensure_stdio() -> None:
    """窗口模式（PyInstaller ``--windowed``）下 ``sys.stdout/stderr`` 是 None。

    第三方库（uvicorn / PyInstaller 的异常输出等）会直接往它们写，因此这里换成
    ``os.devnull`` 的黑洞流，避免"打日志把自己打崩"。
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        devnull = open(os.devnull, "w", encoding="utf-8")
    except OSError:
        return
    if sys.stdout is None:
        sys.stdout = devnull
    if sys.stderr is None:
        sys.stderr = devnull


def configure_logging(quiet: bool = False) -> None:
    """日志既写文件（窗口模式唯一的排错入口），也在有控制台时输出。"""
    handlers: list[logging.Handler] = []
    try:
        handlers.append(logging.FileHandler(log_file(), encoding="utf-8"))
    except OSError:
        pass
    if sys.stdout is not None and not quiet:
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )


def _wait_until_server_stops(handle) -> None:
    """浏览器回退模式下保持进程存活，直到服务被 Ctrl+C 停止。"""
    try:
        while handle.running:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass


def run_desktop(
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    smoke: bool = False,
    cdp_port: int | None = None,
    browser_fallback: bool = True,
    lock_port: int | None = None,
) -> int:
    """启动桌面版，返回进程退出码。

    :param smoke: 只做服务自检（不开窗口），供打包验证使用
    :param cdp_port: 打开窗口的 Chromium 调试端口（自动化验证用）
    :param browser_fallback: 窗口不可用时自动改用系统浏览器打开
    """
    ensure_stdio()
    configure_logging()
    if smoke:
        return run_smoke(host, port)

    lock = InstanceLock(lock_port) if lock_port else InstanceLock()
    is_owner, existing_url = lock.acquire()
    if not is_owner:
        url = existing_url or read_instance_url()
        if url:
            log.info("检测到已有实例：%s（本次只打开窗口，不再启动服务）", url)
            try:
                return run_window(url, owns_server=False)
            except DesktopWindowError as exc:
                log.warning("第二个窗口打开失败（%s），改为交给系统浏览器", exc)
                webbrowser.open(url)
                return 0
        log.warning("实例锁被占用但读不到已有实例地址（锁端口可能被其它程序占用）")

    handle = start_server(host, port)
    try:
        if not wait_health(handle.url, timeout=30.0):
            message = f"本地服务未能在 30 秒内就绪。\n\n日志：{log_file()}"
            log.error(message)
            show_error("pictureSaver 启动失败", message)
            return 1
        lock.publish(handle.url)
        log.info("界面地址：%s", handle.url)
        try:
            return run_window(handle.url, owns_server=True, cdp_port=cdp_port)
        except DesktopWindowError as exc:
            log.error("桌面窗口创建失败：%s", exc)
            if not browser_fallback:
                show_error("pictureSaver 桌面窗口不可用", f"{exc}\n\n日志：{log_file()}")
                return 1
            show_error(
                "pictureSaver 桌面窗口不可用",
                f"无法创建桌面窗口（可能缺少 QtWebEngine 运行环境）。\n\n"
                f"已改用系统默认浏览器打开：{handle.url}\n日志：{log_file()}",
            )
            webbrowser.open(handle.url)
            _wait_until_server_stops(handle)
            return 0
    finally:
        handle.stop()
        lock.release()


def build_parser() -> argparse.ArgumentParser:
    """桌面入口参数。"""
    parser = argparse.ArgumentParser(
        prog="picsaver-desktop",
        description="pictureSaver 桌面端：用 QtWebEngine 打开现有 Web 界面",
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help="监听地址（默认 127.0.0.1）")
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"首选端口（默认 {DEFAULT_PORT}），被占用时自动改用系统分配的空闲端口",
    )
    parser.add_argument("--smoke", action="store_true", help="只自检服务与静态资源，不开窗口")
    parser.add_argument("--cdp-port", type=int, default=None, help="窗口的 Chromium 调试端口（自动化验证用）")
    parser.add_argument(
        "--no-browser-fallback",
        dest="browser_fallback",
        action="store_false",
        help="窗口不可用时不自动改用系统浏览器",
    )
    parser.add_argument("--lock-port", type=int, default=None, help="单实例锁端口（默认 8766）")
    return parser


def main(argv: list[str] | None = None) -> int:
    """``python -m picsaver.desktop`` 的入口。"""
    args = build_parser().parse_args(argv)
    try:
        return run_desktop(
            host=args.host,
            port=args.port,
            smoke=args.smoke,
            cdp_port=args.cdp_port,
            browser_fallback=args.browser_fallback,
            lock_port=args.lock_port,
        )
    except Exception as exc:  # noqa: BLE001 - 窗口模式没有控制台，异常必须落到日志与对话框
        ensure_stdio()
        configure_logging()
        log.exception("桌面端启动失败")
        show_error(
            "pictureSaver 启动失败",
            f"{type(exc).__name__}: {exc}\n\n日志：{log_file()}",
        )
        return 1
