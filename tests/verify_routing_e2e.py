"""#178 端到端验证：从页面发起一条 AI 任务，看成本/路由/预探索是否真的落了库。

为什么要从页面走：直接调 ORM 只能证明"字段能写"，不能证明"页面 → 接口 → 后台线程 →
asyncio → 归因 → 回写"整条链路是通的。这条脚本走的就是用户真实路径。

判定标准（任一条不满足即失败）：
1. 执行记录进入终态（passed/failed/stopped，不能停在 running）；
2. routed_model 非空（说明路由决策落库了）；
3. total_tokens 是正整数（说明 browser-use 的 usage 被抓住了）；
4. pre_explore.count >= 1（说明 #177 预探索在真实执行里生效）。

用法（服务已启动）：
    venv/Scripts/python.exe tests/verify_routing_e2e.py
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

# 脚本在 tests/ 下，sys.path[0] 是 tests/，必须先补仓库根目录才能 import backend
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "backend.settings")

import django  # noqa: E402

django.setup()

from playwright.sync_api import sync_playwright  # noqa: E402

from apps.ui_automation.models import AIExecutionRecord  # noqa: E402

BASE = "http://127.0.0.1:3900"
USER, PWD = "e2etest", "E2e@123456"
SHOTS = ROOT / "logs" / "e2e_ui"

# 故意选一条"简单任务"：既验证路由会降到轻档模型，又把真实耗时/花费压到最低。
TASK = "打开 http://127.0.0.1:3900/login ，确认页面上有用户名输入框和登录按钮，然后结束。"

TERMINAL = {"passed", "failed", "stopped"}


def login(page) -> bool:
    page.goto(f"{BASE}/login", wait_until="domcontentloaded", timeout=30_000)
    page.wait_for_selector("input", timeout=15_000)
    page.locator("input").nth(0).fill(USER)
    pwd = page.locator("input[type='password']").first
    pwd.fill(PWD)
    pwd.press("Enter")
    try:
        page.wait_for_url(lambda url: "/login" not in url, timeout=25_000)
        return True
    except Exception:
        return False


def wait_for_record(before_id: int, timeout: float) -> AIExecutionRecord | None:
    """等待最新记录进入终态。"""
    deadline = time.time() + timeout
    latest = None
    while time.time() < deadline:
        latest = AIExecutionRecord.objects.order_by("-id").first()
        if latest and latest.id != before_id and latest.status in TERMINAL:
            return latest
        time.sleep(5)
    return latest


def main() -> int:
    before = AIExecutionRecord.objects.order_by("-id").first()
    before_id = before.id if before else 0
    print(f"基线记录 id={before_id}")

    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-proxy-server", "--no-sandbox"])
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        if not login(page):
            print("❌ 登录失败")
            return 1
        print("✅ 登录成功")

        page.goto(f"{BASE}/ai-intelligent-mode/testing", wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_timeout(2_000)

        textarea = page.locator("textarea").first
        if textarea.count() == 0:
            print("❌ 未找到任务输入框")
            return 1
        textarea.fill(TASK)
        page.wait_for_timeout(300)

        run_btn = page.locator("button", has_text="开始执行").first
        if run_btn.count() == 0:
            print("❌ 未找到「开始执行」按钮")
            page.screenshot(path=str(SHOTS / "routing-e2e-no-button.png"))
            return 1
        run_btn.click()
        print("▶️ 已点击开始执行，等待后端创建记录…")
        page.wait_for_timeout(8_000)
        page.screenshot(path=str(SHOTS / "routing-e2e-running.png"))
        browser.close()

    print("⏳ 等待执行结束（最长 10 分钟）…")
    record = wait_for_record(before_id, timeout=600)
    if record is None or record.id == before_id:
        print("❌ 没有产生新的执行记录（接口可能被拒）")
        return 1

    print("\n=== 执行记录 ===")
    print(f"id              : {record.id}")
    print(f"status          : {record.status}")
    print(f"routed_model    : {record.routed_model!r}")
    print(f"total_tokens    : {record.total_tokens}")
    print(f"cost            : {record.cost}")
    print(f"pre_explore     : count={record.pre_explore.get('count')} url={record.pre_explore.get('url')}")
    print(f"duration        : {record.duration}")

    logs = record.logs or ""
    for line in logs.splitlines():
        if "[模型路由]" in line or "[预探索]" in line or "预探索元素清单" in line:
            print(f"log: {line}")

    problems = []
    if record.status not in TERMINAL:
        problems.append(f"未进入终态（{record.status}）")
    if not record.routed_model:
        problems.append("routed_model 为空")
    if not record.total_tokens:
        problems.append("total_tokens 为空/0")
    if (record.pre_explore or {}).get("count", 0) < 1:
        problems.append("pre_explore.count < 1")

    if problems:
        print("\n❌ 断言未通过：")
        for item in problems:
            print(f"   - {item}")
        print("\n--- 日志尾部 ---")
        print(logs[-1500:])
        return 1

    print("\n✅ 四项断言全部通过")
    return 0


if __name__ == "__main__":
    argparse.ArgumentParser(description="AI 执行成本/路由/预探索端到端验证").parse_args()
    sys.exit(main())
