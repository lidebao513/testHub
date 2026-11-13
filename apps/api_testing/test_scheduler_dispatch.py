"""P0 定时回归调度：到期判定 + 派发容错（celery 不可用时回落线程池）。

不触发真实套件执行（mock 掉底层执行），仅验证"定时回归"调度链路的健壮性。
"""

from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from .models import ApiProject, ScheduledTask, TaskExecutionLog, TestSuite
from .views import _dispatch_scheduled_task

User = get_user_model()


@pytest.mark.django_db
def test_scheduled_task_due_detection():
    user = User.objects.create_user(username="sched_due", password="x")
    proj = ApiProject.objects.create(
        name="Sched Proj", project_type="HTTP", status="IN_PROGRESS", owner=user
    )
    suite = TestSuite.objects.create(
        name="Reg Suite", project=proj, is_draft=False, created_by=user
    )

    # 到期（next_run_time 在过去）+ ACTIVE → 应触发
    due = ScheduledTask.objects.create(
        name="nightly",
        task_type="TEST_SUITE",
        trigger_type="INTERVAL",
        interval_seconds=86400,
        test_suite=suite,
        status="ACTIVE",
        created_by=user,
        next_run_time=timezone.now() - timedelta(minutes=1),
    )
    assert due.should_run_now() is True
    assert due.calculate_next_run() is not None

    # 未来 → 不触发
    due.next_run_time = timezone.now() + timedelta(hours=1)
    due.save()
    assert due.should_run_now() is False

    # 暂停 → 不触发（即使已到期）
    due.next_run_time = timezone.now() - timedelta(minutes=1)
    due.status = "PAUSED"
    due.save()
    assert due.should_run_now() is False


@pytest.mark.django_db
def test_dispatch_falls_back_to_thread_pool_without_broker():
    """celery broker 不可用时，定时回归任务应安全回落到线程池派发（不抛 TaskQueueFull）。"""
    user = User.objects.create_user(username="sched_disp", password="x")
    proj = ApiProject.objects.create(
        name="Sched Proj2", project_type="HTTP", status="IN_PROGRESS", owner=user
    )
    suite = TestSuite.objects.create(
        name="Reg Suite2", project=proj, is_draft=False, created_by=user
    )
    task = ScheduledTask.objects.create(
        name="regression",
        task_type="TEST_SUITE",
        trigger_type="INTERVAL",
        interval_seconds=86400,
        test_suite=suite,
        status="ACTIVE",
        created_by=user,
        next_run_time=timezone.now() - timedelta(minutes=1),
    )
    log = TaskExecutionLog.objects.create(task=task, status="PENDING")

    # 模拟 broker 不可用：celery .delay 抛异常；线程池 submit 用 Mock 接收（不真正执行）
    fake_pool = MagicMock()
    with patch(
        "apps.api_testing.tasks.execute_scheduled_task"
    ) as mock_task, patch(
        "apps.api_testing.views._FALLBACK_POOL", fake_pool
    ):
        mock_task.delay.side_effect = RuntimeError("broker unavailable")
        mode, celery_id = _dispatch_scheduled_task(task, log, user.id)

    assert mode == "thread"
    assert fake_pool.submit.called is True
    # 执行日志仍存在（派发成功，仅真实执行被 mock 拦截）
    assert TaskExecutionLog.objects.filter(id=log.id).exists()
