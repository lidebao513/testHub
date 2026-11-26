"""PW-2 断言来源纪律守护测试：关键业务禁改 / 非关键 diff / 指纹稳定 / 接线。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from apps.ui_automation.assertion_guard import (
    AssertionMutationError,
    diff_assertion_items,
    guard_assertion_change,
    is_critical_business,
    snapshot_assertions,
)
from apps.ui_automation.closed_loop import (
    ClosedLoopOrchestrator,
    ExecutionResult,
    combine_verdict,
)
from apps.ui_automation.views import apply_assertion_report


def _item(level="rule", typ="page_contains", value="提交成功", task_id=1, **extra):
    return {
        "level": level,
        "type": typ,
        "value": value,
        "task_id": task_id,
        "status": "passed",
        "detail": "",
        **extra,
    }


def _report(*items, signal="pass"):
    return {
        "quality_signal": signal,
        "counts": {"passed": len(items)},
        "items": list(items),
    }


# ---------------------------------------------------------------------------
# 关键业务识别
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "完成支付流程",
        "提交订单并付款",
        "订单结算页",
        "核对交易金额",
        "CHECKOUT flow",
        "PAYMENT success",
        "place ORDER",
    ],
)
def test_critical_business_hits(text):
    assert is_critical_business(text) is True


@pytest.mark.parametrize(
    "text",
    ["打开首页", "查询列表", "登录系统", "筛选商品", "", None],
)
def test_non_critical_business(text):
    assert is_critical_business(text) is False


# ---------------------------------------------------------------------------
# 指纹与 diff
# ---------------------------------------------------------------------------


def test_snapshot_is_order_independent():
    a = _report(_item(value="A"), _item(value="B", task_id=2))
    b = _report(_item(value="B", task_id=2), _item(value="A"))
    assert snapshot_assertions(a) == snapshot_assertions(b)


def test_snapshot_ignores_status_and_detail():
    """断言本身未变，只是评估结果不同 → 不算改断言。"""
    before = _report(_item(status="unknown", detail="旧"))
    after = _report(_item(status="failed", detail="新"))
    assert snapshot_assertions(before) == snapshot_assertions(after)


def test_snapshot_changes_when_assertion_value_changes():
    before = _report(_item(value="提交成功"))
    after = _report(_item(value="提交失败"))
    assert snapshot_assertions(before) != snapshot_assertions(after)


def test_snapshot_handles_dirty_data():
    assert snapshot_assertions(None) == snapshot_assertions({})
    assert snapshot_assertions({"items": ["not a dict", None]}) == snapshot_assertions({})


def test_diff_reports_added_and_removed():
    before = _report(_item(value="A"))
    after = _report(_item(value="B"))
    changes = diff_assertion_items(before, after)
    assert len(changes) == 2
    reasons = {c["reason"] for c in changes}
    assert reasons == {"added", "removed"}
    assert all("source" in c and "before" in c and "after" in c for c in changes)


def test_diff_is_empty_when_only_status_changed():
    before = _report(_item(status="unknown"))
    after = _report(_item(status="passed"))
    assert diff_assertion_items(before, after) == []


# ---------------------------------------------------------------------------
# guard_assertion_change
# ---------------------------------------------------------------------------


def test_guard_critical_change_is_blocked():
    before = _report(_item(value="支付成功"))
    after = _report(_item(value="支付失败"))
    result = guard_assertion_change(before, after, critical=True)
    assert result.blocked is True
    assert result.allowed is False
    assert result.final_report["items"] == before["items"]
    assert result.final_report["blocked_assertion_change"]["reason"] == (
        "critical_business_assertion_locked"
    )
    assert result.changes


def test_guard_critical_strict_raises():
    before = _report(_item(value="支付成功"))
    after = _report(_item(value="支付失败"))
    with pytest.raises(AssertionMutationError):
        guard_assertion_change(before, after, critical=True, strict=True)


def test_guard_non_critical_change_is_recorded():
    before = _report(_item(value="查询成功"))
    after = _report(_item(value="查询完成"))
    result = guard_assertion_change(before, after, critical=False)
    assert result.blocked is False
    assert result.allowed is True
    assert result.final_report["items"] == after["items"]
    assert len(result.final_report["assertion_changes"]) == 2


def test_guard_unchanged_is_passthrough():
    before = _report(_item(status="unknown"))
    after = _report(_item(status="passed", detail="now ok"))
    result = guard_assertion_change(before, after, critical=True)
    assert result.blocked is False
    assert result.changes == []
    assert result.final_report == after


def test_guard_no_baseline_is_passthrough():
    after = _report(_item())
    result = guard_assertion_change(None, after, critical=True)
    assert result.blocked is False
    assert result.final_report == after


def test_guard_does_not_mutate_inputs():
    before = _report(_item(value="A"))
    after = _report(_item(value="B"))
    guard_assertion_change(before, after, critical=True)
    assert before["items"][0]["value"] == "A"
    assert after["items"][0]["value"] == "B"


# ---------------------------------------------------------------------------
# 接线：views.apply_assertion_report
# ---------------------------------------------------------------------------


def test_apply_assertion_report_without_baseline_unchanged():
    """缺省 baseline：行为与 PW-2 之前完全一致。"""
    report = _report(_item())
    record = SimpleNamespace(assertion_report={}, task_description="支付订单", case_name="支付")
    agent = SimpleNamespace(_assertion_report=report)
    apply_assertion_report(record, agent)
    assert record.assertion_report == report
    assert "assertion_changes" not in record.assertion_report


def test_apply_assertion_report_blocks_critical_with_baseline():
    baseline = _report(_item(value="支付成功"))
    new = _report(_item(value="支付成功但跳过校验"), signal="pass")
    record = SimpleNamespace(assertion_report={}, task_description="支付订单", case_name="支付")
    agent = SimpleNamespace(_assertion_report=new)
    apply_assertion_report(record, agent, baseline_report=baseline)
    assert record.assertion_report["items"] == baseline["items"]
    assert "blocked_assertion_change" in record.assertion_report
    # signal 取真实值，不因护栏而伪装通过/失败
    assert record.assertion_report["quality_signal"] == "pass"


def test_apply_assertion_report_records_non_critical_diff():
    baseline = _report(_item(value="查询A"))
    new = _report(_item(value="查询B"))
    record = SimpleNamespace(assertion_report={}, task_description="查询列表", case_name="查询")
    agent = SimpleNamespace(_assertion_report=new)
    apply_assertion_report(record, agent, baseline_report=baseline)
    assert record.assertion_report["items"] == new["items"]
    assert record.assertion_report["assertion_changes"]


# ---------------------------------------------------------------------------
# 接线：闭环重试
# ---------------------------------------------------------------------------


def _fake_reflow(job, result, verdict):
    return SimpleNamespace(note="ok")


def test_closed_loop_retry_critical_assertion_keeps_baseline():
    """关键业务：重试时改断言被拦，items 保留基准、verdict 仍按真实 signal。"""
    baseline_item = _item(value="支付成功")
    mutated_item = _item(value="支付成功（删去校验）")
    calls = {"n": 0}

    def execute(job):
        calls["n"] += 1
        if calls["n"] == 1:
            return ExecutionResult(
                status="error", assertion_report=_report(baseline_item, signal="none")
            )
        return ExecutionResult(
            status="passed", assertion_report=_report(mutated_item, signal="pass")
        )

    orch = ClosedLoopOrchestrator(
        execute,
        _fake_reflow,
        max_retries=1,
        on_item=lambda _item: None,
    )
    items = orch.run([{"case_id": 1, "name": "支付用例", "task_description": "完成支付订单"}])
    final = items[0]
    assert calls["n"] == 2
    assert final.assertion_report["items"][0]["value"] == "支付成功"
    assert "blocked_assertion_change" in final.assertion_report
    # 真实 signal=pass → verdict 仍为 pass（护栏不伪装）
    assert final.assertion_signal == "pass"
    assert final.verdict == combine_verdict("passed", "pass")


def test_closed_loop_retry_non_critical_records_diff():
    baseline_item = _item(value="查询A")
    mutated_item = _item(value="查询B")
    calls = {"n": 0}

    def execute(job):
        calls["n"] += 1
        if calls["n"] == 1:
            return ExecutionResult(
                status="error", assertion_report=_report(baseline_item, signal="none")
            )
        return ExecutionResult(
            status="passed", assertion_report=_report(mutated_item, signal="warn")
        )

    orch = ClosedLoopOrchestrator(execute, _fake_reflow, max_retries=1)
    items = orch.run([{"case_id": 2, "name": "查询用例", "task_description": "查询列表"}])
    final = items[0]
    assert final.assertion_report["items"][0]["value"] == "查询B"
    assert final.assertion_report["assertion_changes"]
    assert final.assertion_signal == "warn"
    assert final.verdict == "warn"
