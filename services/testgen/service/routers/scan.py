"""扫描联动路由：分支/提交、中文功能描述（describe）、快速扫描、多仓 refs、安全裁判。

从 service/app.py 拆出（#439 上帝文件拆分批次 3）。被测试 patch 的函数
（list_repo_branches / list_repo_commits / _resolve_code_source / _dispatch_scan）
定义保留在 app.py 门面，本模块 handler 通过 ``A.xxx`` 运行时引用，保证
``patch("service.app.X")`` 继续生效。鉴权由 app.py include_router 统一注入。
"""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import partial
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from core import store
from core.config import get_settings
from core.describe_namespace import describe_namespace
from core.errors import ValidationError
from core.log import get_logger, log_extra
from engine.codeup_client import CodeupError
from engine.remote_scan import (
    CHANGE_ADDED,
    CHANGE_MODIFIED,
    CHANGE_UNCHANGED,
)
from engine.verdict import run_five_dimension_verdict
from service import app as A


router: APIRouter = APIRouter()


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

log = get_logger(__name__)
settings = get_settings()

# M18：/describe 分批与并发参数（单组 5 条，组间最多 4 路并发）
_DESCRIBE_CHUNK_SIZE = 5
_DESCRIBE_MAX_WORKERS = 4


class RefsRequest(BaseModel):
    """分支/提交下拉所用：仓库 + 只读凭证 + （commits 用）ref。"""

    repo_url: str = Field("", description="仓库地址（git@/ssh/https/*.git）")
    local_path: str = Field("", description="本地 git 目录（与 repo_url 二选一）")
    code_source: dict[str, Any] = Field(
        default_factory=dict, description="只读凭证 {credential_type, org_id, access_key}"
    )
    ref: str = Field("", description="分支/标签/commit（commits 端点必填）")


class DescribeItem(BaseModel):
    """单个待描述项：文件（也可为函数/类）。

    ``namespace`` 可逐条覆盖请求级命名空间 —— 多仓同批提交时按仓隔离缓存。
    """

    path: str = ""
    name: str = ""
    kind: str = "file"
    functions: list[str] = Field(default_factory=list)
    classes: list[str] = Field(default_factory=list)
    namespace: str = Field("", description="条目自带命名空间（多仓隔离，优先于请求级）")


class DescribeRequest(BaseModel):
    """把扫描出的文件节点批量翻译成中文功能描述。

    缓存命名空间由 ``core.describe_namespace`` 统一计算：显式 namespace > runtime_url
    > (repo_key, project_id) > repo_url > local_path；全部为空时不读也不写缓存。
    ``repo_url`` 保留兼容旧调用，且沿用原值以命中历史缓存。
    """

    items: list[DescribeItem] = Field(default_factory=list)
    namespace: str = ""
    runtime_url: str = ""
    repo_key: str = ""
    project_id: int | None = None
    repo_url: str = ""
    local_path: str = ""
    async_mode: bool = Field(
        False,
        description="true=提交后台任务返回 202+poll_url；默认同步返回，老调用方零影响",
    )


class DescribeCacheClearRequest(BaseModel):
    """清空描述缓存：带身份字段时只清该命名空间；全空时兼容旧行为清全部。"""

    namespace: str = ""
    runtime_url: str = ""
    repo_key: str = ""
    project_id: int | None = None
    repo_url: str = ""
    local_path: str = ""


class ScanRequest(BaseModel):
    """快速扫描（只读）：列模块树 + 标注变更状态，供前端勾选范围。

    `repo_key` / `project_id` / `namespace` 是 B-5 多仓归属（P1）：
    后端用它们算出 describe 缓存命名空间并随返回项回传，前端只透传不自己拼。
    旧调用不传这三个字段时行为完全不变。
    """

    repo_url: str = Field("", description="仓库地址（git 模式）")
    code_source: dict[str, Any] = Field(default_factory=dict, description="只读凭证")
    ref: str = Field("", description="目标 ref（分支/标签/commit）")
    base_ref: str = Field("", description="基线 ref（对比出 新增/修改）")
    local_path: str = Field("", description="本地目录（local 模式）")
    repo_key: str = Field("", description="多仓归属键（repo_bindings.repo_key）")
    project_id: int | None = Field(None, description="项目 id，与 repo_key 组成 multi:<pid>:<key>")
    namespace: str = Field("", description="显式命名空间覆盖（最高优先级，一般不由前端传）")


class RepoRefsRequest(BaseModel):
    """多仓只读批量请求（v1.7）：一次列出项目下所有仓的分支/提交/扫描树。"""

    project_id: int = Field(..., description="项目 id（从 repo_bindings 读仓库清单）")
    kind: str = Field("branches", description="branches / commits / scan")
    ref: str = Field("", description="commits/scan 用；各仓可带自己的（binding.ref 优先）")
    base_ref: str = Field("", description="scan 用：基线 ref")
    code_source: dict[str, Any] = Field(
        default_factory=dict, description="只读凭证（按各仓 repo_url 带出，为空则从仓库配置取）"
    )


# ---------------------------------------------------------------------------
# describe 纯 helper（无 patch 依赖；chat_with_fallback 通过门面 A 引用）
# ---------------------------------------------------------------------------
def _describe_item_key(it: Any) -> str:
    """取缓存键：优先 path，回退 name。"""
    if isinstance(it, DescribeItem):
        return (it.path or it.name or "").strip()
    return str((it.get("path") or it.get("name") or "").strip())


def _describe_symbols_sig(it: Any) -> str:
    """文件「结构指纹」：函数/类排序后 sha1。sig 一致 = 内容/结构未变 → 可复用历史描述。"""
    if isinstance(it, DescribeItem):
        funcs = sorted(it.functions or [])
        classes = sorted(it.classes or [])
    else:
        funcs = sorted(it.get("functions") or [])
        classes = sorted(it.get("classes") or [])
    raw = json.dumps({"f": funcs, "c": classes}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()  # nosec B324 - 仅作缓存内容指纹，非安全用途


def _extract_json_block(content: str) -> Any:
    """从 LLM 输出中稳健抽取 JSON（容忍 ```json 围栏与前后闲文）。"""
    text = (content or "").strip()
    fenced = __import__("re").search(r"```(?:json)?\s*(.*?)\s*```", text, __import__("re").S)
    candidate = fenced.group(1) if fenced else text
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(candidate[start : end + 1])
        except json.JSONDecodeError:
            return None
    return None


def _describe_cache_lookup(
    namespace: str | None, items: list[Any]
) -> tuple[dict[str, str], dict[str, bool], list[Any]]:
    """缓存命中判断：返回 (descriptions, cached_map, need)；None = 禁用缓存。"""
    descriptions: dict[str, str] = {}
    cached_map: dict[str, bool] = {}
    sigs = {_describe_item_key(it): _describe_symbols_sig(it) for it in items}
    if namespace is None:
        for it in items:
            cached_map[_describe_item_key(it)] = False
        return descriptions, cached_map, list(items)
    cached_rows = store.get_file_descriptions(namespace, list(sigs.keys()))
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


def _describe_persist(namespace: str | None, need: list[Any], new_desc: dict[str, str]) -> None:
    """把 LLM 新描述写回缓存（串行写库；namespace=None 时不写，失败降级为不缓存）。"""
    if namespace is None:
        return
    try:
        with A._DESCRIBE_DB_LOCK:
            for it in need:
                key = _describe_item_key(it)
                if key in new_desc:
                    store.upsert_file_description(
                        namespace,
                        key,
                        _describe_symbols_sig(it),
                        new_desc[key],
                        A.settings.llm_model or "",
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


def _describe_run_chunks(namespace: str | None, chunks: list[list[Any]]) -> dict[str, str]:
    """并发跑各分组 LLM（M18），带总预算兜底；返回已拿到的描述。"""
    if not chunks:
        return {}
    budget = float(getattr(A.settings, "describe_budget_seconds", 90) or 90)
    pool = ThreadPoolExecutor(max_workers=min(_DESCRIBE_MAX_WORKERS, len(chunks)))
    merged: dict[str, str] = {}
    try:
        futures = {pool.submit(_describe_one_chunk, namespace, c): c for c in chunks}
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
        pool.shutdown(wait=False, cancel_futures=True)
    return merged


def _describe_one_chunk(namespace: str | None, chunk: list[Any]) -> dict[str, str]:
    """单组（≤5 条）调一次 LLM 并写缓存；失败返回空字典（该组回退展示原名）。"""
    prompt = _describe_build_prompt(chunk)
    try:
        resp = A.chat_with_fallback(
            channel="describe",
            api_key=A.settings.llm_api_key,
            base_url=A.settings.llm_base_url,
            model=A.settings.llm_model,
            timeout=float(A.settings.llm_timeout or 60),
            model_chain=A.settings.llm_model_chain or None,
            messages=[{"role": "user", "content": prompt}],
        )
        content = resp.choices[0].message.content or ""
        chunk_desc = _describe_parse(_extract_json_block(content))
    except Exception as exc:  # noqa: BLE001 - 诚实降级：该组 LLM 失败不阻塞其他组
        log.warning("describe LLM 失败，该组回退原名", extra=log_extra(err=type(exc).__name__))
        return {}
    if chunk_desc:
        _describe_persist(namespace, chunk, chunk_desc)
    return chunk_desc


def _describe_item_namespace(it: Any, default: str | None) -> str | None:
    """条目自带 namespace 优先，否则沿用请求级命名空间。"""
    own = it.namespace if isinstance(it, DescribeItem) else (it.get("namespace") or "")
    own = (own or "").strip()
    return describe_namespace(namespace=own) if own else default


@router.post("/api/v1/describe")
def describe_features(req: DescribeRequest) -> Any:
    """把扫描出的文件节点批量翻译成中文功能描述（带命名空间隔离的缓存）。

    Batch 4 A-2：`async_mode=true` 时提交后台任务（202 + poll_url），避免大仓 LLM
    调用长时间占用 HTTP 连接；默认同步分支与老契约完全一致。
    """
    if req.async_mode:
        return A.submit_aux_task(
            "describe",
            json.dumps(req.model_dump(), ensure_ascii=False),
            partial(describe_features_sync, req),
        )
    return describe_features_sync(req)


def describe_features_sync(req: DescribeRequest) -> dict[str, Any]:
    """describe 的同步实现（供默认分支与后台作业共用，返回值即轮询 result）。"""
    items = req.items or []
    if not items:
        return {"ok": True, "llm": False, "descriptions": {}, "cached": {}, "error": "empty"}
    llm_ready = bool(A.settings.llm_api_key and A.settings.llm_base_url and A.settings.llm_model)
    default_ns = describe_namespace(
        namespace=req.namespace,
        runtime_url=req.runtime_url,
        repo_key=req.repo_key,
        project_id=req.project_id,
        repo_url=req.repo_url,
        local_path=req.local_path,
    )
    # 同批可含多仓条目：按命名空间分组读写，缓存严格隔离。
    # 响应仍是 {path: 描述} 的旧契约，跨仓同名 path 时以后写条目为准（前端按仓分别提交）。
    groups: dict[str | None, list[Any]] = {}
    for it in items:
        groups.setdefault(_describe_item_namespace(it, default_ns), []).append(it)

    descriptions: dict[str, str] = {}
    cached_map: dict[str, bool] = {}
    need_groups: dict[str | None, list[Any]] = {}
    for ns, group in groups.items():
        group_desc, group_cached, group_need = _describe_cache_lookup(ns, group)
        descriptions.update(group_desc)
        cached_map.update(group_cached)
        if group_need:
            need_groups[ns] = group_need

    if not need_groups:
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
            "unresolved": sum(len(n) for n in need_groups.values()),
            "error": "未配置 LLM（llm_api_key / llm_base_url / llm_model 不全）——未命中缓存的文件将回退展示类/函数名",
        }
    unresolved = 0
    for ns, need in need_groups.items():
        chunks = [
            need[i : i + _DESCRIBE_CHUNK_SIZE] for i in range(0, len(need), _DESCRIBE_CHUNK_SIZE)
        ]
        created = _describe_run_chunks(ns, chunks)
        unresolved += _describe_merge(need, created, descriptions, cached_map)
    return {
        "ok": True,
        "llm": True,
        "descriptions": descriptions,
        "cached": cached_map,
        "cache_hits": sum(1 for v in cached_map.values() if v),
        "unresolved": unresolved,
    }


@router.post("/api/v1/describe/cache/clear")
def clear_describe_cache(req: DescribeCacheClearRequest) -> dict[str, Any]:
    """清空描述缓存：带身份字段只清该命名空间；全空保留旧行为清全部。"""
    ns = describe_namespace(
        namespace=req.namespace,
        runtime_url=req.runtime_url,
        repo_key=req.repo_key,
        project_id=req.project_id,
        repo_url=req.repo_url,
        local_path=req.local_path,
    )
    if ns is None:
        log.warning("describe 缓存清理未带身份字段，按兼容语义清空全部（含运行时模块缓存）")
        deleted = store.clear_file_descriptions()
        return {"ok": True, "deleted": deleted, "scope": "all"}
    deleted = store.clear_file_descriptions(ns)
    return {"ok": True, "deleted": deleted, "scope": ns}


# ---------------------------------------------------------------------------
# 分支 / 提交 / 快速扫描 / 多仓 refs / 安全裁判
# 被 patch 的 list_repo_branches / list_repo_commits / _dispatch_scan 走门面 A
# ---------------------------------------------------------------------------
def _attach_repo_ownership(
    items: list[dict[str, Any]],
    *,
    namespace: str | None,
    repo_key: str = "",
    project_id: int | None = None,
) -> list[dict[str, Any]]:
    """给 scan items 附加仓库归属（P1），返回浅拷贝，不改动扫描引擎原对象。

    `namespace` 由 `describe_namespace()` 统一计算；`None` 时**不写该键**——JSON
    null 会被前端读成有效值，且空命名空间在清缓存接口带有「清全部」语义。
    """
    attached: list[dict[str, Any]] = []
    for item in items:
        row = dict(item)
        if namespace is not None:
            row["namespace"] = namespace
        if repo_key:
            row["repo_key"] = repo_key
        if project_id is not None:
            row["project_id"] = project_id
        attached.append(row)
    return attached


@router.post("/api/v1/branches")
def list_repo_branches_route(req: RefsRequest) -> dict[str, Any]:
    return A.list_repo_branches(req)


@router.post("/api/v1/commits")
def list_repo_commits_route(req: RefsRequest) -> dict[str, Any]:
    return A.list_repo_commits(req)


@router.post("/api/v1/repos/refs")
def list_repo_refs(req: RepoRefsRequest) -> dict[str, Any]:
    """多仓只读批量端点：按 repo_bindings 逐仓列分支/提交/扫描树，按 repo_key 分组。

    单仓失败不阻断其他仓（错误记在该仓条目的 `error` 字段）；凭证按各仓 repo_url
    从 code_source / 仓库配置带出。旧单仓端点（`/branches`、``/commits`、`/scan`）不变。
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
                result = A.list_repo_branches(refs_req)
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
                result = A.list_repo_commits(refs_req)
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
                    repo_key=key,
                    project_id=req.project_id,
                    namespace="",  # mypy 无 pydantic 插件：Field 默认值视为必填，显式传空
                )
                items, source, meta = A._dispatch_scan(scan_req)
                # P1：逐条带 multi:<pid>:<repo_key>，与外层 repos[key] 严格一致。
                item_ns = describe_namespace(
                    repo_key=key,
                    project_id=req.project_id,
                    repo_url=b["repo_url"] or "",
                    local_path=b["local_path"] or "",
                )
                attached = _attach_repo_ownership(
                    items, namespace=item_ns, repo_key=key, project_id=req.project_id
                )
                entry.update(items=attached, source=source, meta=meta, namespace=item_ns)
        except (CodeupError, ValidationError) as exc:
            entry["error"] = str(exc)[:200]
        except Exception as exc:  # noqa: BLE001 - 单仓失败不阻断其他仓
            log.warning(
                "多仓只读端点单仓失败", extra=log_extra(repo_key=key, err=type(exc).__name__)
            )
            entry["error"] = str(exc)[:200]
        repos[key] = entry
    return {"ok": True, "project_id": req.project_id, "kind": req.kind, "repos": repos}


@router.post("/api/v1/scan")
def scan_code(req: ScanRequest) -> dict[str, Any]:
    """快速扫描（只读）：列模块树 + 标注变更状态，供前端勾选范围。不克隆/不生成。

    P1：返回值与每个 item 都带后端统一计算的 `namespace`，前端 describe 只透传，
    不再因多仓无归属而禁用缓存。
    """
    try:
        items, source, meta = A._dispatch_scan(req)
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
    namespace = describe_namespace(
        namespace=req.namespace,
        repo_key=req.repo_key,
        project_id=req.project_id,
        repo_url=req.repo_url,
        local_path=req.local_path,
    )
    items = _attach_repo_ownership(
        items,
        namespace=namespace,
        repo_key=req.repo_key,
        project_id=req.project_id,
    )
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
        "namespace": namespace,
        "repo_key": req.repo_key,
        "project_id": req.project_id,
        "summary": summary,
        "count": len(items),
        "total": int(meta.get("total_matched", len(items))),
        "truncated": bool(meta.get("truncated", False)),
    }


@router.post("/api/v1/verdict")
def verdict(req: VerdictRequest) -> dict[str, Any]:
    """安全语义裁判（M2）：复用 engine 既有五维判定，对 security 用例的观测事实做安全判定。"""
    facts = req.model_dump()
    if facts.get("auth_mode") is None:
        facts["auth_mode"] = ""
    return run_five_dimension_verdict(facts)
