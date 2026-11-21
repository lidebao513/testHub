"""T2 影响面单测。

背景：函数级收窄只保留「改动本体」，被改函数的调用方一律按「未变更」丢掉，
于是「改 A 崩 B」完全没有回归覆盖。影响面通道补的是「因 X 改动，验证 Y 未回归」。

覆盖：
- 按入口聚合（多调用点 → 1 条）、且已有功能点（本体已覆盖）的入口不重复产点；
- L2 数据契约：改动涉及的文案/字段被某入口引用 → 该入口判 data，不是 direct；
- 两条预算（单函数上限 / 整次上限）截断并如实计数；
- 无可达入口时 0 条，不编造；
- 测试点字段可直接驱动接口探测（area/method/expect/verify_layer/origin）。
"""

from pathlib import Path

from core.contracts import FunctionalPoint
from core.enums import FType, Tag, VerifyLayer
from engine import impact
from engine.scan import SourceFile


_SRC = """\
router = object()


def _helper(value):
    return value is not None or "未命名对话"


def orphan(value):
    return value


@router.post("/api/v1/chat")
def chat(payload):
    return _helper(payload)


@router.get("/api/v1/threads")
def list_threads():
    return {"title": _helper(None) or "未命名对话"}


@router.post("/api/v1/threads")
def create_thread(payload):
    return _helper(payload)


@router.delete("/api/v1/threads/{tid}")
def delete_thread(tid):
    return _helper(tid)
"""

_REL = "app/api.py"


def _line_of(text: str) -> int:
    needle = text.strip()
    for i, ln in enumerate(_SRC.splitlines(), 1):
        if ln.strip() == needle:
            return i
    raise AssertionError(text)


def _line_of_prefix(prefix: str) -> int:
    needle = prefix.strip()
    for i, ln in enumerate(_SRC.splitlines(), 1):
        if ln.strip().startswith(needle):
            return i
    raise AssertionError(prefix)


def _files() -> dict:
    return {_REL: SourceFile(rel=_REL, abspath=Path(_REL), text=_SRC)}


def _chat_fp() -> FunctionalPoint:
    return FunctionalPoint(
        fp_id="FP::chat",
        ftype=FType.API.value,
        file_path=_REL,
        name="POST /api/v1/chat",
        title="chat",
        description=f"chat() @ {_REL}:{_line_of_prefix('def chat(')}",
    )


def _helper_hunk() -> dict:
    return {_REL: [(_line_of('return value is not None or "未命名对话"'), 1)]}


def _plan(**kwargs) -> impact.ImpactPlan:
    return impact.build_impact_plan(_files(), _helper_hunk(), [_chat_fp()], **kwargs)


def test_aggregates_by_entry_and_skips_covered_entry() -> None:
    plan = _plan()

    paths = sorted(i.path for i in plan.items)
    assert paths == ["/api/v1/threads", "/api/v1/threads", "/api/v1/threads/{tid}"]
    # chat 已有功能点（本体用例已覆盖）→ 不作为影响面重复产出
    assert all(i.entry != "chat" for i in plan.items)
    # 4 个入口里 1 个已被本体覆盖，其余 3 个进影响面，覆盖率 3/3
    assert plan.reachable_entries == 3
    assert plan.coverage == 1.0
    assert all(i.anchor_fp_id == "FP::chat" for i in plan.items)
    assert plan.truncated == 0


def test_l2_literal_marks_data_contract_kind() -> None:
    plan = _plan()
    by_entry = {i.entry: i for i in plan.items}

    data = by_entry["list_threads"]  # 该入口自己也引用了「未命名对话」
    assert data.kind == impact.KIND_DATA
    assert "未命名对话" in data.literals
    assert data.tag == Tag.IMPACT_DATA.value
    assert data.dimension.startswith("回归-数据")

    direct = by_entry["create_thread"]
    assert direct.kind == impact.KIND_DIRECT
    assert direct.tag == Tag.IMPACT_DIRECT.value


def test_per_func_budget_truncates() -> None:
    plan = _plan(max_per_func=2)

    assert len(plan.items) == 2
    assert plan.truncated == 1  # 3 个待覆盖入口只出 2 条


def test_total_budget_truncates() -> None:
    plan = _plan(max_total=1)

    assert len(plan.items) == 1
    assert plan.truncated == 2


def test_no_reachable_entry_produces_nothing() -> None:
    hunks = {_REL: [(_line_of("return value"), 1)]}  # orphan() 没有任何调用方
    plan = impact.build_impact_plan(_files(), hunks, [_chat_fp()])

    assert plan.items == []
    assert plan.reachable_entries == 0
    assert plan.truncated == 0


def test_impact_test_points_are_probeable() -> None:
    tps = impact.impact_test_points(_plan())

    assert len(tps) == 3
    for tp in tps:
        assert tp.fp_contract_id == "FP::chat"
        assert tp.category == "正常"
        assert tp.origin == "impact"
        assert tp.verify_layer == VerifyLayer.INTERFACE.value
        assert tp.method in {"GET", "POST", "DELETE"}
        assert tp.area.startswith("/api/v1/threads")
        assert tp.expect.startswith("因 _helper")
        assert "未回归" in tp.expect
        assert tp.evidence and "1 跳" in tp.evidence[0]
