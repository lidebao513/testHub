"""需求保存时把 module 字符串挂到 BizModule（任务 #453）。

口径（用户 2026-11-24 拍板）：
- 只挂需求（BusinessRequirement），GeneratedTestCase 与 #452 的 by_module 报表均不动；
- namespace = requirement:<document_id>，source = requirement，与 code/runtime 三类并列；
- 文档无关联项目或 module 为空时跳过，不猜默认项目；
- 幂等：已有归属不覆盖；删除模块后 FK 经 SET_NULL 降级，不删需求。
"""

from __future__ import annotations

import logging

from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.biz_modules.services import ensure_module, module_namespace

from .models import BusinessRequirement

logger = logging.getLogger(__name__)

_SOURCE = "requirement"


def bind_requirement_module(requirement: BusinessRequirement) -> None:
    """把单条需求的 module 字符串解析成模块归属，失败不阻断需求保存。"""
    if requirement.module_ref_id:
        return
    name = (requirement.module or "").strip()
    if not name:
        return

    document = getattr(getattr(requirement, "analysis", None), "document", None)
    project_id = getattr(document, "project_id", None)
    if not document or not project_id:
        return

    namespace = module_namespace(_SOURCE, str(document.id))
    try:
        module = ensure_module(
            project_id,
            name,
            namespace,
            _SOURCE,
            description=f"来源需求文档：{document.title}",
        )
    except Exception:
        logger.exception("需求 #%s 的业务模块归属建立失败", requirement.pk)
        return

    # 用 queryset.update 避免再次触发 post_save 递归
    type(requirement).objects.filter(pk=requirement.pk).update(module_ref=module)
    requirement.module_ref = module
    requirement.module_ref_id = module.id


@receiver(post_save, sender=BusinessRequirement)
def _on_requirement_saved(sender, instance, **kwargs) -> None:
    bind_requirement_module(instance)
