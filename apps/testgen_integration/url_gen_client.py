"""地址优先入口 → sidecar 地址通道生成客户端（独立实现，不改现有 client.py）。

红线：本客户端只提交「生成」请求（sidecar /api/v1/url-generation/generate 强制
execute=False）；poll / cases 直接复用现有 client 的同名函数，避免重复实现任务协议。
"""

from __future__ import annotations

import requests

from .client import auth_token, base_url, timeout

_session = requests.Session()
_session.trust_env = False  # 本机透明代理会劫持 localhost


def _headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    tok = auth_token()
    if tok:
        headers["X-Auth-Token"] = tok
    return headers


# 只透传白名单字段：凭证不落库、execute 永不为真（由 sidecar 再次强制）。
_PASSTHROUGH = (
    "test_url",
    "login_url",
    "login_user",
    "login_password",
    "login_otp",
    "runtime_routes",
    "changed_files",
    "repo_url",
    "code_source",
    "base",
    "target",
    "project_id",
    "project_name",
    "runtime_scope",
    "scopes",
)


def submit_url_generation(payload: dict) -> dict:
    """提交地址通道生成任务，返回 sidecar 的 202 任务受理体（task_id/poll_url）。"""
    body = {key: payload[key] for key in _PASSTHROUGH if payload.get(key) not in (None, "", [])}
    body["execute"] = False  # 显式生成-only（sidecar 亦强制）
    try:
        resp = _session.post(
            f"{base_url()}/api/v1/url-generation/generate",
            json=body,
            headers=_headers(),
            timeout=timeout(),
        )
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
        raise RuntimeError(f"testgen sidecar 不可达：{exc}") from exc
    except Exception as exc:
        raise RuntimeError(f"地址通道生成请求失败：{exc}") from exc
    if resp.status_code >= 400:
        raise RuntimeError(
            f"地址通道生成被拒绝（HTTP {resp.status_code}）：{resp.text[:300]}"
        )
    return resp.json()
