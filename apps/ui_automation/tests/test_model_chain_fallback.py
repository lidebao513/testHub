"""模型链降级验证：`a,b,c` 配置下链头 404/额度耗尽时自动切到可用模型。

全程不启动浏览器、不调真实 LLM：裸实例 + fake llm，只让连通性检查链路真实执行。
"""

from __future__ import annotations

import asyncio

import pytest

from apps.ui_automation import ai_base


class FakeLLM:
    def __init__(self, model, fail=None):
        self.model = model
        self.fail = fail
        self.calls = []

    async def ainvoke(self, prompt):
        self.calls.append(prompt)
        if self.fail:
            raise self.fail
        return 'OK'


def _bare_agent(chain, active=None, first_fail=None):
    agent = object.__new__(ai_base.BaseBrowserAgent)
    agent.model_chain = chain
    agent.model_name = active or chain[0]
    agent.routed_model = active or chain[0]
    agent.routing_info = {'model': agent.routed_model}
    agent.model_fallback = None
    agent.llm = FakeLLM(agent.routed_model, fail=first_fail)
    return agent


def test_fallback_skips_404_models_in_order(monkeypatch):
    err = RuntimeError('HTTP 404: model not found')
    agent = _bare_agent(['kimi-k1.1-pro', 'qwen4-max', 'glm-5.3'], first_fail=err)
    built = []

    def build(model):
        llm = FakeLLM(model, fail=err if model == 'qwen4-max' else None)
        built.append(llm)
        return llm

    monkeypatch.setattr(agent, '_build_chat_llm', build)
    asyncio.run(agent._verify_execution_llm())

    assert [llm.model for llm in built] == ['qwen4-max', 'glm-5.3']
    assert agent.llm is built[-1]
    assert agent.model_name == 'glm-5.3'
    assert agent.routed_model == 'glm-5.3'
    assert agent.routing_info['model'] == 'glm-5.3'
    assert agent.model_fallback['from'] == 'kimi-k1.1-pro'
    assert agent.model_fallback['to'] == 'glm-5.3'


def test_auth_error_does_not_fallback(monkeypatch):
    agent = _bare_agent(['kimi', 'qwen'], first_fail=RuntimeError('401 Unauthorized'))
    monkeypatch.setattr(agent, '_build_chat_llm', lambda model: pytest.fail('不应降级'))

    with pytest.raises(RuntimeError, match='Execution LLM unavailable'):
        asyncio.run(agent._verify_execution_llm())
    assert agent.model_name == 'kimi'


def test_single_model_chain_keeps_original_behavior(monkeypatch):
    agent = _bare_agent(['qwen4-max'])
    monkeypatch.setattr(agent, '_build_chat_llm', lambda model: pytest.fail('不应重建客户端'))

    asyncio.run(agent._verify_execution_llm())
    assert agent.model_name == 'qwen4-max'
    assert agent.model_fallback is None
    assert agent.llm.calls == ['Reply with OK.']


def test_all_unavailable_raises_last_error(monkeypatch):
    err = RuntimeError('429 rate limit exceeded')
    agent = _bare_agent(['a', 'b'], first_fail=err)
    monkeypatch.setattr(agent, '_build_chat_llm', lambda model: FakeLLM(model, fail=err))

    with pytest.raises(RuntimeError, match='429'):
        asyncio.run(agent._verify_execution_llm())


def test_resolve_chain_filters_blocked_models(monkeypatch):
    def fake_guard(model):
        if model == 'blocked':
            raise ValueError('[cost-guard] 已过期')

    monkeypatch.setattr(ai_base, '_assert_cost_allowed', fake_guard)
    assert ai_base._resolve_model_chain('blocked,good,good,bad2') == ['good', 'bad2']


@pytest.mark.parametrize(
    ('error', 'expected'),
    [
        (RuntimeError('404 model not found'), True),
        (RuntimeError('AllocationQuota.FreeTierOnly'), True),
        (RuntimeError('401 Unauthorized'), False),
        (RuntimeError('connection refused'), False),
    ],
)
def test_fallback_error_classification(error, expected):
    assert ai_base._is_fallback_error(error) is expected
