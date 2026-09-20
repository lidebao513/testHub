"""P2 接入骨架冒烟测试：模块可导入、LLM 设计桩按预期抛出 NotImplementedError。

PRD 解析（`prd_ingest`）已落地为真实实现，其行为测试见 `test_p2_prd_ingest.py`。
"""

from __future__ import annotations

from engine import llm_design, prd_ingest


def test_modules_importable():
    assert prd_ingest.PrdDoc is not None
    assert prd_ingest.PrdRequirement is not None
    assert llm_design.DesignOptions is not None
    assert llm_design.DesignResult is not None


def test_prd_format_resolver():
    assert prd_ingest._resolve_format("api.yaml", "auto") == "openapi"
    assert prd_ingest._resolve_format("spec.json", "auto") == "openapi"
    assert prd_ingest._resolve_format("prd.md", "auto") == "markdown"
    assert prd_ingest._resolve_format("x.md", "markdown") == "markdown"


def test_llm_design_realized():
    """F10b：design_cases 已真实实现（不再抛 NotImplementedError），未启用返回空 added。"""
    res = llm_design.design_cases([], [])
    assert isinstance(res, llm_design.DesignResult)
    assert res.added == []
