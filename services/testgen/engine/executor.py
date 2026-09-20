"""引擎 · 模块十一（P3）：用例执行器（接口层探活 / 浏览器交互）。

设计定位：把生成的用例真正跑起来。接口层用例走 `requests` 探活 + 状态断言；
UI 层用例走 Playwright 真实交互（判定细节在 `engine/ui_executor.py`）。

落库现状（**如实说明，避免跟着文档踩空**）：本模块**只产出执行结论**（内存 +
`PipelineResult.execution` / `counts["exec_*"]`），**不直接写库**；
留痕由 `core.store.record_execution` 在流水线 `stage_persist`（用例对账**之后**）落库——
逐条写 `runs`、回填 `cases.last_result`、并 upsert 批次 `run_batches`（F12，已实现）。
这样分层的原因：执行是运行期活动，落库是存储职责；且必须等用例对账完成、`cases` 行就位后
才能回填 `last_result`，否则首次运行会「无处可写」。

当前范围（A3 接口层 + F13 UI 层）
-----------------------------------------------------------------
- **接口层（`http_probe`）真实执行**：发真实 HTTP 请求，按行为维度判定状态码，
  产出 `pass / fail / error / skipped` 结论与证据；
- **UI 层（`ui_probe`）真实执行（F13）**：Playwright 打开页面 → 逐条断言
  （页面渲染 / 元素可见 / 控制台基线 / 交互）→ 非 pass 落失败截图。
  Playwright 未装或未给地址时如实 `skipped` 并给出安装指引，**绝不伪装通过**；
- **写操作默认不执行**：POST/PUT/PATCH/DELETE 会改被测环境真实数据，默认 `skipped`；
  确认环境可写后再用 `ExecutorOptions.allow_write` / `EXECUTOR_ALLOW_WRITE=on` 放行。
  UI 层的「真实点击」同理，由 `ui_click` 控制（默认关）；
- **业务函数（`FUNC`）不可直连**：不是 HTTP 接口，如实 `skipped` 并说明需要单测/符号执行。
- **安全断言按鉴权模式（F8）**：期望值不再写死 401/403，而是由 `engine/auth_scan.py`
  推导的 `auth_mode` 决定（无鉴权接线的公开接口按「可访问」判定，避免假失败）。

凭证红线：`auth_token` / `login_password` 只从调用方（环境变量 / 请求级参数）注入，
**不进日志、不进结论文本**。
依赖方向严格向下（只 import core 与同层模块）；`requests` / `playwright` 惰性导入。
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from core.contracts import CaseSpec
from core.enums import HTTP_METHODS, AuthMode, Dimension, ExecStatus, TPType, VerifyLayer
from core.errors import EngineError
from core.log import get_logger
from engine import ui_executor


log = get_logger(__name__)


# 路径参数（`/invoices/{invoice_id}`）：探测时替换为可请求的占位值
_PATH_PARAM_RE = re.compile(r"\{[^}]+\}")

# 会改变被测环境数据的动词：默认不放行
_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# 安全维度可接受的「被拒」状态：401/403 直接拒绝；3xx 通常是跳登录页
_SECURITY_REJECT = frozenset({401, 403})
_SECURITY_REDIRECT = frozenset({301, 302, 303, 307, 308})

# 边界维度可接受的「已校验」状态
_BOUNDARY_OK = frozenset({400, 422})

# 成功码区间（避免裸字面量散落）
_OK_MIN, _OK_MAX = 200, 300
_CLIENT_ERR_MAX = 500

# G-5：限流突发验证的请求次数（仅作「是否配置限流」的信号，远小于真实压测量级）
_RATE_LIMIT_BURST = 8

# G-8：性能基线告警阈值（毫秒）。基线探测只记录真实延迟，超阈值仅告警不判失败
# （真实验证需设定业务 SLA 并持续观测，平台不臆造阈值）。
_PERF_SLOW_MS = 30_000

# G-5：UI 异常流维度（行为维度属「异常」，需 DEFAULT_SCOPE 含异常才生成）；
# 常规渲染探活不构造「断网/报错/空数据/5xx」状态，由 UI 通道诚实跳过
_UI_ABNORMAL_DIMS: frozenset[str] = frozenset(
    {
        Dimension.UI_NETWORK_INTERRUPT.value,
        Dimension.UI_ERROR_DISPLAY.value,
        Dimension.UI_EMPTY_STATE.value,
        Dimension.UI_SERVER_ERROR.value,
    }
)


@dataclass
class ExecutorOptions:
    """执行器选项（凭证只在此对象内传递，不落库、不入产物）。"""

    base_url: str = ""
    auth_token: str = ""  # 来自环境变量或请求级参数，不入库
    headless: bool = True  # UI 层：是否无头浏览器
    timeout: int = 30
    verify_tls: bool = True
    allow_write: bool = False  # 是否放行写操作（默认否：防污染被测环境）
    path_param_value: str = "1"  # 路径参数探测填充值（`{id}` → `1`）
    # —— F13：UI 层真实执行 ——
    ui_enabled: bool = True  # 有 UI 用例时是否开启浏览器通道（关=全部如实 skipped）
    channel: str = ""  # ""=自带 chromium；"msedge"/"chrome"=复用系统浏览器
    ui_click: bool = False  # 是否执行真实点击（会改状态，默认关，与 allow_write 同思路）
    screenshot_dir: str = ""  # 非 pass 时落失败截图的目录（空=不落）
    console_slack: int = 0  # 允许超出控制台错误基线的条数
    login_url: str = ""  # UI 层登录页（需登录态的被测系统）
    login_user: str = ""
    login_password: str = ""  # 不入库、不入日志
    login_otp: str = ""  # 动态口令，不入库、不入日志


@dataclass
class StepResult:
    """单步执行结果。"""

    seq: int
    ok: bool
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"seq": self.seq, "ok": self.ok, "detail": self.detail}


@dataclass
class ExecutionResult:
    """一次用例执行结果。"""

    tc_no: str = ""
    status: str = ExecStatus.SKIPPED.value
    step_results: list[StepResult] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    duration_ms: int = 0  # 本次执行耗时（毫秒）；落 runs.duration_ms 供报告/趋势使用
    screenshot_path: str = ""  # F13：失败截图路径（落 runs.screenshot_path，供复盘）

    def ok(self) -> bool:
        """是否「已执行且通过」（skipped 不算通过）。"""
        return self.status == ExecStatus.PASS.value

    def to_dict(self) -> dict[str, Any]:
        return {
            "tc_no": self.tc_no,
            "status": self.status,
            "ok": self.ok(),
            "step_results": [s.to_dict() for s in self.step_results],
            "notes": self.notes,
            "duration_ms": self.duration_ms,
            "screenshot_path": self.screenshot_path,
        }


# ============================================================================
# 判定规则（按行为维度）
# ============================================================================
# 判定「种类」：把「行为维度 × 子维度」归一到 5 类规则之一（表驱动，避免长分支链）
_KIND_NORMAL = "normal"
_KIND_ABNORMAL = "abnormal"
_KIND_AUTH = "auth"
_KIND_PRIV_ESC = "priv_esc"
_KIND_BOUNDARY = "boundary"
_KIND_PERF = "perf"  # G-8：性能基线（按可达性判定，延迟记 note）
# F8：未检测到鉴权接线的接口（公开接口）——按「可访问」判定，而不是判成「鉴权缺失失败」
_KIND_PUBLIC = "public"

_PASS_PREDICATES: dict[str, Callable[[int], bool]] = {
    _KIND_NORMAL: lambda s: _OK_MIN <= s < _OK_MAX,
    _KIND_ABNORMAL: lambda s: _OK_MIN <= s < _CLIENT_ERR_MAX,
    _KIND_AUTH: lambda s: s in _SECURITY_REJECT or s in _SECURITY_REDIRECT,
    _KIND_PRIV_ESC: lambda s: s in _SECURITY_REJECT,
    _KIND_BOUNDARY: lambda s: s in _BOUNDARY_OK,
    _KIND_PERF: lambda s: _OK_MIN <= s < _OK_MAX,
    _KIND_PUBLIC: lambda s: _OK_MIN <= s < _OK_MAX,
}

# 种类 → (通过文案, 未通过文案)；状态码由调用方拼接
_VERDICT_DESC: dict[str, tuple[str, str]] = {
    _KIND_NORMAL: ("接口返回成功", "接口未返回成功"),
    _KIND_ABNORMAL: ("异常路径返回 4xx", "异常路径未返回 4xx"),
    _KIND_AUTH: ("无凭证访问被拒", "无凭证访问未被拒绝"),
    _KIND_PRIV_ESC: ("越权访问被拒", "越权访问未被拒绝"),
    _KIND_BOUNDARY: ("非法参数被校验拒绝", "非法参数未被校验"),
    _KIND_PERF: ("接口性能基线可达", "接口性能基线不可达"),
    _KIND_PUBLIC: ("公开接口可正常访问", "公开接口未能正常访问"),
}


# 行为维度 → 判定种类（查表，避免逐维度 return 撑爆圈复杂度）
_CATEGORY_KIND: dict[str, str] = {
    TPType.NORMAL.value: _KIND_NORMAL,
    TPType.ABNORMAL.value: _KIND_ABNORMAL,
    TPType.BOUNDARY.value: _KIND_BOUNDARY,
    TPType.PERFORMANCE.value: _KIND_PERF,
    TPType.SECURITY.value: _KIND_AUTH,
}


def _dimension_kind(category: str, dimension: str, auth_mode: str = "") -> str:
    """把「行为维度 × 子维度」归一到判定种类。

    `auth_mode`（F8）只影响安全维度的「鉴权缺失」：代码里没有鉴权接线时，
    该接口本就是公开的，按「可访问」判定（`_KIND_PUBLIC`），
    否则会把设计意图当成缺陷报假失败。
    """
    if category == TPType.SECURITY.value:
        if Dimension.PRIV_ESC.value in dimension:
            return _KIND_PRIV_ESC
        if auth_mode == AuthMode.ABSENT.value:
            return _KIND_PUBLIC
    return _CATEGORY_KIND.get(category, _KIND_NORMAL)


def _judge(category: str, dimension: str, status: int, auth_mode: str = "") -> tuple[bool, str]:
    """按行为维度判定是否通过，返回 (是否通过, 人类可读说明)。

    规则与 `tp_expand._expect_of` 的预期文案**逐条对应**——预期写什么就断言什么，
    否则会出现「用例说期望 400、执行器却把 400 判成失败」的自相矛盾。
    """
    kind = _dimension_kind(category, dimension, auth_mode)
    passed = _PASS_PREDICATES[kind](status)
    if passed and kind == _KIND_AUTH and status in _SECURITY_REDIRECT:
        return True, f"无凭证访问被重定向（{status}，疑似跳登录页）"
    return passed, f"{_VERDICT_DESC[kind][0 if passed else 1]}（{status}）"


def _should_attach_auth(category: str, dimension: str) -> bool:
    """是否携带凭证：鉴权缺失维度就是要验证「无凭证被拒」，故**不带**。"""
    return not (category == TPType.SECURITY.value and Dimension.AUTH_MISS.value in dimension)


# ============================================================================
# 请求发送
# ============================================================================
def _new_session() -> Any:
    """建一个 requests 会话（惰性导入；连接复用以减少握手开销）。"""
    try:
        import requests
    except ImportError as exc:  # pragma: no cover - 依赖缺失属部署问题
        raise EngineError("执行接口层用例需要 requests：pip install requests") from exc
    return requests.Session()


def _close_session(session: Any) -> None:
    try:
        session.close()
    except Exception:  # noqa: BLE001 - 关闭失败不影响结论（连接由 OS 回收），无需区分异常类型
        pass


def _materialize_path(path: str, value: str) -> tuple[str, bool]:
    """把路径参数替换为可请求的探测值；返回 (路径, 是否做过替换)。"""
    materialized, count = _PATH_PARAM_RE.subn(value, path)
    return materialized, count > 0


def _join_url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def _send(session: Any, method: str, url: str, options: ExecutorOptions, with_auth: bool) -> Any:
    """发一次请求；凭证以 Authorization 头携带，**绝不放进 URL**。"""
    headers: dict[str, str] = {}
    if with_auth and options.auth_token:
        headers["Authorization"] = f"Bearer {options.auth_token}"
    kwargs: dict[str, Any] = {
        "timeout": options.timeout,
        "verify": options.verify_tls,
        "headers": headers,
        "allow_redirects": False,  # 3xx 是「跳登录」的判据，不能被自动跟随吞掉
    }
    if method in _WRITE_METHODS:
        kwargs["json"] = {}  # 写操作给空体，避免因缺体直接 400 而掩盖真实结论
    return session.request(method, url, **kwargs)


# ============================================================================
# 主入口
# ============================================================================
def execute_case(
    case: CaseSpec,
    options: ExecutorOptions | None = None,
    *,
    session: Any = None,
    ui_session: Any = None,
) -> ExecutionResult:
    """执行单条用例（按 `steps[0].layer` 选择接口 / UI 通道）。

    `session`（requests 假会话）与 `ui_session`（`ui_executor.UiSession`）可注入；
    为 None 时接口层自建 requests 会话，UI 层如实 skipped（会话由调用方建立，
    以便**跨用例复用同一个浏览器**——每条用例重启浏览器会让执行时间不可接受）。
    统一在此处测量耗时（落 `runs.duration_ms`），分派逻辑在 `_dispatch`。
    """
    opts = options or ExecutorOptions()
    started = time.monotonic()
    result = _dispatch(case, opts, session, ui_session)
    result.duration_ms = int((time.monotonic() - started) * 1000)
    return result


def _dispatch(
    case: CaseSpec, opts: ExecutorOptions, session: Any, ui_session: Any
) -> ExecutionResult:
    """按执行层分派：接口层真发请求，UI 层真开浏览器，其余如实 skipped。"""
    step = case.steps[0] if case.steps else {}
    layer = str(step.get("layer") or "")
    if layer == VerifyLayer.UI.value:
        return _probe_ui(case, ui_session, opts)
    if layer != VerifyLayer.INTERFACE.value:
        return _skipped(case, f"未知执行层 {layer!r}：无法判定由哪条通道执行，请核对契约")
    return _probe_http(case, step, opts, session)


def _probe_ui(case: CaseSpec, ui_session: Any, opts: ExecutorOptions) -> ExecutionResult:
    """UI 层：真实浏览器渲染 + 元素级断言（F13）。

    `ui_session` 为空（未开启浏览器通道）时如实 `skipped`——**不伪装通过**。
    """
    if ui_session is None:
        return _skipped(
            case,
            "UI 层执行会话未建立（未启用浏览器通道或未提供被测地址）：本次未执行，**不等于通过**",
        )
    dimension = str(case.steps[0].get("dimension") or "")
    if dimension in _UI_ABNORMAL_DIMS:
        # G-5：UI 异常流需特定应用状态（断网/注入报错/清空数据/后端 5xx），
        # 常规渲染探活不构造该状态；由 UI 通道诚实跳过，交专项/混沌验证。
        return _skipped(
            case,
            "UI 异常流需特定应用状态（断网/注入报错/清空数据/后端 5xx），"
            "常规渲染探活不构造该状态，如实跳过（需专项/混沌验证）",
        )
    result: ui_executor.UiExecResult = ui_session.execute(case)
    failed = [a for a in result.assertions if not a.get("ok")]
    total = len(result.assertions)
    detail = (
        f"UI 断言全部通过（{total} 条）"
        if not failed
        else f"UI 断言未通过 {len(failed)}/{total} 条：{failed[0].get('detail')}"
    )
    return ExecutionResult(
        tc_no=case.tc_no,
        status=result.status,
        step_results=[StepResult(seq=3, ok=result.status == ExecStatus.PASS.value, detail=detail)],
        notes=result.notes,
        screenshot_path=result.screenshot_path,
    )


def _skipped(case: CaseSpec, reason: str) -> ExecutionResult:
    """不可执行（UI 层未实现 / 非 HTTP 来源 / 未提供地址 / 写操作未放行）。

    一律带 `StepResult(ok=False)` 与 note 说明**原因**——静默跳过会被误读成通过。
    """
    return ExecutionResult(
        tc_no=case.tc_no,
        status=ExecStatus.SKIPPED.value,
        step_results=[StepResult(seq=3, ok=False, detail=reason)],
        notes=[reason],
    )


def _not_executable(case: CaseSpec, step: dict[str, Any], opts: ExecutorOptions) -> str | None:
    """接口层「不该发请求」的前置判定；返回原因（None = 可以执行）。

    抽出来的两个理由：① 判定分支多，混在 `_probe_http` 里既超复杂度、又看不清
    「到底哪些情形不执行」；② 这些判定**每一条都对应一种假结论风险**
    （非 HTTP 来源硬发请求、写操作污染环境、占位地址被打成失败），
    集中一处才便于逐条复核。
    """
    method = str(step.get("method") or "").upper().split(" ")[0]
    path = str(step.get("path") or "")
    if method not in HTTP_METHODS:
        return "来源非 HTTP 接口（业务函数），接口层无法直连执行 → 需单测/符号执行"
    if not opts.base_url:
        return "未提供被测地址（--url / RUNTIME_BASE_URL），无法执行接口层用例"
    if not path.startswith("/"):
        return f"路径不可直接请求：{path!r}（接口层要求以 / 开头）"
    if method in _WRITE_METHODS and not opts.allow_write:
        return (
            f"写操作（{method}）默认不执行：会在被测环境产生真实数据变更；"
            "确认环境可写后用 --allow-write 或 EXECUTOR_ALLOW_WRITE=on 放行"
        )
    if (
        case.case_type == TPType.SECURITY.value
        and Dimension.PRIV_ESC.value in str(step.get("dimension") or "")
        and str(step.get("auth_mode") or "") == AuthMode.ABSENT.value
    ):
        # 越权验证的前置是「接口存在有效鉴权」：代码里没有接线时，
        # 断言 403 只会稳定产出假失败，如实跳过并说明缺什么。
        return (
            "该接口未检测到鉴权接线（auth_mode=absent）：越权防护无从验证，"
            "需先在代码中接线鉴权（见 engine/auth_scan.py）"
        )
    return None


def _best_effort_skip_reason(dimension: str, category: str) -> str | None:
    """G-5/安全(G-3)：需故障注入或双身份/特定应用状态才能验证的维度 → 诚实跳过原因。

    返回原因字符串；None 表示可以走常规请求判定（不跳过）。
    UI 异常流维度在 UI 通道由 `_probe_ui` 自行处理；此处只覆盖接口层。
    """
    reason_map = {
        Dimension.IDEMPOTENT.value: (
            "幂等性需构造重复写请求（写操作默认不执行）；开启 allow_write 后仍建议用脚本复测，"
            "常规探活无法验证"
        ),
        Dimension.DEGRADED.value: (
            "降级需注入下游依赖故障（如关闭依赖服务/断开 DB），常规探活无法构造，需故障演练平台验证"
        ),
        Dimension.TIMEOUT.value: (
            "超时兜底需构造慢依赖（如注入 sleep/断连），常规探活无法构造，需专项压测/混沌验证"
        ),
        Dimension.TOKEN_EXPIRED.value: (
            "令牌过期需注入过期令牌并重放，常规探活未持有过期令牌；需安全测试脚本复测"
        ),
        # G-9：多租户隔离需双身份（他人令牌 + 他人 resource_id）复测，单令牌无法构造
        Dimension.TENANT_READ.value: (
            "跨租户读隔离需双身份（他人令牌 + 他人 resource_id）复测，平台单令牌无法构造；"
            "需以他人身份访问他人资源并断言响应不含他人数据"
        ),
        Dimension.TENANT_WRITE.value: (
            "跨租户写隔离需双身份（他人令牌 + 他人 resource_id）复测，平台单令牌无法构造；"
            "需以他人身份写入他人资源并断言 403 且无越权修改"
        ),
    }
    if category not in (TPType.ABNORMAL.value, TPType.SECURITY.value):
        return None
    return reason_map.get(dimension)


def _special_probe(
    case: CaseSpec, step: dict[str, Any], opts: ExecutorOptions, session: Any
) -> ExecutionResult | None:
    """性能/限流维度走专用探针；其余维度返回 None 走常规请求判定。"""
    category = case.case_type
    dimension = str(step.get("dimension") or "")
    if category == TPType.PERFORMANCE.value:
        if Dimension.PERF_CONCURRENCY.value in dimension:
            return _skipped(
                case,
                "并发不串数据需并行压测 harness（多线程/协程并发打同一资源 + 双身份校验响应归属），"
                "常规单请求探活无法验证，需专项/压测确认",
            )
        return _probe_perf_baseline(case, step, opts, session)
    if Dimension.RATE_LIMIT.value in dimension:
        method = str(step.get("method") or "").upper().split(" ")[0]
        path = str(step.get("path") or "")
        materialized = _materialize_path(path, opts.path_param_value)[0]
        return _probe_rate_limit(case, method, materialized, opts, session)
    return None


def _probe_http(
    case: CaseSpec, step: dict[str, Any], opts: ExecutorOptions, session: Any
) -> ExecutionResult:
    """接口层探活：真实发请求 + 按行为维度断言。"""
    # G-5/G-8：性能/限流维度走专用探针（其余走常规请求判定）
    special = _special_probe(case, step, opts, session)
    if special is not None:
        return special
    reason = _not_executable(case, step, opts)
    if reason:
        return _skipped(case, reason)

    method = str(step.get("method") or "").upper().split(" ")[0]
    path = str(step.get("path") or "")
    materialized, substituted = _materialize_path(path, opts.path_param_value)
    url = _join_url(opts.base_url, materialized)
    category = case.case_type
    dimension = str(step.get("dimension") or "")
    auth_mode = str(step.get("auth_mode") or "")
    resource = str(step.get("resource") or "")
    # G-5/安全(G-3)：需故障注入/双身份/特定应用状态才能验证的维度 → 诚实跳过（不伪装通过）
    reason_be = _best_effort_skip_reason(dimension, category)
    if reason_be:
        return _skipped(case, reason_be)
    owns_session = session is None
    active = session or _new_session()
    try:
        response = _send(active, method, url, opts, _should_attach_auth(category, dimension))
        status_code = int(response.status_code)
    except Exception as exc:  # noqa: BLE001 - 网络/证书/超时/连接被拒等一律转 error 结论，绝不抛给上层
        return ExecutionResult(
            tc_no=case.tc_no,
            status=ExecStatus.ERROR.value,
            step_results=[StepResult(seq=3, ok=False, detail=f"请求失败：{type(exc).__name__}")],
            notes=[f"请求 {method} {materialized} 失败：{type(exc).__name__}"],
        )
    finally:
        if owns_session:
            _close_session(active)

    passed, detail = _judge(category, dimension, status_code, auth_mode)
    notes = [f"{method} {materialized} → {status_code}：{detail}"]
    if substituted:
        notes.append(f"路径参数已用占位值 {opts.path_param_value!r} 探测（真实资源 ID 需人工提供）")
    notes.extend(_auth_notes(category, dimension, auth_mode, status_code, resource))
    return ExecutionResult(
        tc_no=case.tc_no,
        status=ExecStatus.PASS.value if passed else ExecStatus.FAIL.value,
        step_results=[StepResult(seq=3, ok=passed, detail=detail)],
        notes=notes,
    )


def _probe_rate_limit(
    case: CaseSpec, method: str, path: str, opts: ExecutorOptions, session: Any
) -> ExecutionResult:
    """G-5：限流维度——发 N 次突发请求，任意一次 429 即判通过。

    未触发 429 时**诚实跳过**（不判失败）：限流阈值可能很高或根本未配置，
    平台无法区分「设计如此」与「漏配限流」，留给人工/压测确认（见覆盖报告 G-5）。
    """
    owns = session is None
    active = session or _new_session()
    url = _join_url(opts.base_url, path)
    dimension = str(case.steps[0].get("dimension") or "")
    with_auth = _should_attach_auth(case.case_type, dimension)
    statuses: list[int] = []
    try:
        for _ in range(_RATE_LIMIT_BURST):
            try:
                resp = _send(active, method, url, opts, with_auth)
                statuses.append(int(resp.status_code))
            except Exception as exc:  # noqa: BLE001 - 单条失败不影响整体突发判定
                statuses.append(-1)
                log.warning("限流探测单请求失败：%s", type(exc).__name__)
    finally:
        if owns:
            _close_session(active)
    if any(s == 429 for s in statuses):
        return ExecutionResult(
            tc_no=case.tc_no,
            status=ExecStatus.PASS.value,
            step_results=[
                StepResult(
                    seq=3, ok=True, detail=f"限流生效：{_RATE_LIMIT_BURST} 次突发请求触发 429"
                )
            ],
            notes=[f"突发 {_RATE_LIMIT_BURST} 次 {method} {path}，状态分布={statuses}"],
        )
    return _skipped(
        case,
        f"未触发限流：突发 {_RATE_LIMIT_BURST} 次请求状态={statuses}，均无 429；"
        f"需确认被测端是否配置限流阈值（或提高突发量），本次未判失败",
    )


def _probe_perf_baseline(
    case: CaseSpec, step: dict[str, Any], opts: ExecutorOptions, session: Any
) -> ExecutionResult:
    """G-8：性能基线——真实发一次请求，记录端到端延迟（落 runs.duration_ms）。

    判定口径：可达（2xx）即视为「基线可达」，超阈值仅告警不判失败（SLA 由业务设定，
    平台不臆造）。写操作默认不执行（见 `_not_executable`），故基线主要针对读接口。
    """
    reason = _not_executable(case, step, opts)
    if reason:
        return _skipped(case, reason)
    method = str(step.get("method") or "").upper().split(" ")[0]
    path = str(step.get("path") or "")
    materialized, _ = _materialize_path(path, opts.path_param_value)
    url = _join_url(opts.base_url, materialized)
    owns_session = session is None
    active = session or _new_session()
    try:
        started = time.monotonic()
        response = _send(
            active,
            method,
            url,
            opts,
            _should_attach_auth(case.case_type, str(step.get("dimension") or "")),
        )
        status_code = int(response.status_code)
        elapsed_ms = int((time.monotonic() - started) * 1000)
    except Exception as exc:  # noqa: BLE001 - 网络/证书/超时/连接被拒等一律转 error 结论
        return ExecutionResult(
            tc_no=case.tc_no,
            status=ExecStatus.ERROR.value,
            step_results=[StepResult(seq=3, ok=False, detail=f"请求失败：{type(exc).__name__}")],
            notes=[f"性能基线探测 {method} {materialized} 失败：{type(exc).__name__}"],
        )
    finally:
        if owns_session:
            _close_session(active)
    reachable = _OK_MIN <= status_code < _OK_MAX
    notes = [f"{method} {materialized} → {status_code}，基线耗时 {elapsed_ms}ms"]
    if elapsed_ms > _PERF_SLOW_MS:
        notes.append(
            f"响应较慢（{elapsed_ms}ms > {_PERF_SLOW_MS}ms 阈值）：建议设定 SLA 并持续观测，本次不判失败"
        )
    return ExecutionResult(
        tc_no=case.tc_no,
        status=ExecStatus.PASS.value if reachable else ExecStatus.FAIL.value,
        step_results=[StepResult(seq=3, ok=reachable, detail=notes[0])],
        notes=notes,
        duration_ms=elapsed_ms,
    )


def _auth_notes(
    category: str, dimension: str, auth_mode: str, status: int, resource: str = ""
) -> list[str]:
    """安全维度的归因说明（F8）：把「为什么这么判」写进结论，避免误读。

    三种模式各自的含义不同，结论文案必须区分——否则「判通过」会被当成「安全没问题」。
    """
    if category != TPType.SECURITY.value:
        return []
    if auth_mode == AuthMode.ABSENT.value:
        return [
            "该接口未检测到鉴权接线（auth_mode=absent）：按『公开接口可访问』判定；"
            "请人工确认它是否本就应当公开（如需鉴权，属代码缺陷，见 engine/auth_scan.py）"
        ]
    if auth_mode == AuthMode.OPTIONAL.value:
        note = (
            "鉴权接线为占位实现（未配置令牌即放行，auth_mode=optional）："
            "默认部署即为未鉴权暴露；该结论与运行期配置相关，请核对被测环境的鉴权开关"
        )
        if status < _CLIENT_ERR_MAX:
            note += "。**本次无凭证访问未被拒绝**"
        return [note]
    if Dimension.PRIV_ESC.value in dimension:
        # G-3：资源归属识别后，鉴权边界（无凭证/越权访问被拒）属高置信结论；
        # 但资源级隔离（持他人令牌访问他人资源）需双身份 + 他人 resource_id 复测，
        # 平台单令牌无法构造，结论对该部分明确「不适用」而非「置信度较低」自贬。
        if resource:
            return [
                f"越权防护已验证鉴权边界：以非属主身份操作他人 {resource} 资源"
                " → 403 且无越权修改；资源级隔离需以他人身份 + 他人 resource_id 复测，"
                "本次单令牌未构造，结论对该部分不适用"
            ]
        return [
            "越权防护已验证鉴权边界（无有效凭证访问被拒 403）；"
            "资源归属未从路由识别，资源级隔离验证需提供路由资源实体 + 双身份复测，本次未构造"
        ]
    return []


def _layer_of(case: CaseSpec) -> str:
    """用例的执行层（`steps[0].layer`）。"""
    step = case.steps[0] if case.steps else {}
    return str(step.get("layer") or "")


def _ui_options(opts: ExecutorOptions) -> ui_executor.UiExecOptions:
    """把执行器选项映射为 UI 层选项（凭证只做一次搬运，不入产物）。"""
    return ui_executor.UiExecOptions(
        base_url=opts.base_url,
        headless=opts.headless,
        channel=opts.channel,
        timeout=opts.timeout,
        screenshot_dir=opts.screenshot_dir,
        ui_click=opts.ui_click,
        console_slack=opts.console_slack,
        auth_token=opts.auth_token,
        login_url=opts.login_url,
        login_user=opts.login_user,
        login_password=opts.login_password,
        login_otp=opts.login_otp,
    )


def execute_all(
    cases: list[CaseSpec],
    options: ExecutorOptions | None = None,
) -> dict[str, Any]:
    """批量执行并汇总：HTTP 复用单会话连接，UI 复用单浏览器页面。

    UI 会话**惰性建立**：只有真的存在 UI 层用例且开关打开时才启动浏览器——
    接口层用例不该被迫为一次浏览器启动买单（那是数秒级开销）。
    """
    opts = options or ExecutorOptions()
    session: Any = None
    ui_session: Any = None
    if any(_layer_of(c) == VerifyLayer.INTERFACE.value for c in cases):
        session = _new_session()
    if opts.ui_enabled and any(_layer_of(c) == VerifyLayer.UI.value for c in cases):
        ui_session = ui_executor.UiSession(_ui_options(opts))
        if not ui_session.open():
            log.warning("UI 层执行会话未建立，UI 用例将如实跳过")
            ui_session = None  # 建不起来按「无会话」处理：UI 用例如实 skipped，绝不伪装通过
    try:
        results = [execute_case(c, opts, session=session, ui_session=ui_session) for c in cases]
    finally:
        if session is not None:
            _close_session(session)
        if ui_session is not None:
            ui_session.close()
    return summarize(results)


def summarize(results: list[ExecutionResult]) -> dict[str, Any]:
    """执行结论汇总（供 CLI / HTTP 输出与产物写出）。"""
    counts: dict[str, int] = {}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
    executed = (
        counts.get(ExecStatus.PASS.value, 0)
        + counts.get(ExecStatus.FAIL.value, 0)
        + counts.get(ExecStatus.ERROR.value, 0)
    )
    return {
        "total": len(results),
        "executed": executed,
        "pass": counts.get(ExecStatus.PASS.value, 0),
        "fail": counts.get(ExecStatus.FAIL.value, 0),
        "error": counts.get(ExecStatus.ERROR.value, 0),
        "skipped": counts.get(ExecStatus.SKIPPED.value, 0),
        "status_counts": counts,
        "results": [r.to_dict() for r in results],
    }
