# -*- coding: utf-8 -*-
"""回归验证：测试连接校验失败时清空上一次的结果区（Git 表单 + LLM 表单）。"""
import os
from datetime import datetime

from playwright.sync_api import sync_playwright

BASE = "http://localhost:3900"
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
USER, PASS = "uiverify_ui", "UiVerify@2026"
REPO_URL = "git@github.com:lidebao513/testHub.git"
SHOT = os.path.join(os.path.dirname(__file__), "shots", "probe_reset_2026-11-16")
os.makedirs(SHOT, exist_ok=True)

results = []


def log(m):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


def record(name, ok, detail=""):
    results.append((name, ok, detail))
    log(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -> {detail}" if detail else ""))


def main():
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=EDGE, headless=True,
                              args=["--no-proxy-server", "--disable-dev-shm-usage"])
        ctx = b.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        dialogs = []
        page.on("dialog", lambda d: (dialogs.append(d.message), d.accept()))

        log("登录")
        page.goto(BASE + "/login", wait_until="load", timeout=40000)
        page.locator("input[type=text]").first.fill(USER)
        page.locator("input[type=password]").first.fill(PASS)
        page.click(".login-button")
        page.wait_for_url(lambda u: "/login" not in u, timeout=20000)

        log("打开 testgen 控制台")
        page.goto("http://localhost:8900/testgen/?embed=1&zone=generate",
                  wait_until="load", timeout=40000)
        page.locator("#repo").wait_for(timeout=15000)
        page.wait_for_timeout(800)

        # ===== Git 表单：真实有效连接 -> 清空地址 -> 再点测试连接 =====
        log("== Git 表单 ==")
        page.locator("#nav-repos").click()
        page.locator("#repo_url").fill(REPO_URL)
        page.locator("#repo_probe").click()
        page.wait_for_function("""() => {
          const t = (document.querySelector('#repo_form_result') || {}).innerText || '';
          return t && !t.includes('测试中');
        }""", timeout=45000)
        first = page.locator("#repo_form_result").inner_text().strip()
        log(f"  首次连接结果: {first[:120]}")
        record("G0 首次有效连接产生结果", bool(first))

        page.locator("#repo_url").fill("")
        dialogs.clear()
        page.locator("#repo_probe").click()
        page.wait_for_timeout(300)
        after = page.locator("#repo_form_result").inner_text().strip()
        record("G1 空地址时结果区被清空", after == "", repr(after))
        record("G2 空地址仍弹出正确提示", dialogs == ["请先填仓库地址"], repr(dialogs))
        page.screenshot(path=os.path.join(SHOT, "git_after_empty.png"))

        # ===== LLM 表单：模拟旧成功状态 -> 清空 Base URL -> 再点测试连接 =====
        log("== LLM 表单 ==")
        page.locator("#nav-llm").click()
        page.evaluate("""() => {
          document.querySelector('#llm_form_result').innerHTML =
            '<div class="test-result ok">✅ 连接成功（模拟上一次状态）</div>';
          document.querySelector('#llm_base_url').value = '';
        }""")
        before_llm = page.locator("#llm_form_result").inner_text().strip()
        record("L0 模拟旧状态存在", "连接成功" in before_llm, before_llm)
        dialogs.clear()
        page.locator("#llm_probe").click()
        page.wait_for_timeout(300)
        after_llm = page.locator("#llm_form_result").inner_text().strip()
        record("L1 空 Base URL 时结果区被清空", after_llm == "", repr(after_llm))
        record("L2 空 Base URL 仍弹出正确提示", dialogs == ["请先填 Base URL"], repr(dialogs))
        page.screenshot(path=os.path.join(SHOT, "llm_after_empty.png"))

        b.close()

    print("\n===== 汇总 =====")
    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  {detail}" if detail else ""))
    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} 通过，失败 {len(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
