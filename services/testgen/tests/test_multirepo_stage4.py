"""多仓改造阶段 4 守护：报告按仓分组 + 旧单仓报告零变化。"""

from __future__ import annotations

import pytest

from core import store
from engine import report as report_engine
from output import report_writer


@pytest.fixture()
def db(fresh_db):
    conn = store.connect()
    yield conn
    conn.close()


def _seed_multirepo(pid: int, conn):
    be = store.upsert_repo_binding(pid, "backend", conn=conn)
    fe = store.upsert_repo_binding(pid, "frontend", conn=conn)
    conn.execute(
        "INSERT INTO functional_points(project_id, contract_id, repo_binding_id, created_at)"
        " VALUES(?,?,?,?)",
        (pid, "FP-be", be, "2026-01-01T00:00:00"),
    )
    conn.execute(
        "INSERT INTO functional_points(project_id, contract_id, repo_binding_id, created_at)"
        " VALUES(?,?,?,?)",
        (pid, "FP-fe", fe, "2026-01-01T00:00:00"),
    )
    conn.execute(
        "INSERT INTO test_points(project_id, tp_id, repo_binding_id, created_at) VALUES(?,?,?,?)",
        (pid, "TP-be", be, "2026-01-01T00:00:00"),
    )
    conn.execute(
        "INSERT INTO cases(project_id, tp_id, repo_binding_id, created_at) VALUES(?,?,?,?)",
        (pid, "TP-be", be, "2026-01-01T00:00:00"),
    )
    conn.commit()
    return be, fe


def test_report_repo_summary_groups_by_repo_key(db):
    pid = store.upsert_project("mr-report", "/tmp/mr", conn=db)
    _seed_multirepo(pid, db)
    snap = store.report_snapshot(pid, conn=db)
    summary = report_engine.repo_summary(pid, snap)
    assert summary["single_repo"] is False
    by_key = {r["repo_key"]: r for r in summary["repos"]}
    assert set(by_key) == {"backend", "frontend"}
    assert by_key["backend"]["functional_points"] == 1
    assert by_key["frontend"]["functional_points"] == 1
    assert by_key["backend"]["test_points"] == 1
    assert by_key["backend"]["cases"] == 1


def test_report_snapshot_rows_carry_repo_binding_id(db):
    pid = store.upsert_project("mr-snap", "/tmp/mr", conn=db)
    _seed_multirepo(pid, db)
    snap = store.report_snapshot(pid, conn=db)
    assert "repo_binding_id" in snap["functional_points"][0]
    assert "repo_binding_id" in snap["test_points"][0]
    assert "repo_binding_id" in snap["cases"][0]


def test_build_report_includes_repo_summary(db):
    pid = store.upsert_project("mr-build", "/tmp/mr", conn=db)
    _seed_multirepo(pid, db)
    built = report_engine.build_report(pid)
    assert "repo_summary" in built
    assert built["repo_summary"]["single_repo"] is False
    assert {r["repo_key"] for r in built["repo_summary"]["repos"]} == {"backend", "frontend"}


def test_markdown_has_repo_section_only_for_multirepo(db):
    pid = store.upsert_project("mr-md", "/tmp/mr", conn=db)
    _seed_multirepo(pid, db)
    multi = report_engine.build_report(pid)
    md = report_writer.render_markdown(multi)
    assert "## 十、仓库分组" in md
    assert "backend" in md and "frontend" in md

    # 旧单仓报告：无仓库分组章节（字节级兼容）
    single = report_engine.build_report(pid)
    single["repo_summary"] = {"single_repo": True, "repos": []}
    md_single = report_writer.render_markdown(single)
    assert "## 十、仓库分组" not in md_single


def test_frontend_buildbody_has_repos_support():
    """前端 buildBody 必须包含 repos 组装 + 多仓注入点（静态守护，防止回退）。"""
    html = (
        __import__("pathlib").Path(__file__).resolve().parents[1] / "service" / "web" / "index.html"
    ).read_text(encoding="utf-8")
    assert "window.MULTI_REPO_SPECS" in html
    assert "function setMultiRepos" in html
    assert "b.repos" in html
    assert "descRepoUrl = (Array.isArray(window.MULTI_REPO_SPECS)" in html
