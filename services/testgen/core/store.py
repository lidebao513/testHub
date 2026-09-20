"""仓储层：数据读写与对账，不感知 HTTP，不承载业务规则。

- 全部走参数化查询；
- 多步写入使用事务（`with conn:` 自动提交/回滚）；
- 用例对账（幂等）在此实现：生成 → 更新 → 复用 → 废弃，键为 `tp_id`；
- 执行留痕（F12）在此实现：逐条 `runs` + 批次 `run_batches`，并回填 `cases.last_result`。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from core.contracts import CaseSpec, FunctionalPoint, TestPoint
from core.db import connect, init_db
from core.enums import CaseLifecycleStatus, RunBatchState
from core.errors import ContractViolation


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _loads(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


# ============================================================================
# 项目
# ============================================================================
def upsert_project(  # noqa: PLR0913 - 项目注册字段本就多，显式关键字参数比结构体更直观
    name: str,
    local_path: str,
    *,
    git_url: str = "",
    branch: str = "",
    base_url: str = "",
    conn: sqlite3.Connection | None = None,
) -> int:
    """按 name 注册或更新项目，返回 project_id。"""
    own = conn is None
    conn = conn or connect()
    init_db(conn)
    try:
        with conn:
            row = conn.execute("SELECT id FROM projects WHERE name = ?", (name,)).fetchone()
            if row:
                pid = int(row["id"])
                conn.execute(
                    "UPDATE projects SET local_path=?, git_url=?, branch=?, base_url=? WHERE id=?",
                    (local_path, git_url, branch, base_url, pid),
                )
                return pid
            cur = conn.execute(
                "INSERT INTO projects(name, local_path, git_url, branch, base_url, created_at)"
                " VALUES(?,?,?,?,?,?)",
                (name, local_path, git_url, branch, base_url, _now()),
            )
            return int(cur.lastrowid or 0)
    finally:
        if own:
            conn.close()


def get_project(pid: int, conn: sqlite3.Connection | None = None) -> dict[str, Any] | None:
    own = conn is None
    conn = conn or connect()
    try:
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (pid,)).fetchone()
        return dict(row) if row else None
    finally:
        if own:
            conn.close()


def list_projects(conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    own = conn is None
    conn = conn or connect()
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM projects ORDER BY id")]
    finally:
        if own:
            conn.close()


# ============================================================================
# 功能点
# ============================================================================
def replace_functional_points(
    pid: int, fps: list[FunctionalPoint], *, conn: sqlite3.Connection | None = None
) -> dict[str, int]:
    """整批替换某项目的功能点（先删后插，保证与分析结果一致）。"""
    own = conn is None
    conn = conn or connect()
    try:
        with conn:
            conn.execute("DELETE FROM functional_points WHERE project_id = ?", (pid,))
            rows = [
                (
                    pid,
                    fp.commit_ref,
                    fp.file_path,
                    fp.name,
                    fp.description,
                    fp.ftype,
                    fp.review_status,
                    _now(),
                    fp.fp_id,
                    fp.title,
                    fp.module,
                    fp.semantic,
                )
                for fp in fps
            ]
            conn.executemany(
                "INSERT INTO functional_points(project_id, commit_ref, file_path, name,"
                " description, ftype, review_status, created_at, contract_id, title, module,"
                " semantic) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
        return {"functional_points": len(rows)}
    finally:
        if own:
            conn.close()


def list_functional_points(
    pid: int, conn: sqlite3.Connection | None = None
) -> list[dict[str, Any]]:
    own = conn is None
    conn = conn or connect()
    try:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM functional_points WHERE project_id = ? ORDER BY id", (pid,)
            )
        ]
    finally:
        if own:
            conn.close()


def fp_row_map(pid: int, conn: sqlite3.Connection | None = None) -> dict[str, int]:
    """contract_id(fp_id) → 自增行 id 的映射（用例回填 fp_id 列用）。"""
    return {
        str(r["contract_id"]): int(r["id"])
        for r in list_functional_points(pid, conn)
        if r.get("contract_id")
    }


# ============================================================================
# 测试点
# ============================================================================
def replace_test_points(
    pid: int, tps: list[TestPoint], *, conn: sqlite3.Connection | None = None
) -> dict[str, int]:
    """整批替换某项目的测试点。"""
    own = conn is None
    conn = conn or connect()
    try:
        created = _now()
        rows = [
            (
                pid,
                tp.tp_id,
                tp.fp_contract_id,
                tp.category,
                tp.module,
                tp.semantic,
                tp.title,
                tp.source,
                tp.method,
                tp.area,
                tp.expect,
                tp.dimension,
                tp.tag,
                tp.review_status,
                created,
                _dumps(tp.evidence),
                tp.confidence,
                tp.origin,
                1 if tp.unverified else 0,
            )
            for tp in tps
        ]
        with conn:
            conn.execute("DELETE FROM test_points WHERE project_id = ?", (pid,))
            conn.executemany(
                "INSERT INTO test_points(project_id, tp_id, fp_contract_id, category, module,"
                " semantic, title, source, method, area, expect, dimension, tag, review_status,"
                " created_at, evidence, confidence, origin, unverified)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
        return {"test_points": len(rows)}
    finally:
        if own:
            conn.close()


def list_test_points(pid: int, conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    own = conn is None
    conn = conn or connect()
    try:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM test_points WHERE project_id = ? ORDER BY id", (pid,)
            )
        ]
        for r in rows:
            r["evidence"] = _loads(r.get("evidence"), [])
        return rows
    finally:
        if own:
            conn.close()


# ============================================================================
# 用例（幂等对账）
# ============================================================================
def _duplicate_tp_ids(specs: list[CaseSpec]) -> list[str]:
    """找出重复的 tp_id（保持首次出现顺序）。"""
    seen: set[str] = set()
    dupes: list[str] = []
    for spec in specs:
        key = spec.tp_id
        if key in seen and key not in dupes:
            dupes.append(key)
        seen.add(key)
    return dupes


def reconcile_cases(
    pid: int, specs: list[CaseSpec], *, conn: sqlite3.Connection | None = None
) -> dict[str, int]:
    """按 `tp_id` 对账生成用例，幂等。

    返回统计：created / updated / reused / obsolete。
    - 新出现 → inserted（status=generated）
    - 内容变化 → updated（version+1）
    - 未变 → reused
    - 旧用例的 tp_id 已不在本次集合 → obsolete（保留可见、不进执行）

    传入 `specs` 内部若出现重复 tp_id，说明上游编号算法发生了碰撞，**直接失败**：
    静默插入两行会绕开以 tp_id 为键的对账，重复行将永远无法被更新或作废。
    """
    dupes = _duplicate_tp_ids(specs)
    if dupes:
        raise ContractViolation(
            f"用例编号重复（上游编号碰撞）：{dupes[:5]}，共 {len(dupes)} 个；"
            "请在生成侧修正编号算法，而非在此处去重"
        )

    own = conn is None
    conn = conn or connect()
    try:
        stats = {"created": 0, "updated": 0, "reused": 0, "obsolete": 0}
        with conn:
            existing = {
                str(r["tp_id"]): dict(r)
                for r in conn.execute(
                    "SELECT * FROM cases WHERE project_id = ? AND tp_id IS NOT NULL", (pid,)
                )
            }
            incoming: set[str] = set()
            for spec in specs:
                incoming.add(spec.tp_id)
                old = existing.get(spec.tp_id)
                payload = _case_payload(spec)
                if old is None:
                    conn.execute(
                        "INSERT INTO cases(project_id, fp_id, title, steps, ctype,"
                        " review_status, status, created_at, tp_id, fp_contract_id, tc_no,"
                        " module, case_type, priority, precondition, doc_steps, version,"
                        " test_type) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            pid,
                            spec.fp_row_id,
                            spec.title,
                            _dumps(spec.steps),
                            spec.ctype,
                            "approved",
                            "generated",
                            _now(),
                            spec.tp_id,
                            spec.fp_contract_id,
                            spec.tc_no,
                            spec.module,
                            spec.case_type,
                            spec.priority,
                            spec.precondition,
                            _dumps(spec.doc_steps),
                            1,
                            spec.test_type,
                        ),
                    )
                    stats["created"] += 1
                    continue
                if _case_changed(old, payload):
                    conn.execute(
                        "UPDATE cases SET title=?, steps=?, ctype=?, tc_no=?, module=?,"
                        " case_type=?, priority=?, precondition=?, doc_steps=?, version=?,"
                        " status='updated', fp_id=?, fp_contract_id=?, test_type=?"
                        " WHERE id=?",
                        (
                            spec.title,
                            _dumps(spec.steps),
                            spec.ctype,
                            spec.tc_no,
                            spec.module,
                            spec.case_type,
                            spec.priority,
                            spec.precondition,
                            _dumps(spec.doc_steps),
                            int(old.get("version") or 1) + 1,
                            spec.fp_row_id,
                            spec.fp_contract_id,
                            spec.test_type,
                            old["id"],
                        ),
                    )
                    stats["updated"] += 1
                else:
                    stats["reused"] += 1

            stale = [tid for tid in existing if tid not in incoming]
            for tid in stale:
                conn.execute(
                    "UPDATE cases SET status='obsolete' WHERE id=?", (existing[tid]["id"],)
                )
                stats["obsolete"] += 1
        return stats
    finally:
        if own:
            conn.close()


def _case_payload(spec: CaseSpec) -> dict[str, Any]:
    return {
        "title": spec.title,
        "steps": spec.steps,
        "doc_steps": spec.doc_steps,
        "priority": spec.priority,
        "precondition": spec.precondition,
        "case_type": spec.case_type,
    }


def _case_changed(old: dict[str, Any], payload: dict[str, Any]) -> bool:
    """内容是否变化：比对标题/步骤/文档步骤/优先级/前置/类型（忽略执行结论列）。"""
    for key, want in payload.items():
        have = _loads(old.get(key), old.get(key)) if key in ("steps", "doc_steps") else old.get(key)
        if have != want:
            return True
    return False


def list_cases(
    pid: int,
    *,
    include_obsolete: bool = False,
    conn: sqlite3.Connection | None = None,
) -> list[dict[str, Any]]:
    own = conn is None
    conn = conn or connect()
    try:
        sql = "SELECT * FROM cases WHERE project_id = ?"
        params: list[Any] = [pid]
        if not include_obsolete:
            sql += " AND status != ?"
            params.append(CaseLifecycleStatus.OBSOLETE.value)
        sql += " ORDER BY id"
        rows = [dict(r) for r in conn.execute(sql, tuple(params))]
        for r in rows:
            r["steps"] = _loads(r.get("steps"), [])
            r["doc_steps"] = _loads(r.get("doc_steps"), [])
        return rows
    finally:
        if own:
            conn.close()


def case_stats(pid: int, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    own = conn is None
    conn = conn or connect()
    try:
        by_status = {
            str(r["status"]): int(r["n"])
            for r in conn.execute(
                "SELECT status, COUNT(*) AS n FROM cases WHERE project_id = ? GROUP BY status",
                (pid,),
            )
        }
        by_priority = {
            str(r["priority"]): int(r["n"])
            for r in conn.execute(
                "SELECT priority, COUNT(*) AS n FROM cases WHERE project_id = ?"
                " AND status != ? GROUP BY priority",
                (pid, CaseLifecycleStatus.OBSOLETE.value),
            )
        }
        return {"by_status": by_status, "by_priority": by_priority}
    finally:
        if own:
            conn.close()


# ============================================================================
# 执行留痕（F12）：runs（逐条）+ run_batches（逐批）
# ============================================================================
# 参与批次汇总的计数键（与 executor.summarize 的输出同名）
_RUN_COUNT_KEYS: tuple[str, ...] = ("total", "executed", "pass", "fail", "error", "skipped")


def _first_note(notes: Any) -> str:
    """取结论首条备注作为 `runs.detail`（人类可读的一句话）。"""
    if isinstance(notes, (list, tuple)) and notes:
        return str(notes[0])
    return ""


def record_execution(
    pid: int,
    summary: Mapping[str, Any],
    *,
    mode: str = "",
    source_kind: str = "",
    conn: sqlite3.Connection | None = None,
) -> dict[str, int]:
    """把**一次执行**的结论落库（F12），返回写入统计 `{"runs", "cases", "batches"}`。

    `summary` 为执行器汇总 + 批次元信息，约定字段：
      - `batch_id`：批次号（**必需**）。缺失即视为「本次没有执行」，直接不写——
        宁可不写，也不留一条没有批次号的孤儿记录；
      - `state`：批次终态（`RunBatchState` 取值，缺省 `completed`）；
      - `results`：逐条结论 `[{tc_no, status, notes, duration_ms}, ...]`；
      - `total/executed/pass/fail/error/skipped`：汇总计数（`error` 是**计数**，不是文案）；
      - `batch_error`：批次级错误文案（基础设施失败时才有，**不可与计数 `error` 同名**）；
      - `started_at/finished_at`：批次起止时间（缺省以当前时间兜底）。

    三件事在**同一事务**内完成：
      1. 逐条写 `runs`（`case_id` 关联用例行、`tp_id` 冗余便于追溯）；
      2. 回填 `cases.last_result`（按 `tp_id` 定位；契约约定 `tc_no == tp_id`）；
      3. upsert `run_batches`（批次终态 + 状态分布）。

    幂等：同一 `batch_id` 重复落库会**先清后写**，不累积重复行。
    **只写 `cases.last_result`，绝不改 `cases.status`**——生命周期状态与执行结论解耦，
    否则「重新生成用例」会把执行结论当成生命周期值误读。
    """
    own = conn is None
    conn = conn or connect()
    if own:
        init_db(conn)
    try:
        batch_id = str(summary.get("batch_id") or "").strip()
        if not batch_id:
            return {"runs": 0, "cases": 0, "batches": 0}

        results = [r for r in (summary.get("results") or []) if isinstance(r, Mapping)]
        now = _now()
        started = str(summary.get("started_at") or now)
        finished = str(summary.get("finished_at") or now)
        state = str(summary.get("state") or RunBatchState.COMPLETED.value)
        error = str(summary.get("batch_error") or "")
        counts = {k: int(summary.get(k, 0) or 0) for k in _RUN_COUNT_KEYS}

        status_counts: dict[str, int] = {}
        rows: list[tuple[Any, ...]] = []
        backfilled = 0
        with conn:
            conn.execute("DELETE FROM runs WHERE project_id = ? AND batch_id = ?", (pid, batch_id))
            case_rows = {
                str(r["tp_id"]): int(r["id"])
                for r in conn.execute(
                    "SELECT id, tp_id FROM cases WHERE project_id = ? AND tp_id IS NOT NULL",
                    (pid,),
                )
            }
            for item in results:
                tp_id = str(item.get("tc_no") or "")
                status = str(item.get("status") or "")
                status_counts[status] = status_counts.get(status, 0) + 1
                rows.append(
                    (
                        pid,
                        batch_id,
                        case_rows.get(tp_id),
                        tp_id,
                        status,
                        _first_note(item.get("notes")),
                        int(item.get("duration_ms") or 0),
                        str(item.get("screenshot_path") or ""),
                        str(item.get("log_path") or ""),
                        mode,
                        source_kind,
                        now,
                    )
                )
                if tp_id:
                    cursor = conn.execute(
                        "UPDATE cases SET last_result = ? WHERE project_id = ? AND tp_id = ?",
                        (status, pid, tp_id),
                    )
                    backfilled += int(cursor.rowcount or 0)
            conn.executemany(
                "INSERT INTO runs(project_id, batch_id, case_id, tp_id, status, detail,"
                " duration_ms, screenshot_path, log_path, mode, source_kind, created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
            _upsert_run_batch(
                conn,
                batch_id=batch_id,
                pid=pid,
                mode=mode,
                source_kind=source_kind,
                counts=counts,
                done=len(rows),
                status_counts=status_counts,
                state=state,
                started=started,
                finished=finished,
                error=error,
                updated=now,
            )
        return {"runs": len(rows), "cases": backfilled, "batches": 1}
    finally:
        if own:
            conn.close()


def _upsert_run_batch(  # noqa: PLR0913 - 批次字段本就多，显式关键字参数比包成字典更直观
    conn: sqlite3.Connection,
    *,
    batch_id: str,
    pid: int,
    mode: str,
    source_kind: str,
    counts: dict[str, int],
    done: int,
    status_counts: dict[str, int],
    state: str,
    started: str,
    finished: str,
    error: str,
    updated: str,
) -> None:
    """写入/覆盖批次终态（同 `batch_id` 重跑即覆盖，保证幂等）。"""
    conn.execute(
        "INSERT INTO run_batches(batch_id, project_id, mode, source_kind, total, done,"
        " status_counts, state, started_at, finished_at, error, filters, webhook_url, updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(batch_id) DO UPDATE SET"
        " project_id=excluded.project_id, mode=excluded.mode,"
        " source_kind=excluded.source_kind, total=excluded.total, done=excluded.done,"
        " status_counts=excluded.status_counts, state=excluded.state,"
        " started_at=excluded.started_at, finished_at=excluded.finished_at,"
        " error=excluded.error, updated_at=excluded.updated_at",
        (
            batch_id,
            pid,
            mode,
            source_kind,
            counts["total"],
            done,
            _dumps(status_counts),
            state,
            started,
            finished,
            error,
            "{}",
            "",
            updated,
        ),
    )


def list_run_batches(
    pid: int, *, limit: int = 50, conn: sqlite3.Connection | None = None
) -> list[dict[str, Any]]:
    """按项目列出执行批次（最新在前）；`status_counts` 已解析为 dict。"""
    own = conn is None
    conn = conn or connect()
    try:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM run_batches WHERE project_id = ?"
                " ORDER BY COALESCE(finished_at, started_at, updated_at) DESC, batch_id DESC"
                " LIMIT ?",
                (pid, max(1, int(limit))),
            )
        ]
        for row in rows:
            row["status_counts"] = _loads(row.get("status_counts"), {})
        return rows
    finally:
        if own:
            conn.close()


def get_run_batch(
    batch_id: str, *, conn: sqlite3.Connection | None = None
) -> dict[str, Any] | None:
    """按批次号取单个批次；不存在返回 None。"""
    own = conn is None
    conn = conn or connect()
    try:
        row = conn.execute("SELECT * FROM run_batches WHERE batch_id = ?", (batch_id,)).fetchone()
        if not row:
            return None
        out = dict(row)
        out["status_counts"] = _loads(out.get("status_counts"), {})
        return out
    finally:
        if own:
            conn.close()


def latest_run_batch(pid: int, *, conn: sqlite3.Connection | None = None) -> dict[str, Any] | None:
    """取项目最近一次执行批次（无则 None）。"""
    batches = list_run_batches(pid, limit=1, conn=conn)
    return batches[0] if batches else None


def list_runs(
    pid: int,
    *,
    batch_id: str = "",
    limit: int = 200,
    conn: sqlite3.Connection | None = None,
) -> list[dict[str, Any]]:
    """列出逐条执行结论；给了 `batch_id` 则只取该批次。"""
    own = conn is None
    conn = conn or connect()
    try:
        sql = "SELECT * FROM runs WHERE project_id = ?"
        params: list[Any] = [pid]
        if batch_id:
            sql += " AND batch_id = ?"
            params.append(batch_id)
        sql += " ORDER BY id LIMIT ?"
        params.append(max(1, int(limit)))
        return [dict(r) for r in conn.execute(sql, tuple(params))]
    finally:
        if own:
            conn.close()


# ============================================================================
# 追溯与变更日志
# ============================================================================
def traceability(pid: int, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    """追溯体检：孤儿测试点 / 孤儿用例应为 0。"""
    own = conn is None
    conn = conn or connect()
    try:
        fp_ids = {
            str(r["contract_id"])
            for r in conn.execute(
                "SELECT contract_id FROM functional_points WHERE project_id = ?", (pid,)
            )
        }
        tp_rows = list(
            conn.execute(
                "SELECT tp_id, fp_contract_id FROM test_points WHERE project_id = ?", (pid,)
            )
        )
        tp_ids = {str(r["tp_id"]) for r in tp_rows}
        case_rows = list(
            conn.execute(
                "SELECT tp_id FROM cases WHERE project_id = ? AND status != 'obsolete'", (pid,)
            )
        )
        orphan_tp = [str(r["tp_id"]) for r in tp_rows if str(r["fp_contract_id"]) not in fp_ids]
        orphan_case = [str(r["tp_id"]) for r in case_rows if str(r["tp_id"]) not in tp_ids]
        return {
            "fp_count": len(fp_ids),
            "tp_count": len(tp_ids),
            "case_count": len(case_rows),
            "orphan_tp": orphan_tp,
            "orphan_case": orphan_case,
            "orphan_tp_count": len(orphan_tp),
            "orphan_case_count": len(orphan_case),
        }
    finally:
        if own:
            conn.close()


def log_change(pid: int, kind: str, detail: str, *, conn: sqlite3.Connection | None = None) -> None:
    own = conn is None
    conn = conn or connect()
    try:
        with conn:
            conn.execute(
                "INSERT INTO change_log(project_id, kind, detail, created_at) VALUES(?,?,?,?)",
                (pid, kind, detail, _now()),
            )
    finally:
        if own:
            conn.close()


# ============================================================================
# 报告取数快照（F15）：一次连接取齐报告所需的全部原始行（只读，不做业务计算）
# ============================================================================
def _select_batch(batches: list[dict[str, Any]], batch_id: str) -> dict[str, Any] | None:
    """选批次：显式指定优先（指定但不存在 → None，由调用方决定是否报 404）；否则取最近一次。

    最近一次按「结束时间 → 开始时间 → 批次号」排序取最大——**不能**直接用 `list_run_batches`
    的倒序结果，因为趋势要的升序与"最近一次"是两个视角，容易写反。
    """
    if batch_id:
        for b in batches:
            if str(b.get("batch_id")) == batch_id:
                return b
        return None
    if not batches:
        return None
    return max(
        batches,
        key=lambda b: (
            str(b.get("finished_at") or b.get("started_at") or ""),
            str(b.get("batch_id")),
        ),
    )


def report_snapshot(
    pid: int, *, batch_id: str = "", conn: sqlite3.Connection | None = None
) -> dict[str, Any]:
    """报告取数快照（F15）：把「报告要用到的原始行」一次取齐，**不做任何业务计算**。

    职责边界：通过率 / 覆盖率 / 趋势 / 结论等**业务规则一律不在此**，全部放在
    `engine/report.py`；本函数只把行从库里读出来，从而保证报告是 DB 事实的**纯函数**
    （同一份数据任意时刻重算，结论一致、可复现）。

    `batch_id` 为空取最近一次执行；显式指定但不存在时 `batch` 返回 `None`
    （由上层决定是"报 404"还是"报告尚未执行"）。
    """
    own = conn is None
    conn = conn or connect()

    def rows(sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        return [dict(r) for r in conn.execute(sql, params)]

    try:
        project = conn.execute("SELECT * FROM projects WHERE id = ?", (pid,)).fetchone()
        fps = rows(
            "SELECT id, contract_id, name, module, ftype, file_path FROM functional_points"
            " WHERE project_id = ? ORDER BY id",
            (pid,),
        )
        tps = rows(
            "SELECT tp_id, fp_contract_id, category, module, title, dimension, method,"
            " expect, source, tag FROM test_points WHERE project_id = ? ORDER BY id",
            (pid,),
        )
        cases = rows(
            "SELECT id, tp_id, fp_contract_id, module, title, case_type, priority, status,"
            " test_type, last_result FROM cases WHERE project_id = ? ORDER BY id",
            (pid,),
        )
        batches = rows(
            "SELECT batch_id, state, mode, source_kind, total, done, status_counts, started_at,"
            " finished_at, error FROM run_batches WHERE project_id = ?"
            " ORDER BY COALESCE(started_at, ''), batch_id",
            (pid,),
        )
        for b in batches:
            b["status_counts"] = _loads(b.get("status_counts"), {})
        batch = _select_batch(batches, batch_id)
        runs: list[dict[str, Any]] = []
        if batch:
            runs = rows(
                "SELECT r.id, r.batch_id, r.case_id, r.tp_id, r.status, r.detail, r.duration_ms,"
                " r.screenshot_path, r.log_path, c.title AS case_title, c.module AS case_module,"
                " c.case_type AS case_type, c.priority AS case_priority, t.title AS tp_title,"
                " t.fp_contract_id AS fp_contract_id, t.source AS source, t.expect AS expect"
                " FROM runs r"
                " LEFT JOIN cases c ON c.id = r.case_id"
                " LEFT JOIN test_points t ON t.project_id = r.project_id AND t.tp_id = r.tp_id"
                " WHERE r.project_id = ? AND r.batch_id = ? ORDER BY r.id",
                (pid, str(batch["batch_id"])),
            )
        run_index = rows(
            "SELECT batch_id, tp_id, case_id, status FROM runs WHERE project_id = ? ORDER BY id",
            (pid,),
        )
        changes = rows(
            "SELECT kind, detail, created_at FROM change_log WHERE project_id = ?"
            " ORDER BY id DESC LIMIT 1",
            (pid,),
        )
        return {
            "project": dict(project) if project else None,
            "functional_points": fps,
            "test_points": tps,
            "cases": cases,
            "batches": batches,
            "batch": batch,
            "runs": runs,
            "run_index": run_index,
            "last_change": changes[0] if changes else None,
            "traceability": traceability(pid, conn),
        }
    finally:
        if own:
            conn.close()
