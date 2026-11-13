"""P0-1 单元测试：三件套 JSON 提取与生成逻辑（LLM 以 mock 替代，不触发真实调用）。"""

import json
from unittest.mock import MagicMock

import pytest
from django.contrib.auth import get_user_model

from .services import (
    RequirementThreeProductsService,
    _extract_json,
    _get_active_llm_config,
)

User = get_user_model()


CANNED = json.dumps(
    {
        "functional_inventory": [
            {"seq": 1, "name": "登录", "summary": "用户登录", "source_ref": "2.1"},
            {"seq": 2, "name": "注册", "summary": "用户注册", "source_ref": "2.2"},
        ],
        "ui_descriptions": [
            {
                "page": "登录页",
                "element": "登录按钮",
                "desc": "提交登录",
                "interaction": "点击",
            },
        ],
        "clarifications": [
            {
                "question": "支持哪些登录方式？",
                "impact": "影响设计",
                "suggestion": "提供账号密码",
            },
        ],
    }
)


def test_extract_json_plain():
    data = _extract_json('{"a": 1}')
    assert data == {"a": 1}


def test_extract_json_fenced():
    content = '```json\n{"a": 2}\n```'
    assert _extract_json(content) == {"a": 2}


def test_extract_json_with_surrounding_text():
    content = '好的，结果如下：\n{"a": 3}\n以上。'
    assert _extract_json(content) == {"a": 3}


def test_extract_json_garbage_returns_none():
    assert _extract_json("完全不是 JSON 的纯文本") is None


@pytest.mark.django_db
def test_generate_three_products_mocked(monkeypatch):
    # 用 mock 替代 LLM 配置与调用，聚焦「解析+落库」逻辑
    monkeypatch.setattr(
        "apps.requirement_analysis.services._get_active_llm_config",
        lambda: MagicMock(),
    )
    monkeypatch.setattr(
        RequirementThreeProductsService,
        "_call_llm",
        classmethod(lambda cls, *a, **k: CANNED),
    )
    user = User.objects.create_user(username="p0ra", password="x")
    doc = __import__(
        "apps.requirement_analysis.models", fromlist=["RequirementDocument"]
    ).RequirementDocument
    document = doc.objects.create(
        title="示例需求",
        document_type="txt",
        uploaded_by=user,
        extracted_text="需求：系统需支持登录与注册。",
    )
    counts = RequirementThreeProductsService.generate_three_products(document)
    assert counts["functional_inventory"] == 2
    assert counts["ui_descriptions"] == 1
    assert counts["clarifications"] == 1
    from .models import FunctionalInventory, UIElementDescription, ClarificationItem

    assert FunctionalInventory.objects.filter(document=document).count() == 2
    assert UIElementDescription.objects.filter(document=document).count() == 1
    assert ClarificationItem.objects.filter(document=document).count() == 1


@pytest.mark.django_db
def test_revise_with_clarification_mocked(monkeypatch):
    monkeypatch.setattr(
        "apps.requirement_analysis.services._get_active_llm_config",
        lambda: MagicMock(),
    )
    monkeypatch.setattr(
        RequirementThreeProductsService,
        "_call_llm",
        classmethod(lambda cls, *a, **k: CANNED),
    )
    user = User.objects.create_user(username="p0ra2", password="x")
    models = __import__(
        "apps.requirement_analysis.models", fromlist=["RequirementDocument"]
    )
    document = models.RequirementDocument.objects.create(
        title="示例需求2",
        document_type="txt",
        uploaded_by=user,
        extracted_text="需求内容。",
    )
    RequirementThreeProductsService.generate_three_products(document)
    cl = models.ClarificationItem.objects.filter(document=document).first()
    result = RequirementThreeProductsService.revise_with_clarification(
        document,
        cl.id,
        "支持账号密码与短信验证码",
    )
    assert result["status"] in ("open", "answered", "resolved")
    refreshed = models.ClarificationItem.objects.filter(document=document).first()
    assert refreshed.revision_log  # 修订日志已写入
