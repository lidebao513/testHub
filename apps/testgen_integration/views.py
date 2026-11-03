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

from typing import Any, ClassVar

from django.db import transaction
from rest_framework.parsers import JSONParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.testcases.models import TestCase

from . import flow
from .client import (
    cancel_task,
    classify_sidecar_error,
    describe,
    generate,
    get_cases,
    is_sidecar_available,
    list_branches,
    list_commits,
    list_stages,
    quality_summary,
    scan,
    stage_analyze,
    stage_compare,
    task_cases,
    verdict,
)
from .defects import create_security_defect_orm
from .mapping import TESTGEN_ID_TAG_PREFIX

# 用 testhub 约定解析请求体（标题/步骤最大长度等由模型约束兜底）
_PARSERS = [JSONParser]


def _ensure_tag(tags: Any, tag: str) -> list:
    tags = list(tags or [])
    if tag not in tags:
        tags.append(tag)
    return tags


class TestgenGenerateView(APIView):
    """触发 testgen 异步生成任务。"""

    permission_classes: ClassVar[list] = [IsAuthenticated]
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
        except Exception as exc:  # noqa: BLE001 - 网络/服务异常：不可达 503、其它 502
            return _sidecar_error_response(exc)
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

    permission_classes: ClassVar[list] = [IsAuthenticated]
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
        except Exception as exc:  # noqa: BLE001 - 不可达 503、其它 502
            return _sidecar_error_response(exc)

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


def _sidecar_error_response(exc: Exception) -> Response:
    """testgen sidecar 异常 → 统一友好提示。

    状态码区分（G2）：
      - sidecar 不可达/超时 → 503（服务未启动，运维动作明确）
      - sidecar 返回错误/响应异常 → 502（上游错误）
    """
    status_code = classify_sidecar_error(exc)
    if status_code == 503:
        message = f"testgen sidecar 不可达，请确认服务已启动：{exc}"
    else:
        message = f"调用 testgen 失败：{exc}"
    return Response({"ok": False, "error": message}, status=status_code)


def execute_security_verdict(body: dict[str, Any], *, user_id: int) -> tuple[dict[str, Any], int]:
    """对一批用例观测执行安全裁决，应拒却放通时自动建缺陷（G3/G4/G5 编排）。

    返回 ``(payload, http_status)``，便于端点与执行钩子复用同一套编排：
      - observations 缺失/非数组 → 400
      - sidecar 不可达 → 503（先探活，避免无谓的批量失败）
      - 正常 → 200，{ok, total, security_cases, unsafe, defects_created, skipped, details}
    """
    observations = body.get("observations")
    if not isinstance(observations, list) or not observations:
        return {"ok": False, "error": "observations 必填且需为非空数组"}, 400

    available, detail = is_sidecar_available()
    if not available:
        # 探活失败统一按「不可达」上报，文案含关键词便于前端/运维识别
        message = detail if "不可达" in detail else f"testgen sidecar 不可达：{detail}"
        return {"ok": False, "error": message}, 503

    default_project_id = body.get("project_id")
    if default_project_id:
        for obs in observations:
            if isinstance(obs, dict) and not obs.get("project_id"):
                obs["project_id"] = default_project_id

    result = flow.run_security_verdict(
        observations,
        verdict_fn=verdict,
        create_defect=create_security_defect_orm,
        reporter_id=user_id,
    )
    return {"ok": True, **result}, 200


class TestgenBranchesView(APIView):
    """列分支（前端目标/基线 ref 下拉）。"""

    permission_classes: ClassVar[list] = [IsAuthenticated]
    parser_classes = _PARSERS

    def post(self, request: Any) -> Response:
        body = request.data or {}
        repo_url = body.get("repo_url", "") or ""
        local_path = body.get("local_path", "") or ""
        code_source = body.get("code_source") or {}
        if not repo_url and not local_path:
            return Response(
                {"ok": False, "error": "repo_url 或 local_path 必填"}, status=400
            )
        try:
            data = list_branches(
                repo_url=repo_url,
                code_source=code_source,
                local_path=local_path,
            )
        except Exception as exc:  # noqa: BLE001 - G2：连接/超时→502
            return _sidecar_error_response(exc)
        return Response({"ok": True, **data})


class TestgenCommitsView(APIView):
    """列指定 ref 的最近提交（选定分支后查看对比数据）。"""

    permission_classes: ClassVar[list] = [IsAuthenticated]
    parser_classes = _PARSERS

    def post(self, request: Any) -> Response:
        body = request.data or {}
        repo_url = body.get("repo_url", "") or ""
        code_source = body.get("code_source") or {}
        ref = body.get("ref", "") or ""
        if not repo_url:
            return Response({"ok": False, "error": "repo_url 必填"}, status=400)
        if not ref:
            return Response({"ok": False, "error": "ref 必填"}, status=400)
        try:
            data = list_commits(repo_url=repo_url, code_source=code_source, ref=ref)
        except Exception as exc:  # noqa: BLE001
            return _sidecar_error_response(exc)
        return Response({"ok": True, **data})


class TestgenDescribeView(APIView):
    """把扫描出的函数/类节点批量翻译成中文功能描述。"""

    permission_classes: ClassVar[list] = [IsAuthenticated]
    parser_classes = _PARSERS

    def post(self, request: Any) -> Response:
        body = request.data or {}
        items = body.get("items") or []
        try:
            data = describe(items=items)
        except Exception as exc:  # noqa: BLE001
            return _sidecar_error_response(exc)
        return Response({"ok": True, **data})


class TestgenTaskCancelView(APIView):
    """取消运行中/排队中的生成任务。"""

    permission_classes: ClassVar[list] = [IsAuthenticated]
    parser_classes = _PARSERS

    def post(self, request: Any, task_id: str) -> Response:
        try:
            data = cancel_task(task_id)
        except Exception as exc:  # noqa: BLE001
            return _sidecar_error_response(exc)
        return Response({"ok": True, **data})


class TestgenTaskCasesView(APIView):
    """取某任务对应项目已生成的用例（前端详情面板用）。"""

    permission_classes: ClassVar[list] = [IsAuthenticated]
    parser_classes = _PARSERS

    def get(self, request: Any, task_id: str) -> Response:
        try:
            data = task_cases(task_id)
        except Exception as exc:  # noqa: BLE001
            return _sidecar_error_response(exc)
        return Response({"ok": True, **data})


class TestgenScanView(APIView):
    """快速扫描（只读）：列模块树 + 标注变更状态，供前端勾选范围。"""

    permission_classes: ClassVar[list] = [IsAuthenticated]
    parser_classes = _PARSERS

    def post(self, request: Any) -> Response:
        body = request.data or {}
        try:
            data = scan(
                repo_url=body.get("repo_url", "") or "",
                code_source=body.get("code_source") or {},
                ref=body.get("ref", "") or "",
                base_ref=body.get("base_ref", "") or "",
                local_path=body.get("local_path", "") or "",
            )
        except Exception as exc:  # noqa: BLE001
            return _sidecar_error_response(exc)
        return Response({"ok": True, **data})


class TestgenQualityView(APIView):
    """testgen 质量分析聚合（趋势/flaky/维度分布/变更），供主平台 reports 入口调用。

    GET /api/testgen/quality?project_id=<testgen 项目 id>
    """

    permission_classes: ClassVar[list] = [IsAuthenticated]

    def get(self, request: Any) -> Response:
        project_id = request.query_params.get("project_id")
        if not project_id:
            return Response(
                {"ok": False, "error": "project_id 必填（testgen 项目 id）"}, status=400
            )
        try:
            pid = int(project_id)
        except ValueError:
            return Response({"ok": False, "error": "project_id 须为整数"}, status=400)
        try:
            data = quality_summary(pid)
        except Exception as exc:  # noqa: BLE001
            return _sidecar_error_response(exc)
        return Response({"project_id": pid, **data})


class TestgenStageListView(APIView):
    """列某仓库（可选某分支）的阶段快照（#121 前端阶段对比 from/to 下拉）。"""

    permission_classes: ClassVar[list] = [IsAuthenticated]
    parser_classes = _PARSERS

    def get(self, request: Any) -> Response:
        repo = (request.query_params.get("repo") or "").strip()
        if not repo:
            return Response({"ok": False, "error": "repo 必填"}, status=400)
        branch = (request.query_params.get("branch") or "").strip()
        try:
            data = list_stages(repo=repo, branch=branch)
        except Exception as exc:  # noqa: BLE001 - 连接/超时→502
            return _sidecar_error_response(exc)
        return Response({"ok": True, **data})


class TestgenStageAnalyzeView(APIView):
    """对本地仓库当前状态做一次分析并登记阶段快照（#121）。"""

    permission_classes: ClassVar[list] = [IsAuthenticated]
    parser_classes = _PARSERS

    def post(self, request: Any) -> Response:
        body = request.data or {}
        local_path = (body.get("local_path") or "").strip()
        if not local_path:
            return Response({"ok": False, "error": "local_path 必填"}, status=400)
        try:
            data = stage_analyze(
                local_path=local_path,
                branch=body.get("branch") or "master",
                commit=body.get("commit") or "",
            )
        except Exception as exc:  # noqa: BLE001 - 连接/超时→502
            return _sidecar_error_response(exc)
        return Response({"ok": True, **data})


class TestgenStageCompareView(APIView):
    """两阶段产物对比（#121 前端阶段对比视图数据源）。"""

    permission_classes: ClassVar[list] = [IsAuthenticated]
    parser_classes = _PARSERS

    def post(self, request: Any) -> Response:
        body = request.data or {}
        repo = (body.get("repo") or "").strip()
        from_ref = (body.get("from_ref") or "").strip()
        to_ref = (body.get("to_ref") or "").strip()
        if not repo or not from_ref or not to_ref:
            return Response(
                {"ok": False, "error": "repo / from_ref / to_ref 必填"}, status=400
            )
        try:
            data = stage_compare(
                repo=repo,
                from_ref=from_ref,
                to_ref=to_ref,
                repo_path=body.get("repo_path") or "",
            )
        except Exception as exc:  # noqa: BLE001 - 连接/超时→502
            return _sidecar_error_response(exc)
        return Response({"ok": True, **data})
