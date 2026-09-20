"""stage_registry 单元测试（P0-3 基线 + P1-1 适配层落地）。

仅断言注册元数据与契约 dataclass 可实例化，**不触发**真实 LLM / playwright /
git 仓库等重依赖（wired 阶段的 fn 只取引用，不执行）。

P1-1 演进：适配层 6 项由 planned 占位填充为真实 stage_* 实现（planned=False）；
新增 `execute` 适配阶段（用例执行）。能力层 11 项名称不变，其 fn 在
`engine.pipeline._wire_stages_to_registry()` 中由「原始纯函数」覆盖为「编排包装」，
使 run_pipeline 经 REGISTRY 组链驱动。故总阶段数 11 + 7 = 18。
"""

from engine.stage_registry import (
    REGISTRY,
    ComparatorStageInput,
    ComparatorStageOutput,
    ExtractStageInput,
    ExtractStageOutput,
    Stage,
    StageKind,
    adaptation_stage_names,
    capability_stage_names,
    get_stage,
    list_stages,
)


CAPABILITY = [
    "extract",
    "tp_expand",
    "case_gen",
    "semantic_enrich",
    "expert_review",
    "llm_design",
    "prd_ingest",
    "comparator",
    "runtime_ui",
    "auth_scan",
    "tag",
]
ADAPTATION = [
    "pull",
    "resolve_sources",
    "register",
    "scan",
    "partition_channels",
    "persist",
    "execute",
]


def test_registry_has_11_capability_stages():
    assert sorted(capability_stage_names()) == sorted(CAPABILITY)


def test_registry_has_7_adaptation_stages():
    assert sorted(adaptation_stage_names()) == sorted(ADAPTATION)


def test_registry_total_18():
    assert len(REGISTRY.list_stages()) == 18


def test_capability_stages_are_wired():
    for name in CAPABILITY:
        stage = get_stage(name)
        assert stage.kind == StageKind.CAPABILITY
        assert stage.fn is not None
        assert stage.input_type is not None
        assert stage.output_type is not None
        assert stage.planned is False


def test_adaptation_stages_are_wired():
    for name in ADAPTATION:
        stage = get_stage(name)
        assert stage.kind == StageKind.ADAPTATION
        assert stage.planned is False
        assert stage.fn is not None


def test_get_stage_unknown_raises_keyerror():
    try:
        get_stage("nope")
    except KeyError:
        pass
    else:
        raise AssertionError("expected KeyError for unknown stage")


def test_get_fn_returns_callable_for_wired():
    fn = REGISTRY.get_fn("extract")
    assert callable(fn)


def test_get_fn_works_for_wired_adaptation_stage():
    # P1-1：适配层已全部实现，get_fn 对适配阶段返回可调用对象
    fn = REGISTRY.get_fn("pull")
    assert callable(fn)
    assert not any(s.planned for s in list_stages())


def test_no_planned_stages_remain():
    # P1-1：所有阶段均已实现，不存在 planned 占位阶段
    assert [s.name for s in list_stages() if s.planned] == []


def test_comparator_contract_aliases():
    from engine.comparator import CompareInput, CompareVerdict

    assert ComparatorStageInput is CompareInput
    assert ComparatorStageOutput is CompareVerdict


def test_contract_dataclasses_instantiate():
    i = ExtractStageInput(files={})
    assert i.include_business is True
    assert i.business_include_dirs is None
    o = ExtractStageOutput(
        functional_points=[],
        screens=[],
        components=[],
        business_absorbed=[],
        business_absorbed_examples=[],
    )
    assert o.functional_points == []


def test_register_duplicate_raises():
    reg = type(REGISTRY)()
    reg.register(Stage(name="x", kind=StageKind.CAPABILITY))
    try:
        reg.register(Stage(name="x", kind=StageKind.CAPABILITY))
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError on duplicate registration")


def test_list_stages_filter_by_kind():
    caps = list_stages(StageKind.CAPABILITY)
    assert len(caps) == len(CAPABILITY)
    assert all(s.kind == StageKind.CAPABILITY for s in caps)
    adapts = list_stages(StageKind.ADAPTATION)
    assert len(adapts) == len(ADAPTATION)
    assert all(s.kind == StageKind.ADAPTATION for s in adapts)


def test_stagekind_values():
    assert StageKind.CAPABILITY.value == "capability"
    assert StageKind.ADAPTATION.value == "adaptation"
