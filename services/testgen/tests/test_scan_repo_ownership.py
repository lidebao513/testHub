"""P1：scan 返回携带仓库归属的守护测试（多仓 describe 缓存复用的前置）。

后端统一算 namespace 并随返回项回传；前端批次 2 只透传。这里锁住：远程/本地/
多仓三种归属、无身份不写键、批量 refs 逐仓隔离、门面 patch 契约与两处模型一致。
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from core import store
from core.describe_namespace import describe_namespace
from service import app as service_app
from service.app import app
from service.routers.scan import ScanRequest as RouterScanRequest


@pytest.fixture()
def client():
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture()
def project_with_repos(fresh_db):
    conn = store.connect()
    pid = store.upsert_project("mr-scan-ns", "/tmp/mr", conn=conn)
    store.upsert_repo_binding(pid, "backend", local_path="/tmp/be", conn=conn)
    store.upsert_repo_binding(pid, "frontend", repo_url="https://x/fe.git", conn=conn)
    conn.close()
    return pid


def _items() -> list[dict[str, object]]:
    return [
        {
            "path": "a.py",
            "name": "a.py",
            "ext": ".py",
            "functions": ["fa"],
            "classes": [],
            "bytes": 10,
            "change_status": "modified",
        },
        {
            "path": "b.py",
            "name": "b.py",
            "ext": ".py",
            "functions": [],
            "classes": [],
            "bytes": 5,
            "change_status": "unchanged",
        },
    ]


def _fake_dispatch(items: list[dict[str, object]]):
    def _run(_req):
        return items, "local", {"total_matched": len(items), "truncated": False}

    return _run


def _post_scan(client, **payload):
    body = {"repo_url": "", "local_path": "", "code_source": {}, **payload}
    with patch("service.app._dispatch_scan", side_effect=_fake_dispatch(_items())):
        return client.post("/api/v1/scan", json=body)


def test_remote_namespace_keeps_repo_url(client) -> None:
    url = "https://codeup.aliyun.com/org/repo.git"
    resp = _post_scan(client, repo_url=url)
    assert resp.status_code == 200
    data = resp.json()
    assert data["namespace"] == url
    assert all(it["namespace"] == url for it in data["items"])


def test_local_namespace_isolated_between_dirs(client, tmp_path) -> None:
    one = _post_scan(client, local_path=str(tmp_path)).json()["namespace"]
    two = _post_scan(client, local_path=str(tmp_path / "other")).json()["namespace"]
    assert one is not None and one.startswith("local:")
    assert two != one
    items = _post_scan(client, local_path=str(tmp_path)).json()["items"]
    assert all(it["namespace"] == one for it in items)


def test_multi_repo_namespace(client) -> None:
    resp = _post_scan(client, local_path="/tmp/be", repo_key="backend", project_id=30)
    assert resp.status_code == 200
    data = resp.json()
    assert data["namespace"] == "multi:30:backend"
    assert data["repo_key"] == "backend"
    assert data["project_id"] == 30
    assert all(it["namespace"] == "multi:30:backend" for it in data["items"])
    assert all(it["repo_key"] == "backend" for it in data["items"])
    assert all(it["project_id"] == 30 for it in data["items"])


def test_explicit_namespace_overrides_all(client) -> None:
    resp = _post_scan(
        client,
        namespace="runtime:http://x/y",
        repo_key="backend",
        project_id=30,
        repo_url="https://codeup.aliyun.com/org/repo.git",
    )
    assert resp.json()["namespace"] == "runtime:http://x/y"


def test_no_identity_omits_namespace_key(client) -> None:
    data = _post_scan(client).json()
    assert data["namespace"] is None
    assert data["repo_key"] == ""
    assert data["project_id"] is None
    assert all("namespace" not in it for it in data["items"])
    assert all("repo_key" not in it for it in data["items"])


def test_legacy_response_fields_unchanged(client) -> None:
    data = _post_scan(client, repo_url="https://x/repo.git").json()
    assert data["ok"] is True
    assert data["source"] == "local"  # 桩固定返回 local，真实分派不受影响
    assert data["count"] == 2
    assert data["total"] == 2
    assert data["truncated"] is False
    assert data["summary"] == {"added": 0, "modified": 1, "unchanged": 1}


def test_refs_scan_carries_per_repo_namespace(client, project_with_repos) -> None:
    with patch("service.app._dispatch_scan", side_effect=_fake_dispatch(_items())):
        resp = client.post(
            "/api/v1/repos/refs",
            json={"project_id": project_with_repos, "kind": "scan"},
        )
    assert resp.status_code == 200
    repos = resp.json()["repos"]
    assert set(repos) == {"backend", "frontend"}
    be = repos["backend"]
    fe = repos["frontend"]
    assert be["namespace"] == f"multi:{project_with_repos}:backend"
    assert fe["namespace"] == f"multi:{project_with_repos}:frontend"
    assert all(it["namespace"] == be["namespace"] for it in be["items"])
    assert all(it["namespace"] == fe["namespace"] for it in fe["items"])
    assert be["items"][0]["namespace"] != fe["items"][0]["namespace"]


def test_dispatch_scan_patch_contract_alive(client) -> None:
    """门面符号仍可 patch（#439 拆分后的硬约束）。"""
    seen: list[object] = []

    def _spy(req):
        seen.append(req)
        return [], "local", {}

    with patch("service.app._dispatch_scan", side_effect=_spy):
        resp = client.post("/api/v1/scan", json={"local_path": "/tmp/x"})
    assert resp.status_code == 200
    assert len(seen) == 1
    assert getattr(seen[0], "local_path", "") == "/tmp/x"


def test_scan_request_models_aligned() -> None:
    """路由与门面两处 ScanRequest 字段集合必须一致（门面被旧测试直接构造）。"""
    router_fields = set(RouterScanRequest.model_fields)
    facade_fields = set(service_app.ScanRequest.model_fields)
    assert router_fields == facade_fields
    assert {"repo_key", "project_id", "namespace"} <= router_fields


def test_attach_does_not_mutate_source() -> None:
    from service.routers.scan import _attach_repo_ownership

    source = [{"path": "a.py"}]
    attached = _attach_repo_ownership(source, namespace="ns", repo_key="rk", project_id=1)
    assert "namespace" not in source[0]
    assert attached[0] == {"path": "a.py", "namespace": "ns", "repo_key": "rk", "project_id": 1}
    assert describe_namespace(repo_key="rk", project_id=1) == "multi:1:rk"
