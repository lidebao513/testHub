"""引擎 · 模块三：测试点展开（tp_expand）。

把每条功能点按「行为维度」展开成若干测试点：
  正常（功能可用性） / 异常（资源或状态异常） / 安全（鉴权与越权） / 边界（参数边界）

与 legacy 的差异（有意修正）：
- `tp_id` 由「枚举序号」改为**内容指纹**（契约缺陷 D-2），同一份代码重复跑编号不变；
- 展开规则集中在 `dimensions_of()`，一眼可见，不再散落在 1500 行里。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from core.contracts import FunctionalPoint, TestPoint, tp_id_of, verify_layer_of_ftype
from core.enums import DEFAULT_SCOPE, AuthMode, Dimension, FType, MethodMarker, Tag, TPType
from engine import auth_scan
from engine.fp_extract import expect_of


# 路径参数（如 `/invoices/{invoice_id}`）
_PATH_PARAM_RE = re.compile(r"\{[^}]+\}")

# 「越权」维度只针对**修改他人资源**的操作（与 legacy 一致）
_PRIV_ESC_METHODS: tuple[str, ...] = ("PUT", "PATCH", "DELETE")
# 写操作（会产生真实数据变更）：幂等维度仅对这些有意义
_WRITE_METHODS: tuple[str, ...] = ("POST", "PUT", "PATCH", "DELETE")

# 非 HTTP 来源 → method 字段标记（契约：PAGE/UI/FUNC，见 enums.MethodMarker）
_MARKER_BY_FTYPE: dict[str, str] = {
    FType.PAGE.value: MethodMarker.PAGE.value,
    FType.UI.value: MethodMarker.UI.value,
    FType.COMPONENT.value: MethodMarker.UI.value,
    FType.BUSINESS.value: MethodMarker.FUNC.value,
}

_SEMANTIC_ACTION: dict[str, str] = {
    TPType.NORMAL.value: "验证功能可用性",
    TPType.ABNORMAL.value: "验证异常路径处理",
    TPType.SECURITY.value: "验证鉴权与越权防护",
    TPType.BOUNDARY.value: "验证参数边界与非法输入",
    TPType.PERFORMANCE.value: "验证性能与并发",  # G-8
}

# 维度规范顺序（与 enums.TPType 声明顺序一致）。
# 用途：生成 tp_id 的 ordinal 时取**规范顺序里的固定位置**，而不是循环下标。
# 为什么必须这样：循环下标是**过滤后**的序号，用户这次只要「正常+异常」、
# 下次加上「安全」，同一条『异常』测试点的序号就会从 1 变 2 → tp_id 漂移 →
# 旧用例被判 obsolete、新建一条，人工审核结论与执行历史全部断链（契约缺陷 D-9）。
_CANONICAL_ORDER: tuple[str, ...] = (
    TPType.NORMAL.value,
    TPType.ABNORMAL.value,
    TPType.SECURITY.value,
    TPType.BOUNDARY.value,
    TPType.PERFORMANCE.value,  # G-8：独立行为维度，tp_id 规范顺序固定末位
)

# 「行为维度 × 子维度」在规范顺序中的槽位：
# 「安全」维度下同时存在「鉴权缺失」与「越权」两条，需靠槽位区分（否则 tp_id 撞号）。
_DIMENSION_SLOTS: dict[str, dict[str, int]] = {
    TPType.NORMAL.value: {
        Dimension.AVAIL.value: 0,
        Dimension.BIZ_LOGIC.value: 0,
        Dimension.PAGE_REACH.value: 0,
        Dimension.INTERACTIVE.value: 0,
        # T3 语义维度（正常类）：独立槽位，避免与「可用性/交互元素」撞号
        Dimension.LIST_INSERT_VISIBLE.value: 1,
        Dimension.LIST_DEDUPE.value: 2,
        Dimension.LIST_ORDER_CONSISTENCY.value: 3,
        Dimension.PAGINATION_NO_OVERLAP.value: 4,
        Dimension.BRANCH_ISOLATION.value: 5,
        Dimension.CONTRACT_SHAPE.value: 6,
    },
    TPType.ABNORMAL.value: {
        Dimension.RES_NOT_FOUND.value: 0,
        # G-5：接口异常流（独立槽位，避免与资源不存在撞号）
        Dimension.RATE_LIMIT.value: 1,
        Dimension.IDEMPOTENT.value: 2,
        Dimension.DEGRADED.value: 3,
        Dimension.TIMEOUT.value: 4,
        # G-5：UI 异常流（行为维度归属「异常」，需 DEFAULT_SCOPE 含异常才默认生成）
        Dimension.UI_NETWORK_INTERRUPT.value: 5,
        Dimension.UI_ERROR_DISPLAY.value: 6,
        Dimension.UI_EMPTY_STATE.value: 7,
        Dimension.UI_SERVER_ERROR.value: 8,
        # scope 增强（2026-09-21）：业务函数异常路径维度（独立槽位）
        Dimension.BIZ_ABNORMAL.value: 9,
        # T3 语义维度（异常类）
        Dimension.FALLBACK_DEFAULT.value: 10,
    },
    TPType.SECURITY.value: {
        Dimension.AUTH_MISS.value: 0,
        Dimension.PRIV_ESC.value: 1,
        # #222：运行时 UI 元素级安全子维度（独立槽位，避免与鉴权缺失/越权撞号）
        Dimension.UI_INPUT_INJECT.value: 2,
        Dimension.UI_UNAUTH_PAGE.value: 3,
        # G-3：令牌过期续期（独立槽位）
        Dimension.TOKEN_EXPIRED.value: 4,
        # G-9：多租户数据隔离（资源级隔离，独立槽位，避免与越权撞号）
        Dimension.TENANT_READ.value: 5,
        Dimension.TENANT_WRITE.value: 6,
        # scope 增强（2026-09-21）：业务函数输入校验维度（独立槽位，避免与越权撞号）
        Dimension.BIZ_INPUT_VALID.value: 7,
    },
    TPType.BOUNDARY.value: {
        Dimension.PARAM_ILLEGAL.value: 0,
        # #222：运行时 UI 元素级边界子维度
        Dimension.UI_LONG_INPUT.value: 1,
        Dimension.UI_ILLEGAL_OPTION.value: 2,
        Dimension.UI_REPEAT_CLICK.value: 3,
        Dimension.UI_DEEPLINK.value: 4,
        Dimension.UI_POOR_VIEWPORT.value: 5,
        # scope 增强（2026-09-21）：业务函数参数边界维度（独立槽位，避免与超长输入撞号）
        Dimension.BIZ_BOUNDARY.value: 6,
        # T3 语义维度（边界类）
        Dimension.PAGINATION_OFFSET_SYNC.value: 7,
        Dimension.GUARD_ZERO_VS_EMPTY.value: 8,
    },
    # G-8：性能/并发（独立行为维度「性能」，slot 0=基线 1=并发）
    TPType.PERFORMANCE.value: {
        Dimension.PERF_BASELINE.value: 0,
        Dimension.PERF_CONCURRENCY.value: 1,
    },
}
_ORDINAL_STRIDE = 10

# #222：运行时 UI 元素级功能点 → 按元素 kind 展开「正常 + 安全 + 边界」维度。
# kind 取自功能点 name 的 `#{kind}:` 段（to_functional_points 的 _element_fp_name 约定），
# 无法解析时退回通用计划。语义与 DEFAULT_SCOPE（正常/安全/边界）对齐。
_UI_KIND_RE = re.compile(r"#(\w+):")
_UI_PLAN_BY_KIND: dict[str, list[tuple[str, str]]] = {
    # 数据录入类：正常输入 + 注入安全 + 超长边界
    "input": [
        (TPType.NORMAL.value, Dimension.INTERACTIVE.value),
        (TPType.SECURITY.value, Dimension.UI_INPUT_INJECT.value),
        (TPType.BOUNDARY.value, Dimension.UI_LONG_INPUT.value),
    ],
    "textarea": [
        (TPType.NORMAL.value, Dimension.INTERACTIVE.value),
        (TPType.SECURITY.value, Dimension.UI_INPUT_INJECT.value),
        (TPType.BOUNDARY.value, Dimension.UI_LONG_INPUT.value),
    ],
    "edit": [
        (TPType.NORMAL.value, Dimension.INTERACTIVE.value),
        (TPType.SECURITY.value, Dimension.UI_INPUT_INJECT.value),
        (TPType.BOUNDARY.value, Dimension.UI_LONG_INPUT.value),
    ],
    "form": [
        (TPType.NORMAL.value, Dimension.INTERACTIVE.value),
        (TPType.SECURITY.value, Dimension.UI_INPUT_INJECT.value),
        (TPType.BOUNDARY.value, Dimension.UI_LONG_INPUT.value),
    ],
    # 选择类：正常选择 + 非法选项边界
    "select": [
        (TPType.NORMAL.value, Dimension.INTERACTIVE.value),
        (TPType.BOUNDARY.value, Dimension.UI_ILLEGAL_OPTION.value),
    ],
    # 点击类：正常点击 + 重复点击边界
    "button": [
        (TPType.NORMAL.value, Dimension.INTERACTIVE.value),
        (TPType.BOUNDARY.value, Dimension.UI_REPEAT_CLICK.value),
    ],
    # 导航类：正常跳转 + 深链直达边界
    "nav": [
        (TPType.NORMAL.value, Dimension.INTERACTIVE.value),
        (TPType.BOUNDARY.value, Dimension.UI_DEEPLINK.value),
    ],
}
# 通用回退（kind 无法解析时）：正常 + 超长边界
_UI_PLAN_DEFAULT: list[tuple[str, str]] = [
    (TPType.NORMAL.value, Dimension.INTERACTIVE.value),
    (TPType.BOUNDARY.value, Dimension.UI_LONG_INPUT.value),
]


def _ui_kind_of(fp: FunctionalPoint) -> str:
    """从 ui 功能点 name 的 `#{kind}:` 段解析元素 kind（无法解析返回空串）。"""
    m = _UI_KIND_RE.search(fp.name)
    return m.group(1) if m else ""


def _ordinal_of(category: str, dimension: str) -> int:
    """(维度, 子维度) 在规范顺序中的固定位置（与请求范围无关，保证 tp_id 稳定）。"""
    base = _CANONICAL_ORDER.index(category) if category in _CANONICAL_ORDER else 0
    slot = _DIMENSION_SLOTS.get(category, {}).get(dimension, 0)
    return base * _ORDINAL_STRIDE + slot


@dataclass
class ExpandContext:
    """展开上下文：范围过滤 + 标签 + 审核门 + 鉴权画像（F8）。"""

    scopes: set[str] = field(default_factory=lambda: set(DEFAULT_SCOPE))
    default_tag: str = Tag.FULL.value
    review_status: str = "pending"
    module_filter: set[str] = field(default_factory=set)
    max_per_fp: int = (
        12  # 单功能点最多展开测试点数（留足 G-3/G-5 新增维度空间，避免静默丢弃关键维度）
    )
    # F8：鉴权接线画像（`engine.auth_scan.AuthProfile`）。为空时安全维度退回
    # 「应当鉴权」的保守口径（保持 v1.0 行为，不影响既有测试与产物）。
    auth_profile: Any = None
    # 增量收窄：这些 fp_id 拿不到函数行号（前端/无行号文件），只展开核心维度，
    # 避免限流/降级/令牌过期等泛化模板在无行号文件上膨胀。空集合=不收敛（兼容全量）。
    narrow_fp_ids: set[str] = field(default_factory=set)
    # T3 语义维度：`fp_id → [(行为维度, 子维度)]`，来自 `engine/change_semantics`
    # 对本次 diff hunk 的识别结果。非空时按「1 个核心类型维度 + 语义维度」合并，
    # 总维度数截断到 `semantic_max_dims`（语义维度优先占位）。空字典=关闭（与基线一致）。
    semantic_dims: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    semantic_max_dims: int = 4


# 维度收敛白名单：按 ftype 只保留「核心可验证」维度（行为维度, 子维度）。
# 仅对 narrow_fp_ids 命中的功能点生效；未命中走完整 plan_of（既有行为不变）。
_NARROW_PLAN: dict[str, tuple[tuple[str, str], ...]] = {
    FType.PAGE.value: (
        (TPType.NORMAL.value, Dimension.PAGE_REACH.value),
        (TPType.SECURITY.value, Dimension.UI_UNAUTH_PAGE.value),
        # 无路由页面（`has_route=False`，如 `pages/pc/PcChatPage.tsx` 未在本文件声明 path）
        # 的完整 plan 只有「交互元素可用」这一档；白名单里若不列它，交集为空 →
        # 变更命中的页面功能点展开 0 条（实测 project 30：91 FP → 保留 2 个页面 FP → 0 TP）。
        (TPType.NORMAL.value, Dimension.INTERACTIVE.value),
    ),
    FType.UI.value: ((TPType.NORMAL.value, Dimension.INTERACTIVE.value),),
    FType.COMPONENT.value: ((TPType.NORMAL.value, Dimension.INTERACTIVE.value),),
    FType.API.value: (
        (TPType.NORMAL.value, Dimension.AVAIL.value),
        (TPType.SECURITY.value, Dimension.AUTH_MISS.value),
        (TPType.BOUNDARY.value, Dimension.PARAM_ILLEGAL.value),
    ),
    FType.BUSINESS.value: (
        (TPType.NORMAL.value, Dimension.BIZ_LOGIC.value),
        (TPType.BOUNDARY.value, Dimension.BIZ_BOUNDARY.value),
    ),
}


def _method_of(fp: FunctionalPoint) -> str:
    """从功能点机器键里取 HTTP 方法（如 "POST /api/x" → POST）。"""
    return fp.name.split(" ", 1)[0].upper() if " " in fp.name else ""


def _resource_entity_of(fp: FunctionalPoint) -> str:
    """从 API 路径提取资源实体（G-3 资源归属）。

    规则：取「路径参数 `{...}` 前一个名词段」——它通常是该接口的归属资源
    （如 `POST /api/v1/invoices/{invoice_id}` → `invoices`）。
    无路径参数时取最后一个名词段；非 API 来源返回空串（无资源归属语义）。
    这是**轻量启发式**（不解析路由树），目的是让越权用例从「操作该资源应 403」
    变成「操作他人{invoices}资源应 403」的具体命题，避免泛泛而不可验证。
    """
    if fp.ftype != FType.API.value or " " not in fp.name:
        return ""
    path = fp.name.split(" ", 1)[1]
    segments = [s for s in path.split("/") if s]
    if not segments:
        return ""
    # 找第一个含 `{` 的段，取其前一个段作为资源实体
    for i, seg in enumerate(segments):
        if "{" in seg and i > 0:
            return segments[i - 1]
    return segments[-1]


def _tp_method(fp: FunctionalPoint) -> str:
    """测试点 method 字段：接口取 HTTP 动词，其余取来源标记（PAGE/UI/FUNC）。

    case_gen 依赖该字段区分 api / e2e 与派发 kind，故非接口类**不可**回退成函数名。
    """
    if fp.ftype == FType.API.value:
        return _method_of(fp)
    return _MARKER_BY_FTYPE.get(fp.ftype, MethodMarker.FUNC.value)


def _area_of(fp: FunctionalPoint) -> str:
    """测试点定位字段：接口取 URL 路径，其余取符号/路由名（业务函数即函数名）。

    T4：无 `path:` 声明的页面若已从仓库静态路由表反查到真实路由（`route_hint`），
    定位字段用它——用例步骤写「打开页面 /pc/chat」才可执行；若仍用文件路径，
    用例正文会变成「打开页面：frontend/app/src/pages/pc/PcChatPage.tsx」，无法操作。
    """
    if fp.ftype == FType.PAGE.value and fp.route_hint:
        return fp.route_hint
    if fp.ftype == FType.API.value and " " in fp.name:
        return fp.name.split(" ", 1)[1]
    return fp.name


def _security_expect(dimension: str, auth_mode: str, resource: str = "") -> str:
    """安全维度的预期文案（F8）：**来自代码事实**，不写死 401/403。

    三种模式的文案必须可区分——否则「判通过」会被读成「安全没问题」：
    - `ABSENT`：本就该公开 → 断言「可访问」；但要提示「如需鉴权应在代码接线」；
    - `OPTIONAL`：有接线但未配置即放行 → 未配置时属未鉴权暴露，仍按「应被拒」断言；
    - `REQUIRED`：正常鉴权 → 越权/无凭证均应被拒。

    `resource`（G-3/G-9 资源归属）：越权/跨租户用例据此变成「操作他人{resource}资源应被拒」
    的具体命题，而不是泛泛的「操作该资源」——验证才可构造、可复核。
    """
    # 维度级期望文案（与越权/跨租户语义一致，按维度归表避免长 return 链）
    dim_expect = {
        Dimension.TOKEN_EXPIRED.value: (
            "使用过期/失效令牌访问 → 返回 401/403；持有效令牌刷新后恢复原访问能力"
            "（验证令牌生命周期与续期机制）"
        ),
        Dimension.PRIV_ESC.value: (
            f"以非属主身份/越权凭证操作他人 {resource} 资源（resource_id 需人工提供他人 ID）"
            "→ 403，且不产生越权修改"
            if resource
            else "以他人身份/越权凭证操作该资源 → 403，且不产生越权修改"
        ),
        # G-9：多租户隔离
        Dimension.TENANT_READ.value: (
            f"以他人租户/他人身份读取他人 {resource} 资源（需双身份 + 他人 resource_id 复测）"
            "→ 403/404，且响应体不含他人数据"
            if resource
            else "以他人租户/他人身份读取他人资源 → 403/404，且响应体不含他人数据"
        ),
        Dimension.TENANT_WRITE.value: (
            f"以他人租户/他人身份写入他人 {resource} 资源（需双身份 + 他人 resource_id 复测）"
            "→ 403，且不产生越权修改/越权归属"
            if resource
            else "以他人租户/他人身份写入他人资源 → 403，且不产生越权修改"
        ),
    }
    if dimension in dim_expect:
        return dim_expect[dimension]
    if auth_mode == AuthMode.ABSENT.value:
        return (
            "该接口未检测到鉴权接线（公开接口）→ 可正常访问且不泄露敏感字段；"
            "如需鉴权应在代码中接线（判据见 engine/auth_scan.py）"
        )
    if auth_mode == AuthMode.OPTIONAL.value:
        return (
            "鉴权接线为占位实现（未配置令牌即放行）→ 未配置时视为未鉴权暴露；"
            "配置令牌后无凭证访问应被拒（401/403），且不泄露资源内容"
        )
    return "未携带或携带无效凭证时返回 401/403，且不泄露资源内容（越权访问同样被拒）"


# UI 专属子维度的预期文案（#222：页面/元素套 DEFAULT_SCOPE 后需要可判定的边界/安全预期）。
# 抽成字典：避免 _expect_of 因「逐维度 if」膨胀到圈复杂度上限。
_UI_EXPECT: dict[str, str] = {
    Dimension.UI_INPUT_INJECT.value: (
        "向该输入/表单提交含脚本或特殊字符的内容 → 系统应转义或拦截，"
        "不执行注入脚本、页面不出现 XSS/布局错乱"
    ),
    Dimension.UI_UNAUTH_PAGE.value: "未登录/无权限直接访问该页面 → 应跳转登录或返回 403，不泄露受限内容",
    Dimension.UI_LONG_INPUT.value: "输入超长或含特殊字符 → 系统应截断或明确提示，不溢出、不产生 5xx",
    Dimension.UI_ILLEGAL_OPTION.value: "选择未列出或越界选项 → 系统应拒绝或回退默认，不产生脏数据",
    Dimension.UI_REPEAT_CLICK.value: "快速重复点击/提交 → 应幂等或防重，不产生重复创建/重复提交",
    Dimension.UI_DEEPLINK.value: "直接以 URL 访问该路由 → 应正常渲染或按权限跳转，不 404/白屏",
    Dimension.UI_POOR_VIEWPORT.value: "极端视口（极小/极大）或弱网下 → 页面可渲染、关键功能可用，不白屏",
}

# UI 异常流预期文案（G-5）：行为维度属「异常」，需 DEFAULT_SCOPE 含异常才默认生成。
# 这些场景多数需人工/专项触发（断网、注入报错、清空数据、杀后端），用例本身描述「出错时应优雅」，
# 执行器对可观测部分（页面不白屏/控制台不新增报错）做最佳努力判定，不可观测部分如实标注。
_UI_ABNORMAL_EXPECT: dict[str, str] = {
    Dimension.UI_NETWORK_INTERRUPT.value: (
        "断网/弱网后刷新或操作 → 应优雅处理（重试/友好提示），不白屏、不抛未捕获异常"
    ),
    Dimension.UI_ERROR_DISPLAY.value: (
        "提交非法数据或触发服务报错 → 应回显友好错误提示，不展示原始堆栈/敏感内部信息"
    ),
    Dimension.UI_EMPTY_STATE.value: (
        "无数据（空列表/空结果）时 → 应渲染空状态提示，不崩溃、不白屏、不抛未捕获异常"
    ),
    Dimension.UI_SERVER_ERROR.value: (
        "后端返回 5xx 时 → 前端应展示错误页/降级提示而非白屏，且不影响其他功能入口"
    ),
}


# 业务函数（code 通道）四档维度预期文案（scope 增强，2026-09-21）。
# 业务函数无 HTTP 语义，不套鉴权/状态码断言，改为「代码层可判定」的命题。
_BUSINESS_EXPECT: dict[str, str] = {
    Dimension.BIZ_LOGIC.value: (
        "函数被正常调用（合法入参、依赖可用）→ 返回预期结果/状态，不抛未捕获异常"
    ),
    Dimension.BIZ_INPUT_VALID.value: (
        "未校验/未转义的入参（空值、超长、特殊字符、注入 payload）传入函数 → "
        "应被校验或转义并拒绝/降级，不产生注入、越权数据写入或脏数据"
    ),
    Dimension.BIZ_BOUNDARY.value: (
        "空值/None/超长/越界参数传入 → 应被参数校验捕获并返回明确错误，不崩溃、不产生静默错误结果"
    ),
    Dimension.BIZ_ABNORMAL.value: (
        "依赖故障/非法状态/异常分支被触发 → 应被捕获并转换为结构化错误"
        "（不静默吞错、不向上抛未捕获异常），调用方拿到可判定的失败信号"
    ),
}


# T3 语义维度的预期文案：每条都写成「对什么输入/操作 → 观察什么 → 判定标准」，
# 保证人工或执行器都能判 pass/fail（不是「验证分页正确」这种无法判定的空话）。
_SEMANTIC_EXPECT: dict[str, str] = {
    Dimension.LIST_INSERT_VISIBLE.value: (
        "新增一条记录后 → 列表立即出现该条且位于排序规则首位/末位（无需刷新），"
        "总条数 +1，不出现空白行或占位行"
    ),
    Dimension.LIST_DEDUPE.value: (
        "对同一对象重复触发新增/合并（同一业务主键或同一 id）→ 列表只保留一条，"
        "条数不重复增长，不出现两行同名/同 id 数据"
    ),
    Dimension.LIST_ORDER_CONSISTENCY.value: (
        "新增/更新/删除若干条后 → 列表顺序仍符合声明的排序规则（如创建时间倒序），"
        "被更新项不跳到错误位置、不出现重复或丢失"
    ),
    Dimension.PAGINATION_NO_OVERLAP.value: (
        "连续翻页（第 1→2→3 页，每页 N 条）→ 各页数据不重叠、不遗漏，"
        "累计条数 = 去重后并集大小；末页 hasMore=false"
    ),
    Dimension.PAGINATION_OFFSET_SYNC.value: (
        "本地新增/删除若干条后继续取下一页 → offset/limit 与实际条数同步："
        "不出现空页、不跳过未返回的记录、不因 offset 偏移触发 4xx/5xx"
    ),
    Dimension.BRANCH_ISOLATION.value: (
        "在模式/渠道 A 与 B 下分别执行同一操作 → 两边的数据与状态互不串扰"
        "（切换后不残留另一模式的条目、A 的新增不出现在 B 的列表）"
    ),
    Dimension.FALLBACK_DEFAULT.value: (
        "主路径输入缺失或依赖返回空（None/空串/空数组）→ 走兜底分支返回默认值"
        "（如「未命名对话」），不抛未捕获异常、不写入空标题/空字段"
    ),
    Dimension.CONTRACT_SHAPE.value: (
        "返回/传递的数据结构变更后 → 消费方仍能按旧字段名与类型取到必需字段；"
        "新增字段不影响旧解析，不出现 undefined/KeyError/字段错位"
    ),
    Dimension.GUARD_ZERO_VS_EMPTY.value: (
        "分别传入「未提供（None/缺省）」「空值（空串/空数组/0）」→ 两者被区分处理："
        "空值走「已发起但内容为空」分支，未提供走「仅预留」分支，不互相覆盖"
    ),
}


def _merge_semantic(
    plan: list[tuple[str, str]],
    semantic: list[tuple[str, str]],
    scopes: set[str],
    max_dims: int,
) -> list[tuple[str, str]]:
    """类型维度 ∪ 语义维度 → 最终维度计划（T3）。

    合并规则（与任务文档 §T3 一致，另加一条「核心维度保底」）：
    1. **保留 1 个核心类型维度**（plan 首条，即该来源类型最本质的维度，如
       API 的「正常-可用性」/ 页面的「交互元素可用」）——否则语义维度占满预算后，
       功能点会连一条「主流程能跑通」的用例都没有；
    2. 语义维度优先占余下槽位（这次改了什么就先测什么）；
    3. 语义维度不足时，用剩余类型维度补位，但**总数不超过 `max_dims`**；
    4. 全部按 `scopes` 过滤——用户只勾「正常+边界」时，语义里的异常维度同样不生成。
    """
    limit = max(1, int(max_dims))
    allowed = set(scopes) if scopes else set(DEFAULT_SCOPE)
    base = [(c, d) for c, d in plan if c in allowed]
    sem = [(c, d) for c, d in semantic if c in allowed]
    merged: list[tuple[str, str]] = []
    for item in base[:1] + sem + base[1:]:
        if item in merged:
            continue
        merged.append(item)
        if len(merged) >= limit:
            break
    return merged


def _business_expect_of(fp: FunctionalPoint, dimension: str) -> str:
    """业务函数预期：第一步 B——把真实签名拼进固定话术，让预期可判定。

    固定话术（如「返回预期结果/状态」）对任何函数都一样，用户看不出测什么。
    这里在句首补上该函数的真实入参与返回类型（来自 AST，见 `signature_hint`），
    使预期变成「入参 domain_id、top_n；返回 DomainDetail → 返回领域详情对象」这类
    针对本函数的命题。无签名信息时原样退回固定话术，保持旧行为。
    """
    base = _BUSINESS_EXPECT[dimension]
    hint = (fp.signature_hint or "").strip()
    if not hint:
        return base
    return f"（{hint}）{base}"


def _expect_of(  # noqa: PLR0911 - 按维度分支，拆函数反而更难对照
    fp: FunctionalPoint, category: str, dimension: str = "", auth_mode: str = "", resource: str = ""
) -> str:
    """按维度给出可判定的预期结果文案。

    安全维度的预期**来自代码事实**（F8）：`auth_mode` 由 `engine/auth_scan.py` 扫源码得出，
    不再写死 401/403——对本来就该公开的接口写死 401/403 会产出假失败。
    `resource`（G-3）用于把越权用例变成「操作他人资源应被拒」的具体命题。
    """
    # T3 语义维度优先：这几条维度是「本次改动具体是什么」的命题，必须压过按来源
    # 类型生成的泛化文案（否则无路由页面的语义维度会退回「交互元素可用」）。
    if dimension in _SEMANTIC_EXPECT:
        return _SEMANTIC_EXPECT[dimension]
    # 无显式路由的屏幕：只展开交互维度，预期按交互口径（地址未知，不能承诺「页面可达」）。
    if fp.ftype == FType.PAGE.value and not fp.has_route:
        return "交互元素可用，点击后产生预期状态变化，且不新增控制台错误"
    # scope 增强（2026-09-21）：业务函数三档维度无 HTTP 语义，优先走 _BUSINESS_EXPECT。
    # 第一步 B：有真实签名时拼进预期，让「返回预期结果」变成可判定命题。
    if dimension in _BUSINESS_EXPECT:
        return _business_expect_of(fp, dimension)
    if category == TPType.NORMAL.value:
        return expect_of(fp.ftype, fp.name, _method_of(fp))
    if category == TPType.ABNORMAL.value:
        return _abnormal_expect(dimension)
    if category == TPType.SECURITY.value:
        return (
            _UI_EXPECT[dimension]
            if dimension in (Dimension.UI_INPUT_INJECT.value, Dimension.UI_UNAUTH_PAGE.value)
            else _security_expect(dimension, auth_mode, resource)
        )
    if category == TPType.PERFORMANCE.value:  # G-8
        return _PERF_EXPECT.get(
            dimension,
            "关键接口应达成约定 SLA（响应时间/并发行为），超阈值或串数据需专项/压测确认",
        )
    # 边界：接口参数维度走通用文案；UI 维度走 _UI_EXPECT。
    return _UI_EXPECT.get(dimension, "参数缺失或越界时返回 400/422，校验信息明确指出非法字段")


def _abnormal_expect(dimension: str) -> str:
    """G-5：异常流各维度的预期文案（与 executor 判定口径一致）。"""
    if dimension in _UI_ABNORMAL_EXPECT:
        return _UI_ABNORMAL_EXPECT[dimension]
    table = {
        Dimension.RATE_LIMIT.value: "高频/突发请求 → 触发限流（429 或 Retry-After/令牌桶耗尽），不拖垮服务",
        Dimension.IDEMPOTENT.value: "重复提交同一请求 → 应幂等或防重（409 冲突/重复拒绝），不产生重复创建",
        Dimension.DEGRADED.value: "依赖（DB/下游服务）故障时 → 服务应降级（503/熔断）而非级联雪崩",
        Dimension.TIMEOUT.value: "慢请求/依赖超时 → 应超时兜底（504 或快速失败）而非无限挂起",
    }
    if dimension in table:
        return table[dimension]
    # E2/A2：与 executor._PASS_PREDICATES[_KIND_ABNORMAL]（200-499 通过）口径一致。
    # 4xx 是典型形态，但「200 + 业务错误码」的软失败也算妥善处理；5xx 才算未通过。
    return (
        "异常被妥善处理：返回 4xx（资源不存在 404 / 状态非法 409）或 200+结构化错误码，"
        "不返回 5xx、不抛未捕获异常"
    )


# G-8：性能维度的预期文案（与 executor 判定口径一致：基线真实测延迟；并发需并行 harness）。
_PERF_EXPECT: dict[str, str] = {
    Dimension.PERF_BASELINE.value: (
        "关键接口单次请求响应时间应低于约定 SLA（基线探测记录实际耗时，超阈值告警不判失败）"
    ),
    Dimension.PERF_CONCURRENCY.value: (
        "并发读/写同一资源 → 不应串入他人数据、不应因竞争产生脏写/重复创建（需并行压测 harness 验证）"
    ),
}


def _semantic_of(fp: FunctionalPoint, category: str) -> str:
    # 第一步 A（仅业务函数）：验证项主体用中文语义（与标题一致），而非英文裸
    # `模块 · 函数名`。其他类型（API/PAGE/UI）保持原行为，避免连锁变化。
    core = _readable_core(fp) if fp.ftype == FType.BUSINESS.value else fp.title
    return f"{_SEMANTIC_ACTION[category]}：{core}"


# 标题可读性（2026-10-14）：`[正常] agents · register_m3_harness_profiles` 这种裸 `模块 · 函数名`
# 用户看不出测的是什么。优先用 docstring 中文摘要做标题主体；无中文摘要时退回
# `模块 · 函数名`。维度（Dimension.value 本身是中文短语，如「安全-越权」）始终拼在尾部。
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def _readable_core(fp: FunctionalPoint) -> str:
    """标题主体：有中文功能描述用描述，否则退回 `模块 · 名称`。

    业务函数的 semantic 形如 `Cls.method（入参 …）`。类名前缀对「测什么」无帮助，
    统一剥掉只留方法名（标题/验证项风格一致，也避免长前缀把标题顶过 48 字被截断）。
    完整签名始终保留在 `fp.signature_hint` 里供预期展示，可判定性不降低；
    追溯用的 fp_id / fp_contract_id 仍带类名，不受影响。
    """
    # 运行时 UI 元素：semantic 句式统一以「页面 {path} 可见{类型}交互元素：文字=…」开头，
    # 同页所有元素前缀相同，48 字截断后标题撞车（23 个不同按钮生成同一标题）。
    # fp.title 把元素文字放在最前（`按钮「深色模式」@ area::…`），优先用它保住区分度。
    if fp.ftype == FType.UI.value and fp.title:
        return fp.title if len(fp.title) <= 48 else fp.title[:46] + "…"
    sem = (fp.semantic or "").strip()
    if sem and _CJK_RE.search(sem) and len(sem) > len(fp.name) + 2:
        # 统一剥类名前缀（不限于超长）：`Cls.method（…）` → `method（…）`
        if fp.ftype == FType.BUSINESS.value and "." in fp.name and sem.startswith(fp.name):
            method = fp.name.rsplit(".", 1)[-1]
            sem = method + sem[len(fp.name) :]
        return sem if len(sem) <= 48 else sem[:46] + "…"
    return fp.title


# G-5：需要注入故障/特定上下文才能验证的异常流，自动化默认无法判定，标记为低把握 + 未验证。
# 这些用例的价值是「覆盖出错」的设计层覆盖；真实验证需专项/人工/压测（详见覆盖评估报告 G-5）。
_BEST_EFFORT_DIMS: frozenset[str] = frozenset(
    {
        Dimension.IDEMPOTENT.value,
        Dimension.DEGRADED.value,
        Dimension.TIMEOUT.value,
        Dimension.TOKEN_EXPIRED.value,
        Dimension.UI_NETWORK_INTERRUPT.value,
        Dimension.UI_ERROR_DISPLAY.value,
        Dimension.UI_EMPTY_STATE.value,
        Dimension.UI_SERVER_ERROR.value,
        # G-9：多租户隔离需双身份 + 他人 resource_id 复测，单令牌无法构造 → 诚实 SKIPPED
        Dimension.TENANT_READ.value,
        Dimension.TENANT_WRITE.value,
        # G-8：并发不串数据需并行压测 harness，单请求探活无法验证 → 诚实 SKIPPED
        Dimension.PERF_CONCURRENCY.value,
        # scope 增强（2026-09-21）：业务函数三档维度无运行时 harness 可自动验证 → 诚实 SKIPPED
        Dimension.BIZ_ABNORMAL.value,
        Dimension.BIZ_INPUT_VALID.value,
        Dimension.BIZ_BOUNDARY.value,
        # T3 语义维度：需要「多页数据 / 重复触发 / 双模式数据 / 依赖故障」才能构造，
        # 单次探活无法判定 → 与 G-5/G-8/G-9 同口径标为 best-effort（expect 仍可人工判定）。
        Dimension.LIST_DEDUPE.value,
        Dimension.PAGINATION_NO_OVERLAP.value,
        Dimension.PAGINATION_OFFSET_SYNC.value,
        Dimension.BRANCH_ISOLATION.value,
        Dimension.FALLBACK_DEFAULT.value,
    }
)
# 限流可发突发请求自动验证（置信度高于纯故障注入类）
_BURST_DIMS: frozenset[str] = frozenset({Dimension.RATE_LIMIT.value})


def _confidence_of(dimension: str) -> float:
    """测试点置信度：可自动验证的高；需故障注入/专项验证的低。"""
    if dimension in _BURST_DIMS:
        return 0.7
    if dimension in _BEST_EFFORT_DIMS:
        return 0.4
    return 1.0


def plan_of(fp: FunctionalPoint) -> list[tuple[str, str]]:  # noqa: C901, PLR0911
    """功能点 → [(行为维度, 子维度)]，**与 legacy 展开规则逐条对齐**（P1 契约冻结）。

    对齐表（顺序即产出顺序，也是 tp_id 序号的规范顺序）：

    | 来源      | 维度 | 子维度         | 条件                        |
    |-----------|------|----------------|-----------------------------|
    | api       | 正常 | 可用性         | 全部                        |
    | api       | 安全 | 鉴权缺失       | 全部                        |
    | api       | 边界 | 参数非法       | 全部                        |
    | api       | 异常 | 资源不存在     | 含路径参数 `{id}`           |
    | api       | 安全 | 越权           | 含 `{id}` 且方法为 PUT/PATCH/DELETE |
    | page      | 正常 | 页面可达       | 全部                        |
    | component | 正常 | 交互元素可用   | 全部                        |
    | business  | 正常 | 业务逻辑可用   | 全部                        |

    两条与 legacy 的有意差异（均已在代码注释说明）：
    - `component` 的来源扩展名加入 `.tsx/.jsx`（legacy 只认 `.vue`，React 仓一个都提不出来）；
    - 不再给 `page` 派生「边界」维度（页面无参数校验语义，属噪声）。
    """
    if fp.ftype == FType.API.value:
        method = _method_of(fp)
        has_id = bool(_PATH_PARAM_RE.search(fp.name))
        plan = [
            (TPType.NORMAL.value, Dimension.AVAIL.value),
            (TPType.SECURITY.value, Dimension.AUTH_MISS.value),
            # G-3：令牌过期续期（对所有鉴权接口有意义；需注入过期令牌复测，故 best-effort）
            (TPType.SECURITY.value, Dimension.TOKEN_EXPIRED.value),
            (TPType.BOUNDARY.value, Dimension.PARAM_ILLEGAL.value),
        ]
        if has_id:
            plan.append((TPType.ABNORMAL.value, Dimension.RES_NOT_FOUND.value))
        # G-5：接口异常流（限流对所有接口有意义；幂等仅对写操作有意义）
        plan.append((TPType.ABNORMAL.value, Dimension.RATE_LIMIT.value))
        if method in _WRITE_METHODS:
            plan.append((TPType.ABNORMAL.value, Dimension.IDEMPOTENT.value))
        plan.append((TPType.ABNORMAL.value, Dimension.DEGRADED.value))
        # G-5：超时兜底（慢依赖/超时请求应超时而非挂起；需构造慢依赖，best-effort）
        plan.append((TPType.ABNORMAL.value, Dimension.TIMEOUT.value))
        if has_id and method in _PRIV_ESC_METHODS:
            plan.append((TPType.SECURITY.value, Dimension.PRIV_ESC.value))
        # G-9：多租户数据隔离（需双身份复测，执行器诚实 SKIPPED）
        if has_id and method == "GET":
            plan.append((TPType.SECURITY.value, Dimension.TENANT_READ.value))
        if has_id and method in _WRITE_METHODS:
            plan.append((TPType.SECURITY.value, Dimension.TENANT_WRITE.value))
        # G-8：性能/并发（独立行为维度「性能」，需 scope 含 性能 才默认生成）
        plan.append((TPType.PERFORMANCE.value, Dimension.PERF_BASELINE.value))
        plan.append((TPType.PERFORMANCE.value, Dimension.PERF_CONCURRENCY.value))
        return plan
    if fp.ftype == FType.PAGE.value:
        if not fp.has_route:
            # 无显式路由声明的屏幕（`views/Home.vue` 这类）：地址未知，**不能**生成
            # 「页面可达/未授权/极端视口/断网…」这些需要 URL 的维度——那会对一条不存在
            # 的地址产出必然失败的假用例。只验证它身上真实存在的交互元素，与 component 同口径。
            return [(TPType.NORMAL.value, Dimension.INTERACTIVE.value)]
        # #222：页面套 DEFAULT_SCOPE → 正常(可达) + 安全(未授权访问) + 边界(极端视口)
        # G-5：UI 异常流（网络中断/错误回显/空状态/错误页）—— 行为维度同属「异常」，
        #      需 DEFAULT_SCOPE 含异常（2026-09-15 已纳入）才默认生成。
        return [
            (TPType.NORMAL.value, Dimension.PAGE_REACH.value),
            (TPType.SECURITY.value, Dimension.UI_UNAUTH_PAGE.value),
            (TPType.BOUNDARY.value, Dimension.UI_POOR_VIEWPORT.value),
            (TPType.ABNORMAL.value, Dimension.UI_NETWORK_INTERRUPT.value),
            (TPType.ABNORMAL.value, Dimension.UI_ERROR_DISPLAY.value),
            (TPType.ABNORMAL.value, Dimension.UI_EMPTY_STATE.value),
            (TPType.ABNORMAL.value, Dimension.UI_SERVER_ERROR.value),
        ]
    if fp.ftype == FType.UI.value:
        # #222：元素级功能点按 kind 展开「正常 + 安全 + 边界」（解决地址通道只到正常维度）
        return _UI_PLAN_BY_KIND.get(_ui_kind_of(fp), _UI_PLAN_DEFAULT)
    if fp.ftype == FType.BUSINESS.value:
        # scope 增强（2026-09-21）：业务函数原本只展开「正常」一档，导致纯 Python 代码仓库
        # （如 Django 后端 apps/）的安全/边界/异常维度永不成点。现统一派生四档，与
        # API/PAGE/UI 通道对齐；tp_id 用规范 ordinal（base*10+slot），同一份代码编号不变。
        return [
            (TPType.NORMAL.value, Dimension.BIZ_LOGIC.value),
            (TPType.SECURITY.value, Dimension.BIZ_INPUT_VALID.value),
            (TPType.BOUNDARY.value, Dimension.BIZ_BOUNDARY.value),
            (TPType.ABNORMAL.value, Dimension.BIZ_ABNORMAL.value),
        ]
    if fp.ftype == FType.COMPONENT.value:
        return [(TPType.NORMAL.value, Dimension.INTERACTIVE.value)]
    return [(TPType.NORMAL.value, Dimension.BIZ_LOGIC.value)]


def dimensions_of(fp: FunctionalPoint) -> list[str]:
    """（兼容视图）某条功能点会展开出哪些行为维度。"""
    return [category for category, _ in plan_of(fp)]


def expand_functional_point(
    fp: FunctionalPoint,
    ctx: ExpandContext,
    *,
    ordinal_base: int = 0,
    tag: str | None = None,
) -> list[TestPoint]:
    """把一条功能点展开为测试点集合（已按 ctx.scopes 过滤）。"""
    method = _tp_method(fp)
    area = _area_of(fp)
    auth_mode = (
        auth_scan.mode_of_fp(fp, ctx.auth_profile)
        if ctx.auth_profile is not None
        else AuthMode.REQUIRED.value
    )
    out: list[TestPoint] = []
    produced = 0
    resource = _resource_entity_of(fp)  # G-3：资源归属（仅 API 有语义）
    plan = plan_of(fp)
    # T3：本次 diff 识别出的语义维度（空=该功能点所在文件没识别出语义，走旧逻辑）
    semantic = list(ctx.semantic_dims.get(fp.fp_id) or ())
    semantic_set = set(semantic)
    if semantic:
        # 语义维度优先：保留 1 个核心类型维度保底，其余槽位给「这次到底改了什么」。
        plan = _merge_semantic(plan, semantic, ctx.scopes, ctx.semantic_max_dims)
    elif fp.fp_id in ctx.narrow_fp_ids and fp.ftype in _NARROW_PLAN:
        narrowed = [x for x in plan if x in _NARROW_PLAN[fp.ftype]]
        # 收窄的目的只是「砍泛化模板」，不是把功能点砍成 0 条：交集为空时保留原 plan 的
        # 第一条（该 ftype 最核心的维度），避免「只改了这一个文件，却生成 0 条用例」。
        plan = narrowed or plan[:1]
    for category, dimension in plan:
        if category not in ctx.scopes:
            continue
        # G-3/G-9：越权与跨租户写用例标记属主隔离（资源实体已识别且为资源级操作）
        owner_scoped = bool(resource) and dimension in (
            Dimension.PRIV_ESC.value,
            Dimension.TENANT_WRITE.value,
        )
        out.append(
            TestPoint(
                tp_id=tp_id_of(
                    fp.fp_id,
                    category,
                    area=area,
                    method=method,
                    dimension=dimension,
                    # 固定序号：与请求范围无关，保证「同一份代码，编号不变」
                    ordinal=ordinal_base + _ordinal_of(category, dimension),
                ),
                fp_contract_id=fp.fp_id,
                category=category,
                module=fp.module,
                title=f"[{category}] {_readable_core(fp)} · {dimension}",
                semantic=(
                    f"变更语义：{_readable_core(fp)} · {dimension}"
                    if (category, dimension) in semantic_set
                    else _semantic_of(fp, category)
                ),
                source=fp.file_path,
                method=method,
                area=area,
                expect=_expect_of(fp, category, dimension, auth_mode, resource),
                dimension=dimension,
                tag=tag or ctx.default_tag,
                review_status=ctx.review_status,
                verify_layer=verify_layer_of_ftype(fp.ftype),
                # G-3/G-5：资源归属与置信度随测试点透出，供执行器与报告使用
                resource=resource,
                owner_scoped=owner_scoped,
                confidence=_confidence_of(dimension),
                unverified=dimension in _BEST_EFFORT_DIMS,
                # T3：语义维度产出的测试点标 origin=semantic，供报告把「本体/影响面/运行时」
                # 分开统计（T5 三 KPI 依赖来源标签，混在 rule 里就分不出来了）。
                origin=("semantic" if (category, dimension) in semantic_set else "rule"),
            )
        )
        produced += 1
        if produced >= ctx.max_per_fp:
            break
    return out


def expand_all(
    fps: list[FunctionalPoint],
    ctx: ExpandContext | None = None,
    *,
    tag_by_fp: dict[str, str] | None = None,
) -> list[TestPoint]:
    """批量展开；`tag_by_fp` 为增量打标结果（fp_id → 全量/更新）。"""
    ctx = ctx or ExpandContext()
    tag_by_fp = tag_by_fp or {}
    out: list[TestPoint] = []
    for fp in fps:
        if ctx.module_filter and fp.module not in ctx.module_filter:
            continue
        out.extend(expand_functional_point(fp, ctx, tag=tag_by_fp.get(fp.fp_id)))
    return out


def scope_summary(tps: list[TestPoint]) -> dict[str, int]:
    """按行为维度汇总（供报告与验收）。"""
    out: dict[str, int] = {}
    for tp in tps:
        out[tp.category] = out.get(tp.category, 0) + 1
    return out
