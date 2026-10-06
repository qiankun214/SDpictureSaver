"""真实浏览器端到端验证：用 CDP 驱动无头 Edge，跑完整界面交互并截图。

前置条件（两步）：

1. 启动后端（仓库根目录）：``.\\run_web.cmd 8791``
2. 启动无头 Edge::

       & 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe' `
         --headless=new --disable-gpu --no-first-run `
         --remote-debugging-port=9222 `
         --user-data-dir="$env:TEMP\\picsaver-edge" `
         --window-size=1460,1240 http://127.0.0.1:8791/

3. 运行本脚本::

       $env:PYTHONIOENCODING='utf-8'
       python scripts\\verify_browser.py --base-url http://127.0.0.1:8791

覆盖的界面行为：

- 目录浏览弹窗**只列文件夹**（两万个文件的目录也能秒开）与 `total_dirs/truncated/elapsed_ms` 提示
- 弹窗内**新建文件夹**（成功进入新目录、重名给出错误提示）
- **扫描 / 预览 / 拷贝**三种任务都能看到真实进度（扫描阶段显示"已扫描 X / 共 Y 个文件"）
- 任务运行期间**配置区锁定**（`fieldset` 禁用、三个操作按钮禁用、只留取消），结束自动解锁
- 复制结果、报告数值与磁盘产物一致
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import sys
import time
import urllib.request
from datetime import datetime, timedelta
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


def generate_card(card: Path, big_mb: int, big_count: int, pairs: int) -> None:
    """生成演示素材：若干大文件（复制进度可见）+ 若干小配对（扫描进度可见）。"""
    from fixtures import make_pair

    if card.exists():
        shutil.rmtree(card)
    directory = card / "DCIM" / "100MSDCF"
    start = datetime(2026, 7, 5, 9, 0, 0)
    for index in range(pairs):
        make_pair(directory, f"SMALL{index + 1:05d}", start + timedelta(seconds=7 * index), suffixes=("jpg", "arw"))
    for index in range(big_count):
        make_pair(directory, f"BIG{index + 1:04d}", datetime(2026, 7, 5, 20, index, 0), suffixes=("jpg", "arw"))
        with open(directory / f"BIG{index + 1:04d}.arw", "wb") as handle:
            remaining = big_mb * 1024 * 1024
            while remaining > 0:
                chunk = min(remaining, 1024 * 1024)
                handle.write(b"\0" * chunk)
                remaining -= chunk
    total = sum(item.stat().st_size for item in card.rglob("*") if item.is_file())
    files = sum(1 for item in card.rglob("*") if item.is_file())
    print(f"素材: {card}  {files} 个文件 / {total / 1024 / 1024:.0f} MB")


def generate_big_dir(target: Path, file_count: int) -> None:
    """造一个"两万个文件 + 3 个子目录"的目录，用于验证浏览接口只列文件夹。"""
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    template = target / "_template"
    template.write_bytes(b"x")
    for index in range(file_count):
        path = target / f"DSC{index:05d}.JPG"
        try:
            os.link(template, path)
        except OSError:
            path.touch()
    template.unlink()
    for name in ("aaa", "bbb", "ccc"):
        (target / name).mkdir()
    print(f"大目录: {target}  {file_count} 个文件 + 3 个子目录")


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


def wait_for(cdp: CDP, expression: str, timeout: float = 60.0, interval: float = 0.05):
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
    parser.add_argument("--big-dir", default=str(ROOT / "_tmp" / "verify-bigdir"))
    parser.add_argument("--out", default=str(ROOT / "_tmp" / "verify-out"))
    parser.add_argument("--shots", default=str(ROOT / "_tmp" / "verify-shots"))
    parser.add_argument("--debug-port", type=int, default=9222)
    parser.add_argument("--big-mb", type=int, default=150, help="每个大 RAW 的大小（MB）")
    parser.add_argument("--big-count", type=int, default=4, help="大文件组数")
    parser.add_argument("--pairs", type=int, default=300, help="小配对组数（让扫描进度可见）")
    parser.add_argument("--big-files", type=int, default=20000, help="大目录里的文件数")
    parser.add_argument("--no-generate", action="store_true", help="复用已有素材")
    args = parser.parse_args()

    card = Path(args.card)
    big_dir = Path(args.big_dir)
    out = Path(args.out)
    shots = Path(args.shots)
    if not args.no_generate:
        generate_card(card, args.big_mb, args.big_count, args.pairs)
        generate_big_dir(big_dir, args.big_files)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    if shots.exists():
        shutil.rmtree(shots)
    shots.mkdir(parents=True)

    expected_files = (args.pairs + args.big_count) * 2

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

    # ---------- 改进 1 + 2：目录浏览与新建文件夹 ----------
    cdp.evaluate("document.getElementById('btn-browse-target').click(); true")
    wait_for(cdp, "!document.getElementById('browser-modal').hidden", timeout=10)

    cdp.evaluate(
        "(() => { const el = document.getElementById('browser-path'); el.value = %s;"
        " el.dispatchEvent(new Event('input', {bubbles: true}));"
        " document.getElementById('browser-go').click(); return true; })()" % json.dumps(str(big_dir))
    )
    # 等真正的目录列表（占位 <li> 不算），并等这次浏览请求结束
    wait_for(
        cdp,
        "document.getElementById('browser-path').value === %s"
        " && document.querySelectorAll('#browser-list .browser-item__btn').length > 0"
        " && !document.getElementById('browser-new-name').disabled" % json.dumps(str(big_dir)),
        timeout=30,
    )
    listed = cdp.evaluate(
        "Array.from(document.querySelectorAll('#browser-list .browser-item__btn .browser-item__name')).map(n => n.textContent)"
    )
    meta = cdp.evaluate("document.getElementById('browser-meta').textContent")
    elapsed_ms = cdp.evaluate("document.getElementById('browser-meta').textContent")
    print(f"   两万文件目录的浏览结果：{listed} / 提示：{meta}")
    check("浏览只列文件夹（两万个文件不出现）", listed == ["aaa", "bbb", "ccc"], str(listed))
    check("浏览提示含文件夹总数与耗时", "3 个文件夹" in (meta or "") and "耗时" in (meta or ""), str(elapsed_ms))
    cdp.shot(shots, "02-browse.png")

    # 转到干净的输出目录，在其下新建文件夹
    cdp.evaluate(
        "(() => { const el = document.getElementById('browser-path'); el.value = %s;"
        " el.dispatchEvent(new Event('input', {bubbles: true}));"
        " document.getElementById('browser-go').click(); return true; })()" % json.dumps(str(out))
    )
    wait_for(
        cdp,
        "document.getElementById('browser-path').value === %s"
        " && !document.getElementById('browser-new-name').disabled" % json.dumps(str(out)),
        timeout=20,
    )

    # 新建文件夹
    new_name = "2026北海道"
    cdp.evaluate(
        "(() => { const el = document.getElementById('browser-new-name'); el.value = %s;"
        " el.dispatchEvent(new Event('input', {bubbles: true}));"
        " document.getElementById('browser-mkdir').click(); return true; })()" % json.dumps(new_name)
    )
    created = wait_for(cdp, "document.getElementById('browser-path').value.indexOf(%s) >= 0" % json.dumps(new_name), timeout=20)
    new_dir = out / new_name
    check("新建文件夹后自动进入新目录", bool(created), str(cdp.evaluate("document.getElementById('browser-path').value")))
    check("新建的文件夹真的落盘", new_dir.is_dir(), str(new_dir))

    # 回到上级目录，验证重名会给出错误提示
    cdp.evaluate("document.getElementById('browser-up').click(); true")
    wait_for(
        cdp,
        "document.getElementById('browser-path').value === %s"
        " && !document.getElementById('browser-new-name').disabled" % json.dumps(str(out)),
        timeout=20,
    )
    cdp.evaluate(
        "(() => { const el = document.getElementById('browser-new-name'); el.value = %s;"
        " el.dispatchEvent(new Event('input', {bubbles: true}));"
        " document.getElementById('browser-mkdir').click(); return true; })()" % json.dumps(new_name)
    )
    duplicate_error = wait_for(
        cdp,
        "document.getElementById('browser-new-error').hidden ? '' : document.getElementById('browser-new-error').textContent",
        timeout=20,
    )
    check("重名时弹窗内提示错误", bool(duplicate_error) and "已存在" in duplicate_error, str(duplicate_error))
    cdp.shot(shots, "03-mkdir.png")

    # 进入刚新建的目录并选择它作为目的父文件夹
    cdp.evaluate(
        "(() => { const items = Array.from(document.querySelectorAll('#browser-list .browser-item__btn'));"
        " const hit = items.find(btn => btn.textContent.indexOf(%s) >= 0); if (hit) { hit.click(); } return !!hit; })()"
        % json.dumps(new_name)
    )
    wait_for(
        cdp,
        "document.getElementById('browser-path').value.indexOf(%s) >= 0"
        " && !document.getElementById('browser-new-name').disabled" % json.dumps(new_name),
        timeout=20,
    )
    cdp.evaluate("document.getElementById('browser-select').click(); true")
    wait_for(cdp, "document.getElementById('browser-modal').hidden", timeout=10)
    check("选择目录后回填目的父文件夹", new_dir.name in cdp.evaluate("document.getElementById('target-parent').value"), str(cdp.evaluate("document.getElementById('target-parent').value")))

    # ---------- 填写其余配置 ----------
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
          set('date-from', '2026-07-05');
          set('date-to', '2026-07-06');
          set('place', '北海道');
          set('note', '家庭旅行');
          set('recursive', true);
          return true;
        })()
        """
        % json.dumps(str(card))
    )
    check("目录名实时预览", cdp.evaluate("document.getElementById('folder-preview-text').textContent") == "[20260705]北海道-家庭旅行")

    # ---------- 改进 3 + 4：扫描任务（进度 + 锁定） ----------
    def snapshot() -> dict:
        return cdp.evaluate(
            """
            ({
              locked: document.getElementById('config-fields').disabled,
              lockNotice: !document.getElementById('config-lock-notice').hidden,
              placeDisabled: document.getElementById('place').matches(':disabled'),
              scanDisabled: document.getElementById('btn-scan').disabled,
              previewDisabled: document.getElementById('btn-preview').disabled,
              startDisabled: document.getElementById('btn-start').disabled,
              cancelDisabled: document.getElementById('btn-cancel').disabled,
              phase: document.getElementById('progress-phase').textContent,
              percent: document.getElementById('progress-percent').textContent,
              files: document.getElementById('progress-files').textContent,
              scanFiles: document.getElementById('progress-scan-files').textContent,
              scanLabel: document.getElementById('progress-scan-label').textContent,
              bytesLabel: document.getElementById('progress-bytes-label').textContent,
              current: document.getElementById('progress-current-name').textContent,
              scanCard: !document.getElementById('scan-card').hidden,
              planCard: !document.getElementById('plan-card').hidden,
              reportCard: !document.getElementById('report-card').hidden
            })
            """
        )

    def wait_locked(timeout: float = 15.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            state = snapshot()
            if state["locked"]:
                return state
            time.sleep(0.03)
        return snapshot()

    def collect_until(cdp: CDP, predicate_js: str, timeout: float = 180.0, shot_name: str | None = None, shots_dir: Path | None = None):
        """轮询直到条件成立，同时收集进度采样；可在首次出现中间进度时截图。"""
        samples: list[dict] = []
        shot_taken = False
        deadline = time.time() + timeout
        while time.time() < deadline:
            state = snapshot()
            samples.append(state)
            if shot_name and shots_dir and not shot_taken:
                percent = float((state["percent"] or "0%").rstrip("%") or 0)
                if 0 < percent < 100:
                    cdp.shot(shots_dir, shot_name)
                    shot_taken = True
            if cdp.evaluate(predicate_js):
                return state, samples
            time.sleep(0.08)
        return snapshot(), samples

    cdp.evaluate("document.getElementById('btn-scan').click(); true")
    locked_state = wait_locked()
    check("扫描任务开始后配置区被锁定", locked_state["locked"] is True, json.dumps(locked_state, ensure_ascii=False))
    check("锁定后地点输入框不可编辑", locked_state["placeDisabled"] is True)
    check("锁定后三个操作按钮都禁用", locked_state["scanDisabled"] and locked_state["previewDisabled"] and locked_state["startDisabled"])
    check("锁定时只留取消任务可用", locked_state["cancelDisabled"] is False)
    check("锁定时显示提示文案", locked_state["lockNotice"] is True)

    scan_state, scan_samples = collect_until(cdp, "!document.getElementById('scan-card').hidden")
    scan_texts = [s["files"] for s in scan_samples if "已扫描" in (s["files"] or "")]
    print("   扫描进度采样:", scan_texts[:6], "...", scan_texts[-1:])
    check("扫描阶段进度条显示「已扫描 X / 共 Y 个文件」", bool(scan_texts), str(scan_texts[:3]))
    scan_labels = {s["scanLabel"] for s in scan_samples}
    check("扫描阶段该栏标签为「命中文件 / 已读 EXIF」", "命中文件 / 已读 EXIF" in scan_labels, str(scan_labels))
    phases_seen = [s["phase"] for s in scan_samples]
    check("扫描期间进度徽标不是空闲状态", any(p not in ("等待开始", "已完成", "") for p in phases_seen), str(phases_seen[:6]))
    check("扫描完成后自动解锁", scan_state["locked"] is False)
    matched = cdp.evaluate("document.getElementById('sum-matched').textContent")
    check("扫描结果命中文件数正确", matched == str(expected_files), str(matched))
    cdp.shot(shots, "04-scan-job.png")

    # ---------- 预览任务 ----------
    cdp.evaluate("document.getElementById('btn-preview').click(); true")
    preview_locked = wait_locked()
    check("预览任务开始后配置区被锁定", preview_locked["locked"] is True)
    preview_state, preview_samples = collect_until(cdp, "!document.getElementById('plan-card').hidden")
    preview_texts = [s["files"] for s in preview_samples if "已扫描" in (s["files"] or "")]
    check("预览任务也有扫描进度", bool(preview_texts), str(preview_texts[:3]))
    check("预览完成后自动解锁", preview_state["locked"] is False)
    plan_files = cdp.evaluate("document.getElementById('plan-files').textContent")
    check("预览计划文件数正确", plan_files == str(expected_files), str(plan_files))
    cdp.shot(shots, "05-preview-job.png")

    # ---------- 拷贝任务 ----------
    cdp.evaluate("document.getElementById('btn-start').click(); true")
    copy_locked = wait_locked()
    check("拷贝任务开始后配置区被锁定", copy_locked["locked"] is True)

    copy_state, copy_samples = collect_until(
        cdp,
        "!document.getElementById('report-card').hidden",
        timeout=300,
        shot_name="05-copy-locked.png",
        shots_dir=shots,
    )
    percents = [float((s["percent"] or "0%").rstrip("%") or 0) for s in copy_samples]
    phases = [s["phase"] for s in copy_samples]
    mids = [p for p in percents if 0 < p < 100]
    scan_texts = [s["files"] for s in copy_samples if "已扫描" in (s["files"] or "")]
    print("   拷贝进度采样:", percents[:8], "...", percents[-3:])
    print("   阶段序列:", phases[:6], "...", phases[-3:])
    check("拷贝阶段出现中间态（真实进度）", bool(mids), str(percents[:6]))
    # 进度只会因为阶段切换（扫描 -> 复制）而回到 0，阶段内部必须单调不减
    drops = [(i, percents[i - 1], percents[i]) for i in range(1, len(percents)) if percents[i] < percents[i - 1]]
    phase_changes = [(a, b) for a, b in zip(phases, phases[1:]) if a != b]
    check(
        "百分比只在阶段切换时回退（阶段内部单调不减）",
        all(i > 0 and phases[i] != phases[i - 1] for i, _prev, _cur in drops),
        f"回退点 {drops}，阶段切换 {phase_changes[:4]}",
    )
    check("拷贝阶段先显示扫描进度再显示字节进度", bool(scan_texts), str(scan_texts[:2]))
    copy_labels = {s["scanLabel"] for s in copy_samples if "已扫描" not in (s["files"] or "")}
    check("拷贝阶段该栏标签换成「计划文件 / 已复制」", "计划文件 / 已复制" in copy_labels, str(copy_labels))
    check("拷贝完成后自动解锁", copy_state["locked"] is False)

    report = {
        "status": cdp.evaluate("document.getElementById('report-status').textContent"),
        "copied": cdp.evaluate("document.getElementById('report-copied').textContent"),
        "failed": cdp.evaluate("document.getElementById('report-failed').textContent"),
        "percent": cdp.evaluate("document.getElementById('progress-percent').textContent"),
        "target": cdp.evaluate("document.getElementById('report-target').textContent"),
        "unlocked": cdp.evaluate("!document.getElementById('config-fields').disabled"),
    }
    print("   报告区:", json.dumps(report, ensure_ascii=False))
    check("报告：已完成", "完成" in (report["status"] or ""), str(report["status"]))
    check("报告：复制数量正确", report["copied"] == str(expected_files), str(report["copied"]))
    check("报告：失败为 0", report["failed"] == "0", str(report["failed"]))
    check("报告：进度 100%", report["percent"] == "100.0%", str(report["percent"]))
    cdp.shot(shots, "06-report.png")

    errors = cdp.evaluate("window.__picsaverErrors || []")
    check("交互全程无 JS 错误", not errors, str(errors)[:200])

    # ---------- 磁盘核对 ----------
    target = out / new_name / "[20260705]北海道-家庭旅行"
    if not target.is_dir():
        target = Path(report["target"].strip()) if report["target"] else target
    jpg = len(list((target / "JPG").iterdir())) if (target / "JPG").is_dir() else 0
    arw = len(list((target / "ARW").iterdir())) if (target / "ARW").is_dir() else 0
    check("磁盘：JPG 子目录数量正确", jpg == args.pairs + args.big_count, str(jpg))
    check("磁盘：ARW 子目录数量正确", arw == args.pairs + args.big_count, str(arw))
    check("磁盘：目标目录建在新建的父目录下", new_name in str(target), str(target))
    check("磁盘：源文件仍在", any(card.rglob("*.arw")))

    print(f"\n截图目录: {shots}")
    if failures:
        print(f"BROWSER CHECK FAILED: {len(failures)} -> {failures}")
        return 1
    print("BROWSER CHECK OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
