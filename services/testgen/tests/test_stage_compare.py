"""两阶段产物对比编排单测（C2）。

覆盖：compare_artifacts（新增/失效/保留/问题新增修复保留）；
compare_stages 经 StageStore 载入两阶段产物后对比（不依赖 git）。
"""

from __future__ import annotations

from engine.stage_compare import StageCompareReport, compare_artifacts, compare_stages
from engine.stage_snapshot import StageSnapshot, StageStore


def _art(stage_id: str, tps: list[dict], issues: list[dict]) -> dict:
    return {"stage_id": stage_id, "test_points": tps, "issues": issues}


def test_compare_artifacts_added_removed_kept() -> None:
    fa = _art("A", [{"id": "t1"}, {"id": "t2"}], [{"id": "i1"}])
    ta = _art("B", [{"id": "t2"}, {"id": "t3"}], [{"id": "i1"}, {"id": "i2"}])
    r = compare_artifacts(fa, ta)
    assert {t["id"] for t in r.added_tps} == {"t3"}
    assert {t["id"] for t in r.removed_tps} == {"t1"}
    assert {t["id"] for t in r.kept_tps} == {"t2"}
    assert {i["id"] for i in r.added_issues} == {"i2"}
    assert r.fixed_issues == []  # i1 仍保留，无修复
    assert {i["id"] for i in r.kept_issues} == {"i1"}


def test_compare_stages_via_store(tmp_path) -> None:
    store = StageStore(tmp_path)
    fa = _art("master@aaa_1", [{"id": "t1"}, {"id": "t2"}], [{"id": "i1"}])
    ta = _art("master@bbb_2", [{"id": "t2"}, {"id": "t3"}], [{"id": "i1"}, {"id": "i2"}])
    s1 = StageSnapshot(
        repo="r",
        branch="master",
        commit="a" * 40,
        stage_id="master@aaa_1",
        artifact_dir="r/master@aaa",
    )
    s2 = StageSnapshot(
        repo="r",
        branch="master",
        commit="b" * 40,
        stage_id="master@bbb_2",
        artifact_dir="r/master@bbb",
    )
    store.add(s1)
    store.add(s2)
    store.save_artifact(s1, "analysis", fa)
    store.save_artifact(s2, "analysis", ta)
    r = compare_stages(store, "r", "master@aaa_1", "master@bbb_2")
    assert isinstance(r, StageCompareReport)
    assert {t["id"] for t in r.added_tps} == {"t3"}
    assert {t["id"] for t in r.removed_tps} == {"t1"}
    assert r.diff_stat == {}  # 未传 repo_path，diff 统计为空


def test_compare_stages_unresolved_ref(tmp_path) -> None:
    store = StageStore(tmp_path)
    with __import__("pytest").raises(KeyError):
        compare_stages(store, "r", "nope", "also-nope")
