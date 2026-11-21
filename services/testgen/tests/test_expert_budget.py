"""专家审阅的成本/时长护栏单测。

背景：PageExpert 逐页调大模型，实测单页 50~170 秒（model_chain 降级会叠加多次超时重试）。
39 页串行 = 半小时以上，面板只看得到「一直没结束」。护栏两层：
1. 页数上限 + 总时间预算（默认 3 页 / 90 秒）；
2. 页前把单页调用超时压进剩余预算，避免「页前检查通过、单页又跑 3 分钟」。
"""

from __future__ import annotations

import time
from dataclasses import fields
from types import SimpleNamespace

from core.config import Settings
from engine import pipeline


def _default_of(name: str):
    return next(f.default for f in fields(Settings) if f.name == name)


def test_expert_budget_defaults_are_small():
    """默认封顶必须足够小：勾了专家也只能多花 ~2~3 分钟，不能回到「半小时」。"""
    assert _default_of("expert_max_pages") <= 3
    assert _default_of("expert_time_budget_sec") <= 90


def test_plan_expert_page_stops_when_budget_exhausted():
    expert = SimpleNamespace(options=SimpleNamespace(timeout=60))
    started = time.monotonic() - 120  # 已用 120s > 预算 90s
    assert pipeline._plan_expert_page(expert, started, 90) is True


def test_plan_expert_page_clamps_timeout_to_remaining_budget():
    """剩余预算只有 10s 时，本页调用超时必须压到 10s，而不是继续用 60s。"""
    expert = SimpleNamespace(options=SimpleNamespace(timeout=60))
    started = time.monotonic() - 80
    assert pipeline._plan_expert_page(expert, started, 90) is False
    assert expert.options.timeout == 10


def test_plan_expert_page_keeps_timeout_when_budget_is_ample():
    expert = SimpleNamespace(options=SimpleNamespace(timeout=45))
    assert pipeline._plan_expert_page(expert, time.monotonic(), 90) is False
    assert expert.options.timeout == 45


def test_plan_expert_page_without_budget_is_noop():
    """预算为 0 表示「不限时」：既不停，也不改 timeout。"""
    expert = SimpleNamespace(options=SimpleNamespace(timeout=60))
    assert pipeline._plan_expert_page(expert, time.monotonic() - 9999, 0) is False
    assert expert.options.timeout == 60


def test_stage_with_detail_encodes_expert_page():
    """`expert_review:2/3` 让面板能显示「第 2/3 页」，不再停在一个不动的阶段名上。"""
    from service.app import _stage_with_detail

    assert _stage_with_detail("expert_review", {"page": 2, "total": 3}) == "expert_review:2/3"
    assert _stage_with_detail("expert_review", {}) == "expert_review"
    assert _stage_with_detail("runtime_ui", {"base_url": "http://x"}) == "runtime_ui"
