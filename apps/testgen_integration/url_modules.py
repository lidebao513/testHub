"""地址优先入口的模块读取/扫描业务服务（只做映射解析 + 转发，不写旧链路）。"""

from __future__ import annotations

from typing import Any

from .models import UrlProject
from .url_identity import normalize_url
from .url_module_client import fetch_modules, scan_modules

_SCAN_KEYS = (
    "max_pages",
    "timeout",
    "login_user",
    "login_password",
    "login_otp",
    "login_url",
    "routes",
    "discover_by_menu",
)


def _mapping(raw_url: str) -> UrlProject:
    identity = normalize_url(raw_url)
    mapping = (
        UrlProject.objects.filter(normalized_url=identity["normalized"]).first()
    )
    if mapping is None:
        raise ValueError("该地址尚未识别项目，请先点「识别项目」")
    return mapping


def read_modules_for_url(*, raw_url: str) -> dict[str, Any]:
    mapping = _mapping(raw_url)
    return fetch_modules(mapping.testgen_project_id)


def scan_modules_for_url(*, raw_url: str, options: dict[str, Any]) -> dict[str, Any]:
    mapping = _mapping(raw_url)
    payload = {key: options[key] for key in _SCAN_KEYS if key in options}
    return scan_modules(mapping.testgen_project_id, payload)
