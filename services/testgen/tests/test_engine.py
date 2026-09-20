"""引擎测试：扫描 / 功能点提取 / 差异打标 / 测试点展开 / 用例生成。"""

from core.contracts import FunctionalPoint
from core.enums import FULL_SCOPE, Dimension, FType, Tag, TPType, VerifyLayer
from engine import case_gen, diff_tag, fp_extract, scan, tp_expand


def _extract(repo):
    files = scan.Scanner(repo).index()
    return files, fp_extract.extract_functional_points(files)


# ---------------------------------------------------------------- 扫描
def test_scan_indexes_files(sample_repo):
    files = scan.Scanner(sample_repo).index()
    assert "billing/api.py" in files
    assert "routes.js" in files
    assert files["billing/api.py"].is_python


def test_scan_skips_excluded_dirs(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "node_modules" / "pkg").mkdir(parents=True)
    (tmp_path / "node_modules" / "pkg" / "index.js").write_text("1", encoding="utf-8")
    files = scan.Scanner(tmp_path).index()
    assert "app/main.py" in files
    assert not any(p.startswith("node_modules") for p in files)


def test_scan_skips_pytest_temp_artifacts(tmp_path):
    """回归：`.pytest_tmp`（pytest `--basetemp`）里装着上一轮测试的夹具仓库副本，不是被测源码。

    真机自测暴露：扫本服务自身时它被当成源码扫进来，产出大量「幽灵功能点」
    （`.pytest_tmp/<case>/sample_app/billing/api.py`），并让语义去重凭空收敛 351 条
    ——数字被污染而不自知。
    """
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("x = 1\n", encoding="utf-8")
    ghost = tmp_path / ".pytest_tmp" / "test_case0" / "sample_app" / "billing"
    ghost.mkdir(parents=True)
    (ghost / "api.py").write_text("y = 1\n", encoding="utf-8")

    files = scan.Scanner(tmp_path).index()
    assert "app/main.py" in files
    assert not any(".pytest_tmp" in rel for rel in files), sorted(files)


def test_scan_is_not_fooled_by_ancestor_dir_names(tmp_path):
    """回归：排除规则按「相对仓库根」判定，不看绝对路径。

    早期实现用 `p.parts`（绝对路径）匹配排除目录，于是**只要仓库位于名为
    `build/ dist/ env/ .pytest_tmp/` 的目录下，整个仓库都会被当成排除目录 →
    扫描结果静默变 0 文件**（与「漏 .tsx 导致前端整层不可见」同一类静默丢层）。
    """
    root = tmp_path / "build" / "sample_app"  # 祖先目录名恰好是排除名
    (root / "billing").mkdir(parents=True)
    (root / "billing" / "api.py").write_text("x = 1\n", encoding="utf-8")

    files = scan.Scanner(root).index()
    assert "billing/api.py" in files, "祖先目录名不得导致整仓被排除"

    # 但仓库**内部**的同名目录仍必须排除（相对路径判定）
    (root / "build").mkdir()
    (root / "build" / "gen.py").write_text("y = 1\n", encoding="utf-8")
    inside = scan.Scanner(root).index()
    assert "billing/api.py" in inside
    assert not any(rel.startswith("build/") for rel in inside), sorted(inside)


def test_scan_marks_noise(sample_repo):
    files = scan.Scanner(sample_repo).index()
    assert files["billing/api.py"].is_noise is False


# ---------------------------------------------------------------- 功能点
def test_extract_routes_and_business(sample_repo):
    _, result = _extract(sample_repo)
    names = {fp.name for fp in result.functional_points}
    assert "GET /api/v1/invoices" in names, "装饰器前缀应被拼回路由"
    assert "GET /api/v1/invoices/{invoice_id}" in names
    assert "POST /api/v1/invoices" in names
    assert "calculate_total" in names
    assert result.errors == []


def test_extract_sets_module_and_type(sample_repo):
    _, result = _extract(sample_repo)
    api = next(fp for fp in result.functional_points if fp.name == "GET /api/v1/invoices")
    assert api.ftype == FType.API.value
    assert api.module == "billing"
    assert api.fp_id.startswith("FP-")
    assert "billing/api.py" in api.description


def _business_names(root) -> list[str]:
    files = scan.Scanner(str(root)).index()
    fps = fp_extract.extract_functional_points(files).functional_points
    return sorted(fp.name for fp in fps if fp.ftype == FType.BUSINESS.value)


def test_same_named_methods_in_different_classes_do_not_collide(tmp_path):
    """不同类的同名方法必须得到不同 fp_id。

    回归要点：业务功能点曾用**裸函数名**做机器键，同一文件里 `A.query` 与 `B.query`
    算出同一个 `fp_id`，落库时被去重静默合并（实测真实仓库上 1034 个功能点被吞掉 11 个）。
    """
    src = tmp_path / "app"
    src.mkdir()
    (src / "models.py").write_text(
        "class A:\n"
        "    def query(self):\n"
        '        """A 的查询。"""\n'
        "        return 1\n"
        "\n"
        "\nclass B:\n"
        "    def query(self):\n"
        '        """B 的查询。"""\n'
        "        return 2\n",
        encoding="utf-8",
    )
    assert _business_names(src) == ["A.query", "B.query"]

    files = scan.Scanner(str(src)).index()
    fps = fp_extract.extract_functional_points(files).functional_points
    assert len({fp.fp_id for fp in fps}) == len(fps), "功能点编号必须两两不同"


def test_module_level_function_keeps_bare_name(tmp_path):
    """模块级函数不应被加上类名前缀（避免过度限定）。"""
    src = tmp_path / "flat_app"
    src.mkdir()
    (src / "flat.py").write_text(
        'def plain_func():\n    """模块级函数。"""\n    return 1\n', encoding="utf-8"
    )
    assert _business_names(src) == ["plain_func"]


def test_extract_pages_from_frontend(sample_repo):
    _, result = _extract(sample_repo)
    pages = [fp for fp in result.functional_points if fp.ftype == FType.PAGE.value]
    assert any(fp.name == "/dashboard" for fp in pages)


def test_scan_covers_all_frontend_extensions(sample_repo):
    """扫描器必须覆盖前端全部扩展名（含 .tsx/.jsx）。

    回归要点：`Scanner.exts` 白名单曾漏掉 `.tsx`，真实仓库上 147 个 `.tsx`
    （整个 React 页面层）**从未进入分析**，表现为「UI 层功能点恒为 0」且无任何报错。
    """
    files = scan.Scanner(str(sample_repo)).index()
    assert "web/App.tsx" in files, "前端 .tsx 文件必须被扫描到"
    for ext in scan.FRONTEND_EXTS:
        assert ext in scan.SOURCE_EXTS, f"{ext} 应包含在扫描扩展名中"


def test_extract_jsx_routes_as_page(sample_repo):
    """JSX 写法 `<Route path="/x" />` 必须被识别为页面功能点（等价于 `path: '/x'`）。"""
    _, result = _extract(sample_repo)
    pages = {fp.name for fp in result.functional_points if fp.ftype == FType.PAGE.value}
    assert "/dashboard" in pages, "对象字面量写法应识别"
    assert {"/pc/chat", "/pc/tasks"} <= pages, "JSX 写法应识别"


def test_page_functional_point_yields_ui_layer_case(sample_repo):
    """页面功能点 → UI 层用例（ctype=e2e / 机器步 ui_probe），不得退化成接口层。"""
    _, result = _extract(sample_repo)
    pages = [fp for fp in result.functional_points if fp.ftype == FType.PAGE.value]
    assert pages, "样例仓库应含页面功能点"
    tps = tp_expand.expand_all(pages, tp_expand.ExpandContext(scopes={TPType.NORMAL.value}))
    assert tps
    for tp in tps:
        assert tp.verify_layer == VerifyLayer.UI.value
        case = case_gen.build_case(tp)
        assert case.ctype == "e2e"
        assert case.steps[0]["action"] == "ui_probe"
        assert case.steps[0]["kind"] == FType.PAGE.value
        assert case.missing_elements() == []


def test_execution_layer_follows_verify_layer_not_http_verb(sample_repo):
    """执行层以契约的 verify_layer 为准，不得按「是否 HTTP 动词」判。

    回归要点：后端业务函数（method=FUNC）属**接口层**，早期实现把它派成 `ui_probe`，
    执行器会去拉浏览器「打开一个后端函数」。
    """
    _, result = _extract(sample_repo)
    tps = tp_expand.expand_all(
        result.functional_points, tp_expand.ExpandContext(scopes={TPType.NORMAL.value})
    )
    cases = [case_gen.build_case(tp) for tp in tps]
    by_kind = {c.steps[0]["kind"]: c for c in cases}
    business, page = by_kind[FType.BUSINESS.value], by_kind[FType.PAGE.value]
    assert business.steps[0]["layer"] == VerifyLayer.INTERFACE.value
    assert business.steps[0]["action"] == "http_probe"
    assert page.steps[0]["layer"] == VerifyLayer.UI.value
    assert page.steps[0]["action"] == "ui_probe"


def test_http_case_kind_is_api_not_empty(sample_repo):
    """HTTP 接口用例的 `steps[0].kind` 必须是 `api`，不得为空串。

    回归要点：`kind` 曾对 HTTP 路由返回 `""`，其他类型是 page/ui/business；
    下游按 `kind` 分组时，整类 HTTP 接口用例（本仓 722 条）会被静默漏掉。
    """
    _, result = _extract(sample_repo)
    tps = tp_expand.expand_all(
        result.functional_points, tp_expand.ExpandContext(scopes={TPType.NORMAL.value})
    )
    api_tps = [tp for tp in tps if tp.verify_layer == VerifyLayer.INTERFACE.value]
    assert api_tps, "样例仓库应含接口类测试点"
    for tp in api_tps:
        kind = case_gen.build_case(tp).steps[0]["kind"]
        assert kind in {f.value for f in FType}, f"kind 必须是 FType 取值，实际 {kind!r}"


def test_doc_steps_wording_matches_layer(sample_repo):
    """人工步骤文案必须与执行层一致——页面/函数不能写成「发请求」。

    回归要点：早期实现一律套用 HTTP 模板，产出「构造请求：PAGE /」「发送 FUNC main 请求」，
    并统一断言「响应状态码符合预期」，对 UI 层与函数层属语义错误（实测影响 993/1715 条）。
    """
    _, result = _extract(sample_repo)
    tps = tp_expand.expand_all(
        result.functional_points, tp_expand.ExpandContext(scopes={TPType.NORMAL.value})
    )
    by_kind = {}
    for tp in tps:
        case = case_gen.build_case(tp)
        by_kind.setdefault(case.steps[0]["kind"], case)

    page_text = " ".join(s["desc"] for s in by_kind[FType.PAGE.value].doc_steps)
    assert "打开页面" in page_text
    assert "请求" not in page_text
    assert "状态码" not in page_text

    biz_text = " ".join(s["desc"] for s in by_kind[FType.BUSINESS.value].doc_steps)
    assert "调用函数" in biz_text
    assert "请求" not in biz_text

    api_text = " ".join(s["desc"] for s in by_kind[FType.API.value].doc_steps)
    assert "构造请求" in api_text
    assert "状态码" in api_text


def test_precondition_matches_layer(sample_repo):
    """前置条件必须与执行层一致：UI 层不该要求 base_url / HTTP token。

    回归要点：前置条件早期只按「维度」生成，UI 层用例也写着「base_url 可达…需持有有效 token」，
    而 UI 层既不用 base_url 也不需要 HTTP 凭证，执行人员按此准备无从下手。
    """
    _, result = _extract(sample_repo)
    tps = tp_expand.expand_all(
        result.functional_points, tp_expand.ExpandContext(scopes={TPType.NORMAL.value})
    )
    by_kind = {}
    for tp in tps:
        case = case_gen.build_case(tp)
        by_kind.setdefault(case.steps[0]["kind"], case)

    ui_pre = by_kind[FType.PAGE.value].precondition
    assert "前端可访问" in ui_pre
    assert "浏览器" in ui_pre
    assert "base_url" not in ui_pre
    assert "token" not in ui_pre

    api_pre = by_kind[FType.API.value].precondition
    assert "base_url" in api_pre


# ---------------------------------------------------------------- 安全维度
def test_security_dimension_scoped_to_api_page_ui(sample_repo):
    """安全维度在 #222 后**有意**扩展到 api + page(未授权访问) + ui(输入注入)，但绝不泄漏到 component/business。

    把关「安全测试点来自哪类功能点」：覆盖 api/page/ui 是设计内（对应接口鉴权缺失、
    未登录直访受限页、向输入/表单注入脚本三类真实风险）；component/business 仍不产出
    安全测试点，避免对纯展示组件/无鉴权语义的业务函数编造安全用例。
    """
    from core.enums import FType

    _, result = _extract(sample_repo)
    secured_ftypes: set[str] = set()
    for fp in result.functional_points:
        tps = tp_expand.expand_all([fp], tp_expand.ExpandContext(scopes={TPType.SECURITY.value}))
        if tps:
            secured_ftypes.add(fp.ftype)
    assert secured_ftypes, "接口功能点应展开出安全测试点"
    assert secured_ftypes <= {FType.API.value, FType.PAGE.value, FType.UI.value}
    assert FType.COMPONENT.value not in secured_ftypes
    assert FType.BUSINESS.value not in secured_ftypes


def test_privilege_escalation_dimension_matches_legacy(sample_repo):
    """「越权」子维度：仅对「含路径参数 + PUT/PATCH/DELETE」的接口产出。

    回归要点：legacy 有此规则（`_expand_api` 写操作补充越权维度），
    新服务重写时整条丢失，导致安全用例只剩「鉴权缺失」一种。
    """
    _, result = _extract(sample_repo)
    tps = tp_expand.expand_all(
        result.functional_points, tp_expand.ExpandContext(scopes={TPType.SECURITY.value})
    )
    dims = {tp.dimension for tp in tps}
    assert Dimension.AUTH_MISS.value in dims, "鉴权缺失维度应存在"
    assert Dimension.PRIV_ESC.value in dims, "越权维度应存在（样例含 DELETE /invoices/{id}）"

    priv = [tp for tp in tps if tp.dimension == Dimension.PRIV_ESC.value]
    assert priv, "越权测试点不应为空"
    assert all("{" in tp.area for tp in priv), "越权只对含路径参数的接口"
    assert {tp.method for tp in priv} <= {"PUT", "PATCH", "DELETE"}, "越权只针对写操作"


def test_expansion_plan_matches_legacy_parity_table():
    """展开规则必须与 legacy 逐条对齐 —— 迁移不得静默丢维度。"""
    cases = {
        # G-3/G-5：每个鉴权接口追加「令牌过期」；每个接口追加「限流/降级/超时兜底」异常流维度
        ("api", "GET /a"): [
            (TPType.NORMAL.value, Dimension.AVAIL.value),
            (TPType.SECURITY.value, Dimension.AUTH_MISS.value),
            (TPType.SECURITY.value, Dimension.TOKEN_EXPIRED.value),
            (TPType.BOUNDARY.value, Dimension.PARAM_ILLEGAL.value),
            (TPType.ABNORMAL.value, Dimension.RATE_LIMIT.value),
            (TPType.ABNORMAL.value, Dimension.DEGRADED.value),
            (TPType.ABNORMAL.value, Dimension.TIMEOUT.value),
            # G-8：性能/并发（独立行为维度「性能」，需 scope 含 性能 才默认生成）
            (TPType.PERFORMANCE.value, Dimension.PERF_BASELINE.value),
            (TPType.PERFORMANCE.value, Dimension.PERF_CONCURRENCY.value),
        ],
        ("api", "GET /a/{id}"): [
            (TPType.NORMAL.value, Dimension.AVAIL.value),
            (TPType.SECURITY.value, Dimension.AUTH_MISS.value),
            (TPType.SECURITY.value, Dimension.TOKEN_EXPIRED.value),
            (TPType.BOUNDARY.value, Dimension.PARAM_ILLEGAL.value),
            (TPType.ABNORMAL.value, Dimension.RES_NOT_FOUND.value),
            (TPType.ABNORMAL.value, Dimension.RATE_LIMIT.value),
            (TPType.ABNORMAL.value, Dimension.DEGRADED.value),
            (TPType.ABNORMAL.value, Dimension.TIMEOUT.value),
            # G-9：跨租户读（GET + 含路径参数）
            (TPType.SECURITY.value, Dimension.TENANT_READ.value),
            (TPType.PERFORMANCE.value, Dimension.PERF_BASELINE.value),
            (TPType.PERFORMANCE.value, Dimension.PERF_CONCURRENCY.value),
        ],
        ("api", "DELETE /a/{id}"): [
            (TPType.NORMAL.value, Dimension.AVAIL.value),
            (TPType.SECURITY.value, Dimension.AUTH_MISS.value),
            (TPType.SECURITY.value, Dimension.TOKEN_EXPIRED.value),
            (TPType.BOUNDARY.value, Dimension.PARAM_ILLEGAL.value),
            (TPType.ABNORMAL.value, Dimension.RES_NOT_FOUND.value),
            (TPType.ABNORMAL.value, Dimension.RATE_LIMIT.value),
            (TPType.ABNORMAL.value, Dimension.IDEMPOTENT.value),
            (TPType.ABNORMAL.value, Dimension.DEGRADED.value),
            (TPType.ABNORMAL.value, Dimension.TIMEOUT.value),
            (TPType.SECURITY.value, Dimension.PRIV_ESC.value),
            # G-9：跨租户写（写操作 + 含路径参数）
            (TPType.SECURITY.value, Dimension.TENANT_WRITE.value),
            (TPType.PERFORMANCE.value, Dimension.PERF_BASELINE.value),
            (TPType.PERFORMANCE.value, Dimension.PERF_CONCURRENCY.value),
        ],
        # G-5：页面套 DEFAULT_SCOPE（含异常）→ 追加 UI 异常流（网络中断/错误回显/空状态/错误页）
        ("page", "/p"): [
            (TPType.NORMAL.value, Dimension.PAGE_REACH.value),
            (TPType.SECURITY.value, Dimension.UI_UNAUTH_PAGE.value),
            (TPType.BOUNDARY.value, Dimension.UI_POOR_VIEWPORT.value),
            (TPType.ABNORMAL.value, Dimension.UI_NETWORK_INTERRUPT.value),
            (TPType.ABNORMAL.value, Dimension.UI_ERROR_DISPLAY.value),
            (TPType.ABNORMAL.value, Dimension.UI_EMPTY_STATE.value),
            (TPType.ABNORMAL.value, Dimension.UI_SERVER_ERROR.value),
        ],
        ("component", "X.tsx"): [(TPType.NORMAL.value, Dimension.INTERACTIVE.value)],
        ("ui", "/p#input:查询|#q"): [
            (TPType.NORMAL.value, Dimension.INTERACTIVE.value),
            (TPType.SECURITY.value, Dimension.UI_INPUT_INJECT.value),
            (TPType.BOUNDARY.value, Dimension.UI_LONG_INPUT.value),
        ],
        ("business", "f"): [(TPType.NORMAL.value, Dimension.BIZ_LOGIC.value)],
    }
    for (ftype, name), expected in cases.items():
        fp = FunctionalPoint(fp_id="FP-p", ftype=ftype, file_path="a", name=name, title=name)
        assert tp_expand.plan_of(fp) == expected, f"{ftype} {name} 展开规则偏离 legacy"


def test_default_scope_includes_security(sample_repo):
    """默认范围含「安全」——不显式指定也会生成安全用例（2026-09-14 变更）。

    变更前默认 `{正常, 边界}`，不显式指定就一条安全用例都没有，极易被误当成
    「已覆盖」；现默认 `{正常, 安全, 边界, 异常}`（见 core.enums.DEFAULT_SCOPE）。
    """
    _, result = _extract(sample_repo)
    tps = tp_expand.expand_all(result.functional_points, tp_expand.ExpandContext())
    assert tps
    assert TPType.SECURITY.value in {tp.category for tp in tps}


# ---------------------------------------------------------------- 交互组件
def test_component_extraction_requires_interaction_hook(sample_repo):
    """含交互钩子的前端文件才算组件；只有 `<Route>` 的 App.tsx 不算。"""
    _, result = _extract(sample_repo)
    names = {fp.name for fp in result.functional_points if fp.ftype == FType.COMPONENT.value}
    assert "SearchBar.tsx" in names, "含 input/button/form 的组件应被提取"
    assert "App.tsx" not in names, "只有路由声明、无交互钩子不应算组件"


def test_component_yields_interactive_test_point_and_ui_case(sample_repo):
    """组件 → 「交互元素可用」测试点 → UI 层用例。"""
    _, result = _extract(sample_repo)
    comps = [fp for fp in result.functional_points if fp.ftype == FType.COMPONENT.value]
    tps = tp_expand.expand_all(comps, tp_expand.ExpandContext(scopes={TPType.NORMAL.value}))
    assert tps
    for tp in tps:
        assert tp.dimension == Dimension.INTERACTIVE.value
        assert tp.verify_layer == VerifyLayer.UI.value
        case = case_gen.build_case(tp)
        assert case.steps[0]["action"] == "ui_probe"
        assert case.steps[0]["kind"] == FType.UI.value
        assert case.missing_elements() == []


# ---------------------------------------------------------------- 编号稳定性（D-9）
def test_test_point_ids_stable_across_scope_changes():
    """扩大范围不得改变已有维度的 tp_id。

    回归要点：序号原用「过滤后的循环下标」，用户这次只要 `正常+异常`、下次加 `安全`，
    同一条『异常』测试点序号就从 1 变 2 → 编号漂移 → 旧用例被判 obsolete 并新建一条，
    人工审核结论与执行历史全部断链（实测真实仓库上 117 条异常用例被误废弃）。
    """
    fp = FunctionalPoint(
        fp_id="FP-scope",
        ftype=FType.API.value,
        file_path="a.py",
        name="GET /x/{id}",
        title="查询详情/x/{id}",
    )
    narrow = tp_expand.expand_all(
        [fp], tp_expand.ExpandContext(scopes={TPType.NORMAL.value, TPType.ABNORMAL.value})
    )
    wide = tp_expand.expand_all([fp], tp_expand.ExpandContext(scopes=set(FULL_SCOPE)))
    narrow_ids = {tp.category: tp.tp_id for tp in narrow}
    wide_ids = {tp.category: tp.tp_id for tp in wide}
    assert narrow_ids[TPType.NORMAL.value] == wide_ids[TPType.NORMAL.value]
    assert narrow_ids[TPType.ABNORMAL.value] == wide_ids[TPType.ABNORMAL.value], (
        "扩大范围后既有维度的编号必须保持不变"
    )


def test_case_title_has_single_category_prefix(sample_repo):
    """用例标题不得出现重复的 `[维度]` 前缀。"""
    _, result = _extract(sample_repo)
    tps = tp_expand.expand_all(
        result.functional_points, tp_expand.ExpandContext(scopes=set(FULL_SCOPE))
    )
    for tp in tps:
        title = case_gen.build_case(tp).title
        assert title.count(f"[{tp.category}]") == 1, f"标题前缀重复：{title}"


# ---------------------------------------------------------------- 差异打标
def test_full_channel_tags_everything_full(sample_repo):
    ctx = diff_tag.DiffContext()
    assert diff_tag.tag_of_rel("billing/api.py", ctx) == Tag.FULL.value


def test_incremental_channel_tags_changed_files(sample_repo):
    ctx = diff_tag.DiffContext(changed_files={"billing/api.py"})
    assert diff_tag.tag_of_rel("billing/api.py", ctx) == Tag.UPDATE.value
    assert diff_tag.tag_of_rel("routes.js", ctx) == Tag.FULL.value


def test_symbol_level_hunk_hit():
    ctx = diff_tag.DiffContext(changed_files={"a.py"}, hunks={"a.py": [(10, 3)]}, aligned=True)
    assert diff_tag.tag_of_symbol("a.py", 12, 20, ctx) == Tag.UPDATE.value
    assert diff_tag.tag_of_symbol("a.py", 30, 40, ctx) == Tag.FULL.value


def test_no_repo_means_no_escape(sample_repo):
    assert diff_tag.repo_escape_blocked(sample_repo) is False


# ---------------------------------------------------------------- 测试点
def test_expand_covers_four_dimensions(sample_repo):
    _, result = _extract(sample_repo)
    detail = next(
        fp for fp in result.functional_points if fp.name == "GET /api/v1/invoices/{invoice_id}"
    )
    ctx = tp_expand.ExpandContext(scopes=set(FULL_SCOPE))
    tps = tp_expand.expand_functional_point(detail, ctx)
    cats = {tp.category for tp in tps}
    assert TPType.NORMAL.value in cats
    assert TPType.SECURITY.value in cats
    assert TPType.ABNORMAL.value in cats
    assert TPType.BOUNDARY.value in cats


def test_expand_respects_scope_filter(sample_repo):
    _, result = _extract(sample_repo)
    ctx = tp_expand.ExpandContext(scopes={TPType.NORMAL.value})
    tps = tp_expand.expand_all(result.functional_points, ctx)
    assert tps
    assert {tp.category for tp in tps} == {TPType.NORMAL.value}


def test_expand_keeps_traceability(sample_repo):
    _, result = _extract(sample_repo)
    tps = tp_expand.expand_all(result.functional_points)
    known = {fp.fp_id for fp in result.functional_points}
    assert tps
    assert all(tp.fp_contract_id in known for tp in tps), "不得出现孤儿测试点"
    assert all(tp.evidence == [] for tp in tps)


def test_verify_layer_derived(sample_repo):
    _, result = _extract(sample_repo)
    tps = tp_expand.expand_all(result.functional_points)
    api_tp = next(tp for tp in tps if tp.verify_layer)
    assert api_tp.verify_layer in ("接口", "UI")


# ---------------------------------------------------------------- 用例
def test_case_has_all_eight_elements(sample_repo):
    _, result = _extract(sample_repo)
    tps = tp_expand.expand_all(result.functional_points)
    cases = case_gen.generate_cases(tps)
    assert cases
    for case in cases:
        assert case.missing_elements() == [], f"{case.tc_no} 八要素不全"


def test_case_machine_step_in_first_position(sample_repo):
    _, result = _extract(sample_repo)
    tps = tp_expand.expand_all(result.functional_points)
    cases = case_gen.generate_cases(tps)
    for case in cases:
        step = case.steps[0]
        assert step["action"] in ("http_probe", "ui_probe")
        assert step["tp_id"] == case.tp_id
        assert "expect" in step


def test_case_generation_is_deterministic(sample_repo):
    _, result = _extract(sample_repo)
    tps = tp_expand.expand_all(result.functional_points)
    first = [c.to_dict() for c in case_gen.generate_cases(tps)]
    second = [c.to_dict() for c in case_gen.generate_cases(tps)]
    assert first == second


def test_case_coverage_no_orphan(sample_repo):
    _, result = _extract(sample_repo)
    tps = tp_expand.expand_all(result.functional_points)
    cases = case_gen.generate_cases(tps)
    cov = case_gen.coverage_of(cases, tps)
    assert cov["orphan_count"] == 0
    assert cov["uncovered_count"] == 0


def test_business_case_uses_interface_layer(sample_repo):
    """业务函数用例：ctype 仍为 e2e（执行家族不变），但执行层必须是接口层。

    说明：`ctype`（api/e2e）是**执行家族**，与 `layer`（接口/UI）是两个维度。
    业务函数不是 HTTP 接口，故 ctype=e2e；但它属接口层，机器步走 http_probe。
    旧断言曾把它钉成 `ui_probe`，等于把「给后端函数拉浏览器」这个缺陷写进了契约。
    """
    _, result = _extract(sample_repo)
    biz = next(fp for fp in result.functional_points if fp.ftype == FType.BUSINESS.value)
    ctx = tp_expand.ExpandContext(scopes={TPType.NORMAL.value})
    tps = tp_expand.expand_functional_point(biz, ctx)
    case = case_gen.build_case(tps[0])
    assert case.ctype == "e2e"
    assert case.steps[0]["layer"] == VerifyLayer.INTERFACE.value
    assert case.steps[0]["action"] == "http_probe"
    assert case.steps[0]["func"] == biz.name
    assert case.steps[0]["kind"] == FType.BUSINESS.value
