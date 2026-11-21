"""变更靶向（change_targeting）单测。

覆盖真实缺陷场景：给了被测地址 + 增量模式时，运行时通道会把整站扫一遍，
必须按「改了什么」把 URL 目标收敛到变更相关的页面/接口，否则改 5 个文件出 294 条用例。
"""

from __future__ import annotations

from engine import change_targeting as ct


_PAGE_REL = "frontend/app/src/pages/pc/PcChatPage.tsx"
_SHELL_REL = "frontend/app/src/pages/pc/PcShell.tsx"
_API_REL = "frontend/app/src/api/backend.ts"
_BACKEND_REL = "backend/research-agent-source/server.py"

_PAGE_SRC = """
export default function PcChatPage() {
  const onSessionCreated = () => {};
  return null;
}
"""

_SHELL_SRC = """
export default function PcShell() {
  return <Routes path="/chat" />;
}
"""

_API_SRC = """
export async function listThreads(channel?: string) {
  return apiJson('/api/v1/threads', {});
}

export function listThreadsPage(channel: string | undefined, offset: number, limit = 20) {
  const params = new URLSearchParams();
  return apiJson(`/api/v1/threads?${params.toString()}`);
}
"""

_BACKEND_SRC = """
from fastapi import APIRouter

router = APIRouter()


@router.get("/api/v1/threads")
async def list_threads():
    return _record_thread_activity()


def _record_thread_activity():
    return 1
"""


def _line_of(text: str, needle: str) -> int:
    for idx, line in enumerate(text.splitlines(), start=1):
        if needle in line:
            return idx
    raise AssertionError(f"未找到 {needle}")


def test_api_layer_file_maps_literal_to_endpoint():
    """前端 api 层变更 → 函数体里的 /api/v1/threads 字面量 → 接口目标。"""
    ranges = {_API_REL: [(_line_of(_API_SRC, "listThreadsPage"), 4)]}
    plan = ct.build_target_plan(
        changed_files=[_API_REL],
        hunks=ranges,
        sources={_API_REL: _API_SRC},
        api_endpoints=[("GET", "/api/v1/threads"), ("GET", "/api/v1/memories")],
        page_paths=["/chat"],
    )
    assert plan.api_paths == ["/api/v1/threads"]
    assert "/api/v1/memories" not in plan.api_paths
    assert plan.notes and "命中" in plan.notes[0]


def test_api_target_covers_sub_paths():
    """`/threads` 应覆盖 `/threads/{id}`、`/threads/{id}/artifacts`，但不越界到别的资源。"""
    plan = ct.build_target_plan(
        changed_files=[_API_REL],
        sources={_API_REL: _API_SRC},
        api_endpoints=[
            ("GET", "/api/v1/threads"),
            ("GET", "/api/v1/threads/{id}"),
            ("GET", "/api/v1/threads/{id}/artifacts"),
            ("GET", "/api/v1/memories"),
        ],
    )
    assert plan.match_api("/api/v1/threads") is True
    assert plan.match_api("/api/v1/threads/{id}/artifacts") is True
    assert plan.match_api("/api/v1/memories") is False


def test_page_file_maps_to_route_by_filename():
    """PcChatPage.tsx → 候选路由 /chat，命中运行时页面。"""
    plan = ct.build_target_plan(
        changed_files=[_PAGE_REL],
        sources={_PAGE_REL: _PAGE_SRC},
        page_paths=["/", "/chat", "/pc/tasks"],
    )
    assert plan.page_paths == ["/chat"]
    assert plan.match_page("/chat") is True
    assert plan.match_page("/pc/tasks") is False


def test_shell_file_does_not_produce_page_targets():
    """壳/布局文件里的路由表是全站菜单，绝不能拿它做靶向（否则又退化为全站巡检）。"""
    plan = ct.build_target_plan(
        changed_files=[_SHELL_REL],
        sources={_SHELL_REL: _SHELL_SRC},
        page_paths=["/", "/chat", "/pc/tasks"],
    )
    assert plan.page_paths == []
    assert plan.is_empty is True


def test_backend_private_helper_resolves_via_caller_route():
    """私有辅助函数没有装饰器 → 反查一层调用方拿路由（本次 server.py 的真实形态）。"""
    ranges = {_BACKEND_REL: [(_line_of(_BACKEND_SRC, "def _record_thread_activity"), 2)]}
    plan = ct.build_target_plan(
        changed_files=[_BACKEND_REL],
        hunks=ranges,
        sources={_BACKEND_REL: _BACKEND_SRC},
        api_endpoints=[("GET", "/api/v1/threads"), ("GET", "/api/v1/memories")],
    )
    assert plan.api_paths == ["/api/v1/threads"]
    assert "_record_thread_activity" in plan.symbols
    assert "api/v1/threads" in plan.reasons["/api/v1/threads"]


def test_keyword_fallback_only_when_no_literal():
    """没有任何接口字面量时才用符号关键词兜底，且标注为弱证据。"""
    src = "export function syncThreadState() { return 1 }\n"
    rel = "frontend/app/src/store/thread.ts"
    plan = ct.build_target_plan(
        changed_files=[rel],
        sources={rel: src},
        api_endpoints=[("GET", "/api/v1/threads"), ("GET", "/api/v1/memories")],
    )
    assert plan.api_paths == ["/api/v1/threads"]
    assert "弱证据" in plan.reasons["/api/v1/threads"]


def test_empty_change_yields_empty_plan_with_note():
    """纯样式改动：没有任何可映射目标 → is_empty 且如实说明。"""
    rel = "frontend/app/src/styles/main.css"
    plan = ct.build_target_plan(
        changed_files=[rel],
        sources={rel: ".a{color:red}"},
        api_endpoints=[("GET", "/api/v1/threads")],
        page_paths=["/chat"],
    )
    assert plan.is_empty is True
    assert plan.unmatched_files == [rel]
    assert any("未命中" in n for n in plan.notes)


def test_normalize_api_path_folds_ids_and_templates():
    assert ct.normalize_api_path("/api/v1/threads/1234") == "/api/v1/threads/{id}"
    assert (
        ct.normalize_api_path("/api/v1/threads/${id}/artifacts") == "/api/v1/threads/{id}/artifacts"
    )
    assert ct.normalize_api_path("/api/v1/threads?offset=20&limit=20") == "/api/v1/threads"
    assert (
        ct.normalize_api_path("/api/v1/threads/8f14e45f-ceea-467a-9c2f-1a2b3c4d5e6f")
        == "/api/v1/threads/{id}"
    )


def test_test_files_are_ignored():
    """测试文件会 import 全站接口，不能作为靶向依据（实测会带出 20 个无关接口）。"""
    rel = "backend/research-agent-source/tests/test_thread_pagination.py"
    src = """
import requests
def test_all():
    requests.get('/api/v1/memories')
    requests.get('/api/v1/my-skills')
"""
    plan = ct.build_target_plan(
        changed_files=[rel],
        sources={rel: src},
        api_endpoints=[("GET", "/api/v1/memories"), ("GET", "/api/v1/my-skills")],
    )
    assert plan.api_paths == []
    assert plan.is_empty is True
    assert plan.unmatched_files == []


def test_generic_words_are_not_used_as_keywords():
    """`list`/`params` 这类通用词进关键词会把靶向放大，必须留在停用词表里。"""
    plan = ct.build_target_plan(
        changed_files=[_API_REL],
        sources={_API_REL: _API_SRC},
        api_endpoints=[("GET", "/api/v1/threads")],
    )
    assert "list" not in plan.keywords
    assert "params" not in plan.keywords
    assert "threads" in plan.keywords


def test_unmatched_files_listed_when_only_some_map():
    """部分文件可映射、部分不可 → 两者都要如实反映（不能静默吞掉）。"""
    css = "frontend/app/src/styles/main.css"
    plan = ct.build_target_plan(
        changed_files=[_API_REL, css],
        sources={_API_REL: _API_SRC, css: ".a{color:red}"},
        api_endpoints=[("GET", "/api/v1/threads")],
    )
    assert plan.api_paths == ["/api/v1/threads"]
    assert plan.unmatched_files == [css]
