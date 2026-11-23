"""地址优先入口的 testgen 侧建项服务。

刻意不复用 ``pipeline.stage_register`` 的 upsert 语义：
那里按 name 全量覆盖 local_path/git_url/branch/base_url，命中旧项目会抹掉已有字段。
本服务先按归一 URL（再按同名）查重，命中一律原样返回；只有未命中才创建。
"""

from __future__ import annotations

from typing import Any

from core import store
from core.db import init_db
from core.url_identity import normalize_url


def ensure_project(*, name: str, base_url: str) -> dict[str, Any]:
    """按归一 URL 查重；命中复用，未命中新建。返回完整项目对象。"""
    identity = normalize_url(base_url)
    normalized = identity["normalized"]

    with store.connect() as conn:
        init_db(conn)
        rows = store.list_projects(conn)

    hit = next((r for r in rows if (r.get("base_url") or "") == normalized), None)
    if hit is None:
        hit = next((r for r in rows if r.get("name") == name), None)
    if hit is not None:
        return {"ok": True, "created": False, "project_id": hit["id"], "project": dict(hit)}

    pid = store.upsert_project(
        name,
        base_url.strip() or normalized,
        base_url=normalized,
    )
    project = store.get_project(pid) or {}
    return {"ok": True, "created": True, "project_id": pid, "project": dict(project)}
