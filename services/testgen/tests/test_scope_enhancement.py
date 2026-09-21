"""scope 增强回归：业务函数（code 通道）应派生 正常/安全/边界/异常 四档测试点。

背景：纯 Python 代码仓库（如 Django 后端 apps/）被整仓识别为 business 类功能点，
原 `plan_of(business)` 仅返回「正常」一档，导致安全/边界/异常维度永不成点
（M1 真实联调时 1320 条 FP 全部 business、TP==FP 且全为「正常」即此缺口）。
"""

from core.contracts import FunctionalPoint
from core.enums import DEFAULT_SCOPE
from engine.tp_expand import ExpandContext, expand_functional_point, plan_of


def _biz_fp(fp_id: str = "FP-biz0001") -> FunctionalPoint:
    return FunctionalPoint(
        fp_id=fp_id,
        ftype="business",
        file_path="apps/x/services.py",
        name="do_something",
        title="示例业务函数",
    )


def test_business_plan_has_four_scopes():
    plan = plan_of(_biz_fp())
    cats = {c for c, _ in plan}
    assert cats == {"正常", "安全", "边界", "异常"}


def test_business_expand_unique_ids_and_unverified_flags():
    ctx = ExpandContext(scopes=set(DEFAULT_SCOPE))
    tps = expand_functional_point(_biz_fp(), ctx)
    assert len(tps) == 4
    ids = [tp.tp_id for tp in tps]
    assert len(set(ids)) == len(ids), "tp_id 碰撞"
    by_cat = {tp.category: tp for tp in tps}
    assert by_cat["正常"].unverified is False
    # 业务函数三档维度无运行时 harness 可自动验证 → 诚实 SKIPPED
    assert by_cat["安全"].unverified is True
    assert by_cat["边界"].unverified is True
    assert by_cat["异常"].unverified is True
    for tp in tps:
        assert tp.expect, "expect 文案不应为空"


def test_business_scope_filter_respected():
    ctx = ExpandContext(scopes={"正常"})
    tps = expand_functional_point(_biz_fp(), ctx)
    assert all(tp.category == "正常" for tp in tps)
