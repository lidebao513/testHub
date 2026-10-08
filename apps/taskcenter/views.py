"""后台任务 REST 端点（挂在 /api/taskcenter/）。

端点：
  GET  /api/taskcenter/tasks/                  任务列表（默认排除已关闭；?include_closed=1 含）
  GET  /api/taskcenter/tasks/<id>/             任务详情（读取前同步一次在途状态）
  POST /api/taskcenter/tasks/<id>/retry/       重试失败任务
  POST /api/taskcenter/tasks/<id>/pause/       暂停运行中任务
  POST /api/taskcenter/tasks/<id>/close/       关闭不需要任务
"""
from __future__ import annotations

from django.shortcuts import get_object_or_404
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from . import services
from .models import BackgroundTask
from .serializers import BackgroundTaskSerializer


class TaskListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request) -> Response:
        include_closed = request.query_params.get("include_closed") == "1"
        services.sync_statuses()  # 刷新在途任务状态
        qs = BackgroundTask.objects.all()
        if not include_closed:
            qs = qs.filter(is_closed=False)
        qs = qs[:200]
        return Response(
            {"ok": True, "tasks": BackgroundTaskSerializer(qs, many=True).data, "count": qs.count()}
        )


class TaskDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, task_id: str) -> Response:
        task = get_object_or_404(BackgroundTask, id=task_id)
        services.sync_one(task)
        return Response({"ok": True, "task": BackgroundTaskSerializer(task).data})


class TaskRetryView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, task_id: str) -> Response:
        task = get_object_or_404(BackgroundTask, id=task_id)
        try:
            updated = services.retry_task(task, user=request.user)
        except ValueError as exc:
            return Response({"ok": False, "error": str(exc)}, status=400)
        return Response({"ok": True, "task": BackgroundTaskSerializer(updated).data})


class TaskPauseView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, task_id: str) -> Response:
        task = get_object_or_404(BackgroundTask, id=task_id)
        try:
            updated = services.pause_task(task)
        except ValueError as exc:
            return Response({"ok": False, "error": str(exc)}, status=400)
        return Response({"ok": True, "task": BackgroundTaskSerializer(updated).data})


class TaskCloseView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, task_id: str) -> Response:
        task = get_object_or_404(BackgroundTask, id=task_id)
        updated = services.close_task(task)
        return Response({"ok": True, "task": BackgroundTaskSerializer(updated).data})
