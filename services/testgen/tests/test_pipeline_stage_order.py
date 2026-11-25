"""A-4 全序守护：stage 单表事实源的顺序、唯一性、fn 可调用与 REGISTRY 契约不变。

回归底线：任何阶段顺序变更都必须显式修改本文件的 ``EXPECTED_STAGE_ORDER``，
不允许「改一张表忘另一张」造成静默错序。
"""

from engine import pipeline
from engine.stage_registry import REGISTRY


EXPECTED_STAGE_ORDER: tuple[str, ...] = (
    "pull",
    "resolve_sources",
    "register",
    "scan",
    "auth_scan",
    "extract",
    "runtime_ui",
    "tag",
    "tp_expand",
    "semantic_enrich",
    "expert_review",
    "prd_ingest",
    "case_gen",
    "llm_design",
    "quality",
    "execute",
    "partition_channels",
    "persist",
)


def test_pipeline_stages_full_order_locked() -> None:
    assert tuple(spec.name for spec in pipeline.PIPELINE_STAGES) == EXPECTED_STAGE_ORDER


def test_pipeline_stage_names_unique() -> None:
    names = [spec.name for spec in pipeline.PIPELINE_STAGES]
    assert len(names) == len(set(names)) == 18


def test_pipeline_stage_fns_callable() -> None:
    assert all(callable(spec.fn) for spec in pipeline.PIPELINE_STAGES)


def test_derived_stage_order_matches_specs() -> None:
    assert pipeline.PIPELINE_STAGE_ORDER == EXPECTED_STAGE_ORDER


def test_import_time_validator_present() -> None:
    assert callable(pipeline._validate_pipeline_stages)


def test_stage_kind_matches_registry() -> None:
    for spec in pipeline.PIPELINE_STAGES:
        assert REGISTRY.get(spec.name).kind == spec.kind


def test_registry_mirrors_stage_fns() -> None:
    """REGISTRY 镜像的 fn 必须与单表同源（回注契约不漂移）。"""
    for spec in pipeline.PIPELINE_STAGES:
        assert REGISTRY.get_fn(spec.name) is spec.fn


def test_registry_contract_unchanged() -> None:
    stages = REGISTRY.list_stages()
    assert len(stages) == 19
    assert [s.name for s in stages if s.planned] == []


def test_comparator_not_in_pipeline_but_in_registry() -> None:
    names = [spec.name for spec in pipeline.PIPELINE_STAGES]
    assert "comparator" not in names
    assert REGISTRY.contains("comparator")


def test_quality_relative_order_locked() -> None:
    names = [spec.name for spec in pipeline.PIPELINE_STAGES]
    assert names.index("quality") > names.index("case_gen")
    assert names.index("quality") > names.index("llm_design")
    assert names.index("quality") < names.index("persist")
