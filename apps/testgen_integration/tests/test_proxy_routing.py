"""`/api/v1/` 兜底代理的命名空间归属判定（纯离线，不依赖 sidecar/DB）。

背景：`re_path(r'^api/v1/(?P<rest>.*)$')` 是全前缀通配，会把 `/api/v1/users/login/`
这类主平台路径一并吃掉——匿名得到 403「未授权」（语义误导），已登录则把请求体
（含明文密码）转发给 sidecar。本用例锁定修复后的行为：

- 平台路径 → 404 + 正确路径提示，且**绝不转发**；
- sidecar 路径 → 照常转发；
- 匿名访问 sidecar 路径 → 仍然 403（原防护不回退）。
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.test import RequestFactory

from apps.testgen_integration import proxy


@pytest.fixture()
def rf():
    return RequestFactory()


def _authed():
    return SimpleNamespace(is_authenticated=True)


class _FakeResponse:
    content = b'{"ok": true}'
    status_code = 200

    def __init__(self):
        self.headers: dict = {}


@pytest.mark.parametrize(
    ("rest", "expected_hint"),
    [
        ("users/login/", "/api/users/login/"),
        ("auth/login/", "/api/auth/login/"),
        ("testcases/", "/api/testcases/"),
        ("api-testing/tasks/", "/api/api-testing/tasks/"),
        ("monitor/", "/api/monitor/"),
    ],
)
def test_platform_path_returns_404_with_hint(rf, rest, expected_hint):
    req = rf.post(f"/api/v1/{rest}", data="{}", content_type="application/json")
    req.user = _authed()

    with patch.object(proxy.requests, "request") as mocked:
        resp = proxy.testgen_api_proxy(req, rest=rest)

    assert resp.status_code == 404
    assert not mocked.called, "平台路径绝不能转发给 sidecar（会外流请求体/凭据）"
    body = json.loads(resp.content)
    assert body["path"] == f"/api/v1/{rest}"
    assert expected_hint in body["hint"]


@pytest.mark.parametrize(
    "rest", ["projects", "generate", "tasks/abc123", "describe", "pipeline", "repos"]
)
def test_sidecar_path_still_forwarded(rf, rest):
    req = rf.post(f"/api/v1/{rest}", data="{}", content_type="application/json")
    req.user = _authed()

    with patch.object(
        proxy.requests, "request", return_value=_FakeResponse()
    ) as mocked:
        resp = proxy.testgen_api_proxy(req, rest=rest)

    assert mocked.called
    assert resp.status_code == 200
    assert mocked.call_args.kwargs["url"].endswith(f"/api/v1/{rest}")


def test_projects_is_sidecar_despite_platform_having_same_name():
    """`/api/projects/` 与 sidecar 的 `/api/v1/projects` 同名，不得判成平台路径。"""
    assert proxy._platform_path_hint("projects") is None
    assert proxy._platform_path_hint("projects/12/cases") is None


def test_anonymous_sidecar_path_still_forbidden(rf):
    req = rf.get("/api/v1/generate")
    req.user = SimpleNamespace(is_authenticated=False)

    with patch.object(proxy.requests, "request") as mocked:
        resp = proxy.testgen_api_proxy(req, rest="generate")

    assert resp.status_code == 403
    assert "未授权的代理访问" in resp.content.decode()
    assert not mocked.called


def test_platform_segments_derived_from_urlpatterns():
    segments = proxy._platform_top_segments()
    for expected in ("users", "auth", "testcases", "api-testing", "monitor", "mcp"):
        assert expected in segments


# ---------------------------------------------------------------------------
# 专属前缀 /testgen-api/v1/（B2）：命名空间已隔离，不再做归属判定
# ---------------------------------------------------------------------------


def test_dedicated_prefix_always_forwards(rf):
    """专属前缀下，即使路径长得像平台接口也必须转发（这是 sidecar 独占空间）。"""
    req = rf.post(
        "/testgen-api/v1/users/login/", data="{}", content_type="application/json"
    )
    req.user = _authed()

    with patch.object(
        proxy.requests, "request", return_value=_FakeResponse()
    ) as mocked:
        resp = proxy.testgen_api_proxy(req, rest="users/login/", dedicated=True)

    assert mocked.called
    assert resp.status_code == 200
    assert mocked.call_args.kwargs["url"].endswith("/api/v1/users/login/")


def test_console_page_injects_api_prefix(rf):
    """打开 /testgen/ 时注入专属前缀，控制台的 apiUrl() 据此改写全部请求。"""
    req = rf.get("/testgen/")
    req.user = _authed()

    with patch.object(
        proxy.requests, "request", return_value=_FakeResponse()
    ) as mocked:
        proxy.testgen_web_proxy(req, rest="")

    assert mocked.called
    assert (
        mocked.call_args.kwargs["headers"]["X-Testhub-Api-Prefix"] == "/testgen-api/v1"
    )


def test_client_cannot_forge_api_prefix(rf):
    """客户端自带的同名头必须被丢弃——前缀只能由代理注入，防止被诱导请求到任意前缀。"""
    req = rf.get("/testgen/", headers={"X-Testhub-Api-Prefix": "/evil"})
    req.user = _authed()

    with patch.object(
        proxy.requests, "request", return_value=_FakeResponse()
    ) as mocked:
        proxy.testgen_web_proxy(req, rest="")

    sent = mocked.call_args.kwargs["headers"]
    assert sent["X-Testhub-Api-Prefix"] == "/testgen-api/v1"
    assert "/evil" not in sent.values()


def test_bare_testgen_redirects_to_trailing_slash(rf):
    """`/testgen` → `/testgen/`：页面内相对路径（static/chart…）依赖尾斜杠。"""
    req = rf.get("/testgen?embed=1")
    req.user = _authed()

    resp = proxy.testgen_web_proxy(req, rest="")

    assert resp.status_code == 302
    assert resp["Location"] == "/testgen/?embed=1"


def test_static_asset_forwarded_not_redirected(rf):
    """rest 非空时不得重定向（否则 /testgen/static/x 会被拼成 …x/ 而 404）。"""
    req = rf.get("/testgen/static/chart.umd.min.js")
    req.user = _authed()

    with patch.object(
        proxy.requests, "request", return_value=_FakeResponse()
    ) as mocked:
        resp = proxy.testgen_web_proxy(req, rest="static/chart.umd.min.js")

    assert resp.status_code == 200
    assert mocked.call_args.kwargs["url"].endswith("/static/chart.umd.min.js")


# ---------------------------------------------------------------------------
# 旧 /api/v1/ 通道命中计数（B3 下线判据）
# ---------------------------------------------------------------------------


def test_legacy_hit_counter_emits_window_summary(monkeypatch, caplog):
    """累计到阈值即输出一条汇总日志，且含窗口增量与路径分布。"""
    import logging

    monkeypatch.setattr(proxy, "_LEGACY_FLUSH_EVERY", 3)
    monkeypatch.setattr(proxy, "_legacy_hits", {})
    monkeypatch.setattr(proxy, "_legacy_flushed", {})
    monkeypatch.setattr(proxy, "_legacy_total", 0)
    monkeypatch.setattr(proxy, "_legacy_misroute", 0)
    monkeypatch.setattr(proxy, "_legacy_flushed_total", 0)

    with caplog.at_level(logging.INFO, logger="apps.testgen_integration.proxy"):
        for _ in range(3):
            proxy._record_legacy_hit("tasks/1")
        # 第 4 次未达阈值（且未满 5 分钟），不应再输出
        proxy._record_legacy_hit("compare/2")

    msgs = [
        r.getMessage()
        for r in caplog.records
        if "旧 /api/v1/ 通道命中" in r.getMessage()
    ]
    assert len(msgs) == 1, f"阈值内应只输出一条汇总，实际 {len(msgs)}"
    assert "本窗口 +3" in msgs[0]
    assert "tasks=3" in msgs[0]


def test_legacy_proxy_records_hit_but_dedicated_does_not(rf, monkeypatch):
    """只有旧通道（dedicated=False）记账；专属前缀不记；归属纠正标记 misroute。"""
    calls: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        proxy,
        "_record_legacy_hit",
        lambda rest, misroute=False: calls.append((rest, misroute)),
    )

    req = rf.get("/api/v1/tasks/abc")
    req.user = _authed()
    with patch.object(proxy.requests, "request", return_value=_FakeResponse()):
        proxy.testgen_api_proxy(req, rest="tasks/abc")
    assert calls == [("tasks/abc", False)]

    req2 = rf.get("/testgen-api/v1/tasks/abc")
    req2.user = _authed()
    with patch.object(proxy.requests, "request", return_value=_FakeResponse()):
        proxy.testgen_api_proxy(req2, rest="tasks/abc", dedicated=True)
    assert len(calls) == 1, "专属前缀不属于旧通道，不应记账"

    req3 = rf.post("/api/v1/users/login/", data="{}", content_type="application/json")
    req3.user = _authed()
    proxy.testgen_api_proxy(req3, rest="users/login/")
    assert calls[-1] == ("users/login/", True), "平台路径被旧前缀吃掉时要标记为归属纠正"
