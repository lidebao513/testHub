"""仓储层：数据读写与对账，不感知 HTTP，不承载业务规则。

- 全部走参数化查询；
- 多步写入使用事务（`with conn:` 自动提交/回滚）；
- 用例对账（幂等）在此实现：生成 → 更新 → 复用 → 废弃，键为 `tp_id`；
- 执行留痕（F12）在此实现：逐条 `runs` + 批次 `run_batches`，并回填 `cases.last_result`。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from core.contracts import CaseSpec, FunctionalPoint, TestPoint
from core.db import connect, init_db
from core.enums import CaseLifecycleStatus, RunBatchState
from core.errors import ContractViolation


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ============================================================================
# 项目级写锁（Batch 6 C-2）：并发重提同一 project 时串行化落库
# ============================================================================
# 背景：`stage_persist` 先在单连接里做 FP/TP 全量替换 + cases 对账，两个任务
# 若同时跑到 persist，会出现「A 删 → B 删 → A 插 → B 插」交错，最终库里是
# 两个产物的混合体（C-2 写覆盖）。幂等键只挡住「完全相同的请求」，不同参数
# 的并发重提（如改了 selected_paths）无法靠幂等键去重。
#
# 为什么是进程内锁：sidecar 是单进程 ThreadPoolExecutor（见 service/tasks.py），
# 线程间共享这片内存；SQLite 的 `BEGIN IMMEDIATE` 只能串行化「写事务开始之后」，
# 挡不住「读到旧 existing → 对方先写 → 自己再按旧快照对账」的读改写竞态。
# 因此锁必须覆盖整个 persist（读 existing 之前就持有）。多进程部署时需在
# 外层换成 DB advisory lock（见 docs/optimization_batches_2026-10-20.md Batch 6），
# 本实现不伪装成跨进程方案。
_project_write_locks: dict[int, threading.RLock] = {}
_project_write_locks_guard = threading.Lock()


@contextmanager
def project_write_lock(project_id: int) -> Iterator[None]:
    """同一 project 的落库串行化；不同 project 并行不受影响。

    用 `RLock` 允许同一线程内嵌套（如 stage_persist 内部再调带锁的辅助
    writer），避免自锁死。锁的生命周期只到 context 退出，与 DB 事务解耦：
    调用方应把锁包在 `with conn:` 事务**外层**，确保读-改-写全程互斥。
    """
    with _project_write_locks_guard:
        lock = _project_write_locks.setdefault(project_id, threading.RLock())
    with lock:
        yield


def clear_project_write_locks() -> None:
    """仅供测试隔离：清空锁注册表（不释放调用方已持有的锁）。"""
    with _project_write_locks_guard:
        _project_write_locks.clear()


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _loads(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def _text(value: Any) -> str:
    """DB 里历史 NULL 与契约侧空串视为同一值（增量对账比较用）。"""
    return "" if value is None else str(value)


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
# 仓库绑定（多仓改造 v1.7）：project 作容器，一个项目下 N 条 binding
# ============================================================================
def upsert_repo_binding(  # noqa: PLR0913 - 仓库绑定字段本就多，显式关键字参数更直观
    project_id: int,
    repo_key: str,
    *,
    local_path: str = "",
    repo_url: str = "",
    base: str = "",
    target: str = "",
    ref: str = "",
    conn: sqlite3.Connection | None = None,
) -> int:
    """按 (project_id, repo_key) 注册或更新一个仓库绑定，返回 binding id。

    repo_key 是项目内稳定短名（backend/frontend/shared），由调用方约定；
    'default' 保留给旧单仓 project 的隐式绑定（迁移脚本创建）。
    """
    own = conn is None
    conn = conn or connect()
    init_db(conn)
    try:
        with conn:
            row = conn.execute(
                "SELECT id FROM repo_bindings WHERE project_id = ? AND repo_key = ?",
                (project_id, repo_key),
            ).fetchone()
            if row:
                bid = int(row["id"])
                conn.execute(
                    "UPDATE repo_bindings SET local_path=?, repo_url=?, base=?, target=?,"
                    " ref=? WHERE id=?",
                    (local_path, repo_url, base, target, ref, bid),
                )
                return bid
            cur = conn.execute(
                "INSERT INTO repo_bindings(project_id, repo_key, local_path, repo_url,"
                " base, target, ref, created_at) VALUES(?,?,?,?,?,?,?,?)",
                (project_id, repo_key, local_path, repo_url, base, target, ref, _now()),
            )
            return int(cur.lastrowid or 0)
    finally:
        if own:
            conn.close()


def list_repo_bindings(
    project_id: int, conn: sqlite3.Connection | None = None
) -> list[dict[str, Any]]:
    """列出某项目下所有仓库绑定（按 repo_key 排序，稳定）。"""
    own = conn is None
    conn = conn or connect()
    try:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM repo_bindings WHERE project_id = ? ORDER BY repo_key",
                (project_id,),
            )
        ]
    finally:
        if own:
            conn.close()


def get_repo_binding(
    binding_id: int, conn: sqlite3.Connection | None = None
) -> dict[str, Any] | None:
    own = conn is None
    conn = conn or connect()
    try:
        row = conn.execute("SELECT * FROM repo_bindings WHERE id = ?", (binding_id,)).fetchone()
        return dict(row) if row else None
    finally:
        if own:
            conn.close()


def repo_binding_map(project_id: int, conn: sqlite3.Connection | None = None) -> dict[str, int]:
    """repo_key → binding id 映射，供生成期把 repo_key 解析成 repo_binding_id。"""
    return {b["repo_key"]: int(b["id"]) for b in list_repo_bindings(project_id, conn)}


# ============================================================================
# 功能点
# ============================================================================
def replace_functional_points(  # noqa: C901, PLR0912 - 增量对账含新增/更新/复用/删除四分支
    pid: int,
    fps: list[FunctionalPoint],
    *,
    repo_binding_id: int | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, int]:
    """按 contract_id 增量对账功能点（Batch 6 D-4：消除先删后插的写放大）。

    语义与旧的「整批替换」一致：本次集合之外的行删除；但相同 contract_id
    且内容未变时**保留原行 id**——`cases.fp_id` 外键引用该 id，全量删插会让
    每次生成都 churn 一遍所有用例的 fp_id（几千条 UPDATE + 追溯链抖动）。

    返回统计：`functional_points` = 最终行数（兼容旧调用方），另有
    created/updated/reused/removed 明细供诊断。

    多仓改造（v1.7）：`repo_binding_id` 为 None 时以整项目为界（旧单仓零回归）；
    传入时删除/查询/插入都以仓为界，生成仓 B 不动仓 A 的行。
    """
    own = conn is None
    conn = conn or connect()
    try:
        # 按 fp_id 去重：相同 fp_id 即同一功能点（fp_extract 注释明确要求「落库静默合并」）。
        # 真实代码下 fp_id=md5(ftype|file_path|name)[:8] 存在生日碰撞，不去重会触发
        # functional_points(project_id, contract_id) 唯一约束导致整批生成失败。
        seen: set[str] = set()
        deduped: list[FunctionalPoint] = []
        for fp in fps:
            if fp.fp_id in seen:
                continue
            seen.add(fp.fp_id)
            deduped.append(fp)

        if repo_binding_id is None:
            old_rows = conn.execute(
                "SELECT * FROM functional_points WHERE project_id = ?", (pid,)
            ).fetchall()
        else:
            old_rows = conn.execute(
                "SELECT * FROM functional_points WHERE project_id = ? AND repo_binding_id = ?",
                (pid, repo_binding_id),
            ).fetchall()
        existing = {str(r["contract_id"]): dict(r) for r in old_rows if r["contract_id"]}

        stats = {"created": 0, "updated": 0, "reused": 0, "removed": 0}
        with conn:
            for fp in deduped:
                payload = (
                    fp.commit_ref,
                    fp.file_path,
                    fp.name,
                    fp.description,
                    fp.ftype,
                    fp.review_status,
                    fp.title,
                    fp.module,
                    fp.semantic,
                )
                old = existing.get(fp.fp_id)
                if old is None:
                    # payload 顺序：commit_ref, file_path, name, description, ftype,
                    # review_status, title, module, semantic；created_at/contract_id 插中间。
                    params: tuple[Any, ...] = (
                        pid,
                        *payload[:6],
                        _now(),
                        fp.fp_id,
                        *payload[6:],
                    )
                    if repo_binding_id is not None:
                        params = (*params, repo_binding_id)
                    conn.execute(
                        "INSERT INTO functional_points(project_id, commit_ref, file_path, name,"
                        " description, ftype, review_status, created_at, contract_id, title,"
                        " module, semantic"
                        f"{', repo_binding_id' if repo_binding_id is not None else ''})"
                        f" VALUES({','.join('?' for _ in params)})",
                        params,
                    )
                    stats["created"] += 1
                    continue
                old_payload = tuple(
                    _text(old.get(k))
                    for k in (
                        "commit_ref",
                        "file_path",
                        "name",
                        "description",
                        "ftype",
                        "review_status",
                        "title",
                        "module",
                        "semantic",
                    )
                )
                if old_payload != tuple(_text(v) for v in payload):
                    conn.execute(
                        "UPDATE functional_points SET commit_ref=?, file_path=?, name=?,"
                        " description=?, ftype=?, review_status=?, title=?, module=?, semantic=?"
                        " WHERE id=?",
                        (*payload, old["id"]),
                    )
                    stats["updated"] += 1
                else:
                    stats["reused"] += 1

            incoming = {fp.fp_id for fp in deduped}
            for cid, old in existing.items():
                if cid not in incoming:
                    conn.execute("DELETE FROM functional_points WHERE id=?", (old["id"],))
                    stats["removed"] += 1
        return {"functional_points": len(deduped), **stats}
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
def replace_test_points(  # noqa: C901, PLR0912 - 增量对账含新增/更新/复用/删除四分支
    pid: int,
    tps: list[TestPoint],
    *,
    repo_binding_id: int | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, int]:
    """按 tp_id 增量对账测试点（Batch 6 D-4：消除先删后插的写放大）。

    与 FP 同理：本次集合之外的行删除；相同 tp_id 且内容未变则保留原行 id，
    避免每次生成把整张 test_points 删插一遍（写放大 + 自增 id churn）。

    返回统计：`test_points` = 最终行数（兼容旧调用方），另有
    created/updated/reused/removed 明细。

    多仓改造（v1.7）：`repo_binding_id` 缺省 None 以整项目为界（零回归）；
    传入时查询/删除/插入都以仓为界。
    """
    own = conn is None
    conn = conn or connect()
    try:
        # 按 tp_id 去重：相同 tp_id 即同一测试点（与 functional_points 同理）。
        # tp_id=md5(fp_id|category|area|method|dimension|ordinal)[:8] 在真实代码下存在生日碰撞，
        # 不去重会触发 test_points(project_id, tp_id) 唯一约束；1:1 用例生成（tc_no==tp_id）
        # 也会连带撞 cases(project_id, tp_id)，故在此一次性去重最稳妥。
        seen: set[str] = set()
        deduped: list[TestPoint] = []
        for tp in tps:
            if tp.tp_id in seen:
                continue
            seen.add(tp.tp_id)
            deduped.append(tp)

        if repo_binding_id is None:
            old_rows = conn.execute(
                "SELECT * FROM test_points WHERE project_id = ?", (pid,)
            ).fetchall()
        else:
            old_rows = conn.execute(
                "SELECT * FROM test_points WHERE project_id = ? AND repo_binding_id = ?",
                (pid, repo_binding_id),
            ).fetchall()
        existing = {str(r["tp_id"]): dict(r) for r in old_rows if r["tp_id"]}

        # 参与「内容是否变化」的列（顺序即 SQL 参数顺序，created_at/id 不参与）。
        fields = (
            "fp_contract_id",
            "category",
            "module",
            "semantic",
            "title",
            "source",
            "method",
            "area",
            "expect",
            "dimension",
            "tag",
            "review_status",
        )
        set_clause = ", ".join(f"{f}=?" for f in fields)

        def _payload(tp: TestPoint) -> tuple[Any, ...]:
            return (
                *(_text(getattr(tp, f)) for f in fields),
                _dumps(tp.evidence),
                float(tp.confidence),
                _text(tp.origin),
                1 if tp.unverified else 0,
                _text(tp.channel),
                _text(tp.unverified_reason),
                _text(tp.verify_layer),
            )

        def _old_payload(old: dict[str, Any]) -> tuple[Any, ...]:
            return (
                *(_text(old.get(f)) for f in fields),
                _dumps(_loads(old.get("evidence"), [])),
                float(old.get("confidence") or 0),
                _text(old.get("origin")),
                int(old.get("unverified") or 0),
                _text(old.get("channel")),
                _text(old.get("unverified_reason")),
                _text(old.get("verify_layer")),
            )

        stats = {"created": 0, "updated": 0, "reused": 0, "removed": 0}
        with conn:
            for tp in deduped:
                payload = _payload(tp)
                old = existing.get(tp.tp_id)
                if old is None:
                    cols = (
                        "project_id, tp_id, "
                        + ", ".join(fields)
                        + ", created_at, evidence, confidence, origin, unverified, channel,"
                        " unverified_reason, verify_layer"
                    )
                    # 列顺序：project_id, tp_id, 12 个内容列, created_at, 6 个扩展列
                    # payload = 12 内容列 + 6 扩展列；created_at 插在两组中间。
                    head = (pid, tp.tp_id, *payload[: len(fields)])
                    tail = payload[len(fields) :]
                    params = (*head, _now(), *tail)
                    if repo_binding_id is not None:
                        params = (*params, repo_binding_id)
                    conn.execute(
                        f"INSERT INTO test_points({cols}"
                        f"{', repo_binding_id' if repo_binding_id is not None else ''})"
                        f" VALUES({','.join('?' for _ in params)})",
                        params,
                    )
                    stats["created"] += 1
                    continue
                if _old_payload(old) != payload:
                    conn.execute(
                        f"UPDATE test_points SET {set_clause}, evidence=?, confidence=?,"
                        " origin=?, unverified=?, channel=?, unverified_reason=?, verify_layer=?"
                        " WHERE id=?",
                        (*payload, old["id"]),
                    )
                    stats["updated"] += 1
                else:
                    stats["reused"] += 1

            incoming = {tp.tp_id for tp in deduped}
            for tid, old in existing.items():
                if tid not in incoming:
                    conn.execute("DELETE FROM test_points WHERE id=?", (old["id"],))
                    stats["removed"] += 1
        return {"test_points": len(deduped), **stats}
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


def reconcile_cases(  # noqa: C901, PLR0912 - 多仓分支与单仓分支并存，显式列出比合并更可审计
    pid: int,
    specs: list[CaseSpec],
    *,
    repo_binding_id: int | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, int]:
    """按 `tp_id` 对账生成用例，幂等。

    返回统计：created / updated / reused / obsolete。
    - 新出现 → inserted（status=generated）
    - 内容变化 → updated（version+1）
    - 未变 → reused
    - 旧用例的 tp_id 已不在本次集合 → obsolete（保留可见、不进执行）

    传入 `specs` 内部若出现重复 tp_id，说明上游编号算法发生了碰撞，**直接失败**：
    静默插入两行会绕开以 tp_id 为键的对账，重复行将永远无法被更新或作废。

    多仓改造（v1.7）：`repo_binding_id` 缺省 None 走旧逻辑（零回归）；传入时
    existing 查询 / 作废判定 / INSERT 归属都以仓为界——生成仓 B 不会把仓 A 的
    用例误标 obsolete。
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
        stats = {"created": 0, "updated": 0, "reused": 0, "obsolete": 0, "revived": 0}
        with conn:
            if repo_binding_id is None:
                existing_rows = conn.execute(
                    "SELECT * FROM cases WHERE project_id = ? AND tp_id IS NOT NULL", (pid,)
                )
            else:
                existing_rows = conn.execute(
                    "SELECT * FROM cases WHERE project_id = ? AND repo_binding_id = ?"
                    " AND tp_id IS NOT NULL",
                    (pid, repo_binding_id),
                )
            existing = {str(r["tp_id"]): dict(r) for r in existing_rows}
            incoming: set[str] = set()
            for spec in specs:
                incoming.add(spec.tp_id)
                old = existing.get(spec.tp_id)
                payload = _case_payload(spec)
                if old is None:
                    if repo_binding_id is None:
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
                    else:
                        conn.execute(
                            "INSERT INTO cases(project_id, fp_id, title, steps, ctype,"
                            " review_status, status, created_at, tp_id, fp_contract_id, tc_no,"
                            " module, case_type, priority, precondition, doc_steps, version,"
                            " test_type, repo_binding_id)"
                            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                                repo_binding_id,
                            ),
                        )
                    stats["created"] += 1
                    continue
                if _case_changed(old, payload):
                    # B8：内容变化且旧行已作废，说明它「复活且内容有更新」——
                    # 除 updated 外也要计 revived，否则复活数少算、报告与 active 集合对不上。
                    was_obsolete = str(old.get("status") or "") == "obsolete"
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
                    if was_obsolete:
                        stats["revived"] += 1
                else:
                    stats["reused"] += 1
                    # 内容未变 → reused，但**已作废的行必须复活**：作废是「上次没再产出它」的
                    # 结果，本次又产出了说明它仍有效。不复活会让产物有 N 条、库里 active 少几条
                    # （典型触发：先跑全量把旧行全标 obsolete，再用收窄子集重跑同一批 tp_id），
                    # 而且完全静默——报告与执行只认 active，三口径对不上却查不出原因。
                    if str(old.get("status") or "") == "obsolete":
                        conn.execute(
                            "UPDATE cases SET status='generated', fp_id=?, fp_contract_id=?,"
                            " tc_no=?, module=? WHERE id=?",
                            (
                                spec.fp_row_id,
                                spec.fp_contract_id,
                                spec.tc_no,
                                spec.module,
                                old["id"],
                            ),
                        )
                        stats["revived"] += 1

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
    """参与「内容是否变化」比对的列。

    B8：稳定的业务追溯列（fp_contract_id / tc_no / module）此前不在 payload 里，
    导致 FP 重排后旧行追溯链错乱却始终走 reused、永不自愈。纳入比对后，
    只要这些列变化就走 updated 分支刷新。

    **不含 `fp_id`（数据库自增行 id）**：它每次生成都会变（FP 行被删了重建），
    纳入比对会让内容未变的用例也走 updated —— 破坏对账幂等（第二次运行全部
    updated 而非 reused）。`fp_id` 仍在 UPDATE 语句里被刷新，只是不作为「内容变化」依据。
    """
    return {
        "title": spec.title,
        "steps": spec.steps,
        "doc_steps": spec.doc_steps,
        "priority": spec.priority,
        "precondition": spec.precondition,
        "case_type": spec.case_type,
        "fp_contract_id": spec.fp_contract_id,
        "tc_no": spec.tc_no,
        "module": spec.module,
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


def list_cases_page(
    pid: int,
    *,
    offset: int = 0,
    limit: int = 200,
    include_obsolete: bool = False,
    conn: sqlite3.Connection | None = None,
) -> tuple[int, list[dict[str, Any]]]:
    """分页读取用例（Batch 4 C-1）：返回 (总数, 当前页)。

    与 `list_cases` 同一过滤/反序列化口径，但在 SQL 层 LIMIT/OFFSET——大仓（4000+
    条、每条带 steps/doc_steps 大字段）不再整表读出再切片。默认排除 obsolete，
    与 `list_cases` 的默认口径一致。`offset/limit` 由路由层做上界钳制。
    """
    own = conn is None
    conn = conn or connect()
    try:
        where = "project_id = ?"
        params: list[Any] = [pid]
        if not include_obsolete:
            where += " AND status != ?"
            params.append(CaseLifecycleStatus.OBSOLETE.value)
        total = int(
            conn.execute(
                f"SELECT COUNT(*) AS n FROM cases WHERE {where}", tuple(params)
            ).fetchone()["n"]
        )
        rows = [
            dict(r)
            for r in conn.execute(
                f"SELECT * FROM cases WHERE {where} ORDER BY id LIMIT ? OFFSET ?",
                (*params, limit, offset),
            )
        ]
        for r in rows:
            r["steps"] = _loads(r.get("steps"), [])
            r["doc_steps"] = _loads(r.get("doc_steps"), [])
        return total, rows
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
                    # B8：只回填活跃行——obsolete 行不该拿到执行结论，否则同一 tp_id
                    # 的新旧行结论分裂（报告读 active、History 读 obsolete 会对不上）。
                    cursor = conn.execute(
                        "UPDATE cases SET last_result = ?"
                        " WHERE project_id = ? AND tp_id = ? AND status != 'obsolete'",
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


def _latest_quality(conn: sqlite3.Connection, pid: int) -> dict[str, Any]:
    """取该项目最近一次成功生成的 T5 质量快照（门禁 + 三 KPI + 三通道）。

    为什么不按「当前库里的测试点」重算：三个 KPI 的分母需要本次 hunk 行号、改动函数、
    被截断的引用方总数——这些只在生成期存在，落库后无从还原。快照是**事实记录**，
    重算会变成猜测（分母缺失时只能编）。库表缺失/无任务时返回空字典，报告照常出。
    """
    try:
        row = conn.execute(
            "SELECT result FROM gen_tasks WHERE project_id = ? AND result IS NOT NULL"
            " AND result != '' ORDER BY COALESCE(finished_at, created_at, '') DESC, rowid DESC"
            " LIMIT 1",
            (pid,),
        ).fetchone()
    except sqlite3.OperationalError:  # 极旧库没有 gen_tasks：降级为「无快照」
        return {}
    if row is None:
        return {}
    data = _loads(row["result"], {})
    quality = data.get("quality") if isinstance(data, dict) else None
    return dict(quality) if isinstance(quality, dict) else {}


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
            "SELECT id, contract_id, name, module, ftype, file_path, repo_binding_id"
            " FROM functional_points WHERE project_id = ? ORDER BY id",
            (pid,),
        )
        tps = rows(
            "SELECT tp_id, fp_contract_id, category, module, title, dimension, method,"
            " expect, source, tag, evidence, confidence, origin, unverified, channel,"
            " unverified_reason, verify_layer, repo_binding_id FROM test_points"
            " WHERE project_id = ? ORDER BY id",
            (pid,),
        )
        for t in tps:
            t["evidence"] = _loads(t.get("evidence"), [])
            t["unverified"] = int(t.get("unverified") or 0)
        cases = rows(
            "SELECT id, tp_id, fp_contract_id, module, title, case_type, priority, status,"
            " test_type, last_result, repo_binding_id FROM cases WHERE project_id = ? ORDER BY id",
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
            # T5：生成期算出的门禁/KPI 快照（无则空字典，报告降级但不出错）
            "quality": _latest_quality(conn, pid),
        }
    finally:
        if own:
            conn.close()


# ============================================================================
# 代码仓库配置（控制台「代码仓库管理」菜单）：CRUD + 连接测试结果留痕
# ============================================================================
def _mask_key(key: str) -> str:
    """凭证掩码：只留首尾可辨识片段，绝不回传明文。"""
    k = (key or "").strip()
    if not k:
        return ""
    if len(k) <= 10:
        return "••••"
    return f"{k[:5]}••••{k[-4:]}"


def _repo_row(row: sqlite3.Row) -> dict[str, Any]:
    keys = row.keys() if isinstance(row, sqlite3.Row) else []
    org = row["codeup_org_id"] if "codeup_org_id" in keys else ""
    key = row["codeup_access_key"] if "codeup_access_key" in keys else ""
    return {
        "id": int(row["id"]),
        "name": row["name"],
        "url": row["url"],
        "ref": row["ref"] or "",
        "remark": row["remark"] or "",
        "codeup_org_id": org or "",
        "codeup_has_key": bool((key or "").strip()),
        "codeup_key_masked": _mask_key(key),
        "last_test_ok": None if row["last_test_ok"] is None else bool(row["last_test_ok"]),
        "last_test_msg": row["last_test_msg"] or "",
        "last_test_at": row["last_test_at"] or "",
        "created_at": row["created_at"] or "",
        "updated_at": row["updated_at"] or "",
    }


def list_repo_configs(conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    own = conn is None
    conn = conn or connect()
    init_db(conn)
    try:
        rows_ = conn.execute("SELECT * FROM repo_configs ORDER BY id DESC").fetchall()
        return [_repo_row(r) for r in rows_]
    finally:
        if own:
            conn.close()


def get_repo_config(rid: int, conn: sqlite3.Connection | None = None) -> dict[str, Any] | None:
    own = conn is None
    conn = conn or connect()
    try:
        row = conn.execute("SELECT * FROM repo_configs WHERE id = ?", (rid,)).fetchone()
        return _repo_row(row) if row else None
    finally:
        if own:
            conn.close()


def find_repo_creds_by_url(
    url: str, conn: sqlite3.Connection | None = None
) -> dict[str, str] | None:
    """按仓库地址匹配已保存的 Codeup 只读凭证（仅内部使用，绝不回传给前端）。

    匹配规则：忽略大小写/尾部斜杠/`.git` 后缀。命中且已存 key 才返回
    ``{"org_id", "access_key"}``，否则 None。
    """
    if not (url or "").strip():
        return None

    def _norm(u: str) -> str:
        u = (u or "").strip().lower().rstrip("/")
        return u[:-4] if u.endswith(".git") else u

    own = conn is None
    conn = conn or connect()
    try:
        rows_ = conn.execute(
            "SELECT url, codeup_org_id, codeup_access_key FROM repo_configs"
        ).fetchall()
    finally:
        if own:
            conn.close()
    target = _norm(url)
    for r in rows_:
        if _norm(r["url"]) != target:
            continue
        key = (r["codeup_access_key"] or "").strip()
        if key:
            return {"org_id": (r["codeup_org_id"] or "").strip(), "access_key": key}
    return None


def create_repo_config(  # noqa: PLR0913 - 新增 codeup 凭证字段导致参数增多（部分插入语义天然多参）
    name: str,
    url: str,
    *,
    ref: str = "",
    remark: str = "",
    codeup_org_id: str = "",
    codeup_access_key: str = "",
    conn: sqlite3.Connection | None = None,
) -> int:
    """新增仓库配置；name 重复抛 ValidationError 由调用方转换。"""
    own = conn is None
    conn = conn or connect()
    init_db(conn)
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO repo_configs(name, url, ref, remark, codeup_org_id,"
                " codeup_access_key, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    name,
                    url,
                    ref,
                    remark,
                    codeup_org_id.strip(),
                    codeup_access_key.strip(),
                    _now(),
                    _now(),
                ),
            )
            return int(cur.lastrowid or 0)
    except sqlite3.IntegrityError as exc:
        raise ContractViolation(f"仓库名称已存在: {name}") from exc
    finally:
        if own:
            conn.close()


def export_repo_configs(conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    """导出全部仓库配置（共享/备份）：只含地址/分支/备注，不含连接测试留痕。"""
    rows_ = list_repo_configs(conn)
    out = []
    for r in rows_:
        out.append(
            {
                "name": r["name"],
                "url": r["url"],
                "ref": r["ref"],
                "remark": r["remark"],
            }
        )
    return out


def import_repo_configs(
    items: list[dict[str, Any]], conn: sqlite3.Connection | None = None
) -> dict[str, int]:
    """从共享 JSON 批量导入仓库配置（按 name 幂等 upsert）。

    返回 {imported, updated}。连接测试留痕不覆盖（保留本机真实状态）。
    """
    own = conn is None
    conn = conn or connect()
    init_db(conn)
    imported = updated = 0
    try:
        for it in items:
            name = (it.get("name") or "").strip()
            url = (it.get("url") or "").strip()
            if not name or not url:
                continue
            existing = conn.execute(
                "SELECT id FROM repo_configs WHERE name = ?", (name,)
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE repo_configs SET url=?, ref=?, remark=?, updated_at=? WHERE id=?",
                    (url, it.get("ref") or "", it.get("remark") or "", _now(), int(existing["id"])),
                )
                updated += 1
            else:
                conn.execute(
                    "INSERT INTO repo_configs(name, url, ref, remark, created_at, updated_at)"
                    " VALUES(?,?,?,?,?,?)",
                    (name, url, it.get("ref") or "", it.get("remark") or "", _now(), _now()),
                )
                imported += 1
        conn.commit()
    finally:
        if own:
            conn.close()
    return {"imported": imported, "updated": updated}


def seed_repo_configs(conn: sqlite3.Connection | None = None) -> int:
    """首次启动播种样例仓库配置（仅当表为空）。

    便于团队/多实例开箱即用与配置共享；已存在则幂等跳过。
    """
    rows_ = list_repo_configs(conn)
    if rows_:
        return 0
    samples = [
        {
            "name": "test-accel",
            "url": "C:/Users/EDY/WorkBuddy/testhub_platform/work/test-accel",
            "ref": "",
            "remark": "测试加速平台（八环门禁仓库）",
        },
        {
            "name": "testhub_platform",
            "url": "C:/Users/EDY/WorkBuddy/testhub_platform",
            "ref": "main",
            "remark": "testhub 主仓库（testCodeFast 并入目标）",
        },
    ]
    n = 0
    own = conn is None
    c = conn or connect()
    try:
        for s in samples:
            c.execute(
                "INSERT INTO repo_configs(name, url, ref, remark, created_at, updated_at)"
                " VALUES(?,?,?,?,?,?)",
                (s["name"], s["url"], s["ref"], s["remark"], _now(), _now()),
            )
            n += 1
        c.commit()
    finally:
        if own:
            c.close()
    return n


def update_repo_config(  # noqa: PLR0913 - 部分更新语义天然多参：None=不改
    rid: int,
    *,
    name: str | None = None,
    url: str | None = None,
    ref: str | None = None,
    remark: str | None = None,
    codeup_org_id: str | None = None,
    codeup_access_key: str | None = None,  # None=不改（保留原值）；""=清除；非空=覆盖
    conn: sqlite3.Connection | None = None,
) -> bool:
    """按需更新（None=不改）；改名重名抛 ContractViolation。返回是否命中行。"""
    own = conn is None
    conn = conn or connect()
    try:
        with conn:
            row = conn.execute("SELECT id FROM repo_configs WHERE id = ?", (rid,)).fetchone()
            if not row:
                return False
            if name is not None:
                conn.execute("UPDATE repo_configs SET name=? WHERE id=?", (name, rid))
            if url is not None:
                conn.execute("UPDATE repo_configs SET url=? WHERE id=?", (url, rid))
            if ref is not None:
                conn.execute("UPDATE repo_configs SET ref=? WHERE id=?", (ref, rid))
            if remark is not None:
                conn.execute("UPDATE repo_configs SET remark=? WHERE id=?", (remark, rid))
            if codeup_org_id is not None:
                conn.execute(
                    "UPDATE repo_configs SET codeup_org_id=? WHERE id=?",
                    (codeup_org_id.strip(), rid),
                )
            if codeup_access_key is not None:
                conn.execute(
                    "UPDATE repo_configs SET codeup_access_key=? WHERE id=?",
                    (codeup_access_key.strip(), rid),
                )
            conn.execute("UPDATE repo_configs SET updated_at=? WHERE id=?", (_now(), rid))
            return True
    except sqlite3.IntegrityError as exc:
        raise ContractViolation(f"仓库名称已存在: {name}") from exc
    finally:
        if own:
            conn.close()


def delete_repo_config(rid: int, conn: sqlite3.Connection | None = None) -> bool:
    own = conn is None
    conn = conn or connect()
    try:
        with conn:
            cur = conn.execute("DELETE FROM repo_configs WHERE id = ?", (rid,))
            return bool(cur.rowcount)
    finally:
        if own:
            conn.close()


def record_repo_test(
    rid: int,
    ok: bool,
    msg: str,
    conn: sqlite3.Connection | None = None,
) -> None:
    """回写连接测试结果（列表页展示最近一次状态）。"""
    own = conn is None
    conn = conn or connect()
    try:
        with conn:
            conn.execute(
                "UPDATE repo_configs SET last_test_ok=?, last_test_msg=?, last_test_at=?,"
                " updated_at=? WHERE id=?",
                (1 if ok else 0, msg[:400], _now(), _now(), rid),
            )
    finally:
        if own:
            conn.close()


# ============================================================================
# LLM 厂商配置（多 provider 独立 key）：CRUD + 连接测试留痕 + 引擎取用
# ============================================================================
def _llm_provider_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "name": row["name"],
        "base_url": row["base_url"],
        # 安全：api_key 只在写入/连接测试时使用，**列表/详情接口返回脱敏预览**，绝不回显明文
        "api_key_masked": ("*" * 6 + (row["api_key"][-4:] if row["api_key"] else ""))
        if row["api_key"]
        else "",
        "api_key_set": bool(row["api_key"]),
        "models": row["models"] or "",
        "is_default": bool(row["is_default"]),
        "priority": int(row["priority"] or 0),
        "last_test_ok": None if row["last_test_ok"] is None else bool(row["last_test_ok"]),
        "last_test_msg": row["last_test_msg"] or "",
        "last_test_at": row["last_test_at"] or "",
        "created_at": row["created_at"] or "",
        "updated_at": row["updated_at"] or "",
    }


def list_llm_providers(conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    own = conn is None
    conn = conn or connect()
    init_db(conn)
    try:
        rows_ = conn.execute(
            "SELECT * FROM llm_providers ORDER BY is_default DESC, priority ASC, id DESC"
        ).fetchall()
        return [_llm_provider_row(r) for r in rows_]
    finally:
        if own:
            conn.close()


def get_llm_provider(pid: int, conn: sqlite3.Connection | None = None) -> dict[str, Any] | None:
    own = conn is None
    conn = conn or connect()
    try:
        row = conn.execute("SELECT * FROM llm_providers WHERE id = ?", (pid,)).fetchone()
        return _llm_provider_row(row) if row else None
    finally:
        if own:
            conn.close()


def get_llm_provider_secret(
    pid: int, conn: sqlite3.Connection | None = None
) -> dict[str, Any] | None:
    """取明文凭证（仅连接测试/引擎调用内部使用，**绝不**经 API 返回给前端）。"""
    own = conn is None
    conn = conn or connect()
    try:
        row = conn.execute(
            "SELECT base_url, api_key, models FROM llm_providers WHERE id = ?", (pid,)
        ).fetchone()
        if not row:
            return None
        return {
            "base_url": row["base_url"],
            "api_key": row["api_key"] or "",
            "models": row["models"] or "",
        }
    finally:
        if own:
            conn.close()


def create_llm_provider(  # noqa: PLR0913
    name: str,
    base_url: str,
    *,
    api_key: str = "",
    models: str = "",
    is_default: bool = False,
    priority: int = 0,
    conn: sqlite3.Connection | None = None,
) -> int:
    """新增厂商配置；name 重复抛 ContractViolation。"""
    own = conn is None
    conn = conn or connect()
    init_db(conn)
    try:
        with conn:
            if is_default:
                conn.execute("UPDATE llm_providers SET is_default=0 WHERE is_default=1")
            cur = conn.execute(
                "INSERT INTO llm_providers(name, base_url, api_key, models, is_default,"
                " priority, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    name,
                    base_url,
                    api_key,
                    models,
                    1 if is_default else 0,
                    int(priority),
                    _now(),
                    _now(),
                ),
            )
            return int(cur.lastrowid or 0)
    except sqlite3.IntegrityError as exc:
        raise ContractViolation(f"厂商名称已存在: {name}") from exc
    finally:
        if own:
            conn.close()


def update_llm_provider(  # noqa: PLR0913, C901 - 部分更新语义天然多参：None=不改（保留原值）；分支多导致复杂度略超
    pid: int,
    *,
    name: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    models: str | None = None,
    is_default: bool | None = None,
    priority: int | None = None,
    conn: sqlite3.Connection | None = None,
) -> bool:
    """按需更新（None=不改）；改名重名抛 ContractViolation。返回是否命中行。"""
    own = conn is None
    conn = conn or connect()
    try:
        with conn:
            row = conn.execute("SELECT id FROM llm_providers WHERE id = ?", (pid,)).fetchone()
            if not row:
                return False
            if name is not None:
                conn.execute("UPDATE llm_providers SET name=? WHERE id=?", (name, pid))
            if base_url is not None:
                conn.execute("UPDATE llm_providers SET base_url=? WHERE id=?", (base_url, pid))
            if api_key is not None:
                conn.execute("UPDATE llm_providers SET api_key=? WHERE id=?", (api_key, pid))
            if models is not None:
                conn.execute("UPDATE llm_providers SET models=? WHERE id=?", (models, pid))
            if is_default is not None and is_default:
                conn.execute("UPDATE llm_providers SET is_default=0 WHERE is_default=1")
            if is_default is not None:
                conn.execute(
                    "UPDATE llm_providers SET is_default=? WHERE id=?",
                    (1 if is_default else 0, pid),
                )
            if priority is not None:
                conn.execute("UPDATE llm_providers SET priority=? WHERE id=?", (int(priority), pid))
            conn.execute("UPDATE llm_providers SET updated_at=? WHERE id=?", (_now(), pid))
            return True
    except sqlite3.IntegrityError as exc:
        raise ContractViolation(f"厂商名称已存在: {name}") from exc
    finally:
        if own:
            conn.close()


def delete_llm_provider(pid: int, conn: sqlite3.Connection | None = None) -> bool:
    own = conn is None
    conn = conn or connect()
    try:
        with conn:
            cur = conn.execute("DELETE FROM llm_providers WHERE id = ?", (pid,))
            return bool(cur.rowcount)
    finally:
        if own:
            conn.close()


def record_llm_provider_test(
    pid: int, ok: bool, msg: str, conn: sqlite3.Connection | None = None
) -> None:
    """回写连接测试结果。"""
    own = conn is None
    conn = conn or connect()
    try:
        with conn:
            conn.execute(
                "UPDATE llm_providers SET last_test_ok=?, last_test_msg=?, last_test_at=?,"
                " updated_at=? WHERE id=?",
                (1 if ok else 0, msg[:400], _now(), _now(), pid),
            )
    finally:
        if own:
            conn.close()


def load_llm_provider_specs(conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    """供引擎 `chat_with_fallback` 取用的厂商规格列表（按默认/优先级排序）。

    返回每项：{label, api_key, base_url, models:list[str]}。仅含 base_url 非空者；
    引擎会把它们追加在「主通道（.env 单 key）」之后作为**独立 key 的降级兜底**。
    """
    specs: list[dict[str, Any]] = []
    own = conn is None
    conn = conn or connect()
    init_db(conn)
    try:
        rows_ = conn.execute(
            "SELECT id, name, base_url, api_key, models FROM llm_providers"
            " WHERE base_url <> '' ORDER BY is_default DESC, priority ASC, id DESC"
        ).fetchall()
        for r in rows_:
            models = [m.strip() for m in (r["models"] or "").split(",") if m.strip()]
            specs.append(
                {
                    "label": f"db:{r['name']}",
                    "api_key": r["api_key"] or "",
                    "base_url": r["base_url"],
                    "models": models,
                }
            )
    finally:
        if own:
            conn.close()
    return specs


# ============================================================================
# 质量分析配套查询（迁移自 test-accel analytics 配套）+ 外部测试点导入
# ============================================================================
def list_change_logs(
    pid: int, *, limit: int = 100, conn: sqlite3.Connection | None = None
) -> list[dict[str, Any]]:
    """按项目列出历次增量对比/变更记录（change_log），最新在前。"""
    own = conn is None
    conn = conn or connect()
    try:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM change_log WHERE project_id = ? ORDER BY id DESC LIMIT ?",
                (pid, max(1, int(limit))),
            )
        ]
    finally:
        if own:
            conn.close()


def run_case_histories(
    pid: int, *, limit: int = 5000, conn: sqlite3.Connection | None = None
) -> list[dict[str, Any]]:
    """取**有结论**的执行留痕（pass/fail/error），供 flaky 跨批次比对。

    环境态（skipped/blocked_auth/structural_only 等）不算结论，不参与不稳定判定。
    """
    own = conn is None
    conn = conn or connect()
    try:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT case_id, tp_id, batch_id, status, created_at FROM runs"
                " WHERE project_id = ? AND status IN ('pass','fail','error')"
                " ORDER BY id LIMIT ?",
                (pid, max(1, int(limit))),
            )
        ]
    finally:
        if own:
            conn.close()


def filter_options(pid: int, *, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    """可用筛选维度与取值分布：告诉调用方能按什么筛、每档有多少条。

    cases 按非归档统计；test_points 单列（查看器筛选器用）。
    """
    own = conn is None
    conn = conn or connect()
    try:
        case_dims: dict[str, list[dict[str, Any]]] = {}
        for col in ("priority", "module", "case_type", "test_type", "ctype", "review_status"):
            rows = conn.execute(
                f"SELECT {col} AS k, COUNT(*) AS c FROM cases"
                f" WHERE project_id = ? AND status != 'obsolete' AND {col} != ''"
                f" GROUP BY {col} ORDER BY c DESC",
                (pid,),
            ).fetchall()
            case_dims[col] = [{"value": r["k"], "count": r["c"]} for r in rows]
        tp_dims: dict[str, list[dict[str, Any]]] = {}
        for col in ("category", "tag", "dimension"):
            rows = conn.execute(
                f"SELECT {col} AS k, COUNT(*) AS c FROM test_points"
                f" WHERE project_id = ? AND {col} != ''"
                f" GROUP BY {col} ORDER BY c DESC",
                (pid,),
            ).fetchall()
            tp_dims[col] = [{"value": r["k"], "count": r["c"]} for r in rows]
        total_cases = conn.execute(
            "SELECT COUNT(*) AS c FROM cases WHERE project_id = ? AND status != 'obsolete'",
            (pid,),
        ).fetchone()["c"]
        total_tps = conn.execute(
            "SELECT COUNT(*) AS c FROM test_points WHERE project_id = ?", (pid,)
        ).fetchone()["c"]
        return {
            "total_cases": total_cases,
            "total_test_points": total_tps,
            "case_dimensions": case_dims,
            "test_point_dimensions": tp_dims,
        }
    finally:
        if own:
            conn.close()


_TP_IMPORT_FIELDS = (
    # (目标列, 旧格式键, 新格式键) —— 兼容 test-accel 阶段2 产物 test_points_new.json
    ("tp_id", "id", "tp_id"),
    ("fp_contract_id", "fp_id", "fp_contract_id"),
    ("category", "tp_type", "category"),
    ("title", "name", "title"),
    ("module", "module", "module"),
    ("semantic", "semantic", "semantic"),
    ("source", "source", "source"),
    ("method", "method", "method"),
    ("area", "area", "area"),
    ("expect", "expect", "expect"),
    ("dimension", "dimension", "dimension"),
    ("tag", "tag", "tag"),
)


def import_test_points(
    pid: int,
    tps: list[dict[str, Any]],
    *,
    replace: bool = True,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """导入外部测试点（旧格式 ``id/fp_id/tp_type/name`` 与新格式键名均兼容）。

    ``replace=True`` 先清空该项目旧测试点（全量刷新幂等）；False 时按
    ``(project_id, tp_id)`` 覆盖更新（逐条 upsert，表无唯一约束故手动判重）。
    """
    own = conn is None
    conn = conn or connect()
    init_db(conn)
    try:
        if replace:
            conn.execute("DELETE FROM test_points WHERE project_id = ?", (pid,))
        by_category: dict[str, int] = {}
        imported = updated = 0
        with conn:  # 事务：全部成功或全部回滚
            for tp in tps:
                row = {
                    col: tp.get(old) if tp.get(old) is not None else tp.get(new)
                    for col, old, new in _TP_IMPORT_FIELDS
                }
                if not row["tp_id"]:
                    continue
                cat = row["category"] or "正常"
                row["category"] = cat
                existing = conn.execute(
                    "SELECT id FROM test_points WHERE project_id = ? AND tp_id = ?",
                    (pid, row["tp_id"]),
                ).fetchone()
                if existing and not replace:
                    conn.execute(
                        "UPDATE test_points SET fp_contract_id=?, category=?, module=?,"
                        " semantic=?, title=?, source=?, method=?, area=?, expect=?,"
                        " dimension=?, tag=? WHERE id = ?",
                        (
                            row["fp_contract_id"],
                            row["category"],
                            row["module"],
                            row["semantic"],
                            row["title"],
                            row["source"],
                            row["method"],
                            row["area"],
                            row["expect"],
                            row["dimension"],
                            row["tag"],
                            existing["id"],
                        ),
                    )
                    updated += 1
                else:
                    conn.execute(
                        "INSERT INTO test_points (project_id, tp_id, fp_contract_id,"
                        " category, module, semantic, title, source, method, area,"
                        " expect, dimension, tag, review_status)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            pid,
                            row["tp_id"],
                            row["fp_contract_id"],
                            row["category"],
                            row["module"],
                            row["semantic"],
                            row["title"],
                            row["source"],
                            row["method"],
                            row["area"],
                            row["expect"],
                            row["dimension"],
                            row["tag"],
                            "pending",
                        ),
                    )
                    imported += 1
                by_category[cat] = by_category.get(cat, 0) + 1
        return {"imported": imported, "updated": updated, "by_category": by_category}
    finally:
        if own:
            conn.close()


# ============================================================================
# 文件中文描述缓存（扫描增强：describe 命中复用，避免重复烧 LLM）
# ============================================================================
def get_file_descriptions(
    repo_url: str, paths: list[str], *, conn: sqlite3.Connection | None = None
) -> dict[str, dict[str, Any]]:
    """按 (repo_url, file_path) 批量取缓存；返回 {path: {description, symbols_sig}}。"""
    own = conn is None
    conn = conn or connect()
    try:
        if not paths:
            return {}
        placeholders = ",".join("?" for _ in paths)
        rows = conn.execute(
            "SELECT file_path, description, symbols_sig FROM file_descriptions"
            f" WHERE repo_url = ? AND file_path IN ({placeholders})",
            (repo_url, *paths),
        ).fetchall()
        return {
            r["file_path"]: {
                "description": r["description"],
                "symbols_sig": r["symbols_sig"],
            }
            for r in rows
        }
    finally:
        if own:
            conn.close()


def list_file_descriptions(
    repo_url: str, *, prefix: str = "", conn: sqlite3.Connection | None = None
) -> list[dict[str, Any]]:
    """按命名空间列出描述缓存；prefix 可筛出特定来源（如 runtime: 扫描模块）。

    复用既有 file_descriptions 表，不新增模块实体表；substr 精确匹配前缀，
    避免 LIKE 的通配符语义误伤。
    """
    own = conn is None
    conn = conn or connect()
    try:
        if prefix:
            rows = conn.execute(
                "SELECT file_path, description, symbols_sig, model, updated_at"
                " FROM file_descriptions WHERE repo_url = ?"
                " AND substr(file_path, 1, ?) = ? ORDER BY file_path",
                (repo_url, len(prefix), prefix),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT file_path, description, symbols_sig, model, updated_at"
                " FROM file_descriptions WHERE repo_url = ? ORDER BY file_path",
                (repo_url,),
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        if own:
            conn.close()


def upsert_file_description(
    repo_url: str,
    path: str,
    symbols_sig: str,
    description: str,
    model: str,
) -> None:
    """写回/覆盖单条描述缓存（同 (repo_url, file_path) 幂等 upsert）。"""
    conn = connect()
    try:
        with conn:
            conn.execute(
                "INSERT INTO file_descriptions(repo_url, file_path, symbols_sig, description,"
                " model, updated_at) VALUES(?,?,?,?,?,?)"
                " ON CONFLICT(repo_url, file_path) DO UPDATE SET"
                " symbols_sig=excluded.symbols_sig, description=excluded.description,"
                " model=excluded.model, updated_at=excluded.updated_at",
                (repo_url, path, symbols_sig, description, model, _now()),
            )
    finally:
        conn.close()


def clear_file_descriptions(repo_url: str = "", *, conn: sqlite3.Connection | None = None) -> int:
    """清空描述缓存。repo_url 为空 = 清空全部；否则只清该仓库。返回删除行数。"""
    own = conn is None
    conn = conn or connect()
    try:
        if repo_url:
            cur = conn.execute("DELETE FROM file_descriptions WHERE repo_url = ?", (repo_url,))
        else:
            cur = conn.execute("DELETE FROM file_descriptions")
        conn.commit()
        return int(cur.rowcount or 0)
    finally:
        if own:
            conn.close()
