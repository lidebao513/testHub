"""地址优先入口 → sidecar 建项客户端（独立实现，不改动现有 client.py）。"""

from __future__ import annotations

import requests

from .client import auth_token, base_url, timeout

_session = requests.Session()
_session.trust_env = False  # 本机透明代理会劫持 localhost


def ensure_sidecar_project(
    *, name: str, raw_url: str, normalized_url: str
) -> dict:
    """调用 sidecar 的独立建项端点；命中复用、未命中创建。"""
    headers = {"Content-Type": "application/json"}
    tok = auth_token()
    if tok:
        headers["X-Auth-Token"] = tok
    try:
        resp = _session.post(
            f"{base_url()}/api/v1/projects/ensure",
            json={"name": name, "base_url": raw_url or normalized_url},
            headers=headers,
            timeout=timeout(),
        )
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
        raise RuntimeError(f"testgen sidecar 不可达：{exc}") from exc
    except Exception as exc:
        raise RuntimeError(f"testgen sidecar 建项失败：{exc}") from exc
    if resp.status_code >= 400:
        raise RuntimeError(
            f"testgen sidecar 建项失败（HTTP {resp.status_code}）：{resp.text[:300]}"
        )
    return resp.json()
