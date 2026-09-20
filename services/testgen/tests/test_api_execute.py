"""G-2 · 服务化执行端点接线测试（FastAPI TestClient，执行器以假结论注入避免真连网络）。

验证点：
- `POST /api/v1/execute` 带 `project_id`：异步提交执行任务、可轮询、结论随任务返回；
- `POST /api/v1/execute` 带来源（生成+执行）：同样走异步任务；execute 被强制开启；
- `POST /api/v1/verify/webhook` 带 `execute=true`：创建 `webhook_execute` 任务并真正执行。
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from engine import pipeline
from service import app as service_app


@pytest.fixture()
def client():
    return TestClient(service_app.app)


def _clear_gen_tasks():
    conn = service_app.connect()
    try:
        conn.execute("DELETE FROM gen_tasks")
        conn.commit()
    finally:
        conn.close()


def _poll_until_done(client: TestClient, task_id: str, timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        last = client.get(f"/api/v1/tasks/{task_id}").json()
        if last["state"] in ("success", "failed", "cancelled"):
            return last
        time.sleep(0.05)
    return last


_FAKE_EXEC_SUMMARY = {
    "total": 2,
    "executed": 2,
    "pass": 2,
    "fail": 0,
    "error": 0,
    "skipped": 0,
    "results": [
        {
            "tc_no": "TP-1",
            "status": "pass",
            "notes": ["ok"],
            "duration_ms": 1,
            "screenshot_path": "",
        },
        {
            "tc_no": "TP-2",
            "status": "pass",
            "notes": ["ok"],
            "duration_ms": 1,
            "screenshot_path": "",
        },
    ],
}


def test_execute_endpoint_project_only(client, fresh_db, monkeypatch):
    """G-2：带 project_id 的执行端点应异步跑通并返回结论（执行器被假结论替换）。"""
    _clear_gen_tasks()
    monkeypatch.setattr(
        pipeline,
        "run_execution",
        lambda pid, opts=None, progress=None: {
            **_FAKE_EXEC_SUMMARY,
            "batch_id": "B-FAKE",
            "state": "completed",
            "started_at": "",
            "finished_at": "",
        },
    )
    resp = client.post("/api/v1/execute", json={"project_id": 7})
    assert resp.status_code == 202
    body = resp.json()
    assert body["task_id"]
    assert body["poll_url"].endswith(body["task_id"])
    assert body["kind"] == "execute"

    result = _poll_until_done(client, body["task_id"])
    assert result["state"] == "success"
    assert result["result"]["pass"] == 2


def test_execute_endpoint_generate_then_execute(client, fresh_db, monkeypatch):
    """G-2：带来源的执行端点应强制开启 execute（即便调用方未显式声明）。"""
    _clear_gen_tasks()
    captured = {}

    def fake_run_pipeline(opts, *, progress=None):
        captured["execute"] = bool(opts.target_req.execute)
        # 须与真实 PipelineResult 对齐：app.py 会读取 test_points / cases 再交给 OutputWriter
        return type(
            "R",
            (),
            {
                "project_id": 1,
                "test_points": [],
                "cases": [],
                "to_dict": lambda self: {"ok": True},
            },
        )()

    monkeypatch.setattr(pipeline, "run_pipeline", fake_run_pipeline)
    monkeypatch.setattr(service_app, "OutputWriter", lambda: _NoopWriter())

    resp = client.post(
        "/api/v1/execute",
        json={"local_path": "/tmp/src", "project_name": "demo"},
    )
    assert resp.status_code == 202
    result = _poll_until_done(client, resp.json()["task_id"])
    assert result["state"] == "success"
    assert captured["execute"] is True  # 生成+执行路径必须强制 execute=True


class _NoopWriter:
    def write_all(self, *args, **kwargs):
        return {}


def test_webhook_with_execute_flag(client, fresh_db, monkeypatch):
    """G-2：webhook 带 execute=true 应创建 webhook_execute 任务并真正执行生成+执行。"""
    _clear_gen_tasks()
    monkeypatch.setattr(
        pipeline,
        "run_pipeline",
        lambda opts, **kw: type(
            "R",
            (),
            {
                "project_id": 1,
                "test_points": [],
                "cases": [],
                "to_dict": lambda self: {},
            },
        )(),
    )
    monkeypatch.setattr(service_app, "OutputWriter", lambda: _NoopWriter())

    resp = client.post(
        "/api/v1/verify/webhook",
        json={"event": "deployment.success", "target": "svc-x", "execute": True},
    )
    assert resp.status_code == 202
    task = resp.json()
    assert task["kind"] == "webhook_execute"
    result = _poll_until_done(client, task["task_id"])
    assert result["state"] == "success"


def test_generate_endpoint_still_rejects_execute(client, fresh_db):
    """回归守护：/api/v1/generate 仍拒绝 execute（生成-only 红线不被 G-2 破）。"""
    _clear_gen_tasks()
    resp = client.post(
        "/api/v1/generate",
        json={"local_path": "/tmp/src", "execute": True},
    )
    assert resp.status_code == 422
