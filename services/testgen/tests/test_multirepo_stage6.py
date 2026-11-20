"""M1 守护：项目仓库绑定清单端点 + 前端多仓载入/切换 UI（渐进注入，旧单仓零回归）。"""

from __future__ import annotations

from pathlib import Path

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
    pid = store.upsert_project("mr-m1", "/tmp/mr", conn=conn)
    store.upsert_repo_binding(
        pid,
        "backend",
        local_path="/tmp/be",
        base="main",
        target="dev",
        ref="dev",
        conn=conn,
    )
    store.upsert_repo_binding(
        pid,
        "frontend",
        repo_url="https://x/fe.git",
        conn=conn,
    )
    conn.close()
    return pid


def test_project_repos_returns_specs_and_multi_flag(client, project_with_repos):
    resp = client.get(f"/api/v1/projects/{project_with_repos}/repos")
    assert resp.status_code == 200
    data = resp.json()
    assert data["project_id"] == project_with_repos
    assert data["multi"] is True
    by_key = {r["repo_key"]: r for r in data["repos"]}
    assert set(by_key) == {"backend", "frontend"}
    assert by_key["backend"]["local_path"] == "/tmp/be"
    assert by_key["backend"]["base"] == "main"
    assert by_key["backend"]["target"] == "dev"
    assert by_key["backend"]["ref"] == "dev"
    assert by_key["frontend"]["repo_url"] == "https://x/fe.git"
    # 未设置的 base/target 转 None，与 RepoSpec 可空语义一致
    assert by_key["frontend"]["base"] is None
    assert by_key["frontend"]["target"] is None
    # 每项都是 setMultiRepos 可直接消费的键集合
    expected = {"repo_key", "local_path", "repo_url", "base", "target", "ref"}
    assert all(expected <= set(r) for r in data["repos"])


def test_project_repos_empty_project_falls_back_single(client, fresh_db):
    conn = store.connect()
    pid = store.upsert_project("mr-m1-empty", "/tmp/empty", conn=conn)
    conn.close()
    resp = client.get(f"/api/v1/projects/{pid}/repos")
    assert resp.status_code == 200
    data = resp.json()
    assert data["repos"] == []
    assert data["multi"] is False


def test_project_repos_single_binding_not_multi(client, fresh_db):
    conn = store.connect()
    pid = store.upsert_project("mr-m1-single", "/tmp/single", conn=conn)
    store.upsert_repo_binding(pid, "default", local_path="/tmp/one", conn=conn)
    conn.close()
    resp = client.get(f"/api/v1/projects/{pid}/repos")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["repos"]) == 1
    assert data["repos"][0]["repo_key"] == "default"
    assert data["multi"] is False


def test_project_repos_unknown_project_returns_empty(client):
    resp = client.get("/api/v1/projects/999999/repos")
    assert resp.status_code == 200
    assert resp.json()["repos"] == []


def test_frontend_has_multirepo_load_and_switch_ui():
    """前端必须有：端点调用、注入点刷新、载入/退出 UI、buildBody repos 组装（静态守护）。"""
    html = (Path(__file__).resolve().parents[1] / "service" / "web" / "index.html").read_text(
        encoding="utf-8"
    )
    assert 'id="multi_pid"' in html
    assert 'id="multi_repo_box"' in html
    assert "async function loadMultiRepos" in html
    assert "function exitMultiRepos" in html
    assert "function renderMultiRepoBox" in html
    assert "/repos" in html and "encodeURIComponent(String(id))" in html
    # setMultiRepos 变更后刷新显示框
    assert "renderMultiRepoBox();" in html
    # 载入后注入点 + 生成体组装仍在（防止 M1 破坏 v1.7 既有能力）
    assert "setMultiRepos(specs)" in html
    assert "b.repos" in html
    assert "window.MULTI_REPO_SPECS" in html
