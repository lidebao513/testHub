"""M12 / M19 回归：取消竞态与取消标志快路径。

M12：`pending→running` 必须是条件更新（compare-and-set），排队期间被取消的任务
      **绝不**能被覆盖回 `running`，更不允许作业跑完写 `success`。
M19：`is_cancelled()` 对「已取消」走进程内快路径（零 IO），未取消仍回查 DB
      以保证多进程 / 重启后的取消语义正确；终态后标志被清理，不长期驻留。
"""

from __future__ import annotations

import threading
import time

from service import tasks as task_engine


def _wait_state(store, task_id: str, expect: set[str], timeout: float = 5.0) -> str:
    """轮询等待任务进入期望终态之一，返回最终 state。"""
    deadline = time.monotonic() + timeout
    state = ""
    while time.monotonic() < deadline:
        task = store.get(task_id)
        state = task.state if task else ""
        if state in expect:
            return state
        time.sleep(0.05)
    return state


# ---------------------------------------------------------------------------
# M12：try_start 条件更新
# ---------------------------------------------------------------------------
def test_try_start_rejects_cancelled_task(fresh_db):
    """已取消的任务不得被置回 running（M12 核心回归）。"""
    store = task_engine.TaskStore()
    task = store.create(kind="generate")
    assert store.cancel(task.task_id) is True

    assert store.try_start(task.task_id) is False
    assert store.get(task.task_id).state == "cancelled"
    assert store.get(task.task_id).started_at == ""


def test_try_start_moves_pending_to_running_once(fresh_db):
    """pending → running 只成功一次，重复抢占返回 False（防重复执行）。"""
    store = task_engine.TaskStore()
    task = store.create(kind="generate")

    assert store.try_start(task.task_id) is True
    assert store.get(task.task_id).state == "running"
    assert store.get(task.task_id).started_at != ""
    assert store.try_start(task.task_id) is False


def test_try_start_on_terminal_task_is_rejected(fresh_db):
    """已成功的任务不会被重新拉起。"""
    store = task_engine.TaskStore()
    task = store.create(kind="generate")
    store.update(task.task_id, state="success")

    assert store.try_start(task.task_id) is False
    assert store.get(task.task_id).state == "success"


def test_executor_skips_job_cancelled_before_start(fresh_db):
    """排队期间被取消：作业函数零调用，终态恒为 cancelled。"""
    store = task_engine.TaskStore()
    executor = task_engine.BackgroundExecutor(store, max_workers=1)
    task = store.create(kind="generate")
    assert store.cancel(task.task_id) is True

    called = threading.Event()

    def job():
        called.set()
        return {"ok": True}

    executor.submit(task.task_id, job)
    final = _wait_state(store, task.task_id, {"cancelled", "success", "failed"}, timeout=3)
    executor.shutdown(wait=True)

    assert final == "cancelled"
    assert not called.is_set(), "已被取消的任务不得执行作业体"


def test_executor_runs_normal_task_to_success(fresh_db):
    """正常路径回归：pending → running → success，result 落库。"""
    store = task_engine.TaskStore()
    executor = task_engine.BackgroundExecutor(store, max_workers=1)
    task = store.create(kind="generate")

    executor.submit(task.task_id, lambda: {"cases": 3})
    final = _wait_state(store, task.task_id, {"success", "failed"}, timeout=5)
    executor.shutdown(wait=True)

    assert final == "success"
    row = store.get(task.task_id)
    assert '"cases": 3' in row.result
    assert row.progress == 1.0
    assert row.stage == "done"


def test_executor_marks_cancelled_when_cancelled_during_run(fresh_db):
    """运行期取消：作业自检抛 TaskCancelledError → 终态 cancelled。"""
    store = task_engine.TaskStore()
    executor = task_engine.BackgroundExecutor(store, max_workers=1)
    task = store.create(kind="generate")

    def job():
        store.cancel(task.task_id)
        raise task_engine.TaskCancelledError(task.task_id)

    executor.submit(task.task_id, job)
    final = _wait_state(store, task.task_id, {"cancelled", "success", "failed"}, timeout=5)
    executor.shutdown(wait=True)

    assert final == "cancelled"
    assert "取消" in store.get(task.task_id).error


# ---------------------------------------------------------------------------
# M19：取消标志快路径
# ---------------------------------------------------------------------------
def test_is_cancelled_uses_memory_fast_path(fresh_db, monkeypatch):
    """cancel 之后 is_cancelled 不再查库（快路径零 IO）。"""
    store = task_engine.TaskStore()
    task = store.create(kind="generate")
    store.cancel(task.task_id)

    calls = {"n": 0}
    original_get = store.get

    def counting_get(task_id: str):
        calls["n"] += 1
        return original_get(task_id)

    monkeypatch.setattr(store, "get", counting_get)

    for _ in range(50):
        assert store.is_cancelled(task.task_id) is True

    assert calls["n"] == 0, "已取消任务的检查应命中内存标志，不应回查数据库"


def test_is_cancelled_reads_db_for_untouched_task(fresh_db):
    """未取消的任务必须回查 DB（多进程场景下取消可能来自其他进程）。"""
    store = task_engine.TaskStore()
    task = store.create(kind="generate")

    assert store.is_cancelled(task.task_id) is False
    # 模拟「另一进程」把任务取消：直接改库、不经过本实例的 cancel()
    store.update(task.task_id, state="cancelled")
    assert store.is_cancelled(task.task_id) is True


def test_forget_clears_memory_flag(fresh_db):
    """终态后清理标志，避免长期驻留。"""
    store = task_engine.TaskStore()
    task = store.create(kind="generate")
    store.cancel(task.task_id)
    assert store.is_cancelled(task.task_id) is True

    store.forget(task.task_id)
    assert task.task_id not in store._cancelled
    assert store.is_cancelled(task.task_id) is True  # 回退 DB 仍正确


def test_cancel_flag_cache_is_bounded(fresh_db, monkeypatch):
    """标志表有上限，超出按插入顺序淘汰最旧（防内存无界增长）。"""
    store = task_engine.TaskStore()
    monkeypatch.setattr(store, "_CANCEL_FLAG_LIMIT", 2)

    ids = []
    for _ in range(4):
        t = store.create(kind="generate")
        store.cancel(t.task_id)
        ids.append(t.task_id)

    assert len(store._cancelled) <= 2
    assert ids[-1] in store._cancelled
    assert ids[0] not in store._cancelled
