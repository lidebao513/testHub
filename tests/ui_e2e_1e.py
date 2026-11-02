"""1e 真实浏览器 UI 级 e2e（Playwright + Chromium）。

验证 1e 批次在真实浏览器链路下的行为：
M8 —— 登录成功后后端种下 HttpOnly refresh cookie；刷新页面后靠 refresh cookie
      静默续期保持登录（修复 initAuth 续期缺陷）；登出后 refresh cookie 被清除。
M6/M7 —— 在 AI 评测师聊天框输入含 onerror 的 XSS，验证 DOMPurify 净化后不执行、
        渲染后的 DOM 中不含 onerror 属性。

说明：/home 站点的功能卡片与用户菜单均为可点击 div（非 <button>），故统一用文本定位。
运行前置：前后端服务已起（Vite :3900 代理到 Django :8900），且已创建 e2etest 账号。
    ./venv/Scripts/python.exe tests/ui_e2e_1e.py
"""
import sys
from playwright.sync_api import sync_playwright

BASE = "http://localhost:3900"
USER, PWD = "e2etest", "E2e@123456"
results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))


def logged_in(page):
    return ("/login" not in page.url) and (page.locator("text=e2etest").count() > 0)


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = browser.new_context()
        page = ctx.new_page()
        page.on("dialog", lambda d: d.dismiss())

        # ---------- M8：登录 ----------
        page.goto(f"{BASE}/login", wait_until="domcontentloaded")
        page.wait_for_selector('input[placeholder="请输入用户名"]', timeout=20000)
        page.fill('input[placeholder="请输入用户名"]', USER)
        page.fill('input[placeholder="请输入密码"]', PWD)
        page.get_by_role("button", name="登录", exact=True).click()
        page.wait_for_timeout(4000)
        check("M8 登录成功(进入应用并显示用户 e2etest)", logged_in(page), f"url={page.url}")

        # ---------- M8：refresh cookie 属性 ----------
        cookies = ctx.cookies()
        rt = [c for c in cookies if c["name"] == "refresh_token"]
        check("M8 refresh_token 已种下(HttpOnly Cookie)", len(rt) == 1, f"count={len(rt)}")
        if rt:
            check("M8 refresh_token httpOnly=true", rt[0]["httpOnly"])
            check("M8 refresh_token secure 与 DEBUG 一致(本地应 False)", rt[0]["secure"] is False,
                  f"secure={rt[0]['secure']}")

        # ---------- M8：刷新续期（修复后的核心验证点） ----------
        page.reload(wait_until="domcontentloaded")
        page.wait_for_timeout(4000)
        back_to_login = page.locator("text=欢迎回来").count() > 0
        check("M8 刷新后保持登录(refresh cookie 续期成功)", logged_in(page) and not back_to_login,
              f"url={page.url} back_to_login={back_to_login}")

        # ---------- M6/M7：XSS 净化 UI 验证 ----------
        try:
            page.goto(f"{BASE}/ai-generation/assistant", wait_until="domcontentloaded")
            page.wait_for_timeout(3000)
            box = page.locator("textarea").first
            payload = '<img src=x onerror="window.__xss_hit=1">'
            box.fill(payload)
            box.press("Enter")
            page.wait_for_timeout(2000)
            xss_exec = page.evaluate("window.__xss_hit === 1")
            check("M6/M7 XSS onerror 未执行", not xss_exec, f"exec={xss_exec}")
            has_onerror = page.evaluate("!!document.querySelector('img[onerror]')")
            check("M6/M7 渲染 DOM 无 onerror(DOMPurify 净化)", not has_onerror)
        except Exception as e:
            check("M6/M7 XSS 净化(UI)", False, f"exception: {e}")

        # ---------- M8：登出清除 refresh cookie ----------
        try:
            page.goto(f"{BASE}/home", wait_until="domcontentloaded")
            page.wait_for_timeout(2500)
            page.get_by_text("e2etest", exact=True).first.hover()
            page.wait_for_timeout(1200)
            page.get_by_role("menuitem", name="退出登录").click()
            page.wait_for_timeout(1000)
            # Home.vue 的登出带 ElMessageBox 二次确认，需点“确定”
            page.locator('.el-message-box__btns .el-button--primary').click()
            page.wait_for_timeout(2500)
            cookies2 = ctx.cookies()
            rt2 = [c for c in cookies2 if c["name"] == "refresh_token"]
            check("M8 登出后 refresh_token 被清除", len(rt2) == 0, f"count={len(rt2)}")
        except Exception as e:
            check("M8 登出清除 cookie", False, f"exception: {e}")

        browser.close()

    ok = all(c for _, c, _ in results)
    print("UI_E2E(1e) RESULT:", "ALL PASS" if ok else "HAS FAILURE")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
