"""Batch 4 A-2/D-2 守护：`/describe`、`/analyze` 的可选异步化。

契约：
- 默认（不传 async_mode）仍是同步 200，老调用方零行为变化；
- `async_mode=true` → 202 + task_id + poll_url，结果经任务详情轮询；
- 提交调用本身不执行重活（不阻塞连接），长耗时 LLM/扫描在后台作业里跑；
- 与 `/generate` 共用 app.py 门面的 task_store / executor，patch 门面符号生效。
"""

from __future__ import annotations

import json
import time
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from service import app as app_module
from service.app import app


@pytest.fixture()
def client():
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _llm_configured(monkeypatch):
    monkeypatch.setattr(app_module.settings, "llm_api_key", "test-key")
    monkeypatch.setattr(app_module.settings, "llm_base_url", "http://stub.local/v1")
    monkeypatch.setattr(app_module.settings, "llm_model", "stub-model")


class _Msg:
    def __init__(self, content: str) -> None:
        self.content = content


class _Choice:
    def __init__(self, content: str) -> None:
        self.message = _Msg(content)


class _Resp:
    def __init__(self, content: str) -> None:
        self.choices = [_Choice(content)]


def _slow_llm(delay: float):
    def _impl(**kwargs):
        time.sleep(delay)
        prompt = (kwargs.get("messages") or [{}])[0].get("content", "")
        keys = [
            line.strip().split("key:", 1)[1].split("|", 1)[0].strip()
            for line in prompt.splitlines()
            if line.strip().startswith("- key:")
        ]
        return _Resp(json.dumps({"结果": [{"key": k, "功能": f"功能-{k}"} for k in keys]}))

    return _impl


def _describe_payload(n: int, repo: str) -> dict:
    return {
        "repo_url": repo,
        "items": [
            {"path": f"pkg/mod_{i}.py", "name": f"mod_{i}.py", "functions": [f"fn_{i}"]}
            for i in range(n)
        ],
    }


def _poll_done(client, poll_url: str, timeout: float = 40.0) -> dict:
    deadline = time.monotonic() + timeout
    last = {}
    while time.monotonic() < deadline:
        resp = client.get(poll_url)
        assert resp.status_code == 200, resp.text
        last = resp.json()
        if last["state"] in ("success", "failed", "cancelled"):
            return last
        time.sleep(0.1)
    raise AssertionError(f"任务未在 {timeout}s 内终态: {last}")


def test_describe_async_returns_task_and_result(client, fresh_db, monkeypatch):
    """async_mode=true → 202 + poll_url；轮询 success，result 与同步结构一致。"""
    monkeypatch.setattr(app_module, "chat_with_fallback", _slow_llm(0.01))
    resp = client.post(
        "/api/v1/describe",
        json={**_describe_payload(10, "https://example.com/b4-async.git"), "async_mode": True},
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["task_id"].startswith("gen-")
    assert body["poll_url"] == f"/api/v1/tasks/{body['task_id']}"
    assert body["kind"] == "describe"

    done = _poll_done(client, body["poll_url"])
    assert done["state"] == "success", done.get("error")
    result = done["result"]
    assert result["ok"] is True
    assert result["unresolved"] == 0
    assert len(result["descriptions"]) == 10


def test_describe_default_stays_synchronous(client, fresh_db, monkeypatch):
    """默认同步 200：不传 async_mode 的老契约完全不变。"""
    monkeypatch.setattr(app_module, "chat_with_fallback", _slow_llm(0.01))
    resp = client.post(
        "/api/v1/describe", json=_describe_payload(5, "https://example.com/b4-sync.git")
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "descriptions" in body
    assert "task_id" not in body


def test_describe_async_submit_does_not_block(client, fresh_db, monkeypatch):
    """提交调用不跑 LLM（耗时远小于同步执行），/health 在任务运行期间仍快速响应。"""
    monkeypatch.setattr(app_module, "chat_with_fallback", _slow_llm(0.3))
    payload = {**_describe_payload(40, "https://example.com/b4-noblock.git"), "async_mode": True}

    started = time.monotonic()
    resp = client.post("/api/v1/describe", json=payload)
    submit_elapsed = time.monotonic() - started
    assert resp.status_code == 202, resp.text
    # 同步跑 8 组 × 0.3s ≈ 0.6s；提交只 create+submit，应远小于该值。
    assert submit_elapsed < 0.3, f"提交被重活阻塞（{submit_elapsed:.2f}s）"

    health_started = time.monotonic()
    health = client.get("/health")
    health_elapsed = time.monotonic() - health_started
    assert health.status_code == 200
    assert health_elapsed < 0.3, f"/health 被长任务阻塞（{health_elapsed:.2f}s）"

    done = _poll_done(client, resp.json()["poll_url"])
    assert done["state"] == "success", done.get("error")


def test_analyze_async_returns_task(client, fresh_db, sample_repo):
    """analyze async_mode=true → 202 + poll_url，终态 result 含 functional_points 等结构。"""
    resp = client.post(
        "/api/v1/analyze",
        json={"local_path": str(sample_repo), "async_mode": True},
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["kind"] == "analyze"
    done = _poll_done(client, body["poll_url"])
    assert done["state"] == "success", done.get("error")
    assert "functional_points" in done["result"]


def test_analyze_default_stays_synchronous(client, fresh_db, sample_repo):
    """analyze 默认同步 200，老契约不变。"""
    resp = client.post("/api/v1/analyze", json={"local_path": str(sample_repo)})
    assert resp.status_code == 200, resp.text
    assert "functional_points" in resp.json()


def test_aux_async_uses_app_facade_symbols(client, fresh_db):
    """门面契约：patch service.app.task_store / executor 生效（与 /generate 一致）。"""
    fake_task = app_module.GenerationTask("gen-aux-facade", kind="describe", state="pending")

    with (
        patch("service.app.task_store") as ts,
        patch("service.app.executor") as ex,
    ):
        ts.create.return_value = fake_task
        resp = client.post(
            "/api/v1/describe",
            json={**_describe_payload(2, "https://example.com/b4-facade.git"), "async_mode": True},
        )
    assert resp.status_code == 202
    assert resp.json()["task_id"] == "gen-aux-facade"
    assert ts.create.called
    assert ex.submit.called
