"""地址优先入口：生成提交的白名单透传、项目复用与视图契约守护测试。"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from apps.projects.models import Project
from apps.testgen_integration.models import UrlProject
from apps.testgen_integration.url_gen_client import submit_url_generation
from apps.testgen_integration.url_gen_views import UrlGenerationView
from apps.testgen_integration.url_projects import find_url_project

User = get_user_model()


def _mapping() -> UrlProject:
    user = User.objects.create_user(username="url-gen-u", password="x")
    project = Project.objects.create(name="url-gen-project", owner=user)
    return UrlProject.objects.create(
        normalized_url="https://gen.example.com/pc",
        scheme="https",
        host="gen.example.com",
        testhub_project=project,
        testgen_project_id=456,
    )


# --- client 白名单透传 ---
def test_submit_forwards_whitelist_and_forces_no_execute():
    payload = {
        "test_url": "https://gen.example.com/pc",
        "login_password": "secret",
        "changed_files": ["a.ts"],
        "project_id": 456,
        "runtime_scope": "targeted",
        "execute": True,
        "malicious_field": "drop-me",
    }
    with patch("apps.testgen_integration.url_gen_client._session.post") as post:
        post.return_value.status_code = 202
        post.return_value.json.return_value = {"task_id": "t1", "poll_url": "/x"}
        submit_url_generation(payload)
    body = post.call_args.kwargs["json"]
    assert body["execute"] is False  # 强制生成-only
    assert body["test_url"] == "https://gen.example.com/pc"
    assert body["login_password"] == "secret"
    assert "malicious_field" not in body


def test_submit_rejects_sidecar_error():
    with patch("apps.testgen_integration.url_gen_client._session.post") as post:
        post.return_value.status_code = 400
        post.return_value.text = "bad"
        with pytest.raises(RuntimeError, match="拒绝"):
            submit_url_generation({"test_url": "https://x"})


# --- 映射只读查询 ---
@pytest.mark.django_db
def test_find_url_project_reads_mapping_without_creating():
    _mapping()
    found = find_url_project(raw_url="HTTPS://GEN.example.com:443/pc/?x=1")
    assert found is not None
    assert found["testgen_project_id"] == 456
    assert UrlProject.objects.count() == 1


@pytest.mark.django_db
def test_find_url_project_returns_none_when_missing():
    assert find_url_project(raw_url="https://unknown.example.com/") is None


# --- 视图契约 ---
@pytest.mark.django_db
def test_view_requires_base_url():
    client = APIClient()
    client.force_authenticate(user=User.objects.create_user("vf", "x"))
    r = client.post("/api/testgen/url-generation/generate", {}, format="json")
    assert r.status_code == 400
    assert r.json()["ok"] is False


@pytest.mark.django_db
def test_view_resolves_project_id_and_submits():
    mapping = _mapping()
    client = APIClient()
    client.force_authenticate(user=User.objects.get(username="url-gen-u"))
    with patch.object(UrlGenerationView, "permission_classes", []), patch(
        "apps.testgen_integration.url_gen_views.submit_url_generation",
        return_value={"task_id": "t2", "poll_url": "/x"},
    ) as submit:
        r = client.post(
            "/api/testgen/url-generation/generate",
            {"test_url": "https://gen.example.com/pc", "changed_files": ["a.ts"]},
            format="json",
        )
    assert r.status_code == 202
    assert r.json()["task_id"] == "t2"
    assert submit.call_args.args[0]["project_id"] == mapping.testgen_project_id
