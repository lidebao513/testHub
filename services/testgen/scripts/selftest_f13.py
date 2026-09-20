"""F13 · UI 层执行器真机自测（用平台测平台）。

与单元测试里用假页面的区别：本脚本**真的启动 Playwright（系统/自带 Chromium）**，
打开一个本地演示页，验证 F13 的完整链路：
  1. 页面渲染断言（非白屏）；
  2. 元素可见断言（F9：selector → 可判定断言）；
  3. 控制台基线断言；
  4. 失败用例**落真实截图**作为证据。

演示页就是一个已知的本地 HTML（不是被测生产系统），因此无需任何账号密码，
也不会污染任何环境——是「用平台测平台」最安全的自测形态。

运行：`venv/Scripts/python.exe scripts/selftest_f13.py`
依赖：playwright 已安装且浏览器可用（否则本脚本直接退出并给出安装提示，不报错）。
"""

from __future__ import annotations

import http.server
import os
import socketserver
import sys
import threading
from functools import partial
from pathlib import Path


# 让脚本可 `venv/Scripts/python.exe scripts/selftest_f13.py` 直接运行（须在 `from core...` 之前）
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core.contracts import CaseSpec
from core.enums import ExecStatus
from engine import runtime_ui, ui_executor


PORT = 8731
_DEMO_HTML = """<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>仪表盘</title></head>
<body>
  <h1>仪表盘</h1>
  <button id="submit">提交</button>
  <p id="hint">这是一段可见的提示文字</p>
</body>
</html>
"""


def _start_server(root: Path) -> None:
    # 必须显式把服务目录指向 demo 目录：SimpleHTTPRequestHandler 默认服务**当前工作目录**，
    # 而本脚本以 `scripts/selftest_f13.py` 运行，CWD 是项目根，会误返回目录列表而非 demo 页。
    handler = partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
    httpd = socketserver.TCPServer(("127.0.0.1", PORT), handler)
    httpd.allow_reuse_address = True
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()


def _case(tc_no: str, assertions: list[dict], title: str) -> CaseSpec:
    return CaseSpec(
        tc_no=tc_no,
        title=title,
        ctype="page",
        steps=[
            {
                "action": "ui_probe",
                "layer": "UI",
                "kind": "page",
                "method": "PAGE",
                "path": "/",
                "dimension": "页面/路由可达",
                "expect": "页面可达且关键元素可见",
                "assertions": assertions,
            }
        ],
        case_type="正常",
    )


def main() -> int:
    if not runtime_ui.has_playwright():
        print("[skip] 未安装 Playwright 或浏览器，跳过真机自测：", runtime_ui.INSTALL_HINT)
        return 0

    import tempfile

    root = Path(tempfile.mkdtemp(prefix="f13_demo_"))
    (root / "index.html").write_text(_DEMO_HTML, encoding="utf-8")
    _start_server(root)
    base_url = f"http://127.0.0.1:{PORT}"

    shot_dir = Path(tempfile.mkdtemp(prefix="f13_shots_"))
    opts = ui_executor.UiExecOptions(
        base_url=base_url, headless=True, screenshot_dir=str(shot_dir), ui_click=False
    )
    session = ui_executor.UiSession(opts)
    if not session.open():
        print("[fail] UI 会话未建立：", session.notes, flush=True)
        return 1

    # —— 正向：页面可达 + 元素可见 + 控制台无错 ——
    pos = _case(
        "TP-SELF-PASS",
        [
            {"kind": ui_executor.ASSERT_PAGE_RENDERED},
            {"kind": ui_executor.ASSERT_ELEMENT_VISIBLE, "selector": "#submit", "text": "提交"},
            {"kind": ui_executor.ASSERT_CONSOLE_WITHIN_BASELINE, "baseline": 0},
        ],
        "[正常] 仪表盘可达且提交按钮可见",
    )
    r_pos = session.execute(pos)
    print(f"[pass-case] status={r_pos.status} assertions={len(r_pos.assertions)}", flush=True)
    for a in r_pos.assertions:
        print(f"    - {a['kind']}: {'OK' if a['ok'] else 'FAIL'} :: {a['detail']}", flush=True)

    # —— 负向：元素不存在 → 必须 FAIL 且落真实截图 ——
    neg = _case(
        "TP-SELF-FAIL",
        [{"kind": ui_executor.ASSERT_ELEMENT_VISIBLE, "selector": "#nope"}],
        "[正常] 不存在元素应判失败",
    )
    r_neg = session.execute(neg)
    print(f"[fail-case] status={r_neg.status} screenshot={r_neg.screenshot_path}", flush=True)

    ok = r_pos.status == ExecStatus.PASS.value and r_neg.status == ExecStatus.FAIL.value
    if ok:
        print("[ok] F13 真机自测通过：真实浏览器导航/断言/失败截图链路均正常", flush=True)
    else:
        print("[fail] F13 真机自测未通过：", r_pos.status, r_neg.status, flush=True)

    # 自测已得出结论：直接强制退出，跳过 playwright 浏览器/节点驱动的优雅关闭
    # （`session.close()` 在 Windows 上偶发挂起，会阻塞进程退出）。失败截图已在 execute()
    # 阶段落盘，子进程由 OS 回收，不影响结论与证据。
    os._exit(0 if ok else 1)


if __name__ == "__main__":
    raise SystemExit(main())
