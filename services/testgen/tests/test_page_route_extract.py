"""页面路由提取回归测试（修复「文件名冒充路由」与「同路径重复」）。

背景（实测 frontend/src 204 个文件）：
  - 145 条 page 功能点里 121 条是 `_screen_fp` 用文件名伪造的 URL（`/Home.vue`、`/Dashboard.vue`），
    每条再展开 7 个维度 → 847 条对着不存在地址的假用例；
  - `/home`、`/performance-testing/comparison` 各出现 2 次（路由文件 + 页面文件各声明一遍）；
  - `_PAGE_NAME_RE.findall(text)[i]` 与第 i 个 `path:` 硬对齐，实测把 `/home` 标成 `Login`。

三条修复各自被下面用例守护。
"""

from __future__ import annotations

from types import SimpleNamespace

from core.enums import Dimension, FType, TPType
from engine import case_gen, fp_extract, tp_expand


def _fe(rel: str, text: str) -> SimpleNamespace:
    """最小前端 SourceFile（fp_extract 对前端只用 rel/name/ext/text/is_noise）。"""
    return SimpleNamespace(
        rel=rel,
        name=rel.split("/")[-1],
        ext="." + rel.rsplit(".", 1)[-1],
        text=text,
        is_noise=False,
        is_python=False,
    )


def _extract(files: dict[str, SimpleNamespace]):
    return fp_extract.extract_functional_points(
        files, include_business=False, extract_pages=True, extract_components=True
    )


# ---------------------------------------------------------------- 假路径
def test_screen_without_route_does_not_fake_url():
    """无 path 声明的屏幕：has_route=False，name 是文件路径，不再是 `/Xxx.vue`。"""
    rel = "frontend/src/views/Home.vue"
    res = _extract({rel: _fe(rel, "<template><el-button @click='go'>去</el-button></template>")})
    pages = [fp for fp in res.functional_points if fp.ftype == FType.PAGE.value]
    assert len(pages) == 1
    fp = pages[0]
    assert fp.has_route is False
    assert fp.name == rel, "name 应为文件相对路径"
    assert not fp.name.startswith("/"), "不得伪装成 URL"


def test_page_with_route_declaration_is_reachable():
    """有 path 声明的页面：has_route=True，name 是真实路由。"""
    rel = "frontend/src/router/index.js"
    res = _extract(
        {rel: _fe(rel, "const routes=[{path:'/projects',name:'ProjectList',component:X}]")}
    )
    pages = [fp for fp in res.functional_points if fp.ftype == FType.PAGE.value]
    assert [fp.name for fp in pages] == ["/projects"]
    assert pages[0].has_route is True
    assert "ProjectList" in pages[0].semantic


def test_screen_only_expands_interactive_dimension():
    """无路由屏幕只展开「交互元素可用」——不生成需要地址的 7 个维度。"""
    rel = "frontend/src/views/Order.vue"
    res = _extract({rel: _fe(rel, "<el-button @click='submit'>提交</el-button>")})
    fp = next(fp for fp in res.functional_points if fp.ftype == FType.PAGE.value)
    plan = tp_expand.plan_of(fp)
    assert plan == [(TPType.NORMAL.value, Dimension.INTERACTIVE.value)]


def test_routed_page_keeps_full_seven_dimensions():
    """有路由的页面仍保留 7 维度（页面可达 + 未授权 + 视口 + 4 个异常流）。"""
    rel = "frontend/src/router/index.js"
    res = _extract({rel: _fe(rel, "const routes=[{path:'/orders',name:'OrderList'}]")})
    fp = next(fp for fp in res.functional_points if fp.ftype == FType.PAGE.value)
    dims = [d for _, d in tp_expand.plan_of(fp)]
    assert Dimension.PAGE_REACH.value in dims
    assert Dimension.UI_UNAUTH_PAGE.value in dims
    assert Dimension.UI_NETWORK_INTERRUPT.value in dims
    assert len(dims) == 7


def test_unrouted_screen_case_steps_do_not_say_open_page():
    """无路由屏幕的用例文案应是「定位交互点」，不能是「打开页面：views/x.vue」。"""
    rel = "frontend/src/views/Order.vue"
    res = _extract({rel: _fe(rel, "<el-button @click='submit'>提交</el-button>")})
    fp = next(fp for fp in res.functional_points if fp.ftype == FType.PAGE.value)
    tp = tp_expand.expand_functional_point(fp, tp_expand.ExpandContext())[0]
    case = case_gen.build_case(tp)
    assert case is not None
    prepare = case.doc_steps[1]["desc"]
    assert "定位交互点" in prepare
    assert "打开页面" not in prepare


# ---------------------------------------------------------------- 重复
def test_same_path_across_files_kept_once_prefers_router_file():
    """同一路径被路由文件与页面文件各声明一次 → 只留一条，且优先路由文件那条。"""
    route_rel = "frontend/src/router/index.js"
    view_rel = "frontend/src/views/Home.vue"
    files = {
        route_rel: _fe(route_rel, "const routes=[{path:'/home',name:'Home',component:H}]"),
        view_rel: _fe(view_rel, "const sub=[{path:'/home',name:'HomeAlias'}]"),
    }
    pages = [fp for fp in _extract(files).functional_points if fp.ftype == FType.PAGE.value]
    assert len(pages) == 1, f"同路径应去重，实际 {[p.name for p in pages]}"
    assert pages[0].file_path == route_rel


def test_dedupe_does_not_touch_other_ftypes():
    """去重只作用于 PAGE，业务函数/组件不受影响。"""
    rel = "frontend/src/views/A.vue"
    files = {
        rel: _fe(rel, "<el-button @click='x'>x</el-button>"),
        "frontend/src/views/B.vue": _fe(
            "frontend/src/views/B.vue", "<el-button @click='y'>y</el-button>"
        ),
    }
    res = _extract(files)
    pages = [fp for fp in res.functional_points if fp.ftype == FType.PAGE.value]
    assert len(pages) == 2, "不同文件的屏幕各有独立相对路径，不应被合并"


# ---------------------------------------------------------------- 名字错位
def test_name_is_paired_within_path_segment_not_by_global_index():
    """`name:` 只在「本条 path 到下一个 path」之间配对，不被前序无关 name 挤错位。

    复现原缺陷：文件里先有 `name: 'Menu'`（菜单项），再声明 `/home`，
    旧实现取 names[0]='Menu' 配给 /home，实测把 /home 标成 Login。
    """
    rel = "frontend/src/router/index.js"
    text = (
        "const menu=[{name:'Menu',path:'/menu'}];"
        "const routes=[{path:'/home',name:'Home',component:H},"
        "{path:'/login',name:'Login',component:L}];"
    )
    pages = {
        fp.name: fp
        for fp in _extract({rel: _fe(rel, text)}).functional_points
        if fp.ftype == FType.PAGE.value
    }
    assert "Home" in pages["/home"].semantic
    assert "Login" in pages["/login"].semantic
    assert "Login" not in pages["/home"].semantic, "不得把下一个路径的 name 串上来"


def test_name_falls_back_to_file_stem_when_segment_has_no_name():
    """段落内没有 name: 时退回文件名（无扩展名），不留空、不串位。"""
    rel = "frontend/src/router/index.js"
    pages = [
        fp
        for fp in _extract(
            {rel: _fe(rel, "const r=[{path:'/plain',component:P}]")}
        ).functional_points
        if fp.ftype == FType.PAGE.value
    ]
    assert "index" in pages[0].semantic
