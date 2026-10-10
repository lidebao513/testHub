"""testhub → testgen sidecar HTTP 客户端（沿用项目对外调用惯例：requests + headers + timeout）。

范本：apps/assistant/views.py 的 Dify 调用风格。

铁律（M1 红线守卫）：本客户端**绝不**向 testgen 发送 ``execute=True`` / ``exec_url``。
- ``generate()`` 默认 ``execute=False``，且显式传 ``execute=True`` 会**就地拒绝**（双保险；
  testgen 服务端也会以 422 拒绝，但调用方不应发出该请求）。
- 所有执行统一交给 testhub 的 ``ui_automation`` / ``api_testing``，testgen 仅生成。

配置读取（与项目惯例一致，且不强依赖 Django）：
  - 优先读 Django settings（若运行在 Django 进程内）；
  - 否则回退 os.getenv；
  - 默认值指向本机 sidecar http://127.0.0.1:8100。

代理处理：本机存在透明代理（localhost 会被劫持），故复用单个 ``trust_env=False`` 的
Session，避免 localhost/内网直连被代理拦截；远程部署（非 127.0.0.1）直连同样适用。
"""

from __future__ import annotations

import concurrent.futures
import os
from typing import Any

import requests

# 旁路透明代理：确保 127.0.0.1/localhost 直连（与 AGENTS 环境一致）
os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
os.environ.setdefault("no_proxy", "127.0.0.1,localhost")

_session = requests.Session()
_session.trust_env = False


def _settings_value(name: str, default: str) -> str:
    try:
        from django.conf import settings

        val = getattr(settings, name, None)
        if val not in (None, ""):
            return str(val)
    except Exception:  # noqa: BLE001, S110 - Django 不可用时静默回退
        pass
    return os.getenv(name, default)


def base_url() -> str:
    return _settings_value("TESTGEN_BASE_URL", "http://127.0.0.1:8100").rstrip("/")


def auth_token() -> str:
    return _settings_value("TESTGEN_AUTH_TOKEN", "")


def timeout() -> int:
    try:
        return int(_settings_value("TESTGEN_TIMEOUT", "120"))
    except ValueError:
        return 120


def _headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    tok = auth_token()
    if tok:
        headers["X-Auth-Token"] = tok
    return headers


def health() -> dict[str, Any]:
    """存活探针（无鉴权）。返回 {"status":"ok","service":"testgen-service",...}。"""
    resp = _session.get(f"{base_url()}/health", timeout=10)
    resp.raise_for_status()
    return resp.json()


def _build_generate_payload(
    *,
    local_path: str = "",
    repo_url: str = "",
    scopes: list[str] | None = None,
    changed_files: list[str] | None = None,
    mode: str = "",
) -> dict[str, Any]:
    """构造 /api/v1/generate 请求体（对齐 testgen PipelineRequest）。

    红线：绝不包含 execute / exec_url 字段。
    """
    return {
        "local_path": local_path or "",
        "repo_url": repo_url or "",
        "scopes": scopes or ["正常", "安全", "边界", "异常"],
        "changed_files": changed_files or [],
        "mode": mode or "",
        "execute": False,  # 强制生成-only
    }


def generate(
    *,
    local_path: str = "",
    repo_url: str = "",
    scopes: list[str] | None = None,
    changed_files: list[str] | None = None,
    mode: str = "",
    execute: bool = False,
) -> dict[str, Any]:
    """异步提交「生成测试用例」任务，返回 {"task_id","poll_url","state",...}。

    execute=True 会被就地拒绝（M1 红线）。
    """
    if execute:
        raise ValueError(
            "testgen 红线：本客户端禁止发送 execute=True（执行统一交给 testhub）"
        )
    payload = _build_generate_payload(
        local_path=local_path,
        repo_url=repo_url,
        scopes=scopes,
        changed_files=changed_files,
        mode=mode,
    )
    resp = _session.post(
        f"{base_url()}/api/v1/generate",
        json=payload,
        headers=_headers(),
        timeout=timeout(),
    )
    resp.raise_for_status()
    return resp.json()


def pull(
    *,
    repo_url: str,
    local_path: str = "",
    base: str = "",
    target: str = "",
    deepen: int = 0,
) -> dict[str, Any]:
    """仅取码（不生成）。凭证只在本请求内传递，testgen 侧掩码不落明文。"""
    payload = {
        "repo_url": repo_url,
        "local_path": local_path,
        "base": base,
        "target": target,
        "deepen": deepen,
    }
    resp = _session.post(
        f"{base_url()}/api/v1/pull",
        json=payload,
        headers=_headers(),
        timeout=timeout(),
    )
    resp.raise_for_status()
    return resp.json()


def poll(task_id: str) -> dict[str, Any]:
    """轮询任务进度/结果。终态(success/failed/cancelled)后含 result / error。"""
    resp = _session.get(
        f"{base_url()}/api/v1/tasks/{task_id}",
        headers=_headers(),
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def get_cases(project_id: int) -> list[dict[str, Any]]:
    """取某 testgen 项目已生成的用例（DB 行列表）。"""
    resp = _session.get(
        f"{base_url()}/api/v1/projects/{project_id}/cases",
        headers=_headers(),
        timeout=timeout(),
    )
    resp.raise_for_status()
    return resp.json().get("cases", [])


def list_branches(
    *, repo_url: str, code_source: dict[str, Any] | None, local_path: str = ""
) -> dict[str, Any]:
    """列仓库分支（供前端目标/基线 ref 下拉）。

    后端按 access_key(Codeup) → local_path(本地 git) → git url(git ls-remote) 三路分派。
    """
    payload = {
        "repo_url": repo_url or "",
        "local_path": local_path or "",
        "code_source": code_source or {},
    }
    resp = _session.post(
        f"{base_url()}/api/v1/branches",
        json=payload,
        headers=_headers(),
        timeout=timeout(),
    )
    resp.raise_for_status()
    return resp.json()


def list_commits(
    *, repo_url: str, code_source: dict[str, Any] | None, ref: str
) -> dict[str, Any]:
    """查指定 ref 的最近提交（选定分支后查看对比数据）。"""
    payload = {
        "repo_url": repo_url or "",
        "code_source": code_source or {},
        "ref": ref or "",
    }
    resp = _session.post(
        f"{base_url()}/api/v1/commits",
        json=payload,
        headers=_headers(),
        timeout=timeout(),
    )
    resp.raise_for_status()
    return resp.json()


def describe(*, items: list[dict[str, Any]]) -> dict[str, Any]:
    """把扫描出的函数/类节点批量翻译成中文功能描述。"""
    payload = {"items": items or []}
    resp = _session.post(
        f"{base_url()}/api/v1/describe",
        json=payload,
        headers=_headers(),
        timeout=timeout(),
    )
    resp.raise_for_status()
    return resp.json()


def cancel_task(task_id: str) -> dict[str, Any]:
    """取消运行中/排队中的生成任务（协作式）。"""
    resp = _session.post(
        f"{base_url()}/api/v1/tasks/{task_id}/cancel",
        json={},
        headers=_headers(),
        timeout=timeout(),
    )
    resp.raise_for_status()
    return resp.json()


def task_cases(task_id: str) -> dict[str, Any]:
    """取某任务对应项目已生成的用例（前端详情面板用）。"""
    resp = _session.get(
        f"{base_url()}/api/v1/tasks/{task_id}/cases",
        headers=_headers(),
        timeout=timeout(),
    )
    resp.raise_for_status()
    return resp.json()


def scan(
    *,
    repo_url: str = "",
    code_source: dict[str, Any] | None = None,
    ref: str = "",
    base_ref: str = "",
    local_path: str = "",
) -> dict[str, Any]:
    """快速扫描（只读）：列模块树 + 标注变更状态，供前端勾选范围。"""
    payload = {
        "repo_url": repo_url or "",
        "code_source": code_source or {},
        "ref": ref or "",
        "base_ref": base_ref or "",
        "local_path": local_path or "",
    }
    resp = _session.post(
        f"{base_url()}/api/v1/scan",
        json=payload,
        headers=_headers(),
        timeout=timeout(),
    )
    resp.raise_for_status()
    return resp.json()


def quality_trend(
    project_id: int, *, only_finished: bool = True, limit: int = 50
) -> dict[str, Any]:
    """取 testgen 项目多批次通过率趋势（reports/trend）。"""
    resp = _session.get(
        f"{base_url()}/api/v1/projects/{project_id}/reports/trend",
        params={"only_finished": str(only_finished), "limit": limit},
        headers=_headers(),
        timeout=timeout(),
    )
    resp.raise_for_status()
    return resp.json()


def quality_flaky(project_id: int, *, min_batches: int = 2) -> dict[str, Any]:
    """取 testgen 项目跨批次不稳定的 flaky 用例（reports/flaky）。"""
    resp = _session.get(
        f"{base_url()}/api/v1/projects/{project_id}/reports/flaky",
        params={"min_batches": min_batches},
        headers=_headers(),
        timeout=timeout(),
    )
    resp.raise_for_status()
    return resp.json()


def quality_filter_options(project_id: int) -> dict[str, Any]:
    """取 testgen 项目可用筛选维度与取值分布（filter-options）。"""
    resp = _session.get(
        f"{base_url()}/api/v1/projects/{project_id}/filter-options",
        headers=_headers(),
        timeout=timeout(),
    )
    resp.raise_for_status()
    return resp.json()


def quality_change_logs(project_id: int, *, limit: int = 100) -> dict[str, Any]:
    """取 testgen 项目历次增量对比/变更记录（change_logs）。"""
    resp = _session.get(
        f"{base_url()}/api/v1/projects/{project_id}/change_logs",
        params={"limit": limit},
        headers=_headers(),
        timeout=timeout(),
    )
    resp.raise_for_status()
    return resp.json()


def quality_summary(project_id: int) -> dict[str, Any]:
    """聚合 testgen 质量分析（趋势/flaky/维度分布/变更），供主平台 reports 入口一次性取。

    - 四项并行调用（避免串行最坏 ~8 分钟阻塞）；
    - 单项调用异常被 _safe 捕获为 ``{"ok": False, "error": ...}``，不影响其余项返回；
    - 顶层 ``ok`` = 四项业务全部成功（子项 HTTP 200 但 ``ok=False`` 也计入失败）。
    """
    def _safe(fn):
        try:
            return fn(project_id)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)[:200]}

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        trend = ex.submit(_safe, quality_trend).result()
        flaky = ex.submit(_safe, quality_flaky).result()
        filters = ex.submit(_safe, quality_filter_options).result()
        changes = ex.submit(_safe, quality_change_logs).result()
    all_ok = all(s.get("ok", False) for s in (trend, flaky, filters, changes))
    return {
        "ok": all_ok,
        "trend": trend,
        "flaky": flaky,
        "filter_options": filters,
        "change_logs": changes,
    }


def verdict(facts: dict[str, Any]) -> dict[str, Any]:
    """安全判定（M2）：把 security 用例执行后的「观测事实」发给 testgen /api/v1/verdict。

    facts **不含凭证明文**（只传 auth_mode / observed 结果）。返回
    ``{"verdict": safe|unsafe|inconclusive, "dimension", "reason", "evidence"}``。
    """
    resp = _session.post(
        f"{base_url()}/api/v1/verdict",
        json=facts,
        headers=_headers(),
        timeout=timeout(),
    )
    resp.raise_for_status()
    return resp.json()


def create_security_defect(payload: dict[str, Any]) -> dict[str, Any]:
    """（可选）在 testhub 自建一条 security 缺陷（M2-3：应拒却放通时自动建缺陷）。

    真实环境：以 testhub 自身鉴权调用其内部 ``/api/defects/defects/``。
    需要 ``TESTHUB_BASE_URL`` / ``TESTHUB_AUTH_TOKEN`` 配置（默认留空 → 调用方应注入
    ``create_defect`` 回调，直接 ``Defect.objects.create(...)`` 更稳，避免自环 HTTP）。
    """
    hub = _settings_value("TESTHUB_BASE_URL", "").rstrip("/")
    tok = _settings_value("TESTHUB_AUTH_TOKEN", "")
    if not hub:
        raise RuntimeError(
            "未配置 TESTHUB_BASE_URL，无法自动建缺陷；请在调用方注入 create_defect 回调"
        )
    headers = {"Content-Type": "application/json"}
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    resp = _session.post(
        f"{hub}/api/defects/defects/", json=payload, headers=headers, timeout=timeout()
    )
    resp.raise_for_status()
    return resp.json()
