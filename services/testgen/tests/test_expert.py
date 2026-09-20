"""测试专家系统 · URL 通道（PageExpert）单测（#276 门禁补 expert 检查）。

用 FakeLLMClient 替掉真实 LLM，验证：
- 红线护栏（base）：不臆造 / 不静默 / 优雅降级；
- S0 菜单反推（治本）：enabled 返回菜单文字、disabled 返回空 + notes；
- S1–S6 提质：area 命中真实锚点才保留、未命中进 rejected、覆盖缺口诚实披露；
- 契约转换：expert_to_test_point 落 origin=expert_page / expert=PageExpert。
全程不触网、不依赖真实 LLM 配置。
"""

from __future__ import annotations

from engine.expert.base import (
    ExpertResult,
    build_anchor_pool,
    validate_area_hits,
)
from engine.expert.llm import ExpertLLMOptions
from engine.expert.page_expert import (
    ORIGIN_EXPERT_PAGE,
    PageCapture,
    PageExpert,
    PageExpertOptions,
    expert_to_test_point,
)


class FakeLLMClient:
    """替身：complete_json 按提示区分 S0（菜单）与 S1–S6（测试点）。"""

    def __init__(self, opts: ExpertLLMOptions) -> None:
        self.opts = opts

    def available(self) -> bool:
        return True

    def complete_json(self, user_prompt, *, system_prompt=None, images=None):
        if "菜单" in user_prompt or "列菜单项" in user_prompt:
            return ["工作台", "任务管理", "报表中心"], {
                "used_vision": False,
                "vision_note": "",
            }
        items = [
            {
                "area": "button.send-btn",
                "category": "正常",
                "dimension": "发送",
                "title": "发送消息",
                "expect": "消息成功发出",
                "priority": "P1",
                "verify_layer": "UI",
                "rationale": "核心动作",
            },
            {
                "area": "/api/chat",
                "category": "安全",
                "dimension": "越权",
                "title": "越权读取他人会话",
                "expect": "返回 403",
                "priority": "P1",
                "verify_layer": "接口",
                "rationale": "越权读",
            },
            {
                "area": "button.nope-x",
                "category": "边界",
                "dimension": "空态",
                "title": "臆造锚点（不应通过）",
                "expect": "x",
                "priority": "P2",
                "verify_layer": "UI",
                "rationale": "不应通过护栏",
            },
        ]
        return items, {"used_vision": False, "vision_note": ""}


def _fake_factory(opts: ExpertLLMOptions) -> FakeLLMClient:
    return FakeLLMClient(opts)


def _capture() -> PageCapture:
    return PageCapture(
        url="http://x/chat",
        path="/chat",
        title="聊天",
        elements=[{"selector": "button.send-btn", "text": "发送", "visible": True}],
        xhr_list=[{"method": "POST", "path": "/api/chat"}],
        discovered_routes=["/home", "/settings"],
    )


# --------------------------------------------------------------------------
# base：红线护栏
# --------------------------------------------------------------------------
def test_validate_area_hits_pass():
    assert validate_area_hits("button.send-btn", {"button.send-btn", "/api/chat"}) is None


def test_validate_area_hits_miss():
    reason = validate_area_hits("button.ghost", {"button.send-btn"})
    assert reason is not None
    assert "臆造" in reason


def test_validate_area_hits_empty():
    assert validate_area_hits("", {"button.send-btn"}) is not None


def test_expert_result_notes_dedup_and_reject():
    r = ExpertResult()
    r.add_note("同一说明")
    r.add_note("同一说明")  # 去重
    assert r.notes == ["同一说明"]
    r.reject({"area": "x"}, "area 未命中")
    assert len(r.rejected) == 1
    assert r.rejected[0]["reason"] == "area 未命中"
    assert r.rejected[0]["item"] == {"area": "x"}


def test_build_anchor_pool_merges_and_dedups():
    pool = build_anchor_pool(element_selectors=["a", "a"], routes=["/b"], xhr_paths=["/api/x"])
    assert pool == {"a", "/b", "/api/x"}


# --------------------------------------------------------------------------
# S0：菜单反推（治本）
# --------------------------------------------------------------------------
def test_propose_routes_enabled_returns_labels():
    expert = PageExpert(PageExpertOptions(enabled=True), client_factory=_fake_factory)
    labels, meta = expert.propose_routes(_capture())
    assert labels == ["工作台", "任务管理", "报表中心"]
    assert meta["count"] == 3


def test_propose_routes_disabled_returns_empty_with_note():
    expert = PageExpert(PageExpertOptions(enabled=False), client_factory=_fake_factory)
    labels, meta = expert.propose_routes(_capture())
    assert labels == []
    assert "note" in meta


# --------------------------------------------------------------------------
# S1–S6：提质（把关不臆造 + 覆盖缺口）
# --------------------------------------------------------------------------
def test_review_page_filters_unreal_anchors():
    expert = PageExpert(PageExpertOptions(enabled=True), client_factory=_fake_factory)
    res = expert.review_page(_capture())
    added_areas = {t.area for t in res.added}
    assert "button.send-btn" in added_areas
    assert "/api/chat" in added_areas
    # 臆造锚点必须被拒
    assert any("button.nope-x" in str(r["item"]) for r in res.rejected)
    # 诚实披露覆盖缺口（本次未覆盖 异常/边界 维度）
    assert res.coverage_gaps
    assert res.used_vision is False


def test_review_page_disabled_returns_empty_with_note():
    expert = PageExpert(PageExpertOptions(enabled=False), client_factory=_fake_factory)
    res = expert.review_page(_capture())
    assert res.added == []
    assert res.notes  # 不静默


# --------------------------------------------------------------------------
# 契约转换
# --------------------------------------------------------------------------
def test_expert_to_test_point_origin_and_expert():
    from engine.expert.page_expert import ExpertTestPoint

    etp = ExpertTestPoint(
        area="button.send-btn",
        category="正常",
        dimension="发送",
        title="发送消息",
        expect="消息发出",
        verify_layer="UI",
        rationale="核心动作",
    )
    tp = expert_to_test_point(etp, "fp_demo_1", "聊天功能点", "chat")
    assert tp.origin == ORIGIN_EXPERT_PAGE
    assert tp.expert == "PageExpert"
    assert tp.unverified is True
    assert tp.method == "EXPERT"
    assert tp.fp_contract_id == "fp_demo_1"
    assert tp.tp_id  # 稳定指纹非空
