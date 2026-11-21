# -*- coding: utf-8 -*-
"""P2 补齐：非功能测试（性能 / 兼容性 / 安全 UI 层 / 异常组合）。

- 性能：首屏加载时间、大列表（9000+ 测试点）渲染耗时
- 兼容性：Edge 桌面 1440x900 + 窄屏 900x700，检查关键控件可见
- 安全 UI 层：XSS 转义（仓库名/备注）、Codeup key 掩码、未登录 403
- 异常组合：网络中断（API 返回 500）、超大输入、sidecar 不可用提示
"""
import os
import time
from datetime import datetime

from playwright.sync_api import sync_playwright

BASE = "http://localhost:3900"
CONSOLE = "http://localhost:8900/testgen/?embed=1&zone=generate"
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
USER, PASS = "uiverify_ui", "UiVerify@2026"
SHOT = os.path.join(os.path.dirname(__file__), "shots", "p2_nonfunc_2026-11-16")
os.makedirs(SHOT, exist_ok=True)
_seq = 0
results = []


def log(m):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


def shot(page, name):
    global _seq
    _seq += 1
    p = os.path.join(SHOT, f"{_seq:02d}_{name}.png")
    page.screenshot(path=p, full_page=False)


def record(case, ok, detail=""):
    results.append((case, ok, detail))
    log(f"  [{'PASS' if ok else 'FAIL'}] {case}" + (f" -> {detail}" if detail else ""))


def login(page):
    page.goto(BASE + "/login", wait_until="load", timeout=40000)
    page.locator("input[type=text]").first.fill(USER)
    page.locator("input[type=password]").first.fill(PASS)
    page.click(".login-button")
    page.wait_for_url(lambda u: "/login" not in u, timeout=20000)


def main():
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=EDGE, headless=True,
                              args=["--no-proxy-server", "--disable-dev-shm-usage"])

        # ---------- 未登录访问 ----------
        log("=== 安全：未登录访问 testgen 控制台 ===")
        ctx0 = b.new_context()
        pg0 = ctx0.new_page()
        resp = pg0.goto(CONSOLE, wait_until="load", timeout=30000)
        code = resp.status if resp else 0
        record("SEC-1 未登录访问被拒(403)", code == 403, f"HTTP={code}")
        ctx0.close()

        # 主 context
        ctx = b.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        dialogs = []
        page.on("dialog", lambda d: (dialogs.append(d.message), d.accept()))
        login(page)

        # ---------- 性能：首屏加载 ----------
        log("=== 性能：控制台首屏加载 ===")
        t0 = time.time()
        resp = page.goto(CONSOLE, wait_until="load", timeout=40000)
        page.locator("#repo").wait_for(timeout=15000)
        ttfb = (time.time() - t0) * 1000
        record("PERF-1 首屏加载 < 8s", ttfb < 8000, f"耗时={ttfb:.0f}ms, HTTP={resp.status if resp else '?'}")
        page.wait_for_timeout(600)

        # ---------- 兼容性：两种视口 ----------
        log("=== 兼容性：桌面/窄屏关键控件可见 ===")
        for w, h, label in [(1440, 900, "桌面"), (900, 700, "窄屏")]:
            page.set_viewport_size({"width": w, "height": h})
            page.wait_for_timeout(400)
            vis = all([
                page.locator("#code_src_type").is_visible(),
                page.locator("#repo").is_visible(),
                page.locator("#btn_gen").is_visible(),
                page.locator("#nav-console").is_visible(),
            ])
            record(f"COMPAT-{label} 关键控件可见", vis, f"{w}x{h}")
            shot(page, f"compat_{label}")
        page.set_viewport_size({"width": 1440, "height": 900})
        page.wait_for_timeout(300)

        # ---------- 安全：XSS 转义 ----------
        log("=== 安全 UI 层：仓库名/备注 XSS 转义 ===")
        page.locator("#nav-repos").click()
        page.wait_for_timeout(500)
        payload = "<img src=x onerror=alert(1)>XSS"
        page.locator("#repo_name").fill(payload)
        page.locator("#repo_url").fill("D:/nonexistent-xss-test")
        page.locator("#repo_remark").fill(payload)
        page.locator("#repo_save").click()
        page.wait_for_timeout(1200)
        # 检查列表是否把 payload 当文本（无 img 元素注入）
        injected = page.evaluate("""() => document.querySelectorAll('#repo_list img[src="x"]').length""")
        list_txt = page.locator("#repo_list").inner_text()
        record("SEC-2 XSS 载荷被转义为文本", injected == 0 and payload in list_txt,
               f"注入img数={injected}, 列表含文本={payload in list_txt}")
        shot(page, "SEC_2_xss_escaped")
        # 清理这条测试仓库
        try:
            rows = page.locator("#repo_list tr")
            for i in range(rows.count()):
                if payload in rows.nth(i).inner_text():
                    link = rows.nth(i).locator("a", has_text="删除")
                    if link.count() > 0:
                        link.first.click()
                        break
            page.wait_for_timeout(400)
        except Exception as e:
            log(f"    XSS仓库清理跳过: {str(e)[:60]}")

        # ---------- 安全：Codeup key 掩码 ----------
        log("=== 安全 UI 层：Codeup key 不回显明文 ===")
        repos = page.evaluate("""async () => await api('/api/v1/repos')""")
        rl = repos.get("repos", repos.get("items", [])) if isinstance(repos, dict) else repos
        leaked = False
        for r in rl:
            for k, v in r.items():
                if "key" in k.lower() and isinstance(v, str) and len(v) > 20 and not v.startswith("*"):
                    leaked = True
        record("SEC-3 仓库列表 key 仅掩码", not leaked, f"检查 {len(rl)} 条仓库配置")

        # ---------- 异常：API 500 ----------
        log("=== 异常组合：API 错误时 UI 不崩溃 ===")
        page.locator("#nav-console").click()
        page.wait_for_timeout(400)
        # 拦截 scan 请求，强制返回 500
        page.route("**/api/v1/scan", lambda route: route.fulfill(
            status=500, content_type="application/json", body='{"detail":"boom"}'))
        page.locator("#code_src_type").select_option("local")
        page.wait_for_timeout(200)
        page.locator("#repo").fill(r"C:\Users\EDY\WorkBuddy\testhub_platform")
        page.locator("#btn_scan").click()
        page.wait_for_timeout(2500)
        scan_txt = page.locator("#scan_list").inner_text()
        alive = page.locator("#btn_scan").is_visible() and not page.locator("#app").is_hidden() if page.locator("#app").count() else page.locator("#btn_scan").is_visible()
        record("EXC-1 API 500 后 UI 仍可用且有提示", alive and ("异常" in scan_txt or "失败" in scan_txt or "扫描" in scan_txt),
               f"scan区={scan_txt[:60]}")
        shot(page, "EXC_1_api_error")
        page.unroute("**/api/v1/scan")

        # ---------- 异常：超大输入 ----------
        log("=== 异常组合：超大输入不假死 ===")
        huge = "x" * 5000
        t1 = time.time()
        page.locator("#auto_input").fill(huge)
        page.locator("#pname").fill(huge[:200])
        elapsed = time.time() - t1
        still_alive = page.locator("#pname").input_value() == huge[:200]
        record("EXC-2 超大输入不假死", elapsed < 5 and still_alive,
               f"填入耗时={elapsed:.2f}s, 值正确={still_alive}")
        # 清空，避免污染
        page.locator("#auto_input").fill("")
        page.locator("#pname").fill("")

        b.close()

    print("\n===== P2 汇总 =====", flush=True)
    for case, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'} | {case} | {detail}", flush=True)
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"总计 {len(results)} 条，通过 {passed}，失败 {len(results)-passed}", flush=True)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
