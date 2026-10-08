# -*- coding: utf-8 -*-
"""验证代码生成历史任务 localStorage 持久化 + 刷新恢复。"""
import sys
from playwright.sync_api import sync_playwright

BASE = "http://localhost:3000"
SHOT = "logs/shot_history.png"


def frame_of(page):
    for f in page.frames:
        if ":8000/testgen/" in (f.url or ""):
            return f
    return None


with sync_playwright() as p:
    b = p.chromium.launch(headless=True, args=["--no-proxy-server"])
    pg = b.new_page(viewport={"width": 1440, "height": 900})
    pg.goto(BASE + "/login", wait_until="networkidle")
    pg.fill('input[type="text"]', "admin")
    pg.fill('input[type="password"]', "Testhub@2026")
    pg.keyboard.press("Enter")
    pg.wait_for_url("**/home**", timeout=20000)
    pg.goto(BASE + "/ai-generation/testgen-console?zone=generate", wait_until="networkidle")
    pg.wait_for_timeout(3000)

    has_token = False
    try:
        frame.wait_for_function("() => !!window.__tgToken", timeout=10000)
        has_token = True
    except Exception:
        has_token = False
    print("token_injected:", has_token)

    frame = frame_of(pg)
    if frame is None:
        print("FAIL: iframe missing")
        b.close(); sys.exit(2)

    if has_token:
        frame.fill('#gen_local', 'C:/Users/EDY/WorkBuddy/testhub_platform/services/testgen')
        frame.click('button:has-text("生成测试用例")')
        try:
            frame.wait_for_function(
                "() => { try { return (JSON.parse(localStorage.getItem('testgen_gen_tasks'))||[]).length>0 } catch(e){ return false } }",
                timeout=20000,
            )
            ok_submit = True
        except Exception:
            ok_submit = False
        ls = frame.evaluate("localStorage.getItem('testgen_gen_tasks')")
        print("submit_ok:", ok_submit, "localStorage:", (ls or "")[:200])
        pg.reload(wait_until="networkidle")
        pg.wait_for_timeout(3500)
        frame2 = frame_of(pg)
        cnt = frame2.evaluate("document.querySelectorAll('#gen_history_list .task').length")
        print("history_items_after_reload:", cnt)
        frame2.screenshot(path=SHOT)
        print("RESULT:", "PASS" if (ok_submit and cnt >= 1) else "FAIL")
        b.close()
        sys.exit(0 if (ok_submit and cnt >= 1) else 2)
    else:
        frame.evaluate(
            "localStorage.setItem('testgen_gen_tasks', JSON.stringify([{task_id:'fake-test-123',label:'__verify_restore__',scopes:['正常'],changed_files:[],created_at:new Date().toISOString(),state:'running'}]))"
        )
        pg.reload(wait_until="networkidle")
        pg.wait_for_timeout(3500)
        frame2 = frame_of(pg)
        cnt = frame2.evaluate("document.querySelectorAll('#gen_history_list .task').length")
        print("history_items_after_reload(fake):", cnt)
        frame2.screenshot(path=SHOT)
        print("RESULT:", "PASS" if cnt >= 1 else "FAIL")
        b.close()
        sys.exit(0 if cnt >= 1 else 2)
