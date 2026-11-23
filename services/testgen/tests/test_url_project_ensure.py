"""地址优先入口：归一 URL 与「命中不覆盖」建项守护测试。"""

from __future__ import annotations

import pytest

from core import store
from core.errors import ValidationError
from core.url_identity import normalize_url
from core.url_projects import ensure_project


def test_normalize_url_strips_default_port_trailing_slash_and_query() -> None:
    got = normalize_url("HTTPS://Example.COM:443/pc///?from=menu#top")
    assert got == {
        "scheme": "https",
        "host": "example.com",
        "path": "/pc",
        "normalized": "https://example.com/pc",
    }


def test_normalize_url_supplies_scheme_and_keeps_nondefault_port() -> None:
    got = normalize_url("example.com:8080/pc/")
    assert got["normalized"] == "http://example.com:8080/pc"
    assert got["host"] == "example.com:8080"


def test_normalize_url_rejects_empty() -> None:
    with pytest.raises(ValidationError):
        normalize_url("   ")


def test_ensure_project_creates_then_reuses_same_normalized_url() -> None:
    first = ensure_project(name="example.com-pc", base_url="https://example.com/pc/")
    assert first["created"] is True
    pid = first["project_id"]

    second = ensure_project(name="different-name", base_url="HTTPS://EXAMPLE.com:443/pc/?x=1")
    assert second["created"] is False
    assert second["project_id"] == pid
    # 命中后不覆盖任何已有字段（不同于 stage_register 的 upsert 语义）
    row = store.get_project(pid)
    assert row["name"] == "example.com-pc"
    assert row["base_url"] == "https://example.com/pc"


def test_ensure_project_same_name_different_url_does_not_overwrite_base_url() -> None:
    first = ensure_project(name="same-name", base_url="http://a.example.com/pc")
    second = ensure_project(name="same-name", base_url="http://b.example.com/pc")
    assert second["created"] is False
    assert second["project_id"] == first["project_id"]
    assert store.get_project(first["project_id"])["base_url"] == "http://a.example.com/pc"
