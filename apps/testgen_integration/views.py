"""testgen 集成 REST 端点（Django + DRF）。

端点（挂在 /api/testgen/）：
  POST /api/testgen/generate  触发 testgen 异步生成，返回 task_id + poll_url
  POST /api/testgen/sync      拉取某 testgen 项目已生成用例 → 映射 → 幂等回流 testhub TestCase

设计要点：
  - generate 仅触发（非阻塞），testgen 侧异步生成；testhub 侧或前端按 task_id 轮询；
  - sync 接收已生成的 testgen project_id，立即拉取用例并回流，返回 created/updated 计数；
  - 写库走注入式 upsert（见 flow.sync_cases），以 ``tg:<tc_no>`` 标签做幂等；
  - 鉴权沿用项目惯例：IsAuthenticated（与 apps/* 其余端点一致）。
"""
from __future__ import annotations

from typing import Any

from django.db import transaction
from rest_framework.parsers import JSONParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.testcases.models import TestCase

from . import flow
from .client import generate, get_cases
from .mapping import TESTGEN_ID_TAG_PREFIX

# 用 testhub 约定解析请求体（标题/步骤最大长度等由模型约束兜底）
_PARSERS = [JSONParser()]


def _ensure_tag(tags: Any, tag: str) -> list:
    tags = list(tags or [])
    if tag not in tags:
        tags.append(tag)
    return tags


class TestgenGenerateView(APIView):
    """触发 testgen 异步生成任务。"""

    permission_classes = [IsAuthenticated]
    parser_classes = _PARSERS

    def post(self, request: Any) -> Response:
        body = request.data or {}
        local_path = body.get("local_path", "") or ""
        repo_url = body.get("repo_url", "") or ""
        scopes = body.get("scopes") or None
        changed_files = body.get("changed_files") or None
        try:
            resp = generate(
                local_path=local_path,
                repo_url=repo_url,
                scopes=scopes,
                changed_files=changed_files,
            )
        except ValueError as exc:  # 红线守卫
            return Response({"ok": False, "error": str(exc)}, status=400)
        except (Exception,) as exc:  # noqa: BLE001 - 网络/服务异常统一转 502
            return Response({"ok": False, "error": f"调用 testgen 失败：{exc}"}, status=502)
        return Response(
            {
                "ok": True,
                "task_id": resp.get("task_id"),
                "poll_url": resp.get("poll_url"),
                "state": resp.get("state") or resp.get("status"),
            }
        )


class TestgenSyncView(APIView):
    """拉取 testgen 项目用例 → 映射 → 幂等回流 testhub TestCase。"""

    permission_classes = [IsAuthenticated]
    parser_classes = _PARSERS

    def post(self, request: Any) -> Response:
        body = request.data or {}
        project_id = body.get("project_id")
        testgen_project_id = body.get("testgen_project_id")
        suite_id = body.get("suite_id")
        if not project_id or testgen_project_id is None:
            return Response(
                {"ok": False, "error": "project_id 与 testgen_project_id 均为必填"},
                status=400,
            )

        try:
            cases = get_cases(int(testgen_project_id))
        except (Exception,) as exc:  # noqa: BLE001
            return Response(
                {"ok": False, "error": f"获取 testgen 用例失败：{exc}"}, status=502
            )

        author_id = request.user.id

        @transaction.atomic
        def _upsert(testgen_id: str, payload: dict) -> str:
            tag = f"{TESTGEN_ID_TAG_PREFIX}{testgen_id}"
            existing = TestCase.objects.filter(
                project_id=project_id, tags__contains=[tag]
            ).first()
            if existing:
                for key, value in payload.items():
                    setattr(existing, key, value)
                existing.tags = _ensure_tag(existing.tags, tag)
                existing.save()
                return flow.UPDATED
            payload = dict(payload)
            payload["tags"] = _ensure_tag(payload.get("tags", []), tag)
            TestCase.objects.create(**payload)
            return flow.CREATED

        result = flow.sync_cases(
            cases,
            project_id=int(project_id),
            author_id=author_id,
            upsert=_upsert,
            suite_id=suite_id,
        )
        return Response({"ok": True, **result})
