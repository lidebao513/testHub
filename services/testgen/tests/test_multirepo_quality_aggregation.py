"""多仓 quality 聚合缺陷守护（方案 A：合并事实字段 + 全量重算）。

复现的缺陷：末仓 quality 字典覆盖前仓、counts 的 kpi_*_pct 被 Counter 加成 300%。
修复口径：`_aggregate_multirepo_results` 先把事实字段并到父 result，再在全量
TP/FP 上调用一次 stage_quality 重算——与单仓同一套 KPI 逻辑。
"""

from __future__ import annotations

from pathlib import Path

from core.contracts import FunctionalPoint, TestPoint
from core.enums import FType
from engine import pipeline, quality
from engine.scan import SourceFile


# 观测：quality 的四个顶层键都会被重算覆盖（不再是字典合并取末仓）
_QUALITY_KEYS = ("enabled", "gate", "kpis", "channels", "summary")


def _sf(rel: str, text: str) -> SourceFile:
    return SourceFile(rel=rel, abspath=Path(rel), text=text)


def _fp(fp_id: str, *, file: str = "app.py", line: int = 1) -> FunctionalPoint:
    return FunctionalPoint(
        fp_id=fp_id,
        ftype=FType.API.value,
        file_path=file,
        name=f"GET /{fp_id}",
        title=fp_id,
        description=f"f() @ {file}:{line}",
    )


def _tp(tp_id: str, fp_id: str, *, dim: str = "正常", evidence: bool = True) -> TestPoint:
    return TestPoint(
        tp_id=tp_id,
        fp_contract_id=fp_id,
        category="正常",
        module="m",
        title=tp_id,
        source="app.py",
        expect="返回状态码 200",
        dimension=dim,
        evidence=[f"app.py:{1}"] if evidence else [],
        origin="rule",
        channel="body",
    )


def _child(  # noqa: PLR0913 - 测试构造器，参数即场景维度，显式传入比包结构体清晰
    *,
    fp_ids: list[str],
    tps: list[TestPoint],
    files: dict[str, SourceFile] | None = None,
    hunks: dict[str, list[tuple[int, int]]] | None = None,
    fps: list[FunctionalPoint] | None = None,
    pct: int = 100,
    gate_blocked: int = 0,
) -> pipeline.PipelineResult:
    """构造一个已过门禁的单仓子结果（quality/counts 按单仓口径填好）。"""
    if fps is None:
        # 有 files 时让功能点行号指向第一个真实文件，否则 changed_func 分母虽在、分子必为 0。
        first_file = next(iter(files), "app.py") if files else "app.py"
        fps = [_fp(fid, file=first_file) for fid in fp_ids]
    quality.apply_gate(tps)
    sub = pipeline.PipelineResult(mode="full")
    sub.functional_points = fps
    sub.test_points = tps
    sub.files = files or {}
    sub.diff_hunks = hunks or {}
    sub.has_code = bool(files)
    # 各仓自己的快照：pct 都填 100，聚合后若仍是「求和」会变 300%。
    sub.quality = {
        "enabled": True,
        "gate": quality.apply_gate(tps),
        "kpis": {
            "changed_func_coverage": {"value": 1.0, "covered": len(fp_ids), "total": len(fp_ids)}
        },
        "channels": quality.channel_summary(tps),
        "summary": quality.summarize(tps),
    }
    sub.counts = {
        "functional_points": len(fps),
        "test_points": len(tps),
        "kpi_changed_func_pct": pct,
        "kpi_impact_entry_pct": pct,
        "kpi_dimension_pct": pct,
        "gate_blocked": gate_blocked,
        "changed_func_covered": len(fp_ids),
        "changed_func_total": len(fp_ids),
    }
    return sub


def _aggregate(
    children: list[pipeline.PipelineResult], *, mode: str = "full"
) -> pipeline.PipelineResult:
    opts = pipeline.PipelineOptions(mode=mode)
    parent = pipeline.PipelineResult(mode=mode)
    return pipeline._aggregate_multirepo_results(opts, parent, children)


def test_gate_and_channels_are_summed_not_overwritten() -> None:
    """门禁 / 三通道计数 = 三仓之和（末仓不再整体覆盖前仓）。"""
    children = [
        _child(fp_ids=["FP-a"], tps=[_tp("TP-a1", "FP-a"), _tp("TP-a2", "FP-a")]),
        _child(fp_ids=["FP-b"], tps=[_tp("TP-b1", "FP-b")]),
        # 末仓：一条被门禁拦下（无证据）
        _child(
            fp_ids=["FP-c"],
            tps=[_tp("TP-c1", "FP-c"), _tp("TP-c2", "FP-c", evidence=False)],
        ),
    ]
    got = _aggregate(children)
    assert got.quality["gate"]["quality_gated"] == 5  # 2+1+2
    assert got.quality["channels"]["body"]["tp"] == 5
    assert got.quality["channels"]["total"] == 5
    # 被拦下的那条必须在聚合快照里可见（末仓自己的 gate_blocked=1，若整体覆盖恰好也是 1；
    # 因此用首仓独有的 quality_gated=2 验证「不是只看末仓」——末仓只有 1 条过检的? 不，末仓 2 条）
    assert got.quality["gate"]["gate_blocked"] == 1
    for key in _QUALITY_KEYS:
        assert key in got.quality


def test_changes_func_coverage_recomputed_from_all_repos() -> None:
    """changed_func 分子/分母 = 三仓之和，value = 总和之比（不是平均百分比）。"""
    # 三仓各有 1 个改动函数，各自 files/hunks 独立；分母不同才能暴露「平均 value」错误。
    src_a = "def fa():\n    return 1\n"
    src_b = "def fb():\n    return 2\n"
    child_a = _child(
        fp_ids=["FP-a"],
        tps=[_tp("TP-a1", "FP-a")],
        files={"a.py": _sf("a.py", src_a)},
        hunks={"a.py": [(1, 1)]},
    )
    child_a.test_points[0].source = "a.py"
    child_b = _child(
        fp_ids=["FP-b"],
        tps=[_tp("TP-b1", "FP-b")],
        files={"b.py": _sf("b.py", src_b)},
        hunks={"b.py": [(1, 1)]},
    )
    child_b.test_points[0].source = "b.py"
    # 末仓无代码（前端），不贡献分母
    child_c = _child(fp_ids=["FP-c"], tps=[_tp("TP-c1", "FP-c")])

    # changed_func 分母只在 incremental 模式下由 hunks 反推（stage_quality 口径）
    got = _aggregate([child_a, child_b, child_c], mode="incremental")
    cf = got.quality["kpis"]["changed_func_coverage"]
    assert cf["total"] == 2, "跨仓改动函数分母应为 2（a.py::fa + b.py::fb）"
    assert cf["covered"] == 2
    assert cf["value"] == 1.0
    # 末仓无代码不会把分子分母清零（若沿用末仓快照会得到 1/1）
    assert got.counts["changed_func_total"] == 2


def test_counts_kpi_pct_matches_recomputed_quality_not_300() -> None:
    """counts 的 kpi_*_pct = 重算后的 quality value，绝不是 Counter 求和的 300%。"""
    children = [
        _child(fp_ids=["FP-a"], tps=[_tp("TP-a1", "FP-a", dim="正常")]),
        _child(fp_ids=["FP-b"], tps=[_tp("TP-b1", "FP-b", dim="异常")]),
        _child(fp_ids=["FP-c"], tps=[_tp("TP-c1", "FP-c", dim="安全")]),
    ]
    got = _aggregate(children)
    q = got.quality
    assert got.counts["kpi_changed_func_pct"] == quality_kpi_pct(q, "changed_func_coverage")
    assert got.counts["kpi_impact_entry_pct"] == quality_kpi_pct(q, "impact_entry_coverage")
    assert got.counts["kpi_dimension_pct"] == quality_kpi_pct(q, "dimension_coverage")
    # 关键回归：三仓各 100% 不能变成 300%
    assert got.counts["kpi_changed_func_pct"] <= 100
    assert got.counts["kpi_impact_entry_pct"] <= 100
    assert got.counts["kpi_dimension_pct"] <= 100


def quality_kpi_pct(quality_snapshot: dict, kpi: str) -> int:
    """从 quality 快照取某 KPI 的百分比整数（与 pipeline._pct 同口径）。"""
    return round(float(quality_snapshot["kpis"][kpi]["value"]) * 100)


def test_dimension_coverage_dedupes_across_repos() -> None:
    """维度按种类去重：同一维度在三仓都出现，只算一次 theory/actual。"""
    children = [
        _child(fp_ids=["FP-a"], tps=[_tp("TP-a1", "FP-a", dim="正常")]),
        _child(fp_ids=["FP-b"], tps=[_tp("TP-b1", "FP-b", dim="正常")]),
        _child(fp_ids=["FP-c"], tps=[_tp("TP-c1", "FP-c", dim="正常")]),
    ]
    got = _aggregate(children)
    dims = got.quality["kpis"]["dimension_coverage"]
    # 单仓各 1 个「正常」维度；若末仓覆盖会是 1/1，若简单相加会是 3/3，
    # 去重后理论值应为按维度名去重的种类数（这里只有「正常」+ scopes 展开的维度）。
    assert dims["actual"] == 1, "同一维度跨仓去重后只算 1 种"
    expected = round(dims["actual"] / dims["theory"], 3) if dims["theory"] else 1.0
    assert dims["value"] == expected


def test_single_repo_quality_unchanged() -> None:
    """单仓路径零回归：子仓自己的 quality 不被聚合逻辑篡改。"""
    sub = _child(fp_ids=["FP-x"], tps=[_tp("TP-x1", "FP-x")])
    before = dict(sub.quality)
    _aggregate([sub])
    assert sub.quality == before, "聚合不得回写各仓自身的 quality 快照"


def test_quality_facts_merged_from_all_repos() -> None:
    """事实字段（files / hunks / semantic / attributed / has_code）按仓合并。"""
    child_a = _child(
        fp_ids=["FP-a"],
        tps=[_tp("TP-a1", "FP-a")],
        files={"a.py": _sf("a.py", "def fa(): pass\n")},
        hunks={"a.py": [(1, 1)]},
    )
    child_a.semantic_dims = {"FP-a": [("边界", "空值")]}
    child_a.attributed_evidence = [{"file_path": "a.py", "via": "fa", "fp_id": "FP-a"}]
    child_b = _child(
        fp_ids=["FP-b"],
        tps=[_tp("TP-b1", "FP-b")],
        files={"b.py": _sf("b.py", "def fb(): pass\n")},
        hunks={"b.py": [(2, 1)]},
    )
    child_b.semantic_dims = {"FP-b": [("安全", "越权")]}
    child_b.attributed_evidence = [{"file_path": "b.py", "via": "fb", "fp_id": "FP-b"}]

    got = _aggregate([child_a, child_b])
    assert set(got.files) == {"a.py", "b.py"}
    assert set(got.diff_hunks) == {"a.py", "b.py"}
    assert set(got.semantic_dims) == {"FP-a", "FP-b"}
    assert len(got.attributed_evidence) == 2
    assert got.has_code is True
