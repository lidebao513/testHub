"""HTTP 层测试：健康检查、鉴权、统一错误结构、端点契约。"""

import pytest
from fastapi.testclient import TestClient

from service.app import app


@pytest.fixture()
def client():
    return TestClient(app, raise_server_exceptions=False)


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["contract_version"] == "1.0"
    assert resp.headers.get("X-Request-ID")


def test_ready(client):
    resp = client.get("/ready")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ready"
    assert "llm_api_key" not in str(resp.json()), "就绪输出不得泄露密钥字段"


def test_request_id_is_echoed(client):
    resp = client.get("/health", headers={"X-Request-ID": "abc123"})
    assert resp.headers["X-Request-ID"] == "abc123"


def test_analyze_endpoint(fresh_db, sample_repo, client):
    resp = client.post("/api/v1/analyze", json={"local_path": str(sample_repo)})
    assert resp.status_code == 200
    body = resp.json()
    assert body["files"] > 0
    assert body["counts"].get("api", 0) >= 3


def test_pipeline_endpoint_writes_outputs(fresh_db, sample_repo, client):
    resp = client.post(
        "/api/v1/pipeline",
        json={"local_path": str(sample_repo), "project_name": "api-demo"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["result"]["project_id"]
    assert body["result"]["counts"]["cases"] > 0
    assert "test_cases" in body["outputs"]


def test_projects_and_traceability_endpoints(fresh_db, sample_repo, client):
    client.post("/api/v1/pipeline", json={"local_path": str(sample_repo)})
    projects = client.get("/api/v1/projects").json()["projects"]
    assert projects
    pid = projects[0]["id"]
    point = client.get(f"/api/v1/projects/{pid}/test-points").json()
    assert point["test_points"]
    trace = client.get(f"/api/v1/projects/{pid}/traceability").json()["traceability"]
    assert trace["orphan_tp_count"] == 0


def test_missing_path_returns_normalized_error(fresh_db, client):
    resp = client.post("/api/v1/pipeline", json={"local_path": "definitely/not/here"})
    assert resp.status_code == 500
    body = resp.json()
    assert body["code"] == "engine_error"
    assert "Traceback" not in resp.text, "不得把堆栈返回给客户端"


def test_invalid_scope_returns_validation_error(fresh_db, client, sample_repo):
    resp = client.post(
        "/api/v1/pipeline",
        json={"local_path": str(sample_repo), "scopes": ["不存在的维度"]},
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "validation_error"


def test_auth_enforced_when_token_configured(monkeypatch, client):
    import service.app as app_mod

    monkeypatch.setattr(app_mod.settings, "auth_token", "s3cret")
    assert client.get("/health").status_code == 200, "健康检查不鉴权"
    resp = client.post("/api/v1/analyze", json={"local_path": "."})
    assert resp.status_code == 401
    assert resp.json()["code"] == "unauthorized"
