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
from collections.abc import Callable
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from core import store
from core.auto_input import parse_auto_input
from core.config import get_settings, resolve_cors_origins
from core.contracts import CONTRACT_VERSION
from core.db import connect, init_db
from core.enums import ALL_TP_TYPES, MODE_FULL, PullStatus
from core.errors import (
    AppError,
    UnauthorizedError,
    ValidationError,
)
from core.log import get_logger, log_extra, set_request_id
from engine import (
    pipeline,
    url_code_bridge,  # noqa: F401 - 门面符号：routers.generation 通过 A.url_code_bridge 引用
)
from engine.codeup_client import CodeupError, parse_repo_to_repo_id
from engine.comparator import (  # noqa: F401 - 门面符号：测试 patch service.app.compare_* / ComparatorOptions
    ComparatorOptions,
    compare_batch,
    compare_one,
)
from engine.dialogue import (  # noqa: F401 - 门面符号：routers.generation 通过 A.DialogueAgent 等引用
    DialogueAgent,
    default_dialogue_template,
)
from engine.llm_fallback import (
    chat_with_fallback,  # noqa: F401 - 门面符号：测试 patch service.app.chat_with_fallback
)
from engine.remote_scan import (
    client_from_code_source,
    scan_git_repo,
    scan_local,
    scan_repository,
)
from output.writer import OutputWriter
from service import repos as service_repos
from service.routers import analysis as _analysis_routes
from service.routers import config as _config_routes
from service.routers import generation as _generation_routes
from service.routers import projects as _projects_routes
from service.routers import scan as _scan_routes
from service.routers import tasks as _tasks_routes
from service.tasks import (
    BackgroundExecutor,
    GenerationTask,
    TaskCancelledError,
    TaskState,  # noqa: F401 - 门面符号：routers.generation 通过 A.TaskState 引用
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

# B-2 修复：CORS 来源收窄（解析逻辑见 core.config.resolve_cors_origins，与主平台同口径）。
# - prod：必须用 CORS_ALLOWED_ORIGINS 显式配白名单，未配置→不允许任何跨域（fail-closed）；
# - dev/test：未配白名单时保留通配，供本机/局域网控制台演示（旧行为不变）。
# sidecar 用 X-Auth-Token 鉴权而非 Cookie，credentials 仅在有明确白名单时开启。
_cors_origins, _cors_credentials = resolve_cors_origins(
    settings.app_env, settings.cors_allowed_origins
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=_cors_credentials,
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
# 请求模型门面：Batch 7 / A-1 后单一真源在 routers/generation.py。
# 此处 re-export 保持既有测试 from service.app import PipelineRequest 等可用。
from service.routers.generation import (  # noqa: E402, F401 - 门面转发：供测试/外部 from service.app import
    DialogueSubmitRequest,
    ExecuteRequest,
    PipelineRequest,
    RepoSpecRequest,
    UrlGenerationRequest,
)


def _run_execution_job(
    task_id: str, project_id: int, opts: pipeline.PipelineOptions
) -> dict[str, Any]:
    """后台作业：对已有项目执行验证（不重新生成），进度写回任务表。"""
    best = {"frac": 0.0}

    # C-4：把取消检查下沉到引擎的阶段内长循环（进度回调只在阶段边界触发，
    # 大仓扫描/逐文件执行期间无法及时取消）。probe 命中即抛，由 executor.submit
    # 捕获置 cancelled 终态；未取消时零影响。
    def _cancel_probe() -> None:
        if task_store.is_cancelled(task_id):
            raise TaskCancelledError(task_id)

    opts.cancel_probe = _cancel_probe

    def _progress(stage: str, info: dict[str, Any]) -> None:
        _cancel_probe()
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


# ============================================================================
# P5 服务化：异步生成任务（仅「生成测试用例」环节）
# handler 已拆到 service/routers/generation.py（Batch 7 / A-1）；
# 下方 helper / 模型 / 单例保留为门面，供 router 通过 A.xxx 引用与测试 patch。
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


def submit_aux_task(kind: str, request: str, job: Callable[[], dict[str, Any]]) -> JSONResponse:
    """把只读分析类请求（describe / analyze）转后台任务（Batch 4 A-2/D-2）。

    与 `/generate` 共用同一套 task_store / executor / _task_accepted 状态机：
    调用方拿到 202 + task_id + poll_url，结果经 `GET /api/v1/tasks/{task_id}` 轮询。
    仅当请求显式 `async_mode=true` 时走这里，默认同步分支零行为变化。
    分析类作业不含敏感登录凭证，request 仅作可观测留痕。
    """
    task = task_store.create(kind=kind, request=request)
    executor.submit(task.task_id, job)
    return _task_accepted(task, 202)


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

    # C-4：取消探针下沉到引擎阶段内长循环（见 _run_execution_job 注释）。
    def _cancel_probe() -> None:
        if task_store.is_cancelled(task_id):
            raise TaskCancelledError(task_id)

    opts.cancel_probe = _cancel_probe

    def _progress(stage: str, info: dict[str, Any]) -> None:
        _cancel_probe()
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


# ============================================================================
# P1-2 对话代理 / 执行验证：handler 已拆到 service/routers/generation.py（A-1）。
# DialogueAgent / default_dialogue_template 等门面符号保留在 import 区，供 router 引用。
# ============================================================================


def _stage_store(project_id: int | None = None, repo_key: str = ""):
    from engine.stage_snapshot import StageStore

    namespace = f"p{project_id}_{repo_key}" if project_id is not None and repo_key else ""
    return StageStore(settings.workspace_root / "stages", namespace=namespace)


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


def _build_codeup_client(req: Any) -> tuple[Any, str]:
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


def list_repo_branches(req: Any) -> dict[str, Any]:
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


def list_repo_commits(req: Any) -> dict[str, Any]:
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


class ScanRequest(BaseModel):
    """快速扫描（只读）：列模块树 + 标注变更状态，供前端勾选范围。

    字段集合必须与 `service.routers.scan.ScanRequest` 一致（P1 多仓归属）：
    门面符号被 `test_scan_git_repo.py` 直接构造，漂移会导致门面对象缺字段。
    """

    repo_url: str = Field("", description="仓库地址（git 模式）")
    code_source: dict[str, Any] = Field(default_factory=dict, description="只读凭证")
    ref: str = Field("", description="目标 ref（分支/标签/commit）")
    base_ref: str = Field("", description="基线 ref（对比出 新增/修改）")
    local_path: str = Field("", description="本地目录（local 模式）")
    repo_key: str = Field("", description="多仓归属键（repo_bindings.repo_key）")
    project_id: int | None = Field(None, description="项目 id，与 repo_key 组成 multi:<pid>:<key>")
    namespace: str = Field("", description="显式命名空间覆盖（最高优先级，一般不由前端传）")


def _dispatch_scan(req: Any) -> tuple[list[dict[str, Any]], str, dict[str, Any]]:
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


# ============================================================================
# 代码仓库管理（控制台「代码仓库」菜单）：可配置仓库清单 + 连接测试
# ============================================================================


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

# ============================================================================
# 路由挂载：各业务路由已拆到 service/routers/，此处统一 include（#439）
# 鉴权在 include 时注入，router 模块顶层无需访问 app 符号，避免循环导入。
# 必须放在所有共享符号/模型定义之后，router 函数运行时才通过门面引用它们。
# ============================================================================
app.include_router(_config_routes.router, dependencies=[Depends(require_auth)])
app.include_router(_projects_routes.router, dependencies=[Depends(require_auth)])
app.include_router(_scan_routes.router, dependencies=[Depends(require_auth)])
app.include_router(_tasks_routes.router, dependencies=[Depends(require_auth)])
app.include_router(_analysis_routes.router, dependencies=[Depends(require_auth)])
app.include_router(_generation_routes.router, dependencies=[Depends(require_auth)])
