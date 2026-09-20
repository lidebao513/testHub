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

import json
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from core import store
from core.auto_input import parse_auto_input
from core.config import get_settings
from core.contracts import CONTRACT_VERSION
from core.db import connect, init_db
from core.enums import ALL_TP_TYPES, MODE_FULL, REPORT_FORMATS, PullStatus, ReportFormat
from core.errors import AppError, NotFoundError, UnauthorizedError, ValidationError
from core.log import get_logger, log_extra, set_request_id
from engine import pipeline
from engine import report as report_engine
from engine.comparator import (
    ComparatorOptions,
    CompareInput,
    compare_batch,
    compare_one,
)
from engine.dialogue import DialogueAgent, default_dialogue_template
from engine.verdict import run_five_dimension_verdict
from output.report_writer import ReportExportError, export_report, render_html, render_markdown
from output.writer import OutputWriter
from service.tasks import BackgroundExecutor, GenerationTask, TaskState, TaskStore, stage_fraction
from workspace.manager import WorkspaceManager


log = get_logger(__name__)
settings = get_settings()

# P5 服务化：进程内后台执行器（生成任务异步化）。生产升级路径为 broker + 多 consumer。
task_store = TaskStore()
executor = BackgroundExecutor(task_store)


@asynccontextmanager
async def lifespan(_: FastAPI) -> Any:
    """启动建表、关闭时优雅停机 worker 池。"""
    init_db()
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
    if not settings.auth_token:
        return
    if x_auth_token != settings.auth_token:
        raise UnauthorizedError("缺少或无效的 X-Auth-Token")


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
class PipelineRequest(BaseModel):
    local_path: str = Field("", description="被测代码所在目录（可选；与 test_url 至少给一个）")
    repo_url: str = Field(
        "", description="仓库地址（F1）：先取码到 local_path（缺省落到工作区根/仓库名）"
    )
    pull_deepen: int = Field(0, ge=0, description="浅克隆加深的提交数（0=不加深）")
    changed_files: list[str] = Field(
        default_factory=list,
        description="显式变更文件清单（F2，正斜杠相对路径）；优先于本地 git diff",
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
    readonly_lock: bool = Field(False, description="分析完成后是否把目录置只读")
    # —— 地址通道（A1）：凭证字段只用于本次运行，**绝不回显、不落库、不入产物** ——
    test_url: str = Field("", description="被测环境地址（走运行时 UI 发现）")
    login_url: str = Field("", description="登录页地址（缺省自动判定）")
    login_user: str = Field("", description="登录账号（更推荐用环境变量注入）")
    login_password: str = Field("", description="登录密码（更推荐用环境变量注入）")
    login_otp: str = Field("", description="动态口令（更推荐用环境变量注入）")
    runtime_routes: list[str] = Field(default_factory=list, description="显式路由（优先级最高）")
    runtime_ui: bool = Field(False, description="启用运行时 UI 发现（等价 RUNTIME_UI_ENABLED=on）")
    auto_input: str = Field("", description="统一智能输入框：一段混排文本（地址+账号+密码+口令）")
    execute: bool = Field(False, description="执行生成的用例（接口层 + UI 层）")
    allow_write: bool = Field(False, description="放行写操作；默认只跑只读请求")
    ui_click: bool = Field(False, description="UI 层执行真实点击（F13）；默认只做只读断言")
    exec_url: str = Field("", description="执行器被测服务地址（只跑接口层时可单独指定）")


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


def _run_execution_job(
    task_id: str, project_id: int, opts: pipeline.PipelineOptions
) -> dict[str, Any]:
    """后台作业：对已有项目执行验证（不重新生成），进度写回任务表。"""
    best = {"frac": 0.0}

    def _progress(stage: str, _info: dict[str, Any]) -> None:
        frac = stage_fraction(stage, best["frac"])
        best["frac"] = frac
        task_store.update(task_id, progress=round(frac, 3), stage=stage)

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
    observed_body_has_sensitive: bool = Field(
        False, description="响应体是否疑似泄露敏感信息"
    )
    expected_denied: bool = Field(
        False, description="该用例是否期望被拒绝（无凭证/越权）"
    )
    tenant_identity: str = Field("", description="越权复测双身份标识（仅作证据）")
    raw_evidence: dict[str, Any] = Field(default_factory=dict, description="调用方自由附带的额外证据")


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
    return {"status": "ready", "config": settings.public_dict()}


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
    if req.pull_deepen:
        opts.pull_deepen = int(req.pull_deepen)


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
    return opts


@app.post("/api/v1/pipeline", dependencies=[Depends(require_auth)])
def run_pipeline(req: PipelineRequest) -> dict[str, Any]:
    """一站式同步生成（兼容旧调用方）。

    注：服务化主入口已迁移到 `POST /api/v1/generate`（异步、可轮询、可幂等）。
    本端点仍同步执行完整生成链路，便于一次性脚本 / 本地调试。
    """
    opts = build_opts_from_request(req)
    result = pipeline.run_pipeline(opts)

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
def _task_accepted(task: GenerationTask, status_code: int = 202) -> JSONResponse:
    body = task.to_dict()
    body["poll_url"] = f"/api/v1/tasks/{task.task_id}"
    return JSONResponse(status_code=status_code, content=body)


def _redacted_request(req: PipelineRequest) -> str:
    """持久化请求体但不含明文凭证（账号 / 密码 / 动态口令一律掩码）。"""
    data = req.model_dump()
    for key in ("login_password", "login_otp", "login_user"):
        if data.get(key):
            data[key] = "***"
    return json.dumps(data, ensure_ascii=False)


def _idem_key_for_generate(req: PipelineRequest) -> str:
    return "gen:" + "|".join(
        [
            req.local_path,
            req.repo_url,
            req.mode or "full",
            req.base or "",
            req.target or "",
            ",".join(sorted(req.scopes)),
            ",".join(sorted(req.changed_files)),
            req.project_name,
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

    def _progress(stage: str, _info: dict[str, Any]) -> None:
        frac = stage_fraction(stage, best["frac"])
        best["frac"] = frac
        task_store.update(task_id, progress=round(frac, 3), stage=stage)

    result = pipeline.run_pipeline(opts, progress=_progress)
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
        return _task_accepted(existing, status_code=202)
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
        idem = "exec:" + str(req.project_id)
        existing = task_store.find_by_idempotency(idem)
        if existing and existing.state != TaskState.CANCELLED.value:
            return _task_accepted(existing, status_code=202)
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
        ]
    )
    existing = task_store.find_by_idempotency(idem)
    if existing and existing.state != TaskState.CANCELLED.value:
        return _task_accepted(existing, status_code=202)
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
        return _task_accepted(existing, status_code=202)
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
# 平台控制台（单页 SPA）：打开即用的网页界面，免去 curl/接口调用
# ============================================================================
WEB_DIR = Path(__file__).resolve().parent / "web"


@app.get("/", include_in_schema=False)
def console_home() -> FileResponse:
    """平台控制台首页：返回 `service/web/index.html`（自包含 HTML/JS，调用本服务 API）。"""
    return FileResponse(WEB_DIR / "index.html")
