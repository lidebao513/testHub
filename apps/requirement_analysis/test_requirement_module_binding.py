"""#453 需求侧模块归属守护测试：自动绑定、撞名隔离、幂等、历史回填。"""

from __future__ import annotations

from io import StringIO
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command

from apps.biz_modules.models import BizModule
from apps.biz_modules.services import ensure_module, module_namespace
from apps.projects.models import Project

from .models import BusinessRequirement, RequirementAnalysis, RequirementDocument
from .serializers import BusinessRequirementSerializer

User = get_user_model()


def _project(name: str) -> Project:
    return Project.objects.create(name=name, owner=User.objects.create_user(username=f"u-{name}"))


def _document(project: Project | None, title: str) -> RequirementDocument:
    return RequirementDocument.objects.create(
        title=title,
        file=f"requirement_docs/{title}.md",
        document_type="md",
        uploaded_by=User.objects.create_user(username=f"doc-{title}"),
        project=project,
    )


def _requirement(
    document: RequirementDocument, module: str = "用户管理", rid: str = "R1"
) -> BusinessRequirement:
    analysis = RequirementAnalysis.objects.create(document=document, requirements_count=1)
    requirement = BusinessRequirement(
        analysis=analysis,
        requirement_id=rid,
        requirement_name="登录需求",
        requirement_type="functional",
        module=module,
        requirement_level="high",
        description="desc",
        acceptance_criteria="acc",
    )
    requirement.save()
    return requirement


@pytest.mark.django_db
def test_requirement_save_binds_module_with_requirement_namespace():
    project = _project("bind")
    document = _document(project, "bind-doc")
    requirement = _requirement(document)
    requirement.refresh_from_db()

    assert requirement.module == "用户管理"  # 原字符串字段保留兼容
    assert requirement.module_ref is not None
    assert requirement.module_ref.name == "用户管理"
    assert requirement.module_ref.source == "requirement"
    assert requirement.module_ref.namespace == f"requirement:{document.id}"
    assert requirement.module_ref.project_id == project.id


@pytest.mark.django_db
def test_same_module_name_in_two_documents_is_isolated():
    project = _project("iso")
    first = _requirement(_document(project, "iso-doc-1"), rid="R1")
    second = _requirement(_document(project, "iso-doc-2"), rid="R2")
    first.refresh_from_db()
    second.refresh_from_db()

    assert first.module_ref_id != second.module_ref_id
    assert BizModule.objects.count() == 2


@pytest.mark.django_db
def test_requirement_module_does_not_share_code_namespace_row():
    project = _project("mix")
    requirement = _requirement(_document(project, "mix-doc"))
    code_module = ensure_module(project.id, "用户管理", module_namespace("code", "42"), "code")
    requirement.refresh_from_db()

    assert requirement.module_ref_id != code_module.id
    assert BizModule.objects.count() == 2


@pytest.mark.django_db
def test_document_without_project_is_skipped():
    requirement = _requirement(_document(None, "no-project"))
    requirement.refresh_from_db()

    assert requirement.module_ref_id is None
    assert BizModule.objects.count() == 0


@pytest.mark.django_db
def test_blank_module_is_skipped():
    requirement = _requirement(_document(_project("blank"), "blank-doc"), module="   ")
    requirement.refresh_from_db()

    assert requirement.module_ref_id is None
    assert BizModule.objects.count() == 0


@pytest.mark.django_db
def test_resave_is_idempotent_and_keeps_manual_description():
    requirement = _requirement(_document(_project("idem"), "idem-doc"))
    requirement.refresh_from_db()
    module_id = requirement.module_ref_id
    BizModule.objects.filter(id=module_id).update(description="人工编辑")

    requirement.requirement_name = "改过的需求"
    requirement.save()
    requirement.refresh_from_db()

    assert requirement.module_ref_id == module_id
    assert BizModule.objects.count() == 1
    assert BizModule.objects.get(id=module_id).description == "人工编辑"


@pytest.mark.django_db
def test_deleting_module_keeps_requirement_and_clears_binding():
    requirement = _requirement(_document(_project("del"), "del-doc"))
    requirement.refresh_from_db()
    requirement.module_ref.delete()

    requirement.refresh_from_db()
    assert BusinessRequirement.objects.filter(pk=requirement.pk).exists()
    assert requirement.module_ref_id is None


@pytest.mark.django_db
def test_binding_failure_does_not_break_requirement_save():
    with patch(
        "apps.requirement_analysis.module_signals.ensure_module",
        side_effect=RuntimeError("db down"),
    ):
        requirement = _requirement(_document(_project("fail"), "fail-doc"))

    assert BusinessRequirement.objects.filter(pk=requirement.pk).exists()
    assert requirement.module_ref_id is None


@pytest.mark.django_db
def test_serializer_exposes_module_binding_readonly():
    requirement = _requirement(_document(_project("ser"), "ser-doc"))
    requirement.refresh_from_db()

    data = BusinessRequirementSerializer(requirement).data
    assert data["module_ref"] == requirement.module_ref_id
    assert data["module_ref_name"] == "用户管理"


@pytest.mark.django_db
def test_backfill_command_is_dry_runnable_and_idempotent():
    document = _document(_project("backfill"), "backfill-doc")
    analysis = RequirementAnalysis.objects.create(document=document, requirements_count=2)
    historical = [
        BusinessRequirement(
            analysis=analysis,
            requirement_id=f"H{i}",
            requirement_name="历史需求",
            requirement_type="functional",
            module="订单管理",
            requirement_level="low",
            description="d",
            acceptance_criteria="a",
        )
        for i in range(2)
    ]
    BusinessRequirement.objects.bulk_create(historical)  # 绕过 signal 模拟历史数据
    assert BizModule.objects.count() == 0

    output = StringIO()
    call_command("backfill_requirement_modules", "--dry-run", stdout=output)
    assert BizModule.objects.count() == 0
    assert "would fill=2" in output.getvalue()

    call_command("backfill_requirement_modules", stdout=output)
    assert BizModule.objects.count() == 1
    assert BusinessRequirement.objects.filter(module_ref__isnull=True).count() == 0

    call_command("backfill_requirement_modules", stdout=output)
    assert BizModule.objects.count() == 1
