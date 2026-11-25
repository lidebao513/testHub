"""配置管理路由：仓库配置 / LLM 厂商 CRUD + 连接测试。

从 service/app.py 拆出（#439 上帝文件拆分批次 1）。本组无测试 patch 依赖，
纯透传 store / service_repos / service_llm。鉴权由 app.py 在 include_router
时统一注入 ``dependencies=[Depends(require_auth)]``，故本模块顶层不访问
app 模块属性，避免循环导入。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from core import store
from core.errors import ContractViolation, NotFoundError, ValidationError
from service import llm_providers as service_llm
from service import repos as service_repos


router: APIRouter = APIRouter()


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


class ImportReposRequest(BaseModel):
    """共享 JSON 批量导入仓库配置。"""

    items: list[dict[str, Any]] = Field(default_factory=list)
    json_path: str = Field("", description="服务端 JSON 文件路径（items 为空时读取）")


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


@router.get("/api/v1/repos")
def list_repos() -> dict[str, Any]:
    """列出已配置的代码仓库（含最近一次连接测试结果）。"""
    items = store.list_repo_configs()
    for it in items:
        it["kind"] = "git" if service_repos.is_git_url(it["url"]) else "local"
    return {"ok": True, "repos": items}


@router.get("/api/v1/repos/export")
def export_repos() -> dict[str, Any]:
    """导出全部仓库配置（共享/备份），仅含地址/分支/备注。"""
    return {"ok": True, "repo_configs": store.export_repo_configs()}


@router.post("/api/v1/repos/import")
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


@router.post("/api/v1/repos")
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


@router.put("/api/v1/repos/{rid}")
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


@router.delete("/api/v1/repos/{rid}")
def delete_repo(rid: int) -> dict[str, Any]:
    """删除仓库配置。"""
    removed = store.delete_repo_config(rid)
    if not removed:
        raise NotFoundError(f"仓库配置不存在: {rid}")
    return {"ok": True}


@router.post("/api/v1/repos/test")
def test_repo_address(req: RepoProbeRequest) -> dict[str, Any]:
    """即时连接测试（地址可未保存）：git 走只读 ls-remote，本地查目录。"""
    return service_repos.probe_repo(req.url, req.ref)


@router.post("/api/v1/repos/{rid}/test")
def test_repo_config(rid: int) -> dict[str, Any]:
    """测试已保存仓库的连通性，并回写最近一次结果。"""
    cfg = store.get_repo_config(rid)
    if cfg is None:
        raise NotFoundError(f"仓库配置不存在: {rid}")
    result = service_repos.probe_repo(cfg["url"], cfg["ref"])
    store.record_repo_test(rid, result["ok"], result["message"])
    return {"ok": True, "id": rid, **result}


# LLM 厂商管理（多 provider 独立 key；控制台「LLM 配置」菜单）：CRUD + 连接测试
@router.get("/api/v1/llm/providers")
def list_llm_providers() -> dict[str, Any]:
    """列出已配置的 LLM 厂商（api_key 已脱敏）。"""
    return {"ok": True, "providers": store.list_llm_providers()}


@router.post("/api/v1/llm/providers")
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


@router.put("/api/v1/llm/providers/{pid}")
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


@router.delete("/api/v1/llm/providers/{pid}")
def delete_llm_provider(pid: int) -> dict[str, Any]:
    """删除厂商配置。"""
    removed = store.delete_llm_provider(pid)
    if not removed:
        raise NotFoundError(f"厂商配置不存在: {pid}")
    return {"ok": True}


@router.post("/api/v1/llm/providers/test")
def test_llm_address(req: LLMProbeRequest) -> dict[str, Any]:
    """即时连接测试（配置可未保存）。"""
    return service_llm.probe_llm(req.base_url, req.api_key, req.model)


@router.post("/api/v1/llm/providers/{pid}/test")
def test_llm_config(pid: int) -> dict[str, Any]:
    """测试已保存厂商的连通性，并回写最近一次结果。"""
    secret = store.get_llm_provider_secret(pid)
    if secret is None:
        raise NotFoundError(f"厂商配置不存在: {pid}")
    first_model = secret["models"].split(",")[0].strip() if secret["models"] else ""
    result = service_llm.probe_llm(secret["base_url"], secret["api_key"], first_model)
    store.record_llm_provider_test(pid, result["ok"], result["message"])
    return {"ok": True, "id": pid, **result}
