"""后台任务中心的真实浏览器验证：排队、进度、关闭、清除、刷新恢复、串行不并发。

前置条件（两步）：

1. 启动后端：``.\\run_web.cmd 8791``
2. 启动无头 Edge::

       & 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe' `
         --headless=new --disable-gpu --no-first-run `
         --remote-debugging-port=9222 `
         --user-data-dir="$env:TEMP\\picsaver-edge" `
         --window-size=1460,1400 http://127.0.0.1:8791/

3. 运行::

       $env:PYTHONIOENCODING='utf-8'
       python scripts\\verify_background.py --base-url http://127.0.0.1:8791
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _cdp import CDP, find_page_ws, wait_for  # noqa: E402

failures: list[str] = []

CARD_JS = """
Array.from(document.querySelectorAll('#tasks-list .task')).map(function (li) {
  var badge = li.querySelector('.phase-badge');
  var close = li.querySelector('.task__close');
  var cancel = li.querySelector('.task__cancel');
  return {
    jobId: li.dataset.jobId,
    mode: (li.querySelector('.task__mode') || {}).textContent || '',
    phase: badge ? (badge.dataset.phase || '') : '',
    badgeText: badge ? badge.textContent : '',
    percent: (li.querySelector('.progress__percent') || {}).textContent || '',
    files: (li.querySelector('.progress__files') || {}).textContent || '',
    queueText: (li.querySelector('.task__queue') || {}).textContent || '',
    closeHidden: close ? !!close.hidden : true,
    cancelDisabled: cancel ? !!cancel.disabled : true,
    text: (li.textContent || '').replace(/\\s+/g, ' ').trim()
  };
})
"""

ACTIVE_PHASES = ("counting", "scanning", "planning", "copying")
TERMINAL_PHASES = ("finished", "canceled", "failed")


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" —— {detail}" if detail else ""))
    if not ok:
        failures.append(name)


def build_card(card: Path, pairs: int) -> None:
    from fixtures import make_pair

    if card.exists():
        shutil.rmtree(card)
    directory = card / "DCIM"
    for index in range(pairs):
        make_pair(directory, f"DSC{index + 1:05d}", datetime(2026, 7, 5, 10, 0, index % 60), suffixes=("jpg", "arw"))
    print(f"素材：{card}   {pairs} 组 jpg+raw（{pairs * 2} 个文件）")


def set_field(cdp: CDP, field_id: str, value: str) -> None:
    cdp.evaluate(
        "(() => { const el = document.getElementById(%s); el.value = %s;"
        " el.dispatchEvent(new Event('input', {bubbles: true}));"
        " el.dispatchEvent(new Event('change', {bubbles: true})); return true; })()"
        % (json.dumps(field_id), json.dumps(value))
    )


def cards(cdp: CDP) -> list[dict]:
    return cdp.evaluate(CARD_JS) or []


def click(cdp: CDP, element_id: str) -> None:
    cdp.evaluate("document.getElementById(%s).click(); true" % json.dumps(element_id))


def wait_cards(cdp: CDP, count: int, timeout: float = 20.0) -> list[dict]:
    deadline = time.time() + timeout
    current: list[dict] = []
    while time.time() < deadline:
        current = cards(cdp)
        if len(current) >= count:
            return current
        time.sleep(0.05)
    return current


def click_background(cdp: CDP, button_id: str, expected_count: int, timeout: float = 20.0) -> list[dict]:
    """点击后台按钮并等待卡片数增加，返回**本次新增**的卡片。

    提交期间按钮会被禁用，连点会丢请求，所以先等按钮恢复可用再点；
    同时按 job_id 差集识别新卡片，避免被历史/恢复出来的卡片干扰。
    """
    before_ids = {item["jobId"] for item in cards(cdp)}
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not cdp.evaluate("document.getElementById(%s).disabled" % json.dumps(button_id)):
            break
        time.sleep(0.05)
    click(cdp, button_id)
    current = wait_cards(cdp, expected_count, timeout=timeout)
    new_cards = [item for item in current if item["jobId"] not in before_ids]
    return new_cards or current


def main() -> int:
    parser = argparse.ArgumentParser(description="后台任务中心浏览器验证")
    parser.add_argument("--base-url", default="http://127.0.0.1:8791")
    parser.add_argument("--card", default=str(ROOT / "_tmp" / "bg-verify-card"))
    parser.add_argument("--out", default=str(ROOT / "_tmp" / "bg-verify-out"))
    parser.add_argument("--shots", default=str(ROOT / "_tmp" / "bg-verify-shots"))
    parser.add_argument("--debug-port", type=int, default=9222)
    parser.add_argument("--pairs", type=int, default=400)
    parser.add_argument("--no-generate", action="store_true")
    args = parser.parse_args()

    card = Path(args.card)
    out = Path(args.out)
    shots = Path(args.shots)
    if not args.no_generate:
        build_card(card, args.pairs)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    if shots.exists():
        shutil.rmtree(shots)
    shots.mkdir(parents=True)

    cdp = CDP(find_page_ws(args.base_url, args.debug_port))
    cdp.call("Page.enable")
    cdp.call("Runtime.enable")
    wait_for(cdp, "document.getElementById('health-text')?.textContent || ''", timeout=15)

    cdp.evaluate(
        "window.__bgErrors = [];"
        "window.addEventListener('error', e => window.__bgErrors.push(String(e.message)));"
        "window.addEventListener('unhandledrejection', e => window.__bgErrors.push(String(e.reason)));"
        "true"
    )

    check("初始无任务卡", len(cards(cdp)) == 0, str(len(cards(cdp))))
    check("初始显示空态提示", cdp.evaluate("!document.getElementById('tasks-empty').hidden") is True)
    cdp.shot(shots, "01-tasks-empty.png")

    # 填写配置
    set_field(cdp, "source-dir", str(card))
    set_field(cdp, "target-parent", str(out))
    set_field(cdp, "date-from", "2026-07-05")
    set_field(cdp, "date-to", "2026-07-06")
    set_field(cdp, "place", "预览")
    set_field(cdp, "note", "后台")

    # ---- 1) 后台预览计划：不锁定配置区 ----
    own_ids: set[str] = set()
    first = click_background(cdp, "btn-bg-preview", 1)
    own_ids.update(item["jobId"] for item in first)
    check("点击后台预览计划后出现任务卡", len(first) >= 1, str(len(first)))
    check("后台任务不锁定配置区", cdp.evaluate("document.getElementById('config-fields').disabled") is False)
    check("后台任务的取消按钮可用", bool(first) and first[0]["cancelDisabled"] is False, str(first[:1]))
    check("未结束时没有关闭按钮", bool(first) and first[0]["closeHidden"] is True, str(first[:1]))

    # ---- 2) 后台拷贝：应当排队（不并发）----
    set_field(cdp, "place", "拷贝一")
    second = click_background(cdp, "btn-bg-copy", 2)
    own_ids.update(item["jobId"] for item in second)
    check("第二张（后台拷贝）任务卡出现", len(second) >= 1, str(len(second)))

    set_field(cdp, "place", "拷贝二")
    own_ids.update(item["jobId"] for item in click_background(cdp, "btn-bg-copy", 3))

    # 采样：观察"排队中"与并发数（上限放宽：机器繁忙时 3 个任务可能要几分钟）
    queued_seen = False
    max_active = 0
    deadline = time.time() + 600
    while time.time() < deadline:
        snapshot = cards(cdp)
        active = [item for item in snapshot if item["phase"] in ACTIVE_PHASES]
        max_active = max(max_active, len(active))
        if any(item["phase"] == "queued" or "排队中" in item["badgeText"] + item["queueText"] for item in snapshot):
            queued_seen = True
        if snapshot and all(item["phase"] in TERMINAL_PHASES for item in snapshot) and len(snapshot) >= 3:
            break
        time.sleep(0.08)

    check("出现排队中的任务卡", queued_seen, "至少一张卡片在采样期间显示过「排队中」" if queued_seen else "未观察到排队中")
    check("任一时刻最多一个任务在真正执行", max_active <= 1, f"峰值 {max_active}")

    final_cards = cards(cdp)
    print("   终局瞬间:", json.dumps([{k: item[k] for k in ("mode", "phase", "badgeText", "percent")} for item in final_cards], ensure_ascii=False))
    # 轮询周期 400ms：终局后可能还差一个 tick 才把"终局详情里的最终进度"渲染到卡片上
    time.sleep(1.5)
    final_cards = [item for item in cards(cdp) if item["jobId"] in own_ids]
    print("   最终卡片:", json.dumps([{k: item[k] for k in ("mode", "phase", "badgeText", "percent", "closeHidden")} for item in final_cards], ensure_ascii=False))
    check("三张卡片都存在", len(final_cards) >= 3, str(len(final_cards)))
    check("全部完成", all(item["phase"] == "finished" for item in final_cards), str([item["badgeText"] for item in final_cards]))
    check("完成后出现关闭按钮", all(item["closeHidden"] is False for item in final_cards), str([item["closeHidden"] for item in final_cards]))
    check(
        "完成后进度都到 100%",
        all(item["percent"] == "100.0%" for item in final_cards),
        str([item["percent"] for item in final_cards]),
    )
    check("结果摘要包含计划信息", any("计划文件" in item["text"] for item in final_cards),
          str([item["text"][-60:] for item in final_cards])[:200])
    check("结果摘要包含复制结果", any("成功复制" in item["text"] or "已复制" in item["text"] for item in final_cards),
          str([item["text"][-60:] for item in final_cards])[:200])
    check("配置区始终未被后台任务锁定", cdp.evaluate("document.getElementById('config-fields').disabled") is False)
    cdp.shot(shots, "02-tasks-done.png")

    # ---- 3) 刷新恢复：先启动两个任务再刷新页面 ----
    set_field(cdp, "place", "刷新一")
    own_ids.update(item["jobId"] for item in click_background(cdp, "btn-bg-copy", 4))
    set_field(cdp, "place", "刷新二")
    before_reload = click_background(cdp, "btn-bg-copy", 5, timeout=30)
    own_ids.update(item["jobId"] for item in before_reload)
    ids_before = [item["jobId"] for item in before_reload]
    check("刷新前有 5 张任务卡", len(before_reload) >= 1, str(len(before_reload)))

    cdp.call("Page.reload", ignoreCache=True)
    wait_for(cdp, "document.readyState === 'complete' && !!document.getElementById('tasks-list')", timeout=30)
    time.sleep(1.0)
    recovered = wait_for(cdp, "document.querySelectorAll('#tasks-list .task').length >= 2 && true", timeout=30)
    after_reload = cards(cdp)
    recovered_ids = [item["jobId"] for item in after_reload]
    print("   刷新后恢复:", json.dumps([{k: item[k] for k in ("mode", "phase", "badgeText")} for item in after_reload], ensure_ascii=False))
    check("刷新后恢复了进行中的任务", len(after_reload) >= 2, str(len(after_reload)))
    check("恢复的任务包含刷新前启动的任务", any(job_id in recovered_ids for job_id in ids_before[-2:]), str(recovered_ids))
    check("恢复后配置区未锁定", cdp.evaluate("document.getElementById('config-fields').disabled") is False)
    cdp.shot(shots, "03-after-reload.png")

    # 等恢复的任务结束
    deadline = time.time() + 180
    while time.time() < deadline:
        snapshot = cards(cdp)
        if snapshot and all(item["phase"] in TERMINAL_PHASES for item in snapshot):
            break
        time.sleep(0.2)

    # ---- 4) 关闭单张卡片：后端也不再有它 ----
    before = cards(cdp)
    target_id = before[0]["jobId"]
    cdp.evaluate(
        "(() => { const li = document.querySelector('#tasks-list .task[data-job-id=\"%s\"]');"
        " li.querySelector('.task__close').click(); return true; })()" % target_id
    )
    deadline = time.time() + 20
    while time.time() < deadline and any(item["jobId"] == target_id for item in cards(cdp)):
        time.sleep(0.05)
    after = cards(cdp)
    check("点 × 后卡片消失", all(item["jobId"] != target_id for item in after), str(len(after)))
    listed = cdp.evaluate("fetch('/api/jobs?brief=1').then(r => r.json()).then(d => d.jobs.map(j => j.job_id))")
    check("后端也不再返回该任务（刷新不会复活）", target_id not in (listed or []), str(listed)[:160])

    # ---- 5) 清除已完成 ----
    click(cdp, "btn-clear-done")
    deadline = time.time() + 30
    while time.time() < deadline and len(cards(cdp)) > 0:
        time.sleep(0.1)
    remaining = cards(cdp)
    check("清除已完成后列表为空", remaining == [], str(len(remaining)))
    check("清空后显示空态提示", cdp.evaluate("!document.getElementById('tasks-empty').hidden") is True)
    cdp.shot(shots, "04-tasks-cleared.png")

    errors = cdp.evaluate("window.__bgErrors || []")
    check("交互全程无 JS 错误", not errors, str(errors)[:200])

    print(f"\n截图目录: {shots}")
    if failures:
        print(f"BG BROWSER CHECK FAILED: {len(failures)} -> {failures}")
        return 1
    print("BG BROWSER CHECK OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
