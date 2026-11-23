"""被测地址归一化（地址优先入口专用，独立于代码扫描链路）。

归一口径：scheme + host（去掉默认端口）+ path（去掉尾部斜杠，空为 "/"）；
不含 query / fragment / 用户名密码。前后端必须以同一口径算出同一个项目键。
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from core.errors import ValidationError


_SCHEME_RE = re.compile(r"^https?://", re.IGNORECASE)


def normalize_url(raw: str) -> dict[str, str]:
    """把用户输入归一成稳定的项目查重键。"""
    value = (raw or "").strip()
    if not value:
        raise ValidationError("请先填写被测地址")
    candidate = value if _SCHEME_RE.match(value) else f"http://{value}"
    try:
        parsed = urlparse(candidate)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValidationError(f"被测地址端口非法：{exc}") from exc
    if not hostname:
        raise ValidationError("被测地址缺少主机名")
    scheme = (parsed.scheme or "http").lower()
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
