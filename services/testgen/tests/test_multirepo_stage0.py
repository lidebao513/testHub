"""多仓改造阶段 0 守护：数据模型地基 + ID 契约冻结 + 按仓隔离 + 迁移。

红线：旧单仓行为零变化。每个断言要么验证「缺省 = 旧行为」，要么验证「传 repo 才多仓隔离」。
"""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

from core import db as db_mod
from core import store
from core.contracts import FunctionalPoint, TestPoint, fp_id_of, tp_id_of
from core.enums import FType, TPType


def _load_migrate():
    """按文件路径加载仓库根的迁移脚本（tests 的 sys.path 只含 testgen 根）。"""
    script = Path(__file__).resolve().parents[3] / "scripts" / "migrate_multirepo.py"
    spec = importlib.util.spec_from_file_location("migrate_multirepo", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def conn(fresh_db):
    """复用 conftest 的干净库，给本文件用例一个共享连接（同一连接内可见写入）。"""
    connection = db_mod.connect()
    yield connection
    connection.close()


# ---------------------------------------------------------------------------
# ID 契约：缺省逐字节冻结，传 repo_key 才分叉
# ---------------------------------------------------------------------------
def test_fp_id_repo_key_frozen_when_absent():
    """缺省 repo_key 的输出必须与冻结版本逐字节一致（md5(ftype|path|name)[:8]）。"""
    import hashlib

    args = (FType.API.value, "billing/api.py", "GET /api/v1/invoices")
    legacy = "FP-" + hashlib.md5("|".join(args).encode("utf-8")).hexdigest()[:8]  # nosec B324
    assert fp_id_of(*args) == legacy
    assert fp_id_of(*args, repo_key="") == legacy  # 显式空串等同缺省


def test_fp_id_repo_key_separates_repos():
    args = (FType.API.value, "src/utils.py", "GET /api/health")
    base = fp_id_of(*args)
    backend = fp_id_of(*args, repo_key="backend")
    frontend = fp_id_of(*args, repo_key="frontend")
    assert base != backend != frontend
    assert backend == fp_id_of(*args, repo_key="backend")  # 同仓稳定
    for value in (base, backend, frontend):
        assert value.startswith("FP-") and len(value) == 11


def test_tp_id_inherits_repo_via_fp():
    """tp_id 由 fp_id 派生，fp_id 带 repo 后 tp_id 自动分仓。"""
    common = (TPType.NORMAL.value, "area", "method", "dim", 0)
    a = tp_id_of(fp_id_of(FType.API.value, "x.py", "f", repo_key="a"), *common)
    b = tp_id_of(fp_id_of(FType.API.value, "x.py", "f", repo_key="b"), *common)
    assert a != b


def test_contracts_repo_key_default_empty():
    assert FunctionalPoint("FP-x", FType.API.value, "a.py", "f", "标题").repo_key == ""
    tp = TestPoint("TP-x", "FP-x", TPType.NORMAL.value)
    assert tp.repo_key == ""
    assert "repo_key" in FunctionalPoint("FP-x", FType.API.value, "a.py", "f", "t").to_dict()


# ---------------------------------------------------------------------------
# repo_bindings CRUD
# ---------------------------------------------------------------------------
def test_repo_binding_crud_and_map(conn):
    pid = store.upsert_project("mr-proj", "/tmp/mr", conn=conn)
    a = store.upsert_repo_binding(pid, "backend", local_path="/tmp/be", conn=conn)
    b = store.upsert_repo_binding(pid, "frontend", repo_url="https://x/fe", conn=conn)
    assert b > a
    assert store.repo_binding_map(pid, conn) == {"backend": a, "frontend": b}
    # 幂等 upsert：同 (project, repo_key) 更新而非新增
    a2 = store.upsert_repo_binding(pid, "backend", local_path="/tmp/be2", conn=conn)
    assert a2 == a
    assert store.get_repo_binding(a, conn)["local_path"] == "/tmp/be2"
    assert len(store.list_repo_bindings(pid, conn)) == 2
    # 项目隔离
    pid2 = store.upsert_project("mr-proj-2", "/tmp/mr2", conn=conn)
    store.upsert_repo_binding(pid2, "backend", conn=conn)
    assert store.repo_binding_map(pid2, conn) != store.repo_binding_map(pid, conn)


# ---------------------------------------------------------------------------
# 按仓删除隔离：这是「生成仓 B 不删仓 A」的第一道防线
# ---------------------------------------------------------------------------
def _fp(repo_key: str, name: str) -> FunctionalPoint:
    return FunctionalPoint(
        fp_id=fp_id_of(FType.API.value, "same/path.py", name, repo_key=repo_key),
        ftype=FType.API.value,
        file_path="same/path.py",
        name=name,
        title=name,
        repo_key=repo_key,
    )


def test_replace_fp_scoped_to_repo(conn):
    pid = store.upsert_project("mr-fp", "/tmp/mr-fp", conn=conn)
    be = store.upsert_repo_binding(pid, "backend", conn=conn)
    fe = store.upsert_repo_binding(pid, "frontend", conn=conn)
    store.replace_functional_points(pid, [_fp("backend", "GET /a")], repo_binding_id=be, conn=conn)
    store.replace_functional_points(pid, [_fp("frontend", "GET /b")], repo_binding_id=fe, conn=conn)
    rows = store.list_functional_points(pid, conn)
    assert {r["repo_binding_id"] for r in rows} == {be, fe}
    # 重新生成 backend 不删 frontend
    store.replace_functional_points(pid, [_fp("backend", "GET /c")], repo_binding_id=be, conn=conn)
    rows = store.list_functional_points(pid, conn)
    assert len(rows) == 2
    assert {r["repo_binding_id"] for r in rows} == {be, fe}


def test_replace_fp_legacy_no_binding_deletes_all(conn):
    """缺省 repo_binding_id 走旧逻辑：DELETE 整项目，行为不变。"""
    pid = store.upsert_project("mr-legacy", "/tmp/mr-legacy", conn=conn)
    store.replace_functional_points(pid, [_fp("", "GET /a")], conn=conn)
    assert len(store.list_functional_points(pid, conn)) == 1
    store.replace_functional_points(pid, [_fp("", "GET /b")], conn=conn)
    rows = store.list_functional_points(pid, conn)
    assert len(rows) == 1 and rows[0]["repo_binding_id"] is None


def test_replace_tp_scoped_to_repo(conn):
    pid = store.upsert_project("mr-tp", "/tmp/mr-tp", conn=conn)
    be = store.upsert_repo_binding(pid, "backend", conn=conn)
    fe = store.upsert_repo_binding(pid, "frontend", conn=conn)
    tp_be = TestPoint(tp_id_of("FP-be", "正常"), "FP-be", TPType.NORMAL.value, title="be")
    tp_fe = TestPoint(tp_id_of("FP-fe", "正常"), "FP-fe", TPType.NORMAL.value, title="fe")
    store.replace_test_points(pid, [tp_be], repo_binding_id=be, conn=conn)
    store.replace_test_points(pid, [tp_fe], repo_binding_id=fe, conn=conn)
    assert len(store.list_test_points(pid, conn)) == 2
    store.replace_test_points(
        pid,
        [TestPoint(tp_id_of("FP-be2", "正常"), "FP-be2", TPType.NORMAL.value, title="be2")],
        repo_binding_id=be,
        conn=conn,
    )
    rows = store.list_test_points(pid, conn)
    assert len(rows) == 2 and {r["repo_binding_id"] for r in rows} == {be, fe}


# ---------------------------------------------------------------------------
# 唯一索引按仓隔离：跨仓同编号共存，同仓同编号仍被挡
# ---------------------------------------------------------------------------
def test_unique_index_scoped_by_repo(conn):
    pid = store.upsert_project("mr-uq", "/tmp/mr-uq", conn=conn)
    be = store.upsert_repo_binding(pid, "backend", conn=conn)
    fe = store.upsert_repo_binding(pid, "frontend", conn=conn)
    now = "2026-01-01T00:00:00"
    # 跨仓同 contract_id 可共存
    conn.execute(
        "INSERT INTO functional_points(project_id, contract_id, repo_binding_id, created_at)"
        " VALUES(?,?,?,?)",
        (pid, "FP-SAME", be, now),
    )
    conn.execute(
        "INSERT INTO functional_points(project_id, contract_id, repo_binding_id, created_at)"
        " VALUES(?,?,?,?)",
        (pid, "FP-SAME", fe, now),
    )
    conn.commit()
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM functional_points WHERE project_id=? AND contract_id='FP-SAME'",
            (pid,),
        ).fetchone()[0]
        == 2
    )
    # 同仓同 contract_id 仍被唯一索引挡住
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO functional_points(project_id, contract_id, repo_binding_id, created_at)"
            " VALUES(?,?,?,?)",
            (pid, "FP-SAME", be, now),
        )
        conn.commit()
    conn.rollback()


# ---------------------------------------------------------------------------
# 迁移：旧单仓 project → default binding + 回填 + 旧索引重建
# ---------------------------------------------------------------------------
def test_migrate_backfills_legacy_and_rebuilds_index(tmp_path, monkeypatch):
    """构造一个「改造前」形态的旧库，跑迁移后旧数据零丢失、索引按仓隔离。"""
    run_migrate = _load_migrate().migrate

    legacy_db = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(legacy_db))
    conn.row_factory = sqlite3.Row
    # 用当前 schema 建表建列，但故意把唯一索引退回旧定义（无 repo_binding_id），
    # 模拟「改造前」的库。
    db_mod.init_db(conn)
    conn.execute("DROP INDEX uq_fp_project_contract")
    conn.execute(
        "CREATE UNIQUE INDEX uq_fp_project_contract ON functional_points(project_id, contract_id)"
        " WHERE contract_id IS NOT NULL"
    )
    # 造旧单仓数据
    conn.execute(
        "INSERT INTO projects(name, local_path, type) VALUES('legacy', '/tmp/legacy', 'repo')"
    )
    pid = conn.execute("SELECT id FROM projects").fetchone()[0]
    conn.execute(
        "INSERT INTO functional_points(project_id, contract_id, created_at) VALUES(?,?,?)",
        (pid, "FP-OLD", "2026-01-01T00:00:00"),
    )
    conn.commit()

    stats = run_migrate(conn)
    assert stats["bindings_created"] == 1
    assert stats["rows_backfilled"] == 1
    assert stats["indexes_rebuilt"] == 1
    binding = conn.execute(
        "SELECT id FROM repo_bindings WHERE project_id=? AND repo_key='default'", (pid,)
    ).fetchone()
    assert binding is not None
    row = conn.execute("SELECT repo_binding_id FROM functional_points").fetchone()
    assert row["repo_binding_id"] == binding["id"]
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='uq_fp_project_contract'"
    ).fetchone()[0]
    assert "repo_binding_id" in sql
    conn.close()


def test_migrate_is_idempotent(tmp_path):
    run_migrate = _load_migrate().migrate

    db_file = tmp_path / "idem.db"
    conn = sqlite3.connect(str(db_file))
    conn.row_factory = sqlite3.Row
    db_mod.init_db(conn)
    conn.execute("INSERT INTO projects(name, local_path) VALUES('p', '/tmp/p')")
    conn.commit()
    first = run_migrate(conn)
    second = run_migrate(conn)
    assert first["bindings_created"] == 1
    assert second["bindings_created"] == 0
    assert second["indexes_rebuilt"] == 0
    assert conn.execute("SELECT COUNT(*) FROM repo_bindings").fetchone()[0] == 1
    conn.close()
