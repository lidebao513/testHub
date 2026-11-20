"""多仓改造阶段 3 守护：批量只读端点按 repo_key 分组、单仓失败不阻断。"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from core import store
from service import app as service_app


@pytest.fixture()
def client():
    return TestClient(service_app.app, raise_server_exceptions=False)


@pytest.fixture()
def project_with_repos(fresh_db):
    conn = store.connect()
    pid = store.upsert_project("mr-refs", "/tmp/mr", conn=conn)
    store.upsert_repo_binding(pid, "backend", local_path="/tmp/be", conn=conn)
    store.upsert_repo_binding(pid, "frontend", repo_url="https://x/fe.git", conn=conn)
    conn.close()
    return pid


def test_repo_refs_branches_groups_by_repo_key(client, project_with_repos):
    def fake_branches(req):
        if "fe" in (req.repo_url or ""):
            return {"ok": True, "branches": ["main"], "source": "git"}
        return {"ok": True, "branches": ["master", "dev"], "source": "local"}

    with patch.object(service_app, "list_repo_branches", side_effect=fake_branches):
        resp = client.post(
            "/api/v1/repos/refs",
            json={"project_id": project_with_repos, "kind": "branches"},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] and data["kind"] == "branches"
    assert set(data["repos"]) == {"backend", "frontend"}
    assert data["repos"]["backend"]["branches"] == ["master", "dev"]
    assert data["repos"]["frontend"]["branches"] == ["main"]


def test_repo_refs_one_failure_does_not_block_others(client, project_with_repos):
    def fake_branches(req):
        if "fe" in (req.repo_url or ""):
            return {"ok": False, "branches": [], "error": "远端不可达"}
        return {"ok": True, "branches": ["master"], "source": "local"}

    with patch.object(service_app, "list_repo_branches", side_effect=fake_branches):
        resp = client.post(
            "/api/v1/repos/refs",
            json={"project_id": project_with_repos, "kind": "branches"},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"]
    assert data["repos"]["backend"]["branches"] == ["master"]
    assert data["repos"]["backend"]["error"] == ""
    assert "远端不可达" in data["repos"]["frontend"]["error"]


def test_repo_refs_commits_uses_binding_ref(client, project_with_repos):
    seen = {}

    def fake_commits(req):
        seen[req.repo_url or req.local_path] = req.ref
        return {"ok": True, "commits": [{"sha": "abc"}], "ref": req.ref}

    with patch.object(service_app, "list_repo_commits", side_effect=fake_commits):
        resp = client.post(
            "/api/v1/repos/refs",
            json={"project_id": project_with_repos, "kind": "commits", "ref": "release"},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert set(data["repos"]) == {"backend", "frontend"}
    assert all(r["commits"] == [{"sha": "abc"}] for r in data["repos"].values())
    assert all(v == "release" for v in seen.values()), "统一 ref 应下发到每个仓"


def test_repo_refs_empty_project(client, fresh_db):
    conn = store.connect()
    pid = store.upsert_project("mr-empty", "/tmp/empty", conn=conn)
    conn.close()
    resp = client.post("/api/v1/repos/refs", json={"project_id": pid, "kind": "branches"})
    assert resp.status_code == 200
    assert resp.json()["repos"] == {}


def test_repo_refs_rejects_invalid_kind(client, project_with_repos):
    resp = client.post(
        "/api/v1/repos/refs",
        json={"project_id": project_with_repos, "kind": "bogus"},
    )
    assert resp.status_code in (400, 422)
