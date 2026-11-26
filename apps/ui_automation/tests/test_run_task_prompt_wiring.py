"""run_task 级集成测试：证明 prompt 段真的拼进了交给 Agent 的 task。

与 test_pre_explore.py 的纯函数测试、test_prompt_injection_guard.py 的
``inspect.getsource`` 静态检查不同，这里**真实调用一次 run_task**，在
``Agent(task=...)`` 处把最终 task 截下来断言。这样：

- 纯函数对了但 run_task 忘了注入 → 本测试会失败（静态检查漏掉的场景）；
- 注入点被挪到规则区之后、或被后续逻辑覆盖 → 本测试会失败。

全程不启动浏览器、不调 LLM：用裸实例 + stub 外部依赖，只让 prompt 拼接链路真实执行。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import ClassVar

import pytest

from apps.ui_automation import ai_base
from apps.ui_automation.skill_loader import BLOCK_START_PREFIX, load_skill, render_skill_block

PLANNED_TASKS = [
    {"id": 1, "description": "打开登录页", "status": "pending"},
    {"id": 2, "description": "输入用户名密码并登录", "status": "pending"},
]


class _FakeAgent:
    """替换 browser_use.Agent，捕获构造时的 task 与 run 调用。"""

    captured: ClassVar[list[str]] = []

    def __init__(self, **kwargs):
        self.task = kwargs["task"]
        type(self).captured.append(self.task)
        self.history = []
        self.kwargs = kwargs

    async def run(self, max_steps=100, on_step_end=None):
        # 保留 on_step_end 形参，匹配 run_task 里 inspect.signature 的分支
        return self.history


class _FakeController:
    """run_task 会用 @controller.action('Done') 注册几个工具，stub 成恒等装饰器。"""

    def action(self, *_a, **_k):
        def identity(func):
            return func

        return identity


@pytest.fixture
def run_task_env(monkeypatch):
    """让 run_task 跑到底的最小 stub 集合，返回取 task 的函数。"""
    monkeypatch.setattr(ai_base, "Agent", _FakeAgent)
    monkeypatch.setattr(ai_base, "Controller", _FakeController)
    monkeypatch.setattr(ai_base, "_ui_assertions_enabled", lambda: False)
    monkeypatch.setattr(
        ai_base.BaseBrowserAgent, "_apply_model_routing", lambda self, *_a, **_k: {}
    )

    async def _no_await(*_a, **_k):
        return None

    monkeypatch.setattr(ai_base.BaseBrowserAgent, "_verify_execution_llm", _no_await)
    monkeypatch.setattr(ai_base.BaseBrowserAgent, "_cleanup_zombie_chrome", lambda self: None)
    monkeypatch.setattr(ai_base.BaseBrowserAgent, "_create_browser_profile", lambda self: object())
    monkeypatch.setattr(ai_base.BaseBrowserAgent, "_capture_usage", lambda self, agent: None)

    _FakeAgent.captured = []

    def run(description="打开首页并登录", planned=None):
        # object.__new__ 绕开 __init__ 的 API Key / ChatOpenAI 依赖；
        # run_task 实际读到的 self 属性只补这几个。
        agent = object.__new__(ai_base.BaseBrowserAgent)
        agent.llm = SimpleNamespace()
        agent.enable_gif = False
        agent.model_name = "gpt-4o"  # run_task 里按模型名决定是否追加省 token 指令
        asyncio.run(agent.run_task(description, planned))
        assert _FakeAgent.captured, "run_task 没有构造 Agent，说明链路被打断"
        return _FakeAgent.captured[-1]

    return run


# ============================================================================
# 核心：prompt 真的进了 Agent.task
# ============================================================================


def test_pre_explore_prompt_is_in_agent_task(run_task_env, monkeypatch):
    monkeypatch.delenv("UI_PRE_EXPLORE", raising=False)
    task = run_task_env()
    prompt = ai_base.build_pre_explore_prompt()
    assert prompt.strip() in task, "预探索 prompt 整段必须出现在交给 Agent 的 task 里"
    assert "NEVER invent an index" in task


def test_pre_explore_prompt_omitted_when_disabled(run_task_env, monkeypatch):
    monkeypatch.setenv("UI_PRE_EXPLORE", "0")
    task = run_task_env()
    assert "PAGE PRE-EXPLORATION" not in task
    assert "NEVER invent an index" not in task


def test_pre_explore_precedes_rules_section(run_task_env, monkeypatch):
    """注入点必须在规则区之前——否则模型先读到操作规则，'先探索'就失去意义。"""
    monkeypatch.delenv("UI_PRE_EXPLORE", raising=False)
    task = run_task_env()
    assert task.index("PAGE PRE-EXPLORATION") < task.index("CRITICAL PERFORMANCE & SYNC RULES")


def test_security_boundary_and_time_are_in_agent_task(run_task_env):
    """同链路的其他关键段也不能退化成死代码。"""
    task = run_task_env()
    assert "SECURITY BOUNDARY" in task
    assert "UNTRUSTED DATA" in task
    assert "CURRENT TIME:" in task


def test_planned_tasks_are_in_agent_task(run_task_env):
    task = run_task_env(planned=PLANNED_TASKS)
    assert "Sub-tasks (Execute in order):" in task
    assert "1. 打开登录页" in task
    assert "2. 输入用户名密码并登录" in task
    assert "mark_task_complete" in task


def test_original_description_is_preserved(run_task_env):
    description = "在示例站点完成一次登录"
    task = run_task_env(description=description)
    assert task.startswith(description)


# ============================================================================
# PW-1：Skill 规范块真的进了 Agent.task
# ============================================================================


def test_skill_block_is_in_agent_task(run_task_env):
    """真实 run_task 链路：规范块必须整段出现在交给 Agent 的 task 里。"""
    task = run_task_env()
    doc = load_skill()
    assert doc is not None
    assert render_skill_block(doc).strip() in task
    assert "探页面纪律" in task
    assert "失败诊断纪律" in task


def test_skill_block_appears_exactly_once(run_task_env):
    """幂等：同一 task 不得出现两次规范块。"""
    task = run_task_env()
    assert task.count(BLOCK_START_PREFIX) == 1


def test_skill_block_precedes_rules_and_follows_pre_explore(run_task_env):
    """顺序护栏：预探索 → Skill 纪律 → 安全边界/规则区。"""
    task = run_task_env()
    assert task.index("PAGE PRE-EXPLORATION") < task.index(BLOCK_START_PREFIX)
    assert task.index(BLOCK_START_PREFIX) < task.index("SECURITY BOUNDARY")
    assert task.index(BLOCK_START_PREFIX) < task.index("CRITICAL PERFORMANCE & SYNC RULES")


def test_skill_block_omitted_when_load_fails(run_task_env, monkeypatch):
    """fail-open：规范加载失败不打断执行，也不在 prompt 里留半截标记。"""
    monkeypatch.setattr(ai_base, "load_skill", lambda *a, **k: None)
    task = run_task_env()
    assert BLOCK_START_PREFIX not in task
    assert "CRITICAL PERFORMANCE & SYNC RULES" in task
