# -*- coding: utf-8 -*-
"""UI 层端到端验证：登录 -> 拉取代码(扫描) -> 生成测试用例。
真实 Edge + Playwright 逐步操作并截图，输出每一步操作过程日志。
新版 testgen 控制台在 iframe(frame[1]) 内，控件 id: code_src_type/repo/btn_scan/btn_gen。
被测代码库用本机已拉取的 research-agent 源码（本地模式）。
"""
import os
import sys
import time
from datetime import datetime

from playwright.sync_api import sync_playwright

BASE = "http://localhost:3900"
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
USER, PASS = "uiverify_ui", "UiVerify@2026"
REPO = r"C:\Users\EDY\WorkBuddy\testhub_platform\services\testgen\data\workspaces\research-agent\backend\research-agent-source"
SHOT = os.path.join(os.path.dirname(__file__), "shots", "flow_2026-11-16")
os.makedirs(SHOT, exist_ok=True)

STEP = 0


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def shot(page, name):
    global STEP
    STEP += 1
    path = os.path.join(SHOT, f"{STEP:02d}_{name}.png")
    page.screenshot(path=path, full_page=False)
    log(f"  📷 截图: {os.path.basename(path)}")
    return path


def text_of(f, sel):
    try:
        return (f.locator(sel).first.inner_text(timeout=3000) or "").strip()
    except Exception:
        return ""


def main():
    console_errs, page_errs = [], []
    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=EDGE, headless=True,
            args=["--no-proxy-server", "--disable-dev-shm-usage"],
        )
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        page.on("console", lambda m: console_errs.append(f"{m.type}: {m.text}")
                if m.type == "error" else None)
        page.on("pageerror", lambda e: page_errs.append(str(e)))

        # ---------- 1. 登录 ----------
        log("=== 步骤 1/6：打开登录页 ===")
        page.goto(BASE + "/login", wait_until="load", timeout=40000)
        page.locator("input[type=password]").first.wait_for(timeout=15000)
        log(f"  页面标题: {page.title()}  URL: {page.url}")
        shot(page, "login_page")

        log("=== 步骤 2/6：真实表单登录 ===")
        page.locator("input[type=text]").first.fill(USER)
        page.locator("input[type=password]").first.fill(PASS)
        shot(page, "login_filled")
        page.click(".login-button")
        page.wait_for_url(lambda u: "/login" not in u, timeout=20000)
        page.wait_for_timeout(1500)
        log(f"  登录成功 -> {page.url}")
        shot(page, "after_login")

        # ---------- 3. 进入控制台，定位 iframe ----------
        log("=== 步骤 3/6：进入 testgen 代码生成控制台 ===")
        page.goto(BASE + "/ai-generation/testgen-console", wait_until="load", timeout=40000)
        page.wait_for_timeout(3000)
        f = None
        for fr in page.frames:
            if fr.url.startswith("http://localhost:8900/testgen/"):
                f = fr
                break
        if f is None:
            log("  ❌ 未找到 testgen iframe")
            for i, fr in enumerate(page.frames):
                log(f"    frame[{i}]={fr.url}")
            raise RuntimeError("testgen iframe not found")
        f.locator("#repo").wait_for(timeout=15000)
        log(f"  控制台 iframe 就绪: {f.url}")
        shot(page, "console_ready")

        # ---------- 4. 拉取代码（扫描） ----------
        log("=== 步骤 4/6：拉取代码 —— 选本地路径、填源码目录、点击扫描 ===")
        # 代码来源类型下拉：看选项，选"本地"
        src = f.locator("#code_src_type")
        opts = src.locator("option")
        log("  代码来源选项: " + " | ".join(
            f"{opts.nth(i).get_attribute('value')}={opts.nth(i).inner_text()}"
            for i in range(opts.count())))
        # 选本地目录类型（value 通常 local / path / dir）
        chosen = None
        for i in range(opts.count()):
            v = (opts.nth(i).get_attribute("value") or "").lower()
            t = opts.nth(i).inner_text()
            if "本地" in t or v in ("local", "path", "dir", "directory"):
                src.select_option(index=i)
                chosen = f"{v}:{t}"
                break
        log(f"  已选代码来源: {chosen or '(保持默认)'}")
        f.locator("#repo").fill(REPO)
        log(f"  已填入代码目录: {REPO}")
        shot(page, "scan_filled")

        f.locator("#btn_scan").click(timeout=10000)
        log("  已点击【扫描】，等待扫描结果…")
        # 扫描结果区：控制台右上有"扫描结果"面板，轮询 body 文本出现模块
        scan_ok = False
        for i in range(40):
            f.wait_for_timeout(1000)
            body = f.locator("body").inner_text()
            if ("扫描完成" in body or "个源模块" in body or "扫描结果" in body and "module" in body.lower()):
                scan_ok = True
                break
            if "失败" in body and "扫描" in body:
                break
        # 抓取扫描结果片段
        body = f.locator("body").inner_text()
        idx = body.find("扫描")
        snippet = body[idx:idx + 500].replace("\n", " ") if idx >= 0 else body[:300]
        log(f"  扫描后页面片段: {snippet[:400]}")
        log(f"  扫描是否完成: {scan_ok}")
        shot(page, "scan_result")

        # ---------- 5. 生成用例 ----------
        log("=== 步骤 5/6：生成测试用例 —— 勾选模块并点击【生成用例】 ===")
        # 全选扫描出的模块
        try:
            f.get_by_role("button", name="全选").click(timeout=5000)
            log("  已点击【全选】勾选全部扫描模块")
        except Exception as e:
            log(f"  【全选】不可用（可能无模块或未渲染）: {str(e)[:80]}")
        # 项目下拉：控制台有"所属项目"select（若存在）
        for sid in ["exec_pid", "gen_project", "project_id"]:
            try:
                if f.locator(f"#{sid}").count() > 0:
                    log(f"  项目字段 #{sid} 存在（值={f.locator(f'#{sid}').input_value()}）")
                    break
            except Exception:
                pass
        shot(page, "before_generate")
        f.locator("#btn_gen").click(timeout=10000)
        log("  已点击【生成用例】，等待任务提交…")
        f.wait_for_timeout(3000)
        body2 = f.locator("body").inner_text()
        # 找 task_id / 状态
        import re
        m = re.search(r"task[_\s]*id[:：]?\s*(\d+)", body2, re.I)
        if m:
            log(f"  生成任务已提交 task_id={m.group(1)}")
        else:
            idx2 = body2.find("生成")
            log(f"  提交后页面片段: {body2[idx2:idx2+300].replace(chr(10),' ') if idx2>=0 else body2[:300]}")
        shot(page, "generate_submitted")

        # ---------- 6. 轮询进度 ----------
        log("=== 步骤 6/6：查看任务实时进度 ===")
        last = ""
        for i in range(20):
            f.wait_for_timeout(3000)
            body3 = f.locator("body").inner_text()
            # 提取"阶段/状态/当前任务"附近的文本
            cur = ""
            for key in ["当前任务", "阶段", "状态"]:
                k = body3.find(key)
                if k >= 0:
                    cur += body3[k:k + 80].replace("\n", " ") + " | "
            cur = cur.strip() or body3[:100].replace("\n", " ")
            if cur != last:
                log(f"  进度[{i+1}]: {cur[:200]}")
                last = cur
            low = body3.lower()
            if any(s in low for s in ("success", "完成", "成功", "failed", "失败")) and i > 2:
                break
        shot(page, "task_progress")
        log("=== 浏览器错误 ===")
        log(f"  console errors={len(console_errs)}" + (f" -> {console_errs[:2]}" if console_errs else ""))
        log(f"  page errors={len(page_errs)}" + (f" -> {page_errs[:2]}" if page_errs else ""))
        browser.close()
    log("=== 验证完成 ===")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        log(f"❌ 脚本异常: {e}")
        traceback.print_exc()
        sys.exit(1)
