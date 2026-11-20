"""T5 单测：命题门禁（gate）+ 三个 KPI + 三通道。

口径要点（改动这些断言前先读 `engine/quality.py` 的模块 docstring）：

- 门禁只标 `unverified` + 记原因，**不删测试点**；
- KPI 分子只算过门禁的条目——否则「无证据的凑数条目」会把覆盖率抬上去；
- 三通道之和必须等于总数（重复计数是最隐蔽的错，专门锁死）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.contracts import FunctionalPoint, TestPoint
from core.enums import FType, TPType
from engine import quality
from engine.scan import SourceFile


def _sf(rel: str, text: str) -> SourceFile:
    return SourceFile(rel=rel, abspath=Path(rel), text=text)


def _tp(  # noqa: PLR0913 - 测试造数工具，参数即字段
    tp_id: str = "TP-00000001",
    *,
    evidence: list[str] | None = None,
    expect: str = "返回 2xx，响应体包含 id",
    origin: str = "rule",
    source: str = "backend/app.py",
    dimension: str = "正常-可用性",
    fp_id: str = "FP-1",
    unverified: bool = False,
) -> TestPoint:
    return TestPoint(
        tp_id=tp_id,
        fp_contract_id=fp_id,
        category=TPType.NORMAL.value,
        dimension=dimension,
        expect=expect,
        origin=origin,
        source=source,
        evidence=evidence if evidence is not None else ["backend/app.py:10"],
        unverified=unverified,
    )


# ============================================================================
# 门禁
# ============================================================================
def test_gate_blocks_missing_evidence() -> None:
    tp = _tp(evidence=[])
    stats = quality.apply_gate([tp])

    assert tp.unverified is True
    assert tp.unverified_reason == quality.GATE_NO_EVIDENCE
    assert stats["gate_blocked"] == 1
    assert stats[quality.GATE_NO_EVIDENCE] == 1
    assert quality.gate_failed(tp) is True


def test_gate_blocks_unobservable_expect() -> None:
    tp = _tp(expect="应当正常工作")
    quality.apply_gate([tp])
    assert tp.unverified_reason == quality.GATE_NO_OBSERVABLE


def test_gate_blocks_missing_origin() -> None:
    tp = _tp(origin="")
    quality.apply_gate([tp])
    assert tp.unverified_reason == quality.GATE_NO_ORIGIN


def test_gate_passes_wellformed_point() -> None:
    tp = _tp()
    stats = quality.apply_gate([tp])
    assert tp.unverified is False
    assert tp.unverified_reason == ""
    assert stats["gate_blocked"] == 0
    assert stats["quality_gated"] == 1


def test_gate_keeps_best_effort_reason_and_does_not_double_count() -> None:
    """T3 的 best_effort（需特殊环境）不是水货：标原因、但不算「门禁拦下」。"""
    tp = _tp(unverified=True, expect="并发 20 请求，数据不串（返回 2xx）")
    stats = quality.apply_gate([tp])
    assert tp.unverified_reason == quality.GATE_BEST_EFFORT
    assert stats["gate_blocked"] == 0
    assert stats[quality.GATE_BEST_EFFORT] == 1
    assert quality.gate_failed(tp) is False  # 仍计入 KPI 分子


def test_gate_disabled_only_labels_best_effort() -> None:
    tp = _tp(evidence=[])
    stats = quality.apply_gate([tp], enabled=False)
    assert tp.unverified is False and tp.unverified_reason == ""
    assert stats["gate_blocked"] == 0


def test_observable_expect_accepts_status_code_and_rejects_vague() -> None:
    assert quality.observable_expect("返回 401 并提示未授权") is True
    assert quality.observable_expect("列表条数不重复增长") is True
    assert quality.observable_expect("符合预期") is False
    assert quality.observable_expect("") is False


def test_observable_expect_accepts_calibrated_real_phrasings() -> None:
    """实跑中这两条曾被误判「无可观测对象」——门禁假阳性会让人开始无视标黄。"""
    assert (
        quality.observable_expect("交互元素可用，点击后产生预期状态变化，且不新增控制台错误")
        is True
    )
    assert (
        quality.observable_expect(
            "分别传入「未提供（None/缺省）」「空值（空串/空数组/0）」→ 两者被区分处理："
            "空值走「已发起但内容为空」分支，未提供走「仅预留」分支，不互相覆盖"
        )
        is True
    )


# ============================================================================
# 三通道
# ============================================================================
def test_channel_split_sums_to_total() -> None:
    tps = [
        _tp("TP-01", origin="rule", source="backend/app.py"),
        _tp("TP-02", origin="semantic", source="backend/app.py"),
        _tp("TP-03", origin="impact", source="backend/app.py"),
        _tp("TP-04", origin="impact_file", source="frontend/a.tsx"),
        _tp("TP-05", origin="rule", source="runtime:/chat"),
    ]
    got = quality.channel_summary(tps)

    assert got[quality.CHANNEL_BODY]["tp"] == 2  # rule + semantic（都是代码本体）
    assert got[quality.CHANNEL_IMPACT]["tp"] == 2  # impact + impact_file
    assert got[quality.CHANNEL_RUNTIME]["tp"] == 1
    assert got["total"] == 5
    assert got["consistent"] is True


def test_runtime_channel_wins_over_body_origin() -> None:
    """运行时发现的页面 origin 仍是 rule，但三栏里必须算「运行时」。"""
    tp = _tp(origin="rule", source="runtime:/pc/chat")
    assert quality.channel_of(tp) == quality.CHANNEL_RUNTIME


def test_channel_gate_ok_counts_exclude_blocked() -> None:
    tps = [_tp("TP-01", evidence=[]), _tp("TP-02")]
    quality.apply_gate(tps)  # 必须先过门禁：channel_summary 只读 unverified 标志
    got = quality.channel_summary(tps)
    assert got[quality.CHANNEL_BODY]["tp"] == 2
    assert got[quality.CHANNEL_BODY]["gate_ok"] == 1


# ============================================================================
# KPI
# ============================================================================
PY_SRC = """\
def helper():
    return 1


def entry():
    return helper()
"""


def test_changed_functions_uses_ast_innermost() -> None:
    files = {"app.py": _sf("app.py", PY_SRC)}
    got = quality.changed_functions(files, {"app.py": [(1, 2)]})
    assert got == [("app.py", "helper")]


def test_changed_functions_falls_back_to_module_for_frontend() -> None:
    files = {"a.tsx": _sf("a.tsx", "export function A(){ return null }\n")}
    got = quality.changed_functions(files, {"a.tsx": [(1, 1)]})
    assert got == [("a.tsx", "A")]


def test_changed_func_coverage_counts_three_of_four() -> None:
    """3/4 覆盖 → 0.75（精确数值，不做四舍五入的模糊断言）。"""
    fps = [
        FunctionalPoint(
            fp_id=f"FP-{i}",
            ftype=FType.API.value,
            file_path="app.py",
            name=f"GET /x{i}",
            title=f"x{i}",
            description=f"x{i}() @ app.py:{line}",
        )
        for i, line in enumerate((1, 5, 9, 13))
    ]
    src = "\n".join(
        [
            "def f1():",
            "    return 1",
            "",
            "",
            "def f2():",
            "    return 2",
            "",
            "",
            "def f3():",
            "    return 3",
            "",
            "",
            "def f4():",
            "    return 4",
        ]
    )
    files = {"app.py": _sf("app.py", src)}
    hunks = {"app.py": [(1, 1), (5, 1), (9, 1), (13, 1)]}
    tps = [
        _tp("TP-1", fp_id="FP-0"),
        _tp("TP-2", fp_id="FP-1"),
        _tp("TP-3", fp_id="FP-2"),
        # FP-3 没有过门禁的测试点（被门禁拦下）
        _tp("TP-4", fp_id="FP-3", evidence=[]),
    ]
    quality.apply_gate(tps)
    got = quality.changed_func_coverage(tps, fps=fps, files=files, hunks=hunks)

    assert got["covered"] == 3
    assert got["total"] == 4
    assert got["value"] == 0.75
    assert got["uncovered"] == [{"file": "app.py", "symbol": "f4"}]


def test_changed_func_coverage_counts_attribution_path() -> None:
    """私有函数被归属到入口功能点：入口的测试点过门禁 → 该改动算被覆盖。"""
    src = "def _private():\n    return 1\n\n\ndef entry():\n    return _private()\n"
    files = {"app.py": _sf("app.py", src)}
    fps = [
        FunctionalPoint(
            fp_id="FP-entry",
            ftype=FType.API.value,
            file_path="app.py",
            name="GET /x",
            title="x",
            description="entry() @ app.py:5",
        )
    ]
    tps = [_tp("TP-1", fp_id="FP-entry")]
    quality.apply_gate(tps)
    got = quality.changed_func_coverage(
        tps,
        fps=fps,
        files=files,
        hunks={"app.py": [(1, 1)]},
        attributed=[{"file_path": "app.py", "via": "_private", "fp_id": "FP-entry", "hops": 1}],
    )
    assert got["covered"] == 1
    assert got["value"] == 1.0


def test_impact_entry_coverage_merges_backend_and_frontend() -> None:
    got = quality.impact_entry_coverage(
        impact_stats={"impact_items": 1, "impact_reachable_entries": 3},
        file_impact_stats={"file_impact_items": 2, "file_impact_identified": 5},
    )
    assert got["covered"] == 3
    assert got["total"] == 8
    assert got["value"] == 0.375
    assert got["backend"] == {"covered": 1, "total": 3}
    assert got["frontend"] == {"covered": 2, "total": 5}


def test_impact_entry_coverage_without_entries_is_full() -> None:
    """本次改动没有任何受影响入口 → 记 1.0（无可覆盖项），不是 0（那是「全漏」）。"""
    got = quality.impact_entry_coverage()
    assert got["value"] == 1.0 and got["total"] == 0


def test_dimension_coverage_counts_semantic_dims_in_theory() -> None:
    fp = FunctionalPoint(
        fp_id="FP-1",
        ftype=FType.API.value,
        file_path="app.py",
        name="GET /x/{id}",
        title="x",
        description="f() @ app.py:1",
    )
    scopes = {TPType.NORMAL.value, TPType.ABNORMAL.value, TPType.BOUNDARY.value}
    semantic = {"FP-1": [(TPType.NORMAL.value, "正常-列表去重")]}
    tps = [_tp("TP-1", dimension="正常-可用性"), _tp("TP-2", dimension="正常-列表去重")]
    quality.apply_gate(tps)

    got = quality.dimension_coverage(tps, fps=[fp], scopes=scopes, semantic=semantic)
    # 实际 2 个维度；理论 = plan_of ∩ scopes（可用性/参数非法/资源不存在/限流/降级/超时）+ 语义去重
    assert got["actual"] == 2
    assert got["theory"] > got["actual"]
    assert 0 < got["value"] < 1
    assert "正常-列表去重" not in got["missing"]


def test_compute_kpis_returns_three_keys_and_note() -> None:
    fp = FunctionalPoint(
        fp_id="FP-1",
        ftype=FType.BUSINESS.value,
        file_path="app.py",
        name="f",
        title="f",
        description="f() @ app.py:1",
    )
    tps = [_tp("TP-1", fp_id="FP-1")]
    got = quality.compute_kpis(
        tps,
        fps=[fp],
        files={"app.py": _sf("app.py", "def f():\n    return 1\n")},
        hunks={"app.py": [(1, 1)]},
        scopes={TPType.NORMAL.value},
    )
    assert set(got) >= {"changed_func_coverage", "impact_entry_coverage", "dimension_coverage"}
    assert got["changed_func_coverage"]["value"] == 1.0
    assert "分子只统计过门禁" in got["note"]


def test_summarize_gate_pass_rate() -> None:
    tps = [_tp("TP-1"), _tp("TP-2", evidence=[]), _tp("TP-3"), _tp("TP-4")]
    quality.apply_gate(tps)
    got = quality.summarize(tps)
    assert got == {
        "total": 4,
        "gate_ok": 3,
        "blocked": 1,
        "gate_pass_rate": pytest.approx(0.75),
    }
