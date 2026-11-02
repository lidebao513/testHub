"""M15 自测：响应体落库上限（stream + 截断 + truncated 标记）与 auth 注入。

纯离线：用假 response 对象验证 ``read_response_body`` / ``read_response_json`` /
``apply_auth``，不触网络。

写法说明：使用 pytest 函数风格（而非 unittest.TestCase）——本仓库的 pytest capture
与 Django 上下文配合时，unittest 风格用例在 Windows 下会出现
"I/O operation on closed file"；同仓其他测试统一用函数 + django_db 标记。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.django_db


def _utils():
    from apps.api_testing import utils

    return utils


class FakeResponse:
    """最小 requests.Response 替身，支持 stream / cached 两种读取路径。"""

    def __init__(self, body: bytes, headers=None, encoding="utf-8", streamed=False):
        self._body = body
        self._content = False if streamed else body
        self._content_consumed = False
        self.headers = headers or {}
        self.encoding = encoding
        self.status_code = 200
        self.closed = False

    def iter_content(self, chunk_size=65536):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i : i + chunk_size]

    @property
    def content(self):
        if self._content is False:
            self._content = self._body
        return self._content

    @property
    def text(self):
        return self.content.decode(self.encoding or "utf-8", errors="replace")

    def close(self):
        self.closed = True


# ---------------- read_response_body ----------------


def test_small_body_not_truncated():
    resp = FakeResponse(b'{"ok": true}', {"content-type": "application/json"})
    text, truncated = _utils().read_response_body(resp)
    assert text == '{"ok": true}'
    assert truncated is False


def test_stream_body_truncated_at_limit():
    resp = FakeResponse(b"x" * 5000, streamed=True)
    text, truncated = _utils().read_response_body(resp, max_bytes=1000)
    assert len(text) == 1000
    assert truncated is True
    assert resp.closed is True, "截断后应主动释放连接"


def test_cached_body_truncated_at_limit():
    resp = FakeResponse(b"y" * 5000)
    text, truncated = _utils().read_response_body(resp, max_bytes=2048)
    assert len(text) == 2048
    assert truncated is True


def test_exact_limit_not_truncated():
    resp = FakeResponse(b"z" * 1000, streamed=True)
    text, truncated = _utils().read_response_body(resp, max_bytes=1000)
    assert len(text) == 1000
    assert truncated is False, "恰好等于上限不应标记截断"


def test_response_text_usable_after_truncation():
    """截断后回填 _content，断言函数仍可读 response.text（不抛 content consumed）。"""
    resp = FakeResponse(b"a" * 3000, streamed=True)
    text, truncated = _utils().read_response_body(resp, max_bytes=500)
    assert truncated is True
    assert resp.text == text
    assert len(resp.text) == 500


def test_default_limit_is_2mb():
    assert _utils().DEFAULT_MAX_RESPONSE_BYTES == 2 * 1024 * 1024


def test_utf8_multibyte_decode():
    resp = FakeResponse("中文响应".encode(), streamed=True)
    text, truncated = _utils().read_response_body(resp)
    assert text == "中文响应"
    assert truncated is False


# ---------------- read_response_json ----------------


def test_json_parsed():
    assert _utils().read_response_json(
        '{"a": 1}', {"content-type": "application/json"}
    ) == {"a": 1}


def test_non_json_content_type_returns_none():
    assert (
        _utils().read_response_json('{"a": 1}', {"content-type": "text/html"}) is None
    )


def test_truncated_json_returns_none_without_raising():
    assert (
        _utils().read_response_json(
            '{"a": 1', {"content-type": "application/json; charset=utf-8"}
        )
        is None
    )


def test_missing_headers_returns_none():
    assert _utils().read_response_json('{"a": 1}', None) is None


# ---------------- apply_auth ----------------


def test_empty_auth_noop():
    headers, params, auth = _utils().apply_auth({"H": "v"}, {"p": "1"}, {})
    assert headers == {"H": "v"}
    assert params == {"p": "1"}
    assert auth is None


def test_none_auth_noop():
    headers, params, auth = _utils().apply_auth({}, {}, None)
    assert headers == {}
    assert auth is None


def test_bearer_injects_authorization_header():
    headers, _, auth = _utils().apply_auth({}, {}, {"type": "bearer", "token": "abc"})
    assert headers["Authorization"] == "Bearer abc"
    assert auth is None


def test_basic_returns_requests_auth_tuple():
    headers, _, auth = _utils().apply_auth(
        {}, {}, {"type": "basic", "username": "u", "password": "p"}
    )
    assert auth == ("u", "p")
    assert "Authorization" not in headers


def test_api_key_in_header():
    headers, params, _ = _utils().apply_auth(
        {}, {}, {"type": "api_key", "key": "X-API-Key", "value": "k1", "in": "header"}
    )
    assert headers["X-API-Key"] == "k1"
    assert params == {}


def test_api_key_in_query():
    headers, params, _ = _utils().apply_auth(
        {}, {}, {"type": "api_key", "key": "api_key", "value": "k2", "in": "query"}
    )
    assert params["api_key"] == "k2"
    assert "api_key" not in headers


def test_existing_authorization_header_wins():
    """用户显式设置的 Authorization 不被 auth 配置覆盖。"""
    headers, _, auth = _utils().apply_auth(
        {"Authorization": "Bearer manual"},
        {},
        {"type": "bearer", "token": "from-auth"},
    )
    assert headers["Authorization"] == "Bearer manual"
    assert auth is None


def test_empty_token_does_not_inject():
    headers, _, _ = _utils().apply_auth({}, {}, {"type": "bearer", "token": ""})
    assert "Authorization" not in headers


def test_unknown_type_noop():
    headers, params, auth = _utils().apply_auth(
        {"H": "v"}, {"p": "1"}, {"type": "digest"}
    )
    assert headers == {"H": "v"}
    assert params == {"p": "1"}
    assert auth is None


def test_alias_keys_supported():
    """兼容前端可能使用的 authType / access_token 别名。"""
    headers, _, _ = _utils().apply_auth(
        {}, {}, {"authType": "bearer", "access_token": "t9"}
    )
    assert headers["Authorization"] == "Bearer t9"
