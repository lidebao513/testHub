"""批次 D 残留闭环（P3）：auto「自动开」分支的真浏览器端到端测试。

背景：auto 态「只有地址、无代码」分支的判定此前只有单测（stub 页面）覆盖；
真机 :3900 因有静态路由必然判「关」，无法触发开分支，且真点卡片有副作用。
本文件起一个**本地受控 SPA**：卡片点击只 `history.pushState` 改 URL（零副作用，
不改数据、不发请求），页面刻意不提供静态路由 / 菜单 / 首页 `<a>` / 显式 routes，
让 auto 判定「四来源全空 → 自动开」，从而真浏览器验证：

1. auto 自动开：audit.mode=auto / enabled=True / decision 含「兜底」；
2. 真点卡片采信路由：3 个导航卡片 URL 变化被采信，危险卡片被过滤不点，
   URL 不变的误报卡片记入 no_nav_labels；
3. 点击预算受 max_pages 约束 + 每次点击前回起点；
4. auto 有静态路由来源时自动关（零点击）——同一夹具对照，证明系统按实际自动开关；
5. 完整闭环：发现的路由 → case_gen 生成用例 → ui_executor 真执行 PASS。

复用批次 C 已真机验证的 `_click_and_read_route` 点击链路，不新增点击实现。
"""

from __future__ import annotations

import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock, Thread
from typing import Any
from urllib.parse import urlparse

import pytest

from core.contracts import CaseSpec
from core.enums import ExecStatus
from core.errors import EngineError
from engine import case_gen, runtime_ui, ui_executor
from engine.ui_executor import UiExecOptions, UiSession


# 受控 SPA：只有地址、没有代码可分析的路由声明。
# 卡片点击 = history.pushState 改 pathname（模拟 Vue router.push），不发网络请求、不改数据。
# 刻意不放 <a href> / nav / menu-item / data-route，保证「四来源全空」。
# 含 1 个危险卡片（"删除任务" → JS 侧过滤）和 1 个误报卡片（点击 URL 不变）。
_CARD_SPA_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>卡片 SPA 夹具</title>
<style>
  body { font-family: sans-serif; padding: 24px; }
  .card { display: block; padding: 16px; margin: 8px 0; border: 1px solid #ccc;
          cursor: pointer; border-radius: 6px; }
</style></head><body>
<main id="workspace">
  <h1>工作台</h1>
  <div id="card-cases" class="card" onclick="go('/cases')">AI用例生成</div>
  <div id="card-api" class="card" onclick="go('/api-test')">接口测试</div>
  <div id="card-ui" class="card" onclick="go('/ui-auto')">UI自动化测试</div>
  <div id="card-more" class="card" onclick="noop()">展开更多</div>
  <div id="card-del" class="card" onclick="go('/danger')">删除任务</div>
</main>
<script>
  function go(p) { history.pushState({}, '', p); }
  function noop() { /* URL 不变：启发式噪声 */ }
</script>
</body></html>"""

# 请求记录（跨线程共享，测试间清空）
_requests: list[str] = []
_requests_lock = Lock()


class _CardSpaHandler(BaseHTTPRequestHandler):
    """所有路径返回同一份 SPA HTML（history 路由 fallback），并记录请求路径。"""

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        with _requests_lock:
            _requests.append(path)
        body = _CARD_SPA_HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:  # 静音访问日志
        return None


@pytest.fixture(autouse=True)
def _chromium_no_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    """本模块真浏览器用例在受限环境跑，局部关闭 Chromium 沙箱（不动全局环境）。

    只在测试进程内生效、结束自动恢复，避免污染
    `testlaunch_browser_passes_channel...` 等依赖默认 args 的断言。
    """
    monkeypatch.setenv("PLAYWRIGHT_CHROMIUM_SANDBOX", "0")


@pytest.fixture()
def card_spa() -> str:
    """起本地受控 SPA，返回基址；每个用例前清空请求记录。"""
    with _requests_lock:
        _requests.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _CardSpaHandler)
    Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def _options(base: str, **kw: object) -> runtime_ui.RuntimeUiOptions:
    opts = runtime_ui.RuntimeUiOptions(
        base_url=base,
        timeout=1,  # 本地 server 足够；_wait_nav_ready 不会空等太久
        max_pages=10,
        discover_by_menu=False,  # 夹具无菜单，跳过等待；auto 判定的 menu_paths 仍为 []
        card_nav_mode="auto",
        channel=os.environ.get("PLAYWRIGHT_CHANNEL", ""),
    )
    for key, value in kw.items():
        setattr(opts, key, value)
    return opts


def _discover(
    base: str, *, static: list[str] | None = None, **kw: object
) -> runtime_ui.RuntimeUiResult:
    pytest.importorskip("playwright")
    try:
        return runtime_ui.discover_ui(_options(base, **kw), static_paths=static)
    except EngineError as exc:  # 内核缺失等环境问题 → 跳过而非失败
        pytest.skip(f"浏览器不可用：{exc}")
    raise AssertionError("unreachable")  # pragma: no cover


# ============================================================================
# 1. auto 自动开：四来源全空 → 真点卡片 → 采信路由
# ============================================================================
def test_auto_opens_and_discovers_card_routes(card_spa: str) -> None:
    """核心闭环：auto 判开 + 真点卡片采信 3 条路由 + 危险过滤 + 误报记录。"""
    result = _discover(card_spa)
    audit = result.card_nav_audit

    assert audit["mode"] == "auto"
    assert audit["enabled"] is True
    assert "兜底" in audit["decision"] and "均为空" in audit["decision"]

    # JS 侧已过滤危险卡片（"删除任务"），剩 4 个候选：3 导航 + 1 误报
    assert audit["candidates"] == 4
    assert audit["clicked"] == 4
    assert audit["url_changed"] == 3
    # 3 个导航卡片的 URL 变化被采信；危险卡片未点，其 /danger 不出现
    assert set(audit["routes"]) == {"/cases", "/api-test", "/ui-auto"}
    assert "/danger" not in audit["routes"]
    # URL 不变的误报被如实记录、不臆造为路由
    assert "展开更多" in audit["no_nav_labels"]

    # 判定理由 + 四来源计数 + 审计明细沉淀到报告 notes，全程可追溯
    digest = [n for n in result.notes if n.startswith("[卡片导航 P3]")]
    assert len(digest) == 3
    assert "模式=auto" in digest[0] and "启用" in digest[0] and "兜底" in digest[0]
    assert "判定来源计数" in digest[1] and "首页链接 0 条" in digest[1]
    assert "候选 4" in digest[2] and "确认路由 3" in digest[2] and "误报 1" in digest[2]
    # 四来源结构化计数写进审计，供多 SPA 校准
    assert audit["sources"] == {
        "显式路由": 0,
        "菜单发现": 0,
        "静态路由": 0,
        "首页链接": 0,
    }


def test_auto_open_returns_to_start_before_each_click(card_spa: str) -> None:
    """四道护栏③：每次点击前回起点；预算 ≤ max_pages。"""
    result = _discover(card_spa, max_pages=2)
    audit = result.card_nav_audit
    # 预算截断到 2（硬上限 12 之前先受 max_pages 约束）
    assert audit["clicked"] == 2
    assert len(audit["routes"]) <= 2

    # 回起点：每次点击前都 goto 基址（"/" 至少 landing + 2 次回起点）
    with _requests_lock:
        root_hits = sum(1 for p in _requests if p == "/")
    assert root_hits >= 3, f"应每次点击前回起点，实际根路径请求 {root_hits} 次"


# ============================================================================
# 2. 对照：auto 有静态路由来源 → 自动关（零点击，零副作用）
# ============================================================================
def test_auto_closes_when_static_routes_exist(card_spa: str) -> None:
    """同一夹具加一条静态路由 → auto 判关，卡片零点击。"""
    result = _discover(card_spa, static=["/cases"])
    audit = result.card_nav_audit
    assert audit["mode"] == "auto"
    assert audit["enabled"] is False
    assert "静态路由" in audit["decision"] and "跳过卡片探测" in audit["decision"]
    assert audit["clicked"] == 0
    assert audit["routes"] == []


# ============================================================================
# 3. 完整闭环：发现的路由 → 生成用例 → 真执行 PASS
# ============================================================================
def _tp_for(area: str, base: str) -> dict[str, Any]:
    return {
        "tp_id": "TP-card-e2e",
        "fp_contract_id": "FP-card-e2e",
        "category": "正常",
        "method": "PAGE",
        "area": area,
        "source": f"runtime:{base}{area}",
        "module": "卡片工作台",
        "title": f"[正常] {area} 页面可达",
        "expect": "页面正常渲染且无新增控制台错误",
        "dimension": "页面/路由可达",
        "verify_layer": "UI",
    }


def test_discovered_route_generates_and_executes_pass(card_spa: str) -> None:
    """发现侧真点出来的路由，喂给生成侧产出用例，执行侧真浏览器跑通。"""
    result = _discover(card_spa)
    assert "/cases" in result.card_nav_audit["routes"]

    index = runtime_ui.to_runtime_index(result)
    assert "/cases" in index, "发现的路由必须进入运行时索引"

    case = case_gen.build_case(_tp_for("/cases", card_spa), runtime_index=index)
    assert case is not None
    assert isinstance(case, CaseSpec)
    step = case.steps[0]
    assert ui_executor.target_url(step, UiExecOptions(base_url=card_spa)) == f"{card_spa}/cases"

    session = UiSession(UiExecOptions(base_url=card_spa, timeout=1))
    try:
        assert session.open() is True
        executed = session.execute(case)
    finally:
        session.close()
    assert executed.status == ExecStatus.PASS.value, (
        f"发现→生成→执行应通过，实际 {executed.status}：{executed.notes}"
    )
