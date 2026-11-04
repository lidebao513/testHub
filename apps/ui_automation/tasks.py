"""闭环异步任务（#200）：把 #183 的闭环编排接到 Celery，供 UI 一键触发。

闭环跑真实浏览器（秒~分钟级），必须异步：
- `run_closed_loop_task.delay(case_ids, project_id, max_retries)` 由 API 端点 fire-and-forget 触发；
- 任务内部复用 #183 的 `ClosedLoopOrchestrator`（真实 execute/reflow 接线）；
- 结果与报告落 `data/closed_loop_reports/`（与 run_closed_loop 命令同目录约定）。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from celery import shared_task
from django.utils import timezone

from apps.ui_automation.closed_loop import (
    ClosedLoopOrchestrator,
    build_loop_report,
    execute_aicase_real,
    reflow_aicase_real,
    render_loop_report_md,
    summarize_loop,
)

logger = logging.getLogger(__name__)
DEFAULT_OUT_DIR = "data/closed_loop_reports"


@shared_task(bind=True)
def run_closed_loop_task(self, case_ids=None, project_id=None, max_retries: int = 0):
    """异步跑闭环：消费 AICase 作业 → 编排 → 回流 → 落报告。

    参数：
      case_ids     AICase id 列表（优先）
      project_id   指定项目下全部 AICase（未给 case_ids 时）
      max_retries  对 infra error 的最大重试次数（传给 Orchestrator）
    """
    from apps.ui_automation.models import AICase

    qs = AICase.objects.all()
    if case_ids:
        qs = qs.filter(id__in=list(case_ids))
    elif project_id:
        qs = qs.filter(project_id=project_id)
    else:
        logger.warning("闭环任务无作业来源（无 case_ids 也无 project_id）")
        return {"summary": {"total": 0}, "items": [], "files": {}}

    jobs = [
        {"case_id": c.id, "name": c.name, "task_description": c.task_description}
        for c in qs
    ]
    orchestrator = ClosedLoopOrchestrator(
        execute_fn=execute_aicase_real,
        reflow_fn=reflow_aicase_real,
        max_retries=int(max_retries or 0),
    )
    started_at = timezone.now()
    items = orchestrator.run(jobs)
    duration = (timezone.now() - started_at).total_seconds()
    summary = summarize_loop(items)
    report = build_loop_report(items, summary, started_at, duration)
    paths = _persist_report(report)
    return {"summary": summary, "items": report["items"], "files": paths}


def _persist_report(report: dict) -> dict:
    """把闭环报告落盘（JSON + MD），与 run_closed_loop 命令同目录约定。"""
    out = Path(DEFAULT_OUT_DIR)
    out.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = out / f"closed_loop_{ts}.json"
    md_path = out / f"closed_loop_{ts}.md"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    md_path.write_text(render_loop_report_md(report), encoding="utf-8")
    return {"json": str(json_path), "md": str(md_path)}
