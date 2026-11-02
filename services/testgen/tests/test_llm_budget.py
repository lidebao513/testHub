"""成本护栏单测（core/llm_budget + engine/llm_fallback 接线）。

背景：百炼免费额度耗尽会自动转按量、不报错不中断。这组用例锁死三道闸的行为，
防止后续改动把「护栏」改回「建议」：

1. 模型门禁——付费关键字默认拦、白名单优先、显式放行可覆盖；
2. 单次输入预算——超限拒绝且 scope=global（换模型也一样超）；
3. 日累计熔断——到顶拒绝、跨进程持久化、次日自然归零。

另含 3 条接线用例：证明 `chat_with_fallback` 真的会在链上跳过付费模型、
日预算到顶时整链停止、以及护栏关闭时行为与旧版一致。
"""

from __future__ import annotations

import json
import types
from datetime import date, timedelta
from typing import ClassVar
from unittest.mock import patch

import pytest

from core import llm_budget
from core.llm_budget import BudgetExceeded
from engine import llm_fallback


@pytest.fixture()
def usage_tmp(tmp_path, monkeypatch):
    """把用量文件指向临时目录，并默认开启护栏（conftest 全局是关闭的）。

    注意：allowlist/registry/allow_paid 必须设为「存在但为空」，不能用 delenv。
    因为测试内 get_settings() 会再次触发 load_dotenv(override=False)，若这些键被删成
    缺失，dotenv 会从 .env 把生产值（截图里的免费模型清单）重新注入，污染本用例假定
    的「空白名单走付费关键字逻辑」环境。
    """
    path = tmp_path / "llm_usage.json"
    monkeypatch.setenv("LLM_USAGE_PATH", str(path))
    # 额度耗尽标记也必须隔离到本用例目录：403 用例会落盘标记，
    # 否则假模型名会写进真实 data/llm_quota_state.json 并影响后续运行。
    monkeypatch.setenv("LLM_QUOTA_STATE_PATH", str(tmp_path / "llm_quota_state.json"))
    monkeypatch.setenv("LLM_BUDGET_ENFORCE", "1")
    monkeypatch.setenv("LLM_MODEL_ALLOWLIST", "")
    monkeypatch.setenv("LLM_MODEL_REGISTRY", "")
    monkeypatch.setenv("LLM_ALLOW_PAID_MODEL", "")
    monkeypatch.setenv("LLM_DAILY_TOKEN_BUDGET", "100000")
    monkeypatch.setenv("LLM_CALL_MAX_INPUT_TOKENS", "24000")
    llm_fallback.reset_fallback_cache()
    return path


def _msgs(text: str = "hi") -> list[dict]:
    return [{"role": "user", "content": text}]


class TestModelGate:
    def test_paid_model_blocked_by_default(self, usage_tmp):
        d = llm_budget.check_call("qwen3.8-max-0902", _msgs())
        assert d.allowed is False
        assert "max" in d.reason
        assert d.scope == "model", "付费模型只跳过自身，链上还有免费档可试"

    @pytest.mark.parametrize("model", ["qwen3.7-flash-2026-07-15", "qwen-turbo", "local-7b"])
    def test_free_model_allowed(self, usage_tmp, model):
        assert llm_budget.check_call(model, _msgs()).allowed is True

    def test_allowlist_only_allows_listed(self, usage_tmp, monkeypatch):
        monkeypatch.setenv("LLM_MODEL_ALLOWLIST", "qwen3.7-flash-2026-07-15")
        assert llm_budget.check_call("qwen3.7-flash-2026-07-15", _msgs()).allowed is True
        assert llm_budget.check_call("qwen-turbo", _msgs()).allowed is False

    def test_allowlist_overrides_paid_hint(self, usage_tmp, monkeypatch):
        """白名单即「已确认开了用完即停」的清单，写进去就放行（即使叫 max）。"""
        monkeypatch.setenv("LLM_MODEL_ALLOWLIST", "qwen3.8-max-0902")
        assert llm_budget.check_call("qwen3.8-max-0902", _msgs()).allowed is True

    def test_allow_paid_flag_bypasses_hint(self, usage_tmp, monkeypatch):
        monkeypatch.setenv("LLM_ALLOW_PAID_MODEL", "1")
        assert llm_budget.check_call("qwen3.8-max", _msgs()).allowed is True

    def test_empty_model_rejected(self, usage_tmp):
        assert llm_budget.check_call("", _msgs()).allowed is False


class TestInputBudget:
    def test_input_too_large_blocks_whole_chain(self, usage_tmp, monkeypatch):
        monkeypatch.setenv("LLM_CALL_MAX_INPUT_TOKENS", "100")
        d = llm_budget.check_call("qwen3.7-flash", _msgs("x" * 5000))
        assert d.allowed is False
        assert d.scope == "global", "输入超限换模型也一样超，必须整链停"

    def test_estimate_counts_image_cost(self):
        text_only = llm_budget.estimate_messages_tokens(_msgs("hi"))
        with_image = llm_budget.estimate_messages_tokens(
            [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "x"}}]}]
        )
        assert with_image > text_only + 900, "图片不能被估成 0 token"


class TestDailyBudget:
    def test_exhausted_budget_rejects(self, usage_tmp, monkeypatch):
        monkeypatch.setenv("LLM_DAILY_TOKEN_BUDGET", "1000")
        llm_budget.record_usage("qwen3.7-flash", 900, 200)
        d = llm_budget.check_call("qwen3.7-flash", _msgs("hello world"))
        assert d.allowed is False
        assert d.scope == "global"
        assert d.day_used == 1100

    def test_record_persists_across_calls(self, usage_tmp):
        llm_budget.record_usage("qwen3.7-flash", 100, 50)
        llm_budget.record_usage("qwen3.7-flash", 200, 25)
        snap = llm_budget.snapshot()
        assert snap["used_tokens"] == 375
        assert snap["calls"] == 2
        raw = json.loads(usage_tmp.read_text(encoding="utf-8"))
        assert raw[date.today().isoformat()]["total"] == 375

    def test_next_day_resets(self, usage_tmp):
        llm_budget.record_usage("qwen3.7-flash", 500, 500)
        assert llm_budget.snapshot()["used_tokens"] == 1000
        # 手工把记录挪到昨天：今天应当视为 0
        data = json.loads(usage_tmp.read_text(encoding="utf-8"))
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        data[yesterday] = data.pop(date.today().isoformat())
        usage_tmp.write_text(json.dumps(data), encoding="utf-8")
        assert llm_budget.snapshot()["used_tokens"] == 0

    def test_old_days_pruned(self, usage_tmp):
        stale = (date.today() - timedelta(days=40)).isoformat()
        usage_tmp.write_text(json.dumps({stale: {"total": 1}}), encoding="utf-8")
        llm_budget.record_usage("qwen3.7-flash", 10, 10)
        assert stale not in json.loads(usage_tmp.read_text(encoding="utf-8"))

    def test_reset_today_clears(self, usage_tmp):
        llm_budget.record_usage("qwen3.7-flash", 800, 800)
        llm_budget.reset_today()
        assert llm_budget.snapshot()["used_tokens"] == 0


# ---------------------------------------------------------------------------
# 接线：护栏必须真的作用在 chat_with_fallback 上（否则只是摆设）
# ---------------------------------------------------------------------------


def _resp(text: str = "ok", prompt: int = 10, completion: int = 5):
    class _Msg:
        content = text

    class _Choice:
        message = _Msg()

    class _Usage:
        prompt_tokens = prompt
        completion_tokens = completion

    class _Resp:
        choices: ClassVar[list] = [_Choice()]
        usage = _Usage()

    return _Resp()


def _client(handler):
    return types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=handler))
    )


def test_guard_skips_paid_model_uses_free_one(usage_tmp):
    """链头是付费 max、链尾是免费 flash：护栏应跳过 max，直接用 flash。"""
    seen: list[str] = []

    def handler(model, messages, temperature, **kwargs):
        seen.append(model)
        return _resp()

    with patch("openai.OpenAI", return_value=_client(handler)):
        llm_fallback.chat_with_fallback(
            channel="llm",
            api_key="k",
            base_url="https://x",
            timeout=10,
            model_chain=["qwen3.8-max-0902", "qwen3.7-flash-2026-07-15"],
            messages=_msgs(),
        )
    assert seen == ["qwen3.7-flash-2026-07-15"], "付费模型应在发出请求前就被拦下"

    snap = llm_budget.snapshot()
    assert snap["used_tokens"] == 15, "成功后必须记账（prompt 10 + completion 5）"


def test_guard_skips_expired_model_uses_next(usage_tmp, monkeypatch):
    """链头模型免费额度已过期：护栏应跳过，自动改用链尾仍有效的模型。

    这是「有效期」的真实后果验证——2026-11-13 之后 deepseek-v4-pro 到期，
    链路必须自动落到 glm-5.3，而不是报错中断。
    """
    expired = (date.today() - timedelta(days=1)).isoformat()
    live = (date.today() + timedelta(days=30)).isoformat()
    monkeypatch.setenv(
        "LLM_MODEL_REGISTRY",
        f"deepseek-v4-pro-0813|llm|{expired}|on,glm-5.3|llm|{live}|on",
    )
    seen: list[str] = []

    def handler(model, messages, temperature, **kwargs):
        seen.append(model)
        return _resp()

    with patch("openai.OpenAI", return_value=_client(handler)):
        llm_fallback.chat_with_fallback(
            channel="llm",
            api_key="k",
            base_url="https://x",
            timeout=10,
            model_chain=["deepseek-v4-pro-0813", "glm-5.3"],
            messages=_msgs(),
        )
    assert seen == ["glm-5.3"], "过期模型必须在使用前被跳过，不能真的发请求"


def test_guard_stops_chain_when_daily_budget_exhausted(usage_tmp, monkeypatch):
    monkeypatch.setenv("LLM_DAILY_TOKEN_BUDGET", "50")
    llm_budget.record_usage("qwen3.7-flash", 40, 40)
    seen: list[str] = []

    def handler(model, messages, temperature, **kwargs):
        seen.append(model)
        return _resp()

    with patch("openai.OpenAI", return_value=_client(handler)), pytest.raises(BudgetExceeded):
        llm_fallback.chat_with_fallback(
            channel="llm",
            api_key="k",
            base_url="https://x",
            timeout=10,
            model_chain=["qwen3.7-flash", "qwen-turbo"],
            messages=_msgs(),
        )
    assert seen == [], "预算到顶时一次请求都不该发出"


def test_guard_passes_max_tokens_when_enabled(usage_tmp, monkeypatch):
    monkeypatch.setenv("LLM_CALL_MAX_OUTPUT_TOKENS", "1234")
    captured: dict = {}

    def handler(model, messages, temperature, **kwargs):
        captured.update(kwargs)
        return _resp()

    with patch("openai.OpenAI", return_value=_client(handler)):
        llm_fallback.chat_with_fallback(
            channel="llm",
            api_key="k",
            base_url="https://x",
            timeout=10,
            model="qwen3.7-flash",
            messages=_msgs(),
        )
    assert captured.get("max_tokens") == 1234


def test_guard_disabled_keeps_old_behavior(usage_tmp, monkeypatch):
    """关闭护栏（LLM_BUDGET_ENFORCE=0）时不拦付费模型、也不传 max_tokens。"""
    monkeypatch.setenv("LLM_BUDGET_ENFORCE", "0")
    seen: list[str] = []
    captured: dict = {}

    def handler(model, messages, temperature, **kwargs):
        seen.append(model)
        captured.update(kwargs)
        return _resp()

    with patch("openai.OpenAI", return_value=_client(handler)):
        llm_fallback.chat_with_fallback(
            channel="llm",
            api_key="k",
            base_url="https://x",
            timeout=10,
            model_chain=["qwen3.8-max-0902"],
            messages=_msgs(),
        )
    assert seen == ["qwen3.8-max-0902"]
    assert "max_tokens" not in captured
