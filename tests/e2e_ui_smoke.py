"""UI 层冒烟 E2E：真实浏览器登录 → 遍历主要页面 → 检查白屏 / 5xx / 控制台报错。

为什么要这一层：单元测试证明不了「改了配置之后页面还能开」。模型登记册从 11 条扩到 53 条、
护栏改到 `_env()` 读取之后，最容易出问题的是**运行时装配**（前端拿不到数据、接口 500、
路由跳回登录页），只有真浏览器才能发现。

前置：MySQL80 / Redis / Django(:8900) / Vite(:3900) 已启动
      （`python scripts/dev_stack.py start`，或计划任务 testhub-dev-stack）。

用法：
    python tests/e2e_ui_smoke.py            # 跑全部页面
    python tests/e2e_ui_smoke.py --keep     # 保留浏览器窗口（调试用，需有桌面）

产物：logs/e2e_ui/<序号>-<页面>.png，失败详情打印到 stdout。
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path

from playwright.sync_api import Error as PWError
from playwright.sync_api import Page, sync_playwright

BASE = "http://127.0.0.1:3900"
USER, PWD = "e2etest", "E2e@123456"
ROOT = Path(__file__).resolve().parent.parent
SHOTS = ROOT / "logs" / "e2e_ui"

# (路径, 页面名)。
# 刻意用**父路径**访问：父路由的 `redirect` 一旦写成相对路径（如 `redirect: 'dashboard'`），
# Vue Router 4 会解析成根路径 `/dashboard` → 无匹配 → 整页白屏。测父路径才能守住这个坑。
PAGES: list[tuple[str, str]] = [
    ("/home", "首页"),
    ("/ai-generation", "AI 用例生成（父路径）"),
    ("/ai-generation/assistant", "AI 智能助手"),
    ("/ai-generation/requirement-analysis", "需求分析"),
    ("/ai-generation/testgen-console", "用例生成台"),
    ("/ai-generation/testcases", "测试用例"),
    ("/api-testing", "接口测试（父路径）"),
    ("/api-testing/interfaces", "接口测试-接口"),
    ("/ui-automation", "UI 自动化（父路径）"),
    ("/app-automation", "APP 自动化（父路径）"),
    ("/performance-testing", "性能测试（父路径）"),
    ("/ai-intelligent-mode", "AI 智能模式（父路径）"),
    ("/defects", "缺陷管理（父路径）"),
    ("/llm-judge", "LLM 评审（父路径）"),
    ("/configuration", "配置中心（父路径）"),
    ("/monitor", "监控（父路径）"),
    ("/mcp", "MCP（父路径）"),
    ("/data-factory", "数据工厂"),
    ("/docs-center", "文档中心"),
]

# 登录页 / 静态资源 / 预检等不应计入「接口报错」
IGNORE_URL_PARTS = (
    "/api/auth/login/",
    "/api/auth/refresh/",
    "/node_modules/",
    "/@vite/",
    "/favicon",
    "/src/",
)


@dataclass
class PageResult:
    path: str
    name: str
    ok: bool = True
    problems: list[str] = field(default_factory=list)
    shot: str = ""


def _login(page: Page) -> bool:
    """用真实登录表单登录（不用注入 token，否则「登录页坏了」这种情况会被漏掉）。"""
    page.goto(f"{BASE}/login", wait_until="domcontentloaded", timeout=30_000)
    page.wait_for_selector("input", timeout=15_000)
    inputs = page.locator("input")
    inputs.nth(0).fill(USER)
    page.locator("input[type='password']").first.fill(PWD)
    # 页面上「密码登录 / 短信登录」两个 tab 也是 button，按文案点会点错；
    # 表单本身绑定了 @keyup.enter="handleLogin"，直接回车最稳。
    page.locator("input[type='password']").first.press("Enter")
    try:
        page.wait_for_url(lambda url: "/login" not in url, timeout=25_000)
    except PWError:  # 超时 = 没跳走，按登录失败处理
        return False
    return True


def _visit(page: Page, path: str, name: str) -> PageResult:
    res = PageResult(path=path, name=name)
    console_errors: list[str] = []
    page_errors: list[str] = []
    server_errors: list[str] = []

    def on_console(msg):
        if msg.type == "error":
            console_errors.append(msg.text[:200])

    def on_pageerror(exc):
        page_errors.append(str(exc)[:200])

    def on_response(resp):
        url = resp.url
        if resp.status >= 500 and not any(p in url for p in IGNORE_URL_PARTS):
            server_errors.append(f"{resp.status} {url[:120]}")

    page.on("console", on_console)
    page.on("pageerror", on_pageerror)
    page.on("response", on_response)
    try:
        page.goto(f"{BASE}{path}", wait_until="networkidle", timeout=45_000)
        page.wait_for_timeout(800)
        if "/login" in page.url:
            res.problems.append("被重定向回登录页（鉴权失败）")
        text = (page.locator("body").inner_text() or "").strip()
        if len(text) < 50:
            res.problems.append(f"页面几乎无内容（可见文本 {len(text)} 字，疑似白屏）")
        if server_errors:
            res.problems.append(
                "接口 5xx: " + "; ".join(sorted(set(server_errors))[:3])
            )
        if page_errors:
            res.problems.append("JS 异常: " + "; ".join(sorted(set(page_errors))[:3]))
        if console_errors:
            res.problems.append(
                "控制台 error: " + "; ".join(sorted(set(console_errors))[:3])
            )
        SHOTS.mkdir(parents=True, exist_ok=True)
        shot = (
            SHOTS
            / f"{PAGES.index((path, name)) + 1:02d}-{path.strip('/').replace('/', '_') or 'root'}.png"
        )
        page.screenshot(path=str(shot), full_page=False)
        res.shot = str(shot.relative_to(ROOT))
    except PWError as exc:  # 导航/超时/选择器失败都算该页失败
        res.problems.append(f"打开失败: {str(exc)[:200]}")
    finally:
        page.remove_listener("console", on_console)
        page.remove_listener("pageerror", on_pageerror)
        page.remove_listener("response", on_response)
    res.ok = not res.problems
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="保留浏览器窗口（调试）")
    args = ap.parse_args()

    failures: list[PageResult] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=not args.keep,
            # 本机有透明代理，localhost 请求必须绕开，否则 Vite 热更新与 API 都会被代理拦
            args=["--no-proxy-server"],
        )
        ctx = browser.new_context(
            viewport={"width": 1440, "height": 900}, locale="zh-CN"
        )
        page = ctx.new_page()
        if not _login(page):
            print("❌ 登录失败：无法从 /login 跳到站内页面")
            page.screenshot(path=str(SHOTS / "00-login-failed.png"))
            browser.close()
            return 1
        print(f"✅ 登录成功（{USER}）\n")

        for path, name in PAGES:
            res = _visit(page, path, name)
            mark = "✅" if res.ok else "❌"
            print(f"{mark} {name:<16} {path}")
            for prob in res.problems:
                print(f"     - {prob}")
            if not res.ok:
                failures.append(res)

        browser.close()

    print()
    total, bad = len(PAGES), len(failures)
    print(f"结果：{total - bad}/{total} 页面通过，截图在 {SHOTS.relative_to(ROOT)}/")
    if bad:
        print("失败页面：" + "、".join(r.name for r in failures))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
