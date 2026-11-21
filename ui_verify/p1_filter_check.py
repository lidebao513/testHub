# -*- coding: utf-8 -*-
"""P1-2 精确复核：测试点按范围筛选后，渲染行是否全为目标类。"""
from playwright.sync_api import sync_playwright
EDGE = r"C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"
CONSOLE = "http://localhost:8900/testgen/?embed=1&zone=generate"
BASE = "http://localhost:3900"
with sync_playwright() as p:
    b = p.chromium.launch(executable_path=EDGE, headless=True, args=["--no-proxy-server"])
    pg = b.new_context(viewport={"width":1440,"height":900}).new_page()
    pg.goto(BASE+"/login", wait_until="load")
    pg.locator("input[type=text]").first.fill("uiverify_ui")
    pg.locator("input[type=password]").first.fill("UiVerify@2026")
    pg.click(".login-button"); pg.wait_for_url(lambda u:"/login" not in u, timeout=20000)
    pg.goto(CONSOLE, wait_until="load"); pg.locator("#repo").wait_for(timeout=15000)
    pg.evaluate("loadTestPoints(23)")
    pg.wait_for_function("()=>document.querySelectorAll('#tp_list tr').length>1", timeout=20000)
    # 读取范围列（第3列 td 的 badge）：全量行里应混合正常/异常/安全/边界
    def cats():
        return pg.evaluate("""() => [...document.querySelectorAll('#tp_list tr')].slice(1).map(tr=>{
          const td=tr.querySelectorAll('td'); return td.length>=3 ? td[2].innerText.trim() : '';
        })""")
    all_c = cats()
    from collections import Counter
    print("未筛选范围分布:", Counter(all_c))
    pg.locator("#filter_scope").select_option("安全")
    pg.wait_for_timeout(600)
    safe_c = cats()
    print("筛选安全后分布:", Counter(safe_c))
    ok = len(safe_c)>0 and all(c=="安全" for c in safe_c)
    print("RESULT:", "PASS" if ok else "FAIL", f"- 筛选后{len(safe_c)}行全为安全={ok}")
    b.close()
