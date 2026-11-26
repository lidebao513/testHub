"""Batch 6 C-2：项目级写锁守护测试。

并发重提同一 project（不同参数，幂等键不同）时，`stage_persist` 的
「删 FP/TP → 插 FP/TP → cases 对账」必须串行，最终库里是某一次完整产物，
不能是两个产物的混合（例如 A 的 FP + B 的 TP + 双方 cases 交错）。
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from core import store
from core.contracts import CaseSpec, FunctionalPoint, TestPoint
from engine import pipeline


def _fp(fid: str) -> FunctionalPoint:
    return FunctionalPoint(fp_id=fid, ftype="api", file_path="app.py", name=fid, title=fid)


def _tp(tid: str, fid: str) -> TestPoint:
    return TestPoint(tp_id=tid, fp_contract_id=fid, category="正常", module="m")


def _case(tid: str, fid: str) -> CaseSpec:
    return CaseSpec(
        tc_no=tid,
        title=tid,
        ctype="api",
        steps=[{"expect": "ok"}],
        tp_id=tid,
        fp_contract_id=fid,
    )


def _run_persist(pid: int, tag: str) -> None:
    opts = pipeline.PipelineOptions(project_id=pid, persist=True)
    result = pipeline.PipelineResult(project_id=pid)
    result.functional_points = [_fp(f"fp-{tag}")]
    result.test_points = [_tp(f"tp-{tag}", f"fp-{tag}")]
    result.cases = [_case(f"tc-{tag}", f"fp-{tag}")]
    pipeline.stage_persist(opts, result, None)


def test_lock_serializes_same_project(fresh_db) -> None:
    """同 project 并发 persist：锁内最大并发=1，最终产物不混合。"""
    store.clear_project_write_locks()
    pid = store.upsert_project("batch6-lock", "")

    active = 0
    peak = 0
    guard = threading.Lock()
    first_entered = threading.Event()
    release_first = threading.Event()
    orig = store.replace_test_points

    def slow_replace(*args, **kwargs):
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
        # 只有第一个进临界区的线程制造等待窗口；无锁时第二个线程也会同时
        # 卡在 barrier/Event 上，peak 变成 2；有锁时被 stage_persist 挡在门外。
        if not first_entered.is_set():
            first_entered.set()
            assert release_first.wait(timeout=5), "首线程放行超时"
        time.sleep(0.02)
        try:
            return orig(*args, **kwargs)
        finally:
            with guard:
                active -= 1

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(store, "replace_test_points", slow_replace)
        with ThreadPoolExecutor(max_workers=2) as pool:
            # 两个任务同时提交，真并发
            f1 = pool.submit(_run_persist, pid, "A")
            f2 = pool.submit(_run_persist, pid, "B")
            assert first_entered.wait(timeout=5)
            # 首线程仍在临界区（等 release_first）时，第二个若未被锁挡住，
            # 它会进入 slow_replace 并把 active 加到 2。
            time.sleep(0.1)
            assert active == 1, f"锁未生效：首线程仍在临界区时 active={active}"
            release_first.set()
            f1.result(timeout=15)
            f2.result(timeout=15)

    assert peak == 1, f"同 project 落库未串行，峰值并发={peak}"

    fp_ids = {r["contract_id"] for r in store.list_functional_points(pid)}
    tp_ids = {r["tp_id"] for r in store.list_test_points(pid)}
    cases = store.list_cases(pid, include_obsolete=True)
    active_cases = {c["tp_id"] for c in cases if c["status"] != "obsolete"}
    # 最终只保留最后写入者的完整一套，另一套全部 obsolete
    assert len(fp_ids) == 1 and fp_ids in ({"fp-A"}, {"fp-B"})
    assert len(tp_ids) == 1 and tp_ids in ({"tp-A"}, {"tp-B"})
    assert len(active_cases) == 1 and active_cases in ({"tc-A"}, {"tc-B"})
    tag = next(iter(active_cases))[-1]
    assert fp_ids == {f"fp-{tag}"} and tp_ids == {f"tp-{tag}"}


def test_lock_is_per_project(fresh_db) -> None:
    """不同 project 不互相阻塞（锁粒度是 project 而非全局）。"""
    store.clear_project_write_locks()
    p1 = store.upsert_project("batch6-p1", "")
    p2 = store.upsert_project("batch6-p2", "")

    entered = threading.Event()
    release = threading.Event()

    orig = store.replace_test_points

    def gated(pid: int):
        def _inner(*args, **kwargs):
            if args[0] == p1:
                entered.set()
                assert release.wait(timeout=5), "p1 等待放行超时"
            return orig(*args, **kwargs)

        return _inner

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(store, "replace_test_points", gated(p1))
        with ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(_run_persist, p1, "A")
            assert entered.wait(timeout=5)
            f2 = pool.submit(_run_persist, p2, "B")
            f2.result(timeout=5)  # 不同 project 应立刻完成，不被 p1 的 gate 卡住
            release.set()
            f1.result(timeout=5)

    assert len(store.list_functional_points(p1)) == 1
    assert len(store.list_functional_points(p2)) == 1


def test_lock_reentrant_same_thread(fresh_db) -> None:
    """同线程嵌套获取同一 project 锁不自锁死（RLock 语义）。"""
    store.clear_project_write_locks()
    with store.project_write_lock(123), store.project_write_lock(123):
        assert True
