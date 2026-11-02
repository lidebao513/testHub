"""AI 用例的成本护栏（apps.ui_automation.ai_base._assert_cost_allowed）。

为什么要单独测：browser-use 每一步都把完整 DOM 重发，单条用例几十万 input token；
付费档模型在免费额度耗尽后会**静默**转按量计费。这个函数是执行前的最后一道闸，
必须证明它在「该拦」时真的拦、在「该放」时真的不误伤。
"""

from __future__ import annotations

from datetime import datetime, timedelta

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
    # 额度标记也必须隔离：否则「403 已耗尽」用例会把假模型名写进真实 data 文件，
    # 导致后续判定脚本把真实模型误判为已耗尽。
    monkeypatch.setenv("LLM_QUOTA_STATE_PATH", str(tmp_path / "llm_quota_state.json"))
    monkeypatch.setenv(
        "LLM_REGISTRY_BASELINE_PATH", str(tmp_path / "llm_registry_baseline.json")
    )
    monkeypatch.delenv("LLM_MODEL_REGISTRY", raising=False)
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


# ---------------------------------------------------------------------------
# 到期 / 额度耗尽这类「静默花钱」的口子：AI 用例单价最高，必须和主链路同源判定。
# ---------------------------------------------------------------------------


def test_expired_model_refused(usage_tmp, monkeypatch):
    """已过期模型必须拦：跨过到期时刻后的调用不报错，只悄悄进账单。"""
    monkeypatch.setenv("LLM_MODEL_REGISTRY", "free-a|llm|2020-01-01|on")
    with pytest.raises(ValueError) as exc:
        ai_base._assert_cost_allowed("free-a")
    assert "到期" in str(exc.value)


def test_near_expiry_model_refused(usage_tmp, monkeypatch):
    """剩余不足 60 分钟的模型提前禁用，避免一次调用跨过到期时刻。"""
    soon = (datetime.now() + timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M")
    monkeypatch.setenv("LLM_MODEL_REGISTRY", f"free-b|llm|{soon}|on")
    with pytest.raises(ValueError) as exc:
        ai_base._assert_cost_allowed("free-b")
    assert "提前禁用闸" in str(exc.value)


def test_quota_exhausted_model_refused(usage_tmp):
    """403 FreeTierOnly 已落盘标记的模型：本地直接拒跑，不必每次撞墙。"""
    assert llm_budget.mark_quota_exhausted("free-c", message="FreeTierOnly") is True
    with pytest.raises(ValueError) as exc:
        ai_base._assert_cost_allowed("free-c")
    assert "免费额度已耗尽" in str(exc.value)


def test_quota_mark_cleared_then_passes(usage_tmp, monkeypatch):
    """清除标记后恢复正常（换新一批免费模型时不会永久拉黑）。"""
    llm_budget.mark_quota_exhausted("free-d")
    llm_budget.clear_quota_marks("free-d")
    monkeypatch.setenv("LLM_MODEL_REGISTRY", "free-d|llm|2999-12-31|on")
    ai_base._assert_cost_allowed("free-d")  # 不抛即通过
