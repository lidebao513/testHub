"""地址优先入口：本次生成结果一键回流用例库（独立文件，不改现有 views.py）。

POST /api/testgen/url-generation/sync
    入参：task_id（必填）+ project_id（可选，缺省时按 base_url 归一映射解析）
    流程：按 task_id 取用例 → 过滤 obsolete → 复用 flow.sync_cases 与 ``tg:`` 幂等键
          写入 TestCase → 可选桥接 AICase（fail-open，与旧回流语义一致）。

与旧 ``POST /api/testgen/sync`` 的区别：旧端点按整个 testgen 项目回流；本端点按
任务结果回流，且显式排除 sidecar 标记 obsolete 的用例，避免把作废用例写成 active。
"""
from __future__ import annotations

from typing import Any, ClassVar

from django.db import transaction
from django.db.utils import NotSupportedError
from rest_framework.parsers import JSONParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.testcases.models import TestCase
from apps.ui_automation.bridge import bridge_aicase_real

from . import flow
from .client import classify_sidecar_error, task_cases
from .mapping import TESTGEN_ID_TAG_PREFIX
from .url_projects import find_url_project


def _ensure_tag(tags: Any, tag: str) -> list:
    """与旧回流一致的标签追加（不改旧函数，避免触碰既有链路）。"""
    values = list(tags or [])
    if tag not in values:
        values.append(tag)
    return values


def _sidecar_error(exc: Exception) -> Response:
    status_code = classify_sidecar_error(exc)
    if status_code == 503:
        message = f"testgen sidecar 不可达，请确认服务已启动：{exc}"
    else:
        message = f"调用 testgen 失败：{exc}"
    return Response({"ok": False, "error": message}, status=status_code)


def active_cases_of(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """仅保留本次结果中仍有效的用例（sidecar 的 obsolete 不回流）。"""
    cases = payload.get("cases") or []
    return [
        case
        for case in cases
        if str(case.get("status") or "").lower() != "obsolete"
    ]


def _make_upsert(project_id: int):
    """构造注入给 flow.sync_cases 的幂等写库回调（tg:<id> 标签去重）。"""

    @transaction.atomic
    def _upsert(testgen_id: str, payload: dict[str, Any]) -> str:
        tag = f"{TESTGEN_ID_TAG_PREFIX}{testgen_id}"
        queryset = TestCase.objects.filter(project_id=project_id)
        try:
            existing = queryset.filter(tags__contains=[tag]).first()
        except NotSupportedError:
            # SQLite 不支持 JSONField 的 contains 查询（测试/轻量部署会走这里）；
            # 按项目内标签扫描兜底，保持跨数据库一致的幂等语义。
            existing = next((row for row in queryset if tag in (row.tags or [])), None)
        if existing:
            for key, value in payload.items():
                setattr(existing, key, value)
            existing.tags = _ensure_tag(existing.tags, tag)
            existing.save()
            return flow.UPDATED
        row = dict(payload)
        row["tags"] = _ensure_tag(row.get("tags", []), tag)
        TestCase.objects.create(**row)
        return flow.CREATED

    return _upsert


class UrlGenerationSyncView(APIView):
    """地址优先入口专用：把一次生成任务的用例幂等回流到 TestHub 用例库。"""

    permission_classes: ClassVar[list] = [IsAuthenticated]
    parser_classes: ClassVar[list] = [JSONParser]

    def post(self, request: Any) -> Response:
        body = request.data or {}
        task_id = str(body.get("task_id") or "").strip()
        if not task_id:
            return Response({"ok": False, "error": "task_id 为必填"}, status=400)

        project_id = body.get("project_id")
        if not project_id:
            raw_url = str(body.get("base_url") or body.get("test_url") or "").strip()
            mapping = find_url_project(raw_url=raw_url) if raw_url else None
            if mapping is None:
                return Response(
                    {
                        "ok": False,
                        "error": "缺少 project_id，且无法按被测地址解析已识别项目",
                    },
                    status=400,
                )
            project_id = mapping["testhub_project_id"]

        try:
            pid = int(project_id)
        except (TypeError, ValueError):
            return Response({"ok": False, "error": "project_id 非法"}, status=400)

        try:
            payload = task_cases(task_id)
        except Exception as exc:  # noqa: BLE001 - 统一 sidecar 错误分类
            return _sidecar_error(exc)

        cases = active_cases_of(payload)
        result = flow.sync_cases(
            cases,
            project_id=pid,
            author_id=request.user.id,
            upsert=_make_upsert(pid),
        )

        # 与旧回流保持同等语义：UI 层用例桥接 AICase，失败不阻断主回流。
        try:
            bridge = flow.bridge_aicases(
                cases,
                project_id=pid,
                author_id=request.user.id,
                bridge=bridge_aicase_real,
            )
        except Exception:  # noqa: BLE001 - 桥接失败不阻断主回流
            bridge = {"total": len(cases), "created": 0, "updated": 0, "skipped": 0}

        return Response(
            {
                "ok": True,
                "task_id": task_id,
                "project_id": pid,
                "total": result["total"],
                "created": result["created"],
                "updated": result["updated"],
                "skipped": result["skipped"],
                "bridge": bridge,
            }
        )
