"""UI 层针对性验证：AI 智能模式页（#177 的入口页）能否正常发起任务。

做什么：
1. 真实浏览器登录 → 进入 /ai-intelligent-mode/testing；
2. 断言任务文本框、运行按钮存在且可交互（页面装配没坏）；
3. 填入一条任务并点击运行，把后端返回结果打印出来。

注意：本条不判断"AI 是否跑成功"——若 DB 里没有 active 的 browser_use_text 模型配置，
后端会 fail-closed 报"未配置"，这属于预期行为，脚本只如实回报。

用法（服务已启动）：
    venv/Scripts/python.exe tests/verify_ai_page_ui.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:3900"
USER, PWD = "e2etest", "E2e@123456"
ROOT = Path(__file__).resolve().parent.parent
SHOTS = ROOT / "logs" / "e2e_ui"

TASK = "打开 http://127.0.0.1:3900/login ，确认页面出现用户名输入框和登录按钮，然后结束。"


def login(page) -> bool:
    page.goto(f"{BASE}/login", wait_until="domcontentloaded", timeout=30_000)
    page.wait_for_selector("input", timeout=15_000)
    # 与 e2e_ui_smoke 一致：页面上「密码登录/短信登录」也是 button，点文案会点错，
    # 表单绑定了 @keyup.enter，直接回车最稳。
    page.locator("input").nth(0).fill(USER)
    pwd = page.locator("input[type='password']").first
    pwd.fill(PWD)
    pwd.press("Enter")
    try:
        page.wait_for_url(lambda url: "/login" not in url, timeout=25_000)
        return True
    except Exception:
        return False


def main() -> int:
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
        if not textarea.is_visible():
            print("❌ 未找到任务描述输入框")
            page.screenshot(path=str(SHOTS / "ai-page-no-textarea.png"))
            return 1
        print("✅ 任务输入框可见")

        run_btn = page.locator("button", has_text="开始执行").first
        if run_btn.count() == 0:
            run_btn = page.locator("button:has(.el-icon)").first
        print(f"✅ 运行按钮存在（count={run_btn.count()}）")

        textarea.fill(TASK)
        page.wait_for_timeout(300)
        page.screenshot(path=str(SHOTS / "ai-page-filled.png"))

        run_btn.click()
        page.wait_for_timeout(6_000)

        body = page.inner_text("body")
        page.screenshot(path=str(SHOTS / "ai-page-after-run.png"))

        for keyword in ("未配置", "失败", "成功", "已开始", "执行"):
            if keyword in body:
                print(f"   页面出现关键字: {keyword}")
                break

        # 只要求"页面没有崩"（有内容、不白屏），具体成败交给后端日志
        if len(body.strip()) < 50:
            print("❌ 页面疑似白屏")
            return 1
        print("✅ 点击运行后页面仍正常渲染（未白屏）")
        browser.close()
        return 0


if __name__ == "__main__":
    argparse.ArgumentParser(description="AI 智能模式页 UI 验证").parse_args()
    raise SystemExit(main())
