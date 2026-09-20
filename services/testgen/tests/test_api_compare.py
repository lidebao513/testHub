"""POST /api/v1/compare 端点测试（P0-2）。

验证点：
- 单条 / 批量路由正确，响应结构含 verdict；
- comparator 禁用（无 LLM）时仍返回规则判定（确定、零外部依赖）；
- LLMError 降级时端点返回 inconclusive 而非 500（诚实降级）；
- 配置了 AUTH_TOKEN 时要求 X-Auth-Token（复用 require_auth）。

设计为纯计算端点，入参不含任何凭证字段；comparator 已对 reason/diff 脱敏。
LLM 调用全部 mock，门禁不会被实时网络拖垮。
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from core.errors import LLMError
from engine.comparator import ComparatorOptions, CompareVerdict
from service import app as service_app


@pytest.fixture()
def client():
    return TestClient(service_app.app, raise_server_exceptions=False)


def _verdict(verdict: str = "pass", rule: str = "pass") -> CompareVerdict:
    return CompareVerdict(
        verdict=verdict,
        confidence=0.9,
        reason="ok",
        diff=[],
        rule_verdict=rule,
        model="qwen3.7-flash",
    )


def test_single_compare_returns_verdict(client):
    """单条比对：响应含 verdict，且透传 rule_verdict。"""
    fake = _verdict("pass", "pass")
    with patch("service.app.compare_one", return_value=fake) as m:
        resp = client.post(
            "/api/v1/compare",
            json={"case": {"id": "C1", "title": "t"}, "actual": {"status": "PASS"}},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert "verdict" in body
    assert body["verdict"]["verdict"] == "pass"
    assert body["verdict"]["rule_verdict"] == "pass"
    m.assert_called_once()


def test_batch_compare_returns_verdicts(client):
    """批量比对：items 非空时走 compare_batch，返回 verdicts 列表 + count。"""
    fakes = [_verdict("pass", "pass"), _verdict("fail", "fail")]
    with patch("service.app.compare_batch", return_value=fakes) as m:
        resp = client.post(
            "/api/v1/compare",
            json={
                "items": [
                    {"case": {"id": "C1"}, "actual": {"status": "PASS"}},
                    {"case": {"id": "C2"}, "actual": {"status": "FAIL"}},
                ]
            },
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 2
    assert [v["verdict"] for v in body["verdicts"]] == ["pass", "fail"]
    m.assert_called_once()


def test_compare_rule_only_when_comparator_disabled(client):
    """comparator 未启用（无 LLM 凭据）时，仍返回规则判定，不触 LLM。"""
    disabled = MagicMock()
    disabled.enabled = False
    with patch("service.app.ComparatorOptions.from_settings", return_value=disabled):
        resp = client.post(
            "/api/v1/compare",
            json={"case": {"id": "C1"}, "actual": {"status": "PASS"}},
        )
    assert resp.status_code == 200
    assert resp.json()["verdict"]["verdict"] == "pass"


def test_compare_degradation_inconclusive_on_llm_error(client):
    """LLMError 降级：端点应返回 inconclusive（rule_verdict 透传），绝不 500。"""
    enabled = ComparatorOptions(enabled=True, api_key="k", base_url="u", model="m")
    with (
        patch("service.app.ComparatorOptions.from_settings", return_value=enabled),
        patch("engine.comparator.compare.chat_with_fallback", side_effect=LLMError("quota")),
    ):
        resp = client.post(
            "/api/v1/compare",
            json={"case": {"id": "C1"}, "actual": {"status": "PASS"}},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["verdict"]["verdict"] == "inconclusive"
    assert body["verdict"]["rule_verdict"] == "pass"


def test_compare_requires_auth_when_configured(client):
    """复用 require_auth：配置了 AUTH_TOKEN 时缺 token → 401，带 token → 200。"""
    with patch.object(service_app.settings, "auth_token", "secret"):
        resp = client.post(
            "/api/v1/compare",
            json={"case": {"id": "C1"}, "actual": {"status": "PASS"}},
        )
        assert resp.status_code == 401
        resp2 = client.post(
            "/api/v1/compare",
            json={"case": {"id": "C1"}, "actual": {"status": "PASS"}},
            headers={"X-Auth-Token": "secret"},
        )
        assert resp2.status_code == 200
