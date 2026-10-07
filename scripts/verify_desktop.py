"""验证桌面窗口里真的是现有 Web 界面：用 CDP 连上 QtWebEngine，检查页面并截图。

用法（先手动启动窗口，再连上去）::

    python -m picsaver desktop --cdp-port 9222        # 终端 A
    python scripts\\verify_desktop.py --cdp-port 9222  # 终端 B

也可以让本脚本自己拉起窗口（源码或打包后的 exe）::

    python scripts\\verify_desktop.py --launch-module --app-port 8796
    python scripts\\verify_desktop.py --launch-exe dist\\pictureSaver2.exe --app-port 8797

退出码 0 表示全部检查通过。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

from _cdp import CDP, find_page_ws, wait_for  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" —— {detail}" if detail else ""))
    if not ok:
        failures.append(name)


def launch(kind: str, *, exe: str | None, app_port: int, cdp_port: int) -> subprocess.Popen:
    """拉起桌面窗口进程（输出丢弃，避免沙箱管道限制）。"""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src")
    env["PYTHONIOENCODING"] = "utf-8"
    if kind == "exe":
        command = [str(Path(exe).resolve()), "--port", str(app_port), "--cdp-port", str(cdp_port)]
    else:
        command = [sys.executable, "-m", "picsaver", "desktop", "--port", str(app_port), "--cdp-port", str(cdp_port)]
    print("启动:", " ".join(command))
    return subprocess.Popen(command, cwd=str(ROOT), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def stop(process: subprocess.Popen) -> None:
    """结束窗口进程（含子进程）。"""
    if process.poll() is not None:
        return
    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()


def main() -> int:
    parser = argparse.ArgumentParser(description="桌面窗口 CDP 验证")
    parser.add_argument("--cdp-port", type=int, default=9222)
    parser.add_argument("--app-port", type=int, default=8796)
    parser.add_argument("--shots", default=str(ROOT / "_tmp" / "desktop-shots"))
    parser.add_argument("--launch-module", action="store_true", help="自己用源码拉起窗口")
    parser.add_argument("--launch-exe", default=None, help="自己拉起打包后的 exe")
    parser.add_argument("--no-shot", action="store_true", help="不截图")
    args = parser.parse_args()

    shots = Path(args.shots)
    shots.mkdir(parents=True, exist_ok=True)

    process: subprocess.Popen | None = None
    if args.launch_exe:
        process = launch("exe", exe=args.launch_exe, app_port=args.app_port, cdp_port=args.cdp_port)
    elif args.launch_module:
        process = launch("module", exe=None, app_port=args.app_port, cdp_port=args.cdp_port)

    try:
        page_ws = find_page_ws(f"127.0.0.1:{args.app_port}", args.cdp_port, timeout=90.0)
    except RuntimeError as exc:
        if process is not None:
            print("窗口进程仍存活:", process.poll() is None)
            stop(process)
        print(f"[FAIL] 找不到窗口的调试目标 —— {exc}")
        return 1

    cdp = CDP(page_ws)
    cdp.call("Page.enable")
    cdp.call("Runtime.enable")

    title = wait_for(cdp, "document.title || ''", timeout=30, interval=0.2)
    check("窗口加载的是 pictureSaver 界面", "pictureSaver" in (title or ""), str(title))

    # 注意：占位文案（"正在连接后端…"）本身也非空，必须等它变成"已连接"
    health = wait_for(
        cdp,
        "(() => { const el = document.getElementById('health-text');"
        " return el && el.textContent.indexOf('已连接') >= 0 ? el.textContent : ''; })()",
        timeout=30,
        interval=0.2,
    )
    check("界面已连上本地后端", "已连接" in (health or ""), str(health))

    has_tasks = cdp.evaluate("!!document.getElementById('tasks-card')")
    has_buttons = cdp.evaluate("!!document.getElementById('btn-bg-preview') && !!document.getElementById('btn-bg-copy')")
    has_progress = cdp.evaluate("!!document.getElementById('progress-card')")
    check("后台任务中心已渲染（静态资源加载成功）", bool(has_tasks), f"tasks-card={has_tasks}")
    check("后台按钮存在", bool(has_buttons))
    check("进度卡存在", bool(has_progress))

    jobs = cdp.evaluate("fetch('/api/jobs?brief=1').then(r => r.json()).then(d => (d.jobs || []).length)")
    check("窗口内可直接访问后端接口", isinstance(jobs, int), f"jobs={jobs}")

    errors = cdp.evaluate("window.__desktopProbe === undefined ? 0 : 0")
    check("页面无阻塞性错误（探测脚本可执行）", errors == 0)

    if not args.no_shot:
        name = "desktop-window.png" if not args.launch_exe else "desktop-exe-window.png"
        cdp.shot(shots, name)
        print(f"截图: {shots / name}")

    if process is not None:
        stop(process)
        time.sleep(1.0)
        check("窗口进程已退出", process.poll() is not None, str(process.poll()))

    print()
    if failures:
        print(f"DESKTOP CHECK FAILED: {len(failures)} -> {failures}")
        return 1
    print("DESKTOP CHECK OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
