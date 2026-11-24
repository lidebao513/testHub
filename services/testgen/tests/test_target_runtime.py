"""增量 + 地址通道的「变更靶向」收窄（stage_target_runtime）单测。

缺陷背景：增量模式只约束代码通道，给了被测地址就整站巡检——实测「改 5 个文件出 294 条
用例」，其中 292 条与本次变更无关。本阶段负责按变更把运行时功能点收窄。
"""

from __future__ import annotations

from core.contracts import FunctionalPoint
from engine import pipeline, runtime_ui


def _fp(ftype: str, name: str, file_path: str, title: str = "") -> FunctionalPoint:
    return FunctionalPoint(
        fp_id=f"{ftype}:{name}",
        ftype=ftype,
        file_path=file_path,
        name=name,
        title=title or name,
        module="m",
    )


def _runtime() -> runtime_ui.RuntimeUiResult:
    return runtime_ui.RuntimeUiResult(
        base_url="http://localhost:3000",
        pages=[
            runtime_ui.UiPage(url="http://localhost:3000/chat", path="/chat", title="会话"),
            runtime_ui.UiPage(url="http://localhost:3000/pc/tasks", path="/pc/tasks", title="任务"),
        ],
        api_endpoints=[
            runtime_ui.ApiEndpoint(method="GET", path="/api/v1/threads", status=200),
            runtime_ui.ApiEndpoint(method="GET", path="/api/v1/memories", status=200),
        ],
    )


def _result() -> pipeline.PipelineResult:
    res = pipeline.PipelineResult(mode="incremental")
    res.runtime_ui = _runtime()
    res.functional_points = [
        _fp("api", "GET /api/v1/threads", "runtime:http://localhost:3000/", "会话列表"),
        _fp("api", "GET /api/v1/memories", "runtime:http://localhost:3000/", "记忆列表"),
        _fp("page", "/chat", "runtime:http://localhost:3000/chat", "会话页"),
        _fp(
            "ui",
            "area::会话#button:新建会话|#new",
            "runtime:http://localhost:3000/chat",
            "新建会话",
        ),
        _fp("page", "/pc/tasks", "runtime:http://localhost:3000/pc/tasks", "任务页"),
        _fp(
            "ui",
            "area::任务#button:新建任务|#x",
            "runtime:http://localhost:3000/pc/tasks",
            "新建任务",
        ),
        _fp("ui", "area::年休假#input:开始时间|#s", "runtime:area::年休假", "开始时间"),
        _fp("business", "listThreadsPage", "frontend/app/src/api/backend.ts", "会话分页"),
    ]
    return res


def _opts(changed: list[str], *, target: bool = True):
    opts = pipeline.default_options("", mode="incremental")
    opts.target_runtime = target
    opts.changed_files = changed
    opts.base = "base-sha"
    opts.target = "target-sha"
    return opts


_API_SRC = """
export function listThreadsPage(channel, offset, limit = 20) {
  return apiJson(`/api/v1/threads?offset=${offset}&limit=${limit}`);
}
"""


def test_targeted_keeps_only_changed_page_and_endpoints():
    res = _result()
    opts = _opts(["frontend/app/src/api/backend.ts"])
    res.files = {"frontend/app/src/api/backend.ts": _API_SRC}
    pipeline.stage_target_runtime(opts, res, None)

    names = sorted(fp.name for fp in res.functional_points)
    assert "GET /api/v1/threads" in names
    assert "GET /api/v1/memories" not in names  # 无关接口被剔除
    # 只改了 api 层文件、没有页面变更 → 页面/元素也一并剔除（memories + /chat 页 + 其元素
    # + /pc/tasks 页 + 其元素 + area 面板区域 = 6），保留 1 条接口
    assert res.counts["runtime_fp_targeted"] == 1
    assert res.counts["runtime_fp_skipped_by_target"] == 6
    # 代码通道（business）功能点不受影响
    assert "listThreadsPage" in names
    assert any("变更靶向" in n for n in res.notes)


def test_targeting_disabled_keeps_everything():
    res = _result()
    before = len(res.functional_points)
    opts = _opts(["frontend/app/src/api/backend.ts"], target=False)
    res.files = {"frontend/app/src/api/backend.ts": _API_SRC}
    pipeline.stage_target_runtime(opts, res, None)
    assert len(res.functional_points) == before
    assert "runtime_fp_skipped_by_target" not in res.counts


def test_full_mode_without_changes_is_not_targeted():
    """纯全站巡检（full 且无变更集）不启动靶向，保留全部功能点。"""
    res = _result()
    before = len(res.functional_points)
    opts = pipeline.default_options("", mode="full")
    opts.target_runtime = True
    # 刻意不给 changed_files：全站巡检场景
    pipeline.stage_target_runtime(opts, res, None)
    assert len(res.functional_points) == before
    assert "runtime_fp_skipped_by_target" not in res.counts


def test_full_mode_with_changed_files_is_targeted():
    """full + 显式变更文件：地址为主也能按变更收窄（阶段一 #435）。"""
    res = _result()
    opts = pipeline.default_options("", mode="full")
    opts.target_runtime = True
    opts.changed_files = ["frontend/app/src/api/backend.ts"]
    res.files = {"frontend/app/src/api/backend.ts": _API_SRC}
    pipeline.stage_target_runtime(opts, res, None)
    names = sorted(fp.name for fp in res.functional_points)
    assert "GET /api/v1/threads" in names
    assert "GET /api/v1/memories" not in names
    assert res.counts["runtime_fp_targeted"] == 1
    assert res.counts["runtime_fp_skipped_by_target"] == 6
    assert "listThreadsPage" in names  # 代码通道不受影响


def test_full_mode_with_changes_outside_selection_is_not_targeted():
    """变更集被 selected_paths 过滤为空时视为无靶向输入，不误伤全站巡检。"""
    res = _result()
    before = len(res.functional_points)
    opts = pipeline.default_options("", mode="full")
    opts.target_runtime = True
    opts.changed_files = ["frontend/app/src/api/backend.ts"]
    opts.selected_paths = ["docs/"]  # 与变更集无交集
    res.files = {"frontend/app/src/api/backend.ts": _API_SRC}
    pipeline.stage_target_runtime(opts, res, None)
    assert len(res.functional_points) == before
    assert "runtime_fp_skipped_by_target" not in res.counts


def test_empty_plan_drops_all_runtime_fps_but_keeps_code_fps():
    """改的是样式/配置：没有可映射目标 → 剔除全部 URL 功能点并如实说明。"""
    res = _result()
    opts = _opts(["frontend/app/src/styles/main.css"])
    res.files = {"frontend/app/src/styles/main.css": ".a{color:red}"}
    pipeline.stage_target_runtime(opts, res, None)

    left = [fp for fp in res.functional_points if fp.file_path.startswith("runtime:")]
    assert left == []
    assert [fp.name for fp in res.functional_points] == ["listThreadsPage"]
    assert any("未命中" in n for n in res.notes)
