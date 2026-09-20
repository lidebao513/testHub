"""专家系统 · URL 通道专家 PageExpert（engine/expert/page_expert）。

职能（与代码通道 CodeExpert 异构）：审视页面**运行时渲染**——截图 / DOM / 元素 / XHR / 路由，
从测试专家视角产出「专家增补测试点」（origin=expert_page）。

全环节 LLM（D4 默认单轮 LLM · 低成本）：
- **S0 `propose_routes`（治本）**：读截图/DOM，列出规则 CSS 选择器漏掉的菜单/导航项文本，
  回灌 runtime_ui 的菜单点击遍历（修复 `routes=0 → 真实菜单数` 的根因）。
- **S1–S6 `review_page`（提质）**：综合产出用户旅程 / 业务规则 / 边界 / 安全 / 异常 / 汇总
  视角的测试点，每条 `area` 必命中真实元素/路由/XHR（不臆造护栏）。

红线（继承 F10b）：
- **不臆造**：`area` 必须命中真实锚点（elements / routes / xhr），否则进 rejected 并公示；
- **不静默**：未启用 / 模型缺失 / 视觉模型未配 → 空 added + notes；
- **Additive**：专家用例只增不加，绝不删规则基线。

只依赖 core（契约/枚举/日志）+ 同层 llm/base，不感知 service / cli。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from core.contracts import ORIGIN_EXPERT_PAGE
from core.enums import TPType, VerifyLayer
from core.log import get_logger
from engine.expert.base import ExpertResult, build_anchor_pool, validate_area_hits
from engine.expert.llm import ExpertLLMClient, ExpertLLMOptions


log = get_logger(__name__)

_VALID_CATEGORIES = {t.value for t in TPType}
_VALID_LAYERS = {VerifyLayer.UI.value, VerifyLayer.INTERFACE.value}

# 单页专家测试点上限（防爆 + 控成本）
DEFAULT_MAX_TPS_PER_PAGE = 8


@dataclass
class PageCapture:
    """一页运行时快照（PageExpert 的输入；复用 runtime_ui 字段 + 截图）。

    纯数据载体，不依赖 runtime_ui 类型，保持本模块与探索器解耦。
    """

    url: str = ""
    path: str = ""
    title: str = ""
    screenshot_b64: str | None = None  # 多模态输入（D1）；纯文本模型自动降级 DOM
    dom_text: str = ""
    elements: list[dict[str, Any]] = field(default_factory=list)  # [{selector,kind,text,visible}]
    console_errors: list[str] = field(default_factory=list)
    xhr_list: list[dict[str, Any]] = field(default_factory=list)  # [{method,path}]
    auth_mode: str = "required"
    discovered_routes: list[str] = field(default_factory=list)


@dataclass
class ExpertTestPoint:
    """PageExpert 产出的单条专家测试点（尚未绑定 fp_contract_id）。

    `area` 必须命中真实锚点（已通过护栏校验）；`fp_contract_id` 由 pipeline 在解析阶段填入。
    """

    area: str
    category: str
    dimension: str
    title: str
    expect: str
    priority: str = "P2"
    verify_layer: str = VerifyLayer.UI.value
    rationale: str = ""
    confidence: float = 0.6
    module: str = ""


@dataclass
class PageExpertOptions:
    """PageExpert 选项。"""

    enabled: bool = True  # D3：默认开
    provider: str = "deepseek"
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    timeout: int = 300
    use_vision: bool = True  # D1：多模态；模型非视觉时自动降级 DOM 并 notes
    vision_model: str = ""
    # 降级链：模型不可用（如免费额度 AllocationQuota.FreeTierOnly.）时按顺序切换的备选模型。
    model_chain: list[str] = field(default_factory=list)
    max_tps_per_page: int = DEFAULT_MAX_TPS_PER_PAGE


@dataclass
class PageExpertResult(ExpertResult):
    """PageExpert 单页结果。"""

    added: list[ExpertTestPoint] = field(default_factory=list)
    used_vision: bool = False
    vision_note: str = ""


# ============================================================================
# S0：治本 —— 反推菜单驱动全站遍历
# ============================================================================
_S0_SYSTEM = (
    "你是资深测试专家，审视一张 Web 应用页面的渲染（截图/DOM）。"
    "请列出**左侧/顶部导航菜单、侧边栏、顶部标签栏里出现的、可点击进入子页面的菜单项文本**。\n"
    "目标：找出规则选择器（nav a / [role=menuitem] / .menu-item 等）**可能漏掉**的导航入口，"
    "帮助自动化工具点进更多真实页面。只列你**确实在页面上看到**的菜单文字，不要推测。"
)

_S0_SCHEMA = (
    '只输出 JSON 数组，元素是字符串（菜单项文字），例如 ["工作台","任务管理","报表中心"]。'
    "没有额外说明文字。"
)


def build_s0_prompt(capture: PageCapture) -> str:
    """构造 S0 提示：给模型页面可见文本与元素，请它列菜单项。"""
    parts: list[str] = []
    if capture.title:
        parts.append(f"页面标题：{capture.title}")
    if capture.path:
        parts.append(f"当前路由：{capture.path}")
    texts = [
        str(e.get("text") or "").strip() for e in capture.elements if (e.get("text") or "").strip()
    ]
    if texts:
        parts.append(
            "页面可见元素文字（含导航/按钮，供你判断有哪些菜单）：\n" + "\n".join(texts[:60])
        )
    if capture.dom_text:
        dom = capture.dom_text.strip()
        if dom:
            parts.append("页面 DOM 文本（节选）：\n" + dom[:1500])
    parts.append(_S0_SCHEMA)
    return "\n\n".join(parts)


def parse_route_labels(raw: str | list[Any]) -> list[str]:
    """解析 S0 模型输出为菜单文字列表（容错）。"""
    if isinstance(raw, list):
        items = raw
    else:
        text = (raw or "").strip()
        if text.startswith("```"):
            import re

            text = re.sub(r"^```[a-zA-Z]*\n?|```$", "", text).strip()
        try:
            items = json.loads(text)
        except ValueError:
            return []
    if not isinstance(items, list):
        return []
    out: list[str] = []
    for it in items:
        s = str(it).strip()
        if s and s not in out:
            out.append(s)
    return out


# ============================================================================
# S1–S6：提质 —— 旅程/规则/边界/安全/异常/汇总
# ============================================================================
_REVIEW_SYSTEM = (
    "你是资深软件测试专家，审视一张 Web 应用页面的**运行时渲染**（截图/DOM/元素/接口）。\n"
    "请从测试专家视角，设计**补充测试点**（规则引擎还没覆盖的视角），覆盖以下环节：\n"
    "S1 核心业务对象与用户目标；S2 用户主流程 E2E 旅程；S3 业务规则组合（权限矩阵/状态机/依赖顺序）；\n"
    "S4 真实边界（页面临界值/空态/超长/移动视口）；S5 安全直觉（未授权直达/输入注入/越权读他人数据）；\n"
    "S6 异常流（提交失败/服务报错/空数据）。\n"
    "每条必须声明一个真实存在的 `area`（页面路径 / 元素选择器 / 接口路径 / 已发现路由），不得臆造。"
)

_REVIEW_SCHEMA = (
    "只输出 JSON 数组，每个元素："
    '{"area":"真实锚点","category":"正常|异常|安全|边界","dimension":"子维度","title":"用例标题",'
    '"expect":"预期结果","priority":"P1|P2","verify_layer":"UI|接口","rationale":"为何这样测"}'
)


def build_review_prompt(capture: PageCapture) -> str:
    """构造 S1–S6 提示：把页面真实事实喂给模型，请其产出补充测试点。"""
    parts: list[str] = []
    if capture.title:
        parts.append(f"页面标题：{capture.title}")
    if capture.path:
        parts.append(f"当前路由：{capture.path}")
    if capture.discovered_routes:
        parts.append("本应用已发现路由：" + "、".join(capture.discovered_routes[:40]))
    selectors = [str(e.get("selector") or "") for e in capture.elements if e.get("selector")]
    if selectors:
        parts.append("本页真实元素选择器（area 只能引用这些）：\n" + "\n".join(selectors[:60]))
    xhr = [f"{x.get('method', '')} {x.get('path', '')}" for x in capture.xhr_list if x.get("path")]
    if xhr:
        parts.append("本页运行时拦截到的接口（可作为 area）：\n" + "\n".join(xhr[:40]))
    if capture.dom_text:
        dom = capture.dom_text.strip()
        if dom:
            parts.append("页面 DOM 文本（节选）：\n" + dom[:2000])
    parts.append(_REVIEW_SCHEMA)
    return "\n\n".join(parts)


def parse_expert_tps(raw: list[dict[str, Any]]) -> list[ExpertTestPoint]:
    """把模型返回的字典列表解析为 ExpertTestPoint（仅做基本字段清洗，护栏在 review_page 内做）。"""
    out: list[ExpertTestPoint] = []
    for cand in raw:
        if not isinstance(cand, dict):
            continue
        area = str(cand.get("area") or "").strip()
        title = str(cand.get("title") or "").strip()
        if not area or not title:
            continue
        category = str(cand.get("category") or TPType.NORMAL.value).strip()
        if category not in _VALID_CATEGORIES:
            category = TPType.NORMAL.value
        layer = str(cand.get("verify_layer") or VerifyLayer.UI.value).strip()
        if layer not in _VALID_LAYERS:
            layer = VerifyLayer.UI.value
        priority = str(cand.get("priority") or "P2").strip()
        if priority not in ("P1", "P2"):
            priority = "P2"
        out.append(
            ExpertTestPoint(
                area=area,
                category=category,
                dimension=str(cand.get("dimension") or ""),
                title=title,
                expect=str(cand.get("expect") or "").strip(),
                priority=priority,
                verify_layer=layer,
                rationale=str(cand.get("rationale") or "").strip(),
                confidence=float(cand.get("confidence") or 0.6),
            )
        )
    return out


# ============================================================================
# PageExpert 主体
# ============================================================================
class PageExpert:
    """URL 通道测试专家（多模态截图 + 全环节 LLM）。"""

    def __init__(
        self,
        options: PageExpertOptions | None = None,
        *,
        client_factory: Callable[[ExpertLLMOptions], Any] | None = None,
    ) -> None:
        self.options = options or PageExpertOptions()
        self._client_factory = client_factory or ExpertLLMClient

    def _client(self) -> ExpertLLMClient:
        o = self.options
        llm_opts = ExpertLLMOptions(
            enabled=o.enabled,
            provider=o.provider,
            base_url=o.base_url,
            model=o.model,
            api_key=o.api_key,
            timeout=o.timeout,
            use_vision=o.use_vision,
            vision_model=o.vision_model,
            model_chain=o.model_chain,
        )
        return self._client_factory(llm_opts)

    # ---- S0：治本 ----
    def propose_routes(self, capture: PageCapture) -> tuple[list[str], dict[str, Any]]:
        """S0：反推菜单项文本（治本），返回 (菜单文字列表, 元数据)。

        模型缺失 / 未启用 → 返回 ([], {note})（不静默）；纯文本模型自动降级 DOM。
        """
        if not self.options.enabled:
            return [], {"note": "PageExpert 未启用（expert_mode=off），S0 跳过"}
        client = self._client()
        if not client.available():
            return [], {
                "note": "专家 LLM 未配置（需 enabled+api_key+base_url+model），S0 未产出菜单"
            }
        try:
            items, meta = client.complete_json(
                build_s0_prompt(capture),
                system_prompt=_S0_SYSTEM,
                images=[capture.screenshot_b64] if capture.screenshot_b64 else None,
            )
            labels = parse_route_labels(items)
        except Exception as exc:  # noqa: BLE001 - 任一失败都不应阻断探索主链路
            return [], {"note": f"S0 菜单反推失败，已降级：{exc}"}
        return labels, {
            "used_vision": meta.get("used_vision", False),
            "vision_note": meta.get("vision_note", ""),
            "count": len(labels),
        }

    # ---- S1–S6：提质 ----
    def review_page(self, capture: PageCapture) -> PageExpertResult:
        """S1–S6：综合产出专家增补测试点；每条 area 经护栏校验，未命中真实锚点的进 rejected。"""
        result = PageExpertResult()
        if not self.options.enabled:
            result.add_note("PageExpert 未启用（expert_mode=off），未新增任何专家测试点")
            return result
        client = self._client()
        if not client.available():
            result.add_note(
                "专家 LLM 未配置（需 enabled+api_key+base_url+model），未新增任何专家测试点"
            )
            return result

        # 真实锚点池：元素选择器 + 已发现路由 + 接口路径 + 本页路径
        anchors = build_anchor_pool(
            element_selectors=[str(e.get("selector") or "") for e in capture.elements],
            routes=[*capture.discovered_routes, capture.path]
            if capture.path
            else capture.discovered_routes,
            xhr_paths=[str(x.get("path") or "") for x in capture.xhr_list],
        )
        try:
            items, meta = client.complete_json(
                build_review_prompt(capture),
                system_prompt=_REVIEW_SYSTEM,
                images=[capture.screenshot_b64] if capture.screenshot_b64 else None,
            )
            result.used_vision = meta.get("used_vision", False)
            if meta.get("vision_note"):
                result.vision_note = meta["vision_note"]
                result.add_note(meta["vision_note"])
        except Exception as exc:  # noqa: BLE001 - LLM 失败降级为纯规则，不阻断主链路
            result.add_note(f"专家模型调用失败，已降级为规则用例：{exc}")
            return result

        candidates = parse_expert_tps(items)
        added: list[ExpertTestPoint] = []
        for cand in candidates:
            reason = validate_area_hits(cand.area, anchors)
            if reason is not None:
                result.reject(cand.__dict__, reason)
                continue
            added.append(cand)
            if len(added) >= self.options.max_tps_per_page:
                break
        result.added = added
        # 覆盖缺口（诚实披露）：模型本次未覆盖的维度提示
        covered = {c.category for c in added}
        missing_dims = [c.value for c in TPType if c.value not in covered]
        if missing_dims:
            result.coverage_gaps.append("本页专家未覆盖维度：" + "、".join(missing_dims))
        result.add_note(
            f"专家候选 {len(candidates)} 条 → 通过 {len(added)} 条、拒绝 {len(result.rejected)} 条"
        )
        return result


def expert_to_test_point(
    etp: ExpertTestPoint,
    fp_id: str,
    fp_name: str,
    module: str,
) -> Any:
    """把通过护栏的专家测试点转换为契约 TestPoint（绑定 fp_contract_id）。

    由 pipeline 在解析 area→fp 后调用；fp_id 来自真实功能点，保证溯源不孤儿。
    """
    from core.contracts import TestPoint, tp_id_of

    return TestPoint(
        tp_id=tp_id_of(
            fp_id,
            etp.category,
            area=etp.area,
            method="EXPERT",
            dimension=etp.dimension or "expert",
        ),
        fp_contract_id=fp_id,
        category=etp.category,
        module=module or etp.module,
        title=f"[专家·{etp.category}] {etp.title}",
        semantic=etp.rationale,
        source=fp_name,
        method="EXPERT",
        area=etp.area,
        expect=etp.expect,
        dimension=etp.dimension or "expert",
        tag="全量",
        review_status="pending",
        verify_layer=etp.verify_layer,
        evidence=[f"page:{etp.area}"],
        confidence=etp.confidence,
        origin=ORIGIN_EXPERT_PAGE,
        unverified=True,  # 专家用例标识为未经验证，待人工审核
        expert="PageExpert",
    )
