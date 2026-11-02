"""L8 回归：定时任务派发（Celery 优先 + 有界兜底池）。

改造前：`run_now` 每个请求一条 `threading.Thread(daemon=True)`，并发无上限
（放大 H4 SSRF）。改造后：
- 主路径进 Celery（并发度由 worker `-c` 决定）；
- broker 不可用退回**固定大小**线程池，池满返回 429（明确拒绝，不静默堆积）。
"""

import time

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.api_testing import views as api_views
from apps.api_testing.models import ScheduledTask, TaskExecutionLog

# transaction=True：兜底池在**子线程**里读库，外层测试事务会锁表（table is locked）
pytestmark = pytest.mark.django_db(transaction=True)

User = get_user_model()


def _make_task(user, name="L8 任务"):
    return ScheduledTask.objects.create(
        name=name,
        task_type="API_REQUEST",
        trigger_type="INTERVAL",
        interval_seconds=3600,
        created_by=user,
    )


def _wait_log(log_id, expect=("COMPLETED", "FAILED"), timeout=5.0):
    deadline = time.monotonic() + timeout
    status = ""
    while time.monotonic() < deadline:
        log = TaskExecutionLog.objects.get(id=log_id)
        status = log.status
        if status in expect:
            return status
        time.sleep(0.02)
    return status


def test_dispatch_goes_through_celery_task(monkeypatch):
    """正常路径：走 Celery（eager 下同步执行完），执行日志到达 COMPLETED。"""
    user = User.objects.create_user(username="l8-a", password="x")
    task = _make_task(user)
    log = TaskExecutionLog.objects.create(task=task, status="PENDING", executed_by=user)

    monkeypatch.setattr(
        api_views.ScheduledTaskViewSet, "_execute_api_request", lambda self, t: {"ok": True}
    )

    mode, _celery_id = api_views._dispatch_scheduled_task(task, log, user.id)

    assert mode == "celery"
    assert TaskExecutionLog.objects.get(id=log.id).status == "COMPLETED"


def test_dispatch_falls_back_to_bounded_pool(monkeypatch):
    """broker 不可用：退回有界线程池，任务仍被执行（不丢单）。"""
    user = User.objects.create_user(username="l8-b", password="x")
    task = _make_task(user)
    log = TaskExecutionLog.objects.create(task=task, status="PENDING", executed_by=user)

    from apps.api_testing import tasks as api_tasks

    def _boom(*args, **kwargs):
        raise ConnectionError("broker down")

    monkeypatch.setattr(api_tasks.execute_scheduled_task, "delay", _boom)
    monkeypatch.setattr(
        api_views.ScheduledTaskViewSet, "_execute_api_request", lambda self, t: {"ok": True}
    )

    mode, celery_id = api_views._dispatch_scheduled_task(task, log, user.id)

    assert mode == "thread"
    assert celery_id is None
    assert _wait_log(log.id) == "COMPLETED"


def test_fallback_pool_rejects_when_saturated(monkeypatch):
    """兜底池槽位占满：明确抛 TaskQueueFull（视图转 429），不再无限起线程。"""
    user = User.objects.create_user(username="l8-c", password="x")
    task = _make_task(user)
    log = TaskExecutionLog.objects.create(task=task, status="PENDING", executed_by=user)

    from apps.api_testing import tasks as api_tasks

    monkeypatch.setattr(
        api_tasks.execute_scheduled_task,
        "delay",
        lambda *a, **k: (_ for _ in ()).throw(ConnectionError("broker down")),
    )

    # 占满所有槽位
    acquired = 0
    while api_views._FALLBACK_SEM.acquire(blocking=False):
        acquired += 1
    try:
        assert acquired == api_views._FALLBACK_MAX_WORKERS
        with pytest.raises(api_views.TaskQueueFull):
            api_views._dispatch_scheduled_task(task, log, user.id)
    finally:
        for _ in range(acquired):
            api_views._FALLBACK_SEM.release()


def test_run_now_reports_dispatch_mode(monkeypatch):
    """run_now 响应如实标注派发方式（celery / thread），便于排障。"""
    user = User.objects.create_user(username="l8-d", password="x")
    task = _make_task(user, name="L8 接口任务")
    monkeypatch.setattr(
        api_views.ScheduledTaskViewSet, "_execute_api_request", lambda self, t: {"ok": True}
    )

    client = Client()
    client.force_login(user)
    resp = client.post(f"/api/api-testing/scheduled-tasks/{task.id}/run_now/")

    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert body["dispatch_mode"] in ("celery", "thread")
    assert "execution_id" in body


def test_run_now_returns_429_when_queue_full(monkeypatch):
    """池满且 broker 不可用 → 429 + 执行日志落 FAILED（不静默丢单）。"""
    user = User.objects.create_user(username="l8-e", password="x")
    task = _make_task(user, name="L8 队列满")

    from apps.api_testing import tasks as api_tasks

    monkeypatch.setattr(
        api_tasks.execute_scheduled_task,
        "delay",
        lambda *a, **k: (_ for _ in ()).throw(ConnectionError("broker down")),
    )

    acquired = 0
    while api_views._FALLBACK_SEM.acquire(blocking=False):
        acquired += 1
    try:
        client = Client()
        client.force_login(user)
        resp = client.post(f"/api/api-testing/scheduled-tasks/{task.id}/run_now/")
        assert resp.status_code == 429, resp.content
        assert "队列已满" in resp.json()["error"]
        log = TaskExecutionLog.objects.filter(task=task).first()
        assert log is not None and log.status == "FAILED"
    finally:
        for _ in range(acquired):
            api_views._FALLBACK_SEM.release()
