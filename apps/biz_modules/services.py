"""业务模块的 get-or-create 入口（回流链路共用，批次 2 的 module_resolver 复用）。"""

from __future__ import annotations

from .models import BizModule


def ensure_module(
    project_id: int,
    name: str,
    namespace: str = "",
    source: str = "manual",
    description: str = "",
) -> BizModule:
    """按唯一键 (project, namespace, name) 取或建模块。

    命中已有记录时不覆盖其描述/来源（幂等，避免回流反复改写人工编辑内容）。
    """
    name = (name or "root").strip() or "root"
    module, _ = BizModule.objects.get_or_create(
        project_id=project_id,
        namespace=namespace or "",
        name=name,
        defaults={"source": source, "description": description or ""},
    )
    return module


def module_namespace(source: str, identifier: str) -> str:
    """拼出带来源前缀的命名空间，避免裸 module 名跨项目撞名。"""
    return f"{source}:{identifier}" if identifier else source


__all__ = ["ensure_module", "module_namespace"]
