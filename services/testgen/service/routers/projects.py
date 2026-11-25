"""项目数据与报告路由：projects / 模块 / 测试点 / 用例 / runs / 报告 / 覆盖率 / trend / flaky / 导入。

从 service/app.py 拆出（#439 上帝文件拆分批次 2）。本组无测试 patch 依赖，
直接引用 core/engine 模块；鉴权由 app.py include_router 时统一注入。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field

from core import store, url_modules, url_projects
from core.config import get_settings
from core.enums import REPORT_FORMATS, ReportFormat
from core.errors import NotFoundError, ValidationError
from engine import analytics, pipeline
from engine import report as report_engine
from output.report_writer import (
    ReportExportError,
    export_report,
    render_html,
    render_markdown,
)
from workspace.manager import WorkspaceManager


router: APIRouter = APIRouter()


@router.get("/api/v1/projects")
def list_projects() -> dict[str, Any]:
    return {"projects": store.list_projects()}


class EnsureProjectRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    base_url: str = Field(..., min_length=1, max_length=2048)


@router.post("/api/v1/projects/ensure")
def ensure_project(req: EnsureProjectRequest) -> dict[str, Any]:
    """地址优先入口专用建项：按归一 URL（再按同名）查重，命中不覆盖已有字段。

    与 ``stage_register`` 的 upsert 语义隔离，旧生成链路行为不变。
    """
    return url_projects.ensure_project(name=req.name.strip(), base_url=req.base_url)


@router.get("/api/v1/projects/{pid}/modules")
def project_modules(pid: int) -> dict[str, Any]:
    """地址优先入口：读取项目模块（既有功能点 + 已扫描页面，不新建模块表）。"""
    return url_modules.read_modules(pid)


class ScanModulesRequest(BaseModel):
    max_pages: int = Field(8, ge=1, le=60)
    timeout: int = Field(8, ge=1, le=60)
    login_user: str = ""
    login_password: str = ""
    login_otp: str = ""
    login_url: str = ""
    routes: list[str] = Field(default_factory=list)
    discover_by_menu: bool = True


@router.post("/api/v1/projects/{pid}/modules/scan")
def scan_project_modules(pid: int, req: ScanModulesRequest) -> dict[str, Any]:
    """地址优先入口：只做浏览器发现并持久化模块，不生成/执行用例。"""
    return url_modules.scan_modules(
        pid,
        max_pages=req.max_pages,
        timeout=req.timeout,
        login_user=req.login_user,
        login_password=req.login_password,
        login_otp=req.login_otp,
        login_url=req.login_url,
        routes=req.routes,
        discover_by_menu=req.discover_by_menu,
    )


@router.get("/api/v1/projects/{pid}/repos")
def get_project_repos(pid: int) -> dict[str, Any]:
    """列出项目仓库绑定清单（多仓前端 setMultiRepos 注入数据源）。

    每项转成 RepoSpec 可消费结构（repo_key/local_path/repo_url/base/target/ref）；
    `multi` 为是否多仓（binding > 1），空项目返回空 repos，前端据此回落单仓模式。
    只读、鉴权沿用 require_auth；旧单仓 project 无 binding 时零回归。
    """
    with store.connect() as conn:
        bindings = store.list_repo_bindings(pid, conn)
    repos = [
        {
            "repo_key": str(b["repo_key"]),
            "local_path": b["local_path"] or "",
            "repo_url": b["repo_url"] or "",
            "base": b["base"] or None,
            "target": b["target"] or None,
            "ref": b["ref"] or "",
        }
        for b in bindings
    ]
    return {"project_id": pid, "repos": repos, "multi": len(repos) > 1}


@router.get("/api/v1/projects/{pid}/test-points")
def get_test_points(pid: int) -> dict[str, Any]:
    return {"project_id": pid, "test_points": store.list_test_points(pid)}


@router.get("/api/v1/projects/{pid}/cases")
def get_cases(pid: int, include_obsolete: bool = False) -> dict[str, Any]:
    return {
        "project_id": pid,
        "cases": store.list_cases(pid, include_obsolete=include_obsolete),
    }


@router.get("/api/v1/projects/{pid}/traceability")
def get_traceability(pid: int) -> dict[str, Any]:
    return {"project_id": pid, "traceability": store.traceability(pid)}


@router.get("/api/v1/projects/{pid}/runs")
def get_runs(pid: int, limit: int = 50) -> dict[str, Any]:
    """执行批次列表（F12 留痕）：最新在前，含状态分布与批次终态。"""
    batches = store.list_run_batches(pid, limit=limit)
    return {"project_id": pid, "batches": batches, "latest": batches[0] if batches else None}


@router.get("/api/v1/projects/{pid}/runs/{batch_id}")
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


@router.get("/api/v1/projects/{pid}/report")
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


@router.get("/api/v1/projects/{pid}/coverage")
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


@router.get("/api/v1/workspaces")
def list_workspaces() -> dict[str, Any]:
    return {"workspaces": [w.to_dict() for w in WorkspaceManager().list_all()]}


# 质量分析与数据导入（迁移自 test-accel：trend/flaky/change_logs/import 等）
@router.get("/api/v1/projects/{pid}/reports/trend")
def project_trend(pid: int, only_finished: bool = True, limit: int = 50) -> dict[str, Any]:
    """多批次通过率趋势（迁移自 test-accel analytics.trend）。"""
    return analytics.trend(pid, only_finished=only_finished, limit=limit)


@router.get("/api/v1/projects/{pid}/reports/flaky")
def project_flaky(pid: int, min_batches: int = 2) -> dict[str, Any]:
    """跨批次结果不一致的不稳定用例识别（迁移自 test-accel analytics.flaky）。"""
    return analytics.flaky(pid, min_batches=min_batches)


@router.get("/api/v1/projects/{pid}/change_logs")
def project_change_logs(pid: int, limit: int = 100) -> dict[str, Any]:
    """查看该项目历次增量对比/变更记录。"""
    logs = store.list_change_logs(pid, limit=limit)
    return {"ok": True, "count": len(logs), "change_logs": logs}


@router.get("/api/v1/projects/{pid}/functional-points")
def project_functional_points(pid: int, limit: int = 500) -> dict[str, Any]:
    """功能点独立查询（此前仅内嵌于 /analyze 响应）。"""
    fps = store.list_functional_points(pid)
    return {"ok": True, "count": len(fps), "functional_points": fps[: max(1, int(limit))]}


@router.get("/api/v1/projects/{pid}/filter-options")
def project_filter_options(pid: int) -> dict[str, Any]:
    """可用筛选维度与取值分布（cases + test_points），供前端筛选器。"""
    opts = store.filter_options(pid)
    return {"ok": True, "project_id": pid, **opts}


class ImportTestPointsRequest(BaseModel):
    """外部测试点导入：直接给 items 或给服务端 json_path（test-accel 阶段2 产物兼容）。"""

    items: list[dict[str, Any]] = Field(default_factory=list, description="测试点列表")
    json_path: str = Field("", description="服务端 JSON 文件路径（items 为空时读取）")
    replace: bool = Field(True, description="true=清空后全量导入；false=按 tp_id 覆盖更新")


@router.post("/api/v1/projects/{pid}/import-test-points")
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


@router.post("/api/v1/projects/{pid}/import-test-points/upload")
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


@router.post("/api/v1/projects/{pid}/rerun-flaky")
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
