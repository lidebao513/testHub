"""统一 LLM 降级链单测（engine/llm_fallback.chat_with_fallback）。

不触网：用替身 client 模拟「模型不可用（403 + AllocationQuota.FreeTierOnly.）」与
「真实错误（401 鉴权）」两类异常，验证：
- 命中额度不可用 → 按 model_chain 顺序切到下一个模型；
- 全链失败 → 抛 LLMError("all models in chain failed")；
- 真实错误（401）→ 不降级，直接失败；
- 进程内缓存：二次调用直接命中已验证模型，不再重测坏模型（快速切换）；
- 视觉兜底：候选模型非视觉时自动剥离 image_url，避免打断降级链。
"""

from __future__ import annotations

import types
from typing import ClassVar
from unittest.mock import patch

import pytest

from core.errors import LLMError
from engine import llm_fallback


def _resp(text: str):
    class _Msg:
        content = text

    class _Choice:
        message = _Msg()

    class _Resp:
        choices: ClassVar[list] = [_Choice()]

    return _Resp()


def _quota_err(model: str) -> Exception:
    class _Err(Exception):
        status_code = 403
        message = f"Error: AllocationQuota.FreeTierOnly. model={model} not available"

    return _Err(f"quota {model}")


def _client(handler):
    """构造替身 OpenAI client：client.chat.completions.create == handler。"""
    return types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=handler))
    )


def test_fallback_to_next_on_quota():
    llm_fallback.reset_fallback_cache()
    seen: list[str] = []

    def handler(model, messages, temperature):
        seen.append(model)
        if model == "m0":
            raise _quota_err("m0")  # 免费额度不可用
        return _resp('[{"title":"ok"}]')

    with patch("openai.OpenAI", return_value=_client(handler)):
        resp = llm_fallback.chat_with_fallback(
            channel="llm",
            api_key="k",
            base_url="https://x",
            timeout=10,
            model="m0",
            model_chain=["m0", "m1", "m2"],
            messages=[{"role": "user", "content": "hi"}],
        )
    assert resp.choices[0].message.content == '[{"title":"ok"}]'
    assert seen == ["m0", "m1"]  # m0 失败 → 切 m1 成功

    # 缓存命中 m1：二次调用不再试坏模型 m0
    seen.clear()
    with patch("openai.OpenAI", return_value=_client(handler)):
        llm_fallback.chat_with_fallback(
            channel="llm",
            api_key="k",
            base_url="https://x",
            timeout=10,
            model="m0",
            model_chain=["m0", "m1", "m2"],
            messages=[{"role": "user", "content": "hi"}],
        )
    assert seen == ["m1"]


def test_all_fail_raises():
    llm_fallback.reset_fallback_cache()

    def handler(model, messages, temperature):
        raise _quota_err(model)

    with patch("openai.OpenAI", return_value=_client(handler)):
        try:
            llm_fallback.chat_with_fallback(
                channel="llm",
                api_key="k",
                base_url="https://x",
                timeout=10,
                model="m0",
                model_chain=["m0", "m1"],
                messages=[{"role": "user", "content": "hi"}],
            )
            pytest.fail("应抛出 LLMError")
        except LLMError as e:
            assert "all models in chain failed" in str(e)


def test_real_error_not_fallback():
    llm_fallback.reset_fallback_cache()

    def handler(model, messages, temperature):
        class _Err(Exception):
            status_code = 401
            message = "Invalid API key"

        raise _Err("auth")

    with patch("openai.OpenAI", return_value=_client(handler)):
        try:
            llm_fallback.chat_with_fallback(
                channel="llm",
                api_key="k",
                base_url="https://x",
                timeout=10,
                model="m0",
                model_chain=["m0", "m1"],
                messages=[{"role": "user", "content": "hi"}],
            )
            pytest.fail("应抛出 LLMError")
        except LLMError as e:
            assert "all models in chain failed" not in str(e)
            assert "m0 调用失败" in str(e)


def test_message_hint_triggers_fallback_without_status():
    """仅错误文本含额度关键字（无 status_code）也应降级。"""
    llm_fallback.reset_fallback_cache()
    seen: list[str] = []

    def handler(model, messages, temperature):
        seen.append(model)
        if model == "m0":

            class _Err(Exception):
                message = "allocationquota.freetieronly. model stopped"

            raise _Err("stopped")
        return _resp("[]")

    with patch("openai.OpenAI", return_value=_client(handler)):
        llm_fallback.chat_with_fallback(
            channel="llm",
            api_key="k",
            base_url="https://x",
            timeout=10,
            model="m0",
            model_chain=["m0", "m1"],
            messages=[{"role": "user", "content": "hi"}],
        )
    assert seen == ["m0", "m1"]


def test_vision_strip_on_non_vision_model():
    """候选模型非视觉时，image_url 应被剥离、user 消息折叠为纯文本（不打断降级）。"""
    llm_fallback.reset_fallback_cache()
    seen: list = []

    def handler(model, messages, temperature):
        seen.append((model, messages))
        return _resp("[]")

    msgs = [
        {"role": "system", "content": "s"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "hi"},
                {"type": "image_url", "url": "data:image/png;base64,xxx"},
            ],
        },
    ]
    with patch("openai.OpenAI", return_value=_client(handler)):
        llm_fallback.chat_with_fallback(
            channel="expert",
            api_key="k",
            base_url="https://x",
            timeout=10,
            model="qwen3.8-flash",
            model_chain=["qwen3.8-flash"],
            messages=msgs,
        )
    _, user_msg = seen[0]
    assert user_msg[1]["content"] == "hi"  # image_url 已被剥离


def test_transient_timeout_falls_back_to_next():
    """瞬时网络超时（APITimeoutError）应重试后降级到下一模型，而非直接判死。

    复现初版缺口：dashscope 端点偶发超时曾导致整条链路挂死（仅认 403/404/429）。
    修复后：同一模型超时先退避重试，耗尽后跳下一模型直至成功。
    """
    llm_fallback.reset_fallback_cache()
    seen: list[str] = []

    class _TimeoutErr(Exception):
        pass

    def handler(model, messages, temperature):
        seen.append(model)
        if model == "m0":
            # 首次超时，重试一次后再超时；之后切 m1 成功
            raise _TimeoutErr("Request timed out")
        return _resp("[]")

    with patch("openai.OpenAI", return_value=_client(handler)):
        resp = llm_fallback.chat_with_fallback(
            channel="llm",
            api_key="k",
            base_url="https://x",
            timeout=10,
            model="m0",
            model_chain=["m0", "m1"],
            messages=[{"role": "user", "content": "hi"}],
        )
    assert resp.choices[0].message.content == "[]"
    # m0 首试 1 次 + 重试 _MAX_TRANSIENT_RETRY 次均超时 → 跳 m1 成功
    assert seen.count("m0") == 1 + llm_fallback._MAX_TRANSIENT_RETRY
    assert seen[-1] == "m1"


def test_auth_error_still_hard_fails():
    """401 鉴权失败仍须直接判死（不降级、不重试），与超时降级区分开。"""
    llm_fallback.reset_fallback_cache()

    def handler(model, messages, temperature):
        class _Err(Exception):
            status_code = 401
            message = "Invalid API key"

        raise _Err("auth")

    with patch("openai.OpenAI", return_value=_client(handler)):
        try:
            llm_fallback.chat_with_fallback(
                channel="llm",
                api_key="k",
                base_url="https://x",
                timeout=10,
                model="m0",
                model_chain=["m0", "m1"],
                messages=[{"role": "user", "content": "hi"}],
            )
            pytest.fail("应抛出 LLMError")
        except LLMError as e:
            assert "all models in chain failed" not in str(e)
            assert "m0 调用失败" in str(e)


def test_vision_model_keeps_images():
    """视觉模型（qwen-vl）保留 image_url。"""
    llm_fallback.reset_fallback_cache()
    seen: list = []

    def handler(model, messages, temperature):
        seen.append((model, messages))
        return _resp("[]")

    msgs = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "hi"},
                {"type": "image_url", "url": "data:image/png;base64,xxx"},
            ],
        },
    ]
    with patch("openai.OpenAI", return_value=_client(handler)):
        llm_fallback.chat_with_fallback(
            channel="expert",
            api_key="k",
            base_url="https://x",
            timeout=10,
            model="qwen-vl-max",
            model_chain=["qwen-vl-max"],
            messages=msgs,
        )
    _, user_msg = seen[0]
    assert isinstance(user_msg[0]["content"], list)
    assert any(p.get("type") == "image_url" for p in user_msg[0]["content"])
