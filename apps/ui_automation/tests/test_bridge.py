"""#199 testgen→AICase 桥接测试。

覆盖：
- 离线：bridge_aicases 纯逻辑（过滤非 UI / 幂等 / payload 映射 / 不携带 project_id）
- 真实接线：bridge_aicase_real 幂等建/更 AICase，project 保持 null
- API 集成：TestgenSyncView 触发后，UI 用例自动生成对应 AICase
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from apps.testgen_integration import flow
from apps.testgen_integration.mapping import TESTGEN_ID_TAG_PREFIX, case_spec_to_aicase

# ---------------------------------------------------------------------------
# 离线：bridge_aicases 纯逻辑
# ---------------------------------------------------------------------------


def _ui_case(tc_no: str = "T1", title: str = "登录页UI", doc_steps=None) -> dict:
    return {
        "tc_no": tc_no,
        "title": title,
        "ctype": "ui",
        "doc_steps": doc_steps
        or [{"kind": "ui", "action": "打开登录页", "expect": "看到登录按钮"}],
        "description": "desc",
    }


def _api_case(tc_no: str = "A1") -> dict:
    return {"tc_no": tc_no, "title": "接口用例", "ctype": "api", "doc_steps": []}


class _MemBridge:
    """内存 bridge，模拟真实 update_or_create 的幂等语义。"""

    def __init__(self) -> None:
        self.store: dict = {}

    def __call__(self, ref: str, payload: dict) -> str:
        if ref in self.store:
            self.store[ref] = payload
            return flow.UPDATED
        self.store[ref] = payload
        return flow.CREATED


def test_bridge_filters_non_ui() -> None:
    bridge = _MemBridge()
    cases = [_ui_case(), _api_case(), {"title": "无指纹"}]
    res = flow.bridge_aicases(cases, project_id=1, author_id=1, bridge=bridge)
    # 仅 1 个 UI 用例被创建，api 与无指纹均 skip
    assert res["created"] == 1
    assert res["skipped"] == 2
    assert len(bridge.store) == 1


def test_bridge_idempotent() -> None:
    bridge = _MemBridge()
    cases = [_ui_case(tc_no="T1"), _ui_case(tc_no="T1")]
    res = flow.bridge_aicases(cases, project_id=1, author_id=1, bridge=bridge)
    assert res["created"] == 1
    assert res["updated"] == 1
    assert len(bridge.store) == 1


def test_bridge_payload_mapping() -> None:
    bridge = _MemBridge()
    flow.bridge_aicases([_ui_case(tc_no="T1")], project_id=7, author_id=9, bridge=bridge)
    payload = bridge.store[f"{TESTGEN_ID_TAG_PREFIX}T1"]
    assert payload["name"] == "登录页UI"
    assert "打开登录页" in payload["task_description"]
    assert "看到登录按钮" in payload["task_description"]
    assert payload["created_by_id"] == 9
    # 关键：不携带 project_id（AICase.project 是 UiProject，与 testgen 的通用 Project 不同模型）
    assert "project_id" not in payload


def test_case_spec_to_aicase_expected() -> None:
    cs = {"tc_no": "X", "title": "t", "doc_steps": [{"action": "a", "expect": "b"}]}
    p = case_spec_to_aicase(cs, project_id=1, author_id=2)
    assert "预期结果：b" in p["task_description"]
    assert p["created_by_id"] == 2


# ---------------------------------------------------------------------------
# 真实接线：bridge_aicase_real
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_bridge_aicase_real_creates_and_idempotent() -> None:
    from apps.ui_automation.bridge import bridge_aicase_real
    from apps.ui_automation.models import AICase

    U = get_user_model()
    user = U.objects.create(username="bridge_user")

    payload = {
        "name": "UI用例",
        "task_description": "打开页面",
        "description": "",
        "created_by_id": user.id,
    }
    ref = f"{TESTGEN_ID_TAG_PREFIX}T1"
    assert bridge_aicase_real(ref, payload) == flow.CREATED
    assert AICase.objects.filter(source_ref=ref).count() == 1

    # 幂等：同 source_ref → updated，不翻倍
    r2 = bridge_aicase_real(ref, {**payload, "name": "UI用例2"})
    assert r2 == flow.UPDATED
    obj = AICase.objects.get(source_ref=ref)
    assert obj.name == "UI用例2"
    assert obj.project is None  # 桥接保持 null，避免 FK 错配
    assert obj.created_by_id == user.id


# ---------------------------------------------------------------------------
# API 集成：TestgenSyncView 触发桥接
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_sync_view_bridges_ui_cases(monkeypatch) -> None:
    from apps.projects.models import Project
    from apps.ui_automation.models import AICase

    U = get_user_model()
    user = U.objects.create(username="sync_user")
    project = Project.objects.create(name="p1", owner=user)

    ui_cases = [_ui_case(tc_no="T1"), _api_case(tc_no="A1")]
    monkeypatch.setattr(
        "apps.testgen_integration.views.get_cases", lambda pid: ui_cases
    )

    # SQLite 测试库不支持 TestCase.tags 的 JSON contains 查询（生产 MySQL 正常），
    # 此处用占位计数替掉 sync_cases，聚焦验证 #199 桥接接入 view 且真实生成 AICase。
    def fake_sync_cases(*_args, **_kwargs):
        return {"total": 2, "created": 1, "updated": 0, "skipped": 1}

    monkeypatch.setattr(
        "apps.testgen_integration.views.flow.sync_cases", fake_sync_cases
    )

    client = APIClient()
    client.force_authenticate(user=user)
    resp = client.post(
        "/api/testgen/sync",
        {"project_id": project.id, "testgen_project_id": 99},
        format="json",
    )

    assert resp.status_code == 200
    data = resp.data
    assert data["ok"] is True
    # 仅 1 个 UI 用例被桥接
    assert data["bridge"]["created"] == 1
    assert data["bridge"]["skipped"] >= 1  # api 用例被过滤
    # 真实 AICase 已生成（project 为 null）
    aic = AICase.objects.get(source_ref=f"{TESTGEN_ID_TAG_PREFIX}T1")
    assert aic.project is None
    assert aic.created_by_id == user.id
