"""报告能力（F15）：把「用例 → 执行 → 结论」变成一份**可交付、可复现**的报告。

设计要点
--------
1. **报告是 DB 事实的纯函数**：取数在 `core.store.report_snapshot`，计算在本模块，
   渲染在 `output.report_writer`。同一份数据任意时刻重算，结论一致（可复现）。
   本模块**不新建 `reports` 表、不落"报告快照"**——避免与 `runs` / `run_batches`
   形成第二真值源。legacy 需要 `reports` 表，是因为它把执行明细只存在报告 JSON 里；
   本服务的明细已经在 `runs`，报告随时可重算。
2. **口径与执行器一致**：`pass_rate = pass / (pass + fail)`，分母**只算真正跑出结论**的用例；
   `skipped` / `error` / 环境态**不计入**通过率，但**必须出现在摘要里**——
   否则「执行器没跑」会被读者误读成「全绿」。
3. **不粉饰**：UI 层用例因 F13 未实现而 skipped，必须如实写明原因，不得让人以为已通过。
4. **导出格式**：json / md / html（自包含浅色页面）。PDF / Word / Excel 需要额外依赖
   （reportlab / python-docx / openpyxl），不在本批引入——需要时按可选依赖扩展
   `ReportFormat`（见任务清单 §十一）。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from core import store
from core.contracts import CONTRACT_VERSION, REPORT_VERSION
from core.enums import CaseLifecycleStatus, ExecStatus, RunBatchState, Tag
from core.errors import NotFoundError
from output.report_writer import ReportWriter


# 不参与「在用」统计的生命周期状态（obsolete/archived 不进入执行与覆盖）
_INACTIVE_STATUSES = (CaseLifecycleStatus.OBSOLETE.value, CaseLifecycleStatus.ARCHIVED.value)
# 缺口明细一次最多返回多少条（防止报告 JSON 被撑爆；完整清单以 DB 为准）
_DEFAULT_GAP_LIMIT = 50

_DIRECTION_TEXT = {"improving": "改善", "regressing": "退化", "stable": "持平"}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _rate(part: int, whole: int) -> float:
    return round(100.0 * part / whole, 1) if whole else 0.0


def _count_by(items: list[Any], key: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    for item in items:
        k = str(key(item))
        out[k] = out.get(k, 0) + 1
    return out


# ============================================================================
# 通过率 / 趋势 / flaky
# ============================================================================
def pass_rate(status_counts: Mapping[str, Any]) -> float | None:
    """真实通过率 = pass / (pass + fail)。

    无判定类结论时返回 `None`（而不是 0）——0 会被误读成「全部失败」，
    而事实往往是「一条都没真正跑」。
    """
    passed = int(status_counts.get(ExecStatus.PASS.value, 0) or 0)
    failed = int(status_counts.get(ExecStatus.FAIL.value, 0) or 0)
    effective = passed + failed
    return round(100.0 * passed / effective, 1) if effective else None


def trend(batches: list[dict[str, Any]]) -> dict[str, Any]:
    """多批次执行趋势（按开始时间升序）。

    每个点给「通过率 + 状态分布」；方向取**最近两次有效通过率**的比较结果。
    """
    points: list[dict[str, Any]] = []
    for b in batches:
        status_counts = b.get("status_counts") or {}
        passed = int(status_counts.get(ExecStatus.PASS.value, 0) or 0)
        failed = int(status_counts.get(ExecStatus.FAIL.value, 0) or 0)
        points.append(
            {
                "batch_id": str(b.get("batch_id") or ""),
                "state": str(b.get("state") or ""),
                "started_at": str(b.get("started_at") or ""),
                "finished_at": str(b.get("finished_at") or ""),
                "total": int(b.get("total") or 0),
                "done": int(b.get("done") or 0),
                "pass": passed,
                "fail": failed,
                "error": int(status_counts.get(ExecStatus.ERROR.value, 0) or 0),
                "skipped": int(status_counts.get(ExecStatus.SKIPPED.value, 0) or 0),
                "effective_total": passed + failed,
                "pass_rate": pass_rate(status_counts),
            }
        )
    rates = [p["pass_rate"] for p in points if p["pass_rate"] is not None]
    direction = "stable"
    if len(rates) >= 2:
        if rates[-1] > rates[-2]:
            direction = "improving"
        elif rates[-1] < rates[-2]:
            direction = "regressing"
    return {"count": len(points), "direction": direction, "points": points}


def flaky(run_index: list[dict[str, Any]], *, min_batches: int = 2) -> dict[str, Any]:
    """跨批次结论不一致的用例（flaky 识别）。

    「结论不一致」= 同一测试点既出现过 pass，又出现过 fail / error；
    只跑过 1 个批次的测试点不参与判定（样本不足）。
    """
    by_tp: dict[str, list[tuple[str, str]]] = {}
    for row in run_index:
        key = str(row.get("tp_id") or row.get("case_id") or "")
        if not key:
            continue
        by_tp.setdefault(key, []).append(
            (str(row.get("batch_id") or ""), str(row.get("status") or ""))
        )

    hits: list[dict[str, Any]] = []
    for tp_id, records in by_tp.items():
        batch_ids = {b for b, _ in records if b}
        if len(batch_ids) < min_batches:
            continue
        statuses = {s for _, s in records}
        changed = ExecStatus.PASS.value in statuses and (
            ExecStatus.FAIL.value in statuses or ExecStatus.ERROR.value in statuses
        )
        if changed:
            hits.append({"tp_id": tp_id, "batches": len(batch_ids), "statuses": sorted(statuses)})
    hits.sort(key=lambda c: (-int(c["batches"]), str(c["tp_id"])))
    return {
        "min_batches": min_batches,
        "evaluated": len(by_tp),
        "count": len(hits),
        "cases": hits,
    }


# ============================================================================
# 覆盖率（F16）：功能点 → 测试点 → 用例
# ============================================================================
def _module_coverage(
    fps: list[dict[str, Any]],
    tps: list[dict[str, Any]],
    fp_with_tp: set[str],
    tp_with_case: set[str],
) -> dict[str, dict[str, Any]]:
    buckets: dict[str, dict[str, int]] = {}

    def bucket_of(name: Any) -> dict[str, int]:
        return buckets.setdefault(
            str(name or "未分类"),
            {"fp_total": 0, "fp_covered": 0, "tp_total": 0, "tp_covered": 0},
        )

    for fp in fps:
        b = bucket_of(fp.get("module"))
        b["fp_total"] += 1
        if str(fp.get("contract_id")) in fp_with_tp:
            b["fp_covered"] += 1
    for tp in tps:
        b = bucket_of(tp.get("module"))
        b["tp_total"] += 1
        if str(tp.get("tp_id")) in tp_with_case:
            b["tp_covered"] += 1

    out: dict[str, dict[str, Any]] = {}
    for name, b in buckets.items():
        out[name] = {
            "fp_total": b["fp_total"],
            "fp_covered": b["fp_covered"],
            "fp_rate": _rate(b["fp_covered"], b["fp_total"]) if b["fp_total"] else 100.0,
            "tp_total": b["tp_total"],
            "tp_covered": b["tp_covered"],
            "tp_rate": _rate(b["tp_covered"], b["tp_total"]) if b["tp_total"] else 100.0,
        }
    return out


def coverage(
    fps: list[dict[str, Any]],
    tps: list[dict[str, Any]],
    cases: list[dict[str, Any]],
    *,
    limit: int = _DEFAULT_GAP_LIMIT,
) -> dict[str, Any]:
    """覆盖率：FP → TP → Case 三级，并定位未覆盖缺口（F16）。

    - **FP 覆盖率**：有 ≥1 个测试点指回的功能点占比（功能点零测试点 = 缺口）；
    - **TP 覆盖率**：有 ≥1 条在用用例的测试点占比（测试点零用例 = 缺口）。
    """
    fp_ids = {str(f["contract_id"]) for f in fps if f.get("contract_id")}
    tp_ids = {str(t["tp_id"]) for t in tps if t.get("tp_id")}
    fp_with_tp = {str(t["fp_contract_id"]) for t in tps if t.get("fp_contract_id")}
    active = [c for c in cases if str(c.get("status") or "") not in _INACTIVE_STATUSES]
    tp_with_case = {str(c["tp_id"]) for c in active if c.get("tp_id")}

    fp_covered = len(fp_with_tp & fp_ids)
    tp_covered = len(tp_with_case & tp_ids)
    uncovered_fps = [
        {
            "contract_id": f.get("contract_id"),
            "name": f.get("name"),
            "module": f.get("module"),
            "ftype": f.get("ftype"),
        }
        for f in fps
        if str(f.get("contract_id")) not in fp_with_tp
    ]
    uncovered_tps = [
        {
            "tp_id": t.get("tp_id"),
            "title": t.get("title"),
            "module": t.get("module"),
            "fp_contract_id": t.get("fp_contract_id"),
        }
        for t in tps
        if str(t.get("tp_id")) not in tp_with_case
    ]
    return {
        "fp_total": len(fp_ids),
        "fp_covered": fp_covered,
        "fp_rate": _rate(fp_covered, len(fp_ids)),
        "tp_total": len(tp_ids),
        "tp_covered": tp_covered,
        "tp_rate": _rate(tp_covered, len(tp_ids)),
        "case_total": len(cases),
        "case_active": len(active),
        "uncovered_fp_count": len(uncovered_fps),
        "uncovered_tp_count": len(uncovered_tps),
        "uncovered_fps": uncovered_fps[:limit],
        "uncovered_tps": uncovered_tps[:limit],
        "module_coverage": _module_coverage(fps, tps, fp_with_tp, tp_with_case),
    }


# ============================================================================
# 执行摘要（给 PM / 领导的一句话结论 + 要点）
# ============================================================================
def _headline(context: Mapping[str, Any], metrics: Mapping[str, Any]) -> str:
    batch = context.get("batch")
    if batch is None:
        return "尚未执行：本轮只完成了用例生成，没有任何执行结论（执行后再出报告才有通过率）。"
    if str(batch.get("state")) == RunBatchState.FAILED.value:
        return "执行未跑起来：本批次在执行基础设施层失败，没有任何单条结论，报告不含通过率。"
    rate = metrics.get("pass_rate")
    if rate is None:
        return (
            f"本批次 {metrics.get('total', 0)} 条用例均未产生判定类结论"
            "（全部跳过 / 异常 / 环境态），暂无可信通过率。"
        )
    if rate >= 90:
        return f"整体健康：真实通过率 {rate}%（{metrics.get('pass', 0)} 通过 / {metrics.get('fail', 0)} 失败）。"
    if rate >= 60:
        return f"基本可用：真实通过率 {rate}%，存在部分失败需关注。"
    return f"存在问题：真实通过率仅 {rate}%，失败较多，建议优先排查。"


def _coverage_bullet(coverage_info: Mapping[str, Any]) -> str:
    gap = int(coverage_info.get("uncovered_tp_count", 0) or 0)
    rate = coverage_info.get("tp_rate")
    if rate is None:
        return "覆盖率：暂无测试点。"
    if gap:
        return f"测试点覆盖率 {rate}%（缺口 {gap} 个测试点未落用例），建议补齐。"
    return f"测试点覆盖率 {rate}%，无未覆盖测试点。"


def _trend_bullet(trend_info: Mapping[str, Any]) -> str:
    direction = str(trend_info.get("direction") or "stable")
    return f"趋势：通过率较上一批次{_DIRECTION_TEXT.get(direction, '持平')}。"


def _flaky_bullet(flaky_info: Mapping[str, Any]) -> str:
    count = int(flaky_info.get("count") or 0)
    if count:
        return f"发现 {count} 个不稳定（flaky）用例，建议加固（隔离外部依赖 / 增加重试）。"
    return "未检测到不稳定（flaky）用例。"


def _problem_bullets(metrics: Mapping[str, Any]) -> list[str]:
    out: list[str] = []
    if metrics.get("skipped"):
        out.append(
            f"跳过 {metrics['skipped']} 条**不等于通过**：主要来自 UI 层执行器尚未实现（F13）、"
            "写操作未放行、或非 HTTP 来源。"
        )
    if metrics.get("blocked_auth"):
        out.append(f"需鉴权 {metrics['blocked_auth']} 条（返回 401/403），建议配置测试令牌后补跑。")
    if metrics.get("error"):
        out.append(f"执行异常 {metrics['error']} 条，需查看执行器日志定位内部错误。")
    if metrics.get("blocked_review"):
        out.append(f"待审批 {metrics['blocked_review']} 条（REVIEW_GATE 开启），需先完成用例审批。")
    return out


def _bullets_without_batch(context: Mapping[str, Any]) -> list[str]:
    counts = dict(context.get("counts") or {})
    return [
        f"已生成用例 {counts.get('cases_active', 0)} 条 / 测试点 {counts.get('test_points', 0)} 条 / "
        f"功能点 {counts.get('functional_points', 0)} 个，但**尚未执行**。",
        "执行入口：`pipeline --execute --exec-url <被测地址>`；执行后重出报告即可得到通过率与证据。",
        _coverage_bullet(context.get("coverage") or {}),
    ]


def _bullets(context: Mapping[str, Any], metrics: Mapping[str, Any]) -> list[str]:
    batch = context.get("batch")
    if batch is None:
        return _bullets_without_batch(context)
    out: list[str] = []
    if str(batch.get("state")) == RunBatchState.FAILED.value:
        out.append(
            f"批次 `{batch.get('batch_id')}` 终态 failed，错误：{batch.get('error') or '(未记录)'}"
        )
    out.append(
        f"通过 {metrics.get('pass', 0)} / 失败 {metrics.get('fail', 0)} / "
        f"异常 {metrics.get('error', 0)} / 跳过 {metrics.get('skipped', 0)}"
        f"（共 {metrics.get('total', 0)} 条）。"
    )
    out += _problem_bullets(metrics)
    out.append(_coverage_bullet(context.get("coverage") or {}))
    out.append(_trend_bullet(context.get("trend") or {}))
    out.append(_flaky_bullet(context.get("flaky") or {}))
    return out


def exec_summary(context: Mapping[str, Any]) -> dict[str, Any]:
    """执行摘要：一句话结论 + 要点 + 关键指标。"""
    status_counts = dict(context.get("status_counts") or {})
    batch = context.get("batch")
    passed = int(status_counts.get(ExecStatus.PASS.value, 0) or 0)
    failed = int(status_counts.get(ExecStatus.FAIL.value, 0) or 0)
    metrics: dict[str, Any] = {
        "total": int((batch or {}).get("total") or 0),
        "pass": passed,
        "fail": failed,
        "effective_total": passed + failed,
        "pass_rate": pass_rate(status_counts),
        "error": int(status_counts.get(ExecStatus.ERROR.value, 0) or 0),
        "skipped": int(status_counts.get(ExecStatus.SKIPPED.value, 0) or 0),
        "blocked_auth": int(status_counts.get(ExecStatus.BLOCKED_AUTH.value, 0) or 0),
        "structural_only": int(status_counts.get(ExecStatus.STRUCTURAL_ONLY.value, 0) or 0),
        "blocked_review": int(status_counts.get(ExecStatus.BLOCKED_REVIEW.value, 0) or 0),
    }
    return {
        "headline": _headline(context, metrics),
        "bullets": _bullets(context, metrics),
        "metrics": metrics,
    }


# ============================================================================
# 复用占比 / 证据
# ============================================================================
def _reuse(cases: list[dict[str, Any]]) -> dict[str, Any]:
    """用例复用占比：在用用例里「沿用（全量）」与「更新（增量）」的构成。"""
    active = [c for c in cases if str(c.get("status") or "") not in _INACTIVE_STATUSES]
    updated = sum(1 for c in active if str(c.get("test_type") or "") == Tag.UPDATE.value)
    reused = len(active) - updated
    return {
        "lifecycle": _count_by(cases, lambda c: str(c.get("status") or "(未标记)")),
        "test_type": _count_by(cases, lambda c: str(c.get("test_type") or "(未标记)")),
        "active": len(active),
        "reused": reused,
        "updated": updated,
        "reuse_rate": _rate(reused, len(active)),
    }


def _evidence_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "case_id": row.get("case_id"),
        "tp_id": row.get("tp_id") or "",
        "fp_contract_id": row.get("fp_contract_id") or "",
        "module": row.get("case_module") or "",
        "title": row.get("case_title") or row.get("tp_title") or "",
        "status": row.get("status") or "",
        "detail": row.get("detail") or "",
        "duration_ms": int(row.get("duration_ms") or 0),
        "screenshot_path": row.get("screenshot_path") or "",
        "log_path": row.get("log_path") or "",
    }


# ============================================================================
# 主入口
# ============================================================================
def build_report(pid: int, *, batch_id: str = "") -> dict[str, Any]:
    """构建报告（只读 DB，不写任何文件）。项目 / 批次不存在时抛 `NotFoundError`。"""
    snapshot = store.report_snapshot(pid, batch_id=batch_id)
    project = snapshot.get("project")
    if not project:
        raise NotFoundError(f"项目不存在：{pid}")
    batch = snapshot.get("batch")
    if batch_id and batch is None:
        raise NotFoundError(f"执行批次不存在：{batch_id}")

    fps = snapshot.get("functional_points") or []
    tps = snapshot.get("test_points") or []
    cases = snapshot.get("cases") or []
    coverage_info = coverage(fps, tps, cases)
    trend_info = trend(snapshot.get("batches") or [])
    flaky_info = flaky(snapshot.get("run_index") or [])
    active = [c for c in cases if str(c.get("status") or "") not in _INACTIVE_STATUSES]
    counts = {
        "functional_points": len(fps),
        "test_points": len(tps),
        "cases": len(cases),
        "cases_active": len(active),
    }
    context: dict[str, Any] = {
        "batch": batch,
        "status_counts": (batch or {}).get("status_counts") or {},
        "coverage": coverage_info,
        "trend": trend_info,
        "flaky": flaky_info,
        "counts": counts,
    }
    return {
        "report_version": REPORT_VERSION,
        "contract_version": CONTRACT_VERSION,
        "project": {
            "id": pid,
            "name": project.get("name"),
            "local_path": project.get("local_path"),
            "base_url": project.get("base_url"),
        },
        "generated_at": _now(),
        "batch": batch,
        "counts": counts,
        "exec_summary": exec_summary(context),
        "coverage": coverage_info,
        "trend": trend_info,
        "flaky": flaky_info,
        "traceability": snapshot.get("traceability") or {},
        "evidence": [_evidence_row(r) for r in (snapshot.get("runs") or [])],
        "change": snapshot.get("last_change"),
        "reuse": _reuse(cases),
    }


def generate(
    pid: int,
    *,
    batch_id: str = "",
    out_dir: str | Path | None = None,
    write: bool = True,
) -> dict[str, Any]:
    """构建报告并（默认）落盘 `REPORT.md` / `REPORT.html` / `report.json`。

    返回 `{"report": <报告结构>, "outputs": {"markdown": ..., "html": ..., "json": ...}}`。
    """
    report = build_report(pid, batch_id=batch_id)
    outputs = ReportWriter(base=out_dir).write(pid, report) if write else {}
    return {"report": report, "outputs": outputs}
