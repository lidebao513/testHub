"""B-5 批次 2：Django 代理层 describe 身份字段透传（离线 mock，不依赖 sidecar）。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from apps.testgen_integration import client, views


def _fake_resp() -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"ok": True, "descriptions": {}}
    return resp


@patch.object(client._session, "post")
def test_client_passes_identity_fields(post) -> None:
    post.return_value = _fake_resp()

    client.describe(
        items=[{"path": "src/api.py"}],
        local_path="/tmp/dir-a",
        project_id=5,
        repo_key="backend",
    )

    payload = post.call_args.kwargs["json"]
    assert payload["items"] == [{"path": "src/api.py"}]
    assert payload["local_path"] == "/tmp/dir-a"
    assert payload["project_id"] == 5
    assert payload["repo_key"] == "backend"
    assert "runtime_url" not in payload  # 空字段不透传，旧调用体保持最小
    assert "namespace" not in payload


@patch.object(client._session, "post")
def test_legacy_client_payload_unchanged(post) -> None:
    post.return_value = _fake_resp()

    client.describe(items=[{"path": "src/api.py"}])

    assert post.call_args.kwargs["json"] == {"items": [{"path": "src/api.py"}]}


def test_view_forwards_identity_fields() -> None:
    body = {
        "items": [{"path": "src/api.py"}],
        "namespace": "custom:ns",
        "runtime_url": "https://app.example/path/",
        "repo_key": "frontend",
        "project_id": 5,
        "repo_url": "https://repo.example/x.git",
        "local_path": "/tmp/dir-b",
    }
    req = SimpleNamespace(data=body)  # DRF Request 的 .data；直接调 view.post 绕开鉴权分发

    with patch.object(views, "describe", return_value={"ok": True}) as mocked:
        resp = views.TestgenDescribeView().post(req)

    assert resp.status_code == 200
    kwargs = mocked.call_args.kwargs
    assert kwargs["items"] == body["items"]
    assert kwargs["namespace"] == "custom:ns"
    assert kwargs["runtime_url"] == body["runtime_url"]
    assert kwargs["repo_key"] == "frontend"
    assert kwargs["project_id"] == 5
    assert kwargs["repo_url"] == body["repo_url"]
    assert kwargs["local_path"] == "/tmp/dir-b"
