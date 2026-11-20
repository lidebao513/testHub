"""数据契约 v1.0（Single Source of Contract）。

冻结依据：`P1-1_契约冻结说明v1.0.md`。
本模块定义三层产物的**字段、编号算法、八要素、追溯键**，是全服务唯一的契约声明处；
任何产出/消费契约的模块都必须 import 本模块，不得自行拼字段名或自行编号。

三条硬承诺：
  1. 产物可被旧消费方直接读取（字段名/取值域/编号格式一致）
  2. 编号稳定（同一份代码重复跑，编号不变）
  3. 追溯双向且无孤儿
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from typing import Any

from core.enums import FType, Tag, TPType, VerifyLayer


# 契约版本：变更规则见 P1-3《契约升版策略》（只增不破）
CONTRACT_VERSION = "1.0"

# 报告版本（F15）：报告 JSON 的结构版本，独立于产物契约版本演进。
# 报告是**只读派生物**（DB 事实的纯函数），故其版本只约束「结构」，不参与三方契约冻结。
REPORT_VERSION = "1.0"

# 测试点 / 用例「来源」取值（Additive：新增取值域，旧消费方忽略未知值）。
# - rule：规则引擎确定性产出（稳定溯源基线，内容指纹 md5 不变）；
# - llm_enrich：语义增强通道补点（semantic_enrich）；
# - llm_design：用例设计通道（llm_design，F10b）；
# - llm_implicit / llm_verified：语义增强对隐性规则的两类标记；
# - expert_page：测试专家系统·URL 通道（PageExpert，S0–S6 全环节 LLM）；
# - expert_code：测试专家系统·代码通道（CodeExpert，Phase 2）。
ORIGIN_RULE = "rule"
ORIGIN_LLM_ENRICH = "llm_enrich"
ORIGIN_LLM_DESIGN = "llm_design"
ORIGIN_LLM_IMPLICIT = "llm_implicit"
ORIGIN_LLM_VERIFIED = "llm_verified"
ORIGIN_EXPERT_PAGE = "expert_page"
ORIGIN_EXPERT_CODE = "expert_code"
# 专家来源取值集合（供护栏 / 报告识别「专家增补」）
EXPERT_ORIGINS = (ORIGIN_EXPERT_PAGE, ORIGIN_EXPERT_CODE)

# 用例八要素（顺序即展示顺序；缺一不可）
EIGHT_ELEMENTS: tuple[str, ...] = (
    "tc_no",
    "module",
    "title",
    "case_type",
    "priority",
    "precondition",
    "doc_steps",
    "expect",
)

# 优先级：异常/安全/边界 → P1；命中高风险模块 → P1；其余 → P2
HIGH_RISK_MODULE_KEYWORDS: tuple[str, ...] = (
    "鉴权",
    "登录",
    "支付",
    "交易",
    "权限",
    "审批",
)

# 历史别名 → 统一取值（契约缺陷 D-1 的兼容修复）
_TEST_TYPE_ALIASES: dict[str, str] = {
    "新增": Tag.UPDATE.value,
    "更新": Tag.UPDATE.value,
    "全量": Tag.FULL.value,
}


# ============================================================================
# 编号算法（冻结，不可改）
# ============================================================================
def fp_id_of(ftype: str, file_path: str, name: str, *, repo_key: str = "") -> str:
    """功能点稳定编号：`FP-` + md5(ftype|file_path|name) 前 8 位。

    用哈希而非自增，是为了跨运行、跨机器稳定，且规避路径中的中文/空格/分隔符干扰。
    md5 在此**仅作指纹用途，非安全用途**。

    `repo_key` 是多仓改造新增的**关键字参数**（项目内仓库稳定短名，如
    `backend`/`frontend`）：传值时并入指纹首段，使跨仓「同路径 + 同函数/接口」
    产出不同 `fp_id`，从根上消除跨仓编号碰撞。缺省空串时指纹构造与冻结版本
    **逐字节一致**——旧单仓调用方零变化（契约冻结红线，见 `test_fp_id_repo_key`）。
    """
    key = f"{ftype}|{file_path}|{name}"
    if repo_key:
        key = f"{repo_key}|{key}"
    digest = hashlib.md5(key.encode("utf-8")).hexdigest()[:8]  # nosec B324  # 非安全用途：仅生成稳定 ID 指纹
    return "FP-" + digest


def tp_id_of(  # noqa: PLR0913, PLR0917 - 编号由多段内容构成，显式列出比包成结构体更清晰
    fp_id: str,
    category: str,
    area: str = "",
    method: str = "",
    dimension: str = "",
    ordinal: int = 0,
) -> str:
    """测试点稳定编号：`TP-` + md5(fp_id|category|area|method|dimension|ordinal) 前 8 位。

    修复契约缺陷 D-2：legacy 用 `TP-{枚举序号}` 是**位置相关**的，文件顺序一变编号全错位。
    本实现把编号绑定到「内容指纹」，同一份代码重复跑编号不变；
    ordinal 用于同一功能点下产生多条同构测试点时的消歧（默认 0）。
    """
    key = f"{fp_id}|{category}|{area}|{method}|{dimension}|{ordinal}"
    digest = hashlib.md5(key.encode("utf-8")).hexdigest()[:8]  # nosec B324  # 非安全用途：仅生成稳定 ID 指纹
    return "TP-" + digest


def normalize_test_type(value: str | None) -> str:
    """把历史标签统一为 `全量` / `更新`（D-1 兼容层：仍接受读出 `新增`）。"""
    if not value:
        return Tag.FULL.value
    return _TEST_TYPE_ALIASES.get(value, Tag.FULL.value)


def tp_id_variant(tp_id: str, variant: str) -> str:
    """在同一测试点下派生「变体」编号，**保持 `TP-` + 8 位十六进制的格式不变**。

    为什么需要：规则引擎会对边界测试点凑对出「参数缺失」变体。早期实现直接做字符串
    拼接（`tp_id + "-M"`），产出 `TP-1a2b3c4d-M` —— 长度与格式都不符合契约，
    任何按 `^TP-[0-9a-f]{8}$` 校验的消费方都会拒收（契约缺陷 D-10）。
    """
    key = f"{tp_id}|{variant}"
    digest = hashlib.md5(key.encode("utf-8")).hexdigest()[:8]  # nosec B324  # 非安全用途：仅生成稳定 ID 指纹
    return "TP-" + digest


# ============================================================================
# 三层产物
# ============================================================================
@dataclass
class FunctionalPoint:
    """功能点：从源码提取出的一个「可测行为单元」。"""

    fp_id: str
    ftype: str
    file_path: str
    name: str  # 机器键（如 "POST /api/v1/tasks"），消费方依赖，不可改
    title: str  # 业务化展示名
    module: str = ""
    semantic: str = ""
    description: str = ""
    commit_ref: str = ""
    review_status: str = "approved"
    # v1.1 预留（只增不改；缺省即 v1.0 语义）：业务函数从 AST 提取的真实签名摘要
    # （如「入参 domain_id、top_n；返回 DomainDetail」），供测试点标题/预期具体化消费。
    signature_hint: str = ""
    # v1.4（只增不改，缺省 True 即 v1.0 语义）：该 PAGE 功能点是否来自**显式路由声明**。
    # True = `name` 是真实可达 URL（可展开「页面可达/未授权/极端视口」等需要地址的维度）；
    # False = 只是「位于 pages/views 目录但没写 path 的屏幕文件」，地址未知——此时不生成
    # 需要 URL 的维度，改为验证其交互元素，避免对不存在的地址产出必然失败的假用例。
    # 缺省 True 的原因：旧行为对所有 PAGE 一律按「有路由」展开，默认 False 会静默改变
    # 所有历史调用方（含手工构造 fp 的测试与 runtime_ui 产出的真实页面）的展开结果。
    has_route: bool = True
    # v1.5（只增不改，缺省空串＝未推断）：T4 从仓库静态路由表反查到的真实路由。
    # 与 `has_route` 的分工：`has_route` 只回答「能不能展开需要地址的维度」，
    # `route_hint` 才是那个地址本身（如 `/pc/chat`）。推断必须被路由表印证
    # （`<Route path="/x" element={<Page/>}>` 的组件名或文件名候选双向匹配），
    # 不做纯文件名猜测；`route_source="inferred"` 标明来源，便于报告区分
    # 「代码里写的路由」与「工具推断的路由」。
    route_hint: str = ""
    route_source: str = ""
    # v1.7（只增不改，缺省空串＝单仓兼容）：多仓改造，功能点归属仓的项目内短名
    # （backend/frontend/shared）。为空即旧单仓；非空时 fp_id 已含 repo 分量，
    # 供报告分组 / 存储替换粒度 / 跨仓命名空间隔离消费。
    repo_key: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["contract_version"] = CONTRACT_VERSION
        return d


@dataclass
class TestPoint:
    """测试点：功能点展开出的一个「要验证什么」的命题。"""

    # 非 dataclass 字段（无注解）：阻止 pytest 把本类当作测试类采集
    __test__ = False

    tp_id: str
    fp_contract_id: str
    category: str  # 正常 / 异常 / 安全 / 边界
    module: str = ""
    title: str = ""
    semantic: str = ""
    source: str = ""  # 来源文件，统一正斜杠
    method: str = ""
    area: str = ""
    expect: str = ""
    dimension: str = ""
    tag: str = Tag.FULL.value
    review_status: str = "pending"
    verify_layer: str = ""
    # v1.1 预留（只增不改；缺省即 v1.0 语义）
    evidence: list[str] = field(default_factory=list)
    confidence: float = 1.0
    origin: str = "rule"
    unverified: bool = False
    # v1.2：安全资源归属（G-3）：该测试点操作/验证的资源实体与是否属主隔离。
    # 仅用于越权/鉴权类用例，使「操作他人资源应被拒」成为可构造的具体命题；
    # 缺省空串即「未识别归属」，执行器据此给出诚实的低把握结论而非假装高置信。
    resource: str = ""
    owner_scoped: bool = False
    # v1.3：专家归因（测试专家系统）：标记该测试点由哪个专家模块产出
    # （"PageExpert" / "CodeExpert"），缺省空串表示非专家产出。旧消费方忽略。
    expert: str = ""
    # v1.6（只增不改，缺省空串＝未标注）：T5 三通道归属，供报告与前端把
    # 「本体 / 影响面 / 运行时」分开计数。
    #   body    = 改动本体（代码或 PRD 展开出来的功能点）
    #   impact  = 影响面回归（origin=impact/impact_file）
    #   runtime = 运行时 UI 发现出来的页面/元素（功能点来自 `runtime:` 前缀）
    # 为什么单独存一列而不是靠 origin 推：origin 回答「怎么来的」，channel 回答
    # 「这份覆盖算在哪一栏」。两者不等价——运行时发现的页面同样 origin=rule，
    # 但报告里必须归到「运行时」而不是「本体」，否则三栏计数会重复或遗漏。
    channel: str = ""
    # v1.6（只增不改，缺省空串＝未标记）：T5 生成后自检（门禁）的落榜原因，可审计。
    # 取值见 engine/quality.py 的 GATE_* 常量：
    #   no_evidence / no_observable_expect / no_origin（本批新增的三道门禁）
    #   best_effort（T3 既有：该维度需特殊环境，诚实标未验证，不是水货）
    # 为空 = 过门禁。与 `unverified` 的分工：`unverified` 回答「能不能直接跑」，
    # `unverified_reason` 回答「为什么」，KPI 据此决定是否计入分母。
    unverified_reason: str = ""
    # v1.7（只增不改，缺省空串＝单仓兼容）：多仓改造，测试点归属仓的项目内短名。
    repo_key: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["contract_version"] = CONTRACT_VERSION
        return d


@dataclass
class CaseSpec:
    """用例规格：测试点落成的「可执行 + 可交付」用例。"""

    tc_no: str
    title: str
    ctype: str  # api / e2e
    steps: list[dict[str, Any]]  # 机器步只在 steps[0]
    module: str = ""
    case_type: str = TPType.NORMAL.value
    priority: str = "P2"
    precondition: str = ""
    doc_steps: list[dict[str, Any]] = field(default_factory=list)
    tp_id: str = ""
    fp_contract_id: str = ""
    fp_row_id: int | None = None
    test_type: str = Tag.FULL.value
    status: str = "generated"
    version: int = 1
    # 覆盖角色（UI 优先策略）：primary=该层优先/唯一覆盖；supplement=接口层用例且对应能力已有 UI 覆盖
    coverage_role: str = ""

    def expect(self) -> str:
        """八要素之「预期结果」：落在 steps[0].expect。"""
        return str(self.steps[0].get("expect", "")) if self.steps else ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["expect"] = self.expect()
        d["contract_version"] = CONTRACT_VERSION
        return d

    def missing_elements(self) -> list[str]:
        """返回缺失的八要素名（空列表 = 八要素齐全）。"""
        mapping: dict[str, Any] = {
            "tc_no": self.tc_no,
            "module": self.module,
            "title": self.title,
            "case_type": self.case_type,
            "priority": self.priority,
            "precondition": self.precondition,
            "doc_steps": self.doc_steps,
            "expect": self.expect(),
        }
        return [k for k in EIGHT_ELEMENTS if not mapping.get(k)]


# ============================================================================
# 派生规则（确定性，不依赖 LLM）
# ============================================================================
def priority_of(category: str, module: str) -> str:
    """优先级规则：异常/安全/边界 → P1；高风险模块 → P1；其余 → P2。"""
    if category in (TPType.ABNORMAL.value, TPType.SECURITY.value, TPType.BOUNDARY.value):
        return "P1"
    if any(k in module for k in HIGH_RISK_MODULE_KEYWORDS):
        return "P1"
    return "P2"


def precondition_of(category: str, layer: str | None = None) -> str:
    """前置条件规则（须与执行层一致）。

    早期实现只看「维度」，不看执行层，导致 UI 层用例的前置条件写着
    「base_url 可达…需持有有效 token」——UI 层既不用 base_url 也不需要 HTTP token，
    执行人员按此准备会无从下手。`layer` 缺省时保持接口层文案（向后兼容）。
    """
    if layer == VerifyLayer.UI.value:
        if category == TPType.SECURITY.value:
            return "被测服务已启动且前端可访问；已准备具备/不具备权限的两个测试账号"
        return "被测服务已启动且前端可访问；已准备可用浏览器（Playwright）与测试账号（如需登录）"
    if category == TPType.ABNORMAL.value:
        return "被测服务已启动且 base_url 可达；已明确合法/非法输入的构造方式"
    return "被测服务已启动，base_url 可达；涉及鉴权接口需持有有效 token（或测试账号可匿名访问）"


def verify_layer_of_ftype(ftype: str) -> str:
    """按来源类型推导执行层：api/business → 接口；page/ui/component → UI。"""
    if ftype in (FType.API.value, FType.BUSINESS.value):
        return VerifyLayer.INTERFACE.value
    return VerifyLayer.UI.value
