"""Batch 6 D-4：FP/TP 增量 upsert 守护测试。

核心保障：
1. 内容未变的重复生成保留 FP/TP 行 id（cases.fp_id 不 churn）；
2. 内容变化的行原地 UPDATE、id 不变；
3. 本次集合之外的行删除；
4. 返回统计兼容旧字段；多仓按 binding 隔离。
"""

from __future__ import annotations

from core import store
from core.contracts import FunctionalPoint, TestPoint


def _fp(fid: str, *, title: str = "") -> FunctionalPoint:
    return FunctionalPoint(
        fp_id=fid,
        ftype="api",
        file_path="app.py",
        name=fid,
        title=title or fid,
    )


def _tp(tid: str, fid: str, *, expect: str = "") -> TestPoint:
    return TestPoint(
        tp_id=tid,
        fp_contract_id=fid,
        category="正常",
        module="m",
        expect=expect,
        evidence=["app.py:1"],
        origin="rule",
    )


def test_fp_reuse_keeps_row_id(fresh_db) -> None:
    pid = store.upsert_project("b6-fp-reuse", "")
    first = store.replace_functional_points(pid, [_fp("fp-1"), _fp("fp-2")])
    assert first["functional_points"] == 2 and first["created"] == 2
    rows1 = {r["contract_id"]: r["id"] for r in store.list_functional_points(pid)}

    second = store.replace_functional_points(pid, [_fp("fp-1"), _fp("fp-2")])
    assert second["reused"] == 2 and second["created"] == 0 and second["updated"] == 0
    rows2 = {r["contract_id"]: r["id"] for r in store.list_functional_points(pid)}
    assert rows1 == rows2, "内容未变却换了行 id → cases.fp_id 会被 churn"


def test_fp_content_change_updates_in_place(fresh_db) -> None:
    pid = store.upsert_project("b6-fp-update", "")
    store.replace_functional_points(pid, [_fp("fp-1")])
    old_id = store.list_functional_points(pid)[0]["id"]

    stats = store.replace_functional_points(pid, [_fp("fp-1", title="新标题")])
    assert stats["updated"] == 1 and stats["created"] == 0
    row = store.list_functional_points(pid)[0]
    assert row["id"] == old_id and row["title"] == "新标题"


def test_fp_stale_removed(fresh_db) -> None:
    pid = store.upsert_project("b6-fp-stale", "")
    store.replace_functional_points(pid, [_fp("fp-1"), _fp("fp-2")])
    stats = store.replace_functional_points(pid, [_fp("fp-1")])
    assert stats["removed"] == 1
    assert [r["contract_id"] for r in store.list_functional_points(pid)] == ["fp-1"]


def test_tp_reuse_keeps_row_id(fresh_db) -> None:
    pid = store.upsert_project("b6-tp-reuse", "")
    first = store.replace_test_points(pid, [_tp("tp-1", "fp-1"), _tp("tp-2", "fp-1")])
    assert first["test_points"] == 2 and first["created"] == 2
    rows1 = {r["tp_id"]: r["id"] for r in store.list_test_points(pid)}

    second = store.replace_test_points(pid, [_tp("tp-1", "fp-1"), _tp("tp-2", "fp-1")])
    assert second["reused"] == 2 and second["created"] == 0 and second["updated"] == 0
    rows2 = {r["tp_id"]: r["id"] for r in store.list_test_points(pid)}
    assert rows1 == rows2, "测试点内容未变却换了行 id"


def test_tp_content_change_updates_in_place(fresh_db) -> None:
    pid = store.upsert_project("b6-tp-update", "")
    store.replace_test_points(pid, [_tp("tp-1", "fp-1")])
    old_id = store.list_test_points(pid)[0]["id"]

    stats = store.replace_test_points(pid, [_tp("tp-1", "fp-1", expect="新预期")])
    assert stats["updated"] == 1 and stats["created"] == 0
    row = store.list_test_points(pid)[0]
    assert row["id"] == old_id and row["expect"] == "新预期"


def test_tp_evidence_and_flags_persisted(fresh_db) -> None:
    """扩展列（evidence/confidence/origin/unverified 等）必须随 INSERT/UPDATE 正确落库。"""
    pid = store.upsert_project("b6-tp-flags", "")
    tp = TestPoint(
        tp_id="tp-x",
        fp_contract_id="fp-1",
        category="安全",
        module="m",
        title="t",
        evidence=["a.py:1", "b.py:2"],
        confidence=0.75,
        origin="semantic",
        unverified=True,
        channel="impact",
        unverified_reason="best_effort",
        verify_layer="static",
    )
    store.replace_test_points(pid, [tp])
    row = store.list_test_points(pid)[0]
    assert row["evidence"] == ["a.py:1", "b.py:2"]
    assert row["confidence"] == 0.75 and row["origin"] == "semantic"
    assert row["unverified"] == 1 and row["channel"] == "impact"
    assert row["unverified_reason"] == "best_effort" and row["verify_layer"] == "static"

    # 二次相同 → reused（比较逻辑要能识别所有扩展列未变）
    stats = store.replace_test_points(pid, [tp])
    assert stats["reused"] == 1, f"扩展列比较有误：{stats}"


def test_multirepo_scope_isolated(fresh_db) -> None:
    """多仓：生成仓 B 不删仓 A 的 FP/TP。"""
    pid = store.upsert_project("b6-multi", "")
    be = store.upsert_repo_binding(pid, "backend")
    fe = store.upsert_repo_binding(pid, "frontend")
    with store.connect() as conn:
        store.replace_functional_points(pid, [_fp("fp-be")], repo_binding_id=be, conn=conn)
        store.replace_functional_points(pid, [_fp("fp-fe")], repo_binding_id=fe, conn=conn)
        store.replace_test_points(pid, [_tp("tp-be", "fp-be")], repo_binding_id=be, conn=conn)
        store.replace_test_points(pid, [_tp("tp-fe", "fp-fe")], repo_binding_id=fe, conn=conn)

    # 只重跑 frontend，且换成新 fp：backend 行必须原样保留
    with store.connect() as conn:
        store.replace_functional_points(pid, [_fp("fp-fe2")], repo_binding_id=fe, conn=conn)
        store.replace_test_points(pid, [_tp("tp-fe2", "fp-fe2")], repo_binding_id=fe, conn=conn)

    with store.connect() as conn:
        fps = {r["contract_id"] for r in store.list_functional_points(pid, conn=conn)}
        tps = {r["tp_id"] for r in store.list_test_points(pid, conn=conn)}
    assert fps == {"fp-be", "fp-fe2"} and tps == {"tp-be", "tp-fe2"}
