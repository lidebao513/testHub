# -*- coding: utf-8 -*-
"""探查新版 testgen 控制台的真实 DOM 控件。"""
from playwright.sync_api import sync_playwright

BASE = "http://localhost:3900"
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
USER, PASS = "uiverify_ui", "UiVerify@2026"

with sync_playwright() as p:
    b = p.chromium.launch(executable_path=EDGE, headless=True,
                          args=["--no-proxy-server", "--disable-dev-shm-usage"])
    ctx = b.new_context(viewport={"width": 1440, "height": 900})
    page = ctx.new_page()
    page.goto(BASE + "/login", wait_until="load", timeout=40000)
    page.locator("input[type=password]").first.wait_for(timeout=15000)
    page.locator("input[type=text]").first.fill(USER)
    page.locator("input[type=password]").first.fill(PASS)
    page.click(".login-button")
    page.wait_for_url(lambda u: "/login" not in u, timeout=20000)
    page.goto(BASE + "/ai-generation/testgen-console", wait_until="load", timeout=40000)
    page.wait_for_timeout(4000)

    print("=== FRAMES ===")
    for i, f in enumerate(page.frames):
        print(f"frame[{i}] url={f.url}")

    print("\n=== IFRAME ELEMENTS ===")
    for el in page.locator("iframe").all():
        print("iframe src=", el.get_attribute("src"))

    # 在顶层文档和所有子 frame 里找控件
    for fi, f in enumerate(page.frames):
        print(f"\n######## CONTROLS in frame[{fi}] {f.url[:80]} ########")
        try:
            print("-- inputs --")
            for el in f.locator("input").all()[:40]:
                print("  input", "type=", el.get_attribute("type"),
                      "id=", el.get_attribute("id"),
                      "name=", el.get_attribute("name"),
                      "ph=", (el.get_attribute("placeholder") or "")[:40])
            print("-- textarea --")
            for el in f.locator("textarea").all()[:15]:
                print("  textarea id=", el.get_attribute("id"),
                      "ph=", (el.get_attribute("placeholder") or "")[:40])
            print("-- select --")
            for el in f.locator("select").all()[:15]:
                print("  select id=", el.get_attribute("id"), "name=", el.get_attribute("name"))
            print("-- buttons --")
            for el in f.locator("button").all()[:40]:
                txt = (el.inner_text() or "").strip().replace("\n", " ")
                print("  button", "id=", el.get_attribute("id"), "txt=", txt[:40])
        except Exception as e:
            print("  (读取控件失败)", e)

    b.close()
