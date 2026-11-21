"""E3 · A6 执行预算 / context 隔离 + A7 配置快速失败契约测试。

A6（P1 规模化）：
- `execute_all` 有总时长预算，超预算的未执行用例如实 skipped（不伪装通过），
  汇总标记 budget_exhausted；预算 0 保持旧行为（全量执行）。
- 每条 UI 用例执行前调用 `UiSession.reset_between_cases()` 做状态隔离；
  reset 清 console 缓冲、best-effort 清 Web Storage，桩环境不报错。

A7（P1 快速失败）：
- 布尔环境变量空值回落默认、合法值正常解析、**非法值启动期抛 ConfigError**
  且错误信息带变量名（拼写错误不再静默变 False 导致链路空跑）。
- 三态 card-nav 的兼容语义不受影响：非法 mode 回落 auto、非法 legacy 回落 off。

全部用假会话 / monkeypatch，无真实网络与浏览器，可在无网环境逐条断言。
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from core import config as cfg
from core.config import load_settings
from core.contracts import CaseSpec
from core.enums import ExecStatus, VerifyLayer
from core.errors import ConfigError
from engine import executor, pipeline, ui_executor
from engine.runtime_ui import RuntimeUiOptions


# ---------------------------------------------------------------- A7 配置快速失败
def test_bool_empty_value_falls_back_to_default() -> None:
    """空值（未配置）才回落默认——这是唯一允许静默的情形。"""
    assert cfg._as_bool(None, True, "X") is True
    assert cfg._as_bool("", False, "X") is False
    assert cfg._as_bool("  ", True, "X") is True


@pytest.mark.parametrize("raw", ["ture", "enabled", "2", "yes please", "False-ish"])
def test_bool_invalid_value_raises_config_error(raw: str) -> None:
    """非空但非白名单的配置（典型拼写错误）启动期即抛错，不再静默回落。"""
    with pytest.raises(ConfigError) as exc:
        cfg._as_bool(raw, False, "MY_SWITCH")
    assert "MY_SWITCH" in str(exc.value)
    assert raw in str(exc.value)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("on", True),
        ("TRUE", True),
        ("yes", True),
        ("1", True),
        ("off", False),
        ("false", False),
        ("N", False),
        ("0", False),
    ],
)
def test_bool_valid_values_still_parse(raw: str, expected: bool) -> None:
    """合法布尔值（含大小写与简写）行为不变。"""
    assert cfg._as_bool(raw, not expected, "X") is expected


def test_load_settings_rejects_invalid_bool_with_var_name() -> None:
    """走完整 load_settings 链路：非法 RUNTIME_UI_ENABLED 直接抛错并点名变量。"""
    with pytest.raises(ConfigError) as exc:
        load_settings(env={"RUNTIME_UI_ENABLED": "ture"})
    assert "RUNTIME_UI_ENABLED" in str(exc.value)


def test_card_nav_compat_semantics_unchanged() -> None:
    """三态安全兜底不被 A7 破坏：非法新值回落 auto、非法 legacy 回落 off。"""
    assert cfg._as_card_nav_mode("sometimes", None) == "auto"
    assert cfg._as_card_nav_mode(None, "ture") == "off"  # legacy 宽容回落
    assert cfg._as_card_nav_mode(None, "on") == "on"
    assert cfg._as_card_nav_mode("off", "on") == "off"  # 新值优先


def test_exec_budget_config_parsed_and_validated() -> None:
    """EXEC_BUDGET_SECONDS 解析与校验：默认 0=不限、负数快速失败。"""
    assert load_settings(env={}).exec_budget_seconds == 0
    assert load_settings(env={"EXEC_BUDGET_SECONDS": "120"}).exec_budget_seconds == 120
    with pytest.raises(ConfigError):
        load_settings(env={"EXEC_BUDGET_SECONDS": "-1"})


# ---------------------------------------------------------------- A6 执行预算
def _iface_case(tc_no: str = "TP-1") -> CaseSpec:
    return CaseSpec(
        tc_no=tc_no,
        title="[正常] 探活",
        ctype="api",
        steps=[
            {
                "action": "http_probe",
                "layer": VerifyLayer.INTERFACE.value,
                "kind": "api",
                "method": "GET",
                "path": "/ping",
                "dimension": "正常-可用性",
                "expect": "e",
            }
        ],
        case_type="正常",
    )


def test_budget_exhausted_skips_remaining_cases(monkeypatch: pytest.MonkeyPatch) -> None:
    """超预算后剩余用例如实 skipped，不伪装通过，汇总标记 budget_exhausted。"""
    calls: list[str] = []

    def fake_execute_case(case: CaseSpec, opts: Any, **kwargs: Any) -> executor.ExecutionResult:
        calls.append(case.tc_no)
        time.sleep(0.05)  # 每条耗时超过 0.01s 预算 → 第一条执行后即耗尽
        return executor.ExecutionResult(tc_no=case.tc_no, status=ExecStatus.PASS.value)

    monkeypatch.setattr(executor, "_new_session", lambda: object())
    monkeypatch.setattr(executor, "_close_session", lambda s: None)
    monkeypatch.setattr(executor, "execute_case", fake_execute_case)

    cases = [_iface_case(f"TP-{i}") for i in range(5)]
    summary = executor.execute_all(cases, executor.ExecutorOptions(budget_seconds=0.01))

    assert calls == ["TP-0"], "预算耗尽后不应再执行真实用例"
    assert summary["budget_exhausted"] is True
    assert summary["budget_seconds"] == 0.01
    assert summary["total"] == 5
    assert summary["skipped"] == 4
    assert summary["pass"] == 1
    # 被跳过的用例带原因且明确「不等于通过」
    skipped = [r for r in summary["results"] if r["status"] == ExecStatus.SKIPPED.value]
    assert all("预算" in " ".join(r.get("notes", [])) for r in skipped)


def test_budget_zero_keeps_all_cases_executed(monkeypatch: pytest.MonkeyPatch) -> None:
    """预算 0 = 不限，保持旧行为：全部用例照常执行，不误标 budget_exhausted。"""
    calls: list[str] = []

    def fake_execute_case(case: CaseSpec, opts: Any, **kwargs: Any) -> executor.ExecutionResult:
        calls.append(case.tc_no)
        return executor.ExecutionResult(tc_no=case.tc_no, status=ExecStatus.PASS.value)

    monkeypatch.setattr(executor, "_new_session", lambda: object())
    monkeypatch.setattr(executor, "_close_session", lambda s: None)
    monkeypatch.setattr(executor, "execute_case", fake_execute_case)

    cases = [_iface_case(f"TP-{i}") for i in range(3)]
    summary = executor.execute_all(cases, executor.ExecutorOptions())

    assert calls == ["TP-0", "TP-1", "TP-2"]
    assert summary["budget_exhausted"] is False
    assert summary["budget_seconds"] == 0.0
    assert summary["pass"] == 3


def test_pipeline_build_exec_options_propagates_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """pipeline 的 _build_exec_options 把 settings 预算贯穿到执行器选项。"""
    s = load_settings(env={"EXEC_BUDGET_SECONDS": "300"})
    monkeypatch.setattr(pipeline, "get_settings", lambda: s)
    opts = pipeline.PipelineOptions()
    ro = RuntimeUiOptions()
    exec_opts = pipeline._build_exec_options(opts, ro, project_id=None)
    assert exec_opts.budget_seconds == 300.0


# ---------------------------------------------------------------- A6 context 隔离
def test_reset_between_cases_clears_console_and_storage() -> None:
    """reset 清 console 缓冲、尽力清 Web Storage（假 page 记录 evaluate）。"""
    session = ui_executor.UiSession(ui_executor.UiExecOptions())
    session.console_errors.extend(["err-a", "err-b"])

    evaluated: list[str] = []

    class _Page:
        def evaluate(self, script: str) -> None:
            evaluated.append(script)

    session.bind(_Page())
    session.reset_between_cases()

    assert session.console_errors == []
    assert evaluated and "localStorage.clear" in evaluated[0]
    assert "sessionStorage.clear" in evaluated[0]


def test_reset_between_cases_tolerates_page_without_evaluate() -> None:
    """测试桩 / 受限页面无 evaluate 时静默跳过，绝不影响用例本身。"""
    session = ui_executor.UiSession(ui_executor.UiExecOptions())
    session.console_errors.append("err")
    session.bind(object())  # 无 evaluate 方法
    session.reset_between_cases()  # 不抛
    assert session.console_errors == []


def test_ui_cases_trigger_reset_before_each_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    """批量执行时每条 UI 用例执行前都调用 reset_between_cases（隔离真接线）。"""
    resets: list[int] = []
    executed: list[str] = []

    class _FakeUiSession:
        def __init__(self, options: Any) -> None:
            pass

        def open(self) -> bool:
            return True

        def reset_between_cases(self) -> None:
            resets.append(1)

        def execute(self, case: CaseSpec) -> ui_executor.UiExecResult:
            executed.append(case.tc_no)
            return ui_executor.UiExecResult(status=ExecStatus.PASS.value)

        def close(self) -> None:
            pass

    monkeypatch.setattr(ui_executor, "UiSession", _FakeUiSession)

    ui_cases = [
        CaseSpec(
            tc_no=f"UI-{i}",
            title="页面",
            ctype="ui",
            steps=[{"action": "ui_probe", "layer": VerifyLayer.UI.value}],
            case_type="交互",
        )
        for i in range(3)
    ]
    summary = executor.execute_all(ui_cases, executor.ExecutorOptions())

    assert len(resets) == 3, "每条 UI 用例前都应隔离"
    assert len(executed) == 3
    assert summary["pass"] == 3
