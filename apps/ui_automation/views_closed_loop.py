"""闭环触发 API（#200）：UI 一键异步跑闭环 + 查状态。

- `POST /api/ui-automation/closed-loop/run/`：校验 case_ids 存在性 → fire-and-forget `.delay`
  返回 task_id（不阻塞 HTTP）；
- `GET  /api/ui-automation/closed-loop/<task_id>/status/`：查 Celery 任务状态与结果。
"""
from __future__ import annotations

import logging
from typing import ClassVar

from celery.result import AsyncResult
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.ui_automation.tasks import run_closed_loop_task

logger = logging.getLogger(__name__)


class ClosedLoopRunView(APIView):
    permission_classes: ClassVar[list] = [IsAuthenticated]

    def post(self, request) -> Response:
        body = request.data or {}
        case_ids = body.get("case_ids") or []
        project_id = body.get("project_id")
        try:
            max_retries = int(body.get("max_retries", 0) or 0)
        except (TypeError, ValueError):
            return Response({"ok": False, "error": "max_retries 必须为整数"}, status=400)

        if case_ids:
            try:
                ids = [int(x) for x in case_ids]
            except (TypeError, ValueError):
                return Response(
                    {"ok": False, "error": "case_ids 必须是整型列表"}, status=400
                )
            from apps.ui_automation.models import AICase

            existing = set(
                AICase.objects.filter(id__in=ids).values_list("id", flat=True)
            )
            missing = [i for i in ids if i not in existing]
            if missing:
                return Response(
                    {"ok": False, "error": f"部分 case_ids 不存在: {missing}"},
                    status=400,
                )
        elif not project_id:
            return Response(
                {"ok": False, "error": "case_ids 与 project_id 至少提供一个"},
                status=400,
            )

        # fire-and-forget：把慢速浏览器闭环推到 Celery worker，不阻塞 HTTP
        async_result = run_closed_loop_task.delay(case_ids, project_id, max_retries)
        return Response(
            {"ok": True, "task_id": async_result.id, "state": async_result.state}
        )


class ClosedLoopStatusView(APIView):
    permission_classes: ClassVar[list] = [IsAuthenticated]

    def get(self, request, task_id: str) -> Response:
        res = AsyncResult(task_id)
        payload: dict = {"ok": True, "task_id": task_id, "state": res.state}
        if res.ready():
            if res.successful():
                try:
                    payload["result"] = res.result
                except Exception:  # noqa: BLE001 - 结果不可序列化时降级
                    payload["result"] = None
            else:
                payload["error"] = str(res.result) if res.result else None
        return Response(payload)
