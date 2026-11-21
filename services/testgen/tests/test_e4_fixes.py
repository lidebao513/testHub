"""E4（P2）修复契约测试：B1/B2 点击副作用、B3 path 泛提、B4 路由前缀、B6 门禁、B8 对账。

全部用纯函数 / 内存 sqlite / 源码字符串断言，无真实浏览器与网络。
"""

from __future__ import annotations

import ast
from pathlib import Path

from core import store
from core.contracts import CaseSpec, TestPoint
from engine import fp_extract, quality, runtime_ui
from engine.scan import SourceFile


# ---------------------------------------------------------------- B8 store 对账
def _case(
    tp_id: str, *, title: str = "用例", fp: str = "FP-1", row: int = 1, module: str = "m"
) -> CaseSpec:
    return CaseSpec(
        tc_no=tp_id,
        title=title,
        ctype="api",
        steps=[{"action": "http_probe", "expect": "返回 2xx"}],
        module=module,
        case_type="正常",
        priority="P2",
        precondition="服务已启动",
        doc_steps=[{"seq": 1, "type": "前置", "desc": "ok"}],
        tp_id=tp_id,
        fp_contract_id=fp,
        fp_row_id=row,
    )


def test_case_payload_includes_trace_columns() -> None:
    """B8：稳定追溯列纳入 payload；易变的 fp_id（自增行 id）不参与内容比对。"""
    payload = store._case_payload(_case("TP-1", fp="FP-new", row=7, module="orders"))
    assert payload["fp_contract_id"] == "FP-new"
    assert payload["tc_no"] == "TP-1"
    assert payload["module"] == "orders"
    assert "fp_id" not in payload, "fp_id 每次生成会变，纳入比对会破坏对账幂等"


def test_trace_column_change_becomes_updated(fresh_db) -> None:
    """B8：稳定追溯列（fp_contract_id/module）变化走 updated 刷新（自愈）。"""
    pid = store.upsert_project("e4-trace", "/tmp/e4-trace")
    store.reconcile_cases(pid, [_case("TP-1", fp="FP-old", module="old")])
    stats = store.reconcile_cases(pid, [_case("TP-1", fp="FP-new", module="new")])
    assert stats["updated"] == 1
    rows = store.list_cases(pid)
    assert rows[0]["fp_contract_id"] == "FP-new"
    assert rows[0]["module"] == "new"


def test_row_id_change_alone_stays_reused(fresh_db) -> None:
    """B8 幂等守护：易变 fp_id 变化不应触发 updated（第二次生成仍 reused）。"""
    pid = store.upsert_project("e4-rowid", "/tmp/e4-rowid")
    store.reconcile_cases(pid, [_case("TP-1", row=1)])
    stats = store.reconcile_cases(pid, [_case("TP-1", row=2)])
    assert stats["reused"] == 1
    assert stats["updated"] == 0


def test_changed_obsolete_case_counts_revived(fresh_db) -> None:
    """B8：作废行内容又变化 → 更新且计 revived（复活数不再少算）。"""
    pid = store.upsert_project("e4-revive", "/tmp/e4-revive")
    store.reconcile_cases(pid, [_case("TP-1", title="旧")])
    store.reconcile_cases(pid, [])  # 全 obsolete
    stats = store.reconcile_cases(pid, [_case("TP-1", title="新")])
    assert stats["updated"] == 1
    assert stats["revived"] == 1


def test_last_result_not_written_to_obsolete(fresh_db) -> None:
    """B8：执行结论只回填活跃行，不污染 obsolete（防结论分裂）。"""
    pid = store.upsert_project("e4-last", "/tmp/e4-last")
    store.reconcile_cases(pid, [_case("TP-1")])
    # 直接把行置 obsolete（模拟上次未再产出）
    from core.db import connect

    with connect() as conn:
        conn.execute(
            "UPDATE cases SET status='obsolete' WHERE project_id=? AND tp_id=?", (pid, "TP-1")
        )
    summary = {
        "total": 1,
        "done": 1,
        "status_counts": {"pass": 1},
        "results": [{"tc_no": "TP-1", "tp_id": "TP-1", "status": "pass"}],
    }
    store.record_execution(pid, summary, mode="full", source_kind="execute")
    rows = store.list_cases(pid, include_obsolete=True)
    assert rows[0]["last_result"] in (None, ""), "obsolete 行不应拿到执行结论"


# ---------------------------------------------------------------- B4 include_router 前缀
def test_router_prefix_from_include_router() -> None:
    """B4：单文件内 include_router(router, prefix=...) 的前缀被识别。"""
    tree = ast.parse("router = APIRouter()\napp.include_router(router, prefix='/api/v1')\n")
    assert fp_extract._router_prefix(tree) == "/api/v1"


def test_router_prefix_prefers_router_declaration() -> None:
    """APIRouter(prefix=...) 优先于 include_router（原有行为不变）。"""
    tree = ast.parse(
        "router = APIRouter(prefix='/own')\napp.include_router(router, prefix='/api/v1')\n"
    )
    assert fp_extract._router_prefix(tree) == "/own"


# ---------------------------------------------------------------- B6 门禁证据
def test_status_like_quantity_is_not_observable() -> None:
    """B6：纯数量/单位（200 条 / 500ms）不算状态码，不该成为可观测依据。"""
    # _STATUS_RE 本身不把带量词的数字当状态码（"返回"等词表命中另算，属另一维度）
    assert quality._STATUS_RE.search("200 条数据") is None
    assert quality._STATUS_RE.search("耗时 500ms") is None
    assert quality._STATUS_RE.search("404 个") is None
    # 真正的状态码仍匹配
    assert quality._STATUS_RE.search("返回 404") is not None
    assert quality._STATUS_RE.search("200，包含 id") is not None
    assert quality.observable_expect("返回 404，响应体为错误信息") is True


def test_gate_exposes_evidence_duplicate_stat() -> None:
    """B6：evidence 重复引用被计入统计（KPI 虚高可观测），但不改门禁判定。"""
    tps = [
        TestPoint(
            tp_id=f"TP-{i}",
            fp_contract_id="FP-1",
            category="normal",
            dimension="正常-可用性",
            expect="返回 200，包含 id",
            origin="rule",
            source="a.py",
            evidence=["a.py:10", "a.py:10"],  # 同一证据重复
        )
        for i in range(2)
    ]
    stats = quality.apply_gate(tps)
    assert stats["evidence_total"] == 4
    assert stats["evidence_unique"] == 1
    assert stats["evidence_duplicate"] == 3
    assert stats["gate_blocked"] == 0, "去重统计不影响门禁判定"


# ---------------------------------------------------------------- B3 path 泛提
def _sf(rel: str, text: str) -> SourceFile:
    return SourceFile(rel=rel, abspath=Path(rel), text=text)


def extract_pages(text: str):
    from core.enums import FType as _FType

    res = fp_extract.extract_functional_points(
        {"frontend/src/router/index.js": _sf("frontend/src/router/index.js", text)},
        include_business=False,
        extract_pages=True,
        extract_components=False,
    )
    return [fp for fp in res.functional_points if fp.ftype == _FType.PAGE.value]


def test_form_path_not_extracted_as_route() -> None:
    """B3：表单字段的 path: 不再被当成路由声明。"""
    # 两段相隔足够远，避免 form 关键字落进 /home 的前置窗口（窗口=160 字符）
    text = (
        "const form = { field: 'x', path: '/upload' }\n"
        + (" " * 200)
        + "\nconst routes = [{path: '/home'}]"
    )
    names = [fp.name for fp in extract_pages(text)]
    assert "/home" in names
    assert "/upload" not in names, "form 上下文里的 path 不是路由"


def test_mock_path_not_extracted_as_route() -> None:
    """B3：mock 配置里的 path: 不提为路由。"""
    text = "const mock = { url: '/x', path: '/fake' }"
    assert extract_pages(text) == []


# ---------------------------------------------------------------- B1/B2 点击副作用
def test_card_danger_hints_include_write_verbs() -> None:
    """B2：中文写动词进入危险词表，点前即拦截。"""
    for verb in ("提交", "保存", "发布", "新增", "创建", "上传"):
        assert runtime_ui._is_card_danger_label(f"{verb}按钮") is True


def test_trigger_js_excludes_danger_before_click() -> None:
    """B1：click-through 的触发器 JS 模板含危险词点前过滤。"""
    assert "__DANGER__" in runtime_ui._TRIGGER_SELECTORS_JS
    assert "danger.some" in runtime_ui._TRIGGER_SELECTORS_JS


def test_card_click_source_has_no_netloc_check() -> None:
    """B2：卡片熔断的写请求监听不再限定同源（跨域写也熔断）。"""
    src = Path(runtime_ui.__file__).read_text(encoding="utf-8")
    fn = (
        src[
            src.index("def _discover_card_routes") : src.index(
                "def _card_nav_decision", src.index("def _discover_card_routes")
            )
        ]
        if "def _card_nav_decision" in src[src.index("def _discover_card_routes") :]
        else src[src.index("def _discover_card_routes") :]
    )
    # 写请求判定块里不应再有 netloc 比较
    assert "urlparse(request.url).netloc" not in fn
