"""语义比对 / 取码 / 代码分析 / 阶段快照路由。

从 service/app.py 拆出（#439 上帝文件拆分批次 4）。被 patch 的
compare_one / compare_batch / ComparatorOptions / _stage_store 保留在 app.py 门面，
本模块 handler 通过 ``A.xxx`` 运行时引用。鉴权由 app.py include_router 统一注入。
"""

from __future__ import annotations

import json
import os
from functools import partial
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from core.config import get_settings
from engine import pipeline
from engine.comparator import CompareInput
from service import app as A


router: APIRouter = APIRouter()

settings = get_settings()


class CompareRequest(BaseModel):
    items: list[dict[str, Any]] = Field(default_factory=list)
    case: dict[str, Any] = Field(default_factory=dict)
    actual: dict[str, Any] = Field(default_factory=dict)
    context: dict[str, Any] = Field(default_factory=dict)


class AnalyzeRequest(BaseModel):
    local_path: str
    project_name: str = ""
    include_business: bool = True
    extract_pages: bool = True
    async_mode: bool = Field(
        False,
        description="true=提交后台任务返回 202+poll_url；默认同步返回，老调用方零影响",
    )


class PullRequest(BaseModel):
    """取码请求（F1）：把仓库准备到本地并（可选）算出变更集。"""

    repo_url: str = Field("", description="仓库地址（含凭证时结果与日志一律掩码）")
    local_path: str = Field("", description="本地目标目录；缺省落到工作区根/仓库名（受管目录）")
    base: str = Field("", description="基线 ref（与 target 一起算变更集）")
    target: str = Field("", description="目标 ref；WORKTREE 表示与当前工作区比较")
    deepen: int = Field(0, ge=0, description="浅克隆加深的提交数（0=不加深）")


class StageAnalyzeRequest(BaseModel):
    """阶段快照请求：对本地仓库当前状态做一次分析并登记为阶段快照。"""

    local_path: str = Field(..., description="本地仓库路径（也是 StageStore 的 repo 键）")
    branch: str = Field("master", description="分支名（快照归属）")
    commit: str = Field("", description="显式 commit（缺省取当前 HEAD）")
    include_business: bool = True
    extract_pages: bool = True
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


@router.post("/api/v1/compare")
def compare(req: CompareRequest) -> dict[str, Any]:
    """语义比对（P0-2）：给定预期 + 实际输出 LLM 增强比对结论。"""
    opts = A.ComparatorOptions.from_settings()
    if req.items:
        inputs = [
            CompareInput(
                case=it.get("case") or {},
                actual=it.get("actual") or {},
                context=it.get("context") or {},
            )
            for it in req.items
        ]
        verdicts = A.compare_batch(inputs, opts)
        return {"verdicts": [v.to_dict() for v in verdicts], "count": len(verdicts)}
    inp = CompareInput(case=req.case, actual=req.actual, context=req.context)
    verdict = A.compare_one(inp, opts)
    return {"verdict": verdict.to_dict()}


@router.post("/api/v1/pull")
def pull_code(req: PullRequest) -> dict[str, Any]:
    """取码（F1）：克隆 / 更新 / 加深 / 算变更集。失败以 success=false 如实返回。"""
    from engine import pull as pull_engine

    url = req.repo_url or (os.environ.get("REPO_URL") or "").strip()
    local_path = req.local_path or str(settings.workspace_root / pipeline.repo_dir_name(url))
    result = pull_engine.pull(url, local_path, base=req.base, target=req.target, deepen=req.deepen)
    return {"pull": result.to_dict()}


@router.post("/api/v1/analyze")
def analyze(req: AnalyzeRequest) -> Any:
    """只做扫描 + 功能点提取，不生成测试点与用例（与 pipeline 同一套语义合并口径）。

    Batch 4 D-2：`async_mode=true` 时提交后台任务，避免大仓扫描长时间占用连接。
    """
    if req.async_mode:
        return A.submit_aux_task(
            "analyze",
            json.dumps(req.model_dump(), ensure_ascii=False),
            partial(analyze_sync, req),
        )
    return analyze_sync(req)


def analyze_sync(req: AnalyzeRequest) -> dict[str, Any]:
    """analyze 的同步实现（供默认分支与后台作业共用，返回值即轮询 result）。"""
    from engine import fp_extract, fp_merge, scan

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


@router.post("/api/v1/stage-analyze")
def stage_analyze(req: StageAnalyzeRequest) -> dict[str, Any]:
    """扫描 + 功能点提取 + 登记阶段快照（C3）。"""
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
    store_stages = A._stage_store(req.project_id, req.repo_key)
    snap = create_snapshot(store_stages, repo, req.branch, commit=req.commit or None)
    analysis = {
        "stage_id": snap.stage_id,
        "commit": snap.commit,
        "branch": snap.branch,
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


@router.post("/api/v1/stage-compare")
def stage_compare(req: StageCompareRequest) -> dict[str, Any]:
    """两阶段产物对比（C3）：新增/失效/保留的功能点与问题 + 可选 diff 统计。"""
    from engine.stage_compare import compare_stages

    repo = str(Path(req.repo).resolve())
    report = compare_stages(
        A._stage_store(req.project_id, req.repo_key),
        repo,
        req.from_ref,
        req.to_ref,
        repo_path=req.repo_path or None,
    )
    return {"ok": True, "report": report.to_dict()}


@router.get("/api/v1/stages")
def list_stages(
    repo: str,
    branch: str = "",
    project_id: int | None = None,
    repo_key: str = "",
) -> dict[str, Any]:
    """列某仓库（可选某分支）的全部阶段快照（前端 from/to 下拉数据源）。"""
    store_stages = A._stage_store(project_id, repo_key)
    repo = str(Path(repo).resolve())
    snaps = store_stages.list_by_branch(repo, branch) if branch else store_stages.list_all(repo)
    return {"ok": True, "stages": [s.to_dict() for s in snaps]}
