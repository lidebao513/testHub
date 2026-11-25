"""#439 上帝文件拆分守护测试。

拆分 app.py 为 service/routers/ 多模块后，锁定两条契约：
1. 所有原 HTTP 路由仍注册（路径 + 方法），拆分不漏路由；
2. 被测试 patch 的共享符号仍在 service.app 门面命名空间，且 router 内部通过
   门面引用（patch 能生效）——防止未来清理 import 时误删导致 patch 静默失效。
"""

from __future__ import annotations

import service.app as app_module
from service.app import app


# 拆分前 app.py 的业务路由（不含 /docs /openapi.json /redoc 等框架路由）
_EXPECTED_API_ROUTES = {
    ("GET", "/"),
    ("GET", "/health"),
    ("GET", "/ready"),
    ("GET", "/api/v1/dialogue/template"),
    ("POST", "/api/v1/dialogue/submit"),
    ("POST", "/api/v1/execute"),
    ("POST", "/api/v1/generate"),
    ("POST", "/api/v1/pipeline"),
    ("POST", "/api/v1/url-generation/generate"),
    ("GET", "/api/v1/tasks"),
    ("GET", "/api/v1/tasks/{task_id}"),
    ("POST", "/api/v1/tasks/{task_id}/cancel"),
    ("GET", "/api/v1/tasks/{task_id}/cases"),
    ("POST", "/api/v1/verify/webhook"),
    ("GET", "/api/v1/metrics"),
    ("POST", "/api/v1/parse-input"),
    ("POST", "/api/v1/compare"),
    ("POST", "/api/v1/pull"),
    ("POST", "/api/v1/analyze"),
    ("POST", "/api/v1/stage-analyze"),
    ("POST", "/api/v1/stage-compare"),
    ("GET", "/api/v1/stages"),
    ("GET", "/api/v1/projects"),
    ("POST", "/api/v1/projects/ensure"),
    ("GET", "/api/v1/projects/{pid}/modules"),
    ("POST", "/api/v1/projects/{pid}/modules/scan"),
    ("GET", "/api/v1/projects/{pid}/repos"),
    ("GET", "/api/v1/projects/{pid}/test-points"),
    ("GET", "/api/v1/projects/{pid}/cases"),
    ("GET", "/api/v1/projects/{pid}/traceability"),
    ("GET", "/api/v1/projects/{pid}/runs"),
    ("GET", "/api/v1/projects/{pid}/runs/{batch_id}"),
    ("GET", "/api/v1/projects/{pid}/report"),
    ("GET", "/api/v1/projects/{pid}/coverage"),
    ("GET", "/api/v1/workspaces"),
    ("GET", "/api/v1/projects/{pid}/reports/trend"),
    ("GET", "/api/v1/projects/{pid}/reports/flaky"),
    ("GET", "/api/v1/projects/{pid}/change_logs"),
    ("GET", "/api/v1/projects/{pid}/functional-points"),
    ("GET", "/api/v1/projects/{pid}/filter-options"),
    ("POST", "/api/v1/projects/{pid}/import-test-points"),
    ("POST", "/api/v1/projects/{pid}/import-test-points/upload"),
    ("POST", "/api/v1/projects/{pid}/rerun-flaky"),
    ("POST", "/api/v1/branches"),
    ("POST", "/api/v1/commits"),
    ("POST", "/api/v1/describe"),
    ("POST", "/api/v1/describe/cache/clear"),
    ("POST", "/api/v1/scan"),
    ("POST", "/api/v1/repos/refs"),
    ("POST", "/api/v1/verdict"),
    ("GET", "/api/v1/repos"),
    ("GET", "/api/v1/repos/export"),
    ("POST", "/api/v1/repos/import"),
    ("POST", "/api/v1/repos"),
    ("PUT", "/api/v1/repos/{rid}"),
    ("DELETE", "/api/v1/repos/{rid}"),
    ("POST", "/api/v1/repos/test"),
    ("POST", "/api/v1/repos/{rid}/test"),
    ("GET", "/api/v1/llm/providers"),
    ("POST", "/api/v1/llm/providers"),
    ("PUT", "/api/v1/llm/providers/{pid}"),
    ("DELETE", "/api/v1/llm/providers/{pid}"),
    ("POST", "/api/v1/llm/providers/test"),
    ("POST", "/api/v1/llm/providers/{pid}/test"),
}

# 被现有测试 patch / import 的共享符号（拆分后必须仍在 app 门面）
_FACADE_SYMBOLS = {
    "app",
    "task_store",
    "executor",
    "settings",
    "chat_with_fallback",
    "compare_one",
    "compare_batch",
    "ComparatorOptions",
    "OutputWriter",
    "_stage_store",
    "_task_accepted",
    "_run_generation_job",
    "_idem_key_for_generate",
    "_idem_key_for_execute",
    "_engine_rev",
    "_stage_with_detail",
    "_DESCRIBE_DB_LOCK",
    "list_repo_branches",
    "list_repo_commits",
    "_resolve_code_source",
    "scan_git_repo",
    "scan_repository",
    "PipelineRequest",
    "ExecuteRequest",
    "RepoSpecRequest",
    "require_auth",
}


def _collect_api_routes() -> set[tuple[str, str]]:
    """递归收集所有路由（含 starlette 新版 _IncludedRouter 的 original_router）。"""
    out: set[tuple[str, str]] = set()

    def walk(routes: list) -> None:
        for route in routes:
            if type(route).__name__ == "_IncludedRouter":
                walk(route.original_router.routes)
            elif hasattr(route, "methods") and hasattr(route, "path"):
                for method in route.methods:
                    if method in {"GET", "POST", "PUT", "DELETE", "PATCH"}:
                        out.add((method, route.path))

    walk(app.routes)
    return out


def test_all_original_routes_still_registered() -> None:
    """拆分后所有原业务路由仍在（多 router include 不漏路由）。"""
    actual = _collect_api_routes()
    missing = _EXPECTED_API_ROUTES - actual
    assert not missing, f"拆分后缺失路由: {sorted(missing)}"
    # 允许框架新增路由，但不允许业务路由消失


def test_facade_symbols_still_patchable() -> None:
    """被测试 patch/import 的符号仍在 app 门面（否则 patch 会 AttributeError）。"""
    missing = sorted(s for s in _FACADE_SYMBOLS if not hasattr(app_module, s))
    assert not missing, f"门面符号缺失（会导致 service.app.X patch 失效）: {missing}"


def test_router_modules_are_separate_from_app() -> None:
    """路由确实拆到了独立模块（防止有人把 handler 又粘回 app.py）。"""
    from service.routers import analysis, config, projects, scan, tasks

    for mod in (analysis, config, projects, scan, tasks):
        assert hasattr(mod, "router"), f"{mod.__name__} 未定义 router"
        assert mod is not app_module
