"""集中配置：全部来自环境变量，并在启动时**集中校验、快速失败**。

规则：
- 密钥只允许出现在 `.env` / 环境变量中，**绝不硬编码**、绝不写日志；
- 非法值（如 PORT 非数字）在启动期即抛 `ConfigError`，不拖到运行期炸；
- 只提交 `.env.example`（占位值），真实 `.env` 由 `.gitignore` 排除。

P3 凭证红线（见 `P3_UI生成_详细设计.md` §4.2）：
- `runtime_login_password` / `runtime_auth_token` **绝不进入** `public_dict()`、
  日志、产物 JSON、用例文本与数据库；
- `public_dict()` 对凭证只输出 `*_configured` 布尔值。
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from core.enums import (
    BUSINESS_EXTRACT_CHOICES,
    BUSINESS_EXTRACT_STRICT,
    LAYER_STRATEGY_CHOICES,
)
from core.errors import ConfigError


# 项目根（core/ 的上一级）
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    """尽力加载 .env；python-dotenv 缺失时静默跳过（不阻断启动）。"""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(PROJECT_ROOT / ".env", override=False)


_TRUE_VALUES = ("1", "true", "yes", "on", "y", "t")
_FALSE_VALUES = ("0", "off", "false", "no", "n", "f")


def _as_bool(raw: str | None, default: bool, name: str = "布尔开关") -> bool:
    """解析布尔环境变量：空值回落默认，**非空非白名单值启动期抛 ConfigError**。

    A7（P1）：旧实现对 `ture`/`enabled`/`2` 之类拼写错误静默回落默认值，
    导致「配了以为生效、实际整条链路没跑」却毫无报错——违背本模块
    "非法值启动期快速失败"的承诺。现在只有真正未配置（None/空串）才回落，
    配置了却无法识别的值一律拒绝启动，把隐性行为偏差挡在启动期。
    """
    if raw is None or raw == "":
        return default
    val = raw.strip().lower()
    if not val:  # 纯空格等同未配置（兼容旧实现的回落行为）
        return default
    if val in _TRUE_VALUES:
        return True
    if val in _FALSE_VALUES:
        return False
    raise ConfigError(
        f"环境变量 {name} 取值非法：{raw!r}，允许 {list(_TRUE_VALUES) + list(_FALSE_VALUES)}"
    )


_CARD_NAV_MODES = ("off", "auto", "on")


def _as_card_nav_mode(mode_raw: str | None, legacy_raw: str | None) -> str:
    """卡片导航三态解析：off（强制关）/ auto（自动判定）/ on（强制开）。

    优先读新变量 RUNTIME_UI_CARD_NAV_MODE；未设置时兼容旧 RUNTIME_UI_CARD_NAV_ENABLED
    （显式 true→on、false→off）；两者都没有则走新默认 auto。非法值安全回落到 auto。
    """
    mode = (mode_raw or "").strip().lower()
    if mode in _CARD_NAV_MODES:
        return mode
    if legacy_raw is not None and str(legacy_raw).strip() != "":
        # legacy 变量是兼容通道（新变量优先），非法值按旧语义宽容回落 off，
        # 不走 _as_bool 的严格版——避免升级时遗留的旧变量值卡住启动。
        return "on" if str(legacy_raw).strip().lower() in _TRUE_VALUES else "off"
    return "auto"


def _as_int(raw: str | None, default: int, name: str) -> int:
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"环境变量 {name} 必须是整数，实际为 {raw!r}") from exc


def _as_choice(raw: str | None, default: str, name: str, allowed: tuple[str, ...]) -> str:
    val = (raw or default).strip().lower()
    if val not in allowed:
        raise ConfigError(f"环境变量 {name} 取值非法：{val!r}，允许 {allowed}")
    return val


def _as_semantic_dims(raw: str | None, default: str) -> str:
    """解析 T3 灰度开关：`off` / `on` / `project:<id>[,<id>]`（非法值启动期报错）。

    为什么不像其它布尔开关那样只认 on/off：关键词规则必有误匹配，一次性全量开放
    会把所有项目的维度都改掉、无法归因；先单项目抽检（默认 project:30），
    确认误匹配率达标后再改 `on`。
    """
    val = (raw or default).strip().lower().replace(" ", "")
    if not val:
        return "off"
    if val in ("off", "on"):
        return val
    if val.startswith("project:"):
        ids = [p for p in val.split(":", 1)[1].split(",") if p]
        if ids and all(p.isdigit() for p in ids):
            return "project:" + ",".join(ids)
    raise ConfigError(
        f"环境变量 SEMANTIC_DIMS 取值非法：{raw!r}，允许 off / on / project:<项目ID>[,<项目ID>]"
    )


def _split_list(raw: str | None, default: list[str]) -> list[str]:
    """将逗号分隔的环境变量解析为去空白、去空字符串的列表。"""
    if not raw:
        return list(default)
    return [p.strip() for p in raw.split(",") if p.strip()]


@dataclass
class Settings:
    """运行时配置快照（启动时构造一次）。"""

    app_env: str = "dev"
    host: str = "127.0.0.1"
    port: int = 8100

    log_level: str = "INFO"
    log_format: str = "json"

    data_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "data")
    output_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "outputs")
    db_path: Path = field(default_factory=lambda: PROJECT_ROOT / "data" / "testgen.db")
    workspace_root: Path = field(default_factory=lambda: PROJECT_ROOT / "data" / "workspaces")

    # 只读工作区：落盘后是否强制置只读（chmod）
    readonly_strict: bool = True

    # 审核门：开启后新产出的功能点/测试点 review_status=pending
    review_gate: bool = False

    # 覆盖层策略（UI 优先）：ui_first=UI 为主、接口仅补充；all=两层独立全量（旧行为）
    layer_strategy: str = "ui_first"
    # ui_first 下是否直接丢弃「接口补充」用例（默认保留并标记 supplement，便于回退）
    drop_supplement_cases: bool = False

    # LLM 增强通道（默认关闭：规则引擎必须先能独立跑通）
    llm_enhance: bool = False
    llm_provider: str = "qwen"
    llm_base_url: str = ""
    llm_model: str = ""
    llm_api_key: str = ""
    llm_timeout: int = 60
    # M18：/describe 的总时间预算（秒）。组间并发后仍需整体兜底——超预算的未完成组
    # 按「回退展示符号名」降级，绝不让单个请求把前端树加载挂死。
    describe_budget_seconds: int = 90
    # A6（P1）：/execute 执行链路的总时长预算（秒），0=不限（保持旧行为）。
    # 4066 条全量执行时防止挂死：超预算的未执行用例如实 skipped（不伪装通过），
    # 并把 budget_exhausted 写进汇总，绝不静默丢弃。
    exec_budget_seconds: int = 0
    # 降级链：LLM 增强通道模型不可用（免费额度 AllocationQuota.FreeTierOnly.）时按顺序切换的备选模型。
    # 未设 LLM_MODEL_CHAIN 时回退复用专家链（同一 key）。
    llm_model_chain: list[str] = field(default_factory=list)

    # 业务函数提取模式（P1 · 收窄）：strict=排除测试/脚手架/构建脚本/配置模式类文件中的
    # 业务函数；loose=保留全部（legacy 行为）。详见 qa-test-points 技能 §十四。
    business_extract_mode: str = BUSINESS_EXTRACT_STRICT
    # strict 模式下仍保留为独立业务能力的目录白名单（相对仓库根，env 逗号分隔覆盖）。
    # 这些目录被显式认定为「独立能力来源」（工具/智能体/MCP 服务/技能）。
    business_include_dirs: list[str] = field(
        default_factory=lambda: ["tools", "agents", "mcp_servers", "workspace/skills"]
    )

    # F5：功能点语义合并（口径见 engine/fp_merge.py）。开=同一语义的多条功能点收敛为一条，
    # 避免「真实后端 + mock server / 静态 + 运行时」重复计数；关=保留全部原始功能点（对照用）。
    fp_semantic_merge: bool = True

    # T1：改动归属。私有函数 / 被 F6 吞并的辅助函数被改动时**根本没有功能点**，
    # 函数级收窄会把这次改动整体丢掉（表现为「后端改了却一条用例都没有」）。开启后
    # 按同文件反向调用图向上找最近入口（路由 / lifespan），把入口功能点补回保留集。
    # `change_attribution_max_entries` 控制最多补几个入口：默认 1 个代表入口，
    # 其余入口的回归覆盖交给影响面通道（engine/impact.py），避免 20 个调用点炸成 20 条。
    change_attribution: bool = True
    change_attribution_max_entries: int = 1

    # T2：影响面回归。被改函数的调用入口（同文件反向调用图 ≤2 跳）不是「改动本体」，
    # 但改动可能让它们回归，需要单独出「因 X 改动，验证 Y 未回归」的用例。
    # 预算两道闸：单函数最多影响 impact_max_per_func 个入口、整次最多 impact_max_total 条，
    # 超出部分进报告 truncated（不是静默丢弃）。
    impact_enabled: bool = True
    impact_max_per_func: int = 3
    impact_max_total: int = 15

    # T3：语义维度（变更语义 → 维度路由）。原问题是「改分页也一律测交互元素可用」，
    # 因为维度只由来源类型（API/页面/组件）决定。开启后按 diff hunk 识别 8 类变更语义
    # （分页/去重/列表新增/分支模式/契约形状/兜底/零值守卫/鉴权），映射到具体维度。
    # 灰度取值：`off` 全关 / `on` 全开 / `project:30` 只在指定项目开（默认，关键词规则
    # 必有误匹配，先单项目抽检再放量）。`semantic_max_dims` 是每功能点维度总上限。
    semantic_dims: str = "project:30"
    semantic_max_dims: int = 4

    # T4：前端跨文件。两件独立开关——
    # `route_infer`：用仓库静态路由表给「pages/ 下但没写 path」的页面反查真实路由，
    #   命中后该页面才能展开「可达/未授权/极端视口」等需要地址的维度（否则只有交互一维）。
    # `file_impact_*`：前端没有 AST 反向调用图，用 import 边补「引用了被改文件的父组件」
    #   文件级回归用例；`max_total` 是硬闸（默认 2），防止一次改动刷出几十条空壳用例。
    route_infer: bool = True
    # 批次 B：`route_infer` 只在增量模式放开地址类维度（全量会炸量）；
    # `route_file_hint` 则只把**已被路由表 import 证据绑定**的屏幕补上 route_hint，
    # 不改 has_route / 不新增维度——让文件级用例拿到可执行的 URL，而不是假文件名。
    route_file_hint: bool = True
    file_impact_enabled: bool = True
    file_impact_max_total: int = 2

    # T5：生成后自检（门禁）。开启时逐条校验「有证据 / 预期有可观测对象 / 有来源标签」，
    # 不合规的置 unverified 并记原因，且不计入三个 KPI 的分子——KPI 虚高比 KPI 难看更危险。
    # 关闭只影响标记与 KPI 分子，不影响用例生成（可一键回滚到 T4 的行为做对照）。
    quality_gate: bool = True

    # P2：PRD 通道（需求文档 / OpenAPI 作为用例设计的补充来源）
    prd_enabled: bool = False
    prd_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "prd")
    llm_design_enabled: bool = False  # 在语义增强之外，叠加 LLM 用例设计

    # P3：运行时浏览器 UI 发现 / 执行（默认关闭：本服务仍以代码静态分析为主）
    runtime_ui_enabled: bool = False
    playwright_headless: bool = True
    # 浏览器通道：空=用 Playwright 自带 chromium；"msedge"/"chrome"=复用系统浏览器
    # （沙箱内无法下载 chromium 内核时，复用系统 Edge 可免 150MB 下载）
    playwright_channel: str = ""
    runtime_base_url: str = ""  # 被测环境地址（由用户提供，经 .env 注入）
    runtime_login_url: str = ""  # 登录页地址；为空则回退到 base_url
    runtime_routes: list[str] = field(default_factory=list)  # 显式路由清单（逗号分隔）
    runtime_ui_timeout: int = 30  # 单页/单步超时（秒）
    runtime_max_pages: int = 60  # 单次发现遍历的页面上限（防爆）
    runtime_ui_mobile_enabled: bool = False  # G-7：移动端视口发现开关（默认关）
    # 批次 D（P3）：卡片式导航启发式（点击主内容区卡片驱动 router.push）。
    # 三态：off=强制关 / auto=按发现结果自动判定（默认）/ on=强制开。
    # auto 时若静态路由/显式 routes/菜单发现任一有结果则不点卡片（避免副作用），
    # 三者皆空才启用兜底；任何开启态都带白名单+危险过滤+预算+URL 变化确认与误报报告。
    runtime_ui_card_nav_mode: str = "auto"
    # 凭证：只从环境变量读取，绝不入库/入日志/入产物（见模块 docstring 红线）
    runtime_login_user: str = ""
    runtime_login_password: str = ""
    runtime_login_otp: str = ""  # 动态口令 / 一次性验证码（不入库 / 不入日志）
    runtime_login_otp_refresh_cmd: str = ""  # G-10：OTP 过期时取新 OTP 的命令（可选）
    runtime_auth_token: str = ""  # 值取自 runtime_auth_token_env 指向的环境变量
    runtime_auth_token_env: str = "RUNTIME_AUTH_TOKEN"  # 令牌来源环境变量名（不入库）
    executor_enabled: bool = False  # P3：用例执行器（接口探活 / 浏览器交互）
    # A3：是否放行写操作（POST/PUT/PATCH/DELETE）。默认否——写操作会真实变更被测环境数据，
    # 必须由使用者显式确认环境可写后才放行，否则接口层只执行 GET/HEAD 等只读请求。
    executor_allow_write: bool = False

    # ===== 测试专家系统（URL 通道 PageExpert / 代码通道 CodeExpert）=====
    # D3：expert_mode 默认开；CLI `--expert-off` 可整体关闭，降级到纯规则基线。
    # D1：expert_vision 多模态截图；模型非视觉（如 deepseek-chat）时自动降级 DOM 并 notes。
    # D4：expert_agent_mode 自主 Agent（本期仅留配置开关，不实现）。
    # 凭证默认复用 LLM 通道（deepseek），未单独配时与 llm_* 同值，开箱即用。
    expert_mode: bool = True
    expert_vision: bool = True
    expert_provider: str = "deepseek"
    expert_base_url: str = ""
    expert_model: str = ""
    expert_api_key: str = ""
    expert_timeout: int = 60
    expert_max_tps_per_page: int = 8
    # 成本/时长护栏：PageExpert 每页一次大模型调用（实测 50~170s/页，模型链降级会叠加多次
    # 超时重试）。39 页串行 = 半小时以上，面板表现为「一直不结束」。双上限封顶：
    # 最多审 N 页 + 总时间预算（秒）。默认值刻意压到很小：3 页 / 90 秒，整体约 2~3 分钟收尾；
    # 想多审几页再显式调 EXPERT_MAX_PAGES / EXPERT_TIME_BUDGET_SEC。
    expert_max_pages: int = 3
    expert_time_budget_sec: int = 90
    expert_agent_mode: bool = False  # 预留：自主 Agent 开关（Phase 4 实现）
    # 降级链：专家通道模型不可用（免费额度 AllocationQuota.FreeTierOnly.）时按顺序切换的备选模型。
    expert_model_chain: list[str] = field(default_factory=list)

    # 鉴权：为空表示不校验（仅限内网/开发）
    auth_token: str = ""
    # H8 修复：fail-closed 逃生口。当且仅当未配置 AUTH_TOKEN 且显式 AUTH_DISABLED=1 时
    # 才放行未鉴权请求；否则「未配令牌也未显式关闭」一律拒绝（默认安全）。
    auth_disabled: bool = False

    def public_dict(self) -> dict[str, object]:
        """可安全输出的配置摘要（**不含任何密钥/密码**）。"""
        return {
            "app_env": self.app_env,
            "host": self.host,
            "port": self.port,
            "log_level": self.log_level,
            "log_format": self.log_format,
            "db_path": str(self.db_path),
            "output_dir": str(self.output_dir),
            "workspace_root": str(self.workspace_root),
            "readonly_strict": self.readonly_strict,
            "review_gate": self.review_gate,
            "layer_strategy": self.layer_strategy,
            "drop_supplement_cases": self.drop_supplement_cases,
            "llm_enhance": self.llm_enhance,
            "llm_provider": self.llm_provider,
            "llm_model": self.llm_model,
            "llm_model_chain": self.llm_model_chain,
            "llm_key_configured": bool(self.llm_api_key),
            "auth_enabled": bool(self.auth_token),
            "auth_disabled": self.auth_disabled,
            "business_extract_mode": self.business_extract_mode,
            "business_include_dirs": self.business_include_dirs,
            "fp_semantic_merge": self.fp_semantic_merge,
            "change_attribution": self.change_attribution,
            "change_attribution_max_entries": self.change_attribution_max_entries,
            "impact_enabled": self.impact_enabled,
            "impact_max_per_func": self.impact_max_per_func,
            "impact_max_total": self.impact_max_total,
            "semantic_dims": self.semantic_dims,
            "semantic_max_dims": self.semantic_max_dims,
            "route_infer": self.route_infer,
            "route_file_hint": self.route_file_hint,
            "file_impact_enabled": self.file_impact_enabled,
            "file_impact_max_total": self.file_impact_max_total,
            "quality_gate": self.quality_gate,
            # P2
            "prd_enabled": self.prd_enabled,
            "prd_dir": str(self.prd_dir),
            "llm_design_enabled": self.llm_design_enabled,
            # P3：凭证一律只输出「是否已配置」，绝不输出取值
            "runtime_ui_enabled": self.runtime_ui_enabled,
            "runtime_base_url": self.runtime_base_url,
            "runtime_login_url": self.runtime_login_url,
            "runtime_routes": self.runtime_routes,
            "runtime_ui_timeout": self.runtime_ui_timeout,
            "runtime_max_pages": self.runtime_max_pages,
            "runtime_ui_card_nav_mode": self.runtime_ui_card_nav_mode,
            "runtime_ui_mobile_enabled": self.runtime_ui_mobile_enabled,  # C1：面板可见
            "playwright_headless": self.playwright_headless,
            "playwright_channel": self.playwright_channel,
            "executor_enabled": self.executor_enabled,
            "executor_allow_write": self.executor_allow_write,
            "runtime_login_configured": bool(
                self.runtime_login_user and self.runtime_login_password
            ),
            "runtime_otp_configured": bool(self.runtime_login_otp),
            "runtime_token_configured": bool(self.runtime_auth_token),
            # 测试专家系统
            "expert_mode": self.expert_mode,
            "expert_vision": self.expert_vision,
            "expert_enabled": bool(
                self.expert_mode
                and self.expert_api_key
                and self.expert_base_url
                and self.expert_model
            ),
            "expert_agent_mode": self.expert_agent_mode,
            "expert_model_chain": self.expert_model_chain,
            "expert_max_pages": self.expert_max_pages,
            "expert_time_budget_sec": self.expert_time_budget_sec,
        }

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.output_dir, self.workspace_root):
            d.mkdir(parents=True, exist_ok=True)

    def semantic_dims_enabled(self, project_id: int | None) -> bool:
        """T3 灰度判定：`off` 全关 / `on` 全开 / `project:30,31` 仅列出的项目开。"""
        spec = (self.semantic_dims or "").strip().lower()
        if not spec or spec == "off":
            return False
        if spec == "on":
            return True
        if spec.startswith("project:"):
            ids = {p.strip() for p in spec.split(":", 1)[1].split(",") if p.strip()}
            return str(project_id) in ids if project_id is not None else False
        return False


def _apply_path_overrides(s: Settings, g: Callable[[str], str | None]) -> None:
    """把目录类环境变量覆盖到已构造的 Settings 上（DB 默认跟随 data_dir）。"""
    if g("DATA_DIR"):
        s.data_dir = Path(g("DATA_DIR") or "").expanduser().resolve()
    if g("OUTPUT_DIR"):
        s.output_dir = Path(g("OUTPUT_DIR") or "").expanduser().resolve()
    if g("WORKSPACE_ROOT"):
        s.workspace_root = Path(g("WORKSPACE_ROOT") or "").expanduser().resolve()
    db_path_env = g("DB_PATH")
    s.db_path = (
        Path(db_path_env).expanduser().resolve() if db_path_env else s.data_dir / "testgen.db"
    )


def _validate_b9(s: Settings) -> None:
    """B9：数值/枚举/URL 配置校验（从 _validate 拆出，避免单函数分支过多）。"""
    if s.describe_budget_seconds <= 0:
        raise ConfigError(f"DESCRIBE_BUDGET_SECONDS 必须为正整数，实际 {s.describe_budget_seconds}")
    for name, value in (
        ("EXPERT_TIMEOUT", s.expert_timeout),
        ("EXPERT_MAX_TPS_PER_PAGE", s.expert_max_tps_per_page),
        ("EXPERT_MAX_PAGES", s.expert_max_pages),
        ("EXPERT_TIME_BUDGET_SEC", s.expert_time_budget_sec),
    ):
        if value <= 0:
            raise ConfigError(f"{name} 必须为正整数，实际 {value}")
    # 浏览器通道白名单：空=自带 chromium；只允许 msedge / chrome
    if s.playwright_channel and s.playwright_channel not in ("msedge", "chrome"):
        raise ConfigError(
            f"PLAYWRIGHT_CHANNEL 取值非法：{s.playwright_channel!r}，允许空 / msedge / chrome"
        )
    # base_url 非空时必须有 http(s) scheme，否则 Playwright/requests 运行期才失败
    for name, url in (
        ("RUNTIME_BASE_URL", s.runtime_base_url),
        ("RUNTIME_LOGIN_URL", s.runtime_login_url),
    ):
        if url and not url.lower().startswith(("http://", "https://")):
            raise ConfigError(f"{name} 必须是 http:// 或 https:// 开头的地址，实际 {url!r}")


def _validate(s: Settings) -> None:
    """启动期集中校验：非法即抛 ConfigError（快速失败，不拖到运行期）。"""
    if s.llm_enhance and not s.llm_api_key:
        raise ConfigError("LLM_ENHANCE=on 时必须提供 LLM_API_KEY")
    if s.port <= 0 or s.port > 65535:
        raise ConfigError(f"PORT 越界：{s.port}")
    # P3 快速失败：开了运行时 UI 发现却没有被测地址，等于白跑（宁启动期报错，不静默跳过）
    if s.runtime_ui_enabled and not s.runtime_base_url:
        raise ConfigError("RUNTIME_UI_ENABLED=on 时必须提供 RUNTIME_BASE_URL（被测环境地址）")
    if s.runtime_ui_timeout <= 0:
        raise ConfigError(f"RUNTIME_UI_TIMEOUT 必须为正整数秒，实际 {s.runtime_ui_timeout}")
    if s.runtime_max_pages <= 0:
        raise ConfigError(f"RUNTIME_MAX_PAGES 必须为正整数，实际 {s.runtime_max_pages}")
    if s.semantic_max_dims <= 0:
        raise ConfigError(f"SEMANTIC_MAX_DIMS 必须为正整数，实际 {s.semantic_max_dims}")
    if s.file_impact_max_total < 0:
        raise ConfigError(
            f"FILE_IMPACT_MAX_TOTAL 不能为负，实际 {s.file_impact_max_total}（0 = 关闭该通道）"
        )
    if s.exec_budget_seconds < 0:
        raise ConfigError(f"EXEC_BUDGET_SECONDS 不能为负，实际 {s.exec_budget_seconds}（0 = 不限）")
    _validate_b9(s)


def load_settings(env: dict[str, str] | None = None) -> Settings:
    """从环境变量构造 Settings；非法即抛 ConfigError。"""
    if env is None:
        _load_dotenv()
        env = dict(os.environ)

    def g(key: str) -> str | None:
        return env.get(key)

    # 降级链解析：未显式设 *_MODEL_CHAIN 时，以主模型为单元素链；
    # LLM 通道未设则复用专家链（同一 key，统一顺序）。
    _expert_model = (g("EXPERT_MODEL") or g("LLM_MODEL") or "").strip()
    _llm_model = (g("LLM_MODEL") or "").strip()
    expert_model_chain = _split_list(
        g("EXPERT_MODEL_CHAIN"),
        [_expert_model] if _expert_model else [],
    )
    llm_model_chain = _split_list(
        g("LLM_MODEL_CHAIN"),
        expert_model_chain if expert_model_chain else ([_llm_model] if _llm_model else []),
    )
    # 未显式配置 LLM_MODEL_CHAIN 时，从 LLM_MODEL_REGISTRY 自动推导文本链路
    # （文本可用 + 开启 + 未过期，按到期升序：用完即停，先消耗快过期的）。
    if not llm_model_chain:
        try:
            from core.llm_budget import evaluate_registry

            llm_model_chain = [v["model"] for v in evaluate_registry() if v["usable_for_text"]]
        except (ImportError, KeyError, ValueError, OSError):  # 推导失败不阻塞启动，退回兜底
            llm_model_chain = []
    else:
        # 显式配置的链同样按到期时刻重排：手写顺序不该压过「先消耗快到期的」原则。
        try:
            from core.llm_budget import sort_chain_by_expiry

            llm_model_chain = sort_chain_by_expiry(llm_model_chain)
        except (ImportError, KeyError, ValueError, OSError):  # 排序失败不阻塞启动，沿用书写顺序
            pass

    prd_dir_env = g("PRD_DIR")
    # 令牌取值来源：先定环境变量名，再从该名下取真实令牌（令牌本身不入 public_dict）。
    token_env_name = (g("RUNTIME_AUTH_TOKEN_ENV") or "RUNTIME_AUTH_TOKEN").strip()
    s = Settings(
        app_env=_as_choice(g("APP_ENV"), "dev", "APP_ENV", ("dev", "test", "prod")),
        host=g("HOST") or "127.0.0.1",
        port=_as_int(g("PORT"), 8100, "PORT"),
        log_level=(g("LOG_LEVEL") or "INFO").upper(),
        log_format=_as_choice(g("LOG_FORMAT"), "json", "LOG_FORMAT", ("json", "text")),
        readonly_strict=_as_bool(g("READONLY_STRICT"), True, "READONLY_STRICT"),
        review_gate=_as_bool(g("REVIEW_GATE"), False, "REVIEW_GATE"),
        layer_strategy=_as_choice(
            g("LAYER_STRATEGY"), "ui_first", "LAYER_STRATEGY", LAYER_STRATEGY_CHOICES
        ),
        drop_supplement_cases=_as_bool(g("DROP_SUPPLEMENT_CASES"), False, "DROP_SUPPLEMENT_CASES"),
        llm_enhance=_as_bool(g("LLM_ENHANCE"), False, "LLM_ENHANCE"),
        llm_provider=(g("LLM_PROVIDER") or "qwen").strip().lower(),
        llm_base_url=g("LLM_BASE_URL") or "",
        llm_model=g("LLM_MODEL") or "",
        llm_api_key=g("LLM_API_KEY") or "",
        llm_timeout=_as_int(g("LLM_TIMEOUT"), 60, "LLM_TIMEOUT"),
        describe_budget_seconds=_as_int(
            g("DESCRIBE_BUDGET_SECONDS"), 90, "DESCRIBE_BUDGET_SECONDS"
        ),
        exec_budget_seconds=_as_int(g("EXEC_BUDGET_SECONDS"), 0, "EXEC_BUDGET_SECONDS"),
        llm_model_chain=llm_model_chain,
        prd_enabled=_as_bool(g("PRD_ENABLED"), False, "PRD_ENABLED"),
        prd_dir=(Path(prd_dir_env).expanduser().resolve() if prd_dir_env else PROJECT_ROOT / "prd"),
        llm_design_enabled=_as_bool(g("LLM_DESIGN_ENABLED"), False, "LLM_DESIGN_ENABLED"),
        # P3：运行时 UI 发现
        runtime_ui_enabled=_as_bool(g("RUNTIME_UI_ENABLED"), False, "RUNTIME_UI_ENABLED"),
        playwright_headless=_as_bool(g("PLAYWRIGHT_HEADLESS"), True, "PLAYWRIGHT_HEADLESS"),
        playwright_channel=(g("PLAYWRIGHT_CHANNEL") or "").strip().lower(),
        runtime_base_url=(g("RUNTIME_BASE_URL") or "").strip(),
        runtime_login_url=(g("RUNTIME_LOGIN_URL") or "").strip(),
        runtime_routes=_split_list(g("RUNTIME_ROUTES"), []),
        runtime_ui_timeout=_as_int(g("RUNTIME_UI_TIMEOUT"), 30, "RUNTIME_UI_TIMEOUT"),
        runtime_max_pages=_as_int(g("RUNTIME_MAX_PAGES"), 60, "RUNTIME_MAX_PAGES"),
        runtime_ui_mobile_enabled=_as_bool(
            g("RUNTIME_UI_MOBILE_ENABLED"), False, "RUNTIME_UI_MOBILE_ENABLED"
        ),
        runtime_ui_card_nav_mode=_as_card_nav_mode(
            g("RUNTIME_UI_CARD_NAV_MODE"), g("RUNTIME_UI_CARD_NAV_ENABLED")
        ),
        runtime_login_user=(g("RUNTIME_LOGIN_USER") or "").strip(),
        runtime_login_password=g("RUNTIME_LOGIN_PASSWORD") or "",
        runtime_login_otp=g("RUNTIME_LOGIN_OTP") or "",
        runtime_login_otp_refresh_cmd=g("RUNTIME_LOGIN_OTP_REFRESH_CMD") or "",
        runtime_auth_token=g(token_env_name) or "",
        runtime_auth_token_env=token_env_name,
        executor_enabled=_as_bool(g("EXECUTOR_ENABLED"), False, "EXECUTOR_ENABLED"),
        executor_allow_write=_as_bool(g("EXECUTOR_ALLOW_WRITE"), False, "EXECUTOR_ALLOW_WRITE"),
        # 测试专家系统（凭证默认复用 LLM 通道 deepseek，未单独配时与 llm_* 同值，开箱即用）
        expert_mode=_as_bool(g("EXPERT_MODE"), True, "EXPERT_MODE"),
        expert_vision=_as_bool(g("EXPERT_VISION"), True, "EXPERT_VISION"),
        expert_provider=(g("EXPERT_PROVIDER") or "deepseek").strip().lower(),
        expert_base_url=(g("EXPERT_BASE_URL") or g("LLM_BASE_URL") or "").strip(),
        expert_model=(g("EXPERT_MODEL") or g("LLM_MODEL") or "").strip(),
        expert_api_key=(g("EXPERT_API_KEY") or g("LLM_API_KEY") or "").strip(),
        expert_timeout=_as_int(g("EXPERT_TIMEOUT"), 60, "EXPERT_TIMEOUT"),
        expert_max_tps_per_page=_as_int(g("EXPERT_MAX_TPS_PER_PAGE"), 8, "EXPERT_MAX_TPS_PER_PAGE"),
        expert_max_pages=_as_int(g("EXPERT_MAX_PAGES"), 3, "EXPERT_MAX_PAGES"),
        expert_time_budget_sec=_as_int(g("EXPERT_TIME_BUDGET_SEC"), 90, "EXPERT_TIME_BUDGET_SEC"),
        expert_agent_mode=_as_bool(
            g("EXPERT_AGENT_MODE"), False, "EXPERT_AGENT_MODE"
        ),  # 预留：自主 Agent 开关
        expert_model_chain=expert_model_chain,
        auth_token=g("AUTH_TOKEN") or "",
        auth_disabled=_as_bool(g("AUTH_DISABLED"), False, "AUTH_DISABLED"),
        business_extract_mode=_as_choice(
            g("BUSINESS_EXTRACT_MODE"),
            BUSINESS_EXTRACT_STRICT,
            "BUSINESS_EXTRACT_MODE",
            BUSINESS_EXTRACT_CHOICES,
        ),
        fp_semantic_merge=_as_bool(g("FP_SEMANTIC_MERGE"), True, "FP_SEMANTIC_MERGE"),
        change_attribution=_as_bool(g("CHANGE_ATTRIBUTION"), True, "CHANGE_ATTRIBUTION"),
        change_attribution_max_entries=_as_int(
            g("CHANGE_ATTRIBUTION_MAX_ENTRIES"), 1, "CHANGE_ATTRIBUTION_MAX_ENTRIES"
        ),
        impact_enabled=_as_bool(g("IMPACT_ENABLED"), True, "IMPACT_ENABLED"),
        impact_max_per_func=_as_int(g("IMPACT_MAX_PER_FUNC"), 3, "IMPACT_MAX_PER_FUNC"),
        impact_max_total=_as_int(g("IMPACT_MAX_TOTAL"), 15, "IMPACT_MAX_TOTAL"),
        # T3：兼容任务文档写的 TESTGEN_SEMANTIC_DIMS，主变量名跟本项目其它开关一致
        semantic_dims=_as_semantic_dims(
            g("SEMANTIC_DIMS") or g("TESTGEN_SEMANTIC_DIMS"), "project:30"
        ),
        semantic_max_dims=_as_int(g("SEMANTIC_MAX_DIMS"), 4, "SEMANTIC_MAX_DIMS"),
        route_infer=_as_bool(g("ROUTE_INFER"), True, "ROUTE_INFER"),
        route_file_hint=_as_bool(g("ROUTE_FILE_HINT"), True, "ROUTE_FILE_HINT"),
        file_impact_enabled=_as_bool(g("FILE_IMPACT_ENABLED"), True, "FILE_IMPACT_ENABLED"),
        file_impact_max_total=_as_int(g("FILE_IMPACT_MAX_TOTAL"), 2, "FILE_IMPACT_MAX_TOTAL"),
        quality_gate=_as_bool(g("QUALITY_GATE"), True, "QUALITY_GATE"),
        business_include_dirs=_split_list(
            g("BUSINESS_INCLUDE_DIRS"),
            ["tools", "agents", "mcp_servers", "workspace/skills"],
        ),
    )

    _apply_path_overrides(s, g)
    _validate(s)
    return s


_settings: Settings | None = None


def get_settings() -> Settings:
    """进程内单例（首次调用即校验）。"""
    global _settings
    if _settings is None:
        _settings = load_settings()
        _settings.ensure_dirs()
    return _settings


def reset_settings() -> None:
    """测试用：清空单例。"""
    global _settings
    _settings = None
