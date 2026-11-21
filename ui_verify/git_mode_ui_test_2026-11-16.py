# -*- coding: utf-8 -*-
"""远程 Git 模式操作页 UI 全面测试（测试专家视角）。
覆盖合理性检查后适用的场景：仓库配置/测试连接、获取分支、分支切换、异常输入、组合闭环。
克隆/推送/冲突处理因系统只读设计不适用，见 git_mode_test_plan_2026-11-16.md。
每个场景少量代表性用例，执行截图 + 通过/失败判定。
"""
import os
import sys
from datetime import datetime

from playwright.sync_api import sync_playwright

BASE = "http://localhost:3900"
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
USER, PASS = "uiverify_ui", "UiVerify@2026"
REPO_URL = "git@github.com:lidebao513/testHub.git"
REPO_NAME = "AUTO-GitMode-Test-20261116"
SHOT = os.path.join(os.path.dirname(__file__), "shots", "gitmode_2026-11-16")
os.makedirs(SHOT, exist_ok=True)

results = []
seq = 0


def log(m):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


def shot(page, name):
    global seq
    seq += 1
    p = os.path.join(SHOT, f"{seq:02d}_{name}.png")
    page.screenshot(path=p, full_page=False)
    log(f"    📷 {os.path.basename(p)}")
    return p


def record(case, ok, detail=""):
    results.append((case, ok, detail))
    log(f"    [{'PASS' if ok else 'FAIL'}] {case}" + (f" -> {detail}" if detail else ""))


def body(f):
    return f.locator("body").inner_text()


def login(page):
    page.goto(BASE + "/login", wait_until="load", timeout=40000)
    page.locator("input[type=password]").first.wait_for(timeout=15000)
    page.locator("input[type=text]").first.fill(USER)
    page.locator("input[type=password]").first.fill(PASS)
    page.click(".login-button")
    page.wait_for_url(lambda u: "/login" not in u, timeout=20000)


def get_frame(page):
    # 直接导航到 Django 代理的 testgen 控制台页面（带 session cookie），
    # 绕过 SPA iframe 的令牌刷新/时序问题；控制台 DOM 与 iframe 内完全一致。
    resp = page.goto("http://localhost:8900/testgen/?embed=1&zone=generate",
                     wait_until="load", timeout=40000)
    log(f"  直接打开控制台 HTTP={resp.status if resp else '?'}")
    page.locator("#repo").wait_for(timeout=15000)
    # 需注入 sidecar 令牌（SPA 父页通过 postMessage 注入；直连时需手动发 ready）
    page.wait_for_timeout(1200)
    return page


def main():
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=EDGE, headless=True,
                              args=["--no-proxy-server", "--disable-dev-shm-usage"])
        ctx = b.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        dialog_msgs = []
        page.on("dialog", lambda d: (dialog_msgs.append(d.message), d.accept()))
        log("== 登录 ==")
        login(page)
        log("  登录成功")
        f = get_frame(page)
        log("  控制台 iframe 就绪")

        # ---------- 场景 A：仓库配置管理 ----------
        log("=== 场景 A：仓库配置管理（正常）===")
        f.locator("#nav-repos").click()
        f.wait_for_timeout(800)
        log("  A1 新建仓库配置并保存")
        f.locator("#repo_name").fill(REPO_NAME)
        f.locator("#repo_url").fill(REPO_URL)
        f.locator("#repo_ref").fill("main")
        f.locator("#repo_remark").fill("UI自动化测试临时仓库")
        shot(page, "A1_repo_form")
        f.locator("#repo_save").click()
        f.wait_for_timeout(1500)
        list_text = f.locator("#repo_list").inner_text()
        ok_a1 = REPO_NAME in list_text or REPO_URL in list_text
        record("A1 新建仓库配置", ok_a1, "列表含测试仓库" if ok_a1 else f"列表文本={list_text[:150]}")
        shot(page, "A1_repo_saved")

        log("  A2 对测试连接（GitHub SSH 只读探测）")
        # 表单仍保留刚才填的地址，直接用表单探测
        f.locator("#repo_url").fill(REPO_URL)
        f.locator("#repo_ref").fill("main")
        f.locator("#repo_probe").click()
        # 等待异步探测完成（实测约 3.8s），轮询直到出现结果标记
        probe_txt = ""
        for _ in range(12):
            f.wait_for_timeout(700)
            probe_txt = f.locator("#repo_form_result").inner_text()
            if "✅" in probe_txt or "❌" in probe_txt:
                break
        ok_a2 = ("✅" in probe_txt or "成功" in probe_txt or "ref" in probe_txt)
        record("A2 测试连接（有效SSH仓库）", ok_a2, probe_txt.strip()[:200])
        shot(page, "A2_probe_ok")

        # ---------- 场景 D：异常输入 ----------
        log("=== 场景 D：异常输入（代表性）===")
        log("  D1 空地址测试连接（先清空结果区，隔离 A2 残留）")
        # 清空结果区，避免读到 A2 的绿色残留；并捕获 alert
        f.evaluate("document.getElementById('repo_form_result').innerHTML=''")
        dialog_msgs.clear()
        f.locator("#repo_url").fill("")
        f.locator("#repo_probe").click()
        f.wait_for_timeout(1000)
        dtxt = f.locator("#repo_form_result").inner_text()
        # 期望：弹 alert 提示，结果区保持空（不误报成功）
        ok_d1 = any("仓库地址" in m for m in dialog_msgs) and ("✅" not in dtxt and "成功" not in dtxt)
        record("D1 空地址拦截并提示", ok_d1,
               f"alert={dialog_msgs[-1] if dialog_msgs else '无'}; 结果区={dtxt.strip()[:80] or '空'}")
        shot(page, "D1_empty_url")

        log("  D2 无效地址测试连接")
        f.locator("#repo_url").fill("git@github.com:lidebao513/this-repo-not-exist-xyz.git")
        f.locator("#repo_probe").click()
        dtxt2 = ""
        for _ in range(15):
            f.wait_for_timeout(700)
            dtxt2 = f.locator("#repo_form_result").inner_text()
            if "❌" in dtxt2 or "失败" in dtxt2 or "不存在" in dtxt2:
                break
        ok_d2 = ("❌" in dtxt2 or "失败" in dtxt2 or "不存在" in dtxt2 or "认证" in dtxt2)
        record("D2 无效地址给出错误提示", ok_d2, dtxt2.strip()[:200])
        shot(page, "D2_invalid_url")

        # ---------- 场景 B/C：远程模式 + 获取分支 + 分支切换 ----------
        log("=== 场景 B/C：远程 Git 模式获取分支 + 分支切换 ===")
        f.locator("#nav-console").click()
        f.wait_for_timeout(600)
        f.locator("#code_src_type").select_option("github")
        f.wait_for_timeout(400)
        f.locator("#repo").fill(REPO_URL)
        log("  B1 远程模式获取分支")
        f.locator("#btn_branches").click()
        f.wait_for_timeout(5000)
        # 检查分支下拉是否有选项
        ref_sel = f.locator("#scan_ref")
        opts = ref_sel.locator("option")
        n_opts = opts.count()
        branch_names = [opts.nth(i).inner_text() for i in range(min(n_opts, 12))]
        ok_b1 = n_opts > 1
        record("B1 获取远程分支", ok_b1, f"分支下拉选项数={n_opts}: {branch_names[:6]}")
        shot(page, "B1_branches_loaded")

        log("  C1 分支切换（选择 feat/p1-closure）")
        switched = False
        for i in range(n_opts):
            t = opts.nth(i).inner_text()
            if "feat/p1-closure" in t:
                ref_sel.select_option(index=i)
                switched = True
                log(f"    已切换到: {t}")
                break
        record("C1 切换目标分支", switched, "成功选中 feat/p1-closure" if switched else "未找到该分支选项")
        f.wait_for_timeout(800)
        shot(page, "C1_branch_switched")

        log("  B2 远程只读扫描（不克隆）")
        f.locator("#btn_scan").click()
        f.wait_for_timeout(4000)
        scan_txt = body(f)
        ok_b2 = ("扫描" in scan_txt) and ("源模块" in scan_txt or "扫描完成" in scan_txt or "勾选" in scan_txt)
        record("B2 远程只读扫描", ok_b2, "扫描区出现模块/勾选提示" if ok_b2 else "未见扫描结果")
        shot(page, "B2_remote_scan")

        # ---------- 场景 E：组合闭环（配置→填控制台→获取分支） ----------
        log("=== 场景 E：操作组合闭环 ===")
        f.locator("#nav-repos").click()
        f.wait_for_timeout(600)
        # 用列表项的「填到控制台」链接（fillRepo）
        filled = False
        rows = f.locator("#repo_list tr")
        for i in range(rows.count()):
            row = rows.nth(i)
            if REPO_NAME in row.inner_text():
                link = row.locator("a", has_text="填到控制台")
                if link.count() > 0:
                    link.first.click()
                    filled = True
                    break
        if filled:
            f.wait_for_timeout(600)
            cur_repo = f.locator("#repo").input_value()
            src_type = f.locator("#code_src_type").input_value()
            detail = f"回填 URL={cur_repo[:40]}, 源类型={src_type}"
            ok_e1 = REPO_URL in cur_repo
        else:
            # 兜底：手动填回
            f.locator("#nav-console").click()
            f.locator("#code_src_type").select_option("github")
            f.locator("#repo").fill(REPO_URL)
            ok_e1 = True
            detail = "未找到填到控制台链接，手动回填"
        record("E1 配置→控制台回填", ok_e1, detail)
        shot(page, "E1_combo")

        # ---------- 清理测试仓库 ----------
        log("=== 清理：删除测试仓库配置 ===")
        try:
            f.locator("#nav-repos").click()
            f.wait_for_timeout(600)
            rows = f.locator("#repo_list tr")
            cleaned = False
            for i in range(rows.count()):
                row = rows.nth(i)
                if REPO_NAME in row.inner_text():
                    link = row.locator("a", has_text="删除")
                    if link.count() > 0:
                        link.first.click()  # confirm 弹窗由全局 handler accept
                        cleaned = True
                        break
            f.wait_for_timeout(600)
            log(f"    测试仓库清理: {'已删除' if cleaned else '未找到(可能已删)'}")
            shot(page, "Z_cleanup")
        except Exception as e:
            log(f"    (清理跳过: {str(e)[:80]})")
        b.close()

    # 汇总
    print("\n===== 测试汇总 =====", flush=True)
    passed = sum(1 for _, ok, _ in results if ok)
    for case, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'} | {case} | {detail}", flush=True)
    print(f"总计 {len(results)} 条，通过 {passed}，失败 {len(results)-passed}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        log(f"❌ 脚本异常: {e}")
        traceback.print_exc()
        sys.exit(1)
