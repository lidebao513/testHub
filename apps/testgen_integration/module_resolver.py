"""回流时的 module_resolver：把 testgen case 的 module 字符串挂到 BizModule（批次 2）。

设计要点（见 docs/business_module_entity_design_2026-11-24.md）：
- 作为注入回调传给 flow.sync_cases，保持 flow 与 Django 解耦（无 Django 环境仍可单测）；
- 唯一键带命名空间：source + identifier，避免裸 module 名跨项目/跨仓撞名；
- fail-open：解析失败返回 None（用例照常回流，只是不挂模块），绝不阻断回流主流程。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from apps.biz_modules.services import ensure_module, module_namespace

Resolver = Callable[[dict[str, Any]], int | None]

_SOURCE_CODE = "code"
_SOURCE_RUNTIME = "runtime"


def code_module_resolver(project_id: int, testgen_project_id: int) -> Resolver:
    """代码扫描回流：namespace = code:<testgen_project_id>。"""
    namespace = module_namespace(_SOURCE_CODE, str(testgen_project_id))

    def resolve(cs: dict[str, Any]) -> int | None:
        name = (cs.get("module") or "").strip()
        if not name:
            return None
        try:
            return ensure_module(project_id, name, namespace, _SOURCE_CODE).id
        except Exception:  # noqa: BLE001 - 挂模块失败不阻断回流
            return None

    return resolve


def runtime_module_resolver(project_id: int, normalized_url: str) -> Resolver:
    """地址通道回流：namespace = runtime:<归一URL>。"""
    namespace = module_namespace(_SOURCE_RUNTIME, normalized_url or "")

    def resolve(cs: dict[str, Any]) -> int | None:
        name = (cs.get("module") or "").strip()
        if not name:
            return None
        try:
            return ensure_module(project_id, name, namespace, _SOURCE_RUNTIME).id
        except Exception:  # noqa: BLE001
            return None

    return resolve
