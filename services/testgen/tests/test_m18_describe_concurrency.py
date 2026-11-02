"""M18 回归：`/describe` 组间并发 + `/pipeline` 可选异步。

要点：
- `/describe` 原本按 5 条一组**串行**调用 LLM，30 个文件最坏 6×llm_timeout；
  改造后组间并发（上限 4 路）+ 总预算兜底，结果结构与降级语义不变。
- `/pipeline` 默认同步（老调用方零影响），`async_mode=true` 返回 202 + poll_url。
"""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from service import app as app_module
from service.app import app


@pytest.fixture()
def client():
    return TestClient(app, raise_server_exceptions=False)


class _Msg:
    def __init__(self, content: str) -> None:
        self.content = content


class _Choice:
    def __init__(self, content: str) -> None:
        self.message = _Msg(content)


class _Resp:
    def __init__(self, content: str) -> None:
        self.choices = [_Choice(content)]


def _keys_from_prompt(messages) -> list[str]:
    """从 prompt 的 `- key: xxx | ...` 行里取回本组的 key（桩据此按组返回）。"""
    prompt = (messages or [{}])[0].get("content", "")
    keys = []
    for line in prompt.splitlines():
        line = line.strip()
        if line.startswith("- key:"):
            keys.append(line.split("key:", 1)[1].split("|", 1)[0].strip())
    return keys


def _stub_llm(*, delay: float = 0.2, fail_keys: tuple[str, ...] = ()):
    def _impl(**kwargs):
        time.sleep(delay)
        keys = _keys_from_prompt(kwargs.get("messages"))
        if any(k in fail_keys for k in keys):
            raise RuntimeError("stub llm failure")
        body = {"结果": [{"key": k, "功能": f"功能-{k}"} for k in keys]}
        return _Resp(json.dumps(body, ensure_ascii=False))

    return _impl


def _items(n: int, repo: str = "https://example.com/m18.git") -> dict:
    """构造 n 个待描述项；每个用例用不同 repo_url，避免描述缓存串味。"""
    return {
        "repo_url": repo,
        "items": [
            {"path": f"pkg/mod_{i}.py", "name": f"mod_{i}.py", "functions": [f"fn_{i}"]}
            for i in range(n)
        ],
    }


def test_describe_runs_chunks_concurrently(client, fresh_db, monkeypatch):
    """8 组 × 0.2s：并发后应明显快于串行 1.6s，且描述完整。"""
    monkeypatch.setattr(app_module, "chat_with_fallback", _stub_llm(delay=0.2))
    items = _items(40)  # 40 条 / 每组 5 条 = 8 组

    started = time.monotonic()
    resp = client.post("/api/v1/describe", json=items)
    elapsed = time.monotonic() - started

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["unresolved"] == 0
    assert len(body["descriptions"]) == 40
    # 8 组串行下限 1.6s；4 路并发两轮 ≈0.4s。阈值取 1.2s 留足机器抖动余量。
    assert elapsed < 1.2, f"describe 未并发（耗时 {elapsed:.2f}s）"


def test_describe_single_group_failure_only_affects_that_group(client, fresh_db, monkeypatch):
    """单组 LLM 失败只让该组计入 unresolved，其余组照常返回。"""
    monkeypatch.setattr(
        app_module, "chat_with_fallback", _stub_llm(delay=0.02, fail_keys=("pkg/mod_0.py",))
    )

    resp = client.post("/api/v1/describe", json=_items(20, repo="https://example.com/m18-fail.git"))
    body = resp.json()

    assert resp.status_code == 200
    assert body["unresolved"] == 5, "失败组恰好 5 条未拿到描述"
    assert len(body["descriptions"]) == 15


def test_describe_budget_degrades_instead_of_hanging(client, fresh_db, monkeypatch):
    """超出总预算：未完成组计入 unresolved，接口仍按时返回（不挂死）。"""
    monkeypatch.setattr(app_module, "chat_with_fallback", _stub_llm(delay=0.35))
    monkeypatch.setattr(app_module.settings, "describe_budget_seconds", 0.1)

    started = time.monotonic()
    resp = client.post(
        "/api/v1/describe", json=_items(20, repo="https://example.com/m18-budget.git")
    )
    elapsed = time.monotonic() - started

    assert resp.status_code == 200
    body = resp.json()
    assert body["unresolved"] > 0, "超预算必须有未解析项（不假装成功）"
    assert elapsed < 1.5, f"超预算后仍被拖住（耗时 {elapsed:.2f}s）"
    # 超预算的组仍在后台收尾（真实语义：不阻塞响应），等它们结束再进入下一用例，
    # 避免与本机 WAL 的单写约束碰到（否则下一用例可能读到 readonly）。
    time.sleep(0.5)


def test_pipeline_async_mode_returns_task(client, fresh_db, sample_repo):
    """`async_mode=true` → 202 + task_id + poll_url，轮询到 success。"""
    resp = client.post(
        "/api/v1/pipeline",
        json={"local_path": str(sample_repo), "async_mode": True},
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["task_id"]
    assert body["poll_url"].startswith("/api/v1/tasks/")

    deadline = time.time() + 40
    last = None
    while time.time() < deadline:
        poll = client.get(body["poll_url"])
        assert poll.status_code == 200, poll.text
        last = poll.json()
        if last["state"] in ("success", "failed", "cancelled"):
            break
        time.sleep(0.15)
    assert last and last["state"] == "success", last
    assert last["result"] is not None


def test_pipeline_default_stays_synchronous(client, fresh_db, sample_repo):
    """默认（不传 async_mode）仍是同步返回，老调用方语义不变。"""
    resp = client.post("/api/v1/pipeline", json={"local_path": str(sample_repo)})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "result" in body
    assert "task_id" not in body
