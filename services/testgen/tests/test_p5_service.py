"""P5 服务化测试：生成任务异步化（仅生成环节）。

覆盖：
- 异步提交 → 202 + task_id + poll_url；轮询到终态 success；
- 生成-only 红线：execute/exec_url 被拒（422）；
- 幂等：同 idempotency_key 重复提交返回既有 task_id；
- webhook：发布事件触发生成任务；
- 监控：metrics 反映任务状态分布；
- 鉴权：配置 token 后未带 token 被拒（401）；
- 契约：任务不存在返回 404（规范化错误）。
"""

import time

import pytest
from fastapi.testclient import TestClient

from service import tasks as task_engine
from service.app import app


@pytest.fixture()
def client():
    return TestClient(app, raise_server_exceptions=False)


def _poll_until(client, task_id: str, timeout: float = 25.0) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        resp = client.get(f"/api/v1/tasks/{task_id}")
        assert resp.status_code == 200, resp.text
        last = resp.json()
        if last["state"] in ("success", "failed", "cancelled"):
            return last
        time.sleep(0.15)
    raise AssertionError(f"任务 {task_id} 未在 {timeout}s 内终态：{last}")


def test_generate_async_runs_to_success(client, fresh_db, sample_repo):
    """P5 主入口：提交生成任务 → 轮询到 success，产物落库。"""
    resp = client.post(
        "/api/v1/generate",
        json={"local_path": str(sample_repo), "project_name": "p5-demo", "persist": True},
    )
    assert resp.status_code == 202
    body = resp.json()
    assert body["task_id"].startswith("gen-")
    assert body["poll_url"] == f"/api/v1/tasks/{body['task_id']}"

    final = _poll_until(client, body["task_id"])
    assert final["state"] == "success"
    assert final["result"]["counts"]["cases"] > 0, "生成环节应产出用例"
    assert final["progress"] == 1.0


def test_generate_rejects_execution(client, fresh_db, sample_repo):
    """生成-only 红线：要求执行用例一律被拒（422）。"""
    resp = client.post(
        "/api/v1/generate",
        json={"local_path": str(sample_repo), "execute": True},
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "validation_error"

    resp2 = client.post(
        "/api/v1/generate",
        json={"local_path": str(sample_repo), "exec_url": "https://x.test"},
    )
    assert resp2.status_code == 422


def test_generate_idempotent_returns_existing_task(client, fresh_db, sample_repo):
    """同请求重复提交：首个仍在 pending/running 时返回既有 task_id（不重复跑）。"""
    payload = {"local_path": str(sample_repo), "project_name": "idem", "persist": True}
    r1 = client.post("/api/v1/generate", json=payload)
    assert r1.status_code == 202
    tid1 = r1.json()["task_id"]
    # 紧接着再提交一次（首个任务大概率仍在 pending/running）
    r2 = client.post("/api/v1/generate", json=payload)
    assert r2.status_code == 202
    assert r2.json()["task_id"] == tid1, "幂等：应返回既有 task_id"
    _poll_until(client, tid1)  # 收尾，避免后台线程跨用例写库


def test_webhook_triggers_generation(client, fresh_db, sample_repo):
    """发布流水线 webhook：收到事件 → 触发生成任务（仅生成，不执行）。"""
    resp = client.post(
        "/api/v1/verify/webhook",
        json={
            "event": "deployment.success",
            "target": "research-agent",
            "commit": "abc123",
            "local_path": str(sample_repo),
            "mode": "full",
        },
    )
    assert resp.status_code == 202
    body = resp.json()
    assert body["task_id"].startswith("gen-")

    final = _poll_until(client, body["task_id"])
    assert final["state"] == "success"
    assert final["kind"] == "webhook_generate"
    assert final["result"]["counts"]["cases"] > 0


def test_webhook_idempotent_for_same_event(client, fresh_db, sample_repo):
    """同发布事件重复推送：只跑一次（返回既有 task_id）。"""
    payload = {
        "event": "deployment.success",
        "target": "ra",
        "commit": "c1",
        "local_path": str(sample_repo),
        "mode": "full",
    }
    r1 = client.post("/api/v1/verify/webhook", json=payload)
    r2 = client.post("/api/v1/verify/webhook", json=payload)
    assert r2.json()["task_id"] == r1.json()["task_id"]
    _poll_until(client, r1.json()["task_id"])


def test_metrics_reflects_task_states(client, fresh_db, sample_repo):
    """监控端点：反映任务按状态分布与最近任务。"""
    r = client.post(
        "/api/v1/generate",
        json={"local_path": str(sample_repo), "project_name": "metrics-demo", "persist": True},
    )
    tid = r.json()["task_id"]
    _poll_until(client, tid)

    m = client.get("/api/v1/metrics").json()
    assert m["service"] == "testgen-service"
    assert m["tasks_total"] >= 1
    assert m["by_state"].get("success", 0) >= 1
    assert any(t["task_id"] == tid for t in m["recent"])


def test_get_task_not_found_returns_normalized_404(client, fresh_db):
    """任务不存在：规范化 404（不得抛堆栈）。"""
    resp = client.get("/api/v1/tasks/nope")
    assert resp.status_code == 404
    assert resp.json()["code"] == "not_found"


def test_auth_enforced_on_generate(client, fresh_db, sample_repo, monkeypatch):
    """配置 token 后，生成端点未带 token 被拒（401）；健康检查不鉴权。"""
    import service.app as app_mod

    monkeypatch.setattr(app_mod.settings, "auth_token", "s3cret")
    assert client.get("/health").status_code == 200
    resp = client.post("/api/v1/generate", json={"local_path": str(sample_repo)})
    assert resp.status_code == 401
    assert resp.json()["code"] == "unauthorized"


def test_task_store_idempotency_lookup():
    """TaskStore 单测：同 idempotency_key 查找返回最新一条。"""
    store = task_engine.TaskStore()
    store.create(kind="generate", idempotency_key="k1")
    t2 = store.create(kind="generate", idempotency_key="k1")
    found = store.find_by_idempotency("k1")
    assert found is not None
    assert found.task_id == t2.task_id, "应返回最新创建的同 key 任务"
    assert store.find_by_idempotency("") is None
    assert store.find_by_idempotency("missing") is None
