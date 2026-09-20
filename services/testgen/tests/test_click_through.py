"""G-6 click-through 深度发现测试（无浏览器，桩对象驱动）。

验证 #223 深度发现已系统覆盖四类二级/三级界面：
- Tab 原地切换；
- 弹窗/抽屉触发按钮（_OPEN_VERBS）；
- 分页翻页（_PAGINATION_SELECTOR，只读导航）；
- 搜索/筛选表单提交后视图（_FILTER_VERBS，只读，不误触创建/删除）。

全程用 FakePage / FakeForm / FakeHandle 模拟 Playwright 句柄，验证「点击后出现的新元素被记为 deep 功能点」且故障隔离（单点异常不中断）。
"""

from __future__ import annotations

from typing import Any

from engine.runtime_ui import (
    RuntimeUiOptions,
    RuntimeUiResult,
    UiElement,
    UiPage,
    _click_forms,
    _click_pagination,
    _click_through,
    _DeepCtx,
)


class _FakeHandle:
    """通用句柄：可见、点击无副作用。"""

    def __init__(self, text: str = "") -> None:
        self._text = text
        self.clicks = 0

    def is_visible(self) -> bool:
        return True

    def click(self, timeout: int = 5000) -> None:
        self.clicks += 1

    def inner_text(self) -> str:
        return self._text

    def get_attribute(self, name: str) -> str | None:
        return None

    def fill(self, value: str) -> None:
        pass

    def query_selector_all(self, selector: str) -> list[_FakeHandle]:
        return []

    def query_selector(self, selector: str) -> _FakeHandle | None:
        return None


class _FakeSubmit(_FakeHandle):
    def __init__(self) -> None:
        super().__init__(text="搜索")  # 命中 _FILTER_VERBS → 会被点击


class _FakeCreateSubmit(_FakeHandle):
    def __init__(self) -> None:
        super().__init__(text="创建")  # 非只读动词 → 不应被点击


class _FakeInput(_FakeHandle):
    pass


class _FakeForm:
    """表单桩：按选择器返回提交按钮与文本输入。"""

    def __init__(self, submit: _FakeHandle) -> None:
        self._submit = submit

    def query_selector_all(self, selector: str) -> list[_FakeHandle]:
        if "button" in selector or "submit" in selector or "role='button'" in selector:
            return [self._submit]
        if "input" in selector or "textarea" in selector:
            return [_FakeInput()]
        return []

    def query_selector(self, selector: str) -> _FakeHandle | None:
        return None


class _FakePage:
    """页面桩：根据选择器返回对应句柄；evaluate 区分「触发按钮扫描」与「元素抽取」。

    evaluate 对元素抽取每次返回**唯一**深层元素，便于验证「多次点击各发现新元素」。
    """

    def __init__(self, *, forms: list[_FakeForm] | None = None) -> None:
        self.forms = forms or []
        self._n = 0
        self.goto_calls = 0

    def query_selector_all(self, selector: str) -> list[Any]:
        if selector == "[role='tab']":
            return [_FakeHandle(), _FakeHandle()]
        if selector == "form":
            return list(self.forms)
        # 分页选择器（含 pagination/pager/page/next 任一关键字）
        low = selector.lower()
        if "pagination" in low or "pager" in low or "page" in low or "next" in low:
            return [_FakeHandle()]
        return []

    def query_selector(self, selector: str) -> Any:
        if "open-modal" in selector or "open" in selector:
            return _FakeHandle(text="打开")
        return None

    def evaluate(self, js: str) -> Any:
        # 触发按钮扫描 js 含 "button,a,input" 选择器（元素抽取 js 不含）→ 返回触发按钮 css 路径
        if isinstance(js, str) and "button,a,input" in js:
            return ["button.open-modal"]
        # 否则视为元素抽取 → 返回全新深层元素（每次唯一）
        self._n += 1
        return [
            {
                "selector": f"deep-{self._n}",
                "kind": "ui",
                "text": f"深层元素{self._n}",
                "visible": True,
            }
        ]

    def goto(self, url: str, wait_until: str | None = None, timeout: int | None = None) -> None:
        self.goto_calls += 1

    def wait_for_load_state(self, state: str | None = None, timeout: int | None = None) -> None:
        pass

    def wait_for_timeout(self, ms: int) -> None:
        pass

    def keyboard(self) -> Any:
        return _FakeKeyboard()


class _FakeKeyboard:
    def press(self, key: str) -> None:
        pass


class _FakeSession:
    def __init__(self, page: _FakePage) -> None:
        self.page = page


def _ctx(uip: UiPage, result: RuntimeUiResult) -> _DeepCtx:
    return _DeepCtx(
        uip=uip,
        path=uip.path or "/",
        base={(e.kind, e.selector) for e in uip.elements},
        global_seen=set(),
        result=result,
    )


def _page_with_base() -> tuple[UiPage, RuntimeUiResult]:
    uip = UiPage(
        url="http://x/p",
        path="/p",
        elements=[UiElement("base-a", "link"), UiElement("base-b", "btn")],
    )
    return uip, RuntimeUiResult(pages=[uip])


def test_click_through_discovers_deep_elements() -> None:
    """集成：Tab/弹窗/分页/表单四类交互均触发深层发现，且元素带 deep 标记。"""
    uip, result = _page_with_base()
    session = _FakeSession(_FakePage(forms=[_FakeForm(_FakeSubmit())]))
    _click_through(session, RuntimeUiOptions(), result, None)  # type: ignore[arg-type]

    assert len(uip.elements) > 2, "基线 2 个，至少应有 1 个深层元素被追加"
    assert any(e.deep for e in uip.elements), "应存在 deep=True 的深层元素"
    assert any("深度发现完成" in n for n in result.notes), "应记录深度发现完成 note"


def test_click_through_fault_isolated() -> None:
    """单页交互异常不应中断整轮（故障隔离）。"""
    uip, result = _page_with_base()
    # 触发按钮句柄点击抛异常 → _click_triggers 应跳过而非崩溃
    boom = _FakeHandle()

    class _BoomPage(_FakePage):
        def query_selector(self, selector: str) -> Any:
            return boom

        boom.click = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))  # type: ignore[assignment]

    session = _FakeSession(_BoomPage(forms=[_FakeForm(_FakeSubmit())]))
    _click_through(session, RuntimeUiOptions(), result, None)  # type: ignore[arg-type]
    # 即便触发异常，分页/表单等仍能发现深层元素
    assert any(e.deep for e in uip.elements)


def test_click_pagination_adds_deep() -> None:
    uip, result = _page_with_base()
    page = _FakePage()
    before = len(uip.elements)
    added = _click_pagination(page, RuntimeUiOptions(), _ctx(uip, result))
    assert added >= 1
    assert len(uip.elements) > before
    assert any(e.deep for e in uip.elements)


def test_click_forms_search_submit_adds_deep() -> None:
    """搜索/筛选表单（只读动词）应被点击并发现提交后视图。"""
    uip, result = _page_with_base()
    page = _FakePage(forms=[_FakeForm(_FakeSubmit())])
    before = len(uip.elements)
    added = _click_forms(page, RuntimeUiOptions(), _ctx(uip, result))
    assert added == 1
    assert len(uip.elements) > before
    assert any(e.deep for e in uip.elements)


def test_click_forms_skips_create_submit() -> None:
    """创建/删除类提交（非只读动词）不应被点击，避免误触写操作。"""
    uip, result = _page_with_base()
    page = _FakePage(forms=[_FakeForm(_FakeCreateSubmit())])
    before = len(uip.elements)
    added = _click_forms(page, RuntimeUiOptions(), _ctx(uip, result))
    assert added == 0, "非只读动词的提交按钮不应被点击"
    assert len(uip.elements) == before


def test_click_through_no_pagination_no_forms() -> None:
    """无分页/无表单时仅 Tab/弹窗触发仍应正常发现，且不报错。"""
    uip, result = _page_with_base()
    # 空 forms，且分页 query_selector_all 对分页选择器返回句柄（默认行为）——这里用无表单页
    page = _FakePage(forms=[])
    session = _FakeSession(page)
    _click_through(session, RuntimeUiOptions(), result, None)  # type: ignore[arg-type]
    assert any(e.deep for e in uip.elements)
