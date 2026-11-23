"""地址优先入口：生成结果一键回流用例库的契约守护测试。

重点：按 task_id 取用例、排除 obsolete、tg: 幂等（重复回流不翻倍）、
项目解析兜底、sidecar 不可达状态码，以及旧回流链路文件零改动。
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
import requests
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from apps.projects.models import Project
from apps.testcases.models import TestCase
from apps.testgen_integration.models import UrlProject
from apps.testgen_integration.url_sync_views import active_cases_of

User = get_user_model()
SYNC_URL = "/api/testgen/url-generation/sync"


def _case(tc_no: str, *, status: str = "generated") -> dict:
    return {
        "tc_no": tc_no,
        "title": f"用例 {tc_no}",
        "case_type": "正常",
        "priority": "P2",
        "status": status,
    }


def _auth_client():
    user = User.objects.create_user(username="url-sync-u", password="x")
    client = APIClient()
    client.force_authenticate(user=user)
    return client, user


def _no_bridge():
    """桥接是附加项，测试里短路以免依赖 AICase 真实写库。"""
    return patch(
        "apps.testgen_integration.url_sync_views.flow.bridge_aicases",
        return_value={"total": 0, "created": 0, "updated": 0, "skipped": 0},
    )


def test_active_cases_of_filters_obsolete_only():
    payload = {
        "cases": [_case("A"), _case("B", status="obsolete"), _case("C", status="updated")]
    }
    assert [c["tc_no"] for c in active_cases_of(payload)] == ["A", "C"]
    assert active_cases_of({}) == []


@pytest.mark.django_db
def test_sync_requires_task_id():
    client, _ = _auth_client()
    r = client.post(SYNC_URL, {}, format="json")
    assert r.status_code == 400
    assert r.json()["ok"] is False


@pytest.mark.django_db
def test_sync_without_project_and_without_mapping_is_400():
    client, _ = _auth_client()
    r = client.post(
        SYNC_URL, {"task_id": "t1", "base_url": "https://unknown.example.com/"}, format="json"
    )
    assert r.status_code == 400
    assert "project_id" in r.json()["error"]


@pytest.mark.django_db
def test_sync_is_idempotent_and_skips_obsolete():
    client, user = _auth_client()
    project = Project.objects.create(name="url-sync-project", owner=user)
    payload = {"cases": [_case("TC-1"), _case("TC-2", status="obsolete")]}

    with (
        patch(
            "apps.testgen_integration.url_sync_views.task_cases", return_value=payload
        ) as fetch,
        _no_bridge(),
    ):
        first = client.post(
            SYNC_URL, {"task_id": "t-1", "project_id": project.id}, format="json"
        )
        second = client.post(
            SYNC_URL, {"task_id": "t-1", "project_id": project.id}, format="json"
        )

    assert first.status_code == 200
    assert first.json()["total"] == 1
    assert first.json()["created"] == 1
    assert second.json()["created"] == 0
    assert second.json()["updated"] == 1
    assert fetch.call_count == 2
    assert TestCase.objects.filter(project=project).count() == 1
    row = TestCase.objects.get(project=project)
    assert "tg:TC-1" in row.tags
    assert row.author_id == user.id


@pytest.mark.django_db
def test_sync_resolves_project_from_normalized_url():
    client, user = _auth_client()
    project = Project.objects.create(name="mapped-project", owner=user)
    UrlProject.objects.create(
        normalized_url="https://sync.example.com/pc",
        scheme="https",
        host="sync.example.com",
        testhub_project=project,
        testgen_project_id=789,
    )
    with (
        patch(
            "apps.testgen_integration.url_sync_views.task_cases",
            return_value={"cases": [_case("TC-3")]},
        ),
        _no_bridge(),
    ):
        r = client.post(
            SYNC_URL,
            {"task_id": "t-2", "base_url": "HTTPS://SYNC.example.com:443/pc/?x=1"},
            format="json",
        )
    assert r.status_code == 200
    assert r.json()["project_id"] == project.id
    assert r.json()["created"] == 1


@pytest.mark.django_db
def test_sync_sidecar_unreachable_returns_503():
    client, user = _auth_client()
    project = Project.objects.create(name="offline-project", owner=user)
    with patch(
        "apps.testgen_integration.url_sync_views.task_cases",
        side_effect=requests.exceptions.ConnectionError("refused"),
    ):
        r = client.post(
            SYNC_URL, {"task_id": "t-3", "project_id": project.id}, format="json"
        )
    assert r.status_code == 503
    assert r.json()["ok"] is False
    assert TestCase.objects.count() == 0
