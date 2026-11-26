"""PW-3b 3b-1：只读 attach 调试后门守护测试。

覆盖：默认关闭、CDP URL 脱敏、profile 构造字段、只读探测调用链、
异常时仍 detach、日志不泄露 token、不接执行链。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import types

import pytest

from apps.ui_automation import debug_attach


class _FakeSession:
    def __init__(self, calls: list[tuple[str, object]], fail_connect: bool = False):
        self.calls = calls
        self.fail_connect = fail_connect

    async def connect(self, cdp_url=None):
        self.calls.append(("connect", cdp_url))
        if self.fail_connect:
            raise RuntimeError("connect failed")

    async def get_current_page_title(self):
        self.calls.append(("title", None))
        return "demo-page"

    async def get_current_page_url(self):
        self.calls.append(("page_url", None))
        return "http://demo.example/path?x=1"

    async def get_browser_state_summary(self, **kwargs):
        self.calls.append(("summary", tuple(sorted(kwargs.items()))))
        dom_state = types.SimpleNamespace(selector_map={1: object(), 2: object()})
        return types.SimpleNamespace(dom_state=dom_state)

    async def stop(self):
        self.calls.append(("stop", None))


class TestConfigAndRedaction:
    def test_default_cdp_url_is_empty(self, monkeypatch):
        monkeypatch.delenv("UI_CDP_URL", raising=False)
        assert debug_attach.configured_cdp_url() == ""

    def test_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv("UI_CDP_URL", raising=False)
        with pytest.raises(debug_attach.DebugAttachDisabled):
            debug_attach.resolve_cdp_url()

    def test_resolve_reads_env(self, monkeypatch):
        monkeypatch.setenv("UI_CDP_URL", "http://127.0.0.1:9222")
        assert debug_attach.resolve_cdp_url() == "http://127.0.0.1:9222"

    @pytest.mark.parametrize(
        "raw",
        [
            "ftp://127.0.0.1:9222",
            "http://",
            "http://127.0.0.1:not-a-port",
        ],
    )
    def test_reject_invalid_url_without_echoing_value(self, raw):
        with pytest.raises(debug_attach.DebugAttachConfigError) as exc:
            debug_attach.resolve_cdp_url(raw)
        assert raw not in str(exc.value)

    def test_safe_label_drops_token_userinfo_and_path(self):
        raw = "http://user:SECRET_USER@127.0.0.1:9222/path?token=SECRET_TOKEN#frag"
        label = debug_attach.safe_cdp_label(raw)
        assert label == "127.0.0.1:9222"
        assert "SECRET_USER" not in label
        assert "SECRET_TOKEN" not in label
        assert "path" not in label

    def test_safe_label_handles_invalid_value(self):
        assert debug_attach.safe_cdp_label("not-a-url") == "<invalid-cdp-url>"


class TestProfileConstruction:
    def test_injected_factory_receives_attach_kwargs(self):
        captured: dict[str, object] = {}

        def fake_factory(**kwargs):
            captured.update(kwargs)
            return object()

        debug_attach.build_attach_profile(
            "http://127.0.0.1:9222?token=SECRET_TOKEN",
            profile_factory=fake_factory,
        )
        assert captured == {
            "cdp_url": "http://127.0.0.1:9222?token=SECRET_TOKEN",
            "user_data_dir": None,
            "is_local": False,
            "executable_path": None,
        }

    def test_real_profile_uses_cdp_without_local_executable(self):
        profile = debug_attach.build_attach_profile("http://127.0.0.1:9222")
        assert profile.cdp_url == "http://127.0.0.1:9222"
        assert profile.is_local is False
        assert profile.executable_path is None


class TestReadOnlyProbe:
    def test_probe_is_read_only_and_detaches(self, caplog):
        calls: list[tuple[str, object]] = []
        captured: dict[str, object] = {}

        def fake_profile(**kwargs):
            captured.update(kwargs)
            return {"kind": "attach-profile"}

        def fake_session(browser_profile):
            calls.append(("session", browser_profile))
            return _FakeSession(calls)

        raw = "http://127.0.0.1:9222?token=SECRET_TOKEN"
        with caplog.at_level(logging.INFO, logger="django"):
            result = asyncio.run(
                debug_attach.probe_attached_page(
                    raw,
                    session_factory=fake_session,
                    profile_factory=fake_profile,
                )
            )

        assert result == debug_attach.AttachProbeResult(
            title="demo-page",
            url="http://demo.example/path?x=1",
            element_count=2,
        )
        assert captured["cdp_url"] == raw
        assert calls[0] == ("session", {"kind": "attach-profile"})
        assert ("connect", raw) in calls
        assert (
            "summary",
            (("cached", False), ("include_screenshot", False)),
        ) in calls
        assert calls[-1] == ("stop", None)
        assert "SECRET_TOKEN" not in caplog.text
        assert "127.0.0.1:9222" in caplog.text

    def test_connect_failure_still_detaches_and_redacts_log(self, caplog):
        calls: list[tuple[str, object]] = []

        def fake_session(browser_profile):
            return _FakeSession(calls, fail_connect=True)

        raw = "http://127.0.0.1:9222?token=SECRET_TOKEN"
        with (
            caplog.at_level(logging.WARNING, logger="django"),
            pytest.raises(RuntimeError, match="connect failed"),
        ):
            asyncio.run(
                debug_attach.probe_attached_page(
                    raw,
                    session_factory=fake_session,
                    profile_factory=lambda **_: object(),
                )
            )

        assert calls == [("connect", raw), ("stop", None)]
        assert "SECRET_TOKEN" not in caplog.text
        assert "RuntimeError" in caplog.text


class TestExecutionBoundary:
    def test_module_has_no_task_execution_entry(self):
        src = inspect.getsource(debug_attach)
        assert "run_task" not in src
        assert "Agent(" not in src
        assert "navigate_to" not in src
        assert "click(" not in src
        assert "fill(" not in src

    def test_main_disabled_exit_code_is_two(self, monkeypatch, capsys):
        monkeypatch.delenv("UI_CDP_URL", raising=False)
        assert debug_attach.main([]) == 2
        err = capsys.readouterr().err
        assert "默认关闭" in err

    def test_main_json_output(self, monkeypatch, capsys):
        async def fake_probe():
            return debug_attach.AttachProbeResult(
                title="demo-page",
                url="http://demo.example/path",
                element_count=3,
            )

        monkeypatch.setattr(debug_attach, "probe_attached_page", fake_probe)
        assert debug_attach.main(["--json"]) == 0
        out = capsys.readouterr().out.strip()
        assert '"title": "demo-page"' in out
        assert '"element_count": 3' in out
