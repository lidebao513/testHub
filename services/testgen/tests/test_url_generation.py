"""地址通道生成（轻量取码桥 + 靶向参数）守护测试。

覆盖：桥降级不阻断、target_sources 注入让接口目标可命中、无注入零回归、
端点强制地址/生成-only/复用 project_id，以及 url+incremental+changed_files 端到端。
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from core import store
from engine import change_targeting, pipeline, url_code_bridge
from service.app import app
from service.tasks import GenerationTask


@pytest.fixture()
def client():
    return TestClient(app, raise_server_exceptions=False)


def _fake_task(task_id: str) -> GenerationTask:
    return GenerationTask(
        task_id=task_id,
        kind="url-generation",
        idempotency_key="k",
        request="{}",
        state="pending",
    )


# ---------------------------------------------------------------------------
# 轻量取码桥：失败降级 + 文本供给
# ---------------------------------------------------------------------------
def test_bridge_empty_inputs_never_raises():
    out = url_code_bridge.fetch_changed_sources("", [])
    assert out.sources == {}
    assert out.hunks == {}
    assert any("未提供仓库地址" in n for n in out.notes)


def test_bridge_no_token_degrades_with_note():
    out = url_code_bridge.fetch_changed_sources(
        "https://codeup.aliyun.com/org/repo.git", ["a.py"], code_source={}
    )
    assert out.sources == {}
    assert any("凭证" in n for n in out.notes)


def test_bridge_fetches_text_and_skips_binary(monkeypatch):
    """模拟只读客户端：取到 1 个文本 + 跳过 1 个二进制，均不展开 FP。"""
    import engine.codeup_client as cc
    import engine.remote_scan as rs

    class FakeClient:
        def compare(self, *a, **k):
            return []

        def get_blob(self, rid, path, *, ref=""):
            return "export const url = '/api/v1/tasks'\n" if path.endswith(".ts") else "BIN\x00DATA"

    monkeypatch.setattr(cc, "parse_repo_to_repo_id", lambda url: "org%2Frepo")
    monkeypatch.setattr(rs, "client_from_code_source", lambda cs, **k: FakeClient())
    out = url_code_bridge.fetch_changed_sources(
        "https://codeup.aliyun.com/org/repo.git",
        ["src/api.ts", "img.bin"],
        code_source={"access_key": "pat", "org_id": "org"},
    )
    assert out.fetched == 1
    assert out.skipped == 1
    assert "/api/v1/tasks" in out.sources["src/api.ts"]
    assert "img.bin" not in out.sources
    assert any("未展开任何代码功能点" in n for n in out.notes)


# ---------------------------------------------------------------------------
# target_sources 注入：接口目标可命中（纯地址下的关键缺口）
# ---------------------------------------------------------------------------
def test_target_plan_without_sources_misses_api_but_keeps_page():
    """无文本：页面靠文件名推断保留，接口一条都映射不到（回归现状缺口）。"""
    plan = change_targeting.build_target_plan(
        changed_files=["frontend/app/src/pages/pc/PcTasksPage.tsx"],
        sources={},
        api_endpoints=[("GET", "/api/v1/tasks")],
        page_paths=["/pc/tasks"],
    )
    assert "/pc/tasks" in plan.page_paths
    assert "/api/v1/tasks" not in plan.api_paths


def test_target_plan_with_bridge_sources_hits_api():
    """有桥文本：接口字面量命中，页面仍保留。"""
    plan = change_targeting.build_target_plan(
        changed_files=["frontend/app/src/api/backend.ts"],
        sources={"frontend/app/src/api/backend.ts": "apiJson('/api/v1/tasks')"},
        api_endpoints=[("GET", "/api/v1/tasks")],
        page_paths=["/pc/tasks"],
    )
    assert "/api/v1/tasks" in plan.api_paths


def test_build_target_plan_prefers_opts_sources(monkeypatch):
    """_build_target_plan 优先用 opts.target_sources（纯地址 result.files 为空时生效）。"""
    seen = {}
    orig_build = change_targeting.build_target_plan

    def fake_build(**kwargs):
        seen.update(kwargs)
        return orig_build(**kwargs)

    monkeypatch.setattr(change_targeting, "build_target_plan", fake_build)
    opts = pipeline.PipelineOptions(
        changed_files=["x.ts"],
        target_sources={"x.ts": "'/api/x'"},
        target_hunks={"x.ts": [(1, 2)]},
    )
    result = pipeline.PipelineResult(mode="incremental")
    result.files = {"real.py": object()}  # 有代码源也不该盖过桥文本
    pipeline._build_target_plan(opts, result)
    assert seen["sources"] == {"x.ts": "'/api/x'"}
    assert seen["hunks"] == {"x.ts": [(1, 2)]}


def test_default_options_have_empty_bridge_fields():
    """默认空 → 旧链路零行为变化。"""
    opts = pipeline.PipelineOptions()
    assert opts.target_sources == {}
    assert opts.target_hunks == {}


# ---------------------------------------------------------------------------
# 端点契约
# ---------------------------------------------------------------------------
def test_url_generation_requires_base_url(client):
    r = client.post("/api/v1/url-generation/generate", json={"test_url": ""})
    assert r.status_code in (400, 422)  # ValidationError 映射为 422


def test_url_generation_forces_targeted_incremental_and_reuses_project(client, fresh_db):
    """提交端点：有变更文件→incremental、targeted、复用 project_id、生成-only。"""
    init = store.upsert_project("urlgen-ep", "https://urlgen.example.com/pc")
    captured = {}

    def fake_submit(task_id, fn):
        captured["task_id"] = task_id
        captured["fn"] = fn

    with patch("service.app.task_store") as ts, patch("service.app.executor") as ex:
        ts.find_by_idempotency.return_value = None
        ts.create.return_value = _fake_task("t-urlgen-1")
        ex.submit.side_effect = fake_submit
        r = client.post(
            "/api/v1/url-generation/generate",
            json={
                "test_url": "https://urlgen.example.com/pc",
                "changed_files": ["src/api.ts"],
                "project_id": init,
                "runtime_scope": "targeted",
                "execute": True,  # 请求里即使给 execute 也不该生效
            },
        )
    assert r.status_code == 202
    assert r.json()["task_id"] == "t-urlgen-1"
    assert ex.submit.called


def test_url_generation_full_mode_no_changes(client, fresh_db):
    """无变更文件 → full 全站巡检仍可提交（③ 与 ① 两种做法并存）。"""
    with patch("service.app.task_store") as ts, patch("service.app.executor"):
        ts.find_by_idempotency.return_value = None
        ts.create.return_value = _fake_task("t-urlgen-full")
        r = client.post(
            "/api/v1/url-generation/generate",
            json={"test_url": "https://full.example.com/pc", "runtime_scope": "full"},
        )
    assert r.status_code == 202


# ---------------------------------------------------------------------------
# 端到端：url + incremental + changed_files（方案阶段 D 缺口）
# ---------------------------------------------------------------------------
def test_url_plus_incremental_changed_files_end_to_end(fresh_db, monkeypatch):
    """有地址 + 变更文件 + 桥文本：端到端产出用例，targeted 计数如实落 counts。"""
    # 用桩替换真浏览器发现（本测试验证编排与靶向，不依赖 Playwright）。
    from engine import runtime_ui

    def fake_discover(options, **kwargs):
        return runtime_ui.RuntimeUiResult(
            pages=[
                runtime_ui.UiPage(
                    url="https://e2e.example.com/pc/tasks", path="/pc/tasks", title="任务页"
                ),
                runtime_ui.UiPage(
                    url="https://e2e.example.com/pc/other", path="/pc/other", title="无关页"
                ),
            ],
            elements=[],
            discovered_routes=["/pc/tasks", "/pc/other"],
            api_endpoints=[
                runtime_ui.ApiEndpoint(method="GET", path="/api/v1/tasks"),
                runtime_ui.ApiEndpoint(method="GET", path="/api/v1/other"),
            ],
        )

    monkeypatch.setattr(runtime_ui, "discover_ui", fake_discover)
    opts = pipeline.PipelineOptions(
        mode="incremental",
        changed_files=["src/api/backend.ts"],
        target_runtime=True,
        target_sources={"src/api/backend.ts": "apiJson('/api/v1/tasks')"},
        persist=False,
    )
    opts.target_req.enabled = True
    opts.target_req.base_url = "https://e2e.example.com/pc"
    result = pipeline.run_pipeline(opts)
    # 靶向计数必须存在且如实：/pc/tasks + /api/v1/tasks 保留，/pc/other + /api/v1/other 剔除。
    assert "runtime_fp_targeted" in result.counts
    assert result.counts["runtime_fp_targeted"] >= 1
    assert result.counts.get("runtime_fp_skipped_by_target", 0) >= 1
    assert result.counts["cases"] > 0
