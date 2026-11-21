"""批次 D（P3）：卡片式导航启发式单测。

锁定四道护栏：白名单容器（JS 侧）、危险标签过滤（JS + Python 双过滤）、
点击预算（≤12 且 ≤max_pages）、URL 变化确认（未变 URL 记误报、不采信）。
三态 off/auto/on（默认 auto）：auto 由 _card_nav_decision 按其它路由来源自动判定；
审计与判定理由落到 `RuntimeUiResult.card_nav_audit`。
"""

from __future__ import annotations

from typing import Any, ClassVar

from core import config as core_config
from engine import runtime_ui


class _CardHandle:
    """模拟一个卡片元素句柄：点击可改变页面 URL，或「无效」（URL 不变）。"""

    def __init__(self, page: _CardPage, target: str, click_noop: bool = False) -> None:
        self._page = page
        self._target = target
        self._click_noop = click_noop

    def is_visible(self) -> bool:
        return True

    def click(self, timeout: int | None = None, no_wait_after: bool = False) -> None:
        if not self._click_noop:
            self._page.url = "http://srv" + self._target


class _CardPage:
    """模拟主内容区卡片页：evaluate 返回候选，query_selector 返回可点句柄。"""

    def __init__(
        self,
        candidates: list[dict[str, Any]],
        *,
        start: str = "http://srv/",
        fail_goto: bool = False,
    ) -> None:
        self.url = start
        self.candidates = candidates
        self.goto_calls: list[str] = []
        self.evaluate_calls: list[str] = []
        self._fail_goto = fail_goto

    def evaluate(self, js: str) -> list[dict[str, str]]:
        self.evaluate_calls.append(js)
        # JS 侧只返回 label + selector；target/noop 是桩内部控制点击行为的元信息
        return [{"label": c["label"], "selector": c["selector"]} for c in self.candidates]

    def goto(self, url: str, wait_until: str | None = None, timeout: int | None = None) -> None:
        self.goto_calls.append(url)
        if self._fail_goto:
            raise RuntimeError("goto blocked")
        self.url = url

    def wait_for_selector(self, selector: str, timeout: int | None = None) -> object:
        return object()

    def query_selector(self, selector: str) -> _CardHandle | None:
        for cand in self.candidates:
            if cand["selector"] == selector:
                if cand.get("noop"):
                    return _CardHandle(self, "", click_noop=True)
                return _CardHandle(self, cand.get("target", "/unknown"))
        return None

    def wait_for_function(
        self, script: str, arg: object | None = None, timeout: int | None = None
    ) -> None:
        return None


class _StubContext:
    def __init__(self) -> None:
        self.listeners: dict[str, object] = {}

    def on(self, event: str, handler: object) -> None:
        self.listeners[event] = handler

    def remove_listener(self, event: str, handler: object) -> None:
        self.listeners.pop(event, None)


def _opts(**kw: object) -> runtime_ui.RuntimeUiOptions:
    # 直调单测默认 on（强制开启），避开 auto 需要发现上下文的分支
    base: dict[str, object] = {"base_url": "http://srv", "card_nav_mode": "on"}
    base.update(kw)
    return runtime_ui.RuntimeUiOptions(**base)  # type: ignore[arg-type]


def _cand(label: str, selector: str, target: str = "/x", noop: bool = False) -> dict[str, Any]:
    return {"label": label, "selector": selector, "target": target, "noop": noop}


# ---------------------------------------------------------------------------
# 护栏 ①：白名单容器 + 危险标签（JS 侧脚本注入）
# ---------------------------------------------------------------------------
def test_build_card_candidate_js_injects_whitelist_and_danger_words() -> None:
    js = runtime_ui._build_card_candidate_js()
    assert "__MAX__" not in js and "__SCOPE__" not in js
    assert "__EXCLUDE__" not in js and "__DANGER__" not in js
    # 白名单容器与排除区已注入
    assert "ant-layout-content" in js
    assert "nav, aside, header" in js
    # 危险标签词表已注入（删除 / 支付 / 退出）
    assert "删除" in js and "支付" in js and "logout" in js


def test_card_danger_filter_python_side() -> None:
    """Python 侧二次过滤：删除/支付/退出命中，普通导航词放行。"""
    assert runtime_ui._is_card_danger_label("删除任务")
    assert runtime_ui._is_card_danger_label("立即支付")
    assert runtime_ui._is_card_danger_label("Log Out")
    assert not runtime_ui._is_card_danger_label("查看详情")
    assert not runtime_ui._is_card_danger_label("报表中心")


# ---------------------------------------------------------------------------
# 默认关 + 配置映射
# ---------------------------------------------------------------------------
def test_card_nav_off_no_evaluate() -> None:
    """off 态：连候选脚本都不执行，零副作用。"""
    result = runtime_ui.RuntimeUiResult(base_url="http://srv")
    page = _CardPage([_cand("卡片A", "div.card-a", "/a")])
    got = runtime_ui._discover_card_routes(
        runtime_ui._Session(page, _StubContext()),
        _opts(card_nav_mode="off"),
        result,
        "http://srv/",
    )
    assert got == []
    assert page.evaluate_calls == []
    assert result.card_nav_audit["enabled"] is False
    assert result.card_nav_audit["mode"] == "off"
    assert "强制关闭" in result.card_nav_audit["decision"]


def _settings_stub() -> type:
    """构造 options_from_settings 所需的最小 Settings 桩。"""

    class _S:
        playwright_headless = True
        runtime_base_url = "http://srv"
        runtime_auth_token = ""
        runtime_ui_timeout = 30
        playwright_channel = ""
        runtime_login_user = ""
        runtime_login_password = ""
        runtime_login_otp = ""
        runtime_login_otp_refresh_cmd = ""
        runtime_login_url = ""
        runtime_routes: ClassVar[list[str]] = []
        runtime_max_pages = 60
        runtime_ui_mobile_enabled = False

    return _S


def test_card_nav_mode_on_from_settings() -> None:
    """新字段 runtime_ui_card_nav_mode=on 映射到 RuntimeUiOptions。"""
    S = _settings_stub()
    S.runtime_ui_card_nav_mode = "on"  # type: ignore[attr-defined]
    assert runtime_ui.options_from_settings(S()).card_nav_mode == "on"


def test_card_nav_default_auto_from_settings() -> None:
    """既不配新字段也不配旧字段 → 默认 auto。"""
    assert runtime_ui.options_from_settings(_settings_stub()()).card_nav_mode == "auto"


def test_card_nav_legacy_bool_compat_from_settings() -> None:
    """旧字段 runtime_ui_card_nav_enabled 仍兼容：True→on / False→off。"""
    S = _settings_stub()
    S.runtime_ui_card_nav_enabled = True  # type: ignore[attr-defined]
    assert runtime_ui.options_from_settings(S()).card_nav_mode == "on"
    S.runtime_ui_card_nav_enabled = False  # type: ignore[attr-defined]
    assert runtime_ui.options_from_settings(S()).card_nav_mode == "off"


# ---------------------------------------------------------------------------
# 护栏 ④：URL 变化确认（未变 URL = 误报，不采信）
# ---------------------------------------------------------------------------
def test_card_nav_collects_url_changed_routes_and_returns_to_start() -> None:
    result = runtime_ui.RuntimeUiResult(base_url="http://srv")
    page = _CardPage(
        [
            _cand("卡片A", "div.card-a", "/a"),
            _cand("卡片B", "div.card-b", "/b"),
        ]
    )
    got = runtime_ui._discover_card_routes(
        runtime_ui._Session(page, _StubContext()), _opts(), result, "http://srv/"
    )
    assert got == ["/a", "/b"]
    # 每次点击前都回起点（副作用隔离）
    assert page.goto_calls.count("http://srv/") == 2
    assert result.card_nav_audit["clicked"] == 2
    assert result.card_nav_audit["url_changed"] == 2
    assert result.card_nav_audit["routes"] == ["/a", "/b"]
    assert result.card_nav_audit["no_nav_labels"] == []


def test_card_nav_records_no_url_change_as_false_positive() -> None:
    """点击后 URL 未变（原地交互/展开）→ 记误报，不臆造路由。"""
    result = runtime_ui.RuntimeUiResult(base_url="http://srv")
    page = _CardPage([_cand("展开更多", "div.more", noop=True)])
    got = runtime_ui._discover_card_routes(
        runtime_ui._Session(page, _StubContext()), _opts(), result, "http://srv/"
    )
    assert got == []
    assert result.card_nav_audit["clicked"] == 1
    assert result.card_nav_audit["url_changed"] == 0
    assert result.card_nav_audit["no_nav_labels"] == ["展开更多"]
    assert any("误报 1 项" in n for n in result.notes)


# ---------------------------------------------------------------------------
# 护栏 ②：危险标签 Python 侧二次过滤（绕过 JS 也不点）
# ---------------------------------------------------------------------------
def test_card_nav_python_side_danger_filter_skips_click() -> None:
    result = runtime_ui.RuntimeUiResult(base_url="http://srv")
    page = _CardPage(
        [
            _cand("删除任务", "div.del", "/delete"),
            _cand("卡片A", "div.card-a", "/a"),
        ]
    )
    got = runtime_ui._discover_card_routes(
        runtime_ui._Session(page, _StubContext()), _opts(), result, "http://srv/"
    )
    assert got == ["/a"]  # 危险卡片未点
    assert result.card_nav_audit["danger_filtered"] == 1
    assert result.card_nav_audit["clicked"] == 1
    assert "删除任务" not in result.card_nav_audit["no_nav_labels"]


# ---------------------------------------------------------------------------
# 护栏 ③：点击预算（≤12 且 ≤max_pages）
# ---------------------------------------------------------------------------
def test_card_nav_respects_max_pages_budget() -> None:
    result = runtime_ui.RuntimeUiResult(base_url="http://srv")
    page = _CardPage([_cand(f"卡片{i}", f"div.c{i}", f"/r{i}") for i in range(4)])
    got = runtime_ui._discover_card_routes(
        runtime_ui._Session(page, _StubContext()), _opts(max_pages=2), result, "http://srv/"
    )
    assert got == ["/r0", "/r1"]
    assert result.card_nav_audit["clicked"] == 2  # max_pages=2 截断


def test_card_nav_budget_hard_cap_twelve() -> None:
    """即使 max_pages 很大，点击预算也不超过硬上限 12。"""
    result = runtime_ui.RuntimeUiResult(base_url="http://srv")
    page = _CardPage([_cand(f"卡片{i}", f"div.c{i}", f"/r{i}") for i in range(15)])
    got = runtime_ui._discover_card_routes(
        runtime_ui._Session(page, _StubContext()), _opts(max_pages=60), result, "http://srv/"
    )
    assert len(got) == runtime_ui._CARD_CLICK_BUDGET == 12


def test_card_nav_stops_when_return_to_start_fails() -> None:
    """回起点失败 → 立即停止点击（不带着脏状态继续点）。"""
    result = runtime_ui.RuntimeUiResult(base_url="http://srv")
    page = _CardPage([_cand("卡片A", "div.card-a", "/a")], fail_goto=True)
    got = runtime_ui._discover_card_routes(
        runtime_ui._Session(page, _StubContext()), _opts(), result, "http://srv/"
    )
    assert got == []
    assert result.card_nav_audit["return_failed"] == 1
    assert result.card_nav_audit["clicked"] == 0


# ---------------------------------------------------------------------------
# 误报审计沉淀到报告
# ---------------------------------------------------------------------------
def test_card_nav_audit_written_to_report_notes() -> None:
    result = runtime_ui.RuntimeUiResult(base_url="http://srv")
    result.card_nav_audit = {
        "mode": "auto",
        "enabled": True,
        "decision": "auto：其它路由来源均为空，启用卡片探测兜底",
        "candidates": 5,
        "danger_filtered": 1,
        "clicked": 4,
        "url_changed": 2,
        "routes": ["/a", "/b"],
        "no_nav_labels": ["展开更多", "切换Tab"],
    }
    runtime_ui._record_summary(result, logged_in=True, reachable=3)
    digest = [n for n in result.notes if n.startswith("[卡片导航 P3]")]
    # 启用态两行：判定理由 + 误报明细
    assert len(digest) == 2
    assert "模式=auto" in digest[0] and "启用" in digest[0]
    assert "候选 5" in digest[1] and "危险过滤 1" in digest[1]
    assert "误报 2" in digest[1] and "['/a', '/b']" in digest[1]


def test_card_nav_audit_silent_when_disabled() -> None:
    """关闭时 summary 不输出卡片导航段（默认路径零噪音）。"""
    result = runtime_ui.RuntimeUiResult(base_url="http://srv")
    runtime_ui._record_summary(result, logged_in=False, reachable=1)
    assert not any("卡片导航" in n for n in result.notes)


# ---------------------------------------------------------------------------
# 三态：off / auto / on —— 系统按发现结果自动开关
# ---------------------------------------------------------------------------
def test_card_nav_decision_off_and_on_are_forced() -> None:
    """off/on 不受发现结果影响：强制关 / 强制开。"""
    off, off_reason = runtime_ui._card_nav_decision(
        "off", static_paths=[], menu_paths=["/a"], explicit_routes=["/b"]
    )
    on, on_reason = runtime_ui._card_nav_decision(
        "on", static_paths=["/s"], menu_paths=["/m"], explicit_routes=["/e"]
    )
    assert off is False and "强制关闭" in off_reason
    assert on is True and "强制开启" in on_reason


def test_card_nav_decision_auto_on_when_no_other_routes() -> None:
    """auto：其它渠道全空 → 自动开启（只有地址、没代码时兜底）。"""
    enabled, reason = runtime_ui._card_nav_decision(
        "auto", static_paths=[], menu_paths=[], explicit_routes=[]
    )
    assert enabled is True
    assert "均为空" in reason and "兜底" in reason


def test_card_nav_decision_auto_off_when_any_route_source() -> None:
    """auto：显式 / 菜单 / 静态任一有路由 → 自动关闭，避免点击副作用。"""
    for kw in (
        {"explicit_routes": ["/e"]},
        {"menu_paths": ["/m"]},
        {"static_paths": ["/s"]},
    ):
        enabled, reason = runtime_ui._card_nav_decision("auto", **kw)
        assert enabled is False, kw
        assert "跳过卡片探测" in reason, reason


def test_card_nav_decision_invalid_mode_falls_back_to_auto() -> None:
    """非法 mode 安全回落 auto：全空开、有来源关。"""
    assert runtime_ui._card_nav_decision("weird", static_paths=[], menu_paths=[])[0] is True
    assert runtime_ui._card_nav_decision("weird", static_paths=["/s"])[0] is False


def test_card_nav_decision_home_links_are_a_source() -> None:
    """首页 <a> 链接也算确定性来源：auto 态有链接即关（纳入四来源统一判定）。"""
    enabled, reason = runtime_ui._card_nav_decision(
        "auto", static_paths=[], menu_paths=[], home_links=["/a", "/b"]
    )
    assert enabled is False
    assert "首页链接 2 条" in reason and "跳过卡片探测" in reason
    # on 强制开：即便有首页链接也不受影响
    assert runtime_ui._card_nav_decision("on", home_links=["/a"])[0] is True


def test_card_nav_decision_sources_structured_for_calibration() -> None:
    """四来源计数结构化写入 sources_out，供多 SPA 实测校准判定口径。"""
    sources: dict[str, int] = {}
    enabled, reason = runtime_ui._card_nav_decision(
        "auto",
        explicit_routes=["/e"],
        menu_paths=["/m1", "/m2"],
        static_paths=[],
        home_links=["/h"],
        sources_out=sources,
    )
    assert enabled is False
    assert sources == {"显式路由": 1, "菜单发现": 2, "静态路由": 0, "首页链接": 1}
    assert "显式路由 1 条" in reason and "菜单发现 2 条" in reason and "首页链接 1 条" in reason


def test_card_nav_auto_decision_passed_in_is_honored_and_audited() -> None:
    """sweep 传入的 auto 判定原样生效，理由写进审计（可追溯系统自动开关）。"""
    result = runtime_ui.RuntimeUiResult(base_url="http://srv")
    page = _CardPage([_cand("卡片A", "div.card-a", "/a")])
    decision = (False, "auto：已有菜单发现 3 条，跳过卡片探测避免副作用")
    got = runtime_ui._discover_card_routes(
        runtime_ui._Session(page, _StubContext()),
        _opts(card_nav_mode="auto"),
        result,
        "http://srv/",
        decision=decision,
    )
    assert got == []
    assert page.evaluate_calls == []  # 判定为关 → 零点击零 evaluate
    assert result.card_nav_audit["mode"] == "auto"
    assert result.card_nav_audit["decision"] == decision[1]
    assert result.card_nav_audit["enabled"] is False


def test_card_nav_summary_reports_auto_decision_when_off() -> None:
    """auto 态即便判定为关，也在报告写明「为什么关」——可观测。"""
    result = runtime_ui.RuntimeUiResult(base_url="http://srv")
    result.card_nav_audit = {
        "mode": "auto",
        "enabled": False,
        "decision": "auto：已有静态路由 5 条，跳过卡片探测避免副作用",
    }
    runtime_ui._record_summary(result, logged_in=True, reachable=6)
    digest = [n for n in result.notes if n.startswith("[卡片导航 P3]")]
    assert len(digest) == 1
    assert "模式=auto" in digest[0] and "未启用" in digest[0]
    assert "静态路由 5 条" in digest[0]
    assert not any("候选" in n for n in digest)  # 未启用不输出误报明细


# ---------------------------------------------------------------------------
# 配置层：RUNTIME_UI_CARD_NAV_MODE 三态解析 + 旧 bool 兼容
# ---------------------------------------------------------------------------
def test_as_card_nav_mode_new_variable() -> None:
    """新变量 off/auto/on 原样生效。"""
    for mode in ("off", "auto", "on"):
        assert core_config._as_card_nav_mode(mode, None) == mode
        assert core_config._as_card_nav_mode(f" {mode.upper()} ", None) == mode


def test_as_card_nav_mode_defaults_auto() -> None:
    """两者都未设置 → 默认 auto（用户无需手动操作）。"""
    assert core_config._as_card_nav_mode(None, None) == "auto"
    assert core_config._as_card_nav_mode("", "") == "auto"


def test_as_card_nav_mode_falls_back_to_legacy_bool() -> None:
    """新变量未设、旧 bool 显式设置 → true→on / false→off。"""
    assert core_config._as_card_nav_mode(None, "true") == "on"
    assert core_config._as_card_nav_mode("", "0") == "off"


def test_as_card_nav_mode_new_wins_over_legacy() -> None:
    """新变量优先于旧 bool。"""
    assert core_config._as_card_nav_mode("auto", "true") == "auto"
    assert core_config._as_card_nav_mode("off", "true") == "off"


def test_as_card_nav_mode_invalid_falls_back_auto() -> None:
    """非法新值且无旧值 → 安全回落 auto。"""
    assert core_config._as_card_nav_mode("sometimes", None) == "auto"


# ---------------------------------------------------------------------------
# 护栏⑤（批次3）：副作用熔断（dialog / 同源写请求 → 停止后续点击）
# ---------------------------------------------------------------------------
class _FuseContext:
    """可触发 dialog / request 的上下文桩；同事件支持多监听器。"""

    def __init__(self) -> None:
        self.handlers: dict[str, list[Any]] = {}

    def on(self, event: str, handler: Any) -> None:
        self.handlers.setdefault(event, []).append(handler)

    def remove_listener(self, event: str, handler: Any) -> None:
        if event in self.handlers and handler in self.handlers[event]:
            self.handlers[event].remove(handler)

    def fire(self, event: str, payload: Any) -> None:
        for handler in list(self.handlers.get(event, [])):
            handler(payload)


class _FusePage(_CardPage):
    """点击第 N 个卡片时注入副作用（dialog 或写请求）。"""

    def __init__(
        self,
        candidates: list[dict[str, Any]],
        *,
        side_effect_on: int,
        kind: str,
    ) -> None:
        super().__init__(candidates)
        self.side_effect_on = side_effect_on
        self.kind = kind
        self.context: _FuseContext | None = None
        self._click_no = 0

    def query_selector(self, selector: str) -> _CardHandle | None:
        self._click_no += 1
        for cand in self.candidates:
            if cand["selector"] == selector:
                return _CardHandle(self, cand.get("target", "/unknown"))
        return None

    def wait_for_function(self, *args: Any, **kwargs: Any) -> None:
        # URL 变化已由 handle.click 完成，这里注入副作用后返回
        if self._click_no == self.side_effect_on and self.context is not None:
            if self.kind == "dialog":
                self.context.fire(
                    "dialog", type("D", (), {"type": "confirm", "dismiss": lambda s: None})()
                )
            elif self.kind == "write":
                self.context.fire(
                    "request", type("R", (), {"method": "POST", "url": "http://srv/api/save"})()
                )


def _fuse_run(
    kind: str, *, candidates: int = 4, side_effect_on: int = 1
) -> tuple[dict[str, Any], list[str]]:
    result = runtime_ui.RuntimeUiResult(base_url="http://srv")
    ctx = _FuseContext()
    page = _FusePage(
        [_cand(f"卡片{i}", f"div.c{i}", f"/r{i}") for i in range(candidates)],
        side_effect_on=side_effect_on,
        kind=kind,
    )
    page.context = ctx
    runtime_ui._discover_card_routes(runtime_ui._Session(page, ctx), _opts(), result, "http://srv/")
    return result.card_nav_audit, result.notes


def test_card_nav_fuse_on_dialog_stops_later_clicks() -> None:
    """点击触发 confirm → 熔断，后续卡片不再点击（零进一步副作用）。"""
    audit, notes = _fuse_run("dialog", side_effect_on=1)
    assert audit["clicked"] == 1  # 只点了第一张就熔断
    assert audit["side_effect_tripped"] == ["dialog:confirm"]
    assert any("熔断" in n for n in notes)


def test_card_nav_fuse_on_same_origin_write_request() -> None:
    """点击触发同源 POST → 熔断；GET / 跨域写不熔断。"""
    audit, notes = _fuse_run("write", side_effect_on=2)
    assert audit["clicked"] == 2  # 第二张触发写请求，点完即停
    assert audit["side_effect_tripped"] == ["write:POST"]
    assert any("熔断" in n for n in notes)


def test_card_nav_no_fuse_on_get_requests() -> None:
    """普通导航（GET）不触发熔断——全部候选照常点击。"""
    audit, notes = _fuse_run("none", candidates=4)
    assert audit["clicked"] == 4
    assert "side_effect_tripped" not in audit
    assert not any("熔断" in n for n in notes)
