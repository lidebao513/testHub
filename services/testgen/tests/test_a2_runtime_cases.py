"""A2 · 运行时发现细节落进用例正文 + 「地址通道」端到端测试。

验收口径（阶段一 A2）：
- 只给地址（不带 `--path`）能产出**非空**用例；
- 用例 `doc_steps` 里出现**真实路由**与**真实元素**（而非通用模板）；
- `steps[0].runtime` 携带机器可读细节且**不含凭证**；
- 代码 + 地址并用时功能点按 `(ftype, name)` 合并，运行时优先。
"""

from __future__ import annotations

import pytest

from engine import case_gen, pipeline, runtime_ui


def _fake_result() -> runtime_ui.RuntimeUiResult:
    """构造一份「像真实发现产物」的结果（不跑浏览器）。"""
    return runtime_ui.RuntimeUiResult(
        base_url="http://host:8090",
        logged_in=True,
        discovered_routes=["/pc/tasks", "/pc/email"],
        pages=[
            runtime_ui.UiPage(
                url="http://host:8090/pc/tasks",
                path="/pc/tasks",
                title="任务中心",
                reachable=True,
                console_errors=[],
                elements=[
                    runtime_ui.UiElement("button.new", "button", "新建任务", True),
                    runtime_ui.UiElement("input.q", "input", "搜索任务", True),
                    runtime_ui.UiElement("div.hidden", "button", "隐藏按钮", False),
                ],
            ),
            runtime_ui.UiPage(
                url="http://host:8090/pc/email",
                path="/pc/email",
                title="邮件",
                reachable=True,
                console_errors=["TypeError: x is not a function"],
                elements=[runtime_ui.UiElement("nav.inbox", "nav", "收件箱", True)],
            ),
            runtime_ui.UiPage(url="http://host:8090/pc/dead", path="/pc/dead", reachable=False),
        ],
    )


def _patch_discover(monkeypatch, result: runtime_ui.RuntimeUiResult) -> None:
    monkeypatch.setattr(runtime_ui, "discover_ui", lambda *a, **k: result)


# ---------------------------------------------------------------- 索引
def test_runtime_index_only_keeps_reachable_pages():
    index = runtime_ui.to_runtime_index(_fake_result())
    assert set(index) == {"/pc/tasks", "/pc/email"}
    assert index["/pc/tasks"].title == "任务中心"
    assert index["/pc/tasks"].element_phrases() == ["按钮「新建任务」", "输入框「搜索任务」"]


def test_runtime_index_of_none_is_empty():
    assert runtime_ui.to_runtime_index(None) == {}


def test_element_phrases_fold_beyond_limit():
    info = runtime_ui.RuntimePageInfo(
        path="/p",
        elements=[{"kind": "button", "text": f"b{i}", "visible": True} for i in range(8)],
    )
    phrases = info.element_phrases(limit=3)
    assert phrases[:3] == ["按钮「b0」", "按钮「b1」", "按钮「b2」"]
    assert phrases[3] == "等 8 个元素"


# ---------------------------------------------------------------- 用例富化
def _tp(area: str, method: str = "PAGE", layer: str = "UI") -> dict[str, object]:
    return {
        "tp_id": "TP-00000001",
        "fp_contract_id": "FP-00000001",
        "category": "正常",
        "title": f"[正常] 页面 {area}",
        "module": "pc",
        "source": f"runtime:http://host:8090{area}",
        "method": method,
        "area": area,
        "expect": "页面正常渲染",
        "dimension": "页面/路由可达",
        "verify_layer": layer,
    }


def test_page_case_doc_steps_carry_real_url_and_elements():
    index = runtime_ui.to_runtime_index(_fake_result())
    case = case_gen.build_case(_tp("/pc/tasks"), runtime_index=index)
    assert case is not None
    text = " ".join(str(s["desc"]) for s in case.doc_steps)
    assert "http://host:8090/pc/tasks" in text
    assert "任务中心" in text
    assert "新建任务" in text
    assert "搜索任务" in text
    # 机器步携带结构化细节
    payload = case.steps[0]["runtime"]
    assert payload["path"] == "/pc/tasks"
    assert payload["console_error_baseline"] == 0
    assert [e["text"] for e in payload["elements"]] == ["新建任务", "搜索任务"]
    assert "隐藏按钮" not in str(payload), "不可见元素不应进入机器步"


def test_page_case_reports_console_error_baseline():
    index = runtime_ui.to_runtime_index(_fake_result())
    case = case_gen.build_case(_tp("/pc/email"), runtime_index=index)
    assert case is not None
    assert case.steps[0]["runtime"]["console_error_baseline"] == 1
    assert any("基线 1 条" in str(s["desc"]) for s in case.doc_steps)


def test_interaction_case_doc_steps_list_real_elements():
    index = runtime_ui.to_runtime_index(_fake_result())
    case = case_gen.build_case(_tp("/pc/tasks", method="UI"), runtime_index=index)
    assert case is not None
    text = " ".join(str(s["desc"]) for s in case.doc_steps)
    assert "新建任务" in text
    assert "不新增控制台错误" in text


def test_case_without_runtime_detail_keeps_legacy_text():
    """回归：无运行时细节时，正文必须与从前逐字一致（不影响代码通道）。"""
    case = case_gen.build_case(_tp("/pc/tasks"))
    assert case is not None
    assert case.steps[0].get("runtime") is None
    assert "打开页面：/pc/tasks" in case.doc_steps[1]["desc"]


# ---------------------------------------------------------------- 地址通道端到端
def _url_only_options() -> pipeline.PipelineOptions:
    opts = pipeline.default_options("")
    opts.persist = False
    opts.target_req.enabled = True
    opts.target_req.base_url = "http://host:8090"
    opts.target_req.login_user = "u@x.com"
    opts.target_req.login_password = "Tp123456"
    return opts


@pytest.mark.live_llm
def test_url_only_channel_produces_cases_end_to_end(fresh_db, monkeypatch):
    """**核心验收**：不带 --path、只给地址，也能产出非空用例且正文含真实路由。"""
    _patch_discover(monkeypatch, _fake_result())
    result = pipeline.run_pipeline(_url_only_options())

    assert result.source_kind == "url"
    assert result.counts["files"] == 0
    assert result.counts["functional_points"] > 0
    assert result.counts["cases"] > 0, "只给地址必须能产出用例"
    assert result.counts["cases_with_runtime_detail"] > 0
    assert result.counts["runtime_ui_routes"] == 2
    assert result.traceability["orphan_count"] == 0
    assert result.traceability["uncovered_count"] == 0

    blob = " ".join(str(s["desc"]) for c in result.cases for s in c.doc_steps)
    assert "/pc/tasks" in blob
    assert "新建任务" in blob


def test_missing_both_sources_fails_fast(fresh_db):
    """既无代码目录又无被测地址 → 入口明确报错，不做一次「什么都没分析」的空跑。"""
    opts = pipeline.default_options("")
    opts.persist = False
    try:
        pipeline.run_pipeline(opts)
    except Exception as exc:  # noqa: BLE001 - 断言异常文案，需按业务异常读取 message
        assert "缺少被测来源" in str(getattr(exc, "message", exc))
    else:
        raise AssertionError("缺少来源时必须报错")


def test_bad_local_path_still_fails_fast(fresh_db):
    opts = pipeline.default_options("definitely/not/here")
    opts.persist = False
    try:
        pipeline.run_pipeline(opts)
    except Exception as exc:  # noqa: BLE001 - 同上
        assert "被测目录不存在" in str(getattr(exc, "message", exc))
    else:
        raise AssertionError("给了不存在的目录必须报错")


@pytest.mark.live_llm
def test_runtime_failure_does_not_block_code_channel(fresh_db, sample_repo, monkeypatch):
    """运行时发现失败只记错误，不阻断代码通道（设计 §10）。"""
    from core.errors import EngineError

    def raise_engine_error(*_a: object, **_k: object) -> None:
        raise EngineError("浏览器启动失败")

    monkeypatch.setattr(runtime_ui, "discover_ui", raise_engine_error)
    opts = pipeline.default_options(str(sample_repo))
    opts.persist = False
    opts.target_req.enabled = True
    opts.target_req.base_url = "http://host:8090"
    result = pipeline.run_pipeline(opts)
    assert result.source_kind == "code+url"
    assert any("运行时 UI 发现失败" in e for e in result.errors)
    assert result.counts["cases"] > 0, "代码通道必须照常产出用例"


@pytest.mark.live_llm
def test_code_plus_url_merges_runtime_functional_points(fresh_db, sample_repo, monkeypatch):
    """代码 + 地址并用：运行时功能点并入静态集合，同名（页面路径）由运行时覆盖。"""
    _patch_discover(monkeypatch, _fake_result())
    opts = pipeline.default_options(str(sample_repo))
    opts.persist = False
    opts.target_req.enabled = True
    opts.target_req.base_url = "http://host:8090"
    result = pipeline.run_pipeline(opts)

    assert result.source_kind == "code+url"
    assert result.counts["runtime_ui_fp_added"] >= 1
    assert any("运行时补入" in n for n in result.notes)
    assert result.counts["cases_with_runtime_detail"] > 0
