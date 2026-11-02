"""LLM 成本护栏（Django 侧）单测 + 接线验证。

覆盖两件事：
1. `apps.requirement_analysis.llm_budget` 三道闸（模型门禁 / 单次输入 / 日累计）；
2. `AIModelService.call_openai_compatible_api` 真的接上了护栏，并且 `model_name`
   填 `a,b,c` 时会**按书写顺序**降级（前一个额度耗尽自动切下一个）。

不触网：用替身 httpx client 返回构造好的 httpx.Response。
"""

from __future__ import annotations

import json
from datetime import date

import httpx
import pytest

from apps.requirement_analysis import llm_budget
from apps.requirement_analysis.models import AIModelConfig, AIModelService


@pytest.fixture()
def usage_tmp(tmp_path, monkeypatch):
    """用量文件指向临时目录 + 默认开启护栏。"""
    path = tmp_path / "llm_usage.json"
    monkeypatch.setenv("LLM_USAGE_PATH", str(path))
    # 额度耗尽标记同样隔离：403 降级用例会落盘标记，不隔离会污染真实
    # data/llm_quota_state.json，导致下次运行直接跳过该模型（假失败）。
    monkeypatch.setenv("LLM_QUOTA_STATE_PATH", str(tmp_path / "llm_quota_state.json"))
    monkeypatch.setenv("LLM_BUDGET_ENFORCE", "1")
    monkeypatch.delenv("LLM_MODEL_ALLOWLIST", raising=False)
    monkeypatch.delenv("LLM_ALLOW_PAID_MODEL", raising=False)
    monkeypatch.setenv("LLM_DAILY_TOKEN_BUDGET", "100000")
    monkeypatch.setenv("LLM_CALL_MAX_INPUT_TOKENS", "24000")
    monkeypatch.setenv("LLM_CALL_MAX_OUTPUT_TOKENS", "4096")
    return path


def _msgs(text: str = "hi") -> list[dict]:
    return [{"role": "user", "content": text}]


def _config(model_name: str, **kw) -> AIModelConfig:
    return AIModelConfig(
        api_key="sk-test",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        model_name=model_name,
        max_tokens=kw.get("max_tokens", 2048),
        temperature=kw.get("temperature", 0.7),
        top_p=kw.get("top_p", 0.9),
        model_type=kw.get("model_type", "qwen"),
    )


def _resp(status: int, payload: dict) -> httpx.Response:
    return httpx.Response(
        status_code=status,
        json=payload,
        request=httpx.Request("POST", "http://test.local/chat/completions"),
    )


class _FakeClient:
    """替身 httpx.AsyncClient：把请求交给 handler 决定返回什么。"""

    def __init__(self, handler, *args, **kwargs):
        self._handler = handler
        self.calls: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, headers=None, json=None):
        self.calls.append(json or {})
        return self._handler(json or {})


def _patch_client(monkeypatch, handler):
    """patch httpx.AsyncClient，并把调用记录暴露出来。"""
    holder: dict = {}

    def factory(*args, **kwargs):
        client = _FakeClient(handler, *args, **kwargs)
        holder["client"] = client
        return client

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return holder


class TestModelGate:
    def test_paid_model_blocked_by_default(self, usage_tmp):
        d = llm_budget.check_call("qwen3.8-max-0902", _msgs())
        assert d.allowed is False
        assert d.scope == "model"

    def test_free_model_allowed(self, usage_tmp):
        assert (
            llm_budget.check_call("qwen3.7-flash-2026-07-15", _msgs()).allowed is True
        )

    def test_allowlist_restricts_to_listed(self, usage_tmp, monkeypatch):
        monkeypatch.setenv("LLM_MODEL_ALLOWLIST", "qwen3.7-flash-2026-07-15")
        assert (
            llm_budget.check_call("qwen3.7-flash-2026-07-15", _msgs()).allowed is True
        )
        assert llm_budget.check_call("qwen-plus", _msgs()).allowed is False

    def test_allowlist_admits_paid_name(self, usage_tmp, monkeypatch):
        """白名单即「已确认开了用完即停」的清单，写进去就放行（即使名字含 max）。"""
        monkeypatch.setenv("LLM_MODEL_ALLOWLIST", "qwen3.8-max-0902")
        assert llm_budget.check_call("qwen3.8-max-0902", _msgs()).allowed is True

    def test_daily_budget_global_scope(self, usage_tmp, monkeypatch):
        monkeypatch.setenv("LLM_DAILY_TOKEN_BUDGET", "100")
        llm_budget.record_usage("qwen3.7-flash", 80, 80)
        d = llm_budget.check_call("qwen3.7-flash", _msgs("hello"))
        assert d.allowed is False
        assert d.scope == "global"

    # --- 写入闸门 assert_text_model：配置落库前的强校验 ---

    def test_assert_text_model_disabled_registry_passes(self, usage_tmp, monkeypatch):
        """登记册未启用（空串）：不阻断，保持旧行为允许第三方模型；但空名仍报错。"""
        monkeypatch.setenv("LLM_MODEL_REGISTRY", "")
        assert llm_budget.assert_text_model("my-private-gpt") == ""
        assert llm_budget.assert_text_model("") == "模型名为空"

    def test_assert_text_model_unregistered_passes(self, usage_tmp, monkeypatch):
        """不在册的第三方模型放行（不强制登记）。"""
        monkeypatch.setenv("LLM_MODEL_REGISTRY", "deepseek-v4-pro-0813|llm|2099-12-31|on")
        assert llm_budget.assert_text_model("my-private-gpt") == ""

    def test_assert_text_model_registered_ok_passes(self, usage_tmp, monkeypatch):
        """在册 + 开启 + 文本类 + 未过期：通过。"""
        monkeypatch.setenv("LLM_MODEL_REGISTRY", "deepseek-v4-pro-0813|llm|2099-12-31|on")
        assert llm_budget.assert_text_model("deepseek-v4-pro-0813") == ""

    def test_assert_text_model_registered_off_rejected(self, usage_tmp, monkeypatch):
        """在册但开关=off：拒绝。"""
        monkeypatch.setenv("LLM_MODEL_REGISTRY", "glm-5.3|llm|2099-12-31|off")
        reason = llm_budget.assert_text_model("glm-5.3")
        assert reason and "已关闭" in reason

    def test_assert_text_model_audio_rejected(self, usage_tmp, monkeypatch):
        """在册但类型=audio（语音合成/识别）：拒绝，不能进文本链路。"""
        monkeypatch.setenv("LLM_MODEL_REGISTRY", "sambert-zh-v1|audio|2099-12-31|on")
        reason = llm_budget.assert_text_model("sambert-zh-v1")
        assert reason and "非文本对话" in reason

    def test_assert_text_model_expired_rejected(self, usage_tmp, monkeypatch):
        """在册但已过期：拒绝。"""
        monkeypatch.setenv("LLM_MODEL_REGISTRY", "qwen-old|llm|2020-01-01|on")
        reason = llm_budget.assert_text_model("qwen-old")
        assert reason and "到期" in reason


class TestCallChain:
    def test_model_chain_parsing(self):
        assert AIModelService.model_chain("a, b ,c") == ["a", "b", "c"]
        assert AIModelService.model_chain("") == []
        assert AIModelService.model_chain("only") == ["only"]

    def test_falls_back_in_order_on_quota(self, usage_tmp, monkeypatch):
        """第一个模型额度耗尽（403）→ 自动切第二个，顺序即配置顺序。"""
        seen: list[str] = []

        def handler(body):
            seen.append(body["model"])
            if body["model"] == "qwen3.7-flash-2026-06-01":
                return _resp(
                    403, {"code": "AllocationQuota.FreeTierOnly", "message": "quota"}
                )
            return _resp(
                200,
                {
                    "choices": [{"message": {"content": "ok"}}],
                    "usage": {"prompt_tokens": 12, "completion_tokens": 8},
                },
            )

        _patch_client(monkeypatch, handler)
        cfg = _config("qwen3.7-flash-2026-06-01,qwen3.7-flash-2026-07-15")
        result = json.loads(
            json.dumps(
                __import__("asyncio").run(
                    AIModelService.call_openai_compatible_api(cfg, _msgs())
                )
            )
        )
        assert result["choices"][0]["message"]["content"] == "ok"
        assert seen == ["qwen3.7-flash-2026-06-01", "qwen3.7-flash-2026-07-15"]
        assert llm_budget.snapshot()["used_tokens"] == 20

    def test_paid_model_skipped_before_any_request(self, usage_tmp, monkeypatch):
        """链头是付费 max：护栏应在发请求前跳过，只调免费档。"""
        seen: list[str] = []

        def handler(body):
            seen.append(body["model"])
            return _resp(
                200, {"choices": [{"message": {"content": "ok"}}], "usage": {}}
            )

        _patch_client(monkeypatch, handler)
        cfg = _config("qwen3.8-max-0902,qwen3.7-flash-2026-07-15")
        __import__("asyncio").run(
            AIModelService.call_openai_compatible_api(cfg, _msgs())
        )
        assert seen == ["qwen3.7-flash-2026-07-15"]

    def test_all_models_blocked_raises(self, usage_tmp, monkeypatch):
        seen: list[str] = []

        def handler(body):
            seen.append(body["model"])
            return _resp(200, {"choices": []})

        _patch_client(monkeypatch, handler)
        cfg = _config("qwen3.8-max-0902")
        with pytest.raises(Exception) as exc:
            __import__("asyncio").run(
                AIModelService.call_openai_compatible_api(cfg, _msgs())
            )
        assert "全部模型均不可用" in str(exc.value)
        assert seen == [], "被护栏拦下的模型不该真的发出请求"

    def test_daily_budget_stops_chain(self, usage_tmp, monkeypatch):
        monkeypatch.setenv("LLM_DAILY_TOKEN_BUDGET", "50")
        llm_budget.record_usage("qwen3.7-flash", 40, 40)
        seen: list[str] = []

        def handler(body):
            seen.append(body["model"])
            return _resp(200, {"choices": []})

        _patch_client(monkeypatch, handler)
        cfg = _config("qwen3.7-flash,qwen-turbo")
        with pytest.raises(llm_budget.BudgetExceeded):
            __import__("asyncio").run(
                AIModelService.call_openai_compatible_api(cfg, _msgs())
            )
        assert seen == []

    def test_max_tokens_clamped_by_guard(self, usage_tmp, monkeypatch):
        monkeypatch.setenv("LLM_CALL_MAX_OUTPUT_TOKENS", "777")
        captured: dict = {}

        def handler(body):
            captured.update(body)
            return _resp(200, {"choices": [], "usage": {}})

        _patch_client(monkeypatch, handler)
        cfg = _config("qwen3.7-flash", max_tokens=99999)
        __import__("asyncio").run(
            AIModelService.call_openai_compatible_api(cfg, _msgs())
        )
        assert captured["max_tokens"] == 777

    def test_usage_file_records_today(self, usage_tmp, monkeypatch):
        def handler(body):
            return _resp(
                200,
                {
                    "choices": [{"message": {"content": "x"}}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 20},
                },
            )

        _patch_client(monkeypatch, handler)
        cfg = _config("qwen3.7-flash")
        __import__("asyncio").run(
            AIModelService.call_openai_compatible_api(cfg, _msgs())
        )
        raw = json.loads(usage_tmp.read_text(encoding="utf-8"))
        assert raw[date.today().isoformat()]["total"] == 120
