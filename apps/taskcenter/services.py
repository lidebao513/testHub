"""后台任务领域服务：落库、状态同步、重试、暂停、关闭。

约定：
- 触发点（testgen 生成/回流/判定）调用 ``record_*`` 写入任务记录；
- 列表/详情读取前调用 ``sync_statuses`` / ``sync_one`` 把在途任务状态从 sidecar 拉回，
  使管理页始终显示最新状态；sidecar 不可达时**保留现状**，不误翻成 failed；
- retry / pause / close 对应「重试失败任务 / 暂停运行中任务 / 关闭不需要任务」。
"""
from __future__ import annotations

import json
from typing import Any

from django.utils import timezone

from .models import BackgroundTask

# sidecar 状态 → 本模型状态
_SIDECAR_STATE_MAP = {
    "pending": "pending",
    "running": "running",
    "success": "success",
    "failed": "failed",
    "cancelled": "cancelled",
}

# 允许重试的任务类型 / 状态
_RETRYABLE_TYPES = {"generate"}
_RETRYABLE_STATUS = {"failed", "cancelled", "paused"}
_PAUSABLE_STATUS = {"pending", "running"}
# 在途状态（需轮询 sidecar 刷新）
_LIVE_STATUS = {"pending", "running"}


def _mask_secret(value: str) -> str:
    """凭据脱敏：仅保留前 4 后 2 位，中间以 **** 遮蔽。"""
    value = str(value or "")
    if len(value) <= 6:
        return "****"
    return f"{value[:4]}****{value[-2:]}"


# ============================================================================
# 记录（触发点调用）
# ============================================================================
def record_generate(
    *,
    user: Any,
    task_id: str,
    local_path: str = "",
    repo_url: str = "",
    scopes: list | None = None,
    changed_files: list | None = None,
    mode: str = "",
    name: str = "",
    project_id: int | None = None,
    project_name: str = "",
    gen_range: str = "",
    code_source: dict | None = None,
) -> BackgroundTask:
    """记录一次「代码生成」异步任务（pending，状态由 sync 拉回）。"""
    masked_cs: dict | None = None
    if code_source:
        masked_cs = {
            "credential_type": code_source.get("credential_type", ""),
            "org_id": code_source.get("org_id", ""),
            "access_key": _mask_secret(code_source.get("access_key", "")),
        }
        if code_source.get("secret"):
            masked_cs["secret"] = _mask_secret(code_source["secret"])
    summary = {
        "local_path": local_path,
        "repo_url": repo_url,
        "scopes": scopes or [],
        "changed_files": changed_files or [],
        "mode": mode,
        "gen_range": gen_range,
        "project_id": project_id,
        "project_name": project_name,
        "code_source": masked_cs,
    }
    target = local_path or repo_url or "未命名目标"
    name = name or (
        f"[{project_name}] 代码生成：{target}" if project_name else f"代码生成：{target}"
    )
    obj, _ = BackgroundTask.objects.update_or_create(
        source=task_id,
        defaults={
            "name": name,
            "task_type": "generate",
            "status": "pending",
            "started_at": timezone.now(),
            "created_by": getattr(user, "id", None),
            "created_by_username": getattr(user, "username", "") or "",
            "payload_summary": json.dumps(summary, ensure_ascii=False),
            "is_closed": False,
        },
    )
    return obj


def record_sync(*, user: Any, summary: str) -> BackgroundTask:
    """记录一次「用例回流」（同步完成，直接 success）。"""
    return BackgroundTask.objects.create(
        name="用例回流",
        task_type="sync",
        status="success",
        progress=1.0,
        finished_at=timezone.now(),
        created_by=getattr(user, "id", None),
        created_by_username=getattr(user, "username", "") or "",
        result_summary=summary,
    )


def record_verdict(*, user_id: int | None, username: str = "", summary: str) -> BackgroundTask:
    """记录一次「安全判定」（同步完成，直接 success）。"""
    return BackgroundTask.objects.create(
        name="安全判定",
        task_type="verdict",
        status="success",
        progress=1.0,
        finished_at=timezone.now(),
        created_by=user_id,
        created_by_username=username,
        result_summary=summary,
    )


# ============================================================================
# 状态同步（读取前调用）
# ============================================================================
def sync_statuses() -> None:
    """刷新所有在途任务（pending/running 且带 source）的状态。"""
    tasks = BackgroundTask.objects.filter(
        is_closed=False, status__in=_LIVE_STATUS, source__gt=""
    )
    for t in tasks:
        sync_one(t)


def sync_one(task: BackgroundTask) -> BackgroundTask:
    """拉回单个在途任务的最新状态；不可达则保留现状。"""
    if not task.source or task.status not in _LIVE_STATUS:
        return task
    try:
        from apps.testgen_integration.client import poll

        data = poll(task.source)
    except Exception:  # noqa: BLE001 - sidecar 不可达：保留现状，不误判失败
        return task

    state = data.get("state") or data.get("status")
    mapped = _SIDECAR_STATE_MAP.get(state)
    if mapped:
        task.status = mapped
    try:
        task.progress = float(data.get("progress") or 0.0)
    except (TypeError, ValueError):
        task.progress = 0.0
    task.stage = (data.get("stage") or "")[:64]

    if mapped in ("success", "failed", "cancelled") and not task.finished_at:
        task.finished_at = timezone.now()
    if mapped == "failed":
        task.error = (data.get("error") or "")[:2000]
    if mapped == "success":
        res = data.get("result")
        if isinstance(res, dict):
            try:
                task.result_summary = json.dumps(res, ensure_ascii=False)[:2000]
            except (TypeError, ValueError):
                pass
    task.save()
    return task


# ============================================================================
# 运维操作
# ============================================================================
def retry_task(task: BackgroundTask, *, user: Any | None = None) -> BackgroundTask:
    """重试失败/已取消/已暂停的任务。当前仅支持 generate（需保留原始请求参数）。"""
    if task.task_type not in _RETRYABLE_TYPES:
        raise ValueError("该类型任务暂不支持重试（仅代码生成任务可重试）")
    if task.status not in _RETRYABLE_STATUS:
        raise ValueError("仅失败 / 已取消 / 已暂停的任务可重试")

    try:
        params = json.loads(task.payload_summary or "{}")
    except (TypeError, ValueError):
        params = {}

    from apps.testgen_integration.client import generate

    resp = generate(
        local_path=params.get("local_path", ""),
        repo_url=params.get("repo_url", ""),
        scopes=params.get("scopes"),
        changed_files=params.get("changed_files"),
        mode=params.get("mode", ""),
    )
    new_id = resp.get("task_id")
    task.source = new_id or task.source
    task.status = "pending"
    task.progress = 0.0
    task.stage = ""
    task.error = ""
    task.started_at = timezone.now()
    task.finished_at = None
    task.is_closed = False
    if user is not None:
        task.created_by = getattr(user, "id", task.created_by)
        task.created_by_username = getattr(user, "username", "") or task.created_by_username
    task.save()
    return task


def pause_task(task: BackgroundTask) -> BackgroundTask:
    """暂停运行中/等待中的任务。尽力通知 sidecar 取消底层作业（无 resume，等价于停止）。"""
    if task.status not in _PAUSABLE_STATUS:
        raise ValueError("仅运行中 / 等待中的任务可暂停")
    if task.source:
        try:
            from apps.testgen_integration.client import cancel_task

            cancel_task(task.source)
        except Exception:  # noqa: BLE001 - sidecar 不可达也照常本地置为 paused
            pass
    task.status = "paused"
    if not task.finished_at:
        task.finished_at = timezone.now()
    task.save()
    return task


def close_task(task: BackgroundTask) -> BackgroundTask:
    """关闭（软隐藏）不需要的任务。"""
    task.is_closed = True
    task.save()
    return task
