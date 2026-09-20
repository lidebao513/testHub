"""F10b：LLM 用例设计真实实现 · 两条护栏的单元测试。

护栏 A（不臆造）：LLM 候选引用的 fp_id 必须真实存在，否则进 rejected。
护栏 B（不静默）：未启用 / 配置缺失 / 调用失败 / 0 新增 → 返回空 added + notes 明示，
              stage 层面（开关开但 0 新增）写 result.errors。

通过 client_factory 注入假客户端，绝不触达真实 LLM 网络。
"""

from __future__ import annotations

from typing import Any

from core.contracts import CaseSpec, FunctionalPoint, VerifyLayer
from engine import llm_design
from engine.llm_design import DesignOptions


def _fp(
    fp_id: str = "FP-1",
    ftype: str = "api",
    name: str = "POST /api/x",
    module: str = "m",
) -> FunctionalPoint:
    return FunctionalPoint(
        fp_id=fp_id, ftype=ftype, file_path="a.py", name=name, title=name, module=module
    )


def _factory(payload: list[dict[str, Any]]):
    """构造 client_factory：返回固定 payload 的假 LLM 客户端。"""

    def _make(opts: Any) -> Any:
        class _FakeClient:
            def available(self) -> bool:
                return True

            def complete_json(self, prompt: str) -> list[dict[str, Any]]:
                return payload

        return _FakeClient()

    return _make


# ---------------------------------------------------------------------------
# 护栏 B：未启用 / 配置缺失 / 调用失败 → 空 added + 明示 notes
# ---------------------------------------------------------------------------
def test_disabled_returns_empty_with_note():
    """开关关闭：不调 LLM，added 为空且 notes 明示「未新增」。"""
    opts = DesignOptions(enabled=False)
    res = llm_design.design_cases([_fp()], [], None, opts)
    assert res.added == []
    assert any("未新增" in n or "未启用" in n for n in res.notes)


def test_enabled_but_no_config_returns_empty():
    """开关开但无 LLM 配置：用真实 LLMClient，available()=False → 降级空 added + notes。"""
    opts = DesignOptions(enabled=True)  # 无 api_key / base_url / model
    res = llm_design.design_cases([_fp()], [], None, opts)
    assert res.added == []
    assert any("未新增" in n or "配置缺失" in n for n in res.notes)


def test_llm_failure_degrades_to_empty():
    """LLM 调用抛异常：不中断主链路，added 为空 + notes 记录降级。"""

    def _boom_factory(opts: Any) -> Any:
        class _Boom:
            def available(self) -> bool:
                return True

            def complete_json(self, prompt: str) -> list[dict[str, Any]]:
                raise RuntimeError("model 503")

        return _Boom()

    opts = DesignOptions(enabled=True, api_key="k", base_url="http://x", model="m")
    res = llm_design.design_cases([_fp()], [], None, opts, client_factory=_boom_factory)
    assert res.added == []
    assert any("降级" in n for n in res.notes)


# ---------------------------------------------------------------------------
# 护栏 A：臆造 fp_id 必须被拒
# ---------------------------------------------------------------------------
def test_fabricated_fp_id_rejected():
    opts = DesignOptions(enabled=True, api_key="k", base_url="http://x", model="m")
    payload = [
        {"fp_id": "FP-999", "category": "异常", "title": "x", "expect": "y", "priority": "P1"}
    ]
    res = llm_design.design_cases([_fp()], [], None, opts, client_factory=_factory(payload))
    assert res.added == []
    assert len(res.rejected) == 1
    assert "臆造" in res.rejected[0]["reason"]


def test_invalid_category_rejected():
    opts = DesignOptions(enabled=True, api_key="k", base_url="http://x", model="m")
    payload = [{"fp_id": "FP-1", "category": "魔法", "title": "x", "expect": "y"}]
    res = llm_design.design_cases([_fp()], [], None, opts, client_factory=_factory(payload))
    assert res.added == []
    assert res.rejected and "category" in res.rejected[0]["reason"]


def test_empty_title_rejected():
    opts = DesignOptions(enabled=True, api_key="k", base_url="http://x", model="m")
    payload = [{"fp_id": "FP-1", "category": "异常", "title": "", "expect": "y"}]
    res = llm_design.design_cases([_fp()], [], None, opts, client_factory=_factory(payload))
    assert res.added == []
    assert res.rejected


# ---------------------------------------------------------------------------
# 正常：真实 fp_id → 产出格式合规的 CaseSpec
# ---------------------------------------------------------------------------
def test_valid_candidate_accepted_and_well_formed():
    opts = DesignOptions(enabled=True, api_key="k", base_url="http://x", model="m")
    payload = [
        {
            "fp_id": "FP-1",
            "category": "异常",
            "title": "非法输入返回 4xx",
            "expect": "返回 400",
            "priority": "P1",
        }
    ]
    res = llm_design.design_cases([_fp()], [], None, opts, client_factory=_factory(payload))
    assert len(res.added) == 1
    spec: CaseSpec = res.added[0]
    assert spec.fp_contract_id == "FP-1"
    assert spec.tc_no.startswith("TC-")
    # 八要素齐全
    assert spec.missing_elements() == []
    # 优先级以 LLM 建议为准
    assert spec.priority == "P1"
    # 机器步 layer 与功能点类型一致（接口层）
    assert spec.steps[0]["layer"] == VerifyLayer.INTERFACE.value
    # 编号稳定：同输入重复生成一致
    res2 = llm_design.design_cases([_fp()], [], None, opts, client_factory=_factory(payload))
    assert res2.added[0].tc_no == spec.tc_no


def test_ui_fp_yields_ui_layer_case():
    """UI 层功能点 → 用例机器步 layer=ui，ctype=e2e。"""
    opts = DesignOptions(enabled=True, api_key="k", base_url="http://x", model="m")
    payload = [{"fp_id": "FP-1", "category": "正常", "title": "页面可达", "expect": "渲染成功"}]
    res = llm_design.design_cases(
        [_fp(ftype="page", name="/pc/tasks")], [], None, opts, client_factory=_factory(payload)
    )
    assert len(res.added) == 1
    assert res.added[0].steps[0]["layer"] == VerifyLayer.UI.value


def test_multiple_candidates_one_fabricated():
    """混合候选：真实 1 条 + 臆造 1 条 → 仅真实条入库。"""
    opts = DesignOptions(enabled=True, api_key="k", base_url="http://x", model="m")
    payload = [
        {"fp_id": "FP-1", "category": "边界", "title": "缺参", "expect": "400"},
        {"fp_id": "FP-ghost", "category": "异常", "title": "不存在", "expect": "x"},
    ]
    res = llm_design.design_cases([_fp()], [], None, opts, client_factory=_factory(payload))
    assert len(res.added) == 1
    assert res.added[0].fp_contract_id == "FP-1"
    assert len(res.rejected) == 1
