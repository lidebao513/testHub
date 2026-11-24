"""批次 1：业务模块实体 + TestCase 归属守护测试。"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from apps.biz_modules.models import BizModule
from apps.biz_modules.services import ensure_module, module_namespace
from apps.projects.models import Project
from apps.testcases.models import TestCase as CaseModel

User = get_user_model()


def _project(name="p") -> Project:
    return Project.objects.create(name=name, owner=User.objects.create_user(username=f"u-{name}"))


@pytest.mark.django_db
def test_module_namespace_prevents_bare_name_collision():
    assert module_namespace("code", "3") == "code:3"
    assert module_namespace("runtime", "https://example.com/pc") == "runtime:https://example.com/pc"
    assert module_namespace("runtime", "") == "runtime"


@pytest.mark.django_db
def test_ensure_module_is_idempotent_and_keeps_existing_description():
    project = _project()
    first = ensure_module(project.id, "pc", "runtime:https://a.com", "runtime", "first")
    second = ensure_module(project.id, "pc", "runtime:https://a.com", "runtime", "second")

    assert first.id == second.id
    assert BizModule.objects.count() == 1
    assert second.description == "first"  # 命中不覆盖


@pytest.mark.django_db
def test_same_name_different_namespace_or_project_survives():
    p1, p2 = _project("p1"), _project("p2")
    a = ensure_module(p1.id, "pc", "runtime:https://a.com", "runtime")
    b = ensure_module(p1.id, "pc", "runtime:https://b.com", "runtime")
    c = ensure_module(p2.id, "pc", "runtime:https://a.com", "runtime")

    assert len({a.id, b.id, c.id}) == 3
    assert BizModule.objects.count() == 3


@pytest.mark.django_db
def test_empty_module_name_falls_back_to_root():
    project = _project()
    assert ensure_module(project.id, "", "runtime:x").name == "root"


@pytest.mark.django_db
def test_deleting_module_sets_testcase_module_null():
    project = _project()
    module = ensure_module(project.id, "pc", "runtime:x", "runtime")
    case = CaseModel.objects.create(
        project=project,
        module=module,
        title="t",
        expected_result="ok",
        author=project.owner,
    )
    module.delete()
    case.refresh_from_db()
    assert case.module_id is None
    assert CaseModel.objects.filter(id=case.id).exists()


@pytest.mark.django_db
def test_biz_module_api_lists_only_accessible_projects_and_filters():
    mine, other = _project("mine"), _project("other")
    ensure_module(mine.id, "pc", "runtime:x", "runtime")
    ensure_module(other.id, "secret", "runtime:y", "runtime")

    client = APIClient()
    client.force_authenticate(user=mine.owner)
    resp = client.get("/api/biz-modules/")
    assert resp.status_code == 200
    names = [m["name"] for m in resp.json()["results"]]
    assert names == ["pc"]

    resp = client.get("/api/biz-modules/", {"source": "code"})
    assert resp.status_code == 200
    assert resp.json()["results"] == []


@pytest.mark.django_db
def test_biz_module_api_rejects_create_in_foreign_project():
    mine, other = _project("mine2"), _project("other2")
    client = APIClient()
    client.force_authenticate(user=mine.owner)
    resp = client.post(
        "/api/biz-modules/",
        {"project": other.id, "name": "hack", "namespace": "runtime:x", "source": "runtime"},
        format="json",
    )
    assert resp.status_code == 403
    assert BizModule.objects.filter(name="hack").count() == 0
