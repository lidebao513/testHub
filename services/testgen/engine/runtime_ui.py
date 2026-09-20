"""引擎 · 模块十（P3）：运行时浏览器 UI 发现。

设计定位：在「代码静态分析得到的 UI 功能点」之外，提供运行时验证通道——
用 Playwright 打开被测环境地址（用户提供 + 凭证经 .env 注入），真实渲染页面、
抓取可交互元素、记录控制台错误，反向补全 / 校验 UI 测试点。

依赖方向严格向下（只 import core），不感知 service / cli。
Playwright 采用**惰性导入**：未安装时不影响主链路（默认关闭），
由 `has_playwright()` 探测，`discover_ui` 在缺失时给出可执行的安装指引。

里程碑：
- M3.1 配置与凭证模型 + 惰性导入探测；
- M3.2 表单登录 + 单页抓取 + 路由来源优先级；
- M3.3 三字段登录（账号 / 密码 / 动态口令）+ SPA 菜单点击路由发现 + 受保护页遍历；
- M3.4 `to_functional_points` 把发现结果转成 `FunctionalPoint`，并由 pipeline
  与静态功能点**合并去重**后参与测试点展开与用例生成。

凭证红线（见 `P3_UI生成_详细设计.md` §4.2）：
密码/令牌/动态口令只从环境变量读取，**不进日志、不进产物、不进数据库**——本模块所有
日志调用点均不携带凭证字段，结果 note 也不回显密码与动态口令。

⚠️ SPA 实战教训（一个真实 React SPA 实测，三条都会让菜单发现静默得到 0 条）：
1. **菜单在 SPA 外壳里跨路由常驻**——不要「每项重新打开页面」再抓句柄；单会话内点击、
   按「文本 → 索引」重新定位即可（重开还会撞上首屏异步挂载，句柄为空）。
2. **不要用很短的预算等导航挂载**：首批菜单常要等后端接口返回才渲染，预算太紧会误判
   「本页没有导航」从而直接放弃。
3. **不要把所有菜单的 `href` 当成真实路由**：SPA 里 `href` 可能是占位值或全部同值，
   只有真实点击才驱动路由库跳转——故取路由必须**点击优先**，`href` 仅作兜底。
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse

from core.contracts import FunctionalPoint, fp_id_of
from core.enums import RUNTIME_UI_MODE_CHOICES, RUNTIME_UI_PLAYWRIGHT, FType
from core.errors import EngineError
from core.log import get_logger, log_extra


log = get_logger(__name__)


# ============================================================================
# 常量（选择器与上限集中于此，便于后续配置化）
# ============================================================================
# 导航类元素：登录后才可见的权限菜单是运行时发现相对静态分析的核心增量。
# 重要：SPA（React/Vue + antd 等）菜单也常是带类名的 div/li，故同时覆盖
# 「语义标签」与「常见菜单类名」，不假设一定是 `<a>`。
_NAV_SELECTOR = (
    'nav a, aside a, [role="menuitem"], [role="tab"], '
    ".menu a, .sidebar a, header a, "
    '[class*="menu-item"], [class*="menuItem"], [class*="nav-item"], [class*="navItem"], '
    '[class*="nav"] [class*="-item"], [class*="sidebar"] [class*="-item"], '
    '[class*="menu"] [class*="-item"]'
)
# 按钮类元素：语义 button 之外，还要覆盖 role=button 与常见 btn 类名
_BUTTON_SELECTOR = 'button, [role="button"], input[type=submit], [class*="btn"]'
# 表单控件：input / select / textarea / form
# 以及富文本编辑区（SPA 常用 contenteditable 代替 input 作输入框）
_FORM_SELECTOR = "form, input, select, textarea, [contenteditable='true']"
# 一次性取回导航项文本（用于 SPA 跳转后按文本重新定位，避免逐元素往返）
_NAV_LABELS_JS = (
    "els => els.map(e => (e.innerText || '').trim().replace(/\\s+/g, ' ').slice(0, 40))"
)
# 广搜兜底：CSS 启发式（_NAV_SELECTOR）常匹配不到真实菜单（类名千变万化 / 图标菜单无文字）。
# 这里直接枚举「导航容器内」的可见叶子项文字，不依赖任何特定类名，亦不依赖 LLM——
# 菜单点击路由发现的 LLM-free 治本兜底，保证 routes 不被静默压成 0。
_MAX_NAV_HARVEST = 60
_NAV_HARVEST_JS = r"""
() => {
  const MAX = __MAX__;
  const containers = document.querySelectorAll(
    'nav, aside, header, [role="navigation"], [role="menu"], .sidebar, .menu, .ant-menu, .el-menu, [class*="menu"], [class*="nav"], [class*="sidebar"], [class*="MenuItem"], [class*="NavItem"], [class*="SubMenu"]'
  );
  const seen = new Set();
  const out = [];
  const textOf = (el) => String(el.innerText || el.getAttribute('aria-label') || el.getAttribute('title') || el.getAttribute('alt') || '').trim().replace(/\\s+/g, ' ').slice(0, 40);
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return false;
    const s = window.getComputedStyle(el);
    return s.visibility !== 'hidden' && s.display !== 'none' && s.opacity !== '0';
  };
  containers.forEach((c) => {
    c.querySelectorAll('a, button, [role="menuitem"], [role="tab"], [role="button"], li, div').forEach((el) => {
      if (out.length >= MAX) return;
      const t = textOf(el);
      if (!t || seen.has(t)) return;
      if (el.querySelector('a, button, [role="menuitem"], [role="tab"]')) return;
      if (!visible(el)) return;
      seen.add(t);
      out.push(t);
    });
  });
  return out;
}
"""
# 危险菜单项（点击会退出登录 / 触发副作用），路由发现阶段跳过，避免把会话踢下线
_DANGER_LABEL_HINTS = ("退出", "注销", "登出", "退出登录", "sign out", "log out", "logout")

# 单页元素上限（防爆：超长列表页可达数千个元素）
MAX_ELEMENTS_PER_PAGE = 200
# 首页链接爬取上限
MAX_HOME_LINKS = 300
# 单页控制台错误上限
MAX_CONSOLE_ERRORS_PER_PAGE = 20
# 控制台错误全站累积上限（防日志/结果爆炸）
MAX_CONSOLE_ERRORS_TOTAL = 500
# SPA 路由切换后的静默等待（毫秒）
SPA_SETTLE_MS = 900
# 进入新页面后等「网络静默」的上限（毫秒）。
# SPA 普遍有长轮询/心跳，`networkidle` 往往**永远不满足**；若照配置超时（几十秒）等，
# 遍历十几个路由就会拖到数分钟。这里只需要「首屏渲染差不多完成」，故给一个短预算。
SETTLE_AFTER_GOTO_MS = 2500
# 等待 SPA 挂载导航菜单的上限（毫秒）：首批菜单常要等接口返回，预算不能太紧
NAV_READY_TIMEOUT_MS = 12000

# 静态资源扩展名：这些「路径」是文件下载 / 前端产物，不是可测页面。
# 真实环境里首页「下载客户端」链接（/downloads/xxx-win.zip）也被 `nav a` 命中，
# 且会被 SPA 兜底路由渲染成与首页同构的假页面，若不去掉就会凭空多出 1 个「页面功能点」。
_STATIC_ASSET_EXTS = (
    ".zip",
    ".rar",
    ".7z",
    ".tar",
    ".gz",
    ".pdf",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".ico",
    ".webp",
    ".css",
    ".js",
    ".mjs",
    ".map",
    ".json",
    ".xml",
    ".txt",
    ".csv",
    ".mp4",
    ".mp3",
    ".woff",
    ".woff2",
    ".ttf",
)

# 登录选择器（按优先级排列；命中第一个即用）
# 密码框：type=password 之外，SPA（antd / Tailwind 自定义组件）常把密码框做成
# `type=text` + 遮罩，故补 placeholder 兜底（实测目标站密码框占位即「密码」）
_PASSWORD_SELECTORS = (
    "input[type=password]",
    'input[name*="pass" i]',
    'input[placeholder*="密码"]',
)
_USER_SELECTORS = (
    'input[placeholder*="邮箱"]',
    'input[placeholder*="账号"]',
    'input[placeholder*="邮箱账号"]',
    "input[type=email]",
    'input[name*="user" i]',
    'input[name*="account" i]',
    'input[name*="mobile" i]',
    'input[name*="email" i]',
    "input[type=text]",
    "input:not([type])",
)
# 动态口令 / 一次性验证码 / 短信码（福享 Agent 登录页为「邮箱账号 + 密码 + 动态口令（6位）」）
_OTP_SELECTORS = (
    'input[placeholder*="动态"]',
    'input[placeholder*="口令"]',
    'input[placeholder*="验证码"]',
    'input[placeholder*="短信"]',
    'input[name*="otp" i]',
    'input[name*="captcha" i]',
    'input[name*="verify" i]',
)
_SUBMIT_SELECTORS = (
    "button[type=submit]",
    "input[type=submit]",
    'button:has-text("登 录")',
    'button:has-text("登录")',
    'button:has-text("登陆")',
    'button:has-text("Login")',
    'button:has-text("Sign in")',
    ".login-button",
)

INSTALL_HINT = (
    "运行时 UI 发现需要 Playwright。请在项目 venv 中执行："
    "python -m pip install playwright && python -m playwright install chromium"
    "（沙箱内无法下载内核时可改用系统浏览器：设 PLAYWRIGHT_CHANNEL=msedge）"
)


# ============================================================================
# 数据契约（只增不破）
# ============================================================================
@dataclass
class RuntimeUiOptions:
    """运行时 UI 发现选项。"""

    mode: str = RUNTIME_UI_PLAYWRIGHT
    headless: bool = True
    base_url: str = ""
    auth_token: str = ""  # 来自环境变量，不入库
    timeout: int = 30
    # --- M3.2 新增（Additive）---
    channel: str = ""  # ""=自带 chromium；"msedge"/"chrome"=复用系统浏览器
    proxy: str = ""  # 浏览器出口代理（内网目标需经沙箱代理）；空=直连
    login_user: str = ""  # 表单登录账号
    login_password: str = ""  # 表单登录密码（不入库 / 不日志）
    login_url: str = ""  # 登录页地址；为空回退到 base_url
    routes: list[str] = field(default_factory=list)  # 显式路由清单（优先级最高）
    max_pages: int = 60  # 遍历页面上限（防爆）
    degraded: bool = False  # 是否降级为 requests 抓取（M3.5 落地，占位）
    # --- M3.3 新增（Additive）---
    login_otp: str = ""  # 动态口令 / 一次性验证码（不入库 / 不日志）
    # G-10：OTP 刷新命令（可选）。code 扫描先于 url 登录时差可能让 OTP 过期；
    # 配置后首次登录失败会自动执行该命令取其 stdout 首行作为新 OTP 重试一次。
    login_otp_refresh_cmd: str = ""
    discover_by_menu: bool = True  # 是否靠点菜单发现 SPA 路由（无正确 href 时的唯一途径）
    # G-7：移动端/响应式发现（默认关：需显式开启 RUNTIME_UI_MOBILE_ENABLED）
    mobile_enabled: bool = False


@dataclass
class UiElement:
    """一个被发现的 UI 元素 / 交互点。"""

    selector: str
    kind: str = ""
    text: str = ""
    visible: bool = False
    deep: bool = False  # 是否来自 click-through 深层视图（弹窗/Tab/表单提交后）
    mobile: bool = False  # G-7：是否来自移动端视口发现（区别于桌面元素）


@dataclass
class ApiEndpoint:
    """运行时拦截到的后端接口（#223：XHR/fetch 响应监听）。"""

    method: str
    path: str
    url: str = ""
    status: int = 0


@dataclass
class UiPage:
    """一个被发现的可达页面。"""

    url: str
    path: str
    title: str = ""
    reachable: bool = True
    console_errors: list[str] = field(default_factory=list)
    elements: list[UiElement] = field(default_factory=list)


@dataclass
class RuntimeUiResult:
    """运行时 UI 发现结果。"""

    base_url: str = ""
    elements: list[UiElement] = field(default_factory=list)
    console_errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    pages: list[UiPage] = field(default_factory=list)
    degraded: bool = False
    logged_in: bool = False  # 是否执行了表单登录并成功（匿名访问为 False）
    # --- M3.3 新增（Additive）---
    discovered_routes: list[str] = field(default_factory=list)  # 菜单点击发现的路由
    # --- #223 新增（Additive）---
    api_endpoints: list[ApiEndpoint] = field(default_factory=list)  # 运行时拦截到的后端接口

    def reachable_pages(self) -> list[UiPage]:
        """可达页面（`reachable=True`）。"""
        return [p for p in self.pages if p.reachable]


@dataclass
class RuntimePageInfo:
    """单个页面的运行时细节（A2：供用例正文富化；**不含任何凭证**）。

    为什么单独建这个结构：`RuntimeUiResult` 是「一次发现的全部结果」，而用例生成需要的
    是「按页面查细节」——一个以路径为键的索引。二者生命周期不同（前者随发现结束即归档，
    后者随用例生成被反复查询），混用一个结构会让 case_gen 反向依赖整次发现的内部形状。
    """

    path: str
    url: str = ""
    title: str = ""
    elements: list[dict[str, Any]] = field(default_factory=list)
    console_errors: list[str] = field(default_factory=list)

    def visible_elements(self) -> list[dict[str, Any]]:
        return [e for e in self.elements if e.get("visible")]

    def element_phrases(self, limit: int = 6) -> list[str]:
        """元素的人类可读短语（`按钮「新建任务」`），超出上限折叠为「等 N 个」。"""
        phrases: list[str] = []
        visible = self.visible_elements()
        for item in visible[:limit]:
            text = str(item.get("text") or "").strip()
            label = text or str(item.get("selector") or "")
            phrases.append(
                f"{_ELEMENT_KIND_CN.get(str(item.get('kind') or ''), '元素')}「{label}」"
            )
        rest = len(visible) - len(phrases)
        if rest > 0:
            phrases.append(f"等 {len(visible)} 个元素")
        return phrases


# 运行时元素 kind → 中文名（与 _EXTRACT_JS_TEMPLATE 的 push() 取值一一对应）
_ELEMENT_KIND_CN: dict[str, str] = {
    "nav": "导航项",
    "button": "按钮",
    "input": "输入框",
    "select": "下拉框",
    "textarea": "文本域",
    "form": "表单",
    "edit": "可编辑区域",
}


@dataclass
class _Session:
    """一次浏览器会话（页面 + 其上下文）。打包传参，避免函数参数过长。"""

    page: Any
    context: Any


@dataclass
class _MenuProbe:
    """菜单点击探测的输入（参数打包，兼顾可读性与 lint 的参数上限）。"""

    session: _Session
    options: RuntimeUiOptions
    start_url: str
    start_route: str
    labels: list[str]


# ============================================================================
# 可用性探测与配置映射（M3.1）
# ============================================================================
def has_playwright() -> bool:
    """探测 Playwright 是否可用（决定真实浏览器通道 / 后续降级路径）。"""
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return False
    return True


def options_from_settings(settings: Any) -> RuntimeUiOptions:
    """从 `core.config.Settings` 构造选项（凭证只在此处做一次搬运，不落任何产物）。

    浏览器出口代理：内网目标经沙箱代理可达（直连常在 TCP 层之后挂起）。优先用
    `RUNTIME_UI_PROXY`，回退到通用的 `HTTPS_PROXY` / `HTTP_PROXY` 环境变量。
    """
    # 仅当显式设置代理时才走代理（内网目标经沙箱代理可达）；
    # 公网目标必须直连——**禁止继承本机透明代理**（如 127.0.0.1:54985），
    # 否则浏览器会把请求发到本机代理而连不上公网地址（Page.goto 超时）。
    proxy = (
        str(getattr(settings, "runtime_ui_proxy", "") or "")
        or os.environ.get("RUNTIME_UI_PROXY")
        or ""
    )
    return RuntimeUiOptions(
        headless=bool(settings.playwright_headless),
        base_url=str(settings.runtime_base_url or ""),
        auth_token=str(settings.runtime_auth_token or ""),
        timeout=int(settings.runtime_ui_timeout),
        channel=str(settings.playwright_channel or ""),
        proxy=str(proxy or ""),
        login_user=str(settings.runtime_login_user or ""),
        login_password=str(settings.runtime_login_password or ""),
        login_otp=str(getattr(settings, "runtime_login_otp", "") or ""),
        login_otp_refresh_cmd=str(getattr(settings, "runtime_login_otp_refresh_cmd", "") or ""),
        login_url=str(settings.runtime_login_url or ""),
        routes=list(settings.runtime_routes or []),
        max_pages=int(settings.runtime_max_pages),
        mobile_enabled=bool(getattr(settings, "runtime_ui_mobile_enabled", False)),
    )


# ============================================================================
# 路由解析（纯函数，便于单测）
# ============================================================================
def _origin(url: str) -> str:
    """取 URL 的源（scheme://netloc），用于同源判定。"""
    parts = urlparse(url)
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme else ""


def _looks_like_asset(path: str) -> bool:
    """路径末段是否像静态资源（.zip/.pdf/.png...）——是则不是可测页面。"""
    name = (path or "").rsplit("/", 1)[-1].lower()
    return any(name.endswith(ext) for ext in _STATIC_ASSET_EXTS)


def _normalize_route(raw: str, base_url: str) -> str | None:
    """把一条候选路由规范化为同源路径；非同源 / 伪协议 / 静态资源返回 None。"""
    item = (raw or "").strip()
    if not item or item.startswith(("#", "javascript:", "mailto:", "tel:", "data:")):
        return None
    parts = urlparse(urljoin(base_url, item))
    if parts.scheme not in ("http", "https"):
        return None
    if _origin(base_url) and f"{parts.scheme}://{parts.netloc}" != _origin(base_url):
        return None  # 外链一律跳过
    path = parts.path or "/"
    if _looks_like_asset(path):
        return None  # 下载 / 静态产物不是页面
    return f"{path}?{parts.query}" if parts.query else path


def _resolve_routes(
    options: RuntimeUiOptions,
    static_paths: list[str] | None = None,
    discovered_links: list[str] | None = None,
    menu_paths: list[str] | None = None,
) -> list[str]:
    """路由清单，按优先级合并去重后截断到上限。

    优先级（M3.3 起四档）：显式 routes > **菜单点击发现** > 静态 page 功能点路径 > 首页可点链接。

    「菜单点击发现」插在静态路径之前：它是**真实点出来的**登录后路由，比静态代码里的
    路径声明更贴近线上实际可达面（SPA 常按权限动态拼路由，静态代码看不到）。
    """
    merged: list[str] = []
    seen: set[str] = set()
    limit = max(1, int(options.max_pages))
    groups = (options.routes, menu_paths or [], static_paths or [], discovered_links or [])
    for group in groups:
        for raw in group:
            path = _normalize_route(raw, options.base_url)
            if path is None or path in seen:
                continue
            seen.add(path)
            merged.append(path)
            if len(merged) >= limit:
                return merged
    return merged


# ============================================================================
# 页面操作（M3.2；page 用 Any 标注——Playwright 未安装时不应触发导入）
# ============================================================================
@dataclass
class ConsoleSink:
    """按顺序累积控制台错误，支持「从某个位置起的新增条目」切片。"""

    entries: list[str] = field(default_factory=list)

    def add(self, message: str) -> None:
        if len(self.entries) < MAX_CONSOLE_ERRORS_TOTAL:
            self.entries.append(message)

    def since(self, mark: int) -> list[str]:
        return self.entries[mark : mark + MAX_CONSOLE_ERRORS_PER_PAGE]


def timeout_ms(options: RuntimeUiOptions) -> int:
    """超时（秒）→ 毫秒（至少 1ms）。"""
    return max(1, int(options.timeout)) * 1000


def _safe_url(page: Any) -> str:
    try:
        return str(page.url or "")
    except Exception:  # 页面已崩溃时取值失败属正常，不能因此中断发现
        return ""


def _route_of(page: Any) -> str:
    """当前页面路由（path[?query]），与 `_normalize_route` 输出同构。"""
    parts = urlparse(_safe_url(page))
    path = parts.path or "/"
    return f"{path}?{parts.query}" if parts.query else path


def attach_console_listeners(page: Any, sink: ConsoleSink) -> None:
    """注册控制台错误与未捕获异常的监听（只进结果，不落日志）。"""

    def _on_console(msg: Any) -> None:
        try:
            if str(getattr(msg, "type", "")) == "error":
                sink.add(f"[console] {str(getattr(msg, 'text', ''))[:300]}")
        except Exception:  # 监听回调里的异常不能反噬主流程
            return

    def _on_pageerror(exc: Any) -> None:
        try:
            sink.add(f"[pageerror] {str(exc)[:300]}")
        except Exception:
            return

    page.on("console", _on_console)
    page.on("pageerror", _on_pageerror)


def _first_match(page: Any, selectors: tuple[str, ...]) -> Any:
    """按优先级返回第一个命中的元素句柄（未命中返回 None）。"""
    for sel in selectors:
        try:
            handle = page.query_selector(sel)
        except Exception:  # 单个选择器语法/状态异常不影响继续尝试
            continue
        if handle is not None:
            return handle
    return None


# ============================================================================
# 登录（M3.2 两字段 → M3.3 三字段 + 可选二次验证）
# ============================================================================
def _fetch_otp(options: RuntimeUiOptions, *, refresh: bool = False) -> str:
    """取动态口令（G-10）。

    - `refresh=False`：返回静态配置的 `login_otp`；
    - `refresh=True` 且配置了 `login_otp_refresh_cmd`：执行该命令取其 **stdout 首行**
      作为新 OTP（用于登录失败时的自动续期）。

    命令输出只取首行并去除首尾空白，**绝不回显、绝不落日志**——与账号密码同一红线。
    命令执行失败 / 无输出时返回空串（交由调用方回退到静态 OTP 或判登录失败）。
    """
    if refresh and options.login_otp_refresh_cmd:
        try:
            out = subprocess.run(
                options.login_otp_refresh_cmd,
                shell=True,  # nosec B602 受控：命令来自环境变量 RUNTIME_LOGIN_OTP_REFRESH_CMD，由运维可信配置，非用户输入
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            lines = [ln.strip() for ln in (out.stdout or "").splitlines() if ln.strip()]
            if lines:
                return lines[0]
        except Exception:  # 刷新失败不致命：回退静态 OTP / 由登录成败判定兜底
            return ""
    return options.login_otp


def _fill_otp_if_present(page: Any, options: RuntimeUiOptions) -> bool:
    """若配置了动态口令且页面存在口令输入框则填入；返回是否填入。"""
    if not options.login_otp:
        return False
    otp = _first_match(page, _OTP_SELECTORS)
    if otp is None:
        return False
    try:
        otp.fill(options.login_otp)
    except Exception:
        return False
    return True


def _click_submit(page: Any, submit: Any, options: RuntimeUiOptions) -> None:
    try:
        submit.click()
        # 不等地 networkidle：SPA（福享 Agent 等）普遍有长轮询/心跳，networkidle 永不满足，
        # 照超时等会把登录误判为失败。真实成败由「提交后密码框是否消失」判定（见 _login）。
        page.wait_for_load_state("domcontentloaded", timeout=timeout_ms(options))
    except Exception:  # 加载态超时/异常不致命——后续密码框消失判定才是成败依据
        return


def _maybe_submit_otp(page: Any, options: RuntimeUiOptions, result: RuntimeUiResult) -> None:
    """二次验证兜底：部分系统是「账号密码 → 下一步 → 动态口令」的两屏流程。

    仅在「首屏没有口令框」且「首屏提交后仍停留登录页且出现口令框」时触发。
    """
    if not options.login_otp or _first_match(page, _PASSWORD_SELECTORS) is None:
        return
    otp = _first_match(page, _OTP_SELECTORS)
    submit = _first_match(page, _SUBMIT_SELECTORS)
    if otp is None or submit is None:
        return
    try:
        otp.fill(options.login_otp)
        submit.click()
        # 同 _click_submit：不等地 networkidle，SPA 心跳会让其永不满足
        page.wait_for_load_state("domcontentloaded", timeout=timeout_ms(options))
        result.notes.append("已执行二次验证（动态口令，取值不记录）")
    except Exception:  # 二次提交失败由调用方的「仍在登录页」判定统一兜底
        return


def _wait_login_ready(page: Any, options: RuntimeUiOptions) -> None:
    """等登录表单渲染完成再判定。

    SPA 首屏是客户端异步渲染：`goto(domcontentloaded)` 返回时表单往往还不存在，
    直接查控件会把登录页误判成「公开页」而跳过登录（实测目标站即如此）。
    这里只等「出现任意 input/form」，**不等 networkidle**——登录页常有长轮询/心跳，
    可能永远不 idle。
    """
    try:
        page.wait_for_selector("input, form", timeout=timeout_ms(options))
    except Exception:  # 超时即认为确实没有登录表单（公开页）
        return


def _login(page: Any, options: RuntimeUiOptions, result: RuntimeUiResult) -> bool:
    """检测登录表单并提交；无表单则视为已登录 / 匿名可访问。

    返回 True 表示执行了表单登录并成功；False 表示未执行登录（无凭证或无表单）。
    失败一律抛 `EngineError`（分类提示），**绝不回显密码 / 动态口令**。
    """
    if not (options.login_user and options.login_password):
        result.notes.append("未提供账号密码：按匿名访问处理（仅公开页可见）")
        return False

    target = options.login_url or options.base_url
    try:
        page.goto(target, wait_until="domcontentloaded", timeout=timeout_ms(options))
    except Exception as exc:  # 打开登录页失败即登录失败
        raise EngineError(f"登录失败：无法打开登录页 {target}（{exc}）") from exc

    _wait_login_ready(page, options)
    pwd = _first_match(page, _PASSWORD_SELECTORS)
    if pwd is None:
        result.notes.append(
            f"登录页未发现密码输入框（{target}）：视为已登录 / 公开页，跳过表单登录"
        )
        return False

    user = _first_match(page, _USER_SELECTORS)
    submit = _first_match(page, _SUBMIT_SELECTORS)
    if user is None or submit is None:
        raise EngineError(
            "登录失败：未定位到账号输入框或提交按钮，"
            "请确认登录页地址（RUNTIME_LOGIN_URL）或页面选择器"
        )

    before_url = _safe_url(page)
    try:
        user.fill(options.login_user)
        pwd.fill(options.login_password)
    except Exception as exc:  # 填值失败归为登录失败
        raise EngineError(f"登录失败：无法填入账号密码（{exc}）") from exc

    # G-10：OTP 可能过期（code 扫描先于 url 登录时差 / 长流水线合并路径）。
    # 首次提交若仍停在登录页，且配置了刷新命令，则取新 OTP 重试一次（至多 1 次，防死循环）。
    succeeded = _login_with_retry(page, submit, options, result)
    if not succeeded:
        raise EngineError(
            "登录失败：提交后仍停留在登录页（含 OTP 自动续期重试），请核对账号密码 / "
            "动态口令是否正确，或被测系统是否存在图片验证码 / 风控"
        )
    result.notes.append(
        f"表单登录成功（账号 {options.login_user}，密码与动态口令均不记录）；"
        f"跳转 {before_url} → {_safe_url(page)}"
    )
    return True


def _login_with_retry(
    page: Any, submit: Any, options: RuntimeUiOptions, result: RuntimeUiResult
) -> bool:
    """最多两次登录尝试（首屏 + 可选 OTP 续期重试），返回是否最终成功。"""
    for attempt in range(2):
        if _login_attempt(page, submit, options, result, attempt):
            return True
        # 是否已无重试手段：无刷新命令则首屏失败即终止；刷新命令取不到新 OTP 也终止
        if not options.login_otp_refresh_cmd:
            return False
        if attempt > 0 and not _fetch_otp(options, refresh=True):
            return False
    return False


def _login_attempt(
    page: Any, submit: Any, options: RuntimeUiOptions, result: RuntimeUiResult, attempt: int
) -> bool:
    """单次登录提交尝试；返回 True 表示密码框已消失（登录成功）。

    G-10：attempt>0 时取刷新后的 OTP 重填；仅首屏且未配置 OTP 时才走二次验证兜底。
    """
    otp = _fetch_otp(options, refresh=(attempt > 0))
    # 首屏若已有口令框（单屏流程）则填入；两屏流程（账号密码 → 下一步 → 动态口令）
    # 首屏无口令框，_fill_otp_with 返回 False，提交后由 _maybe_submit_otp 兜底二次提交。
    otp_filled_first = _fill_otp_with(page, options, otp)
    _click_submit(page, submit, options)
    if not otp_filled_first:
        _maybe_submit_otp(page, options, result)
    # SPA 客户端跳转有延迟：提交后登录表单不会瞬间卸载，需等其消失再判定，
    # 否则会把「刚提交、尚未跳转」误判成「停留在登录页」→ 假失败（实测福享 Agent 即如此）。
    try:
        page.wait_for_function(
            "() => !document.querySelector('input[type=password]')",
            timeout=min(timeout_ms(options), 12000),
        )
    except Exception:  # 超时则交由下面的密码框判定兜底
        pass
    return _first_match(page, _PASSWORD_SELECTORS) is None


def _fill_otp_with(page: Any, options: RuntimeUiOptions, otp: str) -> bool:
    """用指定 OTP 填入口令框（G-10：支持刷新后重试）。返回是否填入。"""
    if not otp:
        return False
    otp_box = _first_match(page, _OTP_SELECTORS)
    if otp_box is None:
        return False
    try:
        otp_box.fill(otp)
        return True
    except Exception:
        return False


def _apply_token(context: Any, options: RuntimeUiOptions, result: RuntimeUiResult) -> None:
    """令牌型鉴权：以自定义 Header 注入（取值不记录）。"""
    if not options.auth_token:
        return
    try:
        context.set_extra_http_headers({"Authorization": f"Bearer {options.auth_token}"})
    except Exception as exc:  # 注入失败降级为继续以匿名访问
        result.notes.append(f"令牌注入失败，已按匿名继续（{exc}）")
        return
    result.notes.append("已注入令牌鉴权头（Authorization: Bearer ***）")


def _relogin_if_needed(page: Any, options: RuntimeUiOptions, result: RuntimeUiResult) -> bool:
    """G-10：发现过程中若被重定向回登录页（会话过期 / OTP 时效），原地重登录一次。

    调用点在「已完成首屏登录、正在进行深度发现」的间隙——此时若会话已失效，
    继续抓取只会得到登录页、污染功能点。返回 True 表示「无需登录或重登录成功」，
    False 表示重登录失败（调用方据此降级而非静默产出错误功能点）。
    """
    login_target = options.login_url or options.base_url
    # 当前不在登录域 → 无需处理
    if _origin(_safe_url(page)) != _origin(login_target):
        return True
    # 当前页面没有密码框 → 不在登录页 → 无需处理
    if _first_match(page, _PASSWORD_SELECTORS) is None:
        return True
    try:
        return _login(page, options, result)
    except EngineError as exc:
        result.degraded = True
        result.notes.append(f"会话过期重登录失败（降级继续）：{exc.message}")
        return False


# ============================================================================
# SPA 菜单点击路由发现（M3.3）
# ============================================================================
def _nav_labels(page: Any) -> list[str]:
    """当前页导航项的文本清单（一次 JS 取回；用于 SPA 跳转后按文本重新定位）。"""
    try:
        raw = page.eval_on_selector_all(_NAV_SELECTOR, _NAV_LABELS_JS)
    except Exception:
        return []
    return [str(t) for t in raw] if isinstance(raw, list) else []


def _harvest_nav_labels(page: Any) -> list[str]:
    """LLM-free 兜底：广搜导航容器内可见叶子项文字（CSS 启发式漏抓时启用）。

    不依赖任何特定菜单类名，也不依赖 LLM——只要页面真有导航区，就能把菜单项文字
    取回来驱动点击路由发现，从而把 `routes=0` 修成真实菜单数（治本，非臆造）。
    """
    try:
        raw = page.evaluate(_build_nav_harvest_js())
    except Exception:
        return []
    return [str(t) for t in raw] if isinstance(raw, list) else []


def _is_danger_label(label: str) -> bool:
    """菜单项是否疑似退出登录等危险操作（路由发现阶段应跳过）。"""
    low = (label or "").lower()
    return any(h in low for h in _DANGER_LABEL_HINTS)


def _wait_nav_ready(page: Any, options: RuntimeUiOptions) -> None:
    """等 SPA 把导航菜单挂载出来。

    `goto(domcontentloaded)` 返回时 React/Vue 常尚未渲染；更关键的是**首批菜单往往要等
    后端接口返回**才出现，故预算不能给太紧（给太紧会误判「本页没有导航」→ 静默 0 条）。
    """
    budget = min(timeout_ms(options), NAV_READY_TIMEOUT_MS)
    try:
        page.wait_for_selector(_NAV_SELECTOR, timeout=budget)
    except Exception:  # 超时即认定该页确实没有导航区
        return


def _return_to(page: Any, url: str, options: RuntimeUiOptions) -> bool:
    """回到起点页并等导航渲染完（仅在需要「恢复」时调用）。"""
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms(options))
    except Exception:
        return False
    _wait_nav_ready(page, options)
    return True


def _nav_handles(page: Any) -> list[Any]:
    """当前页的导航项句柄（失败返回空表）。"""
    try:
        return list(page.query_selector_all(_NAV_SELECTOR))
    except Exception:
        return []


def _locate_nav(page: Any, label: str, idx: int) -> Any:
    """在当前 DOM 里定位导航项：优先按文本匹配 CSS 句柄（SPA 跳转后元素会重建）。

    未命中 CSS 句柄时返回 None，交回 `_click_label` 的**文本兜底**点击——这是修复
    `routes=0` 的关键：CSS 启发式漏掉的菜单项，靠可见文字点击仍能驱动 SPA 路由跳转。
    """
    handles = _nav_handles(page)
    if not handles:
        return None
    if label:
        current = _nav_labels(page)
        if label in current and current.index(label) < len(handles):
            return handles[current.index(label)]
    return None  # 未命中：不靠索引误点，交给文本兜底


def _menu_href_route(handle: Any, base_url: str) -> str:
    """菜单项 `href` 指向的路由（兜底用：SPA 里它可能是占位值，不能当主依据）。"""
    try:
        href = handle.get_attribute("href")
    except Exception:
        return ""
    if not href:
        return ""
    return _normalize_route(str(href), base_url) or ""


def _click_and_read_route(page: Any, handle: Any, options: RuntimeUiOptions) -> str:
    """点击一个导航项，等 URL 变化后读回路由（未变化返回空串）。"""
    before = _route_of(page)
    try:
        if not handle.is_visible():
            return ""
        handle.click(timeout=timeout_ms(options), no_wait_after=True)
    except Exception:  # 单个菜单项点不动不影响其它项（交给 href 兜底）
        return ""
    try:
        page.wait_for_function(
            "prev => location.pathname + location.search !== prev",
            arg=before,
            timeout=SPA_SETTLE_MS + 2000,
        )
    except Exception:  # 未变化（可能是当前页 / 新开标签）→ 交给调用方兜底
        pass
    after = _route_of(page)
    return "" if after == before else after


def _popup_route(popups: list[Any], options: RuntimeUiOptions) -> str:
    """若点击新开了标签页，从弹出页读路由。"""
    for popup in popups:
        try:
            popup.wait_for_load_state("domcontentloaded", timeout=timeout_ms(options))
            route = _route_of(popup)
        except Exception:
            continue
        if route:
            return route
    return ""


def _route_of_nav_item(page: Any, handle: Any, options: RuntimeUiOptions, popups: list[Any]) -> str:
    """取一个导航项指向的路由。

    顺序：**先真实点击**——SPA 的 `href` 常是占位符、或所有菜单项写成同一个值，
    此时只有点击才会驱动路由库 `pushState` 真正跳转；点击未产生 URL 变化时再退回读
    `href`，最后兜底新标签页。
    """
    popups.clear()
    route = _click_and_read_route(page, handle, options)
    if route:
        return route
    route = _menu_href_route(handle, options.base_url)
    if route:
        return route
    if popups:
        route = _popup_route(popups, options)
    return route


def _click_label(page: Any, label: str, options: RuntimeUiOptions, popups: list[Any]) -> str:
    """按 label 点击导航项并读回路由：先 CSS 句柄，未命中则按可见文字点击（S0 治本兜底）。

    CSS 启发式（`_NAV_SELECTOR`）匹配不到真实菜单时，专家 S0 反推出的菜单文字也无法用
    CSS 句柄定位——此时退回 `page.get_by_text` 文本点击，仍能驱动 SPA 路由跳转，
    把 `routes=0` 修成真实菜单数。任何异常均返回空串，不影响主链路。
    """
    popups.clear()
    handle = _locate_nav(page, label, 0)
    if handle is not None:
        route = _route_of_nav_item(page, handle, options, popups)
        if route:
            return route
    # 文本兜底：CSS 启发式漏掉的菜单项靠可见文字点击
    try:
        el = page.get_by_text(label, exact=False).first
    except Exception:
        el = None
    if el is None:
        return ""
    before = _route_of(page)
    try:
        if not el.is_visible():
            return ""
        el.click(timeout=timeout_ms(options), no_wait_after=True)
    except Exception:
        return ""
    try:
        page.wait_for_function(
            "prev => location.pathname + location.search !== prev",
            arg=before,
            timeout=SPA_SETTLE_MS + 2000,
        )
    except Exception:
        pass
    after = _route_of(page)
    return "" if after == before else after


def _probe_menu_routes(probe: _MenuProbe) -> list[str]:
    """**单会话**逐个导航项探测路由（不逐项重新打开页面）。

    真实环境实测：菜单在 SPA 外壳里跨路由常驻，单会话点击 + 按「文本 → 索引」重新定位
    即可；逐项 `goto` 重开不仅慢，还会撞上首屏异步挂载导致句柄为空、静默丢掉全部路由。

    点击优先用 CSS 句柄（`_click_label` 内部），CSS 未命中时自动退回**可见文字点击**
    （S0 专家反推的菜单项多属此类），保证漏抓的菜单也能被遍历到。
    """
    page = probe.session.page
    popups: list[Any] = []

    def _on_page(new_page: Any) -> None:
        popups.append(new_page)

    try:
        probe.session.context.on("page", _on_page)
    except Exception:  # 监听注册失败不影响主流程（只是拿不到新标签页路由）
        pass

    found: list[str] = []
    seen: set[str] = set()
    limit = max(1, int(probe.options.max_pages))
    try:
        for _idx, label in enumerate(probe.labels[:limit]):
            raw = _click_label(page, label, probe.options, popups)
            if not raw:  # CSS 与文本点击都失败 → 回起点后重试一次（导航可能被跳转带走）
                if not _return_to(page, probe.start_url, probe.options):
                    break
                raw = _click_label(page, label, probe.options, popups)
            route = _normalize_route(raw, probe.options.base_url)
            if route and route != probe.start_route and route not in seen:
                seen.add(route)
                found.append(route)
    finally:
        try:
            probe.session.context.remove_listener("page", _on_page)
        except Exception:
            pass
        close_quietly(*popups)
    return found


def _discover_menu_routes(
    page: Any,
    context: Any,
    options: RuntimeUiOptions,
    result: RuntimeUiResult,
    extra_labels: list[str] | None = None,
) -> list[str]:
    """SPA 路由发现：逐一点击导航项，记录 URL 变化。

    点击本身不计入结果元素，只用于发现路由；收尾回起点供后续抓取落地页。
    `extra_labels`：专家 S0 反推出的、CSS 启发式漏掉的菜单文字，并入后一并遍历（治本）。
    """
    if not options.discover_by_menu:
        return []
    labels = _nav_labels(page)
    if not labels:  # 导航还没挂出来 → 等一等再看（首批菜单要等接口）
        _wait_nav_ready(page, options)
        labels = _nav_labels(page)
    # S0 治本：把专家反推的菜单文字并入（CSS 启发式漏掉的导航项），去重
    if extra_labels:
        merged = list(labels)
        for lb in extra_labels:
            if lb and lb not in merged:
                merged.append(lb)
        labels = merged
    if not labels:
        return []
    start_url = _safe_url(page) or options.base_url
    start_route = _route_of(page) if _safe_url(page) else (urlparse(options.base_url).path or "/")
    probe = _MenuProbe(_Session(page, context), options, start_url, start_route, labels)
    found = _probe_menu_routes(probe)
    _return_to(page, start_url, options)  # 收尾回起点，供后续抓取落地页
    if found:
        result.notes.append(f"菜单点击发现路由 {len(found)} 条：{', '.join(found[:10])}")
    return found


# ============================================================================
# 元素抓取
# ============================================================================
_EXTRACT_JS_TEMPLATE = """
() => {
  const MAX = __MAX__;
  const seen = new Set();
  const isVisible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return false;
    const s = window.getComputedStyle(el);
    return s.visibility !== 'hidden' && s.display !== 'none' && s.opacity !== '0';
  };
  const cssPath = (el) => {
    const parts = [];
    let node = el;
    let depth = 0;
    while (node && node.nodeType === 1 && depth < 5) {
      if (node.id) { parts.unshift('#' + node.id); break; }
      let sel = node.tagName.toLowerCase();
      const cls = (node.getAttribute('class') || '').trim().split(/\\s+/).filter(Boolean).slice(0, 2);
      if (cls.length) sel += '.' + cls.join('.');
      const parent = node.parentElement;
      if (parent) {
        const same = Array.from(parent.children).filter((c) => c.tagName === node.tagName);
        if (same.length > 1) sel += ':nth-of-type(' + (same.indexOf(node) + 1) + ')';
      }
      parts.unshift(sel);
      node = node.parentElement;
      depth += 1;
    }
    return parts.join(' > ');
  };
  const textOf = (el) => String(
    el.innerText || el.value || el.getAttribute('aria-label')
    || el.getAttribute('placeholder') || el.getAttribute('title') || ''
  ).trim().replace(/\\s+/g, ' ').slice(0, 80);
  const out = [];
  const push = (el, kind) => {
    if (out.length >= MAX || seen.has(el)) return;
    seen.add(el);
    out.push({ selector: cssPath(el), kind: kind, text: textOf(el), visible: isVisible(el) });
  };
  document.querySelectorAll(__NAV__).forEach((el) => push(el, 'nav'));
  document.querySelectorAll(__BTN__).forEach((el) => push(el, 'button'));
  document.querySelectorAll(__FORM__).forEach((el) => {
    const tag = el.tagName.toLowerCase();
    push(el, el.getAttribute('contenteditable') === 'true' ? 'edit' : tag);
  });
  return out;
}
"""

_LINKS_JS_TEMPLATE = """
() => {
  const out = [];
  document.querySelectorAll('a[href]').forEach((a) => out.push(a.getAttribute('href')));
  ['data-href', 'data-url', 'data-path', 'data-route'].forEach((attr) => {
    document.querySelectorAll('[' + attr + ']').forEach((el) => out.push(el.getAttribute(attr)));
  });
  return out.filter((h) => !!h).slice(0, __MAX__);
}
"""


def _build_extract_js() -> str:
    """元素抓取脚本；选择器与上限取自本模块常量，避免两处漂移。"""
    return (
        _EXTRACT_JS_TEMPLATE.replace("__MAX__", str(MAX_ELEMENTS_PER_PAGE))
        .replace("__NAV__", json.dumps(_NAV_SELECTOR))
        .replace("__BTN__", json.dumps(_BUTTON_SELECTOR))
        .replace("__FORM__", json.dumps(_FORM_SELECTOR))
    )


def _build_links_js() -> str:
    """链接抓取脚本（含 data-* 路由提示：SPA 常无 `<a>`，靠 data 属性路由）。"""
    return _LINKS_JS_TEMPLATE.replace("__MAX__", str(MAX_HOME_LINKS))


def _build_nav_harvest_js() -> str:
    """广搜导航菜单项文字的脚本（不依赖特定类名，亦不依赖 LLM）。"""
    return _NAV_HARVEST_JS.replace("__MAX__", str(_MAX_NAV_HARVEST))


def _crawl_links(page: Any) -> list[str]:
    """抓取当前页所有 `<a href>` 与 data-* 路由提示（供路由兜底）。"""
    try:
        links = page.evaluate(_build_links_js())
    except Exception:  # 链接爬取失败不影响主流程
        return []
    return [str(x) for x in links] if isinstance(links, list) else []


def _collect_page(
    page: Any,
    url: str,
    options: RuntimeUiOptions,
    sink: ConsoleSink,
    mark: int,
) -> UiPage:
    """抓取单页：可达性、控制台错误、导航 / 表单 / 按钮元素。"""
    budget_ms = timeout_ms(options)
    path = urlparse(url).path or "/"
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=budget_ms)
        try:
            # 只等「首屏差不多渲染完」；SPA 长轮询会让 networkidle 永不满足，
            # 照配置超时等会把十几个路由的遍历拖到数分钟。
            page.wait_for_load_state("networkidle", timeout=SETTLE_AFTER_GOTO_MS)
        except Exception:
            pass
    except Exception as exc:  # 单页失败只标记不可达，不中断遍历
        return UiPage(
            url=url,
            path=path,
            reachable=False,
            console_errors=[*sink.since(mark), f"[goto] {str(exc)[:200]}"],
        )

    try:
        title = str(page.title() or "")
    except Exception:  # 标题取值失败不影响元素抓取
        title = ""

    raw_elements: list[dict[str, Any]] = []
    try:
        got = page.evaluate(_build_extract_js())
        if isinstance(got, list):
            raw_elements = [x for x in got if isinstance(x, dict)]
    except Exception:  # 元素抓取失败仍保留该页（可达性已确立）
        raw_elements = []

    elements = [
        UiElement(
            selector=str(item.get("selector") or ""),
            kind=str(item.get("kind") or ""),
            text=str(item.get("text") or ""),
            visible=bool(item.get("visible")),
        )
        for item in raw_elements
        if item.get("selector")
    ]
    return UiPage(
        url=url,
        path=path,
        title=title,
        reachable=True,
        console_errors=sink.since(mark),
        elements=elements,
    )


def _absorb(result: RuntimeUiResult, ui_page: UiPage) -> None:
    """把单页结果并入总结果（按页聚合 + 展平元素/错误）。"""
    result.pages.append(ui_page)
    result.elements.extend(ui_page.elements)
    result.console_errors.extend(ui_page.console_errors)


def _collect_current_page(
    page: Any,
    key: str,
    options: RuntimeUiOptions,
    sink: ConsoleSink,
    mark: int,
) -> UiPage:
    """就地采集当前页元素（不跳转）：用于状态驱动 SPA 的「面板切换」发现。

    路由驱动 SPA 用 `page.goto(route)` 逐页采集；但状态驱动 SPA（如 福享Work AI工作台）
    点击菜单只切换右侧面板、URL 不变，只能就地采集当前 DOM，并用内容签名区分不同面板。
    """
    try:
        title = str(page.title() or "")
    except Exception:  # 标题取值失败不影响元素抓取
        title = ""
    raw_elements: list[dict[str, Any]] = []
    try:
        got = page.evaluate(_build_extract_js())
        if isinstance(got, list):
            raw_elements = [x for x in got if isinstance(x, dict)]
    except Exception:  # 元素抓取失败仍保留该页（可达性已确立）
        raw_elements = []
    elements = [
        UiElement(
            selector=str(item.get("selector") or ""),
            kind=str(item.get("kind") or ""),
            text=str(item.get("text") or ""),
            visible=bool(item.get("visible")),
        )
        for item in raw_elements
        if item.get("selector")
    ]
    return UiPage(
        url=key,
        path=key,
        title=title,
        reachable=True,
        console_errors=sink.since(mark),
        elements=elements,
    )


_PANEL_SIG_JS = """
() => {
  // 优先用「当前选中的菜单项」作为状态驱动 SPA 的区域签名（最稳定、最贴近功能分区）
  const sel = document.querySelector(
    '.ant-menu-item-selected, .ant-menu-submenu-selected, [class*=menu-item-selected], [class*=MenuItem-selected], [aria-selected="true"]'
  );
  if (sel) {
    const t = String(sel.innerText || sel.getAttribute('aria-label') || '').trim().replace(/\\s+/g, ' ').slice(0, 30).toLowerCase();
    if (t) return t;
  }
  // 兜底：主内容区标题
  const m = document.querySelector('main, [role=main], .ant-layout-content, .content, [class*=content]');
  if (!m) return '';
  const h = m.querySelector('h1, h2, h3, [role=heading]');
  const t = (h ? h.innerText : m.innerText) || '';
  return t.trim().replace(/\\s+/g, ' ').slice(0, 30).toLowerCase();
}
"""


def _panel_signature(page: Any) -> str:
    """当前主内容面板的稳定签名（标题 / 首段文本）；用于识别「状态驱动 SPA」的面板切换。

    路由驱动 SPA 靠 URL 区分页面；状态驱动 SPA（菜单点击只换面板、URL 不变）须靠内容签名
    区分不同功能区域，否则所有菜单项都被压成「1 个页面」，用例覆盖严重偏少。
    """
    try:
        sig = page.evaluate(_PANEL_SIG_JS)
    except Exception:
        return ""
    return str(sig or "").strip()


def _page_dom_text(page: Any) -> str:
    """抓取页面可见正文文本（供专家 S0 反推菜单时作为 LLM 输入；失败返回空串）。"""
    try:
        return str(
            page.evaluate(
                "() => (document.body && document.body.innerText) ? document.body.innerText : ''"
            )
            or ""
        )
    except Exception:
        return ""


def _page_capture_from_uipage(page: Any, landing: UiPage, result: RuntimeUiResult) -> Any:
    """把已抓取的落地页（UiPage）+ 当前 DOM 文本组装成 PageCapture，供专家 S0 反推菜单。

    不依赖 engine.expert 类型（保持解耦）；返回其 PageCapture dataclass 实例。
    """
    from engine.expert.page_expert import PageCapture

    elements = [
        {"selector": e.selector, "kind": e.kind, "text": e.text, "visible": e.visible}
        for e in landing.elements
    ]
    xhr = [{"method": ep.method, "path": ep.path} for ep in result.api_endpoints]
    return PageCapture(
        url=landing.url,
        path=landing.path,
        title=landing.title,
        dom_text=_page_dom_text(page),
        elements=elements,
        console_errors=list(landing.console_errors),
        xhr_list=xhr,
        auth_mode="required",
        discovered_routes=list(result.discovered_routes),
    )


def _absorb_panel_areas(  # noqa: PLR0913, PLR0917 - 面板发现器参数多但职责单一，已收敛
    page: Any,
    options: RuntimeUiOptions,
    result: RuntimeUiResult,
    sink: ConsoleSink,
    labels: list[str],
    start_url: str,
) -> int:
    """状态驱动 SPA 兜底：点击菜单项切换面板、URL 不变时，用内容签名就地发现不同功能区域。

    返回实际采集到的面板区域数。仅当面板签名变化且为新区域时才采集，避免重复 / 抖动。
    危险菜单项（退出登录等）跳过；点击若只是展开子菜单（签名不变）也跳过，零副作用。
    """
    seen: set[str] = set()
    landing_sig = _panel_signature(page)
    seen.add(landing_sig)
    collected = 0
    limit = max(1, int(options.max_pages))
    for label in labels:
        if _is_danger_label(label):
            continue
        if collected >= limit:
            break
        before = _panel_signature(page)
        _click_label(page, label, options, [])
        try:
            page.wait_for_timeout(SPA_SETTLE_MS)
        except Exception:
            pass
        sig = _panel_signature(page)
        if not sig or sig == before or sig in seen:
            continue
        seen.add(sig)
        mark = len(sink.entries)
        up = _collect_current_page(page, f"area::{sig}", options, sink, mark)
        if up.elements:
            _absorb(result, up)
            collected += 1
            result.notes.append(
                f"面板区域发现：{label} → {sig}（就地采集 {len(up.elements)} 元素）"
            )
    _return_to(page, start_url, options)
    if collected:
        result.notes.append(
            f"状态驱动面板区域发现 {collected} 个（菜单点击未改 URL，按内容签名区分）"
        )
    return collected


def _sweep_pages(  # noqa: PLR0913, PLR0917 - 探索器签名参数多但职责单一，已收敛
    session: _Session,
    opts: RuntimeUiOptions,
    result: RuntimeUiResult,
    sink: ConsoleSink,
    static_paths: list[str] | None,
    expert_options: Any = None,
) -> int:
    """抓取登录后落地页与各路由（含菜单点击发现），返回可达页数。"""
    page = session.page
    landing = _collect_page(page, _safe_url(page) or opts.base_url, opts, sink, len(sink.entries))
    _absorb(result, landing)
    # S0 治本：专家反推菜单（仅当启用且可用），补 CSS 启发式漏掉的导航项
    extra_labels: list[str] = []
    if expert_options and getattr(expert_options, "enabled", False):
        try:
            from engine.expert.page_expert import PageExpert

            cap = _page_capture_from_uipage(page, landing, result)
            expert = PageExpert(expert_options)
            extra_labels, meta = expert.propose_routes(cap)
            if meta.get("note"):
                result.notes.append(f"[专家S0] {meta['note']}")
            if extra_labels:
                result.notes.append(
                    f"[专家S0] 反推菜单 {len(extra_labels)} 项：{', '.join(extra_labels[:10])}"
                )
        except Exception as exc:  # 专家失败不阻断主链路
            result.notes.append(f"[专家S0] 反推菜单失败，已降级：{exc}")
    menu_paths = _discover_menu_routes(
        page, session.context, opts, result, extra_labels=extra_labels
    )
    result.discovered_routes = list(menu_paths)
    routes = _resolve_routes(opts, static_paths, _crawl_links(page), menu_paths)
    for route in routes:
        if route == landing.path:
            continue
        mark = len(sink.entries)
        _absorb(result, _collect_page(page, urljoin(opts.base_url, route), opts, sink, mark))
    # 状态驱动 SPA 兜底：菜单点击只换面板不改 URL → 用内容签名就地发现不同功能区域
    panel_labels = list(_nav_labels(page))
    try:
        panel_labels.extend(_harvest_nav_labels(page))
    except Exception:
        pass
    _absorb_panel_areas(page, opts, result, sink, panel_labels, _safe_url(page) or opts.base_url)
    return sum(1 for p in result.pages if p.reachable)


# ============================================================================
# #223 click-through 深度发现（弹窗 / Tab / 表单提交后的子视图）
# ============================================================================
_TRIGGER_SELECTORS_JS = """
(function(){
  function cssPath(el){
    if(!(el instanceof Element)) return null;
    var path=[];
    while(el && el.nodeType===1){
      var nm=el.nodeName.toLowerCase();
      if(nm==='body'||nm==='html'){ path.unshift(nm); break; }
      var sel=nm;
      if(el.id){ sel+='#'+el.id; path.unshift(sel); break; }
      var sib=el.parentNode?Array.prototype.slice.call(el.parentNode.children):[];
      var cnt=0, idx=0;
      for(var i=0;i<sib.length;i++){ if(sib[i].nodeName.toLowerCase()===nm){ cnt++; if(sib[i]===el) idx=cnt; } }
      if(cnt>1) sel+=':nth-of-type('+idx+')';
      if(el.className && typeof el.className==='string' && el.className.trim()){
        sel+='.'+el.className.trim().split(/\\s+/).join('.');
      }
      path.unshift(sel);
      el=el.parentNode;
    }
    return path.join(' > ');
  }
  var verbs=__VERBS__;
  var nodes=Array.from(document.querySelectorAll('button,a,input[type="submit"],[role="button"]')).filter(function(e){
    if(e.offsetParent===null) return false;
    var t=(e.innerText||e.textContent||e.getAttribute('title')||'').trim();
    return verbs.some(function(v){ return t.indexOf(v)!==-1; });
  });
  return nodes.map(function(n){ return cssPath(n); }).filter(function(s){ return !!s; });
})()
"""


def _goto_settle(page: Any, url: str, opts: RuntimeUiOptions) -> None:
    """导航并等待首屏渲染（不重新收集基线元素，供 click-through 复用）。"""
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms(opts))
        try:
            page.wait_for_load_state("networkidle", timeout=SETTLE_AFTER_GOTO_MS)
        except Exception:
            pass
    except Exception:  # 单页跳转失败由调用方跳过该页
        pass


def _rescan_elements(page: Any) -> list[UiElement]:
    """不导航地重新抓取当前页可见元素（点击后页面状态变了但没跳转）。"""
    try:
        got = page.evaluate(_build_extract_js())
    except Exception:
        return []
    if not isinstance(got, list):
        return []
    return [
        UiElement(
            selector=str(item.get("selector") or ""),
            kind=str(item.get("kind") or ""),
            text=str(item.get("text") or ""),
            visible=bool(item.get("visible")),
        )
        for item in got
        if item.get("selector")
    ]


def _find_trigger_selectors(page: Any, verbs: tuple[str, ...]) -> list[str]:
    """找出文本命中动词、可能打开弹窗/子视图的按钮选择器（按 CSS 路径稳定定位）。"""
    js = _TRIGGER_SELECTORS_JS.replace("__VERBS__", json.dumps(list(verbs)))
    try:
        got = page.evaluate(js)
    except Exception:
        return []
    return [s for s in got if isinstance(s, str) and s] if isinstance(got, list) else []


@dataclass
class _DeepCtx:
    """click-through 单页上下文（减少 _capture_deep 参数个数，规避 PLR0913/PLR0917）。"""

    uip: UiPage
    path: str
    base: set[tuple[str, str]]
    global_seen: set[tuple[str, str, str]]
    result: RuntimeUiResult


def _capture_deep(pw_page: Any, ctx: _DeepCtx) -> int:
    """点击后重新扫描，把新出现的元素记为深层（deep）元素，返回新增数。"""
    new_els = _rescan_elements(pw_page)
    added = 0
    for ne in new_els:
        if not ne.visible:
            continue
        key = (ctx.path, ne.kind, ne.selector)
        if key in ctx.base or key in ctx.global_seen:
            continue
        ctx.global_seen.add(key)
        ne.deep = True
        ctx.uip.elements.append(ne)
        added += 1
    if added:
        ctx.result.notes.append(f"click-through 在 {ctx.path} 发现 {added} 个深层元素")
    return added


def _click_tabs(pw_page: Any, opts: RuntimeUiOptions, ctx: _DeepCtx) -> int:
    """Tab 原地切换（不导航，句柄有效）；返回本次新增交互次数。"""
    try:
        tabs = pw_page.query_selector_all("[role='tab']")
    except Exception:
        return 0
    count = 0
    for h in tabs:
        if count >= MAX_CLICKTHROUGH_PER_PAGE:
            break
        try:
            if not h.is_visible():
                continue
            h.click(timeout=5000)
            pw_page.wait_for_timeout(800)
            _capture_deep(pw_page, ctx)
            count += 1
        except Exception:  # 单点失败仅跳过，不中断
            continue
    return count


def _click_triggers(pw_page: Any, opts: RuntimeUiOptions, ctx: _DeepCtx) -> int:
    """弹窗/子视图触发按钮（文本命中 _OPEN_VERBS）：每次重置后重查句柄，避免导航后失效。"""
    count = 0
    while count < MAX_CLICKTHROUGH_PER_PAGE:
        try:
            _goto_settle(pw_page, ctx.uip.url, opts)
            sels = _find_trigger_selectors(pw_page, _OPEN_VERBS)
        except Exception:
            break
        if count >= len(sels):
            break
        sel = sels[count]
        count += 1
        try:
            h = pw_page.query_selector(sel)
            if h is None or not h.is_visible():
                continue
            h.click(timeout=5000)
            pw_page.wait_for_timeout(800)
            _capture_deep(pw_page, ctx)
            try:
                pw_page.keyboard.press("Escape")
            except Exception:
                pass
        except Exception:  # 单点失败仅跳过
            continue
    return count


def _click_pagination(pw_page: Any, opts: RuntimeUiOptions, ctx: _DeepCtx) -> int:
    """分页翻页（只读导航）：点击下一页/页码控件 → 抓取翻页后出现的新元素。

    每点一次后回到基准页，避免后续点击句柄错位；单页受 MAX_CLICKTHROUGH_PER_PAGE 约束。
    """
    count = 0
    for sel in _PAGINATION_SELECTOR:
        if count >= MAX_CLICKTHROUGH_PER_PAGE:
            break
        try:
            nodes = pw_page.query_selector_all(sel)
        except Exception:
            continue
        for node in nodes:
            if count >= MAX_CLICKTHROUGH_PER_PAGE:
                break
            try:
                if not node.is_visible():
                    continue
                node.click(timeout=5000)
                pw_page.wait_for_timeout(800)
                _capture_deep(pw_page, ctx)
                _goto_settle(pw_page, ctx.uip.url, opts)
                count += 1
            except Exception:  # 末页 disabled / 翻页异常 → 回基准页继续
                try:
                    _goto_settle(pw_page, ctx.uip.url, opts)
                except Exception:
                    pass
                continue
    return count


def _form_submit_handle(form: Any, verbs: tuple[str, ...]) -> Any | None:
    """在表单内找「提交按钮且文本命中只读动词」的句柄（创建/删除类不点）。"""
    try:
        candidates = form.query_selector_all("button, input[type=submit], [role='button']")
    except Exception:
        return None
    for c in candidates:
        try:
            t = (c.inner_text() or c.get_attribute("value") or "").strip()
        except Exception:
            t = ""
        if any(v in t for v in verbs):
            return c
    return None


def _fill_form_inputs(form: Any) -> None:
    """给文本类输入填无害探测值（仅 text/search/textarea，不碰 password/file）。"""
    try:
        inputs = form.query_selector_all(
            "input[type=text], input[type=search], input:not([type]), textarea"
        )
    except Exception:
        return
    for inp in inputs:
        try:
            inp.fill("测试")
        except Exception:
            pass


def _click_forms(pw_page: Any, opts: RuntimeUiOptions, ctx: _DeepCtx) -> int:
    """搜索/筛选表单提交（只读）：填无害值 → 点搜索/筛选 → 抓取提交后新视图。

    仅命中 `_FILTER_VERBS` 的提交按钮才点（避免误触创建/删除等写操作）；
    每次提交后回到基准页，保证后续点击稳定。属「深度发现」的只读子集。
    """
    count = 0
    try:
        forms = pw_page.query_selector_all("form")
    except Exception:
        return 0
    for form in forms:
        if count >= MAX_CLICKTHROUGH_PER_PAGE:
            break
        try:
            submit = _form_submit_handle(form, _FILTER_VERBS)
            if submit is None or not submit.is_visible():
                continue
            _fill_form_inputs(form)
            submit.click(timeout=5000)
            pw_page.wait_for_timeout(800)
            _capture_deep(pw_page, ctx)
            _goto_settle(pw_page, ctx.uip.url, opts)
            count += 1
        except Exception:
            try:
                _goto_settle(pw_page, ctx.uip.url, opts)
            except Exception:
                pass
            continue
    return count


def _click_through(
    session: _Session,
    opts: RuntimeUiOptions,
    result: RuntimeUiResult,
    sink: ConsoleSink,
) -> None:
    """#223 深度发现：对可达页面做有界 click-through，把弹窗/Tab/表单提交后出现的深层元素记为功能点。

    设计原则（与 runtime_ui 整体「降级优先、故障隔离」一致）：
    - 每页先加载到干净态，逐一点击 Tabs / 弹窗触发按钮；Tab 原地切换（句柄有效），
      弹窗触发每次重置后重查句柄（避免导航导致句柄失效）；
    - 新出现的 selector（不在该页基线 + 全局已见集合）记为 deep 元素，追加到 page.elements；
    - 全程 try/except 故障隔离：任一交互异常仅跳过并记 degraded，绝不中断整轮发现；
    - 受 MAX_CLICKTHROUGH_* 护栏约束，保证运行时长与产物量级可控。
    """
    pw_page = session.page
    pages = list(result.reachable_pages())[:MAX_CLICKTHROUGH_PAGES]
    global_seen: set[tuple[str, str, str]] = set()
    total = 0
    for p in pages:
        if total >= MAX_CLICKTHROUGH_TOTAL:
            break
        path = p.path or "/"
        _goto_settle(pw_page, p.url, opts)
        ctx = _DeepCtx(
            uip=p,
            path=path,
            base={(e.kind, e.selector) for e in p.elements},
            global_seen=global_seen,
            result=result,
        )
        total += _click_tabs(pw_page, opts, ctx)
        total += _click_triggers(pw_page, opts, ctx)
        total += _click_pagination(pw_page, opts, ctx)
        total += _click_forms(pw_page, opts, ctx)
    if total:
        result.notes.append(
            "click-through 深度发现完成：共 "
            f"{total} 次交互（Tab/弹窗-抽屉触发/分页翻页/搜索筛选表单提交）"
        )


# ============================================================================
# G-7 移动端 / 响应式发现（手机视口 + /m/* 专属路由）
# ============================================================================
# 用主流手机视口（iPhone 12/13 逻辑分辨率）模拟移动端；has_touch 让点击走触摸事件。
_MOBILE_VIEWPORT: dict[str, int] = {"width": 390, "height": 844}
_MOBILE_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/15.0 Mobile/15E148 Safari/604.1"
)
# 移动端专属路由发现：抓取所有指向 /m/* 的链接 / data-route 提示（SPA 常无 <a>）。
_MOBILE_M_ROUTES_JS = """
(function(){
  var sel = 'a[href], [data-route], [data-to], [data-path]';
  var nodes = Array.from(document.querySelectorAll(sel));
  var out = [];
  nodes.forEach(function(n){
    var h = n.getAttribute('href') || n.getAttribute('data-route') ||
            n.getAttribute('data-to') || n.getAttribute('data-path') || '';
    if (h && h.indexOf('/m/') === 0) out.push(h);
  });
  return out;
})()
"""


def _discover_mobile(
    session: _Session, opts: RuntimeUiOptions, result: RuntimeUiResult, sink: ConsoleSink
) -> int:
    """G-7：移动端视口发现——用手机视口打开被测页，抽取移动专属元素并标记 `mobile=True`，
    同时发现 /m/* 专属路由。

    复用于桌面发现的同一套元素抽取脚本（`_build_extract_js`）；与桌面元素的差异仅在于
    「视口/交互方式」不同（底部 Tab、手势、下拉刷新等移动专属交互在手机视口下才出现）。
    已存在于桌面的元素不重复计入（按 selector 去重），只保留移动专属增量。
    故障隔离：单页异常不影响主链路（调用方已包 try）。
    """
    page = session.page
    before = len(result.elements)
    raw: list[dict[str, Any]] = []
    try:
        got = page.evaluate(_build_extract_js())
        if isinstance(got, list):
            raw = [x for x in got if isinstance(x, dict)]
    except Exception:  # 元素抽取失败只记降级，不中断
        raw = []
    seen = {e.selector for e in result.elements}
    added = 0
    for item in raw:
        sel = str(item.get("selector") or "")
        if not sel or sel in seen:
            continue
        result.elements.append(
            UiElement(
                selector=sel,
                kind=str(item.get("kind") or ""),
                text=str(item.get("text") or ""),
                visible=bool(item.get("visible")),
                mobile=True,
            )
        )
        seen.add(sel)
        added += 1
    # /m/* 专属路由发现
    m_routes: list[str] = []
    try:
        links = page.evaluate(_MOBILE_M_ROUTES_JS)
        if isinstance(links, list):
            m_routes = [str(x) for x in links if isinstance(x, str) and x.startswith("/m/")]
    except Exception:
        m_routes = []
    if m_routes:
        result.discovered_routes.extend(m_routes)
    result.notes.append(
        f"移动端发现完成：新增移动专属元素 {added} 个（去重后），/m/* 专属路由 {len(m_routes)} 条"
    )
    return len(result.elements) - before


def _discover_mobile_pass(
    browser: Any, opts: RuntimeUiOptions, result: RuntimeUiResult, sink: ConsoleSink
) -> None:
    """G-7 主链路入口：建手机视口上下文 → 登录 → 调 `_discover_mobile`，故障隔离。"""
    m_context: Any = None
    try:
        m_context = browser.new_context(
            ignore_https_errors=True,
            is_mobile=True,
            has_touch=True,
            viewport=_MOBILE_VIEWPORT,
            user_agent=_MOBILE_USER_AGENT,
        )
        m_context.set_default_timeout(timeout_ms(opts))
        m_page, _ = open_authenticated_page(
            m_context, opts, result, attach=lambda p: attach_console_listeners(p, sink)
        )
        _discover_mobile(_Session(m_page, m_context), opts, result, sink)
    except Exception as exc:  # 移动端发现失败绝不阻断主链路
        result.degraded = True
        result.notes.append(f"移动端发现降级：{exc}")
    finally:
        close_quietly(m_context)


# ============================================================================
# 产出：运行时发现 → 功能点（M3.4）
# ============================================================================
def _module_of_path(path: str) -> str:
    """由路径首段推导模块名（`/pc/tasks` → `pc`；根路径 → `root`）。"""
    segs = [s for s in (path or "").split("/") if s]
    return segs[0] if segs else "root"


def _desensitize_runtime_source(url: str) -> str:
    """地址通道来源脱敏：剥离 URL 中的 `userinfo`（账号:密码@），保证 `runtime:<url>`
    这种 file_path **绝不**含明文账号密码（凭证红线，与 core.config 同一红线）。

    登录走表单提交，页面 URL 通常不含凭据；但万一用户用 `https://user:pass@host` 直达，
    这里兜底剥离 userinfo，仅保留 scheme / host / port / path，避免凭证随产物落盘。
    """
    try:
        p = urlparse(url)
        if not p.netloc or "@" not in p.netloc:
            return url
        # urlparse 用「最后一个 @」区分 userinfo 与 host，故密码里若含 @ 也能正确剥离
        netloc = p.hostname or ""
        if p.port is not None:
            netloc = f"{netloc}:{p.port}"
        return urlunparse((p.scheme, netloc, p.path, p.params, p.query, p.fragment))
    except Exception:  # 解析失败则原样返回，不阻断主链路
        return url


# 元素 kind → 动作类型（中文，写入功能点语义，供用例步骤推断「点击/输入/选择/提交」）
_ELEMENT_ACTION_CN: dict[str, str] = {
    "nav": "点击",
    "button": "点击",
    "input": "输入",
    "textarea": "输入",
    "select": "选择",
    "form": "提交",
    "edit": "输入",
}


def _element_action(kind: str) -> str:
    return _ELEMENT_ACTION_CN.get(kind, "操作")


# #224 量级护栏（防爆炸与噪声）：单页元素上限 + 单项目功能点总量上限。
# 元素级分解（#221）后单页可达数十~数百元素，全量易破千；护栏保证产物落在
# 「数百」合理区间，且高价值路径排在前面。
MAX_ELEMENTS_PER_PAGE = 150  # 单页可见元素上限（超出截断，防单页刷量）
MAX_FP_TOTAL = 800  # 单项目功能点总量上限（超出按业务价值截断）
# #223 click-through 深度发现（弹窗/Tab/表单）护栏：有界、故障隔离
MAX_CLICKTHROUGH_PAGES = 10  # 参与深度点击的页面上限（保运行时长可控）
MAX_CLICKTHROUGH_PER_PAGE = 8  # 单页尝试的交互（Tab + 弹窗触发）上限
MAX_CLICKTHROUGH_TOTAL = 40  # 总交互次数上限（防爆炸）
# 文本命中即视为「可能打开弹窗/子视图」的按钮（命中才点，避免误触导航/提交）
_OPEN_VERBS = (
    "新建",
    "添加",
    "新增",
    "创建",
    "设置",
    "详情",
    "查看",
    "更多",
    "编辑",
    "修改",
    "打开",
    "展开",
    "筛选",
    "搜索",
    "导出",
    "上传",
    "导入",
    "配置",
    "管理",
)

# 分页控件选择器（命中即尝试翻页，抓取下一页深层元素；只读导航，安全）
_PAGINATION_SELECTOR = (
    "[class*='pagination'] a",
    "[class*='pager'] a",
    "a[rel='next']",
    "[aria-label*='next' i]",
    "button[class*='next']",
    ".page-item a",
    "[class*='page'] a",
)

# 表单提交动词（仅命中「搜索/筛选」类只读提交才点，避免误触创建/删除等写操作）
_FILTER_VERBS = ("搜索", "查询", "筛选", "过滤", "查找", "刷新", "搜")

# 功能点业务价值排序（越小越优先）：页面结构 > 后端接口 > 交互元素 > 组件 > 业务函数
_FP_VALUE_RANK: dict[str, int] = {
    FType.PAGE.value: 0,
    FType.API.value: 1,
    FType.UI.value: 2,
    FType.COMPONENT.value: 3,
    FType.BUSINESS.value: 4,
}
# 交互元素 kind 的二次排序：数据录入类（form/input/textarea/edit）优先于点击/导航
_UI_KIND_RANK: dict[str, int] = {
    "form": 0,
    "input": 1,
    "textarea": 2,
    "edit": 3,
    "button": 4,
    "select": 5,
    "nav": 6,
}


def _fp_sort_key(fp: FunctionalPoint) -> tuple[int, str, int, str]:
    """功能点业务价值排序键（确定性，便于复跑顺序稳定）。"""
    kind = ""
    if fp.ftype == FType.UI.value:
        # name 形如 /chat#button:发送|#send-btn → 取首个 '#xxx:' 的 xxx
        idx = fp.name.find("#")
        if idx != -1:
            seg = fp.name[idx + 1 :].split(":", 1)[0]
            if seg.isalpha():
                kind = seg
    return (_FP_VALUE_RANK.get(fp.ftype, 9), fp.module or "", _UI_KIND_RANK.get(kind, 9), fp.name)


_STATIC_ASSET_EXT = (
    ".js",
    ".css",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".webp",
    ".woff",
    ".woff2",
    ".ttf",
    ".ico",
    ".webmanifest",
    ".map",
)


def _attach_api_listener(page: Any, result: RuntimeUiResult, base_url: str) -> None:
    """监听页面 XHR/fetch 响应，把同源后端接口记为 api 功能点来源（#223）。

    判定：同源 +（content-type 含 json 或路径含 /api/）视为接口；排除静态资源与
    跨域请求。按 (method, path) 去重（忽略 query，避免同接口不同参数刷量）。
    监听失败一律静默，不阻断主发现链路。
    """
    from urllib.parse import urlparse

    base = urlparse(base_url)
    seen: set[tuple[str, str]] = set()

    def on_response(response: Any) -> None:
        try:
            u = urlparse(response.url)
            if not u.netloc or u.netloc != base.netloc:
                return
            path = u.path.split("?")[0]
            if path.endswith(_STATIC_ASSET_EXT):
                return
            ct = (response.headers.get("content-type") or "").lower()
            is_api = ("application/json" in ct) or ("/api/" in u.path) or u.path.startswith("/api")
            if not is_api:
                return
            method = (response.request.method or "GET").upper()
            key = (method, path)
            if key in seen:
                return
            seen.add(key)
            result.api_endpoints.append(
                ApiEndpoint(method=method, path=path, url=response.url, status=response.status)
            )
        except Exception:  # 监听异常不应阻断主链路
            return

    page.on("response", on_response)


def _element_fp_name(page_path: str, el: UiElement) -> str:
    """元素级功能点的稳定机器键（决定 fp_id 稳定）。

    键 = 页面路径 + kind + 可见文字(截断) + selector。同页面内 (kind, selector)
    唯一即可保证编号不撞号；文字仅用于消歧，不参与稳定性（文字变化不改编号，
    符合「内容指纹」契约）。
    """
    text_part = (el.text or "").strip().replace("\n", " ")[:24]
    return f"{page_path}#{el.kind}:{text_part or 'el'}|{el.selector}"


def _element_fp_title(page_path: str, el: UiElement, label: str) -> str:
    kind_cn = _ELEMENT_KIND_CN.get(el.kind, "元素")
    text = (el.text or "").strip().replace("\n", " ")
    if text:
        return f"{kind_cn}「{text}」@ {label}"
    return f"{kind_cn}（{el.selector}）@ {label}"


@dataclass
class _EmitCtx:
    """to_functional_points 的跨页面共享上下文（减少 _emit_* 参数个数，规避 PLR0913/PLR0917）。"""

    uip: UiPage
    path: str
    src: str
    label: str
    module: str
    out: list[FunctionalPoint]
    seen: set[tuple[str, str]]
    seen_element: set[tuple[str, str, str]]


def to_functional_points(result: RuntimeUiResult) -> list[FunctionalPoint]:
    """把运行时发现结果转成功能点（复用既有契约，**不新造字段**）。

    规则（#221 元素级分解 + #223 接口拦截 + #224 量级护栏）：
    - 每个**可达页面** → 1 条 `page` 功能点（验证页面可达）；
    - 每个**可见交互元素** → 1 条 `ui` 功能点（button/input/select/textarea/edit/nav/form
      各自成点，带文字 / selector / 动作类型），解决「整页元素被打包成一条」导致地址通道
      用例极少的问题（G0-2）；单页元素超 `MAX_ELEMENTS_PER_PAGE` 截断，防单页刷量；
    - 运行时拦截到的**同源后端接口** → 1 条 `api` 功能点（#223，覆盖参数/鉴权边界）；
    - 同 (kind, selector) / (method, path) 去重，避免近乎重复刷量；
    - 全部功能点按**业务价值排序**（页面 > 接口 > 表单/输入 > 按钮/导航），超
      `MAX_FP_TOTAL` 截断（#224 护栏，保证落「数百」合理区间）；
    - `file_path` = `runtime:<url>`（脱敏，不含凭证）；
    - `fp_id` = `fp_id_of(ftype, runtime:<url>, name)` → 同环境重复跑编号稳定。

    只产出**可达**页面，不可达页（goto 失败）不产出功能点，避免生成必然失败的用例。
    """
    out: list[FunctionalPoint] = []
    seen: set[tuple[str, str]] = set()  # (ftype, name) 全局去重
    seen_element: set[tuple[str, str, str]] = set()  # (page_path, kind, selector) 元素去重
    for page in result.reachable_pages():
        path = page.path or "/"
        module = _module_of_path(path)
        src = f"runtime:{_desensitize_runtime_source(page.url)}"
        page_title = (page.title or "").strip()
        label = f"{page_title}｜{path}" if page_title else path
        ctx = _EmitCtx(
            uip=page,
            path=path,
            src=src,
            label=label,
            module=module,
            out=out,
            seen=seen,
            seen_element=seen_element,
        )
        _emit_page_fp(ctx)
        _emit_element_fps(ctx)
    _emit_api_fps(result, out, seen)
    # #224 量级护栏：业务价值排序 + 总量上限截断
    out.sort(key=_fp_sort_key)
    if len(out) > MAX_FP_TOTAL:
        out = out[:MAX_FP_TOTAL]
        result.notes.append(f"功能点超总量上限 {MAX_FP_TOTAL}，已按业务价值截断")
    return out


def _emit_page_fp(ctx: _EmitCtx) -> None:
    """每条可达页产出 1 条 `page` 功能点（验证页面可达）。"""
    page_fp = FunctionalPoint(
        fp_id=fp_id_of(FType.PAGE.value, ctx.src, ctx.path),
        ftype=FType.PAGE.value,
        file_path=ctx.src,
        name=ctx.path,
        title=f"页面 {ctx.label}",
        module=ctx.module,
        semantic=f"运行时发现页面 {ctx.path}（{(ctx.uip.title or '').strip() or '无标题'}）",
        description=ctx.uip.url,
    )
    if (page_fp.ftype, page_fp.name) not in ctx.seen:
        ctx.seen.add((page_fp.ftype, page_fp.name))
        ctx.out.append(page_fp)


def _emit_element_fps(ctx: _EmitCtx) -> None:
    """#221 元素级分解：每个可见交互元素 1 条 `ui` 功能点（带文字/selector/动作）。

    单页元素超 `MAX_ELEMENTS_PER_PAGE` 截断（#224 防单页刷量）；同 (kind, selector)
    去重。深层视图（click-through）元素带 `deep` 标记，语义注明「深层视图」。
    """
    page_ui_count = 0
    for el in ctx.uip.elements:
        if not el.visible:
            continue
        if page_ui_count >= MAX_ELEMENTS_PER_PAGE:
            break
        dk = (ctx.path, el.kind, el.selector)
        if dk in ctx.seen_element:
            continue
        ctx.seen_element.add(dk)
        name = _element_fp_name(ctx.path, el)
        if (FType.UI.value, name) in ctx.seen:
            continue
        ctx.seen.add((FType.UI.value, name))
        action = _element_action(el.kind)
        kind_cn = _ELEMENT_KIND_CN.get(el.kind, "元素")
        deep_note = "（深层视图·click-through）" if el.deep else ""
        ctx.out.append(
            FunctionalPoint(
                fp_id=fp_id_of(FType.UI.value, ctx.src, name),
                ftype=FType.UI.value,
                file_path=ctx.src,
                name=name,
                title=_element_fp_title(ctx.path, el, ctx.label),
                module=ctx.module,
                semantic=(
                    f"页面 {ctx.path} 可见{kind_cn}交互元素{deep_note}："
                    f"文字={el.text or '（无文字）'}，动作={action}，选择器={el.selector}"
                ),
                description=el.selector,
            )
        )
        page_ui_count += 1


def _emit_api_fps(
    result: RuntimeUiResult, out: list[FunctionalPoint], seen: set[tuple[str, str]]
) -> None:
    """#223 运行时拦截到的同源后端接口 → 1 条 `api` 功能点（覆盖参数/鉴权边界）。"""
    api_src = f"runtime:{_desensitize_runtime_source(result.base_url)}"
    for ep in result.api_endpoints:
        name = f"{ep.method} {ep.path}"
        if (FType.API.value, name) in seen:
            continue
        seen.add((FType.API.value, name))
        out.append(
            FunctionalPoint(
                fp_id=fp_id_of(FType.API.value, api_src, name),
                ftype=FType.API.value,
                file_path=api_src,
                name=name,
                title=f"接口 {ep.method} {ep.path}（运行时拦截）",
                module=_module_of_path(ep.path),
                semantic=(
                    f"运行时拦截到后端接口 {ep.method} {ep.path}（样例响应状态 {ep.status}）"
                ),
                description=ep.url,
            )
        )


def to_runtime_index(result: RuntimeUiResult | None) -> dict[str, RuntimePageInfo]:
    """把发现结果转成「路径 → 页面运行时细节」索引（A2）。

    用途：`case_gen` 按测试点的 `area`（页面路径）取出真实元素 / 控制台错误，
    写进用例正文与机器步——这是「URL 生成」与「代码生成」用例正文**唯一**的区分点。
    只收**可达**页面；不可达页面的细节没有验证价值。
    """
    if result is None:
        return {}
    index: dict[str, RuntimePageInfo] = {}
    for page in result.reachable_pages():
        path = page.path or "/"
        index[path] = RuntimePageInfo(
            path=path,
            url=page.url,
            title=(page.title or "").strip(),
            elements=[
                {
                    "kind": e.kind,
                    "text": e.text,
                    "visible": e.visible,
                    "selector": e.selector,
                }
                for e in page.elements
            ],
            console_errors=list(page.console_errors),
        )
    return index


# ============================================================================
# 主入口
# ============================================================================
def open_authenticated_page(
    context: Any,
    options: RuntimeUiOptions,
    result: RuntimeUiResult,
    attach: Any = None,
) -> tuple[Any, bool]:
    """注入令牌 → 新建页面 →（可选）挂控制台监听 → 表单登录；返回 `(页面, 是否已登录)`。

    为什么收成一个公共入口：运行时发现（`discover_ui`）与 UI 层用例执行
    （`engine/ui_executor.py`）需要**同一套**「令牌 + 建页 + 登录」动作。
    各写一遍必然漂移，而「登录态行为不一致」是最难排查的一类差异
    （表现往往是「一半用例通过、一半莫名失败」）。

    `attach` 由调用方传入（发现通道要收集控制台错误与页面事件），保持本函数与
    「监听什么」解耦。
    """
    _apply_token(context, options, result)
    page = context.new_page()
    if attach is not None:
        attach(page)
    logged_in = _login(page, options, result)
    return page, logged_in


def launch_browser(playwright: Any, options: RuntimeUiOptions) -> Any:
    """启动浏览器：优先配置的 channel（可复用系统 Edge，免 150MB 内核下载）。

    浏览器出口代理（内网目标经沙箱代理可达）：`proxy` 非空时透传给 Playwright 的
    `launch(proxy=...)`。直连在 HTTP 层常挂起，故缺省从环境变量推断代理。

    Chromium 自有沙箱：在 CI / 受限容器 / 沙箱化运行环境里常因权限不足启动失败。
    由 `PLAYWRIGHT_CHROMIUM_SANDBOX=0`（或 false/off/no）显式关闭；缺省不关，
    保持普通机器上的安全默认。关闭后附带 `--disable-dev-shm-usage` 规避 /dev/shm 过小。
    """
    kwargs: dict[str, Any] = {"headless": bool(options.headless)}
    if options.channel:
        kwargs["channel"] = options.channel
    if options.proxy:
        kwargs["proxy"] = {"server": options.proxy}
    sandbox_flag = str(os.environ.get("PLAYWRIGHT_CHROMIUM_SANDBOX", "")).strip().lower()
    args: list[str] = []
    if sandbox_flag in ("0", "false", "off", "no"):
        args += ["--no-sandbox", "--disable-dev-shm-usage"]
    # 无显式代理时强制直连：避免 Chromium 继承本机透明代理而连不上公网目标
    if not options.proxy:
        args.append("--no-proxy-server")
    if args:
        kwargs["args"] = args
    try:
        return playwright.chromium.launch(**kwargs)
    except Exception as exc:  # 启动失败要带上环境信息提示用户
        hint = (
            "请先执行 `python -m playwright install chromium`，"
            "或在已装 Edge/Chrome 的机器上设 PLAYWRIGHT_CHANNEL=msedge"
        )
        channel = options.channel or "chromium"
        raise EngineError(f"浏览器启动失败（channel={channel}）：{exc}；{hint}") from exc


def close_quietly(*resources: Any) -> None:
    """尽力关闭浏览器上下文（不留驻 cookie / storage）。"""
    for item in resources:
        if item is None:
            continue
        try:
            item.close()
        except Exception:  # 关闭失败不应覆盖主流程结论
            continue


def _record_summary(result: RuntimeUiResult, logged_in: bool, reachable: int) -> None:
    result.logged_in = logged_in
    result.notes.append(
        f"运行时发现完成：登录={'是' if logged_in else '否'}，"
        f"可达页面 {reachable}/{len(result.pages)}，元素 {len(result.elements)} 个，"
        f"菜单发现路由 {len(result.discovered_routes)} 条，"
        f"控制台错误 {len(result.console_errors)} 条"
    )
    log.info(
        "运行时 UI 发现完成",
        extra=log_extra(
            pages=len(result.pages),
            reachable=reachable,
            elements=len(result.elements),
            routes=len(result.discovered_routes),
            console_errors=len(result.console_errors),
            logged_in=logged_in,
        ),
    )


def discover_ui(
    options: RuntimeUiOptions | None = None,
    *,
    static_paths: list[str] | None = None,
    expert_options: Any = None,
) -> RuntimeUiResult:
    """打开被测环境，登录并遍历路由，抓取可交互元素与控制台错误。

    `static_paths` 为静态分析已提取的页面路径（路由优先级第 3 档）。
    `expert_options` 为 PageExpert 选项（测试专家系统 · S0 治本）；不传则不做专家反推。
    """
    opts = options or RuntimeUiOptions()
    if opts.mode not in RUNTIME_UI_MODE_CHOICES:
        raise ValueError(f"未知的运行时 UI 发现模式：{opts.mode!r}，允许 {RUNTIME_UI_MODE_CHOICES}")
    if not opts.base_url:
        raise EngineError("运行时 UI 发现缺少被测地址（RUNTIME_BASE_URL）")
    if not has_playwright():
        raise EngineError(INSTALL_HINT)

    from playwright.sync_api import sync_playwright

    result = RuntimeUiResult(base_url=opts.base_url)
    sink = ConsoleSink()
    browser = None
    context = None
    try:
        with sync_playwright() as pw:
            browser = launch_browser(pw, opts)
            context = browser.new_context(ignore_https_errors=True)
            context.set_default_timeout(timeout_ms(opts))
            page, logged_in = open_authenticated_page(
                context, opts, result, attach=lambda p: attach_console_listeners(p, sink)
            )
            _attach_api_listener(page, result, opts.base_url)  # #223：监听 XHR/fetch
            session = _Session(page, context)
            reachable = _sweep_pages(session, opts, result, sink, static_paths, expert_options)
            if reachable == 0:
                raise EngineError(f"运行时 UI 发现失败：所有页面均不可达（{opts.base_url}）")
            # G-10：首屏抓完后若会话已过期被弹回登录页，先原地重登录再进深度发现
            _relogin_if_needed(page, opts, result)
            # #223 深度发现：click-through 弹窗/Tab/表单，故障隔离（异常仅记 degraded）
            try:
                _click_through(session, opts, result, sink)
            except Exception as exc:  # 深度发现失败绝不阻断主链路
                result.degraded = True
                result.notes.append(f"click-through 深度发现降级：{exc}")
            # G-7：移动端视口发现（手机视口 + /m/* 专属路由），故障隔离
            if opts.mobile_enabled:
                _discover_mobile_pass(browser, opts, result, sink)
            _record_summary(result, logged_in, reachable)
    except EngineError:
        raise
    except Exception as exc:  # 浏览器驱动异常面很宽，统一转 EngineError
        raise EngineError(f"运行时 UI 发现失败：{exc}") from exc
    finally:
        close_quietly(context, browser)
    return result
