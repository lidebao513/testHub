"""优化总纲 Batch 5 批次 2（E-3~E-6）守护测试。

E-3 失败错误可展开 / E-4 空选全量确认 / E-5 5 分钟超时 + loading / E-6 任务筛选分页。
前端是原生静态页（service/web/index.html），沿用仓库既有「静态守护」先例
（test_b5_frontend_describe_params.py）；后端分页用真实 store + TestClient 跑契约。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from service import app as app_module
from service.app import app


_INDEX_HTML = Path(__file__).resolve().parents[1] / "service" / "web" / "index.html"
_TASKS_PY = Path(__file__).resolve().parents[1] / "service" / "tasks.py"
_TASKS_ROUTER_PY = Path(__file__).resolve().parents[1] / "service" / "routers" / "tasks.py"


def _html() -> str:
    return _INDEX_HTML.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# E-3：失败原因默认 40 字，点「展开」看全文（转义防注入）
# ---------------------------------------------------------------------------
def test_e3_failed_error_expandable() -> None:
    html = _html()
    assert "function taskErrorHtml(t)" in html
    assert "const _expandedTaskErrors = new Set()" in html
    assert "function toggleTaskError(id)" in html
    assert 't.state==="failed") prog = taskErrorHtml(t)' in html
    assert 'String(t.error||"失败").slice(0,40)' not in html  # 旧的裸截断不得回来
    assert "raw.slice(0, 40)" in html  # 列表仍只显示 40 字
    assert "escapeHtml(raw)" in html  # 全文也要转义
    assert 'onclick="toggleTaskError(' in html
    assert 'title="${full}"' in html  # 悬停可见全文


# ---------------------------------------------------------------------------
# E-4：空选语义仍是「全量」，但提交前确认里显式说明
# ---------------------------------------------------------------------------
def test_e4_empty_selection_confirmation() -> None:
    html = _html()
    assert "if(selCount === 0){" in html
    assert "未勾选任何代码文件" in html
    assert "未勾选=全量" in html
    assert "请点「取消」后到 B 区勾选" in html
    assert "preflightGenerate(body)" in html  # 复用已有确认弹窗，不新增二次弹窗


# ---------------------------------------------------------------------------
# E-5：5 分钟硬超时 + 提交 loading + 慢任务提示
# ---------------------------------------------------------------------------
def test_e5_request_timeout_and_loading() -> None:
    html = _html()
    assert "const REQUEST_TIMEOUT_MS = 5 * 60 * 1000" in html
    assert "async function api(path, opts, timeoutMs)" in html
    assert "new AbortController()" in html
    assert 'e.name === "AbortError"' in html
    assert "if(timer) clearTimeout(timer)" in html
    assert (
        '"/api/v1/generate", {method:"POST", body:JSON.stringify(body)}, REQUEST_TIMEOUT_MS' in html
    )
    assert (
        '"/api/v1/execute", {method:"POST", body:JSON.stringify(body)}, REQUEST_TIMEOUT_MS' in html
    )
    assert "正在提交生成任务" in html
    assert "function notifyIfSlow()" in html
    assert "notifyIfSlow();" in html
    assert "任务已运行超过 5 分钟" in html
    assert "resetSlowNotice()" in html


# ---------------------------------------------------------------------------
# E-6：前端筛选/分页控件 + 后端契约
# ---------------------------------------------------------------------------
def test_e6_frontend_filter_pagination() -> None:
    html = _html()
    assert 'id="tasks_state_filter"' in html
    assert 'id="tasks_prev"' in html
    assert 'id="tasks_next"' in html
    assert 'id="tasks_page_info"' in html
    assert "function onTaskFilterChange()" in html
    assert "function taskPage(delta)" in html
    assert "const TASK_PAGE_SIZE = 20" in html
    assert "&offset=${taskOffset}&state=${encodeURIComponent(state)}" in html
    assert "r.total != null ? r.total : ts.length" in html


def test_e6_backend_accepts_filter_params() -> None:
    router = _TASKS_ROUTER_PY.read_text(encoding="utf-8")
    assert "offset: int = Query(0, ge=0)" in router
    assert 'pattern="^(|pending|running|success|failed|cancelled)$"' in router
    assert "A.task_store.count_tasks(state=state)" in router
    store_src = _TASKS_PY.read_text(encoding="utf-8")
    assert 'def count_tasks(self, state: str = "") -> int:' in store_src
    assert "ORDER BY created_at DESC LIMIT ? OFFSET ?" in store_src


@pytest.fixture()
def client():
    return TestClient(app, raise_server_exceptions=False)


def _seed_tasks() -> None:
    """造 5 条任务：3 成功 / 2 失败，验证筛选与分页。"""
    for i in range(5):
        state = "success" if i < 3 else "failed"
        task = app_module.task_store.create(kind="generate")
        app_module.task_store.update(
            task.task_id,
            state=state,
            error=("x" * (50 + i)) if state == "failed" else "",
        )


def test_e6_task_list_filter_and_pagination(client, fresh_db) -> None:
    # fresh_db 只重置业务库，gen_tasks 可能留有前面测试的任务；
    # 所以断言「本次 seed 带来的增量」，不假设绝对总数（全量/单跑都要过）。
    def total_of(query: str = "") -> int:
        suffix = "&" + query if query else ""
        return client.get(f"/api/v1/tasks?limit=1{suffix}").json()["total"]

    base_all = total_of()
    base_failed = total_of("state=failed")
    base_success = total_of("state=success")
    base_cancelled = total_of("state=cancelled")

    _seed_tasks()

    page1 = client.get("/api/v1/tasks?limit=2&offset=0").json()
    assert page1["total"] == base_all + 5
    assert page1["offset"] == 0
    assert page1["limit"] == 2
    assert len(page1["tasks"]) == 2

    page2 = client.get("/api/v1/tasks?limit=2&offset=2").json()
    assert page2["total"] == base_all + 5
    assert page2["tasks"][0]["task_id"] != page1["tasks"][0]["task_id"]
    assert len(page2["tasks"]) == 2

    failed = client.get("/api/v1/tasks?limit=10&state=failed").json()
    assert failed["total"] == base_failed + 2
    assert {t["state"] for t in failed["tasks"]} == {"failed"}

    success = client.get("/api/v1/tasks?limit=10&state=success").json()
    assert success["total"] == base_success + 3

    assert total_of("state=cancelled") == base_cancelled


def test_e6_invalid_state_is_rejected(client, fresh_db) -> None:
    assert client.get("/api/v1/tasks?state=bogus").status_code == 422


def test_e6_summary_still_lightweight(client, fresh_db) -> None:
    """E-6 新增字段不得破坏 Batch 4 D-1 的轻量列表契约。"""
    task = app_module.task_store.create(kind="generate")
    app_module.task_store.update(
        task.task_id, state="success", result=json.dumps({"big": [1] * 100})
    )
    row = client.get("/api/v1/tasks?limit=5").json()["tasks"][0]
    assert "result" not in row and "request" not in row and "error" not in row
