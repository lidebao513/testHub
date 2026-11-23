"""地址优先入口的模块读取 / 扫描（复用既有表，不新建模块实体）。

复用口径：
- 扫描发现的页面/路由写入既有 ``file_descriptions`` 缓存表，命名空间 = 归一 URL，
  键 = ``runtime:<页面路径>``，用 ``symbols_sig`` 存页面指纹，天然幂等且不会与源码文件同键；
- 既有项目模块从 ``functional_points.module`` 聚合（report 模块覆盖的同一口径），
  不另建"模块"表，避免出现同一功能两套数据 / 重复计数 / 归属不清。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any
from urllib.parse import urlparse

from core import store
from core.config import get_settings
from core.db import init_db
from core.enums import MethodMarker
from core.errors import NotFoundError, ValidationError
from core.url_identity import normalize_url
from engine import runtime_ui


MODULE_PREFIX = "runtime:"
MODEL_TAG = "runtime-scan"


def _namespace(project: dict[str, Any]) -> str:
    base = (project.get("base_url") or "").strip()
    return normalize_url(base)["normalized"] if base else ""


def _page_path(page: Any) -> str:
    path = getattr(page, "path", "") or ""
    if path:
        return path
    url = getattr(page, "url", "") or ""
    parsed = urlparse(url)
    return parsed.path or url or "/"


def _signature(path: str, title: str, elements: list[Any]) -> str:
    """页面指纹：路径 + 标题 + 元素签名（截断防止超大页面撑爆缓存）。"""
    keys = sorted(
        f"{getattr(e, 'kind', '')}|{getattr(e, 'selector', '')}|{getattr(e, 'text', '')}"
        for e in elements[:150]
    )
    payload = {"path": path, "title": title or "", "elements": keys}
    return hashlib.sha1(  # nosec B324 - 非安全用途：仅做页面内容去重指纹
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _existing_modules(pid: int) -> list[dict[str, Any]]:
    """按 functional_points 聚合：页面类按路由路径，其余按 module 字段。

    页面功能点的 name 就是真实路由（PAGE 提取口径），与扫描发现的页面路径同键，
    这样同一页面既能显示扫描描述又能显示既有功能点数，不会出现两套数据。
    """
    buckets: dict[str, dict[str, Any]] = {}
    for fp in store.list_functional_points(pid):
        ftype = str(fp.get("ftype") or "").strip()
        label = str(fp.get("name") or "").strip()
        if ftype == MethodMarker.PAGE.value and label.startswith("/"):
            name = label
        else:
            name = str(fp.get("module") or "").strip() or "未分类"
        bucket = buckets.setdefault(name, {"fp_count": 0, "ftypes": {}, "samples": []})
        bucket["fp_count"] += 1
        if ftype:
            bucket["ftypes"][ftype] = bucket["ftypes"].get(ftype, 0) + 1
        if len(bucket["samples"]) < 5:
            sample = str(fp.get("name") or fp.get("file_path") or "").strip()
            if sample and sample not in bucket["samples"]:
                bucket["samples"].append(sample[:120])
    return [{"name": name, **bucket} for name, bucket in sorted(buckets.items())]


def read_modules(pid: int) -> dict[str, Any]:
    """读取项目模块：扫描发现的页面 + 既有功能点模块，按名称合并来源。"""
    project = store.get_project(pid)
    if project is None:
        raise NotFoundError(f"testgen 项目 {pid} 不存在")
    ns = _namespace(project)

    scanned: dict[str, dict[str, Any]] = {}
    if ns:
        for row in store.list_file_descriptions(ns, prefix=MODULE_PREFIX):
            name = row["file_path"][len(MODULE_PREFIX) :]
            scanned[name] = {
                "name": name,
                "source": "scan",
                "description": row.get("description") or "",
                "updated_at": row.get("updated_at") or "",
                "fp_count": 0,
                "ftypes": {},
                "samples": [],
            }

    merged = dict(scanned)
    for item in _existing_modules(pid):
        name = item["name"]
        if name in merged:
            merged[name].update(
                {
                    "source": "both",
                    "fp_count": item["fp_count"],
                    "ftypes": item["ftypes"],
                    "samples": item["samples"],
                }
            )
        else:
            merged[name] = {
                "name": name,
                "source": "existing",
                "description": "",
                "updated_at": "",
                **item,
            }

    modules = sorted(merged.values(), key=lambda m: (-int(m["fp_count"]), str(m["name"])))
    return {
        "ok": True,
        "project_id": pid,
        "normalized_url": ns,
        "modules": modules,
        "scanned_count": len(scanned),
        "existing_count": len(_existing_modules(pid)),
        "total": len(modules),
    }


def scan_modules(  # noqa: PLR0913 - 扫描参数本就是一组平铺的发现配置
    pid: int,
    *,
    max_pages: int = 8,
    timeout: int = 8,
    login_user: str = "",
    login_password: str = "",
    login_otp: str = "",
    login_url: str = "",
    routes: list[str] | None = None,
    discover_by_menu: bool = True,
) -> dict[str, Any]:
    """只做浏览器发现（不生成用例、不执行用例），把页面/路由写入既有描述缓存。"""
    project = store.get_project(pid)
    if project is None:
        raise NotFoundError(f"testgen 项目 {pid} 不存在")
    ns = _namespace(project)
    if not ns:
        raise ValidationError("该项目没有被测地址，无法扫描模块")

    # 描述缓存表可能尚未在任何扫描/生成流程中初始化（新项目首扫即此情形）
    with store.connect() as conn:
        init_db(conn)

    settings = get_settings()
    opts = runtime_ui.RuntimeUiOptions(
        base_url=(project.get("base_url") or "").strip(),
        channel=settings.playwright_channel,
        max_pages=max(1, min(int(max_pages), 60)),
        timeout=max(1, min(int(timeout), 60)),
        login_user=(login_user or "").strip(),
        login_password=login_password or "",
        login_otp=(login_otp or "").strip(),
        login_url=(login_url or "").strip(),
        routes=list(routes or []),
        discover_by_menu=bool(discover_by_menu),
        card_nav_mode="off",
        mobile_enabled=False,
    )
    result = runtime_ui.discover_ui(opts)

    found: dict[str, dict[str, Any]] = {}
    for page in result.reachable_pages():
        path = _page_path(page)
        if not path:
            continue
        found[path] = {
            "title": getattr(page, "title", "") or "",
            "elements": list(getattr(page, "elements", []) or []),
        }
    for route in result.discovered_routes:
        path = str(route).strip()
        if path and path not in found:
            found[path] = {"title": "", "elements": []}

    persisted = 0
    for path, info in sorted(found.items()):
        elements = info["elements"]
        title = info["title"]
        description = (
            f"{title or path}｜{len(elements)} 个交互元素"
            if elements
            else f"{title or path}｜仅页面"
        )
        store.upsert_file_description(
            ns,
            f"{MODULE_PREFIX}{path}",
            _signature(path, title, elements),
            description[:500],
            MODEL_TAG,
        )
        persisted += 1

    payload = read_modules(pid)
    payload["scan"] = {
        "pages": len(result.reachable_pages()),
        "routes": len(result.discovered_routes),
        "elements": len(result.elements),
        "api_endpoints": len(result.api_endpoints),
        "persisted": persisted,
        "logged_in": bool(result.logged_in),
        "notes": list(result.notes or [])[:20],
    }
    return payload
