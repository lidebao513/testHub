"""UI 优先覆盖策略回归测试。

守护两条易回归的规则：
1. 屏幕识别：路由渲染出来的屏幕（pages/ 目录、*Page 命名）应被识别为「页面」功能点，
   而非漏掉或仅当成「交互组件」——这是「UI 层用例为什么这么少」的根因修复。
2. 覆盖角色：UI 层恒为 primary；接口层在 ui_first 下、对应模块已有 UI 覆盖时为 supplement，
   否则 primary；drop_supplement 时补充用例被丢弃。
"""

from __future__ import annotations

from types import SimpleNamespace

from core.enums import (
    COVERAGE_ROLE_PRIMARY,
    COVERAGE_ROLE_SUPPLEMENT,
    LAYER_STRATEGY_ALL,
    LAYER_STRATEGY_UI_FIRST,
)
from engine import case_gen, fp_extract


def _frontend(rel: str, text: str) -> object:
    """构造最小前端 SourceFile（fp_extract 对前端只用 ext/rel/text/is_noise）。"""
    name = rel.split("/")[-1]
    return SimpleNamespace(
        rel=rel,
        name=name,
        ext=".tsx",
        text=text,
        is_noise=False,
        is_python=False,
    )


def test_screen_in_pages_dir_is_page_not_component():
    """pages/ 下无 path= 声明的屏幕文件 → 页面功能点（不再漏成组件）。"""
    files = {
        "frontend/app/src/pages/mobile/MobileChatPage.tsx": _frontend(
            "frontend/app/src/pages/mobile/MobileChatPage.tsx",
            "export default function MobileChatPage(){ return <button onClick={x}>发</button> }",
        ),
    }
    res = fp_extract.extract_functional_points(files, extract_pages=True, extract_components=True)
    ftypes = {fp.ftype for fp in res.functional_points}
    assert "page" in ftypes
    assert ftypes == {"page"}  # 单文件单决策：屏幕只产页面，不重复产组件


def test_widget_component_with_hooks_is_component():
    """非屏幕、含交互钩子的组件文件 → 交互组件功能点。"""
    files = {
        "frontend/app/src/components/ui/button.tsx": _frontend(
            "frontend/app/src/components/ui/button.tsx",
            "export function Button(){ return <button>ok</button> }",
        ),
    }
    res = fp_extract.extract_functional_points(files, extract_pages=True, extract_components=True)
    assert [fp.ftype for fp in res.functional_points] == ["component"]


def test_page_with_path_declaration_still_page():
    """带 self path= 声明的页面文件 → 页面功能点。"""
    files = {
        "frontend/app/src/pages/pc/PcChatPage.tsx": _frontend(
            "frontend/app/src/pages/pc/PcChatPage.tsx",
            "const routes = [{ path: '/pc/chat', element: <Chat/> }]",
        ),
    }
    res = fp_extract.extract_functional_points(files, extract_pages=True, extract_components=True)
    assert [fp.ftype for fp in res.functional_points] == ["page"]


def _ui_tp(module: str) -> dict:
    return {
        "category": "正常",
        "method": "PAGE",
        "area": "/chat",
        "source": "x.tsx",
        "tp_id": "TP-ui1",
        "fp_contract_id": "FP-ui1",
        "expect": "渲染",
        "title": "t",
        "module": module,
        "verify_layer": "UI",
        "dimension": "",
        "tag": "全量",
    }


def _iface_tp(module: str) -> dict:
    return {
        "category": "正常",
        "method": "GET",
        "area": "/api/x",
        "source": "x.py",
        "tp_id": "TP-if1",
        "fp_contract_id": "FP-if1",
        "expect": "2xx",
        "title": "t",
        "module": module,
        "verify_layer": "接口",
        "dimension": "",
        "tag": "全量",
    }


def test_ui_case_always_primary():
    spec = case_gen.build_case(_ui_tp("mobile"), ui_modules={"mobile"})
    assert spec is not None
    assert spec.coverage_role == COVERAGE_ROLE_PRIMARY
    assert spec.steps[0]["coverage_role"] == COVERAGE_ROLE_PRIMARY


def test_interface_case_supplement_when_module_has_ui():
    spec = case_gen.build_case(
        _iface_tp("mobile"), ui_modules={"mobile"}, strategy=LAYER_STRATEGY_UI_FIRST
    )
    assert spec.coverage_role == COVERAGE_ROLE_SUPPLEMENT


def test_interface_case_primary_when_module_has_no_ui():
    spec = case_gen.build_case(
        _iface_tp("admin_api"), ui_modules={"mobile"}, strategy=LAYER_STRATEGY_UI_FIRST
    )
    assert spec.coverage_role == COVERAGE_ROLE_PRIMARY


def test_interface_case_primary_under_all_strategy():
    spec = case_gen.build_case(
        _iface_tp("mobile"), ui_modules={"mobile"}, strategy=LAYER_STRATEGY_ALL
    )
    assert spec.coverage_role == COVERAGE_ROLE_PRIMARY


def test_drop_supplement_removes_case():
    specs = case_gen.generate_cases(
        [_iface_tp("mobile"), _iface_tp("admin_api")],
        ui_modules={"mobile"},
        strategy=LAYER_STRATEGY_UI_FIRST,
        drop_supplement=True,
    )
    # mobile 应为 supplement 被丢弃；admin_api 为 primary 保留
    assert len(specs) == 1
    assert specs[0].module == "admin_api"
    assert specs[0].coverage_role == COVERAGE_ROLE_PRIMARY
