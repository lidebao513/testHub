"""任务与运营路由：任务清单/进度/取消/用例、webhook、metrics、统一智能输入框解析。

从 service/app.py 拆出（#439 上帝文件拆分批次 4）。被测试 patch 的 task_store /
executor / _task_accepted / _run_generation_job 等保留在 app.py 门面，本模块通过
``A.xxx`` 运行时引用。鉴权由 app.py include_router 统一注入。
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from core.auto_input import parse_auto_input
from core.contracts import CONTRACT_VERSION
from core.enums import ALL_TP_TYPES, MODE_FULL
from core.errors import NotFoundError, ValidationError
from service import app as A
from service.tasks import TaskState


router: APIRouter = APIRouter()


class WebhookRequest(BaseModel):
    """发布流水线 webhook 载荷（仅携带触发生成所需的最小信息，不含凭证）。"""

    event: str = Field("", description="发布事件类型，如 deployment.success")
    target: str = Field("", description="被测目标名（用作 project_name）")
    env: str = Field("", description="环境：staging / prod")
    commit: str = Field("", description="刚发布的 commit（增量对比的 target ref）")
    base_url: str = Field("", description="已发布实例地址（生成用例时作 base_url 上下文）")
    health_url: str = Field("", description="存活探针地址（仅记录，本服务不主动探活执行）")
    callback_url: str = Field("", description="回调地址（预留；当前生成完成不主动回调）")
    repo_url: str = Field("", description="仓库地址（F1 取码）")
    local_path: str = Field("", description="本地目录（优先于 repo_url）")
    scopes: list[str] = Field(default_factory=list, description="行为维度范围：正常/异常/安全/边界")
    mode: str = Field("", description="full / incremental")
    execute: bool = Field(
        False,
        description="生成完成后是否紧接着执行验证（默认否：仅生成用例）。需被测实例已就绪",
    )


class ParseInputRequest(BaseModel):
    text: str = Field("", description="混合自由文本（URL / 凭据 / 权限 / 说明）")


@router.get("/api/v1/tasks")
def list_tasks(limit: int = 20) -> dict[str, Any]:
    """任务清单（最近优先），便于运营后台 / 流水线巡检。"""
    tasks = A.task_store.list_recent(limit=limit)
    return {"tasks": [t.to_dict() for t in tasks]}


@router.get("/api/v1/tasks/{task_id}")
def get_task(task_id: str) -> dict[str, Any]:
    """轮询任务进度 / 结果。终态（success/failed/cancelled）后 result / error 落地。"""
    task = A.task_store.get(task_id)
    if task is None:
        raise NotFoundError(f"任务不存在: {task_id}")
    return task.to_dict()


@router.post("/api/v1/tasks/{task_id}/cancel")
def cancel_task(task_id: str) -> dict[str, Any]:
    """取消运行中/排队中的任务（协作式：作业在阶段边界检出后中断）。"""
    cancelled = A.task_store.cancel(task_id)
    return {"ok": True, "cancelled": cancelled, "task_id": task_id}


@router.get("/api/v1/tasks/{task_id}/cases")
def task_cases(task_id: str) -> dict[str, Any]:
    """取某任务对应项目已生成的用例（前端详情面板用）。"""
    task = A.task_store.get(task_id)
    if task is None:
        raise NotFoundError(f"任务不存在: {task_id}")
    pid = task.project_id
    if not pid:
        return {"ok": True, "task_id": task_id, "project_id": None, "cases": []}
    cases = A.store.list_cases(pid, include_obsolete=True)
    return {"ok": True, "task_id": task_id, "project_id": pid, "cases": cases}


@router.post("/api/v1/verify/webhook")
def verify_webhook(req: WebhookRequest) -> JSONResponse:
    """发布流水线 webhook：收到 deployment.success → 触发「生成测试用例」任务。

    默认只生成用例、不执行；execute=true 时生成完成后执行只读验证。
    幂等：同一 (target, commit, mode, scopes, execute) 的重复事件只跑一次。
    """
    invalid = set(req.scopes) - set(ALL_TP_TYPES)
    if invalid:
        raise ValidationError(f"非法行为维度：{sorted(invalid)}，允许 {ALL_TP_TYPES}")
    idem = "wh:" + "|".join(
        [
            req.target or req.repo_url,
            req.commit,
            req.mode or MODE_FULL,
            ",".join(sorted(req.scopes)),
            "exec" if req.execute else "gen",
        ]
    )
    existing = A.task_store.find_by_idempotency(idem)
    if existing and existing.state != TaskState.CANCELLED.value:
        return A._task_accepted(existing, status_code=202, reused=True)
    opts = A.pipeline.default_options(req.local_path or "", mode=req.mode or MODE_FULL)
    if req.repo_url:
        opts.repo_url = req.repo_url
    if req.base_url:
        opts.target_req.base_url = req.base_url
    if req.target:
        opts.project_name = req.target
    if req.scopes:
        opts.scopes = set(req.scopes)
    opts.persist = True
    kind = "webhook_execute" if req.execute else "webhook_generate"
    task = A.task_store.create(
        kind=kind,
        idempotency_key=idem,
        request=json.dumps(req.model_dump(), ensure_ascii=False),
    )
    A.executor.submit(
        task.task_id, lambda: A._run_generation_job(task.task_id, opts, force_execute=req.execute)
    )
    return A._task_accepted(task, status_code=202)


@router.get("/api/v1/metrics")
def metrics() -> dict[str, Any]:
    """监控端点：任务总量 / 按状态分布 / 队列深度 / 最近任务。"""
    counts = A.task_store.counts_by_state()
    recent = A.task_store.list_recent(5)
    return {
        "service": "testgen-service",
        "contract_version": CONTRACT_VERSION,
        "tasks_total": sum(counts.values()),
        "by_state": counts,
        "queue_depth": counts.get(TaskState.PENDING.value, 0)
        + counts.get(TaskState.RUNNING.value, 0),
        "recent": [t.to_dict() for t in recent],
    }


@router.post("/api/v1/parse-input")
def parse_input(req: ParseInputRequest) -> dict[str, Any]:
    """统一智能输入框：只解析混排文本，返回掩码视图（可用于前端实时预览）。

    绝不回显明文凭证——前端据此确认「识别对不对」，而不是把密码再抄一遍。
    """
    parsed = parse_auto_input(req.text)
    return {
        "recognized": parsed.recognized_fields(),
        "parsed": parsed.redacted(),
    }
