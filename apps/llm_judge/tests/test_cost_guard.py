"""裁判模型的成本护栏（judge_engine.cost_guard + JudgeEngine.judge 的拦截点）。

为什么要单独测：judge 引擎走独立 OpenAI 客户端，不经主调用链，历史上**完全没挂护栏**；
而裁判是 `n_runs × judge_models` 的放大用法，模型过期/额度耗尽后会不报错地转按量计费。
这里要证三件事：该拦的拦、该放的放、**被拦时绝不发请求**。
"""

from __future__ import annotations

import pytest

from apps.llm_judge.judge_engine import cost_guard
from apps.llm_judge.judge_engine import judge as judge_mod
from apps.requirement_analysis import llm_budget


@pytest.fixture()
def guard_env(tmp_path, monkeypatch):
    """把护栏用到的三份落盘文件全部指到临时目录，避免污染真实 data/。"""
    monkeypatch.setenv("LLM_USAGE_PATH", str(tmp_path / "llm_usage.json"))
    monkeypatch.setenv("LLM_QUOTA_STATE_PATH", str(tmp_path / "llm_quota_state.json"))
    monkeypatch.setenv("LLM_REGISTRY_BASELINE_PATH", str(tmp_path / "baseline.json"))
    monkeypatch.setenv("LLM_BUDGET_ENFORCE", "1")
    monkeypatch.delenv("LLM_MODEL_ALLOWLIST", raising=False)
    monkeypatch.delenv("LLM_MODEL_REGISTRY", raising=False)
    return tmp_path


def test_enforce_off_passes(guard_env, monkeypatch):
    monkeypatch.setenv("LLM_BUDGET_ENFORCE", "0")
    monkeypatch.setenv("LLM_MODEL_REGISTRY", "old|llm|2020-01-01|on")
    assert cost_guard.reject_reason("old") == ""


def test_expired_model_rejected(guard_env, monkeypatch):
    monkeypatch.setenv("LLM_MODEL_REGISTRY", "old|llm|2020-01-01|on")
    assert "到期" in cost_guard.reject_reason("old")


def test_quota_exhausted_model_rejected(guard_env):
    llm_budget.mark_quota_exhausted("judge-a", message="FreeTierOnly")
    assert "免费额度已耗尽" in cost_guard.reject_reason("judge-a")


def test_unregistered_model_passes(guard_env):
    """不在册的模型沿用既有约定放行（交给白名单/付费关键字判断）。"""
    assert cost_guard.reject_reason("some-unregistered-model") == ""


def test_judge_raises_before_any_request_when_all_blocked(guard_env, monkeypatch):
    """全部裁判模型不可用 → 直接抛错，且一次请求都不能发出。"""
    monkeypatch.setenv("LLM_MODEL_REGISTRY", "judge-old|llm|2020-01-01|on")
    engine = judge_mod.JudgeEngine(
        model="judge-old", api_key="sk-test", n_runs=1, judge_models=["judge-old"]
    )
    # build_prompt 依赖完整 ScoreRequest/Rubric；这里只测拦截点，构造过程与断言无关
    monkeypatch.setattr(engine, "build_prompt", lambda *a, **k: [])
    called: list[tuple] = []
    monkeypatch.setattr(engine, "_call", lambda *a, **k: called.append(a) or {})

    with pytest.raises(ValueError) as exc:
        engine.judge(None, None, [])
    assert "[cost-guard]" in str(exc.value)
    assert called == [], "被护栏拦截时绝不能发出 LLM 请求"


def test_judge_skips_blocked_but_keeps_usable(guard_env, monkeypatch):
    """部分不可用 → 只跳过被拦的裁判，其余照跑（不因一个过期模型整体瘫掉）。"""
    engine = judge_mod.JudgeEngine(
        model="judge-ok",
        api_key="sk-test",
        n_runs=1,
        judge_models=["judge-old", "judge-ok"],
    )
    monkeypatch.setenv(
        "LLM_MODEL_REGISTRY", "judge-old|llm|2020-01-01|on,judge-ok|llm|2999-12-31|on"
    )
    monkeypatch.setattr(engine, "build_prompt", lambda *a, **k: [])
    used: list[str] = []
    monkeypatch.setattr(
        engine,
        "_call",
        lambda messages, model: used.append(model) or {"dimensions": []},
    )
    monkeypatch.setattr(
        engine, "_parse", lambda data, model: engine._neutral_verdict(model)
    )

    engine.judge(None, None, [])
    assert used == ["judge-ok"], "只应调用通过护栏的那个裁判"
