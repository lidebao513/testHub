"""F13 · UI 层执行器（ui_probe）契约测试。

为什么用假页面而不是真浏览器：本模块要守的是**判定与证据**语义——
「断言怎么算通过 / 不可执行有没有如实说 / 失败有没有留证据」。
真浏览器由 CLI `pipeline --url ... --execute` 承担（见 F13 真机自测）。

覆盖点：
- `target_url`：`runtime.url` 优先；只接受「像路由」的 path（组件名不得拼成垃圾 URL）；
- 断言语义：页面渲染 / 元素可见（F9 selector→断言）/ 控制台基线 / 交互；
- **不可执行一律 skipped 且写明原因**（无会话 / 缺页面地址），绝不伪装通过；
- 失败与异常必留截图证据；截图失败不得覆盖主结论；
- `ui_click` 默认关（不点击、不改被测状态），开启后才真实点击。
"""

from __future__ import annotations

from typing import Any

from core.contracts import CaseSpec
from core.enums import ExecStatus, VerifyLayer
from engine import executor, ui_executor


# ============================================================================
# 假页面（记录调用，不发真实网络/不启浏览器）
# ============================================================================
class _FakeLocator:
    def __init__(self, count: int, visible: bool = True) -> None:
        self._count = count
        self._visible = visible
        self.clicks: list[Any] = []

    @property
    def first(self) -> _FakeLocator:
        return self

    def count(self) -> int:
        return self._count

    def is_visible(self) -> bool:
        return self._visible

    def click(self, timeout: int | None = None) -> None:
        self.clicks.append(timeout)


class _FakePage:
    """最小可用的假 page：只实现 ui_executor 真正会调用的那几个方法。"""

    def __init__(
        self,
        *,
        text: str = "仪表盘",
        selectors: dict[str, tuple[int, bool]] | None = None,
        goto_error: Exception | None = None,
        console_on_load: list[str] | None = None,
    ) -> None:
        self._text = text
        self._selectors = selectors or {}
        self._goto_error = goto_error
        self._console_on_load = console_on_load or []
        self.goto_calls: list[str] = []
        self.screenshots: list[str] = []
        self.locators: dict[str, _FakeLocator] = {}
        self._handlers: dict[str, Any] = {}

    # ---- 断言用到的接口 ----
    def evaluate(self, _js: str) -> int:
        return len(self._text)

    def locator(self, selector: str) -> _FakeLocator:
        count, visible = self._selectors.get(selector, (0, False))
        return self.locators.setdefault(selector, _FakeLocator(count, visible))

    def goto(self, url: str, wait_until: str | None = None, timeout: int | None = None) -> None:
        if self._goto_error is not None:
            raise self._goto_error
        self.goto_calls.append(url)
        # 模拟「加载过程中产生的控制台错误」：断言必须在导航之后读到它们
        handler = self._handlers.get("console")
        if handler is not None:
            for text in self._console_on_load:
                handler(_FakeConsoleMessage(text))

    def wait_for_timeout(self, _ms: int) -> None:
        pass

    def screenshot(self, path: str) -> None:
        self.screenshots.append(path)

    def on(self, event: str, handler: Any) -> None:
        self._handlers[event] = handler


class _FakeConsoleMessage:
    def __init__(self, text: str) -> None:
        self.type = "error"
        self.text = text


# ============================================================================
# 构造
# ============================================================================
def _case(
    *,
    path: str = "/pc/tasks",
    runtime: dict[str, Any] | None = None,
    assertions: list[dict[str, Any]] | None = None,
    layer: str = VerifyLayer.UI.value,
    tc_no: str = "TP-ui0001",
) -> CaseSpec:
    step: dict[str, Any] = {
        "action": "ui_probe",
        "layer": layer,
        "kind": "page",
        "method": "PAGE",
        "path": path,
        "dimension": "页面/路由可达",
        "expect": "页面可达",
    }
    if runtime is not None:
        step["runtime"] = runtime
    if assertions is not None:
        step["assertions"] = assertions
    return CaseSpec(
        tc_no=tc_no,
        title="[正常] 任务页可达",
        ctype="page",
        steps=[step],
        case_type="正常",
    )


def _options(**kwargs: Any) -> ui_executor.UiExecOptions:
    base: dict[str, Any] = {"base_url": "http://app.local"}
    base.update(kwargs)
    return ui_executor.UiExecOptions(**base)


def _run(page: _FakePage | None, case: CaseSpec, **opt_kwargs: Any) -> ui_executor.UiExecResult:
    session = ui_executor.UiSession(_options(**opt_kwargs))
    if page is not None:
        session.bind(page)
        session._attach(page)
    return session.execute(case)


# ============================================================================
# 目标地址
# ============================================================================
def test_target_url_prefers_runtime_url():
    step = {"runtime": {"url": "http://app.local/pc/tasks?x=1"}, "path": "/ignored"}
    assert ui_executor.target_url(step, _options()) == "http://app.local/pc/tasks?x=1"


def test_target_url_joins_base_for_route_path():
    assert ui_executor.target_url({"path": "/pc/tasks"}, _options()) == "http://app.local/pc/tasks"


def test_target_url_rejects_component_name():
    """组件名（非路由）拼出来的是垃圾 URL → 必须返回空串，让调用方如实 skipped。"""
    assert ui_executor.target_url({"path": "SearchBar"}, _options()) == ""


def test_target_url_empty_without_base_url():
    assert ui_executor.target_url({"path": "/x"}, _options(base_url="")) == ""


# ============================================================================
# 断言语义
# ============================================================================
def test_default_assertions_from_runtime_details():
    specs = ui_executor.default_assertions(
        {
            "console_error_baseline": 2,
            "elements": [{"selector": "#btn", "text": "提交"}, {"selector": ""}],
        }
    )
    kinds = [s["kind"] for s in specs]
    assert kinds[0] == ui_executor.ASSERT_PAGE_RENDERED
    assert ui_executor.ASSERT_CONSOLE_WITHIN_BASELINE in kinds
    visible = [s for s in specs if s["kind"] == ui_executor.ASSERT_ELEMENT_VISIBLE]
    assert [s["selector"] for s in visible] == ["#btn"], "空 selector 不得变成断言"
    assert specs[1]["baseline"] == 2


def test_assertions_of_falls_back_to_runtime_for_legacy_artifacts():
    """老产物没有 `assertions` 字段：必须能退回由 runtime 细节推导，不能直接跳过。"""
    step = {"runtime": {"elements": [{"selector": "#a"}]}}
    specs = ui_executor.assertions_of(step)
    assert any(s["kind"] == ui_executor.ASSERT_ELEMENT_VISIBLE for s in specs)


def test_unknown_assertion_kind_fails_not_passes():
    page = _FakePage()
    result = _run(page, _case(assertions=[{"kind": "no_such_kind"}]))
    assert result.status == ExecStatus.FAIL.value
    assert "未知断言种类" in result.notes[1]


def test_blank_page_is_failure():
    page = _FakePage(text="")
    result = _run(page, _case(assertions=[{"kind": ui_executor.ASSERT_PAGE_RENDERED}]))
    assert result.status == ExecStatus.FAIL.value
    assert "白屏" in result.assertions[0]["detail"]


def test_element_not_found_is_failure():
    page = _FakePage(selectors={"#missing": (0, False)})
    result = _run(
        page,
        _case(assertions=[{"kind": ui_executor.ASSERT_ELEMENT_VISIBLE, "selector": "#missing"}]),
    )
    assert result.status == ExecStatus.FAIL.value
    assert "未找到元素" in result.assertions[0]["detail"]


def test_element_present_but_invisible_is_failure():
    page = _FakePage(selectors={"#hidden": (1, False)})
    result = _run(
        page,
        _case(assertions=[{"kind": ui_executor.ASSERT_ELEMENT_VISIBLE, "selector": "#hidden"}]),
    )
    assert result.status == ExecStatus.FAIL.value
    assert "不可见" in result.assertions[0]["detail"]


def test_console_overflow_beyond_baseline_fails():
    page = _FakePage(console_on_load=["boom"])
    result = _run(
        page,
        _case(assertions=[{"kind": ui_executor.ASSERT_CONSOLE_WITHIN_BASELINE, "baseline": 0}]),
    )
    assert result.status == ExecStatus.FAIL.value
    assert "超过基线" in result.assertions[0]["detail"]


def test_console_within_baseline_passes():
    page = _FakePage(console_on_load=["boom"])
    result = _run(
        page,
        _case(assertions=[{"kind": ui_executor.ASSERT_CONSOLE_WITHIN_BASELINE, "baseline": 1}]),
    )
    assert result.status == ExecStatus.PASS.value


# ============================================================================
# 交互（ui_click 默认关）
# ============================================================================
def test_interaction_does_not_click_by_default():
    page = _FakePage(selectors={"#btn": (1, True)})
    result = _run(
        page,
        _case(assertions=[{"kind": ui_executor.ASSERT_INTERACTION_OK, "selector": "#btn"}]),
    )
    assert result.status == ExecStatus.PASS.value
    # 默认 ui_click=False：校验分支直接返回「未开启真实点击」，根本不创建 locator、不发点击
    assert "#btn" not in page.locators, "默认不得真实点击（防污染被测环境）"
    assert any("未开启真实点击" in a["detail"] for a in result.assertions)


def test_interaction_clicks_when_enabled():
    page = _FakePage(selectors={"#btn": (1, True)})
    result = _run(
        page,
        _case(assertions=[{"kind": ui_executor.ASSERT_INTERACTION_OK, "selector": "#btn"}]),
        ui_click=True,
    )
    assert result.status == ExecStatus.PASS.value
    assert len(page.locators["#btn"].clicks) == 1


def test_interaction_click_failure_is_failure():
    class _BoomLocator(_FakeLocator):
        def click(self, timeout: int | None = None) -> None:
            raise RuntimeError("element intercepted")

    class _BoomPage(_FakePage):
        def locator(self, selector: str) -> Any:
            return _BoomLocator(1, True)

    result = _run(
        _BoomPage(),
        _case(assertions=[{"kind": ui_executor.ASSERT_INTERACTION_OK, "selector": "#btn"}]),
        ui_click=True,
    )
    assert result.status == ExecStatus.FAIL.value
    assert "点击失败" in result.assertions[0]["detail"]


# ============================================================================
# 不可执行：如实 skipped
# ============================================================================
def test_no_session_is_skipped_and_never_claims_pass():
    result = _run(None, _case())
    assert result.status == ExecStatus.SKIPPED.value
    assert any("不等于通过" in n for n in result.notes)
    assert result.assertions == []


def test_missing_target_url_is_skipped():
    result = _run(_FakePage(), _case(path="SearchBar"))
    assert result.status == ExecStatus.SKIPPED.value
    assert any("缺少页面地址" in n for n in result.notes)


def test_goto_failure_is_error_with_screenshot():
    page = _FakePage(goto_error=TimeoutError("nav timeout"))
    result = _run(page, _case(), screenshot_dir="/tmp/nowhere-f13")
    assert result.status == ExecStatus.ERROR.value
    assert any("打开页面失败" in n for n in result.notes)


# ============================================================================
# 证据（截图）
# ============================================================================
def test_failure_records_screenshot(tmp_path):
    page = _FakePage(selectors={"#x": (0, False)})
    result = _run(
        page,
        _case(assertions=[{"kind": ui_executor.ASSERT_ELEMENT_VISIBLE, "selector": "#x"}]),
        screenshot_dir=str(tmp_path),
    )
    assert result.status == ExecStatus.FAIL.value
    assert result.screenshot_path.endswith(".png")
    assert page.screenshots == [result.screenshot_path]


def test_pass_does_not_take_screenshot(tmp_path):
    page = _FakePage()
    result = _run(page, _case(), screenshot_dir=str(tmp_path))
    assert result.status == ExecStatus.PASS.value
    assert result.screenshot_path == ""
    assert page.screenshots == []


def test_screenshot_failure_does_not_break_conclusion():
    """截图属辅助证据：写盘失败不得把「已判定的失败」变成异常。"""

    class _NoShotPage(_FakePage):
        def screenshot(self, path: str) -> None:
            raise OSError("disk full")

    result = _run(
        _NoShotPage(selectors={"#x": (0, False)}),
        _case(assertions=[{"kind": ui_executor.ASSERT_ELEMENT_VISIBLE, "selector": "#x"}]),
        screenshot_dir="/tmp/nowhere-f13",
    )
    assert result.status == ExecStatus.FAIL.value
    assert result.screenshot_path == ""


# ============================================================================
# 与执行器的接线（F13）
# ============================================================================
def test_executor_routes_ui_layer_to_ui_session():
    """`executor._dispatch` 必须按 layer 把 UI 用例交给 UI 会话，而不是发 HTTP。"""
    page = _FakePage()
    session = ui_executor.UiSession(_options())
    session.bind(page)
    session._attach(page)
    result = executor.execute_case(
        _case(),
        executor.ExecutorOptions(base_url="http://app.local"),
        ui_session=session,
    )
    assert result.status == ExecStatus.PASS.value
    assert page.goto_calls == ["http://app.local/pc/tasks"]


def test_executor_ui_without_session_is_skipped_not_passed():
    result = executor.execute_case(
        _case(), executor.ExecutorOptions(base_url="http://app.local"), ui_session=None
    )
    assert result.status == ExecStatus.SKIPPED.value
    assert "不等于通过" in result.notes[0]


def test_ui_options_mapping_keeps_credentials_out_of_repr():
    opts = executor.ExecutorOptions(
        base_url="http://app.local",
        login_user="alice",
        login_password="s3cret",
        login_otp="123456",
        channel="msedge",
        screenshot_dir="/tmp/shots",
        ui_click=True,
    )
    ui_opts = executor._ui_options(opts)
    assert ui_opts.channel == "msedge"
    assert ui_opts.ui_click is True
    assert ui_opts.login_password == "s3cret"
    assert ui_opts.runtime_options().login_otp == "123456"
