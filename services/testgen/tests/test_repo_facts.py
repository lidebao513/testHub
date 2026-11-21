"""T4 单测：仓库级事实（静态路由表 + 前端 import 边）。

守护点（每条对应一个真实踩过的坑）：
- 嵌套 `element={<Wrap><Page /></Wrap>}` 必须把真正的页面组件也绑上（正则会截断在第一个 `>`）；
- 相对子路由（`path="daily-perf"` 嵌在 `path="/pc"` 下）一律不采信——还原不出父前缀就会
  写出 `/daily-perf` 这种不存在的地址；
- 非法路由文件（mock/表单里的 `path:`）不算路由声明；
- import 边要能同时解析相对路径与 `@/` 别名，并排除路由/壳文件与测试夹具。
"""

from __future__ import annotations

from pathlib import Path

from engine import repo_facts as RF
from engine.scan import SourceFile


def _sf(rel: str, text: str) -> SourceFile:
    return SourceFile(rel=rel, abspath=Path(rel), text=text)


APP_ROUTER = """\
import { Routes, Route } from 'react-router';
import PcShell from '@/pages/pc/PcShell';
import PcChatPage from '@/pages/pc/PcChatPage';
const DailyPerfPage = lazy(() => import('@/pages/DailyPerfPage'));
const MobileInvestHomePage = lazy(() => import('@/pages/mobile/MobileInvestHomePage'));
export default function App() {
  return (
    <Routes>
      <Route path="/" element={<Navigate to="/chat" replace />} />
      <Route element={<PcShell />}>
        <Route path="/chat/:id?" element={<PcChatPage />} />
        <Route path="/pc/chat/:id?" element={<PcChatPage />} />
        <Route path="/pc/daily-perf" element={<DailyPerfPage />} />
        <Route path="/pc/research" element={<PcWrap width="wide"><MobileInvestHomePage /></PcWrap>} />
        <Route path="daily-perf" element={<DailyPerfPage />} />
      </Route>
    </Routes>
  );
}
"""


def _facts() -> RF.RepoFacts:
    files = {
        "frontend/app/src/App.tsx": _sf("frontend/app/src/App.tsx", APP_ROUTER),
        "frontend/app/src/pages/pc/PcChatPage.tsx": _sf(
            "frontend/app/src/pages/pc/PcChatPage.tsx",
            "export function PcComposer(){ return null }\nexport default function PcChatPage(){ return null }\n",
        ),
        "frontend/app/src/pages/pc/PcShell.tsx": _sf(
            "frontend/app/src/pages/pc/PcShell.tsx",
            "export default function PcShell(){ return null }\n",
        ),
        "frontend/app/src/pages/DailyPerfPage.tsx": _sf(
            "frontend/app/src/pages/DailyPerfPage.tsx",
            "import { PcComposer } from '@/pages/pc/PcChatPage';\nexport default function DailyPerfPage(){ return null }\n",
        ),
        "frontend/app/src/pages/mobile/MobileInvestHomePage.tsx": _sf(
            "frontend/app/src/pages/mobile/MobileInvestHomePage.tsx",
            "import { PcComposer } from '../pc/PcChatPage';\nexport default function MobileInvestHomePage(){ return null }\n",
        ),
        "frontend/app/tests/harness.tsx": _sf(
            "frontend/app/tests/harness.tsx",
            "import PcChatPage from '../src/pages/pc/PcChatPage';\n",
        ),
        "frontend/app/src/mock/chat.ts": _sf(
            "frontend/app/src/mock/chat.ts",
            "export const menu = [{ path: '/not-a-route', component: FakeComp }];\n",
        ),
    }
    return RF.build_repo_facts(files)


# ---------------------------------------------------------------- 路由归一化
def test_normalize_route_drops_param_segments() -> None:
    assert RF.normalize_route("/chat/:id?") == "/chat"
    assert RF.normalize_route("/pc/chat/:threadId/") == "/pc/chat"
    assert RF.normalize_route("/pc/list?tab=1") == "/pc/list"


def test_normalize_route_rejects_param_only_paths() -> None:
    """全是参数段的路由给不出稳定前缀 → 空串（避免误绑成根路由）。"""
    assert RF.normalize_route("/:id") == ""
    assert RF.normalize_route("/*") == ""


def test_normalize_route_keeps_root() -> None:
    assert RF.normalize_route("/") == "/"


# ---------------------------------------------------------------- 路由表
def test_jsx_binding_includes_nested_component() -> None:
    """`element={<PcWrap><MobileInvestHomePage /></PcWrap>}` 两个组件都要绑上。"""
    facts = _facts()
    inner = facts.route_of_component("MobileInvestHomePage")
    assert inner is not None
    assert inner.path == "/pc/research"


def test_relative_child_route_is_ignored() -> None:
    """相对子路由（`path="daily-perf"`）不采信 → 只留绝对声明 `/pc/daily-perf`。"""
    facts = _facts()
    hit = facts.route_of_component("DailyPerfPage")
    assert hit is not None
    assert hit.path == "/pc/daily-perf"


def test_layout_route_without_path_produces_no_binding() -> None:
    facts = _facts()
    assert facts.route_of_component("PcShell") is None


def test_non_router_file_is_not_a_route_source() -> None:
    """mock/配置里的 `path:`/`component:` 不是路由声明。"""
    facts = _facts()
    assert all(b.source.endswith("App.tsx") for b in facts.routes)


def test_object_style_binding_is_parsed() -> None:
    text = "export default [{ path: '/pc/board', component: BoardPage }];"
    facts = RF.build_repo_facts({"src/router.ts": _sf("src/router.ts", text)})
    hit = facts.route_of_component("BoardPage")
    assert hit is not None and hit.path == "/pc/board"


def test_component_with_multiple_routes_picks_shortest() -> None:
    facts = _facts()
    hit = facts.route_of_component("PcChatPage")
    assert hit is not None and hit.path == "/chat"


# ---------------------------------------------------------------- 路由反查
def test_infer_route_by_component_name() -> None:
    facts = _facts()
    hit = RF.infer_route("frontend/app/src/pages/pc/PcChatPage.tsx", facts)
    assert hit is not None and hit.path == "/chat"
    assert "App.tsx" in hit.evidence()


def test_infer_route_falls_back_to_filename_candidates() -> None:
    """组件名对不上（改名/包装）时，用文件名候选去匹配已声明路由。"""
    text = "<Route path='/pc/chat/:id?' element={<ChatWrapper />} />"
    facts = RF.build_repo_facts({"src/App.tsx": _sf("src/App.tsx", text)})
    hit = RF.infer_route("src/pages/pc/PcChatPage.tsx", facts)
    assert hit is not None and hit.path == "/pc/chat"


def test_infer_route_returns_none_without_evidence() -> None:
    facts = _facts()
    assert RF.infer_route("frontend/app/src/pages/Unknown.tsx", facts) is None


def test_infer_route_skips_shell_files() -> None:
    """壳/布局文件按约定不产出文件名候选（否则一个壳会把全站页面算成变更相关）。"""
    facts = _facts()
    assert RF.infer_route("frontend/app/src/pages/pc/PcShell.tsx", facts) is None


def test_infer_route_uses_runtime_pages() -> None:
    facts = RF.build_repo_facts({})
    hit = RF.infer_route("src/pages/pc/PcChatPage.tsx", facts, runtime_pages=["/pc/chat/123"])
    assert hit is not None and hit.path == "/pc/chat"


# ---------------------------------------------------------------- import 边
def test_importers_resolve_alias_and_relative() -> None:
    facts = _facts()
    importers = {s.importer for s in facts.importers_of("frontend/app/src/pages/pc/PcChatPage.tsx")}
    assert importers == {
        "frontend/app/src/pages/DailyPerfPage.tsx",
        "frontend/app/src/pages/mobile/MobileInvestHomePage.tsx",
    }


def test_importers_exclude_router_and_test_files() -> None:
    """App.tsx（路由表）与 tests/ 夹具不算「受影响父组件」。"""
    facts = _facts()
    importers = {s.importer for s in facts.importers_of("frontend/app/src/pages/pc/PcChatPage.tsx")}
    assert "frontend/app/src/App.tsx" not in importers
    assert "frontend/app/tests/harness.tsx" not in importers


def test_import_symbols_are_captured() -> None:
    facts = _facts()
    specs = facts.importers_of("frontend/app/src/pages/pc/PcChatPage.tsx")
    assert all(s.symbols == ("PcComposer",) for s in specs)


def test_build_skips_noise_files() -> None:
    facts = _facts()
    assert "frontend/app/tests/harness.tsx" not in facts.imports


def test_stats_are_reported() -> None:
    facts = _facts()
    assert facts.stats["repo_route_bindings"] >= 4
    assert facts.stats["repo_import_specs"] >= 4
    assert facts.stats["repo_import_truncated"] == 0


# ---------------------------------------------------------------- 批次 B：别名/嵌套路由
_VUE_ROUTER = """\
import { createRouter } from 'vue-router'
import Layout from '@/layout/index.vue'
import ApiDashboard from '@/views/api-testing/Dashboard.vue'
const routes = [
  { path: '/api-testing', component: Layout, children: [
      { path: 'dashboard', name: 'ApiDashboard', component: ApiDashboard },
      { path: 'projects', component: () => import('@/views/api-testing/ProjectManagement.vue') }
  ]},
  { path: '/full', component: () => import('@/views/Full.vue') }
]
export default routes
"""


def _vue_facts() -> RF.RepoFacts:
    files = {
        "src/router/index.js": _sf("src/router/index.js", _VUE_ROUTER),
        "src/layout/index.vue": _sf("src/layout/index.vue", "<template><div/></template>"),
        "src/views/api-testing/Dashboard.vue": _sf(
            "src/views/api-testing/Dashboard.vue", "<template><button/></template>"
        ),
        "src/views/api-testing/ProjectManagement.vue": _sf(
            "src/views/api-testing/ProjectManagement.vue", "<template><button/></template>"
        ),
        "src/views/Full.vue": _sf("src/views/Full.vue", "<template><button/></template>"),
    }
    return RF.build_repo_facts(files)


def test_nested_relative_child_gets_parent_prefix() -> None:
    facts = _vue_facts()
    hit = facts.route_of_file("src/views/api-testing/Dashboard.vue")
    assert hit is not None
    assert hit.path == "/api-testing/dashboard", "相对子路由必须拼父前缀，不能写成 /dashboard"


def test_dynamic_import_resolves_to_real_file() -> None:
    facts = _vue_facts()
    hit = facts.route_of_file("src/views/api-testing/ProjectManagement.vue")
    assert hit is not None and hit.path == "/api-testing/projects"


def test_component_from_next_route_does_not_leak() -> None:
    """回归：动态 import 的组件被误认成下一条路由的 Layout（18 条绑定成因）。"""
    facts = _vue_facts()
    full = facts.route_of_file("src/views/Full.vue")
    assert full is not None and full.path == "/full"
    assert full.component == "Full", f"串到下一条路由的组件：{full.component}"


def test_route_of_file_requires_evidence() -> None:
    facts = _vue_facts()
    assert facts.route_of_file("src/views/NotInRouter.vue") is None
    assert facts.route_of_file("") is None


def test_infer_route_prefers_file_binding_over_component_name() -> None:
    """两个同名 Dashboard.vue：靠 import 绑定消歧，而不是按 stem 取最短路由。"""
    text = """\
import ApiDash from '@/views/api-testing/Dashboard.vue'
import MonDash from '@/views/monitor/Dashboard.vue'
const routes = [
  { path: '/api-testing/dashboard', component: ApiDash },
  { path: '/monitor/dashboard', component: MonDash }
]
"""
    files = {
        "src/router.js": _sf("src/router.js", text),
        "src/views/api-testing/Dashboard.vue": _sf(
            "src/views/api-testing/Dashboard.vue", "<template><div/></template>"
        ),
        "src/views/monitor/Dashboard.vue": _sf(
            "src/views/monitor/Dashboard.vue", "<template><div/></template>"
        ),
    }
    facts = RF.build_repo_facts(files)
    hit = RF.infer_route("src/views/monitor/Dashboard.vue", facts)
    assert hit is not None and hit.path == "/monitor/dashboard"


def test_route_binding_to_dict_exposes_file() -> None:
    facts = _vue_facts()
    rows = facts.to_dict()["routes"]
    assert any(r["file"] == "src/views/api-testing/Dashboard.vue" for r in rows)
