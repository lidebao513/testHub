"""地址优先入口的独立 REST 视图（不改现有 views.py 的任何类）。"""

from __future__ import annotations

from typing import Any, ClassVar

from rest_framework.parsers import JSONParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .url_modules import read_modules_for_url, scan_modules_for_url
from .url_projects import ensure_url_project


class UrlProjectEnsureView(APIView):
    """POST /api/testgen/url-projects/ensure —— 识别/创建项目。"""

    permission_classes: ClassVar[list] = [IsAuthenticated]
    parser_classes: ClassVar[list] = [JSONParser]

    def post(self, request: Any) -> Response:
        body = request.data or {}
        raw_url = body.get("base_url", "") or ""
        if not raw_url:
            return Response({"ok": False, "error": "请填写被测地址"}, status=400)
        try:
            result = ensure_url_project(raw_url=raw_url, user=request.user)
        except ValueError as exc:
            return Response({"ok": False, "error": str(exc)}, status=400)
        except RuntimeError as exc:
            return Response({"ok": False, "error": str(exc)}, status=502)
        return Response(result)


class UrlModulesView(APIView):
    """GET /api/testgen/url-projects/modules?base_url=... —— 读取模块清单。"""

    permission_classes: ClassVar[list] = [IsAuthenticated]

    def get(self, request: Any) -> Response:
        raw_url = request.query_params.get("base_url", "") or ""
        if not raw_url:
            return Response({"ok": False, "error": "请填写被测地址"}, status=400)
        try:
            result = read_modules_for_url(raw_url=raw_url)
        except ValueError as exc:
            return Response({"ok": False, "error": str(exc)}, status=400)
        except RuntimeError as exc:
            return Response({"ok": False, "error": str(exc)}, status=502)
        return Response(result)


class UrlModulesScanView(APIView):
    """POST /api/testgen/url-projects/modules/scan —— 只发现页面/路由并入库。"""

    permission_classes: ClassVar[list] = [IsAuthenticated]
    parser_classes: ClassVar[list] = [JSONParser]

    def post(self, request: Any) -> Response:
        body = request.data or {}
        raw_url = body.get("base_url", "") or ""
        if not raw_url:
            return Response({"ok": False, "error": "请填写被测地址"}, status=400)
        try:
            result = scan_modules_for_url(raw_url=raw_url, options=body)
        except ValueError as exc:
            return Response({"ok": False, "error": str(exc)}, status=400)
        except RuntimeError as exc:
            return Response({"ok": False, "error": str(exc)}, status=502)
        return Response(result)
