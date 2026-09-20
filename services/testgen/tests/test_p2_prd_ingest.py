"""P2 真实解析测试：Markdown / OpenAPI → 结构化需求 → 业务规则测试点。

覆盖：两种格式解析、编号稳定、缺文件/非法格式报错、需求对齐功能点、
未对齐需求不臆造（只记提示）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.contracts import FunctionalPoint
from core.enums import Dimension, FType, ReviewStatus, TPType
from core.errors import ConfigError, EngineError
from engine import prd_ingest


_MD = """# 账单系统需求

## 查询账单列表
支持分页查询，接口：GET /api/v1/invoices
标签：账单, 查询

## 创建账单
创建后需要审批流。接口 POST /api/v1/invoices
"""


def _write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _fp(name: str, fp_id: str = "FP-abc12345") -> FunctionalPoint:
    return FunctionalPoint(
        fp_id=fp_id,
        ftype=FType.API.value,
        file_path="billing/api.py",
        name=name,
        title=name,
        module="billing",
    )


# ---------------------------------------------------------------- Markdown
def test_ingest_markdown(tmp_path):
    src = _write(tmp_path, "prd.md", _MD)
    doc = prd_ingest.ingest_prd(str(src))
    assert doc.fmt == prd_ingest.PRD_FORMAT_MARKDOWN
    assert len(doc.requirements) == 2
    first = doc.requirements[0]
    assert first.title == "查询账单列表"
    assert "GET /api/v1/invoices" in first.endpoints
    assert "账单" in first.tags
    assert first.rid.startswith("R-")


def test_markdown_rid_is_stable(tmp_path):
    """同一份文档重复解析，需求编号必须一致（绑内容指纹，非位置）。"""
    src = _write(tmp_path, "prd.md", _MD)
    first = prd_ingest.ingest_prd(str(src)).requirements
    again = prd_ingest.ingest_prd(str(src)).requirements
    assert [r.rid for r in first] == [r.rid for r in again]


def test_markdown_without_requirements_records_note(tmp_path):
    src = _write(tmp_path, "flat.md", "只有正文，没有任何二级标题。\n")
    doc = prd_ingest.ingest_prd(str(src))
    assert doc.requirements == []
    assert doc.notes


# ---------------------------------------------------------------- OpenAPI
def test_ingest_openapi_yaml(tmp_path):
    spec = (
        'openapi: "3.0.0"\n'
        "info: {title: demo, version: '1'}\n"
        "paths:\n"
        "  /api/v1/invoices:\n"
        "    get:\n"
        "      summary: 查询账单列表\n"
        "      tags: [账单]\n"
        "    post:\n"
        "      summary: 创建账单\n"
    )
    src = _write(tmp_path, "api.yaml", spec)
    doc = prd_ingest.ingest_prd(str(src))
    assert doc.fmt == prd_ingest.PRD_FORMAT_OPENAPI
    endpoints = {e for r in doc.requirements for e in r.endpoints}
    assert endpoints == {"GET /api/v1/invoices", "POST /api/v1/invoices"}
    assert {r.title for r in doc.requirements} == {"查询账单列表", "创建账单"}


def test_ingest_openapi_json(tmp_path):
    spec = {"openapi": "3.0.0", "paths": {"/x": {"get": {"summary": "查看"}}}}
    src = _write(tmp_path, "api.json", json.dumps(spec, ensure_ascii=False))
    doc = prd_ingest.ingest_prd(str(src))
    assert [r.title for r in doc.requirements] == ["查看"]
    assert doc.requirements[0].endpoints == ["GET /x"]


# ---------------------------------------------------------------- 错误路径
def test_missing_file_raises_engine_error(tmp_path):
    with pytest.raises(EngineError):
        prd_ingest.ingest_prd(str(tmp_path / "nope.md"))


def test_illegal_format_raises_config_error(tmp_path):
    src = _write(tmp_path, "x.md", "# 标题")
    with pytest.raises(ConfigError):
        prd_ingest.ingest_prd(str(src), prd_ingest.PrdIngestOptions(fmt="pdf"))


# ---------------------------------------------------------------- 需求对齐
def test_requirements_align_to_functional_points(tmp_path):
    src = _write(tmp_path, "prd.md", _MD)
    doc = prd_ingest.ingest_prd(str(src))
    fps = [
        _fp("GET /api/v1/invoices", "FP-00000001"),
        _fp("POST /api/v1/invoices", "FP-00000002"),
        _fp("GET /api/v1/other", "FP-00000003"),
    ]
    tps = prd_ingest.requirements_to_test_points(doc, fps)
    assert len(tps) == 2
    assert {tp.fp_contract_id for tp in tps} == {"FP-00000001", "FP-00000002"}
    for tp in tps:
        assert tp.category == TPType.NORMAL.value
        assert tp.dimension == Dimension.BIZ_RULE.value
        assert tp.origin == "prd"
        assert tp.review_status == ReviewStatus.PENDING.value
        assert tp.unverified is True
        assert tp.evidence and tp.evidence[0].startswith("prd:")
        assert tp.tp_id.startswith("TP-")
    assert all(r.matched_fp_id for r in doc.requirements)


def test_unmatched_requirement_not_fabricated(tmp_path):
    src = _write(tmp_path, "prd.md", "## 需求甲\n接口 POST /api/v1/ghost\n")
    doc = prd_ingest.ingest_prd(str(src))
    tps = prd_ingest.requirements_to_test_points(doc, [_fp("GET /api/v1/invoices")])
    assert tps == [], "代码里没有的能力不得臆造测试点"
    assert any("未对齐" in n for n in doc.notes)
