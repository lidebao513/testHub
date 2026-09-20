"""仓储层测试：对账幂等、编号唯一性防线、追溯体检。

这些是「静默数据损坏」的哨兵：编号碰撞、重复行、孤儿行一旦发生，
必须在这里被拦住或被抓出来，而不是等到下游执行时才发现。
"""

import pytest

from core import store
from core.contracts import CaseSpec, FunctionalPoint, TestPoint
from core.errors import ContractViolation


def _case(tp_id: str, *, title: str = "用例") -> CaseSpec:
    return CaseSpec(
        tc_no=tp_id,
        title=title,
        ctype="api",
        steps=[{"action": "http_probe", "tp_id": tp_id, "expect": "返回 2xx"}],
        module="m",
        case_type="正常",
        priority="P2",
        precondition="服务已启动",
        doc_steps=[{"seq": 1, "type": "前置", "desc": "服务已启动"}],
        tp_id=tp_id,
        fp_contract_id="FP-x",
    )


def test_reconcile_is_idempotent(fresh_db):
    pid = store.upsert_project("store-a", "/tmp/a")
    specs = [_case("TP-1"), _case("TP-2")]

    first = store.reconcile_cases(pid, specs)
    assert first == {"created": 2, "updated": 0, "reused": 0, "obsolete": 0}

    second = store.reconcile_cases(pid, specs)
    assert second == {"created": 0, "updated": 0, "reused": 2, "obsolete": 0}


def test_reconcile_content_change_bumps_version(fresh_db):
    pid = store.upsert_project("store-b", "/tmp/b")
    store.reconcile_cases(pid, [_case("TP-1", title="旧标题")])
    stats = store.reconcile_cases(pid, [_case("TP-1", title="新标题")])
    assert stats["updated"] == 1
    rows = store.list_cases(pid)
    assert rows[0]["title"] == "新标题"
    assert rows[0]["version"] == 2


def test_reconcile_marks_obsolete_and_hides_by_default(fresh_db):
    pid = store.upsert_project("store-c", "/tmp/c")
    store.reconcile_cases(pid, [_case("TP-1"), _case("TP-2")])
    stats = store.reconcile_cases(pid, [_case("TP-1")])
    assert stats["obsolete"] == 1
    assert [r["tp_id"] for r in store.list_cases(pid)] == ["TP-1"]
    assert len(store.list_cases(pid, include_obsolete=True)) == 2


def test_duplicate_tp_ids_are_rejected(fresh_db):
    """重复 tp_id 必须报错，绝不能静默插入两行（会造成永久孤儿行）。"""
    pid = store.upsert_project("store-d", "/tmp/d")
    with pytest.raises(ContractViolation) as exc:
        store.reconcile_cases(pid, [_case("TP-dup"), _case("TP-dup")])
    assert "TP-dup" in exc.value.message
    assert store.list_cases(pid, include_obsolete=True) == []


def test_no_orphan_cases_after_reconcile(fresh_db):
    """对账后不得留下非 obsolete 的孤儿用例。"""
    pid = store.upsert_project("store-e", "/tmp/e")
    store.replace_functional_points(
        pid,
        [
            FunctionalPoint(
                fp_id="FP-x", ftype="api", file_path="a.py", name="GET /a", title="查询 A"
            )
        ],
    )
    store.replace_test_points(
        pid,
        [
            TestPoint(tp_id="TP-1", fp_contract_id="FP-x", category="正常"),
            TestPoint(tp_id="TP-2", fp_contract_id="FP-x", category="正常"),
        ],
    )
    store.reconcile_cases(pid, [_case("TP-1"), _case("TP-2")])
    store.reconcile_cases(pid, [_case("TP-2")])

    health = store.traceability(pid)
    assert health["orphan_case_count"] == 0
    assert health["orphan_tp_count"] == 0
