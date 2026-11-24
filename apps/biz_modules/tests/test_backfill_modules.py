"""#451：历史用例模块回填守护（幂等 / dry-run / 命名空间判定 / 筛选）。"""

from __future__ import annotations

from io import StringIO

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command

from apps.biz_modules.management.commands.backfill_testcase_modules import (
    Command,
    _trace_module,
)
from apps.biz_modules.models import BizModule
from apps.projects.models import Project
from apps.testcases.models import TestCase as CaseModel
from apps.testgen_integration.models import UrlProject

User = get_user_model()


def _project(name: str) -> Project:
    return Project.objects.create(name=name, owner=User.objects.create_user(username=f"u-{name}"))


def _case(
    project: Project,
    *,
    module_name: str = "pc",
    tags: list | None = None,
    description: str | None = None,
) -> CaseModel:
    desc = (
        description
        if description is not None
        else f"追溯：suite_id=1; module={module_name}; testgen_tc_no=TC-1; source=testgen"
    )
    return CaseModel.objects.create(
        project=project,
        title=f"t-{module_name}",
        expected_result="ok",
        author=project.owner,
        description=desc,
        tags=["testgen", "tg:TC-1"] if tags is None else tags,
    )


def test_trace_module_only_reads_trace_line() -> None:
    assert _trace_module("追溯：a=1; module=pc; source=testgen") == "pc"
    assert _trace_module("普通说明：module=wrong\n追溯：module=right; x=1") == "right"
    assert _trace_module("追溯：module=; source=testgen") is None
    assert _trace_module("没有追溯段") is None


@pytest.mark.django_db
def test_dry_run_does_not_write_and_reports_would_fill() -> None:
    project = _project("dry")
    UrlProject.objects.create(
        normalized_url="https://example.com/pc",
        host="example.com",
        testhub_project=project,
        testgen_project_id=7,
    )
    case = _case(project)

    out = StringIO()
    call_command("backfill_testcase_modules", dry_run=True, stdout=out)

    case.refresh_from_db()
    assert case.module_id is None
    assert BizModule.objects.count() == 0
    assert "[dry-run]" in out.getvalue()
    assert "待回填 1 条" in out.getvalue()


@pytest.mark.django_db
def test_runtime_backfill_is_idempotent_and_filterable() -> None:
    project = _project("runtime")
    UrlProject.objects.create(
        normalized_url="https://example.com/pc",
        host="example.com",
        testhub_project=project,
        testgen_project_id=7,
    )
    case = _case(project, module_name="pc")

    first = Command().backfill()
    assert first["filled"] == 1
    case.refresh_from_db()
    module = BizModule.objects.get()
    assert case.module_id == module.id
    assert module.namespace == "runtime:https://example.com/pc"
    assert module.source == "runtime"

    # 幂等：第二次扫描仍命中候选条件之外的行数为 0，不重复建模块/改写
    second = Command().backfill()
    assert second["filled"] == 0
    assert BizModule.objects.count() == 1

    # 回填后按模块筛选可命中（前端 /api/testcases/?module=<id> 走的同一 ORM 条件）
    assert list(CaseModel.objects.filter(module=module).values_list("id", flat=True)) == [case.id]


@pytest.mark.django_db
def test_code_backfill_uses_explicit_testgen_project_id() -> None:
    project = _project("code")
    _case(project, module_name="api")

    stats = Command().backfill(testgen_project_id=42)
    assert stats["filled"] == 1
    module = BizModule.objects.get()
    assert module.namespace == "code:42"
    assert module.source == "code"


@pytest.mark.django_db
def test_skips_non_testgen_and_missing_trace() -> None:
    project = _project("skip")
    UrlProject.objects.create(
        normalized_url="https://a.com",
        host="a.com",
        testhub_project=project,
        testgen_project_id=1,
    )
    _case(project, tags=["manual"])
    _case(project, description="人工用例，没有追溯段")

    stats = Command().backfill()
    assert stats["skipped_not_testgen"] == 1
    assert stats["skipped_no_module"] == 1
    assert stats["filled"] == 0
    assert BizModule.objects.count() == 0


@pytest.mark.django_db
def test_multiple_url_bindings_are_skipped_instead_of_guessing() -> None:
    project = _project("ambiguous")
    for url in ("https://a.com", "https://b.com"):
        UrlProject.objects.create(
            normalized_url=url,
            host=url.split("//")[1],
            testhub_project=project,
            testgen_project_id=1,
        )
    case = _case(project)

    stats = Command().backfill()
    assert stats["skipped_ambiguous_namespace"] == 1
    case.refresh_from_db()
    assert case.module_id is None


@pytest.mark.django_db
def test_existing_manual_assignment_is_never_overwritten() -> None:
    project = _project("manual-assigned")
    UrlProject.objects.create(
        normalized_url="https://a.com",
        host="a.com",
        testhub_project=project,
        testgen_project_id=1,
    )
    module = BizModule.objects.create(project=project, name="人工模块", source="manual")
    case = _case(project, module_name="pc")
    CaseModel.objects.filter(id=case.id).update(module_id=module.id)

    stats = Command().backfill()
    assert stats["scanned"] == 0  # 已有归属的用例不在候选集
    case.refresh_from_db()
    assert case.module_id == module.id
    assert BizModule.objects.count() == 1


@pytest.mark.django_db
def test_project_filter_limits_backfill() -> None:
    p1, p2 = _project("f1"), _project("f2")
    for p in (p1, p2):
        UrlProject.objects.create(
            normalized_url=f"https://{p.name}.com",
            host=f"{p.name}.com",
            testhub_project=p,
            testgen_project_id=1,
        )
        _case(p, module_name=p.name)

    stats = Command().backfill(project_id=p1.id)
    assert stats["scanned"] == 1
    assert stats["filled"] == 1
    assert BizModule.objects.filter(project=p2).count() == 0
