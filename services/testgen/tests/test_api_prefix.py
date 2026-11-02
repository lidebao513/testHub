"""控制台 API 前缀注入（B1）：`/` 按请求注入前缀，直连时默认 `/api/v1` 不变。

背景：控制台前端原本把 API 路径写死成 `/api/v1/`，与主平台 `/api/v1/` 兜底代理
共用命名空间。改造后前缀由服务端注入，前端 `apiUrl()` 据此改写。本用例锁定：

- 无参数（直连 8910）→ 默认 `/api/v1`，与改造前完全一致；
- 请求头 `X-Testhub-Api-Prefix` 优先于 `?api_prefix=`；
- 非法前缀（注入脚本 / 路径穿越）→ 回退默认，绝不写进 HTML；
- 静态资源不受影响。
"""

import pytest
from fastapi.testclient import TestClient

from service.app import app


_PLACEHOLDER = "__TESTGEN_API_PREFIX__"


@pytest.fixture()
def client():
    return TestClient(app, raise_server_exceptions=False)


def _injected(html: str) -> str:
    """取出注入到 `const injected = "...";` 的值。"""
    marker = 'const injected = "'
    start = html.index(marker) + len(marker)
    return html[start : html.index('"', start)]


def test_default_prefix_keeps_legacy_behaviour(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert _injected(resp.text) == "/api/v1"
    assert _PLACEHOLDER not in resp.text, (
        "占位符必须被替换，否则前端会请求到 __TESTGEN_API_PREFIX__"
    )


def test_header_injects_custom_prefix(client):
    resp = client.get("/", headers={"X-Testhub-Api-Prefix": "/testgen-api/v1"})
    assert resp.status_code == 200
    assert _injected(resp.text) == "/testgen-api/v1"


def test_query_injects_custom_prefix(client):
    resp = client.get("/?api_prefix=/testgen-api/v1")
    assert resp.status_code == 200
    assert _injected(resp.text) == "/testgen-api/v1"


def test_header_takes_precedence_over_query(client):
    resp = client.get(
        "/?api_prefix=/from-query",
        headers={"X-Testhub-Api-Prefix": "/from-header"},
    )
    assert _injected(resp.text) == "/from-header"


@pytest.mark.parametrize(
    "evil",
    [
        "/</script><script>alert(1)</script>",
        '/api/v1";alert(1);//',
        "/../etc/passwd",
        "no-leading-slash",
        "/has space",
    ],
)
def test_illegal_prefix_falls_back_and_is_not_injected(client, evil):
    resp = client.get("/", params={"api_prefix": evil})
    assert resp.status_code == 200
    assert _injected(resp.text) == "/api/v1"
    assert "alert(1)" not in resp.text
    assert "passwd" not in resp.text


def test_trailing_slash_is_normalized(client):
    resp = client.get("/", headers={"X-Testhub-Api-Prefix": "/testgen-api/v1/"})
    assert _injected(resp.text) == "/testgen-api/v1"


def test_no_store_header(client):
    """前缀随请求变化，必须禁止缓存，否则切换代理后会读到旧值。"""
    resp = client.get("/")
    assert "no-store" in resp.headers.get("cache-control", "")


def test_static_assets_unaffected(client):
    resp = client.get("/static/chart.umd.min.js")
    assert resp.status_code == 200
