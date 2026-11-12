"""P0-2 服务层：OpenAPI 导入 + 接口用例 Agent + 失败归因。

约定（与现有 api_testing 视图一致）：
- AI 配置复用 ``AIServiceConfig``，出站调用经 ``safe_request``（内置 SSRF 守卫）。
- 同步视图内直接调用（``safe_request`` 为阻塞式），不在服务层自管事件循环。
- AI 生成内容一律走「草稿待确认」闸门：单接口用例落 ``GeneratedApiTestCase(draft)``，
  场景套件落 ``TestSuite(is_draft=True)``，均不直接作为正式用例执行。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

import yaml
from django.db import transaction

from .models import (
    ApiProject,
    ApiCollection,
    ApiRequest,
    TestSuite,
    TestSuiteRequest,
    TestExecution,
    ApiSpecImport,
    GeneratedApiTestCase,
    AIServiceConfig,
)
from .ssrf_guard import SsrfBlocked, validate_url

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------
def _get_active_config(
    role: Optional[str] = None, config_id: Optional[int] = None
) -> Optional[AIServiceConfig]:
    """取一个可用的 AI 配置：优先指定 id，其次指定 role，最后任意活跃配置。"""
    if config_id:
        return AIServiceConfig.objects.filter(id=config_id, is_active=True).first()
    qs = AIServiceConfig.objects.filter(is_active=True)
    if role:
        qs = qs.filter(role=role)
    return qs.first()


def _call_llm(
    config: AIServiceConfig, messages: List[Dict[str, str]], timeout: float = 90.0
) -> str:
    """用 AIServiceConfig 同步调用 chat/completions，返回 message content。"""
    from .ssrf_guard import safe_request

    headers = {
        "Authorization": f"Bearer {config.get_api_key()}",
        "Content-Type": "application/json",
    }
    data = {
        "model": config.model_name,
        "messages": messages,
        "max_tokens": config.max_tokens,
        "temperature": config.temperature,
    }
    try:
        resp = safe_request(
            "POST",
            f"{config.base_url}/chat/completions",
            headers=headers,
            json=data,
            timeout=timeout,
        )
    except SsrfBlocked:
        raise
    if resp.status_code != 200:
        raise RuntimeError(f"AI 调用失败 {resp.status_code}: {(resp.text or '')[:500]}")
    result = resp.json()
    return result["choices"][0]["message"]["content"]


def _extract_json(content: str) -> Any:
    """从 LLM 文本中尽力解析 JSON（容忍 ```json 围栏与多余文字）。"""
    if not content:
        return None
    text = content.strip()
    # 去 ```json ... ``` 围栏
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # 退而求其次：截取首个 { 到末尾的最后一个 }
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            pass
    return None


# ---------------------------------------------------------------------------
# OpenAPI / Swagger 导入
# ---------------------------------------------------------------------------
class OpenApiImportService:
    """解析 swagger/openapi 规格，并将 paths 落为 ApiCollection + ApiRequest。"""

    @classmethod
    def parse_openapi(cls, raw_spec: str, spec_format: str = "json") -> Dict[str, Any]:
        """解析原始规格文本为 dict。支持 JSON 与 YAML（仅 safe_load）。"""
        spec_format = (spec_format or "json").lower()
        if spec_format in ("yaml", "yml"):
            try:
                return yaml.safe_load(raw_spec)
            except yaml.YAMLError as e:
                raise ValueError(f"YAML 解析失败: {e}")
        try:
            return json.loads(raw_spec)
        except json.JSONDecodeError as e:
            raise ValueError(f"JSON 解析失败: {e}")

    @classmethod
    def _resolve_base_urls(cls, spec: Dict[str, Any]) -> List[str]:
        """从 openapi3 servers 或 swagger2 host/basePath/schemes 抽取服务地址。

        每个地址在返回前经 ``validate_url`` 做 SSRF 校验，命中黑名单直接抛 SsrfBlocked。
        """
        urls: List[str] = []
        # OpenAPI 3.x
        for srv in spec.get("servers") or []:
            url = (srv.get("url") or "").rstrip("/")
            if url:
                validate_url(url)
                urls.append(url)
        # Swagger 2.0
        if not urls:
            host = spec.get("host")
            base_path = (spec.get("basePath") or "").rstrip("/")
            schemes = spec.get("schemes") or ["https"]
            if host:
                for scheme in schemes:
                    url = f"{scheme}://{host}{base_path}"
                    validate_url(url)
                    urls.append(url)
        return urls

    @classmethod
    def _build_request_payload(
        cls, path: str, method: str, op: Dict[str, Any], base_url: str
    ) -> Dict[str, Any]:
        """由单条 path 项构造 ApiRequest 的字段字典。"""
        summary = (
            op.get("summary") or op.get("operationId") or f"{method.upper()} {path}"
        )
        description = op.get("description") or ""

        params: Dict[str, Any] = {}
        headers: Dict[str, Any] = {}
        for p in op.get("parameters") or []:
            loc = p.get("in")
            name = p.get("name")
            if not name:
                continue
            ref = {
                "name": name,
                "required": bool(p.get("required", False)),
                "type": (p.get("schema") or {}).get("type") or "string",
                "description": p.get("description") or "",
            }
            if loc == "query":
                params[name] = ref
            elif loc == "header":
                headers[name] = ref
            elif loc == "path":
                # 路径参数也并入 params 便于前端提示，url 保留 {name}
                params[name] = ref

        body: Dict[str, Any] = {}
        req_body = op.get("requestBody") or {}
        content = req_body.get("content") or {}
        for ctype, cval in content.items():
            schema = cval.get("schema") or {}
            body.setdefault(ctype, schema)
        if req_body.get("required") is not None:
            body["_required"] = bool(req_body.get("required"))

        url = f"{base_url}{path}" if base_url else path
        return {
            "name": summary,
            "description": description,
            "method": method.upper(),
            "url": url,
            "headers": headers,
            "params": params,
            "body": body,
        }

    @classmethod
    def import_openapi(
        cls,
        raw_spec: str,
        spec_format: str,
        project: ApiProject,
        user,
        name: Optional[str] = None,
        source: str = "paste",
    ) -> ApiSpecImport:
        """解析并落库：ApiSpecImport + ApiCollection + ApiRequest。

        返回导入记录；导入过程中任何 SSRF 命中、解析失败都会把记录标记为 failed 并返回，
        不会抛异常（保证 failed 状态可持久化，便于前端回显错误）。
        """
        spec_import = ApiSpecImport.objects.create(
            name=name or "未命名导入",
            source=source,
            raw_spec=raw_spec,
            spec_format=spec_format,
            project=project,
            status="pending",
            created_by=user,
        )
        try:
            spec = cls.parse_openapi(raw_spec, spec_format)
            base_urls = cls._resolve_base_urls(spec)
            base_url = base_urls[0] if base_urls else ""

            paths = spec.get("paths") or {}
            title = (spec.get("info") or {}).get("title") or name or "OpenAPI 导入"

            with transaction.atomic():
                collection = ApiCollection.objects.create(
                    name=title,
                    description=f"由规格导入：{spec_import.name}",
                    project=project,
                    order=0,
                )
                created_ids: List[int] = []
                http_methods = {
                    "get",
                    "post",
                    "put",
                    "delete",
                    "patch",
                    "head",
                    "options",
                }
                order = 0
                for path, ops in paths.items():
                    if not isinstance(ops, dict):
                        continue
                    for method, op in ops.items():
                        if method.lower() not in http_methods or not isinstance(
                            op, dict
                        ):
                            continue
                        payload = cls._build_request_payload(path, method, op, base_url)
                        payload["collection"] = collection
                        payload["order"] = order
                        payload["created_by"] = user
                        payload.setdefault("tags", [])
                        payload["tags"].append(f"spec_import:{spec_import.id}")
                        req = ApiRequest.objects.create(**payload)
                        created_ids.append(req.id)
                        order += 1

            spec_import.base_url = base_url
            spec_import.status = "success" if created_ids else "partial"
            spec_import.imported_count = len(created_ids)
            spec_import.imported_requests = created_ids
            spec_import.save(
                update_fields=[
                    "base_url",
                    "status",
                    "imported_count",
                    "imported_requests",
                    "updated_at",
                ]
            )
            return spec_import

        except (ValueError, SsrfBlocked) as e:
            spec_import.status = "failed"
            spec_import.error_message = str(e)
            spec_import.save(update_fields=["status", "error_message", "updated_at"])
            return spec_import
        except Exception as e:  # noqa: BLE001 - 顶层兜底，记录后返回 failed
            spec_import.status = "failed"
            spec_import.error_message = f"导入异常: {e}"
            spec_import.save(update_fields=["status", "error_message", "updated_at"])
            logger.exception("OpenAPI 导入失败")
            return spec_import


# ---------------------------------------------------------------------------
# 接口用例 Agent（单接口 / 场景）
# ---------------------------------------------------------------------------
class ApiTestCaseAgent:
    """依据接口资产用 LLM 生成测试用例草稿。"""

    CASE_SYSTEM_PROMPT = (
        "你是一名资深的接口测试专家。给定一条 HTTP 接口信息，请设计测试用例，"
        "覆盖【正常】【边界】【异常】三类。每类给出若干用例，每个用例包含："
        "name（用例名）、description（说明）、request_overrides（对原接口的可覆盖字段，"
        "如 headers/params/body 的覆盖值，无覆盖则给空对象）、assertions（断言列表，"
        "每项含 field（如 status_code/body.xxx/header.xxx）、op（eq/neq/in/contains）、"
        "expect（期望值））。只返回 JSON，结构为："
        '{"normal":[...],"boundary":[...],"abnormal":[...]}。不要输出多余解释。'
    )

    SCENARIO_SYSTEM_PROMPT = (
        "你是一名资深的接口自动化架构师。给定多个 HTTP 接口，请设计一条业务场景测试流，"
        "明确请求的执行顺序与数据依赖（例如后一个接口需要使用前一个接口响应中的某个字段）。"
        '只返回 JSON：{"name":"场景名","description":"说明","steps":[{"order":1,'
        '"request_id":<原接口id>,"depends_on":<上一步order或null>,"'
        'extract":{"var_name":"body.path.to.field"},"request_overrides":{},'
        '"assertions":[]}]}。request_id 必须来自输入的接口 id 列表。'
    )

    @classmethod
    def generate_cases_for_request(
        cls,
        api_request: ApiRequest,
        config: Optional[AIServiceConfig] = None,
        config_id: Optional[int] = None,
        user=None,
    ) -> List[GeneratedApiTestCase]:
        """调用 LLM 为该接口生成三类草稿用例并落库（status=draft）。"""
        config = config or _get_active_config(role="mock_data", config_id=config_id)
        if not config:
            raise RuntimeError("未找到可用的 AI 配置（请先配置并启用 AIServiceConfig）")

        req_info = {
            "name": api_request.name,
            "method": api_request.method,
            "url": api_request.url,
            "headers": api_request.headers,
            "params": api_request.params,
            "body": api_request.body,
            "description": api_request.description,
        }
        messages = [
            {"role": "system", "content": cls.CASE_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(req_info, ensure_ascii=False)},
        ]
        content = _call_llm(config, messages, timeout=90)
        data = _extract_json(content)
        if not isinstance(data, dict):
            raise RuntimeError("AI 返回无法解析为用例 JSON")

        created: List[GeneratedApiTestCase] = []
        for category in ("normal", "boundary", "abnormal"):
            for item in data.get(category) or []:
                if not isinstance(item, dict):
                    continue
                created.append(
                    GeneratedApiTestCase.objects.create(
                        api_request=api_request,
                        name=item.get("name") or f"{category} 用例",
                        category=category,
                        request_overrides=item.get("request_overrides") or {},
                        assertions=item.get("assertions") or [],
                        description=item.get("description") or "",
                        status="draft",
                        created_by=user,
                    )
                )
        return created

    @classmethod
    def generate_scenario(
        cls,
        request_ids: List[int],
        config: Optional[AIServiceConfig] = None,
        config_id: Optional[int] = None,
        project: Optional[ApiProject] = None,
        user=None,
        name: Optional[str] = None,
    ) -> TestSuite:
        """调用 LLM 串联多接口业务流，生成 TestSuite(is_draft=True) + TestSuiteRequest。"""
        requests = list(ApiRequest.objects.filter(id__in=request_ids))
        if not requests:
            raise RuntimeError("未提供有效的接口 id")

        config = config or _get_active_config(role="mock_data", config_id=config_id)
        if not config:
            raise RuntimeError("未找到可用的 AI 配置")

        req_brief = [
            {"id": r.id, "name": r.name, "method": r.method, "url": r.url}
            for r in requests
        ]
        messages = [
            {"role": "system", "content": cls.SCENARIO_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"可用接口列表（id 必须来自此处）：\n"
                    f"{json.dumps(req_brief, ensure_ascii=False)}\n"
                    f"请据此设计场景流。"
                ),
            },
        ]
        content = _call_llm(config, messages, timeout=90)
        data = _extract_json(content)
        if not isinstance(data, dict):
            raise RuntimeError("AI 返回无法解析为场景 JSON")

        project = (
            project or requests[0].collection.project
            if requests[0].collection
            else None
        )
        if not project:
            raise RuntimeError("无法确定场景归属项目")

        with transaction.atomic():
            suite = TestSuite.objects.create(
                project=project,
                name=name or data.get("name") or "AI 生成场景套件",
                description=data.get("description") or "",
                is_draft=True,
                created_by=user,
            )
            order_map = {str(s.get("request_id")): s for s in (data.get("steps") or [])}
            for idx, req in enumerate(requests, start=1):
                step = order_map.get(str(req.id)) or {}
                TestSuiteRequest.objects.create(
                    test_suite=suite,
                    request=req,
                    order=int(step.get("order", idx)),
                    assertions=step.get("assertions") or [],
                    enabled=True,
                )
        return suite


# ---------------------------------------------------------------------------
# 失败归因
# ---------------------------------------------------------------------------
class FailureAttributionService:
    """对失败的执行记录，用 LLM 基于请求/响应/断言/日志产出根因与修复建议。"""

    SYSTEM_PROMPT = (
        "你是一名接口测试排障专家。给定一次失败测试执行的上下文（接口信息、请求、"
        "响应、断言结果、错误日志），请分析最可能的失败根因，并给出可操作的修复建议。"
        '只返回 JSON：{"root_cause":"简短根因","details":"详细分析","suggestions":'
        '["建议1","建议2"],"confidence":"high|medium|low"}。不要输出多余解释。'
    )

    @classmethod
    def attribute(
        cls,
        execution: TestExecution,
        config: Optional[AIServiceConfig] = None,
        config_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """计算归因并写回 execution.attribution；返回归因字典。"""
        config = config or _get_active_config(role="description", config_id=config_id)
        if not config:
            raise RuntimeError("未找到可用的 AI 配置")

        suite = execution.test_suite
        results = execution.results or {}
        ctx = {
            "suite": suite.name if suite else None,
            "status": execution.status,
            "total": execution.total_requests,
            "passed": execution.passed_requests,
            "failed": execution.failed_requests,
            "results": results,
        }
        messages = [
            {"role": "system", "content": cls.SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps(ctx, ensure_ascii=False, default=str),
            },
        ]
        content = _call_llm(config, messages, timeout=60)
        data = _extract_json(content)
        attribution = data if isinstance(data, dict) else {"raw": content or ""}
        execution.attribution = attribution
        execution.save(update_fields=["attribution", "updated_at"])
        return attribution
