"""终态断言回归：浏览器已关闭时复用历史 state，不再发无人响应的事件。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from apps.ui_automation.ai_base import (
    BaseBrowserAgent,
    _page_text_from_history,
    _page_text_from_pre_explore,
)
from apps.ui_automation.assertions import page_text_from_state


def test_capture_final_state_reuses_last_history_step():
    state = SimpleNamespace(url='http://127.0.0.1:3900/login', title='Login')
    agent = object.__new__(BaseBrowserAgent)
    # browser-use 的真实字段名是 .history（不是 .steps）
    agent.history = SimpleNamespace(history=[
        SimpleNamespace(state=SimpleNamespace(url='about:blank', title='')),
        SimpleNamespace(state=state),
    ])

    class DeadSession:
        def get_browser_state_summary(self, **kwargs):
            raise AssertionError('浏览器已关闭，不应再抓 state')

    agent.browser_session = DeadSession()
    assert asyncio.run(agent._capture_final_state(agent)) is state


def test_capture_final_state_falls_back_to_session_without_history():
    state = SimpleNamespace(url='http://x/', title='x')

    class LiveSession:
        async def get_browser_state_summary(self, **kwargs):
            return state

    agent = object.__new__(BaseBrowserAgent)
    agent.history = SimpleNamespace(steps=[])
    agent.browser_session = LiveSession()
    assert asyncio.run(agent._capture_final_state(agent)) is state


def test_page_text_from_interacted_elements():
    state = SimpleNamespace(
        dom_state=None,
        interacted_element=[
            SimpleNamespace(node_value='登录', attributes={}),
            None,
            SimpleNamespace(node_value='', attributes={'placeholder': '请输入用户名'}),
        ],
    )
    text = page_text_from_state(state)
    assert '登录' in text
    assert '请输入用户名' in text


def test_page_text_falls_back_to_recent_history_step():
    def step_with(values):
        return SimpleNamespace(state=SimpleNamespace(interacted_element=[
            SimpleNamespace(node_value=value, attributes={}) for value in values
        ]))

    agent = object.__new__(BaseBrowserAgent)
    agent.history = SimpleNamespace(history=[step_with(['登录']), step_with([])])
    assert '登录' in _page_text_from_history(agent)


def test_pre_explore_text_used_only_on_same_page():
    agent = object.__new__(BaseBrowserAgent)
    agent._pre_explore_summary = {
        'url': 'http://127.0.0.1:3900/login',
        'elements': [{'label': 'button "登录"'}],
    }
    assert '登录' in _page_text_from_pre_explore(agent, 'http://127.0.0.1:3900/login')
    assert _page_text_from_pre_explore(agent, 'http://127.0.0.1:3900/home') == ''
