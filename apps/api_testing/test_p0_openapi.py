"""P0-2 单元测试：OpenAPI 解析 / SSRF 守卫 / 导入落库（不触发真实 LLM）。"""

import json

import pytest
from django.contrib.auth import get_user_model

from .services import OpenApiImportService

User = get_user_model()


MINIMAL_OPENAPI = {
    "openapi": "3.0.0",
    "info": {"title": "Demo API", "version": "1.0.0"},
    "servers": [{"url": "https://api.example.com"}],
    "paths": {
        "/users": {
            "get": {
                "summary": "列表用户",
                "parameters": [
                    {
                        "name": "page",
                        "in": "query",
                        "required": False,
                        "schema": {"type": "integer"},
                    }
                ],
            },
            "post": {
                "summary": "创建用户",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {"name": {"type": "string"}},
                            }
                        }
                    },
                },
            },
        },
        "/users/{id}": {
            "get": {
                "summary": "获取用户",
                "parameters": [
                    {
                        "name": "id",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                    }
                ],
            },
        },
    },
}


def test_parse_openapi_json():
    raw = json.dumps(MINIMAL_OPENAPI)
    spec = OpenApiImportService.parse_openapi(raw, "json")
    assert spec["info"]["title"] == "Demo API"
    assert "/users" in spec["paths"]


def test_parse_openapi_yaml():
    raw = (
        "openapi: 3.0.0\n"
        "info:\n"
        "  title: YAML API\n"
        "  version: '1.0'\n"
        "paths:\n"
        "  /ping:\n"
        "    get:\n"
        "      summary: 健康检查\n"
    )
    spec = OpenApiImportService.parse_openapi(raw, "yaml")
    assert spec["info"]["title"] == "YAML API"
    assert "/ping" in spec["paths"]


def test_parse_openapi_invalid_json_raises():
    with pytest.raises(ValueError):
        OpenApiImportService.parse_openapi("{not valid json", "json")


def test_resolve_base_urls_blocks_metadata():
    # 169.254.169.254 属链路本地/云元数据地址，必然被 SSRF 守卫拦截（无需 DNS）
    spec = {"servers": [{"url": "http://169.254.169.254/latest"}]}
    with pytest.raises(Exception):
        OpenApiImportService._resolve_base_urls(spec)


def test_resolve_base_urls_valid(monkeypatch):
    # 隔离真实 DNS/SSRF 解析，仅验证 _resolve_base_urls 的拼接与透传
    monkeypatch.setattr(
        "apps.api_testing.services.validate_url", lambda url, *a, **k: None
    )
    spec = {"servers": [{"url": "https://api.example.com/"}]}
    urls = OpenApiImportService._resolve_base_urls(spec)
    assert urls == ["https://api.example.com"]


def test_resolve_base_urls_swagger2(monkeypatch):
    monkeypatch.setattr(
        "apps.api_testing.services.validate_url", lambda url, *a, **k: None
    )
    spec = {"host": "api.example.com", "basePath": "/v1", "schemes": ["https"]}
    urls = OpenApiImportService._resolve_base_urls(spec)
    assert urls == ["https://api.example.com/v1"]


def test_build_request_payload_basic():
    payload = OpenApiImportService._build_request_payload(
        "/users/{id}",
        "get",
        {
            "summary": "获取用户",
            "parameters": [
                {
                    "name": "id",
                    "in": "path",
                    "required": True,
                    "schema": {"type": "string"},
                }
            ],
        },
        "https://api.example.com",
    )
    assert payload["method"] == "GET"
    assert payload["url"] == "https://api.example.com/users/{id}"
    assert "id" in payload["params"]


@pytest.mark.django_db
def test_import_openapi_creates_assets(monkeypatch):
    # 隔离 SSRF 解析（避免测试环境无 DNS），其余走真实解析与落库逻辑
    monkeypatch.setattr(
        "apps.api_testing.services.validate_url", lambda url, *a, **k: None
    )
    user = User.objects.create_user(username="p0tester", password="x")
    from .models import ApiProject

    proj = ApiProject.objects.create(
        name="P0 测试项目",
        project_type="HTTP",
        status="IN_PROGRESS",
        owner=user,
    )
    spec_import = OpenApiImportService.import_openapi(
        raw_spec=json.dumps(MINIMAL_OPENAPI),
        spec_format="json",
        project=proj,
        user=user,
        name="导入测试",
    )
    assert spec_import.status == "success"
    assert spec_import.imported_count == 3  # get/post /users + get /users/{id}
    from .models import ApiCollection, ApiRequest

    assert ApiCollection.objects.filter(project=proj).count() == 1
    reqs = ApiRequest.objects.filter(collection__project=proj)
    assert reqs.count() == 3
    # 导入的接口应带可追溯 tag
    assert any("spec_import:" in (t or "") for t in reqs.first().tags)


@pytest.mark.django_db
def test_import_openapi_ssrf_blocked_rolls_back():
    # 真实 SSRF 守卫：元数据地址必须被拒绝并标记 failed（不抛异常，记录持久化）
    user = User.objects.create_user(username="p0tester2", password="x")
    from .models import ApiProject

    proj = ApiProject.objects.create(
        name="P0 测试项目2",
        project_type="HTTP",
        status="IN_PROGRESS",
        owner=user,
    )
    bad_spec = json.dumps({"servers": [{"url": "http://169.254.169.254"}], "paths": {}})
    spec_import = OpenApiImportService.import_openapi(
        raw_spec=bad_spec,
        spec_format="json",
        project=proj,
        user=user,
    )
    assert spec_import.status == "failed"
    assert spec_import.error_message
    from .models import ApiSpecImport

    persisted = ApiSpecImport.objects.filter(id=spec_import.id, status="failed").first()
    assert persisted is not None


@pytest.mark.django_db
def test_import_openapi_skips_ref_and_reports(monkeypatch):
    # $ref 操作应被跳过并计入 skipped，不阻断其余接口导入（标记为 partial）
    monkeypatch.setattr(
        "apps.api_testing.services.validate_url", lambda url, *a, **k: None
    )
    user = User.objects.create_user(username="p0tester3", password="x")
    from .models import ApiProject

    proj = ApiProject.objects.create(
        name="P0 测试项目3",
        project_type="HTTP",
        status="IN_PROGRESS",
        owner=user,
    )
    spec = {
        "openapi": "3.0.0",
        "info": {"title": "Ref API", "version": "1.0.0"},
        "servers": [{"url": "https://api.example.com"}],
        "paths": {
            "/ok": {"get": {"summary": "正常接口"}},
            "/ref": {"get": {"$ref": "#/components/operations/getOk"}},
        },
    }
    spec_import = OpenApiImportService.import_openapi(
        raw_spec=json.dumps(spec),
        spec_format="json",
        project=proj,
        user=user,
    )
    assert spec_import.status == "partial"
    assert spec_import.imported_count == 1
    assert spec_import.skipped_count == 1
    assert any("$ref" in r for r in spec_import.skipped_reasons)
    from .models import ApiRequest

    assert ApiRequest.objects.filter(collection__project=proj).count() == 1


@pytest.mark.django_db
def test_import_openapi_empty_paths_reports(monkeypatch):
    # 无可导入接口时标记为 partial 并给出原因（不误判为成功）
    monkeypatch.setattr(
        "apps.api_testing.services.validate_url", lambda url, *a, **k: None
    )
    user = User.objects.create_user(username="p0tester4", password="x")
    from .models import ApiProject

    proj = ApiProject.objects.create(
        name="P0 测试项目4",
        project_type="HTTP",
        status="IN_PROGRESS",
        owner=user,
    )
    spec = {
        "openapi": "3.0.0",
        "info": {"title": "Empty API", "version": "1.0.0"},
        "servers": [{"url": "https://api.example.com"}],
        "paths": {},
    }
    spec_import = OpenApiImportService.import_openapi(
        raw_spec=json.dumps(spec),
        spec_format="json",
        project=proj,
        user=user,
    )
    assert spec_import.status == "partial"
    assert spec_import.imported_count == 0
    assert any("未发现" in r for r in spec_import.skipped_reasons)


@pytest.mark.django_db
def test_import_openapi_oversized_guard():
    # 规格超过 2MB 应被拒绝并标记 failed（不触发解析/落库）
    user = User.objects.create_user(username="p0tester5", password="x")
    from .models import ApiProject, ApiSpecImport

    proj = ApiProject.objects.create(
        name="P0 测试项目5",
        project_type="HTTP",
        status="IN_PROGRESS",
        owner=user,
    )
    big = "x" * (2 * 1024 * 1024 + 10)
    spec_import = OpenApiImportService.import_openapi(
        raw_spec=big,
        spec_format="json",
        project=proj,
        user=user,
    )
    assert spec_import.status == "failed"
    assert "过大" in spec_import.error_message
    # 不应创建任何 collection/request
    from .models import ApiCollection, ApiRequest

    assert ApiCollection.objects.filter(project=proj).count() == 0
    assert ApiRequest.objects.filter(collection__project=proj).count() == 0
    ApiSpecImport.objects.filter(id=spec_import.id).delete()
