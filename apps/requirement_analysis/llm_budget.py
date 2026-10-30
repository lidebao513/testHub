"""Django 侧 LLM 成本护栏。

与 `services/testgen/core/llm_budget.py` 同构（两侧 venv 隔离，无法共享代码），
职责相同：在大模型调用前设三道闸，避免「免费额度耗尽后静默转按量计费」产生账单。

1. **模型门禁**：配置 `LLM_MODEL_ALLOWLIST` 时只允许清单内模型；未配置时用
   `LLM_PAID_MODEL_HINTS` 关键字（max/plus/pro/2.4t）拦付费档。
2. **单次输入预算**：估算输入 token 超 `LLM_CALL_MAX_INPUT_TOKENS` 直接拒绝。
3. **日累计熔断**：当日累计超 `LLM_DAILY_TOKEN_BUDGET` 后拒绝一切调用。

计数落 `data/llm_usage.json`，进程重启不清零。
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_PAID_HINTS = ("max", "plus", "pro", "2.4t")
_RETAIN_DAYS = 14
_LOCK = threading.Lock()


class BudgetExceeded(Exception):
    """超出成本预算——调用被主动拒绝，不是上游错误。"""


@dataclass
class BudgetDecision:
    """一次调用的准入结论。

    `scope` 决定调用方如何处理拒绝：
    - `model`：仅该模型不可用（命中付费关键字）→ 可继续试链上下一个模型；
    - `global`：预算类拒绝（输入过大 / 日累计到顶）→ 换模型一样超，必须整链停止。
    """

    allowed: bool
    reason: str = ""
    model: str = ""
    est_input_tokens: int = 0
    day_used: int = 0
    day_budget: int = 0
    scope: str = "model"


@dataclass
class BudgetSettings:
    enforce: bool = True
    allowlist: list[str] = field(default_factory=list)
    paid_hints: list[str] = field(default_factory=lambda: list(DEFAULT_PAID_HINTS))
    allow_paid: bool = False
    max_input_tokens: int = 24000
    max_output_tokens: int = 4096
    daily_token_budget: int = 300000
    usage_path: str = ""


def _split_csv(raw: str | None) -> list[str]:
    return [x.strip() for x in (raw or "").split(",") if x.strip()]


def _as_bool(raw: str | None, default: bool) -> bool:
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _as_int(raw: str | None, default: int) -> int:
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _default_usage_path() -> str:
    env = os.getenv("LLM_USAGE_PATH")
    if env:
        return env
    base = os.getenv("DJANGO_BASE_DIR") or os.getcwd()
    return str(Path(base) / "data" / "llm_usage.json")


def load_settings() -> BudgetSettings:
    """读取护栏配置（每次现读，便于测试与运行期热调）。"""
    return BudgetSettings(
        enforce=_as_bool(os.getenv("LLM_BUDGET_ENFORCE"), True),
        allowlist=[m.lower() for m in _split_csv(os.getenv("LLM_MODEL_ALLOWLIST"))],
        paid_hints=[h.lower() for h in _split_csv(os.getenv("LLM_PAID_MODEL_HINTS"))]
        or list(DEFAULT_PAID_HINTS),
        allow_paid=_as_bool(os.getenv("LLM_ALLOW_PAID_MODEL"), False),
        max_input_tokens=_as_int(os.getenv("LLM_CALL_MAX_INPUT_TOKENS"), 24000),
        max_output_tokens=_as_int(os.getenv("LLM_CALL_MAX_OUTPUT_TOKENS"), 4096),
        daily_token_budget=_as_int(os.getenv("LLM_DAILY_TOKEN_BUDGET"), 300000),
        usage_path=_default_usage_path(),
    )


def estimate_tokens(text: str) -> int:
    """粗估 token 数（不引入 tiktoken）。1 token ≈ 1.6 字符，偏保守。"""
    if not text:
        return 0
    return max(1, int(len(text) / 1.6))


def estimate_messages_tokens(messages: list[dict[str, Any]]) -> int:
    total = 0
    for msg in messages or []:
        content = msg.get("content")
        if isinstance(content, str):
            total += estimate_tokens(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    if part.get("type") == "image_url":
                        total += 1000
                    else:
                        total += estimate_tokens(str(part.get("text", "")))
        total += 4
    return total


def _read_usage(path: str) -> dict[str, Any]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_usage(path: str, data: dict[str, Any]) -> None:
    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, target)
    except OSError as exc:
        logger.warning("LLM 用量记账落盘失败（本次仅进程内计数）：%s", exc)


def _day_key(when: date | None = None) -> str:
    return (when or date.today()).isoformat()


def day_usage(path: str, day: str | None = None) -> dict[str, Any]:
    entry = _read_usage(path).get(day or _day_key()) or {}
    return entry if isinstance(entry, dict) else {}


def check_model_allowed(model: str, cfg: BudgetSettings) -> str:
    """模型门禁：返回拒绝原因，空串表示放行。"""
    name = (model or "").strip().lower()
    if not name:
        return "模型名为空"
    if cfg.allowlist:
        if name not in cfg.allowlist:
            return f"模型 {model} 不在 LLM_MODEL_ALLOWLIST 白名单内"
        return ""
    if cfg.allow_paid:
        return ""
    hit = next((h for h in cfg.paid_hints if h and h in name), "")
    if hit:
        return f"模型 {model} 命中付费关键字 '{hit}'（免费额度耗尽会静默转按量计费）"
    return ""


def check_call(
    model: str,
    messages: list[dict[str, Any]],
    *,
    cfg: BudgetSettings | None = None,
) -> BudgetDecision:
    """调用前准入检查。"""
    cfg = cfg or load_settings()
    est = estimate_messages_tokens(messages)

    reason = check_model_allowed(model, cfg)
    if reason:
        return BudgetDecision(False, reason, model, est, scope="model")

    if est > cfg.max_input_tokens:
        return BudgetDecision(
            False,
            f"输入估算 {est} token 超过单次上限 {cfg.max_input_tokens}",
            model,
            est,
            scope="global",
        )

    used = int(day_usage(cfg.usage_path).get("total") or 0)
    if used + est > cfg.daily_token_budget:
        return BudgetDecision(
            False,
            f"当日已用 {used} + 本次估算 {est} 超过日预算 {cfg.daily_token_budget}",
            model,
            est,
            used,
            cfg.daily_token_budget,
            scope="global",
        )
    return BudgetDecision(True, "", model, est, used, cfg.daily_token_budget)


def record_usage(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    *,
    channel: str = "",
    cfg: BudgetSettings | None = None,
) -> dict[str, Any]:
    """记一次成功调用（拿不到上游 usage 时由调用方传估算值）。"""
    cfg = cfg or load_settings()
    day = _day_key()
    total = max(0, int(prompt_tokens or 0)) + max(0, int(completion_tokens or 0))
    with _LOCK:
        data = _read_usage(cfg.usage_path)
        entry = data.get(day)
        if not isinstance(entry, dict):
            entry = {"total": 0, "calls": 0, "by_model": {}}
        entry["total"] = int(entry.get("total") or 0) + total
        entry["calls"] = int(entry.get("calls") or 0) + 1
        by_model = entry.get("by_model")
        if not isinstance(by_model, dict):
            by_model = {}
        key = f"{model}" + (f"@{channel}" if channel else "")
        by_model[key] = int(by_model.get(key) or 0) + total
        entry["by_model"] = by_model
        data[day] = entry
        cutoff = (date.today() - timedelta(days=_RETAIN_DAYS)).isoformat()
        data = {k: v for k, v in data.items() if k >= cutoff}
        _write_usage(cfg.usage_path, data)
    if total:
        logger.info(
            "LLM 用量 +%d token（模型 %s，通道 %s）；当日累计 %d/%d",
            total,
            model,
            channel or "-",
            int(entry.get("total") or 0),
            cfg.daily_token_budget,
        )
    return entry


def snapshot(cfg: BudgetSettings | None = None) -> dict[str, Any]:
    """当日用量快照。"""
    cfg = cfg or load_settings()
    entry = day_usage(cfg.usage_path)
    used = int(entry.get("total") or 0)
    return {
        "day": _day_key(),
        "used_tokens": used,
        "daily_budget": cfg.daily_token_budget,
        "calls": int(entry.get("calls") or 0),
        "by_model": entry.get("by_model") or {},
        "enforce": cfg.enforce,
        "allowlist": cfg.allowlist,
        "allow_paid": cfg.allow_paid,
        "remaining": max(0, cfg.daily_token_budget - used),
    }


def reset_today(cfg: BudgetSettings | None = None) -> None:
    """清零当日计数（人工解封用）。"""
    cfg = cfg or load_settings()
    with _LOCK:
        data = _read_usage(cfg.usage_path)
        data.pop(_day_key(), None)
        _write_usage(cfg.usage_path, data)
