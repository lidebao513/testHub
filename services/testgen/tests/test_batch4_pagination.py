"""Batch 4 C-1/D-1 守护：用例分页 + 任务列表轻量字段。

契约：
- `GET /api/v1/tasks` 与 `/metrics.recent` 只返轻量字段，不含 request/result/error；
- `GET /api/v1/tasks/{id}` 仍返完整 result（详情端点不被削弱）；
- `GET /api/v1/tasks/{id}/cases` 支持 offset/limit + total，默认排除 obsolete；
- 分页在 SQL 层做，limit 有上界钳制（防一次拉爆）。
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from core import store
from core.contracts import CaseSpec
from service import app as app_module
from service.app import app


@pytest.fixture()
def client():
    return TestClient(app, raise_server_exceptions=False)


def _case(tp_id: str) -> CaseSpec:
    return CaseSpec(
        tc_no=tp_id,
        title=f"用例 {tp_id}",
        ctype="api",
        steps=[{"action": "http_probe", "tp_id": tp_id, "expect": "返回 2xx"}],
        module="m",
        case_type="正常",
        priority="P2",
        precondition="服务已启动",
        doc_steps=[{"seq": 1, "type": "前置", "desc": "服务已启动"}],
        tp_id=tp_id,
        fp_contract_id="FP-x",
    )


def _make_task(pid: int, *, with_result: bool = True) -> str:
    task = app_module.task_store.create(
        kind="pipeline",
        project_id=pid,
        request=json.dumps({"local_path": "/tmp/repo", "login_password": "secret"}),
    )
    if with_result:
        app_module.task_store.update(
            task.task_id,
            state="success",
            result=json.dumps({"summary": {"total": 1}, "cases": [{"x": "y"}] * 50}),
        )
    return task.task_id


def test_summary_dict_is_subset_of_full(client, fresh_db):
    """轻量字段集是 to_dict 的真子集：轮询所需字段全在，大字段全不在。"""
    task = app_module.task_store.create(kind="pipeline")
    app_module.task_store.update(
        task.task_id,
        state="success",
        result=json.dumps({"summary": {"total": 1}}),
        error="boom",
    )
    full = app_module.task_store.get(task.task_id).to_dict()
    summary = app_module.task_store.get(task.task_id).to_summary_dict()

    for key in ("request", "result", "error"):
        assert key in full and key not in summary
    for key in ("task_id", "project_id", "kind", "state", "progress", "stage", "created_at"):
        assert summary[key] == full[key]


def test_task_list_and_metrics_are_lightweight(client, fresh_db):
    """列表与监控的 recent 均不含大字段；详情端点仍完整返回 result。"""
    pid = store.upsert_project("b4-list", "/tmp/b4-list")
    store.reconcile_cases(pid, [_case(f"TP-{i}") for i in range(5)])
    tid = _make_task(pid)

    listed = client.get("/api/v1/tasks?limit=10").json()["tasks"]
    assert listed and all("result" not in t and "request" not in t for t in listed)

    metrics = client.get("/api/v1/metrics").json()["recent"]
    assert metrics and all("result" not in t for t in metrics)

    detail = client.get(f"/api/v1/tasks/{tid}").json()
    assert detail["result"]["summary"] == {"total": 1}, "详情端点必须仍返回完整 result"


def test_task_cases_pagination(client, fresh_db):
    """offset/limit/total 生效；SQL 层分页，只回当前页。"""
    pid = store.upsert_project("b4-page", "/tmp/b4-page")
    store.reconcile_cases(pid, [_case(f"TP-{i:03d}") for i in range(7)])
    tid = _make_task(pid, with_result=False)

    page1 = client.get(f"/api/v1/tasks/{tid}/cases?offset=0&limit=3").json()
    assert page1["total"] == 7
    assert page1["offset"] == 0 and page1["limit"] == 3
    assert len(page1["cases"]) == 3
    assert [c["tp_id"] for c in page1["cases"]] == ["TP-000", "TP-001", "TP-002"]

    page3 = client.get(f"/api/v1/tasks/{tid}/cases?offset=6&limit=3").json()
    assert page3["total"] == 7
    assert [c["tp_id"] for c in page3["cases"]] == ["TP-006"]


def test_task_cases_excludes_obsolete_by_default(client, fresh_db):
    """默认排除 obsolete；显式 include_obsolete=true 才含。"""
    pid = store.upsert_project("b4-obsolete", "/tmp/b4-obsolete")
    store.reconcile_cases(pid, [_case("TP-A"), _case("TP-B")])
    store.reconcile_cases(pid, [_case("TP-A")])  # TP-B 变 obsolete
    tid = _make_task(pid, with_result=False)

    default = client.get(f"/api/v1/tasks/{tid}/cases").json()
    assert default["total"] == 1
    assert [c["tp_id"] for c in default["cases"]] == ["TP-A"]

    included = client.get(f"/api/v1/tasks/{tid}/cases?include_obsolete=true").json()
    assert included["total"] == 2


def test_task_cases_limit_is_clamped(client, fresh_db):
    """limit 上界钳制：超大值返回 422，防一次拉爆。"""
    pid = store.upsert_project("b4-clamp", "/tmp/b4-clamp")
    store.reconcile_cases(pid, [_case("TP-1")])
    tid = _make_task(pid, with_result=False)
    assert client.get(f"/api/v1/tasks/{tid}/cases?limit=5000").status_code == 422
    assert client.get(f"/api/v1/tasks/{tid}/cases?offset=-1").status_code == 422


def test_store_list_cases_page_matches_list_cases(client, fresh_db):
    """分页全量拼接与 list_cases 口径一致（含 steps/doc_steps 反序列化）。"""
    pid = store.upsert_project("b4-consistency", "/tmp/b4-consistency")
    store.reconcile_cases(pid, [_case(f"TP-{i}") for i in range(5)])
    expected = store.list_cases(pid)
    total, page = store.list_cases_page(pid, offset=0, limit=100)
    assert total == len(expected) == 5
    assert page == expected
