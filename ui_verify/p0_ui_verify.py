"""P0 批次 UI 层验证（稳定版）。
- 冒烟（浏览器）：用本地 Edge 加载 SPA、真实表单登录、进入需求分析 / 接口测试两页并截图、收集 console 错误。
- 集成（requests 直连 Django:8900，绕开透明代理）：在已认证会话内调用 P0 新增端点，
  验证路由可达、返回结构正确（非 500 崩溃）。import-openapi 为纯解析链路，应端到端成功。
  （端点集成检查直连 Django，等价于 SPA 经 Vite 代理访问的同一组 URL；SPA 渲染已由浏览器冒烟覆盖。）
"""
import json
import os
import sys
import requests

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
BASE = "http://localhost:3900"
API = "http://127.0.0.1:8900"
USER, PASS = "uiverify", "UiVerify@2026"
SHOT = os.path.join(os.path.dirname(__file__), "shots")
os.makedirs(SHOT, exist_ok=True)


def browser_smoke():
    from playwright.sync_api import sync_playwright
    console_msgs = []
    page_errors = []
    smoke = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=EDGE, headless=True,
            args=["--no-proxy-server", "--disable-dev-shm-usage"],
        )
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        page.on("console", lambda m: console_msgs.append(f"{m.type}: {m.text}")
                if m.type in ("error", "warning") else None)
        page.on("pageerror", lambda e: page_errors.append(str(e)))

        page.goto(BASE + "/login", wait_until="load", timeout=30000)
        page.wait_for_selector("input[type=password]", timeout=15000)
        page.screenshot(path=os.path.join(SHOT, "01_login.png"))
        try:
            page.fill("input[type=text]", USER, timeout=8000)
            page.fill("input[type=password]", PASS, timeout=8000)
            page.click(".login-button", timeout=8000)
            page.wait_for_url(lambda u: "/login" not in u, timeout=15000)
            smoke["login"] = "ok"
        except Exception as e:
            smoke["login"] = f"form-login-failed: {e}"
        page.screenshot(path=os.path.join(SHOT, "02_after_login.png"))

        try:
            page.goto(BASE + "/ai-generation/requirement-analysis", wait_until="load", timeout=30000)
            page.wait_for_timeout(2500)
            page.screenshot(path=os.path.join(SHOT, "03_requirement_analysis.png"))
            smoke["requirement_page"] = "rendered"
        except Exception as e:
            smoke["requirement_page"] = f"err: {e}"
        try:
            page.goto(BASE + "/api-testing/interfaces", wait_until="load", timeout=30000)
            page.wait_for_timeout(2500)
            page.screenshot(path=os.path.join(SHOT, "04_api_testing.png"))
            smoke["api_testing_page"] = "rendered"
        except Exception as e:
            smoke["api_testing_page"] = f"err: {e}"
        browser.close()
    return smoke, console_msgs, page_errors


def api_session():
    s = requests.Session()
    s.proxies = {"http": None, "https": None}  # 绕开本机透明代理
    s.trust_env = False
    r = s.post(API + "/api/auth/login/", json={"username": USER, "password": PASS}, timeout=30)
    token = (r.json() or {}).get("access")
    if token:
        s.headers.update({"Authorization": "Bearer " + token})
    return s, r.status_code, bool(token)


def jstatus(resp):
    try:
        return resp.status_code, (resp.json() if resp.content else None)
    except Exception:
        return resp.status_code, None


def test_endpoints(s):
    out = {}
    # P0-1 需求三件套
    sc, docs = jstatus(s.get(API + "/api/requirement-analysis/documents/", timeout=30))
    out["documents_status"] = sc
    results = (docs or {}).get("results") or []
    env_doc = os.environ.get("UI_DOC_ID")
    doc_id = int(env_doc) if env_doc else (results[0]["id"] if results else None)
    out["doc_id"] = doc_id
    if doc_id:
        sc, prod = jstatus(s.get(API + f"/api/requirement-analysis/documents/{doc_id}/products/", timeout=30))
        out["products_get"] = {"status": sc, "shape_ok": isinstance(prod, dict)
                               and "functional_inventory" in (prod or {})}
        sc, gen = jstatus(s.post(API + f"/api/requirement-analysis/documents/{doc_id}/generate-products/",
                                json={}, timeout=120))
        out["generate_products"] = {"status": sc, "snippet": json.dumps(gen or {}, ensure_ascii=False)[:400]}
        sc, prod2 = jstatus(s.get(API + f"/api/requirement-analysis/documents/{doc_id}/products/", timeout=30))
        d2 = prod2 or {}
        out["products_after"] = {
            "fi": len(d2.get("functional_inventory") or []),
            "ui": len(d2.get("ui_descriptions") or []),
            "cl": len(d2.get("clarifications") or []),
        }
        open_cl = next((c for c in (d2.get("clarifications") or []) if c.get("status") == "open"), None)
        if open_cl:
            sc, cl = jstatus(s.post(API + f"/api/requirement-analysis/documents/{doc_id}/clarify/",
                                   json={"clarification_id": open_cl["id"], "answer": "UI验证自动回答"},
                                   timeout=120))
            out["clarify"] = {"status": sc, "snippet": json.dumps(cl or {}, ensure_ascii=False)[:300]}

    # P0-2 openapi 导入 + 接口 Agent + AI 归因
    sc, proj = jstatus(s.post(API + "/api/api-testing/projects/",
                             json={"name": "UI验证临时项目", "project_type": "HTTP", "status": "IN_PROGRESS"},
                             timeout=30))
    out["project_create"] = {"status": sc, "id": (proj or {}).get("id")}
    pid = (proj or {}).get("id")
    spec = {"openapi": "3.0.0", "info": {"title": "UI验证Spec", "version": "1.0.0"},
           "paths": {"/ping": {"get": {"summary": "健康检查", "responses": {"200": {"description": "ok"}}}}}}
    if pid:
        sc, imp = jstatus(s.post(API + "/api/api-testing/spec-imports/import-openapi/",
                                json={"raw_spec": json.dumps(spec), "spec_format": "json",
                                      "project": pid, "name": "UI验证导入", "source": "paste"},
                                timeout=60))
        out["import_openapi"] = {"status": sc,
                                 "status_field": (imp or {}).get("status"),
                                 "count": (imp or {}).get("imported_count"),
                                 "snippet": json.dumps(imp or {}, ensure_ascii=False)[:300]}
    sc, sil = jstatus(s.get(API + "/api/api-testing/spec-imports/", timeout=30))
    out["spec_imports_list"] = {"status": sc, "count": (sil or {}).get("count")}
    sc, gcl = jstatus(s.get(API + "/api/api-testing/generated-cases/", timeout=30))
    out["generated_cases_list"] = {"status": sc, "count": (gcl or {}).get("count")}
    sc, reqs = jstatus(s.get(API + "/api/api-testing/requests/", timeout=30))
    out["requests_status"] = sc
    req_id = (((reqs or {}).get("results") or [{}])[0] or {}).get("id")
    out["request_id"] = req_id
    if req_id:
        sc, gc = jstatus(s.post(API + f"/api/api-testing/requests/{req_id}/generate-cases/", json={}, timeout=120))
        out["generate_cases"] = {"status": sc, "snippet": json.dumps(gc or {}, ensure_ascii=False)[:300]}
        sc, scn = jstatus(s.post(API + "/api/api-testing/test-suites/scenario/generate/",
                                json={"name": "UI验证场景", "request_ids": [req_id]}, timeout=120))
        out["scenario_generate"] = {"status": sc, "snippet": json.dumps(scn or {}, ensure_ascii=False)[:300]}
    sc, execs = jstatus(s.get(API + "/api/api-testing/test-executions/", timeout=30))
    els = (execs or {}).get("results") or []
    failed = next((e for e in els if e.get("status") == "FAILED"), None)
    if failed:
        sc, at = jstatus(s.post(API + f"/api/api-testing/test-executions/{failed['id']}/attribution/",
                                json={}, timeout=120))
        out["attribution"] = {"status": sc, "snippet": json.dumps(at or {}, ensure_ascii=False)[:300]}
    else:
        sc, at = jstatus(s.post(API + "/api/api-testing/test-executions/999999/attribution/", json={}, timeout=30))
        out["attribution"] = {"status": sc, "note": "无 FAILED 执行；伪 id 路由校验"}
    return out


def main():
    smoke, console_msgs, page_errors = browser_smoke()
    sess, auth_status, has_token = api_session()
    endpoints = {"auth": {"status": auth_status, "hasToken": has_token}}
    if has_token:
        try:
            endpoints.update(test_endpoints(sess))
        except Exception as e:
            endpoints["test_error"] = str(e)
    result = {"smoke": smoke, "endpoints": endpoints,
              "console_errors": console_msgs, "page_errors": page_errors}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    doc_id = endpoints.get("doc_id")
    with open(os.path.join(os.path.dirname(__file__), "last_doc_id.txt"), "w") as f:
        f.write(str(doc_id or ""))
    return result


if __name__ == "__main__":
    main()
