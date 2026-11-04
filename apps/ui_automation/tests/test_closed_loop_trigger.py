"""#200 闭环触发入口测试。

覆盖：
- 任务：run_closed_loop_task 调用编排、真实 reflow 写回 AICase.last_loop、报告落盘
- 端点：POST run 校验 case_ids 存在性 → fire-and-forget 返回 task_id（不真跑任务）
- 端点：不存在的 case_ids → 400
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from apps.ui_automation.closed_loop import ExecutionResult


@pytest.mark.django_db
def test_run_closed_loop_task_calls_orchestrator(monkeypatch) -> None:
    from apps.ui_automation import tasks
    from apps.ui_automation.models import AICase

    case = AICase.objects.create(name="闭环任务用例", task_description="打开登录页")
    # 只 mock 真实执行（避免起浏览器），保留 reflow_aicase_real 真实写回 last_loop
    monkeypatch.setattr(
        tasks,
        "execute_aicase_real",
        lambda job: ExecutionResult(
            status="passed",
            assertion_report={"quality_signal": "pass"},
            record_id=1,
        ),
    )
    out = tasks.run_closed_loop_task(case_ids=[case.id])
    assert out["summary"]["total"] == 1
    assert out["summary"]["done"] == 1
    case.refresh_from_db()
    assert case.last_loop.get("state") == "pass"
    assert out["files"]["json"] and out["files"]["md"]


@pytest.mark.django_db
def test_closed_loop_run_endpoint_dispatches(monkeypatch) -> None:
    from apps.ui_automation.models import AICase

    U = get_user_model()
    user = U.objects.create(username="cl_user")
    case = AICase.objects.create(name="c", task_description="t")

    captured: dict = {}

    class FakeAsync:
        id = "fake-task-id"
        state = "PENDING"

    def fake_delay(case_ids, project_id, max_retries):
        captured["case_ids"] = case_ids
        captured["project_id"] = project_id
        captured["max_retries"] = max_retries
        return FakeAsync()

    monkeypatch.setattr(
        "apps.ui_automation.tasks.run_closed_loop_task.delay", fake_delay
    )

    client = APIClient()
    client.force_authenticate(user=user)
    resp = client.post(
        "/api/ui-automation/closed-loop/run/",
        {"case_ids": [case.id], "max_retries": 1},
        format="json",
    )
    assert resp.status_code == 200
    assert resp.data["ok"] is True
    assert resp.data["task_id"] == "fake-task-id"
    assert captured["case_ids"] == [case.id]
    assert captured["max_retries"] == 1


@pytest.mark.django_db
def test_closed_loop_run_endpoint_missing_case(monkeypatch) -> None:
    U = get_user_model()
    user = U.objects.create(username="cl_user2")
    monkeypatch.setattr(
        "apps.ui_automation.tasks.run_closed_loop_task.delay",
        lambda *a, **k: None,
    )
    client = APIClient()
    client.force_authenticate(user=user)
    resp = client.post(
        "/api/ui-automation/closed-loop/run/",
        {"case_ids": [99999]},
        format="json",
    )
    assert resp.status_code == 400
    assert resp.data["ok"] is False
