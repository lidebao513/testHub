"""T4 单测：前端文件级跨文件兜底（file_impact）。

守护点：
- 只对「目标 ∈ 本次 hunk 覆盖的页面/组件」的 import 边产点（api 层薄封装不参与）；
- 同一引用方 import 了多个被改文件 → 聚合成 1 条（不按边数膨胀）；
- 导入符号落在改动声明内 → precise（断言写成「因 X 改动」）；
- 对不上 → 退为文件级、置信度降档，断言如实说明「导入符号本身不在改动行内」；
- 每次 ≤ max_total（默认 2）；地址优先用引用方真实路由，无路由才退为文件路径。
"""

from __future__ import annotations

from pathlib import Path

from core.contracts import FunctionalPoint
from core.enums import Dimension, FType, MethodMarker, Tag, TPType, VerifyLayer
from engine import file_impact as FI
from engine import repo_facts as RF
from engine.scan import SourceFile


ROUTER = """\
<Routes>
  <Route path="/chat/:id?" element={<PcChatPage />} />
  <Route path="/pc/daily-perf" element={<DailyPerfPage />} />
  <Route path="/pc/invest" element={<InvestPage />} />
</Routes>
"""

CHANGED = """\
import { useState } from 'react';

export function PcComposer({ value }) {
  const [text, setText] = useState(value);
  return text;
}

export default function PcChatPage() {
  const onSessionCreated = useCallback(() => {}, []);
  return null;
}
"""

CHANGED_REL = "src/pages/pc/PcChatPage.tsx"


def _sf(rel: str, text: str) -> SourceFile:
    return SourceFile(rel=rel, abspath=Path(rel), text=text)


def _facts() -> RF.RepoFacts:
    return RF.build_repo_facts(
        {
            "src/App.tsx": _sf("src/App.tsx", ROUTER),
            CHANGED_REL: _sf(CHANGED_REL, CHANGED),
            "src/pages/DailyPerfPage.tsx": _sf(
                "src/pages/DailyPerfPage.tsx",
                "import { PcComposer } from './pc/PcChatPage';\n"
                "import PcChatPage from './pc/PcChatPage';\n",
            ),
            "src/pages/InvestPage.tsx": _sf(
                "src/pages/InvestPage.tsx", "import { PcComposer } from './pc/PcChatPage';\n"
            ),
            "src/pages/pc/PcOtherPage.tsx": _sf(
                "src/pages/pc/PcOtherPage.tsx", "import { PcComposer } from './PcChatPage';\n"
            ),
        }
    )


def _files() -> dict[str, SourceFile]:
    return {CHANGED_REL: _sf(CHANGED_REL, CHANGED)}


def _changed_fp() -> FunctionalPoint:
    return FunctionalPoint(
        fp_id="FP-changed",
        ftype=FType.PAGE.value,
        file_path=CHANGED_REL,
        name=CHANGED_REL,
        title="屏幕 PcChatPage",
        module="pc",
        has_route=False,
    )


def _importer_fp(rel: str, route: str) -> FunctionalPoint:
    return FunctionalPoint(
        fp_id=f"FP-{route.strip('/')}",
        ftype=FType.PAGE.value,
        file_path=rel,
        name=route,
        title=f"页面{route}",
        module="pc",
        has_route=True,
    )


def _item(
    importer: str,
    *,
    symbols: tuple[str, ...] = ("PcComposer",),
    changed_symbols: tuple[str, ...] = ("PcChatPage",),
    route: str = "",
    changed: str = CHANGED_REL,
) -> FI.FileImpactItem:
    return FI.FileImpactItem(
        importer=importer,
        edges=[
            FI.FileImpactEdge(
                changed=changed,
                spec="./pc/PcChatPage",
                symbols=list(symbols),
                changed_symbols=list(changed_symbols),
            )
        ],
        route=route,
        changed_fp_id="FP-changed",
    )


# ---------------------------------------------------------------- 符号定位
def test_enclosing_symbols_finds_top_level_function() -> None:
    """组件体内的局部 const 不算：跨文件引用方 import 的是组件本身。"""
    assert FI.enclosing_symbols(CHANGED, [(9, 1)]) == ["PcChatPage"]
    assert FI.enclosing_symbols(CHANGED, [(4, 1)]) == ["PcComposer"]


def test_enclosing_symbols_keeps_top_level_const() -> None:
    assert FI.enclosing_symbols("const a = 1\n", [(1, 1)]) == ["a"]


# ---------------------------------------------------------------- 影响项
def test_build_requires_hunks() -> None:
    assert FI.build_file_impact(_facts(), hunks={}, files=_files(), fps=[_changed_fp()]) == []


def test_build_caps_items() -> None:
    items = FI.build_file_impact(
        _facts(),
        hunks={CHANGED_REL: [(9, 1)]},
        files=_files(),
        fps=[_changed_fp()],
        max_total=2,
    )
    assert len(items) == 2


def test_build_zero_budget_disables_channel() -> None:
    items = FI.build_file_impact(
        _facts(),
        hunks={CHANGED_REL: [(9, 1)]},
        files=_files(),
        fps=[_changed_fp()],
        max_total=0,
    )
    assert items == []


def test_items_are_grouped_by_importer() -> None:
    """DailyPerfPage 有两条 import 语句 → 仍然只出 1 条（按引用方聚合）。"""
    items = FI.build_file_impact(
        _facts(),
        hunks={CHANGED_REL: [(9, 1)]},
        files=_files(),
        fps=[_changed_fp()],
        max_total=10,
    )
    importers = [i.importer for i in items]
    assert len(importers) == len(set(importers)) == 3
    assert all(len(i.edges) >= 1 for i in items)


def test_api_layer_files_are_not_file_level_impacts() -> None:
    """api 薄封装被全站 import → 不参与文件级兜底（交给接口影响面），避免刷屏。"""
    api_rel = "src/api/adapter.ts"
    facts = RF.build_repo_facts(
        {
            api_rel: _sf(api_rel, "export function threadToSession(t){ return t }\n"),
            "src/pages/DailyPerfPage.tsx": _sf(
                "src/pages/DailyPerfPage.tsx",
                "import { threadToSession } from '../api/adapter';\n",
            ),
        }
    )
    items = FI.build_file_impact(
        facts,
        hunks={api_rel: [(1, 1)]},
        files={api_rel: _sf(api_rel, "export function threadToSession(t){ return t }\n")},
        fps=[_changed_fp()],
        max_total=5,
    )
    assert items == []


def test_route_having_importer_ranks_first() -> None:
    items = FI.build_file_impact(
        _facts(),
        hunks={CHANGED_REL: [(9, 1)]},
        files=_files(),
        fps=[_changed_fp()],
        max_total=3,
    )
    assert items[0].route  # 有真实路由的排前面（地址可直接执行）
    assert all(i.route for i in items[:2])


def test_precise_when_symbol_is_changed() -> None:
    """hunk 落在 PcComposer 体内 → 导入了 PcComposer 的边判为 precise。"""
    items = FI.build_file_impact(
        _facts(),
        hunks={CHANGED_REL: [(4, 1)]},
        files=_files(),
        fps=[_changed_fp()],
        max_total=3,
    )
    assert items and all(i.precise for i in items)


def test_anchor_prefers_importer_fp() -> None:
    rel = "src/pages/DailyPerfPage.tsx"
    items = FI.build_file_impact(
        _facts(),
        hunks={CHANGED_REL: [(9, 1)]},
        files=_files(),
        fps=[_changed_fp(), _importer_fp(rel, "/pc/daily-perf")],
        max_total=5,
    )
    by_importer = {i.importer: i for i in items}
    assert by_importer[rel].anchor_fp_id == "FP-pc/daily-perf"


def test_anchor_falls_back_to_changed_fp() -> None:
    """引用方没被扫到（不在勾选范围）→ 挂到被改文件的功能点，避免孤儿用例。"""
    items = FI.build_file_impact(
        _facts(),
        hunks={CHANGED_REL: [(9, 1)]},
        files=_files(),
        fps=[_changed_fp()],
        max_total=5,
    )
    assert all(i.anchor_fp_id == "FP-changed" for i in items)


# ---------------------------------------------------------------- 测试点
def test_test_point_fields() -> None:
    rel = "src/pages/DailyPerfPage.tsx"
    items = FI.build_file_impact(
        _facts(),
        hunks={CHANGED_REL: [(9, 1)]},
        files=_files(),
        fps=[_changed_fp(), _importer_fp(rel, "/pc/daily-perf")],
        max_total=5,
    )
    tps = FI.file_impact_test_points(items)
    assert tps
    tp = next(t for t in tps if t.area == "/pc/daily-perf")
    assert tp.category == TPType.NORMAL.value
    assert tp.dimension == Dimension.IMPACT_FILE.value
    assert tp.tag == Tag.IMPACT_FILE.value
    assert tp.origin == "impact_file"
    assert tp.verify_layer == VerifyLayer.UI.value
    assert tp.method == MethodMarker.PAGE.value
    assert tp.expect and "控制台无新增报错" in tp.expect
    assert tp.evidence and CHANGED_REL in tp.evidence[0]


def test_test_point_uses_ui_marker_without_route() -> None:
    """引用方查不到路由 → 地址退为文件路径，method 标 UI（不冒充页面可达）。"""
    facts = RF.build_repo_facts(
        {
            CHANGED_REL: _sf(CHANGED_REL, CHANGED),
            "src/components/Reuse.tsx": _sf(
                "src/components/Reuse.tsx", "import { PcComposer } from '../pages/pc/PcChatPage';\n"
            ),
        }
    )
    items = FI.build_file_impact(
        facts,
        hunks={CHANGED_REL: [(9, 1)]},
        files=_files(),
        fps=[_changed_fp()],
        max_total=2,
    )
    tp = FI.file_impact_test_points(items)[0]
    assert tp.method == MethodMarker.UI.value
    assert tp.area == "src/components/Reuse.tsx"


def test_expect_distinguishes_precise_and_module_level() -> None:
    precise = _item("/pc/daily-perf", changed_symbols=("PcComposer",))
    vague = _item("/pc/daily-perf")
    tp_precise = FI.file_impact_test_points([precise])[0]
    tp_vague = FI.file_impact_test_points([vague])[0]
    assert "被修改" in tp_precise.expect
    assert "模块级影响" in tp_vague.expect
    assert tp_precise.confidence > tp_vague.confidence


def test_multiple_targets_are_merged_into_one_expect() -> None:
    """同一引用方 import 了两个被改文件 → 断言里同时点名，不拆成两条用例。"""
    item = FI.FileImpactItem(
        importer="src/pages/DailyPerfPage.tsx",
        edges=[
            FI.FileImpactEdge(changed="src/pages/pc/PcChatPage.tsx", symbols=["PcComposer"]),
            FI.FileImpactEdge(changed="src/components/Card.tsx", symbols=["Card"]),
        ],
        route="/pc/daily-perf",
        changed_fp_id="FP-changed",
    )
    tp = FI.file_impact_test_points([item])[0]
    assert "PcChatPage" in tp.expect and "Card" in tp.expect
    assert len(tp.evidence) >= 2


def test_no_anchor_no_point() -> None:
    orphan = FI.FileImpactItem(importer="src/pages/A.tsx")
    assert FI.file_impact_test_points([orphan]) == []
