"""#452：报表 by_module 模块维度守护。

要点：by_module 是**平行新增字段**，与 sidecar 的 body/impact/runtime 三通道、
quality KPI 和 dashboard 既有字段互不覆盖。这里只验证主平台报表契约与口径自洽；
三通道 consistent 由 sidecar 侧 test_quality.py 守护（本批未改 sidecar 代码）。
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from apps.biz_modules.models import BizModule
from apps.projects.models import Project
from apps.testcases.models import TestCase

User = get_user_model()
DASHBOARD_URL = "/api/reports/reports/dashboard/"
ORIGINAL_FIELDS = {
    "active_plans",
    "plan_progress",
    "total_cases",
    "total_defects",
    "pass_rate",
}
BY_MODULE_FIELDS = {
    "module_id",
    "name",
    "namespace",
    "source",
    "total",
    "active",
    "draft",
    "deprecated",
}


def _project(name: str) -> Project:
    return Project.objects.create(
        name=name, owner=User.objects.create_user(username=f"u-{name}")
    )


def _case(project: Project, title: str, *, module=None, status: str = "active") -> TestCase:
    return TestCase.objects.create(
        project=project,
        author=project.owner,
        title=title,
        expected_result="ok",
        module=module,
        status=status,
    )


@pytest.mark.django_db
def test_dashboard_adds_parallel_by_module_without_changing_existing_contract():
    project = _project("p")
    module = BizModule.objects.create(
        project=project,
        name="pc",
        namespace="runtime:https://a.com",
        source="runtime",
    )
    _case(project, "a", module=module, status="active")
    _case(project, "b", module=module, status="draft")
    _case(project, "c", module=None, status="active")
    _case(project, "d", module=None, status="deprecated")

    client = APIClient()
    client.force_authenticate(user=project.owner)
    # 报表 API 不得触碰 sidecar 的 quality 聚合（三通道/KPI 口径保持原样）
    with patch(
        "apps.testgen_integration.client.quality_summary",
        side_effect=AssertionError("dashboard 不应调用 sidecar quality"),
    ):
        response = client.get(DASHBOARD_URL, {"project": project.id})

    assert response.status_code == 200
    data = response.json()
    assert ORIGINAL_FIELDS <= data.keys()
    assert data["total_cases"] == 4
    for key in ("channel_summary", "quality", "consistent", "body", "impact", "runtime"):
        assert key not in data

    by_module = data["by_module"]
    assert sum(row["total"] for row in by_module) == data["total_cases"]
    assert all(set(row) == BY_MODULE_FIELDS for row in by_module)
    assert [row["module_id"] for row in by_module][-1] is None  # 未归属固定末尾

    assigned = next(row for row in by_module if row["module_id"] == module.id)
    assert (assigned["name"], assigned["source"], assigned["namespace"]) == (
        "pc",
        "runtime",
        "runtime:https://a.com",
    )
    assert (assigned["total"], assigned["active"], assigned["draft"], assigned["deprecated"]) == (
        2,
        1,
        1,
        0,
    )
    unassigned = next(row for row in by_module if row["module_id"] is None)
    assert (unassigned["name"], unassigned["total"], unassigned["deprecated"]) == (
        None,
        2,
        1,
    )


@pytest.mark.django_db
def test_by_module_is_project_scoped_and_empty_when_no_cases():
    mine, other = _project("mine"), _project("other")
    module = BizModule.objects.create(project=mine, name="api", namespace="code:42", source="code")
    _case(mine, "mine-case", module=module)
    _case(other, "other-case")

    client = APIClient()
    client.force_authenticate(user=mine.owner)

    mine_data = client.get(DASHBOARD_URL, {"project": mine.id}).json()
    assert mine_data["total_cases"] == 1
    assert len(mine_data["by_module"]) == 1
    assert mine_data["by_module"][0]["source"] == "code"

    empty = client.get(DASHBOARD_URL, {"project": 999999}).json()
    assert empty["total_cases"] == 0
    assert empty["by_module"] == []


@pytest.mark.django_db
def test_by_module_sums_to_total_cases_across_mixed_statuses():
    project = _project("mixed")
    for name, source in (("pc", "runtime"), ("api", "code"), ("manual-mod", "manual")):
        module = BizModule.objects.create(
            project=project, name=name, namespace=f"{source}:x", source=source
        )
        for idx, status in enumerate(("active", "draft", "deprecated")):
            _case(project, f"{name}-{idx}", module=module, status=status)
    _case(project, "unassigned")

    client = APIClient()
    client.force_authenticate(user=project.owner)
    data = client.get(DASHBOARD_URL, {"project": project.id}).json()

    assert data["total_cases"] == 10
    assert sum(row["total"] for row in data["by_module"]) == 10
    assert all(
        row["total"] == row["active"] + row["draft"] + row["deprecated"]
        for row in data["by_module"]
    )
