"""M8 回归（直接调用视图，避免 urlconf 导入破坏 pytest capture；HTTP 链路见 e2e_1d_live.py）。

验证点：
- 注册 /api/auth/register/ 返回 {success,user,access,refresh}（201），并下发
  HttpOnly + SameSite=Lax 的 refresh_token Cookie（前端绝不持有 refresh）。
- /api/auth/token/refresh/ 仅凭 HttpOnly Cookie 即可换发新 access（前端无需传 refresh）。
- /api/auth/logout/ 清除 refresh_token Cookie。
"""
from __future__ import annotations

import json

import pytest
from django.test import RequestFactory

from apps.users import sms as sms_module

# 短信验证码前置闸门在 RegisterView 内通过 _sms_module.validate_verify_code 调用，
# 这里整体放行，避免连真实 Redis。
pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _patch_sms(monkeypatch):
    monkeypatch.setattr(sms_module, "validate_verify_code", lambda phone, token, code: (True, ""))


def _register_payload(username: str) -> dict:
    return {
        "username": username,
        "email": f"{username}@example.com",
        "phone": "13800138000",
        "password": "Str0ng!Pass9",
        "password_confirm": "Str0ng!Pass9",
        "verify_code": "123456",
        "verify_code_token": "tok",
    }


def _call_register(payload: dict):
    from apps.users.views import RegisterView

    req = RequestFactory().post(
        "/api/auth/register/", data=json.dumps(payload), content_type="application/json"
    )
    resp = RegisterView.as_view()(req)
    if hasattr(resp, "render"):
        resp.render()
    return resp


def _call_view(view, url: str, refresh_token: str | None = None):
    req = RequestFactory().post(
        url, data=json.dumps({}), content_type="application/json",
        HTTP_COOKIE=f"refresh_token={refresh_token}" if refresh_token else "",
    )
    resp = view(req)
    if hasattr(resp, "render"):
        resp.render()
    return resp


def test_register_returns_jwt_and_sets_httponly_cookie():
    resp = _call_register(_register_payload("m8_reg_1"))
    assert resp.status_code == 201, resp.content
    body = json.loads(resp.content)
    assert body.get("success") is True
    assert {"access", "refresh", "user"} <= set(body)

    cookie = resp.cookies.get("refresh_token")
    assert cookie is not None, "必须下发 refresh_token Cookie"
    assert cookie["httponly"] is True
    assert (cookie.get("samesite") or "").lower() == "lax"


def test_cookie_refresh_issues_new_access_without_body_refresh():
    resp = _call_register(_register_payload("m8_reg_2"))
    assert resp.status_code == 201
    refresh_token = resp.cookies["refresh_token"].value

    # 仅发空 body；refresh 由 HttpOnly Cookie 携带，前端不持有 refresh
    from apps.users.views import cookie_token_refresh

    r2 = _call_view(cookie_token_refresh, "/api/auth/token/refresh/", refresh_token)
    assert r2.status_code == 200, r2.content
    assert "access" in json.loads(r2.content)


def test_logout_clears_refresh_cookie():
    resp = _call_register(_register_payload("m8_reg_3"))
    refresh_token = resp.cookies["refresh_token"].value

    from apps.users.views import logout_view

    r2 = _call_view(logout_view, "/api/auth/logout/", refresh_token)
    assert r2.status_code == 200, r2.content

    cleared = r2.cookies.get("refresh_token")
    assert cleared is not None, "logout 必须清除 refresh_token Cookie"
    assert cleared.value == "" or int(cleared.get("max-age", "1")) <= 0
