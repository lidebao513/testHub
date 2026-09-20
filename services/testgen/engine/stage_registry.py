"""能力层阶段契约与注册表（模块化 P0-3）。

本模块是 testgen「能力层 / 适配层」拆分的中枢描述层：

- 为 11 项**能力层**阶段定义 ``StageInput`` / ``StageOutput`` 契约（只读描述 +
  薄适配器），并将每项映射到既有的「无副作用纯函数」实现；
- 提供 ``StageRegistry``，为后续的 P1 适配层「按 registry 组链」提供单一事实源；
- 为 6 项**适配层**阶段预留 ``planned=True`` 占位（P1-1 填充实现）。

设计铁律（不破坏既有调用）：
- 本模块只描述契约与登记映射，**不改写**任何既有函数签名，也不改写
  ``pipeline.run_pipeline`` 的硬编码编排（那一步留给 P1-1）。
- 仅依赖 ``core``（契约/枚举）与 ``engine`` 内部模块，绝不 import ``service`` / ``cli``。
- ``expert_review`` 的真函数 ``PageExpert.review_page`` 为类方法，使用懒加载适配器薄封装，
  避免导入期拉入 playwright 等重依赖。

验收：八环门禁全绿；现有端到端用例无回归（本模块为只读描述 + 登记，不改变运行路径）。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from engine.comparator import CompareInput as ComparatorStageInput
from engine.comparator import CompareVerdict as ComparatorStageOutput


class StageKind(StrEnum):
    """阶段类别：能力层（纯计算）/ 适配层（编排·存储·双通道）。"""

    CAPABILITY = "capability"
    ADAPTATION = "adaptation"


# ---------------------------------------------------------------------------
# 11 项能力层 · 契约（StageInput / StageOutput）
# 说明：以下 dataclass 为「规范化契约描述」，字段取自各阶段既有实现的公开签名；
# 运行期真实函数签名维持不变（P1-1 再统一收敛到这些契约）。
# ---------------------------------------------------------------------------


# 1) extract —— 文件集 -> 功能点
@dataclass
class ExtractStageInput:
    files: dict[str, Any]
    include_business: bool = True
    extract_pages: bool = True
    extract_components: bool = True
    business_extract_mode: str = "strict"
    business_include_dirs: list[str] | None = None


@dataclass
class ExtractStageOutput:
    functional_points: list[Any]
    screens: list[Any]
    components: list[Any]
    business_absorbed: list[str]
    business_absorbed_examples: list[str]


# 2) tp_expand —— 功能点 -> 测试点
@dataclass
class TpExpandStageInput:
    fps: list[Any]
    ctx: Any | None = None
    tag_by_fp: dict[str, str] | None = None


@dataclass
class TpExpandStageOutput:
    test_points: list[Any]
    scope_counts: dict[str, int] | None = None


# 3) case_gen —— 测试点 -> 用例
@dataclass
class CaseGenStageInput:
    tps: list[Any]
    fp_row_map: dict[str, int] | None = None
    ui_modules: set[str] | None = None
    strategy: str = "ui_first"
    drop_supplement: bool = False
    runtime_index: Mapping[str, Any] | None = None
    auth_profile: Any = None


@dataclass
class CaseGenStageOutput:
    cases: list[Any]
    coverage: dict[str, Any] | None = None


# 4) semantic_enrich —— 规则 + 可选 LLM 增强
@dataclass
class SemanticEnrichStageInput:
    tps: list[Any]
    fps: list[Any]
    options: Any | None = None


@dataclass
class SemanticEnrichStageOutput:
    test_points: list[Any]
    notes: list[str]
    added: int = 0
    rejected: int = 0


# 5) expert_review —— 页面渲染反推（PageExpert）
@dataclass
class ExpertReviewStageInput:
    capture: Any
    options: Any | None = None


@dataclass
class ExpertReviewStageOutput:
    hits: list[Any]
    notes: list[str]
    areas: dict[str, Any] | None = None


# 6) llm_design —— LLM 补充用例设计
@dataclass
class LlmDesignStageInput:
    fps: list[Any]
    tps: list[Any]
    prd: Any | None = None
    options: Any | None = None
    client_factory: Callable[..., Any] | None = None


@dataclass
class LlmDesignStageOutput:
    added: list[Any]
    rejected: list[Any]
    notes: list[str]


# 7) prd_ingest —— PRD 文档 -> 结构化需求
@dataclass
class PrdIngestStageInput:
    source: str
    options: Any | None = None


@dataclass
class PrdIngestStageOutput:
    requirements: list[Any]
    fmt: str
    raw: str
    notes: list[str]


# 8) comparator —— 预期 vs 实际 语义比对（复用 P0-1 契约，见上方顶部别名）


# 9) runtime_ui —— 运行时 UI 发现
@dataclass
class RuntimeUiStageInput:
    options: Any | None = None
    static_paths: list[str] | None = None
    expert_options: Any = None


@dataclass
class RuntimeUiStageOutput:
    pages: list[Any]
    elements: list[Any]
    console_errors: list[str]
    logged_in: bool = False


# 10) auth_scan —— 鉴权接线画像
@dataclass
class AuthScanStageInput:
    files: dict[str, Any]


@dataclass
class AuthScanStageOutput:
    login_signals: list[str]
    authz_signals: list[str]
    summary: dict[str, Any] | None = None


# 11) tag —— 增量打标（git diff -> 变更上下文）
@dataclass
class TagStageInput:
    repo: str
    base: str | None = None
    target: str | None = None


@dataclass
class TagStageOutput:
    changed_files: set[str]
    is_incremental: bool = False
    base: str | None = None
    target: str | None = None


# ---------------------------------------------------------------------------
# Stage 描述符 + 注册表
# ---------------------------------------------------------------------------


@dataclass
class Stage:
    """单个阶段的注册描述符。"""

    name: str
    kind: StageKind
    fn: Callable[..., Any] | None = None
    input_type: type | None = None
    output_type: type | None = None
    description: str = ""
    planned: bool = False

    def run(self, *args: Any, **kwargs: Any) -> Any:
        """调用已登记的实现；未实现（planned）阶段抛 NotImplementedError。"""
        fn = self.fn
        if fn is None:
            raise NotImplementedError(f"stage {self.name!r} 尚未实现（planned={self.planned}）")
        return fn(*args, **kwargs)


class StageRegistry:
    """能力层 / 适配层阶段注册表（P1 适配层组链的唯一事实源）。"""

    def __init__(self) -> None:
        self._stages: dict[str, Stage] = {}

    def register(self, stage: Stage) -> Stage:
        if stage.name in self._stages:
            raise ValueError(f"重复注册阶段：{stage.name}")
        self._stages[stage.name] = stage
        return stage

    def get(self, name: str) -> Stage:
        if name not in self._stages:
            raise KeyError(f"阶段未注册：{name}")
        return self._stages[name]

    def get_fn(self, name: str) -> Callable[..., Any]:
        stage = self.get(name)
        if stage.fn is None:
            raise NotImplementedError(f"stage {name!r} 尚未实现（planned）")
        return stage.fn

    def contains(self, name: str) -> bool:
        return name in self._stages

    def list_stages(self, kind: StageKind | None = None) -> list[Stage]:
        if kind is None:
            return list(self._stages.values())
        return [s for s in self._stages.values() if s.kind == kind]

    def names(self, kind: StageKind | None = None) -> list[str]:
        return [s.name for s in self.list_stages(kind)]


# ---------------------------------------------------------------------------
# 实现映射（薄适配器）
# ---------------------------------------------------------------------------


def _expert_review_adapter(capture: Any, options: Any = None) -> Any:
    """薄适配器：构造 PageExpert 并调用 review_page（懒加载，避免导入期拉入 playwright）。"""
    from engine.expert.page_expert import PageExpert

    return PageExpert(options).review_page(capture)


def _build_registry() -> StageRegistry:
    """登记 11 项能力层（已实现）+ 6 项适配层（planned 占位），共 17 个阶段。"""
    reg = StageRegistry()

    # ---- 能力层（11） ----
    from engine.auth_scan import scan_auth
    from engine.case_gen import generate_cases
    from engine.comparator import compare_one
    from engine.diff_tag import build_context
    from engine.fp_extract import extract_functional_points
    from engine.llm_design import design_cases
    from engine.prd_ingest import ingest_prd
    from engine.runtime_ui import discover_ui
    from engine.semantic_enrich import enrich
    from engine.tp_expand import expand_all

    reg.register(
        Stage(
            name="extract",
            kind=StageKind.CAPABILITY,
            fn=extract_functional_points,
            input_type=ExtractStageInput,
            output_type=ExtractStageOutput,
            description="扫描文件集 -> 功能点（API / 业务 / 页面 / 组件）",
        )
    )
    reg.register(
        Stage(
            name="tp_expand",
            kind=StageKind.CAPABILITY,
            fn=expand_all,
            input_type=TpExpandStageInput,
            output_type=TpExpandStageOutput,
            description="功能点 -> 测试点（四维展开 + 范围过滤 + 增量打标）",
        )
    )
    reg.register(
        Stage(
            name="case_gen",
            kind=StageKind.CAPABILITY,
            fn=generate_cases,
            input_type=CaseGenStageInput,
            output_type=CaseGenStageOutput,
            description="测试点 -> 用例（八要素 + 确定性 + 运行时富化）",
        )
    )
    reg.register(
        Stage(
            name="semantic_enrich",
            kind=StageKind.CAPABILITY,
            fn=enrich,
            input_type=SemanticEnrichStageInput,
            output_type=SemanticEnrichStageOutput,
            description="规则增强 + 可选 LLM 增强 + 防胡说校验",
        )
    )
    reg.register(
        Stage(
            name="expert_review",
            kind=StageKind.CAPABILITY,
            fn=_expert_review_adapter,
            input_type=ExpertReviewStageInput,
            output_type=ExpertReviewStageOutput,
            description="页面运行时渲染反推（PageExpert.review_page 薄封装）",
        )
    )
    reg.register(
        Stage(
            name="llm_design",
            kind=StageKind.CAPABILITY,
            fn=design_cases,
            input_type=LlmDesignStageInput,
            output_type=LlmDesignStageOutput,
            description="功能点 + 测试点 + 可选 PRD -> LLM 补充用例（护栏不臆造）",
        )
    )
    reg.register(
        Stage(
            name="prd_ingest",
            kind=StageKind.CAPABILITY,
            fn=ingest_prd,
            input_type=PrdIngestStageInput,
            output_type=PrdIngestStageOutput,
            description="PRD 文档（Markdown / OpenAPI）-> 结构化需求",
        )
    )
    reg.register(
        Stage(
            name="comparator",
            kind=StageKind.CAPABILITY,
            fn=compare_one,
            input_type=ComparatorStageInput,
            output_type=ComparatorStageOutput,
            description="预期（CaseSpec）vs 实际（ExecutionResult）语义比对（P0-1）",
        )
    )
    reg.register(
        Stage(
            name="runtime_ui",
            kind=StageKind.CAPABILITY,
            fn=discover_ui,
            input_type=RuntimeUiStageInput,
            output_type=RuntimeUiStageOutput,
            description="打开被测环境、登录、遍历路由、抓取元素与控制台错误",
        )
    )
    reg.register(
        Stage(
            name="auth_scan",
            kind=StageKind.CAPABILITY,
            fn=scan_auth,
            input_type=AuthScanStageInput,
            output_type=AuthScanStageOutput,
            description="源码鉴权接线画像（登录 / 授权信号）",
        )
    )
    reg.register(
        Stage(
            name="tag",
            kind=StageKind.CAPABILITY,
            fn=build_context,
            input_type=TagStageInput,
            output_type=TagStageOutput,
            description="git diff -> 变更上下文（全量 / 增量基线校验 + 逃逸阻断）",
        )
    )

    # ---- 适配层（6，P1-1 填充实现） ----
    _adaptation = [
        ("pull", "拉取代码仓库 / 更新 / 加深（防误伤父仓库安全网）"),
        ("resolve_sources", "解析 code / url / code+url 三态来源"),
        ("register", "登记项目与运行上下文（写入 projects 表）"),
        ("scan", "目录 -> 文件集合（AST 只解析一次）"),
        ("partition_channels", "按 FP 来源拆分 code / url 双通道"),
        ("persist", "幂等落库（cases / runs / run_batches）"),
    ]
    for _name, _desc in _adaptation:
        reg.register(
            Stage(
                name=_name,
                kind=StageKind.ADAPTATION,
                fn=None,
                planned=True,
                input_type=None,
                output_type=None,
                description=_desc,
            )
        )

    return reg


REGISTRY: StageRegistry = _build_registry()


# ---------------------------------------------------------------------------
# 便捷 API
# ---------------------------------------------------------------------------


def get_stage(name: str) -> Stage:
    return REGISTRY.get(name)


def register_stage(stage: Stage) -> Stage:
    return REGISTRY.register(stage)


def list_stages(kind: StageKind | None = None) -> list[Stage]:
    return REGISTRY.list_stages(kind)


def capability_stage_names() -> list[str]:
    return REGISTRY.names(StageKind.CAPABILITY)


def adaptation_stage_names() -> list[str]:
    return REGISTRY.names(StageKind.ADAPTATION)


__all__ = [
    "REGISTRY",
    "AuthScanStageInput",
    "AuthScanStageOutput",
    "CaseGenStageInput",
    "CaseGenStageOutput",
    "ComparatorStageInput",
    "ComparatorStageOutput",
    "ExpertReviewStageInput",
    "ExpertReviewStageOutput",
    "ExtractStageInput",
    "ExtractStageOutput",
    "LlmDesignStageInput",
    "LlmDesignStageOutput",
    "PrdIngestStageInput",
    "PrdIngestStageOutput",
    "RuntimeUiStageInput",
    "RuntimeUiStageOutput",
    "SemanticEnrichStageInput",
    "SemanticEnrichStageOutput",
    "Stage",
    "StageKind",
    "StageRegistry",
    "TagStageInput",
    "TagStageOutput",
    "TpExpandStageInput",
    "TpExpandStageOutput",
    "adaptation_stage_names",
    "capability_stage_names",
    "get_stage",
    "list_stages",
    "register_stage",
]
