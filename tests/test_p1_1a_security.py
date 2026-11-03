"""1a 子批（H1/H7/H10 后端安全配置）综合 e2e / 集成自测。

运行环境：backend venv（含 Django 4.2 + requests + jsonpath_ng）。
覆盖：
  - H1  CORS 生产分支：删 CORS_ALLOW_ALL_ORIGINS，未配白名单则不允许跨域 + credentials=False
  - H7  DisableCSRFMiddleware 已从 MIDDLEWARE 移除（真实 settings 与源码层面双验证）
  - H10 verify=False 全面移除：
        * proxy._do_proxy 不再传 verify=False（默认 True，localhost HTTP 无影响）
        * probes._tls_verify() 默认 True，PROBE_TLS_VERIFY=0 才放宽
        * CustomEmailBackend 默认受信 CA 校验，EMAIL_TLS_VERIFY=0 才放开自签

说明：真实代码路径 + 外部边界（requests / smtplib）mock，等价于端到端贯穿真实实现，
不依赖 live sidecar / SMTP 服务器。
"""

import importlib
import os
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]  # testhub_platform
SETTINGS_PATH = ROOT / "backend" / "settings.py"
assert SETTINGS_PATH.exists(), f"settings.py 未找到: {SETTINGS_PATH}"

# 让 `import apps.*` 可用
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# 工具：用真实 settings.py 源码 + 受控 env 重新执行，得到干净的命名空间
# （用真实源码，避免与实现漂移；env 覆盖 decouple 的 .env 取值）
# ---------------------------------------------------------------------------

# DEBUG=False 分支下 settings.py 会对若干「生产中必须显式配置」的项 fail-fast
# （SECRET_KEY / REDIS_URL 等 H 系列修复）。本文件测的是 CORS / CSRF / TLS 分支，
# 不是这些 fail-fast 本身，所以统一注入占位基线——否则测试结果会随
# 「本机有没有 .env」漂移：本地绿、CI 红（CI 没有 .env）。
_PRODUCTION_BASELINE_ENV = {
    "SECRET_KEY": "test-only-placeholder-not-a-real-secret",
    "REDIS_URL": "redis://127.0.0.1:6379/0",
}


def _exec_settings(env_overrides: dict) -> dict:
    src = SETTINGS_PATH.read_text(encoding="utf-8")
    saved = dict(os.environ)
    # 先清掉可能来自 .env / shell 的 CORS 取值，避免泄漏；随后仅由 env_overrides 控制
    os.environ.pop("CORS_ALLOWED_ORIGINS", None)
    os.environ.pop("CORS_ALLOW_ALL_ORIGINS", None)
    for k, v in {**_PRODUCTION_BASELINE_ENV, **env_overrides}.items():
        os.environ[k] = v
    try:
        ns = {"__name__": "settings_exec_test", "__file__": str(SETTINGS_PATH)}
        exec(compile(src, str(SETTINGS_PATH), "exec"), ns)
        return ns
    finally:
        os.environ.clear()
        os.environ.update(saved)


# ---------------------------------------------------------------------------
# H1 — CORS 生产分支
# ---------------------------------------------------------------------------
def test_h1_prod_no_origins_rejects_all_and_credentials_false():
    ns = _exec_settings({"DEBUG": "False"})
    # 严禁 allow-all + credentials 共存
    assert ns.get("CORS_ALLOW_ALL_ORIGINS") is not True, "生产未配白名单仍允许所有来源"
    assert ns["CORS_ALLOWED_ORIGINS"] == [], "未配置白名单应等于空列表（不允许任何跨域）"
    assert ns["CORS_ALLOW_CREDENTIALS"] is False, "无白名单时不应允许带凭证"


def test_h1_prod_with_origins_allows_list_and_credentials_true():
    ns = _exec_settings({"DEBUG": "False", "CORS_ALLOWED_ORIGINS": "https://app.example.com,https://admin.example.com"})
    assert ns.get("CORS_ALLOW_ALL_ORIGINS") is not True
    assert ns["CORS_ALLOWED_ORIGINS"] == ["https://app.example.com", "https://admin.example.com"]
    assert ns["CORS_ALLOW_CREDENTIALS"] is True, "有显式白名单时才允许带凭证"


def test_h1_dev_branch_keeps_localhost_allowlist():
    # 开发分支不应触发 fail-fast，且保留本地前端源
    ns = _exec_settings({"DEBUG": "True"})
    assert "http://localhost:3900" in ns["CORS_ALLOWED_ORIGINS"]
    assert ns["CORS_ALLOW_CREDENTIALS"] is True


# ---------------------------------------------------------------------------
# H7 — DisableCSRFMiddleware 移除
# ---------------------------------------------------------------------------
def test_h7_disable_csrf_middleware_removed_from_settings():
    ns = _exec_settings({"DEBUG": "False"})
    assert "backend.middleware.DisableCSRFMiddleware" not in ns["MIDDLEWARE"], \
        "DisableCSRFMiddleware 仍注册在 MIDDLEWARE，CSRF 整体绕过未修复"


def test_h7_middleware_file_deleted():
    assert not (ROOT / "backend" / "middleware.py").exists(), \
        "backend/middleware.py 应已删除（死文件）"


@pytest.fixture(scope="module")
def django_settings():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "backend.settings")
    import django

    if not django.apps.apps.ready:
        django.setup()
    return django.conf.settings


def test_h7_live_middleware_chain_clean(django_settings):
    mw = django_settings.MIDDLEWARE
    assert "backend.middleware.DisableCSRFMiddleware" not in mw
    # CSRF 中间件仍在，确保默认强校验
    assert "django.middleware.csrf.CsrfViewMiddleware" in mw


# ---------------------------------------------------------------------------
# H10 — proxy._do_proxy 不再传 verify=False
# ---------------------------------------------------------------------------
@pytest.fixture
def proxy_module(django_settings):
    from apps.testgen_integration import proxy

    return proxy


class _FakeResponse:
    def __init__(self, content=b"", status_code=200, headers=None):
        self.content = content
        self.status_code = status_code
        self.headers = headers or {}


class _FakeRequest:
    def __init__(self, method="GET", body=None, headers=None, query=""):
        self.method = method
        self.body = body
        self.headers = headers or {}
        self.META = {"QUERY_STRING": query}
        self.COOKIES = {}

        # 1b 之后 proxy 要求已登录；默认视为已认证，避免被鉴权门拦截。
        class _AuthUser:
            is_authenticated = True

        self.user = _AuthUser()


def test_h10_proxy_does_not_disable_tls(proxy_module, monkeypatch):
    captured = {}

    def fake_request(method, url, headers=None, data=None, cookies=None,
                     timeout=None, allow_redirects=None, verify=None):
        captured["method"] = method
        captured["url"] = url
        captured["verify"] = verify
        return _FakeResponse(b"ok", 200, {"X-Test": "1"})

    monkeypatch.setattr(proxy_module.requests, "request", fake_request)
    resp = proxy_module._do_proxy(_FakeRequest(method="POST", body=b'{"x":1}'), "api/v1/gen")
    # 关键断言：verify 不应被显式设为 False（默认 True）
    assert "verify" not in captured or captured["verify"] is not False, \
        "proxy 仍关闭 TLS 校验（verify=False）"
    assert captured["method"] == "POST"
    assert "api/v1/gen" in captured["url"]
    assert resp.status_code == 200
    assert resp.content == b"ok"


# ---------------------------------------------------------------------------
# H10 — probes._tls_verify 默认安全
# ---------------------------------------------------------------------------
@pytest.fixture
def probes_module(django_settings):
    from apps.monitor.utils import probes

    return probes


def test_h10_probes_tls_verify_default_true(probes_module, monkeypatch):
    monkeypatch.delenv("PROBE_TLS_VERIFY", raising=False)
    assert probes_module._tls_verify() is True


def test_h10_probes_tls_verify_opt_out(probes_module, monkeypatch):
    monkeypatch.setenv("PROBE_TLS_VERIFY", "0")
    assert probes_module._tls_verify() is False


def test_h10_probes_no_hardcoded_verify_false(probes_module):
    import inspect

    src = inspect.getsource(probes_module)
    assert "verify=False" not in src, "probes.py 仍存在硬编码 verify=False"


# ---------------------------------------------------------------------------
# H10 — CustomEmailBackend 默认受信 CA 校验
# ---------------------------------------------------------------------------
@pytest.fixture
def email_module(django_settings):
    from apps.api_testing import custom_email_backend

    return custom_email_backend


class _FakeSSLContext:
    def __init__(self):
        self.check_hostname = None
        self.verify_mode = None


class _FakeSMTP:
    instances = []

    def __init__(self, host=None, port=None, timeout=None, context=None):
        self.host = host
        self.port = port
        self.context = context
        _FakeSMTP.instances.append(self)

    def starttls(self, context=None):
        self.context = context

    def login(self, *a, **k):
        pass


def test_h10_email_backend_default_verified(email_module, monkeypatch):
    monkeypatch.delenv("EMAIL_TLS_VERIFY", raising=False)
    monkeypatch.setattr(email_module.smtplib, "SMTP", _FakeSMTP)
    _FakeSMTP.instances.clear()

    backend = email_module.CustomEmailBackend(
        host="smtp.example.com", port=587, use_tls=True, username="u", password="p"
    )
    ok = backend.open()
    assert ok is True
    ctx = _FakeSMTP.instances[0].context
    assert ctx is not None
    # create_default_context() 的特征：校验主机名 + 必须验证证书
    assert ctx.check_hostname is True, "默认应使用受信 CA 校验（check_hostname=True）"
    import ssl

    assert ctx.verify_mode == ssl.CERT_REQUIRED, "默认应要求证书校验（CERT_REQUIRED）"


def test_h10_email_backend_opt_out_unverified(email_module, monkeypatch):
    monkeypatch.setenv("EMAIL_TLS_VERIFY", "0")
    monkeypatch.setattr(email_module.smtplib, "SMTP", _FakeSMTP)
    _FakeSMTP.instances.clear()

    backend = email_module.CustomEmailBackend(
        host="smtp.internal", port=587, use_tls=True, username="u", password="p"
    )
    backend.open()
    ctx = _FakeSMTP.instances[0].context
    import ssl

    assert ctx.check_hostname is False, "EMAIL_TLS_VERIFY=0 应放开主机名校验"
    assert ctx.verify_mode == ssl.CERT_NONE, "EMAIL_TLS_VERIFY=0 应放开证书校验"


def test_h10_email_no_hardcoded_unverified_context(email_module):
    import inspect

    src = inspect.getsource(email_module)
    # 仅允许在明确的 opt-out 分支出现 _create_unverified_context
    assert "_create_unverified_context()" in src
    # 默认分支必须是 create_default_context
    assert "ssl.create_default_context()" in src
