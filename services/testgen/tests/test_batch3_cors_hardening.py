"""Batch 3 B-2 收尾守护：testgen sidecar CORS 来源收窄。

契约（与主平台 backend/settings.py 同口径）：
- prod 未配白名单 → 不允许任何跨域（fail-closed）；
- dev/test 未配白名单 → 保留通配（本机/局域网演示，旧行为不变）；
- 配了白名单 → 只用白名单；credentials 仅在有白名单且不含通配时开启，
  杜绝 `allow_origins=["*"] + allow_credentials=True`。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from core.config import load_settings, resolve_cors_origins
from service.app import app


@pytest.fixture()
def client():
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize(
    ("app_env", "whitelist", "expected_origins", "expected_credentials"),
    [
        ("prod", [], [], False),  # 生产未配白名单 → 拒绝所有跨域
        ("prod", ["https://app.example.com"], ["https://app.example.com"], True),
        ("prod", ["*"], ["*"], False),  # 显式通配也不开 credentials
        ("dev", [], ["*"], False),  # dev 未配 → 保留通配，但不带凭证
        ("test", [], ["*"], False),
        ("dev", ["http://localhost:3900"], ["http://localhost:3900"], True),
    ],
)
def test_resolve_cors_origins_matrix(app_env, whitelist, expected_origins, expected_credentials):
    origins, credentials = resolve_cors_origins(app_env, whitelist)
    assert origins == expected_origins
    assert credentials is expected_credentials


def test_load_settings_parses_cors_whitelist():
    """完整 load_settings 链路：CORS_ALLOWED_ORIGINS 逗号分隔解析。"""
    s = load_settings(
        env={"CORS_ALLOWED_ORIGINS": " https://a.example.com, , https://b.example.com "}
    )
    assert s.cors_allowed_origins == ["https://a.example.com", "https://b.example.com"]
    assert "cors_allowed_origins" in s.public_dict(), "白名单可观测但不泄露密钥"


def test_load_settings_cors_defaults_empty():
    s = load_settings(env={})
    assert s.cors_allowed_origins == []


def test_dev_env_allows_origin_but_no_credentials(client):
    """dev 环境（测试默认）：通配来源放行，但不回带 Access-Control-Allow-Credentials。"""
    resp = client.get("/health", headers={"Origin": "https://any.example.com"})
    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") == "*"
    assert resp.headers.get("access-control-allow-credentials") is None
