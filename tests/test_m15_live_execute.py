"""M15 真实链路自测：出站请求的 auth 注入 + 响应体截断落库。

用一个进程内 HTTP 服务充当被测接口：
  - 记录收到的 ``Authorization`` 头（验证 ApiRequest.auth 真的被注入出站请求）；
  - ``/big`` 返回 3MB JSON（> 2MB 上限）→ 落库应截断且 ``truncated=True``；
  - ``/small`` 返回小 JSON → ``truncated=False`` 且 json 可解析。

用 SQLite 测试库（backend.test_settings），不依赖 MySQL/Redis。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.api_testing.models import ApiRequest, RequestHistory
from apps.api_testing.utils import DEFAULT_MAX_RESPONSE_BYTES
from apps.api_testing.views import ApiRequestViewSet

pytestmark = pytest.mark.django_db

BIG_BYTES = 3 * 1024 * 1024


class _EchoHandler(BaseHTTPRequestHandler):
    received = {}

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler 接口名固定
        type(self).received["authorization"] = self.headers.get("Authorization")
        type(self).received["path"] = self.path
        if self.path.startswith("/big"):
            body = json.dumps({"data": "x" * BIG_BYTES}).encode("utf-8")
        else:
            body = json.dumps({"ok": True, "value": 42}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # 静默，避免污染输出
        pass


@pytest.fixture()
def echo_server():
    server = HTTPServer(("127.0.0.1", 0), _EchoHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _EchoHandler.received.clear()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    thread.join(timeout=3)


def _execute(url, user, auth=None):
    """通过 ApiRequestViewSet.execute 发起真实出站请求，返回 (response, history)。"""
    api_request = ApiRequest.objects.create(
        name="m15-live",
        method="GET",
        url=url,
        headers={},
        params={},
        body={},
        auth=auth or {},
        created_by=user,
    )
    factory = APIRequestFactory()
    request = factory.post(
        f"/api/api-testing/requests/{api_request.id}/execute/", {}, format="json"
    )
    force_authenticate(request, user=user)
    view = ApiRequestViewSet.as_view({"post": "execute"})
    response = view(request, pk=api_request.id)
    response.render()
    history = RequestHistory.objects.filter(request=api_request).first()
    return response, history


def test_bearer_auth_injected_into_outbound_request(echo_server):
    user = get_user_model().objects.create_user(
        username="m15_auth_user", password="M15pass!2026"
    )
    response, history = _execute(
        f"{echo_server}/small", user, {"type": "bearer", "token": "m15-e2e-token"}
    )
    assert response.status_code == 200
    assert _EchoHandler.received.get("authorization") == "Bearer m15-e2e-token"
    assert history is not None
    assert history.status_code == 200


def test_big_response_truncated_and_flagged(echo_server):
    user = get_user_model().objects.create_user(
        username="m15_big_user", password="M15pass!2026"
    )
    response, history = _execute(f"{echo_server}/big", user)
    assert response.status_code == 200
    assert history is not None
    data = history.response_data
    assert data["truncated"] is True, "超过上限必须标记 truncated"
    assert len(data["body"]) <= DEFAULT_MAX_RESPONSE_BYTES, "落库体积不得超过上限"
    # 截断后的 JSON 不完整 → json 字段应为 None（不抛异常、不写半截结构）
    assert data["json"] is None


def test_small_response_not_truncated_and_json_parsed(echo_server):
    user = get_user_model().objects.create_user(
        username="m15_small_user", password="M15pass!2026"
    )
    _, history = _execute(f"{echo_server}/small", user)
    data = history.response_data
    assert data["truncated"] is False
    assert data["json"] == {"ok": True, "value": 42}
