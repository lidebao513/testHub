"""T1 改动归属单测。

背景：`fp_extract` 对私有函数（`_` 前缀）不产功能点、对被 F6 调用图吞并的辅助函数也不产。
函数级收窄只认「功能点函数体 ∩ hunk」，于是这两类函数被改动时整次改动会被静默丢掉
（实测：`_record_thread_activity` 的标题语义改动 → 后端 0 条用例）。

覆盖：
- 私有函数被路由调用 → 改动归属到最近入口，入口功能点回到保留集；
- 同一入口被多个 hunk 命中 → 只归属一次（不重复补点）；
- 没有调用方（孤立函数）→ 不产点，只计数；
- 路由函数体自己变更 → 功能点本来就在保留集，不触发归属；
- 开关关闭 / max_entries 截断 → 行为与基线一致。
"""

from pathlib import Path

from core.contracts import FunctionalPoint
from core.enums import FType
from engine import incremental_narrow as IN
from engine.scan import SourceFile


_SRC = '''\
router = object()


def _helper(value):
    return value is not None


def _second(value):
    return value or "x"


@router.post("/api/v1/chat")
def chat(payload):
    """发送消息。"""
    return _helper(payload)


@router.get("/api/v1/threads")
def list_threads():
    """会话列表。"""
    return _second(_helper(None))


def orphan_helper():
    return 1
'''


def _sf() -> SourceFile:
    return SourceFile(rel="app/api.py", abspath=Path("app/api.py"), text=_SRC)


def _line_of(text: str) -> int:
    needle = text.strip()
    for i, ln in enumerate(_SRC.splitlines(), 1):
        if ln.strip() == needle:
            return i
    raise AssertionError(text)


def _line_of_prefix(prefix: str) -> int:
    needle = prefix.strip()
    for i, ln in enumerate(_SRC.splitlines(), 1):
        if ln.strip().startswith(needle):
            return i
    raise AssertionError(prefix)


def _api_fp(func: str) -> FunctionalPoint:
    name = "/api/v1/chat" if func == "chat" else "/api/v1/threads"
    return FunctionalPoint(
        fp_id=f"FP::{func}",
        ftype=FType.API.value,
        file_path="app/api.py",
        name=f"{'POST' if func == 'chat' else 'GET'} {name}",
        title=func,
        description=f"{func}() @ app/api.py:{_line_of_prefix('def ' + func + '(')}",
    )


def _files() -> dict:
    return {"app/api.py": _sf()}


def _run(hunks, *, attribution=True, max_entries=1, fps=None):
    fps = fps if fps is not None else [_api_fp("chat"), _api_fp("list_threads")]
    return IN.narrow_to_changed_functions(
        fps,
        _files(),
        hunks,
        changed_files={"app/api.py"},
        attribution=IN.AttributionOptions(enabled=attribution, max_entries=max_entries),
    )


def test_private_change_attributes_to_nearest_entry() -> None:
    line = _line_of("    return value is not None")
    res = _run({"app/api.py": [(line, 1)]})

    kept_ids = {fp.fp_id for fp in res.kept}
    assert "FP::chat" in kept_ids  # 私有函数没有功能点，改动归属到调用它的路由
    assert res.stats["fp_attributed"] == 1
    assert res.stats["hunk_uncovered"] == 1
    ev = res.attributed_evidence[0]
    assert ev["entry"] == "chat"
    assert ev["via"] == "_helper"
    assert ev["hunk_line"] == line
    assert ev["fp_id"] == "FP::chat"
    # 被补回的功能点不再是「未变更剔除」
    assert "FP::chat" not in res.dropped_fps


def test_switch_off_keeps_baseline_behaviour() -> None:
    line = _line_of("    return value is not None")
    res = _run({"app/api.py": [(line, 1)]}, attribution=False)

    assert res.kept == []
    assert res.stats["fp_attributed"] == 0
    assert res.attributed_evidence == []
    assert res.dropped_fps  # 两个路由功能点都因未变更被剔除


def test_orphan_helper_produces_no_point() -> None:
    line = _line_of("    return 1")
    res = _run({"app/api.py": [(line, 1)]})

    assert res.kept == []
    assert res.stats["fp_attributed"] == 0
    assert res.stats["hunk_uncovered"] == 1


def test_multiple_hunks_same_entry_attributed_once() -> None:
    hunks = {
        "app/api.py": [
            (_line_of("def _helper(value):"), 1),
            (_line_of("    return value is not None"), 1),
        ]
    }
    res = _run(hunks)

    assert res.stats["fp_attributed"] == 1
    assert len(res.attributed_evidence) == 1


def test_route_body_change_needs_no_attribution() -> None:
    line = _line_of("    return _helper(payload)")
    res = _run({"app/api.py": [(line, 1)]})

    kept_ids = {fp.fp_id for fp in res.kept}
    assert kept_ids == {"FP::chat"}  # 路由自身被改动，功能点本来就在
    assert res.stats["fp_attributed"] == 0


def test_max_entries_caps_sibling_entries() -> None:
    hunks = {
        "app/api.py": [
            (_line_of("    return value is not None"), 1),
            (_line_of('    return value or "x"'), 1),
        ]
    }
    one = _run(hunks, max_entries=1)
    two = _run(hunks, max_entries=2)

    assert one.stats["fp_attributed"] == 1
    assert two.stats["fp_attributed"] == 2
    assert {fp.fp_id for fp in two.kept} == {"FP::chat", "FP::list_threads"}
