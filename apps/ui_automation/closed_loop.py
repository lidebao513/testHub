"""生成→执行→回流 闭环编排（#183）。

把「生成的 AI 用例 → 派发执行 → 三层断言 → 结果回流」收口成一个可观测、可重试的
状态机。模块本身是**纯逻辑 + 注入式步骤**，便于在无 Django / 无浏览器环境下单测；
真实执行由 management 命令通过注入的 ``execute_fn`` 接线 ``run_full_process_sync``。

设计约定（避免破坏各模块独立能力）：
- 生成是上游生产者（testgen / 手工 AICase），闭环只消费 ``AICase`` 作业；
- 执行落 ``AIExecutionRecord``（已承载 status + assertion_report，天然持久化）；
- 回流把「闭环判定」写回 ``AICase.last_loop``（JSON，可追溯）+ 产出本轮报告；
- 仅对**基础设施类错误**（verdict=error）做有界重试，避免对真实失败无限循环。

状态机（闭环核心）：
    PENDING → DISPATCHED → EXECUTED → ASSERTED → REFLOWED → 终态(DONE/WARN/FAILED/ERROR/SKIPPED)
    ERROR/RETRY 为回退边：RETRY 把作业送回 DISPATCHED 重新执行。
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 状态机
# ---------------------------------------------------------------------------


class LoopState(str, Enum):
    """闭环单作业生命周期状态。"""

    PENDING = "pending"
    DISPATCHED = "dispatched"
    EXECUTED = "executed"
    ASSERTED = "asserted"
    REFLOWED = "reflowed"
    DONE = "done"        # 通过（执行通过且断言未抓到问题）
    WARN = "warn"        # 执行通过但断言不确定（需人工看）
    FAILED = "failed"    # 真实失败（执行失败 或 断言抓到真实问题）
    ERROR = "error"      # 基础设施类错误（可重试）
    SKIPPED = "skipped"  # 用户停止等被动跳过
    RETRY = "retry"      # 回退边（瞬时态，送回 DISPATCHED）

    @property
    def is_terminal(self) -> bool:
        return self in (
            LoopState.DONE,
            LoopState.WARN,
            LoopState.FAILED,
            LoopState.ERROR,
            LoopState.SKIPPED,
        )


# 合法状态转移表（用于 can_transition 校验，运行时违规仅告警不崩）
_TRANSITIONS: dict[LoopState, set[LoopState]] = {
    LoopState.PENDING: {LoopState.DISPATCHED, LoopState.SKIPPED, LoopState.ERROR},
    LoopState.DISPATCHED: {LoopState.EXECUTED, LoopState.ERROR, LoopState.RETRY},
    LoopState.EXECUTED: {LoopState.ASSERTED, LoopState.ERROR},
    LoopState.ASSERTED: {
        LoopState.REFLOWED,
        LoopState.DONE,
        LoopState.WARN,
        LoopState.FAILED,
        LoopState.ERROR,
        LoopState.SKIPPED,
    },
    LoopState.REFLOWED: {
        LoopState.DONE,
        LoopState.WARN,
        LoopState.FAILED,
        LoopState.ERROR,
        LoopState.SKIPPED,
    },
    LoopState.RETRY: {LoopState.DISPATCHED},
    LoopState.DONE: set(),
    LoopState.WARN: set(),
    LoopState.FAILED: set(),
    LoopState.ERROR: set(),
    LoopState.SKIPPED: set(),
}


def can_transition(frm: LoopState, to: LoopState) -> bool:
    """校验状态转移是否合法。"""
    allowed = _TRANSITIONS.get(frm, set())
    return to in allowed


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class ExecutionResult:
    """一次执行的结果（由 execute_fn 返回）。

    status: passed / failed / stopped / error
    assertion_report: #181 三层断言报告 dict（含 quality_signal）
    history: browser-use 返回的 AgentHistoryList（或 None）
    error: 异常信息（status=error 时）
    record_id: 关联的 AIExecutionRecord.id（真实执行回填，便于回流溯源）
    """

    status: str
    assertion_report: dict = field(default_factory=dict)
    history: Any = None
    error: str = ""
    record_id: int | None = None


@dataclass
class ReflowResult:
    verdict: str
    note: str = ""


@dataclass
class LoopItem:
    """闭环单作业的运行态（含状态历史，便于审计与重试判定）。"""

    case_id: Any
    name: str = ""
    task_description: str = ""
    state: LoopState = LoopState.PENDING
    state_history: list = field(default_factory=list)
    attempt: int = 0
    exec_status: str = ""
    assertion_report: dict = field(default_factory=dict)
    assertion_signal: str = "none"
    verdict: str = ""
    reflow_note: str = ""
    error: str = ""

    def set_state(self, state: LoopState, *, strict: bool = False) -> None:
        if self.state != state and strict and not can_transition(self.state, state):
            logger.warning(
                "闭环状态转移不合法: %s → %s（case_id=%s），仍按目标态记录",
                self.state.value,
                state.value,
                self.case_id,
            )
        self.state = state
        self.state_history.append(state.value)


# ---------------------------------------------------------------------------
# 闭环核心：执行结果 + 断言信号 → 闭环判定
# ---------------------------------------------------------------------------


def combine_verdict(exec_status: str, assertion_signal: str) -> str:
    """把「执行器判定」与「三层断言信号」合并为闭环判定。

    闭环价值点：执行器自认通过、但断言抓到真实问题（如空白页 / 未导航到目标 /
    应显示文案缺失）→ 判定 fail（降级），而非盲目信任「AI 说跑完了」。

    - exec_status=stopped        → skipped（用户被动停止，不计失败）
    - exec_status=failed         → fail（执行器已判失败，以执行器为准）
    - exec_status=error(异常)    → error（基础设施类，可重试）
    - exec_status=passed:
        assertion=fail  → fail（断言抓到真实问题，闭环降级）
        assertion=warn  → warn（不确定，交人工）
        assertion=pass/none → pass
    """
    sig = (assertion_signal or "none").strip().lower()
    if exec_status == "stopped":
        return "skipped"
    if exec_status == "failed":
        # 执行器判失败；若断言也 fail 则一致，否则以执行器为准
        return "fail"
    if exec_status == "error":
        return "error"
    # exec_status == passed
    if sig == "fail":
        return "fail"
    if sig == "warn":
        return "warn"
    return "pass"


# 仅这些判定可重试（避免对真实失败/告警无限循环）
RETRYABLE_VERDICTS = {"error"}


# ---------------------------------------------------------------------------
# 编排器
# ---------------------------------------------------------------------------


class ClosedLoopOrchestrator:
    """生成→执行→回流 编排器。

    参数：
      execute_fn  注入式执行：``fn(job) -> ExecutionResult``
      reflow_fn   注入式回流：``fn(job, result, verdict) -> ReflowResult``
      max_retries 对 retryable 判定的最大重试次数（默认 1）
      on_item     可选回调：每作业状态变化后调用 ``fn(item)``（日志/可观测）
    """

    def __init__(
        self,
        execute_fn: Callable[[dict], ExecutionResult],
        reflow_fn: Callable[[dict, ExecutionResult, str], ReflowResult],
        *,
        max_retries: int = 1,
        on_item: Callable[[LoopItem], None] | None = None,
    ) -> None:
        self.execute_fn = execute_fn
        self.reflow_fn = reflow_fn
        self.max_retries = max(0, int(max_retries))
        self.on_item = on_item

    def run(self, jobs: list[dict]) -> list[LoopItem]:
        items: list[LoopItem] = []
        for job in jobs:
            item = self._process_one(job)
            items.append(item)
            if self.on_item:
                try:
                    self.on_item(item)
                except Exception as e:  # noqa: BLE001 - 回调失败不应中断整批
                    logger.warning("on_item 回调失败（忽略）: %s", e)
        return items

    def _process_one(self, job: dict) -> LoopItem:
        item = LoopItem(
            case_id=job.get("case_id"),
            name=job.get("name", "") or "",
            task_description=job.get("task_description", "") or "",
        )
        item.set_state(LoopState.PENDING)
        item.set_state(LoopState.DISPATCHED)
        while True:
            try:
                result = self.execute_fn(job)
            except Exception as e:  # noqa: BLE001 - 执行异常归为 error，进入重试分支
                logger.warning("闭环执行异常（case_id=%s）: %s", item.case_id, e)
                item.error = str(e)
                item.set_state(LoopState.ERROR)
                result = ExecutionResult(status="error", error=str(e))

            item.set_state(LoopState.EXECUTED)
            item.exec_status = result.status or ""
            item.assertion_report = result.assertion_report or {}
            item.assertion_signal = (item.assertion_report or {}).get(
                "quality_signal", "none"
            ) or "none"

            item.set_state(LoopState.ASSERTED)
            verdict = combine_verdict(item.exec_status, item.assertion_signal)
            item.verdict = verdict

            try:
                reflow = self.reflow_fn(job, result, verdict)
                item.reflow_note = reflow.note if reflow else ""
            except Exception as e:  # noqa: BLE001 - 回流失败不拖垮整批
                logger.warning("闭环回流异常（case_id=%s）: %s", item.case_id, e)
                item.reflow_note = f"reflow error: {e}"

            item.set_state(LoopState.REFLOWED)

            # 终态 / 重试分支
            if verdict in RETRYABLE_VERDICTS and item.attempt < self.max_retries:
                item.attempt += 1
                item.set_state(LoopState.RETRY)
                item.set_state(LoopState.DISPATCHED)
                continue

            terminal = {
                "pass": LoopState.DONE,
                "warn": LoopState.WARN,
                "fail": LoopState.FAILED,
                "error": LoopState.ERROR,
                "skipped": LoopState.SKIPPED,
            }.get(verdict, LoopState.FAILED)
            item.set_state(terminal)
            break
        return item


# ---------------------------------------------------------------------------
# 汇总与报告（纯函数，便于单测）
# ---------------------------------------------------------------------------


def summarize_loop(items: list[LoopItem]) -> dict:
    """汇总本轮闭环结果。

    返回 {total, done, warn, failed, error, skipped, pass_rate, signal}
    signal 取最差：有 failed/error → fail；否则有 warn → warn；否则 pass。
    """
    total = len(items)
    counts = {
        "done": 0,
        "warn": 0,
        "failed": 0,
        "error": 0,
        "skipped": 0,
    }
    for it in items:
        if it.state == LoopState.DONE:
            counts["done"] += 1
        elif it.state == LoopState.WARN:
            counts["warn"] += 1
        elif it.state == LoopState.FAILED:
            counts["failed"] += 1
        elif it.state == LoopState.ERROR:
            counts["error"] += 1
        elif it.state == LoopState.SKIPPED:
            counts["skipped"] += 1

    if counts["failed"] or counts["error"]:
        signal = "fail"
    elif counts["warn"]:
        signal = "warn"
    elif total == 0:
        signal = "none"
    else:
        signal = "pass"

    pass_rate = (counts["done"] / total * 100) if total else 0.0
    return {
        "total": total,
        "done": counts["done"],
        "warn": counts["warn"],
        "failed": counts["failed"],
        "error": counts["error"],
        "skipped": counts["skipped"],
        "pass_rate": round(pass_rate, 1),
        "signal": signal,
    }


def build_loop_report(
    items: list[LoopItem],
    summary: dict,
    started_at: datetime,
    duration: float,
) -> dict:
    """构造闭环报告 dict（落盘由命令负责）。"""
    details = []
    for it in items:
        details.append(
            {
                "case_id": it.case_id,
                "name": it.name,
                "state": it.state.value,
                "attempt": it.attempt,
                "exec_status": it.exec_status,
                "assertion_signal": it.assertion_signal,
                "verdict": it.verdict,
                "reflow_note": it.reflow_note,
                "error": it.error,
                "state_history": it.state_history,
            }
        )
    return {
        "started_at": started_at.isoformat(),
        "duration_seconds": round(duration, 2),
        "summary": summary,
        "items": details,
    }


def render_loop_report_md(report: dict) -> str:
    """渲染 Markdown 报告（给人看）。"""
    s = report["summary"]
    lines = [
        "# 生成→执行→回流 闭环报告",
        "",
        f"- 开始时间：{report['started_at']}",
        f"- 耗时：{report['duration_seconds']}s",
        f"- 总判定信号：**{s['signal']}**",
        (
            f"- 总数：{s['total']}　通过：{s['done']}　告警：{s['warn']}"
            f"　失败：{s['failed']}　异常：{s['error']}　跳过：{s['skipped']}"
        ),
        f"- 通过率：{s['pass_rate']}%",
        "",
        "## 明细",
        "",
        "| 用例 | 状态 | 尝试 | 执行 | 断言 | 判定 | 备注 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for d in report["items"]:
        name = (d["name"] or str(d["case_id"])).replace("|", "/")
        note = (d["reflow_note"] or d["error"] or "").replace("|", "/")[:60]
        lines.append(
            f"| {name} | {d['state']} | {d['attempt']} | {d['exec_status']} "
            f"| {d['assertion_signal']} | {d['verdict']} | {note} |"
        )
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 真实接线（延迟导入 Django，保持纯逻辑可单测）
# ---------------------------------------------------------------------------


def finalize_execution_record(
    record: Any,
    history: Any,
    agent: Any,
    *,
    captured_planned: list | None = None,
) -> str:
    """把一次真实执行的结果写进 AIExecutionRecord（status + 断言 + 用量归因）。

    判定口径与执行端点一致：planned_tasks 含 failed/error 或 history 步骤失败 → failed。
    断言/用量复用 ``apps.ui_automation.views`` 的只读函数，避免重复归因逻辑。
    整体只读 + 兜底：归因失败不改变执行判定。
    """
    failed = False
    planned = captured_planned if captured_planned is not None else (record.planned_tasks or [])
    if planned:
        failed = any(t.get("status") in ("failed", "error") for t in planned)
    if not failed and history:
        try:
            steps = history.steps if hasattr(history, "steps") else []
            for step in steps:
                if getattr(step, "status", None) == "failed":
                    failed = True
                    break
        except Exception:  # noqa: BLE001,S110
            pass

    record.status = "failed" if failed else "passed"

    try:
        from apps.ui_automation.views import (  # 延迟导入，避免循环
            apply_assertion_report,
            apply_usage_snapshot,
        )

        apply_usage_snapshot(record, agent)
        apply_assertion_report(record, agent)
    except Exception as e:  # noqa: BLE001
        logger.warning("执行记录归因失败（不影响判定）: %s", e)

    record.save()
    return record.status


def execute_aicase_real(
    case: Any,
    *,
    dry_run: bool = False,
    enable_gif: bool = False,
) -> ExecutionResult:
    """真实执行一个 AICase：建执行记录 → 跑 browser-use → 终态 + 断言。

    dry_run=True 时不启动浏览器，直接标记 passed + 通过断言（供 CI / 验证用），
    真实执行由带有效模型/浏览器的环境触发。
    """
    from apps.ui_automation.ai_agent import run_full_process_sync
    from apps.ui_automation.models import AIExecutionRecord

    record = AIExecutionRecord.objects.create(
        ai_case=case,
        case_name=case.name,
        task_description=case.task_description,
        status="running",
        project=case.project,
        source_generation_task=getattr(case, "source_generation_task", None),
    )

    if dry_run:
        record.status = "passed"
        record.assertion_report = {
            "quality_signal": "pass",
            "counts": {"ai": 0, "rule": 0, "session": 0},
        }
        record.save()
        return ExecutionResult(
            status="passed",
            assertion_report=record.assertion_report,
            record_id=record.id,
        )

    holder: dict = {}
    planned_holder: dict = {}

    def on_analysis(planned_tasks):
        planned_holder["tasks"] = planned_tasks

    def should_stop():
        return False

    try:
        history = run_full_process_sync(
            case.task_description,
            analysis_callback=on_analysis,
            step_callback=None,
            should_stop=should_stop,
            case_name=case.name,
            enable_gif=enable_gif,
            agent_holder=holder,
        )
    except Exception as e:  # noqa: BLE001 - 异常归为 error（可重试）
        logger.warning("AICase 执行异常（id=%s）: %s", case.id, e)
        record.status = "failed"
        record.logs += f"\n执行出错: {e}"
        record.save()
        return ExecutionResult(status="error", error=str(e), record_id=record.id)

    status = finalize_execution_record(
        record, history, holder.get("agent"), captured_planned=planned_holder.get("tasks")
    )
    return ExecutionResult(
        status=status,
        assertion_report=record.assertion_report or {},
        history=history,
        record_id=record.id,
    )


def reflow_aicase_real(job: dict, result: ExecutionResult, verdict: str) -> ReflowResult:
    """真实回流：把闭环判定写回 AICase.last_loop（可追溯）+ 返回 ReflowResult。"""
    from apps.ui_automation.models import AICase

    case = AICase.objects.filter(id=job.get("case_id")).first()
    if case is None:
        return ReflowResult(verdict, note="AICase not found")

    case.last_loop = {
        "state": verdict,
        "exec_status": result.status,
        "assertion_signal": (result.assertion_report or {}).get("quality_signal", "none"),
        "last_record_id": result.record_id,
        "updated_at": datetime.now(UTC).isoformat(),
    }
    case.save(update_fields=["last_loop"])
    return ReflowResult(verdict, note="reflowed")
