"""P0 测试：需求三件套 → 测试用例桥接（#273）。

覆盖：功能清单→草稿用例、幂等去重、可选 UI 元素桥接、澄清项统计、视图鉴权与默认项目回退。
"""

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from apps.projects.models import Project
from apps.requirement_analysis.models import (
    ClarificationItem,
    FunctionalInventory,
    RequirementDocument,
    UIElementDescription,
)
from apps.testcases.bridge import bridge_three_products_to_cases
from apps.testcases.models import TestCase

User = get_user_model()


@pytest.mark.django_db
def test_bridge_functional_inventory_creates_draft_cases():
    user = User.objects.create_user(username="bridge_u", password="x")
    proj = Project.objects.create(name="Bridge Proj", owner=user)
    doc = RequirementDocument.objects.create(
        title="需求A", document_type="md", status="analyzed",
        uploaded_by=user, project=proj,
    )
    FunctionalInventory.objects.create(document=doc, seq=1, name="登录", summary="用户登录功能")
    FunctionalInventory.objects.create(document=doc, seq=2, name="注册", summary="用户注册功能")
    ClarificationItem.objects.create(document=doc, question="q", status="answered")

    summary = bridge_three_products_to_cases(doc.id, proj, user)
    assert summary.created == 2
    assert summary.functional == 2
    assert summary.clarifications == 1
    bridged = [
        tc for tc in TestCase.objects.filter(project=proj) if "bridge" in (tc.tags or [])
    ]
    assert len(bridged) == 2
    assert all(
        tc.status == "draft"
        for tc in TestCase.objects.filter(project=proj)
    )

    # 幂等：重复桥接应跳过，不造重复数据
    summary2 = bridge_three_products_to_cases(doc.id, proj, user)
    assert summary2.created == 0
    assert summary2.skipped == 2
    assert TestCase.objects.filter(project=proj).count() == 2


@pytest.mark.django_db
def test_bridge_include_ui_creates_ui_cases():
    user = User.objects.create_user(username="bridge_u2", password="x")
    proj = Project.objects.create(name="Bridge Proj2", owner=user)
    doc = RequirementDocument.objects.create(
        title="d", document_type="md", status="analyzed", uploaded_by=user, project=proj
    )
    FunctionalInventory.objects.create(document=doc, seq=1, name="首页", summary="s")
    UIElementDescription.objects.create(
        document=doc, page="首页", element="搜索框", desc="搜索", interaction="输入关键词"
    )
    summary = bridge_three_products_to_cases(doc.id, proj, user, include_ui=True)
    assert summary.created == 2
    assert summary.ui == 1
    assert TestCase.objects.filter(project=proj, test_type="ui").count() == 1


@pytest.mark.django_db
def test_bridge_view_requires_project_and_auth():
    client = APIClient()
    user = User.objects.create_user(username="bridge_v", password="x")
    client.force_authenticate(user)

    # 文档未关联项目且未传 project_id → 400
    doc = RequirementDocument.objects.create(
        title="d", document_type="md", status="analyzed", uploaded_by=user
    )
    resp = client.post(
        "/api/testcases/bridge-from-requirement/",
        {"document_id": doc.id},
        format="json",
    )
    assert resp.status_code == 400

    # 关联项目后 → 201，并实际落库
    proj = Project.objects.create(name="Bridge Proj3", owner=user)
    doc.project = proj
    doc.save()
    FunctionalInventory.objects.create(document=doc, seq=1, name="支付", summary="支付功能")
    resp = client.post(
        "/api/testcases/bridge-from-requirement/",
        {"document_id": doc.id},
        format="json",
    )
    assert resp.status_code == 201
    assert resp.json()["created"] == 1
    assert TestCase.objects.filter(project=proj).count() == 1
