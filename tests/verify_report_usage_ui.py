"""UI 层验证：#178 四个用量字段（模型 / Token / 成本 / 预探索）在页面真的渲染出来。

为什么要有这条：接口返回字段 ≠ 页面显示字段。本次改动的全部价值就在「人能不能看见」，
所以必须从登录页走进去、点开列表、点详情、点报告，逐屏确认。

用法（前后端已启动）：
    venv/Scripts/python.exe tests/verify_report_usage_ui.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:3900"
LIST_URL = f"{BASE}/ai-intelligent-mode/execution-records"
USER, PWD = "e2etest", "E2e@123456"
ROOT = Path(__file__).resolve().parent.parent
SHOTS = ROOT / "logs" / "e2e_ui"


def login(page) -> bool:
    page.goto(f"{BASE}/login", wait_until="domcontentloaded", timeout=30_000)
    page.wait_for_selector("input", timeout=15_000)
    page.locator("input").nth(0).fill(USER)
    pwd = page.locator("input[type='password']").first
    pwd.fill(PWD)
    pwd.press("Enter")
    try:
        page.wait_for_url(lambda url: "/login" not in url, timeout=25_000)
        return True
    except Exception:
        return False


def has_text(page, *keywords) -> tuple[bool, list[str]]:
    body = page.inner_text("body")
    missing = [k for k in keywords if k not in body]
    return (not missing, missing)


def main() -> int:
    SHOTS.mkdir(parents=True, exist_ok=True)
    failures: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-proxy-server", "--no-sandbox"])
        page = browser.new_page(viewport={"width": 1600, "height": 1000})

        if not login(page):
            print("❌ 登录失败")
            return 1
        print("✅ 登录成功")

        # ---------- 1. 列表页：模型 / Token 两列 ----------
        page.goto(LIST_URL, wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_timeout(3_000)
        page.screenshot(path=str(SHOTS / "usage-1-list.png"), full_page=True)

        ok, missing = has_text(page, "模型", "Token 数")
        if ok:
            print("✅ 列表页表头含「模型」「Token 数」")
        else:
            failures.append(f"列表页表头缺少: {missing}")
            print(f"❌ 列表页表头缺少: {missing}")

        rows = page.locator(".el-table__body-wrapper tbody tr")
        row_count = rows.count()
        print(f"   列表行数: {row_count}")
        if row_count == 0:
            failures.append("列表无数据，无法验证详情/报告")
            print("❌ 列表无数据（请先跑一条 AI 执行）")
            browser.close()
            return 1

        # 首行应带路由模型（deepseek 系）与 token 数字；空值显示 '-' 也算渲染成功
        first_row_text = rows.nth(0).inner_text().replace("\n", " | ")
        print(f"   首行内容: {first_row_text}")

        # ---------- 2. 详情弹窗：四个归因项 ----------
        page.locator("button", has_text="查看详情").first.click()
        page.wait_for_timeout(1_500)
        page.screenshot(path=str(SHOTS / "usage-2-detail.png"), full_page=True)

        # 详情弹窗用的是列表页的短标签（模型 / Token 数），与报告页措辞不同
        ok, missing = has_text(page, "模型", "Token 数", "成本(USD)", "预探索")
        if ok:
            print("✅ 详情弹窗含四项归因标签")
        else:
            failures.append(f"详情弹窗缺少: {missing}")
            print(f"❌ 详情弹窗缺少: {missing}")

        detail_body = page.inner_text(".el-dialog")
        # 元素清单逐行渲染成「[index] tag label」，用序号方括号判断是否真的列出来了
        listed = page.locator(".pre-explore-item").count()
        if listed > 0:
            print(f"✅ 预探索元素清单已渲染（{listed} 项）")
        elif "预探索元素" in detail_body:
            print("ℹ️ 有「预探索元素」标签但清单为空（该记录未捕获元素）")
        else:
            failures.append("详情弹窗既无元素清单也无相关标签")
            print("❌ 详情弹窗既无元素清单也无相关标签")

        page.locator(".el-dialog__footer button", has_text="关闭").first.click()
        page.wait_for_timeout(800)

        # ---------- 3. 报告弹窗（摘要）：AI 用量归因 section ----------
        page.locator("button", has_text="查看报告").first.click()
        page.wait_for_timeout(4_000)
        page.screenshot(path=str(SHOTS / "usage-3-report-summary.png"), full_page=True)

        ok, missing = has_text(page, "AI 用量归因", "执行模型", "Token 消耗", "执行成本")
        if ok:
            print("✅ 报告摘要页含「AI 用量归因」四项")
        else:
            failures.append(f"报告摘要缺少: {missing}")
            print(f"❌ 报告摘要缺少: {missing}")

        report_body = page.inner_text(".el-dialog")
        if "未知" in report_body:
            print("✅ 成本未知显示为「未知」（未被渲染成 0）")
        if "动手前页面元素清单" in report_body:
            print("✅ 预探索元素清单已渲染")
        elif "本次未捕获" in report_body:
            print("ℹ️ 预探索显示「本次未捕获」（该记录未开开关或未就绪）")

        # ---------- 4. 性能分析页：也带 token / 成本 ----------
        page.locator(".el-radio-button", has_text="性能分析").first.click()
        page.wait_for_timeout(4_000)
        page.screenshot(path=str(SHOTS / "usage-4-report-performance.png"), full_page=True)

        ok, missing = has_text(page, "Token 消耗", "执行成本")
        if ok:
            print("✅ 性能分析页含 Token / 成本卡")
        else:
            failures.append(f"性能分析页缺少: {missing}")
            print(f"❌ 性能分析页缺少: {missing}")

        browser.close()

    print("\n===== 结果 =====")
    if failures:
        for f in failures:
            print(f"❌ {f}")
        print(f"截图目录: {SHOTS}")
        return 1
    print("✅ 全部通过")
    print(f"截图目录: {SHOTS}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
