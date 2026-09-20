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


def _as_bool(raw: str | None, default: bool) -> bool:
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on", "y", "t")


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
    expert_agent_mode: bool = False  # 预留：自主 Agent 开关（Phase 4 实现）
    # 降级链：专家通道模型不可用（免费额度 AllocationQuota.FreeTierOnly.）时按顺序切换的备选模型。
    expert_model_chain: list[str] = field(default_factory=list)

    # 鉴权：为空表示不校验（仅限内网/开发）
    auth_token: str = ""

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
            "business_extract_mode": self.business_extract_mode,
            "business_include_dirs": self.business_include_dirs,
            "fp_semantic_merge": self.fp_semantic_merge,
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
        }

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.output_dir, self.workspace_root):
            d.mkdir(parents=True, exist_ok=True)


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

    prd_dir_env = g("PRD_DIR")
    # 令牌取值来源：先定环境变量名，再从该名下取真实令牌（令牌本身不入 public_dict）。
    token_env_name = (g("RUNTIME_AUTH_TOKEN_ENV") or "RUNTIME_AUTH_TOKEN").strip()
    s = Settings(
        app_env=_as_choice(g("APP_ENV"), "dev", "APP_ENV", ("dev", "test", "prod")),
        host=g("HOST") or "127.0.0.1",
        port=_as_int(g("PORT"), 8100, "PORT"),
        log_level=(g("LOG_LEVEL") or "INFO").upper(),
        log_format=_as_choice(g("LOG_FORMAT"), "json", "LOG_FORMAT", ("json", "text")),
        readonly_strict=_as_bool(g("READONLY_STRICT"), True),
        review_gate=_as_bool(g("REVIEW_GATE"), False),
        layer_strategy=_as_choice(
            g("LAYER_STRATEGY"), "ui_first", "LAYER_STRATEGY", LAYER_STRATEGY_CHOICES
        ),
        drop_supplement_cases=_as_bool(g("DROP_SUPPLEMENT_CASES"), False),
        llm_enhance=_as_bool(g("LLM_ENHANCE"), False),
        llm_provider=(g("LLM_PROVIDER") or "qwen").strip().lower(),
        llm_base_url=g("LLM_BASE_URL") or "",
        llm_model=g("LLM_MODEL") or "",
        llm_api_key=g("LLM_API_KEY") or "",
        llm_timeout=_as_int(g("LLM_TIMEOUT"), 60, "LLM_TIMEOUT"),
        llm_model_chain=llm_model_chain,
        prd_enabled=_as_bool(g("PRD_ENABLED"), False),
        prd_dir=(Path(prd_dir_env).expanduser().resolve() if prd_dir_env else PROJECT_ROOT / "prd"),
        llm_design_enabled=_as_bool(g("LLM_DESIGN_ENABLED"), False),
        # P3：运行时 UI 发现
        runtime_ui_enabled=_as_bool(g("RUNTIME_UI_ENABLED"), False),
        playwright_headless=_as_bool(g("PLAYWRIGHT_HEADLESS"), True),
        playwright_channel=(g("PLAYWRIGHT_CHANNEL") or "").strip().lower(),
        runtime_base_url=(g("RUNTIME_BASE_URL") or "").strip(),
        runtime_login_url=(g("RUNTIME_LOGIN_URL") or "").strip(),
        runtime_routes=_split_list(g("RUNTIME_ROUTES"), []),
        runtime_ui_timeout=_as_int(g("RUNTIME_UI_TIMEOUT"), 30, "RUNTIME_UI_TIMEOUT"),
        runtime_max_pages=_as_int(g("RUNTIME_MAX_PAGES"), 60, "RUNTIME_MAX_PAGES"),
        runtime_ui_mobile_enabled=_as_bool(g("RUNTIME_UI_MOBILE_ENABLED"), False),
        runtime_login_user=(g("RUNTIME_LOGIN_USER") or "").strip(),
        runtime_login_password=g("RUNTIME_LOGIN_PASSWORD") or "",
        runtime_login_otp=g("RUNTIME_LOGIN_OTP") or "",
        runtime_login_otp_refresh_cmd=g("RUNTIME_LOGIN_OTP_REFRESH_CMD") or "",
        runtime_auth_token=g(token_env_name) or "",
        runtime_auth_token_env=token_env_name,
        executor_enabled=_as_bool(g("EXECUTOR_ENABLED"), False),
        executor_allow_write=_as_bool(g("EXECUTOR_ALLOW_WRITE"), False),
        # 测试专家系统（凭证默认复用 LLM 通道 deepseek，未单独配时与 llm_* 同值，开箱即用）
        expert_mode=_as_bool(g("EXPERT_MODE"), True),
        expert_vision=_as_bool(g("EXPERT_VISION"), True),
        expert_provider=(g("EXPERT_PROVIDER") or "deepseek").strip().lower(),
        expert_base_url=(g("EXPERT_BASE_URL") or g("LLM_BASE_URL") or "").strip(),
        expert_model=(g("EXPERT_MODEL") or g("LLM_MODEL") or "").strip(),
        expert_api_key=(g("EXPERT_API_KEY") or g("LLM_API_KEY") or "").strip(),
        expert_timeout=_as_int(g("EXPERT_TIMEOUT"), 60, "EXPERT_TIMEOUT"),
        expert_max_tps_per_page=_as_int(g("EXPERT_MAX_TPS_PER_PAGE"), 8, "EXPERT_MAX_TPS_PER_PAGE"),
        expert_agent_mode=_as_bool(g("EXPERT_AGENT_MODE"), False),  # 预留：自主 Agent 开关
        expert_model_chain=expert_model_chain,
        auth_token=g("AUTH_TOKEN") or "",
        business_extract_mode=_as_choice(
            g("BUSINESS_EXTRACT_MODE"),
            BUSINESS_EXTRACT_STRICT,
            "BUSINESS_EXTRACT_MODE",
            BUSINESS_EXTRACT_CHOICES,
        ),
        fp_semantic_merge=_as_bool(g("FP_SEMANTIC_MERGE"), True),
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
