"""F15 · 报告能力（执行摘要 / 覆盖率 / 趋势 / 追溯 / 证据 / 导出）。

验收口径（任务清单 §10.5 / §十一）：
- 报告是 **DB 事实的纯函数**：取数（`store.report_snapshot`）→ 计算（`engine.report`）→
  渲染（`output.report_writer`）。同一份数据任意时刻重算，结论一致（可复现）。
- **不新建 `reports` 表**：明细已在 `runs`，报告随时可重算，避免第二真值源。
- 通过率口径 `pass / (pass + fail)`：跳过 / 异常 / 环境态**不计入**分母，
  但必须如实出现在摘要里，不得被误读成「全绿」。

覆盖点：
- `report_snapshot` 只取原始行、不做业务计算，且不建 `reports` 表；批次选取（最近一次 / 显式指定）；
- `pass_rate` 在无判定类结论时返回 `None`（而非 0，避免被误读成「全部失败」）；
- `coverage` 缺口定位（零测试点的功能点 / 零在用用例的测试点）与分模块覆盖；
- `trend` 方向 improving / regressing / stable（取最近两次有效通过率）；
- `flaky` 跨批次 pass↔fail 命中；单批次不参与判定，稳定通过不误报；
- `exec_summary` 诚实性：未执行 / 批次失败 / 跳过不算通过；三档通过率语气；
- 渲染产物：Markdown 章节齐全并转义竖线、HTML 自包含（零外部依赖）且转义外部文本；
- HTTP `/report`（json/md/html + write + 404 + 非法格式）与 `/coverage`；
- CLI `report` 子命令（含 `--no-write`）；
- `ReportFormat` 单一真值源。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core import store
from core.contracts import CaseSpec, FunctionalPoint, TestPoint
from core.db import connect
from core.enums import (
    REPORT_FORMATS,
    CaseLifecycleStatus,
    ExecStatus,
    ReportFormat,
    RunBatchState,
    Tag,
)
from core.errors import NotFoundError
from engine import report as report_engine
from output.report_writer import render_html, render_markdown


# ---------------------------------------------------------------- 造数工具
def _fp(contract_id: str, *, module: str = "m", ftype: str = "api") -> FunctionalPoint:
    return FunctionalPoint(
        fp_id=contract_id,
        ftype=ftype,
        file_path=f"{module}.py",
        name=f"GET /{contract_id}",
        title=f"功能 {contract_id}",
        module=module,
    )


def _tp(tp_id: str, *, fp_contract_id: str = "FP-1", module: str = "m") -> TestPoint:
    return TestPoint(
        tp_id=tp_id,
        fp_contract_id=fp_contract_id,
        category="正常",
        module=module,
        title=f"测试 {tp_id}",
    )


def _case_spec(
    tp_id: str, *, title: str | None = None, test_type: str = Tag.FULL.value
) -> CaseSpec:
    return CaseSpec(
        tc_no=tp_id,
        title=title or f"用例 {tp_id}",
        ctype="api",
        steps=[{"action": "http_probe", "tp_id": tp_id, "expect": "返回 2xx"}],
        module="m",
        case_type="正常",
        priority="P2",
        precondition="服务已启动",
        doc_steps=[{"seq": 1, "type": "前置", "desc": "服务已启动"}],
        tp_id=tp_id,
        fp_contract_id="FP-1",
        test_type=test_type,
    )


def _seed(
    pid: int,
    *,
    fps: list[FunctionalPoint] | None = None,
    tps: list[TestPoint] | None = None,
    case_tp_ids: list[str] | None = None,
    specs: list[CaseSpec] | None = None,
) -> None:
    """铺好功能点 / 测试点 / 用例三层。"""
    store.replace_functional_points(pid, fps or [_fp("FP-1")])
    store.replace_test_points(pid, tps or [_tp("TP-1")])
    wanted = specs if specs is not None else [_case_spec(t) for t in (case_tp_ids or ["TP-1"])]
    store.reconcile_cases(pid, wanted)


def _record(
    pid: int,
    batch_id: str,
    pairs: list[tuple[str, str]],
    *,
    at: str,
    state: str = RunBatchState.COMPLETED.value,
) -> dict[str, int]:
    """按 (tp_id, status) 显式写一批执行留痕（避免与索引耦合）。"""
    statuses = [s for _, s in pairs]
    summary = {
        "batch_id": batch_id,
        "state": state,
        "started_at": at,
        "finished_at": at,
        "total": len(pairs),
        "executed": sum(1 for s in statuses if s != ExecStatus.SKIPPED.value),
        "pass": statuses.count(ExecStatus.PASS.value),
        "fail": statuses.count(ExecStatus.FAIL.value),
        "error": statuses.count(ExecStatus.ERROR.value),
        "skipped": statuses.count(ExecStatus.SKIPPED.value),
        "results": [
            {"tc_no": tp, "status": s, "notes": [f"结论 {s}"], "duration_ms": 5 * i}
            for i, (tp, s) in enumerate(pairs)
        ],
    }
    return store.record_execution(pid, summary)


def _project(tag: str) -> int:
    return store.upsert_project(f"f15-{tag}", f"/tmp/f15-{tag}")


# ---------------------------------------------------------------- 仓储层：取数快照
def test_report_snapshot_is_raw_data_only(fresh_db):
    """报告取数只给原始行，业务计算一律不在仓储层（保证报告是 DB 事实的纯函数）。"""
    pid = _project("snap")
    _seed(pid, case_tp_ids=["TP-1"])
    _record(pid, "RUN-1", [("TP-1", "pass")], at="2026-09-11T10:00:00")

    snap = store.report_snapshot(pid)
    assert set(snap) == {
        "project",
        "functional_points",
        "test_points",
        "cases",
        "batches",
        "batch",
        "runs",
        "run_index",
        "last_change",
        "traceability",
    }
    # 业务结论（摘要 / 覆盖率 / 趋势）**不得**在仓储层出现
    for computed in ("exec_summary", "coverage", "trend", "flaky", "reuse"):
        assert computed not in snap
    # 证据行要能一路回溯到功能点
    assert snap["runs"][0]["fp_contract_id"] == "FP-1"
    assert snap["runs"][0]["case_title"] == "用例 TP-1"
    assert snap["runs"][0]["case_module"] == "m"


def test_no_reports_table_is_created(fresh_db):
    """报告不落独立表：明细已在 runs / run_batches，报告随时可重算。"""
    pid = _project("notable")
    _seed(pid, case_tp_ids=["TP-1"])
    report_engine.build_report(pid)

    conn = connect()
    try:
        names = {
            str(r["name"])
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    finally:
        conn.close()
    assert "reports" not in names
    assert {"runs", "run_batches", "cases", "test_points", "functional_points"} <= names


def test_snapshot_selects_latest_batch_and_honours_explicit(fresh_db):
    pid = _project("batchsel")
    _seed(pid, case_tp_ids=["TP-1"])
    _record(pid, "RUN-a", [("TP-1", "pass")], at="2026-09-11T09:00:00")
    _record(pid, "RUN-b", [("TP-1", "fail")], at="2026-09-11T11:00:00")

    assert store.report_snapshot(pid)["batch"]["batch_id"] == "RUN-b", "缺省取最近一次"
    snap_a = store.report_snapshot(pid, batch_id="RUN-a")
    assert snap_a["batch"]["batch_id"] == "RUN-a"
    assert {r["batch_id"] for r in snap_a["runs"]} == {"RUN-a"}
    # 显式指定但不存在 → batch 为 None（由上层决定是 404 还是"尚未执行"）
    assert store.report_snapshot(pid, batch_id="RUN-nope")["batch"] is None


# ---------------------------------------------------------------- 口径：通过率 / 趋势 / flaky
def test_pass_rate_counts_only_concluded_cases():
    assert report_engine.pass_rate({"pass": 3, "fail": 1}) == 75.0
    assert report_engine.pass_rate({"pass": 2, "fail": 0}) == 100.0
    # 无判定类结论 → None（而非 0）：0 会被误读成"全部失败"，事实往往是"一条都没跑"
    assert report_engine.pass_rate({"skipped": 5}) is None
    assert report_engine.pass_rate({"skipped": 4, "error": 1}) is None
    assert report_engine.pass_rate({"structural_only": 2, "blocked_auth": 1}) is None
    assert report_engine.pass_rate({}) is None


def test_trend_direction_from_last_two_valid_rates(fresh_db):
    pid = _project("trend")
    _seed(pid, case_tp_ids=["TP-1"])
    _record(pid, "RUN-1", [("TP-1", "pass")], at="2026-09-11T09:00:00")
    _record(pid, "RUN-2", [("TP-1", "fail")], at="2026-09-11T10:00:00")

    tr = report_engine.trend(store.report_snapshot(pid)["batches"])
    assert tr["count"] == 2
    assert [p["pass_rate"] for p in tr["points"]] == [100.0, 0.0], "趋势点按开始时间升序"
    assert tr["direction"] == "regressing"


def test_trend_direction_variants():
    def _batch(bid: str, passed: int, failed: int, started: str) -> dict[str, object]:
        return {
            "batch_id": bid,
            "state": RunBatchState.COMPLETED.value,
            "started_at": started,
            "finished_at": started,
            "total": passed + failed,
            "done": passed + failed,
            "status_counts": {"pass": passed, "fail": failed},
        }

    up = report_engine.trend(
        [_batch("a", 1, 1, "2026-09-01T00:00:00"), _batch("b", 3, 1, "2026-09-02T00:00:00")]
    )
    assert up["direction"] == "improving"
    same = report_engine.trend(
        [_batch("a", 1, 1, "2026-09-01T00:00:00"), _batch("b", 2, 2, "2026-09-02T00:00:00")]
    )
    assert same["direction"] == "stable"
    single = report_engine.trend([_batch("a", 1, 0, "2026-09-01T00:00:00")])
    assert single["direction"] == "stable", "单批次无可比对象 → 平稳"
    assert report_engine.trend([]) == {"count": 0, "direction": "stable", "points": []}


def test_flaky_detects_cross_batch_inconsistency(fresh_db):
    pid = _project("flaky")
    _seed(pid, case_tp_ids=["TP-1"])
    _record(pid, "RUN-1", [("TP-1", "pass")], at="2026-09-11T09:00:00")
    _record(pid, "RUN-2", [("TP-1", "fail")], at="2026-09-11T10:00:00")

    fl = report_engine.flaky(store.report_snapshot(pid)["run_index"])
    assert fl["count"] == 1
    assert fl["cases"][0]["tp_id"] == "TP-1"
    assert fl["cases"][0]["batches"] == 2
    assert fl["cases"][0]["statuses"] == ["fail", "pass"]


def test_flaky_ignores_single_batch_and_stable_pass():
    single = [
        {"batch_id": "b1", "tp_id": "TP-1", "case_id": 1, "status": "pass"},
        {"batch_id": "b1", "tp_id": "TP-2", "case_id": 2, "status": "fail"},
    ]
    assert report_engine.flaky(single) == {
        "min_batches": 2,
        "evaluated": 2,
        "count": 0,
        "cases": [],
    }
    stable = [
        {"batch_id": "b1", "tp_id": "TP-3", "case_id": 3, "status": "pass"},
        {"batch_id": "b2", "tp_id": "TP-3", "case_id": 3, "status": "pass"},
    ]
    assert report_engine.flaky(stable)["count"] == 0, "稳定通过不得误报 flaky"
    error_mixed = [
        {"batch_id": "b1", "tp_id": "TP-4", "case_id": 4, "status": "pass"},
        {"batch_id": "b2", "tp_id": "TP-4", "case_id": 4, "status": "error"},
    ]
    assert report_engine.flaky(error_mixed)["count"] == 1, "pass↔error 同样属不稳定"


# ---------------------------------------------------------------- 覆盖率（F16）
def test_coverage_locates_gaps(fresh_db):
    pid = _project("cov")
    _seed(
        pid,
        fps=[_fp("FP-1", module="billing"), _fp("FP-2", module="billing")],
        tps=[_tp("TP-1", module="billing"), _tp("TP-2", module="billing")],
        case_tp_ids=["TP-1"],  # TP-2 未落用例；FP-2 无测试点
    )
    snap = store.report_snapshot(pid)
    cov = report_engine.coverage(snap["functional_points"], snap["test_points"], snap["cases"])

    assert (cov["fp_total"], cov["fp_covered"], cov["fp_rate"]) == (2, 1, 50.0)
    assert (cov["tp_total"], cov["tp_covered"], cov["tp_rate"]) == (2, 1, 50.0)
    assert cov["uncovered_fp_count"] == 1
    assert [f["contract_id"] for f in cov["uncovered_fps"]] == ["FP-2"]
    assert cov["uncovered_tp_count"] == 1
    assert [t["tp_id"] for t in cov["uncovered_tps"]] == ["TP-2"]
    assert cov["module_coverage"]["billing"]["fp_rate"] == 50.0
    assert cov["module_coverage"]["billing"]["tp_rate"] == 50.0
    assert cov["case_active"] == 1


def test_coverage_ignores_obsolete_cases(fresh_db):
    """obsolete 用例不进入「在用」：其对应测试点应重新回到缺口，不能假装还覆盖着。"""
    pid = _project("covobs")
    _seed(pid, tps=[_tp("TP-1"), _tp("TP-2")], case_tp_ids=["TP-1", "TP-2"])
    assert report_engine.build_report(pid)["coverage"]["uncovered_tp_count"] == 0

    store.reconcile_cases(pid, [_case_spec("TP-1")])  # TP-2 用例变 obsolete
    cov = report_engine.build_report(pid)["coverage"]
    assert cov["case_total"] == 2
    assert cov["case_active"] == 1
    assert [t["tp_id"] for t in cov["uncovered_tps"]] == ["TP-2"]


# ---------------------------------------------------------------- 执行摘要（诚实性）
def test_exec_summary_without_batch_says_not_executed(fresh_db):
    pid = _project("nobatch")
    _seed(pid, case_tp_ids=["TP-1"])

    report = report_engine.build_report(pid)
    assert report["batch"] is None
    es = report["exec_summary"]
    assert "尚未执行" in es["headline"]
    assert es["metrics"]["total"] == 0
    assert es["metrics"]["pass_rate"] is None
    assert any("尚未执行" in b for b in es["bullets"])
    assert report["evidence"] == []


def test_exec_summary_failed_batch_is_honest(fresh_db):
    """执行基础设施失败：不给通过率，不假装跑过。"""
    pid = _project("failed")
    _seed(pid, case_tp_ids=["TP-1"])
    store.record_execution(
        pid,
        {
            "batch_id": "RUN-err",
            "state": RunBatchState.FAILED.value,
            "batch_error": "执行接口层用例需要 requests",
            "results": [],
            "total": 1,
            "executed": 0,
            "pass": 0,
            "fail": 0,
            "error": 0,
            "skipped": 0,
            "started_at": "2026-09-11T10:00:00",
            "finished_at": "2026-09-11T10:00:01",
        },
    )

    es = report_engine.build_report(pid)["exec_summary"]
    assert "执行未跑起来" in es["headline"]
    assert es["metrics"]["pass_rate"] is None
    assert any("requests" in b for b in es["bullets"])
    assert report_engine.build_report(pid)["evidence"] == []


def test_exec_summary_does_not_whitewash_skips(fresh_db):
    """跳过 ≠ 通过：通过率无分母，但跳过条数必须显式列出并说明。"""
    pid = _project("skip")
    _seed(pid, tps=[_tp("TP-1"), _tp("TP-2")], case_tp_ids=["TP-1", "TP-2"])
    _record(pid, "RUN-skip", [("TP-1", "skipped"), ("TP-2", "skipped")], at="2026-09-11T10:00:00")

    es = report_engine.build_report(pid)["exec_summary"]
    assert es["metrics"]["pass_rate"] is None
    assert "未产生判定类结论" in es["headline"]
    assert es["metrics"]["skipped"] == 2
    assert any("不等于通过" in b for b in es["bullets"])


def test_exec_summary_headline_tiers():
    def _ctx(passed: int, failed: int) -> dict[str, object]:
        return {
            "batch": {
                "batch_id": "B",
                "state": RunBatchState.COMPLETED.value,
                "total": passed + failed,
            },
            "status_counts": {"pass": passed, "fail": failed},
            "coverage": {"tp_rate": 100.0, "uncovered_tp_count": 0},
            "trend": {"direction": "stable"},
            "flaky": {"count": 0},
            "counts": {"cases_active": passed + failed},
        }

    assert "整体健康" in report_engine.exec_summary(_ctx(9, 1))["headline"]
    assert "基本可用" in report_engine.exec_summary(_ctx(7, 3))["headline"]
    assert "存在问题" in report_engine.exec_summary(_ctx(1, 9))["headline"]


def test_exec_summary_healthy_metrics_and_evidence(fresh_db):
    pid = _project("healthy")
    _seed(pid, case_tp_ids=["TP-1"])
    _record(pid, "RUN-ok", [("TP-1", "pass")], at="2026-09-11T10:00:00")

    report = report_engine.build_report(pid)
    assert "整体健康" in report["exec_summary"]["headline"]
    assert report["exec_summary"]["metrics"]["pass_rate"] == 100.0
    assert report["batch"]["batch_id"] == "RUN-ok"
    row = report["evidence"][0]
    assert (row["tp_id"], row["fp_contract_id"], row["status"]) == ("TP-1", "FP-1", "pass")
    assert report["traceability"]["orphan_tp_count"] == 0
    assert report["traceability"]["orphan_case_count"] == 0


# ---------------------------------------------------------------- 复用占比
def test_reuse_counts_update_vs_full(fresh_db):
    pid = _project("reuse")
    _seed(
        pid,
        tps=[_tp("TP-1"), _tp("TP-2"), _tp("TP-3")],
        specs=[
            _case_spec("TP-1"),
            _case_spec("TP-2", test_type=Tag.UPDATE.value),
            # TP-3 从未落用例 → 不进复用统计
        ],
    )
    reuse = report_engine.build_report(pid)["reuse"]
    assert (reuse["active"], reuse["reused"], reuse["updated"]) == (2, 1, 1)
    assert reuse["reuse_rate"] == 50.0
    assert reuse["test_type"] == {Tag.FULL.value: 1, Tag.UPDATE.value: 1}


def test_reuse_lifecycle_reports_obsolete(fresh_db):
    pid = _project("reuselife")
    _seed(pid, tps=[_tp("TP-1"), _tp("TP-2")], case_tp_ids=["TP-1", "TP-2"])
    store.reconcile_cases(pid, [_case_spec("TP-1")])

    reuse = report_engine.build_report(pid)["reuse"]
    assert reuse["active"] == 1
    assert reuse["lifecycle"].get(CaseLifecycleStatus.OBSOLETE.value) == 1


# ---------------------------------------------------------------- 主入口 / 可复现
def test_build_report_raises_for_missing_project_and_batch(fresh_db):
    with pytest.raises(NotFoundError):
        report_engine.build_report(99999)
    pid = _project("404")
    _seed(pid, case_tp_ids=["TP-1"])
    with pytest.raises(NotFoundError):
        report_engine.build_report(pid, batch_id="RUN-nope")
    # 不指定批次：无批次是合法状态（"尚未执行"），不得抛
    assert report_engine.build_report(pid)["batch"] is None


def test_report_is_reproducible_from_db(fresh_db):
    """报告是纯函数：同一份数据重算，除时间戳外完全一致。"""
    pid = _project("repro")
    _seed(pid, case_tp_ids=["TP-1"])
    _record(pid, "RUN-1", [("TP-1", "pass")], at="2026-09-11T10:00:00")

    first = report_engine.build_report(pid)
    second = report_engine.build_report(pid)
    first.pop("generated_at")
    second.pop("generated_at")
    assert first == second


def test_generate_writes_three_artifacts(fresh_db):
    pid = _project("gen")
    _seed(pid, case_tp_ids=["TP-1"])
    _record(pid, "RUN-1", [("TP-1", "pass")], at="2026-09-11T10:00:00")

    built = report_engine.generate(pid)
    assert set(built["outputs"]) == {"markdown", "html", "json"}
    for key, expected_name in (
        ("markdown", "REPORT.md"),
        ("html", "REPORT.html"),
        ("json", "report.json"),
    ):
        path = Path(built["outputs"][key])
        assert path.is_file()
        assert path.name == expected_name
        assert path.read_text(encoding="utf-8").strip()
    # write=False 只计算不落盘
    assert report_engine.generate(pid, write=False)["outputs"] == {}


# ---------------------------------------------------------------- 渲染
def test_render_markdown_has_key_sections_and_escapes(fresh_db):
    pid = _project("md")
    _seed(pid, specs=[_case_spec("TP-1", title="A | B <C>")])
    _record(pid, "RUN-1", [("TP-1", "pass")], at="2026-09-11T10:00:00")

    md = render_markdown(report_engine.build_report(pid))
    for heading in (
        "# 测试报告",
        "## 一、执行摘要",
        "## 二、覆盖率",
        "## 三、执行趋势",
        "## 四、Flaky",
        "## 五、追溯体检",
        "## 六、执行证据",
        "## 七、用例复用占比",
        "## 八、变更摘要",
    ):
        assert heading in md
    assert "A \\| B <C>" in md, "竖线必须转义，否则会破坏 Markdown 表格"
    assert md.endswith("\n")


def test_render_html_is_self_contained_and_escapes(fresh_db):
    pid = _project("html")
    _seed(pid, specs=[_case_spec("TP-1", title="单据 <script>alert(1)</script> & 备注")])
    _record(pid, "RUN-1", [("TP-1", "pass")], at="2026-09-11T10:00:00")

    out = render_html(report_engine.build_report(pid))
    assert out.startswith("<!doctype html>")
    assert out.rstrip().endswith("</html>")
    # 外部文本必须转义，防止标题里的标签破坏页面结构
    assert "<script>alert(1)</script>" not in out
    assert "&lt;script&gt;" in out
    assert "&amp;" in out
    # 自包含：零外部依赖（无外链、无外链脚本/样式）
    assert "http://" not in out
    assert "https://" not in out
    assert "<script" not in out
    assert "<style>" in out


# ---------------------------------------------------------------- HTTP 入口
def _client():
    from fastapi.testclient import TestClient

    from service.app import app

    return TestClient(app, raise_server_exceptions=False)


def test_http_report_endpoints(fresh_db):
    pid = _project("api")
    _seed(pid, case_tp_ids=["TP-1"])
    _record(pid, "RUN-1", [("TP-1", "pass")], at="2026-09-11T10:00:00")
    client = _client()

    body = client.get(f"/api/v1/projects/{pid}/report").json()
    assert body["project_id"] == pid
    assert body["report"]["batch"]["batch_id"] == "RUN-1"
    assert body["report"]["exec_summary"]["metrics"]["pass"] == 1

    md = client.get(f"/api/v1/projects/{pid}/report?format=md")
    assert md.status_code == 200
    assert "text/markdown" in md.headers["content-type"]
    assert "## 一、执行摘要" in md.text

    html = client.get(f"/api/v1/projects/{pid}/report?format=html")
    assert html.status_code == 200
    assert "text/html" in html.headers["content-type"]
    assert html.text.startswith("<!doctype html>")

    # 恒非法格式 → 422（入参校验失败）。注：pdf 现已随 reportlab 安装成为合法格式，
    # 故此处用明确非法的 xyzzy 验证「未知格式拒绝」，避免与环境（CI 未装可选依赖）耦合。
    bad = client.get(f"/api/v1/projects/{pid}/report?format=xyzzy")
    assert bad.status_code == 422
    assert bad.json()["code"] == "validation_error"

    # 指定不存在的批次 / 项目 → 404
    missing = client.get(f"/api/v1/projects/{pid}/report?batch=RUN-nope")
    assert missing.status_code == 404
    assert missing.json()["code"] == "not_found"
    assert client.get("/api/v1/projects/99999/report").status_code == 404


def test_http_report_write_and_coverage(fresh_db):
    pid = _project("apiwrite")
    _seed(
        pid,
        fps=[_fp("FP-1"), _fp("FP-2")],
        tps=[_tp("TP-1")],
        case_tp_ids=["TP-1"],
    )
    _record(pid, "RUN-1", [("TP-1", "pass")], at="2026-09-11T10:00:00")
    client = _client()

    body = client.get(f"/api/v1/projects/{pid}/report?write=true").json()
    assert set(body["outputs"]) == {"markdown", "html", "json"}
    for path in body["outputs"].values():
        assert Path(path).is_file()

    cov = client.get(f"/api/v1/projects/{pid}/coverage").json()
    assert cov["project_id"] == pid
    assert cov["coverage"]["tp_rate"] == 100.0
    assert cov["coverage"]["fp_rate"] == 50.0  # FP-2 无测试点
    assert cov["coverage"]["uncovered_fp_count"] == 1

    assert client.get("/api/v1/projects/99999/coverage").status_code == 404


# ---------------------------------------------------------------- CLI 入口
def test_cli_report_command(fresh_db, capsys):
    import json

    from cli.main import main

    pid = _project("cli")
    _seed(pid, case_tp_ids=["TP-1"])
    _record(pid, "RUN-1", [("TP-1", "pass")], at="2026-09-11T10:00:00")
    capsys.readouterr()

    rc = main(["report", "--project", str(pid), "--batch", "RUN-1"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["project_id"] == pid
    assert payload["batch_id"] == "RUN-1"
    assert payload["metrics"]["pass"] == 1
    assert payload["coverage"]["tp_rate"] == 100.0
    assert payload["outputs"]["markdown"].endswith("REPORT.md")

    rc2 = main(["report", "--project", str(pid), "--no-write"])
    assert rc2 == 0
    assert json.loads(capsys.readouterr().out)["outputs"] == {}


# ---------------------------------------------------------------- 枚举单一真值源
def test_report_format_enum_single_source():
    assert REPORT_FORMATS == ("json", "md", "html", "pdf", "docx", "xlsx")
    assert set(REPORT_FORMATS) == {f.value for f in ReportFormat}
