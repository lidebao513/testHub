"""地址优先入口：归一 URL → 两侧项目查重/建项守护测试。"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model

from apps.projects.models import Project, ProjectEnvironment
from apps.testgen_integration.models import UrlProject
from apps.testgen_integration.url_identity import normalize_url
from apps.testgen_integration.url_projects import ensure_url_project

User = get_user_model()


@pytest.mark.django_db
def test_normalize_matches_sidecar_contract() -> None:
    got = normalize_url("HTTPS://Example.COM:443/pc/?from=menu#top")
    assert got["normalized"] == "https://example.com/pc"
    assert got["host"] == "example.com"
    assert got["path"] == "/pc"


@pytest.mark.django_db
def test_ensure_creates_both_sides_and_mapping() -> None:
    user = User.objects.create_user(username="url-u1", password="x")
    with patch(
        "apps.testgen_integration.url_projects.ensure_sidecar_project",
        return_value={"ok": True, "project_id": 77},
    ) as mock:
        result = ensure_url_project(raw_url="https://example.com:443/pc/", user=user)

    assert result["created"] is True
    assert result["normalized_url"] == "https://example.com/pc"
    assert result["testgen_project"]["id"] == 77
    assert UrlProject.objects.count() == 1
    assert Project.objects.filter(owner=user, name="example.com-pc").count() == 1
    env = ProjectEnvironment.objects.get(project__owner=user)
    assert env.base_url == "https://example.com:443/pc/"
    assert env.is_default is True
    mock.assert_called_once()


@pytest.mark.django_db
def test_ensure_hit_reuses_project_and_skips_sidecar() -> None:
    user = User.objects.create_user(username="url-u2", password="x")
    with patch(
        "apps.testgen_integration.url_projects.ensure_sidecar_project",
        return_value={"ok": True, "project_id": 88},
    ) as mock:
        first = ensure_url_project(raw_url="http://example.com:8080/pc", user=user)
        second = ensure_url_project(raw_url="http://EXAMPLE.com:8080/pc/?a=1", user=user)

    assert first["created"] is True
    assert second["created"] is False
    assert second["testhub_project"]["id"] == first["testhub_project"]["id"]
    assert second["testgen_project"]["id"] == 88
    assert UrlProject.objects.count() == 1
    assert Project.objects.filter(owner=user).count() == 1
    mock.assert_called_once()  # 第二次命中映射，不再调 sidecar


@pytest.mark.django_db
def test_ensure_generates_distinct_names_for_distinct_paths() -> None:
    user = User.objects.create_user(username="url-u3", password="x")
    with patch(
        "apps.testgen_integration.url_projects.ensure_sidecar_project",
        side_effect=[{"project_id": 1}, {"project_id": 2}],
    ):
        a = ensure_url_project(raw_url="http://example.com/pc", user=user)
        b = ensure_url_project(raw_url="http://example.com/chat", user=user)

    assert a["created"] is True and b["created"] is True
    assert a["testhub_project"]["name"] == "example.com-pc"
    assert b["testhub_project"]["name"] == "example.com-chat"
    assert UrlProject.objects.count() == 2
