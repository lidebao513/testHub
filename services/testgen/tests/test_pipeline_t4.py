"""T4 单测（编排层）：路由反查与文件级影响面在 pipeline 里的接线。

这两步都在 `stage_test_points` 内部完成，单测模块层通过不代表编排层通过——
实现中曾把 `FileImpactItem.changed`（已改为聚合后的 `targets`）写进备注，
模块单测全绿、端到端直接 failed：「'FileImpactItem' object has no attribute 'changed'」。
本文件把「编排 + 备注 + counts」一起锁住。
"""

from __future__ import annotations

from pathlib import Path

from core.contracts import FunctionalPoint
from core.enums import FType, TPType
from engine import pipeline
from engine import repo_facts as RF
from engine.scan import SourceFile


ROUTER = """\
import PcChatPage from '@/pages/pc/PcChatPage';
import DailyPerfPage from '@/pages/DailyPerfPage';
<Routes>
  <Route path="/chat/:id?" element={<PcChatPage />} />
  <Route path="/pc/daily-perf" element={<DailyPerfPage />} />
</Routes>
"""

CHANGED_REL = "src/pages/pc/PcChatPage.tsx"
IMPORTER_REL = "src/pages/DailyPerfPage.tsx"
WIDGET_REL = "src/pages/Widget.tsx"
UNBOUND_REL = "src/pages/Orphan.tsx"
CHANGED_TEXT = "export function PcComposer(){ return null }\nexport default function PcChatPage(){ return null }\n"


def _sf(rel: str, text: str) -> SourceFile:
    return SourceFile(rel=rel, abspath=Path(rel), text=text)


def _facts() -> RF.RepoFacts:
    return RF.build_repo_facts(
        {
            "src/App.tsx": _sf("src/App.tsx", ROUTER),
            CHANGED_REL: _sf(CHANGED_REL, CHANGED_TEXT),
            IMPORTER_REL: _sf(
                IMPORTER_REL,
                "import { PcComposer } from './pc/PcChatPage';\nimport Widget from './Widget';\n",
            ),
            WIDGET_REL: _sf(WIDGET_REL, "export default function Widget(){ return null }\n"),
            UNBOUND_REL: _sf(UNBOUND_REL, "export default function Orphan(){ return null }\n"),
        }
    )


def _page_fp(rel: str, *, has_route: bool, route: str = "") -> FunctionalPoint:
    return FunctionalPoint(
        fp_id=f"FP-{Path(rel).stem}",
        ftype=FType.PAGE.value,
        file_path=rel,
        name=route or rel,
        title=f"屏幕 {Path(rel).stem}",
        module="pc",
        has_route=has_route,
        route_hint=route,
    )


def _result(fps: list[FunctionalPoint]) -> pipeline.PipelineResult:
    return pipeline.PipelineResult(
        project_id=30,
        mode="incremental",
        has_code=True,
        files={CHANGED_REL: _sf(CHANGED_REL, CHANGED_TEXT)},
        functional_points=fps,
        repo_facts=_facts(),
        diff_hunks={CHANGED_REL: [(1, 1)]},
    )


def _opts() -> pipeline.PipelineOptions:
    opts = pipeline.default_options("", mode="incremental")
    opts.scopes = {TPType.NORMAL.value, TPType.ABNORMAL.value, TPType.BOUNDARY.value}
    return opts


def test_infer_page_routes_sets_hint_and_note() -> None:
    result = _result([_page_fp(CHANGED_REL, has_route=False)])
    pipeline._infer_page_routes(_opts(), result)

    fp = result.functional_points[0]
    assert fp.has_route is True
    assert fp.route_hint == "/chat"
    assert fp.route_source == "inferred"
    assert result.counts["route_inferred"] == 1
    assert any("路由推断" in n for n in result.notes)


def test_infer_page_routes_skips_pages_with_declared_route() -> None:
    result = _result([_page_fp(CHANGED_REL, has_route=True, route="/chat")])
    pipeline._infer_page_routes(_opts(), result)
    assert result.counts.get("route_inferred", 0) == 0


def test_infer_page_routes_off_when_not_incremental() -> None:
    opts = _opts()
    opts.mode = "full"
    result = _result([_page_fp(CHANGED_REL, has_route=False)])
    pipeline._infer_page_routes(opts, result)
    assert result.functional_points[0].has_route is False


def test_append_file_impact_produces_point_and_note() -> None:
    result = _result([_page_fp(CHANGED_REL, has_route=True, route="/chat")])
    tps = pipeline._append_file_impact(_opts(), result, [])

    assert len(tps) == 1
    assert tps[0].origin == "impact_file"
    assert tps[0].area == "/pc/daily-perf"
    assert result.counts["file_impact_items"] == 1
    assert any("文件级影响面" in n for n in result.notes)


def test_append_file_impact_requires_normal_scope() -> None:
    opts = _opts()
    opts.scopes = {TPType.SECURITY.value}
    result = _result([_page_fp(CHANGED_REL, has_route=True, route="/chat")])
    assert pipeline._append_file_impact(opts, result, []) == []


def test_append_file_impact_tolerates_missing_facts() -> None:
    result = _result([_page_fp(CHANGED_REL, has_route=True, route="/chat")])
    result.repo_facts = None
    assert pipeline._append_file_impact(_opts(), result, []) == []


class _OffCfg:
    """开关全关的配置替身：验证「关闭后与基线一致」。"""

    route_infer = False
    route_file_hint = False
    file_impact_enabled = False
    file_impact_max_total = 2


def test_infer_page_routes_respects_switch(monkeypatch) -> None:
    opts = _opts()  # 先构造（default_options 会读真实配置），再替换开关替身
    monkeypatch.setattr(pipeline, "get_settings", lambda: _OffCfg())
    result = _result([_page_fp(CHANGED_REL, has_route=False)])
    pipeline._infer_page_routes(opts, result)
    fp = result.functional_points[0]
    assert fp.has_route is False and fp.route_hint == ""
    assert result.counts.get("route_inferred", 0) == 0


def test_file_impact_respects_switch(monkeypatch) -> None:
    opts = _opts()
    monkeypatch.setattr(pipeline, "get_settings", lambda: _OffCfg())
    result = _result([_page_fp(CHANGED_REL, has_route=True, route="/chat")])
    assert pipeline._append_file_impact(opts, result, []) == []


# ---------------------------------------------------------------- 批次 B：文件级屏幕补地址
def test_file_bound_hint_sets_address_without_opening_dimensions() -> None:
    result = _result([_page_fp(CHANGED_REL, has_route=False)])
    pipeline._hint_file_bound_routes(_opts(), result)
    fp = result.functional_points[0]
    assert fp.route_hint == "/chat"
    assert fp.route_source == "file_bound"
    assert fp.has_route is False, "不能放开地址类维度：全量模式会成倍放大用例"
    assert result.counts["route_file_hint"] == 1


def test_file_bound_hint_via_parent_page() -> None:
    result = _result([_page_fp(WIDGET_REL, has_route=False)])
    pipeline._hint_file_bound_routes(_opts(), result)
    fp = result.functional_points[0]
    assert fp.route_hint == "/pc/daily-perf"
    assert fp.route_source == "parent_page"
    assert result.counts["route_parent_hint"] == 1


def test_file_bound_hint_counts_unbound_without_guessing() -> None:
    result = _result([_page_fp(UNBOUND_REL, has_route=False)])
    pipeline._hint_file_bound_routes(_opts(), result)
    fp = result.functional_points[0]
    assert fp.route_hint == "" and fp.has_route is False
    assert result.counts["route_unbound_screens"] == 1
    assert any("仍无路由证据 1 条" in n for n in result.notes)


def test_file_bound_hint_respects_switch(monkeypatch) -> None:
    opts = _opts()  # 先构造（default_options 会读真实配置），再替换开关替身
    monkeypatch.setattr(pipeline, "get_settings", lambda: _OffCfg())
    result = _result([_page_fp(CHANGED_REL, has_route=False)])
    pipeline._hint_file_bound_routes(opts, result)
    assert result.functional_points[0].route_hint == ""
    assert result.counts.get("route_file_hint", 0) == 0
