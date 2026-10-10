"""testgen 集成 URL 配置（挂在 /api/testgen/）。"""

from __future__ import annotations

from django.urls import path

from .views import (
    TestgenBranchesView,
    TestgenCommitsView,
    TestgenDescribeView,
    TestgenGenerateView,
    TestgenQualityView,
    TestgenScanView,
    TestgenSyncView,
    TestgenTaskCancelView,
    TestgenTaskCasesView,
)

app_name = "testgen_integration"

urlpatterns = [
    path("generate", TestgenGenerateView.as_view(), name="generate"),
    path("scan", TestgenScanView.as_view(), name="scan"),
    path("branches", TestgenBranchesView.as_view(), name="branches"),
    path("quality", TestgenQualityView.as_view(), name="quality"),
    path("commits", TestgenCommitsView.as_view(), name="commits"),
    path("describe", TestgenDescribeView.as_view(), name="describe"),
    path("sync", TestgenSyncView.as_view(), name="sync"),
    path(
        "tasks/<str:task_id>/cancel",
        TestgenTaskCancelView.as_view(),
        name="task_cancel",
    ),
    path(
        "tasks/<str:task_id>/cases", TestgenTaskCasesView.as_view(), name="task_cases"
    ),
]
