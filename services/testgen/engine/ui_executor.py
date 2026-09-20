"""引擎 · 模块十二（F13）：UI 层执行器（Playwright 真实渲染 + 元素级断言 + 失败截图）。

设计定位
--------
接口层用例走 `executor._probe_http`（真发 HTTP）；UI 层用例走本模块——**真的把浏览器
打开、真的渲染页面、真的按 selector 断言元素可见**。在此之前 UI 层一律返回 `skipped`，
把「没跑」写进结论；现在它给出 pass / fail / error，前提是有地址、有浏览器、有用例细节。

三条诚实约束（与 A3 同一口径）
------------------------------
1. **不可执行一律 `skipped` 并写明原因**：Playwright 未安装 / 未给地址 / 用例缺页面地址。
   绝不静默通过——「没跑」被读成「跑过了」比报错危险得多。
2. **非 pass 必留证据**：尝试落一张截图（`runs.screenshot_path`），没有证据的失败无法复盘。
3. **点击默认不做**：`ui_click` 未开启时只断言「页面可达 + 元素可见」；开启后才对交互元素
   真实点击。与接口层 `allow_write` 同一套「防污染被测环境」思路。

与 `runtime_ui` 的分工：本模块复用其浏览器启动 / 登录态建立 / 关闭逻辑
（`launch_browser` / `open_authenticated_page` / `close_quietly`），只负责「单条用例的判定」，
不重复实现驱动细节——两处各写一遍登录逻辑，必然出现「一半用例通过、一半莫名失败」。

依赖方向严格向下（只 import core 与同层 runtime_ui），不感知 service / cli。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.contracts import CaseSpec
from core.enums import ExecStatus
from core.log import get_logger, log_extra
from engine import runtime_ui


log = get_logger(__name__)


# ============================================================================
# 断言种类（机器可读契约；与 `engine.case_gen.design_assertions` 一一对应）
# ============================================================================
ASSERT_PAGE_RENDERED = "page_rendered"  # 页面渲染出正文（非白屏）
ASSERT_ELEMENT_VISIBLE = "element_visible"  # selector 命中且可见（F9：selector → 断言）
ASSERT_CONSOLE_WITHIN_BASELINE = "console_within_baseline"  # 控制台错误不超基线
ASSERT_INTERACTION_OK = "interaction_ok"  # 交互元素可点击且不新增报错

# 导航后等首屏（单用例口径：比发现通道的 2.5s 更短，SPA 首帧即可判定）
SETTLE_AFTER_GOTO_MS = 1200
# 点击后等界面响应
SETTLE_AFTER_CLICK_MS = 600

# 白屏判定：`document.body.innerText` 的长度下限
MIN_VISIBLE_TEXT = 1

# 截图文件名净化（用例编号可能含 `/` 等路径字符）
_UNSAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_MAX_FILENAME = 60
# 浏览器异常文案入结论前截断，避免把整段堆栈塞进 runs.detail
_MAX_REASON = 160

SKIP_NO_SESSION = (
    "UI 层执行会话不可用（Playwright 未安装或未提供被测地址）：本次未执行，**不等于通过**"
)
SKIP_NO_TARGET = (
    "用例缺少页面地址（`steps[0].runtime.url` / `path` 不是路由）：无法定位页面，"
    "本次未执行，**不等于通过**"
)

# 状态驱动面板区域：发现阶段存的是内容签名 `area::<签名>`，不是可导航路由。
# 执行器只会 `page.goto(路由)`，直接 goto 签名会抛错并污染成「error」假结论。
# 诚实转为 skip（写明需父路由+签名协同），既不假通过也不假失败。
_PANEL_AREA_PREFIXES = ("area::", "runtime:area::")
SKIP_PANEL_AREA = (
    "状态驱动面板区域用例（area:: 签名）：执行器暂不支持按内容签名重定位面板"
    "（需发现侧补记父路由 + 执行侧按签名定位），本次跳过，**不等于通过**"
)


def _is_panel_area_url(url: str) -> bool:
    """判断目标地址是否面板内容签名（非可导航路由）。"""
    u = str(url or "").strip().lower()
    return any(u.startswith(p) for p in _PANEL_AREA_PREFIXES)


# ============================================================================
# 选项与结果
# ============================================================================
@dataclass
class UiExecOptions:
    """UI 层执行选项（凭证只在此对象内传递，**不落库、不入产物**）。"""

    base_url: str = ""
    headless: bool = True
    channel: str = ""  # ""=自带 chromium；"msedge"/"chrome"=复用系统浏览器
    timeout: int = 30
    screenshot_dir: str = ""  # 非 pass 时落截图的目录（空=不落）
    ui_click: bool = False  # 是否执行真实点击（会改状态，默认关）
    console_slack: int = 0  # 允许超出控制台错误基线的条数
    auth_token: str = ""  # 令牌型鉴权（不入库）
    login_url: str = ""
    login_user: str = ""
    login_password: str = ""
    login_otp: str = ""

    def runtime_options(self) -> runtime_ui.RuntimeUiOptions:
        """映射到浏览器通道选项（复用其令牌注入与表单登录实现）。"""
        return runtime_ui.RuntimeUiOptions(
            headless=self.headless,
            base_url=self.base_url,
            auth_token=self.auth_token,
            timeout=self.timeout,
            channel=self.channel,
            login_url=self.login_url,
            login_user=self.login_user,
            login_password=self.login_password,
            login_otp=self.login_otp,
        )


@dataclass
class UiExecResult:
    """单条 UI 用例的执行结论。

    结构上对齐 `executor.ExecutionResult`，但**独立定义**：`ui_executor` 不 import
    `executor`（否则两个模块互相依赖，任一方改动都会被另一方牵制）。
    """

    status: str = ExecStatus.SKIPPED.value
    notes: list[str] = field(default_factory=list)
    assertions: list[dict[str, Any]] = field(default_factory=list)
    screenshot_path: str = ""


# ============================================================================
# 用例 → 目标地址 / 断言
# ============================================================================
def _timeout_ms(options: UiExecOptions) -> int:
    return max(1000, int(options.timeout) * 1000)


def _join_url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def target_url(step: dict[str, Any], options: UiExecOptions) -> str:
    """用例的目标页面地址。

    `runtime.url` 优先（运行时发现拿到的真实地址，含真实主机）；
    否则用「被测地址 + 路由路径」，但**仅当 path 像路由**（以 `/` 开头）——
    组件名（如 `SearchBar`）拼出来的是垃圾 URL，硬导航会产出**假失败**。
    """
    runtime = step.get("runtime") or {}
    url = str(runtime.get("url") or "").strip()
    if url:
        return url
    path = str(step.get("path") or "").strip()
    if path.startswith("/") and options.base_url:
        return _join_url(options.base_url, path)
    return ""


def default_assertions(runtime: dict[str, Any]) -> list[dict[str, Any]]:
    """无显式断言时按运行时细节兜底（老产物没有 `assertions` 字段也能执行）。"""
    specs: list[dict[str, Any]] = [
        {"kind": ASSERT_PAGE_RENDERED},
        {
            "kind": ASSERT_CONSOLE_WITHIN_BASELINE,
            "baseline": int(runtime.get("console_error_baseline") or 0),
        },
    ]
    for element in runtime.get("elements") or []:
        selector = str(element.get("selector") or "")
        if selector:
            specs.append(
                {
                    "kind": ASSERT_ELEMENT_VISIBLE,
                    "selector": selector,
                    "text": str(element.get("text") or ""),
                }
            )
    return specs


def assertions_of(step: dict[str, Any]) -> list[dict[str, Any]]:
    """取用例断言；缺省时由运行时细节兜底。"""
    specs = step.get("assertions")
    if isinstance(specs, list) and specs:
        return [dict(s) for s in specs if isinstance(s, dict)]
    return default_assertions(dict(step.get("runtime") or {}))


# ============================================================================
# 单项断言（每项返回 `(是否通过, 人类可读说明)`）
# ============================================================================
def _check_page_rendered(page: Any) -> tuple[bool, str]:
    try:
        length = page.evaluate(
            "() => (document.body && document.body.innerText || '').trim().length"
        )
    except Exception as exc:  # noqa: BLE001 - 读取失败按「未通过」处理，绝不猜成通过
        return False, f"无法读取页面正文：{type(exc).__name__}"
    if int(length or 0) < MIN_VISIBLE_TEXT:
        return False, "页面正文为空（疑似白屏 / 渲染失败）"
    return True, "页面已渲染出正文内容"


def _check_element_visible(page: Any, selector: str) -> tuple[bool, str]:
    if not selector.strip():
        return False, "断言缺少 selector（无法定位元素）"
    try:
        locator = page.locator(selector)
        if int(locator.count()) == 0:
            return False, f"未找到元素：{selector}"
        if not bool(locator.first.is_visible()):
            return False, f"元素存在但不可见：{selector}"
    except Exception as exc:  # noqa: BLE001 - 定位异常（选择器非法等）按未通过处理
        return False, f"元素断言异常：{selector}（{type(exc).__name__}）"
    return True, f"元素可见：{selector}"


def _check_console(console_errors: list[str], baseline: int, slack: int) -> tuple[bool, str]:
    allowed = max(0, int(baseline)) + max(0, int(slack))
    count = len(console_errors)
    if count > allowed:
        sample = console_errors[0][:80] if console_errors else ""
        return False, f"控制台错误 {count} 条超过基线 {allowed} 条（示例：{sample}）"
    return True, f"控制台错误 {count} 条，未超基线 {allowed} 条"


def _check_interaction(page: Any, selector: str, options: UiExecOptions) -> tuple[bool, str]:
    """真实点击交互元素（会改状态，只在 `ui_click` 开启时执行）。"""
    if not selector.strip():
        return False, "交互断言缺少 selector"
    if not options.ui_click:
        return True, f"未开启真实点击（ui_click=off），仅记录交互点：{selector}"
    try:
        page.locator(selector).first.click(timeout=_timeout_ms(options))
        page.wait_for_timeout(SETTLE_AFTER_CLICK_MS)
    except Exception as exc:  # noqa: BLE001 - 点击失败按未通过处理，不中断
        return False, f"点击失败：{selector}（{type(exc).__name__}）"
    return True, f"已点击并等待界面响应：{selector}"


def _evaluate_assertions(
    page: Any,
    specs: list[dict[str, Any]],
    options: UiExecOptions,
    console_errors: list[str],
) -> list[dict[str, Any]]:
    """逐条执行断言，产出机器可读结果（未知种类按**未通过**处理，不放过）。"""
    outcomes: list[dict[str, Any]] = []
    for spec in specs:
        kind = str(spec.get("kind") or "")
        ok, detail = _run_assertion(page, kind, spec, options, console_errors)
        outcomes.append({"kind": kind, "ok": ok, "detail": detail})
    return outcomes


def _run_assertion(
    page: Any,
    kind: str,
    spec: dict[str, Any],
    options: UiExecOptions,
    console_errors: list[str],
) -> tuple[bool, str]:
    if kind == ASSERT_PAGE_RENDERED:
        return _check_page_rendered(page)
    if kind == ASSERT_ELEMENT_VISIBLE:
        return _check_element_visible(page, str(spec.get("selector") or ""))
    if kind == ASSERT_CONSOLE_WITHIN_BASELINE:
        return _check_console(
            console_errors, int(spec.get("baseline") or 0), int(options.console_slack)
        )
    if kind == ASSERT_INTERACTION_OK:
        return _check_interaction(page, str(spec.get("selector") or ""), options)
    return False, f"未知断言种类 {kind!r}：执行器无法判定，按未通过处理"


def _screenshot(page: Any, options: UiExecOptions, tc_no: str) -> str:
    """落一张失败截图；任何失败都返回空串（截图失败不得覆盖主结论）。"""
    if not options.screenshot_dir or page is None:
        return ""
    safe = _UNSAFE_FILENAME_RE.sub("_", tc_no or "case")[:_MAX_FILENAME]
    path = Path(options.screenshot_dir) / f"{safe or 'case'}.png"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(path))
    except Exception:  # noqa: BLE001 - 截图属辅助证据，失败不应把结论也带崩
        return ""
    return str(path)


# ============================================================================
# 会话（跨用例复用同一个浏览器页面）
# ============================================================================
@dataclass
class UiSession:
    """一次 UI 执行会话：**复用同一个 page**，不在每条用例上重启浏览器。"""

    options: UiExecOptions
    console_errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    opened: bool = False
    _pw: Any = None
    _browser: Any = None
    _context: Any = None
    _page: Any = None

    # ---------------------------------------------------------- 生命周期
    def open(self) -> bool:
        """建立浏览器会话；不可用时返回 False，并把**原因**写进 `notes`。"""
        if not runtime_ui.has_playwright():
            self.notes.append(runtime_ui.INSTALL_HINT)
            return False
        if not self.options.base_url:
            self.notes.append("未提供被测地址（--url / RUNTIME_BASE_URL），无法执行 UI 层用例")
            return False
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:  # pragma: no cover - 与 has_playwright 双保险
            self.notes.append(runtime_ui.INSTALL_HINT)
            return False
        try:
            self._pw = sync_playwright().start()
            runtime = self.options.runtime_options()
            self._browser = runtime_ui.launch_browser(self._pw, runtime)
            self._context = self._browser.new_context(ignore_https_errors=True)
            self._context.set_default_timeout(_timeout_ms(self.options))
            carrier = runtime_ui.RuntimeUiResult(base_url=self.options.base_url)
            self._page, logged_in = runtime_ui.open_authenticated_page(
                self._context, runtime, carrier, attach=self._attach
            )
            self.notes.extend(carrier.notes)
            self.notes.append(
                "UI 层执行会话已就绪（已登录）" if logged_in else "UI 层执行会话已就绪（匿名访问）"
            )
        except Exception as exc:  # noqa: BLE001 - 驱动异常面宽，统一转「会话不可用」
            reason = f"浏览器会话建立失败：{type(exc).__name__}（{exc}）"
            self.notes.append(reason[:_MAX_REASON])
            self.close()
            return False
        self.opened = True
        log.info("UI 层执行会话就绪", extra=log_extra(base_url=self.options.base_url))
        return True

    def bind(self, page: Any) -> None:
        """注入一个已就绪的页面（**测试用**；生产由 `open()` 建立）。"""
        self._page = page
        self.opened = True

    def close(self) -> None:
        """尽力关闭上下文与浏览器（不留驻 cookie / storage）。"""
        runtime_ui.close_quietly(self._context, self._browser)
        if self._pw is not None:
            try:
                self._pw.stop()
            except Exception:  # noqa: BLE001 - 关闭失败不影响结论（进程退出即回收）
                pass
        self._context = None
        self._browser = None
        self._pw = None
        self._page = None
        self.opened = False

    def _attach(self, page: Any) -> None:
        """挂控制台监听：只收 error 级（与发现通道同一口径）。"""

        def _on_console(msg: Any) -> None:
            try:
                if str(getattr(msg, "type", "")) == "error":
                    self.console_errors.append(str(getattr(msg, "text", ""))[:200])
            except Exception:  # noqa: BLE001 - 监听回调失败不得影响用例执行
                return

        def _on_pageerror(exc: Any) -> None:
            self.console_errors.append(f"pageerror: {type(exc).__name__}")

        try:
            page.on("console", _on_console)
            page.on("pageerror", _on_pageerror)
        except Exception:  # noqa: BLE001 - 无法挂监听不致命：控制台断言退化为「0 条」
            return

    # ---------------------------------------------------------- 执行
    def execute(self, case: CaseSpec) -> UiExecResult:
        """执行单条 UI 用例：前置判定 → 导航 → 逐条断言 → 非 pass 落截图。"""
        step = case.steps[0] if case.steps else {}
        if self._page is None:
            return UiExecResult(
                status=ExecStatus.SKIPPED.value, notes=[*self.notes, SKIP_NO_SESSION]
            )
        url = target_url(step, self.options)
        if not url:
            return UiExecResult(status=ExecStatus.SKIPPED.value, notes=[SKIP_NO_TARGET])
        if _is_panel_area_url(url):
            return UiExecResult(
                status=ExecStatus.SKIPPED.value, notes=[*self.notes, SKIP_PANEL_AREA]
            )

        mark = len(self.console_errors)
        failure = self._navigate(url)
        if failure:
            return self._conclude(case, ExecStatus.ERROR.value, [], [failure])

        specs = assertions_of(step)
        outcomes = _evaluate_assertions(self._page, specs, self.options, self.console_errors[mark:])
        failed = [o for o in outcomes if not o["ok"]]
        notes = [f"UI 层执行：{url}（断言 {len(outcomes)} 条，未通过 {len(failed)} 条）"]
        notes.extend(f"未通过：{o['detail']}" for o in failed[:5])
        if failed:
            return self._conclude(case, ExecStatus.FAIL.value, outcomes, notes)
        notes.append("全部断言通过")
        return self._conclude(case, ExecStatus.PASS.value, outcomes, notes)

    def _navigate(self, url: str) -> str:
        """导航到目标页；失败返回原因字符串（空串=成功）。"""
        page = self._page
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=_timeout_ms(self.options))
            page.wait_for_timeout(SETTLE_AFTER_GOTO_MS)
        except Exception as exc:  # noqa: BLE001 - 超时/连接被拒/证书错误一律转 error 结论
            return f"打开页面失败：{type(exc).__name__}（{url}）"
        return ""

    def _conclude(
        self,
        case: CaseSpec,
        status: str,
        outcomes: list[dict[str, Any]],
        notes: list[str],
    ) -> UiExecResult:
        """收口：非 pass 时尝试落截图，并把截图路径写进备注。"""
        screenshot_path = ""
        if status != ExecStatus.PASS.value:
            screenshot_path = _screenshot(self._page, self.options, case.tc_no)
            if screenshot_path:
                notes = [*notes, f"失败截图：{screenshot_path}"]
        return UiExecResult(
            status=status,
            notes=notes,
            assertions=outcomes,
            screenshot_path=screenshot_path,
        )
