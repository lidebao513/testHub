"""P3 运行时 UI 发现测试（M3.1 配置/探测 + M3.2 登录与单页抓取）。

分两层：
1. **无浏览器**（必跑）：路由优先级 / 同源过滤、登录选择器识别与失败分类、
   单页抓取与控制台错误切片、错误包装 —— 全部用桩对象驱动，秒级完成；
2. **真浏览器端到端**（有 playwright 才跑）：本地起迷你站点（登录页 + 受保护页），
   断言 `discover_ui` 能登录并发现「只在登录后可见」的页面。
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import ClassVar
from urllib.parse import urlparse

import pytest

from core.config import load_settings
from core.contracts import FunctionalPoint
from core.enums import Dimension, FType
from core.errors import ConfigError, EngineError
from engine import runtime_ui
from engine.runtime_ui import (
    _PASSWORD_SELECTORS,
    _SUBMIT_SELECTORS,
    _USER_SELECTORS,
)


_SECRET_PASSWORD = "S3cret-PW-xyz"
_OTP_CODE = "123456"  # 测试用假动态口令（真实口令绝不入库）


# ============================================================================
# 桩对象：不依赖 Playwright 也能验证页面交互逻辑
# ============================================================================
class _StubElement:
    def __init__(self) -> None:
        self.value: str | None = None
        self.clicks = 0

    def fill(self, value: str) -> None:
        self.value = value

    def click(self) -> None:
        self.clicks += 1


class _StubLoginPage:
    """模拟登录页。

    `present` 控制页面上「存在」的控件（user / pwd / submit）；
    `password_after` 控制提交后是否仍停留在登录页（用于验证失败判定）。
    """

    def __init__(
        self,
        *,
        present: tuple[str, ...] = ("user", "pwd", "submit"),
        password_after: bool = False,
        goto_error: Exception | None = None,
    ) -> None:
        self.present = present
        self.password_after = password_after
        self.goto_error = goto_error
        self.url = "http://srv/login"
        self.url_after = "http://srv/dashboard"
        self.visited: list[str] = []
        self.submitted = False
        self.user = _StubElement()
        self.pwd = _StubElement()
        self.submit = _StubElement()

    def goto(self, url: str, wait_until: str | None = None, timeout: int | None = None) -> None:
        self.visited.append(url)
        if self.goto_error is not None:
            raise self.goto_error
        self.url = url

    def query_selector(self, selector: str) -> _StubElement | None:
        if selector in _PASSWORD_SELECTORS:
            if "pwd" not in self.present:
                return None
            return None if (self.submitted and not self.password_after) else self.pwd
        if selector in _USER_SELECTORS:
            return self.user if "user" in self.present else None
        if selector in _SUBMIT_SELECTORS:
            return self.submit if "submit" in self.present else None
        return None

    def wait_for_load_state(self, state: str | None = None, timeout: int | None = None) -> None:
        self.submitted = True
        self.url = self.url_after


class _StubCrawlPage:
    """模拟被抓取的页面：evaluate 按脚本特征返回元素或链接。"""

    def __init__(
        self,
        *,
        elements: list[dict[str, object]] | None = None,
        links: list[str] | None = None,
        title: str = "首页",
        goto_error: Exception | None = None,
    ) -> None:
        self.url = "http://srv/"
        self._elements = elements or []
        self._links = links or []
        self._title = title
        self.goto_error = goto_error
        self.goto_calls: list[str] = []

    def goto(self, url: str, wait_until: str | None = None, timeout: int | None = None) -> None:
        self.goto_calls.append(url)
        if self.goto_error is not None:
            raise self.goto_error
        self.url = url

    def wait_for_load_state(self, state: str | None = None, timeout: int | None = None) -> None:
        return None

    def title(self) -> str:
        return self._title

    def evaluate(self, script: str) -> list[object]:
        if "querySelectorAll('a[href]')" in script:
            return list(self._links)
        return list(self._elements)


class _StubBrowserLauncher:
    """模拟 playwright 对象（chromium.launch）。"""

    def __init__(self, exc: Exception | None = None) -> None:
        self.chromium = self
        self._exc = exc
        self.kwargs: dict[str, object] | None = None

    def launch(self, **kwargs: object) -> str:
        self.kwargs = kwargs
        if self._exc is not None:
            raise self._exc
        return "BROWSER"


class _StubContext:
    def __init__(self) -> None:
        self.headers: dict[str, str] | None = None

    def set_extra_http_headers(self, headers: dict[str, str]) -> None:
        self.headers = headers


def _opts(**kw: object) -> runtime_ui.RuntimeUiOptions:
    base: dict[str, object] = {"base_url": "http://srv", "login_user": "u", "login_password": "p"}
    base.update(kw)
    return runtime_ui.RuntimeUiOptions(**base)  # type: ignore[arg-type]


# ============================================================================
# M3.1 可用性探测与配置映射
# ============================================================================
def testhas_playwright_returns_bool() -> None:
    assert isinstance(runtime_ui.has_playwright(), bool)


def test_options_from_settings_maps_all_fields() -> None:
    class _S:
        playwright_headless: ClassVar[bool] = False
        runtime_base_url: ClassVar[str] = "http://srv:9001/login"
        runtime_auth_token: ClassVar[str] = "TOK"
        runtime_ui_timeout: ClassVar[int] = 15
        playwright_channel: ClassVar[str] = "msedge"
        runtime_login_user: ClassVar[str] = "tester"
        runtime_login_password: ClassVar[str] = _SECRET_PASSWORD
        runtime_login_url: ClassVar[str] = "http://srv:9001/login"
        runtime_routes: ClassVar[list[str]] = ["/pc/tasks"]
        runtime_max_pages: ClassVar[int] = 7
        runtime_ui_mobile_enabled: ClassVar[bool] = True

    opts = runtime_ui.options_from_settings(_S())
    assert opts.base_url == "http://srv:9001/login"
    assert opts.headless is False
    assert opts.timeout == 15
    assert opts.channel == "msedge"
    assert opts.login_user == "tester"
    assert opts.login_password == _SECRET_PASSWORD
    assert opts.routes == ["/pc/tasks"]
    assert opts.max_pages == 7
    assert opts.mobile_enabled is True


def test_public_dict_hides_password_and_token() -> None:
    """凭证红线：public_dict 只输出「是否已配置」，绝不输出取值。"""
    s = load_settings(
        {
            "RUNTIME_UI_ENABLED": "on",
            "RUNTIME_BASE_URL": "http://srv:9001",
            "RUNTIME_LOGIN_USER": "tester",
            "RUNTIME_LOGIN_PASSWORD": _SECRET_PASSWORD,
            "RUNTIME_AUTH_TOKEN": "TOK-abc",
        }
    )
    dumped = repr(s.public_dict())
    assert _SECRET_PASSWORD not in dumped
    assert "TOK-abc" not in dumped
    assert s.public_dict()["runtime_login_configured"] is True
    assert s.public_dict()["runtime_token_configured"] is True


def test_settings_reject_runtime_ui_without_base_url() -> None:
    with pytest.raises(ConfigError, match="RUNTIME_BASE_URL"):
        load_settings({"RUNTIME_UI_ENABLED": "on"})


# ============================================================================
# M3.2 路由解析
# ============================================================================
def test_normalize_route_rejects_foreign_and_pseudo_schemes() -> None:
    base = "http://srv:9001/user/login"
    assert runtime_ui._normalize_route("/pc/tasks", base) == "/pc/tasks"
    assert runtime_ui._normalize_route("/pc/tasks?page=1", base) == "/pc/tasks?page=1"
    assert runtime_ui._normalize_route("http://srv:9001/pc/x", base) == "/pc/x"
    assert runtime_ui._normalize_route("http://other/x", base) is None
    assert runtime_ui._normalize_route("javascript:void(0)", base) is None
    assert runtime_ui._normalize_route("mailto:a@b.c", base) is None
    assert runtime_ui._normalize_route("#tab", base) is None
    assert runtime_ui._normalize_route("   ", base) is None


def test_resolve_routes_priority_and_dedupe() -> None:
    opts = _opts(routes=["/explicit"], max_pages=10)
    got = runtime_ui._resolve_routes(
        opts,
        ["/static", "/explicit"],
        ["/home", "/static", "http://elsewhere/x", "javascript:void(0)"],
    )
    assert got == ["/explicit", "/static", "/home"]


def test_resolve_routes_respects_max_pages() -> None:
    opts = _opts(max_pages=2)
    assert runtime_ui._resolve_routes(opts, ["/a", "/b", "/c"]) == ["/a", "/b"]


# ============================================================================
# M3.2 登录
# ============================================================================
def test_login_skipped_without_credentials() -> None:
    result = runtime_ui.RuntimeUiResult(base_url="http://srv")
    page = _StubLoginPage()
    opts = runtime_ui.RuntimeUiOptions(base_url="http://srv")
    assert runtime_ui._login(page, opts, result) is False
    assert page.visited == []  # 无凭证不应打开登录页
    assert any("未提供账号密码" in n for n in result.notes)


def test_login_success_fills_and_submits() -> None:
    result = runtime_ui.RuntimeUiResult(base_url="http://srv")
    page = _StubLoginPage()
    opts = _opts(login_password=_SECRET_PASSWORD)
    assert runtime_ui._login(page, opts, result) is True
    assert page.user.value == "u"
    assert page.pwd.value == _SECRET_PASSWORD
    assert page.submit.clicks == 1
    joined = " ".join(result.notes)
    assert "表单登录成功" in joined
    assert _SECRET_PASSWORD not in joined  # 红线：结果不回显密码


def test_login_prefers_configured_login_url() -> None:
    result = runtime_ui.RuntimeUiResult(base_url="http://srv")
    page = _StubLoginPage()
    runtime_ui._login(page, _opts(login_url="http://srv/user/login"), result)
    assert page.visited == ["http://srv/user/login"]


def test_login_fails_when_still_on_login_page() -> None:
    result = runtime_ui.RuntimeUiResult(base_url="http://srv")
    page = _StubLoginPage(password_after=True)
    with pytest.raises(EngineError) as ei:
        runtime_ui._login(page, _opts(login_password=_SECRET_PASSWORD), result)
    assert "仍停留在登录页" in str(ei.value)
    assert _SECRET_PASSWORD not in str(ei.value)


def test_login_without_password_field_is_public_page() -> None:
    result = runtime_ui.RuntimeUiResult(base_url="http://srv")
    page = _StubLoginPage(present=())
    assert runtime_ui._login(page, _opts(), result) is False
    assert any("未发现密码输入框" in n for n in result.notes)


def test_login_missing_user_or_submit_raises() -> None:
    result = runtime_ui.RuntimeUiResult(base_url="http://srv")
    with pytest.raises(EngineError, match="未定位到账号输入框"):
        runtime_ui._login(_StubLoginPage(present=("pwd", "submit")), _opts(), result)
    with pytest.raises(EngineError, match="未定位到账号输入框"):
        runtime_ui._login(_StubLoginPage(present=("user", "pwd")), _opts(), result)


def test_login_goto_error_wrapped_as_engine_error() -> None:
    result = runtime_ui.RuntimeUiResult(base_url="http://srv")
    page = _StubLoginPage(goto_error=RuntimeError("conn refused"))
    with pytest.raises(EngineError, match="无法打开登录页"):
        runtime_ui._login(page, _opts(), result)


def test_apply_token_sets_header_without_recording_value() -> None:
    ctx = _StubContext()
    result = runtime_ui.RuntimeUiResult()
    runtime_ui._apply_token(ctx, runtime_ui.RuntimeUiOptions(auth_token="TOK-abc"), result)
    assert ctx.headers == {"Authorization": "Bearer TOK-abc"}
    assert "TOK-abc" not in " ".join(result.notes)


def test_apply_token_skipped_when_absent() -> None:
    ctx = _StubContext()
    runtime_ui._apply_token(ctx, runtime_ui.RuntimeUiOptions(), runtime_ui.RuntimeUiResult())
    assert ctx.headers is None


# ============================================================================
# M3.2 单页抓取
# ============================================================================
def _elements() -> list[dict[str, object]]:
    return [
        {"selector": "#nav", "kind": "nav", "text": "任务", "visible": True},
        {"selector": "input.x", "kind": "input", "text": "", "visible": False},
        {"selector": "", "kind": "input", "text": "无选择器应被丢弃", "visible": True},
    ]


def test_collect_page_parses_elements_and_drops_empty_selector() -> None:
    page = _StubCrawlPage(elements=_elements(), title="工作台")
    sink = runtime_ui.ConsoleSink()
    ui_page = runtime_ui._collect_page(page, "http://srv/pc/tasks", _opts(), sink, 0)
    assert ui_page.reachable is True
    assert ui_page.path == "/pc/tasks"
    assert ui_page.title == "工作台"
    assert [e.selector for e in ui_page.elements] == ["#nav", "input.x"]
    assert ui_page.elements[0].visible is True


def test_collect_page_marks_unreachable_on_goto_error() -> None:
    page = _StubCrawlPage(goto_error=RuntimeError("net down"))
    sink = runtime_ui.ConsoleSink()
    ui_page = runtime_ui._collect_page(page, "http://srv/x", _opts(), sink, 0)
    assert ui_page.reachable is False
    assert any("[goto]" in e for e in ui_page.console_errors)


def test_collect_page_slices_console_errors_since_mark() -> None:
    sink = runtime_ui.ConsoleSink()
    sink.add("old-1")
    sink.add("old-2")
    page = _StubCrawlPage(elements=_elements())
    sink.add("new-1")
    ui_page = runtime_ui._collect_page(page, "http://srv/x", _opts(), sink, 2)
    assert ui_page.console_errors == ["new-1"]


def test_console_sink_caps_total_entries() -> None:
    sink = runtime_ui.ConsoleSink()
    for i in range(600):
        sink.add(f"e{i}")
    assert len(sink.entries) == 500
    assert sink.since(499) == ["e499"]


def test_crawl_links_reads_anchor_hrefs() -> None:
    page = _StubCrawlPage(links=["/a", "/b"])
    assert runtime_ui._crawl_links(page) == ["/a", "/b"]


def test_extract_js_embeds_single_source_selectors() -> None:
    """选择器/上限来自模块常量，禁止在 JS 里另写一份（防漂移）。"""
    js = runtime_ui._build_extract_js()
    # json.dumps 会在 JS 字面量里转义双引号，故比对转义后的形态
    assert json.dumps(runtime_ui._NAV_SELECTOR)[1:-1] in js
    assert json.dumps(runtime_ui._BUTTON_SELECTOR)[1:-1] in js
    assert json.dumps(runtime_ui._FORM_SELECTOR)[1:-1] in js
    assert str(runtime_ui.MAX_ELEMENTS_PER_PAGE) in js
    for token in ("__NAV__", "__BTN__", "__FORM__", "__MAX__"):
        assert token not in js
    assert str(runtime_ui.MAX_HOME_LINKS) in runtime_ui._build_links_js()


def test_spa_without_anchor_tags_is_still_covered() -> None:
    """实测回归：目标站整站 **0 个 `<a>`**，导航是带类名的 div。

    若选择器只认 `<a>`，这类 SPA 会「一个导航都抓不到」——本条锁死该覆盖。
    """
    nav = runtime_ui._NAV_SELECTOR
    assert '[class*="nav-item"]' in nav
    assert '[class*="menu-item"]' in nav
    assert '[class*="btn"]' in runtime_ui._BUTTON_SELECTOR
    assert "contenteditable" in runtime_ui._FORM_SELECTOR
    links_js = runtime_ui._build_links_js()
    for attr in ("data-href", "data-url", "data-path", "data-route"):
        assert attr in links_js


def testlaunch_browser_passes_channel_and_wraps_error() -> None:
    pw = _StubBrowserLauncher()
    assert runtime_ui.launch_browser(pw, _opts(channel="msedge")) == "BROWSER"
    assert pw.kwargs == {"headless": True, "channel": "msedge"}
    with pytest.raises(EngineError, match="浏览器启动失败"):
        runtime_ui.launch_browser(
            _StubBrowserLauncher(exc=RuntimeError("no browser")), runtime_ui.RuntimeUiOptions()
        )


def test_discover_ui_requires_base_url_before_playwright_check() -> None:
    with pytest.raises(EngineError, match="RUNTIME_BASE_URL"):
        runtime_ui.discover_ui(runtime_ui.RuntimeUiOptions())


def test_discover_ui_rejects_unknown_mode() -> None:
    with pytest.raises(ValueError, match="未知的运行时 UI 发现模式"):
        runtime_ui.discover_ui(runtime_ui.RuntimeUiOptions(mode="nope", base_url="http://srv"))


# ============================================================================
# 真浏览器端到端（本地迷你站点：登录页 + 受保护页）
# ============================================================================
_LOGIN_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>登录</title></head>
<body><div id="app">
<form id="f" onsubmit="return doLogin(event)">
  <input type="text" name="username" placeholder="账号">
  <input type="password" name="password" placeholder="密码">
  <button type="submit">登录</button>
</form>
<script>
function doLogin(e) {
  e.preventDefault();
  var u = document.querySelector('input[name=username]').value;
  if (u === 'good') { window.location.href = '/dashboard'; }
  return false;
}
</script>
</div></body></html>"""

_DASHBOARD_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>工作台</title></head>
<body><nav><a href="/pc/tasks">任务管理</a><a href="/pc/secret">仅登录可见</a></nav>
<div id="main">工作台内容</div></body></html>"""

_TASKS_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>任务列表</title></head>
<body><h1>任务列表</h1><form><input type="text" name="kw"><button type="submit">查询</button></form>
</body></html>"""

_SECRET_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>隐藏页</title></head>
<body><h1>仅登录可见</h1><button id="do">执行</button></body></html>"""


class _SiteHandler(BaseHTTPRequestHandler):
    pages: ClassVar[dict[str, str]] = {}

    def do_GET(self) -> None:
        html = self.pages.get(urlparse(self.path).path)
        if html is None:
            self.send_error(404)
            return
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:  # 静音访问日志
        return None


@pytest.fixture()
def mini_site() -> str:
    """起一个本地迷你站点，返回其源地址（http://127.0.0.1:<port>）。"""
    _SiteHandler.pages = {
        "/login": _LOGIN_HTML,
        "/dashboard": _DASHBOARD_HTML,
        "/pc/tasks": _TASKS_HTML,
        "/pc/secret": _SECRET_HTML,
    }
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SiteHandler)
    Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def _browser_options(base: str, **kw: object) -> runtime_ui.RuntimeUiOptions:
    opts = runtime_ui.RuntimeUiOptions(
        base_url=f"{base}/login",
        login_url=f"{base}/login",
        login_user="good",
        login_password=_SECRET_PASSWORD,
        timeout=15,
        channel=os.environ.get("PLAYWRIGHT_CHANNEL", ""),
        max_pages=10,
    )
    for key, value in kw.items():
        setattr(opts, key, value)
    return opts


def test_discover_ui_end_to_end_finds_protected_pages(mini_site: str) -> None:
    """核心验收：真浏览器登录后，能发现「只在登录后才可见」的页面。"""
    pytest.importorskip("playwright")
    try:
        result = runtime_ui.discover_ui(_browser_options(mini_site))
    except EngineError as exc:  # 内核缺失等环境问题 → 跳过而非失败
        pytest.skip(f"浏览器不可用：{exc}")

    paths = {p.path for p in result.pages}
    assert "/dashboard" in paths, f"未抓到登录后落地页，实际：{sorted(paths)}"
    assert "/pc/secret" in paths, f"未发现受保护页，实际：{sorted(paths)}"
    assert any(p.reachable for p in result.pages)
    assert result.base_url == f"{mini_site}/login"
    assert any("表单登录成功" in n for n in result.notes)
    kinds = {e.kind for e in result.elements}
    assert "nav" in kinds
    assert kinds & {"input", "button", "form"}
    assert _SECRET_PASSWORD not in repr(result)  # 红线


def test_discover_ui_end_to_end_detects_login_failure(mini_site: str) -> None:
    """登录失败（账号不存在）必须显式报错，而不是静默继续。"""
    pytest.importorskip("playwright")
    opts = _browser_options(mini_site, login_user="nobody")
    try:
        runtime_ui.discover_ui(opts)
    except EngineError as exc:
        if "仍停留在登录页" in str(exc):
            return
        pytest.skip(f"浏览器不可用：{exc}")
    pytest.fail("账号不存在时未报登录失败")


def test_discover_ui_end_to_end_no_credentials_public_pages(mini_site: str) -> None:
    """无凭证时按匿名访问（M3.2 验收口径：无登录公开页先打通）。"""
    pytest.importorskip("playwright")
    opts = _browser_options(mini_site, login_user="", login_password="")
    try:
        result = runtime_ui.discover_ui(opts)
    except EngineError as exc:
        pytest.skip(f"浏览器不可用：{exc}")
    assert any(p.reachable for p in result.pages)
    assert any("未提供账号密码" in n for n in result.notes)


def test_repo_has_no_plaintext_password_in_tracked_files() -> None:
    """红线自检：仓库中不得出现真实环境凭证。"""
    root = Path(__file__).resolve().parents[1]
    needle = "Taiping" + "@"
    for path in root.rglob("*.py"):
        if "venv" in path.parts:
            continue
        assert needle not in path.read_text(encoding="utf-8", errors="ignore")


# ============================================================================
# M3.3 三字段登录（账号 / 密码 / 动态口令）
# ============================================================================
class _StubOtpLoginPage(_StubLoginPage):
    """登录页带动态口令框（福享 Agent 形态：账号 + 密码 + 动态口令（6 位））。"""

    def __init__(self, **kw: object) -> None:
        super().__init__(**kw)  # type: ignore[arg-type]
        self.otp = _StubElement()

    def query_selector(self, selector: str) -> _StubElement | None:
        if selector in runtime_ui._OTP_SELECTORS:
            return self.otp if "otp" in self.present else None
        return super().query_selector(selector)


def test_login_fills_three_fields_including_otp() -> None:
    result = runtime_ui.RuntimeUiResult(base_url="http://srv")
    page = _StubOtpLoginPage(present=("user", "pwd", "otp", "submit"))
    opts = _opts(login_password=_SECRET_PASSWORD, login_otp=_OTP_CODE)
    assert runtime_ui._login(page, opts, result) is True
    assert page.user.value == "u"
    assert page.pwd.value == _SECRET_PASSWORD
    assert page.otp.value == _OTP_CODE
    assert page.submit.clicks == 1
    joined = " ".join(result.notes)
    assert _OTP_CODE not in joined  # 红线：动态口令不回显
    assert _SECRET_PASSWORD not in joined


def test_login_leaves_otp_empty_when_not_configured() -> None:
    result = runtime_ui.RuntimeUiResult(base_url="http://srv")
    page = _StubOtpLoginPage(present=("user", "pwd", "otp", "submit"))
    runtime_ui._login(page, _opts(login_password=_SECRET_PASSWORD), result)
    assert page.otp.value is None


def test_fill_otp_if_present_returns_false_without_field() -> None:
    page = _StubOtpLoginPage(present=("user", "pwd", "submit"))
    assert runtime_ui._fill_otp_if_present(page, _opts(login_otp=_OTP_CODE)) is False


def test_login_second_step_otp_submits_again() -> None:
    """两屏登录：首屏无口令框，首屏提交后才出现 → 触发二次提交。"""

    class _TwoStepPage(_StubLoginPage):
        def __init__(self) -> None:
            super().__init__(present=("user", "pwd", "submit"))
            self.otp = _StubElement()

        def query_selector(self, selector: str) -> _StubElement | None:
            if selector in runtime_ui._OTP_SELECTORS:
                return self.otp if self.submitted else None
            if selector in _PASSWORD_SELECTORS:
                if not self.submitted:
                    return self.pwd
                # 第二次提交后才离开登录页（否则触发二次提交）
                return None if self.submit.clicks >= 2 else self.pwd
            return super().query_selector(selector)

    result = runtime_ui.RuntimeUiResult(base_url="http://srv")
    page = _TwoStepPage()
    opts = _opts(login_password=_SECRET_PASSWORD, login_otp=_OTP_CODE)
    assert runtime_ui._login(page, opts, result) is True
    assert page.otp.value == _OTP_CODE
    assert page.submit.clicks == 2
    assert any("二次验证" in n for n in result.notes)


def test_login_waits_for_late_rendered_spa_form() -> None:
    """实测回归：SPA 首屏是客户端异步渲染。

    `page.goto(..., domcontentloaded)` 返回时 React 往往**还没渲染出登录表单**；
    若此时就去查控件，会把登录页误判成「公开页」而跳过登录（实测目标站即如此）。
    本条锁死「先等表单就绪，再判定有无密码框」。
    """

    class _LateFormPage(_StubLoginPage):
        def __init__(self) -> None:
            super().__init__(present=("user", "pwd", "submit"))
            self.ready = False

        def wait_for_selector(self, selector: str, timeout: int | None = None) -> object:
            self.ready = True
            return object()

        def query_selector(self, selector: str) -> _StubElement | None:
            if not self.ready:  # 渲染完成前任何控件都查不到
                return None
            return super().query_selector(selector)

    result = runtime_ui.RuntimeUiResult(base_url="http://srv")
    page = _LateFormPage()
    assert runtime_ui._login(page, _opts(login_password=_SECRET_PASSWORD), result) is True
    assert any("表单登录成功" in n for n in result.notes)


def test_password_selector_covers_spa_text_masked_forms() -> None:
    """SPA 常把密码框做成 type=text + 遮罩，placeholder 兜底不可退化。"""
    assert any("placeholder" in s and "密码" in s for s in _PASSWORD_SELECTORS)


# ============================================================================
# M3.3 路由来源（新增「菜单点击发现」档）与 SPA 菜单点击发现
# ============================================================================
def test_resolve_routes_menu_outranks_static() -> None:
    opts = _opts(routes=["/explicit"], max_pages=10)
    got = runtime_ui._resolve_routes(opts, ["/static"], ["/home"], ["/menu", "/static"])
    assert got == ["/explicit", "/menu", "/static", "/home"]


class _StubNavHandle:
    """模拟一个导航项：可自带 href、也可让点击「无效」（复现 SPA 占位 href）。"""

    def __init__(
        self,
        page: _StubMenuPage,
        target: str,
        href: str | None = None,
        click_noop: bool = False,
    ) -> None:
        self._page = page
        self._target = target
        self._href = href
        self._click_noop = click_noop

    def is_visible(self) -> bool:
        return True

    def get_attribute(self, name: str) -> str | None:
        return self._href if name == "href" else None

    def click(self, timeout: int | None = None, no_wait_after: bool = False) -> None:
        if not self._click_noop:
            self._page.url = "http://srv" + self._target


class _StubMenuPage:
    """模拟「点菜单即跳路由」的 SPA 页（整站无 `<a href>`）。

    `_rendered` 复刻真实 SPA 行为：`goto(domcontentloaded)` 返回时导航**尚未挂载**，
    只有 `wait_for_selector` 被调用后才「出现」——漏掉这一步会让菜单发现静默 0 条。
    `click_noop=True` 模拟「href 是占位符且点击不改变 URL」的退化场景。
    """

    def __init__(
        self,
        targets: list[str],
        start: str = "http://srv/",
        *,
        hrefs: dict[str, str] | None = None,
        click_noop: bool = False,
    ) -> None:
        self.url = start
        self._targets = targets
        self._hrefs = hrefs or {}
        self._click_noop = click_noop
        self.goto_calls: list[str] = []
        self._rendered = True  # 初始视为已渲染（调用方通常已抓过落地页）

    def goto(self, url: str, wait_until: str | None = None, timeout: int | None = None) -> None:
        self.goto_calls.append(url)
        self.url = url
        self._rendered = False  # 重新打开＝需要重新等渲染

    def wait_for_selector(self, selector: str, timeout: int | None = None) -> object:
        self._rendered = True
        return object()

    def eval_on_selector_all(self, selector: str, script: str) -> list[str]:
        # 该桩同时服务「取导航文本清单」：未渲染＝返回空表
        return list(self._targets) if self._rendered else []

    def query_selector_all(self, selector: str) -> list[_StubNavHandle]:
        if not self._rendered:
            return []  # 未渲染＝抓不到任何导航项（复现真实环境的 0 路由缺陷）
        return [
            _StubNavHandle(self, t, self._hrefs.get(t), self._click_noop) for t in self._targets
        ]

    def wait_for_function(
        self, script: str, arg: object | None = None, timeout: int | None = None
    ) -> None:
        return None

    def wait_for_timeout(self, ms: int) -> None:
        return None


class _StubMenuContext:
    """最小浏览器上下文桩：仅支持 `page` 监听（新标签页路由兜底）。"""

    def __init__(self) -> None:
        self.listeners: dict[str, object] = {}

    def on(self, event: str, handler: object) -> None:
        self.listeners[event] = handler

    def remove_listener(self, event: str, handler: object) -> None:
        self.listeners.pop(event, None)


def test_discover_menu_routes_collects_changed_paths() -> None:
    result = runtime_ui.RuntimeUiResult(base_url="http://srv")
    page = _StubMenuPage(["/a", "/b", "/a"])  # 第三条与首条重复 → 只记一次
    got = runtime_ui._discover_menu_routes(page, _StubMenuContext(), _opts(max_pages=10), result)
    assert got == ["/a", "/b"]
    assert result.notes and "菜单点击发现路由 2 条" in result.notes[0]
    assert page.goto_calls[-1] == "http://srv/"  # 收尾回起点


def test_discover_menu_routes_requires_render_wait() -> None:
    """锁死 SPA 渲染竞态——真实环境「菜单发现路由 0 条」的根因之一。

    `goto(domcontentloaded)` 返回时菜单还没挂载；不等渲染就 `query_selector_all`
    只会拿到空表并静默放弃。
    """

    class _NeverRendered(_StubMenuPage):
        def wait_for_selector(self, selector: str, timeout: int | None = None) -> object:
            return object()  # 假装等到了，实际仍不渲染

    result = runtime_ui.RuntimeUiResult(base_url="http://srv")
    ok_page = _StubMenuPage(["/a"])
    assert runtime_ui._discover_menu_routes(ok_page, _StubMenuContext(), _opts(), result) == ["/a"]

    stale = _NeverRendered(["/a"])
    stale._rendered = False
    assert runtime_ui._discover_menu_routes(stale, _StubMenuContext(), _opts(), result) == []


def test_discover_menu_routes_clicks_even_when_href_is_placeholder() -> None:
    """真实环境根因之二：SPA 的菜单 `href` 常是占位值（或全部指向首页）。

    若实现「优先读 href」，这些项会被判成「与起点同路由」而全部丢弃 → 静默 0 条。
    本条锁死「点击优先」：只要点击能跳转，就必须发现路由。
    """
    result = runtime_ui.RuntimeUiResult(base_url="http://srv")
    page = _StubMenuPage(["/a", "/b"], hrefs={"/a": "/", "/b": "/"})  # href 全是首页占位
    got = runtime_ui._discover_menu_routes(page, _StubMenuContext(), _opts(), result)
    assert got == ["/a", "/b"]


def test_discover_menu_routes_falls_back_to_href_when_click_is_noop() -> None:
    """点击不改变 URL 时（如整页刷新被拦截），退回用 href 取路由。"""
    result = runtime_ui.RuntimeUiResult(base_url="http://srv")
    page = _StubMenuPage(["/a"], hrefs={"/a": "/a"}, click_noop=True)
    got = runtime_ui._discover_menu_routes(page, _StubMenuContext(), _opts(), result)
    assert got == ["/a"]


def test_discover_menu_routes_can_be_disabled() -> None:
    result = runtime_ui.RuntimeUiResult(base_url="http://srv")
    page = _StubMenuPage(["/a"])
    got = runtime_ui._discover_menu_routes(
        page, _StubMenuContext(), _opts(discover_by_menu=False), result
    )
    assert got == []


def test_normalize_route_rejects_static_assets() -> None:
    """下载 / 前端产物不是页面——真实环境首页「下载客户端」链接曾被当成假页面。"""
    base = "http://srv/"
    assert runtime_ui._normalize_route("/downloads/app-win.zip", base) is None
    assert runtime_ui._normalize_route("/assets/logo.png", base) is None
    assert runtime_ui._normalize_route("/doc/guide.pdf", base) is None
    assert runtime_ui._normalize_route("/pc/tasks", base) == "/pc/tasks"


# ============================================================================
# M3.4 运行时发现 → 功能点（to_functional_points）与合并去重
# ============================================================================
def _ui_pages() -> list[runtime_ui.UiPage]:
    return [
        runtime_ui.UiPage(
            url="http://srv/pc/tasks",
            path="/pc/tasks",
            title="任务列表",
            elements=[runtime_ui.UiElement(selector="#q", kind="input", text="查询", visible=True)],
        ),
        runtime_ui.UiPage(
            url="http://srv/blank",
            path="/blank",
            title="空页",
            elements=[
                runtime_ui.UiElement(selector="#h", kind="button", text="隐藏", visible=False)
            ],
        ),
        runtime_ui.UiPage(url="http://srv/dead", path="/dead", reachable=False),
    ]


def test_to_functional_points_emits_page_and_element_fps() -> None:
    result = runtime_ui.RuntimeUiResult(base_url="http://srv", pages=_ui_pages())
    fps = runtime_ui.to_functional_points(result)
    keys = {(fp.ftype, fp.name) for fp in fps}
    assert (FType.PAGE.value, "/pc/tasks") in keys
    # #221 元素级分解：可见 input 元素 → 1 条 ui 功能点（带文字/selector，名称含合成 #）
    assert (FType.UI.value, "/pc/tasks#input:查询|#q") in keys
    # 无「可见」元素的页面只产 page，不产 ui（不可见元素跳过）
    assert (FType.PAGE.value, "/blank") in keys
    assert (FType.UI.value, "/blank#button:隐藏|#h") not in keys
    # 不可达页不产功能点
    assert not any(fp.name == "/dead" for fp in fps)
    page_fp = next(fp for fp in fps if fp.ftype == FType.PAGE.value and fp.name == "/pc/tasks")
    assert page_fp.module == "pc"
    assert page_fp.file_path == "runtime:http://srv/pc/tasks"
    assert page_fp.fp_id.startswith("FP-")


def test_to_functional_points_ids_are_stable_and_unique() -> None:
    result = runtime_ui.RuntimeUiResult(base_url="http://srv", pages=_ui_pages())
    first = [fp.fp_id for fp in runtime_ui.to_functional_points(result)]
    second = [fp.fp_id for fp in runtime_ui.to_functional_points(result)]
    assert first == second
    assert len(first) == len(set(first))


def test_to_functional_points_feed_existing_expansion_rules() -> None:
    """运行时 FP 必须能被既有展开规则直接消费（不新造维度）。"""
    from engine import tp_expand

    result = runtime_ui.RuntimeUiResult(base_url="http://srv", pages=_ui_pages())
    dims: dict[str, set[str]] = {}
    for fp in runtime_ui.to_functional_points(result):
        dims.setdefault(fp.ftype, set()).update(d for _, d in tp_expand.plan_of(fp))
    assert Dimension.PAGE_REACH.value in dims[FType.PAGE.value]
    # #221 元素级分解产出的 ui 功能点被 INTERACTIVE 维度消费（交互元素可用）
    assert Dimension.INTERACTIVE.value in dims[FType.UI.value]


def test_merge_runtime_fps_runtime_wins_on_same_key() -> None:
    from engine.pipeline import _merge_runtime_fps

    static = [
        FunctionalPoint(
            fp_id="FP-s1", ftype="page", file_path="src/a.tsx", name="/pc/tasks", title="静态"
        ),
        FunctionalPoint(
            fp_id="FP-s2", ftype="api", file_path="src/api.py", name="GET /x", title="接口"
        ),
    ]
    runtime = [
        FunctionalPoint(
            fp_id="FP-r1",
            ftype="page",
            file_path="runtime:http://srv/pc/tasks",
            name="/pc/tasks",
            title="运行时",
        ),
        FunctionalPoint(
            fp_id="FP-r2",
            ftype="page",
            file_path="runtime:http://srv/new",
            name="/new",
            title="新页",
        ),
    ]
    merged, stats = _merge_runtime_fps(static, runtime)
    keys = [(fp.ftype, fp.name) for fp in merged]
    assert stats["added"] == 1
    assert stats["replaced"] == 1
    assert stats["deduped"] == 0
    assert len(merged) == 3
    assert keys.count(("page", "/pc/tasks")) == 1
    assert next(fp for fp in merged if fp.name == "/pc/tasks").title == "运行时"  # 运行时优先
    assert ("api", "GET /x") in keys  # 仅静态有的保留
    assert ("page", "/new") in keys  # 仅运行时有的追加
