"""B-5 批次 2：describe 缓存命名空间接入的集成守护。

覆盖方案 §4 的五类场景：本地双目录不串味、多仓隔离、无身份真禁用、旧 repo_url
仍命中、生成侧背景注入与写入侧同源；外加「精确清缓存不误删 runtime 模块」。
LLM 调用全部本地桩，不触网。
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from core import store
from core.contracts import FunctionalPoint
from core.describe_namespace import describe_namespace
from core.enums import FType
from engine import pipeline
from service import app as app_module
from service.app import app


@pytest.fixture()
def client():
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _clean_describe_cache(fresh_db):
    """fresh_db 不清描述缓存表（跨用例会串），本用例组显式清干净。"""
    store.clear_file_descriptions()
    yield
    store.clear_file_descriptions()


@pytest.fixture(autouse=True)
def _llm_configured(monkeypatch):
    monkeypatch.setattr(app_module.settings, "llm_api_key", "test-key")
    monkeypatch.setattr(app_module.settings, "llm_base_url", "http://stub.local/v1")
    monkeypatch.setattr(app_module.settings, "llm_model", "stub-model")


class _Msg:
    def __init__(self, content: str) -> None:
        self.content = content


class _Choice:
    def __init__(self, content: str) -> None:
        self.message = _Msg(content)


class _Resp:
    def __init__(self, content: str) -> None:
        self.choices = [_Choice(content)]


def _stub_llm():
    """按调用序返回「描述N-<key>」，据此判定是否真的重新调用了 LLM。"""
    calls: list[list[str]] = []

    def _impl(**kwargs: Any) -> _Resp:
        prompt = (kwargs.get("messages") or [{}])[0].get("content", "")
        keys = [
            line.strip().split("key:", 1)[1].split("|", 1)[0].strip()
            for line in prompt.splitlines()
            if line.strip().startswith("- key:")
        ]
        calls.append(keys)
        rows = [{"key": k, "功能": f"描述{len(calls)}-{k}"} for k in keys]
        return _Resp(json.dumps({"结果": rows}, ensure_ascii=False))

    return _impl, calls


def _items() -> list[dict[str, Any]]:
    return [{"path": "src/api.py", "name": "api", "functions": ["list_items"]}]


def _fp(path: str = "src/api.py") -> FunctionalPoint:
    return FunctionalPoint(
        fp_id="FP-b5-0001",
        ftype=FType.BUSINESS.value,
        file_path=path,
        name="list_items",
        title="api · list_items",
        module="api",
    )


def test_local_directories_do_not_share_description_cache(client, monkeypatch) -> None:
    stub, calls = _stub_llm()
    monkeypatch.setattr(app_module, "chat_with_fallback", stub)
    payload = {"items": _items(), "local_path": "/tmp/dir-a"}

    first = client.post("/api/v1/describe", json=payload).json()
    second = client.post("/api/v1/describe", json={**payload, "local_path": "/tmp/dir-b"}).json()
    third = client.post("/api/v1/describe", json=payload).json()

    assert first["descriptions"]["src/api.py"] == "描述1-src/api.py"
    assert second["descriptions"]["src/api.py"] == "描述2-src/api.py"
    assert second["cached"]["src/api.py"] is False  # 不同本地目录不得命中彼此
    assert third["descriptions"]["src/api.py"] == "描述1-src/api.py"
    assert third["cached"]["src/api.py"] is True  # 同目录仍可复用
    assert len(calls) == 2


def test_multi_repo_keys_are_isolated(client, monkeypatch) -> None:
    stub, calls = _stub_llm()
    monkeypatch.setattr(app_module, "chat_with_fallback", stub)
    base = {"items": _items(), "project_id": 5}

    first = client.post("/api/v1/describe", json={**base, "repo_key": "backend"}).json()
    second = client.post("/api/v1/describe", json={**base, "repo_key": "frontend"}).json()

    assert first["descriptions"]["src/api.py"] == "描述1-src/api.py"
    assert second["descriptions"]["src/api.py"] == "描述2-src/api.py"
    assert second["cached"]["src/api.py"] is False
    assert len(calls) == 2


def test_no_identity_means_no_cache_read_or_write(client, monkeypatch) -> None:
    stub, _ = _stub_llm()
    monkeypatch.setattr(app_module, "chat_with_fallback", stub)
    stats = {"get": 0, "upsert": 0}
    original_get = store.get_file_descriptions
    original_upsert = store.upsert_file_description

    def _spy_get(*args: Any, **kwargs: Any) -> Any:
        stats["get"] += 1
        return original_get(*args, **kwargs)

    def _spy_upsert(*args: Any, **kwargs: Any) -> None:
        stats["upsert"] += 1
        original_upsert(*args, **kwargs)

    monkeypatch.setattr(store, "get_file_descriptions", _spy_get)
    monkeypatch.setattr(store, "upsert_file_description", _spy_upsert)

    resp = client.post("/api/v1/describe", json={"items": _items()}).json()

    assert stats == {"get": 0, "upsert": 0}
    assert resp["cached"] == {"src/api.py": False}
    assert resp["descriptions"]["src/api.py"] == "描述1-src/api.py"


def test_legacy_repo_url_still_hits_history_cache(client, monkeypatch) -> None:
    stub, calls = _stub_llm()
    monkeypatch.setattr(app_module, "chat_with_fallback", stub)
    payload = {"items": _items(), "repo_url": "https://legacy.example/x.git"}

    first = client.post("/api/v1/describe", json=payload).json()
    second = client.post("/api/v1/describe", json=payload).json()

    assert first["cached"]["src/api.py"] is False
    assert second["cached"]["src/api.py"] is True
    assert second["descriptions"]["src/api.py"] == first["descriptions"]["src/api.py"]
    assert len(calls) == 1


def test_load_file_context_shares_namespace_with_describe() -> None:
    ns = describe_namespace(local_path="/tmp/dir-a")
    store.upsert_file_description(ns, "src/api.py", "sig", "领域查询模块", "m")

    same = pipeline.load_file_context("", [_fp()], local_path="/tmp/dir-a")
    other = pipeline.load_file_context("", [_fp()], local_path="/tmp/dir-b")
    disabled = pipeline.load_file_context("", [_fp()])

    assert same == {"src/api.py": "领域查询模块"}
    assert other == {}
    assert disabled == {}


def test_clear_namespace_keeps_runtime_modules(client) -> None:
    runtime_ns = describe_namespace(runtime_url="https://app.example/dashboard/")
    store.upsert_file_description(runtime_ns, "runtime:/dashboard", "sig", "页面模块", "m")
    repo_ns = "https://legacy.example/x.git"
    store.upsert_file_description(repo_ns, "src/api.py", "sig", "接口模块", "m")

    result = client.post("/api/v1/describe/cache/clear", json={"repo_url": repo_ns}).json()

    assert result["scope"] == repo_ns
    assert result["deleted"] == 1
    assert store.get_file_descriptions(repo_ns, ["src/api.py"]) == {}
    assert (
        store.get_file_descriptions(runtime_ns, ["runtime:/dashboard"])["runtime:/dashboard"][
            "description"
        ]
        == "页面模块"
    )
