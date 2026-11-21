"""UI 用例「生成 → 执行」跨模块契约测试（覆盖今日 A/B/C/D 四批交汇处）。

背景：A（元素断言）、B（路由绑定）、C（面板坐标）、D（卡片导航）各模块单测全绿，
但生成侧 `case_gen.build_case` 产出的 `steps[0]` 是否被执行侧 `ui_executor`
正确消费，此前没有闭环测试——批次 C 正是踩过「元素级面板 selector 被整页
design_assertions 挤掉」的字段漂移坑。本文件用**真实生成产物**喂给执行侧解析器，
锁死四类用例的目标地址 / 断言 / 重放坐标契约。
"""

from __future__ import annotations

from typing import Any

from engine import case_gen, runtime_ui, ui_executor
from engine.ui_executor import UiExecOptions


def _tp(area: str, method: str = "PAGE", layer: str = "UI") -> dict[str, Any]:
    return {
        "tp_id": "TP-00000001",
        "fp_contract_id": "FP-00000001",
        "category": "正常",
        "title": f"[正常] {area}",
        "module": "pc",
        "source": f"runtime:http://host:8090{area}",
        "method": method,
        "area": area,
        "expect": "页面正常渲染，元素可见",
        "dimension": "页面/路由可达",
        "verify_layer": layer,
    }


def _route_index() -> dict[str, runtime_ui.RuntimePageInfo]:
    return {
        "/pc/tasks": runtime_ui.RuntimePageInfo(
            path="/pc/tasks",
            url="http://host:8090/pc/tasks",
            title="任务中心",
            elements=[
                {"selector": "button.new", "kind": "button", "text": "新建任务", "visible": True},
            ],
            console_errors=[],
        )
    }


def _panel_index(sig: str = "15条/页") -> dict[str, runtime_ui.RuntimePageInfo]:
    return {
        f"area::{sig}": runtime_ui.RuntimePageInfo(
            path=f"area::{sig}",
            url=f"area::{sig}",
            title="面板",
            elements=[
                {"selector": "button.dl", "kind": "button", "text": "下载", "visible": True},
            ],
            console_errors=[],
            parent_url="http://host:8090/home",
            nav_path=["测试用例", "面板入口"],
        )
    }


_OPTS = UiExecOptions(base_url="http://host:8090")


# ============================================================================
# 1. 页面级路由用例（批次 B）：真实 URL + 默认断言
# ============================================================================
def test_route_page_case_target_url_and_assertions() -> None:
    case = case_gen.build_case(_tp("/pc/tasks"), runtime_index=_route_index())
    assert case is not None
    step = case.steps[0]
    assert ui_executor.target_url(step, _OPTS) == "http://host:8090/pc/tasks"
    specs = ui_executor.assertions_of(step)
    kinds = {s["kind"] for s in specs}
    assert ui_executor.ASSERT_PAGE_RENDERED in kinds
    assert ui_executor.ASSERT_CONSOLE_WITHIN_BASELINE in kinds
    # 生成侧 design_assertions 已给整页元素断言 → 执行侧显式断言照单全收
    assert any(s.get("selector") == "button.new" for s in specs)


# ============================================================================
# 2. 元素级路由用例（批次 A）：path 带 selector → 执行侧补元素断言
# ============================================================================
def test_element_route_case_selector_reaches_executor() -> None:
    """元素级用例如实把发现阶段抓到的 selector 交给执行器，杜绝「元素删了还 PASS」。"""
    area = "/pc/tasks#button:新建任务|button.new"
    case = case_gen.build_case(_tp(area, method="UI"), runtime_index={})
    assert case is not None
    step = case.steps[0]
    # 元素级 path 以真实路由开头（`/pc/tasks#...`）：target_url 拼 base_url 后，
    # 浏览器把 `#...|selector` 当 fragment 忽略，实际导航到 /pc/tasks；
    # 元素定位不靠 URL 而靠断言 selector（下一行验证）。
    assert ui_executor.target_url(step, _OPTS).startswith("http://host:8090/pc/tasks")
    specs = ui_executor.assertions_of(step)
    assert any(
        s["kind"] == ui_executor.ASSERT_ELEMENT_VISIBLE and s.get("selector") == "button.new"
        for s in specs
    ), "批次 A：元素 selector 必须进入机器断言"


# ============================================================================
# 3. 页面级面板用例（批次 C）：坐标被 panel_target 解析
# ============================================================================
def test_page_panel_case_replay_coordinates_reach_executor() -> None:
    index = _panel_index()
    case = case_gen.build_case(_tp("area::15条/页"), runtime_index=index)
    assert case is not None
    step = case.steps[0]
    target = ui_executor.panel_target(step)
    assert target is not None
    assert target["signature"] == "15条/页"
    assert target["parent_url"] == "http://host:8090/home"
    assert target["nav_path"] == ["测试用例", "面板入口"]
    # 面板签名断言由执行器 _execute_panel 动态注入（不在 assertions_of 里），
    # 这里验证生成产物满足 _execute_panel 的入口条件：坐标齐全、目标被识别为面板。
    assert ui_executor.panel_target(step) is not None  # execute() 据此走面板分支
    assert ui_executor.ASSERT_PANEL_SIGNATURE == "panel_signature"


# ============================================================================
# 4. 元素级面板用例（批次 A+C 交汇）：坐标 + 元素 selector 都不丢
# ============================================================================
def test_element_panel_case_keeps_both_nav_coords_and_element_selector() -> None:
    """最易漂移的契约：精简 runtime 给坐标，path 给元素 selector，两者缺一不可。"""
    area = "area::15条/页#button:下载导入模板|main.el-main > button.dl"
    case = case_gen.build_case(_tp(area, method="UI"), runtime_index=_panel_index())
    assert case is not None
    step = case.steps[0]
    # 坐标：panel_target 必须能从元素级复合 path 里只取签名主键
    target = ui_executor.panel_target(step)
    assert target is not None
    assert target["signature"] == "15条/页", "签名不能混入元素提示"
    assert target["parent_url"] == "http://host:8090/home"
    # 元素：精简 runtime 不挂整页断言，selector 靠 path 兜底补上
    specs = ui_executor.assertions_of(step)
    assert any(
        s["kind"] == ui_executor.ASSERT_ELEMENT_VISIBLE
        and s.get("selector") == "main.el-main > button.dl"
        for s in specs
    ), "批次 C 的精简 runtime 不能挤掉批次 A 的元素 selector 断言"


def test_element_panel_case_does_not_carry_whole_page_elements() -> None:
    """元素级面板的 runtime 是精简坐标，不含整页 elements（否则整页断言淹没特定元素）。"""
    area = "area::15条/页#button:下载|main button.dl"
    case = case_gen.build_case(_tp(area, method="UI"), runtime_index=_panel_index())
    assert case is not None
    runtime = case.steps[0].get("runtime") or {}
    assert "elements" not in runtime, "精简 runtime 不应挂整页元素清单"
    assert runtime.get("parent_url") == "http://host:8090/home"


# ============================================================================
# 5. 无路由证据的文件级用例（批次 B）：不臆造 URL，执行侧诚实 skip
# ============================================================================
def test_unrouted_file_case_yields_no_target_and_no_false_pass() -> None:
    """文件级屏幕无真实路由：生成侧不编造 URL，执行侧目标为空 → skip 而非假通过。"""
    case = case_gen.build_case(_tp("src/components/Foo.vue", method="PAGE"), runtime_index={})
    assert case is not None
    step = case.steps[0]
    assert ui_executor.target_url(step, _OPTS) == ""
    assert ui_executor.panel_target(step) is None


# ============================================================================
# 6. 显式断言优先：生成侧显式断言不被执行侧兜底改写
# ============================================================================
def test_explicit_assertions_survive_full_pipeline_contract() -> None:
    """生成产物里已有显式断言 → 执行侧逐字采用，不追加兜底元素断言。"""
    case = case_gen.build_case(_tp("/pc/tasks"), runtime_index=_route_index())
    assert case is not None
    step = case.steps[0]
    explicit = [
        {"kind": ui_executor.ASSERT_ELEMENT_VISIBLE, "selector": "button.new", "text": "特定"}
    ]
    step["assertions"] = explicit
    assert ui_executor.assertions_of(step) == explicit


# ============================================================================
# 7. 文档步骤与机器步一致性：正文写的元素 = 机器断言的元素
# ============================================================================
def test_doc_steps_and_machine_assertions_agree_on_element() -> None:
    """正文（给人看）与 steps[0]（给机器执行）指向同一个真实元素，避免「文档说 A、断言查 B」。"""
    case = case_gen.build_case(
        _tp("/pc/tasks#button:新建任务|button.new", method="UI"), runtime_index={}
    )
    assert case is not None
    doc_blob = " ".join(str(s.get("desc", "")) for s in case.doc_steps)
    assert "新建任务" in doc_blob
    specs = ui_executor.assertions_of(case.steps[0])
    assert any(s.get("selector") == "button.new" for s in specs)


# ============================================================================
# E2/A4：用例点名的元素不受 20 条截断
# ============================================================================
def test_named_element_beyond_truncation_still_asserted() -> None:
    """用例标题/期望点名的元素即使排在发现顺序第 21+，也必须生成断言。"""
    elements = [
        {"selector": f"button.e{i}", "kind": "button", "text": f"普通{i}", "visible": True}
        for i in range(24)
    ]
    elements.append(
        {"selector": "button.submit", "kind": "button", "text": "提交订单", "visible": True}
    )
    info = runtime_ui.RuntimePageInfo(path="/checkout", elements=elements, console_errors=[])

    specs = case_gen.design_assertions(info, named_text="应显示提交订单按钮")
    selectors = [
        s.get("selector") for s in specs if s.get("kind") == ui_executor.ASSERT_ELEMENT_VISIBLE
    ]
    assert "button.submit" in selectors, "点名的第 25 个元素被截断了"
    assert len(selectors) == case_gen.MAX_RUNTIME_ELEMENTS_IN_STEP, "仍受总量上限约束"


def test_unnamed_elements_keep_discovery_order() -> None:
    """无点名元素时保持原发现顺序（不引入不稳定重排）。"""
    elements = [
        {"selector": f"button.e{i}", "kind": "button", "text": "", "visible": True}
        for i in range(25)
    ]
    info = runtime_ui.RuntimePageInfo(path="/x", elements=elements)
    specs = case_gen.design_assertions(info, named_text="页面正常渲染")
    selectors = [
        s.get("selector") for s in specs if s.get("kind") == ui_executor.ASSERT_ELEMENT_VISIBLE
    ]
    assert selectors == [f"button.e{i}" for i in range(case_gen.MAX_RUNTIME_ELEMENTS_IN_STEP)]


# ============================================================================
# E2/A5：控制台基线按文本白名单同期抵扣
# ============================================================================
def test_console_baseline_errors_filtered_by_text() -> None:
    """发现阶段的已知错误文本被抵扣，只把新增错误计入。"""
    # 2 条里 known-error 被基线抵扣，只剩 1 条新增 → slack=1 时通过
    ok, detail = ui_executor._check_console(
        ["known-error", "new-error"],
        baseline=1,
        slack=1,
        baseline_errors=["known-error"],
    )
    assert ok is True, detail
    assert "新增控制台错误 1 条" in detail  # 证明 known-error 已被抵扣，只数新增


def test_console_all_known_errors_pass_with_zero_slack() -> None:
    """全是发现阶段已知错误（含重复次数不超基线）时，slack=0 也通过。"""
    ok, detail = ui_executor._check_console(
        ["known-error", "known-error", "known-2"],
        baseline=3,
        slack=0,
        baseline_errors=["known-error", "known-error", "known-2"],
    )
    assert ok is True, detail
    assert "新增控制台错误 0 条" in detail


def test_console_new_error_beyond_slack_fails() -> None:
    """新增错误超过 slack → FAIL（即使发现阶段有同名已知错误）。"""
    ok, detail = ui_executor._check_console(
        ["known-error", "new-error", "another-new"],
        baseline=1,
        slack=0,
        baseline_errors=["known-error"],
    )
    assert ok is False
    assert "新增控制台错误 2 条" in detail


def test_console_int_baseline_still_works() -> None:
    """旧产物传 int baseline（无 baseline_errors）行为逐字不变。"""
    assert ui_executor._check_console(["boom"], baseline=1, slack=0)[0] is True
    assert ui_executor._check_console(["boom"], baseline=0, slack=0)[0] is False


def test_design_assertions_emits_baseline_errors() -> None:
    """生成侧把已知错误文本写进断言，供执行侧按文本抵扣。"""
    info = runtime_ui.RuntimePageInfo(path="/x", console_errors=["known-1", "known-2"], elements=[])
    specs = case_gen.design_assertions(info)
    console_spec = next(s for s in specs if s["kind"] == ui_executor.ASSERT_CONSOLE_WITHIN_BASELINE)
    assert console_spec["baseline_errors"] == ["known-1", "known-2"]
