"""服务壳（HTTP 薄壳）：只做「解析请求 → 调引擎 → 格式化响应」，不含业务逻辑。

契约：
- 版本化前缀 `/api/v1`，便于后续演进不破坏调用方；
- 统一错误结构 `{code, message, detail?}`，**不返回堆栈**；
- 每个请求分配 `request_id`，贯穿结构化日志；
- `/health`（存活）与 `/ready`（就绪）分离，便于发布流水线探活。

已知边界（有意）：当前端点**同步执行**。异步任务化 + webhook 回调属于服务化改造范围，
不在本次「代码 → 用例生成」范围（见 P0-P3 确认书 ⛔ 部分）。
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import asynccontextmanager
from functools import lru_cache, partial
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, File, Form, Header, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from core import store
from core.auto_input import parse_auto_input
from core.config import get_settings
from core.contracts import CONTRACT_VERSION
from core.db import connect, init_db
from core.enums import ALL_TP_TYPES, MODE_FULL, REPORT_FORMATS, PullStatus, ReportFormat
from core.errors import (
    AppError,
    ContractViolation,
    NotFoundError,
    UnauthorizedError,
    ValidationError,
)
from core.log import get_logger, log_extra, set_request_id
from engine import analytics, pipeline
from engine import report as report_engine
from engine.codeup_client import CodeupError, parse_repo_to_repo_id
from engine.comparator import (
    ComparatorOptions,
    CompareInput,
    compare_batch,
    compare_one,
)
from engine.dialogue import DialogueAgent, default_dialogue_template
from engine.llm_fallback import chat_with_fallback
from engine.remote_scan import (
    CHANGE_ADDED,
    CHANGE_MODIFIED,
    CHANGE_UNCHANGED,
    client_from_code_source,
    scan_git_repo,
    scan_local,
    scan_repository,
)
from engine.verdict import run_five_dimension_verdict
from output.report_writer import ReportExportError, export_report, render_html, render_markdown
from output.writer import OutputWriter
from service import llm_providers as service_llm
from service import repos as service_repos
from service.tasks import (
    BackgroundExecutor,
    GenerationTask,
    TaskCancelledError,
    TaskState,
    TaskStore,
    stage_fraction,
)
from workspace.manager import WorkspaceManager


log = get_logger(__name__)
settings = get_settings()

# M18：/describe 分批与并发参数（单组 5 条，组间最多 4 路并发）
_DESCRIBE_CHUNK_SIZE = 5
_DESCRIBE_MAX_WORKERS = 4
# 描述缓存写入锁：LLM 调用并发，DB 写串行（SQLite 多读单写）
_DESCRIBE_DB_LOCK = threading.Lock()

# P5 服务化：进程内后台执行器（生成任务异步化）。生产升级路径为 broker + 多 consumer。
task_store = TaskStore()
executor = BackgroundExecutor(task_store)

# 专家审阅阶段（逐页大模型调用）的进度区间：把 0.84~0.89 按「第 i/N 页」细分，
# 否则该阶段一跑十几分钟而进度条纹丝不动，前端看起来就是「卡死」。
_EXPERT_STAGE_BASE = 0.84
_EXPERT_STAGE_SPAN = 0.05


def _stage_progress(stage: str, info: dict[str, Any], best: float) -> float:
    """阶段名 + 阶段内细粒度信息 → 单调不减的进度（0~1）。"""
    frac = stage_fraction(stage, best)
    if stage == "expert_review":
        page = int(info.get("page") or 0)
        total = int(info.get("total") or 0)
        if page > 0 and total > 0:
            frac = max(frac, _EXPERT_STAGE_BASE + _EXPERT_STAGE_SPAN * min(1.0, page / total))
    return max(best, min(1.0, frac))


def _stage_with_detail(stage: str, info: dict[str, Any]) -> str:
    """把阶段内细粒度信息编码进 stage（表里没有 stage_info 列，避免为它做迁移）。

    形如 `expert_review:2/3`。前端拆出页码显示「第 2/3 页」，用户能看出还在推进，
    而不是盯着一动不动的阶段名等十几分钟。
    """
    page = int(info.get("page") or 0)
    total = int(info.get("total") or 0)
    if page > 0 and total > 0:
        return f"{stage}:{page}/{total}"
    return stage


@asynccontextmanager
async def lifespan(_: FastAPI) -> Any:
    """启动建表、关闭时优雅停机 worker 池。"""
    init_db()
    store.seed_repo_configs()
    yield
    executor.shutdown(wait=True)


app = FastAPI(
    title="testgen-service",
    version="0.1.0",
    description=(
        "测试用例生成服务：代码解析 → 测试点 → 用例 → 执行验证（契约 v1.0）。"
        "服务化：生成任务异步化（POST /api/v1/generate → 轮询 GET /api/v1/tasks/{id}），"
        "执行验证异步化（POST /api/v1/execute，对已有用例或生成+执行皆可），"
        "发布流水线经 POST /api/v1/verify/webhook 触发（可带 execute=true 一并验证）。"
    ),
    lifespan=lifespan,
)

# 本地控制台同源便利：允许跨域（仅本机/局域网演示用，生产应收窄来源）。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================================
# 中间件 / 错误处理
# ============================================================================
@app.middleware("http")
async def request_context(request: Request, call_next: Any) -> Any:
    rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
    set_request_id(rid)
    response = await call_next(request)
    response.headers["X-Request-ID"] = rid
    return response


@app.exception_handler(AppError)
async def app_error_handler(_: Request, exc: AppError) -> JSONResponse:
    log.warning("业务异常", extra=log_extra(code=exc.code, message=exc.message))
    return JSONResponse(status_code=exc.http_status, content=exc.to_payload())


@app.exception_handler(CodeupError)
async def codeup_error_handler(_: Request, exc: CodeupError) -> JSONResponse:
    """Codeup 远端失败映射：401/403 → 401（凭证问题），其余 → 502（远端不可用）。

    只透出状态码与远端摘要，不泄露内部细节。
    """
    status = exc.status or 0
    http = 401 if status in (401, 403) else 502
    log.warning("Codeup 远端失败", extra=log_extra(code=f"codeup_{status}", message=exc.message))
    return JSONResponse(
        status_code=http,
        content={"code": "codeup_error", "message": f"Codeup API [{status}] {exc.message}"},
    )


@app.exception_handler(Exception)
async def unhandled_handler(_: Request, exc: Exception) -> JSONResponse:
    """兜底：只暴露类型名，绝不返回堆栈或内部细节。"""
    log.error("未处理异常", extra=log_extra(err=type(exc).__name__), exc_info=exc)
    return JSONResponse(
        status_code=500,
        content={"code": "internal_error", "message": "服务内部错误"},
    )


# ============================================================================
# 鉴权（占位实现：配置了 AUTH_TOKEN 才校验）
# ============================================================================
def require_auth(
    x_auth_token: Annotated[str | None, Header(alias="X-Auth-Token")] = None,
) -> None:
    # H8 修复：默认 fail-closed。
    # - 配置了 AUTH_TOKEN：必须携带且匹配，否则拒绝；
    # - 未配置 AUTH_TOKEN：仅当显式 AUTH_DISABLED=1（开发/内网逃生口）才放行，
    #   否则一律拒绝，避免「忘记配令牌」导致服务裸奔。
    if settings.auth_token:
        if x_auth_token != settings.auth_token:
            raise UnauthorizedError("缺少或无效的 X-Auth-Token")
        return
    if not settings.auth_disabled:
        raise UnauthorizedError(
            "服务未配置 AUTH_TOKEN 且未显式设置 AUTH_DISABLED=1，已拒绝请求（fail-closed）"
        )


AuthDep = Annotated[None, Depends(require_auth)]


def _is_managed(local_path: str) -> bool:
    """目标目录是否位于工作区根内（只有受管目录才做只读加固）。"""
    try:
        WorkspaceManager().assert_inside(local_path)
    except AppError:
        return False
    return True


# ============================================================================
# 请求模型
# ============================================================================
class RepoSpecRequest(BaseModel):
    """多仓改造（v1.7）：项目下单个仓库的取码/对比输入。"""

    repo_key: str = Field(
        "", description="项目内稳定短名（backend/frontend/shared）；必填且项目内唯一"
    )
    local_path: str = Field("", description="本仓代码目录")
    repo_url: str = Field("", description="本仓仓库地址")
    base: str | None = Field(None, description="本仓增量基线 ref")
    target: str | None = Field(None, description="本仓增量目标 ref")
    ref: str = Field("", description="本仓默认分支/提交")
    changed_files: list[str] = Field(default_factory=list, description="本仓显式变更文件清单")
    selected_paths: list[str] = Field(default_factory=list, description="本仓勾选范围")


class PipelineRequest(BaseModel):
    local_path: str = Field("", description="被测代码所在目录（可选；与 test_url 至少给一个）")
    repos: list[RepoSpecRequest] = Field(
        default_factory=list,
        description=(
            "多仓改造：一个项目下关联 N 个仓库。非空时优先（每仓各自取码/扫描/生成，"
            "共享同一 project、按 repo_key 隔离产物）；空则回落旧单仓字段。"
        ),
    )
    repo_url: str = Field(
        "", description="仓库地址（F1）：先取码到 local_path（缺省落到工作区根/仓库名）"
    )
    code_source: dict[str, Any] = Field(
        default_factory=dict, description="取码凭证（PAT）；空则由后端按 repo_url 从仓库配置带出"
    )
    pull_deepen: int = Field(0, ge=0, description="浅克隆加深的提交数（0=不加深）")
    changed_files: list[str] = Field(
        default_factory=list,
        description="显式变更文件清单（F2，正斜杠相对路径）；优先于本地 git diff",
    )
    selected_paths: list[str] = Field(
        default_factory=list,
        description="按子集生成（控制台「扫描→勾选范围」）：仅对勾选的文件/目录产出测试点；空=全量",
    )
    project_name: str = Field("", description="项目名（缺省用目录名或被测主机名）")
    mode: str = Field("", description="full=全量扫描 / incremental=增量扫描（留空=默认 full）")
    base: str | None = Field(None, description="增量模式基线 ref")
    target: str | None = Field(None, description="增量模式目标 ref")
    scopes: list[str] = Field(
        default_factory=list,
        description="行为维度范围：正常/异常/安全/边界（留空=默认 正常+安全+边界）",
    )
    prd_source: str = Field("", description="PRD / OpenAPI 文件路径（启用 PRD 通道时使用）")
    include_business: bool = Field(True, description="是否提取业务函数功能点")
    extract_pages: bool = Field(True, description="是否提取前端路由")
    persist: bool = Field(True, description="是否落库")
    llm_enhance: bool = Field(False, description="是否启用 LLM 增强通道")
    expert_ui: bool = Field(
        False,
        description=(
            "是否启用「逐页大模型专家审阅」（PageExpert）。每页一次调用（30~60s），"
            "默认关闭：不勾就不烧 token、也不会把任务拖成十分钟以上。"
        ),
    )
    readonly_lock: bool = Field(False, description="分析完成后是否把目录置只读")
    # —— 地址通道（A1）：凭证字段只用于本次运行，**绝不回显、不落库、不入产物** ——
    test_url: str = Field("", description="被测环境地址（走运行时 UI 发现）")
    login_url: str = Field("", description="登录页地址（缺省自动判定）")
    login_user: str = Field("", description="登录账号（更推荐用环境变量注入）")
    login_password: str = Field("", description="登录密码（更推荐用环境变量注入）")
    login_otp: str = Field("", description="动态口令（更推荐用环境变量注入）")
    runtime_routes: list[str] = Field(default_factory=list, description="显式路由（优先级最高）")
    runtime_ui: bool = Field(False, description="启用运行时 UI 发现（等价 RUNTIME_UI_ENABLED=on）")
    runtime_scope: str = Field(
        "targeted",
        description=(
            "地址通道范围：targeted=只测与本次变更相关的页面/接口（默认，增量模式下生效）；"
            "full=全站巡检（39 页/28 接口那种，产出与变更无关）"
        ),
    )
    auto_input: str = Field("", description="统一智能输入框：一段混排文本（地址+账号+密码+口令）")
    execute: bool = Field(False, description="执行生成的用例（接口层 + UI 层）")
    allow_write: bool = Field(False, description="放行写操作；默认只跑只读请求")
    ui_click: bool = Field(False, description="UI 层执行真实点击（F13）；默认只做只读断言")
    exec_url: str = Field("", description="执行器被测服务地址（只跑接口层时可单独指定）")
    async_mode: bool = Field(
        False,
        description=(
            "M18：true 时本端点不再同步等待，立即返回 202 + task_id/poll_url"
            "（任务模型与 /generate 一致）；默认 false 保持同步语义。"
        ),
    )


class ParseInputRequest(BaseModel):
    """统一智能输入框解析请求（只解析、不跑流水线）。"""

    text: str = Field("", description="混排文本：地址 / 账号 / 密码 / 动态口令 / 代码路径")


class CompareRequest(BaseModel):
    """语义比对请求（P0-2）：输入「预期(case) + 实际(actual)」输出比对结论。

    不落库、不写文件、不触执行动作；复用 comparator 模块（规则为主·LLM 增强）。
    本端点入参**不含任何凭证字段**——纯粹的预期/实际文本，安全红线天然满足。
    """

    case: dict = Field(
        default_factory=dict,
        description="预期侧：CaseSpec 子集（id/title/steps[].expect/dimension/severity）",
    )
    actual: dict = Field(
        default_factory=dict,
        description="实际侧：ExecutionResult.to_dict()（status/notes/error/step_results）",
    )
    context: dict = Field(
        default_factory=dict, description="可选覆盖（model/language/project_id 等）"
    )
    items: list[dict] = Field(
        default_factory=list,
        description="批量模式：省略顶层 case/actual 时，按 [{case,actual,context}] 逐条比对",
    )


class DialogueSubmitRequest(BaseModel):
    """对话提交请求（P1-2）：模板填写值 或 自由文本 二选一。

    - ``filled``：模板渲染后用户填写的字典（字段名见 GET /api/v1/dialogue/template）；
    - ``text``：统一智能输入框式的混排自由文本（兜底）；
    - 两者都给时 ``filled`` 优先，``text`` 作为安全网原文回灌。
    """

    filled: dict[str, Any] = Field(default_factory=dict, description="模板填写值")
    text: str = Field("", description="自由文本兜底（与 filled 二选一）")


class ExecuteRequest(BaseModel):
    """执行验证请求（G-2 服务化执行侧）。

    两条路径（互斥，取其一）：
    - 给 `project_id`：对**已有用例**执行验证（不重新生成，等价于 CLI `testgen execute`）；
    - 给代码/地址来源：先生成用例再执行验证（等价于 CLI `pipeline --execute`）。
    凭证字段只用于本次运行，**绝不回显、不落库、不入产物**。
    """

    project_id: int = Field(0, description="指定已有项目，仅执行其已生成用例（不重新生成）")
    # —— 生成+执行路径（与 PipelineRequest 同义子集）——
    local_path: str = Field("", description="被测代码目录（与 repo_url/test_url 至少给一个）")
    repo_url: str = Field("", description="仓库地址（F1）：先取码再生成+执行")
    project_name: str = Field("", description="项目名（缺省用目录名或被测主机名）")
    mode: str = Field("", description="full=全量扫描 / incremental=增量扫描（留空=默认 full）")
    scopes: list[str] = Field(
        default_factory=list,
        description="行为维度范围：正常/异常/安全/边界（留空=默认 正常+安全+边界）",
    )
    test_url: str = Field("", description="被测环境地址（运行时 UI 发现 + 接口执行 base_url）")
    login_url: str = Field("", description="登录页地址（缺省自动判定）")
    login_user: str = Field("", description="登录账号（更推荐用环境变量注入）")
    login_password: str = Field("", description="登录密码（更推荐用环境变量注入）")
    login_otp: str = Field("", description="动态口令（更推荐用环境变量注入）")
    runtime_ui: bool = Field(False, description="启用运行时 UI 发现（生成阶段）")
    runtime_routes: list[str] = Field(default_factory=list, description="显式路由（优先级最高）")
    auto_input: str = Field("", description="统一智能输入框：一段混排文本（地址+账号+密码+口令）")
    # —— 执行控制（两条路径共用）——
    exec_url: str = Field("", description="执行器被测服务地址（只跑接口层时单独指定）")
    allow_write: bool = Field(False, description="放行写操作；默认只跑只读请求")
    ui_click: bool = Field(False, description="UI 层执行真实点击（F13）；默认只做只读断言")
    # —— 生成+执行路径的子集/增量透传（与 PipelineRequest 同名字段贯通，一键闭环用）——
    base: str = Field("", description="基线 ref（增量模式计算 diff）")
    target: str = Field("", description="目标 ref（增量模式）")
    selected_paths: list[str] = Field(
        default_factory=list, description="子集生成：仅对勾选的文件/目录生成测试点（空=全量）"
    )


def _run_execution_job(
    task_id: str, project_id: int, opts: pipeline.PipelineOptions
) -> dict[str, Any]:
    """后台作业：对已有项目执行验证（不重新生成），进度写回任务表。"""
    best = {"frac": 0.0}

    def _progress(stage: str, info: dict[str, Any]) -> None:
        if task_store.is_cancelled(task_id):
            raise TaskCancelledError(task_id)
        frac = _stage_progress(stage, info, best["frac"])
        best["frac"] = frac
        task_store.update(task_id, progress=round(frac, 3), stage=_stage_with_detail(stage, info))

    return pipeline.run_execution(project_id, opts, progress=_progress)


def _build_exec_opts_from_request(req: ExecuteRequest) -> pipeline.PipelineOptions:
    """把 `ExecuteRequest` 的项目级执行字段映射到流水线选项（仅执行，不重新生成）。"""
    opts = pipeline.default_options()
    t = opts.target_req
    t.exec_url = req.exec_url
    t.allow_write = req.allow_write
    t.ui_click = req.ui_click
    if req.test_url:
        t.base_url = req.test_url
        t.enabled = True
    if req.login_url:
        t.login_url = req.login_url
    if req.login_user:
        t.login_user = req.login_user
    if req.login_password:
        t.login_password = req.login_password
    if req.login_otp:
        t.login_otp = req.login_otp
    if req.runtime_ui:
        t.enabled = True
    return opts


class VerdictRequest(BaseModel):
    """安全判定请求（M2）：testhub 执行 security 用例后回传的「观测事实」。

    入参**不含任何凭证明文**——只传 auth_mode / observed 结果（脱敏责任在 testhub 侧 facts 构造）。
    category/dimension 接受 testgen 原生中文（安全/安全-越权）或英文别名（security/priv_esc）。
    """

    target_url: str = Field("", description="被测目标地址（仅作证据记录，不参与判定）")
    endpoint: str = Field("", description="被测接口路径（仅作证据记录）")
    method: str = Field("GET", description="执行方法")
    category: str = Field(
        "", description="行为维度大类：正常/异常/安全/边界/性能（亦接受 normal/security...）"
    )
    dimension: str = Field("", description="子维度（如 安全-越权 / priv_esc / 安全-鉴权缺失）")
    auth_mode: str | None = Field(None, description="期望鉴权接线（required/optional/absent）")
    observed_status: int | None = Field(None, description="执行后实际 HTTP 状态码（无则不可判定）")
    observed_body_has_sensitive: bool = Field(False, description="响应体是否疑似泄露敏感信息")
    expected_denied: bool = Field(False, description="该用例是否期望被拒绝（无凭证/越权）")
    tenant_identity: str = Field("", description="越权复测双身份标识（仅作证据）")
    raw_evidence: dict[str, Any] = Field(
        default_factory=dict, description="调用方自由附带的额外证据"
    )


# ============================================================================
# 基础端点
# ============================================================================
@app.get("/health")
def health() -> dict[str, Any]:
    """存活探针：进程活着即 200。"""
    return {
        "status": PullStatus.OK.value,
        "service": "testgen-service",
        "contract_version": CONTRACT_VERSION,
    }


@app.get("/ready")
def ready() -> dict[str, Any]:
    """就绪探针：数据库可用即 200。"""
    try:
        init_db()
        conn = connect()
        conn.execute("SELECT 1")
        conn.close()
    except Exception as exc:  # noqa: BLE001 - 就绪检查需兜住一切异常并如实上报
        return {"status": "not_ready", "detail": type(exc).__name__}
    # M20：就绪探针只返回最小状态，绝不回显 db_path/output_dir/workspace_root 等内部路径
    return {"status": "ready"}


# ============================================================================
# 业务端点 v1
# ============================================================================
def _apply_code_sources(req: PipelineRequest, opts: pipeline.PipelineOptions) -> None:
    """把「代码来源类」字段覆盖到选项（F1 仓库地址 / F2 变更清单 / 模式 / 基线）。"""
    for attr in ("project_name", "mode", "base", "target", "prd_source", "repo_url"):
        value = getattr(req, attr, None)
        if value:
            setattr(opts, attr, value)
    if req.scopes:
        opts.scopes = set(req.scopes)
    # F2：显式变更文件清单（统一正斜杠，与 git diff 输出形态对齐）
    files = [str(f).strip().replace("\\", "/") for f in req.changed_files if str(f).strip()]
    if files:
        opts.changed_files = files
    sel = [str(p).strip().replace("\\", "/") for p in req.selected_paths if str(p).strip()]
    if sel:
        opts.selected_paths = sel
    if req.pull_deepen:
        opts.pull_deepen = int(req.pull_deepen)
    # 取码凭证：与扫描链路共用同一 PAT。请求带 code_source 时用其 access_key，
    # 否则按 repo_url 从仓库配置带出——避免 generate 用 CODEUP_TOKEN（可能失效）
    # 而 scan 用 repo_configs 有效 PAT 导致的「扫描成功、取码 invalid token」割裂。
    # 凭证只存 opts 内存供 git 注入，绝不回显 / 落库 / 入产物。
    if opts.repo_url and "codeup.aliyun.com" in opts.repo_url.lower():
        cs = _resolve_code_source(opts.repo_url, req.code_source or {})
        opts.codeup_token = (cs.get("access_key") or "").strip()
    _apply_repo_specs(req, opts)


def _apply_repo_specs(req: PipelineRequest, opts: pipeline.PipelineOptions) -> None:
    """多仓改造（v1.7）：把请求 repos 转成引擎 RepoSpec；空则保持 []（单仓零回归）。

    每个仓的 PAT 按本仓 repo_url 单独带出，避免多仓共用一份失效凭证。
    """
    if not req.repos:
        return
    opts.repo_specs = []
    for r in req.repos:
        token = ""
        if "codeup.aliyun.com" in r.repo_url.lower():
            token = (
                _resolve_code_source(r.repo_url, req.code_source or {}).get("access_key") or ""
            ).strip()
        opts.repo_specs.append(
            pipeline.RepoSpec(
                repo_key=str(r.repo_key or "").strip(),
                local_path=r.local_path,
                repo_url=r.repo_url,
                base=r.base,
                target=r.target,
                ref=r.ref,
                codeup_token=token,
                changed_files=[str(f).replace("\\", "/") for f in r.changed_files],
                selected_paths=[str(p).replace("\\", "/") for p in r.selected_paths],
            )
        )


def _apply_request(req: PipelineRequest, opts: pipeline.PipelineOptions) -> None:
    """把请求字段覆盖到选项上（表驱动）。

    顺序即优先级：调用方须先让智能输入框填（可覆盖默认值），再调本函数（覆盖输入框）。
    """
    _apply_code_sources(req, opts)
    target = opts.target_req
    for attr, value in (
        ("base_url", req.test_url),
        ("login_url", req.login_url),
        ("login_user", req.login_user),
        ("login_password", req.login_password),
        ("login_otp", req.login_otp),
    ):
        if value:
            setattr(target, attr, value)
    if req.runtime_routes:
        target.routes = [*req.runtime_routes, *target.routes]
    if req.runtime_ui or req.test_url or req.login_url:
        target.enabled = True
    if req.exec_url:
        target.exec_url = req.exec_url
    target.execute = req.execute or bool(req.exec_url)
    target.allow_write = req.allow_write
    target.ui_click = req.ui_click


def build_opts_from_request(req: PipelineRequest) -> pipeline.PipelineOptions:
    """把 HTTP 请求转成流水线选项（表驱动）：同步 / 异步两条入口共用，避免逻辑分叉。"""
    invalid = set(req.scopes or []) - set(ALL_TP_TYPES)
    if invalid:
        raise ValidationError(f"非法行为维度：{sorted(invalid)}，允许 {ALL_TP_TYPES}")
    opts = pipeline.default_options(req.local_path, mode=req.mode or MODE_FULL)
    if req.auto_input.strip():
        pipeline.apply_auto_input(opts, parse_auto_input(req.auto_input))
    _apply_request(req, opts)
    opts.include_business = req.include_business
    opts.extract_pages = req.extract_pages
    opts.persist = req.persist
    opts.llm.enabled = bool(req.llm_enhance and settings.llm_enhance)
    # 专家审阅（逐页大模型）改为「请求显式开启」：默认关，避免 URL 通道下 39 页 × 50s
    # 串行调用导致任务长时间无进度、并迅速吃满当日 LLM token 预算。
    opts.expert_mode = bool(req.expert_ui and settings.expert_mode)
    # 地址通道范围：默认「只测与变更相关的页面/接口」，避免增量模式下整站巡检刷出几百条
    # 与本次改动无关的用例；显式传 full 才回到全站巡检。
    opts.target_runtime = str(req.runtime_scope or "targeted").strip().lower() != "full"
    return opts


@app.post("/api/v1/pipeline", dependencies=[Depends(require_auth)])
def run_pipeline(req: PipelineRequest) -> Any:
    """一站式生成：默认同步执行，`async_mode=true` 时转后台任务（M18）。

    注：服务化主入口是 `POST /api/v1/generate`（异步、可轮询、可幂等）。
    本端点保留同步语义供一次性脚本 / 本地调试使用；大仓同步跑会长时间挂住 HTTP
    连接，调用方应改传 `async_mode=true` 拿 `task_id` 轮询。
    """
    opts = build_opts_from_request(req)
    if req.async_mode:
        # 复用 /generate 的任务模型（同一状态机、同一幂等键空间、同一 poll_url），不另起一套机制
        idem = _idem_key_for_generate(req)
        existing = task_store.find_by_idempotency(idem)
        if existing and existing.state != TaskState.CANCELLED.value:
            return _task_accepted(existing, status_code=202, reused=True)
        task = task_store.create(
            kind="pipeline",
            idempotency_key=idem,
            request=_redacted_request(req),
        )
        executor.submit(
            task.task_id,
            partial(
                _run_generation_job,
                task.task_id,
                opts,
                force_execute=bool(opts.target_req.execute),
            ),
        )
        return _task_accepted(task, 202)

    result = pipeline.run_pipeline_auto(opts)

    payload: dict[str, Any] = {"result": result.to_dict()}
    if result.project_id:
        outputs = OutputWriter().write_all(
            result.project_id, result.test_points, result.cases, result.to_dict()
        )
        payload["outputs"] = outputs
        if req.readonly_lock:
            payload["readonly"] = (
                WorkspaceManager().lock(Path(req.local_path).name, verify=True)
                if req.local_path and _is_managed(req.local_path)
                else {"skipped": "外部目录未纳入工作区管理"}
            )
    return payload


# ============================================================================
# P5 服务化：异步生成任务（仅「生成测试用例」环节）
# ============================================================================
def _task_accepted(
    task: GenerationTask, status_code: int = 202, reused: bool = False
) -> JSONResponse:
    body = task.to_dict()
    body["poll_url"] = f"/api/v1/tasks/{task.task_id}"
    # reused=True 表示命中幂等键、直接复用历史任务（不会重新跑管道）。
    # 面板据此提示「这是旧产物」，避免把历史点数误判成「本次生成的」。
    body["reused"] = reused
    return JSONResponse(status_code=status_code, content=body)


def _redacted_request(req: PipelineRequest) -> str:
    """持久化请求体但不含明文凭证（账号 / 密码 / 动态口令一律掩码）。"""
    data = req.model_dump()
    for key in ("login_password", "login_otp", "login_user"):
        if data.get(key):
            data[key] = "***"
    return json.dumps(data, ensure_ascii=False)


def _runtime_sig(req: PipelineRequest) -> str:
    """运行时/执行侧实质输入的指纹（决定是否走真实登录 + 元素级发现）。

    幂等键缺这段会让「同一仓库但换了被测地址」命中旧任务，直接返回上一次产物
    （历史缺陷：面板填了新地址、按了生成，结果仍是 0 点旧项目）。
    口令只记「是否提供」，不把明文写进键。
    """
    return "|".join(
        [
            req.test_url or "",
            req.login_url or "",
            req.login_user or "",
            "1" if req.login_otp else "0",
            "1" if req.runtime_ui else "0",
            "1" if req.ui_click else "0",
            req.exec_url or "",
            "1" if req.execute else "0",
            "1" if req.allow_write else "0",
            "1" if req.llm_enhance else "0",
            "1" if req.expert_ui else "0",
            str(req.runtime_scope or "targeted"),
            "1" if req.include_business else "0",
            "1" if req.extract_pages else "0",
        ]
    )


@lru_cache(maxsize=1)
def _engine_rev() -> str:
    """引擎代码指纹（`engine/`、`core/` 下 .py 的 mtime+大小摘要）。

    幂等键必须带上它：引擎改过之后，同样的请求参数会产出不同结果。若不带，面板会
    一直复用改造前的旧任务（历史现场：修完「变更靶向」后重新生成，点数仍是旧的 208）。
    """
    root = Path(__file__).resolve().parent.parent
    items: list[str] = []
    for sub in ("engine", "core"):
        for path in sorted((root / sub).glob("*.py")):
            try:
                st = path.stat()
            except OSError:  # pragma: no cover - 文件被并发删除时忽略
                continue
            items.append(f"{path.name}:{int(st.st_mtime)}:{st.st_size}")
    if not items:  # pragma: no cover - 目录结构异常时的兜底
        return "na"
    return hashlib.sha1("\n".join(items).encode("utf-8")).hexdigest()[:8]  # nosec B324 - 仅幂等键指纹，非安全用途


def _idem_key_for_execute(req: ExecuteRequest) -> str:
    """执行验证的幂等键：项目 + 执行侧实质输入 + 引擎版本。

    「只有项目ID」是不够的：同一项目换地址、开/关「放行写」「真实点击」都会改变执行结论，
    此时若仍命中第一次的执行任务，面板会直接复用旧结论（看起来像点了没反应）。
    """
    return "exec:" + "|".join(
        [
            str(req.project_id),
            req.test_url or "",
            req.exec_url or "",
            req.login_url or "",
            req.login_user or "",
            "1" if req.login_otp else "0",
            "1" if req.allow_write else "0",
            "1" if req.ui_click else "0",
            _engine_rev(),
        ]
    )


def _idem_key_for_generate(req: PipelineRequest) -> str:
    # M11 修复：补充 selected_paths，否则「同一仓库 + 不同勾选子集」会命中旧的幂等任务，
    # 返回与本次请求范围不符的历史产物。
    # 追加：运行时/执行输入 + 智能输入框摘要，避免「换了被测环境仍返回旧产物」。
    auto = (req.auto_input or "").strip()
    auto_digest = (
        hashlib.sha1(auto.encode("utf-8")).hexdigest()[:10]  # nosec B324 - 幂等键摘要，非安全用途
        if auto
        else ""
    )
    # 多仓改造（v1.7）：同一 project 下不同仓集合/顺序 = 不同任务，不得命中旧产物。
    # 用有序 (repo_key, path, url, base, target) 元组的摘要，repo_key 排序保证与输入顺序无关。
    repo_signature = ""
    if req.repos:
        signature = sorted(
            (
                str(r.repo_key or ""),
                str(r.local_path or ""),
                str(r.repo_url or ""),
                str(r.base or ""),
                str(r.target or ""),
            )
            for r in req.repos
        )
        repo_signature = hashlib.sha1(  # nosec B324 - 仅幂等键摘要，非安全用途
            repr(signature).encode("utf-8")
        ).hexdigest()[:12]
    return "gen:" + "|".join(
        [
            repo_signature,
            req.local_path,
            req.repo_url,
            req.mode or "full",
            req.base or "",
            req.target or "",
            ",".join(sorted(req.scopes)),
            ",".join(sorted(req.changed_files)),
            ",".join(sorted(req.selected_paths)),
            req.project_name,
            _runtime_sig(req),
            auto_digest,
            # 引擎版本：代码改动后不得复用旧任务产物（否则修了逻辑面板还显示旧点数）
            _engine_rev(),
        ]
    )


def _run_generation_job(
    task_id: str, opts: pipeline.PipelineOptions, *, force_execute: bool = False
) -> dict[str, Any]:
    """后台作业：跑生成链路。

    - `force_execute=False`（默认）：强制生成-only（与 `/api/v1/generate` 的红线一致）；
    - `force_execute=True`：生成后紧接着执行验证（对应 `/api/v1/execute` 的「生成+执行」、
      webhook 的 `execute=true`）。执行结论由 `stage_persist → record_execution` 自动落库。
    """
    if force_execute:
        opts.target_req.execute = True
        opts.persist = True
    best = {"frac": 0.0}

    def _progress(stage: str, info: dict[str, Any]) -> None:
        if task_store.is_cancelled(task_id):
            raise TaskCancelledError(task_id)
        frac = _stage_progress(stage, info, best["frac"])
        best["frac"] = frac
        task_store.update(task_id, progress=round(frac, 3), stage=_stage_with_detail(stage, info))

    result = pipeline.run_pipeline_auto(opts, progress=_progress)
    payload: dict[str, Any] = result.to_dict()
    if result.project_id:
        task_store.update(task_id, project_id=result.project_id)
        payload["outputs"] = OutputWriter().write_all(
            result.project_id, result.test_points, result.cases, result.to_dict()
        )
    return payload


@app.post("/api/v1/generate", dependencies=[Depends(require_auth)])
def submit_generate(req: PipelineRequest) -> JSONResponse:
    """异步提交「生成测试用例」任务（P5 服务化主入口）。

    - 返回 **202** + `task_id` + `poll_url`；结果经 `GET /api/v1/tasks/{task_id}` 轮询；
    - **生成-only 红线**：`execute` / `exec_url` 一律拒绝（422）——本服务当前只生成用例；
    - 幂等：同一 `(路径/repo/模式/基线/目标/范围/变更集)` 的重复提交，若仍在 pending/running
      则返回既有 `task_id`，不会重复跑。
    """
    return submit_generate_core(req)


def submit_generate_core(req: PipelineRequest) -> JSONResponse:
    """`/api/v1/generate` 与对话端点共用的异步提交核心（薄封装，无行为差异）。"""
    if req.execute or req.exec_url:
        raise ValidationError(
            "本服务当前仅提供「生成测试用例」能力，不执行用例（execute/exec_url 不被接受）"
        )
    opts = build_opts_from_request(req)
    opts.target_req.execute = False  # 双保险：强制生成-only，绝不落入 stage_execute
    idem = _idem_key_for_generate(req)
    existing = task_store.find_by_idempotency(idem)
    if existing and existing.state != TaskState.CANCELLED.value:
        return _task_accepted(existing, status_code=202, reused=True)
    task = task_store.create(kind="generate", idempotency_key=idem, request=_redacted_request(req))
    executor.submit(task.task_id, lambda: _run_generation_job(task.task_id, opts))
    return _task_accepted(task, status_code=202)


# ============================================================================
# P1-2 对话代理：模板优先 + 自由文本兜底（只生成用例，不执行）
# ============================================================================
@app.get("/api/v1/dialogue/template", dependencies=[Depends(require_auth)])
def dialogue_template() -> dict[str, Any]:
    """返回对话模板 schema（字段 / 选项 / 条件显隐规则 / 维度真值）。"""
    return default_dialogue_template().render_template_json()


@app.post("/api/v1/dialogue/submit", dependencies=[Depends(require_auth)])
def dialogue_submit(body: DialogueSubmitRequest) -> JSONResponse:
    """对话提交：模板填写值 或 自由文本 → 校验 → 复用异步生成提交。

    与 `/api/v1/generate` 共用 `submit_generate_core`，因此同样满足
    **生成-only 红线 + 幂等 + 可轮询**。凭证在 `redacted()` 视图中掩码，不回显。
    """
    agent = DialogueAgent()
    if body.filled:
        plan = agent.handle_template(body.filled)
    elif body.text.strip():
        plan = agent.handle_text(body.text)
    else:
        raise ValidationError("需提供 filled（模板填写值）或 text（自由文本）之一")

    if not plan.ok:
        raise ValidationError("；".join(plan.errors))

    req = PipelineRequest(**plan.params)
    return submit_generate_core(req)


@app.post("/api/v1/execute", dependencies=[Depends(require_auth)])
def submit_execute(req: ExecuteRequest) -> JSONResponse:
    """异步提交「执行验证」任务（G-2 服务化执行侧）。

    - 给了 `project_id`：对**已有用例**执行验证（不重新生成），等价于 CLI `testgen execute`；
    - 给了代码/地址来源：先生成用例再执行验证（等价于 CLI `pipeline --execute`）。
    结果经 `GET /api/v1/tasks/{task_id}` 轮询，复用同一张任务表（`kind=execute`）。

    ⚠️ 诚实边界：本端点**只做只读验证**（默认不执行写操作、不真实点击 UI）。要主动污染被测环境，
    调用方须显式传 `allow_write=true` / `ui_click=true` 并确认环境可写。写操作失败不会伪造通过。
    """
    if req.project_id:
        opts = _build_exec_opts_from_request(req)
        idem = _idem_key_for_execute(req)
        existing = task_store.find_by_idempotency(idem)
        if existing and existing.state != TaskState.CANCELLED.value:
            return _task_accepted(existing, status_code=202, reused=True)
        task = task_store.create(
            kind="execute",
            idempotency_key=idem,
            request=json.dumps(req.model_dump(), ensure_ascii=False),
        )
        executor.submit(
            task.task_id, lambda: _run_execution_job(task.task_id, req.project_id, opts)
        )
        return _task_accepted(task, status_code=202)

    # 生成 + 执行：复用 PipelineRequest 同义字段，避免两套校验逻辑分叉
    pr = PipelineRequest(**req.model_dump(exclude={"project_id"}))
    if req.scopes:
        invalid = set(req.scopes) - set(ALL_TP_TYPES)
        if invalid:
            raise ValidationError(f"非法行为维度：{sorted(invalid)}，允许 {ALL_TP_TYPES}")
    opts = build_opts_from_request(pr)
    idem = "genexec:" + "|".join(
        [
            req.local_path,
            req.repo_url,
            req.mode or "full",
            ",".join(sorted(req.scopes)),
            req.test_url,
            req.exec_url,
            ",".join(sorted(req.selected_paths)),
        ]
    )
    existing = task_store.find_by_idempotency(idem)
    if existing and existing.state != TaskState.CANCELLED.value:
        return _task_accepted(existing, status_code=202, reused=True)
    task = task_store.create(kind="execute", idempotency_key=idem, request=_redacted_request(pr))
    executor.submit(
        task.task_id, lambda: _run_generation_job(task.task_id, opts, force_execute=True)
    )
    return _task_accepted(task, status_code=202)


@app.get("/api/v1/tasks", dependencies=[Depends(require_auth)])
def list_tasks(limit: int = 20) -> dict[str, Any]:
    """任务清单（最近优先），便于运营后台 / 流水线巡检。"""
    tasks = task_store.list_recent(limit=limit)
    return {"tasks": [t.to_dict() for t in tasks]}


@app.get("/api/v1/tasks/{task_id}", dependencies=[Depends(require_auth)])
def get_task(task_id: str) -> dict[str, Any]:
    """轮询任务进度 / 结果。终态（success/failed/cancelled）后 `result` / `error` 落地。"""
    task = task_store.get(task_id)
    if task is None:
        raise NotFoundError(f"任务不存在：{task_id}")
    return task.to_dict()


class WebhookRequest(BaseModel):
    """发布流水线 webhook 载荷（仅携带触发生成所需的最小信息，不含凭证）。"""

    event: str = Field("", description="发布事件类型，如 deployment.success")
    target: str = Field("", description="被测目标名（用作 project_name）")
    env: str = Field("", description="环境：staging / prod")
    commit: str = Field("", description="刚发布的 commit（增量对比的 target ref）")
    base_url: str = Field("", description="已发布实例地址（生成用例时作 base_url 上下文）")
    health_url: str = Field("", description="存活探针地址（仅记录，本服务不主动探活执行）")
    callback_url: str = Field("", description="回调地址（预留；当前生成完成不主动回调）")
    repo_url: str = Field("", description="仓库地址（F1 取码）")
    local_path: str = Field("", description="本地目录（优先于 repo_url）")
    scopes: list[str] = Field(default_factory=list, description="行为维度范围：正常/异常/安全/边界")
    mode: str = Field("", description="full / incremental")
    execute: bool = Field(
        False,
        description="生成完成后是否紧接着执行验证（默认否：仅生成用例）。需被测实例已就绪",
    )


@app.post("/api/v1/verify/webhook", dependencies=[Depends(require_auth)])
def verify_webhook(req: WebhookRequest) -> JSONResponse:
    """发布流水线 webhook：收到 `deployment.success` → 触发「生成测试用例」任务。

    ⚠️ 诚实边界：默认**只生成用例、不执行**。若载荷带 `execute=true`（需被测实例已就绪），
    则在生成完成后紧接着执行只读验证，结论随任务结果返回（`stage_persist` 自动落库）。
    执行默认只读（不写操作、不真实点击），避免污染刚发布的实例。
    幂等：同一 `(target, commit, mode, scopes, execute)` 的重复事件只跑一次。
    """
    invalid = set(req.scopes) - set(ALL_TP_TYPES)
    if invalid:
        raise ValidationError(f"非法行为维度：{sorted(invalid)}，允许 {ALL_TP_TYPES}")
    idem = "wh:" + "|".join(
        [
            req.target or req.repo_url,
            req.commit,
            req.mode or "full",
            ",".join(sorted(req.scopes)),
            "exec" if req.execute else "gen",
        ]
    )
    existing = task_store.find_by_idempotency(idem)
    if existing and existing.state != TaskState.CANCELLED.value:
        return _task_accepted(existing, status_code=202, reused=True)
    opts = pipeline.default_options(req.local_path or "", mode=req.mode or MODE_FULL)
    if req.repo_url:
        opts.repo_url = req.repo_url
    if req.base_url:
        opts.target_req.base_url = req.base_url
    if req.target:
        opts.project_name = req.target
    if req.scopes:
        opts.scopes = set(req.scopes)
    opts.persist = True
    kind = "webhook_execute" if req.execute else "webhook_generate"
    task = task_store.create(
        kind=kind,
        idempotency_key=idem,
        request=json.dumps(req.model_dump(), ensure_ascii=False),
    )
    executor.submit(
        task.task_id, lambda: _run_generation_job(task.task_id, opts, force_execute=req.execute)
    )
    return _task_accepted(task, status_code=202)


@app.get("/api/v1/metrics", dependencies=[Depends(require_auth)])
def metrics() -> dict[str, Any]:
    """监控端点：任务总量 / 按状态分布 / 队列深度 / 最近任务（供 Prometheus 文本化或巡检）。"""
    counts = task_store.counts_by_state()
    recent = task_store.list_recent(5)
    return {
        "service": "testgen-service",
        "contract_version": CONTRACT_VERSION,
        "tasks_total": sum(counts.values()),
        "by_state": counts,
        "queue_depth": counts.get(TaskState.PENDING.value, 0)
        + counts.get(TaskState.RUNNING.value, 0),
        "recent": [t.to_dict() for t in recent],
    }


@app.post("/api/v1/parse-input", dependencies=[Depends(require_auth)])
def parse_input(req: ParseInputRequest) -> dict[str, Any]:
    """统一智能输入框：只解析混排文本，返回**掩码视图**（可用于前端实时预览）。

    绝不回显明文凭证——前端据此确认「识别对不对」，而不是把密码再抄一遍。
    """
    parsed = parse_auto_input(req.text)
    return {
        "recognized": parsed.recognized_fields(),
        "parsed": parsed.redacted(),
    }


@app.post("/api/v1/compare", dependencies=[Depends(require_auth)])
def compare(req: CompareRequest) -> dict[str, Any]:
    """语义比对（P0-2）：给定「预期 + 实际」输出 LLM 增强比对结论。

    - 纯计算端点：不落库、不写文件、不触执行动作；与 `/api/v1/generate` 生成-only 红线隔离；
    - 复用 comparator 模块（规则为主·LLM 增强）：未配置 LLM 时仅规则判定，
      LLM 失败/超时/非 JSON → 诚实降级 `inconclusive`，**绝不假装通过**；
    - 安全红线：入参不含任何凭证字段；comparator 已对 reason/diff 中敏感值掩码；
      不回显、不落库、不入产物、不写日志明文。
    """
    opts = ComparatorOptions.from_settings()
    if req.items:
        inputs = [
            CompareInput(
                case=it.get("case") or {},
                actual=it.get("actual") or {},
                context=it.get("context") or {},
            )
            for it in req.items
        ]
        verdicts = compare_batch(inputs, opts)
        return {"verdicts": [v.to_dict() for v in verdicts], "count": len(verdicts)}
    inp = CompareInput(case=req.case, actual=req.actual, context=req.context)
    verdict = compare_one(inp, opts)
    return {"verdict": verdict.to_dict()}


class AnalyzeRequest(BaseModel):
    local_path: str
    project_name: str = ""
    include_business: bool = True
    extract_pages: bool = True


class PullRequest(BaseModel):
    """取码请求（F1）：把仓库准备到本地并（可选）算出变更集。

    与 `/api/v1/pipeline` 的 `repo_url` 的区别：本端点**只取码**、不跑生成链路——
    供上游平台在触发流水线前先确认「代码拿到了、变更集对不对」。凭证只在本请求内传递。
    """

    repo_url: str = Field("", description="仓库地址（含凭证时结果与日志一律掩码）")
    local_path: str = Field("", description="本地目标目录；缺省落到工作区根/仓库名（受管目录）")
    base: str = Field("", description="基线 ref（与 target 一起算变更集）")
    target: str = Field("", description="目标 ref；WORKTREE 表示与当前工作区比较")
    deepen: int = Field(0, ge=0, description="浅克隆加深的提交数（0=不加深）")


@app.post("/api/v1/pull", dependencies=[Depends(require_auth)])
def pull_code(req: PullRequest) -> dict[str, Any]:
    """取码（F1）：克隆 / 更新 / 加深 / 算变更集。

    失败**不抛 5xx**：取码失败是常见业务结果（远端不可达、非空目录、ref 失效），
    以 `success=false` + `status` 如实返回，调用方据 `status` 决策。
    凭证（URL 中的 user:token）在返回结果中**一律掩码**。
    """
    import os

    from engine import pull as pull_engine

    url = req.repo_url or (os.environ.get("REPO_URL") or "").strip()
    local_path = req.local_path or str(settings.workspace_root / pipeline.repo_dir_name(url))
    result = pull_engine.pull(url, local_path, base=req.base, target=req.target, deepen=req.deepen)
    return {"pull": result.to_dict()}


@app.post("/api/v1/analyze", dependencies=[Depends(require_auth)])
def analyze(req: AnalyzeRequest) -> dict[str, Any]:
    """只做「扫描 + 功能点提取」，不生成测试点与用例。

    应用与流水线**同一套**功能点语义合并口径（F5），否则同一份代码经 `/analyze`
    与 `/pipeline` 会得到两个不同的功能点数——那是最难排查的一类「数字不一致」。
    """
    from engine import fp_extract, fp_merge, scan  # 局部导入：避免服务壳顶部依赖过重

    scanner = scan.Scanner(req.local_path)
    files = scanner.index()
    extracted = fp_extract.extract_functional_points(
        files, include_business=req.include_business, extract_pages=req.extract_pages
    )
    fps = extracted.functional_points
    merge_stats: dict[str, Any] = {"deduped": 0}
    if settings.fp_semantic_merge:
        fps, merge_stats = fp_merge.dedupe_functional_points(fps)
    counts: dict[str, int] = {}
    for fp in fps:
        counts[fp.ftype] = counts.get(fp.ftype, 0) + 1
    return {
        "files": len(files),
        "counts": counts,
        "fp_semantic_merge": {
            "rule_version": fp_merge.MERGE_RULE_VERSION,
            "deduped": int(merge_stats.get("deduped", 0)),
            "examples": list(merge_stats.get("examples") or []),
        },
        "errors": extracted.errors[:20],
        "functional_points": [fp.to_dict() for fp in fps[:500]],
    }


# ---------------------------------------------------------------------------
# 分支阶段快照与对比（C3/C4：stage-analyze / stage-compare / stages 列表）
# ---------------------------------------------------------------------------


class StageAnalyzeRequest(BaseModel):
    """阶段快照请求：对本地仓库当前状态做一次分析并登记为阶段快照。"""

    local_path: str = Field(..., description="本地仓库路径（也是 StageStore 的 repo 键）")
    branch: str = Field("master", description="分支名（快照归属）")
    commit: str = Field("", description="显式 commit（缺省取当前 HEAD）")
    include_business: bool = True
    extract_pages: bool = True
    # 多仓改造（v1.7）：project/repo 归属，用于快照目录隔离（同名仓不撞）。
    # 缺省空串＝旧单仓行为（快照落在默认命名空间）。
    project_id: int | None = Field(None, description="项目 id（快照命名空间）")
    repo_key: str = Field("", description="项目内仓库短名（快照命名空间）")


class StageCompareRequest(BaseModel):
    """两阶段对比请求。from_ref/to_ref 为 stage_id 或分支 ref（由 StageStore.resolve）。"""

    repo: str = Field(..., description="仓库路径（与 stage-analyze 的 local_path 一致）")
    from_ref: str
    to_ref: str
    repo_path: str = Field("", description="可选；提供时附带两 commit 的 git diff 统计")
    project_id: int | None = Field(None, description="项目 id（快照命名空间，与 analyze 一致）")
    repo_key: str = Field("", description="项目内仓库短名（快照命名空间）")


def _stage_store(project_id: int | None = None, repo_key: str = ""):
    from engine.stage_snapshot import StageStore

    namespace = f"p{project_id}_{repo_key}" if project_id is not None and repo_key else ""
    return StageStore(settings.workspace_root / "stages", namespace=namespace)


@app.post("/api/v1/stage-analyze", dependencies=[Depends(require_auth)])
def stage_analyze(req: StageAnalyzeRequest) -> dict[str, Any]:
    """扫描 + 功能点提取 + 登记阶段快照（C3）。

    与 `/api/v1/analyze` 同一套功能点语义（含合并开关）；产物作为 `analysis`
    artifact 存入 StageStore，供 stage-compare 对比两阶段差异。
    """
    from engine import fp_extract, fp_merge, scan
    from engine.stage_snapshot import create_snapshot

    scanner = scan.Scanner(req.local_path)
    files = scanner.index()
    extracted = fp_extract.extract_functional_points(
        files, include_business=req.include_business, extract_pages=req.extract_pages
    )
    fps = extracted.functional_points
    if settings.fp_semantic_merge:
        fps, _ = fp_merge.dedupe_functional_points(fps)

    repo = str(Path(req.local_path).resolve())
    store_stages = _stage_store(req.project_id, req.repo_key)
    snap = create_snapshot(store_stages, repo, req.branch, commit=req.commit or None)
    analysis = {
        "stage_id": snap.stage_id,
        "commit": snap.commit,
        "branch": snap.branch,
        # 对比引擎按 id 索引；功能点用 fp_id 做稳定键
        "test_points": [
            {
                "id": fp.fp_id,
                "name": fp.name,
                "title": fp.title,
                "module": fp.module,
                "file_path": fp.file_path,
                "ftype": fp.ftype,
            }
            for fp in fps
        ],
        "issues": [],
    }
    store_stages.save_artifact(snap, "analysis", analysis)
    return {
        "ok": True,
        "stage_id": snap.stage_id,
        "commit": snap.commit,
        "branch": snap.branch,
        "stats": {"files": len(files), "functional_points": len(fps)},
    }


@app.post("/api/v1/stage-compare", dependencies=[Depends(require_auth)])
def stage_compare(req: StageCompareRequest) -> dict[str, Any]:
    """两阶段产物对比（C3）：新增/失效/保留的功能点与问题 + 可选 diff 统计。"""
    from engine.stage_compare import compare_stages

    repo = str(Path(req.repo).resolve())
    report = compare_stages(
        _stage_store(req.project_id, req.repo_key),
        repo,
        req.from_ref,
        req.to_ref,
        repo_path=req.repo_path or None,
    )
    return {"ok": True, "report": report.to_dict()}


@app.get("/api/v1/stages", dependencies=[Depends(require_auth)])
def list_stages(
    repo: str,
    branch: str = "",
    project_id: int | None = None,
    repo_key: str = "",
) -> dict[str, Any]:
    """列某仓库（可选某分支）的全部阶段快照（前端 from/to 下拉数据源）。"""
    store_stages = _stage_store(project_id, repo_key)
    repo = str(Path(repo).resolve())
    snaps = store_stages.list_by_branch(repo, branch) if branch else store_stages.list_all(repo)
    return {"ok": True, "stages": [s.to_dict() for s in snaps]}


@app.get("/api/v1/projects", dependencies=[Depends(require_auth)])
def list_projects() -> dict[str, Any]:
    return {"projects": store.list_projects()}


@app.get("/api/v1/projects/{pid}/test-points", dependencies=[Depends(require_auth)])
def get_test_points(pid: int) -> dict[str, Any]:
    return {"project_id": pid, "test_points": store.list_test_points(pid)}


@app.get("/api/v1/projects/{pid}/cases", dependencies=[Depends(require_auth)])
def get_cases(pid: int, include_obsolete: bool = False) -> dict[str, Any]:
    return {
        "project_id": pid,
        "cases": store.list_cases(pid, include_obsolete=include_obsolete),
    }


@app.get("/api/v1/projects/{pid}/traceability", dependencies=[Depends(require_auth)])
def get_traceability(pid: int) -> dict[str, Any]:
    return {"project_id": pid, "traceability": store.traceability(pid)}


@app.get("/api/v1/projects/{pid}/runs", dependencies=[Depends(require_auth)])
def get_runs(pid: int, limit: int = 50) -> dict[str, Any]:
    """执行批次列表（F12 留痕）：最新在前，含状态分布与批次终态。"""
    batches = store.list_run_batches(pid, limit=limit)
    return {"project_id": pid, "batches": batches, "latest": batches[0] if batches else None}


@app.get("/api/v1/projects/{pid}/runs/{batch_id}", dependencies=[Depends(require_auth)])
def get_run_detail(pid: int, batch_id: str, limit: int = 500) -> dict[str, Any]:
    """单批次详情：批次终态 + 逐条执行结论（`cases.last_result` 的来源）。"""
    batch = store.get_run_batch(batch_id)
    if batch is None or int(batch.get("project_id") or 0) != pid:
        raise NotFoundError(f"执行批次不存在：{batch_id}")
    return {
        "project_id": pid,
        "batch": batch,
        "runs": store.list_runs(pid, batch_id=batch_id, limit=limit),
    }


@app.get("/api/v1/projects/{pid}/report", dependencies=[Depends(require_auth)])
def get_report(
    pid: int, batch: str = "", format: str = ReportFormat.JSON.value, write: bool = False
):
    """项目报告（F15）：执行摘要 + 覆盖率 + 趋势 + 追溯 + 执行证据。

    - `format=json`（默认）→ 返回报告结构（机读，含全部字段）；
    - `format=md` → 直接返回可交付 Markdown；`format=html` → 返回自包含浅色页面；
    - `write=true` → 同时把 `REPORT.md` / `REPORT.html` / `report.json` 落到 `outputs/<pid>/`。

    报告是 DB 事实的**纯函数**（取数 `store.report_snapshot` → 计算 `engine.report`），
    不新建报告表——同一份数据任意时刻重算结论一致。
    """
    fmt = (format or "").strip().lower()
    if fmt not in REPORT_FORMATS:
        raise ValidationError(f"不支持的报告格式：{format!r}，允许 {list(REPORT_FORMATS)}")
    built = report_engine.generate(pid, batch_id=batch, write=write)
    body = built["report"]
    if fmt == ReportFormat.MARKDOWN.value:
        return Response(content=render_markdown(body), media_type="text/markdown; charset=utf-8")
    if fmt == ReportFormat.HTML.value:
        return HTMLResponse(content=render_html(body))
    # G-11：可选导出格式（pdf / docx / xlsx）经 export_report 落盘后返回下载地址；
    # 缺可选依赖时 export_report 抛 ReportExportError → 转 422 + pip install 提示。
    if fmt in (ReportFormat.PDF.value, ReportFormat.WORD.value, ReportFormat.EXCEL.value):
        out_dir = get_settings().output_dir / str(pid)
        out_dir.mkdir(parents=True, exist_ok=True)
        try:
            out_path = export_report(body, fmt, str(out_dir / f"REPORT.{fmt}"))
        except ReportExportError as exc:
            raise ValidationError(str(exc)) from exc
        return {"project_id": pid, "format": fmt, "download": out_path, "report": body}
    payload: dict[str, Any] = {"project_id": pid, "report": body}
    if built["outputs"]:
        payload["outputs"] = built["outputs"]
    return payload


@app.get("/api/v1/projects/{pid}/coverage", dependencies=[Depends(require_auth)])
def get_coverage(pid: int) -> dict[str, Any]:
    """覆盖率视图（F16）：功能点 → 测试点 → 用例，含未覆盖缺口。"""
    snapshot = store.report_snapshot(pid)
    if not snapshot.get("project"):
        raise NotFoundError(f"项目不存在：{pid}")
    return {
        "project_id": pid,
        "coverage": report_engine.coverage(
            snapshot["functional_points"], snapshot["test_points"], snapshot["cases"]
        ),
    }


@app.get("/api/v1/workspaces", dependencies=[Depends(require_auth)])
def list_workspaces() -> dict[str, Any]:
    return {"workspaces": [w.to_dict() for w in WorkspaceManager().list_all()]}


# ============================================================================
# 质量分析与数据导入（迁移自 test-accel：trend/flaky/change_logs/import 等）
# ============================================================================
@app.get("/api/v1/projects/{pid}/reports/trend", dependencies=[Depends(require_auth)])
def project_trend(pid: int, only_finished: bool = True, limit: int = 50) -> dict[str, Any]:
    """多批次通过率趋势（迁移自 test-accel analytics.trend）。"""
    return analytics.trend(pid, only_finished=only_finished, limit=limit)


@app.get("/api/v1/projects/{pid}/reports/flaky", dependencies=[Depends(require_auth)])
def project_flaky(pid: int, min_batches: int = 2) -> dict[str, Any]:
    """跨批次结果不一致的不稳定用例识别（迁移自 test-accel analytics.flaky）。"""
    return analytics.flaky(pid, min_batches=min_batches)


@app.get("/api/v1/projects/{pid}/change_logs", dependencies=[Depends(require_auth)])
def project_change_logs(pid: int, limit: int = 100) -> dict[str, Any]:
    """查看该项目历次增量对比/变更记录。"""
    logs = store.list_change_logs(pid, limit=limit)
    return {"ok": True, "count": len(logs), "change_logs": logs}


@app.get("/api/v1/projects/{pid}/functional-points", dependencies=[Depends(require_auth)])
def project_functional_points(pid: int, limit: int = 500) -> dict[str, Any]:
    """功能点独立查询（此前仅内嵌于 /analyze 响应）。"""
    fps = store.list_functional_points(pid)
    return {"ok": True, "count": len(fps), "functional_points": fps[: max(1, int(limit))]}


@app.get("/api/v1/projects/{pid}/filter-options", dependencies=[Depends(require_auth)])
def project_filter_options(pid: int) -> dict[str, Any]:
    """可用筛选维度与取值分布（cases + test_points），供前端筛选器。"""
    opts = store.filter_options(pid)
    return {"ok": True, "project_id": pid, **opts}


class ImportTestPointsRequest(BaseModel):
    """外部测试点导入：直接给 items 或给服务端 json_path（test-accel 阶段2 产物兼容）。"""

    items: list[dict[str, Any]] = Field(default_factory=list, description="测试点列表")
    json_path: str = Field("", description="服务端 JSON 文件路径（items 为空时读取）")
    replace: bool = Field(True, description="true=清空后全量导入；false=按 tp_id 覆盖更新")


@app.post("/api/v1/projects/{pid}/import-test-points", dependencies=[Depends(require_auth)])
def project_import_test_points(pid: int, req: ImportTestPointsRequest) -> dict[str, Any]:
    """导入外部测试点 JSON 到引擎库（「测试点 → 用例」链路的前置步骤）。"""
    tps = req.items
    if not tps and req.json_path:
        try:
            data = json.loads(Path(req.json_path).read_text(encoding="utf-8"))
        except OSError as exc:
            raise ValidationError(f"读取测试点 JSON 失败：{exc}") from exc
        loaded: object = data.get("test_points") if isinstance(data, dict) else data
        if not isinstance(loaded, list):
            raise ValidationError('JSON 结构不对：期望 {"test_points": [...]} 或数组')
        tps = loaded
    if not tps:
        raise ValidationError("请提供 items 或有效的 json_path")
    result = store.import_test_points(pid, tps, replace=req.replace)
    return {"ok": True, "project_id": pid, **result}


@app.post("/api/v1/projects/{pid}/import-test-points/upload", dependencies=[Depends(require_auth)])
async def project_import_test_points_upload(
    pid: int,
    file: UploadFile = File(...),
    replace: bool = Form(True),
) -> dict[str, Any]:
    """导入外部测试点（文件上传版，供控制台「📥 导入测试点」按钮使用）。"""
    try:
        raw = await file.read()
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"解析上传文件失败：{exc}") from exc
    tps = data.get("test_points") if isinstance(data, dict) else data
    if not isinstance(tps, list):
        raise ValidationError('JSON 结构不对：期望 {"test_points": [...]} 或数组')
    if not tps:
        raise ValidationError("文件为空或没有 test_points")
    result = store.import_test_points(pid, tps, replace=replace)
    return {"ok": True, "project_id": pid, **result}


@app.post("/api/v1/projects/{pid}/rerun-flaky", dependencies=[Depends(require_auth)])
def project_rerun_flaky(pid: int) -> dict[str, Any]:
    """对当前项目的不稳定用例（flaky）做针对性重跑，刷新其结论留痕。"""
    flaky = analytics.flaky(pid)
    if not flaky.get("count"):
        return {"ok": True, "rerun": 0, "batch_id": "", "message": "没有 flaky 用例"}
    tp_ids = [f["tp_id"] for f in flaky["flaky"] if f.get("tp_id")]
    if not tp_ids:
        return {"ok": True, "rerun": 0, "batch_id": "", "message": "flaky 用例缺少 tp_id，无法重跑"}
    summary = pipeline.run_execution(pid, pipeline.default_options(), tp_ids=tp_ids)
    return {
        "ok": True,
        "rerun": len(tp_ids),
        "batch_id": summary.get("batch_id", ""),
        "message": f"已对 {len(tp_ids)} 个 flaky 测试点发起重跑",
    }


# ============================================================================
# 扫描块联动：分支 / 提交 / 中文功能描述 / 任务取消（前端 console.html）
# ============================================================================


class RefsRequest(BaseModel):
    """分支/提交下拉所用：仓库 + 只读凭证 + （commits 用）ref。"""

    repo_url: str = Field("", description="仓库地址（git@/ssh/https/*.git）")
    local_path: str = Field("", description="本地 git 目录（与 repo_url 二选一）")
    code_source: dict[str, Any] = Field(
        default_factory=dict, description="只读凭证 {credential_type, org_id, access_key}"
    )
    ref: str = Field("", description="分支/标签/commit（commits 端点必填）")


class DescribeItem(BaseModel):
    """单个待描述项：文件（也可为函数/类）。"""

    path: str = ""
    name: str = ""
    kind: str = "file"
    functions: list[str] = Field(default_factory=list)
    classes: list[str] = Field(default_factory=list)


class DescribeRequest(BaseModel):
    """把扫描出的文件节点批量翻译成中文功能描述。

    ``repo_url``：用于描述缓存命中（同名文件按 (repo_url, file_path) 区分，避免串味）；
    为空则不做缓存，全部走 LLM（兼容旧调用）。
    """

    items: list[DescribeItem] = Field(default_factory=list)
    repo_url: str = ""


class DescribeCacheClearRequest(BaseModel):
    """清空文件描述缓存：repo_url 为空 = 清空全部；否则只清该仓库。"""

    repo_url: str = ""


def _describe_item_key(it: Any) -> str:
    """取缓存键：优先 path，回退 name。"""
    if isinstance(it, DescribeItem):
        return (it.path or it.name or "").strip()
    return str((it.get("path") or it.get("name") or "").strip())


def _describe_symbols_sig(it: Any) -> str:
    """文件「结构指纹」：函数/类排序后 sha1。sig 一致 = 内容/结构未变 → 可复用历史描述。"""
    import hashlib
    import json

    if isinstance(it, DescribeItem):
        funcs = sorted(it.functions or [])
        classes = sorted(it.classes or [])
    else:
        funcs = sorted(it.get("functions") or [])
        classes = sorted(it.get("classes") or [])
    raw = json.dumps({"f": funcs, "c": classes}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()  # nosec B324 - 仅作缓存内容指纹，非安全用途


def _resolve_code_source(repo_url: str, code_source: dict[str, Any]) -> dict[str, Any]:
    """凭证解析：请求未带 access_key 时，按仓库地址从仓库配置表取已存凭证（免每次手填）。"""
    cs = dict(code_source or {})
    if not (cs.get("access_key") or "").strip():
        saved = store.find_repo_creds_by_url(repo_url or "")
        if saved:
            cs.setdefault("credential_type", "pat")
            if not (cs.get("org_id") or "").strip():
                cs["org_id"] = saved["org_id"]
            cs["access_key"] = saved["access_key"]
    return cs


def _build_codeup_client(req: RefsRequest) -> tuple[Any, str]:
    """从 RefsRequest 构造只读 Codeup 客户端（统一校验口径）。"""
    code_source = _resolve_code_source(req.repo_url, req.code_source or {})
    if not req.repo_url:
        raise ValidationError("repo_url 必填")
    access_key = (code_source.get("access_key") or "").strip()
    if not access_key:
        raise ValidationError("远程只读取码：凭证（access_key）为空")
    client = client_from_code_source(code_source, verify_ssl=False)
    repo_id = parse_repo_to_repo_id(req.repo_url)
    return client, repo_id


@app.post("/api/v1/branches", dependencies=[Depends(require_auth)])
def list_repo_branches(req: RefsRequest) -> dict[str, Any]:
    """列分支（供前端目标/基线 ref 下拉：分支名 + 最近提交时间 + 标题）。

    分派顺序（打通三类仓库）：
      1) Codeup 只读凭证（access_key）→ 走 Codeup API（企业内网仓库）；
      2) 本地 git 目录（local_path）→ 列本地/远程跟踪分支；
      3) 远程 git 地址（git@/ssh/https/*.git）→ ``git ls-remote``（GitHub/自建通用，复用本机 SSH/凭据）。

    远端不可达等业务失败以 ``ok=False`` 返回（不动 5xx），前端据 `error` 提示。
    """
    code_source = _resolve_code_source(req.repo_url or "", req.code_source)
    access_key = (code_source.get("access_key") or "").strip()
    try:
        if access_key:
            client, repo_id = _build_codeup_client(req)
            branches = client.list_branches(repo_id)
            return {"ok": True, "branches": branches, "source": "codeup"}
        if req.local_path and Path(req.local_path).expanduser().exists():
            branches = service_repos.list_local_branches(req.local_path)
            return {"ok": True, "branches": branches, "source": "local"}
        if service_repos.is_git_url(req.repo_url):
            branches = service_repos.list_git_branches(req.repo_url)
            return {"ok": True, "branches": branches, "source": "git"}
    except CodeupError:
        raise  # AppError → 由统一异常处理器映射（远端鉴权/不可达）
    except ValidationError:
        raise
    except Exception as exc:  # noqa: BLE001 - 远端不可达等通用异常
        log.warning("列分支失败", extra=log_extra(err=type(exc).__name__))
        return {"ok": False, "branches": [], "error": str(exc)[:200]}
    return {
        "ok": False,
        "branches": [],
        "error": "无法识别仓库类型：请提供 Codeup access_key，或有效的 git 地址 / 本地 git 目录",
    }


@app.post("/api/v1/commits", dependencies=[Depends(require_auth)])
def list_repo_commits(req: RefsRequest) -> dict[str, Any]:
    """列指定 ref 的最近提交（选定分支后查看对比数据）。"""
    if not req.ref:
        raise ValidationError("ref 必填（分支/标签/commit）")
    try:
        client, repo_id = _build_codeup_client(req)
        commits = client.list_commits(repo_id, ref_name=req.ref)
    except CodeupError:
        raise
    except ValidationError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("列提交失败", extra=log_extra(err=type(exc).__name__))
        return {"ok": False, "ref": req.ref, "commits": [], "error": str(exc)[:200]}
    return {"ok": True, "ref": req.ref, "commits": commits}


def _extract_json_block(content: str) -> Any:
    """从 LLM 输出中稳健抽取 JSON（容忍 ```json 围栏与前后闲文）。"""
    import json as _json
    import re as _re

    text = (content or "").strip()
    fenced = _re.search(r"```(?:json)?\s*(.*?)\s*```", text, _re.S)
    candidate = fenced.group(1) if fenced else text
    try:
        return _json.loads(candidate)
    except _json.JSONDecodeError:
        pass
    # 退一步：抓第一个 { 到最后一个 } 之间的内容
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return _json.loads(candidate[start : end + 1])
        except _json.JSONDecodeError:
            return None
    return None


def _describe_cache_lookup(
    repo_url: str, items: list[Any]
) -> tuple[dict[str, str], dict[str, bool], list[Any]]:
    """缓存命中判断：返回 (descriptions, cached_map, need)。

    统一按 (repo_url, file_path) + 符号指纹查库（repo_url 为空时以空串作命名空间，
    使本地目录场景也能复用缓存）；指纹一致且有描述 → 命中（cached=True），否则进 need 待 LLM 生成。
    """
    descriptions: dict[str, str] = {}
    cached_map: dict[str, bool] = {}
    sigs = {_describe_item_key(it): _describe_symbols_sig(it) for it in items}
    cached_rows = store.get_file_descriptions(repo_url, list(sigs.keys()))
    need: list[Any] = []
    for it in items:
        key = _describe_item_key(it)
        row = cached_rows.get(key)
        if row and row["symbols_sig"] == sigs[key] and row["description"]:
            descriptions[key] = row["description"]
            cached_map[key] = True
        else:
            cached_map[key] = False
            need.append(it)
    return descriptions, cached_map, need


def _describe_build_prompt(need: list[Any]) -> str:
    """构造 LLM 提示词：把文件内部真实符号喂给模型，让其对每个文件生成中文功能描述。"""
    lines = []
    for it in need:
        key = _describe_item_key(it)
        if isinstance(it, DescribeItem):
            name = it.name or it.path
            funcs = it.functions or []
            classes = it.classes or []
        else:
            name = it.get("name") or it.get("path") or key
            funcs = it.get("functions") or []
            classes = it.get("classes") or []
        symbols = []
        if classes:
            symbols.append("类[" + ", ".join(classes[:12]) + "]")
        if funcs:
            symbols.append("函数[" + ", ".join(funcs[:20]) + "]")
        sym_txt = "；".join(symbols) if symbols else "（未提取到符号）"
        lines.append(f"- key: {key} | 文件: {name} | 内部符号: {sym_txt}")
    return (
        "你是测试用例系统的代码分析助手。下面列出若干代码文件及其内部真实提取到的类/函数名，"
        "请为每一项用 1-2 句简洁的中文描述：①这个文件是做什么的（什么模块/页面/服务）；"
        "②它主要承载哪些业务功能（结合内部符号推断，不要逐字翻译符号名）。\n"
        '只输出 JSON，格式：{"结果": [{"key": <文件key>, "功能": <中文描述>}, ...]}\n'
        + "\n".join(lines)
    )


def _describe_persist(repo_url: str, need: list[Any], new_desc: dict[str, str]) -> None:
    """把 LLM 新生成的描述写回缓存（按 (repo_url, path) 作唯一键，repo_url 为空时亦缓存）。

    M18：各组并发调用 LLM，但**写库必须串行**——SQLite（WAL）支持多读单写，
    多线程同时写会互相抢锁（本机 safe-delete 拦截 WAL 辅助文件时还会退化为
    readonly）。写操作很短，串行不损并发收益；写失败也只降级为「本次不缓存」，
    绝不影响已经拿到的描述返回给调用方。
    """
    try:
        with _DESCRIBE_DB_LOCK:
            for it in need:
                key = _describe_item_key(it)
                if key in new_desc:
                    store.upsert_file_description(
                        repo_url,
                        key,
                        _describe_symbols_sig(it),
                        new_desc[key],
                        settings.llm_model or "",
                    )
    except Exception as exc:  # noqa: BLE001 - 缓存写失败不影响本次响应（下次再补）
        log.warning(
            "describe 缓存写入失败（本次不缓存）",
            extra=log_extra(err=type(exc).__name__),
        )


def _describe_parse(parsed: Any) -> dict[str, str]:
    """从 LLM 输出解析出 {path: 中文描述}（兼容 结果/result/items 多种键）。"""
    new_desc: dict[str, str] = {}
    rows = (
        parsed.get("结果") or parsed.get("result") or parsed.get("items") or []
        if isinstance(parsed, dict)
        else []
    )
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = row.get("key") or row.get("名称") or row.get("name") or ""
        fn = row.get("功能") or row.get("desc") or ""
        if key and fn:
            new_desc[str(key)] = str(fn)
    return new_desc


def _describe_merge(
    need: list[Any],
    new_desc: dict[str, str],
    descriptions: dict[str, str],
    cached_map: dict[str, bool],
) -> int:
    """把 LLM 新描述合并进结果；返回未拿到描述（需回退展示符号）的文件数。"""
    unresolved = 0
    for it in need:
        key = _describe_item_key(it)
        if key in new_desc:
            descriptions[key] = new_desc[key]
            cached_map[key] = False
        else:
            cached_map[key] = False
            unresolved += 1
    return unresolved


def _describe_run_chunks(repo_url: str, chunks: list[list[Any]]) -> dict[str, str]:
    """并发跑各分组 LLM（M18），带总预算兜底；返回已拿到的描述。

    - 组间并发上限 `_DESCRIBE_MAX_WORKERS`：LLM 是 IO 等待型，4 路足以把
      「6 组 × 60s」压到「2 轮 × 60s」量级，且不给上游模型造成突发压力。
    - 总预算 `describe_budget_seconds`：超预算后不再等待剩余组，未完成组由
      `_describe_merge` 计入 `unresolved`（前端回退展示符号名），**不假装成功**。
    - 单组异常只影响该组（沿用原降级语义）。
    """
    if not chunks:
        return {}
    budget = float(getattr(settings, "describe_budget_seconds", 90) or 90)
    pool = ThreadPoolExecutor(max_workers=min(_DESCRIBE_MAX_WORKERS, len(chunks)))
    merged: dict[str, str] = {}
    try:
        futures = {pool.submit(_describe_one_chunk, repo_url, c): c for c in chunks}
        try:
            for fut in as_completed(futures, timeout=budget):
                merged.update(fut.result() or {})
        except TimeoutError:
            pending = sum(1 for f in futures if not f.done())
            log.warning(
                "describe 超出总预算，未完成组回退原名",
                extra=log_extra(budget=budget, chunks=len(chunks), pending=pending),
            )
    finally:
        # 超预算时不能 wait=True（会把响应再挂满一个 llm_timeout）；运行中的线程
        # 会在自身 timeout 到达后自然结束，不影响返回结果。
        pool.shutdown(wait=False, cancel_futures=True)
    return merged


def _describe_one_chunk(repo_url: str, chunk: list[Any]) -> dict[str, str]:
    """单组（≤5 条）调一次 LLM 并写缓存；失败返回空字典（该组回退展示原名）。"""
    prompt = _describe_build_prompt(chunk)
    try:
        resp = chat_with_fallback(
            channel="describe",
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
            model=settings.llm_model,
            timeout=float(settings.llm_timeout or 60),
            model_chain=settings.llm_model_chain or None,
            messages=[{"role": "user", "content": prompt}],
        )
        content = resp.choices[0].message.content or ""
        chunk_desc = _describe_parse(_extract_json_block(content))
    except Exception as exc:  # noqa: BLE001 - 诚实降级：该组 LLM 失败不阻塞其他组
        log.warning("describe LLM 失败，该组回退原名", extra=log_extra(err=type(exc).__name__))
        return {}
    if chunk_desc:
        _describe_persist(repo_url, chunk, chunk_desc)
    return chunk_desc


@app.post("/api/v1/describe", dependencies=[Depends(require_auth)])
def describe_features(req: DescribeRequest) -> dict[str, Any]:
    """把扫描出的文件节点批量翻译成中文功能描述（带缓存命中，避免重复烧 LLM）。

    缓存策略：
      - 统一按 (repo_url, file_path) + 符号指纹查 ``file_descriptions``（repo_url 为空时以空串作命名空间，
        本地目录同样可复用缓存）；指纹一致 → 直接读历史描述（标记 cached=True），不再调 LLM；
        否则调 LLM 生成并写回缓存。
    诚实降级：未配置 LLM / LLM 失败 → 已命中的缓存照常返回，未命中的文件前端回退展示类/函数名。
    """
    items = req.items or []
    if not items:
        return {"ok": True, "llm": False, "descriptions": {}, "cached": {}, "error": "empty"}
    llm_ready = bool(settings.llm_api_key and settings.llm_base_url and settings.llm_model)
    repo_url = (req.repo_url or "").strip()

    descriptions, cached_map, need = _describe_cache_lookup(repo_url, items)
    if not need:
        return {
            "ok": True,
            "llm": llm_ready,
            "descriptions": descriptions,
            "cached": cached_map,
            "cache_hits": len(descriptions),
            "unresolved": 0,
            "error": "" if llm_ready else "未配置 LLM（仅命中缓存）",
        }
    if not llm_ready:
        return {
            "ok": True,
            "llm": False,
            "descriptions": descriptions,
            "cached": cached_map,
            "cache_hits": sum(1 for v in cached_map.values() if v),
            "unresolved": len(need),
            "error": "未配置 LLM（llm_api_key / llm_base_url / llm_model 不全）——未命中缓存的文件将回退展示类/函数名",
        }

    # M18：单批量过大（如 30 条一次调 LLM）易超时被整批丢弃；改为每 5 条一组，
    # **组间并发**（原为串行 for 循环，6 组最坏 6×llm_timeout）。单组失败只回退该组。
    chunks = [need[i : i + _DESCRIBE_CHUNK_SIZE] for i in range(0, len(need), _DESCRIBE_CHUNK_SIZE)]
    new_desc = _describe_run_chunks(repo_url, chunks)
    unresolved = _describe_merge(need, new_desc, descriptions, cached_map)
    return {
        "ok": True,
        "llm": True,
        "descriptions": descriptions,
        "cached": cached_map,
        "cache_hits": sum(1 for v in cached_map.values() if v),
        "unresolved": unresolved,
    }


@app.post("/api/v1/describe/cache/clear", dependencies=[Depends(require_auth)])
def clear_describe_cache(req: DescribeCacheClearRequest) -> dict[str, Any]:
    """清空文件描述缓存（换模型 / 强制刷新时用）。

    ``repo_url`` 为空 = 清空全部；否则只清该仓库。
    """
    deleted = store.clear_file_descriptions((req.repo_url or "").strip())
    return {"ok": True, "deleted": deleted, "scope": (req.repo_url or "").strip() or "all"}


@app.post("/api/v1/tasks/{task_id}/cancel", dependencies=[Depends(require_auth)])
def cancel_task(task_id: str) -> dict[str, Any]:
    """取消运行中/排队中的任务（协作式：作业在阶段边界检出后中断）。"""
    cancelled = task_store.cancel(task_id)
    return {"ok": True, "cancelled": cancelled, "task_id": task_id}


@app.get("/api/v1/tasks/{task_id}/cases", dependencies=[Depends(require_auth)])
def task_cases(task_id: str) -> dict[str, Any]:
    """取某任务对应项目已生成的用例（前端详情面板用）。"""
    task = task_store.get(task_id)
    if task is None:
        raise NotFoundError(f"任务不存在: {task_id}")
    pid = task.project_id
    if not pid:
        return {"ok": True, "task_id": task_id, "project_id": None, "cases": []}
    cases = store.list_cases(pid, include_obsolete=True)
    return {"ok": True, "task_id": task_id, "project_id": pid, "cases": cases}


class ScanRequest(BaseModel):
    """快速扫描（只读）：列模块树 + 标注变更状态，供前端勾选范围。"""

    repo_url: str = Field("", description="仓库地址（git 模式）")
    code_source: dict[str, Any] = Field(default_factory=dict, description="只读凭证")
    ref: str = Field("", description="目标 ref（分支/标签/commit）")
    base_ref: str = Field("", description="基线 ref（对比出 新增/修改）")
    local_path: str = Field("", description="本地目录（local 模式）")


def _dispatch_scan(req: ScanRequest) -> tuple[list[dict[str, Any]], str, dict[str, Any]]:
    """执行扫描并返回 (items, source, meta)；远端/本地异常由调用方统一处理。

    分派口径与「列分支」一致（三类仓库都可用）：
      1) 本地目录 → ``scan_local``；
      2) Codeup 地址（含域名或显式 PAT）→ 原只读 HTTP API（不克隆，需 PAT）；
      3) 通用 git 地址（git@ / ssh:// / https://*.git）→ 浅克隆到临时目录后本地扫描，
         复用本机 git 凭据（与 generate 的 ``pull`` 同口径），**不要求 PAT**。
    """
    if req.local_path:
        items, meta = scan_local(req.local_path, base_ref=req.base_ref)
        return items, "local", meta
    if req.repo_url:
        url = req.repo_url.strip()
        cs = _resolve_code_source(url, req.code_source or {})
        access_key = (cs.get("access_key") or "").strip()
        is_codeup = "codeup.aliyun.com" in url.lower() or bool(access_key)
        if is_codeup:
            if not access_key:
                raise ValidationError("远程扫描：凭证（access_key）为空")
            items, meta = scan_repository(
                cs, url, ref=req.ref, base_ref=req.base_ref, verify_ssl=False
            )
            return items, "remote", meta
        if service_repos.is_git_url(url):
            items, meta = scan_git_repo(url, ref=req.ref, base_ref=req.base_ref)
            return items, "remote", meta
        raise ValidationError("无法识别仓库类型：请提供 Codeup access_key 或有效的 git 地址")
    raise ValidationError("repo_url 或 local_path 至少给一个")


class RepoRefsRequest(BaseModel):
    """多仓只读批量请求（v1.7）：一次列出项目下所有仓的分支/提交/扫描树。"""

    project_id: int = Field(..., description="项目 id（从 repo_bindings 读仓库清单）")
    kind: str = Field("branches", description="branches / commits / scan")
    ref: str = Field("", description="commits/scan 用；各仓可带自己的（binding.ref 优先）")
    base_ref: str = Field("", description="scan 用：基线 ref")
    code_source: dict[str, Any] = Field(
        default_factory=dict, description="只读凭证（按各仓 repo_url 带出，为空则从仓库配置取）"
    )


@app.post("/api/v1/repos/refs", dependencies=[Depends(require_auth)])
def list_repo_refs(req: RepoRefsRequest) -> dict[str, Any]:
    """多仓只读批量端点：按 repo_bindings 逐仓列分支/提交/扫描树，按 repo_key 分组。

    单仓失败不阻断其他仓（错误记在该仓条目的 `error` 字段）；凭证按各仓 repo_url
    从 code_source / 仓库配置带出。旧单仓端点（`/branches`、`/commits`、`/scan`）不变。
    """
    if req.kind not in ("branches", "commits", "scan"):
        raise ValidationError(f"非法 kind：{req.kind}，允许 branches/commits/scan")
    with store.connect() as conn:
        bindings = store.list_repo_bindings(req.project_id, conn)
    if not bindings:
        return {"ok": True, "project_id": req.project_id, "kind": req.kind, "repos": {}}

    repos: dict[str, Any] = {}
    for b in bindings:
        key = str(b["repo_key"])
        entry: dict[str, Any] = {"source": "", "error": ""}
        try:
            if req.kind == "branches":
                refs_req = RefsRequest(
                    repo_url=b["repo_url"] or "",
                    local_path=b["local_path"] or "",
                    code_source=req.code_source,
                    ref="",
                )
                result = list_repo_branches(refs_req)
                entry.update(
                    branches=result.get("branches", []),
                    source=result.get("source", ""),
                )
                if not result.get("ok"):
                    entry["error"] = str(result.get("error") or "")
            elif req.kind == "commits":
                refs_req = RefsRequest(
                    repo_url=b["repo_url"] or "",
                    local_path=b["local_path"] or "",
                    code_source=req.code_source,
                    ref=req.ref or b["ref"] or "",
                )
                result = list_repo_commits(refs_req)
                entry.update(commits=result.get("commits", []), ref=refs_req.ref)
                if not result.get("ok"):
                    entry["error"] = str(result.get("error") or "")
            else:  # scan
                scan_req = ScanRequest(
                    repo_url=b["repo_url"] or "",
                    local_path=b["local_path"] or "",
                    code_source=req.code_source,
                    ref=req.ref or b["ref"] or "",
                    base_ref=req.base_ref,
                )
                items, source, meta = _dispatch_scan(scan_req)
                entry.update(items=items, source=source, meta=meta)
        except (CodeupError, ValidationError) as exc:
            entry["error"] = str(exc)[:200]
        except Exception as exc:  # noqa: BLE001 - 单仓失败不阻断其他仓
            log.warning(
                "多仓只读端点单仓失败", extra=log_extra(repo_key=key, err=type(exc).__name__)
            )
            entry["error"] = str(exc)[:200]
        repos[key] = entry
    return {"ok": True, "project_id": req.project_id, "kind": req.kind, "repos": repos}


@app.post("/api/v1/scan", dependencies=[Depends(require_auth)])
def scan_code(req: ScanRequest) -> dict[str, Any]:
    """快速扫描（只读）：列模块树 + 标注变更状态，供前端勾选范围。不克隆/不生成。

    - git 模式：远程只读列树 + 取符号（前若干文件）；
    - local 模式：复用本地 Scanner；
    - 提供 ``base_ref`` 时经 compare 标注 ``change_status``（新增/修改/未变更），
      变更文件优先入选；``total``/``truncated`` 如实回报截断情况。
    """
    try:
        items, source, meta = _dispatch_scan(req)
    except (CodeupError, ValidationError):
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("扫描失败", extra=log_extra(err=type(exc).__name__))
        return {
            "ok": False,
            "source": "remote" if req.repo_url else "local",
            "items": [],
            "error": str(exc)[:200],
        }
    summary = {"added": 0, "modified": 0, "unchanged": 0}
    for it in items:
        st = it.get("change_status") or CHANGE_UNCHANGED
        if st == CHANGE_ADDED:
            summary["added"] += 1
        elif st == CHANGE_MODIFIED:
            summary["modified"] += 1
        else:
            summary["unchanged"] += 1
    return {
        "ok": True,
        "source": source,
        "items": items,
        "summary": summary,
        "count": len(items),
        "total": int(meta.get("total_matched", len(items))),
        "truncated": bool(meta.get("truncated", False)),
    }


# ============================================================================
# M2：安全语义裁判端点（纯计算，同步返回，无 LLM / 无浏览器）
# ============================================================================
@app.post("/api/v1/verdict", dependencies=[Depends(require_auth)])
def verdict(req: VerdictRequest) -> dict[str, Any]:
    """安全语义裁判（M2）：复用 engine 既有五维判定，对 security 用例的「观测事实」做安全判定。

    - 判定逻辑**不重写**：``run_five_dimension_verdict`` 直接复用 ``engine.executor._judge``；
    - 纯计算（无 LLM / 无浏览器），同步返回，延迟低（M2 注意点 4：不要异步任务化）；
    - 入参不含凭证明文；响应亦不含凭证明文（facts 由 testhub 侧脱敏后构造）；
    - 返回 ``verdict``（safe/unsafe/inconclusive）+ ``dimension``（normal/abnormal/auth/priv_esc/...）
      + 可读 ``reason`` + ``evidence``。
    """
    facts = req.model_dump()
    # auth_mode 允许 None → 归一为空串交给底层判定（F8 公开接口判定）
    if facts.get("auth_mode") is None:
        facts["auth_mode"] = ""
    return run_five_dimension_verdict(facts)


# ============================================================================
# 代码仓库管理（控制台「代码仓库」菜单）：可配置仓库清单 + 连接测试
# ============================================================================
class RepoConfigRequest(BaseModel):
    """仓库配置入参（可存 Codeup 只读凭证，2026-10-10 用户确认；接口只回掩码）。"""

    name: str = Field(..., min_length=1, max_length=100, description="显示名称（唯一）")
    url: str = Field(..., min_length=1, max_length=500, description="本地目录或 git 仓库地址")
    ref: str = Field("", max_length=200, description="分支/标签（可空，测试时顺带校验）")
    remark: str = Field("", max_length=300, description="备注")
    codeup_org_id: str = Field("", max_length=100, description="Codeup 组织 ID（可空）")
    codeup_access_key: str | None = Field(
        None, description="Codeup 只读 PAT；更新时 None=不改（保留原值），空串=清除"
    )


class RepoProbeRequest(BaseModel):
    """未保存地址的即时连接测试入参。"""

    url: str = Field(..., min_length=1, max_length=500)
    ref: str = Field("", max_length=200)


@app.get("/api/v1/repos", dependencies=[Depends(require_auth)])
def list_repos() -> dict[str, Any]:
    """列出已配置的代码仓库（含最近一次连接测试结果）。"""
    items = store.list_repo_configs()
    for it in items:
        it["kind"] = "git" if service_repos.is_git_url(it["url"]) else "local"
    return {"ok": True, "repos": items}


@app.get("/api/v1/repos/export", dependencies=[Depends(require_auth)])
def export_repos() -> dict[str, Any]:
    """导出全部仓库配置（共享/备份），仅含地址/分支/备注。"""
    return {"ok": True, "repo_configs": store.export_repo_configs()}


class ImportReposRequest(BaseModel):
    """共享 JSON 批量导入仓库配置。"""

    items: list[dict[str, Any]] = Field(default_factory=list)
    json_path: str = Field("", description="服务端 JSON 文件路径（items 为空时读取）")


@app.post("/api/v1/repos/import", dependencies=[Depends(require_auth)])
def import_repos(req: ImportReposRequest) -> dict[str, Any]:
    """从共享 JSON 批量导入仓库配置（按 name 幂等 upsert）。"""
    items = req.items
    if not items and req.json_path:
        try:
            data = json.loads(Path(req.json_path).read_text(encoding="utf-8"))
        except OSError as exc:
            raise ValidationError(f"读取仓库配置 JSON 失败：{exc}") from exc
        loaded: object = data.get("repo_configs") if isinstance(data, dict) else data
        if not isinstance(loaded, list):
            raise ValidationError('JSON 结构不对：期望 {"repo_configs": [...]} 或数组')
        items = loaded
    if not items:
        raise ValidationError("请提供 items 或有效的 json_path")
    res = store.import_repo_configs(items)
    return {"ok": True, **res}


@app.post("/api/v1/repos", dependencies=[Depends(require_auth)])
def create_repo(req: RepoConfigRequest) -> dict[str, Any]:
    """新增仓库配置（name 唯一）。"""
    try:
        rid = store.create_repo_config(
            req.name.strip(),
            req.url.strip(),
            ref=req.ref.strip(),
            remark=req.remark.strip(),
            codeup_org_id=req.codeup_org_id,
            codeup_access_key=req.codeup_access_key or "",
        )
    except ContractViolation as exc:
        raise ValidationError(str(exc)) from exc
    return {"ok": True, "id": rid}


@app.put("/api/v1/repos/{rid}", dependencies=[Depends(require_auth)])
def update_repo(rid: int, req: RepoConfigRequest) -> dict[str, Any]:
    """更新仓库配置。"""
    try:
        changed = store.update_repo_config(
            rid,
            name=req.name.strip(),
            url=req.url.strip(),
            ref=req.ref.strip(),
            remark=req.remark.strip(),
            codeup_org_id=req.codeup_org_id,
            codeup_access_key=req.codeup_access_key,  # None=保留原值（前端编辑留空即不改）
        )
    except ContractViolation as exc:
        raise ValidationError(str(exc)) from exc
    if not changed:
        raise NotFoundError(f"仓库配置不存在: {rid}")
    return {"ok": True}


@app.delete("/api/v1/repos/{rid}", dependencies=[Depends(require_auth)])
def delete_repo(rid: int) -> dict[str, Any]:
    """删除仓库配置。"""
    removed = store.delete_repo_config(rid)
    if not removed:
        raise NotFoundError(f"仓库配置不存在: {rid}")
    return {"ok": True}


@app.post("/api/v1/repos/test", dependencies=[Depends(require_auth)])
def test_repo_address(req: RepoProbeRequest) -> dict[str, Any]:
    """即时连接测试（地址可未保存）：git 走只读 ls-remote，本地查目录。"""
    return service_repos.probe_repo(req.url, req.ref)


@app.post("/api/v1/repos/{rid}/test", dependencies=[Depends(require_auth)])
def test_repo_config(rid: int) -> dict[str, Any]:
    """测试已保存仓库的连通性，并回写最近一次结果。"""
    cfg = store.get_repo_config(rid)
    if cfg is None:
        raise NotFoundError(f"仓库配置不存在: {rid}")
    result = service_repos.probe_repo(cfg["url"], cfg["ref"])
    store.record_repo_test(rid, result["ok"], result["message"])
    return {"ok": True, "id": rid, **result}


# ============================================================================
# LLM 厂商管理（多 provider 独立 key；控制台「LLM 配置」菜单）：CRUD + 连接测试
# ============================================================================
class LLMProviderRequest(BaseModel):
    """厂商配置入参（含 api_key：本地 SQLite 存储，列表/详情接口只回显脱敏预览）。"""

    name: str = Field(..., min_length=1, max_length=100, description="显示名称（唯一）")
    base_url: str = Field(..., min_length=1, max_length=500, description="OpenAI 兼容端点")
    api_key: str = Field("", max_length=2000, description="厂商 API Key（可空，留空则不可用）")
    models: str = Field("", max_length=1000, description="逗号分隔的模型名列表")
    is_default: bool = Field(False, description="是否默认厂商")
    priority: int = Field(0, ge=0, le=9999, description="优先级（小优先）")


class LLMProbeRequest(BaseModel):
    """未保存配置的即时连接测试入参。"""

    base_url: str = Field(..., min_length=1, max_length=500)
    api_key: str = Field("", max_length=2000)
    model: str = Field("", max_length=200, description="测试用模型名（可空则自动列举）")


@app.get("/api/v1/llm/providers", dependencies=[Depends(require_auth)])
def list_llm_providers() -> dict[str, Any]:
    """列出已配置的 LLM 厂商（api_key 已脱敏）。"""
    return {"ok": True, "providers": store.list_llm_providers()}


@app.post("/api/v1/llm/providers", dependencies=[Depends(require_auth)])
def create_llm_provider(req: LLMProviderRequest) -> dict[str, Any]:
    """新增厂商配置（name 唯一）。"""
    try:
        pid = store.create_llm_provider(
            req.name.strip(),
            req.base_url.strip(),
            api_key=req.api_key,
            models=req.models,
            is_default=req.is_default,
            priority=req.priority,
        )
    except ContractViolation as exc:
        raise ValidationError(str(exc)) from exc
    return {"ok": True, "id": pid}


@app.put("/api/v1/llm/providers/{pid}", dependencies=[Depends(require_auth)])
def update_llm_provider(pid: int, req: LLMProviderRequest) -> dict[str, Any]:
    """更新厂商配置。"""
    try:
        changed = store.update_llm_provider(
            pid,
            name=req.name.strip(),
            base_url=req.base_url.strip(),
            api_key=req.api_key,
            models=req.models,
            is_default=req.is_default,
            priority=req.priority,
        )
    except ContractViolation as exc:
        raise ValidationError(str(exc)) from exc
    if not changed:
        raise NotFoundError(f"厂商配置不存在: {pid}")
    return {"ok": True}


@app.delete("/api/v1/llm/providers/{pid}", dependencies=[Depends(require_auth)])
def delete_llm_provider(pid: int) -> dict[str, Any]:
    """删除厂商配置。"""
    removed = store.delete_llm_provider(pid)
    if not removed:
        raise NotFoundError(f"厂商配置不存在: {pid}")
    return {"ok": True}


@app.post("/api/v1/llm/providers/test", dependencies=[Depends(require_auth)])
def test_llm_address(req: LLMProbeRequest) -> dict[str, Any]:
    """即时连接测试（配置可未保存）。"""
    return service_llm.probe_llm(req.base_url, req.api_key, req.model)


@app.post("/api/v1/llm/providers/{pid}/test", dependencies=[Depends(require_auth)])
def test_llm_config(pid: int) -> dict[str, Any]:
    """测试已保存厂商的连通性，并回写最近一次结果。"""
    secret = store.get_llm_provider_secret(pid)
    if secret is None:
        raise NotFoundError(f"厂商配置不存在: {pid}")
    first_model = secret["models"].split(",")[0].strip() if secret["models"] else ""
    result = service_llm.probe_llm(secret["base_url"], secret["api_key"], first_model)
    store.record_llm_provider_test(pid, result["ok"], result["message"])
    return {"ok": True, "id": pid, **result}


# ============================================================================
# 平台控制台（单页 SPA）：打开即用的网页界面，免去 curl/接口调用
# ============================================================================
WEB_DIR = Path(__file__).resolve().parent / "web"

# 控制台 API 前缀：默认值与改造前写死的值一致，保证直连 8910 的旧用法行为不变。
_API_PREFIX_DEFAULT = "/api/v1"
# 白名单：仅允许「/ 开头 + 路径安全字符」，杜绝把引号/尖括号注入 JS 字符串（XSS）。
_API_PREFIX_RE = re.compile(r"^/[A-Za-z0-9._~\-/]{0,120}$")


def _resolve_api_prefix(request: Request) -> str:
    """解析控制台 API 前缀：请求头 > query > 默认；非法值一律回退默认。

    平台代理以 `X-Testhub-Api-Prefix` 注入专属前缀（如 `/testgen-api/v1`），
    使控制台不再占用主平台的 `/api/v1/` 命名空间；`?api_prefix=` 仅用于手工调试。
    """
    raw = (
        request.headers.get("X-Testhub-Api-Prefix") or request.query_params.get("api_prefix") or ""
    ).strip()
    if not raw:
        return _API_PREFIX_DEFAULT
    if not _API_PREFIX_RE.fullmatch(raw) or ".." in raw:
        log.warning("非法 api_prefix 已拒绝并回退默认", extra=log_extra(value=raw[:64]))
        return _API_PREFIX_DEFAULT
    return raw.rstrip("/") or _API_PREFIX_DEFAULT


@app.get("/", include_in_schema=False)
def console_home(request: Request) -> HTMLResponse:
    """平台控制台首页：返回 `service/web/index.html`，并注入本次请求的 API 前缀。

    直接打开 :8910 时前缀为默认 `/api/v1`（与改造前一致）；被主平台以 iframe
    嵌入时，由代理通过请求头注入专属前缀，前端 `apiUrl()` 据此改写全部请求。
    """
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    html = html.replace("__TESTGEN_API_PREFIX__", _resolve_api_prefix(request))
    # 前缀随请求变化，禁止缓存，避免切换代理后读到上一次注入的值
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


# 本地静态资源（Chart.js 等）：从 `service/web` 提供，免去外部 CDN 依赖。
app.mount("/static", StaticFiles(directory=str(WEB_DIR), check_dir=True), name="web-static")
