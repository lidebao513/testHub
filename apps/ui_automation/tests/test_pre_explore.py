"""页面预探索（#177）的离线自测。

为什么要单独测：预探索是"增强项"，它必须在**开关关闭时零行为变化**、
在**浏览器/会话异常时只记日志不抛错**。这两条一旦破了，会把主执行链路一起拖下水，
所以纯函数与"非阻断"语义都要锁住。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from apps.ui_automation import ai_base


# ============================================================================
# 开关
# ============================================================================


def test_enabled_by_default(monkeypatch):
    monkeypatch.delenv("UI_PRE_EXPLORE", raising=False)
    assert ai_base._ui_pre_explore_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "FALSE", "off", "no", " false "])
def test_disabled_values(monkeypatch, value):
    monkeypatch.setenv("UI_PRE_EXPLORE", value)
    assert ai_base._ui_pre_explore_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "yes", "", "anything"])
def test_truthy_values(monkeypatch, value):
    # 只有显式 0/false/off/no 才关闭；空串属于"配了但没写值"，按开启处理
    monkeypatch.setenv("UI_PRE_EXPLORE", value)
    assert ai_base._ui_pre_explore_enabled() is True


# ============================================================================
# 提示词
# ============================================================================


def test_prompt_contains_guardrails():
    prompt = ai_base.build_pre_explore_prompt()
    assert "PAGE PRE-EXPLORATION" in prompt
    assert "index" in prompt
    # 防臆造索引是这条提示词的核心约束，不能被删掉
    assert "NEVER invent an index" in prompt
    assert len(prompt) < 900  # 每条任务都要带，不能膨胀


# ============================================================================
# 元素清单格式化
# ============================================================================


def _node(tag, text="", attrs=None, visible=True):
    return SimpleNamespace(
        tag_name=tag,
        node_value=text,
        attributes=attrs or {},
        is_visible=visible,
    )


def test_format_sorted_by_index_and_limited():
    selector_map = {
        5: _node("button", "保存"),
        2: _node("input", "", {"placeholder": "用户名"}),
    }
    elements = ai_base.format_pre_explore_elements(selector_map)
    assert [e["index"] for e in elements] == [2, 5]
    assert elements[0]["tag"] == "input"
    assert "用户名" in elements[0]["label"]

    limited = ai_base.format_pre_explore_elements(selector_map, limit=1)
    assert len(limited) == 1
    assert limited[0]["index"] == 2


def test_format_skips_invisible_and_none():
    selector_map = {
        1: _node("button", "可见"),
        2: _node("button", "隐藏", visible=False),
        3: None,
    }
    elements = ai_base.format_pre_explore_elements(selector_map)
    assert [e["index"] for e in elements] == [1]


def test_format_skips_bad_index():
    selector_map = {"not-a-number": _node("div", "x"), 7: _node("a", "链接")}
    elements = ai_base.format_pre_explore_elements(selector_map)
    # 无法归一化为 int 的 key 直接跳过，不能污染清单
    assert [e["index"] for e in elements] == [7]


def test_format_empty_map():
    assert ai_base.format_pre_explore_elements({}) == []
    assert ai_base.format_pre_explore_elements(None) == []


def test_format_skips_noise_containers():
    # 真实登录页实测：selector_map 里混着一堆无 role/无文本的 div/span，
    # 不过滤会把 23 项清单稀释成 8 项噪音。
    selector_map = {
        1: _node("div", ""),
        2: _node("button", "确定"),
        3: _node("div", "", {"role": "button"}),  # 有 role，是真控件
        4: _node("span", "文字"),  # 有文本，保留
        5: _node("svg", ""),
    }
    elements = ai_base.format_pre_explore_elements(selector_map)
    assert [e["index"] for e in elements] == [2, 3, 4]


def test_label_collapses_newlines():
    # 换行会把"一行一个元素"的清单撑破（登录页语言切换按钮实测带 \n）
    node = _node("span", "🇨🇳\n简体中文")
    label = ai_base._pre_explore_element_label(node)
    assert "\n" not in label
    assert "简体中文" in label


def test_label_prefers_meaningful_text():
    node = _node("button", "提交")
    node.get_meaningful_text_for_llm = lambda: "提交订单"
    label = ai_base._pre_explore_element_label(node)
    assert "提交订单" in label
    assert label.startswith("button")


def test_label_falls_back_to_attributes():
    node = _node("input", "", {"aria-label": "搜索框", "type": "search"})
    label = ai_base._pre_explore_element_label(node)
    assert "input" in label
    assert "搜索框" in label


def test_label_truncates_long_text():
    node = _node("div", "长" * 200)
    label = ai_base._pre_explore_element_label(node)
    # 文本被截断并加省略号（标签外层还会包一层引号，所以不能断言 endswith）
    assert "…" in label
    assert len(label) < 80


def test_label_failure_is_tolerated():
    node = _node("button", "ok")

    def boom():
        raise RuntimeError("dom parse failed")

    node.get_meaningful_text_for_llm = boom
    label = ai_base._pre_explore_element_label(node)
    # 文本提取炸了也要给出可用标签（回退 node_value）
    assert "ok" in label


# ============================================================================
# 渲染
# ============================================================================


def test_render_empty():
    assert "0 项" in ai_base.render_pre_explore_block([])


def test_render_header_and_tail_truncation():
    elements = [
        {"index": i, "tag": "button", "label": f'button "btn{i}"'} for i in range(20)
    ]
    block = ai_base.render_pre_explore_block(elements, url="http://a/b", title="页面", limit=5)
    assert "预探索元素清单 20 项" in block
    assert "http://a/b" in block
    assert "[0]" in block
    assert "[19]" not in block
    assert "其余 15 项已省略" in block


# ============================================================================
# 非阻断语义（不启动浏览器）
# ============================================================================


def test_run_pre_explore_without_session_is_non_blocking():
    stub = SimpleNamespace()
    agent = SimpleNamespace(browser_session=None)
    result = asyncio.run(ai_base.BaseBrowserAgent._run_pre_explore(stub, agent))
    assert result is None


def test_run_pre_explore_handles_session_without_api():
    stub = SimpleNamespace()
    agent = SimpleNamespace(browser_session=SimpleNamespace())
    result = asyncio.run(ai_base.BaseBrowserAgent._run_pre_explore(stub, agent))
    assert result is None


def test_run_pre_explore_aggregates_and_notifies():
    state = SimpleNamespace(
        url="http://localhost:3900/login",
        title="登录",
        dom_state=SimpleNamespace(
            selector_map={
                1: _node("input", "", {"placeholder": "账号"}),
                2: _node("button", "登录"),
            }
        ),
    )

    class _FakeSession:
        async def get_browser_state_summary(self, include_screenshot=False):
            assert include_screenshot is False  # 不抓截图，避免额外开销
            return state

    stub = SimpleNamespace()
    agent = SimpleNamespace(browser_session=_FakeSession())

    payloads = []

    async def cb(payload):
        payloads.append(payload)

    summary = asyncio.run(ai_base.BaseBrowserAgent._run_pre_explore(stub, agent, cb))

    assert summary["count"] == 2
    assert summary["url"].endswith("/login")
    assert [e["index"] for e in summary["elements"]] == [1, 2]
    assert payloads and "预探索" in payloads[0]["content"]
    assert "[2]" in payloads[0]["content"]


def test_run_pre_explore_callback_failure_does_not_raise():
    state = SimpleNamespace(
        url="http://x", title="t", dom_state=SimpleNamespace(selector_map={1: _node("a", "x")})
    )

    class _FakeSession:
        async def get_browser_state_summary(self, include_screenshot=False):
            return state

    stub = SimpleNamespace()
    agent = SimpleNamespace(browser_session=_FakeSession())

    async def cb(payload):
        raise RuntimeError("ws closed")

    # 回调炸了也要返回摘要，不能把异常抛回主链路
    summary = asyncio.run(ai_base.BaseBrowserAgent._run_pre_explore(stub, agent, cb))
    assert summary["count"] == 1
