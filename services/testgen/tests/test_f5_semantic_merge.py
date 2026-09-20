"""F5 · 功能点语义级合并（`engine/fp_merge.py`）的单元与端到端测试。

要守护的三件事：
1. **判等价**：同族（api / page+ui）内按「路径归一」等价，跨族绝不互并；
2. **判保留**：来源优先级 runtime 3 > 真实源码 2 > mock/stub 0；平局保留先出现者；
3. **不误删**：component / business 解析不出语义键，**一条都不许合并**。
"""

import pytest

from core.config import get_settings
from core.contracts import FunctionalPoint
from core.enums import FType
from engine import fp_merge, pipeline


def _fp(ftype: str, name: str, file_path: str = "src/a.py") -> FunctionalPoint:
    """构造功能点（本组单测不涉及落库，编号只需在用例内唯一）。"""
    return FunctionalPoint(
        fp_id=f"FP-{ftype}.{name}.{file_path}",
        ftype=ftype,
        file_path=file_path,
        name=name,
        title=name,
    )


# ============================================================================
# 1. 名称归一
# ============================================================================
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("/pc/tasks/", "/pc/tasks"),  # 去尾斜杠
        ("/pc//tasks", "/pc/tasks"),  # 折叠重复斜杠
        ("/pc/tasks?page=1", "/pc/tasks"),  # 去 query
        ("/pc/tasks#top", "/pc/tasks"),  # 去 fragment
        ("/invoices/{invoice_id}", "/invoices/{}"),  # FastAPI 占位符
        ("/invoices/<int:invoice_id>", "/invoices/{}"),  # Flask 占位符
        ("/invoices/:id", "/invoices/{}"),  # 前端路由占位符
        ("/", "/"),  # 根路径不被削成空串
        ("/PC/Tasks", "/PC/Tasks"),  # **不折大小写**（避免过度合并）
    ],
)
def test_normalize_path(raw: str, expected: str) -> None:
    assert fp_merge.normalize_path(raw) == expected


def test_semantic_key_aligns_api_syntax_variants() -> None:
    """api 功能点的语义键对占位符语法/大小写/尾斜杠不敏感（不同写法合并为同键）。"""
    api_a = _fp(FType.API.value, "GET /invoices/{invoice_id}", "backend/api.py")
    api_b = _fp(FType.API.value, "get /invoices/<int:invoice_id>/", "other/api.py")
    assert fp_merge.semantic_key(api_a) == fp_merge.semantic_key(api_b)


def test_semantic_key_keeps_ui_separate_from_page() -> None:
    """#221 修复：ui 元素级功能点（名称含合成分隔符 `#`）必须**不与**同路径的 page 功能点撞键，

    否则整页元素功能点会被 F5 语义合并掉，元素级分解在完整流水线里被撤销（首跑曾塌成 33 FP）。
    """
    page = _fp(FType.PAGE.value, "/pc/tasks", "web/App.tsx")
    ui = _fp(FType.UI.value, "/pc/tasks", "web/legacy.js")
    assert fp_merge.semantic_key(page) != fp_merge.semantic_key(ui)
    assert fp_merge.semantic_key(page) == "page|/pc/tasks"
    assert fp_merge.semantic_key(ui) == "page|ui|/pc/tasks"


def test_component_and_business_have_no_semantic_key() -> None:
    """component / business 的 name 是文件名 / 限定函数名，跨目录同名会撞车 → 不参与合并。"""
    assert fp_merge.semantic_key(_fp(FType.COMPONENT.value, "SearchBar.tsx")) is None
    assert fp_merge.semantic_key(_fp(FType.BUSINESS.value, "Report.build")) is None
    # 不像路由的前端名（组件名）同样不参与
    assert fp_merge.semantic_key(_fp(FType.UI.value, "SearchBar")) is None


def test_source_rank_prefers_runtime_then_source_then_mock() -> None:
    runtime = fp_merge.source_rank("runtime:http://srv/pc/tasks")
    source = fp_merge.source_rank("backend/api.py")
    mock = fp_merge.source_rank("frontend/mock-server/server.py")
    assert runtime > source > mock
    assert fp_merge.source_rank("front/mocks/x.py") == mock
    assert fp_merge.source_rank("tools/admin_api.py") == source


def test_source_rank_prefers_page_owner_over_menu_declaration() -> None:
    """真机实测：抽屉菜单里的 MENUS 表与页面文件都声明了 /m/schedule，页面文件必须胜出。"""
    page = fp_merge.source_rank("frontend/app/src/pages/mobile/MobileMePage.tsx", FType.PAGE.value)
    menu = fp_merge.source_rank(
        "frontend/app/src/components/mobile/HistoryDrawer.tsx", FType.PAGE.value
    )
    assert page > menu
    # 主优先级永远压过声明质量：运行时（3,0）必须高于任何静态（2,x）
    assert fp_merge.source_rank("runtime:http://srv/m/schedule") > page


def test_dedupe_prefers_page_file_over_menu_list() -> None:
    menu = _fp(FType.PAGE.value, "/m/schedule", "web/components/mobile/HistoryDrawer.tsx")
    page = _fp(FType.PAGE.value, "/m/schedule", "web/pages/mobile/MobileMePage.tsx")
    merged, _ = fp_merge.dedupe_functional_points([menu, page])
    assert [fp.file_path for fp in merged] == ["web/pages/mobile/MobileMePage.tsx"]


# ============================================================================
# 2. 合并 / 去重
# ============================================================================
def test_dedupe_keeps_real_source_over_mock_server() -> None:
    """旧平台 C-②3：mock server 与真实后端声明同名路由 → 只保留真实后端。"""
    real = _fp(FType.API.value, "GET /api/v1/projects", "backend/api.py")
    mock = _fp(FType.API.value, "GET /api/v1/projects", "frontend/mock-server/server.py")
    merged, stats = fp_merge.dedupe_functional_points([mock, real])  # 故意把 mock 放前面

    assert len(merged) == 1
    assert merged[0].file_path == "backend/api.py", "真实源码必须优先于 mock"
    assert stats["input"] == 2
    assert stats["deduped"] == 1
    assert "覆盖" in stats["examples"][0]


def test_dedupe_does_not_touch_component_or_business() -> None:
    """同名不同目录的两个组件：一条都不许合（误删真实功能点比重复更糟）。"""
    comps = [
        _fp(FType.COMPONENT.value, "SearchBar.tsx", "web/a/SearchBar.tsx"),
        _fp(FType.COMPONENT.value, "SearchBar.tsx", "web/b/SearchBar.tsx"),
    ]
    merged, stats = fp_merge.dedupe_functional_points(comps)
    assert len(merged) == 2
    assert stats["deduped"] == 0


def test_merge_runtime_replaces_static_page_of_same_key() -> None:
    static = [_fp(FType.PAGE.value, "/pc/tasks", "web/App.tsx")]
    runtime = [_fp(FType.PAGE.value, "/pc/tasks/", "runtime:http://srv/pc/tasks")]
    merged, stats = fp_merge.merge_functional_points(static, runtime)
    assert len(merged) == 1
    assert merged[0].file_path.startswith("runtime:")  # 运行时优先
    assert stats["added"] == 0
    assert stats["replaced"] == 1


def test_merge_is_stable_and_reports_examples() -> None:
    """平局保留**先出现**者：跨运行结果稳定，且必须留下可追溯的示例。"""
    first = _fp(FType.PAGE.value, "/a", "web/1.tsx")
    second = _fp(FType.PAGE.value, "/a", "web/2.tsx")
    merged, stats = fp_merge.merge_functional_points([], [first, second])
    assert [fp.file_path for fp in merged] == ["web/1.tsx"]
    assert stats["deduped"] == 1
    # 平局保留先出现者，且结构化冲突清单可追溯（不再只靠一行文本）
    assert stats["conflicts"][0]["survivor"]["file_path"] == "web/1.tsx"
    assert stats["conflicts"][0]["reason"] == "同源同名保留首次出现(平级)"


def test_summary_line_is_none_when_nothing_merged() -> None:
    _, stats = fp_merge.dedupe_functional_points([_fp(FType.PAGE.value, "/a", "web/1.tsx")])
    assert fp_merge.summary_line(stats, "x") is None


# ============================================================================
# 3. 端到端：真实后端 vs mock server 的重复计数必须消失
# ============================================================================
@pytest.fixture()
def dup_repo(tmp_path):
    """构造「真实后端 + 前端 mock server 声明同名路由」的最小仓库。"""
    src = tmp_path / "dup_app"
    (src / "backend").mkdir(parents=True)
    (src / "backend" / "api.py").write_text(
        '"""真实后端接口。"""\n'
        "from fastapi import APIRouter\n"
        'router = APIRouter(prefix="/api/v1")\n'
        "\n"
        '\n@router.get("/projects")\n'
        "def list_projects():\n"
        '    """查询项目列表。"""\n'
        "    return []\n",
        encoding="utf-8",
    )
    mock_dir = src / "frontend" / "mock-server"
    mock_dir.mkdir(parents=True)
    (mock_dir / "server.py").write_text(
        '"""前端本地 mock 服务：与真实后端路由同名（旧平台 C-②3）。"""\n'
        "from fastapi import FastAPI\n"
        "\n"
        "app = FastAPI()\n"
        "\n"
        '\n@app.get("/api/v1/projects")\n'
        "def mock_list_projects():\n"
        "    return []\n",
        encoding="utf-8",
    )
    return src


def _run(repo, **overrides):
    opts = pipeline.default_options(str(repo))
    for key, value in overrides.items():
        setattr(opts, key, value)
    return pipeline.run_pipeline(opts)


def _api_names(result) -> list[str]:
    return [fp.name for fp in result.functional_points if fp.ftype == FType.API.value]


def test_pipeline_merges_mock_server_duplicate(fresh_db, dup_repo) -> None:
    result = _run(dup_repo)
    assert _api_names(result).count("GET /api/v1/projects") == 1
    kept = next(fp for fp in result.functional_points if fp.name == "GET /api/v1/projects")
    assert kept.file_path == "backend/api.py"
    assert result.counts["fp_merged"] == 1
    assert any("语义去重" in note for note in result.notes)
    # 三级计数一致：功能点与以它为准的测试点/用例都不重复
    assert result.counts["test_points"] == result.counts["cases"]


def test_pipeline_semantic_merge_can_be_disabled_for_comparison(fresh_db, dup_repo) -> None:
    """关掉开关即回到旧行为（用于「合并前后计数对比」，也是出问题时的回退阀）。"""
    get_settings().fp_semantic_merge = False
    result = _run(dup_repo)
    assert _api_names(result).count("GET /api/v1/projects") == 2
    assert "fp_merged" not in result.counts
