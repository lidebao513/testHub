"""归一 URL → 项目查重/建项业务服务（地址优先入口专用）。

命中映射：直接读取两侧已有项目，零写入、零 sidecar 调用。
未命中：sidecar 先建项（独立端点，命中不覆盖），再在事务内建 TestHub 项目 +
ProjectEnvironment + 映射。并发重复点击由 unique(normalized_url) + 事务内复查兜底。
"""

from __future__ import annotations

import re
from typing import Any

from django.db import transaction

from apps.projects.models import Project, ProjectEnvironment

from .models import UrlProject
from .url_identity import normalize_url
from .url_project_client import ensure_sidecar_project


def _project_name(identity: dict[str, str]) -> str:
    """项目名取 host，不同根路径加后缀避免撞名（如 /pc → host-pc）。"""
    suffix = ""
    if identity["path"] != "/":
        suffix = "-" + re.sub(r"[^0-9A-Za-z]+", "-", identity["path"]).strip("-")
    return f"{identity['host']}{suffix}"[:200]


def _render(mapping: UrlProject, *, created: bool) -> dict[str, Any]:
    return {
        "ok": True,
        "created": created,
        "normalized_url": mapping.normalized_url,
        "scheme": mapping.scheme,
        "host": mapping.host,
        "testhub_project": {
            "id": mapping.testhub_project_id,
            "name": mapping.testhub_project.name,
        },
        "testgen_project": {"id": mapping.testgen_project_id},
    }


def find_url_project(*, raw_url: str) -> dict[str, Any] | None:
    """只读：按归一地址查已识别的项目映射；未识别返回 None（不建项）。"""
    normalized = normalize_url(raw_url)["normalized"]
    mapping = UrlProject.objects.filter(normalized_url=normalized).first()
    if mapping is None:
        return None
    return {
        "normalized_url": mapping.normalized_url,
        "testgen_project_id": mapping.testgen_project_id,
        "testhub_project_id": mapping.testhub_project_id,
    }


def ensure_url_project(*, raw_url: str, user: Any) -> dict[str, Any]:
    """查重命中则读取，未命中则新建两侧项目并落映射。"""
    identity = normalize_url(raw_url)
    normalized = identity["normalized"]
    original_url = raw_url.strip()

    mapping = (
        UrlProject.objects.select_related("testhub_project")
        .filter(normalized_url=normalized)
        .first()
    )
    if mapping is not None:
        return _render(mapping, created=False)

    name = _project_name(identity)
    sidecar = ensure_sidecar_project(
        name=name, raw_url=original_url, normalized_url=normalized
    )
    testgen_pid = int(sidecar["project_id"])

    with transaction.atomic():
        # 并发/重入：事务内再查一次，命中则复用，不产生第二组项目
        mapping = (
            UrlProject.objects.select_related("testhub_project")
            .filter(normalized_url=normalized)
            .first()
        )
        if mapping is not None:
            return _render(mapping, created=False)

        project = Project.objects.create(name=name, owner=user)
        ProjectEnvironment.objects.create(
            project=project,
            name="默认环境",
            base_url=original_url,
            is_default=True,
        )
        mapping = UrlProject.objects.create(
            normalized_url=normalized,
            scheme=identity["scheme"],
            host=identity["host"],
            testhub_project=project,
            testgen_project_id=testgen_pid,
        )
    return _render(mapping, created=True)
