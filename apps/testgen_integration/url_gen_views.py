"""地址优先入口的生成视图（独立文件，不改现有 views.py）。

POST /api/testgen/url-generation/generate —— 提交地址通道生成任务。
轮询 / 用例列表复用现有 TestgenTaskStatusView / TestgenTaskCasesView，无需新建。
"""

from __future__ import annotations

from typing import Any, ClassVar

from rest_framework.parsers import JSONParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .url_gen_client import submit_url_generation
from .url_projects import find_url_project


class UrlGenerationView(APIView):
    """地址优先入口专用：地址+凭证为主、变更文件经轻量取码桥辅助的生成入口。"""

    permission_classes: ClassVar[list] = [IsAuthenticated]
    parser_classes: ClassVar[list] = [JSONParser]

    def post(self, request: Any) -> Response:
        body = request.data or {}
        raw_url = (body.get("test_url") or body.get("base_url") or "").strip()
        if not raw_url:
            return Response({"ok": False, "error": "请填写被测地址"}, status=400)

        payload = dict(body)
        payload["test_url"] = raw_url

        # 未显式给 project_id 时，按归一地址解析已识别的 testgen 项目，复用避免重复建项。
        if not payload.get("project_id"):
            mapping = find_url_project(raw_url=raw_url)
            if mapping is not None:
                payload["project_id"] = mapping.get("testgen_project_id")

        try:
            result = submit_url_generation(payload)
        except ValueError as exc:
            return Response({"ok": False, "error": str(exc)}, status=400)
        except RuntimeError as exc:
            return Response({"ok": False, "error": str(exc)}, status=502)
        # sidecar 202 体含 task_id/poll_url；凭证不在其中（_redacted_request 掩码）。
        return Response({"ok": True, **result}, status=202)
