"""F12 · 执行留痕落库（`runs` 逐条 / `run_batches` 逐批 / `cases.last_result` 回填）。

验收口径（任务清单 §9.3）：
- `cases.last_result` 能查到执行结论；
- `runs` 有批次记录（批次号 / 结论 / 时间）。

覆盖点：
- 三件事在同一事务内完成：逐条 runs + 回填 last_result + upsert 批次；
- 生命周期与执行结论**解耦**：只写 `last_result`，绝不改 `cases.status`；
- 无批次号的汇总**不写**（宁可不写，不留孤儿）；同批次重跑幂等（先清后写）；
- 执行基础设施失败也要留痕（批次 `failed`，0 条 runs）；
- 表列名与 legacy 对齐（保证与旧库对账）；
- 全链路：pipeline 执行 → 落库；HTTP/CLI 查询入口可用。
"""

from __future__ import annotations

from core import store
from core.contracts import CaseSpec, FunctionalPoint, TestPoint
from core.db import connect, table_columns
from core.enums import ExecStatus, RunBatchState, TPType
from engine import executor, pipeline


class _FakeResponse:
    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code
        self.text = "{}"


class _FakeSession:
    """假会话：按固定状态码应答，**绝不真发网络请求**（判定逻辑才是被测对象）。"""

    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code
        self.calls: list[dict[str, object]] = []

    def request(self, method: str, url: str, **kwargs: object) -> _FakeResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        return _FakeResponse(self.status_code)

    def close(self) -> None:
        pass


def _case_spec(tp_id: str) -> CaseSpec:
    return CaseSpec(
        tc_no=tp_id,
        title=f"用例 {tp_id}",
        ctype="api",
        steps=[{"action": "http_probe", "tp_id": tp_id, "expect": "返回 2xx"}],
        module="m",
        case_type=TPType.NORMAL.value,
        priority="P2",
        precondition="服务已启动",
        doc_steps=[{"seq": 1, "type": "前置", "desc": "服务已启动"}],
        tp_id=tp_id,
        fp_contract_id="FP-1",
    )


def _seed(pid: int, tp_ids: tuple[str, ...]) -> None:
    """铺好功能点 / 测试点 / 用例三层，让 `cases` 行就位。"""
    store.replace_functional_points(
        pid,
        [
            FunctionalPoint(
                fp_id="FP-1", ftype="api", file_path="a.py", name="GET /a", title="查询 A"
            )
        ],
    )
    store.replace_test_points(
        pid,
        [TestPoint(tp_id=tp, fp_contract_id="FP-1", category="正常") for tp in tp_ids],
    )
    store.reconcile_cases(pid, [_case_spec(tp) for tp in tp_ids])


def _summary(
    batch_id: str = "RUN-test-1",
    statuses: tuple[str, ...] = (
        ExecStatus.PASS.value,
        ExecStatus.FAIL.value,
        ExecStatus.SKIPPED.value,
    ),
) -> dict[str, object]:
    return {
        "batch_id": batch_id,
        "state": RunBatchState.COMPLETED.value,
        "started_at": "2026-09-14T17:00:00",
        "finished_at": "2026-09-14T17:00:03",
        "total": len(statuses),
        "executed": sum(1 for s in statuses if s != ExecStatus.SKIPPED.value),
        "pass": statuses.count(ExecStatus.PASS.value),
        "fail": statuses.count(ExecStatus.FAIL.value),
        "error": statuses.count(ExecStatus.ERROR.value),
        "skipped": statuses.count(ExecStatus.SKIPPED.value),
        "results": [
            {"tc_no": f"TP-{i}", "status": s, "notes": [f"结论 {s}"], "duration_ms": 5 * i}
            for i, s in enumerate(statuses)
        ],
    }


# ---------------------------------------------------------------- 仓储层
def test_record_execution_writes_runs_and_backfills_last_result(fresh_db):
    pid = store.upsert_project("f12-a", "/tmp/f12a")
    _seed(pid, ("TP-0", "TP-1", "TP-2"))

    stats = store.record_execution(pid, _summary())
    assert stats == {"runs": 3, "cases": 3, "batches": 1}

    # 验收：cases.last_result 能查到执行结论
    assert {c["tp_id"]: c["last_result"] for c in store.list_cases(pid)} == {
        "TP-0": ExecStatus.PASS.value,
        "TP-1": ExecStatus.FAIL.value,
        "TP-2": ExecStatus.SKIPPED.value,
    }

    # 验收：runs 有批次记录（批次号 / 结论 / 时间）
    rows = store.list_runs(pid)
    assert [r["status"] for r in rows] == ["pass", "fail", "skipped"]
    assert all(r["batch_id"] == "RUN-test-1" for r in rows)
    assert all(r["case_id"] is not None for r in rows)
    assert [r["duration_ms"] for r in rows] == [0, 5, 10]
    assert rows[0]["detail"] == "结论 pass"
    assert all(r["created_at"] for r in rows)


def test_execution_result_does_not_touch_case_lifecycle_status(fresh_db):
    """只写 `last_result`，绝不改 `cases.status`（生命周期与执行结论解耦）。"""
    pid = store.upsert_project("f12-b", "/tmp/f12b")
    _seed(pid, ("TP-0",))
    before = {c["id"]: c["status"] for c in store.list_cases(pid)}

    store.record_execution(pid, _summary())

    after = {c["id"]: c["status"] for c in store.list_cases(pid)}
    assert before == after
    assert all(s == "generated" for s in after.values())


def test_run_batch_records_state_and_status_counts(fresh_db):
    pid = store.upsert_project("f12-c", "/tmp/f12c")
    _seed(pid, ("TP-0", "TP-1", "TP-2"))
    store.record_execution(pid, _summary("RUN-b1"))

    batch = store.latest_run_batch(pid)
    assert batch is not None
    assert batch["batch_id"] == "RUN-b1"
    assert batch["state"] == RunBatchState.COMPLETED.value
    assert (batch["total"], batch["done"]) == (3, 3)
    assert batch["status_counts"] == {"pass": 1, "fail": 1, "skipped": 1}
    assert batch["started_at"] == "2026-09-14T17:00:00"
    assert batch["finished_at"] == "2026-09-14T17:00:03"
    assert store.get_run_batch("RUN-b1") == batch
    assert store.get_run_batch("RUN-nope") is None


def test_summary_without_batch_id_is_not_written(fresh_db):
    """没有批次号 = 本次没有执行 → 什么都不写（不留无批次号的孤儿行）。"""
    pid = store.upsert_project("f12-d", "/tmp/f12d")
    _seed(pid, ("TP-0",))
    stats = store.record_execution(pid, {"results": [{"tc_no": "TP-0", "status": "pass"}]})
    assert stats == {"runs": 0, "cases": 0, "batches": 0}
    assert store.list_runs(pid) == []
    assert store.list_run_batches(pid) == []
    assert store.list_cases(pid)[0]["last_result"] is None


def test_recording_same_batch_is_idempotent(fresh_db):
    pid = store.upsert_project("f12-e", "/tmp/f12e")
    _seed(pid, ("TP-0", "TP-1", "TP-2"))
    store.record_execution(pid, _summary("RUN-b1"))
    store.record_execution(pid, _summary("RUN-b1"))
    assert len(store.list_runs(pid, batch_id="RUN-b1")) == 3, "同批次重跑不得累积重复行"
    assert len(store.list_run_batches(pid)) == 1


def test_failed_batch_is_recorded_without_run_rows(fresh_db):
    """执行基础设施失败（如未装 requests）：批次必须可见，但没有任何单条结论。"""
    pid = store.upsert_project("f12-f", "/tmp/f12f")
    _seed(pid, ("TP-0",))
    summary = {
        "batch_id": "RUN-err-1",
        "state": RunBatchState.FAILED.value,
        "batch_error": "执行接口层用例需要 requests",
        "results": [],
        "total": 1,
        "executed": 0,
        "pass": 0,
        "fail": 0,
        "error": 0,
        "skipped": 0,
        "started_at": "2026-09-14T17:00:00",
        "finished_at": "2026-09-14T17:00:01",
    }
    stats = store.record_execution(pid, summary)
    assert stats == {"runs": 0, "cases": 0, "batches": 1}

    batch = store.get_run_batch("RUN-err-1")
    assert batch is not None
    assert batch["state"] == RunBatchState.FAILED.value
    assert batch["error"] == "执行接口层用例需要 requests"
    assert batch["total"] == 1
    assert store.list_runs(pid, batch_id="RUN-err-1") == []


def test_runs_tables_align_with_legacy_column_names(fresh_db):
    """表列名与 legacy 对齐（保证新旧库可对账）；新增列只增不改。"""
    conn = connect()
    try:
        runs = table_columns(conn, "runs")
        batches = table_columns(conn, "run_batches")
    finally:
        conn.close()
    assert {
        "project_id",
        "case_id",
        "status",
        "duration_ms",
        "screenshot_path",
        "log_path",
        "created_at",
    } <= runs
    assert {"batch_id", "tp_id", "detail", "mode", "source_kind"} <= runs
    assert {
        "batch_id",
        "project_id",
        "total",
        "done",
        "status_counts",
        "state",
        "started_at",
        "finished_at",
        "error",
        "filters",
        "webhook_url",
        "updated_at",
    } <= batches


# ---------------------------------------------------------------- 执行器
def test_duration_ms_is_measured_and_serialized():
    session = _FakeSession(200)
    case = _case_spec("TP-0")
    case.steps = [
        dict(case.steps[0], layer="接口", method="GET", path="/a", dimension="正常-可用性")
    ]
    result = executor.execute_case(
        case, executor.ExecutorOptions(base_url="http://x"), session=session
    )
    assert result.duration_ms >= 0
    assert result.to_dict()["duration_ms"] == result.duration_ms


# ---------------------------------------------------------------- 全链路
def _exec_opts(repo):
    opts = pipeline.default_options(str(repo))
    opts.scopes = {TPType.NORMAL.value, TPType.SECURITY.value, TPType.BOUNDARY.value}
    opts.target_req.execute = True
    opts.target_req.exec_url = "http://api.local"
    return opts


def test_pipeline_persists_execution_ledger(fresh_db, sample_repo, monkeypatch):
    """全链路：执行 → 落库（runs / run_batches / cases.last_result）。"""
    monkeypatch.setattr(executor, "_new_session", lambda: _FakeSession(200))
    result = pipeline.run_pipeline(_exec_opts(sample_repo))

    assert result.run_batch_id.startswith("RUN-")
    pid = result.project_id or 0

    batches = store.list_run_batches(pid)
    assert [b["batch_id"] for b in batches] == [result.run_batch_id]
    assert batches[0]["state"] == RunBatchState.COMPLETED.value
    assert batches[0]["total"] == result.counts["cases"]

    runs = store.list_runs(pid, batch_id=result.run_batch_id)
    assert len(runs) == result.counts["cases"]
    assert all(r["status"] for r in runs)
    assert any(r["status"] == ExecStatus.PASS.value for r in runs), "2xx 的正常维度应判通过"
    assert any(r["status"] == ExecStatus.SKIPPED.value for r in runs), "UI 层应如实跳过"

    rows = store.list_cases(pid)
    assert all(r["last_result"] for r in rows), "每条用例都应回填执行结论"
    assert all(r["status"] == "generated" for r in rows), "生命周期状态不得被执行结论污染"
    assert any("执行留痕" in n for n in result.notes)


def test_pipeline_without_execute_writes_no_ledger(fresh_db, sample_repo):
    """不执行就不留痕：不得凭空造批次。"""
    result = pipeline.run_pipeline(pipeline.default_options(str(sample_repo)))
    assert result.run_batch_id == ""
    assert result.execution == {}
    assert store.list_run_batches(result.project_id or 0) == []
    assert store.list_runs(result.project_id or 0) == []


def test_failed_execution_still_records_failed_batch(fresh_db, sample_repo, monkeypatch):
    """执行基础设施失败也必须留痕为 failed 批次（不静默、不假装通过）。"""

    def _boom() -> None:
        from core.errors import EngineError

        raise EngineError("执行接口层用例需要 requests：pip install requests")

    monkeypatch.setattr(executor, "_new_session", _boom)
    result = pipeline.run_pipeline(_exec_opts(sample_repo))

    assert result.run_batch_id.startswith("RUN-")
    assert any("用例执行失败" in e for e in result.errors)
    batch = store.latest_run_batch(result.project_id or 0)
    assert batch is not None
    assert batch["state"] == RunBatchState.FAILED.value
    assert "requests" in batch["error"]
    assert store.list_runs(result.project_id or 0, batch_id=result.run_batch_id) == []


# ---------------------------------------------------------------- 查询入口
def test_http_runs_endpoints(fresh_db, sample_repo, monkeypatch):
    from fastapi.testclient import TestClient

    from service.app import app

    monkeypatch.setattr(executor, "_new_session", lambda: _FakeSession(200))
    client = TestClient(app, raise_server_exceptions=False)
    body = client.post(
        "/api/v1/pipeline",
        json={
            "local_path": str(sample_repo),
            "project_name": "f12-api",
            "execute": True,
            "exec_url": "http://api.local",
        },
    ).json()
    pid = body["result"]["project_id"]
    assert body["result"]["run_batch_id"].startswith("RUN-")

    listing = client.get(f"/api/v1/projects/{pid}/runs").json()
    assert listing["batches"]
    batch_id = listing["latest"]["batch_id"]

    detail = client.get(f"/api/v1/projects/{pid}/runs/{batch_id}").json()
    assert detail["batch"]["batch_id"] == batch_id
    assert detail["runs"]
    assert detail["runs"][0]["batch_id"] == batch_id

    missing = client.get(f"/api/v1/projects/{pid}/runs/RUN-nope")
    assert missing.status_code == 404
    assert missing.json()["code"] == "not_found"


def test_cli_runs_command(fresh_db, sample_repo, monkeypatch, capsys):
    import json

    from cli.main import main

    monkeypatch.setattr(executor, "_new_session", lambda: _FakeSession(200))
    result = pipeline.run_pipeline(_exec_opts(sample_repo))
    capsys.readouterr()

    rc = main(["runs", "--project", str(result.project_id), "--batch", result.run_batch_id])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["project_id"] == result.project_id
    assert payload["latest"]["batch_id"] == result.run_batch_id
    assert payload["batch"]["batch_id"] == result.run_batch_id
    assert payload["runs"]


def test_pipeline_result_serializes_run_batch_id(fresh_db, sample_repo):
    result = pipeline.run_pipeline(pipeline.default_options(str(sample_repo)))
    assert "run_batch_id" in result.to_dict()
