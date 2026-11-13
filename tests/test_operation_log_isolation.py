"""操作日志按用户隔离的回归测试。

修复前 OperationLogViewSet.get_queryset 返回 OperationLog.objects.all()，
任何登录用户都能看到全部人的操作日志（越权）。
修复后：
- 普通用户仅能看到自己的操作日志；
- 管理员（is_staff 或 is_superuser）可查看全部，用于审计。

用 SQLite 测试库（backend.test_settings），不依赖 MySQL/Redis。
"""
import pytest
from django.contrib.auth import get_user_model
from django.test import RequestFactory

from apps.api_testing.models import OperationLog
from apps.api_testing.operation_logger import log_operation
from apps.api_testing.views import OperationLogViewSet

pytestmark = pytest.mark.django_db

User = get_user_model()
RF = RequestFactory()


def _make_user(username: str, **extra) -> "User":
    return User.objects.create_user(username=username, password="Passw0rd!23", **extra)


def _log_for(user, n: int = 2):
    for i in range(n):
        log_operation(
            operation_type="create",
            resource_type="project",
            resource_id=1000 + i,
            resource_name=f"res-{user.username}-{i}",
            user=user,
            description=f"{user.username} did {i}",
        )


def _build_view(user) -> OperationLogViewSet:
    request = RF.get("/api/api-testing/operation-logs/")
    request.user = user
    view = OperationLogViewSet()
    view.request = request
    return view


def test_regular_user_sees_only_own_logs():
    u1 = _make_user("op-u1")
    u2 = _make_user("op-u2")
    _log_for(u1, n=2)
    _log_for(u2, n=3)

    view = _build_view(u1)
    qs = view.get_queryset()

    assert qs.count() == 2, "普通用户应只看到自己的 2 条日志"
    assert all(entry.user_id == u1.id for entry in qs), "普通用户不应看到他人日志"


def test_user_cannot_see_other_users_logs():
    u1 = _make_user("op-u1b")
    u2 = _make_user("op-u2b")
    _log_for(u1, n=1)
    _log_for(u2, n=5)

    view = _build_view(u1)
    qs = view.get_queryset()

    assert qs.count() == 1
    assert u2.id not in {entry.user_id for entry in OperationLog.objects.all() if entry in qs}


def test_admin_sees_all_logs():
    u1 = _make_user("op-u1c")
    u2 = _make_user("op-u2c")
    admin = _make_user("op-admin", is_staff=True, is_superuser=True)
    _log_for(u1, n=2)
    _log_for(u2, n=3)

    view = _build_view(admin)
    qs = view.get_queryset()

    assert qs.count() == 5, "管理员应能看到全部 5 条日志"


def test_staff_only_admin_sees_all_logs():
    u1 = _make_user("op-u1d")
    staff = _make_user("op-staff", is_staff=True, is_superuser=False)
    _log_for(u1, n=2)

    view = _build_view(staff)
    qs = view.get_queryset()

    assert qs.count() == 2, "仅 is_staff（非 superuser）的管理员也应能看到全部"
