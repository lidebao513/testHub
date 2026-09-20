"""引擎 · 模块六：用例生成（case_gen）。

测试点 → 用例，契约 v1.0 的八要素在此落地：
  编号(tc_no) / 模块(module) / 标题(title) / 类型(case_type) /
  优先级(priority) / 前置(precondition) / 步骤(doc_steps) / 预期(steps[0].expect)

**确定性为主**：不依赖 LLM；同一输入重复生成结果完全一致（配合 store 侧幂等对账）。
**机器步只在 steps[0]**：执行器只读第一步，其余人类可读步骤写入 `doc_steps`。

A2 富化（运行时细节落进正文）
------------------------------
拿到 `runtime_index`（`路径 → RuntimePageInfo`）时，UI 层用例的 `doc_steps` 会用
**真实页面地址 / 真实可交互元素 / 控制台错误基线**替换通用模板，并在 `steps[0].runtime`
挂上机器可读的细节（契约 Additive：只增字段，旧消费方忽略即可）。
未提供 `runtime_index` 时行为与从前**逐字一致**，不影响「代码静态分析」通道。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.contracts import (
    CaseSpec,
    TestPoint,
    precondition_of,
    priority_of,
    verify_layer_of_ftype,
)
from core.enums import (
    COVERAGE_ROLE_PRIMARY,
    COVERAGE_ROLE_SUPPLEMENT,
    LAYER_STRATEGY_UI_FIRST,
    AuthMode,
    Dimension,
    FType,
    TPType,
    VerifyLayer,
    method_kind,
)
from engine import ui_executor
from engine.runtime_ui import RuntimePageInfo


# 机器步里最多携带的可见元素数（防单条用例的 payload 过大）
MAX_RUNTIME_ELEMENTS_IN_STEP = 20


def _get(tp: Any, key: str, default: Any = "") -> Any:
    value = tp.get(key, default) if isinstance(tp, Mapping) else getattr(tp, key, default)
    return default if value is None else value


def _method_kind(method: str) -> tuple[bool, str]:
    """返回 (是否接口类, kind)。

    kind 一律是 FType 取值（`api`/`page`/`ui`/`business`），**不留空串**：
    早期实现给 HTTP 路由返回 `""`，导致下游按 `steps[0].kind` 分组时
    整类 HTTP 接口用例被漏掉（空值既不等于 api 也不等于 business）。
    """
    kind = method_kind(method)
    return kind == FType.API.value, kind


def _runtime_steps(
    kind: str, area: str, tail: str, info: RuntimePageInfo
) -> tuple[str, str, str] | None:
    """A2：有运行时细节时，用真实页面 / 元素 / 控制台基线替换通用模板。

    返回 (准备, 执行, 断言) 三段文案；非 UI 面（接口 / 业务函数）返回 None，
    由调用方回退到通用模板——接口层用例没有「页面元素」语义，硬套会误导执行人员。
    """
    if kind == FType.PAGE.value:
        title = f"（页面标题：{info.title}）" if info.title else ""
        phrases = info.element_phrases()
        visible_desc = f"关键元素可见：{'、'.join(phrases)}" if phrases else "页面结构完整"
        prepare = f"用浏览器打开被测环境地址 {info.url}{title}{tail}"
        execute = f"访问 {area}，等待首屏渲染完成，并收集控制台输出"
        baseline = info.console_errors
        if baseline:
            assert_desc = (
                f"校验：页面正常渲染（无白屏）；控制台错误不超过基线 {len(baseline)} 条"
                f"（基线示例：{baseline[0][:60]}）；{visible_desc}"
            )
        else:
            assert_desc = f"校验：页面正常渲染（无白屏、无控制台报错）；{visible_desc}"
        return prepare, execute, assert_desc
    if kind == FType.UI.value:
        phrases = info.element_phrases()
        target = "、".join(phrases) if phrases else "该页面的各交互元素"
        prepare = f"打开 {info.url}，定位交互点：{target}{tail}"
        execute = f"依次触发 {target} 的交互（点击／输入），观察界面响应"
        assert_desc = "校验：交互后界面按预期变化，且不新增控制台错误"
        return prepare, execute, assert_desc
    return None


def build_doc_steps(
    tp: Any,
    precondition: str,
    layer: str,
    runtime_info: RuntimePageInfo | None = None,
    auth_mode: str = "",
) -> list[dict[str, Any]]:
    """人类可读四步：前置 → 准备 → 执行 → 断言。

    文案必须与**执行层**一致：页面/组件不是「请求」，业务函数也不是「请求」，
    早期实现一律写成「构造请求：PAGE /」「发送 FUNC main 请求」，并统一断言
    「响应状态码符合预期」——对 UI 层与函数层属**语义错误**，人工审核与执行
    人员按此理解会走错通道（实测影响 993/1715 条用例）。

    `runtime_info` 非空（该测试点来源是运行时 UI 发现的页面）时，走 A2 富化。
    `auth_mode`（F8）决定安全维度断言文案，与执行器 `_judge` 的判定口径一致。
    """
    method = _get(tp, "method")
    area = _get(tp, "area")
    source = str(_get(tp, "source")).replace("\\", "/")
    category = _get(tp, "category", TPType.NORMAL.value)
    expect = _get(tp, "expect")
    dimension = _get(tp, "dimension", "")
    tail = f"，来源文件 {source}" if source else ""
    kind = method_kind(str(method))
    is_ui = layer == VerifyLayer.UI.value

    override = _runtime_steps(kind, area, tail, runtime_info) if runtime_info else None
    if override is not None:
        prepare, execute, assert_desc = override
    elif is_ui and kind == FType.PAGE.value:
        prepare = f"打开页面：{area}{tail}"
        execute = f"访问 {area}，等待首屏渲染完成并收集控制台错误"
    elif is_ui:
        prepare = f"定位交互点：{area}{tail}"
        execute = f"触发 {area} 的交互（点击／输入），观察界面响应"
    elif kind == FType.API.value:
        prepare = f"构造请求：{method} {area}{tail}"
        execute = f"发送 {method} {area} 请求并捕获响应"
    else:
        prepare = f"准备调用上下文：{area}{tail}"
        execute = f"调用函数 {area}，捕获返回值与异常"

    if override is None:
        assert_desc = _assert_desc(category, kind, auth_mode, dimension)

    return [
        {"seq": 1, "type": "前置", "desc": precondition},
        {"seq": 2, "type": "准备", "desc": prepare},
        {"seq": 3, "type": "执行", "desc": execute},
        {"seq": 4, "type": "断言", "desc": assert_desc, "expect": expect},
    ]


def _security_assert(auth_mode: str, dimension: str = "") -> str:
    """安全维度的断言文案（F8）：与 `tp_expand._expect_of` 同一口径。

    三处（测试点期望 / 用例断言 / 执行器判定）必须一致，否则会出现
    「用例写期望 401、执行器按『公开可访问』判通过」的自相矛盾。
    """
    if dimension == Dimension.TOKEN_EXPIRED.value:
        return "校验：使用过期/失效令牌访问被拒（401/403）；持有效令牌刷新后恢复原访问能力"
    if auth_mode == AuthMode.ABSENT.value:
        return (
            "校验：该接口未检测到鉴权接线（公开接口）→ 可正常访问且不泄露敏感字段；"
            "如需鉴权应在代码中接线（engine/auth_scan.py）"
        )
    if auth_mode == AuthMode.OPTIONAL.value:
        return (
            "校验：鉴权接线为占位实现（未配置令牌即放行）→ 未配置时属未鉴权暴露；"
            "配置令牌后无凭证/越权访问应被拒（401/403），且不泄露资源内容"
        )
    return "校验：无凭证/越权访问被拒绝（401/403），且不泄露资源内容"


# G-5：UI 异常流的断言文案（与 tp_expand._UI_ABNORMAL_EXPECT 同一口径；
# 这些场景多需人工/专项触发，用例描述「出错时应优雅」，执行器对可观测部分做最佳努力判定）
_UI_ABNORMAL_ASSERT: dict[str, str] = {
    "异常-网络中断": "校验：断网/弱网刷新或操作后页面不白屏、不抛未捕获异常，有重试或友好提示",
    "异常-错误回显": "校验：提交非法数据或后端报错时回显友好提示，不展示原始堆栈/敏感内部信息",
    "异常-空状态": "校验：无数据（空列表/空结果）时渲染空状态提示，不崩溃、不白屏",
    "异常-错误页": "校验：后端 5xx 时前端展示错误页/降级提示而非白屏，不影响其他功能入口",
}


def _assert_desc(category: str, kind: str, auth_mode: str = "", dimension: str = "") -> str:
    """通用断言文案（无运行时细节时的回退）。"""
    if category == TPType.ABNORMAL.value:
        return _abnormal_assert(dimension, kind)
    if category == TPType.SECURITY.value:
        return _security_assert(auth_mode, dimension)
    return _normal_assert(kind)


def _abnormal_assert(dimension: str, kind: str) -> str:
    if dimension in _UI_ABNORMAL_ASSERT:
        return _UI_ABNORMAL_ASSERT[dimension]
    if kind == FType.API.value:
        return "校验：接口对非法/边界输入返回预期错误（4xx/5xx），且不产生未捕获异常或数据损坏"
    return "校验：函数对非法输入抛出预期异常或返回错误码，且不产生未捕获异常或数据损坏"


def _normal_assert(kind: str) -> str:
    if kind == FType.API.value:
        return "校验：响应状态码符合预期，关键业务字段完整"
    if kind == FType.PAGE.value:
        return "校验：页面正常渲染（无白屏／无控制台报错），关键元素可见"
    if kind == FType.UI.value:
        return "校验：交互后界面按预期变化，无报错提示或状态残留"
    return "校验：返回值符合语义，异常分支被正确捕获"


def design_assertions(info: RuntimePageInfo | None) -> list[dict[str, Any]]:
    """F9：把 A2 收进用例的 `selector` 变成**可判定断言**，而不是「仅作参考」。

    产出的是机器可读断言清单，由 `engine/ui_executor.py` 逐条执行：
      1. `page_rendered`：页面渲染出正文（白屏即失败）；
      2. `console_within_baseline`：控制台错误不超过运行时基线；
      3. `element_visible`：每个真实元素按 selector 断言「命中且可见」。

    无运行时细节（非 UI 面 / 未跑运行时发现）时返回空列表——执行器会退化为
    「页面可达 + 控制台基线」两条，绝不凭空编造元素断言。
    """
    if info is None:
        return []
    specs: list[dict[str, Any]] = [
        {"kind": ui_executor.ASSERT_PAGE_RENDERED},
        {
            "kind": ui_executor.ASSERT_CONSOLE_WITHIN_BASELINE,
            "baseline": len(info.console_errors),
        },
    ]
    for element in info.visible_elements()[:MAX_RUNTIME_ELEMENTS_IN_STEP]:
        selector = str(element.get("selector") or "")
        if selector:
            specs.append(
                {
                    "kind": ui_executor.ASSERT_ELEMENT_VISIBLE,
                    "selector": selector,
                    "text": str(element.get("text") or ""),
                }
            )
    return specs


def _runtime_payload(info: RuntimePageInfo) -> dict[str, Any]:
    """机器步携带的运行时细节（供 UI 层执行器使用；**不含凭证**）。"""
    return {
        "url": info.url,
        "path": info.path,
        "title": info.title,
        "elements": [
            {
                "kind": str(e.get("kind") or ""),
                "text": str(e.get("text") or ""),
                "selector": str(e.get("selector") or ""),
            }
            for e in info.visible_elements()[:MAX_RUNTIME_ELEMENTS_IN_STEP]
        ],
        "console_error_baseline": len(info.console_errors),
    }


def build_case(  # noqa: PLR0913 - 生成选项本就多，显式关键字参数比选项对象更直观
    tp: Any,
    fp_row_id: int | None = None,
    *,
    ui_modules: set[str] | None = None,
    strategy: str = LAYER_STRATEGY_UI_FIRST,
    drop_supplement: bool = False,
    runtime_index: Mapping[str, RuntimePageInfo] | None = None,
    auth_profile: Any = None,
) -> CaseSpec | None:
    """由一条测试点构建用例规格。

    覆盖角色（UI 优先策略）：
    - UI 层用例恒为 `primary`（UI 是优先验证层）；
    - 接口层用例：在 `ui_first` 策略下，若其所属模块已有 UI 覆盖 → `supplement`
      （接口仅作补充），否则仍为 `primary`（该后端能力 UI 无法覆盖，接口是必要的）；
    - `drop_supplement=True` 时，补充用例直接返回 None（被上层过滤丢弃）。

    `runtime_index`（A2）：运行时发现的「页面路径 → 细节」；命中时富化 `doc_steps`
    并在 `steps[0].runtime` 挂机器可读细节，同时把元素 `selector` 变成可判定断言（F9）。
    `auth_profile`（F8）：鉴权接线画像；安全用例的期望与判定口径由它决定
    （`steps[0].auth_mode`），不再写死 401/403。
    """
    category = _get(tp, "category", TPType.NORMAL.value)
    method = str(_get(tp, "method"))
    area = str(_get(tp, "area"))
    source = str(_get(tp, "source")).replace("\\", "/")
    tp_id = str(_get(tp, "tp_id"))
    fp_contract_id = str(_get(tp, "fp_contract_id"))
    expect = str(_get(tp, "expect"))
    title_text = str(_get(tp, "title")) or tp_id
    # 测试点标题通常已自带 `[维度]` 前缀，此处只在缺失时补，避免出现 `[安全] [安全] …`
    prefix = f"[{category}]"
    title = title_text if title_text.startswith(prefix) else f"{prefix} {title_text}"

    is_api, kind = _method_kind(method)
    ctype = FType.API.value if is_api else "e2e"
    # 执行层（接口 / UI）以测试点自带的 verify_layer 为准；缺失时按 method 标记回退推导。
    # 不能再用「是否 HTTP 动词」当判据：后端业务函数（method=FUNC）不是 HTTP 接口，
    # 但它属于**接口层**，早期实现会把它派成 ui_probe → 执行器去拉浏览器打开一个函数。
    layer = str(_get(tp, "verify_layer")) or verify_layer_of_ftype(kind)
    is_ui_layer = layer == VerifyLayer.UI.value
    module = str(_get(tp, "module"))

    # —— 覆盖角色 ——
    if is_ui_layer:
        coverage_role = COVERAGE_ROLE_PRIMARY
    elif strategy == LAYER_STRATEGY_UI_FIRST and module in (ui_modules or set()):
        coverage_role = COVERAGE_ROLE_SUPPLEMENT
    else:
        coverage_role = COVERAGE_ROLE_PRIMARY
    if drop_supplement and coverage_role == COVERAGE_ROLE_SUPPLEMENT:
        return None

    precondition = precondition_of(category, layer)
    runtime_info = (runtime_index or {}).get(area)
    auth_mode = (
        auth_profile.mode_for(source) if auth_profile is not None else AuthMode.REQUIRED.value
    )

    step0: dict[str, Any] = {
        "action": "ui_probe" if is_ui_layer else "http_probe",
        "layer": layer,  # 契约「只增不破」：新增可选字段，供下游按执行层分组
        "kind": kind,
        "tp_id": tp_id,
        "fp_id": fp_contract_id,
        "source": source,
        "file": source,  # 兼容字段：与 source 同值，供旧消费方读取
        "method": method,
        "path": area,
        "func": area if kind == FType.BUSINESS.value else None,
        "dimension": _get(tp, "dimension"),
        "expect": expect,
        "coverage_role": coverage_role,  # 契约 Additive：UI 优先覆盖角色
        "auth_mode": auth_mode,  # 契约 Additive（F8）：安全维度判定口径
        # G-3：资源归属（供执行器产出高置信越权结论）；契约 Additive，缺省即「未识别」
        "resource": str(_get(tp, "resource", "")),
        "owner_scoped": bool(_get(tp, "owner_scoped", False)),
    }
    if runtime_info is not None:
        # 契约 Additive：运行时发现细节，供 UI 层执行器与人工复核使用（不含凭证）
        step0["runtime"] = _runtime_payload(runtime_info)
        # F9：把 selector 变成可判定断言（不再只是「参考」）
        step0["assertions"] = design_assertions(runtime_info)
    steps = [step0]

    return CaseSpec(
        tc_no=tp_id,
        title=title,
        ctype=ctype,
        steps=steps,
        module=module,
        case_type=str(category),
        priority=priority_of(str(category), module),
        precondition=precondition,
        doc_steps=build_doc_steps(tp, precondition, layer, runtime_info, auth_mode),
        tp_id=tp_id,
        fp_contract_id=fp_contract_id,
        fp_row_id=fp_row_id,
        test_type=str(_get(tp, "tag", "全量")),
        coverage_role=coverage_role,
    )


def generate_cases(  # noqa: PLR0913 - 生成选项本就多，显式关键字参数比选项对象更直观
    tps: list[Any],
    fp_row_map: dict[str, int] | None = None,
    *,
    ui_modules: set[str] | None = None,
    strategy: str = LAYER_STRATEGY_UI_FIRST,
    drop_supplement: bool = False,
    runtime_index: Mapping[str, RuntimePageInfo] | None = None,
    auth_profile: Any = None,
) -> list[CaseSpec]:
    """批量生成：测试点 → 用例，1:1 对应（契约不变）。

    `ui_modules` 为「已有 UI 覆盖的模块集合」，用于判定接口层用例是否仅为补充。
    `drop_supplement=True` 时过滤掉所有补充用例（纯 UI 优先视图）。
    `runtime_index` 见 `build_case`（A2 运行时细节富化 + F9 元素断言）。
    `auth_profile` 见 `build_case`（F8 鉴权口径）。
    """
    fp_row_map = fp_row_map or {}
    out: list[CaseSpec] = []
    for tp in tps:
        spec = build_case(
            tp,
            fp_row_map.get(str(_get(tp, "fp_contract_id"))),
            ui_modules=ui_modules,
            strategy=strategy,
            drop_supplement=drop_supplement,
            runtime_index=runtime_index,
            auth_profile=auth_profile,
        )
        if spec is not None:
            out.append(spec)
    return out


def from_test_point(tp: TestPoint, fp_row_id: int | None = None) -> CaseSpec | None:
    return build_case(tp, fp_row_id)


def coverage_of(cases: list[CaseSpec], tps: list[Any]) -> dict[str, Any]:
    """用例 / 测试点覆盖体检：孤儿为 0 才算达标。"""
    tp_ids = {str(_get(t, "tp_id")) for t in tps}
    case_tps = {c.tp_id for c in cases}
    orphan = sorted(case_tps - tp_ids)
    uncovered = sorted(tp_ids - case_tps)
    return {
        "tp_count": len(tp_ids),
        "case_count": len(cases),
        "orphan_cases": orphan,
        "orphan_count": len(orphan),
        "uncovered_tp": uncovered,
        "uncovered_count": len(uncovered),
    }
