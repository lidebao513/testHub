"""E2E 冒烟（固化进 CI，#274）。

默认整模块跳过（ENABLE_E2E != 1），不依赖浏览器、不影响单元/集成测试。
当团队在 CI 提供真实运行栈预览地址（E2E_BASE_URL）并置 ENABLE_E2E=1 时，
本用例会被收集并真正用 Playwright 打开前端首页，守住「改了配置页面还能开」的运行时装配坑。

深度 E2E（遍历 20+ 页面、检查白屏/5xx/控制台报错）见 tests/e2e_ui_smoke.py（手动脚本）。
"""

import os

import pytest

E2E_BASE_URL = os.environ.get("E2E_BASE_URL", "http://127.0.0.1:3900")
ENABLE_E2E = os.environ.get("ENABLE_E2E") == "1"

pytestmark = pytest.mark.skipif(
    not ENABLE_E2E,
    reason="ENABLE_E2E != 1：跳过 E2E 冒烟（需真实运行栈 + Playwright 浏览器）",
)


@pytest.mark.django_db(transaction=False)
def test_frontend_root_loads():
    """前端首页可加载且非空白（真浏览器验证运行时装配）。"""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(E2E_BASE_URL, timeout=30000, wait_until="domcontentloaded")
            title = page.title()
            assert title is not None and title.strip() != "", "前端首页 title 为空，疑似白屏"
        finally:
            browser.close()
