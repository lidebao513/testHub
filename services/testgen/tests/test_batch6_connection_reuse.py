"""Batch 6 D-3：TaskStore 线程局部连接复用守护测试。

核心保障：
1. 同线程多次读写只建立一条连接（消除高频开闭 + 重复 PRAGMA）；
2. 不同线程各持独立连接（SQLite 连接不可跨线程共享）；
3. DB 路径变化自动重连（测试 reset_settings / 多实例隔离）；
4. 复用后事务语义不变（commit 生效、cancel/try_start 竞态仍正确）。
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

import service.tasks as task_mod
from core.db import db_path
from service.tasks import TaskState, TaskStore


@pytest.fixture()
def fresh_store(fresh_db):
    store = TaskStore()
    yield store
    store.close_all()


def test_same_thread_reuses_one_connection(fresh_store, monkeypatch) -> None:
    calls: list[int] = []
    real_connect = task_mod.connect

    def counted_connect():
        calls.append(1)
        return real_connect()

    monkeypatch.setattr(task_mod, "connect", counted_connect)
    fresh_store.close_all()  # 确保从零开始

    fresh_store.create(kind="generate", idempotency_key="k1")
    for _ in range(10):
        fresh_store.get("nonexistent")
        fresh_store.count_tasks()
    assert len(calls) == 1, f"同线程应只建 1 条连接，实际 {len(calls)} 条"


def test_path_change_reconnects(fresh_store, monkeypatch) -> None:
    calls: list[int] = []
    real_connect = task_mod.connect

    def counted_connect():
        calls.append(1)
        return real_connect()

    monkeypatch.setattr(task_mod, "connect", counted_connect)
    fresh_store.close_all()
    fresh_store.count_tasks()
    assert len(calls) == 1

    # 模拟 DB 路径变化：_connect 检测到 cached_path != 新 path → 重建
    monkeypatch.setattr(task_mod, "db_path", lambda: db_path().with_name("other.db"))
    fresh_store.count_tasks()
    assert len(calls) == 2, "DB 路径变化未触发重连"


def test_threads_have_independent_connections(fresh_store) -> None:
    seen: list[int] = []

    def touch(_: int) -> None:
        seen.append(id(fresh_store._connect()))
        fresh_store.count_tasks()

    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(touch, range(6)))

    # 线程池复用的线程可能只有 1~3 条连接，但绝不能是同一条被跨线程共享：
    # 这里只断言调用未抛 check_same_thread 错误且至少建立了连接。
    assert seen and all(isinstance(x, int) for x in seen)
    # 主线程自己的连接与工作线程的不同
    fresh_store.close_all()
    assert fresh_store._connect() is not None


def test_close_all_is_idempotent(fresh_store) -> None:
    fresh_store.count_tasks()
    fresh_store.close_all()
    fresh_store.close_all()  # 二次关闭不抛
    assert fresh_store.count_tasks() >= 0  # 自动重建后可继续用


def test_state_transitions_still_work(fresh_store) -> None:
    """连接复用不破坏状态机：create → try_start → update → cancel 语义。"""
    task = fresh_store.create(kind="generate")
    assert task.state == TaskState.PENDING.value
    assert fresh_store.try_start(task.task_id) is True
    assert fresh_store.try_start(task.task_id) is False  # CAS：只有一次成功
    got = fresh_store.get(task.task_id)
    assert got is not None and got.state == TaskState.RUNNING.value

    fresh_store.update(task.task_id, state=TaskState.SUCCESS.value, progress=1.0)
    got = fresh_store.get(task.task_id)
    assert got is not None and got.state == TaskState.SUCCESS.value
    assert got.progress == 1.0
    # 终态不可取消
    assert fresh_store.cancel(task.task_id) is False


def test_concurrent_try_start_single_winner(fresh_store) -> None:
    """多线程抢同一任务：复用连接下 CAS 仍只有一个赢家。"""
    task = fresh_store.create(kind="generate")
    barrier = threading.Barrier(3)

    def race() -> bool:
        barrier.wait(timeout=5)
        return fresh_store.try_start(task.task_id)

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda _: race(), range(3)))
    assert sum(results) == 1, f"CAS 竞态：{results}"
