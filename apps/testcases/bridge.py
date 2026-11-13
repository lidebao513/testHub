"""需求三件套 → 测试用例自动桥接（#273）。

将需求分析产出的「三件套」自动转换为 testcases 应用下的测试用例草稿，
供测试人员在此基础上补全步骤/预期后激活，避免从零手写。

范围：
- FunctionalInventory（功能清单）→ 功能测试用例（每个功能点 1 条）
- UIElementDescription（界面元素，可选）→ UI 测试用例
- ClarificationItem（待澄清项）不在本桥接直接生成用例：澄清是「问题」而非「功能」，
  桥接仅统计数量并在返回中提示，由人工在用例中补充边界/异常覆盖。

幂等：同一文档 + 同一项目下，已存在相同标题且带 `bridge` / `doc:<id>` 标签的用例会被跳过，
重复触发不会造重复数据。
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction

from apps.projects.models import Project
from apps.users.models import User

from .models import TestCase


@dataclass
class BridgeSummary:
    created: int
    skipped: int
    total: int
    functional: int
    ui: int
    clarifications: int
    testcase_ids: list[int]


def _existing_bridge_titles(project: Project, document_id: int) -> set[str]:
    """已桥接的用例标题集合（跨数据库安全：避免 sqlite 不支持 JSON contains 查询）。"""
    tag = f"doc:{document_id}"
    existing: set[str] = set()
    for tc in TestCase.objects.filter(project=project).only("title", "tags"):
        tags = tc.tags or []
        if "bridge" in tags and tag in tags:
            existing.add(tc.title)
    return existing


def bridge_three_products_to_cases(
    document_id: int,
    project: Project,
    user: User,
    *,
    include_ui: bool = False,
) -> BridgeSummary:
    """把需求文档三件套桥接为测试用例草稿。

    :param document_id: `RequirementDocument` 主键
    :param project: 落库目标项目（testcases.Project）
    :param user: 用例作者
    :param include_ui: 是否把界面元素描述也桥接为 UI 测试用例
    :returns: 桥接统计
    """
    # 延迟导入，避免跨 app 循环依赖
    from apps.requirement_analysis.models import (
        ClarificationItem,
        FunctionalInventory,
        UIElementDescription,
    )

    functional_items = list(
        FunctionalInventory.objects.filter(document_id=document_id).order_by("seq")
    )
    ui_items = (
        list(UIElementDescription.objects.filter(document_id=document_id).order_by("id"))
        if include_ui
        else []
    )
    clarification_count = ClarificationItem.objects.filter(
        document_id=document_id, status__in=["answered", "resolved"]
    ).count()

    existing = _existing_bridge_titles(project, document_id)
    created = 0
    skipped = 0
    new_ids: list[int] = []

    with transaction.atomic():
        tag_doc = f"doc:{document_id}"
        for item in functional_items:
            title = f"{item.seq}. {item.name}"
            if title in existing:
                skipped += 1
                continue
            tc = TestCase.objects.create(
                project=project,
                title=title,
                description=item.summary or "",
                preconditions="",
                steps="",
                expected_result=f"功能「{item.name}」可正常使用并达到预期效果（桥接草稿，请补充具体预期）",
                priority="medium",
                status="draft",
                test_type="functional",
                tags=["bridge", "three-products", tag_doc],
                author=user,
                assignee=None,
            )
            new_ids.append(tc.id)
            created += 1

        for ui in ui_items:
            title = f"UI | {ui.page} / {ui.element}"
            if title in existing:
                skipped += 1
                continue
            tc = TestCase.objects.create(
                project=project,
                title=title,
                description=ui.desc or "",
                preconditions="",
                steps=ui.interaction or "",
                expected_result=f"页面「{ui.page}」的「{ui.element}」元素交互符合预期（桥接草稿，请补充具体预期）",
                priority="medium",
                status="draft",
                test_type="ui",
                tags=["bridge", "three-products", tag_doc],
                author=user,
                assignee=None,
            )
            new_ids.append(tc.id)
            created += 1

    total = len(functional_items) + len(ui_items)
    return BridgeSummary(
        created=created,
        skipped=skipped,
        total=total,
        functional=len(functional_items),
        ui=len(ui_items),
        clarifications=clarification_count,
        testcase_ids=new_ids,
    )
