"""3b-0：命名会话登录态"真回写"守护测试。

背景（PW-3a 假通过根因）：browser-use 0.10.1 的 StorageStateWatchdog 仅在
storage_state 为 **路径** 时才加载/写回；PW-3a 传 dict，两端都被静默跳过。
且 ai_base 在 agent.run() 返回后再调 export_storage_state()，彼时 browser-use
已 kill()→reset()（CDP 断开），导出必然失败被吞。结果全绿但登录态从未复用/回写。

本测试锁定修复后的真实链路：
- 注入的是 runtime state **路径**（非 dict），watchdog 才能在 CDP 存活期读写；
- 回写从 runtime 路径读回（模拟 watchdog 在 kill 前写回）→ 归档正式 registry；
- persist=False 不归档、无论何种情况都清理 runtime state（Cookie 等同凭据）；
- AST 层确认源码不再有 export_storage_state() 调用（防止回到坏实现）。

全程假 Cookie、假 Agent，不启动浏览器、不调 LLM。
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import textwrap
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest

from apps.ui_automation import ai_base
from apps.ui_automation import session_registry as SR

PERSIST_STATE = {
    "cookies": [{"name": "sid", "value": "FAKE_PERSISTED", "domain": "x.test", "path": "/"}],
    "origins": [],
}


class _WritebackAgent:
    """在 run() 里模拟 watchdog 于 kill() 前把 state 写回 profile 收到的路径。"""

    captured: ClassVar[list[str]] = []
    writeback_path: ClassVar[str | None] = None
    writeback_state: ClassVar[dict | None] = None
    export_called: ClassVar[int] = 0

    def __init__(self, **kwargs):
        self.task = kwargs["task"]
        self.history = []
        type(self).captured.append(self.task)
        self.browser_session = SimpleNamespace(
            export_storage_state=self._export_should_not_be_called
        )

    async def _export_should_not_be_called(self):
        type(self).export_called += 1
        return PERSIST_STATE

    async def run(self, max_steps=100, on_step_end=None):
        # 模拟 StorageStateWatchdog 在 kill() 阶段写回 runtime state 文件
        path = type(self).writeback_path
        state = type(self).writeback_state
        if path and state is not None:
            Path(path).write_text(json.dumps(state), encoding="utf-8")
        return self.history


class _FakeController:
    def action(self, *_a, **_k):
        def identity(func):
            return func

        return identity


@pytest.fixture
def writeback_env(monkeypatch, tmp_path):
    """让 run_task 跑到底，且能控制 watchdog 写回内容。返回 (run, captured)。"""
    monkeypatch.setenv("UI_SESSION_ALIAS_ENABLED", "true")
    monkeypatch.setenv("UI_SESSION_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(ai_base, "Agent", _WritebackAgent)
    monkeypatch.setattr(ai_base, "Controller", _FakeController)
    monkeypatch.setattr(ai_base, "_ui_assertions_enabled", lambda: False)
    monkeypatch.setattr(
        ai_base.BaseBrowserAgent, "_apply_model_routing", lambda self, *_a, **_k: {}
    )

    async def _no_await(*_a, **_k):
        return None

    monkeypatch.setattr(ai_base.BaseBrowserAgent, "_verify_execution_llm", _no_await)
    monkeypatch.setattr(ai_base.BaseBrowserAgent, "_cleanup_zombie_chrome", lambda self: None)

    captured: dict[str, object] = {"state_dir": str(tmp_path)}

    def _fake_profile(self, storage_state=None):
        captured["storage_state"] = storage_state
        # 写回目标 = profile 实际收到的路径（run_task 内部物化的 runtime 文件），
        # 这样模拟的 watchdog 写回与 run_task 的读回/清理指向同一文件。
        _WritebackAgent.writeback_path = storage_state
        captured["runtime_path"] = storage_state
        return object()

    monkeypatch.setattr(ai_base.BaseBrowserAgent, "_create_browser_profile", _fake_profile)
    monkeypatch.setattr(ai_base.BaseBrowserAgent, "_capture_usage", lambda self, agent: None)

    _WritebackAgent.captured = []
    _WritebackAgent.writeback_path = None
    _WritebackAgent.writeback_state = None
    _WritebackAgent.export_called = 0

    def run(*, alias="admin", persist=False, seed=None, writeback=None):
        registry = SR.SessionRegistry(captured["state_dir"])
        registry.save(alias, seed or {"cookies": [], "origins": []})
        _WritebackAgent.writeback_state = writeback

        agent = object.__new__(ai_base.BaseBrowserAgent)
        agent.llm = SimpleNamespace()
        agent.enable_gif = False
        agent.model_name = "gpt-4o"
        asyncio.run(
            agent.run_task(
                "打开首页",
                None,
                session_alias=alias,
                persist_session_state=persist,
            )
        )
        return captured

    return run, captured


class TestWritebackChain:
    def test_injects_path_not_dict(self, writeback_env):
        """修复核心：交给 profile 的是 runtime 路径字符串，可触发 watchdog。"""
        run, captured = writeback_env
        run()
        injected = captured["storage_state"]
        assert isinstance(injected, str)
        assert ".runtime-admin-" in injected
        assert injected.endswith(".storage_state.json")

    def test_persist_archives_watchdog_written_state(self, writeback_env):
        """persist=True：watchdog 写回的 state 被归档到正式 alias 文件。"""
        run, captured = writeback_env
        run(persist=True, writeback=PERSIST_STATE)
        saved = SR.SessionRegistry(captured["state_dir"]).load("admin")
        assert saved == PERSIST_STATE

    def test_no_persist_does_not_archive(self, writeback_env):
        """persist=False：即使 watchdog 写回，也不污染正式 state。"""
        run, captured = writeback_env
        seed = {"cookies": [{"name": "sid", "value": "SEED"}], "origins": []}
        run(persist=False, seed=seed, writeback=PERSIST_STATE)
        saved = SR.SessionRegistry(captured["state_dir"]).load("admin")
        assert saved == seed

    def test_runtime_file_always_cleaned(self, writeback_env):
        """无论是否归档，runtime state（含 Cookie）执行后必须清理。"""
        run, captured = writeback_env
        run(persist=True, writeback=PERSIST_STATE)
        assert not Path(captured["runtime_path"]).exists()

    def test_export_storage_state_never_called(self, writeback_env):
        """CDP 断开后不再调 export_storage_state()（PW-3a 的坏实现已移除）。"""
        run, _captured = writeback_env
        run(persist=True, writeback=PERSIST_STATE)
        assert _WritebackAgent.export_called == 0


class TestSourceContract:
    def test_no_export_storage_state_call(self):
        """AST：run_task 源码不含 export_storage_state 调用（注释不算）。"""
        src = inspect.getsource(ai_base.BaseBrowserAgent.run_task)
        tree = ast.parse(textwrap.dedent(src))
        called = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert "export_storage_state" not in called

    def test_runtime_state_path_used(self):
        """源码层：会话创建用路径物化，回写从路径读回并清理。"""
        src = inspect.getsource(ai_base.BaseBrowserAgent.run_task)
        assert "prepare_runtime_state" in src
        assert "read_runtime_state" in src
        assert "cleanup_runtime_state" in src
