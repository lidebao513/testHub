"""批次 C（P2）：状态驱动面板区域（area:: 签名）的父页重放 + 签名断言。

背景：发现阶段点击菜单只切面板、URL 不变，旧产物只留 `area::<签名>`，执行器只能
诚实 skip。本批让发现侧补记 parent_url + nav_path，执行侧 goto 父页 → 重放点击 →
按签名断言；没有父页坐标的旧产物继续 skip，绝不猜路由。

三层覆盖：
1. 发现：`_absorb_panel_areas` 给面板页写入 parent_url + 累计 nav_path；
2. 生成：页面级 / 元素级面板用例的 machine step 都带定位坐标；
3. 执行：有坐标 → goto 父页 + 重放 + 签名断言；无坐标 → skip 且写明缺什么。
"""

from __future__ import annotations

from typing import Any

from core.contracts import CaseSpec
from core.enums import ExecStatus, VerifyLayer
from engine import case_gen, runtime_ui, ui_executor


# ============================================================================
# 1. 发现侧：_absorb_panel_areas
# ============================================================================
class _PanelPage:
    """面板桩页：`evaluate(_PANEL_SIG_JS)` 读当前签名，点导航项切签名（URL 不变）。"""

    def __init__(self, signatures: dict[str, str], landing_sig: str = "") -> None:
        self._signatures = signatures  # label -> sig；点击后以此 sig 为当前面板
        self._current = landing_sig
        self.clicked: list[str] = []

    def evaluate(self, js: str) -> Any:
        if "menu-item-selected" in js or "role=heading" in js:
            return self._current
        # 元素抓取脚本：返回一个可见元素，让 _collect_current_page 认定为有效面板
        return [{"selector": "#panel", "kind": "button", "text": "面板按钮", "visible": True}]

    def query_selector_all(self, selector: str) -> list[Any]:
        return []

    def get_by_text(self, text: str, exact: bool = False) -> Any:
        raise AssertionError("桩页未实现 get_by_text（本测试走 CSS 句柄失败→文本兜底）")

    def wait_for_timeout(self, ms: int) -> None:
        return None

    def goto(self, url: str, **kwargs: Any) -> None:
        self._current = ""


class _NavHandle:
    def __init__(self, page: _PanelPage, label: str) -> None:
        self._page = page
        self._label = label

    def is_visible(self) -> bool:
        return True

    def click(self, **kwargs: Any) -> None:
        self._page.clicked.append(self._label)
        self._page._current = self._page._signatures.get(self._label, self._page._current)


def _panel_page_with_nav(signatures: dict[str, str], landing_sig: str = "") -> _PanelPage:
    page = _PanelPage(signatures, landing_sig)
    # 用 query_selector_all + _nav_labels 的路径：_locate_nav 依赖 _nav_handles 与 _nav_labels。
    page.query_selector_all = lambda selector: [  # type: ignore[method-assign]
        _NavHandle(page, label) for label in signatures
    ]
    page.eval_on_selector_all = lambda selector, script: list(signatures)  # type: ignore[attr-defined]
    return page


class _Sink:
    def __init__(self) -> None:
        self.entries: list[str] = []

    def since(self, mark: int) -> list[str]:
        return self.entries[mark:]


def test_absorb_panel_areas_records_parent_and_nav_path():
    """发现器给每个面板页写入父页地址 + 从父页到该面板的完整点击序列。"""
    page = _panel_page_with_nav({"合同管理": "htgl", "报表中心": "bbzx"})
    result = runtime_ui.RuntimeUiResult(base_url="http://srv")
    opts = runtime_ui.RuntimeUiOptions(base_url="http://srv", max_pages=10)
    n = runtime_ui._absorb_panel_areas(
        page, opts, result, _Sink(), ["合同管理", "报表中心"], "http://srv/home"
    )
    assert n == 2
    panels = [p for p in result.pages if p.path.startswith("area::")]
    assert {p.path for p in panels} == {"area::htgl", "area::bbzx"}
    for p in panels:
        assert p.parent_url == "http://srv/home"
        assert p.nav_path, "面板页必须带点击路径"
    # 第二个面板的 nav_path 含第一个标签：发现器不在两面板间回起点，路径是累计的
    second = next(p for p in panels if p.path == "area::bbzx")
    assert second.nav_path[:2] == ["合同管理", "报表中心"]


def test_absorb_panel_areas_danger_label_not_in_nav_path():
    """危险导航项被跳过，也不进任何面板的 nav_path（执行器不重放危险点击）。"""
    page = _panel_page_with_nav({"退出登录": "logout", "合同管理": "htgl"})
    result = runtime_ui.RuntimeUiResult(base_url="http://srv")
    opts = runtime_ui.RuntimeUiOptions(base_url="http://srv", max_pages=10)
    runtime_ui._absorb_panel_areas(
        page, opts, result, _Sink(), ["退出登录", "合同管理"], "http://srv/home"
    )
    panels = [p for p in result.pages if p.path.startswith("area::")]
    for p in panels:
        assert "退出登录" not in p.nav_path


def test_runtime_index_carries_panel_coordinates():
    """to_runtime_index 把父页 + 点击路径带进 RuntimePageInfo。"""
    result = runtime_ui.RuntimeUiResult(
        base_url="http://srv",
        pages=[
            runtime_ui.UiPage(
                url="area::htgl",
                path="area::htgl",
                parent_url="http://srv/home",
                nav_path=["合同管理"],
                elements=[runtime_ui.UiElement("button.add", "button", "新建", True)],
            )
        ],
    )
    index = runtime_ui.to_runtime_index(result)
    info = index["area::htgl"]
    assert info.parent_url == "http://srv/home"
    assert info.nav_path == ["合同管理"]


# ============================================================================
# 2. 生成侧：页面级 / 元素级面板用例都带定位坐标
# ============================================================================
def _panel_info(nav_path: list[str] | None = None) -> runtime_ui.RuntimePageInfo:
    return runtime_ui.RuntimePageInfo(
        path="area::15条/页",
        url="area::15条/页",
        title="AI用例生成",
        parent_url="http://srv/home",
        nav_path=nav_path or ["智能用例生成", "AI用例生成"],
        console_errors=[],
        elements=[{"kind": "button", "text": "新建", "selector": "#new", "visible": True}],
    )


def _tp(area: str, kind: str) -> dict[str, Any]:
    return {
        "tp_id": "TP-c0000001",
        "fp_contract_id": "FP-c0000001",
        "category": "正常",
        "method": "PAGE" if kind == "page" else "UI",
        "area": area,
        "source": "runtime:area::15条/页",
        "module": "智能用例生成",
        "title": f"面板 {area}",
        "expect": "面板可达且元素可见",
        "verify_layer": VerifyLayer.UI.value,
    }


def test_build_case_page_panel_carries_coordinates():
    case = case_gen.build_case(
        _tp("area::15条/页", "page"), runtime_index={"area::15条/页": _panel_info()}
    )
    assert case is not None
    rt = case.steps[0]["runtime"]
    assert rt["parent_url"] == "http://srv/home"
    assert rt["nav_path"] == ["智能用例生成", "AI用例生成"]
    # 文案写父页 + 导航路径，不再冒充「打开 area::15条/页」
    assert "http://srv/home" in case.doc_steps[1]["desc"]
    assert "智能用例生成" in case.doc_steps[1]["desc"]


def test_build_case_element_panel_carries_minimal_coordinates():
    area = "area::15条/页#button:新建|#new"
    case = case_gen.build_case(_tp(area, "ui"), runtime_index={"area::15条/页": _panel_info()})
    assert case is not None
    rt = case.steps[0]["runtime"]
    assert rt["parent_url"] == "http://srv/home"
    assert rt["nav_path"] == ["智能用例生成", "AI用例生成"]
    assert "elements" not in rt, "元素级面板只挂定位坐标，不整页元素清单"
    # 未设显式断言：由 assertions_of 从 path 补特定元素 selector（批次 A 行为不变）
    assert "assertions" not in case.steps[0]
    specs = ui_executor.assertions_of(case.steps[0])
    assert any(s.get("selector") == "#new" for s in specs)


# ============================================================================
# 3. 执行侧：panel_target + 重放 + 签名断言
# ============================================================================
def test_panel_target_parses_element_area_signature():
    step = {
        "path": "area::15条/页#button:新建|#new",
        "runtime": {
            "url": "area::15条/页",
            "parent_url": "http://srv/home",
            "nav_path": ["智能用例生成"],
        },
    }
    got = ui_executor.panel_target(step)
    assert got is not None
    assert got["signature"] == "15条/页"
    assert got["parent_url"] == "http://srv/home"
    assert got["nav_path"] == ["智能用例生成"]


def test_panel_target_none_for_legacy_and_normal():
    # 旧产物：有签名无父页 → None（execute 走 SKIP_PANEL_AREA）
    assert (
        ui_executor.panel_target({"path": "area::15条/页", "runtime": {"url": "area::15条/页"}})
        is None
    )
    # 普通路由页 → None
    assert (
        ui_executor.panel_target({"path": "/home", "runtime": {"url": "http://srv/home"}}) is None
    )


class _ExecPage:
    """执行器桩页：记录 goto / 点击，evaluate 返回正文长度或面板签名。"""

    def __init__(self, signature: str, text: str = "面板正文") -> None:
        self.signature = signature
        self.text = text
        self.goto_calls: list[str] = []
        self.clicks: list[str] = []
        self._handlers: dict[str, Any] = {}

    def goto(self, url: str, **kwargs: Any) -> None:
        self.goto_calls.append(url)

    def wait_for_timeout(self, ms: int) -> None:
        return None

    def on(self, event: str, handler: Any) -> None:
        self._handlers[event] = handler

    def evaluate(self, js: str) -> Any:
        if "menu-item-selected" in js or "role=heading" in js:
            return self.signature
        return len(self.text)

    def locator(self, selector: str) -> Any:
        class _L:
            def __init__(self) -> None:
                self.count = lambda: 1
                self.first = self
                self.is_visible = lambda: True

        return _L()

    def screenshot(self, path: str) -> None:
        return None

    def query_selector_all(self, selector: str) -> list[Any]:
        return [_NavHandle(self, label) for label in ["智能用例生成"]]

    def eval_on_selector_all(self, selector: str, script: str) -> list[str]:
        return ["智能用例生成"]

    def wait_for_function(self, *args: Any, **kwargs: Any) -> None:
        return None


def _exec_case(area: str, parent: str, nav: list[str], sig: str) -> CaseSpec:
    return CaseSpec(
        tc_no="TP-panel-c",
        title="[正常] 面板重放",
        ctype="e2e",
        steps=[
            {
                "action": "ui_probe",
                "layer": VerifyLayer.UI.value,
                "kind": "ui",
                "path": area,
                "runtime": {
                    "url": area.split("#", 1)[0],
                    "parent_url": parent,
                    "nav_path": nav,
                    "console_error_baseline": 0,
                },
            }
        ],
        case_type="正常",
    )


def _panel_session(page: _ExecPage) -> ui_executor.UiSession:
    session = ui_executor.UiSession(ui_executor.UiExecOptions(base_url="http://srv"))
    session.bind(page)
    session._attach(page)
    return session


def test_panel_case_executes_and_passes(monkeypatch):
    """有坐标 + 签名命中 → goto 父页、重放点击、断言通过。"""
    page = _ExecPage(signature="15条/页")
    monkeypatch.setattr(runtime_ui, "replay_panel_nav", lambda _p, nav, _o: (True, ""))
    result = _panel_session(page).execute(
        _exec_case("area::15条/页#button:新建|#new", "http://srv/home", ["智能用例生成"], "15条/页")
    )
    assert result.status == ExecStatus.PASS.value
    assert page.goto_calls == ["http://srv/home"]
    assert any(
        o["kind"] == ui_executor.ASSERT_PANEL_SIGNATURE and o["ok"] for o in result.assertions
    )


def test_panel_case_signature_mismatch_fails(monkeypatch):
    """签名不匹配 → fail（不是假通过），带未通过原因。"""
    page = _ExecPage(signature="别的面板")
    monkeypatch.setattr(runtime_ui, "replay_panel_nav", lambda _p, nav, _o: (True, ""))
    result = _panel_session(page).execute(
        _exec_case("area::15条/页", "http://srv/home", ["智能用例生成"], "15条/页")
    )
    assert result.status == ExecStatus.FAIL.value
    sig = next(o for o in result.assertions if o["kind"] == ui_executor.ASSERT_PANEL_SIGNATURE)
    assert not sig["ok"] and "不匹配" in sig["detail"]


def test_panel_case_legacy_artifact_skips_with_reason():
    """旧产物无父页坐标 → skip 且写明缺什么，不猜路由。"""
    case = CaseSpec(
        tc_no="TP-panel-legacy",
        title="[正常] 旧面板产物",
        ctype="e2e",
        steps=[
            {
                "action": "ui_probe",
                "layer": VerifyLayer.UI.value,
                "kind": "ui",
                "path": "area::15条/页#button:新建|#new",
                "runtime": {"url": "area::15条/页"},
            }
        ],
        case_type="正常",
    )
    page = _ExecPage(signature="15条/页")
    result = _panel_session(page).execute(case)
    assert result.status == ExecStatus.SKIPPED.value
    assert not page.goto_calls, "旧产物不得 goto 猜测的地址"
    assert "缺父页" in "".join(result.notes) or "缺" in "".join(result.notes)


def test_replay_panel_nav_clicks_and_refuses_danger(monkeypatch):
    """replay_panel_nav 依次点击；遇危险标签拒绝重放。"""
    page = _panel_page_with_nav({"合同管理": "htgl"})
    ok, reason = runtime_ui.replay_panel_nav(
        page, ["合同管理"], runtime_ui.RuntimeUiOptions(base_url="http://srv")
    )
    assert ok and not reason
    assert page.clicked == ["合同管理"]
    ok, reason = runtime_ui.replay_panel_nav(
        page, ["退出登录"], runtime_ui.RuntimeUiOptions(base_url="http://srv")
    )
    assert not ok and "危险" in reason
