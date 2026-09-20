"""生成任务异步化（P5 服务化骨架，仅「生成测试用例」环节）。

设计取舍（骨架级，可独立运行、无需外部依赖）：
- **进程内 `ThreadPoolExecutor`** 作为后台 worker：不引入 Redis/ARQ 等 broker，保持单机可跑；
  生产升级路径为「broker + 多 consumer」（见 服务化改造分析.md §8）。
- 任务状态落库（`gen_tasks` 表），**每个写操作独立连接**（`connect()`/`close()`），
  避免跨线程共享 SQLite 连接（SQLite 连接默认绑定创建线程）。
- 状态机：`pending → running → success / failed / cancelled`；超时与重试留给生产队列。
- **幂等**：同一 `idempotency_key` 重复提交，若既有任务仍 `pending/running` 则返回既有 `task_id`。
- **范围红线**：本模块只服务于「生成用例」，绝不触发 `stage_execute`（执行环节）。
  调用方若要求执行，应在 API 层被拒（422），不应落入此处。
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from enum import Enum
from typing import Any

from core.db import connect, init_db
from core.log import get_logger, log_extra


log = get_logger(__name__)

DEFAULT_MAX_WORKERS = int(os.environ.get("GEN_MAX_WORKERS") or "4")

# 阶段 → 进度占比（单调映射，用于任务轮询的进度条）。execute 阶段不在生成范围，故不列。
_STAGE_FRACTION: dict[str, float] = {
    "pull": 0.05,
    "scan": 0.18,
    "fp_extract": 0.30,
    "diff_tag": 0.38,
    "auth_scan": 0.45,
    "tp_expand": 0.55,
    "semantic_enrich": 0.65,
    "prd_ingest": 0.72,
    "llm_design": 0.82,
    "runtime_ui": 0.78,
    "case_gen": 0.90,
    "persist": 0.97,
    "done": 1.0,
}


class TaskState(Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class GenerationTask:
    """内存态任务对象；与 `gen_tasks` 表一一对应。"""

    __slots__ = (
        "created_at",
        "error",
        "finished_at",
        "idempotency_key",
        "kind",
        "progress",
        "project_id",
        "request",
        "result",
        "stage",
        "started_at",
        "state",
        "task_id",
        "updated_at",
    )

    def __init__(  # noqa: PLR0913
        self,
        task_id: str,
        *,
        project_id: int | None = None,
        kind: str = "",
        idempotency_key: str = "",
        state: str = TaskState.PENDING.value,
        progress: float = 0.0,
        stage: str = "",
        request: str = "",
        result: str = "",
        error: str = "",
        created_at: str = "",
        started_at: str = "",
        finished_at: str = "",
        updated_at: str = "",
    ) -> None:
        self.task_id = task_id
        self.project_id = project_id
        self.kind = kind
        self.idempotency_key = idempotency_key
        self.state = state
        self.progress = progress
        self.stage = stage
        self.request = request
        self.result = result
        self.error = error
        self.created_at = created_at
        self.started_at = started_at
        self.finished_at = finished_at
        self.updated_at = updated_at

    def to_dict(self) -> dict[str, Any]:
        parsed_result: Any = json.loads(self.result) if self.result else None
        return {
            "task_id": self.task_id,
            "project_id": self.project_id,
            "kind": self.kind,
            "state": self.state,
            "progress": round(self.progress, 3),
            "stage": self.stage,
            "request": (json.loads(self.request) if self.request else None),
            "result": parsed_result,
            "error": self.error or None,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "updated_at": self.updated_at,
        }


class TaskStore:
    """`gen_tasks` 表读写；每操作独立连接，线程安全。"""

    def create(
        self,
        *,
        kind: str,
        project_id: int | None = None,
        idempotency_key: str = "",
        request: str = "",
    ) -> GenerationTask:
        task_id = f"gen-{uuid.uuid4().hex[:12]}"
        now = _now()
        conn = connect()
        try:
            init_db(conn)
            conn.execute(
                "INSERT INTO gen_tasks("
                "task_id, project_id, kind, idempotency_key, state, progress, stage,"
                "request, created_at, updated_at) "
                "VALUES(?,?,?,?,?,0,'',?,?,?)",
                (
                    task_id,
                    project_id,
                    kind,
                    idempotency_key,
                    TaskState.PENDING.value,
                    request,
                    now,
                    now,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return GenerationTask(
            task_id,
            project_id=project_id,
            kind=kind,
            idempotency_key=idempotency_key,
            state=TaskState.PENDING.value,
            request=request,
            created_at=now,
            updated_at=now,
        )

    def get(self, task_id: str) -> GenerationTask | None:
        row = self._fetch_one("task_id = ?", (task_id,))
        return row

    def find_by_idempotency(self, key: str) -> GenerationTask | None:
        if not key:
            return None
        return self._fetch_one("idempotency_key = ? ORDER BY rowid DESC LIMIT 1", (key,))

    def update(self, task_id: str, **fields: Any) -> None:
        """增量更新任务字段；`fields` 仅含 schema 列名。"""
        allowed = {
            "project_id",
            "kind",
            "idempotency_key",
            "state",
            "progress",
            "stage",
            "request",
            "result",
            "error",
            "started_at",
            "finished_at",
        }
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return
        sets["updated_at"] = _now()
        cols = ", ".join(f"{c} = ?" for c in sets)
        vals = [*list(sets.values()), task_id]
        conn = connect()
        try:
            conn.execute(f"UPDATE gen_tasks SET {cols} WHERE task_id = ?", vals)
            conn.commit()
        finally:
            conn.close()

    def cancel(self, task_id: str) -> bool:
        """仅 `pending/running` 可取消；已终态不可变。返回是否成功置为 cancelled。"""
        task = self.get(task_id)
        if task is None:
            return False
        if task.state in (
            TaskState.SUCCESS.value,
            TaskState.FAILED.value,
            TaskState.CANCELLED.value,
        ):
            return False
        self.update(task_id, state=TaskState.CANCELLED.value, finished_at=_now())
        return True

    def list_recent(self, limit: int = 10) -> list[GenerationTask]:
        conn = connect()
        try:
            init_db(conn)
            rows = conn.execute(
                "SELECT * FROM gen_tasks ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        finally:
            conn.close()
        return [self._row_to_task(r) for r in rows]

    def counts_by_state(self) -> dict[str, int]:
        conn = connect()
        try:
            init_db(conn)
            rows = conn.execute(
                "SELECT state, COUNT(*) AS n FROM gen_tasks GROUP BY state"
            ).fetchall()
        finally:
            conn.close()
        return {r["state"]: int(r["n"]) for r in rows}

    # ---- 内部 ----
    def _fetch_one(self, where: str, params: tuple) -> GenerationTask | None:
        conn = connect()
        try:
            init_db(conn)
            row = conn.execute(f"SELECT * FROM gen_tasks WHERE {where}", params).fetchone()
        finally:
            conn.close()
        return self._row_to_task(row) if row else None

    @staticmethod
    def _row_to_task(row: Any) -> GenerationTask:
        return GenerationTask(
            task_id=row["task_id"],
            project_id=row["project_id"],
            kind=row["kind"] or "",
            idempotency_key=row["idempotency_key"] or "",
            state=row["state"] or TaskState.PENDING.value,
            progress=float(row["progress"] or 0.0),
            stage=row["stage"] or "",
            request=row["request"] or "",
            result=row["result"] or "",
            error=row["error"] or "",
            created_at=row["created_at"] or "",
            started_at=row["started_at"] or "",
            finished_at=row["finished_at"] or "",
            updated_at=row["updated_at"] or "",
        )


class BackgroundExecutor:
    """进程内后台执行器：把生成作业丢进线程池，自动管理任务状态机。

    作业函数签名：`Callable[[], dict]`——返回可 JSON 化的结果字典，或抛出异常。
    执行器负责：`pending→running`、成功写 `result`+`success`、失败写 `error`+`failed`。
    """

    def __init__(self, store: TaskStore, max_workers: int = DEFAULT_MAX_WORKERS) -> None:
        self._store = store
        self._pool = ThreadPoolExecutor(max_workers=max(1, max_workers))

    def submit(self, task_id: str, job: Callable[[], dict[str, Any]]) -> None:
        """提交作业到线程池（非阻塞）。"""

        def _run() -> None:
            self._store.update(task_id, state=TaskState.RUNNING.value, started_at=_now())
            try:
                result = job()
                self._store.update(
                    task_id,
                    state=TaskState.SUCCESS.value,
                    result=json.dumps(result, ensure_ascii=False),
                    progress=1.0,
                    stage="done",
                    finished_at=_now(),
                )
            except Exception as exc:  # 任务失败须兜住并如实落库，绝不冒泡到 worker
                log.error(
                    "生成任务失败",
                    extra=log_extra(task_id=task_id, err=type(exc).__name__),
                    exc_info=exc,
                )
                self._store.update(
                    task_id,
                    state=TaskState.FAILED.value,
                    error=str(exc)[:500],
                    finished_at=_now(),
                )

        self._pool.submit(_run)

    def shutdown(self, wait: bool = True) -> None:
        self._pool.shutdown(wait=wait)


def stage_fraction(stage: str, best: float) -> float:
    """把进度回调的阶段名映射为单调不减的进度占比。"""
    frac = _STAGE_FRACTION.get(stage)
    if frac is None:
        frac = min(1.0, best + 0.05)
    return max(best, min(1.0, frac))
