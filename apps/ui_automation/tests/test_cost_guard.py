"""AI 用例的成本护栏（apps.ui_automation.ai_base._assert_cost_allowed）。

为什么要单独测：browser-use 每一步都把完整 DOM 重发，单条用例几十万 input token；
付费档模型在免费额度耗尽后会**静默**转按量计费。这个函数是执行前的最后一道闸，
必须证明它在「该拦」时真的拦、在「该放」时真的不误伤。
"""

from __future__ import annotations

import pytest

from apps.requirement_analysis import llm_budget
from apps.ui_automation import ai_base


@pytest.fixture()
def usage_tmp(tmp_path, monkeypatch):
    path = tmp_path / "llm_usage.json"
    monkeypatch.setenv("LLM_USAGE_PATH", str(path))
    monkeypatch.setenv("LLM_BUDGET_ENFORCE", "1")
    monkeypatch.delenv("LLM_MODEL_ALLOWLIST", raising=False)
    monkeypatch.delenv("LLM_ALLOW_PAID_MODEL", raising=False)
    monkeypatch.setenv("LLM_DAILY_TOKEN_BUDGET", "100000")
    return path


def test_paid_model_refused(usage_tmp):
    with pytest.raises(ValueError) as exc:
        ai_base._assert_cost_allowed("qwen3.8-max")
    assert "[cost-guard]" in str(exc.value)
    assert "付费关键字" in str(exc.value)


def test_free_model_passes(usage_tmp):
    ai_base._assert_cost_allowed("qwen3.7-flash-2026-07-15")  # 不抛即通过


def test_allowlist_admits_listed_paid_name(usage_tmp, monkeypatch):
    """用户确认开了「用完即停」的模型写进白名单后，即使是 max 也放行。"""
    monkeypatch.setenv("LLM_MODEL_ALLOWLIST", "qwen3.8-max-0902")
    ai_base._assert_cost_allowed("qwen3.8-max-0902")


def test_allowlist_blocks_unlisted(usage_tmp, monkeypatch):
    monkeypatch.setenv("LLM_MODEL_ALLOWLIST", "qwen3.7-flash-2026-07-15")
    with pytest.raises(ValueError) as exc:
        ai_base._assert_cost_allowed("qwen-turbo")
    assert "白名单" in str(exc.value)


def test_daily_budget_exhausted_refused(usage_tmp, monkeypatch):
    monkeypatch.setenv("LLM_DAILY_TOKEN_BUDGET", "100")
    llm_budget.record_usage("qwen3.7-flash", 60, 60)
    with pytest.raises(ValueError) as exc:
        ai_base._assert_cost_allowed("qwen3.7-flash")
    assert "已达上限" in str(exc.value)


def test_enforce_off_never_blocks(usage_tmp, monkeypatch):
    monkeypatch.setenv("LLM_BUDGET_ENFORCE", "0")
    ai_base._assert_cost_allowed("qwen3.8-max")
