"""被测地址归一化（地址优先入口专用）。

口径与 sidecar ``services/testgen/core/url_identity.py`` 完全一致：
scheme + host（去默认端口）+ path（去尾斜杠，空为 "/"），不含 query/fragment/凭证。
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

_SCHEME_RE = re.compile(r"^https?://", re.IGNORECASE)


def normalize_url(raw: str) -> dict[str, str]:
    """归一成功返回 {scheme, host, path, normalized}；非法输入抛 ValueError。"""
    value = (raw or "").strip()
    if not value:
        raise ValueError("请先填写被测地址")
    candidate = value if _SCHEME_RE.match(value) else f"http://{value}"
    try:
        parsed = urlparse(candidate)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"被测地址端口非法：{exc}") from exc
    if not hostname:
        raise ValueError("被测地址缺少主机名")
    scheme = (parsed.scheme or "http").lower()
    if scheme not in ("http", "https"):
        raise ValueError("被测地址仅支持 http / https")
    default_port = 443 if scheme == "https" else 80
    host = hostname.lower()
    if port and port != default_port:
        host = f"{host}:{port}"
    path = re.sub(r"/+$", "", parsed.path) or "/"
    return {
        "scheme": scheme,
        "host": host,
        "path": path,
        "normalized": f"{scheme}://{host}{path}",
    }
