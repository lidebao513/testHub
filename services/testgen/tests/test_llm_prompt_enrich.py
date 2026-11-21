"""第二步 C · LLM 提示词升级（喂真实签名/语义/文件背景）。

验证两条 LLM 链路的提示词从「只给函数名」升级为「给函数签名 + docstring 语义 +
describe 文件业务背景」，让 LLM 一旦启用就能设计贴题用例（而不是只知道有个函数）。

护栏保持：开关关闭时仍走纯规则、describe 缺失时零影响降级、防幻觉校验层不变。
本测试不触网——用文本断言 + fresh_db 缓存，LLM 调用由 conftest 的 session 桩兜底。
"""

from __future__ import annotations

from core import store
from core.contracts import FunctionalPoint
from core.enums import FType
from engine import llm_design, pipeline, semantic_enrich


def _fp(
    signature: str = "", semantic: str = "", path: str = "cockpit/domain.py"
) -> FunctionalPoint:
    return FunctionalPoint(
        fp_id="FP-test0001",
        ftype=FType.BUSINESS.value,
        file_path=path,
        name="get_domain_detail",
        title="cockpit · get_domain_detail",
        module="cockpit",
        semantic=semantic,
        signature_hint=signature,
    )


# ---------------------------------------------------------------------------
# 共享事实行
# ---------------------------------------------------------------------------
def test_fact_line_carries_signature_semantic_and_file_context():
    fp = _fp(
        signature="入参 domain_id、top_n；返回 DomainDetail",
        semantic="get_domain_detail（入参 domain_id、top_n）",
    )
    line = semantic_enrich.fp_fact_line(fp, {fp.file_path: "领域管理模块，负责域名查询与解析"})
    assert "fp_id=FP-test0001" in line
    assert "真实签名[入参 domain_id、top_n；返回 DomainDetail]" in line
    assert "功能语义[get_domain_detail（入参 domain_id、top_n）]" in line
    assert "文件业务背景" in line and "领域管理模块" in line


def test_fact_line_no_context_is_clean():
    """无 describe 背景时只输出代码事实，不留空括号/空行。"""
    line = semantic_enrich.fp_fact_line(_fp(signature="入参 x；返回 y", semantic="功能 x"))
    assert "文件业务背景" not in line
    assert "真实签名[入参 x；返回 y]" in line


# ---------------------------------------------------------------------------
# semantic_enrich 提示词
# ---------------------------------------------------------------------------
def test_semantic_enrich_prompt_uses_real_facts():
    fp = _fp(signature="入参 domain_id；返回 DomainDetail", semantic="按域名查详情")
    prompt = semantic_enrich._build_prompt([fp], 50, {fp.file_path: "查询领域详情的服务"})
    assert "真实签名[入参 domain_id；返回 DomainDetail]" in prompt
    assert "功能语义[按域名查详情]" in prompt
    assert "文件业务背景" in prompt


def test_semantic_enrich_prompt_limit_still_applies():
    """max_candidates 限制提示词里功能点数量（范围限定，增量成本护栏）。"""
    fps = [_fp(signature=f"入参 p{i}；返回 R") for i in range(10)]
    prompt = semantic_enrich._build_prompt(fps, 3)
    assert "p0" in prompt and "p2" in prompt
    assert "p3" not in prompt


# ---------------------------------------------------------------------------
# llm_design 提示词（复用口径）
# ---------------------------------------------------------------------------
def test_llm_design_prompt_uses_real_facts():
    fp = _fp(signature="入参 domain_id；返回 DomainDetail", semantic="按域名查详情")
    prompt = llm_design._build_prompt([fp], [], None, {fp.file_path: "领域服务"})
    assert "真实签名[入参 domain_id；返回 DomainDetail]" in prompt
    assert "功能语义[按域名查详情]" in prompt
    assert "领域服务" in prompt


# ---------------------------------------------------------------------------
# describe 缓存回填（load_file_context）
# ---------------------------------------------------------------------------
def test_load_file_context_reads_only_requested_paths(fresh_db):
    store.upsert_file_description("repo-A", "cockpit/domain.py", "sig1", "领域查询模块", "m")
    store.upsert_file_description("repo-A", "other.py", "sig2", "无关模块", "m")
    fps = [_fp(path="cockpit/domain.py")]
    ctx = pipeline.load_file_context("repo-A", fps)
    assert ctx == {"cockpit/domain.py": "领域查询模块"}
    assert "other.py" not in ctx  # 范围限定：只查本次功能点涉及的文件


def test_load_file_context_missing_cache_is_empty(fresh_db):
    """describe 未跑过 / 缓存缺失 → 空字典，零影响降级。"""
    ctx = pipeline.load_file_context("repo-none", [_fp(path="never.py")])
    assert ctx == {}


def test_load_file_context_empty_fps_is_empty():
    assert pipeline.load_file_context("repo-A", []) == {}


# ---------------------------------------------------------------------------
# 护栏：开关关闭时零影响
# ---------------------------------------------------------------------------
def test_enrich_disabled_stays_rule_only():
    """LLM_ENHANCE=off 时 enrich 只走规则，不因新字段产生任何 LLM 候选。"""
    result = semantic_enrich.enrich(
        [], [_fp(signature="入参 x；返回 y")], semantic_enrich.EnrichOptions(enabled=False)
    )
    assert result.llm_used is False
    assert any("规则引擎" in n for n in result.notes)
