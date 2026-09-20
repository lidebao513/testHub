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

import os
from typing import Any

import requests
from requests.exceptions import RequestException, Timeout

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
    except Exception:  # noqa: BLE001 - Django 不可用时静默回退
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
