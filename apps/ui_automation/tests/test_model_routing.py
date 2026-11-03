"""模型分级路由 + 成本归因（#178）的离线自测。

为什么要单独测：分级路由一旦判错，会把**复杂用例降到弱模型**上跑歪——那比多花钱
严重得多。所以这里锁的是三条语义：
1. 只有 simple 才降级，complex/异常/未配置一律留在主模型（fail-safe）；
2. 成本记 NULL 表示「未知」，绝不写成 0 冒充免费；
3. 同一 agent 重复执行时能复位回主模型。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from apps.ui_automation import ai_base


# ============================================================================
# 开关
# ============================================================================


def test_routing_enabled_by_default(monkeypatch):
    monkeypatch.delenv("UI_MODEL_ROUTING", raising=False)
    assert ai_base._ui_model_routing_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "FALSE", "off", "no", " false "])
def test_routing_disabled_values(monkeypatch, value):
    monkeypatch.setenv("UI_MODEL_ROUTING", value)
    assert ai_base._ui_model_routing_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "yes", "", "anything"])
def test_routing_truthy_values(monkeypatch, value):
    monkeypatch.setenv("UI_MODEL_ROUTING", value)
    assert ai_base._ui_model_routing_enabled() is True


# ============================================================================
# 复杂度估算（复用原 _step_complexity_score 口径）
# ============================================================================


def test_step_score_empty_and_simple():
    assert ai_base.step_complexity_score("") == 0
    assert ai_base.step_complexity_score("打开首页") == 0


def test_step_score_grows_with_length_connectors_and_actions():
    # 长度 >=24 → 1；含「然后」等连接词 → 1
    assert ai_base.step_complexity_score("点击登录按钮然后等待页面加载完成并确认首页标题显示正确") >= 2
    # 两个动作词 → 再 +1
    assert ai_base.step_complexity_score("输入用户名并点击登录") >= 2
    # 短且无连接词/多动作 → 不提分
    assert ai_base.step_complexity_score("点击登录按钮然后等待页面加载完成") == 1


def test_method_delegates_to_module_function():
    agent = object.__new__(ai_base.BaseBrowserAgent)
    text = "输入用户名并点击登录按钮然后等待跳转"
    assert agent._step_complexity_score(text) == ai_base.step_complexity_score(text)


def test_estimate_simple_task():
    result = ai_base.estimate_task_complexity("打开登录页")
    assert result["complexity"] == "simple"
    assert result["score"] == 0


def test_estimate_text_too_long_is_complex():
    result = ai_base.estimate_task_complexity("打开页面" * 60)
    assert result["complexity"] == "complex"
    assert any("任务描述" in reason for reason in result["reasons"])


def test_estimate_uses_steps_when_provided():
    simple_steps = [{"description": "打开首页"}, {"description": "点击登录"}]
    assert ai_base.estimate_task_complexity("随便", simple_steps)["complexity"] == "simple"


def test_estimate_complex_step_wins():
    steps = [
        {"description": "打开首页"},
        {"description": "输入用户名并点击登录然后等待页面加载完成后校验标题"},
    ]
    result = ai_base.estimate_task_complexity("随便", steps)
    assert result["complexity"] == "complex"
    assert any("高复杂步骤" in reason for reason in result["reasons"])


def test_estimate_too_many_steps_is_complex():
    steps = [{"description": f"步骤{i}"} for i in range(6)]
    result = ai_base.estimate_task_complexity("随便", steps)
    assert result["complexity"] == "complex"
    assert result["step_count"] == 6


def test_estimate_long_url_does_not_inflate_complexity():
    # 回归：真实踩到的坑——URL 长度把「打开某页确认有输入框」判成 complex，
    # 结果分级路由永远不生效。
    task = "打开 http://127.0.0.1:3900/login/some/very/long/path ，确认页面有输入框和按钮，然后结束。"
    assert ai_base.estimate_task_complexity(task)["complexity"] == "simple"


def test_estimate_many_actions_is_complex():
    task = "打开登录页，输入用户名并点击登录，然后进入首页查看待办列表"
    result = ai_base.estimate_task_complexity(task)
    assert result["complexity"] == "complex"
    assert any("动作" in reason for reason in result["reasons"])


# ============================================================================
# 轻档模型解析
# ============================================================================


def test_resolve_light_model_explicit_env_wins(monkeypatch):
    monkeypatch.setenv("UI_MODEL_LIGHT", "my-light-model")
    assert ai_base.resolve_light_model("deepseek-v4-pro-0813") == "my-light-model"


def test_resolve_light_model_by_sibling_mapping(monkeypatch):
    monkeypatch.delenv("UI_MODEL_LIGHT", raising=False)
    assert ai_base.resolve_light_model("deepseek-v4-pro-0813") == "deepseek-v4.1-flash"
    assert ai_base.resolve_light_model("qwen-max-latest") == "qwen-turbo"


def test_resolve_light_model_none_when_already_light(monkeypatch):
    monkeypatch.delenv("UI_MODEL_LIGHT", raising=False)
    assert ai_base.resolve_light_model("deepseek-v4.1-flash") == ""
    assert ai_base.resolve_light_model("") == ""


# ============================================================================
# temperature
# ============================================================================


def test_temperature_special_model_forces_value():
    temp, source = ai_base._resolve_temperature("kimi-k2.5", 0.3)
    assert temp == 1.0
    assert "kimi" in source


def test_temperature_uses_configured_when_present():
    assert ai_base._resolve_temperature("deepseek-v4-pro", 0.3)[0] == 0.3


def test_temperature_falls_back_to_zero():
    assert ai_base._resolve_temperature("deepseek-v4-pro", None)[0] == 0.0
    # 配置值非法时不炸，退回默认值
    assert ai_base._resolve_temperature("deepseek-v4-pro", "bad")[0] == 0.0


# ============================================================================
# 成本归因：未知必须记 None，不能记 0
# ============================================================================


def test_cost_provider_reported_wins(monkeypatch):
    monkeypatch.delenv("UI_MODEL_PRICE_JSON", raising=False)
    cost, source = ai_base.resolve_usage_cost("m", 1000, 500, reported_cost=0.01)
    assert (cost, source) == (0.01, "provider")


def test_cost_estimated_from_price_table(monkeypatch):
    monkeypatch.setenv("UI_MODEL_PRICE_JSON", '{"mymodel": {"in": 1.0, "out": 2.0}}')
    cost, source = ai_base.resolve_usage_cost("MyModel", 1_000_000, 500_000)
    assert source == "pricing"
    assert cost == pytest.approx(2.0)


def test_cost_price_table_accepts_pair(monkeypatch):
    monkeypatch.setenv("UI_MODEL_PRICE_JSON", '{"m": [1.0, 0.0]}')
    cost, source = ai_base.resolve_usage_cost("m", 1_000_000, 0)
    assert (cost, source) == (1.0, "pricing")


def test_cost_unknown_is_none_not_zero(monkeypatch):
    monkeypatch.delenv("UI_MODEL_PRICE_JSON", raising=False)
    cost, source = ai_base.resolve_usage_cost("unknown-model", 1000, 500)
    assert cost is None
    assert source == "unavailable"


def test_cost_bad_json_does_not_raise(monkeypatch):
    monkeypatch.setenv("UI_MODEL_PRICE_JSON", "{not json")
    cost, source = ai_base.resolve_usage_cost("m", 1, 1)
    assert (cost, source) == (None, "unavailable")


# ============================================================================
# 路由决策（fail-safe）
# ============================================================================


def _make_agent(monkeypatch, model="deepseek-v4-pro-0813"):
    """不跑 __init__ 地造一个 agent：只补路由需要的最小属性。"""
    agent = object.__new__(ai_base.BaseBrowserAgent)
    agent.model_name = model
    agent.provider = "qwen"
    agent.api_key = "test-key"
    agent.base_url = "http://example.invalid/v1"
    agent.configured_temperature = 0.3
    agent._primary_llm = "primary-llm"
    agent.llm = agent._primary_llm
    agent.routed_model = model
    agent.routing_info = {}
    agent._usage_snapshot = None
    agent._pre_explore_summary = None
    agent._build_chat_llm = lambda name: f"llm:{name}"
    monkeypatch.delenv("UI_MODEL_LIGHT", raising=False)
    monkeypatch.delenv("UI_MODEL_ROUTING", raising=False)
    monkeypatch.setattr(ai_base, "_assert_cost_allowed", lambda name: None)
    return agent


def test_routing_switches_simple_task_to_light_model(monkeypatch):
    agent = _make_agent(monkeypatch)
    info = agent._apply_model_routing("打开登录页")

    assert info["switched"] is True
    assert info["complexity"] == "simple"
    assert agent.routed_model == "deepseek-v4.1-flash"
    assert agent.llm == "llm:deepseek-v4.1-flash"


def test_routing_keeps_primary_for_complex_task(monkeypatch):
    agent = _make_agent(monkeypatch)
    steps = [{"description": f"步骤{i}"} for i in range(7)]
    info = agent._apply_model_routing("随便", steps)

    assert info["switched"] is False
    assert info["complexity"] == "complex"
    assert agent.llm == "primary-llm"
    assert agent.routed_model == "deepseek-v4-pro-0813"


def test_routing_disabled_keeps_primary(monkeypatch):
    agent = _make_agent(monkeypatch)
    monkeypatch.setenv("UI_MODEL_ROUTING", "off")
    info = agent._apply_model_routing("打开登录页")

    assert info["switched"] is False
    assert info["enabled"] is False
    assert agent.llm == "primary-llm"


def test_routing_falls_back_when_light_model_rejected(monkeypatch):
    agent = _make_agent(monkeypatch)

    def _reject(name):
        raise ValueError("[cost-guard] 轻档模型不在白名单")

    monkeypatch.setattr(ai_base, "_assert_cost_allowed", _reject)
    info = agent._apply_model_routing("打开登录页")

    assert info["switched"] is False
    assert "回退主模型" in info["reason"]
    assert agent.llm == "primary-llm"


def test_routing_noop_when_no_light_mapping(monkeypatch):
    agent = _make_agent(monkeypatch, model="deepseek-v4.1-flash")
    info = agent._apply_model_routing("打开登录页")

    assert info["switched"] is False
    assert "未解析到轻档模型" in info["reason"]


def test_routing_resets_to_primary_on_second_run(monkeypatch):
    agent = _make_agent(monkeypatch)
    agent._apply_model_routing("打开登录页")
    assert agent.llm == "llm:deepseek-v4.1-flash"

    steps = [{"description": f"步骤{i}"} for i in range(7)]
    agent._apply_model_routing("随便", steps)
    assert agent.llm == "primary-llm"
    assert agent.routed_model == "deepseek-v4-pro-0813"


# ============================================================================
# 用量快照
# ============================================================================


def test_usage_snapshot_reads_browser_use_usage(monkeypatch):
    monkeypatch.delenv("UI_MODEL_PRICE_JSON", raising=False)
    agent = _make_agent(monkeypatch)
    usage = type(
        "Usage",
        (),
        {
            "total_tokens": 1234,
            "total_prompt_tokens": 1000,
            "total_completion_tokens": 234,
            "total_cost": 0.0,
        },
    )()
    history = type("History", (), {"usage": usage, "steps": [1, 2, 3]})()
    agent_instance = type("AgentInstance", (), {"history": history})()

    agent._capture_usage(agent_instance)
    snapshot = agent.get_usage_snapshot()

    assert snapshot["total_tokens"] == 1234
    assert snapshot["prompt_tokens"] == 1000
    assert snapshot["completion_tokens"] == 234
    assert snapshot["steps"] == 3
    assert snapshot["cost"] is None
    assert snapshot["cost_source"] == "unavailable"


def test_usage_snapshot_survives_missing_history(monkeypatch):
    agent = _make_agent(monkeypatch)
    agent._capture_usage(object())
    snapshot = agent.get_usage_snapshot()

    assert snapshot["total_tokens"] is None
    assert snapshot["cost"] is None


def test_usage_snapshot_includes_routing_and_pre_explore(monkeypatch):
    agent = _make_agent(monkeypatch)
    agent._apply_model_routing("打开登录页")
    agent._pre_explore_summary = {"url": "http://x", "title": "t", "count": 7, "elements": []}

    snapshot = agent.get_usage_snapshot()

    assert snapshot["routed_model"] == "deepseek-v4.1-flash"
    assert snapshot["pre_explore"]["count"] == 7
    assert snapshot["routing"]["switched"] is True


# ============================================================================
# token 计数：browser-use 记 0 时靠回调兜底
# ============================================================================


def test_extract_token_usage_from_usage_metadata():
    message = SimpleNamespace(usage_metadata={"input_tokens": 100, "output_tokens": 20})
    response = SimpleNamespace(generations=[[SimpleNamespace(message=message)]], llm_output=None)
    assert ai_base._extract_token_usage(response) == (100, 20)


def test_extract_token_usage_from_llm_output():
    response = SimpleNamespace(
        generations=[],
        llm_output={"token_usage": {"prompt_tokens": 30, "completion_tokens": 5}},
    )
    assert ai_base._extract_token_usage(response) == (30, 5)


def test_extract_token_usage_missing_returns_zero():
    assert ai_base._extract_token_usage(SimpleNamespace(generations=[], llm_output=None)) == (0, 0)


def test_counter_accumulates_across_calls():
    counter = ai_base.TokenUsageCounter()
    msg = SimpleNamespace(usage_metadata={"input_tokens": 10, "output_tokens": 2})
    counter.on_llm_end(SimpleNamespace(generations=[[SimpleNamespace(message=msg)]], llm_output=None))
    counter.on_llm_end(SimpleNamespace(generations=[[SimpleNamespace(message=msg)]], llm_output=None))
    assert counter.total_tokens == 24
    assert counter.calls == 2


def test_usage_falls_back_to_counter_when_browser_use_reports_zero(monkeypatch):
    agent = _make_agent(monkeypatch)
    agent._token_counter = ai_base.TokenUsageCounter()
    agent._token_counter.prompt_tokens = 900
    agent._token_counter.completion_tokens = 100
    agent._token_counter.calls = 3

    usage = SimpleNamespace(
        total_tokens=0, total_prompt_tokens=0, total_completion_tokens=0, total_cost=0.0
    )
    history = SimpleNamespace(usage=usage, history=[1, 2])
    agent._capture_usage(SimpleNamespace(history=history))
    snapshot = agent.get_usage_snapshot()

    assert snapshot["total_tokens"] == 1000
    assert snapshot["prompt_tokens"] == 900
    assert snapshot["completion_tokens"] == 100
    assert snapshot["steps"] == 2


def test_history_step_count_handles_browser_use_shape():
    # AgentHistoryList 把步骤放在 history 字段（不是 steps）
    assert ai_base._history_step_count(SimpleNamespace(history=[1, 2, 3])) == 3
    assert ai_base._history_step_count([1, 2]) == 2
    assert ai_base._history_step_count(None) == 0
