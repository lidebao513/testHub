"""地址优先入口：模块读取/扫描复用既有表，不新建模块实体。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core import store, url_modules
from core.db import init_db
from core.errors import NotFoundError, ValidationError
from core.url_projects import ensure_project


def test_list_file_descriptions_prefix_only_matches_runtime_modules() -> None:
    ns = "http://modules-prefix.example.com/pc"
    with store.connect() as conn:
        init_db(conn)
    store.upsert_file_description(ns, "src/views/tasks.py", "s1", "代码文件", "")
    store.upsert_file_description(ns, "runtime:/pc/tasks", "s2", "任务页", "runtime-scan")
    store.upsert_file_description(ns, "runtime:/pc/logs", "s3", "日志页", "runtime-scan")

    assert len(store.list_file_descriptions(ns)) == 3
    runtime = store.list_file_descriptions(ns, prefix="runtime:")
    assert [r["file_path"] for r in runtime] == ["runtime:/pc/logs", "runtime:/pc/tasks"]


def test_read_modules_merges_scanned_pages_and_existing_functional_points() -> None:
    created = ensure_project(
        name="modules-read.example.com", base_url="http://modules-read.example.com/pc"
    )
    pid = created["project_id"]
    ns = store.get_project(pid)["base_url"]

    store.upsert_file_description(
        ns, "runtime:/pc/tasks", "sig-tasks", "任务页｜3 个交互元素", "runtime-scan"
    )
    with store.connect() as conn:
        conn.execute(
            "INSERT INTO functional_points(project_id, name, ftype, module, contract_id)"
            " VALUES(?,?,?,?,?)",
            (pid, "/pc/tasks", "PAGE", "任务管理", "fp-modules-read-1"),
        )
        conn.execute(
            "INSERT INTO functional_points(project_id, name, ftype, module, contract_id)"
            " VALUES(?,?,?,?,?)",
            (pid, "/pc/logs", "PAGE", "日志", "fp-modules-read-2"),
        )

    result = url_modules.read_modules(pid)
    by_name = {m["name"]: m for m in result["modules"]}
    assert result["normalized_url"] == "http://modules-read.example.com/pc"
    assert by_name["/pc/tasks"]["source"] == "both"
    assert by_name["/pc/tasks"]["description"] == "任务页｜3 个交互元素"
    assert by_name["/pc/tasks"]["fp_count"] == 1
    assert by_name["/pc/logs"]["source"] == "existing"
    assert result["scanned_count"] == 1

    # 没有新建"模块"实体表
    with store.connect() as conn:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
    assert "modules" not in tables
    assert "url_modules" not in tables


def test_read_modules_rejects_unknown_project() -> None:
    with pytest.raises(NotFoundError):
        url_modules.read_modules(99999999)


def test_scan_modules_persists_pages_idempotently_without_generating_cases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = ensure_project(
        name="modules-scan.example.com", base_url="http://modules-scan.example.com/pc"
    )
    pid = created["project_id"]
    ns = store.get_project(pid)["base_url"]

    page = SimpleNamespace(
        path="/pc/tasks",
        url="http://modules-scan.example.com/pc/tasks",
        title="任务页",
        elements=[SimpleNamespace(kind="button", selector="button.primary", text="新建任务")],
    )
    discovered = SimpleNamespace(
        reachable_pages=lambda: [page],
        discovered_routes=["/pc/tasks", "/pc/logs"],
        elements=list(page.elements),
        api_endpoints=[],
        notes=[],
        logged_in=True,
    )
    monkeypatch.setattr(url_modules.runtime_ui, "discover_ui", lambda opts: discovered)

    first = url_modules.scan_modules(pid, max_pages=5, timeout=3)
    assert first["scan"]["persisted"] == 2
    assert first["scan"]["pages"] == 1
    assert first["scan"]["logged_in"] is True
    rows = store.list_file_descriptions(ns, prefix="runtime:")
    assert {r["file_path"] for r in rows} == {"runtime:/pc/logs", "runtime:/pc/tasks"}
    tasks = next(r for r in rows if r["file_path"] == "runtime:/pc/tasks")
    assert "任务页" in tasks["description"]
    assert len(tasks["symbols_sig"]) == 40

    # 同页面二次扫描幂等：不新增记录、指纹不变
    second = url_modules.scan_modules(pid, max_pages=5, timeout=3)
    assert second["scan"]["persisted"] == 2
    rows_again = store.list_file_descriptions(ns, prefix="runtime:")
    assert len(rows_again) == 2
    assert {r["symbols_sig"] for r in rows_again} == {r["symbols_sig"] for r in rows}


def test_scan_modules_requires_project_base_url() -> None:
    pid = store.upsert_project("modules-no-url.example.com", "", base_url="")
    with pytest.raises(ValidationError):
        url_modules.scan_modules(pid)
