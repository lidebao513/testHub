"""1d 子批安全整改自测（H5 api_key 明文 / M3 静态目录暴露 / M5 测试注册路由）。

覆盖：
- H5：落库密文、幂等单层加密、明文历史数据兼容、序列化器不再回显明文、
  新建必须给 Key、局部更新不清空 Key、出站调用改用解密值；
- M3：匿名 401、登录后 200 + nosniff、路径穿越/隐藏文件/非白名单类型/
  目录请求一律 404；
- M5：异常不回显原始内容、字段校验委托正式序列化器（弱密码/两次密码
  不一致被拒）、成功路径仍返回前端所需的 access/refresh。

用 SQLite 测试库（backend.test_settings），不依赖 MySQL/Redis。
"""
import json
import pathlib

import pytest
from django.contrib.auth import get_user_model
from django.test import RequestFactory

from apps.api_testing.models import AIServiceConfig
from apps.app_automation.static_views import protected_serve

pytestmark = pytest.mark.django_db

User = get_user_model()
ROOT = pathlib.Path(__file__).resolve().parents[1]
TEMPLATE_ROOT = ROOT / "apps" / "app_automation" / "Template"

PLAIN_KEY = "sk-plain-0001-abcdef"


def _make_user(username: str) -> "User":
    return User.objects.create_user(username=username, password="Passw0rd!23")


def _make_config(user, api_key: str = PLAIN_KEY, base_url: str = "http://127.0.0.1:9"):
    return AIServiceConfig.objects.create(
        name="h5-config",
        service_type="openai",
        role="description",
        api_key=api_key,
        base_url=base_url,
        model_name="gpt-4o-mini",
        created_by=user,
    )


# --------------------------------------------------------------------------- #
# H5 —— api_key 落库加密
# --------------------------------------------------------------------------- #
def test_h5_api_key_encrypted_at_rest():
    cfg = _make_config(_make_user("h5-at-rest"))
    cfg.refresh_from_db()
    assert cfg.api_key.startswith("gAAAAA"), "落库必须是 Fernet 密文"
    assert PLAIN_KEY not in cfg.api_key, "库内不得残留明文"
    assert cfg.get_api_key() == PLAIN_KEY


def test_h5_repeated_save_stays_single_layer():
    cfg = _make_config(_make_user("h5-idempotent"))
    cfg.name = "renamed"
    cfg.save()
    cfg.refresh_from_db()
    assert cfg.get_api_key() == PLAIN_KEY, "重复保存不得套娃加密"


def test_h5_legacy_plaintext_still_usable():
    """历史明文数据（未经 save 加密）仍可正常取出，保证平滑升级。"""
    cfg = _make_config(_make_user("h5-legacy"))
    AIServiceConfig.objects.filter(id=cfg.id).update(api_key="sk-legacy-raw-1")
    cfg.refresh_from_db()
    assert cfg.get_api_key() == "sk-legacy-raw-1"


def test_h5_masked_key_hides_secret():
    cfg = _make_config(_make_user("h5-mask"))
    masked = cfg.masked_api_key()
    assert masked.endswith("****")
    assert PLAIN_KEY not in masked
    assert masked.startswith(PLAIN_KEY[:8])


def test_h5_serializer_never_returns_plaintext():
    from apps.api_testing.serializers import AIServiceConfigSerializer

    cfg = _make_config(_make_user("h5-serializer"))
    data = AIServiceConfigSerializer(cfg).data
    assert "api_key" not in data, "api_key 必须 write_only"
    assert data["api_key_masked"].endswith("****")
    assert PLAIN_KEY not in json.dumps(data, default=str)


def test_h5_create_requires_api_key():
    from rest_framework import serializers as drf_serializers

    from apps.api_testing.serializers import AIServiceConfigSerializer

    ser = AIServiceConfigSerializer(data={
        "name": "no-key", "service_type": "openai", "role": "description",
        "base_url": "http://127.0.0.1:9", "model_name": "m",
    })
    with pytest.raises(drf_serializers.ValidationError) as exc:
        ser.is_valid(raise_exception=True)
    assert "api_key" in exc.value.detail


def test_h5_partial_update_keeps_existing_key():
    from apps.api_testing.serializers import AIServiceConfigSerializer

    cfg = _make_config(_make_user("h5-partial"))
    ser = AIServiceConfigSerializer(cfg, data={"name": "only-name"}, partial=True)
    assert ser.is_valid(), ser.errors
    ser.save()
    cfg.refresh_from_db()
    assert cfg.get_api_key() == PLAIN_KEY


def test_h5_outbound_calls_use_decrypted_key():
    """静态守卫：views 中不得再直接读 `config.api_key`。"""
    src = (ROOT / "apps" / "api_testing" / "views.py").read_text(encoding="utf-8")
    assert "config.api_key" not in src, "出站调用必须使用 get_api_key()"
    assert src.count("config.get_api_key()") == 5, "5 处出站调用应全部改为解密值"


# --------------------------------------------------------------------------- #
# M3 —— 静态目录鉴权
# 说明：本仓库 import 完整 urlconf 会破坏 pytest 的 capture 流（已知问题），
# 因此这里直接调用视图函数验证逻辑；真实 HTTP 链路在
# tests/e2e_1d_live.py 中验证（独立进程，不受 capture 影响）。
# --------------------------------------------------------------------------- #
def _request(method="get", authenticated=True, **kwargs):
    req = getattr(RequestFactory(), method)("/x", **kwargs)

    class _U:
        is_authenticated = authenticated

    req.user = _U()
    return req


def test_m3_anonymous_template_access_denied():
    anon = RequestFactory().get("/x")
    from django.contrib.auth.models import AnonymousUser

    anon.user = AnonymousUser()
    resp = protected_serve(anon, "common/plus_sign.png", document_root=str(TEMPLATE_ROOT))
    assert resp.status_code == 401


def test_m3_authenticated_template_access_ok():
    resp = protected_serve(
        _request(), "common/plus_sign.png", document_root=str(TEMPLATE_ROOT)
    )
    assert resp.status_code == 200
    assert resp["X-Content-Type-Options"] == "nosniff"
    assert int(resp["Content-Length"]) > 0


@pytest.mark.parametrize(
    "rel",
    [
        "../backend/settings.py",
        "..\\..\\backend\\settings.py",
        "common/../../backend/settings.py",
        ".env",
        "common/.git/config",
        "evil.py",
        "script.sh",
        "",
    ],
)
def test_m3_unsafe_paths_rejected(rel, tmp_path):
    (tmp_path / "evil.py").write_text("print(1)", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=1", encoding="utf-8")
    (tmp_path / "script.sh").write_text("echo 1", encoding="utf-8")
    resp = protected_serve(_request(), rel, document_root=str(tmp_path))
    assert resp.status_code == 404


def test_m3_directory_listing_denied(tmp_path):
    (tmp_path / "common").mkdir()
    resp = protected_serve(_request(), "common", document_root=str(tmp_path))
    assert resp.status_code == 404


def test_m3_allowed_file_served_with_nosniff(tmp_path):
    (tmp_path / "index.html").write_text("<html>report</html>", encoding="utf-8")
    resp = protected_serve(_request(), "index.html", document_root=str(tmp_path))
    assert resp.status_code == 200
    assert resp["X-Content-Type-Options"] == "nosniff"


def test_m3_urlconf_uses_protected_serve():
    src = (ROOT / "backend" / "urls.py").read_text(encoding="utf-8")
    assert "from django.views.static import serve" not in src
    assert "protected_serve" in src


# --------------------------------------------------------------------------- #
# M5 —— 正式注册接口（已移除 test-register，统一走 RegisterView）
# 直接用 APIRequestFactory 调用视图（避免 urlconf 导入破坏 capture）；HTTP 链路见 e2e_1d_live.py
# --------------------------------------------------------------------------- #
REGISTER_URL = "/api/auth/register/"


def _post(payload):
    from apps.users.views import RegisterView

    body = payload if isinstance(payload, str) else json.dumps(payload)
    req = RequestFactory().post(
        REGISTER_URL, data=body, content_type="application/json"
    )
    resp = RegisterView.as_view()(req)
    # DRF Response 为惰性渲染，读 .content 前需显式 render
    if hasattr(resp, "render"):
        resp.render()
    return resp


def test_m5_requires_verify_code():
    resp = _post({"phone": "13800000001", "username": "m5-nocode"})
    assert resp.status_code == 400
    assert "短信验证码" in json.loads(resp.content)["error"]


def test_m5_invalid_json():
    resp = _post("not-json")
    assert resp.status_code == 400
    body = json.loads(resp.content)
    assert "JSON" in (body.get("error") or body.get("detail") or ""), body


def test_m5_invalid_phone():
    resp = _post({"phone": "123", "verify_code": "1", "verify_code_token": "t"})
    assert resp.status_code == 400
    assert "手机号" in json.loads(resp.content)["error"]


def test_m5_exception_detail_not_leaked(monkeypatch):
    from apps.users import sms

    def boom(*args, **kwargs):
        raise RuntimeError("SECRET_DB_ERROR_xyz table users missing at /srv/app/db.py")

    monkeypatch.setattr(sms, "validate_verify_code", boom)
    resp = _post({
        "phone": "13800000002", "verify_code": "1234", "verify_code_token": "tok",
    })
    body = resp.content.decode("utf-8")
    message = json.loads(resp.content)["error"]
    assert resp.status_code == 400
    assert "SECRET_DB_ERROR_xyz" not in body, "异常原文不得回显"
    assert "SECRET_DB_ERROR_xyz" not in message
    assert "/srv/app/db.py" not in body
    assert "注册失败" in message, "应返回通用提示"


def test_m5_weak_password_rejected_by_formal_serializer(monkeypatch):
    from apps.users import sms

    monkeypatch.setattr(sms, "validate_verify_code", lambda *a, **k: (True, ""))
    resp = _post({
        "phone": "13800000003", "verify_code": "1234", "verify_code_token": "tok",
        "username": "m5-weak", "password": "123", "password_confirm": "123",
    })
    assert resp.status_code == 400
    assert not User.objects.filter(username="m5-weak").exists(), "校验失败不得建号"
    assert "password" in (json.loads(resp.content).get("error") or ""), "错误应包含 password 字段提示"


def test_m5_password_mismatch_rejected(monkeypatch):
    from apps.users import sms

    monkeypatch.setattr(sms, "validate_verify_code", lambda *a, **k: (True, ""))
    resp = _post({
        "phone": "13800000004", "verify_code": "1234", "verify_code_token": "tok",
        "username": "m5-mismatch", "password": "abcdef", "password_confirm": "abcdeg",
    })
    assert resp.status_code == 400
    assert not User.objects.filter(username="m5-mismatch").exists()


def test_m5_success_still_returns_tokens(monkeypatch):
    from apps.users import sms

    monkeypatch.setattr(sms, "validate_verify_code", lambda *a, **k: (True, ""))
    resp = _post({
        "phone": "13800000005", "verify_code": "1234", "verify_code_token": "tok",
        "username": "m5-ok", "password": "abcdef", "password_confirm": "abcdef",
        "email": "m5ok@example.com",
    })
    assert resp.status_code == 201, resp.content[:400]
    data = json.loads(resp.content)
    assert data["success"] is True
    assert data["user"]["username"] == "m5-ok"
    assert data["access"] and data["refresh"], "前端依赖 access/refresh"
    assert User.objects.filter(username="m5-ok").exists()


def test_m5_no_raw_exception_echo_in_source():
    src = (ROOT / "apps" / "users" / "views.py").read_text(encoding="utf-8")
    assert "'error': str(e)" not in src
    assert "str(exc)" not in src
    assert "logger.exception" in src
    assert "set_refresh_cookie" in src, "register 应种 HttpOnly refresh Cookie"
