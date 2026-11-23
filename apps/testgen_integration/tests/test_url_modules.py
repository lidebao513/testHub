"""地址优先入口：模块读取/扫描的映射解析与视图契约守护测试。"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from apps.projects.models import Project
from apps.testgen_integration.models import UrlProject
from apps.testgen_integration.url_modules import (
    read_modules_for_url,
    scan_modules_for_url,
)

User = get_user_model()


def _mapping() -> UrlProject:
    user = User.objects.create_user(username="url-mod-u", password="x")
    project = Project.objects.create(name="url-mod-project", owner=user)
    return UrlProject.objects.create(
        normalized_url="https://mod.example.com/pc",
        scheme="https",
        host="mod.example.com",
        testhub_project=project,
        testgen_project_id=321,
    )


@pytest.mark.django_db
def test_read_modules_uses_mapping_and_forwards_raw_url_variants() -> None:
    _mapping()
    with patch(
        "apps.testgen_integration.url_modules.fetch_modules",
        return_value={"ok": True, "project_id": 321, "modules": []},
    ) as mock:
        result = read_modules_for_url(raw_url="HTTPS://MOD.example.com:443/pc/?x=1")

    assert result["ok"] is True
    mock.assert_called_once_with(321)


@pytest.mark.django_db
def test_read_modules_without_mapping_asks_user_to_identify_project() -> None:
    with (
        patch("apps.testgen_integration.url_modules.fetch_modules") as mock,
        pytest.raises(ValueError, match="识别项目"),
    ):
        read_modules_for_url(raw_url="https://unknown.example.com/pc")
    mock.assert_not_called()


@pytest.mark.django_db
def test_scan_forwards_only_allowlisted_credentials_and_options() -> None:
    _mapping()
    with patch(
        "apps.testgen_integration.url_modules.scan_modules",
        return_value={"ok": True, "scan": {"persisted": 1}},
    ) as mock:
        scan_modules_for_url(
            raw_url="https://mod.example.com/pc",
            options={
                "base_url": "https://mod.example.com/pc",
                "login_user": "tester",
                "login_password": "secret",
                "login_otp": "261015",
                "max_pages": 5,
                "unknown": "ignored",
            },
        )

    payload = mock.call_args.args[1]
    assert mock.call_args.args[0] == 321
    assert payload == {
        "login_user": "tester",
        "login_password": "secret",
        "login_otp": "261015",
        "max_pages": 5,
    }


@pytest.mark.django_db
def test_modules_views_return_400_when_address_missing_or_unmapped() -> None:
    user = User.objects.create_user(username="url-mod-view-u", password="x")
    client = APIClient()
    client.force_authenticate(user=user)

    missing = client.get("/api/testgen/url-projects/modules")
    assert missing.status_code == 400

    unmapped = client.get(
        "/api/testgen/url-projects/modules", {"base_url": "https://none.example.com"}
    )
    assert unmapped.status_code == 400
    assert "识别项目" in unmapped.json()["error"]

    scan_missing = client.post(
        "/api/testgen/url-projects/modules/scan", {}, format="json"
    )
    assert scan_missing.status_code == 400


@pytest.mark.django_db
def test_modules_views_return_sidecar_payload() -> None:
    _mapping()
    user = User.objects.create_user(username="url-mod-view-ok", password="x")
    client = APIClient()
    client.force_authenticate(user=user)
    payload = {"ok": True, "modules": [{"name": "/pc/tasks", "source": "scan"}]}

    with patch(
        "apps.testgen_integration.url_modules.fetch_modules", return_value=payload
    ):
        read = client.get(
            "/api/testgen/url-projects/modules", {"base_url": "https://mod.example.com/pc"}
        )
    assert read.status_code == 200
    assert read.json()["modules"][0]["name"] == "/pc/tasks"

    with patch(
        "apps.testgen_integration.url_modules.scan_modules",
        return_value={**payload, "scan": {"persisted": 1}},
    ):
        scanned = client.post(
            "/api/testgen/url-projects/modules/scan",
            {"base_url": "https://mod.example.com/pc", "login_otp": "261015"},
            format="json",
        )
    assert scanned.status_code == 200
    assert scanned.json()["scan"]["persisted"] == 1
