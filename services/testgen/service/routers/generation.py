"""生成主链路路由：pipeline / generate / url-generation / dialogue / execute。

从 service/app.py 拆出（Batch 7 / A-1 收尾）。被测试 patch / import 的
helper、请求模型、单例（task_store / executor / build_opts_from_request /
_idem_key_for_* / OutputWriter 等）全部保留在 app.py 门面，本模块 handler
通过 ``A.xxx`` 运行时引用，保证 ``patch("service.app.X")`` 依旧生效。
鉴权由 app.py include_router 统一注入，本模块不重复声明。

纯结构拆移：handler 逻辑逐字保留，端点 path / method / 请求体模型零变化。
"""

from __future__ import annotations

import hashlib
import json
from functools import partial
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from core.enums import ALL_TP_TYPES
from core.errors import ValidationError
from engine import url_code_bridge
from service import app as A


router: APIRouter = APIRouter()


# 请求模型：与 app.py 门面模型字段逐字一致（A-1 纯结构拆移）。
# 不能从 A 绑定——app.py 在 import 本模块时模型尚未定义（循环导入），
# 与 analysis.py 的 CompareRequest 等同属「router 自持模型」模式。
# app.py 保留同名门面模型供既有测试直接 import，两者字段一致即契约不变。
class RepoSpecRequest(BaseModel):
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


class UrlGenerationRequest(BaseModel):
    """地址优先入口的专用生成请求。"""

    test_url: str = Field("", description="被测环境地址（必填）")
    login_url: str = Field("", description="登录页地址（缺省自动判定）")
    login_user: str = Field("", description="登录账号")
    login_password: str = Field("", description="登录密码")
    login_otp: str = Field("", description="动态口令")
    runtime_routes: list[str] = Field(default_factory=list, description="显式路由（优先级最高）")
    changed_files: list[str] = Field(
        default_factory=list, description="变更文件（正斜杠相对路径，靶向辅助证据）"
    )
    repo_url: str = Field("", description="代码仓库地址（轻量取码桥；空则靶向按文件名推断）")
    code_source: dict[str, Any] = Field(default_factory=dict, description="取码凭证（PAT）")
    base: str | None = Field(None, description="增量基线 ref（取 hunk 用）")
    target: str | None = Field(None, description="增量目标 ref（取 hunk / blob 用）")
    project_id: int | None = Field(
        None, description="已识别的 testgen 项目 id（复用，避免重复建项）"
    )
    project_name: str = Field("", description="项目名（缺省用被测主机名）")
    runtime_scope: str = Field("targeted", description="targeted=变更靶向（默认）/ full=全站巡检")
    scopes: list[str] = Field(default_factory=list, description="行为维度范围")


class DialogueSubmitRequest(BaseModel):
    filled: dict[str, Any] = Field(default_factory=dict, description="模板填写值")
    text: str = Field("", description="自由文本兜底（与 filled 二选一）")


class ExecuteRequest(BaseModel):
    project_id: int = Field(0, description="指定已有项目，仅执行其已生成用例（不重新生成）")
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
    exec_url: str = Field("", description="执行器被测服务地址（只跑接口层时单独指定）")
    allow_write: bool = Field(False, description="放行写操作；默认只跑只读请求")
    ui_click: bool = Field(False, description="UI 层执行真实点击（F13）；默认只做只读断言")
    base: str = Field("", description="基线 ref（增量模式计算 diff）")
    target: str = Field("", description="目标 ref（增量模式）")
    selected_paths: list[str] = Field(
        default_factory=list, description="子集生成：仅对勾选的文件/目录生成测试点（空=全量）"
    )


@router.post("/api/v1/pipeline")
def run_pipeline(req: PipelineRequest) -> Any:
    """一站式生成：默认同步执行，`async_mode=true` 时转后台任务（M18）。

    注：服务化主入口是 `POST /api/v1/generate`（异步、可轮询、可幂等）。
    本端点保留同步语义供一次性脚本 / 本地调试使用；大仓同步跑会长时间挂住 HTTP
    连接，调用方应改传 `async_mode=true` 拿 `task_id` 轮询。
    """
    opts = A.build_opts_from_request(req)
    if req.async_mode:
        # 复用 /generate 的任务模型（同一状态机、同一幂等键空间、同一 poll_url），不另起一套机制
        idem = A._idem_key_for_generate(req)
        existing = A.task_store.find_by_idempotency(idem)
        if existing and existing.state != A.TaskState.CANCELLED.value:
            return A._task_accepted(existing, status_code=202, reused=True)
        task = A.task_store.create(
            kind="pipeline",
            idempotency_key=idem,
            request=A._redacted_request(req),
        )
        A.executor.submit(
            task.task_id,
            partial(
                A._run_generation_job,
                task.task_id,
                opts,
                force_execute=bool(opts.target_req.execute),
            ),
        )
        return A._task_accepted(task, 202)

    result = A.pipeline.run_pipeline_auto(opts)

    payload: dict[str, Any] = {"result": result.to_dict()}
    if result.project_id:
        outputs = A.OutputWriter().write_all(
            result.project_id, result.test_points, result.cases, result.to_dict()
        )
        payload["outputs"] = outputs
        if req.readonly_lock:
            payload["readonly"] = (
                A.WorkspaceManager().lock(A.Path(req.local_path).name, verify=True)
                if req.local_path and A._is_managed(req.local_path)
                else {"skipped": "外部目录未纳入工作区管理"}
            )
    return payload


@router.post("/api/v1/generate")
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
    opts = A.build_opts_from_request(req)
    opts.target_req.execute = False  # 双保险：强制生成-only，绝不落入 stage_execute
    idem = A._idem_key_for_generate(req)
    existing = A.task_store.find_by_idempotency(idem)
    if existing and existing.state != A.TaskState.CANCELLED.value:
        return A._task_accepted(existing, status_code=202, reused=True)
    task = A.task_store.create(
        kind="generate", idempotency_key=idem, request=A._redacted_request(req)
    )
    A.executor.submit(task.task_id, lambda: A._run_generation_job(task.task_id, opts))
    return A._task_accepted(task, status_code=202)


@router.post("/api/v1/url-generation/generate")
def submit_url_generation(req: UrlGenerationRequest) -> JSONResponse:
    """地址优先入口专用生成端点：地址+凭证为主，变更文件经轻量取码桥做靶向辅助。

    与 `/api/v1/generate` 的差异（均为新增能力，不改旧端点）：
    - 强制 ``runtime_ui=True``、``execute=False``（生成-only 红线）；
    - 默认 ``mode=incremental`` + ``runtime_scope=targeted``（地址为主、变更辅助）；
    - 有 ``repo_url`` + ``changed_files`` 时经 ``url_code_bridge`` 只取文件文本供靶向，
      不 clone、不展开代码功能点；复用已识别的 ``project_id`` 避免重复建项。
    异步语义、幂等键、轮询 URL 与 `/generate` 完全一致（同一任务状态机）。
    """
    if not req.test_url.strip():
        raise ValidationError("地址通道生成必须提供被测地址（test_url）")

    # 构造等价的 PipelineRequest，复用现有映射/幂等/掩码/任务机制（零逻辑分叉）。
    # model_validate：与构造函数等价，但避免 mypy 对 Pydantic 默认字段的误报。
    pr = PipelineRequest.model_validate(
        {
            "test_url": req.test_url.strip(),
            "login_url": req.login_url,
            "login_user": req.login_user,
            "login_password": req.login_password,
            "login_otp": req.login_otp,
            "runtime_routes": req.runtime_routes,
            "changed_files": req.changed_files,
            "repo_url": req.repo_url,
            "code_source": req.code_source,
            "base": req.base,
            "target": req.target,
            "project_name": req.project_name,
            "mode": "incremental" if req.changed_files else "full",
            "runtime_ui": True,
            "runtime_scope": req.runtime_scope,
            "scopes": req.scopes,
            "persist": True,
            "execute": False,
        }
    )
    opts = A.build_opts_from_request(pr)
    opts.target_req.execute = False  # 双保险：生成-only，绝不落入 stage_execute
    if req.project_id is not None and req.project_id > 0:
        opts.project_id = req.project_id  # 复用已识别项目，避免每次生成都新建

    # 轻量取码桥：只给靶向供文本，失败降级为空（内部吞异常、记 note）。
    bridge = None
    if req.repo_url.strip() and req.changed_files:
        bridge = url_code_bridge.fetch_changed_sources(
            req.repo_url.strip(),
            req.changed_files,
            code_source=req.code_source,
            base=req.base or "",
            target=req.target or "",
        )
        opts.target_sources = bridge.sources
        opts.target_hunks = bridge.hunks

    idem = (
        A._idem_key_for_generate(pr)
        + "|urlgen:"
        + hashlib.sha1(  # nosec B324 - 幂等键摘要，非安全用途
            (
                ",".join(sorted(req.changed_files))
                + "|"
                + req.repo_url
                + "|"
                + str(req.project_id or "")
            ).encode("utf-8")
        ).hexdigest()[:10]
    )
    existing = A.task_store.find_by_idempotency(idem)
    if existing and existing.state != A.TaskState.CANCELLED.value:
        return A._task_accepted(existing, status_code=202, reused=True)
    task = A.task_store.create(
        kind="url-generation", idempotency_key=idem, request=A._redacted_request(pr)
    )

    def _job() -> dict[str, Any]:
        result = A._run_generation_job(task.task_id, opts)
        if bridge:
            result.setdefault("url_code_bridge", bridge.as_dict())
        return result

    A.executor.submit(task.task_id, _job)
    return A._task_accepted(task, status_code=202)


@router.get("/api/v1/dialogue/template")
def dialogue_template() -> dict[str, Any]:
    """返回对话模板 schema（字段 / 选项 / 条件显隐规则 / 维度真值）。"""
    return A.default_dialogue_template().render_template_json()


@router.post("/api/v1/dialogue/submit")
def dialogue_submit(body: DialogueSubmitRequest) -> JSONResponse:
    """对话提交：模板填写值 或 自由文本 → 校验 → 复用异步生成提交。

    与 `/api/v1/generate` 共用 `submit_generate_core`，因此同样满足
    **生成-only 红线 + 幂等 + 可轮询**。凭证在 `redacted()` 视图中掩码，不回显。
    """
    agent = A.DialogueAgent()
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


@router.post("/api/v1/execute")
def submit_execute(req: ExecuteRequest) -> JSONResponse:
    """异步提交「执行验证」任务（G-2 服务化执行侧）。

    - 给了 `project_id`：对**已有用例**执行验证（不重新生成），等价于 CLI `testgen execute`；
    - 给了代码/地址来源：先生成用例再执行验证（等价于 CLI `pipeline --execute`）。
    结果经 `GET /api/v1/tasks/{task_id}` 轮询，复用同一张任务表（`kind=execute`）。

    ⚠️ 诚实边界：本端点**只做只读验证**（默认不执行写操作、不真实点击 UI）。要主动污染被测环境，
    调用方须显式传 `allow_write=true` / `ui_click=true` 并确认环境可写。写操作失败不会伪造通过。
    """
    if req.project_id:
        opts = A._build_exec_opts_from_request(req)
        idem = A._idem_key_for_execute(req)
        existing = A.task_store.find_by_idempotency(idem)
        if existing and existing.state != A.TaskState.CANCELLED.value:
            return A._task_accepted(existing, status_code=202, reused=True)
        task = A.task_store.create(
            kind="execute",
            idempotency_key=idem,
            request=json.dumps(req.model_dump(), ensure_ascii=False),
        )
        A.executor.submit(
            task.task_id, lambda: A._run_execution_job(task.task_id, req.project_id, opts)
        )
        return A._task_accepted(task, status_code=202)

    # 生成 + 执行：复用 PipelineRequest 同义字段，避免两套校验逻辑分叉
    pr = PipelineRequest(**req.model_dump(exclude={"project_id"}))
    if req.scopes:
        invalid = set(req.scopes) - set(ALL_TP_TYPES)
        if invalid:
            raise ValidationError(f"非法行为维度：{sorted(invalid)}，允许 {ALL_TP_TYPES}")
    opts = A.build_opts_from_request(pr)
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
    existing = A.task_store.find_by_idempotency(idem)
    if existing and existing.state != A.TaskState.CANCELLED.value:
        return A._task_accepted(existing, status_code=202, reused=True)
    task = A.task_store.create(
        kind="execute", idempotency_key=idem, request=A._redacted_request(pr)
    )
    A.executor.submit(
        task.task_id, lambda: A._run_generation_job(task.task_id, opts, force_execute=True)
    )
    return A._task_accepted(task, status_code=202)
