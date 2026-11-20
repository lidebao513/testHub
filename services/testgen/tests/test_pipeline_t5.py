"""T5 单测（编排层）：`stage_quality` 的接线与输出契约。

为什么单独测编排层：`engine/quality.py` 的单测全绿，不代表 pipeline 里接对了——
T4 就踩过「模块单测全绿、编排层因字段名过期直接 failed」。这里锁三件事：
1. `result.quality` 的结构（报告/前端只读它，字段一改就断链）；
2. `result.counts` 的数值口径（度量脚本按这些 key 采集）；
3. `QUALITY_GATE=off` 时与基线一致（可回滚）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from core.contracts import FunctionalPoint, TestPoint
from core.enums import FType, TPType
from engine import pipeline
from engine.scan import SourceFile


SRC = """\
def helper():
    return 1


def entry():
    return helper()
"""


def _sf(rel: str, text: str) -> SourceFile:
    return SourceFile(rel=rel, abspath=Path(rel), text=text)


def _fp() -> FunctionalPoint:
    return FunctionalPoint(
        fp_id="FP-entry",
        ftype=FType.API.value,
        file_path="app.py",
        name="GET /x",
        title="查询 x",
        module="app",
        description="entry() @ app.py:5",
    )


def _tp(  # noqa: PLR0913 - 测试造数工具，参数即字段
    tp_id: str,
    *,
    fp_id: str = "FP-entry",
    evidence: list[str] | None = None,
    origin: str = "rule",
    dimension: str = "正常-可用性",
    unverified: bool = False,
) -> TestPoint:
    return TestPoint(
        tp_id=tp_id,
        fp_contract_id=fp_id,
        category=TPType.NORMAL.value,
        dimension=dimension,
        expect="返回 2xx，响应体包含 id",
        source="app.py",
        origin=origin,
        evidence=evidence if evidence is not None else ["app.py:5"],
        unverified=unverified,
    )


def _result(tps: list[TestPoint], *, semantic: dict[str, Any] | None = None) -> Any:
    return pipeline.PipelineResult(
        project_id=30,
        mode="incremental",
        has_code=True,
        files={"app.py": _sf("app.py", SRC)},
        functional_points=[_fp()],
        diff_hunks={"app.py": [(1, 2)]},
        test_points=list(tps),
        semantic_dims=semantic or {},
        counts={"impact_items": 1, "impact_reachable_entries": 2},
    )


def _opts() -> pipeline.PipelineOptions:
    opts = pipeline.default_options("", mode="incremental")
    opts.scopes = {TPType.NORMAL.value, TPType.ABNORMAL.value, TPType.BOUNDARY.value}
    return opts


def test_stage_quality_fills_three_kpis_and_channels() -> None:
    result = _result(
        [
            _tp("TP-1"),
            _tp("TP-2", origin="impact", dimension="回归-直接调用"),
            _tp("TP-3", origin="rule", dimension="正常-可用性", evidence=[]),
        ]
    )
    pipeline.stage_quality(_opts(), result, None)

    q = result.quality
    assert set(q["kpis"]) >= {
        "changed_func_coverage",
        "impact_entry_coverage",
        "dimension_coverage",
    }
    # 改动落在 helper()（无功能点、无归属证据）→ 未覆盖
    assert q["kpis"]["changed_func_coverage"] == {
        "value": 0.0,
        "covered": 0,
        "total": 1,
        "uncovered": [{"file": "app.py", "symbol": "helper"}],
    }
    assert q["channels"]["total"] == 3
    assert q["channels"]["consistent"] is True
    assert q["channels"]["impact"]["tp"] == 1
    assert q["gate"]["gate_blocked"] == 1
    assert q["enabled"] is True


def test_stage_quality_sets_counts_and_notes() -> None:
    result = _result([_tp("TP-1")])
    pipeline.stage_quality(_opts(), result, None)

    assert result.counts["gate_blocked"] == 0
    assert "kpi_changed_func_pct" in result.counts
    assert "kpi_impact_entry_pct" in result.counts
    assert "kpi_dimension_pct" in result.counts
    assert result.counts["channel_body"] == 1
    assert result.counts["channel_impact"] == 0
    assert any("质量门禁" in n for n in result.notes)
    assert any("三 KPI" in n for n in result.notes)


def test_stage_quality_counts_impact_entry_coverage_from_counts() -> None:
    """影响入口覆盖直接复用 T2/T4 写进 counts 的统计，不重算（避免第二真值源）。"""
    result = _result([_tp("TP-1")])
    result.counts.update({"file_impact_items": 1, "file_impact_identified": 3})
    pipeline.stage_quality(_opts(), result, None)

    im = result.quality["kpis"]["impact_entry_coverage"]
    assert im["backend"] == {"covered": 1, "total": 2}
    assert im["frontend"] == {"covered": 1, "total": 3}
    assert im["covered"] == 2 and im["total"] == 5


def test_stage_quality_disabled_keeps_points_and_marks_flag(monkeypatch) -> None:
    class _OffCfg:
        quality_gate = False

    opts = _opts()
    monkeypatch.setattr(pipeline, "get_settings", lambda: _OffCfg())
    tps = [_tp("TP-1", evidence=[]), _tp("TP-2")]
    result = _result(tps)
    pipeline.stage_quality(opts, result, None)

    assert result.quality["enabled"] is False
    assert result.quality["gate"]["gate_blocked"] == 0
    assert all(not tp.unverified for tp in tps)  # 门禁不置标
    assert any("已关闭" in n for n in result.notes)


def test_stage_quality_never_drops_test_points() -> None:
    """门禁只标不删：删掉会让人以为「本来就没这个维度」。"""
    tps = [_tp("TP-1", evidence=[]), _tp("TP-2")]
    result = _result(tps)
    pipeline.stage_quality(_opts(), result, None)
    assert len(result.test_points) == 2


def test_to_dict_exposes_quality() -> None:
    result = _result([_tp("TP-1")])
    pipeline.stage_quality(_opts(), result, None)
    data = result.to_dict()
    assert data["quality"]["kpis"]["dimension_coverage"]["theory"] >= 1


def test_stage_registered_before_execute() -> None:
    """顺序不能错：质量快照必须在用例/LLM 定稿之后、落库之前。"""
    order = pipeline.PIPELINE_STAGE_ORDER
    assert order.index("quality") > order.index("llm_design")
    assert order.index("quality") > order.index("case_gen")
    assert order.index("quality") < order.index("persist")
