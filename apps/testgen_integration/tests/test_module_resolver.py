"""批次 2：回流 module_resolver 与用例归属守护测试。"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model

from apps.biz_modules.models import BizModule
from apps.projects.models import Project
from apps.testcases.models import TestCase as CaseModel
from apps.testgen_integration import flow
from apps.testgen_integration.module_resolver import (
    code_module_resolver,
    runtime_module_resolver,
)

User = get_user_model()


def _project(name="p") -> Project:
    return Project.objects.create(
        name=name, owner=User.objects.create_user(username=f"u-{name}")
    )


def _case(module="pc", tc_no="tc-1") -> dict:
    return {"tc_no": tc_no, "title": "t", "module": module, "priority": "P1"}


@pytest.mark.django_db
def test_runtime_resolver_creates_module_with_namespace():
    project = _project("rt")
    resolver = runtime_module_resolver(project.id, "https://example.com/pc")
    mid = resolver(_case())
    assert mid is not None
    module = BizModule.objects.get(id=mid)
    assert module.name == "pc"
    assert module.source == "runtime"
    assert module.namespace == "runtime:https://example.com/pc"


@pytest.mark.django_db
def test_code_resolver_same_name_does_not_collide_with_runtime():
    project = _project("mix")
    code_id = code_module_resolver(project.id, 42)(_case(tc_no="a"))
    rt_id = runtime_module_resolver(project.id, "https://example.com/pc")(
        _case(tc_no="b")
    )
    assert code_id != rt_id
    assert BizModule.objects.count() == 2
    assert BizModule.objects.get(id=code_id).namespace == "code:42"


@pytest.mark.django_db
def test_resolver_returns_none_for_missing_module_name():
    project = _project("blank")
    assert runtime_module_resolver(project.id, "https://a.com")(_case(module="")) is None
    assert BizModule.objects.count() == 0


@pytest.mark.django_db
def test_resolver_fail_open_when_ensure_raises():
    project = _project("fail")
    resolver = runtime_module_resolver(project.id, "https://a.com")
    with patch(
        "apps.testgen_integration.module_resolver.ensure_module",
        side_effect=RuntimeError("db down"),
    ):
        assert resolver(_case()) is None  # 不抛异常，回落 None


@pytest.mark.django_db
def test_sync_cases_with_resolver_persists_module_id():
    project = _project("sync")
    cases = [_case(tc_no="tc-1"), _case(module="api", tc_no="tc-2")]

    def upsert(tg_id: str, payload: dict) -> str:
        row = CaseModel.objects.create(**payload)
        row.tags = payload.get("tags", []) + [f"tg:{tg_id}"]
        row.save()
        return flow.CREATED

    result = flow.sync_cases(
        cases,
        project_id=project.id,
        author_id=project.owner.id,
        upsert=upsert,
        module_resolver=runtime_module_resolver(project.id, "https://example.com"),
    )
    assert result == {"total": 2, "created": 2, "updated": 0, "skipped": 0}
    assert CaseModel.objects.filter(project_id=project.id, module__isnull=False).count() == 2
    assert set(
        CaseModel.objects.filter(project_id=project.id).values_list("module__name", flat=True)
    ) == {"pc", "api"}


@pytest.mark.django_db
def test_sync_cases_without_resolver_keeps_module_null():
    """旧回流路径（不注入 resolver）行为完全不变：module_id 不写。"""
    project = _project("old")

    def upsert(tg_id: str, payload: dict) -> str:
        assert "module_id" not in payload
        CaseModel.objects.create(**payload)
        return flow.CREATED

    result = flow.sync_cases(
        [_case()],
        project_id=project.id,
        author_id=project.owner.id,
        upsert=upsert,
    )
    assert result["created"] == 1
    assert CaseModel.objects.filter(project_id=project.id, module__isnull=True).count() == 1


@pytest.mark.django_db
def test_repeated_sync_does_not_duplicate_module_or_case():
    """重复回流：tg: 幂等用例不翻倍，模块 get-or-create 也不翻倍。"""
    project = _project("idem")
    resolver = runtime_module_resolver(project.id, "https://example.com")
    seen: dict[str, object] = {}

    def upsert(tg_id: str, payload: dict) -> str:
        tag = f"tg:{tg_id}"
        if tag in seen:
            row = seen[tag]
            for key, value in payload.items():
                setattr(row, key, value)
            row.save()
            return flow.UPDATED
        row = CaseModel.objects.create(**payload)
        seen[tag] = row
        return flow.CREATED

    first = flow.sync_cases(
        [_case()],
        project_id=project.id,
        author_id=project.owner.id,
        upsert=upsert,
        module_resolver=resolver,
    )
    second = flow.sync_cases(
        [_case()],
        project_id=project.id,
        author_id=project.owner.id,
        upsert=upsert,
        module_resolver=resolver,
    )
    assert first["created"] == 1 and second["updated"] == 1
    assert CaseModel.objects.filter(project_id=project.id).count() == 1
    assert BizModule.objects.count() == 1
