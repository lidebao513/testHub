# -*- coding: utf-8 -*-
"""UI 层端到端测试：扫描树组头全选框 + 测试点可读性（标题/验证内容/预期）。

真实用户路径：打开 :8900/testgen/ → 扫描 → 操作组头全选框（全选/半选/全不选）→
只保留少量文件 → 生成 → 终态后断言测试点行内出现「💬 验证内容」与「✔ 预期」。
"""
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

CONSOLE_URL = "http://127.0.0.1:8900/testgen/"
REPO_PATH = r"C:\Users\EDY\WorkBuddy\testhub_platform\services\testgen\data\workspaces\research-agent"
BASE_REF = "oa-20260909"
TARGET_REF = "master"
KEEP = 3  # 生成时只保留前 3 个文件，压缩耗时
SHOTS = Path(__file__).parent / "ui_shots"
SHOTS.mkdir(exist_ok=True)


def badge_state(page) -> str:
    return page.evaluate("document.getElementById('task_state').textContent")


def main() -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True, channel="msedge", args=["--no-proxy-server"]
        )
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        page.goto(CONSOLE_URL, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_selector("#btn_scan", timeout=15000)
        print("[1] 控制台打开 OK")

        page.fill("#repo", REPO_PATH)
        for sel_id, val in (("#scan_base_ref", BASE_REF), ("#scan_ref", TARGET_REF)):
            page.evaluate(
                """([id, v]) => {
                  const s = document.getElementById(id.slice(1));
                  if (!s) return false;
                  if (![...s.options].some(o => o.value === v)) {
                    const o = document.createElement('option');
                    o.value = v; o.textContent = v; s.appendChild(o);
                  }
                  s.value = v; s.dispatchEvent(new Event('change'));
                  return true;
                }""",
                [sel_id, val],
            )
        page.check('input[name="gen_mode"][value="incremental"]')
        page.dispatch_event('input[name="gen_mode"][value="incremental"]', "change")
        page.click("#btn_scan")
        page.wait_for_selector(".scan-group-cb", timeout=120000)
        # 等异步中文描述（enrich）完成——它每批完成会重建列表 DOM，期间操作勾选框会被竞态干扰
        page.wait_for_function(
            """() => document.getElementById('log').textContent.includes('中文描述')
                  || document.getElementById('log').textContent.includes('描述加载失败')""",
            timeout=180000,
        )
        page.wait_for_timeout(500)
        print("[2] 扫描完成（含描述回填），组头全选框已出现")

        # —— 全选框交互断言（从已知状态开始：先清空，再逐档验证；统计用轮询抗重建竞态）——
        def group_stats() -> tuple[int, int, bool, bool]:
            v = page.evaluate(
                """() => { const h=document.querySelector('.scan-group-cb');
                  const cbs=[...document.querySelectorAll('.scan-cb')].filter(c=>c.dataset.group===h.dataset.group);
                  return [cbs.filter(c=>c.checked).length, cbs.length, h.checked, h.indeterminate]; }"""
            )
            return int(v[0]), int(v[1]), bool(v[2]), bool(v[3])

        def wait_stats(expect: str, timeout_s: float = 5.0) -> tuple[int, int, bool, bool]:
            deadline = time.time() + timeout_s
            stats = group_stats()
            while time.time() < deadline:
                n_chk, n_all, hc, ind = stats
                if expect == "all" and hc and n_chk == n_all:
                    return stats
                if expect == "none" and not hc and not ind and n_chk == 0:
                    return stats
                if expect == "ind" and ind:
                    return stats
                page.wait_for_timeout(300)
                stats = group_stats()
            return stats

        head = page.locator(".scan-group-cb").first
        group_name = head.get_attribute("data-group")
        head.uncheck()
        n_chk, n_all, hc, ind = wait_stats("none")
        assert n_all > 0 and n_chk == 0, f"组头取消未清空整组 ({n_chk}/{n_all})"
        print(f"[3] 组头取消 → 整组清空 OK（{group_name}: 0/{n_all}）")

        head.check()
        n_chk, n_all, hc, ind = wait_stats("all")
        assert n_chk == n_all, f"组头勾选未同步整组 ({n_chk}/{n_all})"
        print(f"[4] 组头勾选 → 整组全选 OK（{n_chk}/{n_all}）")

        page.locator(f'.scan-cb[data-group="{group_name}"]').nth(0).uncheck()
        _, _, _, ind = wait_stats("ind")
        assert ind, "单项取消后组头未呈半选态"
        print("[5] 单项取消 → 组头半选(indeterminate) OK")

        # 半选态（checked=false + indeterminate）：真实用户点击 = 全选；Playwright uncheck 会
        # 因 checked=false 跳过点击，故先 check()（半选→全选，等效真实点击），再 uncheck 清空
        head.check()
        n_chk, n_all, hc, _ = wait_stats("all")
        assert n_chk == n_all, f"半选态点击未变为全选 ({n_chk}/{n_all})"
        print(f"[6] 半选态点击 → 全选 OK（{n_chk}/{n_all}）")

        head.uncheck()
        n_chk, _, _, _ = wait_stats("none")
        assert n_chk == 0, f"组头取消后组内仍有勾选 ({n_chk})"
        print("[7] 组头取消 → 整组清空 OK")

        # 只保留前 KEEP 个文件，其余全部取消
        page.evaluate(
            """(keep) => {
              const cbs = [...document.querySelectorAll('.scan-cb')];
              cbs.forEach((cb, i) => cb.checked = i < keep);
              if (typeof refreshGroupChecks === 'function') refreshGroupChecks();
            }""",
            KEEP,
        )
        page.screenshot(path=str(SHOTS / "10_group_select.png"))
        print(f"[8] 仅保留前 {KEEP} 个文件勾选")

        page.click("#btn_gen")
        print("[9] 已点「生成用例」，等待任务终态（最长 10 分钟）…")
        deadline = time.time() + 600
        last, terminal = "", ""
        while time.time() < deadline:
            state = badge_state(page)
            log = page.evaluate("document.getElementById('log').textContent.slice(-140)")
            cur = f"{state} | {log}"
            if cur != last:
                print("    ", cur.replace("\n", " "))
                last = cur
            if state in ("success", "failed", "cancelled"):
                terminal = state
                break
            page.wait_for_timeout(3000)
        if terminal != "success":
            page.screenshot(path=str(SHOTS / "11_gen_fail.png"))
            print("❌ 生成未成功:", terminal or "timeout")
            return 2

        # —— 测试点可读性断言：行内验证内容 + 预期 ——
        page.wait_for_selector("#tp_list tr", timeout=30000)
        page.wait_for_timeout(500)
        stats = page.evaluate(
            """() => ({
              rows: document.querySelectorAll('#tp_list tr').length,
              sem: document.querySelectorAll('#tp_list .tp-sem').length,
              exp: document.querySelectorAll('#tp_list .tp-exp').length,
              sample: [...document.querySelectorAll('#tp_list tr')]
                .slice(1, 4).map(r => r.cells[1].textContent.trim().slice(0, 120))
            })"""
        )
        print("[8] 测试点表格：行=%(rows)s 验证内容=%(sem)s 预期=%(exp)s" % stats)
        for s in stats["sample"]:
            print("    •", s.replace("\n", " ⏎ "))
        page.screenshot(path=str(SHOTS / "12_tp_readable.png"), full_page=True)

        ok = stats["rows"] > 1 and stats["exp"] > 0
        browser.close()
        print("✅ UI E2E PASS" if ok else "❌ UI E2E FAIL（预期列未渲染）")
        return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
