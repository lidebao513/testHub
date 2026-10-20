# -*- coding: utf-8 -*-
"""UI 层端到端测试：testgen 控制台「生成用例」短分支名（G8 修复回归）。

真实用户路径：打开 Django 代理页 :8900/testgen/ → 填代码目录 + 基线/分支 ref →
选增量 → 点「生成用例」→ 轮询任务徽章直到终态 → 断言成功并截图。
"""
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

CONSOLE_URL = "http://127.0.0.1:8900/testgen/"
REPO_PATH = r"C:\Users\EDY\WorkBuddy\testhub_platform\services\testgen\data\workspaces\research-agent"
BASE_REF = "oa-20260909"  # 复现用户报错时的输入：基线=oa-20260909 / 分支=master
TARGET_REF = "master"
SHOTS = Path(__file__).parent / "ui_shots"
SHOTS.mkdir(exist_ok=True)


def badge_state(page) -> str:
    return page.evaluate("document.getElementById('task_state').textContent")


def main() -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True, channel="msedge", args=["--no-proxy-server"]
        )
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        page.goto(CONSOLE_URL, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_selector("#btn_gen", timeout=15000)
        print("[1] 控制台打开 OK:", CONSOLE_URL)

        page.fill("#repo", REPO_PATH)
        # scan_base_ref/scan_ref 是分支下拉框：优先真实选分支，选不到（未加载）时
        # 直接补 option 再选，保证 onModeChange 的 syncRefs 链路照常走
        for sel_id, val in (("#scan_base_ref", BASE_REF), ("#scan_ref", TARGET_REF)):
            ok = page.evaluate(
                """([id, v]) => {
                  const s = document.getElementById(id.slice(1));
                  if (!s) return false;
                  if (![...s.options].some(o => o.value === v)) {
                    const o = document.createElement('option');
                    o.value = v; o.textContent = v; s.appendChild(o);
                  }
                  s.value = v; s.dispatchEvent(new Event('change'));
                  return true;
                }""",
                [sel_id, val],
            )
            print(f"    set {sel_id}={val}: {ok}")
        page.check('input[name="gen_mode"][value="incremental"]')
        page.dispatch_event('input[name="gen_mode"][value="incremental"]', "change")
        print(f"[2] 已填：repo=...research-agent, 基线={BASE_REF}, 分支={TARGET_REF}, 增量")

        page.click("#btn_gen")
        print("[3] 已点「生成用例」，等待任务终态（最长 10 分钟）…")
        page.wait_for_timeout(1500)
        page.screenshot(path=str(SHOTS / "01_submitted.png"))

        deadline = time.time() + 600
        last = ""
        terminal = ""
        while time.time() < deadline:
            state = badge_state(page)
            log = page.evaluate("document.getElementById('log').textContent.slice(-160)")
            cur = f"{state} | {log}"
            if cur != last:
                print("    ", cur.replace("\n", " "))
                last = cur
            if state in ("success", "failed", "cancelled"):
                terminal = state
                break
            page.wait_for_timeout(3000)
        if not terminal:
            print("❌ 超时未到终态")
            page.screenshot(path=str(SHOTS / "02_timeout.png"))
            return 2

        pid = page.evaluate("document.getElementById('pid').textContent")
        page.screenshot(path=str(SHOTS / f"03_{terminal}.png"))
        # 任务列表应有该任务且行内可见
        rows = page.evaluate(
            "Array.from(document.querySelectorAll('#tasks_list tr')).map(r=>r.textContent.trim().slice(0,80))"
        )
        print("[4] 终态:", terminal, "| project_id:", pid)
        print("[5] 任务列表行:")
        for r in rows[:5]:
            print("   ", r)
        page.screenshot(path=str(SHOTS / "04_tasklist.png"), full_page=True)
        browser.close()
        if terminal == "success" and pid and pid != "—":
            print("✅ UI E2E PASS")
            return 0
        print("❌ UI E2E FAIL")
        return 1


if __name__ == "__main__":
    sys.exit(main())
