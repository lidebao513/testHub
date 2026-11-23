"""地址优先入口 → sidecar 模块读取/扫描客户端（独立实现，不改现有 client.py）。"""

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


def _request(method: str, path: str, *, payload: dict | None = None) -> dict:
    try:
        resp = _session.request(
            method,
            f"{base_url()}{path}",
            json=payload,
            headers=_headers(),
            timeout=timeout(),
        )
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
        raise RuntimeError(f"testgen sidecar 不可达：{exc}") from exc
    except Exception as exc:
        raise RuntimeError(f"testgen sidecar 模块请求失败：{exc}") from exc
    if resp.status_code >= 400:
        raise RuntimeError(
            f"testgen sidecar 模块请求失败（HTTP {resp.status_code}）：{resp.text[:300]}"
        )
    return resp.json()


def fetch_modules(testgen_project_id: int) -> dict:
    return _request("GET", f"/api/v1/projects/{testgen_project_id}/modules")


def scan_modules(testgen_project_id: int, options: dict) -> dict:
    return _request(
        "POST", f"/api/v1/projects/{testgen_project_id}/modules/scan", payload=options
    )
