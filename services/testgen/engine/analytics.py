"""执行质量分析：多批次通过率趋势 + flaky 不稳定用例识别。

迁移自 test-accel ``backend/modules/analytics.py`` 的 trend/flaky 两件套，
适配 testgen 现有 store（run_batches / runs 留痕表）。
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from core import store
from core.enums import ExecStatus, RunBatchState

# 真正「有结论」的状态——用于判定不稳定（环境态不计入）
_VERDICT_STATUSES = (ExecStatus.PASS.value, ExecStatus.FAIL.value, ExecStatus.ERROR.value)


def _pass_rate(status_counts: dict[str, int]) -> float | None:
    """有效通过率 = pass / (pass + fail)；无有效结论返回 None。"""
    effective = (
        status_counts.get(ExecStatus.PASS.value, 0) + status_counts.get(ExecStatus.FAIL.value, 0)
    )
    if effective <= 0:
        return None
    return round(status_counts.get(ExecStatus.PASS.value, 0) / effective, 4)


def trend(pid: int, *, only_finished: bool = True, limit: int = 50) -> dict[str, Any]:
    """多批次执行趋势序列（按 started_at 升序）。

    每个点含 batch_id / started_at / state / total / pass / fail /
    effective_total / pass_rate / structural_only / blocked_auth / error，
    并给出整体方向（improving / regressing / stable）。
    """
    batches = store.list_run_batches(pid, limit=200)
    if only_finished:
        batches = [b for b in batches if b.get("state") != RunBatchState.RUNNING.value]
    batches.sort(key=lambda b: str(b.get("started_at") or ""))
    batches = batches[-max(1, int(limit)) :]

    points: list[dict[str, Any]] = []
    for b in batches:
        sc = b.get("status_counts") or {}
        points.append(
            {
                "batch_id": b.get("batch_id"),
                "started_at": b.get("started_at"),
                "state": b.get("state"),
                "total": sum(sc.values()),
                ExecStatus.PASS.value: sc.get(ExecStatus.PASS.value, 0),
                ExecStatus.FAIL.value: sc.get(ExecStatus.FAIL.value, 0),
                "effective_total": (
                    sc.get(ExecStatus.PASS.value, 0) + sc.get(ExecStatus.FAIL.value, 0)
                ),
                "pass_rate": _pass_rate(sc),
                ExecStatus.STRUCTURAL_ONLY.value: sc.get(ExecStatus.STRUCTURAL_ONLY.value, 0),
                ExecStatus.BLOCKED_AUTH.value: sc.get(ExecStatus.BLOCKED_AUTH.value, 0),
                ExecStatus.ERROR.value: sc.get(ExecStatus.ERROR.value, 0),
            }
        )

    rates = [p["pass_rate"] for p in points if p["pass_rate"] is not None]
    direction = "stable"
    if len(rates) >= 2:
        if rates[-1] > rates[-2]:
            direction = "improving"
        elif rates[-1] < rates[-2]:
            direction = "regressing"
    return {
        "ok": True,
        "count": len(points),
        "pass_rate_direction": direction,
        "points": points,
    }


def flaky(pid: int, *, min_batches: int = 2, limit: int = 5000) -> dict[str, Any]:
    """跨批次结果不一致的用例（flaky 识别）。

    同一用例（case_id/tp_id）在 ≥min_batches 个批次中出现过 ≥2 种结论
    （pass/fail/error 之间摇摆）即判为不稳定；按涉及批次数降序。
    """
    rows = store.run_case_histories(pid, limit=limit)
    by_case: dict[tuple[Any, Any], dict[str, Any]] = defaultdict(
        lambda: {"batches": set(), "statuses": set(), "last_status": "", "last_at": ""}
    )
    for r in rows:
        key = (r.get("case_id"), r.get("tp_id"))
        rec = by_case[key]
        rec["batches"].add(r.get("batch_id"))
        rec["statuses"].add(r.get("status"))
        if str(r.get("created_at") or "") >= rec["last_at"]:
            rec["last_at"] = str(r.get("created_at") or "")
            rec["last_status"] = str(r.get("status") or "")

    flaky_list: list[dict[str, Any]] = []
    for (case_id, tp_id), rec in by_case.items():
        statuses = rec["statuses"] & set(_VERDICT_STATUSES)
        if len(rec["batches"]) >= max(2, int(min_batches)) and len(statuses) >= 2:
            flaky_list.append(
                {
                    "case_id": case_id,
                    "tp_id": tp_id,
                    "batch_count": len(rec["batches"]),
                    "batches": sorted(str(b) for b in rec["batches"] if b),
                    "statuses": sorted(statuses),
                    "last_status": rec["last_status"],
                }
            )
    flaky_list.sort(key=lambda x: (-x["batch_count"], str(x["tp_id"])))
    return {
        "ok": True,
        "count": len(flaky_list),
        "min_batches": max(2, int(min_batches)),
        "flaky": flaky_list,
    }
