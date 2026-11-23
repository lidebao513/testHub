"""testgen 集成 URL 配置（挂在 /api/testgen/）。"""

from __future__ import annotations

from django.urls import path

from .url_gen_views import UrlGenerationView
from .url_project_views import (
    UrlModulesScanView,
    UrlModulesView,
    UrlProjectEnsureView,
)
from .url_sync_views import UrlGenerationSyncView
from .views import (
    TestgenBranchesView,
    TestgenCommitsView,
    TestgenDescribeView,
    TestgenGenerateView,
    TestgenHealthView,
    TestgenQualityView,
    TestgenScanView,
    TestgenSecurityVerdictView,
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
    path(
        "url-projects/ensure",
        UrlProjectEnsureView.as_view(),
        name="url_project_ensure",
    ),
    path(
        "url-projects/modules",
        UrlModulesView.as_view(),
        name="url_modules_read",
    ),
    path(
        "url-projects/modules/scan",
        UrlModulesScanView.as_view(),
        name="url_modules_scan",
    ),
    path(
        "url-generation/generate",
        UrlGenerationView.as_view(),
        name="url_generation_generate",
    ),
    path(
        "url-generation/sync",
        UrlGenerationSyncView.as_view(),
        name="url_generation_sync",
    ),
    path("generate", TestgenGenerateView.as_view(), name="generate"),
    path("scan", TestgenScanView.as_view(), name="scan"),
    path("branches", TestgenBranchesView.as_view(), name="branches"),
    path("quality", TestgenQualityView.as_view(), name="quality"),
    path("commits", TestgenCommitsView.as_view(), name="commits"),
    path("describe", TestgenDescribeView.as_view(), name="describe"),
    path("sync", TestgenSyncView.as_view(), name="sync"),
    path("security-verdict", TestgenSecurityVerdictView.as_view(), name="security_verdict"),
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
