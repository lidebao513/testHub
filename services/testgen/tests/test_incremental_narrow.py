"""增量收窄单测（方案 A 函数级 + 方案 B 前端维度收敛）。

覆盖：
- 后端功能点：函数体与 hunk 相交则保留，否则过滤（未变更函数不产出用例）；
- 函数体中段改动（def 行不在 hunk）仍能正确保留（get_thread 场景）；
- 前端/无行号功能点：保留但标记收敛维度，不删除；
- hunks 为空 / 全量：零过滤（向后兼容）；
- 维度收敛：narrow_fp_ids 命中的功能点展开的测试点数 < 完整展开。
"""

from pathlib import Path

from core.contracts import FunctionalPoint
from core.enums import FType
from engine import incremental_narrow as IN
from engine import tp_expand
from engine.scan import SourceFile


_SRC = '''\
def unchanged_func():
    """未变更函数。"""
    return 1


def changed_func():
    """本次改动的函数。"""
    a = 1
    b = 2
    return a + b


class Handler:
    def method_changed(self):
        x = 10
        return x
'''


def _sf() -> SourceFile:
    return SourceFile(rel="app/svc.py", abspath=Path("app/svc.py"), text=_SRC)


def _fp(name: str, line: int, *, ftype: str = FType.BUSINESS.value) -> FunctionalPoint:
    return FunctionalPoint(
        fp_id=f"FP::{name}",
        ftype=ftype,
        file_path="app/svc.py",
        name=name,
        title=name,
        description=f"{name}() @ app/svc.py:{line}",
    )


def _frontend_fp(name: str = "PcShell") -> FunctionalPoint:
    return FunctionalPoint(
        fp_id=f"FP::{name}",
        ftype=FType.PAGE.value,
        file_path="frontend/pc/PcShell.tsx",
        name="/pc/shell",
        title=name,
        description="frontend/pc/PcShell.tsx",  # 前端无行号
    )


def _line_of(fn_name: str) -> int:
    for i, ln in enumerate(_SRC.splitlines(), 1):
        if ln.strip().startswith(f"def {fn_name}("):
            return i
    raise AssertionError(fn_name)


def test_unchanged_function_dropped_changed_kept():
    """未变更函数被过滤，变更函数保留。"""
    files = {"app/svc.py": _sf()}
    fps = [
        _fp("unchanged_func", _line_of("unchanged_func")),
        _fp("changed_func", _line_of("changed_func")),
        _fp("method_changed", _line_of("method_changed")),
    ]
    # hunk 命中 changed_func 的 return 行（函数体中段）与 method_changed
    hunks = {"app/svc.py": [(_line_of("changed_func") + 3, 2), (_line_of("method_changed") + 1, 1)]}
    r = IN.narrow_to_changed_functions(fps, files, hunks)
    kept_ids = {f.fp_id for f in r.kept}
    assert "FP::unchanged_func" not in kept_ids
    assert "FP::changed_func" in kept_ids
    assert "FP::method_changed" in kept_ids
    assert "FP::unchanged_func" in r.dropped_fps
    assert r.stats["fp_dropped_unchanged"] == 1


def test_change_in_function_body_keeps_whole_function():
    """def 行不在 hunk、变更在函数体中段（get_thread 场景）仍保留。"""
    files = {"app/svc.py": _sf()}
    fp = _fp("changed_func", _line_of("changed_func"))
    # hunk 只在函数体尾部，不含 def 行
    hunks = {"app/svc.py": [(_line_of("changed_func") + 4, 1)]}
    r = IN.narrow_to_changed_functions([fp], files, hunks)
    assert r.kept == [fp]
    assert not r.dropped_fps


def test_frontend_fp_kept_and_marked_narrow():
    """前端无行号功能点保留并标记收敛维度，不删除。"""
    files = {"app/svc.py": _sf()}
    fe = _frontend_fp()
    fe.ftype = FType.PAGE.value
    fe.description = "frontend/pc/PcShell.tsx"
    r = IN.narrow_to_changed_functions([fe], files, {"app/svc.py": [(1, 1)]})
    assert r.kept == [fe]
    assert fe.fp_id in r.narrow_fp_ids
    assert not r.dropped_fps


def test_no_hunks_no_drop_backward_compatible():
    """拿不到 hunk 行号时零过滤（向后兼容）。"""
    files = {"app/svc.py": _sf()}
    fps = [_fp("unchanged_func", _line_of("unchanged_func"))]
    r = IN.narrow_to_changed_functions(fps, files, {})
    assert r.kept == fps
    assert not r.dropped_fps


def test_narrow_dimensions_reduces_test_points():
    """收敛维度命中的功能点展开测试点数 < 完整展开（方案 B 生效）。"""
    fp = _frontend_fp()
    full = tp_expand.expand_functional_point(
        fp, tp_expand.ExpandContext(scopes={"正常", "异常", "安全", "边界"})
    )
    narrow_ctx = tp_expand.ExpandContext(
        scopes={"正常", "异常", "安全", "边界"}, narrow_fp_ids={fp.fp_id}
    )
    narrow = tp_expand.expand_functional_point(fp, narrow_ctx)
    assert len(narrow) < len(full)
    assert len(full) >= 5  # PAGE 原始展开含可达/未授权/视口/异常流等
    assert all(tp.category in {"正常", "安全"} for tp in narrow)


def test_narrow_never_drops_no_route_page_to_zero():
    """无路由页面（has_route=False）被收敛时不能变 0 条。

    回归现场（project 30，2026-11-18 14:55）：增量只保留 2 个前端页面功能点，
    它们没有路由声明 → 完整 plan 只有「交互元素可用」，不在 PAGE 收敛白名单里，
    交集为空 → 展开 0 条测试点，面板弹「本次生成 0 条」。
    """
    fp = FunctionalPoint(
        fp_id="FP::PcChatPage",
        ftype=FType.PAGE.value,
        file_path="frontend/app/src/pages/pc/PcChatPage.tsx",
        name="frontend/app/src/pages/pc/PcChatPage.tsx",
        title="pc · 屏幕 PcChatPage",
        description="frontend/app/src/pages/pc/PcChatPage.tsx",
        has_route=False,
    )
    ctx = tp_expand.ExpandContext(scopes={"正常", "异常", "安全", "边界"}, narrow_fp_ids={fp.fp_id})
    tps = tp_expand.expand_functional_point(fp, ctx)
    assert len(tps) == 1, "变更命中的无路由页面至少保留 1 条核心维度用例"
    assert tps[0].category == "正常"


def test_hunks_from_compare_diffs():
    """Codeup compare diff 文本能解析成 hunk 行号（沙箱 git 不可用时的来源）。"""
    diffs = [
        {
            "newPath": "backend/svc.py",
            "deletedFile": False,
            "diff": "--- a/backend/svc.py\n+++ b/backend/svc.py\n"
            "@@ -10,2 +10,3 @@ def foo():\n ctx\n+new\n"
            "@@ -50 +50,2 @@ def bar():\n-old\n+new1\n+new2\n",
        },
        {"newPath": "deleted.py", "deletedFile": True, "diff": "@@ -1 +0,0 @@"},
    ]
    hunks = IN.hunks_from_compare_diffs(diffs)
    assert hunks == {"backend/svc.py": [(10, 3), (50, 2)]}
    assert "deleted.py" not in hunks


def test_narrow_uses_compare_hunks_to_drop_unchanged():
    """端到端：用 compare 解析的 hunk 收窄，未变更函数被过滤。"""
    files = {"app/svc.py": _sf()}
    fps = [
        _fp("unchanged_func", _line_of("unchanged_func")),
        _fp("changed_func", _line_of("changed_func")),
    ]
    diff_text = (
        "--- a/app/svc.py\n+++ b/app/svc.py\n"
        f"@@ -{_line_of('changed_func') + 3},2 +{_line_of('changed_func') + 3},2 @@\n ctx\n+new\n"
    )
    hunks = IN.hunks_from_compare_diffs([{"newPath": "app/svc.py", "diff": diff_text}])
    r = IN.narrow_to_changed_functions(fps, files, hunks)
    assert [f.fp_id for f in r.kept] == ["FP::changed_func"]
    assert r.dropped_fps == ["FP::unchanged_func"]


def test_full_mode_no_narrow():
    """全量模式 stages 不收窄（向后兼容）：stage_narrow 第一行直接返回。"""
    from engine import pipeline

    opts = pipeline.PipelineOptions(mode="full")
    opts.has_code = True
    result = pipeline.PipelineResult(mode="full")
    result.has_code = True
    fps = [_fp("unchanged_func", _line_of("unchanged_func"))]
    result.functional_points = list(fps)
    result.files = {"app/svc.py": _sf()}
    pipeline.stage_narrow(opts, result, None)
    assert result.functional_points == fps
    assert result.narrow_fp_ids == set()
