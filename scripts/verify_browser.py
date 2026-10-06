"""真实浏览器端到端验证：用 CDP 驱动无头 Edge，跑完整界面交互并截图。

前置条件（两步）：

1. 启动后端（仓库根目录）：``.\\run_web.ps1 8791``
2. 启动无头 Edge（沙箱/普通环境都需要，自行拉起）::

       & 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe' `
         --headless=new --disable-gpu --no-first-run `
         --remote-debugging-port=9222 `
         --user-data-dir="$env:TEMP\\picsaver-edge" `
         --window-size=1460,1240 http://127.0.0.1:8791/

3. 运行本脚本::

       $env:PYTHONIOENCODING='utf-8'
       python scripts\\verify_browser.py --base-url http://127.0.0.1:8791

脚本会（必要时）生成演示素材、填写表单、扫描、预览、真实拷贝数百 MB 文件，
校验进度条中间态、报告数值与磁盘产物，并把截图写到 ``_tmp/verify-shots``。
"""

from __future__ import annotations

import argparse
import base64
import json
import shutil
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

from websockets.sync.client import connect

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "src"))

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" —— {detail}" if detail else ""))
    if not ok:
        failures.append(name)


def generate_card(card: Path, big_mb: int, count: int) -> None:
    """生成含大文件的演示素材，让复制过程持续足够久以观察进度。"""
    from fixtures import make_pair

    if card.exists():
        shutil.rmtree(card)
    directory = card / "DCIM" / "100MSDCF"
    for index in range(count):
        moment = datetime(2026, 7, 5, 10 + index, 0, 0)
        make_pair(directory, f"BIG{index + 1:04d}", moment, suffixes=("jpg", "arw"))
        with open(directory / f"BIG{index + 1:04d}.arw", "wb") as handle:
            remaining = big_mb * 1024 * 1024
            while remaining > 0:
                chunk = min(remaining, 1024 * 1024)
                handle.write(b"\0" * chunk)
                remaining -= chunk
    total = sum(p.stat().st_size for p in card.rglob("*") if p.is_file())
    print(f"素材: {card}  {count * 2} 个文件 / {total / 1024 / 1024:.0f} MB")


class CDP:
    """极简 Chrome DevTools Protocol 客户端。"""

    def __init__(self, ws_url: str) -> None:
        self.ws = connect(ws_url, max_size=256 * 1024 * 1024, open_timeout=20)
        self.counter = 0

    def call(self, method: str, **params):
        self.counter += 1
        message_id = self.counter
        self.ws.send(json.dumps({"id": message_id, "method": method, "params": params}))
        deadline = time.time() + 60
        while time.time() < deadline:
            message = json.loads(self.ws.recv(timeout=60))
            if message.get("id") == message_id:
                if "error" in message:
                    raise RuntimeError(f"{method}: {message['error']}")
                return message.get("result", {})
        raise TimeoutError(method)

    def evaluate(self, expression: str):
        result = self.call("Runtime.evaluate", expression=expression, returnByValue=True, awaitPromise=True)
        return result.get("result", {}).get("value")

    def shot(self, path: Path, name: str) -> None:
        data = self.call("Page.captureScreenshot", format="png")["data"]
        (path / name).write_bytes(base64.b64decode(data))


def find_page_ws(base_url: str, debug_port: int) -> str:
    for _ in range(60):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{debug_port}/json/list", timeout=3) as response:
                targets = json.load(response)
            for target in targets:
                if target.get("type") == "page" and base_url.split("//")[-1] in target.get("url", ""):
                    return target["webSocketDebuggerUrl"]
        except Exception:  # noqa: BLE001 - 浏览器/端口还没就绪
            pass
        time.sleep(0.5)
    raise RuntimeError(f"在 {debug_port} 端口找不到已打开页面的调试目标")


def wait_for(cdp: CDP, expression: str, timeout: float = 60.0, interval: float = 0.15):
    deadline = time.time() + timeout
    value = None
    while time.time() < deadline:
        value = cdp.evaluate(expression)
        if value:
            return value
        time.sleep(interval)
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description="真实浏览器端到端验证")
    parser.add_argument("--base-url", default="http://127.0.0.1:8791")
    parser.add_argument("--card", default=str(ROOT / "_tmp" / "verify-card"))
    parser.add_argument("--out", default=str(ROOT / "_tmp" / "verify-out"))
    parser.add_argument("--shots", default=str(ROOT / "_tmp" / "verify-shots"))
    parser.add_argument("--debug-port", type=int, default=9222)
    parser.add_argument("--big-mb", type=int, default=150, help="每个 RAW 的大小（MB）")
    parser.add_argument("--count", type=int, default=4, help="jpg+raw 组数")
    parser.add_argument("--no-generate", action="store_true", help="复用已有素材")
    args = parser.parse_args()

    card = Path(args.card)
    out = Path(args.out)
    shots = Path(args.shots)
    if not args.no_generate:
        generate_card(card, args.big_mb, args.count)
    if out.exists():
        shutil.rmtree(out)
    if shots.exists():
        shutil.rmtree(shots)
    shots.mkdir(parents=True)

    cdp = CDP(find_page_ws(args.base_url, args.debug_port))
    cdp.call("Page.enable")
    cdp.call("Runtime.enable")

    health = wait_for(cdp, "document.getElementById('health-text')?.textContent || ''", timeout=15)
    check("页面显示后端连通状态", "已连接" in (health or ""), str(health))
    check("页面标题", "pictureSaver" in (cdp.evaluate("document.title") or ""), cdp.evaluate("document.title") or "")

    cdp.evaluate(
        "window.__picsaverErrors = [];"
        "window.addEventListener('error', e => window.__picsaverErrors.push(String(e.message)));"
        "window.addEventListener('unhandledrejection', e => window.__picsaverErrors.push(String(e.reason)));"
        "true"
    )
    cdp.shot(shots, "01-initial.png")

    cdp.evaluate(
        """
        (() => {
          const set = (id, value) => {
            const el = document.getElementById(id);
            el.value = value;
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
          };
          set('source-dir', %s);
          set('target-parent', %s);
          set('date-from', '2026-07-05');
          set('date-to', '2026-07-06');
          set('place', '北海道');
          set('note', '家庭旅行');
          set('recursive', true);
          return true;
        })()
        """
        % (json.dumps(str(card)), json.dumps(str(out)))
    )
    preview_name = cdp.evaluate("document.getElementById('folder-preview-text').textContent")
    check("目录名实时预览", preview_name == "[20260705]北海道-家庭旅行", str(preview_name))

    cdp.evaluate("document.getElementById('btn-scan').click(); true")
    wait_for(cdp, "!document.getElementById('scan-card').hidden")
    wait_for(cdp, "document.getElementById('sum-matched').textContent !== '0'")
    matched = cdp.evaluate("document.getElementById('sum-matched').textContent")
    check("扫描命中文件数", matched == str(args.count * 2), str(matched))
    cdp.shot(shots, "02-scan.png")

    cdp.evaluate("document.getElementById('btn-preview').click(); true")
    wait_for(cdp, "!document.getElementById('plan-card').hidden")
    wait_for(cdp, "document.getElementById('plan-files').textContent !== '0'")
    folder = cdp.evaluate("document.getElementById('plan-folder').textContent")
    files = cdp.evaluate("document.getElementById('plan-files').textContent")
    check("预览文件夹名", folder == "[20260705]北海道-家庭旅行", str(folder))
    check("预览计划文件数", files == str(args.count * 2), str(files))
    cdp.shot(shots, "03-preview.png")

    cdp.evaluate("document.getElementById('btn-start').click(); true")
    samples: list[dict] = []
    deadline = time.time() + 180
    while time.time() < deadline:
        state = cdp.evaluate(
            """
            ({
              percent: document.getElementById('progress-percent').textContent,
              files: document.getElementById('progress-files').textContent,
              bytes: document.getElementById('progress-bytes').textContent,
              speed: document.getElementById('progress-speed').textContent,
              eta: document.getElementById('progress-eta').textContent,
              current: document.getElementById('progress-current-name').textContent,
              phase: document.getElementById('progress-phase').textContent,
              report_visible: !document.getElementById('report-card').hidden
            })
            """
        )
        samples.append(state)
        if state["report_visible"]:
            break
        if len(samples) <= 3:
            cdp.shot(shots, f"04-progress-{len(samples)}.png")
        time.sleep(0.12)

    mids = [s for s in samples if 0 < float((s["percent"] or "0%").rstrip("%") or 0) < 100]
    check("进度条出现中间态（真实进度）", bool(mids), f"{len(mids)}/{len(samples)} 个采样在 0~100%")
    percents = [float((s["percent"] or "0%").rstrip("%") or 0) for s in samples]
    check("进度百分比单调不减", percents == sorted(percents), str(percents[:6]))
    check("显示当前文件名", any(s["current"] not in ("—", "等待开始", "") for s in samples))
    check("显示速度与 ETA", any(s["speed"] not in ("—", "") and s["eta"] not in ("—", "") for s in samples))

    wait_for(cdp, "!document.getElementById('report-card').hidden")
    report = {
        "status": cdp.evaluate("document.getElementById('report-status').textContent"),
        "copied": cdp.evaluate("document.getElementById('report-copied').textContent"),
        "failed": cdp.evaluate("document.getElementById('report-failed').textContent"),
        "percent": cdp.evaluate("document.getElementById('progress-percent').textContent"),
        "target": cdp.evaluate("document.getElementById('report-target').textContent"),
    }
    check("报告：已完成", "完成" in (report["status"] or ""), str(report["status"]))
    check("报告：复制数量", report["copied"] == str(args.count * 2), str(report["copied"]))
    check("报告：失败为 0", report["failed"] == "0", str(report["failed"]))
    check("报告：进度 100%", report["percent"] == "100.0%", str(report["percent"]))
    cdp.shot(shots, "05-report.png")

    errors = cdp.evaluate("window.__picsaverErrors || []")
    check("交互全程无 JS 错误", not errors, str(errors)[:200])

    target = out / "[20260705]北海道-家庭旅行"
    copied_bytes = sum(p.stat().st_size for p in target.rglob("*") if p.is_file())
    source_bytes = sum(p.stat().st_size for p in card.rglob("*") if p.is_file())
    check("磁盘：字节数与源一致", copied_bytes == source_bytes, f"{copied_bytes} vs {source_bytes}")
    check("磁盘：源文件仍在", any(card.rglob("*.arw")))
    check("磁盘：按后缀分目录", (target / "JPG").is_dir() and (target / "ARW").is_dir())

    print(f"\n截图目录: {shots}")
    if failures:
        print(f"BROWSER CHECK FAILED: {len(failures)} -> {failures}")
        return 1
    print("BROWSER CHECK OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
