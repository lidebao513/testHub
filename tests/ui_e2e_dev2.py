"""二开（二次开发新增）功能 UI 层 e2e（Playwright + Chromium）。

覆盖范围（相对 testhub 基础功能之外、二次开发新增的 AI / testgen 相关模块）：
  1. 需求分析（智能测试用例生成）
  2. testgen 控制台（Django 反向代理 + sidecar 内嵌 iframe）
  3. AI 生成用例记录
  4. AI 测试报告
  5. AI 评测师
  6. 配置中心三件套（AI 模型 / 提示词 / 生成行为）
  7. 文档中心
  8. 智能评分器（llm-judge）
  9. MCP 控制台

每页断言：未跳登录 + 无 JS 异常(pageerror) + 页面内 /api/ 无 4xx-5xx + 关键元素渲染；
另做交互断言：文档中心点击渲染与搜索、testgen iframe 内视图切换、
MCP tab 切换、AI 模型配置弹窗、需求分析创建生成任务。

运行前置（服务已在跑）：
  MySQL80 / Memurai(:6379) / Django(:8900, DEBUG=True) / Vite(:3900) / testgen sidecar(:8910)
  ./venv/Scripts/python.exe tests/ui_e2e_dev2.py
"""
import sys

from playwright.sync_api import sync_playwright

BASE = "http://localhost:3900"
USER, PWD = "e2etest", "E2e@123456"

results = []
errors = []
failed_api = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))


def reset_listeners():
    errors.clear()
    failed_api.clear()


def summarize_failures():
    return f"jsErr={errors[:2]} api={(failed_api[:3])}"


# (展示名, 路径, 关键文本断言)
PAGES = [
    ("需求分析", "/ai-generation/requirement-analysis", "智能测试用例生成"),
    ("AI生成用例记录", "/ai-generation/generated-testcases", "AI生成用例记录"),
    ("AI测试报告", "/ai-generation/reports", "执行状态分布"),
    ("AI评测师", "/ai-generation/assistant", "AI 评测师"),
    ("AI模型配置", "/configuration/ai-model", "模型配置列表"),
    ("提示词配置", "/configuration/prompt-config", "提示词配置列表"),
    ("生成行为配置", "/configuration/generation-config", "配置列表"),
    ("文档中心", "/docs-center", "文档中心"),
    ("智能评分器", "/llm-judge/dashboard", "智能评分器"),
    ("MCP控制台", "/mcp/console", "工具目录"),
]


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = browser.new_context()
        page = ctx.new_page()
        page.on("dialog", lambda d: d.dismiss())
        page.on("pageerror", lambda e: errors.append(str(e)[:160]))
        page.on(
            "response",
            lambda r: failed_api.append((r.status, r.url[:100]))
            if r.status >= 400 and "/api/" in r.url
            else None,
        )

        # ---------- 登录 ----------
        page.goto(f"{BASE}/login", wait_until="domcontentloaded")
        page.wait_for_selector('input[placeholder="请输入用户名"]', timeout=20000)
        page.fill('input[placeholder="请输入用户名"]', USER)
        page.fill('input[placeholder="请输入密码"]', PWD)
        page.get_by_role("button", name="登录", exact=True).click()
        page.wait_for_timeout(3500)
        logged = "/login" not in page.url
        check("登录并进入应用", logged, f"url={page.url}")
        if not logged:
            browser.close()
            print("UI_E2E(二开) RESULT: LOGIN FAILED")
            sys.exit(1)

        # ================= A. 二开页面冒烟 =================
        for label, path, key_text in PAGES:
            reset_listeners()
            try:
                page.goto(f"{BASE}{path}", wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(3200)
                kicked = "/login" in page.url
                has_key = page.get_by_text(key_text, exact=False).count() > 0
                ok = (not kicked) and not errors and not failed_api and has_key
                detail = f"kicked={kicked} key='{key_text}'={has_key} {summarize_failures()}"
                check(f"二开页面可用: {label}", ok, "" if ok else detail)
            except Exception as exc:  # noqa: BLE001
                check(f"二开页面可用: {label}", False, f"exception: {exc}")

        # ================= B. 需求分析：生成链路 =================
        reset_listeners()
        try:
            page.goto(f"{BASE}/ai-generation/requirement-analysis", wait_until="domcontentloaded")
            page.wait_for_timeout(3000)
            page.evaluate("document.querySelector('.modal-overlay')?.remove()")
            page.wait_for_timeout(500)
            has_input = page.locator(".manual-input-section").count() > 0
            has_btn = page.get_by_role("button", name="生成测试用例", exact=False).count() > 0
            check("需求分析: 手动输入区与生成按钮就位", has_input and has_btn,
                  f"input={has_input} btn={has_btn}")

            page.locator(".manual-input-section input").first.fill("UI e2e 二开冒烟需求")
            page.locator(".manual-input-section textarea").first.fill(
                "验证登录接口在正确凭据下返回 200，错误凭据返回 401。"
            )
            # 以接口响应为准（ElMessage 默认 3s 即消失，不能作为可靠断言依据）
            with page.expect_response(
                lambda r: "testcase-generation/generate" in r.url, timeout=25000
            ) as resp_info:
                page.get_by_role("button", name="生成测试用例", exact=False).click()
            resp = resp_info.value
            body = {}
            try:
                body = resp.json()
            except Exception:  # noqa: BLE001
                pass
            task_id = body.get("task_id") if isinstance(body, dict) else None
            page.wait_for_timeout(1500)
            submitted = page.get_by_text("生成任务已提交", exact=False).count() > 0
            check(
                "需求分析: 提交生成任务成功(异步任务已创建)",
                resp.status in (200, 201) and bool(task_id),
                f"status={resp.status} task_id={task_id} toast={submitted}",
            )
        except Exception as exc:  # noqa: BLE001
            check("需求分析: 提交生成任务", False, f"exception: {exc}")

        # ================= C. testgen 控制台 iframe（sidecar 链路） =================
        reset_listeners()
        try:
            page.goto(f"{BASE}/ai-generation/testgen-console", wait_until="domcontentloaded")
            page.wait_for_timeout(6000)
            iframe_src = page.evaluate(
                "() => document.querySelector('iframe.testgen-iframe')?.src || ''"
            )
            check("testgen控制台: iframe 指向 Django 反代 /testgen/",
                  "/testgen/" in iframe_src and "embed=1" in iframe_src, f"src={iframe_src[:90]}")

            frames = [f for f in page.frames if "/testgen/" in (f.url or "")]
            if not frames:
                check("testgen控制台: iframe 内容加载(sidecar 链路)", False, "no frame matched")
            else:
                fr = frames[0]
                title = fr.evaluate("() => document.title")
                btns = fr.evaluate("() => document.querySelectorAll('button').length")
                repos = fr.evaluate(
                    "() => Array.from(document.querySelectorAll('button'))"
                    ".map(b => b.innerText.trim()).filter(Boolean).slice(0,8)"
                )
                ok = ("testgen" in title.lower()) and btns >= 10
                check("testgen控制台: iframe 内容加载(sidecar 链路)", ok,
                      f"title={title} buttons={btns} sample={repos}")

                # iframe 内切换到「代码仓库」视图（验证内嵌页 JS 可交互）
                fr.click("#nav-repos")
                page.wait_for_timeout(2500)
                view_text = fr.evaluate("() => (document.body.innerText || '').slice(0, 400)")
                switched = ("仓库" in view_text) and ("填写被测对象" not in view_text)
                check("testgen控制台: iframe 内视图切换(代码仓库)", switched,
                      f"text={view_text[:80]!r}")
        except Exception as exc:  # noqa: BLE001
            check("testgen控制台: iframe 链路", False, f"exception: {exc}")

        # ================= D. AI 生成用例记录 =================
        reset_listeners()
        try:
            page.goto(f"{BASE}/ai-generation/generated-testcases", wait_until="domcontentloaded")
            page.wait_for_timeout(3200)
            body = page.evaluate("() => document.body.innerText")
            has_stat = "任务总数" in body
            has_head = "AI生成用例记录" in body
            check("AI生成用例记录: 统计与列表渲染", has_stat and has_head,
                  f"stat={has_stat} head={has_head}")
        except Exception as exc:  # noqa: BLE001
            check("AI生成用例记录: 列表渲染", False, f"exception: {exc}")

        # ================= E. AI 测试报告：图表 =================
        reset_listeners()
        try:
            page.goto(f"{BASE}/ai-generation/reports", wait_until="domcontentloaded")
            page.wait_for_timeout(4500)
            canvas = page.evaluate("() => document.querySelectorAll('canvas').length")
            inst = page.evaluate("() => document.querySelectorAll('[_echarts_instance_]').length")
            check("AI测试报告: ECharts 图表渲染", canvas >= 3 and inst >= 3,
                  f"canvas={canvas} echarts={inst}")
        except Exception as exc:  # noqa: BLE001
            check("AI测试报告: 图表渲染", False, f"exception: {exc}")

        # ================= F. 配置中心三件套 =================
        try:
            page.goto(f"{BASE}/configuration/ai-model", wait_until="domcontentloaded")
            page.wait_for_timeout(3000)
            cards = page.locator(".config-card").count()
            body = page.evaluate("() => document.body.innerText")
            check("配置中心: AI 模型配置列表渲染", cards >= 1 or "Qwen" in body,
                  f"cards={cards} hasModel={'Qwen' in body}")

            page.locator(".add-config-btn").first.click()
            page.wait_for_timeout(1500)
            modal = page.locator(".config-modal").first
            visible = modal.is_visible() if modal.count() else False
            has_form = page.locator(".config-modal .form-input").count() > 0
            check("配置中心: AI 模型新增弹窗可打开", visible and has_form,
                  f"visible={visible} form={has_form}")
            if visible:
                page.locator(".config-modal .close-btn").first.click()
                page.wait_for_timeout(600)

            page.goto(f"{BASE}/configuration/prompt-config", wait_until="domcontentloaded")
            page.wait_for_timeout(2800)
            pcards = page.locator(".config-card").count()
            pbody = page.evaluate("() => document.body.innerText")
            check("配置中心: 提示词配置列表渲染", pcards >= 1 or "提示词" in pbody,
                  f"cards={pcards}")

            page.goto(f"{BASE}/configuration/generation-config", wait_until="domcontentloaded")
            page.wait_for_timeout(2800)
            gcards = page.locator(".config-card").count()
            gbody = page.evaluate("() => document.body.innerText")
            check("配置中心: 生成行为配置渲染", gcards >= 1 or "默认生成配置" in gbody,
                  f"cards={gcards}")
        except Exception as exc:  # noqa: BLE001
            check("配置中心三件套", False, f"exception: {exc}")

        # ================= G. 文档中心：点击渲染 + 搜索 + 净化 =================
        reset_listeners()
        try:
            page.goto(f"{BASE}/docs-center", wait_until="domcontentloaded")
            page.wait_for_timeout(3000)
            items = page.locator(".doc-item").count()
            check("文档中心: 文档列表加载(>=10 篇)", items >= 10, f"items={items}")

            page.locator(".doc-item").first.click()
            page.wait_for_timeout(2500)
            html_len = page.evaluate(
                "() => (document.querySelector('.markdown-body')?.innerHTML || '').length"
            )
            has_tags = page.evaluate(
                "() => !!document.querySelector('.markdown-body p, .markdown-body h1, "
                ".markdown-body h2, .markdown-body ul')"
            )
            dangerous = page.evaluate(
                "() => { const el = document.querySelector('.markdown-body'); if (!el) return true;"
                " return !!(el.querySelector('script') || el.querySelector('[onerror]')) }"
            )
            check("文档中心: 点击文档后 Markdown 渲染为 HTML", html_len > 200 and has_tags,
                  f"htmlLen={html_len} tags={has_tags}")
            check("文档中心: 渲染结果无 script/onerror(净化)", not dangerous)

            page.locator(".docs-sidebar input").first.fill("数据工厂")
            page.wait_for_timeout(1200)
            filtered = page.locator(".doc-item").count()
            titles = page.evaluate(
                "() => Array.from(document.querySelectorAll('.doc-item .doc-title'))"
                ".map(e => e.innerText)"
            )
            all_match = all("数据工厂" in t for t in titles) if titles else False
            check("文档中心: 关键词搜索过滤生效", 0 < filtered < items and all_match,
                  f"filtered={filtered} titles={titles[:3]}")
        except Exception as exc:  # noqa: BLE001
            check("文档中心交互", False, f"exception: {exc}")

        # ================= H. 智能评分器 =================
        try:
            page.goto(f"{BASE}/llm-judge/dashboard", wait_until="domcontentloaded")
            page.wait_for_timeout(3000)
            body = page.evaluate("() => document.body.innerText")
            ok = ("评分总数" in body) and ("通过率" in body)
            check("智能评分器: 看板统计渲染", ok, f"head={'评分总数' in body} rate={'通过率' in body}")

            page.goto(f"{BASE}/llm-judge/rubrics", wait_until="domcontentloaded")
            page.wait_for_timeout(2800)
            rbody = page.evaluate("() => document.body.innerText")
            check("智能评分器: 评分标准页可打开", "评分标准" in rbody, f"len={len(rbody)}")
        except Exception as exc:  # noqa: BLE001
            check("智能评分器", False, f"exception: {exc}")

        # ================= I. MCP 控制台：工具目录 + tab 切换 =================
        reset_listeners()
        try:
            page.goto(f"{BASE}/mcp/console", wait_until="domcontentloaded")
            page.wait_for_timeout(3500)
            rows = page.locator(".el-table__row").count()
            tabs = page.locator(".el-tabs__item").count()
            body = page.evaluate("() => document.body.innerText")
            check("MCP控制台: 工具目录加载(列表+3 tab)",
                  rows >= 10 and tabs == 3 and "工具总数" in body,
                  f"rows={rows} tabs={tabs} total={'工具总数' in body}")

            page.get_by_text("调用日志", exact=True).first.click()
            page.wait_for_timeout(2500)
            logs_ok = (not errors) and (not failed_api)
            check("MCP控制台: 切换到调用日志无接口错误", logs_ok, summarize_failures())
        except Exception as exc:  # noqa: BLE001
            check("MCP控制台", False, f"exception: {exc}")

        browser.close()

    ok = all(c for _, c, _ in results)
    total = len(results)
    passed = sum(1 for _, c, _ in results if c)
    print(f"\nUI_E2E(二开) {passed}/{total} passed")
    print("UI_E2E(二开) RESULT:", "ALL PASS" if ok else "HAS FAILURE")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
