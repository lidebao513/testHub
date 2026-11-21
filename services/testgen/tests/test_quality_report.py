"""T5 单测（报告侧）：`quality` 段的三 KPI / 三通道 / 未验证原因。

报告是「给人看质量」的出口，最容易出的错是**看起来有数**：快照缺失时用库里的
测试点硬凑分母，或三栏计数与总数对不上。这里分别锁死。
"""

from __future__ import annotations

import json

from core import store
from core.db import connect
from engine import report as report_engine
from output.report_writer import render_html, render_markdown


def _row(  # noqa: PLR0913 - 测试造数工具，参数即字段
    tp_id: str,
    *,
    origin: str = "rule",
    source: str = "backend/app.py",
    unverified: int = 0,
    reason: str = "",
    dimension: str = "正常-可用性",
) -> dict:
    return {
        "tp_id": tp_id,
        "origin": origin,
        "source": source,
        "unverified": unverified,
        "unverified_reason": reason,
        "dimension": dimension,
        "category": "正常",
        "expect": "返回 2xx",
    }


GEN = {
    "kpis": {
        "changed_func_coverage": {"value": 0.75, "covered": 3, "total": 4},
        "impact_entry_coverage": {"value": 0.6, "covered": 3, "total": 5},
        "dimension_coverage": {"value": 0.5, "actual": 6, "theory": 12},
        "note": "分子只统计过门禁的测试点",
    },
    "gate": {"quality_gated": 10, "gate_blocked": 1},
}


def test_quality_section_passes_kpis_through_and_counts_channels() -> None:
    tps = [
        _row("TP-1"),
        _row("TP-2", origin="semantic"),
        _row("TP-3", origin="impact"),
        _row("TP-4", origin="impact_file"),
        _row("TP-5", origin="rule", source="runtime:/chat"),
    ]
    got = report_engine.quality(tps, GEN)

    assert got["available"] is True and got["source"] == "generation"
    assert got["kpis"]["changed_func_coverage"]["value"] == 0.75
    ch = got["channels"]
    assert (ch["body"]["tp"], ch["impact"]["tp"], ch["runtime"]["tp"]) == (2, 2, 1)
    assert ch["total"] == 5 and ch["consistent"] is True
    assert ch["body"]["label"] == "本体"


def test_quality_section_breaks_down_unverified_by_reason() -> None:
    tps = [
        _row("TP-1", unverified=1, reason="no_evidence"),
        _row("TP-2", unverified=1, reason="best_effort"),
        _row("TP-3"),
    ]
    got = report_engine.quality(tps, GEN)
    un = got["unverified"]
    assert un["total"] == 2
    assert un["blocked"] == 1  # 门禁拦下（best_effort 不算）
    assert un["best_effort"] == 1
    assert un["text"]["no_evidence"].startswith("没有证据")
    # 未验证的不算「过门禁」
    assert got["channels"]["body"]["gate_ok"] == 1


def test_quality_section_degrades_without_snapshot() -> None:
    """快照缺失时 KPI 留空，**不拿库里的数据硬凑**（那会得到编出来的覆盖率）。"""
    got = report_engine.quality([_row("TP-1")], {})
    assert got["available"] is False and got["source"] == "none"
    assert got["kpis"] == {}
    assert got["channels"]["total"] == 1


def test_markdown_report_contains_quality_section() -> None:
    report = {
        "project": {"name": "demo", "id": 30},
        "generated_at": "2026-11-18T18:00:00",
        "report_version": "1.0",
        "counts": {"functional_points": 3, "test_points": 5, "cases": 5, "cases_active": 5},
        "exec_summary": {"headline": "尚未执行", "bullets": [], "metrics": {}},
        "coverage": {"tp_rate": 100.0, "uncovered_tp_count": 0},
        "quality": report_engine.quality([_row("TP-1", unverified=1, reason="best_effort")], GEN),
        "trend": {"direction": "stable", "points": []},
        "flaky": {"count": 0, "evaluated": 0, "cases": []},
        "traceability": {},
        "evidence": [],
        "reuse": {},
        "change": None,
    }
    md = render_markdown(report)
    assert "生成质量（门禁 + 三 KPI）" in md
    assert "改动函数覆盖" in md and "影响入口覆盖" in md and "维度覆盖" in md
    assert "3/4" in md and "需特殊环境" in md

    html = render_html(report)
    assert "改动函数覆盖" in html and "本体" in html and "运行时" in html


def test_latest_quality_reads_generation_snapshot(fresh_db) -> None:
    """`_latest_quality` 从 gen_tasks.result 里取最近一次生成的质量快照。"""
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO projects(name, local_path, created_at) VALUES(?,?,?)",
            ("p-quality", "/tmp/p", "2026-11-18T18:00:00"),
        )
        pid = int(
            conn.execute("SELECT id FROM projects WHERE name = ?", ("p-quality",)).fetchone()[0]
        )
        payload = {"counts": {"cases": 5}, "quality": GEN}
        conn.execute(
            "INSERT INTO gen_tasks(task_id, project_id, kind, state, result, created_at)"
            " VALUES(?,?,?,?,?,?)",
            (
                "gen-old",
                pid,
                "generate",
                "success",
                json.dumps({"quality": {"kpis": {}}}),
                "2026-11-18T17:00:00",
            ),
        )
        conn.execute(
            "INSERT INTO gen_tasks(task_id, project_id, kind, state, result, created_at, finished_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (
                "gen-new",
                pid,
                "generate",
                "success",
                json.dumps(payload, ensure_ascii=False),
                "2026-11-18T18:00:00",
                "2026-11-18T18:00:10",
            ),
        )
        conn.commit()

        got = store._latest_quality(conn, pid)
        assert got["kpis"]["dimension_coverage"]["theory"] == 12

        # 报告快照里也带上了（报告只读它，不重算）
        snap = store.report_snapshot(pid, conn=conn)
        assert snap["quality"]["gate"]["gate_blocked"] == 1
    finally:
        conn.close()


def test_latest_quality_empty_when_no_tasks(fresh_db) -> None:
    conn = connect()
    try:
        assert store._latest_quality(conn, 99999) == {}
    finally:
        conn.close()
