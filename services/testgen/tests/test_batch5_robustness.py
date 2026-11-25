"""批 5 批次 1 守护测试（C-3 / C-4）。

C-3：/api/v1/analyze 显式带出 total 与 truncated，截断不再被误当全量。
C-4：取消探针下沉到引擎阶段内长循环，默认 None 零行为变化；service 层注入
      后循环命中即抛 TaskCancelledError，由任务状态机置 cancelled 终态。
"""

from copy import deepcopy
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from engine import case_gen, executor, fp_extract, scan
from engine.pipeline import PipelineOptions, cancel_tick
from service import app as app_module
from service.tasks import TaskCancelledError


class _Probe:
    """可计数、可设置触发阈值的探针。"""

    def __init__(self, boom_at: int | None = None) -> None:
        self.calls = 0
        self.boom_at = boom_at

    def __call__(self) -> None:
        self.calls += 1
        if self.boom_at is not None and self.calls >= self.boom_at:
            raise TaskCancelledError("t-cancel")


# ---------------------------------------------------------------- C-3
def test_analyze_response_has_total_and_truncated(fresh_db, sample_repo):
    client = TestClient(app_module.app, raise_server_exceptions=False)
    resp = client.post("/api/v1/analyze", json={"local_path": str(sample_repo)})
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body.get("total"), int)
    assert isinstance(body.get("truncated"), bool)
    # 返回条数恒 <= 500；truncated 与 total>500 严格一致
    assert len(body["functional_points"]) <= 500
    assert body["truncated"] == (body["total"] > 500)
    assert body["total"] >= len(body["functional_points"])


def _patch_analyze_engine(monkeypatch, n_fps: int):
    """替换 analyze_sync 依赖的引擎函数，造 n_fps 个功能点。"""
    from core.contracts import FunctionalPoint
    from engine import fp_extract as fe
    from engine import scan as scan_mod
    from service.routers import analysis

    def _fake_scan_index(self):
        return {"a.py": object()}

    class _FakeExtractor:
        pass

    extracted = _FakeExtractor()
    extracted.functional_points = [
        FunctionalPoint(
            fp_id=f"fp-{i}", ftype="api", file_path="a.py", name=f"fn{i}", title=f"功能{i}"
        )
        for i in range(n_fps)
    ]
    extracted.errors = []

    monkeypatch.setattr(scan_mod.Scanner, "index", _fake_scan_index, raising=False)
    # 局部导入的 scan 是 engine.scan 模块对象，Scanner 类相同，patch 类方法即可
    monkeypatch.setattr(fe, "extract_functional_points", lambda *a, **k: extracted)
    return analysis


def test_analyze_truncation_flag_when_over_limit(fresh_db, sample_repo, monkeypatch):
    """造 520 个功能点：total=520、truncated=True、functional_points=500。"""
    analysis = _patch_analyze_engine(monkeypatch, 520)
    result = analysis.analyze_sync(analysis.AnalyzeRequest(local_path=str(sample_repo)))
    assert result["total"] == 520
    assert result["truncated"] is True
    assert len(result["functional_points"]) == 500


def test_analyze_no_truncation_flag_when_under_limit(fresh_db, sample_repo, monkeypatch):
    """不足 500 条：truncated=False，functional_points 全量返回。"""
    analysis = _patch_analyze_engine(monkeypatch, 1)
    result = analysis.analyze_sync(analysis.AnalyzeRequest(local_path=str(sample_repo)))
    assert result["total"] == 1
    assert result["truncated"] is False
    assert len(result["functional_points"]) == 1


# ---------------------------------------------------------------- C-4
def test_cancel_tick_fires_at_intervals():
    probe = _Probe()
    for i in range(33):
        cancel_tick(probe, i)
    # 0/16/32 三次
    assert probe.calls == 3


def test_cancel_tick_none_is_noop():
    cancel_tick(None, 0)
    cancel_tick(None, 16)  # 不抛异常


def test_pipeline_options_cancel_probe_is_runtime_only():
    opts = PipelineOptions()
    assert opts.cancel_probe is None

    def probe():
        return None

    opts.cancel_probe = probe
    # deepcopy（多仓派生）共享同一探针引用（函数对象 deepcopy 后仍是同一引用）
    assert deepcopy(opts).cancel_probe is probe
    # repr 不含探针（避免闭包进日志）
    assert "cancel_probe" not in repr(opts)


def test_case_gen_propagates_cancel():
    from core.contracts import TestPoint

    tps = [TestPoint(tp_id=f"tp-{i}", fp_contract_id=f"fp-{i}", category="正常") for i in range(40)]
    probe = _Probe(boom_at=2)
    with pytest.raises(TaskCancelledError):
        case_gen.generate_cases(tps, cancel_probe=probe)
    assert probe.calls == 2


def test_fp_extract_propagates_cancel():
    from engine.scan import SourceFile

    files = {f"f{i}.py": SourceFile(rel=f"f{i}.py", abspath=None, text="x=1\n") for i in range(40)}
    probe = _Probe(boom_at=2)
    with pytest.raises(TaskCancelledError):
        fp_extract.extract_functional_points(files, cancel_probe=probe)
    assert probe.calls == 2


def test_scan_propagates_cancel(tmp_path):
    for i in range(40):
        (tmp_path / f"f{i}.py").write_text("x=1\n", encoding="utf-8")
    probe = _Probe(boom_at=2)
    with pytest.raises(TaskCancelledError):
        list(scan.Scanner(tmp_path, cancel_probe=probe).iter_files())
    assert probe.calls == 2


def test_executor_propagates_cancel_and_closes_resources():
    """逐用例循环命中探针时抛出，finally 仍执行（不吞异常）。"""
    from core.contracts import CaseSpec

    probe = _Probe(boom_at=1)
    cases = [CaseSpec(tc_no=f"TC-{i}", title=f"t{i}", ctype="api", steps=[]) for i in range(40)]
    with patch.object(executor, "execute_case") as exec_case, pytest.raises(TaskCancelledError):
        executor.execute_all(cases, cancel_probe=probe)
    assert probe.calls == 1
    assert exec_case.call_count == 0  # 第一条之前就命中


def test_cancel_from_engine_loop_reaches_cancelled_terminal(fresh_db):
    """端到端：引擎循环探针抛错 → 真实任务状态机置 cancelled 终态。"""
    from core.contracts import TestPoint
    from service import tasks as task_engine

    store = task_engine.TaskStore()
    executor = task_engine.BackgroundExecutor(store, max_workers=1)
    task = store.create(kind="generate")

    def job():
        # 任务在作业启动时被取消（模拟用户点取消）
        store.cancel(task.task_id)

        def probe():
            if store.is_cancelled(task.task_id):
                raise task_engine.TaskCancelledError(task.task_id)

        tps = [
            TestPoint(tp_id=f"tp-{i}", fp_contract_id=f"fp-{i}", category="正常") for i in range(40)
        ]
        # 探针下沉在 case_gen 循环内，命中即抛，由 executor.submit 捕获
        case_gen.generate_cases(tps, cancel_probe=probe)
        return {"unreachable": True}

    executor.submit(task.task_id, job)
    from tests.test_task_cancel_race import _wait_state

    final = _wait_state(store, task.task_id, {"cancelled", "success", "failed"}, timeout=5)
    executor.shutdown(wait=True)
    assert final == "cancelled"


def test_service_job_injects_probe(monkeypatch):
    """service 层两个后台 job 必须给 opts 注入取消探针。"""
    from service import app as app_mod

    sent: dict[str, object] = {}

    class _Result:
        project_id = None

        def to_dict(self):
            return {}

    def fake_run_auto(opts, *, progress=None):
        sent["probe"] = opts.cancel_probe
        return _Result()

    monkeypatch.setattr(app_mod.pipeline, "run_pipeline_auto", fake_run_auto)
    monkeypatch.setattr(app_mod.task_store, "is_cancelled", lambda tid: False)
    opts = PipelineOptions()
    app_mod._run_generation_job("t-1", opts)
    assert callable(sent["probe"])
    sent["probe"]()  # 未取消不抛
