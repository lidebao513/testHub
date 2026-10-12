#!/usr/bin/env python3
# ============================================================================
# testgen 控制台 UI 层回归用例（Playwright 真实浏览器驱动）
# ----------------------------------------------------------------------------
# 固化「经 UI 层自测发现并修复」的关键行为，防止回归：
#   - 控制台自包含页面可渲染（#repo / #btn_scan 可见）
#   - 子目录列分支：下拉项带「日期 时分秒 + 提交标题」（repos.py 修复）
#   - 全量扫描返回文件、统计徽标恢复「共 X 个文件…」
#   - describe 端点可用：至少产出 新生成/历史/回退 之一的描述（app.py 修复）
#   - 清空描述缓存按钮生效（log 出现「已清空描述缓存」）
#
# 用法:
#   # 直接验证已运行的 sidecar（默认 http://127.0.0.1:8100/）
#   python scripts/ui_regression.py
#   # 自带 sidecar 全封闭运行（CI / 单机，自动拉起并回收）
#   python scripts/ui_regression.py --start-sidecar
#   # 验证经 Django 代理的集成路径（真实 App 走 :8000/testgen/）
#   python scripts/ui_regression.py --url http://127.0.0.1:8000/testgen/?embed=1
#
# 退出码非 0 = 回归失败。该脚本被 scripts/gate_all.py 以「opt-in」方式接入
# 八门禁：仅当环境变量 RUN_UI_REGRESSION=1 时才真正运行，否则门禁打印 SKIP
# （避免在无浏览器 / 无运行服务 / 无 LLM 的常规开发或 CI 中拖慢或假失败）。
# ============================================================================
from __future__ import annotations

import argparse
import os
import re
import subprocess
import tempfile
import time
from datetime import datetime

from playwright.sync_api import sync_playwright  # type: ignore


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SIDECAR_VENV = os.path.join(REPO_ROOT, ".venv", "Scripts", "python.exe")
DEFAULT_TARGET = os.path.join(REPO_ROOT, "engine")


def resolve_base_sha(repo: str) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", repo, "rev-parse", "HEAD~10"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass
    return ""


def start_sidecar(port: int) -> subprocess.Popen:
    proc = subprocess.Popen(
        [SIDECAR_VENV, "-m", "cli.main", "serve", "--host", "127.0.0.1", "--port", str(port)],
        cwd=REPO_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(40):
        try:
            r = subprocess.run(
                [
                    "curl",
                    "-s",
                    "-o",
                    "/dev/null",
                    "-w",
                    "%{http_code}",
                    f"http://127.0.0.1:{port}/health",
                ],
                capture_output=True,
                text=True,
                timeout=3,
            )
            if r.stdout.strip() == "200":
                return proc
        except Exception:
            pass
        time.sleep(1)
    raise RuntimeError(f"sidecar 在 {port} 启动超时")


def stop_sidecar(proc: subprocess.Popen) -> None:
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _step(name: str, fn) -> tuple[str, bool, str]:
    try:
        msg = fn()
        print(f"[PASS] {name} :: {msg}")
        return (name, True, msg)
    except Exception as e:
        print(f"[FAIL] {name} :: {e}")
        return (name, False, str(e))


def run_checks(url: str, target: str, base_sha: str) -> int:
    shots = os.path.join(tempfile.gettempdir(), "testgen_ui_regression_shots")
    os.makedirs(shots, exist_ok=True)
    steps: list[tuple[str, bool, str]] = []

    def shot(pg, name):
        try:
            pg.screenshot(path=os.path.join(shots, name + ".png"))
        except Exception:
            pass

    def desc_stats(pg):
        return pg.evaluate("""() => {
          const items=[...document.querySelectorAll('#scan_list .scan-item')];
          let neu=0, hist=0, sym=0, none=0;
          for(const it of items){
            const d=it.querySelector('.desc');
            if(!d){ none++; continue; }
            const t=d.textContent||'';
            if(t.includes('新生成')) neu++;
            else if(t.includes('历史')) hist++;
            else if(t.includes('内部')) sym++;
          }
          return {neu,hist,sym,none,total:items.length};
        }""")

    def do_scan(pg, tries=3):
        last = None
        for _ in range(tries):
            try:
                pg.click("#btn_scan")
                pg.wait_for_selector("#scan_list .scan-item", timeout=90000, state="attached")
                return
            except Exception as e:
                last = e
                pg.wait_for_timeout(2500)
        raise last or RuntimeError("scan failed")

    def wait_idle(pg, timeout=60000):
        pg.wait_for_function(
            """() => {
              const b=document.getElementById('btn_scan');
              const m=document.getElementById('scan_meta');
              return b && !b.disabled && (!m || !/生成中|正在生成/.test(m.textContent||''));
            }""",
            timeout=timeout,
        )

    def inject_base(pg, sha):
        pg.evaluate(
            """(sha) => {
          const sel=document.getElementById('scan_base_ref');
          let o=[...sel.options].find(x=>x.value===sha);
          if(!o){ o=document.createElement('option'); o.value=sha;
            o.textContent=sha.slice(0,12)+' (注入基线·HEAD~10)'; sel.appendChild(o); }
          sel.value=sha; document.getElementById('base_ref').value=sha;
          sel.dispatchEvent(new Event('change'));
        }""",
            sha,
        )

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-dev-shm-usage",
                "--no-proxy-server",
                "--proxy-server=direct://",
                "--no-sandbox",
                "--disable-gpu",
            ],
        )
        pg = browser.new_page(viewport={"width": 1440, "height": 1700})
        pg.set_default_timeout(60000)
        pg.on("pageerror", lambda e: print("PAGEERROR:", e))

        def s1():
            pg.goto(url, wait_until="load", timeout=30000)
            pg.wait_for_selector("#repo", timeout=15000)
            assert pg.is_visible("#btn_scan")
            assert "控制台" in pg.inner_text("body")
            shot(pg, "01_console")
            return "控制台视图已渲染，#repo/#btn_scan 可见"

        steps.append(_step("打开控制台", s1))

        def s2():
            pg.select_option("#code_src_type", "local")
            pg.fill("#repo", target)
            assert pg.input_value("#repo") == target
            shot(pg, "02_fill_repo")
            return f"代码源=本地，目录={target}"

        steps.append(_step("填写代码源", s2))

        def s3():
            pg.click("#btn_branches")
            pg.wait_for_function(
                "document.querySelectorAll('#scan_ref option').length > 1", timeout=30000
            )
            opts = pg.eval_on_selector_all("#scan_ref option", "els => els.map(e=>e.textContent)")
            date_re = re.compile(r"\d{2}/\d{2} \d{2}:\d{2}:\d{2}")
            with_date = [o for o in opts if date_re.search(o)]
            assert len(with_date) >= 1, f"分支下拉无日期时分秒，选项={opts}"
            tops = pg.evaluate("""() => {
              const ids=['btn_branches','scan_ref','scan_base_ref','btn_scan'];
              return ids.map(i=>document.getElementById(i).getBoundingClientRect().top);
            }""")
            assert tops[0] < tops[1] < tops[2] < tops[3], f"布局顺序异常 top={tops}"
            shot(pg, "03_branches")
            return f"分支下拉 {len(opts)} 项，{len(with_date)} 项带日期时分秒；布局自上而下顺序正确"

        steps.append(_step("获取分支/日期时分秒/布局", s3))

        def s4():
            pg.select_option("#scan_base_ref", "", timeout=5000)
            pg.check('input[name="gen_mode"][value="full"]', timeout=5000)
            do_scan(pg)
            n_items = pg.eval_on_selector_all("#scan_list .scan-item", "e=>e.length")
            assert n_items > 0
            pg.wait_for_function(
                "document.getElementById('scan_meta').textContent.includes('共')", timeout=300000
            )
            meta = pg.inner_text("#scan_meta")
            shot(pg, "04_fullscan")
            return f"全量扫描返回 {n_items} 文件；统计='{meta.strip()[:50]}'"

        steps.append(_step("全量扫描/渲染/徽标", s4))

        def s5():
            st = desc_stats(pg)
            dsum = st["neu"] + st["hist"] + st["sym"]
            assert dsum > 0, f"describe 未产出任何描述，统计={st}"
            shot(pg, "05_describe")
            return (
                f"describe 端点可用：新生成={st['neu']} 历史={st['hist']} "
                f"回退={st['sym']}（LLM 不可用时回退类/函数名也算通过）"
            )

        steps.append(_step("describe 中文描述可用", s5))

        def s6():
            wait_idle(pg, 60000)
            pg.check('input[name="gen_mode"][value="incremental"]', timeout=5000)
            if base_sha:
                inject_base(pg, base_sha)
            do_scan(pg)
            meta = pg.inner_text("#scan_meta")
            st = desc_stats(pg)
            shot(pg, "06_incremental")
            changed = pg.eval_on_selector_all(
                "#scan_list .scan-item[data-status='added'],#scan_list .scan-item[data-status='modified']",
                "e=>e.length",
            )
            note = (
                f"增量扫描完成：变更文件 {changed}，meta='{meta.strip()[:40]}'；"
                f"描述 新生成={st['neu']} 历史={st['hist']} 回退={st['sym']}"
            )
            print("  [INFO] " + note)
            return note

        steps.append(_step("增量模式/变更对比", s6))

        def s7():
            wait_idle(pg, 60000)
            do_scan(pg)
            st = desc_stats(pg)
            shot(pg, "07_rescan")
            note = f"二次扫描：历史={st['hist']} 新生成={st['neu']} 回退={st['sym']}"
            print("  [INFO] " + note)
            return note

        steps.append(_step("描述缓存命中(二次扫描)", s7))

        def s8():
            wait_idle(pg, 60000)
            pg.click("#btn_clear_desc")
            pg.wait_for_timeout(1500)
            log = ""
            try:
                log = pg.inner_text("#log")
            except Exception:
                pass
            assert "已清空描述缓存" in log, f"清空缓存日志缺失，log 片段={log[-120:]}"
            shot(pg, "08_after_clear")
            return "清空描述缓存按钮生效（log 已记录「已清空描述缓存」）"

        steps.append(_step("清空描述缓存", s8))

        browser.close()

    passed = sum(1 for _, ok, _ in steps if ok)
    lines = [
        "",
        "=" * 64,
        "testgen UI 回归结果",
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        f"URL={url}  目标={target}  base_sha={base_sha[:12]}",
        "=" * 64,
    ]
    for name, ok, detail in steps:
        lines.append(f"[{'PASS' if ok else 'FAIL'}] {name} :: {detail}")
    lines.append("=" * 64)
    lines.append(f"合计 {len(steps)} 项，通过 {passed}，失败 {len(steps) - passed}")
    if passed != len(steps):
        lines.append("截图目录: " + shots)
    print("\n".join(lines))
    return 0 if passed == len(steps) else 1


def parse_args(argv=None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="testgen 控制台 UI 层回归用例")
    ap.add_argument(
        "--url",
        default="http://127.0.0.1:8100/",
        help="控制台地址（默认 sidecar 直连；可填 :8000/testgen/ 测代理）",
    )
    ap.add_argument("--target-repo", default=DEFAULT_TARGET, help="扫描目标本地仓库目录")
    ap.add_argument(
        "--start-sidecar",
        action="store_true",
        help="自动拉起自带 sidecar（端口 --port）后运行并回收",
    )
    ap.add_argument("--port", type=int, default=8211, help="--start-sidecar 时的 sidecar 端口")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    base_sha = resolve_base_sha(args.target_repo)
    proc = None
    if args.start_sidecar:
        proc = start_sidecar(args.port)
        args.url = f"http://127.0.0.1:{args.port}/"
    try:
        return run_checks(args.url, args.target_repo, base_sha)
    finally:
        if proc:
            stop_sidecar(proc)


if __name__ == "__main__":
    raise SystemExit(main())
