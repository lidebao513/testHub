# -*- coding: utf-8 -*-
"""DEF-20261116-01 UI 回归：GitHub SSH 仓库「扫描→勾选范围」可用。

不生成用例、不落业务数据，只验证扫描端点修复后前端能正常拿到文件列表。
"""
import os

from playwright.sync_api import sync_playwright

BASE = "http://localhost:3900"
CONSOLE = "http://localhost:8900/testgen/?embed=1&zone=generate"
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
USER, PASS = "uiverify_ui", "UiVerify@2026"
REPO = "git@github.com:lidebao513/testHub.git"
SHOT = os.path.join(os.path.dirname(__file__), "shots", "git_scan_fix_2026-11-16")
os.makedirs(SHOT, exist_ok=True)

results = []


def record(name, ok, detail=""):
    results.append((name, ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" -> {detail}" if detail else ""), flush=True)


with sync_playwright() as p:
    browser = p.chromium.launch(executable_path=EDGE, headless=True, args=["--no-proxy-server"])
    page = browser.new_context(viewport={"width": 1440, "height": 900}).new_page()

    page.goto(BASE + "/login", wait_until="load")
    page.locator("input[type=text]").first.fill(USER)
    page.locator("input[type=password]").first.fill(PASS)
    page.click(".login-button")
    page.wait_for_url(lambda u: "/login" not in u, timeout=20000)
    record("登录成功", True)

    page.goto(CONSOLE, wait_until="load")
    page.locator("#repo").wait_for(timeout=15000)
    page.locator("#nav-console").click()
    page.wait_for_timeout(400)
    page.locator("#code_src_type").select_option("github")
    page.locator("#repo").fill(REPO)
    page.wait_for_timeout(300)
    record("填入 GitHub SSH 地址", page.locator("#repo").input_value() == REPO)

    # 清掉历史结果，避免上一轮残留造成假阳性
    page.evaluate("""() => {
      const m = document.querySelector('#scan_meta');
      if (m) { m.style.display='none'; m.textContent=''; }
      const l = document.querySelector('#scan_list');
      if (l) l.textContent='';
    }""")
    page.locator("#btn_scan").click()
    page.wait_for_function("""() => {
      const cbs = document.querySelectorAll('.scan-cb');
      const l = document.querySelector('#scan_list');
      const t = l ? (l.textContent||'') : '';
      return cbs.length > 0 || /失败|异常|凭证|错误/.test(t);
    }""", timeout=120000)

    n = page.locator(".scan-cb").count()
    meta = page.locator("#scan_meta").inner_text().strip() if page.locator("#scan_meta").count() else ""
    list_txt = page.locator("#scan_list").inner_text()[:160]
    record("SSH 扫描返回可勾选文件", n > 0, f"文件数={n}; meta={meta[:80]}; 列表={list_txt[:80]}")

    if n > 0:
        page.locator(".scan-cb").first.check()
        record("可勾选扫描范围", page.locator(".scan-cb").first.is_checked())

    page.screenshot(path=os.path.join(SHOT, "ssh_scan_files.png"), full_page=False)
    print(f"TOTAL {sum(1 for _, ok in results if ok)}/{len(results)} PASS", flush=True)
    browser.close()
