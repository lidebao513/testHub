"""#183 闭环编排测试：纯逻辑（状态机/combine_verdict/编排/报告）+ 真实回流接线。

纯逻辑用例不依赖 Django；带 @pytest.mark.django_db 的用例验证 AICase.last_loop 回流落库。
"""
from __future__ import annotations

import json

import pytest

from apps.ui_automation.closed_loop import (
    ClosedLoopOrchestrator,
    ExecutionResult,
    LoopItem,
    LoopState,
    ReflowResult,
    build_loop_report,
    can_transition,
    combine_verdict,
    execute_aicase_real,
    reflow_aicase_real,
    render_loop_report_md,
    summarize_loop,
)

# ===========================================================================
# 状态机
# ===========================================================================


def test_can_transition_happy_path():
    assert can_transition(LoopState.PENDING, LoopState.DISPATCHED)
    assert can_transition(LoopState.DISPATCHED, LoopState.EXECUTED)
    assert can_transition(LoopState.EXECUTED, LoopState.ASSERTED)
    assert can_transition(LoopState.ASSERTED, LoopState.REFLOWED)
    assert can_transition(LoopState.REFLOWED, LoopState.DONE)


def test_can_transition_illegal():
    assert not can_transition(LoopState.PENDING, LoopState.DONE)
    assert not can_transition(LoopState.DONE, LoopState.DISPATCHED)
    assert not can_transition(LoopState.FAILED, LoopState.EXECUTED)


def test_terminal_states_have_no_outgoing():
    for s in (LoopState.DONE, LoopState.WARN, LoopState.FAILED, LoopState.ERROR, LoopState.SKIPPED):
        assert s.is_terminal
        assert can_transition(s, LoopState.DISPATCHED) is False


# ===========================================================================
# combine_verdict 矩阵（闭环核心：断言可降级执行器的"自认通过"）
# ===========================================================================


@pytest.mark.parametrize(
    "exec_status,signal,expected",
    [
        ("passed", "pass", "pass"),
        ("passed", "none", "pass"),
        ("passed", "", "pass"),
        ("passed", "warn", "warn"),
        ("passed", "fail", "fail"),          # 闭环价值点：断言抓到真实问题
        ("failed", "pass", "fail"),          # 执行器已判失败，以执行器为准
        ("failed", "fail", "fail"),
        ("failed", "warn", "fail"),
        ("stopped", "pass", "skipped"),      # 用户停止，不计失败
        ("stopped", "fail", "skipped"),
        ("error", "none", "error"),          # 异常 → 可重试
    ],
)
def test_combine_verdict_matrix(exec_status, signal, expected):
    assert combine_verdict(exec_status, signal) == expected


# ===========================================================================
# 编排器（注入式，无 Django）
# ===========================================================================


def _fake_reflow(job, result, verdict):
    return ReflowResult(verdict, note="ok")


def test_orchestrator_pass():
    calls = []

    def execute(job):
        calls.append(job["case_id"])
        return ExecutionResult(status="passed", assertion_report={"quality_signal": "pass"})

    orch = ClosedLoopOrchestrator(execute, _fake_reflow, max_retries=1)
    items = orch.run([{"case_id": 1, "name": "登录", "task_description": "打开登录页"}])
    assert len(items) == 1
    it = items[0]
    assert it.state == LoopState.DONE
    assert it.verdict == "pass"
    assert it.exec_status == "passed"
    assert it.assertion_signal == "pass"
    assert it.attempt == 0
    assert calls == [1]


def test_orchestrator_assertion_downgrade():
    def execute(job):
        # 执行器自认通过，但断言抓到空白页/未导航
        return ExecutionResult(status="passed", assertion_report={"quality_signal": "fail"})

    orch = ClosedLoopOrchestrator(execute, _fake_reflow, max_retries=1)
    items = orch.run([{"case_id": 9}])
    it = items[0]
    assert it.verdict == "fail"
    assert it.state == LoopState.FAILED


def test_orchestrator_exec_failed():
    def execute(job):
        return ExecutionResult(status="failed")

    orch = ClosedLoopOrchestrator(execute, _fake_reflow, max_retries=1)
    it = orch.run([{"case_id": 2}])[0]
    assert it.verdict == "fail"
    assert it.state == LoopState.FAILED


def test_orchestrator_warn():
    def execute(job):
        return ExecutionResult(status="passed", assertion_report={"quality_signal": "warn"})

    orch = ClosedLoopOrchestrator(execute, _fake_reflow, max_retries=1)
    it = orch.run([{"case_id": 3}])[0]
    assert it.verdict == "warn"
    assert it.state == LoopState.WARN


def test_orchestrator_stopped():
    def execute(job):
        return ExecutionResult(status="stopped")

    orch = ClosedLoopOrchestrator(execute, _fake_reflow, max_retries=1)
    it = orch.run([{"case_id": 4}])[0]
    assert it.verdict == "skipped"
    assert it.state == LoopState.SKIPPED


def test_orchestrator_error_retries_then_terminal():
    attempts = []

    def execute(job):
        attempts.append(1)
        return ExecutionResult(status="error", error="timeout")

    orch = ClosedLoopOrchestrator(execute, _fake_reflow, max_retries=2)
    it = orch.run([{"case_id": 5}])[0]
    # 首次 + 2 次重试 = 3 次执行
    assert len(attempts) == 3
    assert it.attempt == 2
    assert it.verdict == "error"
    assert it.state == LoopState.ERROR


def test_orchestrator_execute_exception_is_error_and_retried():
    attempts = []

    def execute(job):
        attempts.append(1)
        raise RuntimeError("browser crashed")

    orch = ClosedLoopOrchestrator(execute, _fake_reflow, max_retries=1)
    it = orch.run([{"case_id": 6}])[0]
    assert len(attempts) == 2  # 第一次 + 1 重试
    assert it.verdict == "error"
    assert "browser crashed" in it.error


def test_orchestrator_reflow_error_non_blocking():
    def execute(job):
        return ExecutionResult(status="passed", assertion_report={"quality_signal": "pass"})

    def bad_reflow(job, result, verdict):
        raise RuntimeError("db down")

    orch = ClosedLoopOrchestrator(execute, bad_reflow, max_retries=0)
    it = orch.run([{"case_id": 7}])[0]
    assert it.state == LoopState.DONE  # 回流失败不拖垮终态
    assert "db down" in it.reflow_note


def test_orchestrator_no_retry_for_fail():
    calls = []

    def execute(job):
        calls.append(1)
        return ExecutionResult(status="failed")

    orch = ClosedLoopOrchestrator(execute, _fake_reflow, max_retries=3)
    it = orch.run([{"case_id": 8}])[0]
    assert len(calls) == 1  # 真实失败不重试
    assert it.state == LoopState.FAILED


def test_orchestrator_state_history_recorded():
    def execute(job):
        return ExecutionResult(status="passed", assertion_report={"quality_signal": "pass"})

    orch = ClosedLoopOrchestrator(execute, _fake_reflow, max_retries=0)
    it = orch.run([{"case_id": 10}])[0]
    assert LoopState.PENDING.value in it.state_history
    assert LoopState.DISPATCHED.value in it.state_history
    assert LoopState.DONE.value in it.state_history
    assert it.state_history[-1] == LoopState.DONE.value


# ===========================================================================
# 汇总与报告
# ===========================================================================


def _item(state, verdict, exec_status="passed", signal="pass"):
    it = LoopItem(case_id=1, name="x")
    it.state = state
    it.verdict = verdict
    it.exec_status = exec_status
    it.assertion_signal = signal
    return it


def test_summarize_signal_worst():
    items = [
        _item(LoopState.DONE, "pass"),
        _item(LoopState.WARN, "warn"),
    ]
    s = summarize_loop(items)
    assert s["signal"] == "warn"
    assert s["done"] == 1 and s["warn"] == 1


def test_summarize_fail_signal():
    items = [_item(LoopState.DONE, "pass"), _item(LoopState.FAILED, "fail")]
    s = summarize_loop(items)
    assert s["signal"] == "fail"
    assert s["failed"] == 1


def test_summarize_empty():
    s = summarize_loop([])
    assert s["total"] == 0
    assert s["signal"] == "none"
    assert s["pass_rate"] == 0.0


def test_render_report_md():
    from datetime import datetime

    items = [
        _item(LoopState.DONE, "pass"),
        _item(LoopState.FAILED, "fail", exec_status="failed", signal="none"),
    ]
    report = build_loop_report(items, summarize_loop(items), datetime.now(), 1.5)
    md = render_loop_report_md(report)
    assert "# 生成→执行→回流 闭环报告" in md
    assert "| 状态 |" in md
    assert "failed" in md


# ===========================================================================
# 真实回流接线（需要 Django + DB）
# ===========================================================================


@pytest.mark.django_db
def test_aicase_last_loop_field_default():
    from apps.ui_automation.models import AICase

    case = AICase.objects.create(name="t", task_description="打开 x")
    case.refresh_from_db()
    assert case.last_loop == {}


@pytest.mark.django_db
def test_execute_aicase_real_dry_run():
    from apps.ui_automation.models import AICase, AIExecutionRecord

    case = AICase.objects.create(name="dry", task_description="打开登录页")
    result = execute_aicase_real(case, dry_run=True)
    assert result.status == "passed"
    assert result.assertion_report.get("quality_signal") == "pass"
    rec = AIExecutionRecord.objects.get(id=result.record_id)
    assert rec.ai_case_id == case.id
    assert rec.status == "passed"


@pytest.mark.django_db
def test_reflow_aicase_real_writes_last_loop():
    from apps.ui_automation.models import AICase

    case = AICase.objects.create(name="reflow", task_description="y")
    result = ExecutionResult(
        status="passed",
        assertion_report={"quality_signal": "pass"},
        record_id=123,
    )
    reflow = reflow_aicase_real({"case_id": case.id}, result, "pass")
    assert reflow.verdict == "pass"
    case.refresh_from_db()
    assert case.last_loop["state"] == "pass"
    assert case.last_loop["exec_status"] == "passed"
    assert case.last_loop["assertion_signal"] == "pass"
    assert case.last_loop["last_record_id"] == 123


@pytest.mark.django_db
def test_orchestrator_with_real_reflow():
    from apps.ui_automation.models import AICase

    case = AICase.objects.create(name="loop", task_description="z")

    def execute(job):
        return ExecutionResult(status="passed", assertion_report={"quality_signal": "pass"})

    orch = ClosedLoopOrchestrator(execute, reflow_aicase_real, max_retries=0)
    items = orch.run([{"case_id": case.id, "name": "loop", "task_description": "z"}])
    assert items[0].state == LoopState.DONE
    case.refresh_from_db()
    assert case.last_loop["state"] == "pass"


@pytest.mark.django_db
def test_run_closed_loop_command_dry_run(tmp_path):
    """命令级端到端：dry-run 不启动浏览器，验证编排→回流写回→报告落盘。"""
    from django.core.management import call_command

    from apps.ui_automation.models import AICase

    case = AICase.objects.create(name="cmd", task_description="打开 x")
    out_dir = tmp_path / "reports"

    call_command(
        "run_closed_loop",
        "--force",
        "--dry-run",
        "--case-ids",
        str(case.id),
        "--out-dir",
        str(out_dir),
    )

    case.refresh_from_db()
    assert case.last_loop["state"] == "pass"
    assert case.last_loop["exec_status"] == "passed"

    json_files = list(out_dir.glob("closed_loop_*.json"))
    md_files = list(out_dir.glob("closed_loop_*.md"))
    assert json_files and md_files
    report = json.loads(json_files[0].read_text(encoding="utf-8"))
    assert report["summary"]["done"] == 1
    assert report["summary"]["signal"] == "pass"
