"""LLM 厂商连接测试探针：用 OpenAI 兼容端点做一次最小 chat 调用。

安全约定：
- base_url / api_key 由调用方传入（前端表单或已存配置），本模块只负责探活；
- 不持久化、不回显明文 key（接口层只返回脱敏预览与测试结果）；
- 连接失败统一翻译为可读原因。
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlparse

import httpx
from core.errors import ValidationError


def _is_localhost(base_url: str) -> bool:
    """判断端点是否为本机地址（localhost / 127.0.0.1 / ::1）。

    本机环境存在透明代理（HTTP_PROXY 指向 127.0.0.1），localhost 必须绕过代理直连，
    否则会被代理返回 502；远程端点则仍走代理以获得出网能力。
    """
    try:
        host = (urlparse(base_url or "").hostname or "").lower()
    except Exception:
        return False
    return host in ("localhost", "127.0.0.1", "::1", "0.0.0.0")


def probe_llm(base_url: str, api_key: str, model: str = "") -> dict[str, Any]:
    """最小连通性测试：发起一次 chat.completions.create（系统提示 ping）。

    返回 {ok, message, duration_ms, model_tested}。
    """
    bu = (base_url or "").strip()
    ak = api_key or ""
    if not bu:
        raise ValidationError("base_url 为空")
    test_model = (model or "").strip()
    started = time.monotonic()
    try:
        from openai import OpenAI
    except ImportError:
        return {
            "ok": False,
            "message": "未安装 openai 依赖，无法测试",
            "duration_ms": 0,
            "model_tested": test_model,
        }

    try:
        # 本机端点绕过透明代理直连；远程端点走代理出网。max_retries=0 避免失败时空转重试。
        trust_env = not _is_localhost(bu)
        client = OpenAI(
            api_key=ak,
            base_url=bu,
            timeout=15,
            max_retries=0,
            http_client=httpx.Client(trust_env=trust_env),
        )
        # 未指定模型时尝试自动列举第一个（兼容 OpenAI/DashScope 等）
        if not test_model:
            try:
                data = client.models.list().data
                test_model = getattr(next(iter(data), None), "id", "") or ""
            except Exception:
                test_model = ""
        if not test_model:
            return {
                "ok": False,
                "message": "未提供模型名且无法自动列举模型，请手动指定模型",
                "duration_ms": int((time.monotonic() - started) * 1000),
                "model_tested": "",
            }
        client.chat.completions.create(
            model=test_model,
            messages=[{"role": "system", "content": "ping"}],
            max_tokens=4,
            temperature=0,
        )
        return {
            "ok": True,
            "message": f"连接成功（模型 {test_model} 可用）",
            "duration_ms": int((time.monotonic() - started) * 1000),
            "model_tested": test_model,
        }
    except Exception as exc:  # noqa: BLE001 - 探活需统一兜底为可读原因
        return {
            "ok": False,
            "message": _translate_llm_error(exc),
            "duration_ms": int((time.monotonic() - started) * 1000),
            "model_tested": test_model,
        }


def _translate_llm_error(exc: Exception) -> str:
    status = getattr(exc, "status_code", None)
    msg = (str(getattr(exc, "message", "")) + " " + str(exc)).strip()
    if status == 401:
        return "鉴权失败（401）：API Key 无效或未授权"
    if status == 403:
        return "禁止访问（403）：Key 无权限或额度不足"
    if status == 404:
        return "模型/端点不存在（404）：检查 base_url 与模型名"
    if status == 429:
        return "限流（429）：额度耗尽或被限流"
    low = msg.lower()
    if "connection" in low or "resolve" in low or "timed out" in low:
        return f"连接失败：{msg[:160]}"
    return msg[:200]
