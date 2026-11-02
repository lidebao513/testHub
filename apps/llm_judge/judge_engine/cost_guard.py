"""裁判模型的成本护栏（与 `llm_budget.check_call` 同一套判定）。

为什么要单独有这一层：judge 引擎走的是**独立** OpenAI 客户端，不经
`requirement_analysis` / `ui_automation` 的调用链，历史上完全没有挂护栏。
而裁判是「n_runs × judge_models」的放大用法——`n_runs=3` + 2 个裁判模型，
一次评审就是 6 次调用；若模型已过免费期或额度耗尽，这些调用会**不报错地**
转成按量计费，只留在账单里。

判定顺序与主链路保持一致：
  白名单 → 登记册（开关 / 类型 / 到期 / 临近到期闸）→ 403 额度耗尽标记

未在登记册里的模型放行（与 `check_model_registry` 的既有约定一致：不在册
交给白名单与付费关键字判断），因此这里不会把未登记的模型一刀切拦掉。
"""

from __future__ import annotations


def reject_reason(model: str) -> str:
    """裁判模型准入判定：返回拒绝原因，空串表示放行。"""
    try:
        from apps.requirement_analysis import llm_budget
    except ImportError:
        # 纯引擎模式（无 Django app 依赖的单测/离线联调）不阻塞调用
        return ""
    cfg = llm_budget.load_settings()
    if not cfg.enforce:
        return ""
    return (
        llm_budget.check_model_allowed(model, cfg)
        or llm_budget.check_model_registry(model, cfg)
        or llm_budget.quota_reject_reason(model)
    )
