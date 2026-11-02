"""B2 真实链路验证：控制台的 API 请求落到专属前缀 `/testgen-api/v1`。

验证链路：登录 → 打开 `/testgen/?embed=1`（与 iframe 内 URL 完全一致）→ 代理注入前缀
→ 前端 `apiUrl()` 改写请求 → sidecar 正常返回。

断言要点：
1. 页面内 `API_PREFIX` 已被注入为专属前缀；
2. 实际网络请求走 `/testgen-api/v1/*`，且**不再**出现 `/api/v1/*`；
3. 项目列表渲染成功（说明 sidecar 真的返回了数据，不是 404 空壳）；
4. 「报告」是 `target=_blank` 直跳链接，href 也必须带新前缀（最易漏的一处）。
"""

from __future__ import annotations

import sys

import requests
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8900"
USER = "e2etest"
PWD = "E2e@123456"
PREFIX = "/testgen-api/v1"


def _login_cookies():
    s = requests.Session()
    s.trust_env = False
    resp = s.post(
        f"{BASE}/api/auth/login/", json={"username": USER, "password": PWD}, timeout=20
    )
    resp.raise_for_status()
    return [
        {"name": c.name, "value": c.value, "domain": "127.0.0.1", "path": "/"}
        for c in s.cookies
    ]


def main() -> int:
    cookies = _login_cookies()
    requests_seen: list[str] = []
    errors: list[tuple[int, str]] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context()
        ctx.add_cookies(cookies)
        page = ctx.new_page()
        page.on("request", lambda r: requests_seen.append(r.url))
        page.on(
            "response",
            lambda r: errors.append((r.status, r.url)) if r.status >= 400 else None,
        )

        page.goto(f"{BASE}/testgen/?embed=1", timeout=30000)
        page.wait_for_timeout(600)

        injected = page.evaluate("() => API_PREFIX")
        page.click('button:has-text("刷新项目")')
        page.wait_for_timeout(2500)

        rows = page.locator("#projects table tr").count()
        report_href = ""
        if rows > 1:
            report_href = page.locator("#projects table a:has-text('报告')").first.get_attribute(
                "href"
            ) or ""

        browser.close()

    legacy = [
        u
        for u in requests_seen
        if "/api/v1/" in u and PREFIX not in u
    ]
    dedicated = [u for u in requests_seen if PREFIX in u]

    checks = [
        ("前缀已注入为专属前缀", injected == PREFIX, f"实际 {injected}"),
        ("有请求走专属前缀", len(dedicated) > 0, f"{len(dedicated)} 个"),
        ("无请求走旧前缀 /api/v1", not legacy, f"泄漏 {legacy[:3]}"),
        ("项目列表已渲染", rows > 1, f"{rows} 行"),
        (
            "报告链接带专属前缀",
            report_href.startswith(PREFIX),
            f"href={report_href[:60]}",
        ),
        ("无 4xx/5xx 响应", not errors, f"{errors[:3]}"),
    ]

    ok = True
    for name, passed, detail in checks:
        print(f"[{'PASS' if passed else 'FAIL'}] {name}  ({detail})")
        ok = ok and passed

    print(f"\n请求样本：{ [u.split('8900')[-1] for u in dedicated[:3]] }")
    print("RESULT:", "ALL PASS" if ok else "HAS FAILURE")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
