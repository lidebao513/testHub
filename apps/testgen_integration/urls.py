"""testgen 集成 URL 配置（挂在 /api/testgen/）。"""

from __future__ import annotations

from django.urls import path

from .views import (
    TestgenBranchesView,
    TestgenCommitsView,
    TestgenDescribeView,
    TestgenGenerateView,
    TestgenHealthView,
    TestgenQualityView,
    TestgenScanView,
    TestgenStageAnalyzeView,
    TestgenStageCompareView,
    TestgenStageListView,
    TestgenSyncView,
    TestgenTaskCancelView,
    TestgenTaskCasesView,
    TestgenTaskStatusView,
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
    path("health", TestgenHealthView.as_view(), name="health"),
    path(
        "tasks/<str:task_id>/cancel",
        TestgenTaskCancelView.as_view(),
        name="task_cancel",
    ),
    path(
        "tasks/<str:task_id>/cases", TestgenTaskCasesView.as_view(), name="task_cases"
    ),
    path(
        "tasks/<str:task_id>",
        TestgenTaskStatusView.as_view(),
        name="task_status",
    ),
    path("stages", TestgenStageListView.as_view(), name="stage_list"),
    path("stage-analyze", TestgenStageAnalyzeView.as_view(), name="stage_analyze"),
    path("stage-compare", TestgenStageCompareView.as_view(), name="stage_compare"),
]
