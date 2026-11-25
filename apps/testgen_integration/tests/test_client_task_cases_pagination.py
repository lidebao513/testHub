"""批 4 回归守护：client.task_cases 必须跨页拉全量，不静默截断大仓用例。

sidecar 批 4 把 /api/v1/tasks/{id}/cases 默认改为 limit=200（上限 1000），
主平台地址通道回流与详情面板的历史语义是「一次拿全部用例」。客户端负责
按 total 循环合并，调用方与现有 mock patch 路径保持不变。
"""

from unittest.mock import Mock, patch

import pytest
import requests

from apps.testgen_integration.client import task_cases


def _resp(payload: dict) -> Mock:
    resp = Mock()
    resp.json.return_value = payload
    resp.raise_for_status.return_value = None
    return resp


def test_task_cases_merges_all_pages():
    """3 页共 5 条：合并结果完整、total 保留、请求次数等于页数。"""
    all_cases = [{"tc_no": f"TC-{i}"} for i in range(5)]

    def fake_get(url, *, params, headers, timeout):
        offset = params["offset"]
        return _resp(
            {
                "ok": True,
                "task_id": "t-1",
                "project_id": 69,
                "total": len(all_cases),
                "offset": offset,
                "limit": params["limit"],
                "cases": all_cases[offset : offset + 2],
            }
        )

    with patch("apps.testgen_integration.client._session") as session:
        session.get.side_effect = fake_get
        result = task_cases("t-1")

    assert [c["tc_no"] for c in result["cases"]] == [f"TC-{i}" for i in range(5)]
    assert result["total"] == 5
    assert result["project_id"] == 69
    assert session.get.call_count == 3
    assert [c.kwargs["params"]["offset"] for c in session.get.call_args_list] == [
        0,
        2,
        4,
    ]


def test_task_cases_stops_when_offset_reaches_total():
    """恰好取整页时立即停止，不多发一次空页请求。"""
    rows = [{"tc_no": "TC-1"}, {"tc_no": "TC-2"}]

    def fake_get(url, *, params, headers, timeout):
        return _resp(
            {
                "ok": True,
                "total": 2,
                "offset": params["offset"],
                "cases": rows if params["offset"] == 0 else [],
            }
        )

    with patch("apps.testgen_integration.client._session") as session:
        session.get.side_effect = fake_get
        result = task_cases("t-1")

    assert [c["tc_no"] for c in result["cases"]] == ["TC-1", "TC-2"]
    assert session.get.call_count == 1


def test_task_cases_stops_on_empty_page_even_if_total_is_large():
    """服务端中途返回空页时应立即停止，避免 total 异常导致死循环。"""
    with patch("apps.testgen_integration.client._session") as session:
        session.get.return_value = _resp({"ok": True, "total": 9999, "cases": []})
        result = task_cases("t-1")

    assert result["cases"] == []
    assert session.get.call_count == 1


def test_task_cases_supports_legacy_sidecar_without_total():
    """老版 sidecar 未分页（无 total 字段）：第一页即全量，不重复拉取。"""
    legacy = {"ok": True, "cases": [{"tc_no": "TC-OLD"}]}
    with patch("apps.testgen_integration.client._session") as session:
        session.get.return_value = _resp(legacy)
        result = task_cases("t-1")

    assert result == legacy
    assert session.get.call_count == 1


def test_task_cases_requests_max_page_and_obsolete_for_compat():
    """按 sidecar 上限 1000 请求，并显式 include_obsolete=true 保持旧语义。"""
    with patch("apps.testgen_integration.client._session") as session:
        session.get.return_value = _resp({"ok": True, "total": 1, "cases": [{"tc_no": "TC-1"}]})
        task_cases("t-1")

    kwargs = session.get.call_args.kwargs
    assert kwargs["params"] == {
        "offset": 0,
        "limit": 1000,
        "include_obsolete": "true",
    }
    assert kwargs["timeout"] > 0
    assert kwargs["headers"]["Content-Type"] == "application/json"


def test_task_cases_propagates_http_error():
    """分页中途 HTTP 失败必须上抛，不能返回半截用例被误当全量回流。"""
    with patch("apps.testgen_integration.client._session") as session:
        session.get.return_value.raise_for_status.side_effect = requests.HTTPError("500 boom")
        with pytest.raises(requests.HTTPError):
            task_cases("t-1")
