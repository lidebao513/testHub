# -*- coding: utf-8 -*-
"""P0 补齐：远程 Git 模式未执行用例 B3 / C2 / D3 / E1。

B3 指定 ref 生成并出报告；C2 基线 ref 增量识别；
D3 Codeup 未填凭据提示；E1 配置→测试→分支→扫描→生成→报告完整串联。
"""
import os
import re
from datetime import datetime

from playwright.sync_api import sync_playwright

BASE = "http://localhost:3900"
CONSOLE = "http://localhost:8900/testgen/?embed=1&zone=generate"
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
USER, PASS = "uiverify_ui", "UiVerify@2026"
REPO_URL = "git@github.com:lidebao513/testHub.git"
TARGET_REF = "feat/p1-closure"
BASE_REF = "main"
LOCAL_REPO = r"C:\Users\EDY\WorkBuddy\testhub_platform\services\testgen\data\workspaces\research-agent"
LOCAL_BASE = "126eacf203a9946b1670f04c880167954bbc76ef"  # HEAD~3，与 HEAD 有 3 个 modified
NEW_REPO_NAME = "AUTO-P0-GitMode-20261116"
SHOT = os.path.join(os.path.dirname(__file__), "shots", "p0_gitmode_2026-11-16")
os.makedirs(SHOT, exist_ok=True)
PROJECTS = []  # 本轮生成的 project_id，供 P1 清理
_seq = 0
results = []


def log(m):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


def shot(page, name):
    global _seq
    _seq += 1
    p = os.path.join(SHOT, f"{_seq:02d}_{name}.png")
    page.screenshot(path=p, full_page=False)
    log(f"    📷 {os.path.basename(p)}")


def record(case, ok, detail=""):
    results.append((case, ok, detail))
    log(f"  [{'PASS' if ok else 'FAIL'}] {case}" + (f" -> {detail}" if detail else ""))


def wait_task(page, timeout_ms=240000):
    page.wait_for_function("""() => {
      const el = document.querySelector('#task_state');
      const t = el ? (el.textContent||'').toLowerCase() : '';
      return t.includes('success') || t.includes('failed') || t.includes('已取消');
    }""", timeout=timeout_ms)
    return page.locator("#task_state").inner_text().strip()


def fill_console_repo(page, repo=REPO_URL, src="github"):
    page.locator("#nav-console").click()
    page.wait_for_timeout(500)
    page.locator("#code_src_type").select_option(src)
    page.wait_for_timeout(300)
    page.locator("#repo").fill(repo)
    page.wait_for_timeout(300)


def load_branches(page):
    page.locator("#btn_branches").click()
    page.wait_for_function("""() => {
      const s = document.querySelector('#scan_ref');
      return s && s.options.length > 1;
    }""", timeout=90000)
    opts = page.locator("#scan_ref option")
    return [opts.nth(i).inner_text() for i in range(opts.count())]


def select_ref(page, sel_id, name):
    opts = page.locator(f"#{sel_id} option")
    for i in range(opts.count()):
        if name in opts.nth(i).inner_text():
            page.locator(f"#{sel_id}").select_option(index=i)
            page.wait_for_timeout(400)
            return True
    return False


def scan(page):
    # 先重置 meta，避免上一轮残留导致误判
    page.evaluate("""() => {
      const m=document.querySelector('#scan_meta'); if(m){ m.style.display='none'; m.textContent=''; }
      const l=document.querySelector('#scan_list'); if(l) l.textContent='⏳ 等待扫描…';
    }""")
    page.locator("#btn_scan").click()
    page.wait_for_function("""() => {
      const l = document.querySelector('#scan_list');
      const m = document.querySelector('#scan_meta');
      const lt = l ? (l.textContent||'') : '';
      const mt = m ? (m.textContent||'') : '';
      return /扫描失败|扫描异常|凭证|access_key/.test(lt) || (m && m.style.display !== 'none' && mt);
    }""", timeout=90000)
    return page.locator("#scan_meta").inner_text().strip()


def select_one_file(page):
    n = page.locator(".scan-cb").count()
    if n == 0:
        return 0
    page.evaluate("""() => {
      document.querySelectorAll('.scan-cb').forEach((cb,i)=>cb.checked = (i===0));
    }""")
    return n


def generate(page, project_name):
    page.locator("#pname").fill(project_name)
    page.locator("#btn_gen").click()
    state = wait_task(page)
    pid = page.locator("#pid").inner_text().strip()
    try:
        n = int(pid)
        PROJECTS.append(n)
    except ValueError:
        n = 0
    link = page.locator("#report_link")
    visible = link.is_visible() and link.get_attribute("href")
    log(f"    任务终态={state}, project_id={pid}, 报告链接={bool(visible)}")
    return state, n, bool(visible)


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
        page.goto(CONSOLE, wait_until="load", timeout=40000)
        page.locator("#repo").wait_for(timeout=15000)
        page.wait_for_timeout(800)

        # ========== B3：指定 ref 生成 + 报告 ==========
        log("=== B3 指定 ref 生成并出报告 ===")
        fill_console_repo(page)
        branches = load_branches(page)
        log(f"    分支数={len(branches)}: {branches[:5]}")
        ok_ref = select_ref(page, "scan_ref", TARGET_REF)
        record("B3-1 指定目标 ref 可选", ok_ref, TARGET_REF if ok_ref else "未找到分支")
        meta = scan(page)
        n_files = select_one_file(page)
        log(f"    扫描={meta}, 文件数={n_files}")
        state, pid, has_report = generate(page, "AUTO-P0-B3-GitRef")
        record("B3-2 指定 ref 生成任务成功", "success" in state.lower() and pid > 0,
               f"state={state}, pid={pid}")
        record("B3-3 生成后出现报告链接", has_report, f"pid={pid}")
        shot(page, "B3_generate_report")

        # ========== C2：基线 ref 增量识别 ==========
        # 远程 GitHub SSH 的 scan 端点强制要 Codeup PAT（见 C2-0 缺陷记录），
        # 故增量识别改用本地 git 仓库（有真实 diff）验证同一 UI/引擎逻辑。
        log("=== C2 基线 ref 增量识别（本地 git 仓库，含真实 diff）===")
        page.locator("#nav-console").click()
        page.wait_for_timeout(400)
        page.locator("#code_src_type").select_option("local")
        page.wait_for_timeout(300)
        page.locator("#repo").fill(LOCAL_REPO)
        page.wait_for_timeout(300)
        # 基线 ref 输入框在 local 模式下为文本输入（scan_base_ref 是 select，本地分支有限）
        # 直接用 JS 设值 + 同步隐藏字段，模拟用户选择历史 commit 作为基线
        page.evaluate("""(base) => {
          const sel = document.querySelector('#scan_base_ref');
          let opt = [...sel.options].find(o => o.value === base);
          if(!opt){ opt = document.createElement('option'); opt.value = base; opt.textContent = base.slice(0,8); sel.appendChild(opt); }
          sel.value = base;
          sel.dispatchEvent(new Event('change', {bubbles:true}));
        }""", LOCAL_BASE)
        page.locator('input[name="gen_mode"][value="incremental"]').check()
        page.wait_for_timeout(300)
        record("C2-1 增量模式可切换 + 基线可设",
               page.locator('input[name="gen_mode"][value="incremental"]').is_checked()
               and page.locator("#scan_base_ref").input_value() == LOCAL_BASE,
               f"mode=incremental, base={LOCAL_BASE[:8]}")
        meta2 = scan(page)
        log(f"    增量扫描 meta={meta2}")
        nums = dict(re.findall(r"(新增|修改)\s*(\d+)", meta2))
        added = int(nums.get("新增", 0))
        modified = int(nums.get("修改", 0))
        checked = page.evaluate("""() => {
          const cbs=[...document.querySelectorAll('.scan-cb')];
          return {total:cbs.length, checked:cbs.filter(c=>c.checked).length,
                  changed:cbs.filter(c=>c.checked && ['added','modified'].includes(c.dataset.status)).length};
        }""")
        record("C2-2 增量扫描识别出变更(新增/修改)",
               (added + modified) > 0,
               f"新增={added}, 修改={modified}, meta={meta2[:100]}")
        record("C2-3 增量模式自动只勾选变更文件",
               checked["checked"] > 0 and checked["changed"] == checked["checked"],
               f"选中={checked['checked']}, 其中变更={checked['changed']}, 总文件={checked['total']}")
        shot(page, "C2_incremental")
        # 恢复全量模式，避免影响后续
        page.locator('input[name="gen_mode"][value="full"]').check()
        page.wait_for_timeout(200)

        # ========== C2-0 缺陷记录：远程 SSH 扫描强制 PAT ==========
        log("=== C2-0 远程 GitHub SSH 扫描凭证要求（缺陷探测）===")
        fill_console_repo(page)
        page.locator("#btn_scan").click()
        page.wait_for_timeout(3500)
        scan_list_txt = page.locator("#scan_list").inner_text()
        log_t = page.locator("#log").inner_text()
        bad_cred = ("凭证" in scan_list_txt or "access_key" in scan_list_txt
                    or "凭证" in log_t or "access_key" in log_t)
        record("C2-0 [缺陷] 远程SSH扫描未带PAT时报错(生成却能成功)",
               bad_cred,
               f"scan区={scan_list_txt[:80]}, log尾={log_t[-120:]}")

        # ========== D3：Codeup 未填凭据提示 ==========
        log("=== D3 Codeup 未填凭据提示（manual 模式空 key）===")
        page.locator("#nav-console").click()
        page.wait_for_timeout(400)
        page.evaluate("""() => {
          localStorage.removeItem('tg_codeup_org_id');
          localStorage.removeItem('tg_codeup_access_key');
        }""")
        page.locator("#code_src_type").select_option("codeup")
        page.wait_for_timeout(300)
        # 未配置仓库时 stored 选项被正确禁用（设计如此），用户只能走 manual
        page.locator("#codeup_key_mode").select_option("manual")
        page.locator("#codeup_access_key").fill("")
        page.locator("#codeup_org_id").fill("")
        page.locator("#repo").fill("https://codeup.aliyun.com/69cf2ce1f167bac7fd252020/fake.git")
        page.wait_for_timeout(300)
        # 顺带验证 stored 选项在无配置仓库时确实被禁用（正确设计）
        stored_disabled = page.evaluate("""() => {
          const o=document.querySelector('#codeup_key_mode option[value="stored"]');
          return o ? o.disabled : null;
        }""")
        record("D3-0 无存凭证时 stored 选项禁用(正确设计)", stored_disabled is True,
               f"stored.disabled={stored_disabled}")
        dialogs.clear()
        page.locator("#btn_scan").click()
        page.wait_for_timeout(900)
        hit = any(("凭证" in m) or ("access_key" in m) or ("PAT" in m) for m in dialogs)
        record("D3-1 manual 模式空 key 给出提示", hit,
               f"alert={dialogs[-1] if dialogs else '无'}")
        shot(page, "D3_codeup_no_cred")
        # 恢复为 github，避免影响后续
        page.locator("#code_src_type").select_option("github")
        page.wait_for_timeout(200)

        # ========== E1：完整串联闭环 ==========
        log("=== E1 配置→测试→分支→扫描→生成→报告 完整串联 ===")
        # 1. 配置仓库
        page.locator("#nav-repos").click()
        page.wait_for_timeout(500)
        page.locator("#repo_name").fill(NEW_REPO_NAME)
        page.locator("#repo_url").fill(REPO_URL)
        page.locator("#repo_ref").fill("main")
        page.locator("#repo_save").click()
        page.wait_for_timeout(1200)
        in_list = NEW_REPO_NAME in page.locator("#repo_list").inner_text()
        record("E1-1 配置仓库成功", in_list, NEW_REPO_NAME)
        shot(page, "E1_1_repo_saved")
        # 2. 测试连接（保存后表单被 reset，需重新填地址）
        page.locator("#repo_url").fill(REPO_URL)
        page.locator("#repo_ref").fill("main")
        page.wait_for_timeout(200)
        page.locator("#repo_probe").click()
        page.wait_for_function("""() => {
          const t=document.querySelector('#repo_form_result');
          return t && /✅|❌/.test(t.textContent||'');
        }""", timeout=60000)
        probe_txt = page.locator("#repo_form_result").inner_text().strip()
        record("E1-2 测试连接成功", "✅" in probe_txt or "成功" in probe_txt, probe_txt[:120])
        # 3. 填到控制台
        filled = False
        rows = page.locator("#repo_list tr")
        for i in range(rows.count()):
            row = rows.nth(i)
            if NEW_REPO_NAME in row.inner_text():
                link = row.locator("a", has_text="填到控制台")
                if link.count() > 0:
                    link.first.click()
                    filled = True
                    break
        page.wait_for_timeout(500)
        record("E1-3 配置回填控制台", filled and REPO_URL in page.locator("#repo").input_value(),
               f"filled={filled}, repo={page.locator('#repo').input_value()[:50]}")
        # 4. 获取分支
        branches2 = load_branches(page)
        select_ref(page, "scan_ref", TARGET_REF)
        record("E1-4 获取分支成功", len(branches2) > 1, f"分支数={len(branches2)}")
        # 5. 扫描（远程 SSH 受凭证缺陷影响，如实记录降级）
        meta3 = scan(page)
        n3 = select_one_file(page)
        record("E1-5 远程只读扫描(受C2-0缺陷影响)", "文件" in meta3 and n3 > 0,
               f"meta={meta3[:80] or '空'}, 文件数={n3}（远程SSH scan 需PAT，见C2-0）")
        # 6. 生成 + 报告（不依赖勾选，走全量）
        state3, pid3, has_report3 = generate(page, "AUTO-P0-E1-FullChain")
        record("E1-6 完整串联生成成功", "success" in state3.lower() and pid3 > 0,
               f"state={state3}, pid={pid3}")
        record("E1-7 完整串联报告可查看", has_report3, f"pid={pid3}")
        shot(page, "E1_7_full_chain")

        # 清理本轮新建的仓库配置
        log("=== 清理测试仓库配置 ===")
        try:
            page.locator("#nav-repos").click()
            page.wait_for_timeout(400)
            rows = page.locator("#repo_list tr")
            for i in range(rows.count()):
                row = rows.nth(i)
                if NEW_REPO_NAME in row.inner_text():
                    link = row.locator("a", has_text="删除")
                    if link.count() > 0:
                        link.first.click()
                        break
            page.wait_for_timeout(500)
            log("    测试仓库已清理")
        except Exception as e:
            log(f"    清理跳过: {str(e)[:80]}")

        b.close()

    print("\n===== 生成的 project_id（供 P1 清理）=====", flush=True)
    print(",".join(str(x) for x in PROJECTS), flush=True)
    print("\n===== P0 汇总 =====", flush=True)
    for case, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'} | {case} | {detail}", flush=True)
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"总计 {len(results)} 条，通过 {passed}，失败 {len(results)-passed}", flush=True)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
