"""P2-10 · G-13 量级稳定性回归守护测试。

验证 `engine.magnitude_guard` 纯函数 + `merge_functional_points` 的 `element_count`
接线：基线健康、比例漂移告警、绝对值硬失败、零 FP 跳过，以及合并统计回写 magnitude。
"""

from __future__ import annotations

from engine.fp_merge import merge_functional_points
from engine.magnitude_guard import check_magnitude


# 基线常量（与 magnitude_guard 同源，复用于断言）
_FP_BASE = 520
_EL_BASE = 2138


def test_baseline_is_healthy():
    """真实观测基线（520 FP / 2138 元素）应判健康：ok 且非 warn。"""
    v = check_magnitude(_FP_BASE, _EL_BASE)
    assert v.ok is True
    assert v.warn is False
    assert v.note == "量级在健康区间"


def test_ratio_drift_warns():
    """元素数暴涨（520 FP / 10000 元素）→ 比例偏离基线 → warn=True（不阻断）。"""
    v = check_magnitude(_FP_BASE, 10_000)
    assert v.ok is True
    assert v.warn is True
    assert "偏离基线" in v.note


def test_absolute_cap_fails():
    """FP 超硬上限（6000）→ ok=False（绝对值失控，必须排查）。"""
    v = check_magnitude(6000, 20_000)
    assert v.ok is False
    assert v.warn is True
    assert "硬上限" in v.note


def test_zero_fp_skips():
    """无功能点（fp_count<=0）→ 跳过校验，不告警不失败。"""
    v = check_magnitude(0, 0)
    assert v.ok is True
    assert v.warn is False


def test_merge_wires_magnitude_stat():
    """合并传入 element_count 后，统计里应回写 magnitude 结论（不阻断合并）。"""

    class _FP:
        def __init__(self, name, file_path="src/a.py", ftype="page"):
            self.name = name
            self.file_path = file_path
            self.ftype = ftype

    base = [_FP("/a"), _FP("/b")]
    incoming: list[_FP] = []
    merged, stats = merge_functional_points(base, incoming, element_count=2138)
    assert len(merged) == 2
    assert "magnitude" in stats
    assert stats["magnitude"]["ok"] is True
    assert stats["magnitude"]["fp_count"] == 2


def test_merge_magnitude_drift_logged_not_blocking():
    """比例漂移时合并照常完成，仅在统计里标记 warn（绝不 raise）。"""

    class _FP:
        def __init__(self, name, file_path="src/a.py", ftype="page"):
            self.name = name
            self.file_path = file_path
            self.ftype = ftype

    base = [_FP(f"/p{i}") for i in range(520)]
    incoming = []
    merged, stats = merge_functional_points(base, incoming, element_count=50_000)
    assert len(merged) == 520  # 合并结果不受量级告警影响
    assert stats["magnitude"]["warn"] is True
