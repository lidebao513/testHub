"""PW-3a 命名会话注册表守护测试。

覆盖方案 (c)：alias 校验/穿越拒绝、save→load 往返、缺失返回 None、clear 幂等、
文件权限、日志不含 Cookie，以及「默认关闭 / 三要素齐备才生效」的开关纪律。

测试夹具一律使用**假 Cookie**（不含真实凭据），符合 PW-7 密钥卫生要求。
"""

import logging
import os
import stat

import pytest

from apps.ui_automation import session_registry as SR


@pytest.fixture
def fake_state():
    return {
        "cookies": [
            {
                "name": "sid",
                "value": "FAKE_COOKIE_VALUE_NOT_REAL_123",
                "domain": "example.test",
                "path": "/",
                "expires": -1,
            }
        ],
        "origins": [
            {
                "origin": "http://example.test",
                "localStorage": [{"name": "token", "value": "FAKE_LOCALSTORAGE_VALUE"}],
            }
        ],
    }


@pytest.fixture
def registry(tmp_path):
    return SR.SessionRegistry(tmp_path / "states")


class TestAliasValidation:
    def test_accepts_expected_aliases(self):
        for alias in ("admin", "user_1", "role-a", "A" * 64):
            assert SR.validate_alias(alias) == alias

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "../etc/passwd",
            "a/b",
            "a\\b",
            "..",
            "has space",
            "中文",
            "x" * 65,
            None,
            123,
            "/abs/path",
        ],
    )
    def test_rejects_traversal_and_invalid(self, bad):
        with pytest.raises(SR.SessionRegistryError):
            SR.validate_alias(bad)

    def test_path_stays_inside_state_dir(self, registry):
        path = registry._path_for("admin")
        assert path.parent == registry.state_dir
        assert path.name == "admin.storage_state.json"


class TestRoundTrip:
    def test_save_then_load(self, registry, fake_state):
        path = registry.save("admin", fake_state)
        assert path.is_file()
        assert registry.load("admin") == fake_state

    def test_missing_load_returns_none(self, registry):
        assert registry.load("nobody") is None

    def test_clear_is_idempotent(self, registry, fake_state):
        registry.save("admin", fake_state)
        assert registry.clear("admin") is True
        assert registry.clear("admin") is True
        assert registry.has("admin") is False

    def test_different_aliases_do_not_mix(self, registry, fake_state):
        admin = dict(fake_state, cookies=[dict(fake_state["cookies"][0], value="ADMIN_FAKE")])
        user = dict(fake_state, cookies=[dict(fake_state["cookies"][0], value="USER_FAKE")])
        registry.save("admin", admin)
        registry.save("user", user)
        assert registry.load("admin")["cookies"][0]["value"] == "ADMIN_FAKE"
        assert registry.load("user")["cookies"][0]["value"] == "USER_FAKE"

    def test_corrupted_state_degrades_to_none(self, registry, tmp_path, caplog):
        # 结构不合法的 state 禁止写入
        with pytest.raises(SR.SessionRegistryError):
            registry.save("admin", {"cookies": []})
        # 直接落一个损坏文件，验证 load 降级而不是抛异常
        path = registry._path_for("admin")
        path.write_text("not-a-json", encoding="utf-8")
        with caplog.at_level(logging.WARNING):
            assert registry.load("admin") is None
        assert "降级为全新会话" in caplog.text


class TestSecurityHygiene:
    def test_no_cookie_in_logs(self, registry, fake_state, caplog):
        with caplog.at_level(logging.DEBUG):
            registry.save("admin", fake_state)
            registry.load("admin")
            registry.has("admin")
            registry.clear("admin")
        assert "FAKE_COOKIE_VALUE_NOT_REAL_123" not in caplog.text
        assert "FAKE_LOCALSTORAGE_VALUE" not in caplog.text
        assert "admin" in caplog.text

    @pytest.mark.skipif(os.name == "nt", reason="Windows chmod 语义不同，POSIX 校验 0600")
    def test_file_permission_is_0600(self, registry, fake_state):
        path = registry.save("admin", fake_state)
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == stat.S_IRUSR | stat.S_IWUSR

    def test_empty_state_dir_rejected(self):
        with pytest.raises(SR.SessionRegistryError):
            SR.SessionRegistry("")


class TestSwitchDiscipline:
    def test_default_disabled(self, monkeypatch):
        monkeypatch.delenv("UI_SESSION_ALIAS_ENABLED", raising=False)
        monkeypatch.delenv("UI_SESSION_STATE_DIR", raising=False)
        assert SR.alias_enabled() is False
        assert SR.configured_state_dir() == ""
        assert SR.is_named_session_active("admin") is False

    @pytest.mark.parametrize("raw", ["0", "false", "no", "off", "", "random"])
    def test_falsy_values_keep_disabled(self, monkeypatch, raw):
        monkeypatch.setenv("UI_SESSION_ALIAS_ENABLED", raw)
        assert SR.alias_enabled() is False

    @pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on"])
    def test_truthy_values_enable(self, monkeypatch, raw):
        monkeypatch.setenv("UI_SESSION_ALIAS_ENABLED", raw)
        assert SR.alias_enabled() is True

    def test_active_requires_all_three(self, monkeypatch, tmp_path):
        monkeypatch.setenv("UI_SESSION_ALIAS_ENABLED", "true")
        monkeypatch.setenv("UI_SESSION_STATE_DIR", str(tmp_path))
        assert SR.is_named_session_active("admin") is True
        assert SR.is_named_session_active("../etc") is False
        assert SR.is_named_session_active(None) is False

    def test_enabled_but_no_dir_is_inactive(self, monkeypatch, tmp_path):
        """开关开但目录未配置 → 不生效（避免落到缺省目录写凭据）。"""
        monkeypatch.setenv("UI_SESSION_ALIAS_ENABLED", "true")
        monkeypatch.delenv("UI_SESSION_STATE_DIR", raising=False)
        assert SR.is_named_session_active("admin") is False
